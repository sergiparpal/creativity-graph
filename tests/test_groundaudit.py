"""Unit tests for the extracted §1.8 grounding-audit log (GroundAuditLog) and its contract with the
reconciler reader half. The crash-safe append/truncate durability protocol used to be inline on KGEngine
and only reachable through a full engine; extracting it into a leaf lets the dance be driven directly."""
from __future__ import annotations

import pytest

from kg_engine import groundaudit
from kg_engine.canon import GROUND_AUDIT, Canon
from kg_engine.groundaudit import GroundAuditLog, OrphanAuditError
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


def test_truncate_reports_success_and_failure(tmp_path, monkeypatch):
    """M5: truncate() must REPORT outcome (return bool) rather than silently swallowing OSError, so
    audited_write can tell a clean rollback from a surviving orphan (§1.8)."""
    log = GroundAuditLog(tmp_path / GROUND_AUDIT)
    log.append("e_x__grounds__y", "unverified", "grounded", "agent")
    assert log.truncate(0) is True                  # a real truncate succeeds and reports True
    assert log.size() == 0

    # a truncate that can't complete (the open raises OSError) must report False, not pretend success
    def boom(*a, **k):
        raise OSError("read-only remount")
    monkeypatch.setattr(groundaudit, "open", boom, raising=False)
    assert log.truncate(0) is False


def test_audited_write_surfaces_untruncatable_orphan_on_failed_write(tmp_path, monkeypatch):
    """M5/[4]: when a write FAILS and the compensating truncate cannot complete, the orphan record
    SURVIVES — audited_write must raise OrphanAuditError instead of silently returning a clean rollback,
    else the orphan inflates the reconciler's forge count and a later replay slips past (§1.8)."""
    log = GroundAuditLog(tmp_path / GROUND_AUDIT)
    monkeypatch.setattr(log, "truncate", lambda offset: False)  # truncate cannot complete

    with pytest.raises(OrphanAuditError):
        log.audited_write([("e_x__grounds__y", "unverified", "grounded", "agent")],
                          lambda: (False, None))
    # the un-truncatable orphan is still on disk (the failure was surfaced, not hidden)
    assert log.size() > 0


def test_audited_write_compensates_a_throw_inside_the_append_loop(tmp_path, monkeypatch):
    """[5]: an append() that throws on record N (after N-1 are durable) must truncate the partial batch
    back to the captured offset before re-raising — the compensation is not limited to a False from
    attempt(). A pre-existing record (the captured offset) must survive."""
    log = GroundAuditLog(tmp_path / GROUND_AUDIT)
    log.append("e_pre__grounds__z", "unverified", "grounded", "agent")  # establishes a non-zero offset
    baseline = log.size()

    real_append = log.append
    calls = {"n": 0}
    def flaky_append(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:          # first batch record appends; second throws
            raise OSError("fsync EIO on record 2")
        return real_append(*a, **k)
    monkeypatch.setattr(log, "append", flaky_append)

    with pytest.raises(OSError):
        log.audited_write(
            [("e_a__grounds__b", "unverified", "grounded", "agent"),
             ("e_c__grounds__d", "unverified", "grounded", "agent")],
            lambda: (True, None))  # attempt() must never run — the append loop threw first
    # the partial batch was truncated back; only the pre-existing record remains
    assert log.size() == baseline
    counts = Reconciler(Canon(tmp_path, ensure_layout=False))._audit_counts()
    assert counts.get("e_pre__grounds__z||grounded") == 1
    assert "e_a__grounds__b||grounded" not in counts


def test_append_fsyncs_the_parent_directory(tmp_path, monkeypatch):
    """[6]: append() must fsync the parent directory (not only the file fd) so the FIRST append's
    directory entry — the log-file creation — is durable across a crash, mirroring atomicio."""
    seen = []
    real_fsync_dir = groundaudit._fsync_dir
    monkeypatch.setattr(groundaudit, "_fsync_dir",
                        lambda d: (seen.append(d), real_fsync_dir(d)))
    log = GroundAuditLog(tmp_path / GROUND_AUDIT)
    log.append("e_x__grounds__y", "unverified", "grounded", "agent")
    assert tmp_path in seen   # the parent dir was fsync'd on creation
