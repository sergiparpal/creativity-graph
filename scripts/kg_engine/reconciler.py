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
from .model import EpistemicState, GROUNDABLE_STATES, VERDICT_STATES, node_from_markdown

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
            return json.loads(self.state_path.read_text())
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
            lines = self.audit_path.read_text().splitlines()
        except (FileNotFoundError, OSError):
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

    # ---- scan
    def scan(self, full_sweep: bool = False) -> ReconcileReport:
        state = self._load_state()
        files_state: dict = state.get("files", {})
        epistemic: dict = state.get("epistemic", {})
        consumed: dict = state.get("consumed", {})
        audit = self._audit_counts()
        report = ReconcileReport(full_sweep=full_sweep)

        for p in self.canon.note_paths():  # excludes .tmp-* atomic-write temporaries (canon-5)
            report.scanned += 1
            rel = p.name
            st = p.stat()
            prev = files_state.get(rel, {})
            # pre-filter: unchanged mtime+size and not a full sweep -> skip the expensive re-read
            prefilter_same = (prev.get("mtime") == st.st_mtime and prev.get("size") == st.st_size)
            if prefilter_same and not full_sweep:
                continue
            digest = _sha256(p)
            if full_sweep and prefilter_same and prev.get("sha256") == digest:
                # mtime/size matched AND hash matches -> genuinely unchanged even under sweep
                files_state[rel] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest}
                continue

            report.changed.append(rel)
            try:
                # parse the file ON DISK directly, not via read_node(p.stem) which re-slugs the stem
                # and could resolve to a different/missing file for a non-slug-canonical filename
                # (reconciler-4) — leaving that note un-reconciled on every scan.
                node = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem)
            except Exception:  # noqa: BLE001 — a single malformed note must not crash the sweep
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
                self.canon.write_one(node)
                st = p.stat()
                digest = _sha256(p)
            files_state[rel] = {"mtime": st.st_mtime, "size": st.st_size, "sha256": digest}

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
        live_nodes = self.canon.all_nodes()
        live_files = {p.name for p in self.canon.note_paths()}
        live_keys = {f"node:{n.id}" for n in live_nodes}
        live_keys |= {e.id for n in live_nodes for e in n.edges}
        for rel in [r for r in files_state if r not in live_files]:
            del files_state[rel]
        for k in [k for k in epistemic if k not in live_keys]:
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
            data = json.loads(Path(graph_json).read_text())
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
