"""Leaf NetworkX helpers — version-robust node-link (de)serialization + a safe node-attr accessor.

`node_link_graph` / `_node_link_data` are the on-disk shape of the derived `graph.json` (§1.2). They
are shared by three modules: the projector *writes* the derived graph, while the harness (`specificity`,
`absorption`) and the generators (`load_second_graph`/ensemble) *reconstruct* a graph from a second
construction's JSON. They previously lived in `projector.py`, which forced `harness` and `generate` to
back-import the projector *inside* their functions — a deferred import whose only purpose was to dodge
the circular import a top-level import would raise. Hoisting them into this dependency-free leaf lets
projector, harness, and generate all import DOWNWARD, so the `projector <-> harness` (and
`generate -> projector`) cycles disappear and the documented import graph becomes honest.

Depends only on networkx + the stdlib; nothing in `kg_engine` is imported here, so this module can never
participate in a cycle. The two helpers wrap the `edges="links"` keyword whose default flipped across
NetworkX versions, so the engine's serialization stays byte-stable regardless of the installed version.
"""
from __future__ import annotations

import networkx as nx


def _node_link_data(G) -> dict:
    try:
        return nx.node_link_data(G, edges="links")
    except TypeError:
        d = nx.node_link_data(G)
        if "links" not in d and "edges" in d:
            d["links"] = d.pop("edges")
        return d


def node_link_graph(data: dict):
    try:
        return nx.node_link_graph(data, edges="links", directed=data.get("directed", True))
    except TypeError:
        # Symmetric with `_node_link_data`'s writer, which normalizes the on-disk edges key to "links".
        # The bare `node_link_graph` (no `edges=` param) reads the version-default key ("edges" on
        # 3.6.1), so rename "links" -> "edges" first; otherwise the edges would be silently dropped and
        # we would reconstruct an EDGELESS graph. Copy so the caller's dict is never mutated.
        d = dict(data)
        if "links" in d and "edges" not in d:
            d["edges"] = d.pop("links")
        return nx.node_link_graph(d, directed=d.get("directed", True))


def node_attr(G, n, key, default=None):
    """Safe read of a precomputed node-rank attribute off the in-memory derived graph (degree,
    community, specificity, spec_betweenness, ...). Shared by the generators and the §8 operations so the
    accessor has ONE home, instead of operations.py reaching across the module boundary into a generate.py
    private (it used to `from .generate import _attr`)."""
    return G.nodes.get(n, {}).get(key, default)
