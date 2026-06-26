"""Regression tests for the follow-up review sweep (2026): the correctness/security fixes the
adversarial review confirmed. Each test fails on the pre-fix code and passes after.

- boundary slug collision: a caller-supplied non-slug node id must not diverge the node lane from the
  edge-source key and roll back the whole batch.
- boundary flood-cap bypass: a QUARANTINED twin must not seed the dedup `seen` set and buy a same-id
  writable edge a free pass through the §Stage-9 cap.
- boundary degenerate endpoint: an empty/punctuation-only edge endpoint (slug -> literal "node") is
  rejected before edge_id can alias distinct edges.
- reconciler intermediate-skip surplus: an audit record for an intermediate state the sweep never
  observes-as-current must not stay spendable for a later out-of-band forgery.
"""
from __future__ import annotations

from kg_engine.boundary import validate_payload
from kg_engine.model import Disposition, Edge, EpistemicState, edge_id

SRC = ("A compression grounds the claims beneath it. Betweenness is confounded by the generality "
       "confound. Heat flows from hot to cold. The canon grounds trust.")


def _edges(results):
    return [r for r in results if r.kind == "edge"]


def test_caller_supplied_non_slug_node_id_does_not_roll_back_batch(engine):
    """A node whose caller-supplied id is not already in slug form ("Canon") plus an edge whose source
    resolves to that node must NOT diverge into two keys ("Canon" vs "canon") that both map to canon.md
    and trip the slug-collision rollback, silently losing the whole section. The boundary now slugs the
    resolved node id so the node lane, the edge-source key, and the filename agree."""
    res = engine.kg_write({
        "nodes": [{"id": "Canon", "label": "Canon", "node_type": "primitive"}],
        "edges": [{"source": "Canon", "target": "trust", "relation": "grounds",
                   "span": "The canon grounds trust", "authored_by": "agent"}],
    })
    assert not res.get("rolled_back"), res
    assert engine.canon.exists("canon")  # landed under the canonical slug, not lost to a collision
    eid = edge_id("Canon", "grounds", "trust")  # edge_id slugs internally -> e_canon__grounds__trust
    assert any(e.id == eid for e in engine.canon.all_edges())


def test_quarantined_twin_does_not_buy_a_free_flood_edge(pack):
    """A QUARANTINED edge (undeclared type) must NOT seed the dedup `seen` set, or a later
    same-canonical-id WRITABLE edge takes the zero-cost 'deduped' branch and slips past the §Stage-9
    flood cap. With the budget already saturated, the real edge must be flood-REJECTED — not accepted
    free via a case-variant ('Grounds' vs declared 'grounds') quarantined twin that slugs to the same
    id."""
    # Saturate the flood budget (floor 64) with existing edges so any NET-NEW writable edge must flood.
    existing = [Edge(source=f"n{i}", target=f"m{i}", relation="grounds",
                     epistemic_state=EpistemicState.UNVERIFIED) for i in range(64)]
    payload = {"edges": [
        # quarantined twin: undeclared 'Grounds' relation slugs to the SAME canonical id as 'grounds'
        {"source": "compression", "target": "claim", "relation": "Grounds",
         "span": "A compression grounds the claims beneath it", "authored_by": "agent"},
        # the real edge: same canonical id, declared type, span verifies
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "A compression grounds the claims beneath it", "authored_by": "agent"},
    ]}
    twin, real = _edges(validate_payload(payload, pack=pack, source_text=SRC, existing=existing))
    assert twin.disposition == Disposition.QUARANTINED and "undeclared-edge-type" in twin.reason
    # the twin must NOT have seeded `seen`, so the real edge is charged and flood-rejected, not free
    assert real.disposition == Disposition.REJECTED and real.reason == "rate-limited-flood"


def test_degenerate_edge_endpoint_rejected(pack):
    """An empty / whitespace / punctuation-only source, relation, or target has no word character, so
    slug() collapses it to the literal "node" and edge_id aliases distinct edges. The boundary rejects
    such degenerate endpoints before that aliasing can dedup-merge unrelated claims."""
    cases = [
        ("source", {"source": "", "target": "b", "relation": "grounds"}),
        ("target", {"source": "a", "target": "   ", "relation": "grounds"}),
        ("relation", {"source": "a", "target": "b", "relation": "---"}),
    ]
    for role, bad in cases:
        edge = dict(bad, span="Heat flows from hot to cold", authored_by="agent")
        e = _edges(validate_payload({"edges": [edge]}, pack=pack, source_text=SRC))[0]
        assert e.disposition == Disposition.REJECTED, (role, e.reason)
        assert e.reason == f"empty-{role}", (role, e.reason)


def test_intermediate_state_record_cannot_resurrect_a_forged_verdict(engine):
    """An edge that passes through an INTERMEDIATE policed state within a session
    (unverified -> grounded -> failed: grounder then adversarial grounder) leaves an audit record for
    the skipped `grounded` state that the next sweep never observes-as-current. That orphan must NOT stay
    spendable: a later out-of-band forgery flipping the edge back to `grounded` has to be caught. The fix
    drains the WHOLE per-key ledger on each validated observation."""
    from kg_engine.reconciler import Reconciler

    engine.kg_write({"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]})
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent")   # audit record: e_x||grounded
    engine.kg_ground(eid, "failed", by="agent")     # audit record: e_x||failed (skips observe-as-grounded)

    recon = Reconciler(engine.canon)
    recon.scan(full_sweep=True)  # observes only `failed`; must also drain the orphan `grounded` record

    # Out-of-band forge the edge back to `grounded` (no kg_ground, no NEW audit record).
    node = engine.canon.read_node("degree")
    next(e for e in node.edges if e.id == eid).epistemic_state = EpistemicState.GROUNDED
    engine.canon.write_one(node)

    report = recon.scan(full_sweep=True)
    assert eid in report.requarantined  # the orphan grounded record was already drained -> caught
    after = next(e for e in engine.canon.all_edges() if e.id == eid)
    assert after.epistemic_state == EpistemicState.UNVERIFIED
