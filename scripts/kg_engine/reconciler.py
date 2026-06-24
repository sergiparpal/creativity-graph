"""The reconciler (P_reconcile, §1.8).

An mtime/size pre-filter backed by a periodic full re-hash sweep (the pre-filter is for performance;
the sweep defeats mtime spoofing). On an out-of-band change it re-validates through the boundary; in
particular an out-of-band epistemic_state transition into a verdict (a forged verdict) with no matching
``kg_ground`` audit record is re-quarantined. Also runs after a derived-layer rebuild to re-attach
grounding verdicts and surface verdicts orphaned by edges that disappeared.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .canon import Canon, GROUND_AUDIT
from .model import EpistemicState, GROUNDABLE_STATES, VERDICT_STATES, node_from_markdown, slug

__all__ = ["Reconciler", "ReconcileReport", "OrphanReport", "GROUND_AUDIT"]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


@dataclass
class ReconcileReport:
    scanned: int = 0
    changed: list[str] = field(default_factory=list)
    requarantined: list[str] = field(default_factory=list)  # edge/node ids reset from a forged verdict
    full_sweep: bool = False


@dataclass
class OrphanReport:
    reattached: int = 0
    orphaned_verdicts: list[str] = field(default_factory=list)  # verdicts whose edge vanished


class Reconciler:
    def __init__(self, canon: Canon, state_path: str | Path | None = None):
        self.canon = canon
        self.state_path = Path(state_path) if state_path else (canon.root / ".kg-reconcile-state.json")
        self.audit_path = canon.root / GROUND_AUDIT

    # ---- state
    def _load_state(self) -> dict:
        try:
            # engine-written file: pin utf-8 so non-ASCII ids round-trip regardless of locale (§1.8).
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            # valid-JSON-but-non-dict (e.g. `[1,2,3]`, `null`) would crash scan() with AttributeError,
            # and that crash (swallowed by the background reconcile) never self-heals because scan dies
            # before _save_state. Coerce a non-dict to the fresh-dict default via the same fail-open path.
            if not isinstance(state, dict):
                raise ValueError("reconcile state is not a JSON object")
            return state
        except (FileNotFoundError, ValueError, OSError):
            return {"files": {}, "epistemic": {}, "consumed": {}}

    def _save_state(self, state: dict) -> None:
        from .canon import _atomic_write
        _atomic_write(self.state_path, json.dumps(state, indent=0))

    def _audit_counts(self) -> dict[str, int]:
        """How many kg_ground audit records justify each `key -> state` transition. Counting (rather
        than set-membership) is what defeats a *replay*: each legitimate transition consumes exactly
        one record, so re-applying a previously-audited verdict out-of-band has no record left to
        justify it and is caught as a forgery."""
        counts: dict[str, int] = {}
        try:
            # engine-written log: pin utf-8 so non-ASCII keys round-trip regardless of locale. A
            # locale-mismatched read (undefined bytes -> UnicodeError) must degrade to "no audit" and
            # fail-open rather than crash the whole reconcile (§1.8).
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeError):
            return counts
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # one corrupt audit line must not blind the reconciler to the rest
            counts[f"{rec.get('key', '')}||{rec.get('to', '')}"] = (
                counts.get(f"{rec.get('key', '')}||{rec.get('to', '')}", 0) + 1)
        return counts

    @staticmethod
    def _file_keys(p) -> "list[str] | None":
        """The node + edge baseline keys a single canon file contributes, or None if it cannot be
        parsed — matching all_nodes()/all_edges(), which skip a malformed note. Used to maintain
        live_keys incrementally in scan() (reconciler-6) so the prune pass needn't re-read+parse the
        whole canon a SECOND time per sweep. Parses by-path with fallback_id=p.stem, byte-identically
        to all_nodes(), so the incrementally-built live set equals the old all_nodes()-derived one."""
        try:
            node = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem)
        except Exception:  # noqa: BLE001 — a malformed note has no parseable keys
            return None
        return [f"node:{node.id}", *(e.id for e in node.edges)]

    # ---- scan
    def scan(self, full_sweep: bool = False) -> ReconcileReport:
        state = self._load_state()
        # Coerce each sub-key defensively: a hand-edited / truncated state with `{"files": null}` (or a
        # non-dict sub-value) would otherwise crash scan() before _save_state can heal it. `or {}`
        # rescues null; the isinstance guard rescues a non-dict (e.g. a list) — both fail open to fresh.
        files_state = state.get("files") or {}
        epistemic = state.get("epistemic") or {}
        consumed = state.get("consumed") or {}
        if not isinstance(files_state, dict):
            files_state = {}
        if not isinstance(epistemic, dict):
            epistemic = {}
        if not isinstance(consumed, dict):
            consumed = {}
        audit = self._audit_counts()
        report = ReconcileReport(full_sweep=full_sweep)
        # On a FULL SWEEP we visit and hash every note in the loop below, so we can build the live
        # file/key sets INCREMENTALLY (reconciler-6) and skip the old post-loop all_nodes() re-parse —
        # a hash match proves the cached keys are current, so the incremental sets are byte-identical to
        # the all_nodes()-derived ones. On the cheap (mtime/size) pre-gate path the loop SKIPS unchanged
        # notes, so cached keys could be stale under an (mtime,size) collision; there we fall back to the
        # authoritative all_nodes() recompute for pruning, exactly as before this change.
        live_files: set[str] = set()
        live_keys: set[str] = set()

        for p in self.canon.note_paths():  # excludes .tmp-* atomic-write temporaries (canon-5)
            report.scanned += 1
            rel = p.name
            st = p.stat()
            prev = files_state.get(rel, {})
            # pre-filter: unchanged mtime+size and not a full sweep -> skip the expensive re-read
            prefilter_same = (prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size)
            if prefilter_same and not full_sweep:
                continue  # cheap pre-gate: trust (mtime,size); the full-sweep prune below re-reads
            digest = _sha256(p)
            if full_sweep and prefilter_same and prev.get("sha256") == digest:
                # mtime/size AND hash matched -> genuinely unchanged even under sweep. Carry the cached
                # keys forward (backfill once if absent) so live_keys stays complete without a re-parse.
                keys = prev.get("keys")
                if keys is None:
                    keys = self._file_keys(p)
                files_state[rel] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest,
                                    "keys": keys if keys is not None else []}
                live_files.add(rel)
                if keys:
                    live_keys.update(keys)
                continue

            report.changed.append(rel)
            try:
                # parse the file ON DISK directly, not via read_node(p.stem) which re-slugs the stem
                # and could resolve to a different/missing file for a non-slug-canonical filename
                # (reconciler-4) — leaving that note un-reconciled on every scan.
                node = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem)
            except Exception:  # noqa: BLE001 — a single malformed note must not crash the sweep
                # the note still exists on disk (so its files_state entry must survive the prune), but a
                # malformed note contributes NO parseable keys — exactly what all_nodes() would yield.
                live_files.add(rel)
                continue  # leave its file_state untouched so it's retried next scan
            mutated = False

            # node-level forged verdict
            nkey = f"node:{node.id}"
            if self._forged(nkey, node.epistemic_state, epistemic, consumed, audit):
                node.epistemic_state = EpistemicState.UNVERIFIED
                report.requarantined.append(node.id)
                mutated = True
            epistemic[nkey] = node.epistemic_state.value

            # edge-level forged verdicts
            for e in node.edges:
                ekey = e.id
                if self._forged(ekey, e.epistemic_state, epistemic, consumed, audit):
                    e.epistemic_state = EpistemicState.UNVERIFIED
                    e.verdict_by = None
                    e.verdict_at = None
                    report.requarantined.append(e.id)
                    mutated = True
                epistemic[ekey] = e.epistemic_state.value

            if mutated:
                # write_one persists to the CANONICAL slug path (node_path(node.id)). When the note we
                # actually read lives at a NON-canonical filename (e.g. hand-created Foo.md for id 'Foo',
                # slug -> foo.md), the correction must land canonically with the stale original gone —
                # else a duplicate edge in two states, and re-statting the untouched original would
                # re-skip it forever (reconciler-3, self-concealing).
                #
                # Detect "non-canonical" by comparing the directory-entry name to the PURE SLUG name
                # (`slug(id).md`), a plain case-sensitive string compare with NO filesystem resolution.
                # node_path() calls Path.resolve(), and on Windows resolve() returns the EXISTING on-disk
                # casing ('Foo.md'), which would mask the difference from the canonical 'foo.md' and skip
                # this correction; note_paths() yields the real stored name ('Foo.md'). And remove the
                # original BEFORE writing: on a case-insensitive filesystem (macOS/Windows) Foo.md and
                # foo.md are the SAME file, so a write-then-unlink would delete the just-written note, and a
                # case-preserving replace would keep the stale 'Foo.md' name anyway. Deleting first lets
                # write_one create a fresh, correctly-cased foo.md; on a case-sensitive FS it just drops the
                # distinct duplicate. The corrected node is in memory, so a crash in the tiny unlink->write
                # window at worst drops an already-forged note (re-corrected next sweep), never a grounded one.
                canonical_name = f"{slug(node.id)}.md"
                if p.name != canonical_name:
                    try:
                        # Would the canonical write collide with a DISTINCT existing note (a real id≠
                        # owning slug(id).md on a case-sensitive FS)? Detect BEFORE the destructive
                        # unlink: the old code unlinked first, then write_one -> _check_slug_collision
                        # raised, and the UNCAUGHT ValueError aborted the ENTIRE sweep with the original
                        # already deleted — a vault-wide reconcile outage plus note loss (review-M1).
                        self.canon._check_slug_collision(node)
                    except ValueError:
                        live_files.add(rel)  # keep the original; retry next sweep, don't abort the sweep
                        continue
                    p.unlink(missing_ok=True)  # drop the non-canonical original before the canonical write
                    files_state.pop(rel, None)
                    rel = canonical_name
                    p = self.canon.notes_dir / canonical_name
                try:
                    self.canon.write_one(node)
                except Exception:  # noqa: BLE001 — one unwritable/colliding note must not abort the sweep
                    live_files.add(rel)
                    continue
                st = p.stat()
                digest = _sha256(p)
            keys = [f"node:{node.id}", *(e.id for e in node.edges)]
            files_state[rel] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest, "keys": keys}
            live_files.add(rel)
            live_keys.update(keys)

        # Prune the `epistemic` BASELINE for anything no longer live. Edge ids are deterministic, so a
        # deleted-then-recreated edge would otherwise inherit the old "already grounded" baseline and
        # let a forged verdict slip past the `last == current` short-circuit in _forged (reconciler-1).
        # Dropping dead keys also bounds growth across renames/churn (reconciler-2, whose comment
        # previously claimed a pruning that did not happen).
        #
        # `consumed` is deliberately NOT pruned: it is the running tally of audit records already
        # spent, and the audit log is append-only (never pruned). Pruning consumed would let an old,
        # still-present audit record be re-spent by a recreated edge — re-opening the very replay the
        # count check defeats. Its growth tracks the audit log, which is the permanent record anyway.
        if full_sweep:
            # every note was visited+hashed in the loop, so the incremental sets equal an
            # all_nodes()/note_paths() recompute exactly — without the redundant second full read.
            prune_files, prune_keys = live_files, live_keys
        else:
            # cheap pre-gate skipped unchanged notes (their cached keys may be stale under an (mtime,size)
            # collision), so recompute authoritatively — byte-identical to the pre-reconciler-6 prune.
            live_nodes = self.canon.all_nodes()
            prune_files = {p.name for p in self.canon.note_paths()}
            prune_keys = {f"node:{n.id}" for n in live_nodes} | {e.id for n in live_nodes for e in n.edges}
        for rel in [r for r in files_state if r not in prune_files]:
            del files_state[rel]
        for k in [k for k in epistemic if k not in prune_keys]:
            del epistemic[k]

        self._save_state({"files": files_state, "epistemic": epistemic, "consumed": consumed})
        return report

    @staticmethod
    def _forged(key: str, current: EpistemicState, epistemic: dict, consumed: dict,
                audit: dict[str, int]) -> bool:
        """True if `current` is a policed state reached out-of-band: it differs from the last validated
        state for this key and there is no UNCONSUMED kg_ground audit record justifying a transition
        into `current`. Each legitimate transition consumes one audit record, so replaying a stale
        verdict (whose record was already spent) is caught. The policed set is GROUNDABLE_STATES — the
        verdicts PLUS `obsolete` — the same set kg_ground stamps and audits; `obsolete` was previously
        excluded, so an out-of-band edit to `obsolete` (which the write boundary demotes) silently
        survived the sweep and could erase a grounding verdict / failure memory (reconciler-5/server-1)."""
        if current not in GROUNDABLE_STATES:
            return False
        last = epistemic.get(key)
        if last == current.value:
            return False  # unchanged since last validated — nothing new to justify
        pair = f"{key}||{current.value}"
        if audit.get(pair, 0) > consumed.get(pair, 0):
            consumed[pair] = consumed.get(pair, 0) + 1  # spend exactly one record for this transition
            return False
        return True

    # ---- post-reproject reattachment
    def reattach_after_reproject(self, graph_json: str | Path) -> OrphanReport:
        report = OrphanReport()
        try:
            # engine-written derived layer: pin utf-8 so non-ASCII edge ids match the canon (parsed
            # utf-8) regardless of locale, rather than mojibake-missing a live verdict (§1.8).
            data = json.loads(Path(graph_json).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return report
        derived_edge_ids = {e.get("id") for e in data.get("links", data.get("edges", []))}
        for e in self.canon.all_edges():
            # only true verdicts (grounded/rejected/failed) are "verdicts" that can be orphaned;
            # OBSOLETE is a lifecycle state, not a verdict, so it is not reported here.
            if e.epistemic_state in VERDICT_STATES:
                if e.id in derived_edge_ids:
                    report.reattached += 1
                else:
                    report.orphaned_verdicts.append(e.id)
        return report
