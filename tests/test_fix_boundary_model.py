"""Regression tests for the boundary/model fixes.

F1 — kg_write must not silently erase failure memory (§1.7). A span-present/inferred edge whose EXACT
canonical identity already lives in FAILURE_STATES (failed/rejected) must be QUARANTINED on re-emit,
never deduped + accepted (which the canon merge would then write back as a fresh `unverified` edge,
overwriting the refutation). The collapse on the NON-hypothesized lane checks the same `ident` ONLY —
the reverse edge has its own honest textual support and is a distinct claim, so it stays accepted.

F33 — slug() docstring softening only; no behavior change. A focused assertion pins the real (weaker)
guarantee the docstring now states, so the doc can't drift back to overclaiming.
"""
from __future__ import annotations

from kg_engine.boundary import validate_payload
from kg_engine.model import (
    Disposition,
    Edge,
    EpistemicState,
    edge_id,
    slug,
)


def _edge_result(results):
    return next(r for r in results if r.kind == "edge")


# --------------------------------------------------------------------------- F1 (end-to-end)


def test_reemit_failed_edge_quarantined_and_verdict_survives(engine):
    """End-to-end through the real engine: write -> ground to `failed` -> re-emit the SAME edge via
    kg_write. The re-emit must be QUARANTINED `collapses-into-known-failure`, and the persisted edge
    must still be `failed` (never reset to `unverified`)."""
    payload = {"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]}
    out = engine.kg_write(payload)
    assert out["dispositions"]["ACCEPTED"] >= 1, out
    eid = edge_id("degree", "approximates", "importance")

    # drive it to a recorded failure through the sole verdict gateway
    engine.kg_ground(eid, "failed", by="agent", note="falsified for the regression test")
    e = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert e.epistemic_state == EpistemicState.FAILED

    # re-emitting the SAME span-present edge must NOT overwrite the verdict
    again = engine.kg_write(payload)
    reasons = [d["reason"] for d in again["details"] if d["kind"] == "edge"]
    assert any("collapses-into-known-failure" in r for r in reasons), again
    assert again["dispositions"]["QUARANTINED"] >= 1
    assert again["dispositions"]["ACCEPTED"] == 0  # nothing accepted, so nothing written back

    # the failed verdict still stands in the canon — failure memory was preserved (§1.7)
    after = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert after.epistemic_state == EpistemicState.FAILED


def test_reemit_rejected_edge_also_quarantined(engine):
    """The companion FAILURE_STATE: a `rejected` identity binds re-extraction the same way `failed` does."""
    payload = {"edges": [
        {"source": "failed-claim", "target": "re-proposal", "relation": "defends_against",
         "span": "A failed claim is negative information and defends against re-proposal",
         "authored_by": "agent"}]}
    out = engine.kg_write(payload)
    assert out["dispositions"]["ACCEPTED"] >= 1, out
    eid = edge_id("failed-claim", "defends_against", "re-proposal")
    engine.kg_ground(eid, "rejected", by="agent", note="vague")

    again = engine.kg_write(payload)
    reasons = [d["reason"] for d in again["details"] if d["kind"] == "edge"]
    assert any("collapses-into-known-failure" in r for r in reasons), again
    after = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert after.epistemic_state == EpistemicState.REJECTED


# --------------------------------------------------------------------------- F1 (validate_payload unit)


def test_validate_payload_same_identity_failure_collapse_non_hypothesized(pack):
    """At the boundary directly: a span-present edge whose own id is already a FAILURE_STATE is
    QUARANTINED, not deduped/accepted."""
    src = "Degree approximates importance"
    failed = Edge(source="degree", target="importance", relation="approximates", span=src,
                  epistemic_state=EpistemicState.FAILED)
    results = validate_payload(
        {"edges": [{"source": "degree", "target": "importance", "relation": "approximates",
                    "span": src, "authored_by": "agent"}]},
        pack=pack, source_text=src, existing=[failed])
    r = _edge_result(results)
    assert r.disposition == Disposition.QUARANTINED
    assert r.reason == "collapses-into-known-failure"


def test_non_hypothesized_lane_does_not_reverse_collapse(pack):
    """CRUCIAL asymmetry: on the span-present/inferred lane only the SAME ident collapses. The reverse
    edge has genuine textual support for its own direction, so it remains an honest, acceptable claim
    (unlike the hypothesized lane, which reverse-collapses)."""
    src = "Degree approximates importance"
    failed = Edge(source="degree", target="importance", relation="approximates", span=src,
                  epistemic_state=EpistemicState.FAILED)
    # the reverse direction is a distinct identity; it must NOT be quarantined here
    results = validate_payload(
        {"edges": [{"source": "importance", "target": "degree", "relation": "approximates",
                    "span": src, "authored_by": "agent"}]},
        pack=pack, source_text=src, existing=[failed])
    r = _edge_result(results)
    assert r.disposition == Disposition.ACCEPTED
    assert r.reason == ""  # a clean, net-new, span-present edge


def test_reemit_non_failed_existing_edge_still_deduped(pack):
    """A normal re-emit of an existing edge that is NOT in FAILURE_STATES must still ACCEPT as
    `deduped` — only failed/rejected identities are quarantined."""
    src = "Degree approximates importance"
    live = Edge(source="degree", target="importance", relation="approximates", span=src,
                epistemic_state=EpistemicState.GROUNDED)
    results = validate_payload(
        {"edges": [{"source": "degree", "target": "importance", "relation": "approximates",
                    "span": src, "authored_by": "agent"}]},
        pack=pack, source_text=src, existing=[live])
    r = _edge_result(results)
    assert r.disposition == Disposition.ACCEPTED
    assert "deduped" in r.reason
    assert "collapses-into-known-failure" not in r.reason


# --------------------------------------------------------------------------- F33 (slug guarantee)


def test_slug_unifies_punctuation_only_variants():
    """The real (weaker) guarantee the docstring now states: punctuation/separators normalize to a
    single '-', so punctuation-only variants are intentionally unified."""
    assert slug("a/b") == slug("a-b") == slug("a b") == "a-b"
    assert slug("!!!foo!!!") == slug("foo") == "foo"


def test_slug_keeps_separated_inputs_distinct_from_concatenation():
    """What mapping (vs deleting) still buys: a separating mark keeps distinct inputs distinct rather
    than collapsing them onto the concatenation."""
    assert slug("a/b") != slug("ab")
    assert slug("I/O") != slug("IO")
    assert slug("foo.bar") != slug("foobar")
