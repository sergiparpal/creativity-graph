"""Generative layer (PLAN Stage 3): deterministic generators emit well-formed hypothesized
Candidates, generality-controlled, never colliding with failure memory.
"""
from __future__ import annotations

import itertools

from kg_engine import generate as gen
from kg_engine.canon import Canon
from kg_engine.generate import ALL_SET, Candidate
from kg_engine.model import Edge, Node, edge_id
from kg_engine.projector import Projector

# default-resolution-merges-all corpus (uniform), unless a test supplies a differentiating one
_UNIFORM = "## a\n## b\n## c"


def _ranked(canon: Canon, edges, *, corpus=_UNIFORM, node_type="claim", labels=None):
    """Write a graph into the canon and return the projected, rank-attributed in-memory graph."""
    labels = labels or {}
    nm: dict = {}
    for s, t in edges:
        nm.setdefault(s, []).append(Edge(source=s, target=t, relation="bridges", span="x"))
        nm.setdefault(t, [])
    nodes = [Node(id=k, label=labels.get(k, k), node_type=node_type, edges=v) for k, v in nm.items()]
    canon.write_nodes(nodes, message="seed generate fixture")
    proj = Projector(canon, source_text=corpus)
    proj.project(incremental=False)
    return proj.load_graph()


def _well_formed(cands, mechanism=None):
    for c in cands:
        assert isinstance(c, Candidate)
        assert c.kind in ("edge", "node")
        d = c.to_dict()
        assert "span" not in d and "provenance" not in d  # proposals, not text claims
        assert c.mechanism in ALL_SET
        if mechanism:
            assert c.mechanism == mechanism
        assert c.rationale and c.section


# --------------------------------------------------------------------------- bridge (§2/§4)


def test_bridge_proposes_cross_community_nonadjacent(canon: Canon):
    # two triangles joined by one edge => two communities; bridge proposes the missing cross links
    edges = [("a1", "a2"), ("a2", "a3"), ("a1", "a3"),
             ("b1", "b2"), ("b2", "b3"), ("b1", "b3"), ("a1", "b1")]
    G = _ranked(canon, edges)
    cands = gen.bridge(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert cands
    _well_formed(cands, "bridge")
    for c in cands:
        assert c.relation == "bridges"
        assert G.nodes[c.source].get("community") != G.nodes[c.target].get("community")
        assert c.target not in {v for _, v in G.out_edges(c.source)}  # non-adjacent


# --------------------------------------------------------------------------- seed (§3 residual)


def test_seed_ranks_abnormally_connectable_above_trivial(canon: Canon):
    # a star (every leaf pair is d=2 sharing exactly the hub -> trivial) + a K_{2,3} gadget where p,q
    # share THREE neighbours at d=2 -> abnormally connectable for its distance.
    star = [("h", "l1"), ("h", "l2"), ("h", "l3"), ("h", "l4"), ("h", "l5")]
    k23 = [("p", "m1"), ("p", "m2"), ("p", "m3"), ("q", "m1"), ("q", "m2"), ("q", "m3")]
    G = _ranked(canon, star + k23)
    cands = gen.seed(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert cands
    _well_formed(cands, "seed")
    top = cands[0]
    assert {top.source, top.target} == {"p", "q"}                  # the abnormal pair ranks first
    trivial = {frozenset((a, b)) for a, b in itertools.combinations(["l1", "l2", "l3"], 2)}
    got = {frozenset((c.source, c.target)) for c in cands}
    assert not (got & trivial)                                     # trivially-close pairs are dropped


# --------------------------------------------------------------------------- compression (§7)


def test_compression_mdl_and_specificity_screen(canon: Canon):
    # two disconnected dense K4 clusters; corpus makes cluster A's terms RARE (specific) and cluster B's
    # COMMON (vague). Both pass the MDL screen; only the specific cluster survives the specificity screen.
    A = list(itertools.combinations(["betweenness", "specificity", "reconciler", "falsification"], 2))
    B = list(itertools.combinations(["system", "idea", "thing", "notion"], 2))
    corpus = "\n".join(["## intro"]
                       + ["## s the system idea thing notion common system idea thing"] * 15
                       + ["## s betweenness specificity reconciler falsification rare technical terms"])
    G = _ranked(canon, A + B, corpus=corpus)
    cands = gen.compression(G, pack=None, corpus=[corpus], failures=set(), k=10)
    assert cands
    _well_formed(cands, "compression")
    for c in cands:
        assert c.kind == "node" and c.node_type == "compression" and c.label == ""  # named by the language layer
        # only the specific cluster: rationale names its rare members, not the vague ones
        assert "system" not in c.rationale and "betweenness" in c.rationale


def test_compression_rejects_sparse_cluster_no_mdl_saving(canon: Canon):
    # a 3-node PATH (2 internal edges) is too sparse to save bits via a star -> MDL screen rejects it
    G = _ranked(canon, [("x", "y"), ("y", "z")])
    cands = gen.compression(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert cands == []


# --------------------------------------------------------------------------- regroup (§8)


def test_regroup_surfaces_pair_invisible_under_prior_partition(canon: Canon):
    # K6 minus one edge (1,6): all six are ONE community at the stored resolution; (1,6) is the only
    # non-adjacent pair and it splits cross-community when re-partitioned at a higher resolution.
    nodes = [str(i) for i in range(1, 7)]
    edges = [p for p in itertools.combinations(nodes, 2) if p != ("1", "6")]
    G = _ranked(canon, edges)
    assert len({G.nodes[n].get("community") for n in nodes}) == 1     # one community at stored resolution
    cands = gen.regroup(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert cands
    _well_formed(cands, "regroup")
    assert {frozenset((c.source, c.target)) for c in cands} == {frozenset(("1", "6"))}


# --------------------------------------------------------------------------- transplant (§5)


def test_transplant_imports_hub_pattern_into_absorptive_community(canon: Canon):
    edges = [("a1", "a2"), ("a2", "a3"), ("a1", "a3"),
             ("b1", "b2"), ("b2", "b3"), ("b1", "b3"), ("a1", "b1")]
    G = _ranked(canon, edges)
    cands = gen.transplant(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert cands
    _well_formed(cands, "transplant")
    for c in cands:
        assert c.kind == "edge" and c.relation == "bridges"
        assert "hidden commitments to audit" in c.rationale


# --------------------------------------------------------------------------- ensemble (§9)


def test_ensemble_degrades_to_regroup_without_second_graph(canon: Canon):
    nodes = [str(i) for i in range(1, 7)]
    edges = [p for p in itertools.combinations(nodes, 2) if p != ("1", "6")]
    G = _ranked(canon, edges)
    cands = gen.ensemble(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10)
    assert cands
    _well_formed(cands, "ensemble")
    for c in cands:
        assert c.section == "§9"
        assert "degraded to regroup" in c.rationale


def test_ensemble_with_second_graph_surfaces_cross_construction_bridge(canon: Canon, tmp_path):
    import json

    import networkx as nx

    from kg_engine.generate import load_second_graph
    from kg_engine.projector import _node_link_data
    # our construction: a-b, b-c (so a and c are NON-adjacent here)
    G = _ranked(canon, [("a", "b"), ("b", "c")])
    # a SECOND construction where a-c IS adjacent — external structure our dynamics resisted
    g2 = nx.MultiDiGraph()
    for n in ("a", "b", "c"):
        g2.add_node(n, label=n)
    g2.add_edge("a", "b", key="e1")
    g2.add_edge("a", "c", key="e2")
    p = tmp_path / "graph2.json"
    p.write_text(json.dumps(_node_link_data(g2)))
    G2 = load_second_graph(str(p))
    cands = gen.ensemble(G, pack=None, corpus=[_UNIFORM], failures=set(), k=10, second_graph=G2)
    assert cands
    _well_formed(cands, "ensemble")
    assert any({c.source, c.target} == {"a", "c"} for c in cands)   # the cross-construction bridge
    for c in cands:
        assert c.section == "§9" and "exo bridge" in c.rationale


def test_kg_ensemble_graph_summary_and_missing(engine, tmp_path):
    import json

    import networkx as nx

    from kg_engine.projector import _node_link_data
    g = nx.MultiDiGraph()
    g.add_edge("x", "y", key="e1")
    p = tmp_path / "g.json"
    p.write_text(json.dumps(_node_link_data(g)))
    out = engine.kg_ensemble_graph(str(p))
    assert out["ok"] and out["nodes"] == 2 and out["edges"] == 1
    miss = engine.kg_ensemble_graph(str(tmp_path / "nope.json"))
    assert miss["ok"] is False and "error" in miss


# --------------------------------------------------------------------------- invariant 5: failure memory


def test_generators_drop_failure_memory(canon: Canon):
    edges = [("a1", "a2"), ("a2", "a3"), ("a1", "a3"),
             ("b1", "b2"), ("b2", "b3"), ("b1", "b3"), ("a1", "b1")]
    G = _ranked(canon, edges)
    base = {frozenset((c.source, c.target)) for c in gen.bridge(G, pack=None, corpus=[_UNIFORM],
                                                               failures=set(), k=20)}
    assert frozenset(("a1", "b2")) in base or frozenset(("a2", "b1")) in base  # something to drop
    # seed the FORWARD identity of one candidate as failure memory; its reverse must drop too
    victim = next(iter(base))
    u, v = tuple(victim)
    failures = {edge_id(u, "bridges", v)}
    after = {frozenset((c.source, c.target)) for c in gen.bridge(G, pack=None, corpus=[_UNIFORM],
                                                                failures=failures, k=20)}
    assert victim not in after  # forward OR reverse collision with failure memory -> dropped


# --------------------------------------------------------------------------- the kg_generate tool


def _seed_engine_graph(engine):
    engine.kg_write({"edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "grounds the claims beneath it", "authored_by": "agent"},
        {"source": "betweenness", "target": "generality-confound", "relation": "confounded_by",
         "span": "Betweenness is confounded by the generality confound", "authored_by": "agent"},
        {"source": "specificity-weighted-betweenness", "target": "bridge", "relation": "reconciles_with",
         "span": "Specificity-weighted betweenness reconciles with the bridge intuition", "authored_by": "agent"},
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"},
        {"source": "entropy", "target": "arrow", "relation": "grounds",
         "span": "Entropy grounds the arrow of time", "authored_by": "agent"},
    ]})


def test_kg_generate_all_smoke_is_readonly(engine):
    _seed_engine_graph(engine)
    before = engine.kg_metrics()
    out = engine.kg_generate(mechanism="all", k=5)
    assert out["count"] >= 1
    assert out["gate_on"] in (0, 1)
    for c in out["candidates"]:
        assert "span" not in c and "provenance" not in c          # hypothesized proposals
        assert c["mechanism"] in ALL_SET
    # READ-ONLY: generation never writes the canon (the inversion — judge defensively, later)
    assert engine.kg_metrics() == before


def test_kg_generate_single_mechanism(engine):
    _seed_engine_graph(engine)
    out = engine.kg_generate(mechanism="bridge", k=3)
    assert out["mechanism"] == "bridge" and len(out["candidates"]) <= 3
    assert all(c["mechanism"] == "bridge" for c in out["candidates"])
