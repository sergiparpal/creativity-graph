"""Unit tests for the extracted §1.8 grounding-audit log (GroundAuditLog) and its contract with the
reconciler reader half. The crash-safe append/truncate durability protocol used to be inline on KGEngine
and only reachable through a full engine; extracting it into a leaf lets the dance be driven directly."""
from __future__ import annotations

from kg_engine.canon import GROUND_AUDIT, Canon
from kg_engine.groundaudit import GroundAuditLog
from kg_engine.reconciler import Reconciler


def test_audited_write_keeps_record_on_success_and_truncates_on_failure(tmp_path):
    log = GroundAuditLog(tmp_path / GROUND_AUDIT)
    assert log.size() == 0

    # a SUCCESSFUL attempt keeps its record and returns the attempt's payload
    out = log.audited_write([("e_x__grounds__y", "unverified", "grounded", "agent")], lambda: (True, "payload"))
    assert out == "payload"
    size_after_ok = log.size()
    assert size_after_ok > 0

    # a FAILED attempt truncates its record back — no orphan survives to inflate the forge count (§1.8)
    out = log.audited_write([("e_p__grounds__q", "unverified", "grounded", "agent")], lambda: (False, None))
    assert out is None
    assert log.size() == size_after_ok  # the failed record was truncated away


def test_reconciler_folds_only_the_surviving_audit_records(tmp_path):
    """The reconciler (the §1.8 READER half) must count exactly the records a successful audited_write
    left behind — and never the truncated orphan of a failed write."""
    canon = Canon(tmp_path, ensure_layout=False)
    log = GroundAuditLog(canon.root / GROUND_AUDIT)
    log.audited_write([("e_x__grounds__y", "unverified", "grounded", "agent")], lambda: (True, None))
    log.audited_write([("e_p__grounds__q", "unverified", "grounded", "agent")], lambda: (False, None))

    counts = Reconciler(canon)._audit_counts()
    assert counts.get("e_x__grounds__y||grounded") == 1   # the surviving record is folded
    assert "e_p__grounds__q||grounded" not in counts      # the truncated orphan is not
