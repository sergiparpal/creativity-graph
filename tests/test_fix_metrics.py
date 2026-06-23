"""Regression tests for the metrics/projector fixes:

- F7/M1  harness.absorption: an unbounded half-life is `None`, not `float('inf')` — the dict is
         returned verbatim by the kg_absorption MCP tool and `inf` serializes to the bareword
         `Infinity` (invalid per RFC 8259, breaks a strict client JSON.parse).
- F8/M2  projector.kg_context: the grounded items[] lane and the hypotheses[] lane share ONE running
         budget (§1.11) — the true serialized token count never exceeds `budget`, and the reported
         `approx_tokens` counts both lanes.
- F18/L6 projector._ranks: degree/betweenness advisories are computed over the NON-FAILED subgraph, so
         the adversarial grounder's `failed` counter-edges don't inflate an attacked hub's centrality —
         while graph.json stays COMPLETE (failure memory preserved).
"""
from __future__ import annotations

import json

from kg_engine.harness import absorption
from kg_engine.model import Edge, EpistemicState, Node, Provenance
from kg_engine.projector import Projector


# --------------------------------------------------------------------------- F7/M1 (absorption)


def test_absorption_result_is_strict_json_serializable():
    """A result containing an isolated node (degree 0) and a zero-growth fertile node must serialize
    under strict JSON (no `allow_nan`) and round-trip; both unbounded half-lives are None."""
    gdata = {"directed": True,
             # only `kept` is connected (degree 1); `iso` and `flat` have no live edges
             "nodes": [{"id": x} for x in ["iso", "flat", "kept", "k1"]],
             "links": [{"source": "flat", "target": "kept"}, {"source": "kept", "target": "k1"}]}
    history = {
        "iso": {"introduced_at": 0, "introduced_degree": 0},   # stays disconnected -> isolated
        "flat": {"introduced_at": 0, "introduced_degree": 1},  # degree>0 but zero growth -> fertile, inf
        "kept": {"introduced_at": 0, "introduced_degree": 1},
    }
    res = absorption(gdata, history, now=5)
    assert res["iso"]["status"] == "isolated"
    assert res["flat"]["status"] == "fertile" and res["flat"]["densification"] == 0
    # the unbounded half-lives are None, NOT float('inf')
    assert res["iso"]["half_life"] is None
    assert res["flat"]["half_life"] is None
    # strict JSON: allow_nan=False rejects Infinity/NaN; this must not raise and must round-trip
    blob = json.dumps(res, allow_nan=False)
    assert "Infinity" not in blob
    reparsed = json.loads(blob)
    assert reparsed["iso"]["half_life"] is None and reparsed["flat"]["half_life"] is None


# --------------------------------------------------------------------------- F8/M2 (kg_context budget)


def _approx_tokens(payload_items) -> int:
    """The engine's own per-record token estimate, summed — the true serialized cost of a lane."""
    return sum(max(1, len(json.dumps(rec)) // 4) for rec in payload_items)


def test_kg_context_shares_budget_across_items_and_hypotheses(canon):
    # A hypothesis-heavy graph: a handful of grounded items plus MANY hypothesized/unverified edges.
    # Each lane used to be capped at the FULL budget independently, so a hypothesis-heavy query could
    # serialize ~2x budget while reporting approx_tokens <= budget (§1.11 violation).
    grounded = [Edge(source="g", target=f"gt{i}", relation="grounds",
                     span=f"grounded span number {i} with some words", provenance=Provenance.SPAN_PRESENT,
                     epistemic_state=EpistemicState.GROUNDED) for i in range(2)]
    hypos = [Edge(source="h", target=f"ht{i}", relation="bridges",
                  provenance=Provenance.HYPOTHESIZED, epistemic_state=EpistemicState.UNVERIFIED,
                  confidence_score=0.5) for i in range(40)]
    nodes = [Node(id="g", label="G", edges=grounded), Node(id="h", label="H", edges=hypos)]
    nodes += [Node(id=f"gt{i}") for i in range(2)] + [Node(id=f"ht{i}") for i in range(40)]
    canon.write_nodes(nodes, message="seed budget graph")
    proj = Projector(canon)
    proj.project()

    # items lane fills ~2 cheap grounded edges, leaving room for a few hypotheses but NOT all 40 — so
    # both lanes are exercised. Under the OLD per-lane-full-budget cap the hypotheses lane alone would
    # have filled to ~budget, doubling the true payload to ~2x budget.
    budget = 300
    ctx = proj.kg_context(budget=budget)
    # both lanes actually got filled (otherwise the test would not exercise the shared-budget path)
    assert ctx["hypotheses"], "expected hypotheses to be populated"
    # the TRUE serialized cost of items + hypotheses is within budget ...
    true_tokens = _approx_tokens(ctx["items"]) + _approx_tokens(ctx["hypotheses"])
    assert true_tokens <= budget, (true_tokens, budget)
    # ... and the reported approx_tokens reflects BOTH lanes (not just the items lane) and is honest
    assert ctx["approx_tokens"] == true_tokens
    assert ctx["approx_tokens"] <= budget
    # segregation is preserved: items carry no hypothesized provenance; hypotheses are all hypothesized
    assert all(i["provenance"] != "hypothesized" for i in ctx["items"])
    assert all(h["provenance"] == "hypothesized" for h in ctx["hypotheses"])


# --------------------------------------------------------------------------- F18/L6 (failed edges don't inflate ranks)


def _degree_of(proj: Projector, node_id: str) -> int:
    return proj.get_node(node_id)["degree"]


def test_failed_counter_edges_do_not_inflate_degree_advisory(canon):
    # A hub with several LIVE edges, plus a control build identical except for N additional `failed`
    # counter-edges (what the adversarial grounder stamps). The degree advisory must be identical —
    # refutation must not read as centrality — while graph.json must still CONTAIN the failed edges.
    live_targets = ["t1", "t2", "t3"]
    base_edges = [Edge(source="hub", target=t, relation="grounds", span=f"span {t}",
                       epistemic_state=EpistemicState.GROUNDED) for t in live_targets]
    failed_edges = [Edge(source="hub", target=f"f{i}", relation="attacked_by",
                         span=f"refutation {i}", epistemic_state=EpistemicState.FAILED) for i in range(5)]

    nodes = ([Node(id="hub", label="Hub", edges=base_edges + failed_edges)]
             + [Node(id=t) for t in live_targets] + [Node(id=f"f{i}") for i in range(5)])
    canon.write_nodes(nodes, message="seed hub with failed counter-edges")
    proj = Projector(canon)
    proj.project(incremental=False)
    deg_with_failed = _degree_of(proj, "hub")

    # graph.json must STILL contain the failed counter-edges (failure memory is never pruned, §1.7)
    data = json.loads(proj.graph_path.read_text())
    failed_ids = {e.id for e in failed_edges}
    json_edge_ids = {l["id"] for l in data["links"]}
    assert failed_ids <= json_edge_ids, "failed edges must remain in graph.json"

    # now a control vault with ONLY the live edges (no failed counter-edges): a second git-backed canon
    # in a sibling dir so its projection is independent of the first.
    import subprocess

    from kg_engine.canon import Canon
    control_root = canon.root / "_control"
    control_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(control_root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(control_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(control_root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(control_root), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    control = Canon(control_root)
    control_nodes = ([Node(id="hub", label="Hub", edges=list(base_edges))]
                     + [Node(id=t) for t in live_targets])
    control.write_nodes(control_nodes, message="control hub no failures")
    cproj = Projector(control)
    cproj.project(incremental=False)
    deg_without_failed = _degree_of(cproj, "hub")

    # the failed counter-edges did NOT inflate the hub's degree advisory
    assert deg_with_failed == deg_without_failed == len(live_targets)
