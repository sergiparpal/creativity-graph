"""Regression tests for the review-sweep fixes to model.py and boundary.py: slug NFC/non-lossiness,
parse resilience, canonical-id dedup, the node flood baseline, the span floor, and failure-memory
budget exclusion.
"""
from __future__ import annotations

import unicodedata

from kg_engine.boundary import MIN_EDGE_BUDGET, merge_results_into_nodes, validate_payload
from kg_engine.model import Edge, EpistemicState, Disposition, edge_id, node_from_markdown, slug

# a source containing both spans the boundary tests cite
SRC = "A compression grounds the claims beneath it. Degree approximates importance."


def _edge(results):
    return next(r for r in results if r.kind == "edge")


# ---- model: slug / edge_id -----------------------------------------------

def test_edge_id_is_nfc_stable():
    # model-pack-1: visually-identical strings in NFC vs NFD must produce the SAME id/slug, or the
    # same logical node/edge forks and dedup silently fails.
    nfc, nfd = unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")
    assert nfc != nfd  # genuinely different code points
    assert slug(nfc) == slug(nfd)
    assert edge_id(nfc, "rel", "x") == edge_id(nfd, "rel", "x")


def test_slug_maps_punctuation_instead_of_dropping():
    # model-pack-2: punctuation maps to a separator, so distinct inputs stay distinct.
    assert slug("a/b") == "a-b" and slug("a/b") != slug("ab")
    assert slug("I/O") != slug("IO")
    assert slug("foo.bar") != slug("foobar")
    # but the id delimiter cannot be injected: a run of separators collapses to one '-'
    assert slug("a __ b") == "a-b"


def test_malformed_enum_or_edge_does_not_drop_node():
    # model-pack-3: a typo'd enum or a malformed edge entry must not take the whole node out of every
    # read (it carries failed/rejected counter-edges that §1.7 says must never vanish).
    md = (
        "---\n"
        "id: n1\n"
        "label: N1\n"
        "epistemic_state: groundd\n"        # typo -> coerced to unverified, not a crash
        "edges:\n"
        "- source: n1\n"
        "  target: t\n"
        "  relation: grounds\n"
        "  epistemic_state: verified\n"      # bogus enum -> coerced
        "  span: s\n"
        "- just a bare string\n"             # malformed entry -> skipped, node survives
        "---\n"
        "body\n"
    )
    node = node_from_markdown(md)
    assert node.id == "n1"
    assert node.epistemic_state == EpistemicState.UNVERIFIED
    assert len(node.edges) == 1                                   # the scalar entry was skipped
    assert node.edges[0].epistemic_state == EpistemicState.UNVERIFIED


# ---- boundary ------------------------------------------------------------

def test_boundary_dedup_and_merge_agree_on_canonical_id(pack):
    # boundary-1: the boundary dedup key now matches the canonical edge id (the slug), so two edges
    # whose endpoints differ only by a slug-equivalent (trailing space) collapse consistently — the
    # boundary marks one deduped AND the merge yields exactly one canonical edge.
    payload = {"edges": [
        {"source": "degree", "target": "claim", "relation": "grounds",
         "span": "A compression grounds the claims beneath it", "authored_by": "agent"},
        {"source": "degree", "target": "claim ", "relation": "grounds",   # trailing space -> same id
         "span": "A compression grounds the claims beneath it", "authored_by": "agent"},
    ]}
    res = validate_payload(payload, pack=pack, source_text=SRC)
    assert any("deduped" in r.reason for r in res if r.kind == "edge")
    nodes = merge_results_into_nodes(res)
    assert sum(len(n.edges) for n in nodes.values()) == 1


def test_node_flood_budget_is_seeded_canon_wide(pack):
    # boundary-3: the node limiter is seeded with the existing canon node ids, not reset to 0 each
    # call — so a NET-NEW node at the cap is flooded.
    existing = {f"n{i}" for i in range(MIN_EDGE_BUDGET)}
    payload = {"nodes": [{"label": "NewOne", "node_type": "compression"}]}
    at_cap = validate_payload(payload, pack=pack, source_text="x", existing_node_ids=existing)
    n = next(r for r in at_cap if r.kind == "node")
    assert n.disposition == Disposition.REJECTED and n.reason == "rate-limited-flood"
    below = validate_payload(payload, pack=pack, source_text="x", existing_node_ids=set())
    assert next(r for r in below if r.kind == "node").disposition == Disposition.ACCEPTED


def test_idempotent_existing_node_not_flooded_at_cap(pack):
    # boundary-3 regression guard: re-emitting an ALREADY-EXISTING node when the canon is at its node
    # budget must NOT be flooded (it grows the canon by zero — the edge path's "deduped costs zero").
    existing = {f"n{i}" for i in range(MIN_EDGE_BUDGET)}
    payload = {"nodes": [{"id": "n0", "label": "n0", "node_type": "compression"}]}  # already exists
    res = validate_payload(payload, pack=pack, source_text="x", existing_node_ids=existing)
    n = next(r for r in res if r.kind == "node")
    assert n.disposition == Disposition.ACCEPTED and "deduped" in n.reason


def test_degenerate_short_span_rejected(pack):
    # boundary-5: a 1-char span is a substring of almost any prose; it must not satisfy span-present.
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds", "span": "a",
                    "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _edge(res)
    assert e.disposition == Disposition.REJECTED and e.reason == "span-too-short"


def test_failure_memory_does_not_consume_flood_budget(pack):
    # boundary-6: never-pruned rejected/failed edges (§1.7) must not count toward the flood baseline
    # and starve legitimate new writes.
    existing = [Edge(source=f"s{i}", target="t", relation="grounds", span="x",
                     epistemic_state=EpistemicState.FAILED) for i in range(MIN_EDGE_BUDGET)]
    payload = {"edges": [{"source": "degree", "target": "importance", "relation": "approximates",
                          "span": "Degree approximates importance", "authored_by": "agent"}]}
    res = validate_payload(payload, pack=pack, source_text=SRC, existing=existing)
    assert _edge(res).disposition == Disposition.ACCEPTED
