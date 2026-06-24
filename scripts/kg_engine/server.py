"""The MCP server (§2.4): the graphify-shaped tool surface + our grounding semantics.

Tool logic lives in the importable `KGEngine` facade so it is unit-testable without an MCP client;
the FastMCP wrappers are thin. Elicitation requests always declare a default applied if unanswered,
so the flow never stalls (§2.4, §4).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from . import __version__
from .boundary import DEFAULT_MAX_EDGES_PER_KB, MIN_SPAN_CHARS, merge_results_into_nodes, validate_payload
from .canon import Canon
from .model import (
    AuthoredBy,
    Disposition,
    EpistemicState,
    GROUNDABLE_STATES,
    Node,
    Provenance,
    normalize_text,
    slug,
    utcnow,
)
from .pack import load_pack
from .projector import Projector
from .reconciler import GROUND_AUDIT, Reconciler
from .scrub import Scrubber
from .sources import SourceSet

# Single source of truth shared with the reconciler's policed set (model.GROUNDABLE_STATES), so the
# states kg_ground may stamp and the states the reconciler re-quarantines can never drift apart.
VALID_VERDICTS = {s.value for s in GROUNDABLE_STATES}
# The known verdict actors, derived from the AuthoredBy enum (mirroring how VALID_VERDICTS derives from
# GROUNDABLE_STATES) so the clamp tracks the model instead of an inline literal that can drift.
VALID_ACTORS = {a.value for a in AuthoredBy}


class KGEngine:
    """Stateful facade over canon + boundary + projector + reconciler + scrubber."""

    def __init__(self, project_dir, data_dir=None, *, source_path=None, pack_path=None,
                 sensitivity="medium", metrics_mode="structure_only",
                 max_edges_per_kb=DEFAULT_MAX_EDGES_PER_KB):
        self.project_dir = Path(project_dir)
        self.data_dir = Path(data_dir) if data_dir else (self.project_dir / ".kg-data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.canon = Canon(self.project_dir)
        self.reconciler = Reconciler(self.canon)
        # pass the bound source reader so the projector can IDF-weight specificity off the source corpus
        # (PLAN Stage 2). It is read lazily, once per real reprojection, off the hot path.
        self.projector = Projector(self.canon, self.data_dir / "derived", metrics_mode=metrics_mode,
                                   source_text=self.source_text, source_set=self.source_set,
                                   specificity_seeds=lambda: dict(getattr(self.pack, "specificity_seeds", {}) or {}))
        self.scrubber = Scrubber(sensitivity)
        self._scrub_map: dict[str, str] = {}  # accumulated egress placeholder -> original (§1.9)
        self.sensitivity = sensitivity
        self.metrics_mode = metrics_mode
        self.max_edges_per_kb = max_edges_per_kb
        # The configured source: a single file (back-compat), or a DIRECTORY / GLOB of .md/.txt (R4).
        # Stays a single Path; SourceSet does the multi-file resolution behind source_set().
        self.source_path = Path(source_path) if source_path else None
        self._source_set_cache: tuple[tuple, SourceSet] | None = None  # (signature, SourceSet) memo
        self.pack = None
        if pack_path and Path(pack_path).exists():
            try:
                self.pack = load_pack(pack_path)
            except Exception:  # noqa: BLE001 — a bad pack must not crash the server
                self.pack = None

    # ---- source set (for span verification)
    def source_set(self) -> SourceSet:
        """The resolved {basename → text} view over the configured source(s) (R4). Memoized on the
        aggregate (resolved-file-list, mtime) signature so an added/removed/edited file is picked up
        while the resolve+read stays off the hot path — generalizing the old per-file mtime memo. A
        single configured file is a one-entry SourceSet, byte-identical to the prior single-blob path."""
        sig = SourceSet.signature(self.source_path)
        if self._source_set_cache is None or self._source_set_cache[0] != sig:
            self._source_set_cache = (sig, SourceSet(self.source_path))
        return self._source_set_cache[1]

    def source_text(self) -> str:
        """The configured source(s) concatenated: a single file is its own text; a dir/glob is every
        .md/.txt member joined. Feeds the flood-budget size and the projector's IDF corpus. Span
        verification itself is source-aware via source_set().verifies (per-file), not this blob."""
        return self.source_set().concat

    # ---- tools -----------------------------------------------------------
    def kg_ping(self) -> dict:
        return {"name": "creativity-graph", "version": __version__,
                "metrics_mode": self.metrics_mode, "sensitivity": self.sensitivity,
                "pack_loaded": self.pack is not None}

    def kg_scrub(self, text: str | None = None) -> dict:
        """Egress scrub (§1.9): redact secrets (always) + PII (per sensitivity) with CONSISTENT
        placeholders before any text is handed to a subagent for semantic work. Accumulates the local
        placeholder->original mapping so kg_write can restore spans to the original for the canon (the
        scrub protects the egress, not the local canon). Pass `text` to scrub a snippet, or omit to scrub
        the configured source. Returns the scrubbed text the subagent should see."""
        src = text if text is not None else self.source_text()
        scrubbed, mapping = self.scrubber.scrub(src)
        self._scrub_map.update(mapping)
        return {"scrubbed": scrubbed, "redactions": len(mapping),
                "sensitivity": self.sensitivity, "categories": sorted({k.split(":")[0].strip("⟦") for k in mapping})}

    def _restore_fn(self):
        """The §1.9 span-restore: map placeholder spans back to the original before span verification,
        but ONLY when a scrub happened this session (else None — verify the span as written)."""
        return (lambda s: Scrubber.restore(s, self._scrub_map)) if self._scrub_map else None

    @staticmethod
    def _append_note(existing: str, addition: str) -> str:
        """Append `addition` to a notes field with the load-bearing ` | ` separator (the field is later
        parsed/displayed); names the separator once."""
        return (existing + " | " if existing else "") + addition

    def kg_write(self, payload: dict, *, message: str = "kg_write", existing_nodes=None) -> dict:
        """Validate an extraction payload at the boundary and write accepted/demoted items.

        `existing_nodes` is the canon baseline used for dedup + rate-limit seeding; it defaults to a
        fresh parse (every existing call site is unchanged). The headless backend threads an
        incrementally-maintained baseline so it doesn't re-parse the entire canon once per section
        (backend-1/server-16)."""
        # if egress scrubbing happened this session, restore placeholder spans to the original before
        # span verification, and store the original in the canon (§1.9).
        restore = self._restore_fn()
        if existing_nodes is None:
            existing_nodes = self.canon.all_nodes()  # read once; derive edges + node baseline from it
        existing_edges = [e for n in existing_nodes for e in n.edges]
        results = validate_payload(payload, pack=self.pack, source_text=self.source_text(),
                                   sources=self.source_set(),
                                   existing=existing_edges,
                                   existing_node_ids={n.id for n in existing_nodes},
                                   restore=restore, max_edges_per_kb=self.max_edges_per_kb)
        nodes = merge_results_into_nodes(results)
        info = self.canon.write_nodes(list(nodes.values()), message=message) if nodes else None
        rolled_back = bool(info and info.rolled_back)
        summary: dict = {d.value: 0 for d in Disposition}
        for r in results:
            summary[r.disposition.value] += 1
        # CONTRACT (F10/M4): the dispositions summary and written_nodes are built from PRE-write
        # ValidationResults; if the batch ROLLED BACK nothing persisted. Re-bucket the would-have-been
        # ACCEPTED/DEMOTED counts into a `rolled_back` bucket and empty written_nodes so the payload can
        # never contradict `rolled_back: True`. Backend consumers: when rolled_back is True,
        # written_nodes is [] and the accepted/demoted counts must NOT be trusted/accumulated.
        written = list(nodes)
        if rolled_back:
            persisted = (Disposition.ACCEPTED.value, Disposition.DEMOTED.value)
            summary["rolled_back"] = sum(summary.get(d, 0) for d in persisted)
            for d in persisted:
                summary[d] = 0
            written = []
        return {
            "dispositions": summary,
            "details": [{"kind": r.kind, "id": getattr(r.item, "id", None), "disposition": r.disposition.value,
                         "reason": r.reason, "retryable": r.retryable} for r in results],
            "written_nodes": written,
            "rolled_back": rolled_back,
            "error": (info.error if rolled_back else None),
        }

    def kg_propose(self, payload: dict, *, message: str = "kg_propose") -> dict:
        """Write hypothesized candidates through the boundary (PLAN Stage 1: the propose lane).

        A thin, explicit alias over `kg_write` that keeps the two write lanes legible at the call site:
        every item is forced to `provenance=hypothesized`, and any item that arrives explicitly claiming
        a text-claim provenance (`span-present`/`inferred`) is REFUSED with reason `propose-lane-text-claim`
        rather than silently re-lanned — text claims belong on `kg_write`, proposals belong here. The
        accepted items then transit the SAME boundary (`validate_payload`), so the hypothesized-lane rules
        (no span required, forged verdicts demoted, failure-collapse quarantined, pack vocabulary enforced)
        apply uniformly."""
        payload = dict(payload or {})
        refused: list[dict] = []

        def _lane(items, kind):
            kept = []
            for it in (items or []):
                it = dict(it or {})
                prov = it.get("provenance")
                if prov in (Provenance.SPAN_PRESENT.value, Provenance.INFERRED.value):
                    refused.append({"kind": kind,
                                    "id": it.get("id") or it.get("source") or it.get("label"),
                                    "disposition": Disposition.REJECTED.value,
                                    "reason": "propose-lane-text-claim", "retryable": False})
                else:
                    it["provenance"] = Provenance.HYPOTHESIZED.value  # force the lane
                    kept.append(it)
            return kept

        clean = {"nodes": _lane(payload.get("nodes"), "node"),
                 "edges": _lane(payload.get("edges"), "edge")}
        if "complete" in payload:
            clean["complete"] = payload["complete"]
        out = self.kg_write(clean, message=message)
        # fold the call-site refusals into the same response shape kg_write returns
        out["details"] = refused + out["details"]
        out["dispositions"][Disposition.REJECTED.value] = (
            out["dispositions"].get(Disposition.REJECTED.value, 0) + len(refused))
        out["propose_lane"] = True
        out["refused_text_claims"] = len(refused)
        return out

    def kg_ground(self, target_id: str, verdict: str, *, by: str = "agent", kind: str = "edge",
                  note: str = "", support_span: str = "", support_note: str = "") -> dict:
        """Apply a grounding verdict (the ONLY path that may set a verdict state). Stamps the verdict
        and appends an audit record so the reconciler treats the transition as legitimate (§1.8).

        **Promotion of a hypothesis requires support (PLAN Stage 8 / §1.2-3).** A `hypothesized` edge
        may become `grounded` ONLY when a grounder supplies support, which UPGRADES its provenance:
        `support_span` (a verbatim substring of the source) → `span-present`; `support_note` (an external
        citation, no span) → `inferred`. Without either, grounding a hypothesis to `grounded` is refused
        with `hypothesis-needs-support` — generated ideas become grounded knowledge only by earning it.
        `support_*` are ignored for non-hypothesized edges and for any verdict other than `grounded`."""
        verdict = verdict.lower()
        if verdict not in VALID_VERDICTS:
            return {"ok": False, "error": f"invalid verdict {verdict!r}"}
        # `by` is provenance, not a free-text field: clamp to the known actors so a stray value can't
        # masquerade as a verdict author (the MCP tool surface already pins this to "agent").
        by = by if by in VALID_ACTORS else "agent"
        state = EpistemicState(verdict)
        promoted_to = None
        # Acquire the single-writer lease FIRST, then read the owning node FRESH under the lease, mutate,
        # audit, and write — so the whole read-modify-write is atomic w.r.t. other writers. Reading before
        # locking (the old order) let a concurrent multi-process grounding clobber our edits with a
        # whole-node overwrite (lost update, F17/L5). The lease also still guards the audit-append +
        # write + compensating-truncate sequence (server-3); write_one re-acquires it re-entrantly.
        if not self.canon.try_acquire_lock():
            return {"ok": False, "error": "canon vault is locked by another live session"}
        try:
            if kind == "node":
                if not self.canon.exists(target_id):
                    return {"ok": False, "error": "node not found"}
                try:
                    node = self.canon.read_node(target_id)  # corrupt/invalid-UTF-8 note → structured error (F13/L1)
                except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                    return {"ok": False, "error": f"node unreadable: {e}"}
                frm = node.epistemic_state.value
                node.epistemic_state = state
                key = f"node:{node.id}"
            else:
                node = self._owner_of_edge(target_id)
                if node is None:
                    return {"ok": False, "error": "edge not found"}
                edge = next(e for e in node.edges if e.id == target_id)
                # the hypothesized→grounded promotion gate: a span-less proposal earns grounding only with
                # support, which upgrades its provenance. Decided BEFORE any state change so a refusal leaves
                # the edge untouched (no audit record, no write).
                if edge.provenance == Provenance.HYPOTHESIZED and state == EpistemicState.GROUNDED:
                    promoted_to, err = self._promote_hypothesis(edge, support_span, support_note)
                    if err:
                        return {"ok": False, "error": err}
                frm = edge.epistemic_state.value
                edge.epistemic_state = state
                edge.verdict_by = by
                edge.verdict_at = utcnow()
                if note:
                    edge.notes = self._append_note(edge.notes, note)
                key = edge.id
            # Append the audit record BEFORE persisting the verdict (a CRASH between the two leaves an
            # audit record with no state change — harmless, unconsumed — rather than a verdict with no
            # audit record, which the reconciler would re-quarantine), and truncate it back on a caught
            # write failure so an orphan record can't inflate _forged's count (server-3). The crash-safe
            # offset/truncate dance lives in _audited_write, shared with kg_rename.
            err_holder: dict = {}

            def _attempt():
                try:
                    self.canon.write_one(node)
                    return True, None
                except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                    err_holder["error"] = f"write failed: {e}"
                    return False, None

            self._audited_write([(key, frm, verdict, by)], _attempt)
            if err_holder:  # the transition never happened; its record was truncated
                return {"ok": False, "error": err_holder["error"]}
            out = {"ok": True, "key": key, "from": frm, "to": verdict, "by": by}
            if promoted_to:  # a hypothesis was promoted — its provenance was upgraded (PLAN Stage 8)
                out["provenance_upgraded_to"] = promoted_to
            return out
        finally:
            self.canon._release_lock()

    def _promote_hypothesis(self, edge, support_span: str, support_note: str):
        """The §1.2-3 / PLAN-Stage-8 hypothesized→grounded promotion gate: a span-less proposal earns
        grounding only with support, which UPGRADES its provenance. Mutates `edge` in place on success
        and returns (promoted_to, None); on a refusal it leaves the edge UNTOUCHED and returns
        (None, error) so the caller can refuse before any state change / audit / write. `support_span`
        (a verbatim source substring) → span-present; `support_note` (an external citation) → inferred;
        neither → `hypothesis-needs-support`."""
        restore = self._restore_fn()
        if support_span and support_span.strip():
            check = restore(support_span) if restore else support_span
            # source-aware (R4): verify against the edge's named source if it has one, else
            # any declared source. The not-in-ANY-source contract is unchanged
            # (support-span-not-in-source) — a promotion span just has to exist SOMEWHERE.
            if not self.source_set().verifies(check, source_file=edge.source_file):
                return None, "support-span-not-in-source"
            if len(normalize_text(check).replace(" ", "")) < MIN_SPAN_CHARS:
                return None, "support-span-too-short"
            edge.span = check
            edge.provenance = Provenance.SPAN_PRESENT       # upgraded: now citable
            return Provenance.SPAN_PRESENT.value, None
        if support_note and support_note.strip():
            edge.provenance = Provenance.INFERRED            # upgraded: asserted via external citation
            edge.notes = self._append_note(edge.notes, f"citation: {support_note.strip()}")
            return Provenance.INFERRED.value, None
        return None, "hypothesis-needs-support"

    def _audited_write(self, records, attempt):
        """The crash-safe audit+write dance shared by the two verdict-writing handlers (§1.8): capture
        the audit offset BEFORE appending so an orphan record can be dropped, append the audit record(s),
        run the caller-supplied `attempt()`, and TRUNCATE the audit back iff the write signals failure —
        so a failed transition never leaves an orphan record that would inflate _forged's count and let a
        genuine forgery slip past. `attempt` returns (ok, payload); the helper accepts the failure SIGNAL
        from the closure (a caught exception in kg_ground, an info.rolled_back in kg_rename) rather than
        assuming one, so both failure shapes route through the same truncate. `records` is an iterable of
        (key, frm, to, by) audit tuples. Returns the payload `attempt()` produced."""
        audit_offset = self._audit_size()
        for key, frm, to, by in records:
            self._audit(key, frm, to, by)
        ok, payload = attempt()
        if not ok:
            self._truncate_audit(audit_offset)
        return payload

    def _owner_of_edge(self, edge_id: str) -> Node | None:
        # O(1) lookup via the derived index (id -> source) instead of an O(N) full-canon scan per
        # kg_ground call, which made draining the grounding queue quadratic (server-2). The index is
        # read-only here; on a miss (just-written edge not yet projected, or no index) fall back to a
        # scan so correctness never depends on derived freshness.
        #
        # Do NOT _ensure_projected() here (review-M3): every prior kg_ground bumps node.updated_at, so
        # is_stale() returns True on the next call and _ensure_projected would run a full
        # betweenness/gate reproject — making a /kg-ground drain O(N * V*E), exactly the quadratic the
        # index was added to remove. Correctness doesn't need freshness: a just-written edge the index
        # hasn't seen is found by the canon-scan fallback below.
        try:
            src = self.projector.owner_of_edge(edge_id)
            if src and self.canon.exists(src):
                node = self.canon.read_node(src)
                if any(e.id == edge_id for e in node.edges):
                    return node
        except Exception:  # noqa: BLE001 — index trouble must never break grounding; fall back
            pass
        for n in self.canon.all_nodes():
            if any(e.id == edge_id for e in n.edges):
                return n
        return None

    def _audit_path(self) -> Path:
        return self.canon.root / GROUND_AUDIT

    def _audit_size(self) -> int:
        try:
            return self._audit_path().stat().st_size
        except OSError:
            return 0

    def _truncate_audit(self, offset: int) -> None:
        try:
            with open(self._audit_path(), "r+", encoding="utf-8") as f:
                f.truncate(offset)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    def _audit(self, key: str, frm: str, to: str, by: str) -> None:
        rec = {"key": key, "from": frm, "to": to, "by": by, "at": utcnow()}
        with open(self._audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())  # the audit log is tamper-evidence; make each record durable

    def _rewrite_endpoints(self, edge, old: str, new: str):
        """Rewrite an edge's old→new endpoints, recompute its deterministic id from the new endpoints,
        and report the integration-1 migration. Returns (changed, migration | None) where migration =
        (new_id, state_value) iff the id actually CHANGED and the edge is in a policed (verdict-or-
        obsolete) state — the load-bearing record that preserves grounding/failure memory across a
        rename, kept in ONE place so the two rename loops can never drift apart."""
        from .model import edge_id
        old_eid = edge.id
        if edge.source == old:
            edge.source = new
        if edge.target == old:
            edge.target = new
        edge.id = edge_id(edge.source, edge.relation, edge.target)  # keep id consistent with endpoints
        changed = edge.id != old_eid
        migration = ((edge.id, edge.epistemic_state.value)
                     if changed and edge.epistemic_state in GROUNDABLE_STATES else None)
        return changed, migration

    def kg_rename(self, old_id: str, new_id: str, *, message: str = "kg_rename") -> dict:
        """Rename a node and rewrite every edge endpoint referencing it (single-canonical-edge safe)."""
        old, new = slug(old_id), slug(new_id)
        if not self.canon.exists(old):
            return {"ok": False, "error": "node not found"}
        if self.canon.exists(new):
            return {"ok": False, "error": "target id exists"}
        try:
            node = self.canon.read_node(old)  # corrupt/invalid-UTF-8 note → structured error (F13/L1)
        except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
            return {"ok": False, "error": f"node unreadable: {e}", "old": old, "new": new}
        # A rename recomputes edge ids (and the node id), but the kg_ground audit record + reconciler
        # baseline are keyed by those ids. Collect every policed-state (verdict OR obsolete) item whose
        # id CHANGES so we can write a migrating audit record for the NEW id — otherwise the reconciler
        # sees a verdict at an id with no audit record and re-quarantines it, silently erasing the
        # grounding/failure memory (integration-1).
        migrations: list[tuple[str, str]] = []  # (new_key, state_value)
        if node.epistemic_state in GROUNDABLE_STATES:
            migrations.append((f"node:{new}", node.epistemic_state.value))
        node.id = new
        for e in node.edges:
            _, mig = self._rewrite_endpoints(e, old, new)
            if mig:
                migrations.append(mig)
        touched = [node]
        for other in self.canon.all_nodes():
            if other.id == old:
                continue
            node_changed = False
            for e in other.edges:
                changed, mig = self._rewrite_endpoints(e, old, new)
                node_changed |= changed
                if mig:
                    migrations.append(mig)
            if node_changed:
                touched.append(other)
        # Hold the lease across the whole audit + write + unlink + commit sequence so the migrating
        # audit records and their compensating truncate are atomic w.r.t. other writers (server-3).
        if not self.canon.try_acquire_lock():
            return {"ok": False, "error": "canon vault is locked by another live session",
                    "old": old, "new": new}
        try:
            # Emit the migrating audit records (compensated by truncation if the batch rolls back, like
            # kg_ground), then write the corrected nodes VERBATIM (merge=False): merging would
            # re-introduce each note's pre-rename edges (different id -> not deduped) and leave dangling
            # old endpoints. The offset/truncate dance lives in _audited_write, shared with kg_ground; here
            # the failure SIGNAL is info.rolled_back from write_nodes, not a caught exception.
            def _attempt():
                info = self.canon.write_nodes(touched, message=message, commit=False, merge=False)
                return (not info.rolled_back), info

            records = [(new_key, EpistemicState.UNVERIFIED.value, state, "agent")
                       for new_key, state in migrations]
            info = self._audited_write(records, _attempt)
            if info.rolled_back:
                # the batch rolled back — do NOT unlink the old note, or the node would be lost entirely
                # (its migrating audit records were already truncated by _audited_write).
                return {"ok": False, "error": f"rename rolled back: {info.error}", "old": old, "new": new}
            try:
                self.canon.node_path(old).unlink(missing_ok=True)
            except OSError as e:  # the new note already landed; surface a structured error, not a raw raise
                return {"ok": False, "error": f"rename wrote '{new}' but could not remove old '{old}': {e}",
                        "old": old, "new": new, "touched": [n.id for n in touched]}
            from .canon import _git, _git_ok
            if _git_ok(self.canon.root):
                # stage only what this rename touched — the rewritten notes + the removed old note —
                # instead of `git add -A` re-scanning the whole working tree per rename (server-9).
                paths = [str(self.canon.node_path(n.id)) for n in touched]
                paths.append(str(self.canon.node_path(old)))
                _git(self.canon.root, "add", "--", *paths, check=False)
                _git(self.canon.root, "commit", "-m", message, "--allow-empty", check=False)
            return {"ok": True, "old": old, "new": new, "touched": [n.id for n in touched]}
        finally:
            self.canon._release_lock()

    def kg_metrics(self) -> dict:
        # When the derived index is already fresh, serve counts from it with O(1) SQL instead of
        # re-parsing the whole canon (server-3). kg_metrics is not itself a projection trigger, so when
        # the index is stale we fall back to the authoritative canon parse rather than forcing a project.
        try:
            if self.projector.db_path.exists() and not self.projector.is_stale():
                con = self.projector._ro()
                try:
                    n = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                    e = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
                    by_state = dict(con.execute(
                        "SELECT epistemic_state, COUNT(*) FROM edges GROUP BY epistemic_state"))
                finally:
                    con.close()
                return {"nodes": n, "edges": e, "edges_by_epistemic_state": by_state}
        except Exception:  # noqa: BLE001 — any index hiccup falls back to the canon parse below
            pass
        nodes = self.canon.all_nodes()
        edges = [e for n in nodes for e in n.edges]
        by_state: dict = {}
        for e in edges:
            by_state[e.epistemic_state.value] = by_state.get(e.epistemic_state.value, 0) + 1
        return {"nodes": len(nodes), "edges": len(edges), "edges_by_epistemic_state": by_state}

    def _failure_ids(self, G=None) -> set:
        """Forward edge ids in failure memory (rejected/failed). The generators also check the reverse,
        so forward ids suffice for invariant 5 (PLAN §13: failure memory binds generation).

        When the caller already loaded the derived graph (kg_generate/kg_operate both do, right before
        calling this), pass it in to derive the ids from the in-memory edges instead of re-parsing the
        whole canon (server-6). The index keeps failure memory (§1.7 never prunes it), so the set is
        identical; EpistemicState subclasses str, so the string compare matches."""
        from .model import FAILURE_STATES
        if G is not None:
            fail = {s.value for s in FAILURE_STATES}
            return {d.get("id") for _, _, d in G.edges(data=True) if d.get("epistemic_state") in fail}
        return {e.id for e in self.canon.all_edges() if e.epistemic_state in FAILURE_STATES}

    def kg_generate(self, mechanism: str = "bridge", k: int = 10, second_graph: str | None = None) -> dict:
        """Generate hypothesized candidates from the derived graph (PLAN Stage 3 — the generative
        engine). Projects if stale, reads precomputed ranks O(1), dispatches to the chosen mechanism(s)
        (`bridge|seed|compression|regroup|transplant|ensemble`, or `all`/`default`), and returns ranked
        candidates. **READ-ONLY** — it never writes the canon; `/kg-generate` routes the candidates
        through the propose lane (`kg_propose`). Generate offensively; grounding judges later."""
        from .generate import run_generators
        self._ensure_projected()
        G = self.projector.load_graph()
        corpus = self.projector._corpus()
        failures = self._failure_ids(G)
        gate_on = int(next((G.nodes[n].get("gate_on", 0) for n in G.nodes()), 0))
        G2, note = None, ""
        if second_graph:
            try:
                G2 = self._second_graph(second_graph)
            except Exception as e:  # noqa: BLE001 — a bad second graph degrades, never crashes
                note = f"second_graph could not be loaded ({e}); ensemble degraded to regroup"
        if not note and G2 is None and mechanism in ("ensemble", "all"):
            note = "no second construction supplied; ensemble degraded to regroup (run /kg-perturb to supply one)"
        cands = run_generators(G, mechanism, pack=self.pack, corpus=corpus, failures=failures,
                               k=k, second_graph=G2)
        return {"mechanism": mechanism, "k": int(k), "gate_on": gate_on, "count": len(cands),
                "candidates": [c.to_dict() for c in cands], "note": note}

    def _second_graph(self, path: str):
        """Load a SECOND construction's graph.json into a NetworkX graph (raises on failure)."""
        from .generate import load_second_graph
        return load_second_graph(path)

    def kg_ensemble_graph(self, path: str) -> dict:
        """Load and summarise a SECOND construction's graph.json (PLAN Stage 7 — the §9/§15 ensemble /
        perturb path). Confirms a second construction projected before cross-generating against it via
        kg_generate(mechanism="ensemble", second_graph=<path>). Returns {ok, nodes, edges, path} or
        {ok: False, error}."""
        try:
            G2 = self._second_graph(path)
        except Exception as e:  # noqa: BLE001 — a missing/bad second graph is a structured error
            return {"ok": False, "error": str(e), "path": path}
        return {"ok": True, "path": path, "nodes": G2.number_of_nodes(), "edges": G2.number_of_edges()}

    def kg_absorption(self) -> dict:
        """Score the absorption window of grounded-from-hypothesized nodes (§14, PLAN Stage 5): how long
        each stayed perturbing before the graph renormalised. Reads the current derived graph plus the
        generation timeline at `derived/generations.json` — a `{generation: int, tracked: {id:
        {introduced_at, introduced_degree, mechanism}}}` ledger the /kg-generate command appends to.
        Returns per-node {half_life, status ∈ fertile|absorbed|isolated} so the slate can prefer the
        fertile middle. With no ledger yet, returns an empty result with a note (never an error)."""
        from .harness import absorption
        self._ensure_projected()
        try:
            data = json.loads(self.projector.graph_path.read_text(encoding="utf-8")) \
                if self.projector.graph_path.exists() else {"nodes": [], "links": []}
        except (ValueError, OSError):
            data = {"nodes": [], "links": []}
        hist_path = self.projector.derived / "generations.json"
        history, now = {}, None
        if hist_path.exists():
            try:
                blob = json.loads(hist_path.read_text(encoding="utf-8"))
                if isinstance(blob, dict):
                    history = blob.get("tracked", {}) if "tracked" in blob else blob
                    now = blob.get("generation")
            except (ValueError, OSError):
                history = {}
        result = absorption(data, history, now=now)
        summary = {s: sum(1 for v in result.values() if v["status"] == s)
                   for s in ("fertile", "absorbed", "isolated")}
        return {"tracked": len(result), "summary": summary, "nodes": result,
                "note": ("" if history else
                         "no generations.json yet — run /kg-generate to start tracking the absorption window")}

    def kg_operate(self, op: str, *, target: str | None = None, label: str = "", body: str = "",
                   members=None, k: int | None = None) -> dict:
        """Run one of the four endo operations (§8, PLAN Stage 4), persisting the result through the
        propose lane. collapse → compression node + collapses_into edges; explode → latent facet
        children; regroup → §8 re-partition bridges; open → a new primitive + attachment points. The
        write goes through kg_propose, so it lands hypothesized/unverified with no span — never a
        verdict, never a forged text anchor."""
        from . import operations as ops
        op = (op or "").lower()
        fn = ops.DISPATCH.get(op)
        if fn is None:
            return {"ok": False, "error": f"unknown op {op!r}; expected collapse|explode|regroup|open"}
        self._ensure_projected()
        G = self.projector.load_graph()
        if op == "collapse":
            payload, info = fn(G, target=target, members=members, label=label, body=body)
        elif op == "explode":
            payload, info = fn(G, target=target, k=k, label=label, body=body)
        elif op == "regroup":
            payload, info = fn(G, failures=self._failure_ids(G), k=k or 10)
        else:  # open
            payload, info = fn(G, label=label, body=body, k=k or 2)
        if not payload or not (payload.get("nodes") or payload.get("edges")):
            return {"ok": False, "op": op, "error": "no structure to operate on", "info": info}
        result = self.kg_propose(payload, message=f"kg_operate:{op}")
        result.update({"ok": True, "op": op, "info": info})
        return result

    # ---- read surface (projects if stale, then reads precomputed ranks)
    def _ensure_projected(self) -> None:
        if not self.projector.db_path.exists() or self.projector.is_stale():
            self.projector.project()

    @property
    def _proj(self) -> Projector:
        """The lazy-projection read seam: ensure the derived layer is fresh, then return the projector.
        The single edit point for the projection trigger every pure read delegate goes through."""
        self._ensure_projected()
        return self.projector

    def get_node(self, node_id: str) -> dict | None:
        return self._proj.get_node(node_id)

    def get_neighbors(self, node_id: str, relation: str | None = None) -> list:
        return self._proj.get_neighbors(node_id, relation=relation)

    def shortest_path(self, source: str, target: str):
        return self._proj.shortest_path(source, target)

    def query_graph(self, **kw) -> dict:
        return self._proj.query_graph(**kw)

    def kg_context(self, query: str | None = None, budget: int = 2000) -> dict:
        return self._proj.kg_context(query, budget=budget)

    def kg_agenda(self, *, limit: int = 5) -> dict:
        return self._proj.kg_agenda(limit=limit)

    def kg_export(self, kind: str = "all") -> dict:
        """Render the human-facing artifacts (R1): a self-contained `graph.html` + `GRAPH_REPORT.md` under
        the derived dir. Read-only — projects-if-stale, then consumes only the derived layer; never writes
        the canon and never `_atomic_write`s graph.json/index.sqlite."""
        self._ensure_projected()
        from . import export as _export
        return _export.export(self, kind=kind)


# --------------------------------------------------------------------------- MCP wiring


def build_engine_from_env(*, project=None, data=None, source=None, pack=None) -> KGEngine:
    """Construct a KGEngine from environment config, with optional explicit overrides (CLI flags win
    over env). All resolution — project dir, source, pack auto-discovery, and the flood rate limit —
    lives here so every caller (MCP server, headless backend) gets identical behavior."""
    # Treat an empty OR unsubstituted `${...}` env value the same as unset. When a `${user_config.*}`
    # placeholder is never substituted — e.g. `source_path`, which has no default in plugin.json — Claude
    # Code passes the literal `${user_config.source_path}` through `.mcp.json`. Taking that as a real path
    # silently breaks the engine: `source_text()` reads a non-existent file and returns "", so every agent
    # edge fails span verification (`span-not-in-source`). Mirrors `bootstrap._clean` / `launch_server.clean`,
    # which strip the same values; without it the documented `examples/source.md` fallback never fires.
    def _env(key):  # noqa: E306
        v = (os.environ.get(key) or "").strip()
        return None if not v or v.startswith("${") else v
    project = project or _env("KG_PROJECT_DIR") or _env("CLAUDE_PROJECT_DIR") or os.getcwd()
    data = data or _env("KG_DATA")
    opt = lambda k, d=None: (os.environ.get(f"CLAUDE_PLUGIN_OPTION_{k}") or "").strip() or d  # noqa: E731
    src = source or opt("SOURCE_PATH") or _env("KG_SOURCE_PATH")
    if not src:
        # documented default: build/ground against the bundled example when nothing is configured
        guess = Path(project) / "examples" / "source.md"
        src = str(guess) if guess.exists() else None
    pack_path = pack or _env("KG_PACK_PATH")
    if not pack_path:
        guess = Path(project) / "pack" / "pack.yaml"
        pack_path = str(guess) if guess.exists() else None
    try:
        rate = float(os.environ["KG_MAX_EDGES_PER_KB"])
        if not math.isfinite(rate) or rate < 0:  # 'nan'/'inf'/negative would crash or disable the limiter
            rate = DEFAULT_MAX_EDGES_PER_KB
    except (KeyError, ValueError):
        rate = DEFAULT_MAX_EDGES_PER_KB
    return KGEngine(project, data, source_path=src, pack_path=pack_path,
                    sensitivity=opt("SENSITIVITY", "medium"), metrics_mode=opt("METRICS_MODE", "structure_only"),
                    max_edges_per_kb=rate)


def _register(mcp, engine: KGEngine) -> None:
    @mcp.tool()
    def kg_ping() -> dict:
        """Health check: returns the engine version and configuration."""
        return engine.kg_ping()

    @mcp.tool()
    def kg_scrub(text: str | None = None) -> dict:
        """Egress PII/secret scrub (§1.9): redact a snippet (or the source) with consistent placeholders
        before handing text to a subagent; the canon later restores spans to the original."""
        return engine.kg_scrub(text)

    @mcp.tool()
    def kg_write(payload: dict) -> dict:
        """Validate an extraction payload at the boundary and write accepted/demoted nodes & edges."""
        return engine.kg_write(payload)

    @mcp.tool()
    def kg_propose(payload: dict) -> dict:
        """Propose hypothesized candidates (PLAN Stage 1: the propose lane). Forces every item to
        provenance=hypothesized (a discovery-mechanism proposal, no span needed) and REFUSES any
        span-present/inferred text claim with reason `propose-lane-text-claim` — text claims belong on
        kg_write. Candidates land `unverified`; only kg_ground (with support) can ever promote them."""
        return engine.kg_propose(payload)

    @mcp.tool()
    def kg_ground(target_id: str, verdict: str, kind: str = "edge", note: str = "",
                  support_span: str = "", support_note: str = "") -> dict:
        """Apply a grounding verdict (grounded|rejected|failed|obsolete) to an edge or node. Verdicts
        applied via this tool are always attributed to the agent — a human verdict cannot be forged
        through the tool surface (§1.4). To PROMOTE a hypothesized edge to grounded you MUST supply
        support, which upgrades its provenance: `support_span` (a verbatim source substring → span-present)
        or `support_note` (an external citation → inferred); without either, the promotion is refused with
        `hypothesis-needs-support`."""
        return engine.kg_ground(target_id, verdict, by="agent", kind=kind, note=note,
                                support_span=support_span, support_note=support_note)

    @mcp.tool()
    def kg_rename(old_id: str, new_id: str) -> dict:
        """Rename a node and rewrite every edge endpoint referencing it."""
        return engine.kg_rename(old_id, new_id)

    @mcp.tool()
    def kg_metrics() -> dict:
        """Summary counts: nodes, edges, edges by epistemic state."""
        return engine.kg_metrics()

    @mcp.tool()
    def kg_generate(mechanism: str = "bridge", k: int = 10, second_graph: str | None = None) -> dict:
        """Generate hypothesized idea candidates from the graph's structure (PLAN Stage 3). Mechanisms:
        bridge (§2/§4), seed (§3 residual), compression (§7 new nodes), regroup (§8), transplant (§5),
        ensemble (§9) — or "all"/"default". READ-ONLY: candidates are proposals (provenance=hypothesized,
        no span); route them through kg_propose. Generate offensively; kg_ground judges later."""
        return engine.kg_generate(mechanism=mechanism, k=k, second_graph=second_graph)

    @mcp.tool()
    def kg_absorption() -> dict:
        """Absorption window (§14): for each grounded-from-hypothesized node, how long it stayed
        perturbing before the graph renormalised — {half_life, status ∈ fertile|absorbed|isolated}.
        Reads derived/generations.json (written by /kg-generate). Prefer the fertile middle."""
        return engine.kg_absorption()

    @mcp.tool()
    def kg_operate(op: str, target: str | None = None, label: str = "", body: str = "",
                   members: list[str] | None = None, k: int | None = None) -> dict:
        """The four endo operations (§8) that WRITE hypothesized structure via the propose lane:
        collapse (cluster→compression node + collapses_into), explode (node→latent facet children),
        regroup (persist §8 re-partition bridges), open (new primitive + attachment points). Everything
        lands hypothesized/unverified with no span — never a verdict, never a forged anchor.
        `members` names an explicit member set for collapse (else the cluster is inferred from target)."""
        return engine.kg_operate(op, target=target, label=label, body=body, members=members, k=k)

    @mcp.tool()
    def query_graph(node_type: str | None = None, relation: str | None = None,
                    epistemic_state: str | None = None, limit: int = 50) -> dict:
        """Query nodes/edges by type, relation, or epistemic state (ranked by precomputed degree)."""
        return engine.query_graph(node_type=node_type, relation=relation,
                                  epistemic_state=epistemic_state, limit=limit)

    @mcp.tool()
    def get_node(node_id: str) -> dict:
        """Fetch a node with its incident edges."""
        return engine.get_node(node_id) or {"error": "not found"}

    @mcp.tool()
    def get_neighbors(node_id: str, relation: str | None = None) -> list:
        """Edges incident to a node, optionally filtered by relation."""
        return engine.get_neighbors(node_id, relation)

    @mcp.tool()
    def shortest_path(source: str, target: str) -> dict:
        """Shortest path between two nodes over the derived graph."""
        return {"path": engine.shortest_path(source, target)}

    @mcp.tool()
    def kg_context(query: str | None = None, budget: int = 2000) -> dict:
        """Grounding-aware, provenance-carrying, token-budgeted context for the session."""
        return engine.kg_context(query, budget)

    @mcp.tool()
    def kg_agenda(limit: int = 5) -> dict:
        """Read-only structural "suggested questions" (R6). Reads ONLY precomputed derived columns and
        returns ~limit structural gaps split into answerable_now[] (well-grounded neighbourhoods) vs
        blocked_on_grounding[] (orphans, hypothesized-only neighbourhoods, under-grounded hubs,
        disconnected clusters). Ranked by the honest gate-aware signal (mirrors kg_context). It suggests,
        never acts — asserts no edges, copies no spans, stamps no verdicts (measure-never-gate); a
        hypothesized-only neighbourhood surfaces as BLOCKED, never as answerable. Heuristic, not a guarantee."""
        return engine.kg_agenda(limit=limit)

    @mcp.tool()
    def kg_export(kind: str = "all") -> dict:
        """Render the human-facing artifacts (R1): a self-contained, offline `graph.html` (vanilla-JS force
        layout encoding the three axes on independent channels — epistemic_state→line, authored_by→border,
        provenance→fill; size=degree; failed/rejected edges drawn, never filtered) and a `GRAPH_REPORT.md`,
        under the derived dir. `kind ∈ {html, report, all}`. READ-ONLY — projects-if-stale, consumes only the
        derived layer, writes only its two disposable artifacts; never forges a verdict or touches the canon."""
        return engine.kg_export(kind)


def main() -> None:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("creativity-graph")
    engine = build_engine_from_env()
    _register(mcp, engine)
    mcp.run()


if __name__ == "__main__":
    main()
