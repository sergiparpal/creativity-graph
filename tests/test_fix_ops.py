"""Regression tests for the generate/operations fixes:

  - F32: explode_payload honours k=0 (zero facets) and guards negative k (no silent slice-from-end),
    matching open_payload's clamp discipline — k is unvalidated LLM-supplied MCP input.
  - F30: transplant() is a behaviour-preserving perf fix (hoist to_undirected + loop-invariant
    absorption); its output must be byte-for-byte unchanged and deterministic across runs.
"""
from __future__ import annotations

import itertools

import networkx as nx

from kg_engine import generate as gen
from kg_engine.canon import Canon
from kg_engine.operations import _resolve_cluster, collapse_payload, explode_payload
from kg_engine.projector import Projector

_UNIFORM = "## a\n## b\n## c"


def _ranked(canon: Canon, edges, *, corpus=_UNIFORM, node_type="claim", rels=None):
    """Write a graph into the canon and return the projected, rank-attributed in-memory graph.

    `rels`, when given, maps (source, target) -> relation so a node can carry distinct out-relations
    (explode reads the node's distinct outgoing relations as its facets)."""
    from kg_engine.model import Edge, Node
    rels = rels or {}
    nm: dict = {}
    for s, t in edges:
        rel = rels.get((s, t), "bridges")
        nm.setdefault(s, []).append(Edge(source=s, target=t, relation=rel, span="x"))
        nm.setdefault(t, [])
    nodes = [Node(id=k, label=k, node_type=node_type, edges=v) for k, v in nm.items()]
    canon.write_nodes(nodes, message="seed fix fixture")
    proj = Projector(canon, source_text=corpus)
    proj.project(incremental=False)
    return proj.load_graph()


def _hub_graph(canon: Canon):
    # a hub with two distinct outgoing relations -> two candidate facets unless k clamps it.
    return _ranked(canon, [("hub", "x"), ("hub", "y")],
                   rels={("hub", "x"): "bridges", ("hub", "y"): "grounds"})


# --------------------------------------------------------------------------- M5: collapse target guards


def test_collapse_on_community_less_target_does_not_sweep_danglers():
    """review-M5: a collapse target sitting in no community (the -1 sentinel — e.g. a dangling,
    attribute-less node) must NOT sweep every other community-less node into one bogus compression."""
    G = nx.MultiDiGraph()
    for n in ("ghost-x", "ghost-y", "ghost-z"):
        G.add_node(n, community=-1, degree=0, specificity=1.0)
    assert _resolve_cluster(G, "ghost-x", None) == []          # not [ghost-x, ghost-y, ghost-z]
    payload, msg = collapse_payload(G, target="ghost-x")
    assert payload is None and "at least 2 members" in msg


def test_collapse_missing_target_is_signalled_not_auto_picked():
    """review-low: an explicit target absent from the graph is a clear error, not a silent collapse of
    the largest community as though no target had been given."""
    G = nx.MultiDiGraph()
    G.add_node("a", community=0)
    G.add_node("b", community=0)
    G.add_node("c", community=0)
    payload, msg = collapse_payload(G, target="not-a-node")
    assert payload is None and "not in the graph" in msg


def test_explode_missing_target_is_signalled_not_auto_picked():
    """review (explode/collapse parity): an explicit explode target absent from the graph is a caller
    error (typo / stale id), not a request to explode the default max-degree hub. Mirrors
    collapse_payload's discipline so a missing target is surfaced, not silently substituted."""
    G = nx.MultiDiGraph()
    G.add_node("a", community=0, degree=5)
    G.add_node("b", community=0, degree=1)
    payload, msg = explode_payload(G, target="not-a-node")
    assert payload is None and "not in the graph" in msg
    # with NO target the hub fallback still applies (the default "explode the hub" behavior is intact)
    payload, t = explode_payload(G, target=None)
    assert payload is not None and t == "a"


def test_collapse_dedups_explicit_members():
    """review-low: duplicate explicit members collapse to one, so ['a','a'] does not pass the >=2 guard
    as a fake two-member cluster."""
    G = nx.MultiDiGraph()
    G.add_node("a", community=0)
    assert _resolve_cluster(G, None, ["a", "a"]) == ["a"]
    payload, msg = collapse_payload(G, members=["a", "a"])
    assert payload is None and "at least 2 members" in msg


# --------------------------------------------------------------------------- F32: explode k clamping


def test_explode_k_zero_yields_zero_facets(canon: Canon):
    G = _hub_graph(canon)
    payload, t = explode_payload(G, target="hub", k=0)
    assert t == "hub"
    # k=0 is an explicit "zero facets" request, NOT a falsy "no limit" — honoured exactly.
    assert payload["nodes"] == [] and payload["edges"] == []


def test_explode_negative_k_does_not_slice_from_end(canon: Canon):
    G = _hub_graph(canon)
    # negative k previously sliced facets[:-1], silently dropping the LAST facet; now clamps to 0.
    payload, _ = explode_payload(G, target="hub", k=-1)
    assert payload["nodes"] == [] and payload["edges"] == []


def test_explode_k_none_is_no_limit(canon: Canon):
    # the default path: k=None must keep every facet (the perf/clamp fix must not change this).
    G = _hub_graph(canon)
    payload, _ = explode_payload(G, target="hub", k=None)
    assert len(payload["nodes"]) == 2 and len(payload["edges"]) == 2


def test_explode_positive_k_still_clamps(canon: Canon):
    # k=1 keeps exactly one facet (positive clamp unchanged by the fix).
    G = _hub_graph(canon)
    payload, _ = explode_payload(G, target="hub", k=1)
    assert len(payload["nodes"]) == 1 and len(payload["edges"]) == 1


# --------------------------------------------------------------------------- F30: transplant sameness


def _transplant_edges():
    # two triangles joined by one edge -> two communities, the classic transplant fixture.
    return [("a1", "a2"), ("a2", "a3"), ("a1", "a3"),
            ("b1", "b2"), ("b2", "b3"), ("b1", "b3"), ("a1", "b1")]


def test_transplant_output_unchanged_and_deterministic(canon: Canon):
    # the perf hoist (to_undirected once, loop-invariant absorption) must be byte-for-byte identical:
    # running twice over the same graph yields the same candidate dicts in the same order.
    G = _ranked(canon, _transplant_edges())
    run1 = [c.to_dict() for c in gen.transplant(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)]
    run2 = [c.to_dict() for c in gen.transplant(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)]
    assert run1  # the fixture actually produces candidates
    assert run1 == run2  # deterministic, including the scores that used the (now hoisted) absorption


def test_transplant_score_uses_loop_invariant_absorption(canon: Canon):
    # every candidate's score is hub-degree * absorption(best_members); since absorption is loop-
    # invariant, all candidates from one call must share the same absorption factor -> score / degree
    # is constant across them. Guards against accidentally recomputing a per-candidate value.
    G = _ranked(canon, _transplant_edges())
    cands = gen.transplant(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert len(cands) >= 1
    hub_degrees = {c.source for c in cands}
    assert len(hub_degrees) == 1  # one hub
    hub = next(iter(hub_degrees))
    deg = float(G.nodes[hub].get("degree", 0))
    assert deg > 0
    factors = {round(c.score / deg, 9) for c in cands}
    assert len(factors) == 1  # a single shared absorption factor across all candidates
