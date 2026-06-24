"""The four endo operations (§8, PLAN Stage 4): open / collapse / explode / regroup.

Where the Stage-3 generators are READ-ONLY (they only propose), these operations *write* the canon —
but only ever through the **propose lane** (`kg_propose`), so everything they persist lands
`provenance=hypothesized`, `epistemic_state=unverified`, with **no span**. They never set a verdict
(only `kg_ground` can) and never forge text support. Each function is pure: given the rank-attributed
derived graph and parameters, it returns a propose-payload dict; `KGEngine.kg_operate` does the write.

  - collapse(subgraph) → a `compression` node + `collapses_into` edges from members to it (§7 made
    persistent: the inverse of a node).
  - explode(node)      → the node's latent sub-structure as hypothesized children that `collapses_into`
    it (the inverse of collapse).
  - regroup()          → persists the §8 re-partition's newly-visible bridges (Stage 3's `regroup`).
  - open() → primitive → a new `primitive` opening territory the current vocabulary cannot express,
    plus the structural attachment points (the language layer names it in Stage 6).
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

from .generate import regroup
from .graphio import node_attr
from .model import slug

HYP = "hypothesized"

# Per-operation default fan-out knobs. server.py's kg_operate imports these (`k or ops.DEFAULT_REGROUP_K`
# / `k or ops.DEFAULT_OPEN_POINTS`) instead of re-literalizing, so each default lives in exactly one place.
# The `k or` coercion is deliberate: k=0/None falls back to the default for regroup/open (explode honors
# k=0 as "zero facets" via its own _as_int path).
DEFAULT_REGROUP_K = 10
DEFAULT_OPEN_POINTS = 2

# Every *_payload below returns a 2-tuple (payload, info): `payload` is a propose-payload dict on
# success or None on failure, and `info` is the identifier/info string when payload is not None, else
# the failure reason. The caller (kg_operate) disambiguates the two meanings via `if not payload`.

# Sentinel marking an explicit collapse target that is not a node in the graph (vs. a target that is
# present but sits in no community, or simply yields too few members) — see _resolve_cluster.
_NO_SUCH_TARGET = object()


def _as_int(x, default=None):
    """Coerce LLM-supplied MCP `k` to int, falling back instead of raising on a non-numeric value
    (defense-in-depth: `k` is unvalidated tool input — review-low)."""
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _compression_id(members) -> str:
    h = hashlib.sha1("\x00".join(sorted(members)).encode("utf-8")).hexdigest()[:8]
    lead = slug(sorted(members)[0]) if members else "cluster"
    return f"compression-{lead}-{h}"


def _community_members(G, cid) -> list:
    return [n for n in G.nodes() if node_attr(G, n, "community", -1) == cid]


def _resolve_cluster(G, target, members):
    """The members to collapse: an explicit list (deduped, order-preserving), the community of a target
    node, or — with neither — the largest real community (≥2 members). A target that sits in no community
    (the -1 sentinel) yields NO members rather than silently auto-picking the largest community or
    sweeping every community-less dangler (review-M5 / missing-target). A target that is not in the graph
    returns the _NO_SUCH_TARGET sentinel so the single membership test lives here (the caller translates
    it into a specific error), not duplicated at the call site."""
    if members:
        seen: set = set()
        return [m for m in members if m in G and not (m in seen or seen.add(m))]
    if target is not None:
        if target not in G:
            return _NO_SUCH_TARGET
        cid = node_attr(G, target, "community", -1)
        return _community_members(G, cid) if cid != -1 else []
    by_comm: dict = defaultdict(list)
    for n in G.nodes():
        by_comm[node_attr(G, n, "community", -1)].append(n)
    real = [(c, ms) for c, ms in by_comm.items() if c != -1 and len(ms) >= 2]
    if not real:
        return []
    return max(real, key=lambda cm: (len(cm[1]), str(cm[0])))[1]


def collapse_payload(G, *, target=None, members=None, label="", body=""):
    members = _resolve_cluster(G, target, members)
    if members is _NO_SUCH_TARGET:
        # an explicit target that isn't a node is a caller error — signal it instead of silently
        # collapsing the largest community as if no target was given (review-low: missing-target)
        return None, f"collapse target {target!r} is not in the graph"
    if len(members) < 2:
        return None, "collapse needs at least 2 members"
    comp_id = _compression_id(members)
    body = body or ("compression standing in for the cluster {" + ", ".join(sorted(members))
                    + "}; earns its keep only if it predicts (§7)")
    node = {"id": comp_id, "label": label or "", "node_type": "compression",
            "provenance": HYP, "body": body}
    edges = [{"source": m, "target": comp_id, "relation": "collapses_into", "provenance": HYP}
             for m in sorted(members)]
    return {"nodes": [node], "edges": edges}, comp_id


def explode_payload(G, *, target=None, k=None, label="", body=""):
    node = target
    if node is None or node not in G:
        node = max(G.nodes(), key=lambda n: (float(node_attr(G, n, "degree", 0)), n), default=None)
    if node is None:
        return None, "no node to explode"
    rels = sorted({d.get("relation") for _, _, d in G.out_edges(node, data=True) if d.get("relation")})
    facets = rels or ["aspect-1", "aspect-2"]
    # k is unvalidated LLM-supplied MCP input: honour k=0 (zero facets, not "no limit") and guard
    # negatives (which would slice from the end), matching open_payload's max(1, int(k)) clamp discipline.
    # A non-numeric k coerces to None (no limit) rather than raising ValueError (review-low: int(k) guard).
    if k is not None:
        kk = _as_int(k)
        if kk is not None:
            facets = facets[: max(0, kk)]
    nodes, edges = [], []
    for i, r in enumerate(facets, 1):
        cid = f"{slug(node)}-facet-{i}"
        nodes.append({"id": cid, "label": label or "", "node_type": "primitive", "provenance": HYP,
                      "body": body or f"latent facet of '{node}' along its '{r}' role (§8 explode)"})
        # the child collapses_into the parent: the parent is the compression of its facets (inverse shape)
        edges.append({"source": cid, "target": node, "relation": "collapses_into", "provenance": HYP})
    return {"nodes": nodes, "edges": edges}, node


def regroup_payload(G, *, failures=None, k=DEFAULT_REGROUP_K):
    cands = regroup(G, pack=None, corpus=None, failures=failures or set(), k=k)
    if not cands:
        return None, "re-partition surfaced no invisible bridges"
    edges = [{"source": c.source, "target": c.target, "relation": c.relation, "provenance": HYP,
              "notes": c.rationale} for c in cands]
    return {"edges": edges}, f"{len(edges)} re-partition bridges"


def open_payload(G, *, label="", body="", k=DEFAULT_OPEN_POINTS):
    # attachment points: the highest-degree nodes — where the current vocabulary is most loaded and a
    # new primitive would most need to connect to open further territory (§8 open).
    pts = sorted(G.nodes(), key=lambda n: (-float(node_attr(G, n, "degree", 0)), n))[
        : max(1, _as_int(k, DEFAULT_OPEN_POINTS))]
    if not pts:
        return None, "empty graph — nothing to open against"
    prim_id = "opening-" + slug(pts[0])
    nodes = [{"id": prim_id, "label": label or "", "node_type": "primitive", "provenance": HYP,
              "body": body or "opens territory the current vocabulary cannot yet express (§8 open)"}]
    edges = [{"source": prim_id, "target": p, "relation": "bridges", "provenance": HYP} for p in pts]
    return {"nodes": nodes, "edges": edges}, prim_id


DISPATCH = {"collapse": collapse_payload, "explode": explode_payload,
            "regroup": regroup_payload, "open": open_payload}
