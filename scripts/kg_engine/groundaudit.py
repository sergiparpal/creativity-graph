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

from .model import utcnow


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
        """Append one durable ``{key, from, to, by, at}`` record (fsync'd: the log is tamper-evidence)."""
        rec = {"key": key, "from": frm, "to": to, "by": by, "at": utcnow()}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def truncate(self, offset: int) -> None:
        """Truncate the log back to ``offset`` (a no-op on OSError) — undoes records appended for a write
        that then failed, so no orphan record survives."""
        try:
            with open(self.path, "r+", encoding="utf-8") as f:
                f.truncate(offset)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    def audited_write(self, records, attempt):
        """The crash-safe audit+write dance shared by the verdict-writing handlers (§1.8): capture the
        offset BEFORE appending so an orphan record can be dropped, append each ``(key, frm, to, by)``
        record, run the caller-supplied ``attempt()`` (which returns ``(ok, payload)``), and TRUNCATE the
        audit back iff the write signals failure — so a failed transition never leaves an orphan record
        that would inflate the reconciler's forge count and let a genuine forgery slip past. ``attempt``
        carries the failure SIGNAL from its closure (a caught exception in kg_ground, an
        ``info.rolled_back`` in kg_rename) rather than the helper assuming one, so both failure shapes
        route through the same truncate. Returns the payload ``attempt()`` produced. The CALLER must hold
        the canon lease across this call so the append + write + truncate is atomic (this log never
        locks)."""
        offset = self.size()
        for key, frm, to, by in records:
            self.append(key, frm, to, by)
        ok, payload = attempt()
        if not ok:
            self.truncate(offset)
        return payload
