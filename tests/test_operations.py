"""The four endo operations (PLAN Stage 4): open / collapse / explode / regroup write hypothesized
structure through the propose lane — never a verdict, never a span.
"""
from __future__ import annotations

import itertools

from kg_engine.model import EpistemicState, Provenance


def _propose_edges(engine, edges):
    # declare every endpoint as a real canon node (a target-only node would dangle with no community,
    # which is correct canon behaviour but not what these cluster fixtures want to exercise)
    ids = sorted({x for e in edges for x in e[:2]})
    return engine.kg_propose({
        "nodes": [{"id": i, "label": i, "node_type": "claim"} for i in ids],
        "edges": [{"source": s, "target": t, "relation": r} for s, t, r in edges]})


def _no_verdict_no_span(engine):
    for e in engine.canon.all_edges():
        assert e.epistemic_state == EpistemicState.UNVERIFIED, e.id
        assert e.span == "", e.id


# --------------------------------------------------------------------------- collapse


def test_collapse_writes_compression_and_collapses_into(engine):
    # build a K4 cluster via the propose lane (no spans needed for hypothesized structure)
    _propose_edges(engine, [(a, b, "bridges") for a, b in
                            itertools.combinations(["n1", "n2", "n3", "n4"], 2)])
    before = engine.kg_metrics()
    out = engine.kg_operate("collapse", target="n1")
    assert out["ok"], out
    comps = [n for n in engine.canon.all_nodes() if n.node_type == "compression"]
    assert comps, out
    comp = comps[0]
    assert comp.provenance == Provenance.HYPOTHESIZED
    assert comp.epistemic_state == EpistemicState.UNVERIFIED
    ci = [e for e in engine.canon.all_edges() if e.relation == "collapses_into"]
    assert ci and all(e.target == comp.id for e in ci)
    assert {e.source for e in ci} == {"n1", "n2", "n3", "n4"}     # every member collapsed
    after = engine.kg_metrics()
    assert after["nodes"] > before["nodes"] and after["edges"] > before["edges"]
    _no_verdict_no_span(engine)


# --------------------------------------------------------------------------- explode (inverse shape)


def test_explode_is_inverse_shape(engine):
    _propose_edges(engine, [("hub", "x", "bridges"), ("hub", "y", "grounds")])
    out = engine.kg_operate("explode", target="hub")
    assert out["ok"], out
    kids = [e for e in engine.canon.all_edges()
            if e.relation == "collapses_into" and e.target == "hub"]
    assert kids                                                  # node -> hypothesized children
    for e in kids:
        assert e.epistemic_state == EpistemicState.UNVERIFIED and e.span == ""
        child = engine.canon.read_node(e.source)
        assert child.provenance == Provenance.HYPOTHESIZED
        assert child.node_type == "primitive"
    _no_verdict_no_span(engine)


# --------------------------------------------------------------------------- regroup (persisted)


def test_regroup_persists_invisible_bridges(engine):
    # K6 minus (1,6): one community at the stored resolution; (1,6) flips cross-community on re-partition
    nodes = [str(i) for i in range(1, 7)]
    _propose_edges(engine, [(a, b, "bridges") for a, b in itertools.combinations(nodes, 2)
                            if (a, b) != ("1", "6")])
    out = engine.kg_operate("regroup")
    assert out["ok"], out
    flipped = [e for e in engine.canon.all_edges()
               if {e.source, e.target} == {"1", "6"} and e.relation == "bridges"]
    assert flipped
    assert flipped[0].provenance == Provenance.HYPOTHESIZED
    assert flipped[0].epistemic_state == EpistemicState.UNVERIFIED and flipped[0].span == ""


# --------------------------------------------------------------------------- open (new primitive)


def test_open_proposes_primitive_and_attachment_points(engine):
    _propose_edges(engine, [("core", "a", "bridges"), ("core", "b", "bridges"), ("core", "c", "bridges")])
    out = engine.kg_operate("open")
    assert out["ok"], out
    prims = [n for n in engine.canon.all_nodes()
             if n.node_type == "primitive" and n.id.startswith("opening-")]
    assert prims
    p = prims[0]
    assert p.provenance == Provenance.HYPOTHESIZED
    attach = [e for e in engine.canon.all_edges() if e.source == p.id]
    assert attach and all(e.span == "" and e.epistemic_state == EpistemicState.UNVERIFIED for e in attach)
    _no_verdict_no_span(engine)


# --------------------------------------------------------------------------- unknown op


def test_unknown_op_is_refused(engine):
    out = engine.kg_operate("frobnicate")
    assert out["ok"] is False and "unknown op" in out["error"]
