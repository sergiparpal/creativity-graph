"""The generative layer (PLAN_generative_layer §3): deterministic candidate generators.

This module holds **deterministic** candidate generators that read the derived graph + source +
pack and emit `hypothesized` candidates — *proposals from a discovery mechanism*, never text claims.
Each generator realises one mechanism from the source theory ("Conclusiones v6") and tags every
candidate with the § it implements.

The design contract (PLAN §1) every generator obeys:
  1. A candidate is `provenance=hypothesized`, `epistemic_state=unverified`, **with no span**. It is
     stored in a lane that can never be mistaken for grounded content.
  2. Generate offensively; judge defensively. Generation is NEVER gatekept by a quality metric — the
     existing grounding loop (`kg_ground`) is the post-hoc filter.
  3. Generality control travels with every generator (§4): structural rankings are
     specificity-weighted; compression candidates pass an MDL screen. No candidate ranks high merely
     for being generic.
  4. Failure memory binds generation (§13): a candidate whose `(source, relation, target)` (or its
     reverse) is already in `FAILURE_STATES` is dropped on sight.

Generators are **pure and read-only**: they never write the canon. The `/kg-generate` command (Stage
6) routes their output through the propose lane (`kg_propose`, Stage 1).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from .model import edge_id

# The mechanism vocabulary. The DEFAULT_SET is what `/kg-generate` runs unless the user opts into all
# six (PLAN Stage 6 non-blocking survey). "all" runs ALL_SET.
DEFAULT_SET = ["bridge", "seed", "compression"]
ALL_SET = ["bridge", "seed", "compression", "regroup", "transplant", "ensemble"]


@dataclass
class Candidate:
    """A single machine-proposed graph element, destined for the hypothesized write lane.

    `provenance` is always `hypothesized`, `epistemic_state` always `unverified`, and there is never
    a span — these are structural proposals, not text claims, so they are not carried on the dataclass
    (the propose lane forces them).
    """
    kind: str            # "edge" | "node"
    mechanism: str       # "bridge" | "seed" | "compression" | "regroup" | "transplant" | "ensemble"
    source: str = ""     # for edges
    target: str = ""     # for edges
    relation: str = ""   # for edges (a pack edge_type)
    label: str = ""      # for nodes (e.g. a proposed compression)
    node_type: str = ""  # for nodes (a pack node_type)
    score: float = 0.0
    specificity: float = 0.0
    rationale: str = ""
    section: str = ""    # the source-theory § the mechanism implements

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# --------------------------------------------------------------------------- shared helpers


def _attr(G, n, key, default):
    return G.nodes.get(n, {}).get(key, default)


def _gate_on(G) -> int:
    for n in G.nodes():
        return int(_attr(G, n, "gate_on", 0))
    return 0


def _mean_specificity(G) -> float:
    vals = [float(_attr(G, n, "specificity", 1.0)) for n in G.nodes()]
    return (sum(vals) / len(vals)) if vals else 1.0


def _bridge_strength(G, n, gate_on: int) -> float:
    """The endpoint's bridging strength under the generality control (PLAN §4). When the gate is ON,
    trust spec_betweenness (the confound-corrected metric); when OFF, fall back to the honest
    structural-bridge flag, tie-broken by degree — never raw betweenness, which the confound inflates."""
    if gate_on:
        return float(_attr(G, n, "spec_betweenness", 0.0))
    return float(_attr(G, n, "structural_bridge", 0)) + 1e-6 * float(_attr(G, n, "degree", 0))


def _undirected_adjacency(G) -> dict:
    adj: dict = defaultdict(set)
    for u, v in G.edges():
        if u != v:
            adj[u].add(v)
            adj[v].add(u)
    return adj


def _is_failure(failures: set, source: str, relation: str, target: str) -> bool:
    """invariant 5 (PLAN §13): a candidate edge whose identity OR its reverse already lives in failure
    memory is dropped on sight — generation never re-proposes what was refuted."""
    return (edge_id(source, relation, target) in failures
            or edge_id(target, relation, source) in failures)


def _rank(cands: list, k: int) -> list:
    """Deterministic ordering: stable sort by score DESC, tie-broken by a stable identity so repeated
    runs over the same graph are byte-identical (PLAN §"deterministic ordering")."""
    def key(c):
        ident = (c.source, c.relation, c.target, c.label, c.node_type)
        return (-c.score, ident)
    return sorted(cands, key=key)[: max(0, int(k))]


def _edge_cand(mechanism, source, target, relation, *, score, specificity, rationale, section):
    return Candidate(kind="edge", mechanism=mechanism, source=source, target=target, relation=relation,
                     score=round(float(score), 6), specificity=round(float(specificity), 4),
                     rationale=rationale, section=section)


# --------------------------------------------------------------------------- the six mechanisms


def bridge(G, *, pack, corpus, failures, k=10) -> list:
    """§2/§4 — generate FROM the bridges (Swanson literature-based discovery, structurally). Rank
    non-adjacent, cross-community node pairs by the combined bridging strength of their endpoints; the
    strength is generality-controlled (spec_betweenness when the gate is on, structural-bridge/degree
    otherwise). Connecting two strong-but-separate hubs creates a shortcut the graph lacked."""
    gate_on = _gate_on(G)
    adj = _undirected_adjacency(G)
    nodes = list(G.nodes())
    cands: list = []
    for i, u in enumerate(nodes):
        su = _bridge_strength(G, u, gate_on)
        if su <= 0:
            continue
        cu = _attr(G, u, "community", -1)
        for v in nodes[i + 1:]:
            if v in adj[u] or _attr(G, v, "community", -1) == cu:
                continue  # already connected, or same community (not a cross-community bridge)
            sv = _bridge_strength(G, v, gate_on)
            if sv <= 0:
                continue
            if _is_failure(failures, u, "bridges", v):
                continue
            spec = min(float(_attr(G, u, "specificity", 1.0)), float(_attr(G, v, "specificity", 1.0)))
            score = su + sv
            cands.append(_edge_cand(
                "bridge", u, v, "bridges", score=score, specificity=spec, section="§2/§4",
                rationale=(f"cross-community bridge: {_attr(G, u, 'label', u)} (c{cu}) ⇄ "
                           f"{_attr(G, v, 'label', v)} (c{_attr(G, v, 'community', -1)}); "
                           f"strength={score:.4f} via {'spec_betweenness' if gate_on else 'structural-bridge'}")))
    return _rank(cands, k)


def seed(G, *, pack, corpus, failures, k=10) -> list:
    """§3 — the residual, not the product. For each non-adjacent connected pair compute graph distance
    d and a connectability proxy c (common-neighbour count). Fit E[c|d] as the mean c per distance, and
    score by the POSITIVE residual c - E[c|d] — "abnormally connectable for its distance." We do NOT
    multiply d×c (the source rejects that as double-counting one tension)."""
    und = G.to_undirected()
    adj = _undirected_adjacency(G)
    nodes = list(G.nodes())
    # gather (u, v, d, c) for non-adjacent, connected pairs
    pairs: list = []
    by_dist: dict = defaultdict(list)
    for i, u in enumerate(nodes):
        try:
            dist = nx.single_source_shortest_path_length(und, u)
        except (nx.NetworkXError, nx.NodeNotFound):
            continue
        nbu = adj[u]
        for v in nodes[i + 1:]:
            if v in nbu:
                continue  # adjacent — nothing to seed
            d = dist.get(v)
            if d is None or d < 2:
                continue  # disconnected, or already adjacent
            c = len(nbu & adj[v])  # common neighbours
            pairs.append([u, v, d, c])
            by_dist[d].append(c)
    exp = {d: (sum(cs) / len(cs)) for d, cs in by_dist.items()}  # E[c | d]
    cands: list = []
    for u, v, d, c in pairs:
        residual = c - exp.get(d, 0.0)
        if residual <= 0:
            continue  # only abnormally-connectable pairs (positive residual)
        if _is_failure(failures, u, "bridges", v):
            continue
        spec = min(float(_attr(G, u, "specificity", 1.0)), float(_attr(G, v, "specificity", 1.0)))
        cands.append(_edge_cand(
            "seed", u, v, "bridges", score=residual, specificity=spec, section="§3",
            rationale=(f"abnormally connectable for its distance: d={d}, shared neighbours={c} vs "
                       f"expected {exp.get(d, 0.0):.2f} (residual {residual:+.2f})")))
    return _rank(cands, k)


def compression(G, *, pack, corpus, failures, k=10) -> list:
    """§7 — new NODES, not new edges. Detect dense communities and propose a `compression` node that
    `collapses_into`-links the members, but only when an MDL screen shows a description-length SAVING
    (re-expressing the cluster's internal edges as a star through one new node costs fewer bits) AND the
    cluster is not vague (mean member specificity ≥ the graph-wide mean specificity). The label is left BLANK for the
    language layer (Stage 6) to name. Members are carried in the rationale."""
    und = G.to_undirected()
    mean_spec = _mean_specificity(G)
    by_comm: dict = defaultdict(list)
    for n in G.nodes():
        by_comm[_attr(G, n, "community", -1)].append(n)
    N = max(G.number_of_nodes(), 2)
    bits = math.log2(N)
    cands: list = []
    for cid, members in by_comm.items():
        if cid == -1 or len(members) < 3:
            continue
        mset = set(members)
        internal = {frozenset((u, v)) for u in members for v in und.neighbors(u)
                    if v in mset and u != v}
        e_int, m = len(internal), len(members)
        direct = e_int * 2 * bits                 # each internal edge: two endpoint ids
        compressed = (m + 1) * bits               # m collapses_into edges + the one new node
        if compressed >= direct:
            continue  # MDL screen: no description-length saving — not a real compression
        member_spec = sum(float(_attr(G, u, "specificity", mean_spec)) for u in members) / m
        if member_spec < mean_spec:
            continue  # vague compression rejected (§4 — generality control)
        saved = direct - compressed
        cands.append(Candidate(
            kind="node", mechanism="compression", node_type="compression", label="",
            score=round(saved, 4), specificity=round(member_spec, 4), section="§7",
            rationale=(f"MDL: {e_int} internal edges among {m} members re-express as a star "
                       f"(saves ~{saved:.0f} bits); specificity {member_spec:.2f} ≥ graph mean "
                       f"{mean_spec:.2f}; collapses: {', '.join(sorted(members))}")))
    return _rank(cands, k)


def regroup(G, *, pack, corpus, failures, k=10) -> list:
    """§8 — re-partition surfaces invisible bridges. Re-run Leiden at a DIFFERENT resolution and diff
    the community assignment against the stored one; any non-adjacent pair that was intra-community
    before but becomes cross-community under the new partition is a bridge that "was invisible under the
    prior partition." This is the generative use of the freedom of resolution."""
    und = G.to_undirected()
    new_comm = _repartition(und, resolution=4.0, seed=7)
    # _repartition returns None ONLY when BOTH community algorithms failed and it fell back to the
    # identity (all-nodes-its-own-community) partition — a non-partition that would make EVERY
    # intra-community pair "split apart", exploding into an O(n^2) slate of meaningless candidates
    # (review-M6). A legitimate high-resolution partition that happens to be all-singletons is a real
    # dict and still flows through (that is how regroup surfaces bridges on small graphs).
    if new_comm is None:
        return []
    adj = _undirected_adjacency(G)
    gate_on = _gate_on(G)
    nodes = list(G.nodes())
    cands: list = []
    for i, u in enumerate(nodes):
        ou, nu = _attr(G, u, "community", -1), new_comm.get(u)
        for v in nodes[i + 1:]:
            if v in adj[u]:
                continue
            # intra-community BEFORE (same stored community), cross-community AFTER (new partition splits)
            if ou == -1 or _attr(G, v, "community", -1) != ou:
                continue
            if nu is None or new_comm.get(v) == nu:
                continue
            if _is_failure(failures, u, "bridges", v):
                continue
            su = _bridge_strength(G, u, gate_on)
            sv = _bridge_strength(G, v, gate_on)
            spec = min(float(_attr(G, u, "specificity", 1.0)), float(_attr(G, v, "specificity", 1.0)))
            cands.append(_edge_cand(
                "regroup", u, v, "bridges", score=(su + sv) + 1e-3, specificity=spec, section="§8",
                rationale=(f"invisible under the prior partition: {_attr(G, u, 'label', u)} and "
                           f"{_attr(G, v, 'label', v)} share community c{ou} at the stored resolution but "
                           f"split apart when re-partitioned")))
    return _rank(cands, k)


def transplant(G, *, pack, corpus, failures, k=10) -> list:
    """§5 — hubs as macro-bridges. Take a high-degree hub from one community and import its reorganising
    pattern (its dominant outgoing relation) into the community with the highest ABSORPTION CAPACITY
    (proxy: high mean specificity, low density). Transfer is asymmetric — we transplant INTO the
    absorptive community and flag the reverse as risky. The rationale names the hub's hidden commitments
    to audit (the language layer expands them in Stage 6)."""
    if G.number_of_nodes() < 4:
        return []
    und = G.to_undirected()  # hoisted once: absorption() reads its neighbours m times per call (perf)
    by_comm: dict = defaultdict(list)
    for n in G.nodes():
        by_comm[_attr(G, n, "community", -1)].append(n)
    if len([c for c in by_comm if c != -1]) < 2:
        return []
    # the hub: highest-degree node in a real community
    hub = max((n for n in G.nodes() if _attr(G, n, "community", -1) != -1),
              key=lambda n: (float(_attr(G, n, "degree", 0)), n), default=None)
    if hub is None:
        return []
    hub_comm = _attr(G, hub, "community", -1)
    # the hub's dominant outgoing relation (its reorganising pattern)
    rel_counts: dict = defaultdict(int)
    for _, _, data in G.out_edges(hub, data=True):
        rel_counts[data.get("relation", "")] += 1
    if not rel_counts:
        return []
    rstar = max(sorted(rel_counts), key=lambda r: rel_counts[r])
    # absorption capacity per other community: mean specificity / density
    def absorption(members):
        m = len(members)
        if m < 1:
            return 0.0
        ms = set(members)
        internal = {frozenset((u, v)) for u in members for v in und.neighbors(u)
                    if v in ms and u != v}
        density = (len(internal) / (m * (m - 1) / 2)) if m > 1 else 1.0
        mean_spec = sum(float(_attr(G, u, "specificity", 1.0)) for u in members) / m
        return mean_spec / (density + 1e-6)
    targets = [(c, members) for c, members in by_comm.items() if c != hub_comm and c != -1]
    if not targets:
        return []
    best_c, best_members = max(targets, key=lambda cm: (absorption(cm[1]), cm[0]))
    best_absorption = absorption(best_members)  # loop-invariant: compute once, reuse per candidate (perf)
    adj = _undirected_adjacency(G)
    cands: list = []
    for b in sorted(best_members, key=lambda n: (-float(_attr(G, n, "degree", 0)), n)):
        if b == hub or b in adj[hub]:
            continue
        if _is_failure(failures, hub, rstar, b):
            continue
        spec = min(float(_attr(G, hub, "specificity", 1.0)), float(_attr(G, b, "specificity", 1.0)))
        score = float(_attr(G, hub, "degree", 0)) * best_absorption
        cands.append(_edge_cand(
            "transplant", hub, b, rstar, score=score, specificity=spec, section="§5",
            rationale=(f"macro-bridge: transplant hub '{_attr(G, hub, 'label', hub)}' (c{hub_comm}, "
                       f"degree {int(_attr(G, hub, 'degree', 0))}) reorganising relation '{rstar}' into "
                       f"the more absorptive community c{best_c}; hidden commitments to audit: does "
                       f"'{_attr(G, b, 'label', b)}' actually admit a '{rstar}' the way the hub's targets do?")))
    return _rank(cands, k)


def ensemble(G, *, pack, corpus, failures, k=10, second_graph=None) -> list:
    """§9 — exo: cross constructions. Given an optional SECOND construction (a second derived graph from
    a different pack/resolution), emit candidate bridges that exist in one construction's structure but
    not the other's — the bridges the graph's own dynamics would resist. With only one construction
    available, this degrades to `regroup` (the internal analogue), tagged so the slate stays honest."""
    if second_graph is None:
        out = regroup(G, pack=pack, corpus=corpus, failures=failures, k=k)
        for c in out:
            c.mechanism = "ensemble"
            c.section = "§9"
            c.rationale = "no second construction supplied — degraded to regroup: " + c.rationale
        return out
    adj1 = _undirected_adjacency(G)
    own = set(G.nodes())
    adj2 = _undirected_adjacency(second_graph)
    cands: list = []
    seen: set = set()
    for u in second_graph.nodes():
        if u not in own:
            continue
        for v in adj2[u]:
            if v not in own or v in adj1[u]:
                continue  # endpoint absent from our construction, or already adjacent here
            key = frozenset((u, v))
            if key in seen:
                continue
            seen.add(key)
            if _is_failure(failures, u, "bridges", v):
                continue
            spec = min(float(_attr(G, u, "specificity", 1.0)), float(_attr(G, v, "specificity", 1.0)))
            cands.append(_edge_cand(
                "ensemble", u, v, "bridges", score=1.0, specificity=spec, section="§9",
                rationale=(f"exo bridge: {_attr(G, u, 'label', u)} ⇄ {_attr(G, v, 'label', v)} is adjacent "
                           f"in the SECOND construction but absent here — external structure our own "
                           f"dynamics resisted (perturbation=external)")))
    return _rank(cands, k)


# --------------------------------------------------------------------------- partition / loading helpers


def _repartition(und, resolution=4.0, seed=7) -> "dict | None":
    """Re-run community detection at a DIFFERENT resolution than the stored projection (§8). Higher
    resolution -> more, smaller communities -> intra-community pairs surface as cross-community. Falls
    back to a different-seed label propagation when leidenalg is unavailable."""
    if und.number_of_nodes() == 0:
        return {}
    try:
        import igraph as ig
        import leidenalg as la
        nodes = list(und.nodes())
        idx = {n: i for i, n in enumerate(nodes)}
        edges = [(idx[u], idx[v]) for u, v in und.edges()]
        g = ig.Graph(n=len(nodes), edges=edges, directed=False)
        part = la.find_partition(g, la.RBConfigurationVertexPartition, seed=seed,
                                 resolution_parameter=resolution)
        return {nodes[i]: m for i, m in enumerate(part.membership)}
    except Exception:  # noqa: BLE001 — degrade to a different-seed label propagation
        try:
            communities = nx.community.asyn_lpa_communities(und, seed=seed)
            return {n: ci for ci, com in enumerate(communities) for n in com}
        except Exception:  # noqa: BLE001 — BOTH algorithms failed: signal "no usable repartition"
            # Return None (not the identity {n: i} partition): the all-singleton identity makes every
            # intra-community pair appear to "split", which regroup would explode into O(n^2) noise.
            # The sentinel lets regroup short-circuit cleanly (review-M6).
            return None


def load_second_graph(path: str | Path) -> nx.MultiDiGraph:
    """Load a second construction's graph.json into a MultiDiGraph (PLAN Stage 7 — the ensemble path)."""
    from .projector import node_link_graph
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return node_link_graph(data)


# --------------------------------------------------------------------------- dispatch


_DISPATCH = {"bridge": bridge, "bridges": bridge, "seed": seed, "compression": compression,
             "regroup": regroup, "transplant": transplant, "ensemble": ensemble}


def run_generators(G, mechanism="bridge", *, pack=None, corpus=None, failures=None, k=10,
                   second_graph=None) -> list:
    """Dispatch to one mechanism, the default set, or all six. `mechanism="all"` runs ALL_SET;
    `mechanism="default"` runs DEFAULT_SET; an unknown name runs DEFAULT_SET (never raises)."""
    failures = failures or set()
    if mechanism == "all":
        mechs = list(ALL_SET)
    elif mechanism == "default":
        mechs = list(DEFAULT_SET)
    elif mechanism in _DISPATCH:
        mechs = [mechanism]
    else:
        mechs = list(DEFAULT_SET)
    out: list = []
    for m in mechs:
        fn = _DISPATCH[m]
        if fn is ensemble:
            out += fn(G, pack=pack, corpus=corpus, failures=failures, k=k, second_graph=second_graph)
        else:
            out += fn(G, pack=pack, corpus=corpus, failures=failures, k=k)
    # Dedup EDGE candidates across mechanisms by (source, target, relation) — the triple the canonical
    # edge_id derives from (review-low): with `second_graph=None`, ensemble degrades to regroup and
    # re-emits the SAME edges under a second mechanism name; other mechanisms can also independently
    # surface the same edge. Keep the FIRST occurrence (highest-priority mechanism by run order). NODE
    # candidates (e.g. compressions) are left untouched — their label is blank until Stage 6 names them,
    # so they carry no stable identity to dedup on and must not be collapsed by an empty-label key.
    seen: set = set()
    deduped: list = []
    for c in out:
        if c.kind == "edge":
            key = (c.source, c.target, c.relation)
            if key in seen:
                continue
            seen.add(key)
        deduped.append(c)
    return deduped
