"""The canonical layer (§1.2): human-editable Markdown notes, crash-safe single-writer I/O.

- single-file writes: temp-file + atomic os.replace
- multi-file mutations: snapshot every touched file's bytes, write-all-then-one-commit; on any write
  failure restore the in-memory snapshot (same on git and non-git vaults — git is used only for the
  success-path commit, never for rollback)
- a reclaimable lease lock so a dead/expired session never wedges the vault
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .atomicio import atomic_write_bytes, atomic_write_text
from .model import (
    Edge,
    EpistemicState,
    GROUNDABLE_STATES,
    Node,
    node_from_markdown,
    node_to_markdown,
    slug,
)

LOCK_NAME = ".kg-session-lock"
CANON_SUBDIR = "canon"
# Refresh the lease this many times per TTL while a long batch is in flight (write_nodes), so the
# lease stays comfortably fresh inside the TTL window and a concurrent session never judges it stale.
HEARTBEAT_REFRESHES_PER_TTL = 3
# Bounded-retention housekeeping for the transient dotfiles the I/O paths can leave in the canon dir
# (perf/housekeeping gap). Keep at most this many `.{name}.unreadable-*.bak` per note (newest first) so
# the F28 recoverability intent is honored while the rest are pruned; reap crash-leftover `.tmp-*` and
# sidelined lock files (`.kg-session-lock.stale-*`/`.release-*`) only once they are older than this many
# seconds — long past any live atomic-write/lock-reclaim window — so the reaper never races a write.
BACKUP_RETENTION_PER_NOTE = 3
TRANSIENT_REAP_TTL = 3600.0
# Bounded wait for the single-writer lease before a WRITER gives up. A parallel /kg-build wave funnels
# every kg_write through the ONE single-threaded MCP server process (FastMCP runs sync tools directly on
# the event loop, so the brief write critical section is already serialized there), so the lease is only
# ever genuinely contended ACROSS processes — the detached per-session reconcile worker
# (`bootstrap --reconcile`) or the headless backend racing a server write. Each holder keeps the lease
# only for its own brief write, so a writer that finds it taken retries with exponential backoff and
# serializes cleanly instead of failing outright. Capped so a genuinely wedged LIVE foreign holder
# surfaces as the locked-vault error rather than hanging forever. A DEAD holder on the SAME host is
# reclaimed immediately via staleness inside acquire() (its pid no longer probes alive), so it is never
# waited on; a CROSS-HOST holder's pid can't be probed (and Windows can't probe at all), so such a dead
# holder is only seen stale once its lease TTL lapses — a writer may wait up to this budget meanwhile,
# then surface the error, and a later attempt reclaims it. The default comfortably covers a full max-size
# (10) wave of brief writes serializing; tests override it per Canon (e.g. 0 to assert the old immediate-fail).
LOCK_ACQUIRE_TIMEOUT = 30.0
LOCK_RETRY_INITIAL = 0.05
LOCK_RETRY_MAX = 0.5
# Grounding audit log (kg_ground tamper-evidence). Defined here — the lowest layer that must keep it
# out of git — and re-exported by reconciler so server/tests have one source of truth.
GROUND_AUDIT = ".kg-ground-audit.jsonl"


# --------------------------------------------------------------------------- git helpers


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _git_ok(repo: Path) -> bool:
    return (repo / ".git").exists() or _git(repo, "rev-parse", "--git-dir", check=False).returncode == 0


# --------------------------------------------------------------------------- lease lock (§Stage 1)


@dataclass
class LeaseLock:
    path: Path
    ttl: float = 120.0
    pid: int = 0
    host: str = ""

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.pid:
            self.pid = os.getpid()
        if not self.host:
            self.host = socket.gethostname()

    def _read(self) -> dict | None:
        # The LIVE lock: share the one reader, but fail CLOSED on an unexpected OSError (e.g.
        # PermissionError) so an unreadable HELD lock is never misread as "no record"/free. Only
        # FileNotFoundError (absent) and ValueError (corrupt) read as None here (tolerant defaults False).
        return self._read_path(self.path)

    def _owned_by_self(self, rec: dict) -> bool:
        return rec.get("pid") == self.pid and rec.get("host") == self.host

    def _rec_stale(self, rec: dict | None, now: float) -> bool:
        """Staleness of a specific record (no re-read), so the reclaim path can re-validate the exact
        record it moved aside rather than whatever is at the path now."""
        if rec is None:
            return True
        if (now - rec.get("heartbeat_at", 0)) > rec.get("ttl", self.ttl):
            return True
        return not _pid_probe(rec.get("pid", 0), rec.get("host", ""), self.host)

    def is_stale(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self._rec_stale(self._read(), now)

    def acquire(self, now: float | None = None) -> bool:
        """Acquire if absent, stale, or already held by us. Returns True on success.

        Every transition is a compare-and-swap so two sessions can't both believe they hold the lock:
        the absent case uses an atomic O_EXCL create; a STALE lock is reclaimed by atomically renaming
        it aside (only one racer can move a given inode) and then O_EXCL-creating a fresh one. A blind
        overwrite of a stale lock (the old behavior) let two racers that both observed it stale each
        write and both return True (canon-4).
        """
        now = time.time() if now is None else now
        rec = self._read()
        if rec is None:
            if self._try_exclusive_create(now):
                return True
            rec = self._read()  # lost the create race; re-evaluate below
        if rec is not None and self._owned_by_self(rec):
            self._write(now)  # refresh our own lock (re-acquire is idempotent)
            return True
        if rec is not None and not self.is_stale(now):
            return False  # held by another live session
        # stale (or vanished after our read): reclaim atomically.
        if not self._reclaim_stale(now):
            return False
        if self._try_exclusive_create(now):
            return True
        rec2 = self._read()  # lost the recreate race to a fresh acquirer; honor theirs unless it's us
        return bool(rec2 and self._owned_by_self(rec2))

    def _reclaim_stale(self, now: float) -> bool:
        """Rename the stale lock aside and clear it, so acquire() can O_EXCL-create a fresh one.

        Returns True when the path is free to recreate (we moved-and-dropped the stale record, or it
        had already vanished), False when we must abandon the acquire (a move failure, or the record
        turned out to be LIVE after we moved it). Rename the stale lock aside — only ONE racer can move
        a given inode, so exactly one wins the right to recreate; a racer whose rename fails (someone
        already moved/removed it) falls through and competes on the O_EXCL.
        """
        sidelined = self.path.with_name(f"{self.path.name}.stale-{self.pid}-{int(now * 1000)}")
        try:
            os.replace(self.path, sidelined)
        except FileNotFoundError:
            return True  # already reclaimed/removed; just try to create
        except OSError:
            return False
        # Re-validate the record we actually moved: if the owner refreshed its heartbeat in the
        # window between our is_stale() read and this move, we just sidelined a LIVE lock. Put it
        # back and lose the race rather than steal it (closes the residual reclaim TOCTOU).
        moved = self._read_path(sidelined, tolerant=True)
        if moved is not None and not self._rec_stale(moved, now):
            # Restore the live record. A transient OSError on the reverse rename (EIO/ENOSPC/EPERM —
            # os.replace cannot raise on an existing target or EXDEV here, same parent dir) would
            # otherwise orphan the live owner's only record at the sidelined path and leave self.path
            # empty, so the live owner silently loses its lease until its NEXT acquire() re-O_EXCLs it.
            # Don't blind-`pass`: fall back to writing the record's content back to self.path so the
            # canonical path is never left empty, then drop the sideline. The reaper sweeps any leak.
            self._restore_or_copy_back(sidelined, moved)
            return False
        try:
            os.unlink(sidelined)
        except OSError:
            pass
        return True

    def _restore_or_copy_back(self, sidelined: Path, rec: dict) -> bool:
        """Put a sidelined-but-LIVE lock record back at self.path. Try the atomic reverse rename first;
        on a transient OSError (EIO/ENOSPC/EPERM) fall back to copying the record's content to self.path
        (so the live owner's canonical path is never left empty) and dropping the sideline. Returns True
        if self.path ends up holding the live record. Best-effort throughout — on total failure the live
        owner re-O_EXCLs the path on its next acquire() and the reaper sweeps the sideline."""
        try:
            os.replace(sidelined, self.path)
            return True
        except OSError:
            pass
        # rename failed transiently — write the record's content back so self.path isn't left empty
        try:
            _atomic_write(self.path, json.dumps(rec))
            try:
                os.unlink(sidelined)
            except OSError:
                pass
            return True
        except OSError:
            return False

    @staticmethod
    def _read_path(p: Path, *, tolerant: bool = False) -> dict | None:
        """Parse the JSON lock record at `p`; a missing (FileNotFoundError) or corrupt (ValueError) file
        reads as "no record". The LIVE lock reader (_read) fails CLOSED on any OTHER OSError — an
        unreadable HELD lock must never be misread as free. The SIDELINED reclaim re-read passes
        tolerant=True: it just moved the file aside, so a transient OSError there means "treat as gone"
        and proceed (the move already serialized racers)."""
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        except OSError:
            if tolerant:
                return None
            raise

    def _record(self, now: float) -> dict:
        return {"pid": self.pid, "host": self.host,
                "acquired_at": now, "ttl": self.ttl, "heartbeat_at": now}

    def _try_exclusive_create(self, now: float) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(self._record(now)))
        return True

    def _write(self, now: float) -> None:
        _atomic_write(self.path, json.dumps(self._record(now)))

    def heartbeat(self, now: float | None = None) -> None:
        rec = self._read()
        # Refresh, never acquire: a heartbeat only extends a lock we VERIFIABLY hold. If the record is
        # gone (rec is None) or owned by someone else, do nothing — blind-writing a fresh self-owned
        # record here would be an un-CAS'd acquisition that could steal a path a successor reclaimed
        # after our lease lapsed. Acquisition goes solely through acquire()'s O_EXCL/reclaim CAS (F16).
        if rec is None or not self._owned_by_self(rec):
            return
        now = time.time() if now is None else now
        merged = dict(rec)
        merged.update({"pid": self.pid, "host": self.host, "ttl": self.ttl, "heartbeat_at": now})
        merged.setdefault("acquired_at", now)
        _atomic_write(self.path, json.dumps(merged))

    def release(self) -> None:
        # Read-then-unlink would be a TOCTOU: if our lease lapsed past TTL and a successor reclaimed the
        # path between our _read() and the unlink, a plain unlink(self.path) would delete THEIR lock.
        # Mirror acquire()'s reclaim discipline — rename our lock aside (only one racer can move a given
        # inode), confirm the MOVED record is still ours, then unlink it; if the path was already
        # reclaimed (our rename moved someone else's record, or the record changed under us) put it back
        # and leave the successor's lock untouched (F15).
        rec = self._read()
        if not (rec and self._owned_by_self(rec)):
            return
        sidelined = self.path.with_name(f"{self.path.name}.release-{self.pid}-{int(time.time() * 1000)}")
        try:
            os.replace(self.path, sidelined)
        except (FileNotFoundError, OSError):
            return  # already gone/reclaimed — nothing of ours to release
        moved = self._read_path(sidelined, tolerant=True)
        if moved is not None and self._owned_by_self(moved):
            try:
                os.unlink(sidelined)
            except OSError:
                pass
            return
        # we moved a foreign/changed record aside (a successor reclaimed the path) — restore it
        try:
            os.replace(sidelined, self.path)
        except OSError:
            try:
                os.unlink(sidelined)
            except OSError:
                pass


def _pid_probe(pid: int, host: str, my_host: str) -> bool:
    """True if the pid is (possibly) alive. A pid on another host is treated as alive."""
    if not pid:
        return False
    if host and host != my_host:
        return True
    if os.name == "nt":
        # On Windows, os.kill(pid, 0) does NOT probe liveness: signal 0 is CTRL_C_EVENT, which delivers
        # a console-control event (KeyboardInterrupt) to the target's process group rather than a
        # no-op existence check — it would interrupt us. Skip the probe and rely on the heartbeat/TTL
        # for staleness on Windows (a crashed holder's lock is reclaimed once its TTL lapses).
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False


# --------------------------------------------------------------------------- atomic write

# The crash-safe write protocol (temp + fsync + os.replace + dir-fsync) lives in the stdlib-only
# `atomicio` leaf module so the engine and the installer share one implementation. canon's old
# mkdir + dir-fsync behavior is exactly atomicio's defaults (mkparents/fsync_dir both True). These
# module-level names keep `canon._atomic_write` / `canon._atomic_write_bytes` available for the
# in-package callers and tests that reference them through this module.
_atomic_write_bytes = atomic_write_bytes
_atomic_write = atomic_write_text


# --------------------------------------------------------------------------- Canon


@dataclass
class RollbackInfo:
    rolled_back: bool
    error: str = ""


class Canon:
    """Markdown canon rooted at a project dir; notes live under <project>/canon/."""

    def __init__(self, project_dir: str | os.PathLike, *, ensure_layout: bool = True):
        self.root = Path(project_dir)
        self.notes_dir = self.root / CANON_SUBDIR
        # Resolve the notes dir ONCE — node_path() runs the vault-prefix check 4-5×/node/batch and was
        # re-running notes_dir.resolve() (a syscall) on every call. The path is fixed for this Canon's
        # lifetime, so cache the resolved form here and reuse it (perf #17).
        self._notes_dir_resolved = self.notes_dir.resolve()
        self.lock = LeaseLock(self.root / LOCK_NAME)
        self._lock_depth = 0  # re-entrancy guard so nested writes don't deadlock the single-writer lease
        # Bounded wait a WRITER tolerates for the lease before raising (cross-process contention only —
        # see LOCK_ACQUIRE_TIMEOUT). Per-instance so a test can shorten/zero it; the lazy projector's
        # try_acquire_lock() is unaffected and stays strictly non-blocking.
        self.lock_acquire_timeout = LOCK_ACQUIRE_TIMEOUT
        # ensure_layout=False lets a READ-ONLY consumer (e.g. the precontext PreToolUse hook, which runs
        # on every Grep/Glob/Read) construct a Canon for kg_context reads WITHOUT the constructor side
        # effects: the canon-dir mkdir and the .git/info/exclude rewrite (_ensure_git_excludes re-reads
        # that file on every call). Reads over a missing notes_dir just glob empty; a write through such
        # an instance still self-heals the dir via _atomic_write_bytes' parent mkdir. Default True keeps
        # the original eager-layout behavior for every writer (server/backend/reconciler).
        if ensure_layout:
            self.notes_dir.mkdir(parents=True, exist_ok=True)
            self._ensure_git_excludes()

    def _ensure_git_excludes(self) -> None:
        """Keep transient runtime files (session lock, temp files, reconcile state) out of git in ANY
        git-backed vault, so `git add -A` / stash-as-rollback never commit or discard them — without
        relying on the user having authored a .gitignore."""
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            return  # not a standard repo (worktree/submodule/no-git) — best-effort only
        info = git_dir / "info"
        exclude = info / "exclude"
        # The grounding audit log is runtime tamper-evidence, NOT canon content: it must never be
        # committed by `git add -A` nor swept by a rollback. (Even with the snapshot-scoped rollback
        # below it is untouched, but excluding it keeps it out of commits and out of `stash -u`.)
        patterns = [LOCK_NAME, ".tmp-*", ".kg-reconcile-state.json", GROUND_AUDIT]
        try:
            info.mkdir(parents=True, exist_ok=True)
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            missing = [p for p in patterns if p not in current.split()]
            if missing:
                with open(exclude, "a", encoding="utf-8") as f:
                    if current and not current.endswith("\n"):
                        f.write("\n")
                    f.write("\n".join(missing) + "\n")
        except OSError:
            pass

    # ---- single-writer lease (re-entrant within this process)
    def _acquire_lock(self) -> None:
        if self._lock_depth == 0 and not self._acquire_lease_blocking():
            raise RuntimeError("canon vault is locked by another live session")
        self._lock_depth += 1

    def _acquire_lease_blocking(self) -> bool:
        """Acquire the single-writer lease, retrying with bounded exponential backoff while it is held by
        ANOTHER live session, so near-simultaneous writers SERIALIZE cleanly instead of one failing
        outright (a full parallel /kg-build wave's brief writes, or the detached reconcile worker racing a
        server write — see LOCK_ACQUIRE_TIMEOUT for why contention is only ever cross-process).

        LeaseLock.acquire() is idempotent for the OWNING process (same pid → re-acquire returns True on
        the first attempt), so the server's own serialized writes never enter the backoff loop; the loop
        only spins for a foreign LIVE holder, which keeps the lease only for its own brief write. A foreign
        DEAD holder on the SAME host is reclaimed by acquire() itself (staleness via pid-probe), not by
        waiting; a cross-host (or Windows) dead holder can't be pid-probed, so it is seen stale only once
        its lease TTL lapses — the writer may wait up to the budget first, then a later attempt reclaims it.
        Returns False only after the whole `lock_acquire_timeout` budget elapses (a wedged live, or
        not-yet-TTL-stale cross-host dead, foreign holder), which the caller surfaces as the locked-vault
        error. Only writers reach this; try_acquire_lock() stays strictly non-blocking so the lazy
        projector never stalls a read behind a write."""
        deadline = time.monotonic() + self.lock_acquire_timeout
        backoff = LOCK_RETRY_INITIAL
        while True:
            try:
                if self.lock.acquire():
                    return True
            except OSError:
                # A transient filesystem error while acquiring under contention — most often a Windows
                # sharing violation (PermissionError) when another writer momentarily holds the lock file
                # open for its own read/rename/O_EXCL-create. Treat it as "didn't get the lease this
                # attempt" and retry within the budget instead of crashing the write; this is the same
                # fail-closed posture as the lock reader (an error never reads as "free"). If it persists
                # past the deadline the caller surfaces the locked-vault error rather than the raw OSError.
                pass
            now = time.monotonic()
            if now >= deadline:
                return False
            time.sleep(min(backoff, deadline - now))
            backoff = min(backoff * 2, LOCK_RETRY_MAX)

    def _release_lock(self) -> None:
        self._lock_depth -= 1
        if self._lock_depth <= 0:
            self._lock_depth = 0
            self.lock.release()

    def try_acquire_lock(self) -> bool:
        """Non-raising acquire for best-effort callers (the lazy projector): take the single-writer
        lease if free/ours, else return False so the caller can serve what it has instead of blocking
        or crashing. Re-entrant within this process like _acquire_lock."""
        if self._lock_depth == 0 and not self.lock.acquire():
            return False
        self._lock_depth += 1
        return True

    @staticmethod
    def _assert_no_slug_collision(node_id: str, existing: "Node", p: Path) -> None:
        """Refuse a write whose target file already holds a DIFFERENT node id — two ids that slug to
        the same filename would silently merge into one note. One place so write_one's check and the
        batch merge raise the identical message."""
        if existing.id != node_id:
            raise ValueError(
                f"node id slug collision: {node_id!r} and {existing.id!r} both map to {p.name}")

    def _check_slug_collision(self, node: "Node") -> None:
        """Two distinct ids that slug to the same filename would silently merge into one note.
        Detect and refuse rather than corrupt either node."""
        p = self.node_path(node.id)
        if not p.exists():
            return
        try:
            existing = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=node.id)
        except Exception:
            # An UNREADABLE existing note at the target path. With fallback_id=node.id we cannot tell
            # whether it is the node's OWN corrupt file (the common self-heal case — overwrite-to-repair
            # must keep working) or a distinct/foreign note that would be silently destroyed. Be
            # conservative: back up its raw bytes BEFORE the write proceeds so the overwrite is never
            # lossy, then allow the write (F28). The backup is a dotfile, so note_paths() ignores it.
            self._backup_unreadable(p)
            return
        self._assert_no_slug_collision(node.id, existing, p)

    @staticmethod
    def _backup_unreadable(p: Path) -> None:
        """Preserve the raw bytes of an unreadable note about to be overwritten, under a dotfile sibling
        (hidden from note_paths()), so a foreign/corrupt note is recoverable rather than lost."""
        try:
            data = p.read_bytes()
        except OSError:
            return  # cannot read the bytes at all — nothing to preserve, let the write proceed
        backup = p.with_name(f".{p.name}.unreadable-{int(time.time() * 1000)}.bak")
        try:
            _atomic_write_bytes(backup, data)
        except OSError:
            pass  # best-effort backup; never block the self-heal write on it

    # ---- paths
    def node_path(self, node_id: str) -> Path:
        """Resolve a node id to its canon file, confined to the vault (§Stage 9 hardened resolver).

        `slug()` already strips path separators, dots, and control bytes, so traversal is structurally
        impossible; this is the explicit belt-and-suspenders vault-prefix check (logical chroot): a
        null byte is rejected outright and the resolved path must stay under the canon dir.
        """
        if "\x00" in str(node_id):
            raise ValueError("null byte in node id")
        notes_dir = self._notes_dir_resolved  # cached at __init__ (perf #17) — fixed for this Canon
        p = (notes_dir / f"{slug(node_id)}.md").resolve()
        if p != notes_dir and notes_dir not in p.parents:
            raise ValueError(f"path escapes canon vault: {node_id!r}")
        return p

    def exists(self, node_id: str) -> bool:
        return self.node_path(node_id).exists()

    # ---- read
    def read_node(self, node_id: str) -> Node:
        p = self.node_path(node_id)
        return node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=node_id)

    def note_paths(self) -> list[Path]:
        """Canon note files, excluding the `.tmp-*.md` atomic-write temporaries (a crash between
        mkstemp and os.replace can leave one behind; globbing `*.md` would otherwise treat it as a
        phantom node — canon-5). One place so every reader (here + reconciler) filters identically."""
        return [p for p in sorted(self.notes_dir.glob("*.md")) if not p.name.startswith(".")]

    def reap_transient_files(self, *, now: float | None = None) -> int:
        """Bounded-retention housekeeping for the transient dotfiles the I/O paths leave behind, so a
        long-lived vault does not grow them without limit. Best-effort and idempotent: a failed unlink
        is swallowed and retried next sweep. Returns the count removed.

        - `.{name}.unreadable-*.bak` (F28 self-heal backups): keep the newest BACKUP_RETENTION_PER_NOTE
          per note (so a foreign/corrupt note stays recoverable — the F28 intent) and prune the rest.
        - crash-leftover `.tmp-*` (atomic-write temporaries) and sidelined locks
          (`.kg-session-lock.stale-*`/`.release-*`): prune only once older than TRANSIENT_REAP_TTL —
          well past any live atomic-write or lock-reclaim window — so the reaper never races a write.

        Designed to be wired into the reconciler's periodic full sweep (which already walks the canon
        dir); it lives here because Canon owns the transient-file naming. The lock sidelines sit under
        `root`, the backups/temps under `notes_dir`."""
        now = time.time() if now is None else now
        removed = 0

        def _unlink(p: Path) -> None:
            nonlocal removed
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass  # best-effort — a vanished/locked file is retried next sweep

        def _aged(p: Path) -> bool:
            try:
                return (now - p.stat().st_mtime) > TRANSIENT_REAP_TTL
            except OSError:
                return False  # cannot stat -> leave it for next sweep

        # group `.{name}.unreadable-<ms>.bak` by the note they back up; the <ms> stamp sorts oldest-first.
        backups: dict[str, list[Path]] = {}
        try:
            for p in self.notes_dir.glob(".*.unreadable-*.bak"):
                stem = p.name[1:p.name.rindex(".unreadable-")]  # strip leading dot + ".unreadable-...bak"
                backups.setdefault(stem, []).append(p)
        except OSError:
            backups = {}
        for paths in backups.values():
            for stale in sorted(paths, key=lambda q: q.name)[:-BACKUP_RETENTION_PER_NOTE]:
                _unlink(stale)

        # crash-leftover atomic-write temporaries (notes_dir) and sidelined lock records (root), TTL-gated.
        for p in self.notes_dir.glob(".tmp-*"):
            if _aged(p):
                _unlink(p)
        for pat in (".tmp-*", f"{LOCK_NAME}.stale-*", f"{LOCK_NAME}.release-*"):
            for p in self.root.glob(pat):
                if _aged(p):
                    _unlink(p)
        return removed

    def all_nodes(self) -> list[Node]:
        out = []
        for p in self.note_paths():
            try:
                out.append(node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem))
            except Exception:  # noqa: BLE001 — one unreadable/malformed note must not crash every read
                continue
        return out

    def all_edges(self) -> list[Edge]:
        return [e for n in self.all_nodes() for e in n.edges]

    # ---- single-file atomic write
    def write_one(self, node: Node) -> None:
        from .model import utcnow
        node.updated_at = utcnow()
        self._acquire_lock()
        try:
            self._check_slug_collision(node)
            _atomic_write(self.node_path(node.id), node_to_markdown(node))
        finally:
            self._release_lock()

    # ---- multi-file mutation with snapshot-restore rollback
    def write_nodes(self, nodes: list[Node], *, message: str, commit: bool = True,
                    merge: bool = True) -> RollbackInfo:
        """Write a batch of nodes, then one commit. With `merge` (default) incoming edges are merged
        into existing notes (single-canonical-edge rule); with `merge=False` each node is written
        verbatim (used by kg_rename, which has already rewritten every endpoint and must NOT re-merge
        the pre-rename edges back in). On any WRITE failure restore the pre-batch in-memory byte
        snapshot of every touched file (same on git and non-git vaults), so a partial batch never
        persists (§Stage 1). The commit is OUTSIDE the rollback scope and best-effort: once the atomic
        writes have durably landed, a git failure (unset user.name/email, a rejecting hook, index.lock
        contention) must NOT revert the already-fsynced canon — mirror kg_rename (write, then check=False
        add/commit)."""
        repo = self.root
        self._acquire_lock()
        try:
            # snapshot every target file BEFORE writing so a non-git/pre-commit vault can still roll back
            snapshot = {}
            for n in nodes:
                p = self.node_path(n.id)
                snapshot[p] = p.read_bytes() if p.exists() else None
            try:
                from .model import utcnow
                # Throttle the lease heartbeat: each heartbeat is a full durable lock rewrite
                # (mkstemp+fsync+replace+dir-fsync). Lease correctness comes from the TTL + CAS
                # acquire/reclaim, NOT cadence — refresh at most once per ttl/HEARTBEAT_REFRESHES_PER_TTL
                # so a long batch stays comfortably fresh inside the TTL window. A sub-interval batch
                # heartbeats once.
                hb_interval = self.lock.ttl / HEARTBEAT_REFRESHES_PER_TTL
                last_hb = time.monotonic()
                self.lock.heartbeat()  # one refresh up front, then only when hb_interval has elapsed
                for node in nodes:
                    now_mono = time.monotonic()
                    if (now_mono - last_hb) > hb_interval:
                        # refresh the lease while a long batch is in flight so a concurrent session can't
                        # judge it stale (TTL) and steal the lock mid-write, breaking single-writer.
                        self.lock.heartbeat()
                        last_hb = now_mono
                    merged = self._merge_into_existing(node) if merge else node
                    merged.updated_at = utcnow()
                    _atomic_write(self.node_path(merged.id), node_to_markdown(merged))
            except Exception as e:  # noqa: BLE001 — rollback must catch everything
                return self._rollback(str(e), snapshot)
            # The writes have durably landed. Commit OUTSIDE the rollback try: a non-zero git exit must
            # not revert fsynced canon (F2). check=False so a commit failure is non-fatal and never
            # leaves content staged-but-reverted — same posture as kg_rename's success-path commit.
            if commit and _git_ok(repo):
                # Stage only this batch's paths (`snapshot` already knows them); `git add -A` would
                # rescan the whole working tree per boundary batch. Still best-effort (check=False) —
                # outside the rollback scope, must stay fail-open.
                if snapshot:
                    _git(repo, "add", "--", *[str(p) for p in snapshot], check=False)
                # allow empty so a no-op batch still succeeds
                _git(repo, "commit", "-m", message, "--allow-empty", check=False)
            return RollbackInfo(False)
        finally:
            self._release_lock()

    def _merge_into_existing(self, node: Node) -> Node:
        """Apply the single-canonical-edge rule: merge incoming edges into an existing note."""
        p = self.node_path(node.id)
        if not p.exists():
            return node
        # Parse the existing note once here and fold the slug-collision check in, so the batch path
        # never double-parses. An unreadable existing note is backed up and the parse error re-raised
        # (the merge path then rolls back the batch); a readable note whose id differs raises the
        # slug-collision ValueError via the shared check.
        try:
            cur = node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=node.id)
        except Exception:
            self._backup_unreadable(p)  # preserve foreign/corrupt bytes before anything overwrites them
            raise
        self._assert_no_slug_collision(node.id, cur, p)
        # key by the canonical edge id (the slug) — the same key the boundary dedup and disk use, so
        # all three layers agree on what "one edge" is (boundary-1 / §1.4).
        by_id = {e.id: e for e in cur.edges}
        for e in node.edges:
            prev = by_id.get(e.id)
            # verdict-durability defense-in-depth (review-C1, §1.8): never silently downgrade a
            # verdict-bearing edge back to `unverified` on a merge. The write boundary already
            # quarantines such re-emits, so in normal flow this never fires; it protects any direct
            # write_nodes(merge=True) caller. The reconciler's LEGITIMATE demote-to-unverified goes
            # through write_one (no merge), so it is unaffected; kg_ground stamps a non-`unverified`
            # state, so its merges don't trip this either.
            if (prev is not None and prev.epistemic_state in GROUNDABLE_STATES
                    and e.epistemic_state == EpistemicState.UNVERIFIED):
                # Preserve not just the verdict state but the evidence it rests on. The reachable path
                # is a kg_propose re-proposal of an already-grounded edge (the hypothesized lane skips
                # the verdict_ids check, boundary.py), whose bare incoming object would otherwise revert
                # a PROMOTED hypothesis's provenance (e.g. back to `hypothesized`), blank its support
                # span, and drop the verdict notes — the citation / falsification rationale §1.7 must
                # survive forever. Carry prev's verdict-associated fields so the stored edge stays a
                # consistent grounded/rejected/failed object, not a verdict floating over empty support.
                e.epistemic_state = prev.epistemic_state
                e.verdict_by = prev.verdict_by
                e.verdict_at = prev.verdict_at
                e.provenance = prev.provenance
                e.span = prev.span
                e.notes = prev.notes
            by_id[e.id] = e  # incoming wins (already validated)
        cur.edges = list(by_id.values())
        if node.body:
            cur.body = node.body
        cur.label = node.label or cur.label
        if node.node_type and node.node_type != "undeclared-type":
            cur.node_type = node.node_type
        return cur

    def _rollback(self, error: str, snapshot: dict | None = None) -> RollbackInfo:
        """Undo a failed batch by restoring ONLY the files it touched, from the pre-batch snapshot.

        This is the same scoped restore on both git and non-git vaults. A repo-wide `git reset --hard
        HEAD` (the old git path) would also discard unrelated UNCOMMITTED work — most importantly the
        grounding verdicts kg_ground writes via write_one without a commit, plus in-progress hand
        edits — silently reverting them to their last committed state. Scoping to `snapshot` keeps the
        rollback confined to this batch and never disturbs anything else in the working tree.
        """
        if snapshot:
            for p, original in snapshot.items():
                if original is None:
                    p.unlink(missing_ok=True)  # file was newly created by this batch -> remove it
                else:
                    # atomic + fsynced restore, consistent with the rest of the module — a crash mid
                    # rollback must not leave a half-written note (review-low: rollback non-atomic).
                    _atomic_write_bytes(p, original)
        return RollbackInfo(True, error)
