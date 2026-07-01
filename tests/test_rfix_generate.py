"""Regression tests for the `generate` group review fixes:

  #1 (M5) Generators build their adjacency/undirected topology over the failure-EXCLUDED subgraph
     (mirroring projector._live_subgraph), so a `failed`/`rejected` edge never seeds a spurious
     shared-neighbour bridge/seed/periphery candidate.
  #2 transplant() folds a per-target signal (the target's live degree) into its score, so a
     higher-degree target genuinely outranks a lower-degree one instead of every candidate tying and
     _rank collapsing to target-id order.
  #3 convergence is tallied over each mechanism's PRE-truncation candidate list, so a pair proposed by
     two mechanisms but dropped from one mechanism's own top-k still reports convergence >= 2.
"""
from __future__ import annotations

import networkx as nx

from kg_engine import generate as gen
from kg_engine.canon import Canon
from kg_engine.model import Edge, Node
from kg_engine.projector import Projector

_UNIFORM = "## a\n## b\n## c"


def _mdg(edges, node_attrs, *, failed=()):
    """A MultiDiGraph with explicit node attrs + per-edge epistemic_state (default `unverified`)."""
    G = nx.MultiDiGraph()
    for n, a in node_attrs.items():
        G.add_node(n, label=n, community=a.get("community", 0), degree=a.get("degree", 2),
                   specificity=a.get("specificity", 1.0), gate_on=0,
                   structural_bridge=a.get("structural_bridge", 1))
    for i, (u, v) in enumerate(edges):
        st = "failed" if (u, v) in failed else "unverified"
        G.add_edge(u, v, key="e%d" % i, relation="bridges", epistemic_state=st)
    return G


def _ranked(canon: Canon, edges, *, corpus=_UNIFORM):
    nm: dict = {}
    for s, t in edges:
        nm.setdefault(s, []).append(Edge(source=s, target=t, relation="bridges", span="x"))
        nm.setdefault(t, [])
    nodes = [Node(id=k, label=k, node_type="claim", edges=v) for k, v in nm.items()]
    canon.write_nodes(nodes, message="rfix generate fixture")
    proj = Projector(canon, source_text=corpus)
    proj.project(incremental=False)
    return proj.load_graph()


# --------------------------------------------------------------------------- #1 live-topology filter


def test_undirected_adjacency_excludes_failed_and_rejected_edges():
    G = nx.MultiDiGraph()
    for n in ("a", "b", "c", "d"):
        G.add_node(n, label=n)
    G.add_edge("a", "b", key="e0", relation="bridges", epistemic_state="unverified")
    G.add_edge("a", "c", key="e1", relation="bridges", epistemic_state="failed")
    G.add_edge("a", "d", key="e2", relation="bridges", epistemic_state="rejected")
    adj = gen._undirected_adjacency(G)
    assert adj["a"] == {"b"}                     # only the live edge survives
    assert "c" not in adj["a"] and "d" not in adj["a"]


def test_live_undirected_drops_failure_edges_keeps_nodes():
    G = nx.MultiDiGraph()
    for n in ("a", "b", "c"):
        G.add_node(n, label=n)
    G.add_edge("a", "b", key="e0", relation="bridges", epistemic_state="unverified")
    G.add_edge("a", "c", key="e1", relation="bridges", epistemic_state="failed")
    und = gen._live_undirected(G)
    assert set(und.nodes()) == {"a", "b", "c"}   # every node kept (an all-refuted hub still appears)
    assert und.has_edge("a", "b")
    assert not und.has_edge("a", "c")            # the failed edge is not live topology


def test_failed_edge_does_not_seed_spurious_shared_neighbour_candidate():
    # p and q are non-adjacent and share the neighbours s1 and s2 (distance 2). With everything live,
    # seed proposes the {p,q} bridge. Flip q-s1 to `failed`: s1 is no longer a live shared neighbour, so
    # the spurious {p,q} candidate must disappear. (Under the pre-fix code the failed edge still counted
    # toward the shared-neighbour proxy and the candidate survived.)
    edges = [("p", "s1"), ("q", "s1"), ("p", "s2"), ("q", "s2"), ("s1", "r"), ("r", "x"), ("x", "y")]
    node_attrs = {n: {} for e in edges for n in e}

    live = gen.seed(_mdg(edges, node_attrs), pack=None, corpus=[_UNIFORM], failures=set(), k=40)
    live_pairs = {frozenset((c.source, c.target)) for c in live}
    assert frozenset(("p", "q")) in live_pairs                    # the (buggy-code) spurious candidate

    withfail = gen.seed(_mdg(edges, node_attrs, failed=[("q", "s1")]),
                        pack=None, corpus=[_UNIFORM], failures=set(), k=40)
    fail_pairs = {frozenset((c.source, c.target)) for c in withfail}
    assert frozenset(("p", "q")) not in fail_pairs               # excluded once the edge is refuted


# --------------------------------------------------------------------------- #2 transplant ranking


def test_transplant_higher_degree_target_outranks_lower():
    # hub in community 0; community 1 holds two non-adjacent targets of different live degree. The
    # per-target degree signal must make the higher-degree target rank strictly first.
    node_attrs = {
        "hub": {"community": 0, "degree": 5},
        "h2": {"community": 0, "degree": 1},
        "t_hi": {"community": 1, "degree": 4},
        "t_lo": {"community": 1, "degree": 1},
        "tx": {"community": 1, "degree": 2},
    }
    edges = [("hub", "h2"), ("hub", "tx"), ("t_hi", "tx"), ("t_hi", "t_lo")]
    # hub's out-edges must carry a real relation (the dominant one it transplants)
    G = nx.MultiDiGraph()
    for n, a in node_attrs.items():
        G.add_node(n, label=n, community=a["community"], degree=a["degree"], specificity=1.0,
                   gate_on=0, structural_bridge=1)
    for i, (u, v) in enumerate(edges):
        G.add_edge(u, v, key="e%d" % i, relation="drives", epistemic_state="unverified")

    cands = gen.transplant(G, pack=None, corpus=[_UNIFORM], failures=set(), k=40)
    order = [c.target for c in cands]
    assert "t_hi" in order and "t_lo" in order
    assert order.index("t_hi") < order.index("t_lo")             # degree-desc intent preserved
    hi = next(c for c in cands if c.target == "t_hi")
    lo = next(c for c in cands if c.target == "t_lo")
    assert hi.score > lo.score                                   # scores no longer tie loop-invariantly


# --------------------------------------------------------------------------- #3 convergence pre-truncation


_CONVERGENCE_EDGES = [("a1", "a2"), ("a2", "a3"), ("a1", "a3"), ("b1", "b2"), ("b2", "b3"),
                      ("b1", "b3"), ("a1", "b1"), ("a2", "b1"), ("a1", "b2"), ("a2", "b2")]


def test_convergence_counts_pair_dropped_from_one_mechanisms_topk(canon: Canon):
    # {a3,b2} is proposed by BOTH bridge (at a non-top rank) and transplant (as its top). At k=1 the
    # pair falls off bridge's own top-1, so a tally over the surfaced (truncated) slate would undercount
    # it as 1. Tallying over each mechanism's PRE-truncation list must still report convergence >= 2.
    G = _ranked(canon, _CONVERGENCE_EDGES)
    pair = frozenset(("a3", "b2"))

    # sanity: bridge proposes the pair but NOT in its top-1 (it would be dropped by k=1 truncation)
    bridge_cands = gen.bridge(G, pack=None, corpus=[_UNIFORM], failures=set(), k=40)
    assert any(frozenset((c.source, c.target)) == pair for c in bridge_cands)
    assert frozenset((bridge_cands[0].source, bridge_cands[0].target)) != pair

    out = gen.run_generators(G, mechanism="all", pack=None, corpus=[_UNIFORM], failures=set(), k=1)
    surv = next(c for c in out if c.kind == "edge" and frozenset((c.source, c.target)) == pair)
    assert surv.convergence >= 2                                 # counted across both, despite k=1 truncation
