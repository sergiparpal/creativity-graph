"""Regression tests for the merge_html_graphio fix group (rfix a10).

Covers three defects:
1. canonmerge.merge_note_files routed edgeless-but-parseable canon nodes to a raw git text merge,
   bypassing merge_nodes' node-level epistemic_state demote (a one-sided verdict was kept).
2. graphio.node_link_graph's TypeError fallback renamed the edges key "links"->"edges", which is
   backwards for the old networkx (3.0-3.3) that actually triggers the fallback (it expects "links").
3. export._render_data silently dropped edges whose endpoint had no nodes-table row while the legend
   count still included them; it must synthesize a placeholder node so links == drawable edges.
"""
from __future__ import annotations

import networkx as nx

from kg_engine import graphio
from kg_engine.canonmerge import merge_note_files
from kg_engine.export import _render_data
from kg_engine.model import EpistemicState, Node, node_from_markdown, node_to_markdown


# --------------------------------------------------------------------------- #1: edgeless node demote


def _edgeless(state: EpistemicState) -> str:
    return node_to_markdown(Node(id="n1", label="n1", body="prose", edges=[], epistemic_state=state))


def test_edgeless_node_one_sided_verdict_demoted_to_unverified():
    """An edgeless (but parseable) canon node with a one-sided grounded state must be demoted to
    unverified by merge_note_files — it must NOT be kept grounded via a raw text merge fallback."""
    ours = _edgeless(EpistemicState.GROUNDED)
    theirs = _edgeless(EpistemicState.UNVERIFIED)
    base = _edgeless(EpistemicState.UNVERIFIED)
    merged_text, _conflicts, ok = merge_note_files(base, ours, theirs)
    merged = node_from_markdown(merged_text)
    assert merged is not None
    # The buggy fallback path would have produced a clean git text merge keeping ours' GROUNDED state.
    assert merged.epistemic_state is EpistemicState.UNVERIFIED
    assert ok  # a verdict demotion is a CLEAN resolution, not a hard conflict


def test_edgeless_node_agreeing_state_preserved():
    """When both edgeless sides agree on a verdict, no forging concern arises and the state is kept."""
    ours = _edgeless(EpistemicState.GROUNDED)
    theirs = _edgeless(EpistemicState.GROUNDED)
    merged_text, _c, ok = merge_note_files(ours, ours, theirs)
    merged = node_from_markdown(merged_text)
    assert merged.epistemic_state is EpistemicState.GROUNDED
    assert ok


# --------------------------------------------------------------------------- #2: graphio round-trip


def test_node_link_round_trips_an_edge():
    """A serialized edge survives the on-disk shape round-trip on the installed networkx."""
    G = nx.MultiDiGraph()
    G.add_node("a")
    G.add_node("b")
    G.add_edge("a", "b", key="e_a__rel__b", relation="rel")
    data = graphio._node_link_data(G)
    G2 = graphio.node_link_graph(data)
    assert G2.number_of_edges() == 1
    assert ("a", "b") in {(u, v) for u, v, _k in G2.edges(keys=True)}


def test_fallback_passes_links_key_through_unrenamed(monkeypatch):
    """Simulate an OLD networkx that lacks the ``edges=`` kwarg and whose bare reader expects the edges
    under the "links" key. The fallback must pass "links" THROUGH (not rename it to "edges"), otherwise
    the old reader would see no edges and reconstruct an edgeless graph."""
    real = nx.node_link_graph

    def fake_node_link_graph(data, *args, **kwargs):
        # Old signature: no `edges=` kwarg -> raise TypeError to drive graphio into its fallback.
        if "edges" in kwargs:
            raise TypeError("node_link_graph() got an unexpected keyword argument 'edges'")
        # Old reader defaults to the "links" key; if the fallback wrongly renamed to "edges", this
        # would find zero edges and yield an edgeless graph.
        assert "links" in data and "edges" not in data, "fallback renamed the edges key backwards"
        H = nx.MultiDiGraph() if kwargs.get("directed", True) else nx.MultiGraph()
        for n in data.get("nodes", []):
            H.add_node(n["id"])
        for e in data.get("links", []):
            H.add_edge(e["source"], e["target"])
        return H

    monkeypatch.setattr(graphio.nx, "node_link_graph", fake_node_link_graph)

    G = nx.MultiDiGraph()
    G.add_node("a")
    G.add_node("b")
    G.add_edge("a", "b", key="e_a__rel__b", relation="rel")
    data = graphio._node_link_data(G)  # writes edges under "links"
    assert "links" in data
    G2 = graphio.node_link_graph(data)
    assert G2.number_of_edges() == 1


# --------------------------------------------------------------------------- #3: export placeholder node


def test_render_data_synthesizes_placeholder_for_dangling_target():
    """An edge whose target has no nodes-table row must get a synthesized placeholder node so the HTML
    renders the edge and links.length matches the drawable-edge count (legend count no longer diverges)."""
    model = {
        "nodes": [{"id": "a", "label": "A", "degree": 1, "provenance": "span-present",
                   "authored_by": "agent", "community": 0}],
        "edges": [{"id": "e_a__rel__b", "source": "a", "target": "b", "relation": "rel",
                   "epistemic_state": "grounded", "provenance": "span-present"}],
        "gate_on": 0,
    }
    data = _render_data(model)
    node_ids = {n["id"] for n in data["nodes"]}
    assert "b" in node_ids, "dangling target 'b' was not synthesized as a placeholder node"
    placeholder = next(n for n in data["nodes"] if n["id"] == "b")
    assert placeholder["degree"] == 0
    assert placeholder["provenance"] is None
    assert placeholder["authored_by"] is None
    # Every link is drawable (both endpoints resolve to a node), so links == edges count.
    drawable = [l for l in data["links"] if l["source"] in node_ids and l["target"] in node_ids]
    assert len(drawable) == len(data["links"]) == len(model["edges"])
    # nodes stay sorted by id (deterministic render).
    assert [n["id"] for n in data["nodes"]] == sorted(node_ids)


def test_render_data_no_placeholder_when_endpoints_present():
    """No spurious placeholder when both endpoints already have node rows."""
    model = {
        "nodes": [{"id": "a", "degree": 1}, {"id": "b", "degree": 1}],
        "edges": [{"id": "e_a__rel__b", "source": "a", "target": "b"}],
        "gate_on": 0,
    }
    data = _render_data(model)
    assert {n["id"] for n in data["nodes"]} == {"a", "b"}
    assert len(data["links"]) == 1
