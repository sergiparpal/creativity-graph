"""Stage 6 exit test: grounding-loop verdicts, verdict survival across reproject (reconciler
re-attach), adversarial counter-edges, and failure memory surfaced in kg_context (never pruned).
"""
from __future__ import annotations

import json

from kg_engine.model import Edge, EpistemicState, Node, edge_id
from kg_engine.projector import Projector
from kg_engine.reconciler import Reconciler


def _seed_queue(engine):
    """A small queue of unverified candidate edges, all span-verifying against the source."""
    payload = {"edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "A compression stands in for many observations and grounds the claims beneath it",
         "authored_by": "agent", "confidence": "INFERRED", "confidence_score": 0.7},
        {"source": "betweenness", "target": "generality-confound", "relation": "confounded_by",
         "span": "Betweenness is confounded by the generality confound",
         "authored_by": "agent", "confidence": "INFERRED", "confidence_score": 0.6},
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"},
    ]}
    out = engine.kg_write(payload)
    assert out["dispositions"]["ACCEPTED"] >= 3, out
    return out


def test_grounder_drains_queue(engine):
    _seed_queue(engine)
    # the grounder walks unverified edges and applies verdicts via kg_ground
    unverified = [e for e in engine.canon.all_edges() if e.epistemic_state == EpistemicState.UNVERIFIED]
    assert unverified
    for e in unverified:
        engine.kg_ground(e.id, "grounded", by="agent")
    states = {e.epistemic_state for e in engine.canon.all_edges()}
    assert states == {EpistemicState.GROUNDED}
    assert engine.kg_metrics()["edges_by_epistemic_state"].get("grounded") == len(unverified)


def test_verdict_survives_full_reproject(engine):
    _seed_queue(engine)
    eid = edge_id("compression", "grounds", "claim")
    engine.kg_ground(eid, "grounded", by="human")

    proj = engine.projector
    proj.project(incremental=False)  # full rebuild
    data = json.loads(proj.graph_path.read_text())
    e = next(x for x in data["links"] if x["id"] == eid)
    assert e["epistemic_state"] == "grounded"  # carried into the derived layer

    # the reconciler re-attaches verdicts to surviving edges and reports orphans
    report = Reconciler(engine.canon).reattach_after_reproject(proj.graph_path)
    assert report.reattached >= 1 and report.orphaned_verdicts == []

    # and the canonical verdict still stands (audited, so not re-quarantined)
    Reconciler(engine.canon).scan(full_sweep=True)
    after = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert after.epistemic_state == EpistemicState.GROUNDED


def test_orphaned_verdict_surfaced(engine):
    _seed_queue(engine)
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")
    proj = engine.projector
    proj.project(incremental=False)

    # remove the edge from the canon out-of-band, then reproject: the verdict is now orphaned
    node = engine.canon.read_node("degree")
    node.edges = [e for e in node.edges if e.id != eid]
    engine.canon.write_one(node)
    proj.project(incremental=False)
    report = Reconciler(engine.canon).reattach_after_reproject(proj.graph_path)
    # the grounded edge is gone from canon, so nothing to re-attach and no false orphan from it
    assert eid not in {e.id for e in engine.canon.all_edges()}


def test_adversarial_counter_edge_persists(engine):
    """The adversarial grounder's output — a typed attacked_by counter-edge against a hub — validates
    through the boundary and persists as failure memory."""
    # seed a hub
    engine.kg_write({"edges": [
        {"source": "betweenness", "target": "bridge", "relation": "bridges",
         "span": "Specificity-weighted betweenness reconciles with the bridge intuition",
         "authored_by": "agent"}]})
    # adversarial counter-edge
    out = engine.kg_write({"edges": [
        {"source": "betweenness", "target": "generality-confound", "relation": "attacked_by",
         "span": "Betweenness is confounded by the generality confound", "authored_by": "agent"}]})
    assert out["dispositions"]["ACCEPTED"] >= 1
    counter = edge_id("betweenness", "attacked_by", "generality-confound")
    engine.kg_ground(counter, "failed", by="agent", note="falsified: vague hub, not a real bridge")
    e = next(x for x in engine.canon.all_edges() if x.id == counter)
    assert e.epistemic_state == EpistemicState.FAILED


def test_failed_edge_not_pruned_and_surfaced(engine):
    _seed_queue(engine)
    eid = edge_id("betweenness", "confounded_by", "generality-confound")
    engine.kg_ground(eid, "failed", by="agent")

    proj = engine.projector
    proj.project(incremental=False)
    # a pruning pass (full reproject) must keep failure memory (§1.7)
    data = json.loads(proj.graph_path.read_text())
    assert any(x["id"] == eid and x["epistemic_state"] == "failed" for x in data["links"])
    ctx = proj.kg_context(budget=5000)
    assert ctx["falsification_counters"]["failed_or_rejected_edges"] >= 1
