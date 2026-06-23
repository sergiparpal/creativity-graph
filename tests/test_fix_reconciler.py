"""Reconciler fail-open hardening (§1.8): a corrupt/non-conforming reconcile-state file must NOT crash
scan() (the crash is swallowed by the background reconcile and never self-heals, silently disabling the
sweep). Companion to test_reconciler.py — covers the state-shape cases the review found (F19), plus the
non-canonical-filename on-disk neutralization (F3) at the Reconciler level.
"""
from __future__ import annotations

import json

from kg_engine.canon import _atomic_write
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
