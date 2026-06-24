"""Semantic git merge driver for the per-node canon (§1.2/§1.4) — the safe half of R5.

A canon note is YAML frontmatter (the three axes + an ``edges:`` block) plus free body text. When two
branches/machines edit the *same* node, a line-based merge mangles the `edges:` list and — worse — can
silently keep one side's grounding verdict. This driver mirrors the **edge-union + cross-branch-verdict-
demote intent** of ``Canon._merge_into_existing`` (canon.py) — NOT its exact resolution logic: it unions
edges by the deterministic ``edge_id`` (model.edge_id) and, for an edge present on both sides at a
**different** ``epistemic_state``, resolves the merged edge to ``unverified`` — never to either side's
verdict, with ``verdict_by``/``verdict_at`` cleared (the same demotion the reconciler applies to an
out-of-band verdict change, §1.8). The git driver's edge rule is **symmetric** (any disagreement demotes),
whereas ``_merge_into_existing`` is asymmetric incoming-wins with a re-promote special-case — each is safe
only in its own context. This driver ADDS beyond ``_merge_into_existing``: a node-level demote-on-
disagreement and a scalar-frontmatter 3-way. The in-process path instead polices a forged node verdict
later, in the reconciler's audit-aware sweep (§1.8), not at merge time.

It is **structurally incapable of forging a verdict**: the only ``epistemic_state`` it can ever *write*
on a conflict is ``unverified``. A grounding verdict that survives a clean (non-conflicting) merge has
no audit record on the merging machine and is re-quarantined by the reconciler's full sweep — so the
deferred cross-machine verdict-sharing half is unnecessary for safety here (see CHANGELOG).

Wired as a git merge driver via ``.gitattributes`` (``canon/*.md merge=kgcanon``) plus a one-time
``git config merge.kgcanon.driver`` pointing at ``scripts/canon_merge_driver.mjs`` (which runs this
module's ``main`` through the resolved engine python). See README / CLAUDE.md for the install steps.

Pure stdlib + the model layer; no network, no canon I/O, no audit log.
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .model import EpistemicState, Node, node_from_markdown, node_to_markdown

__all__ = ["merge_nodes", "merge_note_files", "main"]

# Scalar frontmatter fields merged with a base-aware 3-way rule (label/type), keeping ours on a true
# divergence — minor metadata, never a hard conflict. epistemic_state is handled separately (demoted on
# divergence, like an edge verdict — never-forge-a-verdict, invariant 3); the immutable id is never merged.
_SCALAR_FIELDS = ("label", "node_type", "file_type")

# git merge-file returns -1 (255) on internal error; a positive return is the conflict-hunk count.
_GIT_MERGE_INTERNAL_ERROR = 255


# --------------------------------------------------------------------------- parse helpers


def _read(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        # git hands an EMPTY base (%O) when the file was added on both sides (no common ancestor);
        # a missing/unreadable path degrades to "" so the text 3-way still runs.
        return ""


def _parse(text: str) -> Node | None:
    """Parse a canon note to a Node, or None if it is empty / not a node-with-frontmatter — the
    signal merge_note_files uses to fall back to a plain text merge rather than force a semantic one."""
    if not text or not text.strip():
        return None
    try:
        return node_from_markdown(text)
    except Exception:  # noqa: BLE001 — any unparseable note must fail OPEN to the text 3-way, never raise
        return None


def _load(path: str | Path) -> Node | None:
    """Read+parse a canon file to a Node, or None on missing/empty/unparseable (public helper)."""
    return _parse(_read(path))


def _eprint(msg: str) -> None:
    """Best-effort stderr logger shared by the file-level merge and the driver entrypoint."""
    try:
        sys.stderr.write(msg + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- semantic node merge


def _demotion_note(subject: str, from_state: EpistemicState, to_state: EpistemicState) -> str:
    """The human-readable note for a verdict demotion, shared by the edge and node branches."""
    return f"{subject}: {from_state.value}/{to_state.value} -> unverified"


def merge_nodes(base: Node | None, ours: Node, theirs: Node) -> tuple[Node, list[str]]:
    """3-way merge two parsed canon nodes — mirrors the edge-union + cross-branch-verdict-demote INTENT
    of Canon._merge_into_existing (not its exact resolution: this edge rule is symmetric, the in-process
    one is asymmetric incoming-wins with a re-promote case).

    - **Edges:** union by the deterministic ``edge_id`` (never lose an edge a side added, mirroring the
      single-canonical-edge rule + never-prune-failure-memory §1.7). An edge on both sides at the SAME
      ``epistemic_state`` keeps ours; at a DIFFERENT state it demotes to ``unverified`` with
      ``verdict_by``/``verdict_at`` cleared — never resolving to either verdict (never-forge-a-verdict).
    - **Scalar frontmatter** (label/type): base-aware 3-way, keeping ours on a true divergence.
    - **Node ``epistemic_state``:** demoted to ``unverified`` when the two sides disagree (forge-proof,
      same intent as the edge rule); ``base`` is consulted only for the scalar 3-way. This node-level
      demote is ADDED here beyond ``_merge_into_existing`` (which leaves node verdicts to the reconciler's
      audit-aware sweep, §1.8).

    Returns ``(merged_node, demotions)`` where ``demotions`` lists the auto-demotions (informational — a
    verdict demotion is a CLEAN resolution, not a merge conflict, so it never sets the exit code)."""
    demotions: list[str] = []
    merged = copy.deepcopy(ours)  # start from ours: preserves ours' frontmatter, timestamps, body

    by_id = {e.id: e for e in merged.edges}
    for e in theirs.edges:
        existing = by_id.get(e.id)
        if existing is None:
            by_id[e.id] = copy.deepcopy(e)  # theirs-only edge -> union it in
        elif existing.epistemic_state != e.epistemic_state:
            demoted = copy.deepcopy(existing)
            demoted.epistemic_state = EpistemicState.UNVERIFIED
            demoted.verdict_by = None
            demoted.verdict_at = None
            by_id[e.id] = demoted
            demotions.append(_demotion_note(e.id, existing.epistemic_state, e.epistemic_state))
        # else: equal verdict on both sides -> keep ours (already in by_id)
    merged.edges = list(by_id.values())

    # scalar frontmatter: base-aware 3-way, keep ours on divergence
    for field in _SCALAR_FIELDS:
        ours_val, theirs_val = getattr(ours, field), getattr(theirs, field)
        if ours_val == theirs_val:
            continue
        base_val = getattr(base, field) if base is not None else None
        if base_val is not None and ours_val == base_val:
            setattr(merged, field, theirs_val)  # only theirs changed it
        # else: only ours changed, or both diverged / no base -> keep ours (minor field, no hard conflict)

    # node-level epistemic_state: demote on disagreement (forge-proof, same intent as the edge rule).
    # Node carries no verdict_by/verdict_at (those live only on Edge), so there is nothing extra to clear.
    if ours.epistemic_state != theirs.epistemic_state:
        if merged.epistemic_state != EpistemicState.UNVERIFIED:
            demotions.append(
                _demotion_note(f"node:{merged.id}", ours.epistemic_state, theirs.epistemic_state))
        merged.epistemic_state = EpistemicState.UNVERIFIED

    return merged, demotions


# --------------------------------------------------------------------------- text 3-way merge


def _manual_conflict(ours_text: str, theirs_text: str) -> tuple[str, list[str], bool]:
    """Fallback when ``git merge-file`` is unavailable (git off PATH): emit standard whole-file conflict
    markers so the divergence is surfaced honestly rather than silently dropping a side."""
    def _nl(s: str) -> str:
        """Ensure a trailing newline so each conflict marker starts on its own line."""
        return s if s.endswith("\n") else s + "\n"
    merged = (
        "<<<<<<< ours\n" + _nl(ours_text)
        + "=======\n" + _nl(theirs_text)
        + ">>>>>>> theirs\n"
    )
    return merged, ["git merge-file unavailable: whole-file conflict"], False


def _git_merge_file(base_text: str, ours_text: str, theirs_text: str) -> tuple[str, list[str], bool]:
    """A plain 3-way text merge via ``git merge-file -p`` (ours as current, %O as base, theirs as other).
    Returns ``(merged_text, conflicts, ok)``; falls back to manual markers if git is absent/erroring."""
    try:
        with tempfile.TemporaryDirectory() as td:
            paths = {}
            for name, text in (("base", base_text), ("ours", ours_text), ("theirs", theirs_text)):
                p = Path(td) / name
                p.write_text(text, encoding="utf-8")
                paths[name] = str(p)
            r = subprocess.run(
                ["git", "merge-file", "-p",
                 "-L", "ours", "-L", "base", "-L", "theirs",
                 paths["ours"], paths["base"], paths["theirs"]],
                capture_output=True, text=True,
            )
    except (FileNotFoundError, OSError):
        return _manual_conflict(ours_text, theirs_text)
    # git merge-file: 0 == clean; a positive count == that many conflict hunks; 255 (-1) == internal error.
    if r.returncode == 0:
        return r.stdout, [], True
    if 0 < r.returncode < _GIT_MERGE_INTERNAL_ERROR:
        return r.stdout, [f"{r.returncode} conflict hunk(s)"], False
    return _manual_conflict(ours_text, theirs_text)


def _merge_body(base_body: str, ours_body: str, theirs_body: str) -> tuple[str, list[str], bool]:
    """3-way merge the free-text body. Resolve the trivial cases in-process (so a pure-Python run needs
    no git) and shell out to ``git merge-file`` only when both sides genuinely diverged."""
    if ours_body == theirs_body:
        return ours_body, [], True
    if ours_body == base_body:
        return theirs_body, [], True  # only theirs changed the body
    if theirs_body == base_body:
        return ours_body, [], True  # only ours changed the body
    return _git_merge_file(base_body, ours_body, theirs_body)


# --------------------------------------------------------------------------- file-level merge


def merge_note_files(base_text: str, ours_text: str, theirs_text: str) -> tuple[str, list[str], bool]:
    """3-way merge two canon note *files*. Returns ``(merged_text, conflicts, ok)`` (``ok`` is the
    merge-clean flag → exit 0/1). If either side is not a parseable node-with-frontmatter, or NEITHER
    side declares any edges, fall back to a plain ``git merge-file`` text merge — a non-canon / pure-prose
    note has no edge structure to union, so forcing a semantic merge would only risk mangling it."""
    ours = _parse(ours_text)
    theirs = _parse(theirs_text)
    if ours is None or theirs is None or (not ours.edges and not theirs.edges):
        return _git_merge_file(base_text, ours_text, theirs_text)

    base = _parse(base_text)  # may be None (file added on both sides, or an unparseable base)
    merged, demotions = merge_nodes(base, ours, theirs)
    # If the base file existed but wasn't parseable as a node (no frontmatter), its RAW text is still the
    # common ancestor for the body 3-way merge. Using "" instead would make a one-sided body edit look
    # like a two-sided change against an empty base and spuriously conflict (review-low). When base is
    # genuinely absent (added on both sides) base_text is "" anyway, so this is safe in both cases.
    base_body = base.body if base is not None else base_text
    merged_body, body_conflicts, body_ok = _merge_body(base_body, ours.body, theirs.body)
    merged.body = merged_body
    for note in demotions:
        _eprint(f"[kg canon merge] demoted verdict (re-ground after merge): {note}")
    return node_to_markdown(merged), body_conflicts, body_ok


# --------------------------------------------------------------------------- driver entrypoint


def _write_atomic(path: Path, text: str) -> None:
    """Atomic write (temp in the same dir + os.replace) so a crash mid-write never leaves git a
    half-written canon note (review-low)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent or "."), prefix=".kgmerge-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    """git merge-driver entrypoint. git invokes us with ``%O %A %B`` (base, ours, theirs); we must
    leave the merged result in the OURS path (%A) and exit 0 (clean) / 1 (conflicted, git marks it)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3:
        _eprint("usage: python -m kg_engine.canonmerge <base %O> <ours %A> <theirs %B>")
        return 2
    base_path, ours_path, theirs_path = argv[0], argv[1], argv[2]
    # FAIL OPEN (review-low): any unexpected error must leave 'ours' (%A) untouched and exit 1 so git
    # marks the file conflicted for a human — NEVER crash after a partial write or corrupt the note.
    try:
        text, conflicts, ok = merge_note_files(_read(base_path), _read(ours_path), _read(theirs_path))
    except Exception as e:  # noqa: BLE001
        _eprint(f"[kg canon merge] merge failed; leaving 'ours' for git to conflict: {e}")
        return 1
    for c in conflicts:
        _eprint(f"[kg canon merge] conflict: {c}")
    try:
        _write_atomic(Path(ours_path), text)
    except OSError as e:
        _eprint(f"[kg canon merge] could not write merged result; leaving 'ours': {e}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
