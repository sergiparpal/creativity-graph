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
from .boundary import DEFAULT_MAX_EDGES_PER_KB, merge_results_into_nodes, validate_payload
from .canon import Canon
from .model import Disposition, EpistemicState, GROUNDABLE_STATES, Node, Provenance, slug, utcnow
from .pack import load_pack
from .projector import Projector
from .reconciler import GROUND_AUDIT, Reconciler
from .scrub import Scrubber

# Single source of truth shared with the reconciler's policed set (model.GROUNDABLE_STATES), so the
# states kg_ground may stamp and the states the reconciler re-quarantines can never drift apart.
VALID_VERDICTS = {s.value for s in GROUNDABLE_STATES}


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
                                   source_text=self.source_text)
        self.scrubber = Scrubber(sensitivity)
        self._scrub_map: dict[str, str] = {}  # accumulated egress placeholder -> original (§1.9)
        self.sensitivity = sensitivity
        self.metrics_mode = metrics_mode
        self.max_edges_per_kb = max_edges_per_kb
        self.source_path = Path(source_path) if source_path else None
        self.pack = None
        if pack_path and Path(pack_path).exists():
            try:
                self.pack = load_pack(pack_path)
            except Exception:  # noqa: BLE001 — a bad pack must not crash the server
                self.pack = None

    # ---- source text (for span verification)
    def source_text(self) -> str:
        if self.source_path and self.source_path.exists():
            return self.source_path.read_text(encoding="utf-8")
        return ""

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

    def kg_write(self, payload: dict, *, message: str = "kg_write") -> dict:
        """Validate an extraction payload at the boundary and write accepted/demoted items."""
        # if egress scrubbing happened this session, restore placeholder spans to the original before
        # span verification, and store the original in the canon (§1.9).
        restore = (lambda s: Scrubber.restore(s, self._scrub_map)) if self._scrub_map else None
        existing_nodes = self.canon.all_nodes()  # read once; derive edges + node baseline from it
        existing_edges = [e for n in existing_nodes for e in n.edges]
        results = validate_payload(payload, pack=self.pack, source_text=self.source_text(),
                                   existing=existing_edges,
                                   existing_node_ids={n.id for n in existing_nodes},
                                   restore=restore, max_edges_per_kb=self.max_edges_per_kb)
        nodes = merge_results_into_nodes(results)
        info = self.canon.write_nodes(list(nodes.values()), message=message) if nodes else None
        summary: dict = {d.value: 0 for d in Disposition}
        for r in results:
            summary[r.disposition.value] += 1
        return {
            "dispositions": summary,
            "details": [{"kind": r.kind, "id": getattr(r.item, "id", None), "disposition": r.disposition.value,
                         "reason": r.reason, "retryable": r.retryable} for r in results],
            "written_nodes": list(nodes),
            "rolled_back": bool(info and info.rolled_back),
            "error": (info.error if info and info.rolled_back else None),
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
                  note: str = "") -> dict:
        """Apply a grounding verdict (the ONLY path that may set a verdict state). Stamps the verdict
        and appends an audit record so the reconciler treats the transition as legitimate (§1.8)."""
        verdict = verdict.lower()
        if verdict not in VALID_VERDICTS:
            return {"ok": False, "error": f"invalid verdict {verdict!r}"}
        # `by` is provenance, not a free-text field: clamp to the known actors so a stray value can't
        # masquerade as a verdict author (the MCP tool surface already pins this to "agent").
        by = by if by in ("agent", "human", "deterministic") else "agent"
        state = EpistemicState(verdict)
        if kind == "node":
            if not self.canon.exists(target_id):
                return {"ok": False, "error": "node not found"}
            node = self.canon.read_node(target_id)
            frm = node.epistemic_state.value
            node.epistemic_state = state
            key = f"node:{node.id}"
        else:
            node = self._owner_of_edge(target_id)
            if node is None:
                return {"ok": False, "error": "edge not found"}
            edge = next(e for e in node.edges if e.id == target_id)
            frm = edge.epistemic_state.value
            edge.epistemic_state = state
            edge.verdict_by = by
            edge.verdict_at = utcnow()
            if note:
                edge.notes = (edge.notes + " | " if edge.notes else "") + note
            key = edge.id
        # Hold the single-writer lease across the WHOLE audit-append + write + compensating-truncate
        # sequence so it is atomic w.r.t. other writers. Otherwise a concurrent session's legitimate
        # audit append could land between our offset capture and our truncate, and our whole-tail
        # truncate would discard it (server-3). write_one re-acquires the lease re-entrantly.
        if not self.canon.try_acquire_lock():
            return {"ok": False, "error": "canon vault is locked by another live session"}
        try:
            # Append the audit record BEFORE persisting the verdict: a CRASH between the two leaves an
            # audit record with no state change (harmless, unconsumed) rather than a verdict with no
            # audit record (which the reconciler would re-quarantine). But a CAUGHT write failure (slug
            # collision, disk error) must not leave that orphan record — on a later legitimate retry of
            # the same key->state it would inflate the count and let one genuine forgery slip past
            # _forged's count check. So we record the pre-append offset and truncate it back on failure.
            audit_offset = self._audit_size()
            self._audit(key, frm, verdict, by)
            try:
                self.canon.write_one(node)
            except Exception as e:  # noqa: BLE001 — surface as a structured error, not an MCP exception
                self._truncate_audit(audit_offset)  # the transition never happened; drop its record
                return {"ok": False, "error": f"write failed: {e}"}
            return {"ok": True, "key": key, "from": frm, "to": verdict, "by": by}
        finally:
            self.canon._release_lock()

    def _owner_of_edge(self, edge_id: str) -> Node | None:
        # O(1) lookup via the derived index (id -> source) instead of an O(N) full-canon scan per
        # kg_ground call, which made draining the grounding queue quadratic (server-2). The index is
        # read-only here; on a miss (just-written edge not yet projected, or no index) fall back to a
        # scan so correctness never depends on derived freshness.
        try:
            self._ensure_projected()
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

    def kg_rename(self, old_id: str, new_id: str, *, message: str = "kg_rename") -> dict:
        """Rename a node and rewrite every edge endpoint referencing it (single-canonical-edge safe)."""
        from .model import edge_id
        old, new = slug(old_id), slug(new_id)
        if not self.canon.exists(old):
            return {"ok": False, "error": "node not found"}
        if self.canon.exists(new):
            return {"ok": False, "error": "target id exists"}
        node = self.canon.read_node(old)
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
            old_eid = e.id
            if e.source == old:
                e.source = new
            e.id = edge_id(e.source, e.relation, e.target)  # keep edge id consistent with endpoints
            if e.id != old_eid and e.epistemic_state in GROUNDABLE_STATES:
                migrations.append((e.id, e.epistemic_state.value))
        touched = [node]
        for other in self.canon.all_nodes():
            if other.id == old:
                continue
            changed = False
            for e in other.edges:
                old_eid = e.id
                ec = False
                if e.target == old:
                    e.target = new; ec = True
                if e.source == old:
                    e.source = new; ec = True
                if ec:
                    e.id = edge_id(e.source, e.relation, e.target)
                    changed = True
                    if e.id != old_eid and e.epistemic_state in GROUNDABLE_STATES:
                        migrations.append((e.id, e.epistemic_state.value))
            if changed:
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
            # old endpoints.
            audit_offset = self._audit_size()
            for new_key, state in migrations:
                self._audit(new_key, EpistemicState.UNVERIFIED.value, state, "agent")
            info = self.canon.write_nodes(touched, message=message, commit=False, merge=False)
            if info.rolled_back:
                # the batch rolled back — do NOT unlink the old note, or the node would be lost entirely
                self._truncate_audit(audit_offset)
                return {"ok": False, "error": f"rename rolled back: {info.error}", "old": old, "new": new}
            self.canon.node_path(old).unlink(missing_ok=True)
            from .canon import _git, _git_ok
            if _git_ok(self.canon.root):
                _git(self.canon.root, "add", "-A", check=False)
                _git(self.canon.root, "commit", "-m", message, "--allow-empty", check=False)
            return {"ok": True, "old": old, "new": new, "touched": [n.id for n in touched]}
        finally:
            self.canon._release_lock()

    def kg_metrics(self) -> dict:
        nodes = self.canon.all_nodes()
        edges = [e for n in nodes for e in n.edges]
        by_state: dict = {}
        for e in edges:
            by_state[e.epistemic_state.value] = by_state.get(e.epistemic_state.value, 0) + 1
        return {"nodes": len(nodes), "edges": len(edges), "edges_by_epistemic_state": by_state}

    def _failure_ids(self) -> set:
        """Forward edge ids in failure memory (rejected/failed). The generators also check the reverse,
        so forward ids suffice for invariant 5 (PLAN §13: failure memory binds generation)."""
        from .model import FAILURE_STATES
        return {e.id for e in self.canon.all_edges() if e.epistemic_state in FAILURE_STATES}

    def kg_generate(self, mechanism: str = "bridge", k: int = 10, second_graph: str | None = None) -> dict:
        """Generate hypothesized candidates from the derived graph (PLAN Stage 3 — the generative
        engine). Projects if stale, reads precomputed ranks O(1), dispatches to the chosen mechanism(s)
        (`bridge|seed|compression|regroup|transplant|ensemble`, or `all`/`default`), and returns ranked
        candidates. **READ-ONLY** — it never writes the canon; `/kg-generate` routes the candidates
        through the propose lane (`kg_propose`). Generate offensively; grounding judges later."""
        from .generate import load_second_graph, run_generators
        self._ensure_projected()
        G = self.projector.load_graph()
        corpus = self.projector._corpus()
        failures = self._failure_ids()
        gate_on = int(next((G.nodes[n].get("gate_on", 0) for n in G.nodes()), 0))
        G2, note = None, ""
        if second_graph:
            try:
                G2 = load_second_graph(second_graph)
            except Exception as e:  # noqa: BLE001 — a bad second graph degrades, never crashes
                note = f"second_graph could not be loaded ({e}); ensemble degraded to regroup"
        if not note and G2 is None and mechanism in ("ensemble", "all"):
            note = "no second construction supplied; ensemble degraded to regroup (run /kg-perturb to supply one)"
        cands = run_generators(G, mechanism, pack=self.pack, corpus=corpus, failures=failures,
                               k=k, second_graph=G2)
        return {"mechanism": mechanism, "k": int(k), "gate_on": gate_on, "count": len(cands),
                "candidates": [c.to_dict() for c in cands], "note": note}

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
            data = json.loads(self.projector.graph_path.read_text()) if self.projector.graph_path.exists() \
                else {"nodes": [], "links": []}
        except (ValueError, OSError):
            data = {"nodes": [], "links": []}
        hist_path = self.projector.derived / "generations.json"
        history, now = {}, None
        if hist_path.exists():
            try:
                blob = json.loads(hist_path.read_text())
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
            payload, info = fn(G, failures=self._failure_ids(), k=k or 10)
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

    def get_node(self, node_id: str) -> dict | None:
        self._ensure_projected(); return self.projector.get_node(node_id)

    def get_neighbors(self, node_id: str, relation: str | None = None) -> list:
        self._ensure_projected(); return self.projector.get_neighbors(node_id, relation=relation)

    def shortest_path(self, source: str, target: str):
        self._ensure_projected(); return self.projector.shortest_path(source, target)

    def query_graph(self, **kw) -> dict:
        self._ensure_projected(); return self.projector.query_graph(**kw)

    def kg_context(self, query: str | None = None, budget: int = 2000) -> dict:
        self._ensure_projected(); return self.projector.kg_context(query, budget=budget)


# --------------------------------------------------------------------------- MCP wiring


def build_engine_from_env(*, project=None, data=None, source=None, pack=None) -> KGEngine:
    """Construct a KGEngine from environment config, with optional explicit overrides (CLI flags win
    over env). All resolution — project dir, source, pack auto-discovery, and the flood rate limit —
    lives here so every caller (MCP server, headless backend) gets identical behavior."""
    project = project or os.environ.get("KG_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    data = data or os.environ.get("KG_DATA")
    # treat an empty env value the same as unset, so a blank `${user_config.*}` substitution falls back
    # to the documented default instead of an empty string the engine then misreports.
    opt = lambda k, d=None: (os.environ.get(f"CLAUDE_PLUGIN_OPTION_{k}") or "").strip() or d  # noqa: E731
    src = source or opt("SOURCE_PATH") or (os.environ.get("KG_SOURCE_PATH") or "").strip() or None
    if not src:
        # documented default: build/ground against the bundled example when nothing is configured
        guess = Path(project) / "examples" / "source.md"
        src = str(guess) if guess.exists() else None
    pack_path = pack or os.environ.get("KG_PACK_PATH")
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
    def kg_scrub(text: str = None) -> dict:
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
    def kg_ground(target_id: str, verdict: str, kind: str = "edge", note: str = "") -> dict:
        """Apply a grounding verdict (grounded|rejected|failed|obsolete) to an edge or node. Verdicts
        applied via this tool are always attributed to the agent — a human verdict cannot be forged
        through the tool surface (§1.4)."""
        return engine.kg_ground(target_id, verdict, by="agent", kind=kind, note=note)

    @mcp.tool()
    def kg_rename(old_id: str, new_id: str) -> dict:
        """Rename a node and rewrite every edge endpoint referencing it."""
        return engine.kg_rename(old_id, new_id)

    @mcp.tool()
    def kg_metrics() -> dict:
        """Summary counts: nodes, edges, edges by epistemic state."""
        return engine.kg_metrics()

    @mcp.tool()
    def kg_generate(mechanism: str = "bridge", k: int = 10, second_graph: str = None) -> dict:
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
    def kg_operate(op: str, target: str = None, label: str = "", body: str = "", k: int = None) -> dict:
        """The four endo operations (§8) that WRITE hypothesized structure via the propose lane:
        collapse (cluster→compression node + collapses_into), explode (node→latent facet children),
        regroup (persist §8 re-partition bridges), open (new primitive + attachment points). Everything
        lands hypothesized/unverified with no span — never a verdict, never a forged anchor."""
        return engine.kg_operate(op, target=target, label=label, body=body, k=k)

    @mcp.tool()
    def query_graph(node_type: str = None, relation: str = None, epistemic_state: str = None,
                    limit: int = 50) -> dict:
        """Query nodes/edges by type, relation, or epistemic state (ranked by precomputed degree)."""
        return engine.query_graph(node_type=node_type, relation=relation,
                                  epistemic_state=epistemic_state, limit=limit)

    @mcp.tool()
    def get_node(node_id: str) -> dict:
        """Fetch a node with its incident edges."""
        return engine.get_node(node_id) or {"error": "not found"}

    @mcp.tool()
    def get_neighbors(node_id: str, relation: str = None) -> list:
        """Edges incident to a node, optionally filtered by relation."""
        return engine.get_neighbors(node_id, relation)

    @mcp.tool()
    def shortest_path(source: str, target: str) -> dict:
        """Shortest path between two nodes over the derived graph."""
        return {"path": engine.shortest_path(source, target)}

    @mcp.tool()
    def kg_context(query: str = None, budget: int = 2000) -> dict:
        """Grounding-aware, provenance-carrying, token-budgeted context for the session."""
        return engine.kg_context(query, budget)


def main() -> None:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("creativity-graph")
    engine = build_engine_from_env()
    _register(mcp, engine)
    mcp.run()


if __name__ == "__main__":
    main()
