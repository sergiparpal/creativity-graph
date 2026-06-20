"""The canonical layer (§1.2): human-editable Markdown notes, crash-safe single-writer I/O.

- single-file writes: temp-file + atomic os.replace
- multi-file mutations: write-all-then-one-commit, with git-as-rollback (stash-before-reset)
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


# --------------------------------------------------------------------------- git helpers


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _git_ok(repo: Path) -> bool:
    return (repo / ".git").exists() or _git(repo, "rev-parse", "--git-dir", check=False).returncode == 0


def _has_commit(repo: Path) -> bool:
    return _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode == 0


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
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def is_stale(self, now: float | None = None) -> bool:
        rec = self._read()
        if rec is None:
            return True
        now = time.time() if now is None else now
        if (now - rec.get("heartbeat_at", 0)) > rec.get("ttl", self.ttl):
            return True
        return not _pid_probe(rec.get("pid", 0), rec.get("host", ""), self.host)

    def acquire(self, now: float | None = None) -> bool:
        """Acquire if absent or stale. Returns True on success."""
        rec = self._read()
        now = time.time() if now is None else now
        if rec is not None and not self.is_stale(now):
            return False  # held by a live session
        self._write(now)
        return True

    def _write(self, now: float) -> None:
        rec = {
            "pid": self.pid, "host": self.host,
            "acquired_at": now, "ttl": self.ttl, "heartbeat_at": now,
        }
        _atomic_write(self.path, json.dumps(rec))

    def heartbeat(self, now: float | None = None) -> None:
        rec = self._read() or {}
        rec.update({"pid": self.pid, "host": self.host, "ttl": self.ttl,
                    "heartbeat_at": time.time() if now is None else now})
        rec.setdefault("acquired_at", rec["heartbeat_at"])
        _atomic_write(self.path, json.dumps(rec))

    def release(self) -> None:
        rec = self._read()
        if rec and rec.get("pid") == self.pid and rec.get("host") == self.host:
            self.path.unlink(missing_ok=True)


def _pid_probe(pid: int, host: str, my_host: str) -> bool:
    """True if the pid is (possibly) alive. A pid on another host is treated as alive."""
    if not pid:
        return False
    if host and host != my_host:
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


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------- Canon


@dataclass
class RollbackInfo:
    stashed: bool
    stash_ref: str | None
    error: str


class Canon:
    """Markdown canon rooted at a project dir; notes live under <project>/canon/."""

    def __init__(self, project_dir: str | os.PathLike):
        self.root = Path(project_dir)
        self.notes_dir = self.root / CANON_SUBDIR
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.lock = LeaseLock(self.root / LOCK_NAME)

    # ---- paths
    def node_path(self, node_id: str) -> Path:
        return self.notes_dir / f"{slug(node_id)}.md"

    def exists(self, node_id: str) -> bool:
        return self.node_path(node_id).exists()

    # ---- read
    def read_node(self, node_id: str) -> Node:
        p = self.node_path(node_id)
        return node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=node_id)

    def all_nodes(self) -> list[Node]:
        out = []
        for p in sorted(self.notes_dir.glob("*.md")):
            try:
                out.append(node_from_markdown(p.read_text(encoding="utf-8"), fallback_id=p.stem))
            except ValueError:
                continue
        return out

    def all_edges(self) -> list[Edge]:
        return [e for n in self.all_nodes() for e in n.edges]

    # ---- single-file atomic write
    def write_one(self, node: Node) -> None:
        from .model import utcnow
        node.updated_at = utcnow()
        _atomic_write(self.node_path(node.id), node_to_markdown(node))

    # ---- multi-file mutation with git-as-rollback
    def write_nodes(self, nodes: list[Node], *, message: str, commit: bool = True) -> RollbackInfo:
        """Write/merge a batch of nodes, then one commit. On any failure: stash-before-reset and
        surface the stash so parallel human edits are never lost (§Stage 1)."""
        repo = self.root
        try:
            for node in nodes:
                merged = self._merge_into_existing(node)
                from .model import utcnow
                merged.updated_at = utcnow()
                _atomic_write(self.node_path(merged.id), node_to_markdown(merged))
            if commit and _git_ok(repo):
                _git(repo, "add", "-A")
                # allow empty so a no-op batch still succeeds
                _git(repo, "commit", "-m", message, "--allow-empty")
            return RollbackInfo(False, None, "")
        except Exception as e:  # noqa: BLE001 — rollback must catch everything
            return self._rollback(repo, str(e))

    def _merge_into_existing(self, node: Node) -> Node:
        """Apply the single-canonical-edge rule: merge incoming edges into an existing note."""
        if not self.exists(node.id):
            return node
        cur = self.read_node(node.id)
        by_ident = {e.identity: e for e in cur.edges}
        for e in node.edges:
            by_ident[e.identity] = e  # incoming wins (already validated)
        cur.edges = list(by_ident.values())
        if node.body:
            cur.body = node.body
        cur.label = node.label or cur.label
        if node.node_type and node.node_type != "undeclared-type":
            cur.node_type = node.node_type
        return cur

    def _rollback(self, repo: Path, error: str) -> RollbackInfo:
        if not _git_ok(repo) or not _has_commit(repo):
            return RollbackInfo(False, None, error)
        stash_ref = None
        st = _git(repo, "stash", "push", "-u", "-m", f"kg-rollback-{int(time.time())}", check=False)
        if st.returncode == 0 and "No local changes" not in (st.stdout + st.stderr):
            ref = _git(repo, "rev-parse", "stash@{0}", check=False)
            stash_ref = ref.stdout.strip() or "stash@{0}"
        _git(repo, "reset", "--hard", "HEAD", check=False)
        return RollbackInfo(stash_ref is not None, stash_ref, error)
