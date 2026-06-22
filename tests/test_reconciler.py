"""Reconciler invariants (§1.8): forged-verdict re-quarantine across delete/recreate, out-of-band
`obsolete`, rename verdict↔audit migration, and non-canonical filenames. (Companion to the crash /
OOB cases in test_chaos.py; this file fills the gaps the review found — tests-5.)
"""
from __future__ import annotations

from kg_engine.model import Edge, EpistemicState, Node, edge_id, node_to_markdown
from kg_engine.reconciler import Reconciler


def _seed_grounded_edge(engine) -> str:
    """Write one span-verifying edge and ground it (audited). Returns its edge id."""
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]})
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")
    return eid


def test_recreated_edge_cannot_inherit_stale_verdict(engine):
    """reconciler-1: a deleted-then-recreated edge id must NOT inherit the old 'already grounded'
    baseline and let a forged verdict bypass re-quarantine."""
    eid = _seed_grounded_edge(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)  # baseline: grounded recorded (audit record consumed)

    # delete the edge out-of-band, then scan so the reconciler observes its absence and prunes the
    # stale 'grounded' baseline for this id
    node = engine.canon.read_node("degree")
    node.edges = [e for e in node.edges if e.id != eid]
    engine.canon.write_one(node)
    recon.scan(full_sweep=True)

    # recreate the SAME edge id but forge "grounded" directly in the canon (no kg_ground, no new audit)
    node = engine.canon.read_node("degree")
    node.edges.append(Edge(source="degree", target="importance", relation="approximates",
                           span="Degree approximates importance", epistemic_state=EpistemicState.GROUNDED))
    engine.canon.write_one(node)

    report = recon.scan(full_sweep=True)
    assert eid in report.requarantined  # the old audit record is already spent -> caught as a forgery
    after = next(e for e in engine.canon.all_edges() if e.id == eid)
    assert after.epistemic_state == EpistemicState.UNVERIFIED


def test_out_of_band_obsolete_is_requarantined(engine):
    """reconciler-5 / server-1: `obsolete` is reachable via kg_ground (audited), so an out-of-band
    edit to `obsolete` (which the write boundary demotes) must also be re-quarantined — previously it
    was excluded from the policed set and silently survived, erasing a grounding verdict."""
    eid = _seed_grounded_edge(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.OBSOLETE
    engine.canon.write_one(node)

    report = recon.scan(full_sweep=True)
    assert eid in report.requarantined
    after = next(e for e in engine.canon.all_edges() if e.id == eid)
    assert after.epistemic_state == EpistemicState.UNVERIFIED


def test_rename_migrates_verdict_so_it_survives_reconcile(engine):
    """integration-1: renaming an endpoint recomputes the edge id; kg_rename must migrate the audit
    record to the new id so the grounded verdict is NOT re-quarantined as a forgery on the next scan."""
    eid = _seed_grounded_edge(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    out = engine.kg_rename("degree", "degree-centrality")
    assert out["ok"], out
    new_eid = edge_id("degree-centrality", "approximates", "importance")
    assert new_eid != eid

    # the renamed edge carries its grounded verdict; with the migrating audit it survives the sweep
    report = recon.scan(full_sweep=True)
    assert new_eid not in report.requarantined
    after = next(e for e in engine.canon.all_edges() if e.id == new_eid)
    assert after.epistemic_state == EpistemicState.GROUNDED


def test_non_canonical_filename_is_reconciled_not_skipped(canon):
    """reconciler-4: a hand-created note whose filename is not slug-canonical must still be read (by
    path) and reconciled, not silently skipped forever via a read_node(p.stem) re-slug miss."""
    forged = Edge(source="Foo", target="bar", relation="grounds", span="x",
                  epistemic_state=EpistemicState.GROUNDED)
    node = Node(id="Foo", label="Foo", edges=[forged])
    # write directly under a NON-canonical filename (slug('Foo') == 'foo', so node_path would miss it)
    (canon.notes_dir / "Foo.md").write_text(node_to_markdown(node), encoding="utf-8")

    report = Reconciler(canon).scan(full_sweep=True)
    # the file was actually parsed (not skipped), so the unaudited grounded edge is caught as forged
    assert forged.id in report.requarantined
