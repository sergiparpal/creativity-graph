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
from kg_engine.projector import Projector, node_link_graph


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
    from kg_engine.projector import _node_link_data
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


def test_derived_contains_nothing_canon_does_not(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    data = json.loads(proj.graph_path.read_text())
    canon_ids = {n.id for n in canon.all_nodes()}
    assert {n["id"] for n in data["nodes"]} <= canon_ids
    canon_edge_ids = {e.id for e in canon.all_edges()}
    assert {e["id"] for e in data["links"]} <= canon_edge_ids
