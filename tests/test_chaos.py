"""Stage 1 exit test: crash-mid-write recovery, stale-lock reclamation, OOB-verdict re-quarantine.
Plus light fuzzing of the markdown round-trip.
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest

import kg_engine.canon as canon_mod
from kg_engine.canon import Canon, LeaseLock
from kg_engine.model import Edge, EpistemicState, Node, node_from_markdown, node_to_markdown
from kg_engine.reconciler import Reconciler


def _git_clean(repo) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return r.stdout.strip() == ""


# ---- crash mid-write -----------------------------------------------------

def test_crash_mid_write_recovers_via_git(canon: Canon, monkeypatch):
    canon.write_nodes([Node(id="a", label="A")], message="seed a")
    assert canon.exists("a") and _git_clean(canon.root)

    real_write = canon_mod._atomic_write

    def boom(path, text):
        if path.name == "c.md":
            raise OSError("simulated crash mid-write")
        return real_write(path, text)

    monkeypatch.setattr(canon_mod, "_atomic_write", boom)
    info = canon.write_nodes([Node(id="b", label="B"), Node(id="c", label="C")], message="batch b,c")

    assert info.rolled_back is True  # the batch was rolled back (scoped to its own files; no stash)
    assert canon.exists("a") and not canon.exists("b") and not canon.exists("c")  # batch undone
    assert _git_clean(canon.root)  # vault consistent


def test_rollback_preserves_unrelated_uncommitted_work(canon: Canon, monkeypatch):
    """A failed batch must roll back ONLY its own files — never a repo-wide `git reset --hard HEAD`
    that would also discard the uncommitted grounding verdicts kg_ground writes via write_one (no
    commit), or in-progress hand edits (canon-2 / integration-2)."""
    canon.write_nodes([Node(id="keep", label="Keep", body="original")], message="seed keep")
    # an UNCOMMITTED change to an unrelated node — exactly what kg_ground does (write_one, no commit)
    node = canon.read_node("keep")
    node.body = "grounded-verdict-uncommitted"
    canon.write_one(node)

    real_write = canon_mod._atomic_write

    def boom(path, text):
        if path.name == "c.md":
            raise OSError("simulated crash mid-write")
        return real_write(path, text)

    monkeypatch.setattr(canon_mod, "_atomic_write", boom)
    info = canon.write_nodes([Node(id="b", label="B"), Node(id="c", label="C")], message="batch b,c")

    assert info.rolled_back is True
    assert not canon.exists("b") and not canon.exists("c")  # the failed batch is undone
    # the unrelated uncommitted work SURVIVES (the old reset --hard HEAD would have reverted it)
    assert canon.read_node("keep").body == "grounded-verdict-uncommitted"


# ---- lease lock ----------------------------------------------------------

def test_stale_lock_always_reclaimed(tmp_path):
    import os
    p = tmp_path / ".kg-session-lock"

    # (a) dead pid, fresh heartbeat -> stale (pid not alive). POSIX-only: Windows cannot probe pid
    # liveness without os.kill(pid, 0) sending a console CTRL_C event, so it relies on the TTL (case b)
    # rather than pid-death detection.
    if os.name != "nt":
        p.write_text(json.dumps({"pid": 2 ** 30, "host": LeaseLock(p).host,
                                 "acquired_at": time.time(), "ttl": 120, "heartbeat_at": time.time()}))
        lock = LeaseLock(p, ttl=120)
        assert lock.is_stale() and lock.acquire()

    # (b) expired heartbeat -> stale (ttl exceeded) — the cross-platform staleness signal
    p.write_text(json.dumps({"pid": os.getpid(), "host": LeaseLock(p).host,
                             "acquired_at": 0, "ttl": 1, "heartbeat_at": time.time() - 999}))
    assert LeaseLock(p, ttl=1).is_stale() and LeaseLock(p, ttl=1).acquire()


def test_live_lock_not_reclaimed(tmp_path):
    p = tmp_path / ".kg-session-lock"
    holder = LeaseLock(p, ttl=120)
    assert holder.acquire()
    # a different session must NOT steal a fresh, live lock
    other = LeaseLock(p, ttl=120, pid=2 ** 30 + 1)  # pretend different pid; holder pid is alive
    assert not other.is_stale()
    assert not other.acquire()
    holder.release()
    assert other.acquire()  # released -> now free


def test_heartbeat_keeps_lock_fresh(tmp_path):
    p = tmp_path / ".kg-session-lock"
    lock = LeaseLock(p, ttl=2)
    lock.acquire(now=1000.0)
    assert not lock.is_stale(now=1001.0)
    lock.heartbeat(now=1001.5)
    assert not lock.is_stale(now=1003.0)  # heartbeat pushed the deadline forward


# ---- out-of-band verdict re-quarantine -----------------------------------

def test_oob_verdict_requarantined(canon: Canon):
    edge = Edge(source="a", target="b", relation="grounds", span="x",
                epistemic_state=EpistemicState.UNVERIFIED)
    canon.write_nodes([Node(id="a", label="A", edges=[edge])], message="seed")
    recon = Reconciler(canon)
    recon.scan(full_sweep=True)  # record baseline validated state

    # forge a verdict by editing the file out-of-band (no kg_ground, no audit record)
    p = canon.node_path("a")
    node = node_from_markdown(p.read_text())
    node.edges[0].epistemic_state = EpistemicState.GROUNDED
    p.write_text(node_to_markdown(node))

    report = recon.scan(full_sweep=True)
    assert edge.id in report.requarantined
    after = canon.read_node("a")
    assert after.edges[0].epistemic_state == EpistemicState.UNVERIFIED  # reset to quarantine


def test_audited_verdict_not_requarantined(canon: Canon):
    from kg_engine.reconciler import GROUND_AUDIT
    edge = Edge(source="a", target="b", relation="grounds", span="x")
    canon.write_nodes([Node(id="a", label="A", edges=[edge])], message="seed")
    recon = Reconciler(canon)
    recon.scan(full_sweep=True)

    # a legitimate verdict: write the audit record THEN flip the state (what kg_ground does)
    (canon.root / GROUND_AUDIT).write_text(
        json.dumps({"key": edge.id, "from": "unverified", "to": "grounded", "by": "agent"}) + "\n")
    p = canon.node_path("a")
    node = node_from_markdown(p.read_text())
    node.edges[0].epistemic_state = EpistemicState.GROUNDED
    p.write_text(node_to_markdown(node))

    report = recon.scan(full_sweep=True)
    assert edge.id not in report.requarantined
    assert canon.read_node("a").edges[0].epistemic_state == EpistemicState.GROUNDED


def test_full_sweep_detects_mtime_spoof(canon: Canon):
    edge = Edge(source="a", target="b", relation="grounds", span="x")
    canon.write_nodes([Node(id="a", label="A", edges=[edge])], message="seed")
    recon = Reconciler(canon)
    recon.scan(full_sweep=True)

    import os
    p = canon.node_path("a")
    st = p.stat()
    node = node_from_markdown(p.read_text())
    node.edges[0].epistemic_state = EpistemicState.FAILED  # forge
    p.write_text(node_to_markdown(node))
    os.utime(p, (st.st_atime, st.st_mtime))  # spoof mtime back

    # pre-filter alone (no sweep) would miss it; the full sweep re-hashes and catches it
    report = recon.scan(full_sweep=True)
    assert edge.id in report.requarantined


# ---- fuzzing -------------------------------------------------------------

@pytest.mark.parametrize("nid", ["a b/c", "../escape", "weird:id", "  spaces  ", "x" * 200])
def test_markdown_roundtrip_fuzz(nid):
    n = Node(id=nid, label=nid, edges=[Edge(source=nid, target="t", relation="grounds", span="s")])
    n2 = node_from_markdown(node_to_markdown(n))
    assert n2.id == nid and len(n2.edges) == 1
