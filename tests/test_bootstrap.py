"""Hermetic tests for the self-provisioning bootstrap (scripts/bootstrap.py).

These exercise only the pure logic — path resolution, the idempotency stamp, the
readiness check, the concurrency lock, and the failure-cleanup contract. No venv is
created and nothing is installed, so the suite stays offline.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest

_BOOT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.py"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("kg_bootstrap", _BOOT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bootstrap = _load_bootstrap()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Drop any inherited provisioning env so resolution is deterministic."""
    for var in ("KG_ENGINE_VENV", "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# _clean / resolve_venv_dir
# --------------------------------------------------------------------------- #
def test_clean_drops_empty_and_unsubstituted():
    assert bootstrap._clean("") == ""
    assert bootstrap._clean(None) == ""
    assert bootstrap._clean("   ") == ""
    # an unsubstituted ${VAR} (e.g. CLAUDE_PLUGIN_DATA unset in dev) must not be used
    assert bootstrap._clean("${CLAUDE_PLUGIN_DATA}/.venv".split("/")[0]) == ""
    # the bare-substitution sentinels (KG_ENGINE_VENV / DATA empty -> "/.venv" | "/venv")
    assert bootstrap._clean("/.venv") == ""
    assert bootstrap._clean("/venv") == ""
    assert bootstrap._clean("  /real/path ") == "/real/path"


def test_resolve_priority_explicit_arg_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_ENGINE_VENV", str(tmp_path / "env"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    got = bootstrap.resolve_venv_dir(str(tmp_path / "explicit"))
    assert got == (tmp_path / "explicit").resolve()


def test_resolve_priority_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_ENGINE_VENV", str(tmp_path / "env"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert bootstrap.resolve_venv_dir(None) == (tmp_path / "env").resolve()


def test_resolve_priority_plugin_data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert bootstrap.resolve_venv_dir(None) == (tmp_path / "data" / ".venv").resolve()


def test_resolve_dev_fallback():
    # No env, no arg, and an empty/unsubstituted --venv all fall back to the dev tree
    # (the same <repo>/.venv that `uv sync` from the repo root builds).
    expected = (bootstrap.REPO_ROOT / ".venv").resolve()
    assert bootstrap.resolve_venv_dir(None) == expected
    assert bootstrap.resolve_venv_dir("") == expected
    assert bootstrap.resolve_venv_dir("/.venv") == expected


def test_venv_python_matches_os(tmp_path):
    py = bootstrap.venv_python(tmp_path)
    if os.name == "nt":
        assert py == tmp_path / "Scripts" / "python.exe"
    else:
        assert py == tmp_path / "bin" / "python"


# --------------------------------------------------------------------------- #
# compute_stamp
# --------------------------------------------------------------------------- #
def test_compute_stamp_is_deterministic():
    assert bootstrap.compute_stamp() == bootstrap.compute_stamp()


def test_compute_stamp_reacts_to_pyproject(tmp_path, monkeypatch):
    # A plugin update that changes pyproject.toml (the dependency source of truth) must
    # change the stamp and so force a rebuild.
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\ndependencies = ['a']\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)
    s1 = bootstrap.compute_stamp()
    assert s1 == bootstrap.compute_stamp()
    pp.write_text("[project]\ndependencies = ['a', 'b']\n", encoding="utf-8")
    assert bootstrap.compute_stamp() != s1


# --------------------------------------------------------------------------- #
# is_ready
# --------------------------------------------------------------------------- #
def _fake_venv(venv_dir: Path, stamp: str) -> None:
    py = bootstrap.venv_python(venv_dir)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!stub\n", encoding="utf-8")
    (venv_dir / bootstrap.PTR_NAME).write_text(py.as_posix(), encoding="utf-8")
    (venv_dir / bootstrap.STAMP_NAME).write_text(stamp, encoding="utf-8")


def test_is_ready_false_when_missing(tmp_path):
    assert bootstrap.is_ready(tmp_path / "venv", "abc") is False


def test_is_ready_true_when_complete_and_matching(tmp_path):
    venv_dir = tmp_path / "venv"
    _fake_venv(venv_dir, "abc")
    assert bootstrap.is_ready(venv_dir, "abc") is True
    # A changed stamp (e.g. plugin update changed deps) invalidates readiness.
    assert bootstrap.is_ready(venv_dir, "different") is False


# --------------------------------------------------------------------------- #
# do_install failure cleanup
# --------------------------------------------------------------------------- #
def _fake_install_real_venv(vd, *a, **k):
    # A real install creates a venv (pyvenv.cfg) plus an interpreter; mirror that so the
    # failure-cleanup path sees a dir that "looks like ours".
    py = bootstrap.venv_python(vd)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!stub\n", encoding="utf-8")
    (vd / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")


def test_do_install_removes_venv_on_failure(tmp_path, monkeypatch):
    # A failed dep install must not leave a partial venv that the next run would later
    # "reuse"; do_install removes it so the next provision rebuilds clean.
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)

    venv_dir = tmp_path / "venv"

    def fail_verify(py):
        raise subprocess.CalledProcessError(1, ["uv", "sync"])

    monkeypatch.setattr(bootstrap, "install_with_uv", _fake_install_real_venv)
    monkeypatch.setattr(bootstrap, "install_with_pip", _fake_install_real_venv)
    monkeypatch.setattr(bootstrap, "verify_imports", fail_verify)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.do_install(venv_dir, "stamp")
    assert not venv_dir.exists()


def test_do_install_keeps_preexisting_foreign_dir_on_failure(tmp_path, monkeypatch):
    # bootstrap-4: --venv / KG_ENGINE_VENV may point at a pre-existing populated USER dir.
    # A failed install must NOT blindly rmtree it (that would delete user data); only a
    # dir we own (pyvenv.cfg / engine-python.txt / install.stamp) may be removed.
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)

    venv_dir = tmp_path / "user-data"
    venv_dir.mkdir()
    sentinel = venv_dir / "important.txt"
    sentinel.write_text("do not delete me\n", encoding="utf-8")

    # do_install refuses upfront (SystemExit) rather than scaffolding a venv into the populated
    # foreign dir — so the user's data is neither deleted NOR polluted with venv files.
    with pytest.raises(SystemExit):
        bootstrap.do_install(venv_dir, "stamp")
    assert venv_dir.exists()
    assert sentinel.read_text(encoding="utf-8") == "do not delete me\n"
    assert not bootstrap.venv_python(venv_dir).exists()  # nothing scaffolded into the user dir
    assert not (venv_dir / "pyvenv.cfg").exists()


def test_stamp_written_strictly_last(tmp_path, monkeypatch):
    # bootstrap-2: a matching stamp must imply a VERIFIED venv. If verify_imports fails the
    # stamp is never written, so is_ready() can never report a half-built venv as ready.
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)

    venv_dir = tmp_path / "venv"
    monkeypatch.setattr(bootstrap, "install_with_uv", _fake_install_real_venv)
    monkeypatch.setattr(bootstrap, "install_with_pip", _fake_install_real_venv)

    def fail_verify(py):
        raise subprocess.CalledProcessError(1, ["uv", "sync"])

    monkeypatch.setattr(bootstrap, "verify_imports", fail_verify)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.do_install(venv_dir, "stamp")
    assert not (venv_dir / bootstrap.STAMP_NAME).exists()


# --------------------------------------------------------------------------- #
# lock
# --------------------------------------------------------------------------- #
def test_lock_is_mutually_exclusive(tmp_path):
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True
    assert bootstrap.try_acquire(venv_dir) is False  # second caller is locked out
    bootstrap.release(venv_dir)
    assert bootstrap.try_acquire(venv_dir) is True    # released -> acquirable again
    bootstrap.release(venv_dir)


def test_stale_lock_is_stolen(tmp_path):
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True
    lock = bootstrap._lock_dir(venv_dir)
    hb = bootstrap._heartbeat_file(venv_dir)
    # Age BOTH the lock dir and the heartbeat: liveness is judged by the heartbeat, so an
    # abandoned holder (no recent heartbeat) is what makes a lock genuinely stealable.
    old = time.time() - bootstrap.STALE_LOCK_SECS - 60
    os.utime(lock, (old, old))
    if hb.exists():
        os.utime(hb, (old, old))
    # A fresh provisioner reclaims an abandoned lock instead of waiting forever.
    assert bootstrap.try_acquire(venv_dir) is True
    bootstrap.release(venv_dir)


def test_fresh_but_long_lock_is_not_stolen(tmp_path):
    # bootstrap-1: a slow cold source-build (igraph/leidenalg from sdist) can outlive
    # STALE_LOCK_SECS while still healthy. The holder refreshes a heartbeat during install,
    # so even when the lock DIR mtime is ancient a recent heartbeat keeps the lock live and
    # a concurrent provisioner must NOT steal it (stealing -> two installs clobber one venv).
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True
    lock = bootstrap._lock_dir(venv_dir)
    # The dir itself looks ancient...
    old = time.time() - bootstrap.STALE_LOCK_SECS - 60
    os.utime(lock, (old, old))
    # ...but the holder just sent a heartbeat (the install loop is alive).
    bootstrap.heartbeat(venv_dir)
    assert bootstrap._heartbeat_file(venv_dir).exists()
    assert bootstrap.try_acquire(venv_dir) is False  # live holder is not stolen
    bootstrap.release(venv_dir)


# --------------------------------------------------------------------------- #
# --check (launcher freshness probe; node-launchers-2)
# --------------------------------------------------------------------------- #
def test_check_exit_code_tracks_readiness(tmp_path, monkeypatch, capsys):
    # The MCP launcher runs `bootstrap.py --check --venv DIR` to detect a STALE venv (old
    # interpreter present but deps changed). It must exit 0 iff is_ready and print nothing
    # to stdout (it shares stdout with the JSON-RPC channel).
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\ndependencies = ['a']\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)

    venv_dir = tmp_path / "venv"
    argv = ["--check", "--venv", str(venv_dir)]

    # Not provisioned yet -> non-zero, silent.
    assert bootstrap.main(argv) != 0
    assert capsys.readouterr().out == ""

    # Provision with the CURRENT stamp -> exit 0.
    _fake_venv(venv_dir, bootstrap.compute_stamp())
    assert bootstrap.main(argv) == 0
    assert capsys.readouterr().out == ""

    # A deps change moves the stamp; the old (now-stale) venv -> non-zero.
    pp.write_text("[project]\ndependencies = ['a', 'b']\n", encoding="utf-8")
    assert bootstrap.main(argv) != 0
    assert capsys.readouterr().out == ""
