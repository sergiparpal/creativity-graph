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
from pathlib import Path


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
        os.replace(tmp, path)
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
