"""Stage 6 exit test: grounding-loop verdicts, verdict survival across reproject (reconciler
re-attach), adversarial counter-edges, and failure memory surfaced in kg_context (never pruned).
"""
from __future__ import annotations

import json

from kg_engine.model import Edge, EpistemicState, Node, Provenance, edge_id
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
    """A verdict whose backing edge SURVIVES in the canon but vanished from the derived layer must be
    reported in orphaned_verdicts (the genuine orphan invariant — tests-1)."""
    _seed_queue(engine)
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")
    proj = engine.projector
    proj.project(incremental=False)
    data = json.loads(proj.graph_path.read_text())
    assert any(x["id"] == eid for x in data["links"])  # present in the derived layer initially

    # simulate the derived layer losing this edge while the canon verdict remains (partial projection
    # / dropped link), then ask the reconciler to reattach
    data["links"] = [x for x in data["links"] if x["id"] != eid]
    proj.graph_path.write_text(json.dumps(data))
    report = Reconciler(engine.canon).reattach_after_reproject(proj.graph_path)

    assert eid in report.orphaned_verdicts                     # surfaced as an orphan...
    assert eid in {e.id for e in engine.canon.all_edges()}     # ...because it is still in the canon


def test_deleted_edge_leaves_no_orphan(engine):
    """The companion case: an edge deleted from the canon entirely yields NO orphan (nothing to
    reattach, no false orphan)."""
    _seed_queue(engine)
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")
    proj = engine.projector
    proj.project(incremental=False)

    node = engine.canon.read_node("degree")
    node.edges = [e for e in node.edges if e.id != eid]
    engine.canon.write_one(node)
    proj.project(incremental=False)
    report = Reconciler(engine.canon).reattach_after_reproject(proj.graph_path)
    assert eid not in {e.id for e in engine.canon.all_edges()}
    assert eid not in report.orphaned_verdicts


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


# --------------------------------------------------------------------------- Stage 8: the hypothesized lane


def test_kg_context_segregates_hypotheses_from_items(engine):
    # a grounded span-present edge ...
    engine.kg_write({"edges": [{"source": "compression", "target": "claim", "relation": "grounds",
                                "span": "A compression stands in for many observations",
                                "authored_by": "agent"}]})
    geid = edge_id("compression", "grounds", "claim")
    engine.kg_ground(geid, "grounded")
    # ... and a hypothesized proposal
    engine.kg_propose({"edges": [{"source": "betweenness", "target": "degree", "relation": "bridges"}]})
    heid = edge_id("betweenness", "bridges", "degree")

    ctx = engine.kg_context(budget=5000)
    item_ids = {i["id"] for i in ctx["items"]}
    hyp_ids = {h["id"] for h in ctx["hypotheses"]}
    assert geid in item_ids and heid not in item_ids        # grounded answers exclude the proposal
    assert heid in hyp_ids and geid not in hyp_ids           # the proposal lives in its own lane
    assert all(h["provenance"] == "hypothesized" for h in ctx["hypotheses"])


def test_hypothesis_grounding_requires_support(engine):
    engine.kg_propose({"edges": [{"source": "betweenness", "target": "degree", "relation": "bridges"}]})
    eid = edge_id("betweenness", "bridges", "degree")
    out = engine.kg_ground(eid, "grounded")                  # no support
    assert out["ok"] is False and out["error"] == "hypothesis-needs-support"
    e = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert e.epistemic_state == EpistemicState.UNVERIFIED    # untouched: no verdict forged
    assert e.provenance == Provenance.HYPOTHESIZED


def test_hypothesis_promoted_with_span_upgrades_to_span_present(engine):
    engine.kg_propose({"edges": [{"source": "compression", "target": "claim", "relation": "grounds"}]})
    eid = edge_id("compression", "grounds", "claim")
    out = engine.kg_ground(eid, "grounded",
                           support_span="A compression stands in for many observations")
    assert out["ok"] and out["provenance_upgraded_to"] == "span-present"
    e = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert e.epistemic_state == EpistemicState.GROUNDED
    assert e.provenance == Provenance.SPAN_PRESENT and e.span     # now citable, earned its grounding


def test_hypothesis_promoted_with_citation_is_inferred(engine):
    engine.kg_propose({"edges": [{"source": "betweenness", "target": "degree", "relation": "bridges"}]})
    eid = edge_id("betweenness", "bridges", "degree")
    out = engine.kg_ground(eid, "grounded", support_note="Swanson 1986, undiscovered public knowledge")
    assert out["ok"] and out["provenance_upgraded_to"] == "inferred"
    e = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert e.provenance == Provenance.INFERRED and e.epistemic_state == EpistemicState.GROUNDED


def test_hypothesis_fabricated_support_span_refused(engine):
    engine.kg_propose({"edges": [{"source": "betweenness", "target": "degree", "relation": "bridges"}]})
    eid = edge_id("betweenness", "bridges", "degree")
    out = engine.kg_ground(eid, "grounded", support_span="this phrase is nowhere in the source at all")
    assert out["ok"] is False and out["error"] == "support-span-not-in-source"
    e = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert e.provenance == Provenance.HYPOTHESIZED and e.epistemic_state == EpistemicState.UNVERIFIED


def test_rejected_hypothesis_binds_next_generation(engine):
    # invariant-5 round trip: propose -> reject -> failure memory -> re-proposal quarantined
    engine.kg_propose({"edges": [{"source": "alpha", "target": "beta", "relation": "bridges"}]})
    eid = edge_id("alpha", "bridges", "beta")
    engine.kg_ground(eid, "rejected", note="vague")
    assert eid in engine._failure_ids()
    out = engine.kg_propose({"edges": [{"source": "alpha", "target": "beta", "relation": "bridges"}]})
    assert "collapses-into-known-failure" in [d["reason"] for d in out["details"]]
    # the reverse identity is bound too, and a generator that runs would consult the same failure set
    rev = engine.kg_propose({"edges": [{"source": "beta", "target": "alpha", "relation": "bridges"}]})
    assert "collapses-into-known-failure" in [d["reason"] for d in rev["details"]]
    assert not any({c["source"], c["target"]} == {"alpha", "beta"}
                   for c in engine.kg_generate("all", k=20)["candidates"])


# --------------------------------------------------------------------------- R4: multi-source promotion


def _multi_source_engine(vault, tmp_path):
    """An engine whose source is a DIRECTORY of two .md files (R4)."""
    from pathlib import Path
    from kg_engine.server import KGEngine
    srcdir = tmp_path / "sources"
    srcdir.mkdir()
    (srcdir / "a.md").write_text("Alpha grounds beta in the first document.\n", encoding="utf-8")
    (srcdir / "b.md").write_text("Gamma bridges delta across the second document.\n", encoding="utf-8")
    pack_path = Path(__file__).resolve().parents[1] / "pack" / "pack.yaml"
    return KGEngine(vault, source_path=srcdir, pack_path=pack_path)


def test_multi_source_support_span_promotes_from_any_file(vault, tmp_path):
    """A hypothesis promotes when its support span is verbatim in ANY declared source — here a span
    from the SECOND document — and the provenance upgrades to span-present (source-aware kg_ground)."""
    eng = _multi_source_engine(vault, tmp_path)
    eng.kg_propose({"edges": [{"source": "gamma", "target": "delta", "relation": "bridges"}]})
    eid = edge_id("gamma", "bridges", "delta")
    out = eng.kg_ground(eid, "grounded", support_span="Gamma bridges delta")  # lives in b.md
    assert out["ok"] and out["provenance_upgraded_to"] == "span-present"
    e = next(x for x in eng.canon.all_edges() if x.id == eid)
    assert e.provenance == Provenance.SPAN_PRESENT and e.epistemic_state == EpistemicState.GROUNDED

    # a different hypothesis promotes from the FIRST document too
    eng.kg_propose({"edges": [{"source": "alpha", "target": "beta", "relation": "grounds"}]})
    aid = edge_id("alpha", "grounds", "beta")
    assert eng.kg_ground(aid, "grounded", support_span="Alpha grounds beta")["ok"]


def test_multi_source_support_span_not_in_any_source_refused(vault, tmp_path):
    """Preserve the existing contract: a support span in NONE of the declared sources is still refused
    `support-span-not-in-source` (the not-in-any reason is unchanged for multi-source)."""
    eng = _multi_source_engine(vault, tmp_path)
    eng.kg_propose({"edges": [{"source": "gamma", "target": "delta", "relation": "bridges"}]})
    eid = edge_id("gamma", "bridges", "delta")
    out = eng.kg_ground(eid, "grounded", support_span="this phrase is in neither document at all")
    assert out["ok"] is False and out["error"] == "support-span-not-in-source"
    e = next(x for x in eng.canon.all_edges() if x.id == eid)
    assert e.provenance == Provenance.HYPOTHESIZED and e.epistemic_state == EpistemicState.UNVERIFIED


def test_multi_source_kg_write_verifies_per_file(vault, tmp_path):
    """End-to-end through kg_write: an edge whose span lives in b.md but is attributed to a.md is
    REJECTED, while the same span attributed to b.md is written — source-aware span-present (R4)."""
    eng = _multi_source_engine(vault, tmp_path)
    out = eng.kg_write({"edges": [
        {"source": "gamma", "target": "delta", "relation": "bridges",
         "span": "Gamma bridges delta", "source_file": "a.md", "authored_by": "agent"},
        {"source": "gamma", "target": "delta", "relation": "bridges",
         "span": "Gamma bridges delta", "source_file": "b.md", "authored_by": "agent"},
    ]})
    reasons = {d["reason"] for d in out["details"] if d["kind"] == "edge"}
    assert "span-not-in-named-source" in reasons          # the a.md mis-attribution rejected
    assert edge_id("gamma", "bridges", "delta") in {e.id for e in eng.canon.all_edges()}  # b.md one landed


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
