"""Crash-safe atomic file writes — a stdlib-only LEAF module.

Imported by the engine (``canon.py``, ``projector.py``) AND by the installer
(``bootstrap.py``), so it must depend on NOTHING beyond the standard library: bootstrap runs
while building the very venv the engine's third-party deps live in, before those deps are
importable. (``kg_engine.__init__`` is import-light — just ``__version__`` — so importing this
module never pulls in the heavy engine.)

The protocol is temp-file -> flush -> fsync -> ``os.replace``, so a reader ever sees either the
old file or the complete new one, never a torn write. ``fsync_dir`` additionally makes the
rename itself durable across a crash (the directory entry), not only the file contents.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_REPLACE_RETRIES = 5
_REPLACE_BACKOFF = 0.05  # seconds, grows linearly per attempt


def _replace_with_retry(tmp: str, path: Path) -> None:
    """``os.replace(tmp, path)`` with a small bounded retry for the Windows sharing-violation case.

    On Windows, replacing a destination another process holds open WITHOUT ``FILE_SHARE_DELETE`` raises
    ``PermissionError`` (ERROR_SHARING_VIOLATION): e.g. a lease-free canon reader (a second session, the
    per-session reconcile worker, the headless backend) mid-reading the note, or the AV/search indexer
    briefly opening the freshly-renamed file. The lease lock file already retries the same transient
    class (``canon._acquire_lease_blocking``); mirror it here so a momentary concurrent open does not
    fail an otherwise-valid canon write — which, via ``canon.write_nodes``, would spuriously roll back
    the whole batch. A no-op on POSIX, where ``os.replace`` over an open file succeeds."""
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it is durable across a crash (best-effort; not all
    platforms/filesystems support directory fds)."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path, data: bytes, *, mkparents: bool = True, fsync_dir: bool = True
) -> None:
    """Write ``data`` to ``path`` atomically (temp + fsync + ``os.replace``).

    ``mkparents`` creates the parent directory first; ``fsync_dir`` fsyncs the parent after the
    rename so the directory entry is durable too. Callers that know the parent already exists
    and do not need directory durability (e.g. the bootstrap readiness pointer/stamp) pass both
    ``False`` to keep the write minimal.
    """
    path = Path(path)
    if mkparents:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Preserve the destination's existing permission bits across the inode-replacing os.replace.
        # mkstemp fixes the temp at 0o600, so without this every write would silently reset the canon
        # note (a "human-editable" vault file) to owner-only, stripping any group/other bit a user or
        # umask had granted. A brand-new file keeps the 0o600 default — a sensible private default for
        # potentially-sensitive scrubbed content, and there is no prior mode to preserve.
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass  # destination absent (new file) or chmod unsupported — keep the mkstemp default
        _replace_with_retry(tmp, path)
        if fsync_dir:
            _fsync_dir(path.parent)  # make the rename itself durable, not just the contents
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mkparents: bool = True,
    fsync_dir: bool = True,
    encoding: str = "utf-8",
) -> None:
    """Atomic text write — ``atomic_write_bytes`` over ``text.encode(encoding)``."""
    atomic_write_bytes(
        Path(path), text.encode(encoding), mkparents=mkparents, fsync_dir=fsync_dir
    )
