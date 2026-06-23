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
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .model import Edge, Node, node_from_markdown, node_to_markdown, slug

LOCK_NAME = ".kg-session-lock"
CANON_SUBDIR = "canon"
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
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

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
        # stale (or vanished after our read): reclaim atomically. Rename the stale lock aside — only
        # ONE racer can move a given inode, so exactly one wins the right to recreate; a racer whose
        # rename fails (someone already moved/removed it) falls through and competes on the O_EXCL.
        sidelined = self.path.with_name(f"{self.path.name}.stale-{self.pid}-{int(now * 1000)}")
        try:
            os.replace(self.path, sidelined)
        except FileNotFoundError:
            pass  # already reclaimed/removed; just try to create
        except OSError:
            return False
        else:
            # Re-validate the record we actually moved: if the owner refreshed its heartbeat in the
            # window between our is_stale() read and this move, we just sidelined a LIVE lock. Put it
            # back and lose the race rather than steal it (closes the residual reclaim TOCTOU).
            moved = self._read_path(sidelined)
            if moved is not None and not self._rec_stale(moved, now):
                try:
                    os.replace(sidelined, self.path)
                except OSError:
                    pass
                return False
            try:
                os.unlink(sidelined)
            except OSError:
                pass
        if self._try_exclusive_create(now):
            return True
        rec2 = self._read()  # lost the recreate race to a fresh acquirer; honor theirs unless it's us
        return bool(rec2 and self._owned_by_self(rec2))

    @staticmethod
    def _read_path(p: Path) -> dict | None:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None

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
        moved = self._read_path(sidelined)
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


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)  # make the rename itself durable, not just the file contents
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _atomic_write(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


# --------------------------------------------------------------------------- Canon


@dataclass
class RollbackInfo:
    rolled_back: bool
    error: str = ""
    stash_ref: str | None = None  # retained for response back-compat; rollback no longer stashes

    @property
    def stashed(self) -> bool:  # back-compat alias for callers that asked "did we roll back?"
        return self.rolled_back


class Canon:
    """Markdown canon rooted at a project dir; notes live under <project>/canon/."""

    def __init__(self, project_dir: str | os.PathLike, *, ensure_layout: bool = True):
        self.root = Path(project_dir)
        self.notes_dir = self.root / CANON_SUBDIR
        self.lock = LeaseLock(self.root / LOCK_NAME)
        self._lock_depth = 0  # re-entrancy guard so nested writes don't deadlock the single-writer lease
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
        if self._lock_depth == 0 and not self.lock.acquire():
            raise RuntimeError("canon vault is locked by another live session")
        self._lock_depth += 1

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
        if existing.id != node.id:
            raise ValueError(
                f"node id slug collision: {node.id!r} and {existing.id!r} both map to {p.name}")

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
        notes_dir = self.notes_dir.resolve()
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
                for node in nodes:
                    # refresh the lease while a long batch is in flight so a concurrent session can't
                    # judge it stale (TTL) and steal the lock mid-write, breaking single-writer.
                    self.lock.heartbeat()
                    merged = self._merge_into_existing(node) if merge else node
                    merged.updated_at = utcnow()
                    _atomic_write(self.node_path(merged.id), node_to_markdown(merged))
            except Exception as e:  # noqa: BLE001 — rollback must catch everything
                return self._rollback(repo, str(e), snapshot)
            # The writes have durably landed. Commit OUTSIDE the rollback try: a non-zero git exit must
            # not revert fsynced canon (F2). check=False so a commit failure is non-fatal and never
            # leaves content staged-but-reverted — same posture as kg_rename's success-path commit.
            if commit and _git_ok(repo):
                _git(repo, "add", "-A", check=False)
                # allow empty so a no-op batch still succeeds
                _git(repo, "commit", "-m", message, "--allow-empty", check=False)
            return RollbackInfo(False)
        finally:
            self._release_lock()

    def _merge_into_existing(self, node: Node) -> Node:
        """Apply the single-canonical-edge rule: merge incoming edges into an existing note."""
        if not self.exists(node.id):
            return node
        self._check_slug_collision(node)
        cur = self.read_node(node.id)
        # key by the canonical edge id (the slug) — the same key the boundary dedup and disk use, so
        # all three layers agree on what "one edge" is (boundary-1 / §1.4).
        by_id = {e.id: e for e in cur.edges}
        for e in node.edges:
            by_id[e.id] = e  # incoming wins (already validated)
        cur.edges = list(by_id.values())
        if node.body:
            cur.body = node.body
        cur.label = node.label or cur.label
        if node.node_type and node.node_type != "undeclared-type":
            cur.node_type = node.node_type
        return cur

    def _rollback(self, repo: Path, error: str, snapshot: dict | None = None) -> RollbackInfo:
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
                    p.write_bytes(original)
        return RollbackInfo(True, error)
