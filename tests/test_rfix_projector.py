"""Regression tests for the `projector` fix group:

  #1 (M6)  kg_context.items[] must NOT serve refuted/obsolete edges, while
           falsification_counters still counts failed/rejected.
  #2       query_graph edges must be a DETERMINISTIC top-N under LIMIT (ORDER BY id ASC).
  #3       betweenness reuse on a topology-unchanged incremental reproject must NOT zero-fill a
           dangling bridging target (a node in the live graph never persisted to the nodes table);
           the result must match a full rebuild of the identical graph.
  #4       ProjectReport carries a `contended` boolean (default False), set True on the
           lease-contended cold-start branch.
"""
from __future__ import annotations

import sqlite3

import networkx as nx

from kg_engine.canon import Canon
from kg_engine.model import Edge, EpistemicState, Node
from kg_engine.projector import ProjectReport, Projector


# --------------------------------------------------------------------------- #1 (M6)

def test_items_lane_excludes_refuted_and_obsolete_but_counters_still_count(canon: Canon):
    good = Edge(source="a", target="b", relation="grounds", span="s1")           # unverified -> served
    failed = Edge(source="a", target="c", relation="contradicts", span="s2",
                  epistemic_state=EpistemicState.FAILED)
    rejected = Edge(source="a", target="d", relation="bridges", span="s3",
                    epistemic_state=EpistemicState.REJECTED)
    obsolete = Edge(source="a", target="e", relation="reconciles_with", span="s4",
                    epistemic_state=EpistemicState.OBSOLETE)
    nodes = [
        Node(id="a", label="A", node_type="claim", edges=[good, failed, rejected, obsolete]),
        Node(id="b", label="B", node_type="claim"),
        Node(id="c", label="C", node_type="claim"),
        Node(id="d", label="D", node_type="claim"),
        Node(id="e", label="E", node_type="claim"),
    ]
    canon.write_nodes(nodes, message="seed refuted graph")
    proj = Projector(canon)
    proj.project()

    ctx = proj.kg_context()
    item_ids = {i["id"] for i in ctx["items"]}
    assert good.id in item_ids                    # a live edge is still served in the answer lane
    assert failed.id not in item_ids              # refuted edges are negative info, never an answer
    assert rejected.id not in item_ids
    assert obsolete.id not in item_ids            # superseded content is not an answer either
    # falsification memory is UNCHANGED: it counts failed + rejected (not obsolete, per FAILURE_STATES)
    assert ctx["falsification_counters"]["failed_or_rejected_edges"] == 2


# --------------------------------------------------------------------------- #2 (determinism)

def test_query_graph_edges_deterministic_top_n_under_limit(canon: Canon):
    edges = [Edge(source="a", target=f"n{i:02d}", relation="grounds", span=f"s{i}") for i in range(20)]
    nodes = [Node(id="a", label="A", node_type="claim", edges=edges)]
    nodes += [Node(id=f"n{i:02d}", label=f"N{i}", node_type="claim") for i in range(20)]
    canon.write_nodes(nodes, message="seed many edges")
    proj = Projector(canon)
    proj.project()

    res1 = proj.query_graph(limit=5)
    res2 = proj.query_graph(limit=5)
    ids1 = [e["id"] for e in res1["edges"]]
    ids2 = [e["id"] for e in res2["edges"]]
    assert ids1 == ids2                                   # stable across calls
    assert len(ids1) == 5
    # the returned subset is the lexicographically smallest-by-id 5 (ORDER BY id ASC), a pure function
    # of the canon — not SQLite's incidental row order (which the pre-fix `LIMIT` without ORDER BY gave).
    assert ids1 == sorted(e.id for e in edges)[:5]


# --------------------------------------------------------------------------- #3 (betweenness reuse)

def _build_dangling_bridge_graph() -> nx.MultiDiGraph:
    """a -> x <- b, where x is a BRIDGE (on the a-x-b path) that will be treated as a dangling target
    (present in the live graph, absent from a persisted-only prior_betweenness dict)."""
    G = nx.MultiDiGraph()
    G.add_node("a", label="alpha specific term")
    G.add_node("b", label="beta specific term")
    G.add_node("x", label="dangling bridge term")
    G.add_edge("a", "x", key="e1", relation="grounds", epistemic_state="unverified")
    G.add_edge("b", "x", key="e2", relation="grounds", epistemic_state="unverified")
    return G


def test_ranks_reuse_does_not_zero_fill_dangling_bridge(canon: Canon):
    proj = Projector(canon)
    G = _build_dangling_bridge_graph()

    full = proj._ranks(G)
    assert full.betweenness["x"] > 0.0                    # the bridge really carries betweenness

    # mimic _read_prior_betweenness: only PERSISTED nodes have a prior value; the dangling target x
    # (never written to the nodes table) is absent from the dict.
    prior_persisted_only = {"a": full.betweenness["a"], "b": full.betweenness["b"]}
    reuse = proj._ranks(G, prior_topo_sig=full.topo_sig, prior_betweenness=prior_persisted_only)
    # the fix recomputes for the whole graph rather than reusing the x-missing (→ x=0.0) dict.
    assert reuse.betweenness == full.betweenness
    assert reuse.betweenness["x"] == full.betweenness["x"] > 0.0
    assert reuse.gate_on == full.gate_on

    # sanity: when the prior dict is COMPLETE the reuse branch is still taken (returns prior verbatim,
    # even if the supplied values are synthetic) — proving the fix only bypasses reuse for the
    # dangling-target case, not always.
    complete = {"a": 9.0, "b": 8.0, "x": 7.0}
    reuse_complete = proj._ranks(G, prior_topo_sig=full.topo_sig, prior_betweenness=complete)
    assert reuse_complete.betweenness == {"a": 9.0, "b": 8.0, "x": 7.0}


def _read_gate_and_betweenness(proj: Projector) -> tuple[str, dict]:
    con = sqlite3.connect(proj.db_path)
    try:
        gate = dict(con.execute("SELECT key,value FROM meta").fetchall()).get("gate_on", "0")
        bet = {r[0]: r[1] for r in con.execute("SELECT id,betweenness FROM nodes")}
        return gate, bet
    finally:
        con.close()


def test_gate_and_betweenness_identical_full_vs_incremental_with_dangling_target(
        canon: Canon, source_path):
    # a -> x <- b, with x a DANGLING target (no canon node file for x).
    nodes = [
        Node(id="a", label="alpha specific term", node_type="claim",
             edges=[Edge(source="a", target="x", relation="grounds", span="s1")]),
        Node(id="b", label="beta specific term", node_type="claim",
             edges=[Edge(source="b", target="x", relation="grounds", span="s2")]),
    ]
    canon.write_nodes(nodes, message="seed dangling-bridge canon")
    src = source_path.read_text(encoding="utf-8")

    # Path A: a fresh FULL rebuild in its own derived dir.
    proj_full = Projector(canon, derived_dir=canon.root / "dA", source_text=src)
    proj_full.project(incremental=False)
    gate_full, bet_full = _read_gate_and_betweenness(proj_full)

    # Path B: full build, then a NON-topological canon edit (body only) -> incremental reproject that
    # takes the betweenness-reuse branch on the identical topology.
    proj_inc = Projector(canon, derived_dir=canon.root / "dB", source_text=src)
    proj_inc.project()
    node_a = canon.read_node("a")
    node_a.body = (node_a.body or "") + "\nan extra body line (no edge change)."
    canon.write_one(node_a)
    rep = proj_inc.project(incremental=True)
    assert rep.full_rebuild is False                       # confirm we actually exercised the reuse path
    gate_inc, bet_inc = _read_gate_and_betweenness(proj_inc)

    assert gate_full == gate_inc                            # the specificity gate is stable across paths
    assert bet_full == bet_inc                              # persisted betweenness identical


# --------------------------------------------------------------------------- #4 (contended field)

def test_project_report_has_contended_defaulting_false():
    rep = ProjectReport()
    assert hasattr(rep, "contended")
    assert rep.contended is False


def test_project_sets_contended_true_when_lease_unavailable(canon: Canon, monkeypatch):
    proj = Projector(canon)
    # simulate a concurrent holder of the canon lease -> the cold-start contended branch.
    monkeypatch.setattr(proj.canon, "try_acquire_lock", lambda *a, **k: False)
    rep = proj.project()
    assert rep.contended is True
