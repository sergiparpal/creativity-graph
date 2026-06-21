"""Stage 9 deferred items: the edges-per-KB injection rate limit and the hardened canon path resolver.

Both are deterministic-tier (§1.4) guarantees — they hold regardless of what an agent or a hostile
source tries to push through.
"""
from __future__ import annotations

import pytest

from kg_engine.boundary import MIN_EDGE_BUDGET, validate_payload
from kg_engine.canon import Canon
from kg_engine.model import Disposition

SPAN = "x grounds y"  # 11 bytes -> budget falls to the floor


def _flood(n: int) -> dict:
    # n distinct edge identities, all sharing one verifying span
    return {"edges": [{"source": "a", "target": f"n{i}", "relation": "grounds",
                       "span": SPAN, "authored_by": "agent"} for i in range(n)]}


def test_flood_past_budget_is_rejected():
    res = validate_payload(_flood(MIN_EDGE_BUDGET + 6), pack=None, source_text=SPAN)
    written = [r for r in res if r.written]
    flooded = [r for r in res if r.reason == "rate-limited-flood"]
    assert len(written) == MIN_EDGE_BUDGET
    assert len(flooded) == 6
    assert all(r.disposition == Disposition.REJECTED and r.retryable is False for r in flooded)


def test_budget_scales_with_source_size():
    big_source = (SPAN + " ") * 4000  # ~48 KB -> budget well above the floor
    res = validate_payload(_flood(MIN_EDGE_BUDGET + 50), pack=None, source_text=big_source)
    assert all(r.written for r in res if r.kind == "edge")  # nothing flooded on a large source


def test_rate_limit_can_be_disabled():
    res = validate_payload(_flood(MIN_EDGE_BUDGET + 20), pack=None, source_text=SPAN,
                           max_edges_per_kb=None)
    assert all(r.written for r in res if r.kind == "edge")


def test_existing_edges_count_against_the_budget(canon):
    from kg_engine.model import Edge
    existing = [Edge(source="a", target=f"e{i}", relation="grounds", span=SPAN,
                     provenance="span-present") for i in range(MIN_EDGE_BUDGET)]
    res = validate_payload(_flood(3), pack=None, source_text=SPAN, existing=existing)
    assert all(r.reason == "rate-limited-flood" for r in res if r.kind == "edge")


def test_idempotent_rebuild_does_not_flood_resends_or_new_edges():
    """A re-build that re-emits already-canonical edges must not trip the flood limiter: deduped edges
    grow the canon by zero, so they cost no budget and genuinely-new edges still fit (regression)."""
    from kg_engine.model import Edge
    n_existing = MIN_EDGE_BUDGET - 4  # leaves room for 4 net-new under the floor budget
    existing = [Edge(source="a", target=f"e{i}", relation="grounds", span=SPAN,
                     provenance="span-present") for i in range(n_existing)]
    payload = {"edges":
        [{"source": "a", "target": f"e{i}", "relation": "grounds", "span": SPAN, "authored_by": "agent"}
         for i in range(n_existing)]                                  # re-send every existing edge
        + [{"source": "a", "target": f"new{i}", "relation": "grounds", "span": SPAN, "authored_by": "agent"}
           for i in range(4)]}                                        # 4 genuinely new, must fit
    res = validate_payload(payload, pack=None, source_text=SPAN, existing=existing)
    flooded = [r for r in res if r.reason == "rate-limited-flood"]
    new_written = [r for r in res if r.kind == "edge" and r.item.target.startswith("new")
                   and r.written and "deduped" not in r.reason]
    assert not flooded, "idempotent re-sends must not trip the flood limiter"
    assert len(new_written) == 4


def test_null_byte_in_node_id_is_rejected(canon: Canon):
    with pytest.raises(ValueError, match="null byte"):
        canon.node_path("evil\x00id")


def test_traversal_id_is_confined_to_the_vault(canon: Canon):
    notes = canon.notes_dir.resolve()
    for nasty in ["../../etc/passwd", "..\\..\\windows", "/abs/escape", "a/../../b"]:
        p = canon.node_path(nasty)
        assert p.is_relative_to(notes), f"{nasty!r} escaped to {p}"
    assert not canon.exists("../../etc/passwd")
