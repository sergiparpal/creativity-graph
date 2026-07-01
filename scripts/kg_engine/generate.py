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

from .graphio import node_attr, node_link_graph
from .model import FAILURE_STATES, edge_id

# The edge epistemic_state string values the generators must treat as NON-live (§1.7). Generators build
# their adjacency/undirected topology over the SAME failure-EXCLUDED subgraph that node ranks were
# computed over (projector._live_subgraph): a `failed`/`rejected` edge is negative information — it must
# never seed a spurious shared-neighbour bridge/seed/periphery candidate or inflate absorption density.
_FAILURE_VALUES = {s.value for s in FAILURE_STATES}

# The mechanism vocabulary. The DEFAULT_SET is what `/kg-generate` runs unless the user opts into all
# six (PLAN Stage 6 non-blocking survey). "all" runs ALL_SET.
DEFAULT_SET = ["bridge", "seed", "compression"]
# `periphery` (§5, low-degree sources) is ALL-only: it deliberately stays OUT of DEFAULT_SET so the
# default `/kg-generate` slate — and every golden expectation built on it — is byte-identical, exactly
# like regroup/transplant/ensemble. "all" runs ALL_SET.
ALL_SET = ["bridge", "seed", "compression", "regroup", "transplant", "ensemble", "periphery"]

# The relation emitted by every structural-bridge mechanism (bridge/seed/regroup/ensemble). It MUST
# exist in pack.yaml edge_types or the candidate is QUARANTINED at the kg_write boundary; and within a
# mechanism the failure-memory check and the emit must use the same string or failure memory silently
# never matches. transplant is exempt — it emits the hub's dynamic dominant relation, not this constant.
BRIDGES_RELATION = "bridges"

# The community id meaning "no community" — the sentinel _attr default for a node's "community" attr.
NO_COMMUNITY = -1

# seed (§3) all-pairs BFS size gate (perf #8). At or below this node count `seed` runs the EXACT
# original code path (unbounded BFS + full O(V^2) pair scan) so small graphs stay byte-identical with
# the golden/reproducibility expectations. Above it, BFS is bounded by SEED_BFS_CUTOFF (see `seed`).
SEED_ALLPAIRS_MAX_NODES = 400
# The cutoff radius the seed model actually consumes. seed scores a pair by the residual of its
# common-neighbour count; a shared neighbour can exist only at distance ≤ 2, so pairs at distance ≥ 3
# always score 0 and are dropped. Radius 2 enumerates every pair seed can ever turn into a candidate.
SEED_BFS_CUTOFF = 2

# Distinct "argument not supplied" sentinel for the re-partition memo (perf #13). The contract (the
# authoritative statement; downstream sites point here): None is a legitimate `_repartition` return
# value (both community algorithms failed and it fell back to no usable partition), so None cannot
# double as "not supplied" — `_UNSET` carries that meaning and a real None still flows through.
_UNSET = object()


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
    convergence: int = 1  # advisory (§4): # of DISTINCT mechanisms that independently proposed this edge.
    # A RANKING PRIOR for the grounding queue — NEVER a score, NEVER a verdict, NEVER folded into
    # `score` or onto a canon edge (G3/G4). Set in run_generators' dedup pass; node candidates keep 1.

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# --------------------------------------------------------------------------- shared helpers


# The node-attr accessor now lives in the graphio leaf, so operations.py imports it there instead of
# reaching into this module's privates (it used to `from .generate import _attr`). Kept as a local alias
# for this module's own internal call sites.
_attr = node_attr


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


def _pair_specificity(G, a, b) -> float:
    """A candidate's specificity is the MIN of its two endpoints' specificity, defaulting to 1.0 — the
    §4 generality-control rule shared by every edge mechanism (no candidate ranks high for being generic)."""
    return min(float(_attr(G, a, "specificity", 1.0)), float(_attr(G, b, "specificity", 1.0)))


def _live_undirected(G):
    """Undirected projection with `failed`/`rejected` edges excluded (§1.7), mirroring
    projector._live_subgraph so generators walk the SAME live topology their node ranks were computed
    over. Nodes are all kept (an attacked hub whose edges are all refuted still appears at degree 0)."""
    live = nx.MultiDiGraph()
    live.add_nodes_from(G.nodes(data=True))
    live.add_edges_from((u, v, k, d) for u, v, k, d in G.edges(keys=True, data=True)
                        if d.get("epistemic_state") not in _FAILURE_VALUES)
    return live.to_undirected()


def _undirected_adjacency(G) -> dict:
    adj: dict = defaultdict(set)
    for u, v, st in G.edges(data="epistemic_state"):
        if u != v and st not in _FAILURE_VALUES:  # failure memory is not live topology (§1.7)
            adj[u].add(v)
            adj[v].add(u)
    return adj


def _internal_edge_count(und, members: set) -> int:
    """Count undirected internal edges among `members`, each counted exactly once (frozenset-dedup).
    Feeds compression's MDL screen and transplant's absorption-capacity density proxy."""
    return len({frozenset((u, v)) for u in members for v in und.neighbors(u)
                if v in members and u != v})


def _members_by_community(G) -> dict:
    """Group node ids by their stored community attr (NO_COMMUNITY default), as a defaultdict(list)."""
    by_comm: dict = defaultdict(list)
    for n in G.nodes():
        by_comm[_attr(G, n, "community", NO_COMMUNITY)].append(n)
    return by_comm


def _shared(G, und, adj):
    """Resolve the per-run shared structures (perf #12). On a full `kg_generate('all')` run the
    undirected projection of G and its adjacency map are rebuilt once in `run_generators` and threaded
    through; called standalone each mechanism passes None and builds them lazily here. The lazily-built
    values are byte-for-byte identical to the threaded ones — same `_live_undirected(G)` /
    `_undirected_adjacency(G)` constructors — so output never changes either way."""
    if und is None:
        und = _live_undirected(G)
    if adj is None:
        adj = _undirected_adjacency(G)
    return und, adj


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


# --------------------------------------------------------------------------- the mechanisms (six core + periphery §5)


def bridge(G, *, pack, corpus, failures, k=10, adj=None) -> list:
    """§2/§4 — generate FROM the bridges (Swanson literature-based discovery, structurally). Rank
    non-adjacent, cross-community node pairs by the combined bridging strength of their endpoints; the
    strength is generality-controlled (spec_betweenness when the gate is on, structural-bridge/degree
    otherwise). Connecting two strong-but-separate hubs creates a shortcut the graph lacked."""
    gate_on = _gate_on(G)
    if adj is None:
        adj = _undirected_adjacency(G)
    nodes = list(G.nodes())
    strength = {n: _bridge_strength(G, n, gate_on) for n in nodes}  # endpoint-only; precomputed once (perf)
    cands: list = []
    for i, u in enumerate(nodes):
        su = strength[u]
        if su <= 0:
            continue
        cu = _attr(G, u, "community", NO_COMMUNITY)
        for v in nodes[i + 1:]:
            if v in adj[u] or _attr(G, v, "community", NO_COMMUNITY) == cu:
                continue  # already connected, or same community (not a cross-community bridge)
            sv = strength[v]
            if sv <= 0:
                continue
            if _is_failure(failures, u, BRIDGES_RELATION, v):
                continue
            spec = _pair_specificity(G, u, v)
            score = su + sv
            cands.append(_edge_cand(
                "bridge", u, v, BRIDGES_RELATION, score=score, specificity=spec, section="§2/§4",
                rationale=(f"cross-community bridge: {_attr(G, u, 'label', u)} (c{cu}) ⇄ "
                           f"{_attr(G, v, 'label', v)} (c{_attr(G, v, 'community', NO_COMMUNITY)}); "
                           f"strength={score:.4f} via {'spec_betweenness' if gate_on else 'structural-bridge'}")))
    return _rank(cands, k)


def seed(G, *, pack, corpus, failures, k=10, und=None, adj=None) -> list:
    """§3 — the residual, not the product. For each non-adjacent connected pair compute graph distance
    d and a connectability proxy c (common-neighbour count). Fit E[c|d] as the mean c per distance, and
    score by the POSITIVE residual c - E[c|d] — "abnormally connectable for its distance." We do NOT
    multiply d×c (the source rejects that as double-counting one tension)."""
    und, adj = _shared(G, und, adj)
    nodes = list(G.nodes())
    # gather (u, v, d, c) for non-adjacent, connected pairs
    pairs: list = []
    by_dist: dict = defaultdict(list)
    # perf #8 — bound the all-pairs BFS ONLY above the size gate; small graphs keep the exact original
    # unbounded path (byte-identical golden output). cutoff=2 is exact, not an approximation: a model
    # candidate needs a POSITIVE residual c-E[c|d], and the proxy c is the common-neighbour count
    # (len(nbu & adj[v])). Two nodes share a common neighbour iff they are at distance ≤ 2, so every
    # pair at d ≥ 3 has c = 0; those pairs only contribute zeros to by_dist[d], giving E[c|d]=0 and
    # residual 0, which is dropped by the `residual <= 0` filter below. Thus no d ≥ 3 pair can ever
    # become a candidate, and refusing to enumerate them (cutoff=2) changes nothing the model consumes.
    cutoff = None if G.number_of_nodes() <= SEED_ALLPAIRS_MAX_NODES else SEED_BFS_CUTOFF
    for i, u in enumerate(nodes):
        try:
            dist = nx.single_source_shortest_path_length(und, u, cutoff=cutoff)
        except (nx.NetworkXError, nx.NodeNotFound):
            continue
        nbu = adj[u]
        for v in nodes[i + 1:]:
            if v in nbu:
                continue  # adjacent — nothing to seed
            d = dist.get(v)
            if d is None or d < 2:
                continue  # disconnected, beyond the cutoff radius, or already adjacent
            c = len(nbu & adj[v])  # common neighbours
            pairs.append([u, v, d, c])
            by_dist[d].append(c)
    exp = {d: (sum(cs) / len(cs)) for d, cs in by_dist.items()}  # E[c | d]
    cands: list = []
    for u, v, d, c in pairs:
        residual = c - exp.get(d, 0.0)
        if residual <= 0:
            continue  # only abnormally-connectable pairs (positive residual)
        if _is_failure(failures, u, BRIDGES_RELATION, v):
            continue
        spec = _pair_specificity(G, u, v)
        cands.append(_edge_cand(
            "seed", u, v, BRIDGES_RELATION, score=residual, specificity=spec, section="§3",
            rationale=(f"abnormally connectable for its distance: d={d}, shared neighbours={c} vs "
                       f"expected {exp.get(d, 0.0):.2f} (residual {residual:+.2f})")))
    return _rank(cands, k)


def compression(G, *, pack, corpus, failures, k=10, und=None) -> list:
    """§7 — new NODES, not new edges. Detect dense communities and propose a `compression` node that
    `collapses_into`-links the members, but only when an MDL screen shows a description-length SAVING
    (re-expressing the cluster's internal edges as a star through one new node costs fewer bits) AND the
    cluster is not vague (mean member specificity ≥ the graph-wide mean specificity). The label is left BLANK for the
    language layer (Stage 6) to name. Members are carried in the rationale."""
    if und is None:
        und = _live_undirected(G)
    mean_spec = _mean_specificity(G)
    by_comm = _members_by_community(G)
    N = max(G.number_of_nodes(), 2)
    bits = math.log2(N)
    cands: list = []
    for cid, members in by_comm.items():
        if cid == NO_COMMUNITY or len(members) < 3:
            continue
        mset = set(members)
        e_int, m = _internal_edge_count(und, mset), len(members)
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


def regroup(G, *, pack, corpus, failures, k=10, und=None, adj=None, new_comm=_UNSET) -> list:
    """§8 — re-partition surfaces invisible bridges. Re-run Leiden at a DIFFERENT resolution and diff
    the community assignment against the stored one; any non-adjacent pair that was intra-community
    before but becomes cross-community under the new partition is a bridge that "was invisible under the
    prior partition." This is the generative use of the freedom of resolution."""
    if und is None:
        und = _live_undirected(G)
    # perf #13 — the Leiden re-partition is the expensive step. On a full `kg_generate('all')` run it
    # is computed once in `run_generators` and threaded in (regroup AND ensemble's degraded path share
    # it), so Leiden runs once per run instead of twice. `_UNSET` vs None: see the sentinel def — a real
    # None (both algorithms failed) is supplied and must flow through; `_UNSET` means "not supplied".
    if new_comm is _UNSET:
        new_comm = _repartition(und, resolution=4.0, seed=7)
    # _repartition returns None ONLY when BOTH community algorithms failed and it fell back to the
    # identity (all-nodes-its-own-community) partition — a non-partition that would make EVERY
    # intra-community pair "split apart", exploding into an O(n^2) slate of meaningless candidates
    # (review-M6). A legitimate high-resolution partition that happens to be all-singletons is a real
    # dict and still flows through (that is how regroup surfaces bridges on small graphs).
    if new_comm is None:
        return []
    if adj is None:
        adj = _undirected_adjacency(G)
    gate_on = _gate_on(G)
    nodes = list(G.nodes())
    strength = {n: _bridge_strength(G, n, gate_on) for n in nodes}  # endpoint-only; precomputed once (perf)
    cands: list = []
    for i, u in enumerate(nodes):
        ou, nu = _attr(G, u, "community", NO_COMMUNITY), new_comm.get(u)
        for v in nodes[i + 1:]:
            if v in adj[u]:
                continue
            # intra-community BEFORE (same stored community), cross-community AFTER (new partition splits)
            if ou == NO_COMMUNITY or _attr(G, v, "community", NO_COMMUNITY) != ou:
                continue
            if nu is None or new_comm.get(v) == nu:
                continue
            if _is_failure(failures, u, BRIDGES_RELATION, v):
                continue
            su = strength[u]
            sv = strength[v]
            spec = _pair_specificity(G, u, v)
            cands.append(_edge_cand(
                "regroup", u, v, BRIDGES_RELATION, score=(su + sv) + 1e-3, specificity=spec, section="§8",
                rationale=(f"invisible under the prior partition: {_attr(G, u, 'label', u)} and "
                           f"{_attr(G, v, 'label', v)} share community c{ou} at the stored resolution but "
                           f"split apart when re-partitioned")))
    return _rank(cands, k)


def transplant(G, *, pack, corpus, failures, k=10, und=None, adj=None) -> list:
    """§5 — hubs as macro-bridges. Take a high-degree hub from one community and import its reorganising
    pattern (its dominant outgoing relation) into the community with the highest ABSORPTION CAPACITY
    (proxy: high mean specificity, low density). Transfer is asymmetric — we transplant INTO the
    absorptive community and flag the reverse as risky. The rationale names the hub's hidden commitments
    to audit (the language layer expands them in Stage 6)."""
    if G.number_of_nodes() < 4:
        return []
    if und is None:
        und = _live_undirected(G)  # hoisted once: absorption() reads its neighbours m times per call (perf)
    by_comm = _members_by_community(G)
    if len([c for c in by_comm if c != NO_COMMUNITY]) < 2:
        return []
    # the hub: highest-degree node in a real community
    hub = max((n for n in G.nodes() if _attr(G, n, "community", NO_COMMUNITY) != NO_COMMUNITY),
              key=lambda n: (float(_attr(G, n, "degree", 0)), n), default=None)
    if hub is None:
        return []
    hub_comm = _attr(G, hub, "community", NO_COMMUNITY)
    # the hub's dominant outgoing relation (its reorganising pattern). Blank/missing relations are
    # dropped first: a relation='' candidate is always QUARANTINED at the boundary (never in pack
    # edge_types), so emitting it only wastes candidate/k budget and prints a nonsensical rationale. A
    # hub whose out-edges are all blank is treated like a hub with no out-edges (the `if not …` guard).
    rel_counts: dict = defaultdict(int)
    for _, _, data in G.out_edges(hub, data=True):
        rel = data.get("relation", "")
        if rel:
            rel_counts[rel] += 1
    if not rel_counts:
        return []
    dominant_relation = max(sorted(rel_counts), key=lambda r: rel_counts[r])
    # absorption capacity per other community: mean specificity / density
    def absorption(members):
        m = len(members)
        if m < 1:
            return 0.0
        member_set = set(members)
        e_int = _internal_edge_count(und, member_set)
        density = (e_int / (m * (m - 1) / 2)) if m > 1 else 1.0
        mean_spec = sum(float(_attr(G, u, "specificity", 1.0)) for u in members) / m
        return mean_spec / (density + 1e-6)
    targets = [(c, members) for c, members in by_comm.items() if c != hub_comm and c != NO_COMMUNITY]
    if not targets:
        return []
    best_c, best_members = max(targets, key=lambda c_members: (absorption(c_members[1]), c_members[0]))
    best_absorption = absorption(best_members)  # loop-invariant: compute once, reuse per candidate (perf)
    if adj is None:
        adj = _undirected_adjacency(G)
    cands: list = []
    for target_node in sorted(best_members, key=lambda n: (-float(_attr(G, n, "degree", 0)), n)):
        if target_node == hub or target_node in adj[hub]:
            continue
        if _is_failure(failures, hub, dominant_relation, target_node):
            continue
        spec = _pair_specificity(G, hub, target_node)
        # hub.degree * best_absorption is loop-INVARIANT across targets, so on its own every candidate
        # ties and _rank collapses to target-id order — discarding the degree-desc intent the emit order
        # already encodes. Fold in a per-target signal (the target's own live degree, +1 so a degree-0
        # target still scores) so a higher-degree target genuinely outranks a lower-degree one (review).
        score = float(_attr(G, hub, "degree", 0)) * best_absorption * (float(_attr(G, target_node, "degree", 0)) + 1.0)
        cands.append(_edge_cand(
            "transplant", hub, target_node, dominant_relation, score=score, specificity=spec, section="§5",
            rationale=(f"macro-bridge: transplant hub '{_attr(G, hub, 'label', hub)}' (c{hub_comm}, "
                       f"degree {int(_attr(G, hub, 'degree', 0))}) reorganising relation '{dominant_relation}' into "
                       f"the more absorptive community c{best_c}; hidden commitments to audit: does "
                       f"'{_attr(G, target_node, 'label', target_node)}' actually admit a '{dominant_relation}' the way the hub's targets do?")))
    return _rank(cands, k)


def ensemble(G, *, pack, corpus, failures, k=10, second_graph=None, und=None, adj=None,
             new_comm=_UNSET) -> list:
    """§9 — exo: cross constructions. Given an optional SECOND construction (a second derived graph from
    a different pack/resolution), emit candidate bridges that exist in one construction's structure but
    not the other's — the bridges the graph's own dynamics would resist. With only one construction
    available, this degrades to `regroup` (the internal analogue), tagged so the slate stays honest."""
    if second_graph is None:
        # perf #13 — pass the shared undirected graph/adjacency and the run's memoized re-partition
        # through to the degraded regroup so Leiden is not recomputed (already run by regroup on 'all').
        out = regroup(G, pack=pack, corpus=corpus, failures=failures, k=k, und=und, adj=adj,
                      new_comm=new_comm)
        for c in out:
            c.mechanism = "ensemble"
            c.section = "§9"
            c.rationale = "no second construction supplied — degraded to regroup: " + c.rationale
        return out
    adj1 = adj if adj is not None else _undirected_adjacency(G)
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
            if _is_failure(failures, u, BRIDGES_RELATION, v):
                continue
            spec = _pair_specificity(G, u, v)
            cands.append(_edge_cand(
                "ensemble", u, v, BRIDGES_RELATION, score=1.0, specificity=spec, section="§9",
                rationale=(f"exo bridge: {_attr(G, u, 'label', u)} ⇄ {_attr(G, v, 'label', v)} is adjacent "
                           f"in the SECOND construction but absent here — external structure our own "
                           f"dynamics resisted (perturbation=external)")))
    return _rank(cands, k)


def periphery(G, *, pack, corpus, failures, k=10, adj=None) -> list:
    """§5 — explore the periphery. Source candidates from LOW-degree nodes (the periphery the
    hub-seeking mechanisms ignore): for each peripheral node, propose a `bridges` edge to a
    non-adjacent anchor that maximises connectability (shared-neighbour count), specificity-
    controlled. Distinct from transplant (hubs) and seed (any residual-rich pair): here the SOURCE
    is deliberately low-degree, surfacing hypotheses that challenge the dense centre's status quo.

    The peripheral band is ADAPTIVE (Stage-1 survey): the bottom quartile of the live degree
    distribution — nodes whose precomputed `degree` is > 0 (degree-0 orphans are surfaced by
    kg_agenda, not re-proposed here) and <= the 25th-percentile degree, computed by the nearest-rank
    rule (no interpolation) so the threshold is byte-stable across repeated runs over the same graph
    (G6). Reuses BRIDGES_RELATION (an existing pack edge_type) — never a new type."""
    # periphery only needs the undirected ADJACENCY (it never walks `und`); resolve adj like `bridge`
    # does, so a standalone single-mechanism run pays no wasted `G.to_undirected()` build.
    if adj is None:
        adj = _undirected_adjacency(G)
    nodes = list(G.nodes())
    degree = {n: int(_attr(G, n, "degree", 0)) for n in nodes}
    # the live degree distribution over CONNECTED nodes (degree-0 orphans excluded — kg_agenda surfaces
    # those; a peripheral SOURCE with no neighbours has no shared-neighbour anchor anyway).
    deg_values = sorted(d for d in degree.values() if d > 0)
    if not deg_values:
        return []
    # adaptive 25th-percentile threshold via the nearest-rank rule: deterministic, no interpolation,
    # corpus-size-independent. p_idx is 0-based, so an all-equal-degree distribution yields its single
    # degree as the threshold (every connected node is then in-band — honest for a flat graph).
    p_idx = max(0, math.ceil(0.25 * len(deg_values)) - 1)
    threshold = deg_values[p_idx]
    peripheral = sorted(n for n in nodes if 0 < degree[n] <= threshold)
    cands: list = []
    for u in peripheral:
        nbu = adj[u]
        # the best non-adjacent connectable anchor: most shared neighbours, then the MORE SPECIFIC
        # anchor, then the smaller id (the deterministic tie rule, §4 generality-control / G6). A
        # failed/rejected pair (forward OR reverse) is excluded from anchor candidacy so failure memory
        # never re-proposes what was refuted (§13).
        options = []
        for v in nodes:
            if v == u or v in nbu:
                continue
            shared = len(nbu & adj[v])
            if shared <= 0:
                continue  # not connectable — no shared neighbour, nothing to bridge through
            if _is_failure(failures, u, BRIDGES_RELATION, v):
                continue
            options.append((shared, float(_attr(G, v, "specificity", 1.0)), v))
        if not options:
            continue  # no non-adjacent connectable anchor — skip this peripheral source
        shared, _spec_v, v = min(options, key=lambda t: (-t[0], -t[1], t[2]))
        deg_u = degree[u]
        # score = peripherality (low source degree) × connectability (shared-neighbour count): monotone
        # in BOTH, never collapsed with specificity (which travels in its own field — §4 / G4).
        score = (1.0 / (deg_u + 1)) * (shared + 1)
        spec = _pair_specificity(G, u, v)
        cands.append(_edge_cand(
            "periphery", u, v, BRIDGES_RELATION, score=score, specificity=spec, section="§5",
            rationale=(f"periphery bridge: low-degree source '{_attr(G, u, 'label', u)}' (degree {deg_u}, "
                       f"bottom-quartile ≤ {threshold}) ⇄ '{_attr(G, v, 'label', v)}' sharing {shared} "
                       f"neighbour(s) — sourced FROM the periphery the hub-seeking mechanisms ignore, "
                       f"NOT a hub transplant")))
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
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return node_link_graph(data)


# --------------------------------------------------------------------------- dispatch


_DISPATCH = {"bridge": bridge, "bridges": bridge, "seed": seed, "compression": compression,
             "regroup": regroup, "transplant": transplant, "ensemble": ensemble, "periphery": periphery}


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

    # perf #12 — build the undirected projection + adjacency ONCE per run and thread them into every
    # mechanism (they are otherwise rebuilt 5-6× on an 'all' run). The threaded values are identical to
    # what each mechanism would build itself (same constructors), so output is unchanged. Built lazily
    # (only when a mechanism in this run actually needs them) so a single-mechanism run pays no extra.
    _shared_und = None
    _shared_adj = None

    def und_of():
        nonlocal _shared_und
        if _shared_und is None:
            _shared_und = _live_undirected(G)
        return _shared_und

    def adj_of():
        nonlocal _shared_adj
        if _shared_adj is None:
            _shared_adj = _undirected_adjacency(G)
        return _shared_adj

    # perf #13 — memoize the §8 Leiden re-partition for the run: regroup and ensemble's degraded path
    # both consume it, so on an 'all' run it is computed once instead of twice. Computed lazily; here
    # `_UNSET` means "not yet computed", distinct from a real None result (see the sentinel def).
    _shared_repart = _UNSET

    def repart_of():
        nonlocal _shared_repart
        if _shared_repart is _UNSET:
            _shared_repart = _repartition(und_of(), resolution=4.0, seed=7)
        return _shared_repart

    # Run every mechanism at a k large enough that NOTHING is truncated (its full pre-truncation
    # candidate list), then surface only that mechanism's own top-k. `_BIG_K` >= any mechanism's
    # candidate count (edge mechanisms are O(V^2); node/periphery/transplant are O(V)). Slicing a
    # full ranked list to k is byte-identical to calling the mechanism with k (_rank sorts then
    # truncates), so the SURFACED slate is unchanged — but the retained full lists let the convergence
    # tally (below) count a pair proposed by two mechanisms even when it fell off ONE mechanism's top-k
    # (review: convergence undercount).
    _BIG_K = G.number_of_nodes() ** 2 + G.number_of_nodes() + 1
    out: list = []
    full: list = []  # every mechanism's PRE-truncation candidate list — feeds the convergence tally only
    for m in mechs:
        # Each branch mirrors that mechanism's keyword-only signature, threading only the lazy thunks it
        # accepts (perf #12/#13). The ladder covers all six distinct _DISPATCH functions exhaustively.
        fn = _DISPATCH[m]
        if fn is bridge:
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, adj=adj_of())
        elif fn is seed:
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, und=und_of(), adj=adj_of())
        elif fn is compression:
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, und=und_of())
        elif fn is regroup:
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, und=und_of(),
                          adj=adj_of(), new_comm=repart_of())
        elif fn is transplant:
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, und=und_of(), adj=adj_of())
        elif fn is ensemble:
            # Only force the (lazy) re-partition for the degraded path; with a real second_graph
            # ensemble takes the exo path and never consults new_comm, so leave it unforced (_UNSET).
            ens_repart = repart_of() if second_graph is None else _UNSET
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, second_graph=second_graph,
                          und=und_of(), adj=adj_of(), new_comm=ens_repart)
        elif fn is periphery:
            produced = fn(G, pack=pack, corpus=corpus, failures=failures, k=_BIG_K, adj=adj_of())
        else:  # unreachable — the ladder is exhaustive over _DISPATCH; defensive only
            produced = []
        full += produced
        out += produced[: max(0, int(k))]  # the SURFACED slate: this mechanism's own top-k (unchanged)
    # Dedup EDGE candidates across mechanisms by (source, target, relation) — the triple the canonical
    # edge_id derives from (review-low): with `second_graph=None`, ensemble degrades to regroup and
    # re-emits the SAME edges under a second mechanism name; other mechanisms can also independently
    # surface the same edge. Keep the FIRST occurrence (highest-priority mechanism by run order). NODE
    # candidates (e.g. compressions) are left untouched — their label is blank until Stage 6 names them,
    # so they carry no stable identity to dedup on and must not be collapsed by an empty-label key.
    #
    # BRIDGES_RELATION candidates (bridge/seed/regroup/ensemble) are treated as semantically symmetric
    # throughout this layer (the dual-orientation _is_failure check, ensemble's frozenset `seen`, the
    # hypothesized lane's check_reverse=True), so an orientation-swapped pair is a logical duplicate. The
    # ensemble exo path orients pairs by the SECOND construction's node order, which differs from G's, so
    # bridge can emit (a,b) while ensemble-exo emits (b,a) for the same undirected bridge — a directional
    # key would let both survive (review-M10). Key those orientation-independently; keep the directional
    # key for transplant, whose dominant_relation is genuinely directional.
    def _edge_key(c):
        # the SAME orientation-independent key the dedup uses (BRIDGES_RELATION is symmetric; the
        # directional key is kept for transplant's genuinely-directional dominant_relation).
        if c.relation == BRIDGES_RELATION:
            return (frozenset((c.source, c.target)), c.relation)
        return (c.source, c.target, c.relation)

    # convergence (§4 advisory): tally how many DISTINCT mechanisms independently proposed each edge key
    # BEFORE the dedup discards the duplicates' signal. The tally runs over `full` — each mechanism's
    # PRE-truncation candidate list — NOT the surfaced top-k slate: a pair proposed by two mechanisms but
    # dropped from one mechanism's own top-k would otherwise be undercounted (review). The
    # degraded-ensemble path (second_graph=None) re-emits regroup's edges under a SECOND name — but that
    # is the SAME construction, not a second independent one, so collapse `ensemble`→`regroup` for the
    # tally; otherwise the degrade path would spuriously inflate convergence to 2 on every regroup edge
    # (asserted in a test). This is computed purely from mechanism agreement on STRUCTURE — no text, no
    # span, no verdict — and only ever rides the response / queue ordering, never `score` and never a
    # canon edge (G3/G4).
    degraded_ensemble = second_graph is None
    def _conv_mech(c):
        return "regroup" if (degraded_ensemble and c.mechanism == "ensemble") else c.mechanism
    mech_by_key: dict = defaultdict(set)
    for c in full:
        if c.kind == "edge":
            mech_by_key[_edge_key(c)].add(_conv_mech(c))

    seen: set = set()
    deduped: list = []
    for c in out:
        if c.kind == "edge":
            key = _edge_key(c)
            if key in seen:
                continue
            seen.add(key)
            c.convergence = len(mech_by_key[key])  # >=1; node candidates keep the default 1 (no identity)
        deduped.append(c)
    return deduped
