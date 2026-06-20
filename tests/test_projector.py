"""Stage 5 exit test: node-link round-trip, incremental one-edge reproject, budgeted O(1) kg_context,
correct get_neighbors / shortest_path on a fixture graph.
"""
from __future__ import annotations

import json

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
    proj = Projector(canon)
    rep = proj.project()
    assert rep.full_rebuild and rep.n_nodes == 4 and rep.n_edges == 3

    data = json.loads(proj.graph_path.read_text())
    G = node_link_graph(data)
    assert isinstance(G, nx.DiGraph)
    assert G.number_of_nodes() == 4 and G.number_of_edges() == 3
    # re-serialize and reload -> stable
    from kg_engine.projector import _node_link_data
    G2 = node_link_graph(_node_link_data(G))
    assert set(G2.nodes()) == set(G.nodes())


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

    # if kg_context computed centrality in-request, this would raise
    monkeypatch.setattr(nx, "betweenness_centrality",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("centrality in-request!")))
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


def test_derived_contains_nothing_canon_does_not(canon: Canon):
    _seed(canon)
    proj = Projector(canon)
    proj.project()
    data = json.loads(proj.graph_path.read_text())
    canon_ids = {n.id for n in canon.all_nodes()}
    assert {n["id"] for n in data["nodes"]} <= canon_ids
    canon_edge_ids = {e.id for e in canon.all_edges()}
    assert {e["id"] for e in data["links"]} <= canon_edge_ids
