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
from typing import TYPE_CHECKING, Callable

import networkx as nx

from .canon import Canon, _atomic_write
from .harness import _node_specificity, idf_seeds
from .harness import specificity as _specificity_gate
from .model import EpistemicState, FAILURE_STATES, Provenance

if TYPE_CHECKING:  # type-only; the projector duck-types .verifies/.concat at runtime
    from .sources import SourceSet

GRAPH_JSON = "graph.json"
INDEX_DB = "index.sqlite"
# The full set of `nodes` columns the current schema declares. The four generative-layer columns
# (betweenness/spec_betweenness/specificity/gate_on, PLAN Stage 2) were added after the original 11;
# an index.sqlite built before them lacks the columns, so a projection that finds them missing forces
# a full rebuild (CREATE TABLE IF NOT EXISTS cannot add a column to an existing table).
_NEW_NODE_COLUMNS = {"betweenness", "spec_betweenness", "specificity", "gate_on"}
# Hard ceiling on the kg_context token budget so a client passing a huge value can't make the engine
# serialize the entire edge table into one response (server-4). The limit clamp on query_graph is the
# row-count analogue.
MAX_CONTEXT_TOKENS = 100_000

# R6 (kg_agenda) detector thresholds. A node with >= _HUB_DEGREE live edges is a "hub"; its
# grounded/(grounded+unverified) ratio splits well-grounded (answerable) from under-grounded (blocked).
_HUB_DEGREE = 3
_GROUNDED_RATIO = 0.5


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


@dataclass
class Ranks:
    """All precomputed per-node signals from one projection (PLAN Stage 2). Computed OFF the hot path
    (`_ranks`); read O(1) by the query surface. `betweenness` and `spec_betweenness` complete the
    partially-implemented bridge metric; `gate_on` (one value per projection) records whether the
    specificity-weighting earned promotion this projection (`harness.specificity`)."""
    community: dict = field(default_factory=dict)
    degree: dict = field(default_factory=dict)
    bridges: dict = field(default_factory=dict)
    betweenness: dict = field(default_factory=dict)
    spec_betweenness: dict = field(default_factory=dict)
    specificity: dict = field(default_factory=dict)
    gate_on: int = 0


# --------------------------------------------------------------------------- projector


class Projector:
    def __init__(self, canon: Canon, derived_dir: str | Path | None = None, *,
                 metrics_mode: str = "structure_only", source_text: "str | Callable[[], str] | None" = None,
                 source_set: "Callable[[], SourceSet] | None" = None):
        self.canon = canon
        self.derived = Path(derived_dir) if derived_dir else (canon.root / "derived")
        self.derived.mkdir(parents=True, exist_ok=True)
        self.graph_path = self.derived / GRAPH_JSON
        self.db_path = self.derived / INDEX_DB
        self.metrics_mode = metrics_mode
        # The source text feeds the IDF specificity weighting (PLAN Stage 2). Accept a str OR a
        # zero-arg callable (KGEngine passes its bound `source_text`, read lazily once per real
        # reprojection — off the hot path). Absent -> an empty corpus, so specificity is uniform and the
        # bridge-metric gate stays closed (spec_betweenness degrades to raw betweenness).
        self._source_text = source_text
        # The resolved SourceSet (R4), as a zero-arg callable, for the R3 source-staleness advisory: it
        # re-verifies each grounded/failed span-present edge against its OWN source_file (per-file, never
        # a global concat). Absent -> no staleness is ever flagged (can't diverge from a missing source).
        self._source_set = source_set

    def _src_text(self) -> str:
        src = self._source_text() if callable(self._source_text) else self._source_text
        return src or ""

    def _corpus(self) -> list[str]:
        """The source split into sections (on `\\n## `) for IDF — the corpus `harness.idf_seeds`
        consumes. Empty when no source is configured."""
        src = self._src_text()
        if not src:
            return []
        return [s for s in src.split("\n## ") if s.strip()]

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
    def _ranks(self, G: nx.DiGraph) -> Ranks:
        # The advisory ranks (degree/communities/betweenness/spec_betweenness) are computed over the
        # NON-FAILED subgraph (§1.7). graph.json and the edges table stay COMPLETE — failure memory is
        # never pruned — but a `failed`/`rejected` edge must not inflate centrality: the adversarial
        # grounder stamps its attacked_by/confounded_by counter-edges `failed`, so counting them would
        # make "more refutation -> higher apparent centrality". Excluding only the edges keeps every
        # node present (an attacked hub whose edges are all refuted still ranks honestly at degree 0).
        _fail = {s.value for s in FAILURE_STATES}
        live = nx.MultiDiGraph()
        live.add_nodes_from(G.nodes(data=True))
        live.add_edges_from((u, v, k, d) for u, v, k, d in G.edges(keys=True, data=True)
                            if d.get("epistemic_state") not in _fail)
        und = live.to_undirected()
        comm = _leiden(und)
        degree = dict(und.degree())
        bridges = {}
        for n in G.nodes():
            neigh_comms = {comm.get(nb) for nb in und.neighbors(n)}
            neigh_comms.discard(None)
            bridges[n] = len(neigh_comms)

        # complete the bridge metric (PLAN Stage 2 / §2/§4), all OFF the hot path:
        #  - raw betweenness: the natural bridge metric, but confounded by generality (a vague node sits
        #    on many shortest paths for empty reasons).
        #  - specificity: IDF rarity of a node's label terms over the source corpus (the confound control).
        #  - spec_betweenness = betweenness * specificity: down-weights vague high-traffic hubs.
        betweenness = nx.betweenness_centrality(und) if und.number_of_nodes() > 2 else {n: 0.0 for n in und}
        corpus = self._corpus()
        seeds = idf_seeds(corpus) if corpus else {}
        default = (sum(seeds.values()) / len(seeds)) if seeds else 1.0
        specificity = {n: _node_specificity(G.nodes[n].get("label") or n, seeds, default) for n in G.nodes()}
        spec_betweenness = {n: betweenness.get(n, 0.0) * specificity.get(n, default) for n in G.nodes()}

        # the gate (one value per projection): does specificity-weighting separate real bridges from
        # vague high-traffic nodes beyond a churn band? Computed once via the harness (it measures the
        # confound + rank churn). gate_on decides only whether spec_betweenness is TRUSTED for ranking —
        # both raw and weighted values are always stored, so nothing is hidden.
        gate_on = 0
        try:
            verdict = _specificity_gate(_node_link_data(G), corpus)
            gate_on = 1 if verdict.get("gate_on") else 0
        except Exception:  # noqa: BLE001 — a gate-computation hiccup must never break projection
            gate_on = 0
        return Ranks(comm, degree, bridges, betweenness, spec_betweenness, specificity, gate_on)

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
        # R3: the stale-verdict advisory is keyed on a hash of the SOURCE payload (SourceSet concat),
        # NOT the per-node canon hash (which never sees a source edit). Computed here, off the hot path —
        # is_stale (the per-read gate) is deliberately left source-blind (Q3 one-projection-lag).
        cur_source_hash = self._source_hash()

        do_full = (not incremental) or (not self.db_path.exists()) or (not prior_hashes) \
            or (not self.graph_path.exists()) or self._schema_outdated()
        report = ProjectReport(full_rebuild=do_full, built_from_commit=head)

        # Up-to-date requires the canon AND the source unchanged: a source edit alone (canon byte-
        # identical) must still fall through so the stale-verdict advisory refreshes — otherwise the flag
        # could never appear once a projection is actually invoked.
        if not do_full and prior.get("built_from_commit") == head and prior_hashes == cur_hashes \
                and prior.get("source_hash", "") == cur_source_hash:
            report.up_to_date = True
            report.n_nodes = len(nodes)
            report.n_edges = sum(len(n.edges) for n in nodes)
            return report

        # R3: stale-verdict advisory (READ-ONLY). On a full rebuild OR a source change, re-scan ALL
        # grounded/failed span-present edges. Otherwise (canon-only change, source unchanged) re-check the
        # already-flagged edges (so a re-grounded/deleted one CLEARS) AND scan the edges on THIS
        # projection's `changed` notes — so a divergence introduced via the CANON (a hand-edited span on
        # an already-grounded edge) is caught too, without a full O(N) source scan.
        sources = self._source_set() if self._source_set else None
        changed = [] if do_full else [n for n in nodes if cur_hashes.get(n.id) != prior_hashes.get(n.id)]
        removed = [] if do_full else [nid for nid in prior_hashes if nid not in cur_hashes]
        if do_full or cur_source_hash != prior.get("source_hash", ""):
            stale = self._stale_verdicts(nodes, sources)
        else:
            refiltered = self._refilter_stale(prior.get("stale_verdicts") or [], nodes, sources)
            seen_ids = {s["edge_id"] for s in refiltered}
            stale = refiltered + [s for s in self._stale_verdicts(changed, sources)
                                  if s["edge_id"] not in seen_ids]

        G = self._build_graph(nodes)
        ranks = self._ranks(G)

        # graph.json is always written in full (cheap projection, must round-trip). Write atomically
        # (temp + os.replace) so a concurrent reader never observes a half-written file.
        data = _node_link_data(G)
        data.setdefault("graph", {})["built_from_commit"] = head
        _atomic_write(self.graph_path, json.dumps(data, indent=2))

        if do_full:
            self._write_full(nodes, ranks, head, cur_hashes, report, cur_source_hash, stale)
        else:
            self._write_incremental(nodes, changed, removed, ranks, head, cur_hashes, report,
                                    cur_source_hash, stale)

        report.n_nodes = G.number_of_nodes()
        report.n_edges = G.number_of_edges()
        report.communities = len(set(ranks.community.values()))
        return report

    # ---- sqlite
    _NODES_DDL = (
        "CREATE TABLE IF NOT EXISTS nodes("
        "id TEXT PRIMARY KEY, label TEXT, node_type TEXT, file_type TEXT, "
        "provenance TEXT, authored_by TEXT, epistemic_state TEXT, "
        "degree INTEGER, community INTEGER, bridge_communities INTEGER, structural_bridge INTEGER, "
        "betweenness REAL, spec_betweenness REAL, specificity REAL, gate_on INTEGER)")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA busy_timeout=5000")  # wait, don't raise, if another writer holds the lock
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            {self._NODES_DDL};
            -- verdict_by/verdict_at are intentionally NOT columns here: verdict attribution lives
            -- authoritatively in the canon frontmatter + audit log (reconciler reads them from there).
            -- "derived contains nothing the canon does not" is one-directional — the derived layer MAY
            -- omit canon fields, so this is contractually allowed, not a gap.
            CREATE TABLE IF NOT EXISTS edges(
                id TEXT PRIMARY KEY, source TEXT, target TEXT, relation TEXT,
                provenance TEXT, authored_by TEXT, epistemic_state TEXT, span TEXT,
                source_file TEXT, confidence TEXT, confidence_score REAL);
            CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
            CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
            CREATE INDEX IF NOT EXISTS idx_nodes_degree ON nodes(degree);
            """
        )
        # CREATE TABLE IF NOT EXISTS cannot add the Stage-2 columns to a pre-existing 11-column `nodes`
        # table. If they are missing, drop and recreate it empty (a full reprojection — forced by
        # _schema_outdated — repopulates it). Done here so every connect path heals the schema.
        cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
        if not _NEW_NODE_COLUMNS <= cols:
            con.executescript(f"DROP TABLE IF EXISTS nodes; {self._NODES_DDL};"
                              "CREATE INDEX IF NOT EXISTS idx_nodes_degree ON nodes(degree);")
        return con

    def _schema_outdated(self) -> bool:
        """True if an index.sqlite exists but its `nodes` table predates the Stage-2 columns — forces a
        full rebuild so the betweenness/specificity/gate columns get populated for every node, not just
        the ones an incremental pass happens to touch."""
        if not self.db_path.exists():
            return False  # no db -> do_full is already True via the exists() check
        try:
            con = sqlite3.connect(self.db_path)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
            finally:
                con.close()
        except sqlite3.Error:
            return True
        return not _NEW_NODE_COLUMNS <= cols

    def _node_row(self, n, ranks: Ranks):
        bc = ranks.bridges.get(n.id, 0)
        return (n.id, n.label, n.node_type, n.file_type, n.provenance.value, n.authored_by.value,
                n.epistemic_state.value, ranks.degree.get(n.id, 0), ranks.community.get(n.id, -1), bc,
                1 if bc >= 2 else 0,
                float(ranks.betweenness.get(n.id, 0.0)), float(ranks.spec_betweenness.get(n.id, 0.0)),
                float(ranks.specificity.get(n.id, 1.0)), int(ranks.gate_on))

    @staticmethod
    def _edge_row(e):
        return (e.id, e.source, e.target, e.relation, e.provenance.value, e.authored_by.value,
                e.epistemic_state.value, e.span, e.source_file, e.confidence.value, e.confidence_score)

    def _write_full(self, nodes, ranks: Ranks, head, hashes, report, source_hash="", stale=None):
        con = self._connect()
        try:
            con.execute("DELETE FROM nodes")
            con.execute("DELETE FROM edges")
            con.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._node_row(n, ranks) for n in nodes])
            erows = [self._edge_row(e) for n in nodes for e in n.edges]
            con.executemany("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?)", erows)
            report.touched_nodes = [n.id for n in nodes]
            report.touched_edges = [e.id for n in nodes for e in n.edges]
            self._save_meta(con, head, hashes, ranks.gate_on, source_hash, stale)
            con.commit()
        finally:
            con.close()

    def _write_incremental(self, nodes, changed, removed, ranks: Ranks, head, hashes, report,
                           source_hash="", stale=None):
        con = self._connect()
        try:
            changed_ids = {c.id for c in changed}  # hoisted out of the per-node loop below
            # removed nodes: drop node + its outgoing edges
            for nid in removed:
                con.execute("DELETE FROM nodes WHERE id=?", (nid,))
                con.execute("DELETE FROM edges WHERE source=?", (nid,))
            for n in changed:
                con.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            self._node_row(n, ranks))
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
            # refresh ranks for unchanged nodes only when a rank value actually moved. Betweenness/
            # spec_betweenness/specificity are GLOBAL — one new edge shifts them for distant nodes — so
            # they are diffed and refreshed here too, not just degree/community/bridge.
            for n in nodes:
                if n.id in changed_ids:
                    continue
                row = self._node_row(n, ranks)
                old = con.execute("SELECT degree,community,bridge_communities,betweenness,"
                                  "spec_betweenness,specificity,gate_on FROM nodes WHERE id=?",
                                  (n.id,)).fetchone()
                new_vals = (row[7], row[8], row[9], row[11], row[12], row[13], row[14])
                if old != new_vals:
                    con.execute("UPDATE nodes SET degree=?,community=?,bridge_communities=?,"
                                "structural_bridge=?,betweenness=?,spec_betweenness=?,specificity=?,"
                                "gate_on=? WHERE id=?",
                                (row[7], row[8], row[9], row[10], row[11], row[12], row[13], row[14], n.id))
            self._save_meta(con, head, hashes, ranks.gate_on, source_hash, stale)
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

    def _source_hash(self) -> str:
        """sha256 of the source payload (the SourceSet concat); '' when no source. R3's stale-verdict
        recompute pre-gate — moves on any add/remove/edit of any source file. Computed only inside a
        projection (off the hot path); is_stale is left source-blind (Q3 one-projection-lag)."""
        sources = self._source_set() if self._source_set else None
        payload = sources.concat if sources is not None else self._src_text()
        return hashlib.sha256(payload.encode()).hexdigest() if payload else ""

    def _stale_verdicts(self, nodes, sources) -> list[dict]:
        """R3 — the source-staleness advisory (READ-ONLY). A grounded/failed span-present edge's stored
        span was verified at verdict time; if the source is later edited so it no longer appears, re-flag
        it as `span-no-longer-in-source`. Source-aware: each edge is checked against its OWN `source_file`
        (lenient any-source fallback), never a global concat — so a multi-file vault never false-flags an
        edge whose span lives in a non-default file. It NEVER mutates a verdict (re-grounding stays a
        kg_ground decision). Empty when no source is configured (no divergence without a source)."""
        if not sources:
            return []
        out = []
        for n in nodes:
            for e in n.edges:
                if (e.epistemic_state in (EpistemicState.GROUNDED, EpistemicState.FAILED)
                        and e.provenance == Provenance.SPAN_PRESENT
                        and not sources.verifies(e.span, source_file=e.source_file)):
                    out.append({"edge_id": e.id, "reason": "span-no-longer-in-source"})
        return out

    def _refilter_stale(self, prior, nodes, sources) -> list[dict]:
        """Re-verify ONLY the already-flagged edges against the current canon+source (the full re-scan is
        gated on do_full/source-moved). Drops a prior flag whose edge was deleted, re-grounded out of the
        grounded/failed set, or whose span verifies again — so a re-grounding clears its flag on the next
        projection even with an unchanged source. New staleness can only come from a source change (which
        moves the hash → full recompute), so this loses nothing."""
        if not prior or not sources:
            return []
        edges = {e.id: e for n in nodes for e in n.edges}
        out = []
        for entry in prior:
            e = edges.get(entry.get("edge_id"))
            if (e is not None
                    and e.epistemic_state in (EpistemicState.GROUNDED, EpistemicState.FAILED)
                    and e.provenance == Provenance.SPAN_PRESENT
                    and not sources.verifies(e.span, source_file=e.source_file)):
                out.append({"edge_id": e.id, "reason": "span-no-longer-in-source"})
        return out

    def _save_meta(self, con, head, hashes, gate_on=0, source_hash="", stale_verdicts=None):
        con.execute("INSERT OR REPLACE INTO meta VALUES ('built_from_commit', ?)", (head,))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('file_hashes', ?)", (json.dumps(hashes),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('cheap_sig', ?)", (json.dumps(self._cheap_sig()),))
        # the bridge-metric gate verdict for this projection (PLAN Stage 2): one value, read by
        # kg_context to decide whether spec_betweenness is the TRUSTED ranking signal this projection.
        con.execute("INSERT OR REPLACE INTO meta VALUES ('gate_on', ?)", (str(int(gate_on)),))
        # R3 source-staleness advisory: the source-payload hash (recompute pre-gate) + the flagged ids.
        con.execute("INSERT OR REPLACE INTO meta VALUES ('source_hash', ?)", (source_hash or "",))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('stale_verdicts', ?)",
                    (json.dumps(stale_verdicts or []),))

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
        out["source_hash"] = rows.get("source_hash", "")  # R3 stale-verdict recompute pre-gate
        try:
            out["stale_verdicts"] = json.loads(rows.get("stale_verdicts", "[]"))
        except ValueError:
            out["stale_verdicts"] = []
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

    def load_graph(self) -> nx.MultiDiGraph:
        """Build an in-memory MultiDiGraph from the derived index, with every precomputed rank column
        attached as a node attribute (PLAN Stage 3 — the generative layer reads ranks O(1) off this).
        Read-only; assumes the caller has already projected. A dangling edge target (a node referenced
        but not itself a canon note) is auto-created attribute-less, so generators must `.get()` ranks."""
        con = self._ro()
        try:
            G = nx.MultiDiGraph()
            for r in con.execute("SELECT * FROM nodes"):
                d = dict(r)
                G.add_node(d.pop("id"), **d)
            for r in con.execute("SELECT * FROM edges"):
                d = dict(r)
                G.add_edge(d["source"], d["target"], key=d["id"], **d)
            return G
        finally:
            con.close()

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
            cols = ("id,source,target,relation,provenance,authored_by,epistemic_state,span,"
                    "confidence,confidence_score")
            # term-wise OR match: a natural-language question matches edges that contain ANY of its terms
            # in any field. A single LIKE on the whole string would only match a verbatim substring of
            # the question, so multi-word queries always missed. Built ONCE and reused for both lanes.
            term_clause, term_args = "", []
            if query:
                import re as _re
                seen: set = set()
                terms = [t for t in _re.findall(r"[A-Za-z0-9_-]{3,}", query.lower())
                         if not (t in seen or seen.add(t))]
                _clause = ("(source LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' "
                           "OR relation LIKE ? ESCAPE '\\' OR span LIKE ? ESCAPE '\\')")
                if terms:
                    parts = []
                    for t in terms:
                        parts.append(_clause)
                        term_args += [f"%{_like_escape(t)}%"] * 4
                    term_clause = "(" + " OR ".join(parts) + ")"
                else:
                    term_clause = _clause
                    term_args = [f"%{_like_escape(query)}%"] * 4

            def _fill(where_sql, args, order_sql, cap):
                rows, used = [], 0
                for r in con.execute(f"SELECT {cols} FROM edges WHERE {where_sql} ORDER BY {order_sql}", args):
                    rec = dict(r)
                    tok = max(1, len(json.dumps(rec)) // 4)
                    if used + tok > cap:
                        break
                    used += tok
                    rows.append(rec)
                return rows, used

            # GROUNDED LANE — items[] never includes a hypothesized proposal (PLAN Stage 8). A
            # hypothesized edge is a machine proposal, not grounded content, and must never be laundered
            # into a grounded answer.
            iwhere = "provenance != 'hypothesized'"
            iargs = list(term_args)
            if term_clause:
                iwhere += " AND " + term_clause
            items, used = _fill(iwhere, iargs, order, budget)
            # HYPOTHESIS LANE — a SEPARATE block of hypothesized, unverified proposals, clearly distinct.
            # Both lanes share ONE running budget (§1.11): the hypotheses cap is what the items lane left
            # unspent (budget - used), so the total serialized payload never exceeds `budget` and the
            # reported approx_tokens (used + hused) is honest. Filling items first preserves grounded
            # priority; the items/hypotheses segregation in the output is unchanged.
            hwhere = "provenance = 'hypothesized' AND epistemic_state = 'unverified'"
            hargs = list(term_args)
            if term_clause:
                hwhere += " AND " + term_clause
            hypotheses, hused = _fill(hwhere, hargs, "confidence_score DESC", budget - used)
            bridges = [dict(r) for r in con.execute(
                "SELECT id,label,degree,bridge_communities FROM nodes WHERE structural_bridge=1 "
                "ORDER BY degree DESC LIMIT 10")]
            # the completed bridge metric (PLAN Stage 2): when the gate is ON, the trusted ranking signal
            # is spec_betweenness (the confound-corrected bridge metric); when OFF, fall back to the
            # honest structural-bridge / degree advisory. Both raw and weighted values are always carried
            # so a reader can see the correction. Read precomputed columns only — no centrality here.
            grow = con.execute("SELECT value FROM meta WHERE key='gate_on'").fetchone()
            gate_on = int(grow[0]) if grow and grow[0] is not None and str(grow[0]).isdigit() else 0
            # R3 source-staleness advisory: grounded/failed span-present edges whose span no longer
            # appears in the source (read-only; the verdict itself is untouched until /kg-ground re-runs).
            srow = con.execute("SELECT value FROM meta WHERE key='stale_verdicts'").fetchone()
            try:
                stale_verdicts = json.loads(srow[0]) if srow and srow[0] else []
            except (ValueError, TypeError):
                stale_verdicts = []
            if gate_on:
                bm_sql = ("SELECT id,label,degree,betweenness,spec_betweenness,specificity FROM nodes "
                          "ORDER BY spec_betweenness DESC, degree DESC LIMIT 10")
                ranked_by = "spec_betweenness"
            else:
                bm_sql = ("SELECT id,label,degree,betweenness,spec_betweenness,specificity FROM nodes "
                          "ORDER BY structural_bridge DESC, betweenness DESC, degree DESC LIMIT 10")
                ranked_by = "structural_bridge"
            bridge_metric = {
                "gate_on": gate_on,
                "ranked_by": ranked_by,
                "note": ("specificity-weighting earned promotion this projection — spec_betweenness is "
                         "the trusted bridge signal" if gate_on else
                         "gated: spec_betweenness stays advisory; ranking by structural-bridge/degree (§1.6)"),
                "nodes": [dict(r) for r in con.execute(bm_sql)],
            }
            return {
                "items": items,
                "hypotheses": hypotheses,   # the SEPARATE hypothesized lane — proposals, NOT grounded content
                "approx_tokens": used + hused,  # both lanes counted against the shared budget (§1.11)
                "budget": budget,
                "falsification_counters": counters,
                "advisory": {"signal": "structural-bridge", "note": "advisory heuristic, not a guarantee",
                             "nodes": bridges, "bridge_metric": bridge_metric,
                             "stale_verdicts": stale_verdicts},
            }
        finally:
            con.close()

    # ---- R6: read-only structural agenda (and the shared reader seam R1 reuses)
    def _agenda_reader(self) -> "tuple[list[dict], list[dict]]":
        """Read ALL node + edge rows from the derived index into plain dicts, then close. READ-ONLY by
        construction: the connection is opened `PRAGMA query_only=ON`, so a consumer physically cannot
        write through it. This is the shared seam both R6 (`kg_agenda`) and R1 (the exporter) consume —
        `projector.py` stays the SOLE writer of the derived layer (graph.json/index.sqlite)."""
        con = self._ro()
        try:
            con.execute("PRAGMA query_only=ON")
            nodes = [dict(r) for r in con.execute("SELECT * FROM nodes")]
            edges = [dict(r) for r in con.execute("SELECT * FROM edges")]
            return nodes, edges
        finally:
            con.close()

    def kg_agenda(self, *, limit: int = 5) -> dict:
        """Read-only structural "suggested questions" (R6). Reads ONLY precomputed derived columns and
        returns ~`limit` structural gaps split into `answerable_now[]` (well-grounded neighbourhoods)
        vs `blocked_on_grounding[]` (orphans, hypothesized-only neighbourhoods, under-grounded hubs,
        disconnected clusters) — mirroring kg_context's items[]/hypotheses[]. Ranked by the existing
        honest signal (gate-aware, mirroring kg_context's switch; never raw betweenness as lead). It
        asserts no edges, copies no spans, stamps no verdicts — measure-never-gate (it suggests, never
        acts); the question text is session-time only and never touches the canon."""
        return _agenda_from_rows(*self._agenda_reader(), limit=limit)


# --------------------------------------------------------------------------- R6 agenda builder (pure)


def _agenda_signals(n: dict) -> dict:
    """The honest signals carried on each suggestion so the ranking is transparent — degree (the
    advisory), the structural-bridge / betweenness / specificity columns, never a minted scalar."""
    return {
        "degree": n.get("degree") or 0,
        "community": n.get("community"),
        "structural_bridge": n.get("structural_bridge") or 0,
        "betweenness": n.get("betweenness") or 0.0,
        "spec_betweenness": n.get("spec_betweenness") or 0.0,
        "specificity": n.get("specificity"),
    }


def _neighbor_labels(nid: str, live_edges: list, by_id: dict, *, cap: int = 4) -> list:
    names, seen = [], set()
    for e in live_edges:
        other = e.get("target") if e.get("source") == nid else e.get("source")
        if other == nid or other in seen:
            continue
        seen.add(other)
        names.append((by_id.get(other) or {}).get("label") or other)
        if len(names) >= cap:
            break
    return names


def _agenda_from_rows(nodes: list, edges: list, *, limit: int = 5) -> dict:
    """Pure R6 agenda builder over precomputed derived rows (no DB, no canon — testable in isolation).

    Detectors (each node matches at most one): orphan (degree 0), hypothesized-only (every live edge a
    proposal), well-grounded hub (answerable), under-grounded hub (blocked), plus edgeless-communities
    (a disconnected cluster of >=2 nodes). Ranked by the gate-aware honest signal — `spec_betweenness`
    ONLY when `gate_on=1`, else `structural_bridge`/betweenness/degree (mirroring kg_context's switch;
    never raw betweenness as lead). Split into the two lanes, each capped at `limit`. Read-only — it
    only inspects rows and returns text."""
    limit = max(1, min(int(limit), 50))
    gate_on = int(next((n.get("gate_on") for n in nodes if n.get("gate_on") is not None), 0) or 0)
    ranked_by = "spec_betweenness" if gate_on else "structural_bridge"
    by_id = {n["id"]: n for n in nodes}

    incident: dict = {n["id"]: [] for n in nodes}
    for e in edges:
        for endp in (e.get("source"), e.get("target")):
            if endp in incident:
                incident[endp].append(e)

    def rank_key(n: dict):  # mirror kg_context's gate switch; never raw betweenness as lead
        d = n.get("degree") or 0
        if gate_on:
            return (float(n.get("spec_betweenness") or 0.0), d)
        return (int(n.get("structural_bridge") or 0), float(n.get("betweenness") or 0.0), d)

    gaps: list = []  # (rank_key, item)
    emitted: set = set()  # node ids already surfaced by a node-level detector (one detector per node)

    for n in nodes:
        nid = n["id"]
        label = n.get("label") or nid
        deg = n.get("degree") or 0
        live = [e for e in incident[nid] if e.get("epistemic_state") not in ("failed", "rejected")]
        grounded = sum(1 for e in live if e.get("epistemic_state") == "grounded")
        unverified = sum(1 for e in live if e.get("epistemic_state") == "unverified")
        decided = grounded + unverified

        if deg == 0:
            item = {"detector": "orphan", "lane": "blocked_on_grounding", "focus": [nid],
                    "question": f"'{label}' is isolated — it has no live relations. What should connect "
                                f"to it, and can that be grounded?"}
        elif live and all(e.get("provenance") == "hypothesized" for e in live):
            item = {"detector": "hypothesized-only", "lane": "blocked_on_grounding", "focus": [nid],
                    "question": f"Every relation on '{label}' is a hypothesis — its role is unverified. "
                                f"Ground them (/kg-ground) before treating it as established."}
        elif deg >= _HUB_DEGREE and decided and grounded / decided >= _GROUNDED_RATIO:
            nbrs = _neighbor_labels(nid, live, by_id)
            item = {"detector": "well-grounded", "lane": "answerable_now", "focus": [nid],
                    "question": f"'{label}' is a well-grounded hub (degree {deg}, {grounded} grounded) — "
                                f"how do its neighbours ({', '.join(nbrs)}) interrelate?"}
        elif deg >= _HUB_DEGREE and decided and grounded / decided < _GROUNDED_RATIO:
            item = {"detector": "under-grounded-hub", "lane": "blocked_on_grounding", "focus": [nid],
                    "question": f"Hub '{label}' (degree {deg}) is under-grounded — only {grounded}/{decided} "
                                f"of its edges are grounded. Drain its unverified queue (/kg-ground) to trust it."}
        else:
            continue
        item["signals"] = _agenda_signals(n)
        gaps.append((rank_key(n), item))
        emitted.add(nid)  # this node is now covered — don't re-surface it in an edgeless-communities item

    # edgeless communities: a disconnected cluster (>=2 nodes, no LIVE inter-community edge) — a coverage
    # gap, never answerable now. A single isolated node is already an `orphan`, so require >=2 members.
    comm_of = {n["id"]: n.get("community") for n in nodes}
    present = {c for c in comm_of.values() if c is not None and c != -1}
    if len(present) > 1:
        crossing: set = set()
        for e in edges:
            if e.get("epistemic_state") in ("failed", "rejected"):
                continue
            a, b = comm_of.get(e.get("source")), comm_of.get(e.get("target"))
            if a is not None and b is not None and a != b:
                crossing.add(a)
                crossing.add(b)
        for c in sorted(present - crossing):
            # exclude members already surfaced by a node-level detector — so a lone island (an `orphan`)
            # and a small cluster whose nodes are each already a gap (e.g. a freshly-proposed
            # hypothesized-only pair) are NOT re-surfaced here (one detector per node). Fire only when
            # >=2 members remain genuinely uncovered.
            fresh = [m for m in nodes if m.get("community") == c and m["id"] not in emitted]
            if len(fresh) < 2:
                continue
            rep = max(fresh, key=lambda m: m.get("degree") or 0)
            labels = ", ".join((m.get("label") or m["id"]) for m in fresh[:3])
            more = "…" if len(fresh) > 3 else ""
            gaps.append((rank_key(rep), {
                "detector": "edgeless-communities", "lane": "blocked_on_grounding",
                "focus": [m["id"] for m in fresh],
                "question": f"The '{rep.get('label') or rep['id']}' cluster ({labels}{more}) is disconnected "
                            f"from the rest of the graph — what relation bridges it?",
                "signals": _agenda_signals(rep)}))

    gaps.sort(key=lambda gi: gi[0], reverse=True)
    answerable: list = []
    blocked: list = []
    for _, item in gaps:
        bucket = answerable if item["lane"] == "answerable_now" else blocked
        if len(bucket) < limit:
            bucket.append(item)
    return {
        "answerable_now": answerable,
        "blocked_on_grounding": blocked,
        "count": len(answerable) + len(blocked),
        "limit": limit,
        "gate_on": gate_on,
        "ranked_by": ranked_by,
        "note": ("structural suggestions — a heuristic, not a guarantee. answerable_now reads grounded "
                 "content; blocked_on_grounding needs grounding (or extraction) first."),
    }
