"""Regression tests for the two review nits deferred from the first fix pass and completed here:

- F29 (reconciler-6): scan() builds its live file/key sets INCREMENTALLY instead of a second full
  all_nodes() re-parse per sweep. These tests pin the safety-critical property that the incremental
  live_keys is BYTE-IDENTICAL to the old all_nodes()-derived set, so pruning of the §1.8 forged-verdict
  baseline is unchanged — plus a spy proving the second full read is actually gone, and a legacy-state
  backfill case.
- F37: a read-only consumer (the precontext PreToolUse hook, fired on every Grep/Glob/Read) can
  construct Canon(..., ensure_layout=False) WITHOUT the constructor side effects (canon-dir mkdir +
  .git/info/exclude rewrite), while the default stays eager for every writer.
"""
from __future__ import annotations

from pathlib import Path

from kg_engine.canon import CANON_SUBDIR, Canon
from kg_engine.model import Node
from kg_engine.reconciler import Reconciler


def _seed_three_edges(engine) -> None:
    """Three span-verifying edges (spans are substrings of conftest SOURCE) -> several canon notes."""
    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"},
        {"source": "entropy", "target": "arrow-of-time", "relation": "grounds",
         "span": "Entropy grounds the arrow of time", "authored_by": "agent"},
        {"source": "canon", "target": "trust", "relation": "grounds",
         "span": "The canon grounds trust", "authored_by": "agent"},
    ]})


def _all_nodes_derived_keys(canon: Canon) -> set[str]:
    """The OLD computation the prune block used: live_keys off a full all_nodes() re-parse."""
    nodes = canon.all_nodes()
    return {f"node:{n.id}" for n in nodes} | {e.id for n in nodes for e in n.edges}


# --------------------------------------------------------------------------- F29

def test_f29_incremental_live_keys_equal_all_nodes_derived(engine):
    """After a full sweep, every parsed node/edge key is recorded in the epistemic baseline, and the
    prune keeps exactly the live ones — so the saved baseline keys must equal the old all_nodes()
    computation. This is the equivalence guarantee that makes the perf change behavior-preserving."""
    _seed_three_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    saved = set(recon._load_state()["epistemic"])
    assert saved == _all_nodes_derived_keys(engine.canon)
    assert saved  # non-trivial


def test_f29_scan_no_longer_calls_all_nodes(engine):
    """The whole point of reconciler-6: scan() must not re-read+parse the entire canon a second time
    via all_nodes(). Spy on it and assert zero calls during a sweep."""
    _seed_three_edges(engine)
    recon = Reconciler(engine.canon)

    calls = []
    orig = engine.canon.all_nodes
    engine.canon.all_nodes = lambda: (calls.append(1), orig())[1]
    try:
        recon.scan(full_sweep=True)
    finally:
        engine.canon.all_nodes = orig
    assert calls == [], "scan() should build live_keys incrementally, not via a second all_nodes() read"


def test_f29_cheap_path_does_not_overprune(engine):
    """A second (cheap-path) scan over unchanged files must NOT prune their baseline keys. The cheap
    path recomputes the live set authoritatively via all_nodes(), so the baseline is preserved intact."""
    _seed_three_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)
    expected = _all_nodes_derived_keys(engine.canon)

    recon.scan(full_sweep=False)  # cheap pre-gate path
    assert set(recon._load_state()["epistemic"]) == expected


def test_f29_cheap_path_uses_authoritative_all_nodes(engine):
    """full_sweep=False keeps the authoritative all_nodes() recompute for pruning: the (mtime,size)
    cheap pre-gate can't guarantee cached-key freshness, so trusting cached keys there could leave a
    stale baseline key and reopen a delete→recreate→forge bypass. Only full_sweep=True takes the
    incremental fast path. This is the inverse of test_f29_scan_no_longer_calls_all_nodes."""
    _seed_three_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    calls = []
    orig = engine.canon.all_nodes
    engine.canon.all_nodes = lambda: (calls.append(1), orig())[1]
    try:
        recon.scan(full_sweep=False)
    finally:
        engine.canon.all_nodes = orig
    assert calls, "full_sweep=False must recompute live keys authoritatively via all_nodes()"


def test_f29_backfills_keys_from_legacy_state_without_them(engine):
    """A reconcile-state file written before key-caching has no 'keys' on its files entries. A full
    sweep must backfill them (one-time parse of a hash-matched note) rather than over-pruning the
    live baseline."""
    _seed_three_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)
    expected = _all_nodes_derived_keys(engine.canon)

    # simulate a pre-upgrade state: strip the cached keys from every file entry
    state = recon._load_state()
    for entry in state["files"].values():
        entry.pop("keys", None)
    recon._save_state(state)

    recon.scan(full_sweep=True)  # full-sweep unchanged branch backfills keys, doesn't over-prune
    healed = recon._load_state()
    assert set(healed["epistemic"]) == expected
    assert all("keys" in entry for entry in healed["files"].values())  # backfilled


def test_f29_deleted_node_keys_are_pruned(engine):
    """Equivalence in the prune direction: a deleted note's keys drop out of the live set, so its
    baseline is pruned — exactly as the old all_nodes()-derived live_keys would have done."""
    _seed_three_edges(engine)
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)

    (engine.canon.notes_dir / "entropy.md").unlink()
    recon.scan(full_sweep=True)

    saved = set(recon._load_state()["epistemic"])
    assert "node:entropy" not in saved
    assert saved == _all_nodes_derived_keys(engine.canon)  # still equal after a deletion


# --------------------------------------------------------------------------- F37

def test_f37_ensure_layout_false_skips_mkdir_and_git_excludes(tmp_path: Path):
    """A read-only Canon construction must not create the canon dir or rewrite .git/info/exclude."""
    (tmp_path / ".git" / "info").mkdir(parents=True)  # a git-backed vault, so excludes WOULD be written

    Canon(tmp_path, ensure_layout=False)
    assert not (tmp_path / CANON_SUBDIR).exists()                 # no canon dir created
    assert not (tmp_path / ".git" / "info" / "exclude").exists()  # excludes not touched


def test_f37_default_construction_stays_eager(tmp_path: Path):
    """The default (writer) path is unchanged: it still creates the canon dir and the git excludes."""
    (tmp_path / ".git" / "info").mkdir(parents=True)

    Canon(tmp_path)  # ensure_layout defaults True
    assert (tmp_path / CANON_SUBDIR).exists()
    assert (tmp_path / ".git" / "info" / "exclude").exists()


def test_f37_read_only_canon_still_serves_reads(engine):
    """The precontext use case: a read-only Canon over an EXISTING canon reads notes normally."""
    _seed_three_edges(engine)
    ro = Canon(engine.canon.root, ensure_layout=False)
    assert any(n.id == "degree" for n in ro.all_nodes())  # reads served, no side effects needed


def test_f37_write_through_read_only_canon_self_heals_dir(tmp_path: Path):
    """Belt-and-suspenders: even though ensure_layout=False skips the eager mkdir, a write still
    self-heals the canon dir via _atomic_write_bytes' parent mkdir, so the instance is not crippled."""
    c = Canon(tmp_path, ensure_layout=False)
    assert not (tmp_path / CANON_SUBDIR).exists()

    c.write_one(Node(id="x", label="x"))
    assert (tmp_path / CANON_SUBDIR / "x.md").exists()
