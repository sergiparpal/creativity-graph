"""The derived layer (§1.2): canon -> NetworkX node-link graph.json + SQLite index.

- Leiden communities (igraph + leidenalg; graceful label-propagation fallback if unavailable)
- precomputed ranks: local DEGREE (cheap advisory) + a labelled STRUCTURAL-BRIDGE signal
  (a node whose neighbours span >=2 communities, §1.4/§1.6) — computed OFF the hot path
- incremental reproject keyed by per-file content hash (mismatch => stale => rebuild)
- kg_context: reads precomputed ranks O(1), token-budgeted, carries provenance + epistemic tier +
  falsification counters; NEVER computes centrality in-request
- the derived layer contains nothing the canon does not, and never prunes failure memory (§1.7)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from .canon import Canon, _atomic_write
from .model import EpistemicState, FAILURE_STATES, Provenance

GRAPH_JSON = "graph.json"
INDEX_DB = "index.sqlite"
# Hard ceiling on the kg_context token budget so a client passing a huge value can't make the engine
# serialize the entire edge table into one response (server-4). The limit clamp on query_graph is the
# row-count analogue.
MAX_CONTEXT_TOKENS = 100_000


def _like_escape(term: str) -> str:
    """Escape SQL LIKE wildcards so a query term like `span_present` or `100%` matches literally
    (the matching clauses use `ESCAPE '\\'`)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------- node-link (version-robust)


def _node_link_data(G) -> dict:
    try:
        return nx.node_link_data(G, edges="links")
    except TypeError:
        d = nx.node_link_data(G)
        if "links" not in d and "edges" in d:
            d["links"] = d.pop("edges")
        return d


def node_link_graph(data: dict):
    try:
        return nx.node_link_graph(data, edges="links", directed=data.get("directed", True))
    except TypeError:
        return nx.node_link_graph(data, directed=data.get("directed", True))


# --------------------------------------------------------------------------- communities


def _leiden(undirected: nx.Graph) -> dict:
    """Return node_id -> community int. Leiden if available, else label propagation."""
    if undirected.number_of_nodes() == 0:
        return {}
    try:
        import igraph as ig
        import leidenalg as la

        nodes = list(undirected.nodes())
        idx = {n: i for i, n in enumerate(nodes)}
        edges = [(idx[u], idx[v]) for u, v in undirected.edges()]
        g = ig.Graph(n=len(nodes), edges=edges, directed=False)
        part = la.find_partition(g, la.RBConfigurationVertexPartition, seed=42)
        return {nodes[i]: m for i, m in enumerate(part.membership)}
    except Exception:  # noqa: BLE001 — any import/runtime failure degrades to fallback
        communities = nx.community.label_propagation_communities(undirected)
        return {n: ci for ci, com in enumerate(communities) for n in com}


# --------------------------------------------------------------------------- reports


@dataclass
class ProjectReport:
    up_to_date: bool = False
    full_rebuild: bool = False
    n_nodes: int = 0
    n_edges: int = 0
    communities: int = 0
    touched_nodes: list[str] = field(default_factory=list)
    touched_edges: list[str] = field(default_factory=list)
    built_from_commit: str = ""


# --------------------------------------------------------------------------- projector


class Projector:
    def __init__(self, canon: Canon, derived_dir: str | Path | None = None, *, metrics_mode: str = "structure_only"):
        self.canon = canon
        self.derived = Path(derived_dir) if derived_dir else (canon.root / "derived")
        self.derived.mkdir(parents=True, exist_ok=True)
        self.graph_path = self.derived / GRAPH_JSON
        self.db_path = self.derived / INDEX_DB
        self.metrics_mode = metrics_mode

    # ---- helpers
    def _head(self) -> str:
        r = subprocess.run(["git", "-C", str(self.canon.root), "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    @staticmethod
    def _file_hash(node) -> str:
        # hash the canonical edge/axis content (not mtime) so reprojection is content-driven
        payload = json.dumps(node.frontmatter(), sort_keys=True) + node.body
        return hashlib.sha256(payload.encode()).hexdigest()

    def _build_graph(self, nodes):
        # MultiDiGraph (not DiGraph): two canon edges can share (source, target) but differ in
        # relation (e.g. `grounds` and `attacked_by`). A DiGraph keys edges by (u, v) only and would
        # silently collapse them — dropping edges from graph.json and undercounting n_edges, violating
        # "derived contains nothing the canon does not". The `key=e.id` keeps each parallel edge.
        G = nx.MultiDiGraph()
        for n in nodes:
            G.add_node(n.id, label=n.label, node_type=n.node_type, file_type=n.file_type,
                       provenance=n.provenance.value, authored_by=n.authored_by.value,
                       epistemic_state=n.epistemic_state.value)
        for n in nodes:
            for e in n.edges:
                # derived contains nothing the canon doesn't; failure memory is kept, not pruned
                G.add_edge(e.source, e.target, key=e.id, id=e.id, relation=e.relation,
                           provenance=e.provenance.value, authored_by=e.authored_by.value,
                           epistemic_state=e.epistemic_state.value, span=e.span,
                           source_file=e.source_file, confidence=e.confidence.value,
                           confidence_score=e.confidence_score)
        return G

    # ---- ranks (off the hot path)
    def _ranks(self, G: nx.DiGraph):
        und = G.to_undirected()
        comm = _leiden(und)
        degree = dict(und.degree())
        bridges = {}
        for n in G.nodes():
            neigh_comms = {comm.get(nb) for nb in und.neighbors(n)}
            neigh_comms.discard(None)
            bridges[n] = len(neigh_comms)
        return comm, degree, bridges

    # ---- main
    def project(self, incremental: bool = True) -> ProjectReport:
        # Serialize the read+write critical section against canon writers AND other projectors: a
        # reprojection reads the whole canon then writes the derived layer, so without exclusion it
        # could persist a snapshot matching no single canon state, or two projectors could collide on
        # the SQLite write lock and crash a read (projector-1). Take the single-writer lease; if
        # another session holds it, skip and let the caller serve the existing derived layer (a later
        # read reprojects). Tests and the common single-session path never contend, so this is free.
        if not self.canon.try_acquire_lock():
            # another session is writing/projecting; serve the existing derived layer. But on a COLD
            # first read under contention there is no derived layer yet — create an empty schema'd
            # index + graph so the read tools return an empty graph instead of crashing on a missing
            # table (the next uncontended read reprojects for real). Schema creation is idempotent
            # (CREATE TABLE IF NOT EXISTS) and WAL-safe against a concurrent projector.
            if not self.db_path.exists():
                self._connect().close()
            if not self.graph_path.exists():
                _atomic_write(self.graph_path, json.dumps(_node_link_data(nx.MultiDiGraph())))
            return ProjectReport(up_to_date=self.db_path.exists() and self.graph_path.exists())
        try:
            return self._project_locked(incremental)
        finally:
            self.canon._release_lock()

    def _project_locked(self, incremental: bool) -> ProjectReport:
        nodes = self.canon.all_nodes()
        head = self._head()
        prior = self._read_meta() if self.db_path.exists() else {}
        prior_hashes = prior.get("file_hashes", {})
        cur_hashes = {n.id: self._file_hash(n) for n in nodes}

        do_full = (not incremental) or (not self.db_path.exists()) or (not prior_hashes) \
            or (not self.graph_path.exists())
        report = ProjectReport(full_rebuild=do_full, built_from_commit=head)

        if not do_full and prior.get("built_from_commit") == head and prior_hashes == cur_hashes:
            report.up_to_date = True
            report.n_nodes = len(nodes)
            report.n_edges = sum(len(n.edges) for n in nodes)
            return report

        G = self._build_graph(nodes)
        comm, degree, bridges = self._ranks(G)

        # graph.json is always written in full (cheap projection, must round-trip). Write atomically
        # (temp + os.replace) so a concurrent reader never observes a half-written file.
        data = _node_link_data(G)
        data.setdefault("graph", {})["built_from_commit"] = head
        _atomic_write(self.graph_path, json.dumps(data, indent=2))

        if do_full:
            self._write_full(nodes, comm, degree, bridges, head, cur_hashes, report)
        else:
            changed = [n for n in nodes if cur_hashes.get(n.id) != prior_hashes.get(n.id)]
            removed = [nid for nid in prior_hashes if nid not in cur_hashes]
            self._write_incremental(nodes, changed, removed, comm, degree, bridges, head, cur_hashes, report)

        report.n_nodes = G.number_of_nodes()
        report.n_edges = G.number_of_edges()
        report.communities = len(set(comm.values()))
        return report

    # ---- sqlite
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA busy_timeout=5000")  # wait, don't raise, if another writer holds the lock
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS nodes(
                id TEXT PRIMARY KEY, label TEXT, node_type TEXT, file_type TEXT,
                provenance TEXT, authored_by TEXT, epistemic_state TEXT,
                degree INTEGER, community INTEGER, bridge_communities INTEGER, structural_bridge INTEGER);
            CREATE TABLE IF NOT EXISTS edges(
                id TEXT PRIMARY KEY, source TEXT, target TEXT, relation TEXT,
                provenance TEXT, authored_by TEXT, epistemic_state TEXT, span TEXT,
                source_file TEXT, confidence TEXT, confidence_score REAL);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
            CREATE INDEX IF NOT EXISTS idx_nodes_degree ON nodes(degree);
            """
        )
        return con

    def _node_row(self, n, comm, degree, bridges):
        bc = bridges.get(n.id, 0)
        return (n.id, n.label, n.node_type, n.file_type, n.provenance.value, n.authored_by.value,
                n.epistemic_state.value, degree.get(n.id, 0), comm.get(n.id, -1), bc, 1 if bc >= 2 else 0)

    @staticmethod
    def _edge_row(e):
        return (e.id, e.source, e.target, e.relation, e.provenance.value, e.authored_by.value,
                e.epistemic_state.value, e.span, e.source_file, e.confidence.value, e.confidence_score)

    def _write_full(self, nodes, comm, degree, bridges, head, hashes, report):
        con = self._connect()
        try:
            con.execute("DELETE FROM nodes")
            con.execute("DELETE FROM edges")
            con.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [self._node_row(n, comm, degree, bridges) for n in nodes])
            erows = [self._edge_row(e) for n in nodes for e in n.edges]
            con.executemany("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)", erows)
            report.touched_nodes = [n.id for n in nodes]
            report.touched_edges = [e.id for n in nodes for e in n.edges]
            self._save_meta(con, head, hashes)
            con.commit()
        finally:
            con.close()

    def _write_incremental(self, nodes, changed, removed, comm, degree, bridges, head, hashes, report):
        con = self._connect()
        try:
            changed_ids = {c.id for c in changed}  # hoisted out of the per-node loop below
            # removed nodes: drop node + its outgoing edges
            for nid in removed:
                con.execute("DELETE FROM nodes WHERE id=?", (nid,))
                con.execute("DELETE FROM edges WHERE source=?", (nid,))
            for n in changed:
                con.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            self._node_row(n, comm, degree, bridges))
                report.touched_nodes.append(n.id)
                # diff this node's edges against the DB; upsert only changed rows, delete vanished
                cur = {r[0]: r for r in con.execute(
                    "SELECT id,source,target,relation,provenance,authored_by,epistemic_state,span,"
                    "source_file,confidence,confidence_score FROM edges WHERE source=?", (n.id,))}
                new = {e.id: self._edge_row(e) for e in n.edges}
                for eid, row in new.items():
                    if cur.get(eid) != row:
                        con.execute("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
                        report.touched_edges.append(eid)
                for eid in cur:
                    if eid not in new:
                        con.execute("DELETE FROM edges WHERE id=?", (eid,))
                        report.touched_edges.append(eid)
            # refresh ranks for unchanged nodes only when a rank value actually moved
            for n in nodes:
                if n.id in changed_ids:
                    continue
                row = self._node_row(n, comm, degree, bridges)
                old = con.execute("SELECT degree,community,bridge_communities FROM nodes WHERE id=?",
                                  (n.id,)).fetchone()
                if old != (row[7], row[8], row[9]):
                    con.execute("UPDATE nodes SET degree=?,community=?,bridge_communities=?,structural_bridge=? "
                                "WHERE id=?", (row[7], row[8], row[9], row[10], n.id))
            self._save_meta(con, head, hashes)
            con.commit()
        finally:
            con.close()

    def _cheap_sig(self) -> str:
        """A cheap signature of the canon dir — a digest over each note's (name, size, mtime) —
        computed with NO YAML parse and NO git fork. The fast staleness pre-gate (projector-2);
        per-node content hashing is the authoritative confirmation, run only when this signal moves.
        Digesting EVERY file's (size, mtime), not just the count + newest mtime, catches an in-place
        edit of a non-newest note (which would not move a max-mtime) and a same-mtime size change."""
        h = hashlib.sha256()
        for p in self.canon.note_paths():  # already sorted, so the digest is order-stable
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.name}\x00{st.st_size}\x00{st.st_mtime_ns}\x00".encode())
        return h.hexdigest()

    def _save_meta(self, con, head, hashes):
        con.execute("INSERT OR REPLACE INTO meta VALUES ('built_from_commit', ?)", (head,))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('file_hashes', ?)", (json.dumps(hashes),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('cheap_sig', ?)", (json.dumps(self._cheap_sig()),))

    def _read_meta(self) -> dict:
        try:
            con = sqlite3.connect(self.db_path)
        except sqlite3.Error:
            return {}
        try:
            rows = dict(con.execute("SELECT key,value FROM meta").fetchall())
        except sqlite3.Error:
            return {}
        finally:
            con.close()  # always close, even if the query raised (no leaked connection)
        out = {"built_from_commit": rows.get("built_from_commit", "")}
        try:
            out["file_hashes"] = json.loads(rows.get("file_hashes", "{}"))
        except ValueError:
            out["file_hashes"] = {}
        try:
            out["cheap_sig"] = json.loads(rows.get("cheap_sig", "null"))
        except ValueError:
            out["cheap_sig"] = None
        return out

    def is_stale(self) -> bool:
        if not self.db_path.exists() or not self.graph_path.exists():
            return True
        prior = self._read_meta()
        # cheap pre-gate (projector-2): if the canon dir's (count, newest mtime) is unchanged since the
        # last projection, nothing on disk changed -> not stale, WITHOUT a git fork or a full YAML
        # parse. This fronts EVERY read, so it must stay O(dir-listing), not O(N parse).
        if prior.get("cheap_sig") is not None and prior["cheap_sig"] == self._cheap_sig():
            return False
        # the cheap signal moved -> authoritative per-node content-hash comparison. This catches any
        # uncommitted canon change (a kg_ground verdict, a hand edit) regardless of HEAD; content
        # equality means the derived layer matches the canon whatever the commit is.
        cur_hashes = {n.id: self._file_hash(n) for n in self.canon.all_nodes()}
        return prior.get("file_hashes", {}) != cur_hashes

    # ---- query surface (read precomputed ranks O(1); NO centrality in-request)
    def _ro(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA busy_timeout=5000")  # tolerate a concurrent reprojection mid-read
        con.row_factory = sqlite3.Row
        return con

    def owner_of_edge(self, edge_id: str) -> str | None:
        """Source node id for an edge, via the indexed edges table (O(1) lookup); None if absent.
        Lets kg_ground resolve an edge's owner without an O(N) full-canon scan per call (server-2)."""
        con = self._ro()
        try:
            r = con.execute("SELECT source FROM edges WHERE id=?", (edge_id,)).fetchone()
            return r[0] if r else None
        finally:
            con.close()

    def get_node(self, node_id: str) -> dict | None:
        con = self._ro()
        try:
            r = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not r:
                return None
            out = dict(r)
            out["edges"] = [dict(e) for e in con.execute(
                "SELECT * FROM edges WHERE source=? OR target=?", (node_id, node_id))]
            return out
        finally:
            con.close()

    def get_neighbors(self, node_id: str, *, relation: str | None = None) -> list[dict]:
        con = self._ro()
        try:
            q = "SELECT * FROM edges WHERE (source=? OR target=?)"
            args = [node_id, node_id]
            if relation:
                q += " AND relation=?"
                args.append(relation)
            return [dict(e) for e in con.execute(q, args)]
        finally:
            con.close()

    def query_graph(self, *, node_type: str | None = None, relation: str | None = None,
                    epistemic_state: str | None = None, limit: int = 50) -> dict:
        limit = max(0, min(int(limit), 10_000))  # a negative LIMIT is unbounded in SQLite; clamp it
        con = self._ro()
        try:
            nq, na = "SELECT * FROM nodes", []
            conds = []
            if node_type:
                conds.append("node_type=?"); na.append(node_type)
            if epistemic_state:
                conds.append("epistemic_state=?"); na.append(epistemic_state)
            if conds:
                nq += " WHERE " + " AND ".join(conds)
            nq += " ORDER BY degree DESC LIMIT ?"; na.append(limit)
            nodes = [dict(r) for r in con.execute(nq, na)]
            eq, ea = "SELECT * FROM edges", []
            if relation:
                eq += " WHERE relation=?"; ea.append(relation)
            eq += " LIMIT ?"; ea.append(limit)
            edges = [dict(r) for r in con.execute(eq, ea)]
            return {"nodes": nodes, "edges": edges}
        finally:
            con.close()

    def shortest_path(self, source: str, target: str) -> list[str] | None:
        # path search over the derived edge list; still no centrality computation
        con = self._ro()
        try:
            adj: dict[str, list[str]] = {}
            for s, t in con.execute("SELECT source,target FROM edges"):
                adj.setdefault(s, []).append(t)
                adj.setdefault(t, []).append(s)
        finally:
            con.close()
        if source == target:
            return [source]
        from collections import deque
        q, seen = deque([[source]]), {source}
        while q:
            path = q.popleft()
            for nb in adj.get(path[-1], []):
                if nb in seen:
                    continue
                if nb == target:
                    return path + [nb]
                seen.add(nb)
                q.append(path + [nb])
        return None

    def kg_context(self, query: str | None = None, *, budget: int = 2000) -> dict:
        """Grounding-aware, provenance-carrying, token-budgeted context. Reads precomputed columns
        only — no centrality is computed here (it is O(1) on the index)."""
        budget = max(0, min(int(budget), MAX_CONTEXT_TOKENS))  # enforce an upper ceiling (server-4)
        con = self._ro()
        try:
            # falsification counters (memory of failures, §1.7) — surfaced, never pruned
            fail_states = [s.value for s in FAILURE_STATES]
            qmarks = ",".join("?" * len(fail_states))
            counters = {
                "failed_or_rejected_edges": con.execute(
                    f"SELECT COUNT(*) FROM edges WHERE epistemic_state IN ({qmarks})", fail_states).fetchone()[0],
            }
            # priority fill: grounded -> span-present -> inferred
            order = ("epistemic_state='grounded' DESC, "
                     "CASE provenance WHEN 'span-present' THEN 0 WHEN 'inferred' THEN 1 ELSE 2 END, "
                     "confidence_score DESC")
            eq = ("SELECT id,source,target,relation,provenance,authored_by,epistemic_state,span,"
                  "confidence,confidence_score FROM edges")
            ea: list = []
            if query:
                # term-wise OR match: a natural-language question matches edges that contain ANY of its
                # terms in any field. A single LIKE on the whole string would only match a verbatim
                # substring of the question, so multi-word queries always missed.
                import re as _re
                seen: set = set()
                terms = [t for t in _re.findall(r"[A-Za-z0-9_-]{3,}", query.lower())
                         if not (t in seen or seen.add(t))]
                _clause = ("(source LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' "
                           "OR relation LIKE ? ESCAPE '\\' OR span LIKE ? ESCAPE '\\')")
                if terms:
                    clauses = []
                    for t in terms:
                        clauses.append(_clause)
                        ea += [f"%{_like_escape(t)}%"] * 4
                    eq += " WHERE (" + " OR ".join(clauses) + ")"
                else:
                    eq += " WHERE " + _clause
                    ea = [f"%{_like_escape(query)}%"] * 4
            eq += f" ORDER BY {order}"
            items, used = [], 0
            for r in con.execute(eq, ea):
                rec = dict(r)
                tok = max(1, len(json.dumps(rec)) // 4)
                if used + tok > budget:
                    break
                used += tok
                items.append(rec)
            bridges = [dict(r) for r in con.execute(
                "SELECT id,label,degree,bridge_communities FROM nodes WHERE structural_bridge=1 "
                "ORDER BY degree DESC LIMIT 10")]
            return {
                "items": items,
                "approx_tokens": used,
                "budget": budget,
                "falsification_counters": counters,
                "advisory": {"signal": "structural-bridge", "note": "advisory heuristic, not a guarantee",
                             "nodes": bridges},
            }
        finally:
            con.close()
