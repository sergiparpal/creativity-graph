"""The crash-safe append-only grounding-audit log (§1.8) — the WRITER half of the forge-detection
contract whose READER half is ``reconciler._audit_counts``.

Each legitimate verdict transition appends one fsync'd ``{key, from, to, by, at}`` record (the log is
tamper-evidence). The offset/truncate dance lets a verdict's record be dropped if the accompanying canon
write fails, so a failed/rolled-back transition never leaves an ORPHAN record that would inflate the
reconciler's ``consumed`` tally and let a real forgery slip past (§1.8).

Extracted into this leaf (depends only on ``model.utcnow`` + the stdlib) so the durability protocol is
unit-testable without a full engine, and so the §1.8 writer has one home — separate from the KGEngine
facade and from the reconciler that reads it.

It does NOT acquire the canon lease: the caller (``kg_ground`` / ``kg_rename``) holds the lease across the
whole append + write + truncate sequence, so the record and the state change it justifies are atomic
w.r.t. other writers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .atomicio import _fsync_dir
from .model import utcnow


class OrphanAuditError(RuntimeError):
    """A failed transition's compensating ``truncate`` could not complete, so an ORPHAN audit record
    survives in the log. Raised out of ``audited_write`` (never swallowed) so the caller surfaces a hard
    error instead of reporting a clean rollback — an un-truncatable orphan would otherwise inflate the
    reconciler's forge count and let a genuine out-of-band forgery slip past (§1.8)."""


class GroundAuditLog:
    def __init__(self, path: "str | os.PathLike"):
        self.path = Path(path)

    def size(self) -> int:
        """Byte size of the log (0 if absent). Captured BEFORE an append so ``audited_write`` can
        truncate an orphan record back on a failed write."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def append(self, key: str, frm: str, to: str, by: str) -> None:
        """Append one durable ``{key, from, to, by, at}`` record (fsync'd: the log is tamper-evidence).
        Also fsyncs the parent directory so the FIRST append's directory entry (log-file creation) is
        durable across a crash, mirroring the canon's crash-safe write protocol (atomicio, fsync_dir) —
        losing the whole log would silently re-quarantine every legitimate verdict on the next sweep."""
        rec = {"key": key, "from": frm, "to": to, "by": by, "at": utcnow()}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(self.path.parent)  # durable directory entry (a no-op on platforms rejecting dir fds)

    def truncate(self, offset: int) -> bool:
        """Truncate the log back to ``offset`` — undoes records appended for a write that then failed, so
        no orphan record survives. Returns True on success, False on OSError (read-only remount, ENOSPC
        on the metadata update, EIO, the log rotated out from under the open): the caller MUST treat a
        False as a surviving orphan, not a clean rollback (§1.8). Also fsyncs the parent directory so the
        size change is durable, consistent with ``append``/atomicio."""
        try:
            with open(self.path, "r+", encoding="utf-8") as f:
                f.truncate(offset)
                f.flush()
                os.fsync(f.fileno())
            _fsync_dir(self.path.parent)
            return True
        except OSError:
            return False

    def audited_write(self, records, attempt):
        """The crash-safe audit+write dance shared by the verdict-writing handlers (§1.8): capture the
        offset BEFORE appending so an orphan record can be dropped, append each ``(key, frm, to, by)``
        record, run the caller-supplied ``attempt()`` (which returns ``(ok, payload)``), and TRUNCATE the
        audit back iff the write signals failure — so a failed transition never leaves an orphan record
        that would inflate the reconciler's forge count and let a genuine forgery slip past. ``attempt``
        carries the failure SIGNAL from its closure (a caught exception in kg_ground, an
        ``info.rolled_back`` in kg_rename) rather than the helper assuming one, so both failure shapes
        route through the same truncate. Returns the payload ``attempt()`` produced.

        Crash/failure semantics: (1) PER-RECORD durability is the only hard guarantee — each ``append``
        is its own fsync'd write. (2) The BATCH (kg_rename's many migration records) is made consistent
        only by the caller-held lease PLUS this offset/truncate compensation — there is no group-atomic
        append. (3) The compensation covers BOTH a False from ``attempt()`` AND a throw inside the append
        loop itself (an ``append`` that fsync-fails on record N after N-1 are durable): the loop is
        guarded so either path truncates back to the captured offset before unwinding. (4) If the
        compensating truncate cannot complete, the orphan record(s) SURVIVE, so we raise
        ``OrphanAuditError`` rather than returning — the caller must not report a clean rollback (an
        un-truncatable orphan would let a later out-of-band replay defeat forge detection). The CALLER
        must hold the canon lease across this call so the append + write + truncate is atomic (this log
        never locks)."""
        offset = self.size()
        try:
            for key, frm, to, by in records:
                self.append(key, frm, to, by)
        except Exception:  # noqa: BLE001 — a mid-batch append failure must still compensate (§1.8)
            # an append threw after appending 0..N-1 records durably: truncate them back before
            # re-raising so the failure path never leaves un-compensated orphans.
            if not self.truncate(offset):
                raise OrphanAuditError(
                    f"audit append failed and orphan record(s) could not be truncated back to {offset}")
            raise
        ok, payload = attempt()
        if not ok and not self.truncate(offset):
            # the write signalled failure but the compensating truncate could not drop the record(s):
            # surface a hard error so the caller does not report a clean rollback (§1.8).
            raise OrphanAuditError(
                f"write failed and orphan audit record(s) could not be truncated back to {offset}")
        return payload
