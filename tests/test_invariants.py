"""Stage 3 exit test: boundary invariants + scrubber leakage.

Fabricated edges, undeclared types, and span-less edges are rejected/demoted; truncated JSON is
rejected with no partial write; a seeded secret never appears in any text leaving the scrubber.
"""
from __future__ import annotations

import pytest

from kg_engine.boundary import WritePayload, merge_results_into_nodes, validate_payload
from kg_engine.model import Disposition
from kg_engine.scrub import Scrubber

SRC = ("A compression grounds the claims beneath it. Betweenness is confounded by the generality "
       "confound. Heat flows from hot to cold.")


def _by_target(results):
    return {r.item.target: r for r in results if r.kind == "edge"}


def test_span_present_edge_accepted(pack):
    res = validate_payload(
        {"edges": [{"source": "compression", "target": "claim", "relation": "grounds",
                    "span": "A compression grounds the claims beneath it", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["claim"]
    assert e.disposition == Disposition.ACCEPTED, e


def test_fabricated_span_rejected(pack):
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds",
                    "span": "unicorns cause gravity", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.REJECTED and e.reason == "span-not-in-source"
    assert e.retryable is False  # semantic outcome


def test_spanless_edge_rejected(pack):
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.REJECTED and e.reason == "no-supporting-span"


def test_undeclared_edge_type_quarantined(pack):
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "smells_like",
                    "span": "Heat flows from hot to cold", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.QUARANTINED and "undeclared-edge-type" in e.reason


def test_undeclared_node_type_quarantined(pack):
    res = validate_payload(
        {"nodes": [{"label": "Weird", "node_type": "banana"}]}, pack=pack, source_text=SRC)
    n = next(r for r in res if r.kind == "node")
    assert n.disposition == Disposition.QUARANTINED and "undeclared-node-type" in n.reason


def test_forged_verdict_stripped(pack):
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds",
                    "span": "Heat flows from hot to cold", "authored_by": "human",
                    "epistemic_state": "grounded"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.DEMOTED
    assert "forged-verdict-stripped" in e.reason and "human-claim-stripped" in e.reason
    assert e.item.epistemic_state.value == "unverified"
    assert e.item.authored_by.value == "agent"


def test_deterministic_authorship_claim_does_not_bypass_span_present(pack):
    # §1.5 anti-bypass: a write can't dodge span verification by self-declaring `deterministic`.
    # The claim is demoted to `agent`, so a span-less "deterministic" edge is REJECTED like any other.
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds", "authored_by": "deterministic"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.REJECTED and e.reason == "no-supporting-span"
    assert e.item.authored_by.value == "agent"  # the deterministic claim was stripped

    # a fabricated span doesn't slip through under a deterministic claim either
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds",
                    "authored_by": "deterministic", "span": "unicorns cause gravity"}]},
        pack=pack, source_text=SRC)
    assert _by_target(res)["b"].disposition == Disposition.REJECTED

    # but a deterministic claim with a REAL verifying span is written — demoted to agent
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds",
                    "authored_by": "deterministic", "span": "Heat flows from hot to cold"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.DEMOTED and "deterministic-claim-stripped" in e.reason
    assert e.item.authored_by.value == "agent"


def test_truncated_payload_rejected_no_partial_write(pack):
    # `complete: false` marks a streamed/truncated payload -> reject whole, no partial write
    res = validate_payload(
        {"complete": False,
         "edges": [{"source": "a", "target": "b", "relation": "grounds",
                    "span": "A compression grounds the claims beneath it", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    assert len(res) == 1 and res[0].disposition == Disposition.REJECTED
    assert res[0].reason == "truncated-payload" and res[0].retryable is True
    assert not merge_results_into_nodes(res)  # nothing written


def test_schema_invalid_rejected():
    res = validate_payload({"edges": [{"source": "a"}]}, source_text=SRC)  # missing required fields
    assert res[0].disposition == Disposition.REJECTED and "schema-invalid" in res[0].reason


def test_dedup_single_canonical_edge(pack):
    payload = {"edges": [
        {"source": "a", "target": "b", "relation": "grounds",
         "span": "Heat flows from hot to cold", "authored_by": "agent"},
        {"source": "a", "target": "b", "relation": "grounds",
         "span": "Heat flows from hot to cold", "authored_by": "agent"},
    ]}
    res = validate_payload(payload, pack=pack, source_text=SRC)
    edges = [r for r in res if r.kind == "edge"]
    assert any("deduped" in r.reason for r in edges)
    nodes = merge_results_into_nodes(res)
    assert sum(len(n.edges) for n in nodes.values()) == 1  # one canonical edge


# ---- scrubber leakage (egress, §1.9) -------------------------------------

def test_secret_never_leaves_scrubber():
    secret = "sk-abcd1234efgh5678ijkl9012"
    aws = "AKIAIOSFODNN7EXAMPLE"
    text = f"the api_key={secret} and aws key {aws} relate to entropy"
    for sens in ("low", "medium", "high"):
        scrubbed, mapping = Scrubber(sens).scrub(text)
        assert Scrubber.leaks(scrubbed, [secret, aws]) == [], (sens, scrubbed)
        # consistent placeholders preserve structure and restore exactly
        assert Scrubber.restore(scrubbed, mapping) == text


def test_pii_scrubbed_per_sensitivity():
    text = "Contact jane.doe@example.com about Alan Turing's proof."
    low, _ = Scrubber("low").scrub(text)
    assert "jane.doe@example.com" in low  # low = secrets only, email survives
    med, mp = Scrubber("medium").scrub(text)
    assert "jane.doe@example.com" not in med and Scrubber.restore(med, mp) == text
    high, hp = Scrubber("high").scrub(text)
    assert "Alan Turing" not in high  # person heuristic active at high


def test_consistent_placeholders_preserve_relation():
    text = "Alice Smith argues with Bob Jones; Alice Smith later agrees."
    scrubbed, mapping = Scrubber("high").scrub(text)
    # the same entity gets the same placeholder both times (relational structure survives)
    assert scrubbed.count("⟦PERSON:1⟧") == 2
    assert Scrubber.restore(scrubbed, mapping) == text


def test_write_payload_pydantic_roundtrip():
    wp = WritePayload.model_validate({"nodes": [], "edges": []})
    assert wp.complete is True


def test_paraphrase_is_fabrication(pack):
    # §1.5: paraphrasing IS fabrication. The source says "A compression grounds the claims beneath
    # it"; a same-meaning paraphrase in different words must NOT verify (tests-3 — the prior test only
    # used unrelated text, never an actual paraphrase).
    res = validate_payload(
        {"edges": [{"source": "compression", "target": "claim", "relation": "grounds",
                    "span": "a summary underpins the assertions below it", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["claim"]
    assert e.disposition == Disposition.REJECTED and e.reason == "span-not-in-source"


# ---- source-aware span verification (R4) ---------------------------------

def test_source_aware_named_file_split(tmp_path, pack):
    """With a SourceSet, a span present in file B but attributed to file A is REJECTED
    `span-not-in-named-source`; the same span attributed to B (or unattributed) is ACCEPTED."""
    from kg_engine.sources import SourceSet
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.md").write_text("A compression grounds the claims beneath it.\n", encoding="utf-8")
    (d / "b.md").write_text("Betweenness is confounded by the generality confound.\n", encoding="utf-8")
    sources = SourceSet(d)

    def _run(source_file):
        return _by_target(validate_payload(
            {"edges": [{"source": "betweenness", "target": "gc", "relation": "confounded_by",
                        "span": "Betweenness is confounded by the generality confound",
                        "source_file": source_file, "authored_by": "agent"}]},
            pack=pack, source_text=sources.concat, sources=sources))["gc"]

    assert _run("a.md").reason == "span-not-in-named-source"   # present in B, attributed to A
    assert _run("b.md").disposition == Disposition.ACCEPTED    # attributed to its real file
    assert _run("").disposition == Disposition.ACCEPTED        # unattributed -> any declared source


def test_sources_none_ignores_source_file_and_uses_blob(pack):
    """`sources=None` preserves the exact single-blob behavior: the edge's `source_file` is irrelevant,
    verification is against `source_text`. Guards every existing direct validate_payload call site."""
    res = _by_target(validate_payload(
        {"edges": [{"source": "compression", "target": "claim", "relation": "grounds",
                    "span": "A compression grounds the claims beneath it",
                    "source_file": "whatever-unknown.md", "authored_by": "agent"}]},
        pack=pack, source_text=SRC))  # no sources= -> blob path
    assert res["claim"].disposition == Disposition.ACCEPTED   # source_file ignored; verified vs SRC


def test_quarantined_and_rejected_never_reach_canon(engine):
    """End-to-end through the REAL engine boundary: an undeclared-type (QUARANTINED) edge and a
    fabricated-span (REJECTED) edge must produce NO canon edge, while a legitimately accepted edge in
    the same payload IS written (tests-2 — the highest-value bypass class, previously only checked at
    the validate_payload level, never through kg_write + canon)."""
    from kg_engine.model import edge_id
    legit = edge_id("degree", "approximates", "importance")
    undeclared = edge_id("a", "smells_like", "b")
    fabricated = edge_id("x", "grounds", "y")
    out = engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"},
        {"source": "a", "target": "b", "relation": "smells_like",
         "span": "Heat flows from hot to cold", "authored_by": "agent"},
        {"source": "x", "target": "y", "relation": "grounds",
         "span": "unicorns cause gravity", "authored_by": "agent"},
    ]})
    assert out["dispositions"]["QUARANTINED"] >= 1 and out["dispositions"]["REJECTED"] >= 1
    canon_ids = {e.id for e in engine.canon.all_edges()}
    assert legit in canon_ids                                      # the accepted edge landed
    assert undeclared not in canon_ids and fabricated not in canon_ids  # the others never reached canon
