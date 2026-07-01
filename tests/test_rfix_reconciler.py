"""Regression tests for the reconciler group fixes (§1.8):

  * M3 — when the canon lease CANNOT be taken, the sweep must NOT write back the STALE pre-lease
    snapshot: doing so verbatim-overwrites the node and silently drops a grounded verdict a concurrent
    kg_ground applied to a SIBLING edge. The lease-less path must skip the note and retry next sweep.
  * perf — the per-sweep spend-ledger checkpoint must not be APPENDED to the append-only forgery/verdict
    audit log every session (superlinear growth + full re-parse). It now lives in a SEPARATE sidecar that
    is atomically OVERWRITTEN each sweep, leaving the forgery records untouched so forge-detection still
    works.
"""
from __future__ import annotations

import json

from kg_engine.groundaudit import GroundAuditLog
from kg_engine.model import Edge, EpistemicState, edge_id
from kg_engine.reconciler import Reconciler


def _write_two_edges(engine):
    """Two agent edges sharing the source node 'degree' (so both live in degree.md) with a real span."""
    span = "Degree approximates importance"
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": span, "authored_by": "agent"},
        {"source": "degree", "target": "importance", "relation": "grounds",
         "span": span, "authored_by": "agent"},
    ]})
    return edge_id("degree", "approximates", "importance"), edge_id("degree", "grounds", "importance")


def _edge(engine, eid):
    return next(e for e in engine.canon.all_edges() if e.id == eid)


# --------------------------------------------------------------------------- M3

def test_lease_unavailable_does_not_overwrite_node(engine):
    """M3: with the lease unavailable, a sweep must NOT re-write the node file — even a genuinely forged
    verdict is left in place (skipped, retried next sweep) rather than clobbering the on-disk note."""
    eid_a, _ = _write_two_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)  # baseline

    # forge a grounded verdict directly in the canon (no kg_ground, no audit record)
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid_a).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    degree_path = engine.canon.node_path("degree")
    before = degree_path.read_bytes()

    # simulate the lease being held by another live writer for the whole sweep
    engine.canon.try_acquire_lock = lambda *a, **k: False
    report = recon.scan(full_sweep=True)

    # the buggy path would have reset+written the stale snapshot; the fix skips the note entirely
    assert degree_path.read_bytes() == before, "node file was overwritten without holding the lease"
    assert eid_a not in report.requarantined
    assert _edge(engine, eid_a).epistemic_state == EpistemicState.GROUNDED  # untouched, retries next sweep


def test_lease_unavailable_does_not_drop_concurrent_sibling_verdict(engine):
    """M3 (the real lost update): a concurrent kg_ground grounds a SIBLING edge between the sweep's
    snapshot read and its (would-be) write. With the lease unavailable the sweep must NOT write the stale
    snapshot back, so the sibling's just-applied grounded verdict survives. A later lease-held sweep then
    re-quarantines the forged edge while KEEPING the sibling grounded."""
    eid_a, eid_b = _write_two_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)  # baseline: both unverified

    # forge edge A grounded out-of-band so the sweep computes mutated=True (which is what would trigger a
    # write of the stale snapshot in the buggy path). Only degree.md changes, so the fake lock hook below
    # fires exactly on degree's critical section, AFTER its snapshot has been read (with B still unverified).
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid_a).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    orig = engine.canon.try_acquire_lock
    fired = {"done": False}

    def fake(*a, **k):
        # On degree's first lease attempt, simulate a concurrent process grounding the SIBLING edge B
        # (using the real lock), then report the lease as unavailable to the reconciler.
        if not fired["done"]:
            fired["done"] = True
            engine.canon.try_acquire_lock = orig
            engine.kg_ground(eid_b, "grounded", by="agent")
            engine.canon.try_acquire_lock = fake
        return False

    engine.canon.try_acquire_lock = fake
    report = recon.scan(full_sweep=True)
    engine.canon.try_acquire_lock = orig

    # the sibling verdict applied mid-sweep must NOT have been clobbered by a stale-snapshot write
    assert _edge(engine, eid_b).epistemic_state == EpistemicState.GROUNDED
    assert eid_a not in report.requarantined  # skipped this sweep (lease unavailable)

    # a subsequent lease-held sweep re-quarantines the forgery yet keeps the legitimate sibling verdict
    report2 = recon.scan(full_sweep=True)
    assert eid_a in report2.requarantined
    assert _edge(engine, eid_a).epistemic_state == EpistemicState.UNVERIFIED
    assert _edge(engine, eid_b).epistemic_state == EpistemicState.GROUNDED


# -------------------------------------------------------------------------- perf

def _audit_lines(recon: Reconciler):
    try:
        return [ln for ln in recon.audit_path.read_bytes().decode("utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def _seed_grounded_edge(engine):
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]})
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")
    return eid


def test_checkpoint_does_not_grow_audit_log(engine):
    """perf: repeated sweeps must not append a whole-ledger checkpoint to the append-only audit log. The
    log stays bounded by real verdict volume; the checkpoint lives in an overwritten sidecar."""
    _seed_grounded_edge(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    log_lines = len(_audit_lines(recon))
    assert log_lines >= 1  # the one real verdict record
    sidecar = recon._ground_audit.checkpoint_path
    assert sidecar.exists()

    for _ in range(6):
        recon.scan(full_sweep=True)

    # the audit log did NOT grow (no per-sweep checkpoint appended) and holds no checkpoint marker
    assert len(_audit_lines(recon)) == log_lines
    assert b"_ckpt" not in recon.audit_path.read_bytes()

    # the sidecar is OVERWRITTEN, not appended: exactly one JSON checkpoint object regardless of sweeps
    body = sidecar.read_text(encoding="utf-8")
    assert len([ln for ln in body.splitlines() if ln.strip()]) == 1
    rec = json.loads(body)  # a single well-formed object
    assert rec.get("_ckpt") == 1 and isinstance(rec.get("consumed"), dict)


def test_forgery_detection_still_works_after_sidecar_change(engine):
    """perf: moving the checkpoint out of the log must not weaken forge detection — an out-of-band forged
    verdict is still re-quarantined."""
    eid = _seed_grounded_edge(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    # demote legitimately out-of-band, sweep so the (sidecar) checkpoint records the demoted baseline
    node = engine.canon.read_node("degree")
    e = next(e for e in node.edges if e.id == eid)
    e.epistemic_state, e.verdict_by, e.verdict_at = EpistemicState.UNVERIFIED, None, None
    engine.canon.write_one(node)
    recon.scan(full_sweep=True)

    # forge it back to grounded (no kg_ground -> no new audit record)
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    report = recon.scan(full_sweep=True)
    assert eid in report.requarantined
    assert _edge(engine, eid).epistemic_state == EpistemicState.UNVERIFIED


def test_checkpoint_recovery_from_sidecar_after_cache_loss(engine):
    """perf + §1.8: losing the disposable reconcile-state cache must still not let an already-spent
    record justify a replay — the spend ledger is recovered from the sidecar checkpoint."""
    eid = _seed_grounded_edge(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)  # writes the sidecar checkpoint (consumed + grounded baseline)

    # demote out-of-band, sweep so the sidecar checkpoint records the demoted baseline + spent record
    node = engine.canon.read_node("degree")
    e = next(e for e in node.edges if e.id == eid)
    e.epistemic_state, e.verdict_by, e.verdict_at = EpistemicState.UNVERIFIED, None, None
    engine.canon.write_one(node)
    recon.scan(full_sweep=True)

    recon.state_path.unlink()  # lose ONLY the disposable cache; the sidecar checkpoint survives
    assert not recon.state_path.exists()
    assert recon._ground_audit.checkpoint_path.exists()

    # forge the verdict back to grounded out-of-band
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    report = recon.scan(full_sweep=True)
    assert eid in report.requarantined  # recovered spend ledger catches the replay
    assert _edge(engine, eid).epistemic_state == EpistemicState.UNVERIFIED


def test_groundaudit_write_checkpoint_overwrites_sidecar(tmp_path):
    """Unit: write_checkpoint OVERWRITES the sidecar (never appends); last_checkpoint reads the latest,
    and the append-only log file is never touched by the checkpoint."""
    log = GroundAuditLog(tmp_path / ".kg-ground-audit.jsonl")
    assert log.write_checkpoint({"k||grounded": 1}, {"k": "grounded"})
    assert log.write_checkpoint({"k||grounded": 2}, {"k": "grounded"})

    # a single record in the sidecar despite two writes
    body = log.checkpoint_path.read_text(encoding="utf-8")
    assert len([ln for ln in body.splitlines() if ln.strip()]) == 1
    assert log.last_checkpoint() == {"consumed": {"k||grounded": 2}, "epistemic": {"k": "grounded"}}

    # the append-only log file itself was never created by the checkpoint path
    assert not log.path.exists()
