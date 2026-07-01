"""Regression tests for the server.py review fixes (the coupled/invariant-critical group handled inline):
idempotency-receipt content folding, read-path egress re-scrub (M4), kg_merge negative-info self-loop
preservation, the watchdog critical-write grace extension, the error-envelope configured sensitivity,
the generative-read projection_degraded flag, and the writers' blocking-lock acquire.
"""
from __future__ import annotations

import json

import pytest

import kg_engine.server as srv
from kg_engine.model import EpistemicState, Provenance, edge_id
from kg_engine.server import KGEngine, _Watchdog

from conftest import make_edge, make_node


# --- 527: idempotency receipt folds content, not just ids ------------------------------------------

def test_payload_receipt_folds_content_not_just_ids():
    same_ids = lambda span: {"edges": [{"source": "x", "relation": "grounds", "target": "y", "span": span}]}
    a = KGEngine._payload_receipt(same_ids("AAA"))
    b = KGEngine._payload_receipt(same_ids("BBB"))
    c = KGEngine._payload_receipt(same_ids("AAA"))
    assert a == c            # deterministic: identical payload -> identical receipt
    assert a != b            # same ids, different span -> DIFFERENT receipt (the fix)


def test_idempotency_replay_vs_content_change(engine):
    p1 = {"edges": [{"source": "Entropy", "relation": "grounds", "target": "arrow of time",
                     "span": "Entropy grounds the arrow of time."}]}
    r1 = engine.kg_write(p1, idempotency_key="k1")
    r1b = engine.kg_write(p1, idempotency_key="k1")           # identical resend -> replay preserved
    assert r1b.get("idempotent_replay") is True
    assert r1b["receipt"] == r1["receipt"]
    # same edge ids but a CHANGED span must be processed, not silently replayed
    p2 = {"edges": [{"source": "Entropy", "relation": "grounds", "target": "arrow of time",
                     "span": "Heat flows from hot to cold."}]}
    r2 = engine.kg_write(p2, idempotency_key="k1")
    assert r2.get("idempotent_replay") is not True
    assert r2["receipt"] != r1["receipt"]


# --- M4: read tools re-scrub free-text (a restored secret must not round-trip to the model) ---------

def test_get_node_and_context_scrub_secret_in_span(engine):
    # write an edge whose span carries a secret DIRECTLY to canon (bypassing the boundary span check),
    # mirroring how kg_write restores the ORIGINAL (unscrubbed) secret into the canon span.
    node = make_node("alpha", edges=[
        make_edge("alpha", "grounds", "beta", span="reach me at secret@example.com anytime",
                  provenance=Provenance.SPAN_PRESENT, epistemic_state=EpistemicState.GROUNDED)])
    engine.canon.write_nodes([node, make_node("beta")], message="seed")

    got = engine.get_node("alpha")
    blob = json.dumps(got, ensure_ascii=False)
    assert "secret@example.com" not in blob        # redacted on egress
    assert "⟦EMAIL" in blob                          # replaced with a consistent placeholder

    ctx = json.dumps(engine.kg_context(query="beta", budget=4000), ensure_ascii=False)
    assert "secret@example.com" not in ctx


# --- 1002: kg_merge preserves a failed/rejected self-loop (negative info is never pruned, §1.7) -----

def test_kg_merge_preserves_failed_selfloop(engine):
    a = make_node("a", edges=[make_edge("a", "grounds", "b", span="s",
                                        provenance=Provenance.SPAN_PRESENT,
                                        epistemic_state=EpistemicState.FAILED)])
    engine.canon.write_nodes([a, make_node("b")], message="seed")

    res = engine.kg_merge("a", "b")
    assert res["ok"], res
    loop_id = edge_id("b", "grounds", "b")
    assert loop_id not in res.get("self_loops_dropped", [])   # NOT pruned
    b_after = engine.canon.read_node("b")
    assert any(e.epistemic_state == EpistemicState.FAILED for e in b_after.edges)


def test_kg_merge_still_drops_positive_selfloop(engine):
    a = make_node("a", edges=[make_edge("a", "grounds", "b", span="s",
                                        provenance=Provenance.SPAN_PRESENT,
                                        epistemic_state=EpistemicState.UNVERIFIED)])
    engine.canon.write_nodes([a, make_node("b")], message="seed")
    res = engine.kg_merge("a", "b")
    assert res["ok"], res
    assert edge_id("b", "grounds", "b") in res.get("self_loops_dropped", [])


# --- 280: watchdog grants ONE grace extension while a critical write is in flight -------------------

def test_watchdog_grace_extension_during_critical_write():
    wd = _Watchdog(10.0, on_trip=lambda: None)
    wd.enter("kg_merge")
    base = wd._started
    wd.begin_critical()
    assert wd.overdue(now=base + 11) is None          # 1st overrun during critical -> extend, no trip
    assert wd.overdue(now=base + 11 + 5) is None       # still inside the extended window
    hit = wd.overdue(now=base + 11 + 11)               # exceeded the extended window -> trips
    assert hit is not None and hit[0] == "kg_merge"


def test_watchdog_trips_immediately_without_critical():
    wd = _Watchdog(10.0, on_trip=lambda: None)
    wd.enter("query_graph")
    base = wd._started
    assert wd.overdue(now=base + 11) is not None        # no critical section -> no grace


def test_watchdog_extension_resets_per_handler():
    wd = _Watchdog(10.0, on_trip=lambda: None)
    wd.enter("kg_merge")
    b1 = wd._started
    wd.begin_critical()
    assert wd.overdue(now=b1 + 11) is None               # consumes the one extension
    wd.end_critical()
    wd.exit()
    wd.enter("kg_rename")                                 # fresh handler -> extension available again
    b2 = wd._started
    wd.begin_critical()
    assert wd.overdue(now=b2 + 11) is None


# --- 1616: the tool envelope scrubs errors at the CONFIGURED sensitivity ----------------------------

def test_error_envelope_uses_configured_sensitivity(engine):
    prior = srv._ACTIVE_ENGINE
    try:
        engine.sensitivity = "high"
        srv._ACTIVE_ENGINE = engine
        assert srv._active_sensitivity() == "high"
        out = srv._scrub_error_text("Dr. Jane Smith could not be read",
                                    sensitivity=srv._active_sensitivity())
        assert "Jane" not in out                          # PERSON redacted at high
        # medium (the old hardcoded default) would NOT redact a person name
        assert "Jane" in srv._scrub_error_text("Dr. Jane Smith could not be read", sensitivity="medium")
    finally:
        srv._ACTIVE_ENGINE = prior


# --- 1259: generative reads surface projection_degraded like the sibling reads ----------------------

def test_kg_generate_surfaces_projection_degraded(engine):
    engine.kg_generate(mechanism="bridge", k=3)           # real projection first (creates the db)
    engine._projection_degraded = "degraded-test"
    engine._ensure_projected = lambda: None               # don't let the real path reset the flag
    out = engine.kg_generate(mechanism="bridge", k=3)
    assert out.get("projection_degraded") == "degraded-test"


# --- 690: the writers still succeed on the normal (uncontended) path after the blocking-lock switch -

def test_kg_ground_normal_path_after_blocking_lock(engine):
    e = make_edge("Degree", "approximates", "importance", span="Degree approximates importance.",
                  provenance=Provenance.SPAN_PRESENT, epistemic_state=EpistemicState.UNVERIFIED)
    engine.canon.write_nodes([make_node("Degree", edges=[e]), make_node("importance")], message="seed")
    res = engine.kg_ground(edge_id("Degree", "approximates", "importance"), "grounded")
    assert res.get("ok") is True, res
