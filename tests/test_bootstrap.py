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


def test_compute_stamp_reacts_to_interpreter_identity(tmp_path, monkeypatch):
    # F22: the venv's compiled wheels (pydantic-core, igraph, leidenalg) are ABI-bound to
    # the interpreter that built them. A same-path interpreter swap that leaves pyproject
    # UNTOUCHED (unversioned stdlib-venv symlink re-pointed, pyenv re-point, moved arch)
    # must still move the stamp so the venv rebuilds clean instead of importing an
    # ABI-mismatched wheel and crashing. So minor version, sys.platform and the machine
    # arch are all folded into the stamp.
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\ndependencies = ['a']\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)
    base = bootstrap.compute_stamp()

    # A python minor bump (3.11 -> 3.12) with the same pyproject must change the stamp.
    class _VI(tuple):
        @property
        def major(self):  # not used by compute_stamp, kept for realism
            return self[0]

        @property
        def minor(self):
            return self[1]

    monkeypatch.setattr(bootstrap.sys, "version_info", _VI((3, 99, 0, "final", 0)))
    bumped_minor = bootstrap.compute_stamp()
    assert bumped_minor != base

    # An arch move (platform.machine) with the same pyproject + interpreter must too.
    monkeypatch.setattr(bootstrap.sys, "version_info", _VI((3, 99, 0, "final", 0)))
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "definitely-not-this-arch")
    bumped_arch = bootstrap.compute_stamp()
    assert bumped_arch != bumped_minor

    # A platform change (sys.platform: linux -> win32) must too.
    monkeypatch.setattr(bootstrap.sys, "version_info", _VI((3, 99, 0, "final", 0)))
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "definitely-not-this-arch")
    monkeypatch.setattr(bootstrap.sys, "platform", "some-other-os")
    bumped_platform = bootstrap.compute_stamp()
    assert bumped_platform != bumped_arch


def test_compute_stamp_keys_on_explicit_venv_interpreter_identity():
    """review-M7: the stamp keys on the VENV interpreter's identity passed in, not the running one — so a
    different bootstrapping/checking interpreter computes the SAME stamp the build wrote. Distinct
    identities give distinct stamps; the same identity is stable; no-arg falls back to the running one."""
    a = bootstrap.compute_stamp("3.12\0linux\0x86_64")
    b = bootstrap.compute_stamp("3.13\0linux\0x86_64")
    assert a != b
    assert a == bootstrap.compute_stamp("3.12\0linux\0x86_64")
    assert bootstrap.compute_stamp() == bootstrap.compute_stamp(bootstrap._running_identity())


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
        bootstrap.do_install(venv_dir)
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
        bootstrap.do_install(venv_dir)
    assert venv_dir.exists()
    assert sentinel.read_text(encoding="utf-8") == "do not delete me\n"
    assert not bootstrap.venv_python(venv_dir).exists()  # nothing scaffolded into the user dir
    assert not (venv_dir / "pyvenv.cfg").exists()


# --------------------------------------------------------------------------- #
# leidenalg soft probe (SAC-blocked native DLL → graceful degradation)
# --------------------------------------------------------------------------- #
def test_verify_imports_excludes_leidenalg_but_keeps_core():
    # Windows Smart App Control can block leidenalg's unsigned native _c_leiden DLL from
    # LOADING even though it installs fine. At runtime projector._leiden already degrades to
    # label propagation, so a blocked leidenalg must NOT be a mandatory import that aborts
    # provisioning. It moved to a separate soft probe; the hard set keeps only what the server
    # genuinely needs to come up.
    assert "leidenalg" not in bootstrap._VERIFY_IMPORTS
    for mod in ("mcp", "pydantic", "networkx", "igraph", "yaml", "kg_engine"):
        assert mod in bootstrap._VERIFY_IMPORTS


def test_leidenalg_probe_swallows_launch_failure(tmp_path, capsys):
    # The probe must NEVER raise or exit non-zero — even if the interpreter can't be launched
    # at all. A missing interpreter path makes subprocess.run raise FileNotFoundError (an
    # OSError); the probe swallows it and still prints the fallback line.
    bootstrap.probe_leidenalg(tmp_path / "no-such-python")  # must not raise
    assert "label-propagation fallback" in capsys.readouterr().out


def test_leidenalg_probe_reports_status_with_real_interpreter(capfd):
    # Against a REAL interpreter the probe prints exactly one status line and returns None
    # (never raises). The line is emitted by the in-venv child subprocess, so capture at the
    # fd level (capfd, not capsys). Which line appears depends on whether leidenalg loads in
    # THIS environment, so accept either — the contract is "always reports, never fails".
    bootstrap.probe_leidenalg(Path(bootstrap.sys.executable))
    out = capfd.readouterr().out
    assert ("Leiden community detection enabled" in out) or ("label-propagation fallback" in out)


def test_do_install_completes_when_leidenalg_unavailable(tmp_path, monkeypatch):
    # The end-to-end guarantee for the SAC case: even when leidenalg is unimportable, do_install
    # still finishes — writing engine-python.txt + install.stamp and returning the interpreter.
    # The probe is advisory only and can never abort the provision (which would rmtree the venv).
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "PYPROJECT", pp)

    venv_dir = tmp_path / "venv"
    monkeypatch.setattr(bootstrap, "install_with_uv", _fake_install_real_venv)
    monkeypatch.setattr(bootstrap, "install_with_pip", _fake_install_real_venv)
    monkeypatch.setattr(bootstrap, "verify_imports", lambda py: None)  # core imports "succeed"

    called = {"probe": False}

    def fake_probe(py):  # leidenalg blocked: reports unavailable, returns cleanly
        called["probe"] = True
        print("[bootstrap] leidenalg unavailable (ImportError: DLL load failed while importing "
              "_c_leiden); using label-propagation fallback (projector._leiden)")

    monkeypatch.setattr(bootstrap, "probe_leidenalg", fake_probe)

    py = bootstrap.do_install(venv_dir)
    assert called["probe"]                                   # the probe ran (after verify)
    assert py.exists()
    assert (venv_dir / bootstrap.PTR_NAME).exists()
    assert (venv_dir / bootstrap.STAMP_NAME).exists()        # stamp written last => provision OK


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
        bootstrap.do_install(venv_dir)
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


def test_orphan_sideline_does_not_block_steal(tmp_path):
    # F24: a crash between os.replace() and rmtree() in the steal path orphans a non-empty
    # ``.kg-provision.lock.stale-<...>`` dir. A later stealer must not be wedged by it: the
    # steal target is now collision-proof (PID + time_ns) and pre-existing ``*.stale-*``
    # orphans are swept first, so the steal still succeeds.
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True
    lock = bootstrap._lock_dir(venv_dir)
    hb = bootstrap._heartbeat_file(venv_dir)

    # Plant a NON-EMPTY orphan sideline that an earlier crashed stealer left behind, named
    # exactly as the old (PID-only) scheme would have — the case that used to ENOTEMPTY.
    orphan = lock.parent / f"{bootstrap.LOCK_NAME}.stale-{os.getpid()}"
    orphan.mkdir()
    (orphan / "leftover").write_text("crashed mid-steal\n", encoding="utf-8")

    # Age the live lock + heartbeat past the stale threshold so it is genuinely stealable.
    old = time.time() - bootstrap.STALE_LOCK_SECS - 60
    os.utime(lock, (old, old))
    if hb.exists():
        os.utime(hb, (old, old))

    # The steal must succeed despite the orphan, and the orphan must be swept away.
    assert bootstrap.try_acquire(venv_dir) is True
    assert not orphan.exists()
    bootstrap.release(venv_dir)
    # No stale sidelines leaked after a clean steal.
    assert not list(lock.parent.glob(f"{bootstrap.LOCK_NAME}.stale-*"))


def _write_info(venv_dir, *, pid, host, token="tok", t=None):
    """Plant a lock-dir ``info`` record (the steal/release ownership + liveness signal)."""
    lock = bootstrap._lock_dir(venv_dir)
    lock.mkdir(parents=True, exist_ok=True)
    when = bootstrap.time.time() if t is None else t
    (lock / "info").write_text(
        f"pid={pid} host={host} token={token} t={when:.0f}\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# M7 — PID-liveness probe: a crashed holder is reclaimable in ms, not 30 min
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.name == "nt",
    reason="pid-liveness probe is POSIX-only: os.kill(pid, 0) is unsafe on Windows "
    "(CTRL_C_EVENT, not a no-op existence check), so _pid_probe assumes-alive there and a "
    "dead-pid lock is reclaimed by age, not the probe — mirroring canon.LeaseLock._pid_probe.",
)
def test_dead_pid_lock_is_stolen_before_stale_window(tmp_path):
    # M7: a hard-killed background worker freezes its heartbeat, so the age signal stays
    # FRESH for the full 30-min STALE_LOCK_SECS. A cheap os.kill(pid, 0) probe must reclaim
    # it in milliseconds instead — mirroring canon._pid_probe. POSIX-only (see skipif).
    venv_dir = tmp_path / "venv"
    # A held, FRESH lock (heartbeat just now) whose recorded holder is a dead pid on THIS host.
    bootstrap._lock_dir(venv_dir).mkdir(parents=True)
    bootstrap.heartbeat(venv_dir)
    _write_info(venv_dir, pid=_dead_pid(), host=bootstrap._HOST)
    assert bootstrap._lock_age(venv_dir) < bootstrap.STALE_LOCK_SECS  # NOT stale by age
    assert bootstrap.try_acquire(venv_dir) is True  # ...but the dead-pid probe reclaims it
    bootstrap.release(venv_dir)


def test_live_pid_fresh_lock_is_not_stolen(tmp_path):
    # The probe must not over-reclaim: a FRESH lock held by a LIVE pid (our own) stays held.
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True  # records our live pid in info
    age = bootstrap._lock_age(venv_dir)
    assert age < bootstrap.STALE_LOCK_SECS
    assert bootstrap.try_acquire(venv_dir) is False  # live holder is not stolen
    bootstrap.release(venv_dir)


def test_foreign_host_pid_is_treated_as_alive(tmp_path):
    # A pid recorded on ANOTHER host can't be probed locally; treat it as alive (so a fresh
    # lock from a different machine on a shared FS is not stolen by the probe). Only age can
    # reclaim it — exactly canon._pid_probe's cross-host rule.
    venv_dir = tmp_path / "venv"
    bootstrap._lock_dir(venv_dir).mkdir(parents=True)
    bootstrap.heartbeat(venv_dir)
    _write_info(venv_dir, pid=_dead_pid(), host="some-other-host")
    assert bootstrap.try_acquire(venv_dir) is False  # foreign-host pid -> assumed alive
    bootstrap.release(venv_dir)  # we don't own it -> no-op
    assert bootstrap._lock_dir(venv_dir).exists()


# --------------------------------------------------------------------------- #
# M8 — reclaim TOCTOU: a holder that refreshes in the steal window keeps its lock
# --------------------------------------------------------------------------- #
def test_steal_restores_lock_that_became_fresh_in_the_window(tmp_path, monkeypatch):
    # M8: the steal decision reads age, then os.replace()s the lock aside. If the holder's
    # heartbeat fires in that window the MOVED dir is now LIVE; the stealer must re-validate
    # the sidelined dir and put it back (lose the race) instead of destroying a live build's
    # heartbeat. Simulate the window by re-validating against a LIVE pid + fresh heartbeat.
    venv_dir = tmp_path / "venv"
    bootstrap._lock_dir(venv_dir).mkdir(parents=True)
    # Make the lock look stealable to the FIRST check (aged heartbeat + a dead pid)...
    lock = bootstrap._lock_dir(venv_dir)
    hb = bootstrap._heartbeat_file(venv_dir)
    hb.write_text("x\n", encoding="utf-8")
    old = bootstrap.time.time() - bootstrap.STALE_LOCK_SECS - 60
    os.utime(lock, (old, old))
    os.utime(hb, (old, old))
    _write_info(venv_dir, pid=_dead_pid(), host=bootstrap._HOST)

    # ...but the holder "refreshes" in the steal window: re-validation of the SIDELINED dir
    # sees a fresh heartbeat + live pid, so the steal must back off and restore the lock.
    real_replace = bootstrap.os.replace
    state = {"moved": False}

    def replace_then_refresh(src, dst, *a, **k):
        real_replace(src, dst, *a, **k)
        if not state["moved"] and bootstrap.LOCK_NAME + ".stale-" in str(dst):
            state["moved"] = True
            now = bootstrap.time.time()
            os.utime(Path(dst), (now, now))
            os.utime(Path(dst) / "heartbeat", (now, now))
            (Path(dst) / "info").write_text(
                f"pid={os.getpid()} host={bootstrap._HOST} token=live t={now:.0f}\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(bootstrap.os, "replace", replace_then_refresh)
    assert bootstrap.try_acquire(venv_dir) is False  # lost the race -> live holder preserved
    # The lock is back at the live path and still carries the holder's live record.
    assert lock.exists()
    assert bootstrap._parse_info_dir(lock).get("token") == "live"


# --------------------------------------------------------------------------- #
# M9 — release() verifies ownership before rmtree (false-steal-then-revive)
# --------------------------------------------------------------------------- #
def test_release_does_not_destroy_a_foreign_lock(tmp_path):
    # M9: a holder falsely judged stale (suspend/resume past STALE_LOCK_SECS) is stolen by a
    # successor that now holds a BRAND-NEW lock at the same path. When the original holder
    # finally resumes and calls release(), it must NOT rmtree the successor's lock — release
    # only removes a lock whose info still carries OUR token (mirrors LeaseLock.release F15).
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True            # original holder (token A)
    our_token = bootstrap._OWNED_TOKENS[str(bootstrap._lock_dir(venv_dir))]

    # A successor steals the path and writes its OWN token, as a real steal+reacquire would.
    lock = bootstrap._lock_dir(venv_dir)
    (lock / "info").write_text(
        f"pid={os.getpid()} host={bootstrap._HOST} token=successor t={bootstrap.time.time():.0f}\n",
        encoding="utf-8",
    )
    assert our_token != "successor"

    bootstrap.release(venv_dir)  # the original holder releases on its way out
    # The successor's lock survives, with its token intact.
    assert lock.exists()
    assert bootstrap._parse_info_dir(lock).get("token") == "successor"
    # Clean up the successor lock (no recorded ownership -> manual rmtree).
    bootstrap.shutil.rmtree(lock, ignore_errors=True)


def test_release_removes_our_own_lock(tmp_path):
    # The happy path still works: a lock we own (our token in info) is removed by release().
    venv_dir = tmp_path / "venv"
    assert bootstrap.try_acquire(venv_dir) is True
    lock = bootstrap._lock_dir(venv_dir)
    assert lock.exists()
    bootstrap.release(venv_dir)
    assert not lock.exists()
    assert str(lock) not in bootstrap._OWNED_TOKENS  # ownership forgotten on release
    assert not list(lock.parent.glob(f"{bootstrap.LOCK_NAME}.release-*"))  # no sideline leaked


# --------------------------------------------------------------------------- #
# low — heartbeat write failure backstops onto the lock-dir mtime
# --------------------------------------------------------------------------- #
def test_heartbeat_failure_touches_lock_dir_as_backstop(tmp_path, monkeypatch):
    # low/edge-case: if the heartbeat file write keeps failing (read-only fs / ENOSPC / AV),
    # the live holder must still advance _lock_age's FALLBACK signal (the lock-dir mtime) so a
    # genuine >30-min build is not judged stale and stolen. The except branch touches the dir.
    venv_dir = tmp_path / "venv"
    lock = bootstrap._lock_dir(venv_dir)
    lock.mkdir(parents=True)
    # No heartbeat file exists yet and every write_text raises -> the else branch is taken and
    # fails, so the backstop os.utime(lock) must run instead.
    monkeypatch.setattr(bootstrap.Path, "write_text", _raise_oserror)
    old = bootstrap.time.time() - 100
    os.utime(lock, (old, old))
    before = lock.stat().st_mtime
    bootstrap.heartbeat(venv_dir)
    assert not bootstrap._heartbeat_file(venv_dir).exists()  # the hb file never landed
    assert lock.stat().st_mtime > before                     # ...but the dir mtime advanced


def _raise_oserror(*a, **k):
    raise OSError("simulated read-only fs")


def _dead_pid() -> int:
    """A pid that is (almost certainly) not running: spawn a trivial child and reap it."""
    p = subprocess.Popen(
        [bootstrap.sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    p.wait()
    return p.pid


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


# --------------------------------------------------------------------------- #
# foreground --wait default (F23)
# --------------------------------------------------------------------------- #
def test_default_wait_outlasts_stale_lock(tmp_path, monkeypatch):
    # F23: try_acquire() can only STEAL a lock once its heartbeat age passes
    # STALE_LOCK_SECS. A hard-killed holder freezes its heartbeat, so the lock is not
    # stealable until STALE_LOCK_SECS elapses. If the foreground --wait deadline fired
    # FIRST (the old 1200s vs the 1800s stale window), provision() would return 0 without
    # building — silently dropping every kg_* tool for that session. So the default --wait
    # must be >= STALE_LOCK_SECS: one run can both wait out a live build and reclaim a dead
    # one. Capture the wait_secs main() forwards to provision() when no --wait is given.
    seen = {}

    def fake_provision(venv_dir, *, wait_secs, reconcile=False):
        seen["wait_secs"] = wait_secs
        return 0

    monkeypatch.setattr(bootstrap, "provision", fake_provision)
    rc = bootstrap.main(["--venv", str(tmp_path / "venv")])
    assert rc == 0
    assert seen["wait_secs"] >= bootstrap.STALE_LOCK_SECS

    # An explicit --wait override still wins (operator can shorten/lengthen at will).
    bootstrap.main(["--venv", str(tmp_path / "venv"), "--wait", "5"])
    assert seen["wait_secs"] == 5.0
