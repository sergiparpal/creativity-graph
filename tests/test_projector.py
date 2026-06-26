"""Stage 5 exit test: node-link round-trip, incremental one-edge reproject, budgeted O(1) kg_context,
correct get_neighbors / shortest_path on a fixture graph.
"""
from __future__ import annotations

import json
import math
import sqlite3

import networkx as nx
import pytest

from kg_engine.canon import Canon
from kg_engine.model import Edge, EpistemicState, Node
from kg_engine.graphio import node_link_graph
from kg_engine.projector import Projector


def _seed(canon: Canon):
    nodes = [
        Node(id="a", label="A", node_type="compression", edges=[
            Edge(source="a", target="b", relation="grounds", span="s1"),
            Edge(source="a", target="c", relation="bridges", span="s2"),
        ]),
        Node(id="b", label="B", node_type="claim", edges=[
            Edge(source="b", target="d", relation="grounds", span="s3"),
        ]),
        Node(id="c", label="C", node_type="metric"),
        Node(id="d", label="D", node_type="claim"),
    ]
    canon.write_nodes(nodes, message="seed graph")


def test_graph_json_roundtrips_through_networkx(canon: Canon):
    _seed(canon)
    # add a PARALLEL edge: same (source, target) as the existing a->b grounds edge but a different
    # relation. A plain DiGraph would silently collapse the two; the projector must keep both
    # (tests-6 — derived contains nothing the canon does not).
    node_a = canon.read_node("a")
    node_a.edges.append(Edge(source="a", target="b", relation="contradicts", span="s5"))
    canon.write_one(node_a)

    proj = Projector(canon)
    rep = proj.project()
    assert rep.full_rebuild and rep.n_nodes == 4 and rep.n_edges == 4  # 3 + the parallel edge

    data = json.loads(proj.graph_path.read_text())
    G = node_link_graph(data)
    assert isinstance(G, nx.MultiDiGraph)  # NOT a plain DiGraph (which can't hold parallel edges)
    assert G.number_of_nodes() == 4 and G.number_of_edges() == 4
    # both parallel a->b edges survive the round-trip, keyed by distinct edge ids
    ab = G.get_edge_data("a", "b")
    assert len(ab) == 2 and {d["relation"] for d in ab.values()} == {"grounds", "contradicts"}
    # re-serialize and reload -> stable, parallel edges preserved
    from kg_engine.graphio import _node_link_data
    G2 = node_link_graph(_node_link_data(G))
    assert set(G2.nodes()) == set(G.nodes()) and G2.number_of_edges() == 4


def test_incremental_reproject_touches_only_changed_edge(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()  # full

    # add exactly one edge to an existing node
    node_a = canon.read_node("a")
    new = Edge(source="a", target="d", relation="reconciles_with", span="s4")
    node_a.edges.append(new)
    canon.write_one(node_a)

    rep = proj.project(incremental=True)
    assert rep.full_rebuild is False
    assert rep.touched_edges == [new.id], rep.touched_edges
    assert rep.touched_nodes == ["a"], rep.touched_nodes


def test_reproject_noop_is_up_to_date(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    rep = proj.project(incremental=True)
    assert rep.up_to_date is True


def test_kg_context_within_budget_and_no_centrality(canon: Canon, monkeypatch):
    _seed(canon)
    proj = Projector(canon)
    proj.project()

    # kg_context must read precomputed columns ONLY. Patch the rank computation (Leiden/degree/bridge)
    # to blow up: if kg_context recomputed ranks in-request, this would raise. The old test patched
    # nx.betweenness_centrality, which the projector never calls — so it asserted nothing (tests-4).
    monkeypatch.setattr(proj, "_ranks",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ranks computed in-request!")))
    ctx = proj.kg_context(budget=200)
    assert ctx["approx_tokens"] <= 200
    assert "falsification_counters" in ctx
    assert ctx["advisory"]["signal"] == "structural-bridge"
    assert "advisory" in ctx["advisory"]["note"]


def test_kg_context_priority_and_failure_counter(canon: Canon):
    # a grounded edge should sort ahead; a failed edge must be counted, not pruned
    nodes = [
        Node(id="a", label="A", edges=[
            Edge(source="a", target="b", relation="grounds", span="s1",
                 epistemic_state=EpistemicState.GROUNDED),
            Edge(source="a", target="c", relation="grounds", span="s2",
                 epistemic_state=EpistemicState.FAILED),
        ]),
        Node(id="b"), Node(id="c"),
    ]
    canon.write_nodes(nodes, message="seed")
    proj = Projector(canon)
    proj.project()
    ctx = proj.kg_context(budget=5000)
    assert ctx["falsification_counters"]["failed_or_rejected_edges"] == 1
    assert ctx["items"][0]["epistemic_state"] == "grounded"  # grounded filled first


def test_kg_context_query_is_termwise_not_whole_string(canon: Canon):
    # a multi-word / natural-language query must match edges containing ANY of its terms; a single
    # LIKE on the whole question string would only match a verbatim substring and always miss.
    nodes = [
        Node(id="betweenness", label="Betweenness", edges=[
            Edge(source="betweenness", target="generality-confound", relation="confounded_by",
                 span="it is confounded_by the generality confound", epistemic_state=EpistemicState.GROUNDED),
        ]),
        Node(id="degree", label="Degree", edges=[
            Edge(source="degree", target="importance", relation="approximates",
                 span="degree approximates importance", epistemic_state=EpistemicState.GROUNDED),
        ]),
        Node(id="generality-confound"), Node(id="importance"),
    ]
    canon.write_nodes(nodes, message="seed")
    proj = Projector(canon)
    proj.project()
    # full-sentence question (would be a 0-item miss under the old whole-string LIKE)
    q = "Is betweenness confounded by the generality confound and does degree approximate importance?"
    items = proj.kg_context(query=q)["items"]
    rels = {(i["source"], i["relation"]) for i in items}
    assert ("betweenness", "confounded_by") in rels
    assert ("degree", "approximates") in rels
    # a term that matches nothing yields no items (no accidental match-all)
    assert proj.kg_context(query="zzznomatch")["items"] == []


def test_get_neighbors_and_shortest_path(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()

    neigh = proj.get_neighbors("a")
    targets = {e["target"] for e in neigh}
    assert targets == {"b", "c"}

    assert proj.get_neighbors("a", relation="bridges")[0]["target"] == "c"
    assert proj.shortest_path("a", "d") == ["a", "b", "d"]
    assert proj.shortest_path("c", "c") == ["c"]


def test_query_graph_ranked_by_degree(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    res = proj.query_graph(limit=10)
    assert len(res["nodes"]) == 4
    degs = [n["degree"] for n in res["nodes"]]
    assert degs == sorted(degs, reverse=True)  # ranked by precomputed degree


def _confound_graph(canon: Canon):
    """A vague hub ('system') carries more cross-cluster traffic than a specific bridge
    ('entropy-arrow'); the corpus makes 'system' common and the bridge's terms rare."""
    nodes = [
        Node(id="system", label="system", node_type="claim", edges=[
            Edge(source="system", target="a1", relation="bridges", span="x"),
            Edge(source="system", target="b1", relation="bridges", span="x"),
            Edge(source="system", target="a2", relation="bridges", span="x"),
            Edge(source="system", target="b2", relation="bridges", span="x"),
        ]),
        Node(id="entropy-arrow", label="thermodynamic entropy arrow", node_type="claim", edges=[
            Edge(source="entropy-arrow", target="a3", relation="bridges", span="x"),
            Edge(source="entropy-arrow", target="b3", relation="bridges", span="x"),
        ]),
        Node(id="a1", label="a1", edges=[Edge(source="a1", target="a2", relation="bridges", span="x"),
                                         Edge(source="a1", target="a3", relation="bridges", span="x")]),
        Node(id="a2", label="a2", edges=[Edge(source="a2", target="a3", relation="bridges", span="x")]),
        Node(id="a3", label="a3"),
        Node(id="b1", label="b1", edges=[Edge(source="b1", target="b2", relation="bridges", span="x"),
                                         Edge(source="b1", target="b3", relation="bridges", span="x")]),
        Node(id="b2", label="b2", edges=[Edge(source="b2", target="b3", relation="bridges", span="x")]),
        Node(id="b3", label="b3"),
    ]
    canon.write_nodes(nodes, message="seed confound graph")
    corpus = "\n".join(["## intro"] + ["## s the system is a system"] * 19
                       + ["## s the system thermodynamic entropy arrow rare once"])
    return corpus


def test_stage2_columns_finite_and_gate_binary(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    for r in proj.query_graph(limit=50)["nodes"]:
        assert math.isfinite(r["betweenness"]) and math.isfinite(r["spec_betweenness"])
        assert math.isfinite(r["specificity"])
        assert r["gate_on"] in (0, 1)


def test_stage2_specificity_corrects_generality_confound(canon: Canon):
    corpus = _confound_graph(canon)
    proj = Projector(canon, source_text=corpus)
    proj.project(incremental=False)
    rows = {r["id"]: r for r in proj.query_graph(limit=50)["nodes"]}
    sysr, entr = rows["system"], rows["entropy-arrow"]
    # the vague hub is the high-traffic node ...
    assert sysr["betweenness"] > entr["betweenness"]
    # ... but its terms are common, so spec-weighting pulls it BELOW the specific bridge.
    assert sysr["specificity"] < entr["specificity"]
    assert sysr["spec_betweenness"] < entr["spec_betweenness"]


def test_stage2_bridge_metric_advisory_nonempty(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    bm = proj.kg_context(budget=2000)["advisory"]["bridge_metric"]
    assert bm["gate_on"] in (0, 1)
    assert bm["ranked_by"] in ("spec_betweenness", "structural_bridge")
    assert bm["nodes"]  # non-empty ranked list
    assert {"betweenness", "spec_betweenness", "specificity"} <= set(bm["nodes"][0])


def test_stage2_outdated_schema_forces_full_rebuild(canon: Canon):
    # simulate an index.sqlite built before the Stage-2 columns: a legacy 11-column nodes table.
    _seed(canon)
    proj = Projector(canon)
    proj.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(proj.db_path)
    con.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE nodes(id TEXT PRIMARY KEY, label TEXT, node_type TEXT, file_type TEXT,"
        " provenance TEXT, authored_by TEXT, epistemic_state TEXT, degree INTEGER, community INTEGER,"
        " bridge_communities INTEGER, structural_bridge INTEGER);"
        "INSERT INTO meta VALUES ('built_from_commit','deadbeef');"
        "INSERT INTO meta VALUES ('file_hashes','{}');")
    con.commit(); con.close()
    assert proj._schema_outdated() is True
    rep = proj.project(incremental=True)
    assert rep.full_rebuild is True  # outdated schema forced a full rebuild
    # the migrated table now carries the new columns and finite values
    r = proj.get_node("a")
    assert "spec_betweenness" in r and math.isfinite(r["spec_betweenness"])


def test_outdated_schema_healed_under_lock_contention_so_reads_do_not_crash(canon: Canon, monkeypatch):
    # M3: a plugin upgrade leaves the on-disk index with the pre-Stage-2 11-column `nodes` table while
    # another session holds the canon lease. project() falls into the contention branch; before the fix
    # it healed only a MISSING db, so kg_context then crashed on `no such column: betweenness`. Now the
    # contention branch ALSO heals an outdated schema, leaving an empty table reads can serve.
    _seed(canon)
    proj = Projector(canon)
    proj.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(proj.db_path)
    con.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE nodes(id TEXT PRIMARY KEY, label TEXT, node_type TEXT, file_type TEXT,"
        " provenance TEXT, authored_by TEXT, epistemic_state TEXT, degree INTEGER, community INTEGER,"
        " bridge_communities INTEGER, structural_bridge INTEGER);")
    con.commit(); con.close()
    # graph.json must already exist so the contention branch heals ONLY the schema (not the cold path).
    proj.graph_path.write_text(json.dumps({"nodes": [], "links": []}))
    assert proj._schema_outdated() is True
    # simulate another session holding the lease -> the contention branch
    monkeypatch.setattr(canon, "try_acquire_lock", lambda: False)
    proj.project()
    assert proj._schema_outdated() is False  # the contention branch healed the schema in place
    # the read that used to crash on `no such column: betweenness` now returns an (empty) result
    ctx = proj.kg_context(budget=2000)
    assert ctx["advisory"]["bridge_metric"]["nodes"] == []  # empty table -> empty ranked list, no crash


def test_ranking_and_agenda_are_deterministic_across_full_vs_incremental(canon: Canon):
    # tiebreak: a graph of all-tied nodes (betweenness/spec_betweenness 0.0, identical degree) must
    # surface the SAME ordered top-N regardless of how the index was built (full rebuild vs an
    # incremental reproject of the identical canon). The `id ASC` tiebreak makes the order a pure
    # function of the canon.
    nodes = [Node(id=f"n{i:02d}", label=f"N{i:02d}", node_type="claim",
                  edges=[Edge(source=f"n{i:02d}", target="hub", relation="bridges", span="x")])
             for i in range(20)]
    nodes.append(Node(id="hub", label="hub", node_type="claim"))
    canon.write_nodes(nodes, message="seed tied graph")

    proj_full = Projector(canon)
    proj_full.project(incremental=False)
    full_bm = [n["id"] for n in proj_full.kg_context(budget=4000)["advisory"]["bridge_metric"]["nodes"]]
    full_qg = [n["id"] for n in proj_full.query_graph(limit=10)["nodes"]]
    full_agenda = ([i["focus"][0] for i in proj_full.kg_agenda(limit=50)["answerable_now"]]
                   + [i["focus"][0] for i in proj_full.kg_agenda(limit=50)["blocked_on_grounding"]])

    # the bridge_metric / query_graph orders are deterministically id-sorted within the tied band
    assert full_bm == sorted(full_bm)
    assert full_qg == sorted(full_qg)

    # force a fresh full rebuild from the same canon and confirm identical ordering (no run-to-run churn)
    proj_rebuilt = Projector(canon)
    proj_rebuilt.db_path.unlink(missing_ok=True)
    proj_rebuilt.graph_path.unlink(missing_ok=True)
    proj_rebuilt.project(incremental=False)
    assert [n["id"] for n in proj_rebuilt.kg_context(budget=4000)["advisory"]["bridge_metric"]["nodes"]] == full_bm
    rebuilt_agenda = ([i["focus"][0] for i in proj_rebuilt.kg_agenda(limit=50)["answerable_now"]]
                      + [i["focus"][0] for i in proj_rebuilt.kg_agenda(limit=50)["blocked_on_grounding"]])
    assert rebuilt_agenda == full_agenda  # agenda order is reproducible too


def test_degree_counts_distinct_neighbours_not_parallel_edges(canon: Canon):
    # A has TWO distinct un-failed relations to B (grounds + bridges) and one to C: 2 distinct
    # neighbours, not 3. Degree must match the distinct-neighbour bridge signal / glossary, so A is NOT
    # mis-counted as a degree-3 hub. (Old `und.degree()` over the MultiGraph returned 3.)
    nodes = [
        Node(id="a", label="A", node_type="claim", edges=[
            Edge(source="a", target="b", relation="grounds", span="s1"),
            Edge(source="a", target="b", relation="bridges", span="s2"),
            Edge(source="a", target="c", relation="bridges", span="s3"),
        ]),
        Node(id="b", label="B"), Node(id="c", label="C"),
    ]
    canon.write_nodes(nodes, message="seed parallel-edge graph")
    proj = Projector(canon)
    proj.project()
    a = proj.get_node("a")
    assert a["degree"] == 2  # distinct neighbours {b, c}, NOT 3 (the two a->b relations are one neighbour)
    # the bridge-community span (computed from und.neighbors) and the persisted degree now agree
    assert a["bridge_communities"] <= a["degree"]
    # below the _HUB_DEGREE (3) cutoff, so kg_agenda does not misclassify A as a hub on edge multiplicity
    hubs = [i["focus"][0] for i in proj.kg_agenda(limit=50)["answerable_now"]
            if i["detector"] == "well-grounded"]
    assert "a" not in hubs


def test_derived_contains_nothing_canon_does_not(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    data = json.loads(proj.graph_path.read_text())
    canon_ids = {n.id for n in canon.all_nodes()}
    assert {n["id"] for n in data["nodes"]} <= canon_ids
    canon_edge_ids = {e.id for e in canon.all_edges()}
    assert {e["id"] for e in data["links"]} <= canon_edge_ids


# --------------------------------------------------------------------------- R3: source-staleness advisory

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from kg_engine.model import Provenance, edge_id  # noqa: E402
from kg_engine.server import KGEngine  # noqa: E402

_PACK = Path(__file__).resolve().parents[1] / "pack" / "pack.yaml"


def _engine(vault, source_path):
    return KGEngine(vault, source_path=source_path, pack_path=_PACK)


def _rewrite(path: Path, text: str) -> None:
    """Rewrite a source file AND push its mtime forward, so the SourceSet (signature-cached on mtime)
    is guaranteed to re-resolve even within the same filesystem mtime tick."""
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 100))


def _stale_ids(eng) -> set:
    return {s["edge_id"] for s in eng.projector.kg_context()["advisory"]["stale_verdicts"]}


def _ground_span_edge(eng, *, src="compression", rel="grounds", tgt="claim", span, verdict="grounded",
                      source_file=""):
    # provenance MUST be span-present (the staleness detector only checks span-present edges); an
    # under-claimed `inferred` is left as-is by the boundary, so declare it explicitly.
    eng.kg_write({"edges": [{"source": src, "target": tgt, "relation": rel, "span": span,
                             "provenance": "span-present", "source_file": source_file,
                             "authored_by": "agent"}]})
    eid = edge_id(src, rel, tgt)
    eng.kg_ground(eid, verdict)
    eng.projector.project()  # sync the verdict into the derived layer
    return eid


def test_stale_verdict_flagged_when_source_diverges_and_never_mutates_verdict(vault, tmp_path):
    src = tmp_path / "s.md"
    src.write_text("A compression grounds the claims beneath it.\n", encoding="utf-8")
    eng = _engine(vault, src)
    eid = _ground_span_edge(eng, span="A compression grounds the claims beneath it")
    assert _stale_ids(eng) == set()                      # span present -> not flagged

    _rewrite(src, "Totally different prose with no such relation.\n")
    eng.projector.project()                              # the NEXT projection picks up the divergence
    assert eng.projector.kg_context()["advisory"]["stale_verdicts"] == \
        [{"edge_id": eid, "reason": "span-no-longer-in-source"}]
    # READ-ONLY advisory: the verdict itself is untouched (never-forge-a-verdict / measure-never-gate)
    e = next(x for x in eng.canon.all_edges() if x.id == eid)
    assert e.epistemic_state == EpistemicState.GROUNDED and e.verdict_by == "agent"


def test_multifile_no_false_flag_for_span_in_its_own_file(vault, tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "a.md").write_text("Alpha grounds beta in the first file.\n", encoding="utf-8")
    (d / "b.md").write_text("Gamma bridges delta in the second file.\n", encoding="utf-8")
    eng = _engine(vault, d)
    eid = _ground_span_edge(eng, src="alpha", tgt="beta", span="Alpha grounds beta", source_file="a.md")
    # b.md lacks the span, but the per-file check verifies against a.md (the edge's source_file) -> clean
    assert _stale_ids(eng) == set()
    # editing a.md to remove it DOES flag it (the positive control — per-file detection works)
    _rewrite(d / "a.md", "Alpha is unrelated to beta now.\n")
    eng.projector.project()
    assert _stale_ids(eng) == {eid}


def test_unverified_and_inferred_are_never_flagged(vault, tmp_path):
    src = tmp_path / "s.md"
    src.write_text("A compression grounds the claims beneath it.\n", encoding="utf-8")
    eng = _engine(vault, src)
    # an UNVERIFIED span-present edge (never grounded)
    eng.kg_write({"edges": [{"source": "compression", "target": "claim", "relation": "grounds",
                             "span": "A compression grounds the claims beneath it",
                             "provenance": "span-present", "authored_by": "agent"}]})
    # an INFERRED grounded edge (promoted from a hypothesis by support_note -> provenance inferred)
    eng.kg_propose({"edges": [{"source": "degree", "target": "importance", "relation": "approximates"}]})
    eng.kg_ground(edge_id("degree", "approximates", "importance"), "grounded", support_note="citation")
    eng.projector.project()
    _rewrite(src, "Nothing here matches any prior span at all.\n")
    eng.projector.project()
    assert _stale_ids(eng) == set()   # not a verdict (unverified) / no span claim (inferred) -> never flagged


def test_failed_edge_with_missing_span_is_flagged(vault, tmp_path):
    src = tmp_path / "s.md"
    src.write_text("Betweenness is confounded by the generality confound.\n", encoding="utf-8")
    eng = _engine(vault, src)
    eid = _ground_span_edge(eng, src="betweenness", rel="confounded_by", tgt="gc",
                            span="Betweenness is confounded by the generality confound", verdict="failed")
    assert _stale_ids(eng) == set()
    _rewrite(src, "An unrelated sentence, no confound here.\n")
    eng.projector.project()
    assert _stale_ids(eng) == {eid}   # FAILED span-present edges are checked too (§1.7 evidence)


def test_stale_advisory_persisted_and_reused_without_a_source_change(vault, tmp_path):
    src = tmp_path / "s.md"
    src.write_text("A compression grounds the claims beneath it.\n", encoding="utf-8")
    eng = _engine(vault, src)
    eid = _ground_span_edge(eng, span="A compression grounds the claims beneath it")
    _rewrite(src, "Different text entirely.\n")
    eng.projector.project()
    assert _stale_ids(eng) == {eid}
    # a no-op projection (canon + source unchanged) short-circuits AND serves the SAME flag from meta
    rep = eng.projector.project()
    assert rep.up_to_date
    assert _stale_ids(eng) == {eid}


def test_canon_edited_span_under_unchanged_source_is_flagged(vault, tmp_path):
    """The canon-edit path (not just source edits): a grounded span-present edge whose SPAN is hand-edited
    to text absent from the source — with the source itself UNCHANGED — is still flagged on the next
    projection (the incremental pass scans the changed notes, not only the already-flagged set)."""
    src = tmp_path / "s.md"
    src.write_text("A compression grounds the claims beneath it.\n", encoding="utf-8")
    eng = _engine(vault, src)
    eid = _ground_span_edge(eng, span="A compression grounds the claims beneath it")
    assert _stale_ids(eng) == set()
    # hand-edit the canon note: swap the span to text NOT in the source (source untouched)
    node = eng.canon.read_node("compression")
    for e in node.edges:
        if e.id == eid:
            e.span = "a span that is nowhere in the source"
    eng.canon.write_one(node)
    eng.projector.project()
    assert _stale_ids(eng) == {eid}
    # still grounded — the advisory only reports, never mutates the verdict
    e = next(x for x in eng.canon.all_edges() if x.id == eid)
    assert e.epistemic_state == EpistemicState.GROUNDED


def test_no_source_yields_empty_stale_list(vault):
    eng = _engine(vault, None)  # no source configured at all
    # seed a grounded span-present edge directly (the boundary would reject it with no source)
    node = Node(id="compression", label="Compression", node_type="compression", edges=[
        Edge(source="compression", target="claim", relation="grounds", span="some span",
             provenance=Provenance.SPAN_PRESENT, epistemic_state=EpistemicState.GROUNDED)])
    eng.canon.write_nodes([node], message="seed grounded")
    eng.projector.project()
    assert eng.projector.kg_context()["advisory"]["stale_verdicts"] == []  # no source -> no divergence


# --------------------------------------------------------------------------- _head git-wedge guard
# Regression for the watchdog exit-71 crash: in the detached MCP server, projector._head() shelled out
# to `git rev-parse HEAD` with no timeout, no stdin redirect, and no non-git guard. On a non-git canon
# (e.g. a cloud-synced Documents folder) or when git hangs on a prompt, subprocess.run() blocked
# forever, the handler exceeded KG_HANDLER_TIMEOUT, and the supervisor force-exited the engine.


def test_head_empty_on_non_git_canon_without_spawning_git(tmp_path, monkeypatch):
    import kg_engine.projector as projector_mod
    canon = Canon(tmp_path / "nogit")  # a fresh canon dir with NO .git — not the git-backed vault
    proj = Projector(canon)

    def _must_not_spawn(*a, **k):  # the .git guard must short-circuit before any git fork
        raise AssertionError("git must not be spawned on a non-git canon")

    monkeypatch.setattr(projector_mod.subprocess, "run", _must_not_spawn)
    assert proj._head() == ""


def test_head_empty_when_git_hangs(canon: Canon, monkeypatch):
    # git-backed canon (the .git guard passes), but git itself wedges -> a bounded _head() must degrade
    # to "" rather than let the TimeoutExpired propagate into the projection handler.
    import kg_engine.projector as projector_mod
    proj = Projector(canon)

    def _hang(*a, **k):
        raise projector_mod.subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(projector_mod.subprocess, "run", _hang)
    assert proj._head() == ""


def test_project_succeeds_on_non_git_canon(tmp_path):
    # End-to-end SMOKE test of the non-git projection path: a full project() over a canon with no .git
    # lands an empty commit pin and a fresh derived layer. NB this does not by itself reproduce the
    # exit-71 hang (a non-git `git rev-parse` returns fast in a normal shell — the wedge only manifests
    # in the detached server) — the two _head tests above are the real regression guards; this just
    # pins the observable contract on the real-world failure shape (canon on a non-repo filesystem).
    canon = Canon(tmp_path / "nogit2")
    nodes = [
        Node(id="a", label="A", node_type="compression",
             edges=[Edge(source="a", target="b", relation="grounds", span="s1")]),
        Node(id="b", label="B", node_type="claim"),
    ]
    canon.write_nodes(nodes, message="seed", commit=False)  # commit=False: no git on a non-git vault
    proj = Projector(canon)
    report = proj.project()
    assert report.built_from_commit == ""  # non-git canon has no HEAD
    assert report.n_nodes == 2 and proj.is_stale() is False
