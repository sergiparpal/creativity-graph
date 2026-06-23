"""Regression tests for canon.py fixes (F2, F15, F16, F28).

F2  — write_nodes: a git COMMIT failure must not roll back successfully-written canon.
F15 — LeaseLock.release: read-then-unlink TOCTOU must not delete a successor's reclaimed lock.
F16 — LeaseLock.heartbeat: 'refresh, never acquire' — a missing/foreign record stays untouched.
F28 — _check_slug_collision: an unreadable existing note is byte-backed-up before the overwrite.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

import kg_engine.canon as canon_mod
from kg_engine.canon import Canon, LeaseLock
from kg_engine.model import Node


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
