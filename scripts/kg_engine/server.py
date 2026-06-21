"""The MCP server (§2.4): the graphify-shaped tool surface + our grounding semantics.

Tool logic lives in the importable `KGEngine` facade so it is unit-testable without an MCP client;
the FastMCP wrappers are thin. Elicitation requests always declare a default applied if unanswered,
so the flow never stalls (§2.4, §4).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import __version__
from .boundary import DEFAULT_MAX_EDGES_PER_KB, merge_results_into_nodes, validate_payload
from .canon import Canon
from .model import Disposition, EpistemicState, Node, slug, utcnow
from .pack import load_pack
from .projector import Projector
from .reconciler import GROUND_AUDIT, Reconciler
from .scrub import Scrubber

VALID_VERDICTS = {"grounded", "rejected", "failed", "obsolete"}


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
        self.projector = Projector(self.canon, self.data_dir / "derived", metrics_mode=metrics_mode)
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
        results = validate_payload(payload, pack=self.pack, source_text=self.source_text(),
                                   existing=self.canon.all_edges(), restore=restore,
                                   max_edges_per_kb=self.max_edges_per_kb)
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
            "rolled_back": bool(info and info.stashed),
            "stash_ref": info.stash_ref if info else None,
        }

    def kg_ground(self, target_id: str, verdict: str, *, by: str = "agent", kind: str = "edge",
                  note: str = "") -> dict:
        """Apply a grounding verdict (the ONLY path that may set a verdict state). Stamps the verdict
        and appends an audit record so the reconciler treats the transition as legitimate (§1.8)."""
        verdict = verdict.lower()
        if verdict not in VALID_VERDICTS:
            return {"ok": False, "error": f"invalid verdict {verdict!r}"}
        state = EpistemicState(verdict)
        if kind == "node":
            if not self.canon.exists(target_id):
                return {"ok": False, "error": "node not found"}
            node = self.canon.read_node(target_id)
            frm = node.epistemic_state.value
            node.epistemic_state = state
            key = f"node:{node.id}"
            self.canon.write_one(node)
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
            self.canon.write_one(node)
        self._audit(key, frm, verdict, by)
        return {"ok": True, "key": key, "from": frm, "to": verdict, "by": by}

    def _owner_of_edge(self, edge_id: str) -> Node | None:
        for n in self.canon.all_nodes():
            if any(e.id == edge_id for e in n.edges):
                return n
        return None

    def _audit(self, key: str, frm: str, to: str, by: str) -> None:
        rec = {"key": key, "from": frm, "to": to, "by": by, "at": utcnow()}
        path = self.canon.root / GROUND_AUDIT
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def kg_rename(self, old_id: str, new_id: str, *, message: str = "kg_rename") -> dict:
        """Rename a node and rewrite every edge endpoint referencing it (single-canonical-edge safe)."""
        old, new = slug(old_id), slug(new_id)
        if not self.canon.exists(old):
            return {"ok": False, "error": "node not found"}
        if self.canon.exists(new):
            return {"ok": False, "error": "target id exists"}
        node = self.canon.read_node(old)
        node.id = new
        for e in node.edges:
            if e.source == old:
                e.source = new
        touched = [node]
        for other in self.canon.all_nodes():
            if other.id == old:
                continue
            changed = False
            for e in other.edges:
                if e.target == old:
                    e.target = new; changed = True
                if e.source == old:
                    e.source = new; changed = True
            if changed:
                touched.append(other)
        # write new + others, remove the old file
        info = self.canon.write_nodes(touched, message=message, commit=False)
        self.canon.node_path(old).unlink(missing_ok=True)
        from .canon import _git, _git_ok
        if _git_ok(self.canon.root):
            _git(self.canon.root, "add", "-A", check=False)
            _git(self.canon.root, "commit", "-m", message, "--allow-empty", check=False)
        return {"ok": not info.stashed, "old": old, "new": new, "touched": [n.id for n in touched]}

    def kg_metrics(self) -> dict:
        nodes = self.canon.all_nodes()
        edges = [e for n in nodes for e in n.edges]
        by_state: dict = {}
        for e in edges:
            by_state[e.epistemic_state.value] = by_state.get(e.epistemic_state.value, 0) + 1
        return {"nodes": len(nodes), "edges": len(edges), "edges_by_epistemic_state": by_state}

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
    opt = lambda k, d=None: os.environ.get(f"CLAUDE_PLUGIN_OPTION_{k}", d)  # noqa: E731
    src = source or opt("SOURCE_PATH") or os.environ.get("KG_SOURCE_PATH")
    pack_path = pack or os.environ.get("KG_PACK_PATH")
    if not pack_path:
        guess = Path(project) / "pack" / "pack.yaml"
        pack_path = str(guess) if guess.exists() else None
    try:
        rate = float(os.environ["KG_MAX_EDGES_PER_KB"])
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
    def kg_ground(target_id: str, verdict: str, by: str = "agent", kind: str = "edge", note: str = "") -> dict:
        """Apply a grounding verdict (grounded|rejected|failed|obsolete) to an edge or node."""
        return engine.kg_ground(target_id, verdict, by=by, kind=kind, note=note)

    @mcp.tool()
    def kg_rename(old_id: str, new_id: str) -> dict:
        """Rename a node and rewrite every edge endpoint referencing it."""
        return engine.kg_rename(old_id, new_id)

    @mcp.tool()
    def kg_metrics() -> dict:
        """Summary counts: nodes, edges, edges by epistemic state."""
        return engine.kg_metrics()

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
