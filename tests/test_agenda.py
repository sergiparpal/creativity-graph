"""Tests for kg_agenda (R6) — the read-only structural "suggested questions" tool.

The detector + ranking logic is a pure function over precomputed derived rows (_agenda_from_rows), so
most cases are tested in isolation with synthetic rows; the engine path is exercised for read-only
invariance (it asserts no edges, copies no spans, stamps no verdicts — measure-never-gate).
"""
from __future__ import annotations

from kg_engine.projector import _agenda_from_rows


def _n(nid, *, degree=0, community=0, structural_bridge=0, betweenness=0.0, spec_betweenness=0.0,
       specificity=1.0, gate_on=0, label=None):
    return {"id": nid, "label": label or nid, "degree": degree, "community": community,
            "structural_bridge": structural_bridge, "betweenness": betweenness,
            "spec_betweenness": spec_betweenness, "specificity": specificity, "gate_on": gate_on}


def _e(src, tgt, *, provenance="span-present", epistemic_state="grounded"):
    return {"source": src, "target": tgt, "provenance": provenance, "epistemic_state": epistemic_state}


def _detectors(out):
    return {g["detector"] for g in out["answerable_now"] + out["blocked_on_grounding"]}


# --------------------------------------------------------------------------- detectors


def test_orphan_detector():
    out = _agenda_from_rows([_n("x", degree=0)], [], limit=5)
    assert out["answerable_now"] == []
    assert [g["detector"] for g in out["blocked_on_grounding"]] == ["orphan"]
    assert out["blocked_on_grounding"][0]["focus"] == ["x"]


def test_hypothesized_only_neighborhood_is_blocked():
    nodes = [_n("h", degree=2)]
    edges = [_e("h", "a", provenance="hypothesized", epistemic_state="unverified"),
             _e("h", "b", provenance="hypothesized", epistemic_state="unverified")]
    out = _agenda_from_rows(nodes, edges, limit=5)
    assert out["answerable_now"] == []   # a hypothesized-only neighbourhood is NEVER answerable
    g = out["blocked_on_grounding"][0]
    assert g["detector"] == "hypothesized-only" and g["focus"] == ["h"]


def test_under_grounded_hub_is_blocked():
    nodes = [_n("u", degree=3)]
    edges = [_e("u", "a", epistemic_state="grounded"),
             _e("u", "b", provenance="inferred", epistemic_state="unverified"),
             _e("u", "c", provenance="inferred", epistemic_state="unverified")]
    out = _agenda_from_rows(nodes, edges, limit=5)
    g = out["blocked_on_grounding"][0]
    assert g["detector"] == "under-grounded-hub"
    assert "1/3" in g["question"]   # 1 grounded of 3 decided


def test_well_grounded_hub_is_answerable_now():
    nodes = [_n("w", degree=3), _n("a", degree=1), _n("b", degree=1), _n("c", degree=1)]
    edges = [_e("w", "a"), _e("w", "b"), _e("w", "c")]  # all grounded
    out = _agenda_from_rows(nodes, edges, limit=5)
    assert out["blocked_on_grounding"] == []
    g = out["answerable_now"][0]
    assert g["detector"] == "well-grounded" and g["focus"] == ["w"]
    assert "a" in g["question"] and "b" in g["question"]   # neighbours named


def test_edgeless_community_pair_detector():
    # two disconnected clusters (no LIVE inter-community edge) -> both flagged as coverage gaps
    nodes = [_n("p", community=0, degree=1), _n("q", community=0, degree=1),
             _n("r", community=1, degree=1), _n("s", community=1, degree=1)]
    edges = [_e("p", "q"), _e("r", "s")]  # only intra-community edges
    out = _agenda_from_rows(nodes, edges, limit=5)
    items = [g for g in out["blocked_on_grounding"] if g["detector"] == "edgeless-communities"]
    assert len(items) == 2
    assert all(g["lane"] == "blocked_on_grounding" for g in items)


def test_disconnected_single_node_is_orphan_not_edgeless_community():
    # communities 0 and 1 are connected by a cross edge (not islands); only the lone node is isolated,
    # and a size-1 island is an `orphan`, never duplicated as an `edgeless-communities` gap.
    nodes = [_n("lone", community=7, degree=0),
             _n("a", community=0, degree=1), _n("b", community=1, degree=1)]
    edges = [_e("a", "b")]   # a cross-community edge -> communities 0 and 1 are NOT islands
    out = _agenda_from_rows(nodes, edges, limit=5)
    dets = [g["detector"] for g in out["blocked_on_grounding"]]
    assert "orphan" in dets
    assert "edgeless-communities" not in dets   # the size-1 island (lone) is suppressed


def test_cluster_already_matched_by_node_detector_is_not_double_counted():
    """One detector per node: a freshly-proposed hypothesized pair {p,q} forms its own disconnected
    community, but each node is already a `hypothesized-only` gap, so the cluster is NOT also re-surfaced
    as edgeless-communities (it would crowd the capped lane). A cluster whose nodes are NOT node-detector
    matches ({r,s}, grounded) IS still reported."""
    nodes = [_n("p", community=0, degree=1), _n("q", community=0, degree=1),
             _n("r", community=1, degree=1), _n("s", community=1, degree=1)]
    edges = [_e("p", "q", provenance="hypothesized", epistemic_state="unverified"),
             _e("r", "s")]  # r-s grounded -> r,s match no node detector, so their island IS reported
    out = _agenda_from_rows(nodes, edges, limit=10)
    blocked = out["blocked_on_grounding"]
    assert [g["detector"] for g in blocked].count("hypothesized-only") == 2     # p and q, once each
    edgeless = [g["focus"] for g in blocked if g["detector"] == "edgeless-communities"]
    assert edgeless == [["r", "s"]]   # {p,q} suppressed (already covered); {r,s} reported
    # no node is surfaced in more than one item
    all_focus = [nid for g in blocked for nid in g["focus"]]
    assert len(all_focus) == len(set(all_focus))


# --------------------------------------------------------------------------- two-lane split + ranking


def test_two_lane_split_surfaces_both():
    nodes = [_n("w", degree=3), _n("a", degree=1), _n("b", degree=1), _n("c", degree=1),
             _n("z", degree=0)]
    edges = [_e("w", "a"), _e("w", "b"), _e("w", "c")]
    out = _agenda_from_rows(nodes, edges, limit=5)
    assert [g["detector"] for g in out["answerable_now"]] == ["well-grounded"]
    assert "orphan" in {g["detector"] for g in out["blocked_on_grounding"]}


def test_ranked_by_is_gate_aware():
    assert _agenda_from_rows([_n("x", degree=0, gate_on=0)], [])["ranked_by"] == "structural_bridge"
    assert _agenda_from_rows([_n("x", degree=0, gate_on=1)], [])["ranked_by"] == "spec_betweenness"
    assert _agenda_from_rows([_n("x", degree=0, gate_on=1)], [])["gate_on"] == 1


def test_specificity_down_weights_vague_hub_when_gate_on():
    # gate_on=1 ranks by spec_betweenness: two equally-between hubs, the SPECIFIC one (higher
    # spec_betweenness) wins the single answerable slot; the vague one is down-ranked out.
    def hub(nid, spec_bet):
        return _n(nid, degree=3, gate_on=1, betweenness=0.5, spec_betweenness=spec_bet,
                  specificity=spec_bet)
    nodes = [hub("specific", 0.9), hub("vague", 0.1),
             _n("a", degree=1), _n("b", degree=1), _n("c", degree=1),
             _n("d", degree=1), _n("e", degree=1), _n("f", degree=1)]
    edges = [_e("specific", "a"), _e("specific", "b"), _e("specific", "c"),
             _e("vague", "d"), _e("vague", "e"), _e("vague", "f")]
    out = _agenda_from_rows(nodes, edges, limit=1)
    assert len(out["answerable_now"]) == 1
    assert out["answerable_now"][0]["focus"] == ["specific"]   # vague hub down-ranked out


# --------------------------------------------------------------------------- clamps + empty


def test_limit_caps_each_lane_and_is_clamped():
    nodes = [_n("o1", degree=0), _n("o2", degree=0), _n("o3", degree=0)]
    out = _agenda_from_rows(nodes, [], limit=1)
    assert len(out["blocked_on_grounding"]) == 1 and out["limit"] == 1
    # limit=0 clamps up to 1; a huge limit clamps down to 50 (and never exceeds the available gaps)
    assert _agenda_from_rows(nodes, [], limit=0)["limit"] == 1
    assert _agenda_from_rows(nodes, [], limit=999)["limit"] == 50
    assert len(_agenda_from_rows(nodes, [], limit=999)["blocked_on_grounding"]) == 3


def test_empty_graph_yields_empty_lanes():
    out = _agenda_from_rows([], [], limit=5)
    assert out["answerable_now"] == [] and out["blocked_on_grounding"] == []
    assert out["count"] == 0 and out["ranked_by"] == "structural_bridge" and out["gate_on"] == 0


def test_output_labels_itself_a_heuristic():
    assert "heuristic" in _agenda_from_rows([], [])["note"]


# --------------------------------------------------------------------------- read-only invariance (engine)


def _snapshot(engine):
    canon = {p.name: p.read_bytes() for p in engine.canon.note_paths()}
    audit = engine._audit_path().read_bytes() if engine._audit_path().exists() else b""
    return canon, audit, engine.kg_metrics()


def test_kg_agenda_is_read_only_through_the_engine(engine):
    """asserts no edges, copies no spans, stamps no verdicts — the canon bytes, the grounding audit log,
    and kg_metrics are all byte/value-identical before and after a kg_agenda call (measure-never-gate)."""
    engine.kg_write({"edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "A compression stands in for many observations and grounds the claims beneath it",
         "provenance": "span-present", "authored_by": "agent"},
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "provenance": "span-present", "authored_by": "agent"}]})
    engine.kg_ground("e_compression__grounds__claim", "grounded")
    engine.projector.project()  # ensure a fresh, non-stale derived layer

    before = _snapshot(engine)
    out = engine.kg_agenda(limit=5)
    after = _snapshot(engine)

    assert before == after  # canon bytes + audit log + metrics all unchanged
    # the shape is the two-lane split + honest ranking metadata
    assert set(out) >= {"answerable_now", "blocked_on_grounding", "ranked_by", "gate_on", "limit", "count"}
    assert isinstance(out["answerable_now"], list) and isinstance(out["blocked_on_grounding"], list)
