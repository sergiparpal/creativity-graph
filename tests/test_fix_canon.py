"""Regression tests for canon.py fixes (F2, F15, F16, F28).

F2  — write_nodes: a git COMMIT failure must not roll back successfully-written canon.
F15 — LeaseLock.release: read-then-unlink TOCTOU must not delete a successor's reclaimed lock.
F16 — LeaseLock.heartbeat: 'refresh, never acquire' — a missing/foreign record stays untouched.
F28 — _check_slug_collision: an unreadable existing note is byte-backed-up before the overwrite.

M6  — _merge_into_existing: preserving a verdict must also carry its evidence (span/provenance/notes),
      not let a bare hypothesized re-proposal of an already-grounded edge erase the support.
M6b — reap_transient_files: bounded retention of `.*.bak` (newest N per note) + TTL-gated `.tmp-*` /
      sidelined lock reaping, so a long-lived vault doesn't grow transient dotfiles unbounded.
M6c — _reclaim_stale: a transient failure restoring a sidelined-but-LIVE record must not leave the
      canonical lock path empty (the live owner silently losing its lease).
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

import kg_engine.canon as canon_mod
from kg_engine.canon import (
    BACKUP_RETENTION_PER_NOTE,
    Canon,
    LeaseLock,
    LOCK_NAME,
    TRANSIENT_REAP_TTL,
)
from kg_engine.model import Edge, EpistemicState, Node, Provenance


@pytest.fixture
def rejecting_commit_vault(tmp_path: Path) -> Path:
    """A git repo whose `git commit` exits non-zero (a rejecting pre-commit hook). Real-world F2
    trigger (a rejecting hook / unset identity / index.lock): the commit fails after the atomic writes
    have already landed. `git add` still succeeds, so this isolates the COMMIT-step failure."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return tmp_path


# --------------------------------------------------------------------------- F2

def test_commit_failure_does_not_rollback_written_canon(rejecting_commit_vault: Path):
    """The atomic writes durably land BEFORE the commit; a real non-zero git exit (a rejecting hook /
    unset identity / index.lock) must NOT revert the already-fsynced files nor report a rollback (F2)."""
    canon = Canon(rejecting_commit_vault)
    # sanity: this vault genuinely cannot commit
    rc = subprocess.run(["git", "-C", str(rejecting_commit_vault), "commit", "--allow-empty", "-m", "x"],
                        capture_output=True, text=True).returncode
    assert rc != 0

    info = canon.write_nodes([Node(id="alpha", label="Alpha", body="kept")], message="batch alpha")

    # the commit failed, but the canon SURVIVES and we did NOT report a rollback
    assert info.rolled_back is False
    assert canon.exists("alpha")
    assert canon.read_node("alpha").body == "kept"
    assert canon.node_path("alpha").read_text(encoding="utf-8")  # real bytes on disk


def test_commit_failure_only_in_inner_try_no_longer_rolls_back(canon: Canon, monkeypatch):
    """Pin the exact bug: when the commit step itself fails, write_nodes must not route through
    _rollback. Monkeypatch the commit to raise and assert the snapshot restore never runs (F2)."""
    real_git = canon_mod._git
    rolled = {"called": False}
    real_rollback = Canon._rollback

    def fail_on_commit(repo, *args, check=True):
        if args[:1] == ("commit",):
            raise RuntimeError("rejecting pre-commit hook")
        return real_git(repo, *args, check=check)

    def spy_rollback(self, *a, **k):
        rolled["called"] = True
        return real_rollback(self, *a, **k)

    monkeypatch.setattr(canon_mod, "_git", fail_on_commit)
    monkeypatch.setattr(Canon, "_rollback", spy_rollback)

    # the commit raise now escapes (it is outside the rollback try) rather than triggering a rollback —
    # but the file is already on disk, and _rollback was NEVER invoked.
    with pytest.raises(RuntimeError):
        canon.write_nodes([Node(id="beta", label="Beta", body="kept")], message="batch beta")
    assert rolled["called"] is False
    assert canon.node_path("beta").exists()  # the durable write was not reverted


def test_write_failure_still_rolls_back(canon: Canon, monkeypatch):
    """The success-path commit moving outside the rollback try must NOT weaken the data-rollback: an
    actual _atomic_write failure still restores the pre-batch snapshot (F2 must not regress chaos)."""
    canon.write_nodes([Node(id="a", label="A")], message="seed a")
    real_write = canon_mod._atomic_write

    def boom(path, text):
        if path.name == "c.md":
            raise OSError("simulated crash mid-write")
        return real_write(path, text)

    monkeypatch.setattr(canon_mod, "_atomic_write", boom)
    info = canon.write_nodes([Node(id="b", label="B"), Node(id="c", label="C")], message="batch b,c")
    assert info.rolled_back is True
    assert canon.exists("a") and not canon.exists("b") and not canon.exists("c")


# --------------------------------------------------------------------------- F15

def test_release_does_not_delete_a_successors_reclaimed_lock(tmp_path, monkeypatch):
    """Our lease lapsed and a successor reclaimed the path between our ownership check and the unlink.
    release() must move-aside + re-validate, find the moved record foreign, restore it, and leave the
    successor's lock intact (F15) — never blind-unlink the path."""
    p = tmp_path / ".kg-session-lock"
    me = LeaseLock(p, ttl=1)

    # On disk: a SUCCESSOR's record (different pid) that reclaimed the path after our lease lapsed.
    successor = {"pid": me.pid + 1, "host": me.host,
                 "acquired_at": time.time(), "ttl": 120, "heartbeat_at": time.time()}
    p.write_text(json.dumps(successor), encoding="utf-8")

    # Force the ownership gate to pass once (the TOCTOU: we believed we still held it) — the real file
    # underneath belongs to the successor.
    monkeypatch.setattr(LeaseLock, "_read", lambda self: dict(successor, pid=self.pid))

    me.release()

    # the successor's lock must STILL be on disk, byte-identical — release didn't steal it
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == me.pid + 1


def test_release_removes_our_own_lock(tmp_path):
    """The normal case still works: release() removes a lock we genuinely hold."""
    p = tmp_path / ".kg-session-lock"
    lock = LeaseLock(p, ttl=120)
    assert lock.acquire()
    assert p.exists()
    lock.release()
    assert not p.exists()
    # no stray sidelined artifacts left behind
    assert not list(tmp_path.glob(".kg-session-lock.release-*"))


# --------------------------------------------------------------------------- F16

def test_heartbeat_is_noop_when_record_missing(tmp_path):
    """heartbeat() must refresh, never ACQUIRE: with no lock on disk it must NOT create one (the old
    blind-write would have minted a fresh self-owned record without acquire()'s CAS) (F16)."""
    p = tmp_path / ".kg-session-lock"
    lock = LeaseLock(p, ttl=120)
    assert not p.exists()
    lock.heartbeat()
    assert not p.exists()  # still nothing — heartbeat did not acquire


def test_heartbeat_does_not_overwrite_a_foreign_lock(tmp_path):
    """A foreign lock on disk is left untouched by our heartbeat."""
    p = tmp_path / ".kg-session-lock"
    foreign = {"pid": 999999, "host": "other-host",
               "acquired_at": 1.0, "ttl": 120, "heartbeat_at": 1.0}
    p.write_text(json.dumps(foreign), encoding="utf-8")
    LeaseLock(p, ttl=120).heartbeat()
    assert json.loads(p.read_text(encoding="utf-8")) == foreign


def test_heartbeat_refreshes_our_own_lock(tmp_path):
    """The legitimate use survives: heartbeat extends a lock we hold."""
    p = tmp_path / ".kg-session-lock"
    lock = LeaseLock(p, ttl=2)
    lock.acquire(now=1000.0)
    assert not lock.is_stale(now=1001.0)
    lock.heartbeat(now=1001.5)
    assert not lock.is_stale(now=1003.0)  # deadline pushed forward by the heartbeat


# --------------------------------------------------------------------------- F28

def test_unreadable_existing_note_is_backed_up_before_overwrite(canon: Canon):
    """An unreadable note at the target path is byte-backed-up before the self-heal overwrite, so a
    foreign/corrupt note is recoverable rather than silently destroyed (F28)."""
    p = canon.node_path("gamma")
    # not valid node markdown -> node_from_markdown raises -> the unreadable branch
    p.write_bytes(b"\x00\x01 not parseable frontmatter \x02\x03")
    original = p.read_bytes()

    canon.write_one(Node(id="gamma", label="Gamma", body="healed"))

    # the overwrite (self-heal) happened
    assert canon.read_node("gamma").body == "healed"
    # and a byte-identical backup of the unreadable original was preserved as a hidden sibling
    backups = list(canon.notes_dir.glob(".gamma.md.unreadable-*.bak"))
    assert backups, "expected a byte-backup of the unreadable note"
    assert backups[0].read_bytes() == original
    # the backup is a dotfile -> never surfaced as a phantom node
    assert "gamma" in {n.id for n in canon.all_nodes()}
    assert all(not pth.name.startswith(".") for pth in canon.note_paths())


# --------------------------------------------------------------------------- M6

def test_merge_preserves_promoted_verdict_evidence_not_just_state(canon: Canon):
    """A re-proposal that re-emits an already-grounded (and promotion-upgraded) edge as a bare
    `hypothesized`/`unverified` object must NOT erase the verdict's evidence on the merge: the stored
    edge keeps prev's span, provenance, and notes — not just its epistemic_state (M6)."""
    grounded = Edge(
        source="alpha", target="beta", relation="supports",
        provenance=Provenance.SPAN_PRESENT, epistemic_state=EpistemicState.GROUNDED,
        span="the verbatim support span", verdict_by="agent", verdict_at="2026-01-01T00:00:00+00:00",
        notes="citation: §3 corroborates this",
    )
    canon.write_nodes([Node(id="alpha", label="Alpha", edges=[grounded])], message="ground it")

    # a bare hypothesized re-proposal of the SAME edge id (kg_propose lane skips the verdict_ids check)
    reproposed = Edge(
        source="alpha", target="beta", relation="supports",
        provenance=Provenance.HYPOTHESIZED, epistemic_state=EpistemicState.UNVERIFIED,
        span="", notes="",
    )
    canon.write_nodes([Node(id="alpha", label="Alpha", edges=[reproposed])], message="re-propose")

    stored = next(e for e in canon.read_node("alpha").edges if e.id == grounded.id)
    # the verdict state survives (the pre-existing defense-in-depth) ...
    assert stored.epistemic_state == EpistemicState.GROUNDED
    assert stored.verdict_by == "agent"
    # ... AND so does the evidence it rests on (the M6 fix): provenance not reverted to hypothesized,
    # support span not blanked, citation note not dropped.
    assert stored.provenance == Provenance.SPAN_PRESENT
    assert stored.span == "the verbatim support span"
    assert stored.notes == "citation: §3 corroborates this"


def test_merge_keeps_failed_edge_falsification_notes(canon: Canon):
    """Defense-in-depth: even a `failed` edge's falsification rationale (§1.7 negative information)
    survives a merge that re-emits it bare — the notes are carried from prev, not lost (M6)."""
    failed = Edge(
        source="x", target="y", relation="contradicts",
        provenance=Provenance.INFERRED, epistemic_state=EpistemicState.FAILED,
        verdict_by="agent", notes="counter: refuted by §3, see attacked_by e_z",
    )
    canon.write_nodes([Node(id="x", label="X", edges=[failed])], message="fail it")
    bare = Edge(source="x", target="y", relation="contradicts",
                provenance=Provenance.HYPOTHESIZED, epistemic_state=EpistemicState.UNVERIFIED)
    canon.write_nodes([Node(id="x", label="X", edges=[bare])], message="re-emit")

    stored = next(e for e in canon.read_node("x").edges if e.id == failed.id)
    assert stored.epistemic_state == EpistemicState.FAILED
    assert stored.notes == "counter: refuted by §3, see attacked_by e_z"


# --------------------------------------------------------------------------- M6b (reaper)

def test_reaper_keeps_newest_n_backups_per_note(canon: Canon):
    """reap_transient_files keeps the newest BACKUP_RETENTION_PER_NOTE `.*.bak` per note (honoring the
    F28 recoverability intent) and prunes the older ones (M6b)."""
    base = ".gamma.md.unreadable-"
    # millisecond stamps sort lexicographically here, newest = largest stamp
    stamps = [1000, 2000, 3000, 4000, 5000]
    for ms in stamps:
        (canon.notes_dir / f"{base}{ms}.bak").write_bytes(b"x")

    removed = canon.reap_transient_files()

    survivors = sorted(p.name for p in canon.notes_dir.glob(".gamma.md.unreadable-*.bak"))
    assert len(survivors) == BACKUP_RETENTION_PER_NOTE
    # the newest N survive; the oldest are gone
    kept_stamps = [int(n[len(base):-len(".bak")]) for n in survivors]
    assert kept_stamps == sorted(stamps)[-BACKUP_RETENTION_PER_NOTE:]
    assert removed == len(stamps) - BACKUP_RETENTION_PER_NOTE


def test_reaper_ttl_gates_tmp_and_lock_sidelines(canon: Canon):
    """Crash-leftover `.tmp-*` and sidelined locks are reaped ONLY once older than the reap TTL — a
    fresh one (a live atomic-write/reclaim in flight) is never raced (M6b)."""
    import os
    old_tmp = canon.notes_dir / ".tmp-abcd.md"
    new_tmp = canon.notes_dir / ".tmp-efgh.md"
    old_stale = canon.root / f"{LOCK_NAME}.stale-1-1"
    old_release = canon.root / f"{LOCK_NAME}.release-1-1"
    for p in (old_tmp, new_tmp, old_stale, old_release):
        p.write_bytes(b"x")

    now = time.time()
    old = now - TRANSIENT_REAP_TTL - 10
    for p in (old_tmp, old_stale, old_release):
        os.utime(p, (old, old))

    canon.reap_transient_files(now=now)

    assert not old_tmp.exists()      # crash leftover, aged out -> reaped
    assert not old_stale.exists()    # sidelined lock, aged out -> reaped
    assert not old_release.exists()
    assert new_tmp.exists()          # fresh temp -> never raced


def test_reaper_does_not_touch_real_notes(canon: Canon):
    """The reaper only sweeps transient dotfiles — real notes (and a live lock) are untouched (M6b)."""
    canon.write_one(Node(id="real", label="Real", body="kept"))
    assert canon.lock.acquire()  # a live lock record sits at root/LOCK_NAME

    canon.reap_transient_files()

    assert canon.exists("real")
    assert (canon.root / LOCK_NAME).exists()
    canon.lock.release()


# --------------------------------------------------------------------------- M6c

def test_reclaim_does_not_orphan_live_record_on_restore_failure(tmp_path, monkeypatch):
    """If we sideline a record that turns out LIVE and the reverse rename fails transiently, the
    canonical path must not be left empty: the live record's content is written back so the owner does
    not silently lose its lease (M6c)."""
    p = tmp_path / LOCK_NAME
    me = LeaseLock(p, ttl=120)
    now = 1000.0
    # A LIVE record on disk (fresh heartbeat, same host, a pid that probes alive = our own pid).
    live = {"pid": me.pid, "host": me.host, "acquired_at": now, "ttl": 120, "heartbeat_at": now}
    p.write_text(json.dumps(live), encoding="utf-8")

    real_replace = canon_mod.os.replace

    def flaky_replace(src, dst, *a, **k):
        # fail ONLY the reverse restore rename (sidelined `.stale-*` -> canonical path); let the
        # forward move-aside and the content-copy fallback's atomic-write replace succeed.
        if ".stale-" in Path(src).name:
            raise OSError("transient EIO on reverse rename")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(canon_mod.os, "replace", flaky_replace)

    # the record is LIVE, so reclaim must FAIL (return False) and not steal it ...
    assert me._reclaim_stale(now) is False
    # ... and crucially the canonical path is NOT left empty — the live record is back at p
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8"))["pid"] == me.pid
    # no sidelined leftover (the content-copy fallback unlinks it)
    assert not list(tmp_path.glob(f"{LOCK_NAME}.stale-*"))
