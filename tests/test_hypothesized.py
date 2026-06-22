"""The hypothesized write lane (PLAN Stage 1): the boundary accepts span-less hypothesized items as
a distinct lane while keeping every span-present/inferred guarantee intact, and the `kg_propose` tool
keeps the two write lanes legible at the call site.
"""
from __future__ import annotations

from kg_engine.boundary import validate_payload
from kg_engine.model import AuthoredBy, Disposition, Edge, EpistemicState, Provenance


def _hyp_edge(source, target, relation="bridges", **kw):
    return {"source": source, "target": target, "relation": relation,
            "provenance": "hypothesized", "authored_by": "agent", **kw}


def _edge_result(results):
    return next(r for r in results if r.kind == "edge")


# --------------------------------------------------------------------------- the lane itself


def test_hypothesized_edge_accepted_without_span(pack):
    results = validate_payload({"edges": [_hyp_edge("a", "b")]}, pack=pack, source_text="irrelevant")
    r = _edge_result(results)
    assert r.disposition == Disposition.ACCEPTED
    assert r.item.provenance == Provenance.HYPOTHESIZED
    assert r.item.epistemic_state == EpistemicState.UNVERIFIED
    assert r.item.span == ""  # no span carried — a proposal, not a text claim


def test_hypothesized_span_is_ignored_and_stored_empty(pack):
    # even a verbatim-looking span on a hypothesized item is dropped (the simpler documented path)
    results = validate_payload({"edges": [_hyp_edge("a", "b", span="anything at all")]},
                               pack=pack, source_text="anything at all is in the source")
    r = _edge_result(results)
    assert r.disposition == Disposition.ACCEPTED and r.item.span == ""


def test_hypothesized_edge_with_verdict_demoted(pack):
    # never-forge-a-verdict binds the hypothesized lane too: a verdict is stripped to unverified
    results = validate_payload({"edges": [_hyp_edge("a", "b", epistemic_state="grounded")]},
                               pack=pack, source_text="src")
    r = _edge_result(results)
    assert r.disposition == Disposition.DEMOTED
    assert "forged-verdict-stripped" in r.reason
    assert r.item.epistemic_state == EpistemicState.UNVERIFIED


def test_hypothesized_preserves_deterministic_authorship(pack):
    # a deterministic discovery mechanism legitimately authors a candidate — no span-bypass to forge
    results = validate_payload({"edges": [_hyp_edge("a", "b", authored_by="deterministic")]},
                               pack=pack, source_text="src")
    r = _edge_result(results)
    assert r.disposition == Disposition.ACCEPTED
    assert r.item.authored_by == AuthoredBy.DETERMINISTIC


def test_hypothesized_human_authorship_still_demoted(pack):
    # `human` is still forgeable and is demoted on every lane
    results = validate_payload({"edges": [_hyp_edge("a", "b", authored_by="human")]},
                               pack=pack, source_text="src")
    r = _edge_result(results)
    assert r.item.authored_by == AuthoredBy.AGENT
    assert "human-claim-stripped" in r.reason


def test_hypothesized_off_vocabulary_quarantined(pack):
    results = validate_payload({"edges": [_hyp_edge("a", "b", relation="refutes")]},
                               pack=pack, source_text="src")
    r = _edge_result(results)
    assert r.disposition == Disposition.QUARANTINED
    assert "undeclared-edge-type" in r.reason


# --------------------------------------------------------------------------- no regression


def test_span_present_edge_without_span_still_rejected(pack):
    results = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "bridges",
                    "provenance": "span-present", "authored_by": "agent"}]},
        pack=pack, source_text="some source text")
    r = _edge_result(results)
    assert r.disposition == Disposition.REJECTED
    assert r.reason == "no-supporting-span"


def test_inferred_edge_with_fabricated_span_still_rejected(pack):
    results = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "bridges",
                    "provenance": "inferred", "authored_by": "agent", "span": "not in the source"}]},
        pack=pack, source_text="some source text")
    r = _edge_result(results)
    assert r.disposition == Disposition.REJECTED
    assert r.reason == "span-not-in-source"


# --------------------------------------------------------------------------- invariant 5: failure memory


def test_hypothesized_collapses_into_known_failure_quarantined(pack):
    failed = Edge(source="a", target="b", relation="bridges", span="x",
                  epistemic_state=EpistemicState.FAILED)
    # forward identity collides with a known failure
    fwd = validate_payload({"edges": [_hyp_edge("a", "b")]},
                           pack=pack, source_text="src", existing=[failed])
    r = _edge_result(fwd)
    assert r.disposition == Disposition.QUARANTINED
    assert r.reason == "collapses-into-known-failure"
    # the REVERSE identity also collapses into the same failure
    rev = validate_payload({"edges": [_hyp_edge("b", "a")]},
                           pack=pack, source_text="src", existing=[failed])
    assert _edge_result(rev).disposition == Disposition.QUARANTINED


def test_hypothesized_not_blocked_by_grounded_neighbour(pack):
    # only FAILURE_STATES bind generation; a grounded edge of the same identity must not block a
    # re-proposal (it would simply dedup as live structure, not a refutation)
    grounded = Edge(source="a", target="b", relation="bridges", span="x",
                    epistemic_state=EpistemicState.GROUNDED)
    results = validate_payload({"edges": [_hyp_edge("a", "b")]},
                               pack=pack, source_text="src", existing=[grounded])
    assert _edge_result(results).disposition != Disposition.QUARANTINED


# --------------------------------------------------------------------------- the propose tool


def test_kg_propose_writes_hypothesized(engine):
    out = engine.kg_propose({"edges": [{"source": "compression", "target": "betweenness",
                                        "relation": "bridges"}]})
    assert out["propose_lane"] is True
    assert out["dispositions"]["ACCEPTED"] >= 1
    e = next(x for x in engine.canon.all_edges() if x.relation == "bridges")
    assert e.provenance == Provenance.HYPOTHESIZED
    assert e.epistemic_state == EpistemicState.UNVERIFIED
    assert e.span == ""


def test_kg_propose_refuses_text_claim(engine):
    out = engine.kg_propose({"edges": [{"source": "a", "target": "b", "relation": "bridges",
                                        "provenance": "span-present", "span": "whatever"}]})
    assert out["refused_text_claims"] == 1
    reasons = [d["reason"] for d in out["details"]]
    assert "propose-lane-text-claim" in reasons
    # nothing written: the text claim never reached the canon
    assert not any(e.relation == "bridges" for e in engine.canon.all_edges())
