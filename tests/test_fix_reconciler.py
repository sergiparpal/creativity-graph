"""Reconciler fail-open hardening (§1.8): a corrupt/non-conforming reconcile-state file must NOT crash
scan() (the crash is swallowed by the background reconcile and never self-heals, silently disabling the
sweep). Companion to test_reconciler.py — covers the state-shape cases the review found (F19), plus the
non-canonical-filename on-disk neutralization (F3) at the Reconciler level.
"""
from __future__ import annotations

import json

from kg_engine import reconciler as reconciler_mod
from kg_engine.canon import GROUND_AUDIT, _atomic_write
from kg_engine.groundaudit import GroundAuditLog
from kg_engine.model import Edge, EpistemicState, Node, edge_id, node_to_markdown
from kg_engine.reconciler import Reconciler


def _state_path(recon: Reconciler):
    return recon.state_path


def test_valid_json_non_dict_state_does_not_crash_scan(canon):
    """F19: a valid-JSON-but-non-dict state file ([1,2,3], or top-level null) must fall through to the
    fresh-dict default instead of crashing scan() with AttributeError on .get()."""
    recon = Reconciler(canon)
    for bad in ("[1, 2, 3]", "null", '"a string"', "42"):
        _atomic_write(recon.state_path, bad)
        report = recon.scan(full_sweep=True)  # must not raise
        assert report.full_sweep is True
        # scan healed the state: it is now a well-formed dict with the three sub-keys
        healed = json.loads(recon.state_path.read_text(encoding="utf-8"))
        assert isinstance(healed, dict)
        assert set(healed) == {"files", "epistemic", "consumed"}


def test_null_sub_key_state_does_not_crash_scan(canon):
    """F19: a `{"files": null}` (or any null/non-dict sub-key) must be coerced to an empty dict, not
    crash scan() when it tries to .get()/index the sub-value."""
    recon = Reconciler(canon)
    # seed a real note so the scan loop actually exercises files_state writes
    node = Node(id="alpha", label="alpha", edges=[])
    canon.write_one(node)
    for bad in (
        {"files": None, "epistemic": {}, "consumed": {}},
        {"files": {}, "epistemic": None, "consumed": {}},
        {"files": {}, "epistemic": {}, "consumed": None},
        {"files": [1, 2], "epistemic": {}, "consumed": {}},  # non-dict (list), not just null
        {"files": None},  # missing sub-keys entirely + null
    ):
        _atomic_write(recon.state_path, json.dumps(bad))
        report = recon.scan(full_sweep=True)  # must not raise
        assert report.full_sweep is True
        healed = json.loads(recon.state_path.read_text(encoding="utf-8"))
        assert isinstance(healed.get("files"), dict)
        assert isinstance(healed.get("epistemic"), dict)
        assert isinstance(healed.get("consumed"), dict)


def test_non_canonical_filename_correction_lands_canonically(canon):
    """F3: the un-forgery correction for a non-slug-canonical note must be written to the CANONICAL slug
    path and the stale original deleted — one file, one edge, reset to UNVERIFIED — not a duplicate."""
    forged = Edge(source="Foo", target="bar", relation="grounds", span="x",
                  epistemic_state=EpistemicState.GROUNDED)
    node = Node(id="Foo", label="Foo", edges=[forged])
    (canon.notes_dir / "Foo.md").write_text(node_to_markdown(node), encoding="utf-8")

    recon = Reconciler(canon)
    report = recon.scan(full_sweep=True)
    eid = edge_id("Foo", "grounds", "bar")
    assert eid in report.requarantined

    # exactly one note on disk (the canonical one), one edge, reset to UNVERIFIED
    paths = canon.note_paths()
    assert [p.name for p in paths] == [canon.node_path("Foo").name]
    edges = [e for e in canon.all_edges() if e.id == eid]
    assert len(edges) == 1
    assert edges[0].epistemic_state == EpistemicState.UNVERIFIED

    # and the files_state baseline points at the canonical file (not the now-deleted original), so the
    # next scan does not resurrect / re-stat a phantom path.
    state = json.loads(recon.state_path.read_text(encoding="utf-8"))
    assert "Foo.md" not in state["files"]
    assert canon.node_path("Foo").name in state["files"]

    # a second scan is stable: nothing re-quarantined, still one note, still UNVERIFIED.
    report2 = recon.scan(full_sweep=True)
    assert eid not in report2.requarantined
    assert len(canon.note_paths()) == 1
    edges2 = [e for e in canon.all_edges() if e.id == eid]
    assert len(edges2) == 1 and edges2[0].epistemic_state == EpistemicState.UNVERIFIED


def test_noncanonical_correction_unlinks_original_before_writing(canon, monkeypatch):
    """Regression for the macOS/Windows CI failure (case-insensitive filesystems): a non-canonically
    named note (Foo.md for id 'Foo', slug foo.md) must have its original UNLINKED BEFORE the canonical
    write. There Foo.md and foo.md are one file, so the old write-then-unlink deleted the just-written
    note (FileNotFoundError) and a case-preserving replace kept the stale 'Foo.md' name; unlink-first
    lets write_one create a fresh, correctly-cased foo.md. We assert the ordering directly so a
    case-sensitive CI host (Linux) guards it too."""
    forged = Edge(source="Foo", target="bar", relation="grounds", span="x",
                  epistemic_state=EpistemicState.GROUNDED)
    node = Node(id="Foo", label="Foo", edges=[forged])
    original = canon.notes_dir / "Foo.md"
    original.write_text(node_to_markdown(node), encoding="utf-8")

    seen = {}
    real_write_one = canon.write_one
    def spy_write_one(n, *a, **k):
        seen["original_present_at_write"] = original.exists()
        return real_write_one(n, *a, **k)
    monkeypatch.setattr(canon, "write_one", spy_write_one)

    report = Reconciler(canon).scan(full_sweep=True)
    assert forged.id in report.requarantined
    # the non-canonical original was removed BEFORE the canonical write (the ordering that is correct on
    # a case-insensitive filesystem) — the old write-first code would have it still present here.
    assert seen.get("original_present_at_write") is False
    # and the end state is the canonical single note with the edge reset to UNVERIFIED
    assert [p.name for p in canon.note_paths()] == [canon.node_path("Foo").name]
    edges = [e for e in canon.all_edges() if e.id == forged.id]
    assert len(edges) == 1 and edges[0].epistemic_state == EpistemicState.UNVERIFIED


def _seed_grounded_edge(engine) -> str:
    """Write one span-verifying edge and ground it (audited). Returns its edge id."""
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]})
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")
    return eid


def test_idempotent_reground_surplus_cannot_later_justify_a_forgery(engine):
    """H1/[2]: an idempotent re-ground (grounded->grounded) appends a SECOND audit record the
    `last == current` branch never spends, leaving a spendable surplus. A later out-of-band forgery
    back into `grounded` must NOT be able to spend that surplus and slip past — the reconciler must
    drain the surplus so the forgery is still caught (§1.8 forge-detection bypass)."""
    eid = _seed_grounded_edge(engine)            # audit grounded=1
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)                  # baseline grounded, consumes record #1

    engine.kg_ground(eid, "grounded", by="agent")  # legitimate idempotent re-verify: audit grounded=2
    recon.scan(full_sweep=True)                  # last==grounded: must DRAIN the surplus (consumed->2)

    # the baseline legitimately moves OFF grounded (an OOB edit to unverified, which reconcile permits)
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.UNVERIFIED
    next(e for e in node.edges if e.id == eid).verdict_by = None
    next(e for e in node.edges if e.id == eid).verdict_at = None
    engine.canon.write_one(node)
    recon.scan(full_sweep=True)                  # baseline now unverified

    # now FORGE grounded out-of-band (no kg_ground, no new audit record)
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    report = recon.scan(full_sweep=True)
    # the drained surplus is gone, so the forged grounded has no record to justify it -> re-quarantined
    assert eid in report.requarantined
    after = next(e for e in engine.canon.all_edges() if e.id == eid)
    assert after.epistemic_state == EpistemicState.UNVERIFIED


def test_verdict_applied_mid_sweep_is_not_reverted(engine):
    """H3+M2/[1]: the audit snapshot is captured ONCE at the top of scan(); the per-session reconcile
    runs in a separate process concurrently with kg_ground. A legitimate verdict whose audit record
    lands AFTER the snapshot but before this note is read must NOT be re-quarantined — _forged must
    re-read the audit log fresh before declaring a forgery."""
    # an edge grounded on disk whose audit record does NOT yet exist in the snapshot
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]})
    eid = edge_id("degree", "approximates", "importance")
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    recon = Reconciler(engine.canon)
    log = GroundAuditLog(engine.canon.root / GROUND_AUDIT)
    real_audit_counts = recon._audit_counts
    state = {"n": 0}

    def racing_audit_counts():
        # FIRST call is the top-of-sweep snapshot: model the concurrent kg_ground landing its record
        # right AFTER the snapshot (so the snapshot is stale/empty for this pair). Subsequent calls
        # (the fresh re-read inside _forged) then observe the just-appended record.
        state["n"] += 1
        if state["n"] == 1:
            out = real_audit_counts()
            log.append(eid, "unverified", "grounded", "agent")  # verdict lands mid-sweep
            return out
        return real_audit_counts()

    recon._audit_counts = racing_audit_counts
    report = recon.scan(full_sweep=True)

    # the mid-sweep record is honored on the fresh re-read -> the legitimate verdict survives
    assert eid not in report.requarantined
    after = next(e for e in engine.canon.all_edges() if e.id == eid)
    assert after.epistemic_state == EpistemicState.GROUNDED


def test_reconcile_does_not_clobber_a_concurrent_sibling_verdict(engine, monkeypatch):
    """M2/[3]: re-quarantining edge A on node N is a read->mutate->write. A concurrent kg_ground that
    grounds a SIBLING edge B on N between the reconciler's read and write must not be clobbered by the
    reconciler writing its stale copy. The fix re-reads N fresh UNDER THE LEASE before writing."""
    # A: forged grounded (no audit) -> will be re-quarantined.  B: starts unverified.
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"},
        {"source": "degree", "target": "trust", "relation": "grounds",
         "span": "The canon grounds trust", "authored_by": "agent"}]})
    a_eid = edge_id("degree", "approximates", "importance")
    b_eid = edge_id("degree", "grounds", "trust")
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == a_eid).epistemic_state = EpistemicState.GROUNDED  # forged
    engine.canon.write_one(node)

    recon = Reconciler(engine.canon)
    log = GroundAuditLog(engine.canon.root / GROUND_AUDIT)

    # simulate the concurrent kg_ground on B landing between the reconciler's snapshot read and its
    # under-lease re-read: the FIRST node_from_markdown (the snapshot read) also writes B grounded +
    # its audit record to disk, so the real under-lease re-read observes B grounded and legitimate.
    real_parse = reconciler_mod.node_from_markdown
    state = {"n": 0}

    def racing_parse(text, *a, **k):
        state["n"] += 1
        parsed = real_parse(text, *a, **k)
        if state["n"] == 1 and any(e.id == a_eid for e in parsed.edges):
            disk = engine.canon.read_node("degree")
            be = next(e for e in disk.edges if e.id == b_eid)
            be.epistemic_state = EpistemicState.GROUNDED
            be.verdict_by = "agent"
            engine.canon.write_one(disk)
            log.append(b_eid, "unverified", "grounded", "agent")  # B legitimately audited
        return parsed

    monkeypatch.setattr(reconciler_mod, "node_from_markdown", racing_parse)
    report = recon.scan(full_sweep=True)

    # A's forgery is reset; B's concurrently-applied, audited verdict is NOT lost
    assert a_eid in report.requarantined
    assert b_eid not in report.requarantined
    after = {e.id: e.epistemic_state for e in engine.canon.all_edges()}
    assert after[a_eid] == EpistemicState.UNVERIFIED
    assert after[b_eid] == EpistemicState.GROUNDED


def test_concurrent_note_delete_does_not_crash_the_sweep(canon, monkeypatch):
    """[7]: scan() holds no lease, so a lease-holding writer can unlink/rename a note between
    note_paths()'s snapshot and the per-note stat()/hash. An unguarded raise would abort the WHOLE
    §1.8 sweep before _save_state (silently disabling forge-detection). A vanished note must be skipped,
    not crash the sweep."""
    canon.write_one(Node(id="alpha", label="alpha", edges=[]))
    canon.write_one(Node(id="beta", label="beta", edges=[]))

    recon = Reconciler(canon)
    gone = canon.node_path("alpha")
    real_sha256 = reconciler_mod._sha256

    def vanishing_sha256(p):
        # model a concurrent unlink (kg_rename) landing right before this note's hash read
        if p == gone and gone.exists():
            gone.unlink()
        return real_sha256(p)

    monkeypatch.setattr(reconciler_mod, "_sha256", vanishing_sha256)
    report = recon.scan(full_sweep=True)  # must not raise

    # the sweep COMPLETED (state was saved) and the surviving note was still processed
    assert json.loads(recon.state_path.read_text(encoding="utf-8"))  # _save_state ran
    assert canon.node_path("beta").name in json.loads(recon.state_path.read_text(encoding="utf-8"))["files"]
