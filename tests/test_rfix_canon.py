"""Regression tests for the `canon` fix batch.

1  — write_nodes success-path git commit: the commit must be SCOPED to the batch's paths (like the
     `git add`), so an unrelated file another process staged concurrently is NOT swept into the vault
     commit.
3  — write_nodes: a second identical write over unchanged content must be a byte-stable no-op — no
     fresh updated_at, no rewrite (which would produce timestamp-only diffs/commits).
4  — atomicio.atomic_write_bytes: a failing unlink in the finally-block cleanup must NOT mask the true
     exception raised by the write itself.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import kg_engine.atomicio as atomicio
import kg_engine.canon as canon_mod
from kg_engine.atomicio import atomic_write_bytes
from kg_engine.canon import Canon
from kg_engine.model import Node


# Self-contained fixtures (do NOT rely on tests/conftest.py, whose imports pull in the whole engine).
@pytest.fixture
def vault(tmp_path: Path) -> Path:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return tmp_path


@pytest.fixture
def canon(vault: Path) -> Canon:
    return Canon(vault)


def _committed_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _staged_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #1

def test_batch_commit_excludes_externally_staged_file(vault: Path):
    """An unrelated file staged by another actor before the batch commit must NOT be recorded by the
    batch's scoped commit — only the notes this batch touched (fix #1)."""
    canon = Canon(vault)
    # Someone else stages an unrelated file into the index (a concurrent process / a hand `git add`).
    unrelated = vault / "unrelated.txt"
    unrelated.write_text("not part of the batch\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(vault), "add", "unrelated.txt"], check=True)

    info = canon.write_nodes([Node(id="alpha", label="Alpha", body="kept")], message="batch alpha")
    assert info.rolled_back is False

    committed = _committed_files(vault)
    assert any("alpha" in f for f in committed), committed  # the note landed in the commit
    assert "unrelated.txt" not in committed, committed      # the foreign staged file did NOT
    # and it is still sitting staged, untouched by our scoped commit
    assert "unrelated.txt" in _staged_files(vault)


# --------------------------------------------------------------------------- #3

def test_identical_rewrite_is_byte_stable_noop(canon: Canon, monkeypatch):
    """A second write of identical content must not rewrite the note nor bump updated_at. Force utcnow
    to advance on every call so the OLD (unconditional bump) behavior would produce different bytes —
    the fixed no-op guard keeps them byte-identical (fix #3)."""
    counter = {"n": 0}

    def fake_utcnow():
        counter["n"] += 1
        return f"2026-01-01T00:00:{counter['n']:02d}Z"

    monkeypatch.setattr(canon_mod, "utcnow", fake_utcnow, raising=False)
    # utcnow is imported inside write_nodes via `from .model import utcnow`; patch the source too.
    import kg_engine.model as model_mod
    monkeypatch.setattr(model_mod, "utcnow", fake_utcnow, raising=False)

    canon.write_nodes([Node(id="beta", label="Beta", body="body text")], message="first")
    first_bytes = canon.node_path("beta").read_bytes()

    # Re-write byte-identical content; the guard must skip the write and the updated_at bump.
    canon.write_nodes([Node(id="beta", label="Beta", body="body text")], message="second")
    second_bytes = canon.node_path("beta").read_bytes()

    assert second_bytes == first_bytes


def test_real_content_change_still_rewrites(canon: Canon):
    """The no-op guard must not suppress a genuine content change — a changed body still lands (fix #3
    negative control)."""
    canon.write_nodes([Node(id="gamma", label="Gamma", body="original")], message="first")
    canon.write_nodes([Node(id="gamma", label="Gamma", body="revised")], message="second")
    assert canon.read_node("gamma").body == "revised"


# --------------------------------------------------------------------------- #4

def test_unlink_failure_does_not_mask_write_error(tmp_path: Path, monkeypatch):
    """When the write fails (here the atomic replace raises) AND the finally-block tmp cleanup ALSO
    raises, the ORIGINAL write exception must propagate — not the unlink OSError (fix #4)."""
    def boom_replace(tmp, path):
        raise RuntimeError("original-write-failure")

    def raising_unlink(_path):
        raise OSError("unlink-should-not-mask")

    monkeypatch.setattr(atomicio, "_replace_with_retry", boom_replace)
    monkeypatch.setattr(atomicio.os, "unlink", raising_unlink)

    with pytest.raises(RuntimeError, match="original-write-failure"):
        atomic_write_bytes(tmp_path / "note.md", b"data", fsync_dir=False)
