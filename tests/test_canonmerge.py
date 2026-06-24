"""Tests for the semantic git canon merge driver (R5, kg_engine.canonmerge).

The driver is the out-of-process mirror of Canon._merge_into_existing: it unions edges by the
deterministic edge_id and demotes a cross-branch verdict conflict to `unverified` — never to either
side's verdict. The load-bearing property is that it is **structurally incapable of forging a verdict**:
the only epistemic_state it can write on a conflict is `unverified` (test_merge_never_yields_a_forged
_verdict sweeps every cross-state pair).
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kg_engine.canonmerge import _merge_body, main, merge_nodes, merge_note_files
from kg_engine.model import (
    Edge,
    EpistemicState,
    GROUNDABLE_STATES,
    Node,
    VERDICT_STATES,
    edge_id,
    node_from_markdown,
    node_to_markdown,
)

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
GIT = shutil.which("git")
TS = "2026-01-01T00:00:00+00:00"


def _edge(state: EpistemicState, *, source="n1", relation="grounds", target="b", span="alpha") -> Edge:
    has_verdict = state in GROUNDABLE_STATES  # verdicts + obsolete carry verdict_by/at
    return Edge(
        source=source, target=target, relation=relation, span=span, epistemic_state=state,
        verdict_by=("agent" if has_verdict else None),
        verdict_at=(TS if has_verdict else None),
    )


def _node(edges, *, nid="n1", body="", **kw) -> Node:
    return Node(id=nid, label=kw.pop("label", nid), body=body, edges=list(edges), **kw)


def _find(node: Node, eid: str) -> Edge:
    return next(e for e in node.edges if e.id == eid)


# --------------------------------------------------------------------------- edge union


def test_disjoint_edges_union():
    """Each side's exclusive edge survives the merge (never lose an edge a branch added, §1.4/§1.7)."""
    a = edge_id("n1", "grounds", "b")
    c = edge_id("n1", "bridges", "d")
    ours = _node([_edge(EpistemicState.UNVERIFIED, relation="grounds", target="b")])
    theirs = _node([_edge(EpistemicState.UNVERIFIED, relation="bridges", target="d")])
    merged, demotions = merge_nodes(None, ours, theirs)
    assert {e.id for e in merged.edges} == {a, c}
    assert demotions == []


def test_same_edge_both_sides_dedups():
    """The same edge id from both sides collapses to one (deterministic identity, §1.4)."""
    ours = _node([_edge(EpistemicState.UNVERIFIED)])
    theirs = _node([_edge(EpistemicState.UNVERIFIED)])
    merged, _ = merge_nodes(None, ours, theirs)
    assert len(merged.edges) == 1


def test_equal_verdict_preserved():
    """An edge GROUNDED on BOTH sides stays grounded — the merge only demotes a *disagreement*."""
    ours = _node([_edge(EpistemicState.GROUNDED)])
    theirs = _node([_edge(EpistemicState.GROUNDED)])
    merged, demotions = merge_nodes(None, ours, theirs)
    e = _find(merged, edge_id("n1", "grounds", "b"))
    assert e.epistemic_state is EpistemicState.GROUNDED
    assert e.verdict_by == "agent" and e.verdict_at == TS  # the verdict survives untouched
    assert demotions == []


def test_verdict_conflict_demotes_to_unverified():
    """grounded on one side, rejected on the other -> unverified, with verdict_by/at cleared."""
    ours = _node([_edge(EpistemicState.GROUNDED)])
    theirs = _node([_edge(EpistemicState.REJECTED)])
    merged, demotions = merge_nodes(None, ours, theirs)
    e = _find(merged, edge_id("n1", "grounds", "b"))
    assert e.epistemic_state is EpistemicState.UNVERIFIED
    assert e.verdict_by is None and e.verdict_at is None
    assert any("unverified" in d for d in demotions)


@pytest.mark.parametrize("s1", list(EpistemicState))
@pytest.mark.parametrize("s2", list(EpistemicState))
def test_merge_never_yields_a_forged_verdict(s1: EpistemicState, s2: EpistemicState):
    """Forge-proof sweep over EVERY cross-state pair: a DISAGREEMENT always resolves to `unverified`
    (verdict cleared); only an exact match is preserved. The driver can never *invent* a verdict that
    was not already identical on both sides (invariant 3, never-forge-a-verdict)."""
    ours = _node([_edge(s1)])
    theirs = _node([_edge(s2)])
    merged, _ = merge_nodes(None, ours, theirs)
    e = _find(merged, edge_id("n1", "grounds", "b"))
    if s1 == s2:
        assert e.epistemic_state is s1  # equal on both sides -> preserved
    else:
        assert e.epistemic_state is EpistemicState.UNVERIFIED
        assert e.verdict_by is None and e.verdict_at is None


def test_obsolete_pairs_are_covered_by_the_groundable_set():
    """OBSOLETE participates in the forge-proof guarantee too (GROUNDABLE_STATES = verdicts ∪ obsolete)."""
    assert EpistemicState.OBSOLETE in GROUNDABLE_STATES
    for verdict in VERDICT_STATES:
        ours = _node([_edge(EpistemicState.OBSOLETE)])
        theirs = _node([_edge(verdict)])
        merged, _ = merge_nodes(None, ours, theirs)
        assert _find(merged, edge_id("n1", "grounds", "b")).epistemic_state is EpistemicState.UNVERIFIED


def test_node_level_verdict_disagreement_demotes():
    """A node's own epistemic_state is demoted on disagreement too (same forge-proof rule)."""
    ours = _node([_edge(EpistemicState.UNVERIFIED)], epistemic_state=EpistemicState.GROUNDED)
    theirs = _node([_edge(EpistemicState.UNVERIFIED)], epistemic_state=EpistemicState.REJECTED)
    merged, _ = merge_nodes(None, ours, theirs)
    assert merged.epistemic_state is EpistemicState.UNVERIFIED


@pytest.mark.parametrize("verdict", sorted(GROUNDABLE_STATES, key=lambda s: s.value))
def test_theirs_only_verdict_edge_is_unioned_verbatim(verdict: EpistemicState):
    """Pins the `existing is None` union branch the cross-state sweep never reaches: a theirs-ONLY edge
    (no counterpart on ours) is unioned in verbatim, so a one-sided verdict SURVIVES the merge. The
    driver only demotes a *disagreement* — a verdict that lands with no local audit record is the
    reconciler's to re-quarantine (§1.8), never the merge driver's to silently strip or honor-as-final.
    A regression that special-cased verdict-bearing edges in the union branch would fail here."""
    ours = _node([_edge(EpistemicState.UNVERIFIED, relation="grounds", target="b")])
    theirs = _node([_edge(verdict, relation="bridges", target="d")])
    merged, demotions = merge_nodes(None, ours, theirs)
    theirs_edge = _find(merged, edge_id("n1", "bridges", "d"))
    assert theirs_edge.epistemic_state is verdict  # one-sided verdict preserved verbatim
    assert theirs_edge.verdict_by == "agent" and theirs_edge.verdict_at == TS
    # ours' edge is untouched and nothing was demoted (there was no disagreement)
    assert _find(merged, edge_id("n1", "grounds", "b")).epistemic_state is EpistemicState.UNVERIFIED
    assert demotions == []


# --------------------------------------------------------------------------- body 3-way


def test_body_one_sided_change_takes_that_side():
    """A body changed on exactly one side (the other == base) is taken without any conflict (no git)."""
    body, conflicts, ok = _merge_body("orig", "orig edited", "orig")
    assert body == "orig edited" and conflicts == [] and ok
    body, conflicts, ok = _merge_body("orig", "orig", "orig too")
    assert body == "orig too" and conflicts == [] and ok


def test_body_identical_sides_clean():
    body, conflicts, ok = _merge_body("orig", "same", "same")
    assert body == "same" and ok and conflicts == []


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_body_3way_merges_disjoint_edits_via_git():
    """Non-overlapping edits on both sides merge cleanly through `git merge-file`."""
    base = "line one\nline two\nline three\n"
    ours = "LINE ONE\nline two\nline three\n"
    theirs = "line one\nline two\nLINE THREE\n"
    body, conflicts, ok = _merge_body(base, ours, theirs)
    assert ok and conflicts == []
    assert "LINE ONE" in body and "LINE THREE" in body


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_body_3way_overlapping_edits_conflict():
    """Both sides editing the SAME line is a genuine conflict (markers in the body, ok=False)."""
    body, conflicts, ok = _merge_body("the line\n", "ours line\n", "theirs line\n")
    assert not ok and conflicts
    assert "<<<<<<<" in body and ">>>>>>>" in body


def test_body_merges_through_note_files_semantic_path():
    """End-to-end through merge_note_files: a one-sided body edit lands while edges stay unioned."""
    e = [_edge(EpistemicState.UNVERIFIED)]
    base = node_to_markdown(_node(e, body="shared"))
    ours = node_to_markdown(_node(e, body="ours rewrote it"))
    theirs = node_to_markdown(_node(e, body="shared"))
    text, conflicts, ok = merge_note_files(base, ours, theirs)
    assert ok and conflicts == []
    assert node_from_markdown(text).body == "ours rewrote it"


def test_unparseable_base_preserves_one_sided_body_edit():
    """review-low: when the BASE is non-empty but unparseable (no frontmatter), its raw text is the body
    ancestor — a one-sided body edit merges cleanly instead of spuriously conflicting against an empty
    base (which would make a one-sided change look like a two-sided one)."""
    raw_base = "Just prose, no frontmatter here."
    e = [_edge(EpistemicState.UNVERIFIED)]
    ours = node_to_markdown(_node(e, body=raw_base + "\nours appended a line"))
    theirs = node_to_markdown(_node(e, body=raw_base))   # theirs left the body at the base text
    text, conflicts, ok = merge_note_files(raw_base, ours, theirs)
    assert ok and conflicts == []                        # one-sided edit -> no spurious conflict
    assert "ours appended a line" in node_from_markdown(text).body


# --------------------------------------------------------------------------- fallbacks


def test_malformed_side_fails_open_to_text_merge():
    """An unparseable side must NOT raise: fall back to a plain text merge that SURFACES the divergence
    (a conflict with both sides present) rather than silently dropping either side or eating the error.
    Holds whether `git merge-file` runs or the git-absent manual-marker fallback does."""
    ours = node_to_markdown(_node([_edge(EpistemicState.UNVERIFIED)]))
    theirs = "this is not a canon note at all"
    text, conflicts, ok = merge_note_files("", ours, theirs)
    assert text                                          # non-empty, did not eat the inputs
    assert edge_id("n1", "grounds", "b") in text         # the parseable side is NOT dropped
    assert "not a canon note at all" in text             # the unparseable side is NOT dropped
    assert not ok and conflicts                          # divergence surfaced, not silently resolved


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_no_edges_notes_use_text_merge_not_semantic_reserialization():
    """Two prose notes with NO `edges:` block are merged through the plain `git merge-file` text path,
    never the semantic path: disjoint body edits both survive AND a non-canon frontmatter field that
    node_to_markdown would DROP is preserved verbatim — the real purpose of the no-edges guard (don't
    re-serialize, and so reshape, a note that has no edge structure to union)."""
    def note(body: str) -> str:
        # hand-built so it carries a `custom_field` that is NOT a Node attribute: a semantic
        # re-serialization (node_to_markdown) would silently drop it; a raw text merge keeps it.
        return (
            "---\nid: prose1\nlabel: Prose\nnode_type: claim\n"
            "custom_field: keepme\nedges: []\n---\n\n" + body + "\n"
        )
    base = note("para one\npara two\npara three")
    ours = note("PARA ONE\npara two\npara three")
    theirs = note("para one\npara two\nPARA THREE")
    text, conflicts, ok = merge_note_files(base, ours, theirs)
    assert ok and conflicts == []
    assert "PARA ONE" in text and "PARA THREE" in text   # both disjoint prose edits survive the 3-way
    assert "custom_field: keepme" in text                # text merge preserved a field semantic re-serialize drops


# --------------------------------------------------------------------------- driver main()


def test_main_writes_merged_result_to_ours_path(tmp_path: Path):
    """The git merge-driver contract: main() leaves the merged result in the OURS (%A) path, and a
    verdict disagreement is a CLEAN resolution (exit 0), not a conflict."""
    e_ours = [_edge(EpistemicState.GROUNDED)]
    e_theirs = [_edge(EpistemicState.REJECTED)]
    base = tmp_path / "base.md"
    ours = tmp_path / "ours.md"
    theirs = tmp_path / "theirs.md"
    base.write_text(node_to_markdown(_node([_edge(EpistemicState.UNVERIFIED)], body="b")), encoding="utf-8")
    ours.write_text(node_to_markdown(_node(e_ours, body="b")), encoding="utf-8")
    theirs.write_text(node_to_markdown(_node(e_theirs, body="b")), encoding="utf-8")

    rc = main([str(base), str(ours), str(theirs)])

    assert rc == 0  # verdict demotion is clean
    merged = node_from_markdown(ours.read_text(encoding="utf-8"))
    assert _find(merged, edge_id("n1", "grounds", "b")).epistemic_state is EpistemicState.UNVERIFIED
    assert theirs.read_text(encoding="utf-8")  # theirs untouched


def test_main_too_few_args_returns_usage_code():
    assert main(["only", "two"]) == 2


# --------------------------------------------------------------------------- git end-to-end


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, **kw)


@pytest.mark.skipif(GIT is None, reason="git not on PATH")
def test_git_merge_driver_end_to_end(tmp_path: Path):
    """Wire canonmerge as a real git merge driver and merge two diverged branches: edges union, the
    verdict disagreement demotes to unverified, and the merge completes cleanly (no conflict)."""
    repo = tmp_path
    _git(repo, "init", "-q", check=True)
    _git(repo, "config", "user.email", "t@t", check=True)
    _git(repo, "config", "user.name", "t", check=True)
    _git(repo, "config", "merge.kgcanon.name", "kg canon merge", check=True)
    driver = f"{shlex.quote(sys.executable)} -m kg_engine.canonmerge %O %A %B"
    _git(repo, "config", "merge.kgcanon.driver", driver, check=True)

    (repo / ".gitattributes").write_text("canon/*.md merge=kgcanon\n", encoding="utf-8")
    canon = repo / "canon"
    canon.mkdir()
    note = canon / "n1.md"
    a, x = ("grounds", "b"), ("bridges", "x")  # (relation, target)

    def write(edges, body="shared"):
        note.write_text(
            node_to_markdown(_node(
                [_edge(st, relation=rel, target=tgt) for (rel, tgt), st in edges.items()], body=body)),
            encoding="utf-8")

    # base on the default branch: edges A and X, both unverified
    write({a: EpistemicState.UNVERIFIED, x: EpistemicState.UNVERIFIED})
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "base", check=True)
    base_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=True).stdout.strip()

    # ours: A grounded, add edge Y
    _git(repo, "checkout", "-q", "-b", "ours", check=True)
    y = ("grounds", "y")
    write({a: EpistemicState.GROUNDED, x: EpistemicState.UNVERIFIED, y: EpistemicState.UNVERIFIED})
    _git(repo, "commit", "-q", "-am", "ours", check=True)

    # theirs: A rejected, add edge Z
    _git(repo, "checkout", "-q", base_branch, check=True)
    _git(repo, "checkout", "-q", "-b", "theirs", check=True)
    z = ("attacked_by", "z")
    write({a: EpistemicState.REJECTED, x: EpistemicState.UNVERIFIED, z: EpistemicState.UNVERIFIED})
    _git(repo, "commit", "-q", "-am", "theirs", check=True)

    # merge theirs into ours -> the driver runs on n1.md
    _git(repo, "checkout", "-q", "ours", check=True)
    env_extra = {"PYTHONPATH": str(SCRIPTS)}
    r = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "theirs"],
        capture_output=True, text=True, env={**_environ(), **env_extra},
    )
    assert r.returncode == 0, f"merge should be clean:\n{r.stdout}\n{r.stderr}"

    merged = node_from_markdown(note.read_text(encoding="utf-8"))
    ids = {e.id for e in merged.edges}
    # every edge unioned across base + both branches
    assert edge_id("n1", *a) in ids
    assert edge_id("n1", *x) in ids
    assert edge_id("n1", *y) in ids
    assert edge_id("n1", *z) in ids
    # the cross-branch verdict (grounded vs rejected) demoted to unverified — no forged verdict
    assert _find(merged, edge_id("n1", *a)).epistemic_state is EpistemicState.UNVERIFIED
    assert "<<<<<<<" not in note.read_text(encoding="utf-8")  # clean, no conflict markers


def _environ() -> dict:
    import os

    return dict(os.environ)
