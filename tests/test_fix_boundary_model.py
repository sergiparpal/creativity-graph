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

import pytest

from kg_engine.boundary import MIN_EDGE_BUDGET, merge_results_into_nodes, validate_payload
from kg_engine.model import (
    Disposition,
    Edge,
    EpistemicState,
    Node,
    edge_id,
    node_from_markdown,
    normalize_text,
    slug,
    span_verifies,
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


def test_reemit_grounded_edge_quarantined_and_verdict_protected(pack):
    """review-C1 (§1.8): a re-emit of an existing GROUNDED edge must be QUARANTINED
    `collapses-into-known-verdict`, NOT deduped+accepted — otherwise the canon's "incoming wins" merge
    overwrites the verdict with a fresh `unverified` edge on a normal idempotent re-build. The positive
    half (grounded/obsolete) gets the same protection the failure half already had."""
    src = "Degree approximates importance"
    live = Edge(source="degree", target="importance", relation="approximates", span=src,
                epistemic_state=EpistemicState.GROUNDED)
    results = validate_payload(
        {"edges": [{"source": "degree", "target": "importance", "relation": "approximates",
                    "span": src, "authored_by": "agent"}]},
        pack=pack, source_text=src, existing=[live])
    r = _edge_result(results)
    assert r.disposition == Disposition.QUARANTINED
    assert r.reason == "collapses-into-known-verdict"


def test_reemit_obsolete_edge_also_quarantined(pack):
    """`obsolete` is in GROUNDABLE_STATES but not FAILURE_STATES — it takes the same protected path."""
    src = "Degree approximates importance"
    live = Edge(source="degree", target="importance", relation="approximates", span=src,
                epistemic_state=EpistemicState.OBSOLETE)
    results = validate_payload(
        {"edges": [{"source": "degree", "target": "importance", "relation": "approximates",
                    "span": src, "authored_by": "agent"}]},
        pack=pack, source_text=src, existing=[live])
    assert _edge_result(results).disposition == Disposition.QUARANTINED


def test_reemit_unverified_existing_edge_still_deduped(pack):
    """Idempotency preserved: a re-emit of an existing UNVERIFIED edge (the only non-protected state) is
    still ACCEPTED as `deduped` — re-running /kg-build before grounding must remain a no-op, not a flood."""
    src = "Degree approximates importance"
    live = Edge(source="degree", target="importance", relation="approximates", span=src,
                epistemic_state=EpistemicState.UNVERIFIED)
    results = validate_payload(
        {"edges": [{"source": "degree", "target": "importance", "relation": "approximates",
                    "span": src, "authored_by": "agent"}]},
        pack=pack, source_text=src, existing=[live])
    r = _edge_result(results)
    assert r.disposition == Disposition.ACCEPTED
    assert "deduped" in r.reason


def test_reemit_grounded_edge_end_to_end_verdict_survives(engine):
    """End-to-end mirror of the `failed` survival test, for the POSITIVE verdict half (review-C1):
    write -> ground to `grounded` -> re-emit the SAME edge. The grounding verdict must still stand."""
    payload = {"edges": [
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"}]}
    out = engine.kg_write(payload)
    assert out["dispositions"]["ACCEPTED"] >= 1, out
    eid = edge_id("degree", "approximates", "importance")
    engine.kg_ground(eid, "grounded", by="agent", note="verified for the regression test")
    assert next(x for x in engine.canon.all_edges() if x.id == eid).epistemic_state == EpistemicState.GROUNDED

    again = engine.kg_write(payload)
    assert again["dispositions"]["ACCEPTED"] == 0  # nothing written back over the verdict
    assert again["dispositions"]["QUARANTINED"] >= 1
    after = next(x for x in engine.canon.all_edges() if x.id == eid)
    assert after.epistemic_state == EpistemicState.GROUNDED  # verdict durability (§1.8) held


# --------------------------------------------------------------------------- C2 (slug attachment key)


def test_edge_attaches_to_slugged_source_not_phantom_node():
    """review-C2: an edge whose `source` is a human LABEL ('Free Energy Principle') must attach to the
    SLUG-keyed node ('free-energy-principle'), not fabricate a phantom Node(id='Free Energy Principle')
    that slug-collides onto the same file and rolls back the whole kg_write batch. (pack=None so the
    hypothesized label-source edge is written without a source span or type gate.)"""
    payload = {
        "nodes": [{"label": "Free Energy Principle", "provenance": "hypothesized"}],
        "edges": [{"source": "Free Energy Principle", "target": "active-inference",
                   "relation": "relates_to", "provenance": "hypothesized"}],
    }
    nodes = merge_results_into_nodes(validate_payload(payload, pack=None))
    assert "Free Energy Principle" not in nodes          # no phantom raw-label node
    assert len(nodes) == 1                                 # exactly the one slug node, no collision pair
    fe = nodes[slug("Free Energy Principle")]
    assert [e.source for e in fe.edges] == ["Free Energy Principle"]  # edge attached to the slug node


def test_placeholder_source_node_id_matches_its_filename_slug():
    """The auto-created placeholder for an edge with no supplied node gets id == slug, so its frontmatter
    id matches its slug(id).md filename (no divergence that a later slug-keyed write would split)."""
    payload = {"edges": [{"source": "Active Inference", "target": "x", "relation": "relates_to",
                          "provenance": "hypothesized"}]}
    nodes = merge_results_into_nodes(validate_payload(payload, pack=None))
    assert set(nodes) == {slug("Active Inference")}
    node = nodes[slug("Active Inference")]
    assert node.id == slug(node.id)  # id is already canonical → matches its filename


# --------------------------------------------------------------------------- C1 (merge-layer guard)


def test_merge_preserves_verdict_against_unverified_reemit(canon):
    """review-C1 defense-in-depth: _merge_into_existing must never downgrade a verdict-bearing edge to
    `unverified`. A direct write_nodes(merge=True) re-emit of a grounded edge as `unverified` keeps the
    grounded verdict — the durable last line of defense that also covers the hypothesized lane (where the
    boundary deliberately lets a grounded re-proposal through as live structure)."""
    e = Edge(source="a", target="b", relation="bridges", span="x", epistemic_state=EpistemicState.GROUNDED,
             verdict_by="agent", verdict_at="2026-01-01T00:00:00+00:00")
    canon.write_nodes([Node(id="a", label="a", edges=[e])], message="seed")
    e2 = Edge(source="a", target="b", relation="bridges", span="x", epistemic_state=EpistemicState.UNVERIFIED)
    canon.write_nodes([Node(id="a", label="a", edges=[e2])], message="reemit")
    after = next(x for x in canon.all_edges() if x.id == e.id)
    assert after.epistemic_state == EpistemicState.GROUNDED
    assert after.verdict_by == "agent"


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


# --------------------------------------------------------------------------- M1 (flood cap bypass)


def _budget_filling_edges(n: int, span: str):
    """n DISTINCT net-new span-present edges sharing one verifying span."""
    return [{"source": "a", "target": f"n{i}", "relation": "grounds", "span": span,
             "authored_by": "agent"} for i in range(n)]


def test_duplicated_overflow_edge_cannot_bypass_flood_cap():
    """M1: the flood cap must not be bypassable by listing an over-budget edge TWICE. Fill the floor
    budget exactly, then append one extra NET-NEW edge listed twice. Without the fix the first copy is
    flood-rejected but its id is left in `seen`, so the second copy takes the zero-cost dedup branch and
    is WRITTEN — exceeding the documented cap. After the fix, BOTH copies are flood-rejected."""
    span = "x grounds y"  # short span -> budget falls to the floor
    overflow = {"source": "a", "target": "OVER", "relation": "grounds", "span": span,
                "authored_by": "agent"}
    payload = {"edges": _budget_filling_edges(MIN_EDGE_BUDGET, span) + [overflow, dict(overflow)]}
    res = validate_payload(payload, pack=None, source_text=span)
    written = [r for r in res if r.kind == "edge" and r.written]
    flooded = [r for r in res if r.reason == "rate-limited-flood"]
    # the budget cap holds: never more than MIN_EDGE_BUDGET writable edges, and BOTH overflow copies rejected
    assert len(written) == MIN_EDGE_BUDGET, [r.identity for r in written]
    assert len(flooded) == 2
    over_id = edge_id("a", "grounds", "OVER")
    assert all(r.identity == over_id for r in flooded)
    # the overflow edge was never written under any disposition
    assert over_id not in {r.identity for r in written}


def test_genuine_dedup_of_preexisting_edge_still_free_under_full_budget():
    """The fix must NOT break idempotent re-build dedup: an edge ALREADY in the canon is a real dedup
    (cost zero) even when the budget is exhausted — it grows the canon by nothing."""
    span = "x grounds y"
    existing = [Edge(source="a", target=f"n{i}", relation="grounds", span=span,
                     provenance="span-present") for i in range(MIN_EDGE_BUDGET)]
    # re-emit one of the existing edges: it is a genuine dedup, must be ACCEPTED `deduped`, not flooded
    payload = {"edges": [{"source": "a", "target": "n0", "relation": "grounds", "span": span,
                          "authored_by": "agent"}]}
    res = validate_payload(payload, pack=None, source_text=span, existing=existing)
    r = _edge_result(res)
    assert r.disposition == Disposition.ACCEPTED
    assert "deduped" in r.reason


def test_in_payload_duplicate_under_budget_still_dedups():
    """And an in-payload duplicate that DOES fit the budget still dedups (the first copy is written and
    charged; the second is a zero-cost dedup) — the fix only withholds `seen` from flood-REJECTED copies."""
    span = "x grounds y"
    dup = {"source": "a", "target": "b", "relation": "grounds", "span": span, "authored_by": "agent"}
    res = validate_payload({"edges": [dict(dup), dict(dup)]}, pack=None, source_text=span)
    edges = [r for r in res if r.kind == "edge"]
    assert edges[0].disposition == Disposition.ACCEPTED and "deduped" not in edges[0].reason
    assert edges[1].disposition == Disposition.ACCEPTED and "deduped" in edges[1].reason


# --------------------------------------------------------------------------- model: U+0130 casefold


def test_dotted_capital_i_span_verifies_case_insensitively():
    """A source written with U+0130 (İ) must still match a lowercased verbatim span — the §1.5 gate's
    case-insensitive contract. Without the fix, casefold(İ) = 'i' + U+0307 (a non-NFC sequence NFC
    can't recompose), so 'istanbul' fails to substring-match and the honest span is wrongly rejected."""
    source = "The city of İstanbul sits on two continents"
    span = "istanbul sits on two continents"  # the verbatim span, lowercased
    assert span_verifies(span, source)
    # both forms collapse to the same dotless 'i' under normalization, and contain no stray combining dot
    assert "i̇" not in normalize_text("İstanbul")
    assert normalize_text("İstanbul") == normalize_text("istanbul")


# --------------------------------------------------------------------------- model: non-dict frontmatter


def test_non_dict_frontmatter_raises_valueerror_not_attributeerror():
    """A hand-edited note whose frontmatter parses to a YAML list (not a mapping) must fail through the
    documented ValueError path, never leak a raw AttributeError from `fm.get(...)`."""
    text = "---\n- id: x\n- stray list entry\n---\n\nbody\n"
    with pytest.raises(ValueError):
        node_from_markdown(text)


def test_scalar_frontmatter_also_raises_valueerror():
    """A bare scalar between the fences is likewise non-mapping frontmatter -> ValueError."""
    text = "---\njust a scalar\n---\n\nbody\n"
    with pytest.raises(ValueError):
        node_from_markdown(text)
