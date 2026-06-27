"""kg_merge: deliberate node-merge with edge-id dedup (§1.4/§1.7/§1.8).

kg_merge collapses two existing nodes that name the same concept — the case kg_rename refuses
(target-exists) and that no other tool could do without forging a verdict. These pin the dedup
precedence (negative-info sticky, grounded>unverified, span/note retained, never forged), self-loop
drop, the typing guard, that kg_rename stays strict, and that a migrated verdict survives reconcile.
"""
from __future__ import annotations

from kg_engine.model import Edge, EpistemicState, Node, Provenance, edge_id
from kg_engine.reconciler import Reconciler

SPAN = "defends against re-proposal"      # verbatim in conftest.SOURCE
SPAN2 = "grounds the claims beneath it"   # verbatim in conftest.SOURCE


def _span_edge(src: str, rel: str, tgt: str, span: str = SPAN) -> Edge:
    return Edge(source=src, target=tgt, relation=rel, span=span,
                provenance=Provenance.SPAN_PRESENT)


def _seed_pair(engine, *, ground: str | None = "a", verdict: str = "grounded") -> str:
    """Nodes a & b, each with a `defends_against c` edge (the minimal collision fixture). `ground`
    names which node's edge to stamp `verdict` via the audited kg_ground; returns its current id."""
    a = Node(id="a", label="a", node_type="claim", edges=[_span_edge("a", "defends_against", "c")])
    b = Node(id="b", label="b", node_type="claim", edges=[_span_edge("b", "defends_against", "c")])
    c = Node(id="c", label="c", node_type="claim")
    engine.canon.write_nodes([a, b, c], message="seed")
    if ground is not None:
        eid = edge_id(ground, "defends_against", "c")
        out = engine.kg_ground(eid, verdict, by="agent", note="checked in source")
        assert out["ok"], out
        return eid
    return edge_id("a", "defends_against", "c")


# ---- the minimal repro: collision dedups to ONE edge; grounded beats unverified; span/note kept ----
def test_merge_collision_dedups_grounded_over_unverified(engine):
    _seed_pair(engine, ground="a", verdict="grounded")  # a's edge grounded, b's still unverified
    out = engine.kg_merge("a", "b")
    assert out["ok"], out

    merged = "e_b__defends-against__c"
    edges = [e for e in engine.canon.all_edges() if e.id == merged]
    assert len(edges) == 1                          # exactly one edge remains, not a duplicate
    e = edges[0]
    assert e.epistemic_state == EpistemicState.GROUNDED   # grounded won
    assert e.span == SPAN                            # the verbatim span is retained
    assert "checked in source" in e.notes            # the stored verdict note is retained
    assert e.verdict_by == "agent"                   # verdict attribution travels with the state
    assert not engine.canon.exists("a")              # the source node was retired
    assert any(d["id"] == merged and d["state"] == "grounded" for d in out["edges_deduped"])


# ---- negative information (§1.7) is sticky: failed/rejected survives a merge against a grounded edge ----
def test_merge_preserves_failed_negative_information(engine):
    _seed_pair(engine, ground="a", verdict="failed")  # a's edge actively falsified
    # ground b's edge so the collision is failed (a) vs grounded (b)
    engine.kg_ground(edge_id("b", "defends_against", "c"), "grounded", by="agent")

    out = engine.kg_merge("a", "b")
    assert out["ok"], out
    e = next(e for e in engine.canon.all_edges() if e.id == "e_b__defends-against__c")
    assert e.epistemic_state == EpistemicState.FAILED   # failed dominates grounded — never pruned


def test_merge_preserves_rejected_over_unverified(engine):
    _seed_pair(engine, ground="a", verdict="rejected")
    out = engine.kg_merge("a", "b")
    assert out["ok"], out
    e = next(e for e in engine.canon.all_edges() if e.id == "e_b__defends-against__c")
    assert e.epistemic_state == EpistemicState.REJECTED


# ---- a rewrite that makes source == target is dropped, leaving the node's other edges intact ----
def test_merge_drops_self_loop(engine):
    a = Node(id="a", label="a", node_type="claim", edges=[
        _span_edge("a", "defends_against", "b"),      # a→b becomes the self-loop b→b on merge
        _span_edge("a", "grounds", "c", span=SPAN2)])  # a→c survives as b→c
    b = Node(id="b", label="b", node_type="claim")
    c = Node(id="c", label="c", node_type="claim")
    engine.canon.write_nodes([a, b, c], message="seed")

    out = engine.kg_merge("a", "b")
    assert out["ok"], out
    assert out["self_loops_dropped"] == ["e_b__defends-against__b"]
    b_node = engine.canon.read_node("b")
    assert any(e.id == "e_b__grounds__c" for e in b_node.edges)        # the non-loop edge survived
    assert not any(e.source == e.target for e in b_node.edges)          # no self-loop persisted


# ---- typing safety: a merge across two DIFFERENT declared node_types fails loudly, mutating nothing ----
def test_merge_refuses_conflicting_node_type(engine):
    a = Node(id="a", label="a", node_type="claim", edges=[_span_edge("a", "grounds", "c", span=SPAN2)])
    b = Node(id="b", label="b", node_type="mechanism")
    engine.canon.write_nodes([a, b], message="seed")

    out = engine.kg_merge("a", "b")
    assert not out["ok"]
    assert "node_type conflict" in out["error"]
    # nothing was mutated: both nodes still exist, a still owns its edge
    assert engine.canon.exists("a") and engine.canon.exists("b")
    assert any(e.id == "e_a__grounds__c" for e in engine.canon.read_node("a").edges)


# ---- kg_rename stays STRICT: it never silently merges on a target collision ----
def test_kg_rename_still_errors_on_target_exists(engine):
    engine.canon.write_nodes([Node(id="a", label="a"), Node(id="b", label="b")], message="seed")
    out = engine.kg_rename("a", "b")
    assert not out["ok"] and out["error"] == "target id exists"
    assert engine.canon.exists("a") and engine.canon.exists("b")   # both untouched


# ---- determinism / idempotency: stable merged id; a second merge is a clean no-op error ----
def test_merge_is_deterministic_and_idempotent(engine):
    _seed_pair(engine, ground="a", verdict="grounded")
    out1 = engine.kg_merge("a", "b")
    assert out1["ok"], out1
    merged = "e_b__defends-against__c"
    s1 = next(e for e in engine.canon.all_edges() if e.id == merged).epistemic_state

    out2 = engine.kg_merge("a", "b")        # `a` is gone — refuse cleanly, never corrupt
    assert not out2["ok"] and out2["error"] == "source node not found"
    s2 = next(e for e in engine.canon.all_edges() if e.id == merged).epistemic_state
    assert s1 == s2 == EpistemicState.GROUNDED


# ---- §1.8: a verdict migrated onto a new edge id survives the reconciler's forgery sweep ----
def test_merged_verdict_survives_reconcile(engine):
    _seed_pair(engine, ground="a", verdict="grounded")  # grounded on a → its id CHANGES on merge
    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)                          # baseline before the merge

    out = engine.kg_merge("a", "b")
    assert out["ok"], out
    merged = "e_b__defends-against__c"
    # the grounded verdict moved a→b, so kg_merge must have emitted a migrating audit record for it
    report = recon.scan(full_sweep=True)
    assert merged not in report.requarantined
    e = next(e for e in engine.canon.all_edges() if e.id == merged)
    assert e.epistemic_state == EpistemicState.GROUNDED


# ---- span-present provenance + non-empty span are preferred when filling from the loser ----
def test_merge_prefers_span_present_provenance(engine):
    # a's edge is grounded but span-LESS (inferred); b's is unverified WITH a verbatim span.
    a_edge = Edge(source="a", target="c", relation="defends_against",
                  provenance=Provenance.INFERRED, span="")
    b_edge = _span_edge("b", "defends_against", "c")
    engine.canon.write_nodes([
        Node(id="a", label="a", node_type="claim", edges=[a_edge]),
        Node(id="b", label="b", node_type="claim", edges=[b_edge]),
        Node(id="c", label="c", node_type="claim")], message="seed")
    engine.kg_ground(edge_id("a", "defends_against", "c"), "grounded", by="agent")

    out = engine.kg_merge("a", "b")
    assert out["ok"], out
    e = next(e for e in engine.canon.all_edges() if e.id == "e_b__defends-against__c")
    assert e.epistemic_state == EpistemicState.GROUNDED      # grounded state kept
    assert e.span == SPAN                                    # the real span filled the gap
    assert e.provenance == Provenance.SPAN_PRESENT           # a surviving real span IS span-present
