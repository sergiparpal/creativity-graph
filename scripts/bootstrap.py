#!/usr/bin/env python3
"""Cross-platform, self-provisioning bootstrap for the creativity-graph engine venv.

This is the single source of truth for building the engine's virtualenv. It is
invoked three ways, all idempotent and safe to re-run:

* by the plugin's ``SessionStart`` hook (``hooks/provision.sh`` / ``provision.ps1``,
  dispatched cross-platform by ``hooks/provision.mjs``) right after the plugin is
  installed/loaded — see ``--background`` below;
* by the MCP launcher (``scripts/launch_server.mjs``) as a foreground last-resort if
  the server is spawned before the background provision has finished (graceful catch-up);
* by a developer from a shell (``python scripts/bootstrap.py``).

What it does — adapted to this repo's runtime model:

* creates a venv and installs the engine's **dependencies** (``uv sync
  --no-install-project`` when ``uv`` is on PATH, else stdlib ``venv`` + ``pip``).
  Unlike the sibling ``creativity-amplifier`` plugin, ``kg_engine`` itself is *not*
  imported from the venv's site-packages — it resolves via ``PYTHONPATH=<repo>/scripts``
  (see ``.mcp.json``), so only the dependencies in ``pyproject.toml`` must land here.
* records the resolved venv interpreter path in ``<venv>/engine-python.txt`` so the
  launchers find it on any OS without hard-coding ``bin/python`` vs ``Scripts\\python.exe``.
* with ``--reconcile`` (set by the detached SessionStart worker), runs the canon
  reconcile (§1.8) after the venv is ready — the once-per-session full re-hash that
  re-quarantines mtime-spoofed forged verdicts. Best-effort, never fatal.

Where the venv lives (first match wins):

1. ``--venv PATH``                  (explicit; passed by the MCP launcher)
2. ``$KG_ENGINE_VENV``              (explicit override)
3. ``$CLAUDE_PLUGIN_DATA/.venv``    (installed via marketplace — persists across plugin
                                     updates; the recommended location)
4. ``<repo>/.venv``                 (developer fallback: ``--plugin-dir .`` / shell — the
                                     same venv ``uv sync`` from the repo root builds)

Idempotency & robustness:

* A content stamp (hash of ``pyproject.toml`` — the dependency source of truth — plus the
  backing interpreter's minor version + platform + arch) lets a fast path skip work when the
  venv is current, and forces a rebuild when a plugin update changes dependencies OR a
  same-path interpreter swap would leave its compiled wheels ABI-mismatched. (Engine
  *source* edits need no rebuild: ``kg_engine`` is read live off ``PYTHONPATH``, never
  installed.)
* An atomic lock dir serializes concurrent provisions (the SessionStart worker, extra
  terminals, the launcher racing the hook) so two builds never clobber a venv.
* ``--background`` re-spawns a fully detached worker and returns in milliseconds, so
  even a Claude Code without ``async`` hook support never blocks on the install.

Launch with any system Python >= 3.10:

    python  scripts/bootstrap.py        # Windows (or:  py scripts/bootstrap.py)
    python3 scripts/bootstrap.py        # macOS / Linux
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import venv
from pathlib import Path

# Stdlib-only leaf module: importable with a bare system Python BEFORE the venv deps exist
# (kg_engine/__init__ is import-light), and bootstrap runs as ``python scripts/bootstrap.py``
# so scripts/ is sys.path[0] and ``import kg_engine.atomicio`` resolves.
from kg_engine.atomicio import atomic_write_text

SCRIPT_DIR = Path(__file__).resolve().parent      # <repo>/scripts
REPO_ROOT = SCRIPT_DIR.parent                     # <repo>
PYPROJECT = REPO_ROOT / "pyproject.toml"          # the dependency source of truth

MIN_PY = (3, 10)                    # matches pyproject's requires-python = ">=3.10"
PTR_NAME = "engine-python.txt"      # interpreter pointer, written inside the venv dir
STAMP_NAME = "install.stamp"        # content hash of the install inputs
LOCK_NAME = ".kg-provision.lock"    # atomic lock dir, kept beside the venv
STALE_LOCK_SECS = 30 * 60           # treat a lock older than this as abandoned
# Heartbeat cadence must stay well under STALE_LOCK_SECS so a healthy holder is never
# judged stale and stolen; the poll interval is how often a waiter re-checks the lock.
HEARTBEAT_SECS = STALE_LOCK_SECS / 4
POLL_SECS = 2.0                     # how often a foreground waiter re-checks the lock
LOG_NAME = "provision.log"          # where the detached worker logs
SCHEMA = "1"                        # bump to force every venv to rebuild

# Modules the engine must be able to import for the MCP server to come up. python-igraph
# imports as ``igraph``; pyyaml as ``yaml``. ``kg_engine`` resolves off PYTHONPATH. (Git is
# used only via the ``git`` CLI through subprocess in canon.py — no ``import git`` — so the
# ``git`` module is intentionally absent here and from [project.dependencies].)
#
# ``leidenalg`` is deliberately NOT in this MANDATORY set. It installs fine, but its unsigned
# native ``_c_leiden`` DLL can be blocked from LOADING by Windows Smart App Control /
# Application Control (reputation-based — igraph's DLL loads, leidenalg's may not). At runtime
# it is already OPTIONAL: ``projector._leiden`` wraps the import in try/except and degrades to
# label propagation. So a blocked-but-installed leidenalg must not abort provisioning — it is
# checked separately by ``probe_leidenalg`` (a soft probe that reports status and never fails).
_VERIFY_IMPORTS = (
    "import mcp, pydantic, networkx, igraph, yaml, kg_engine; "
    "print('[bootstrap] core imports OK')"
)


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
def _clean(value: str | None) -> str:
    """Drop empty / unsubstituted ``${...}`` values from env or args."""
    if not value:
        return ""
    value = value.strip()
    if not value or value.startswith("${") or value in ("/.venv", "/venv"):
        return ""
    return value


def resolve_venv_dir(explicit: str | None = None) -> Path:
    """Where the engine venv should live, by priority (see module docstring)."""
    chosen = _clean(explicit)
    if chosen:
        return Path(chosen).expanduser().resolve()

    override = _clean(os.environ.get("KG_ENGINE_VENV"))
    if override:
        return Path(override).expanduser().resolve()

    plugin_data = _clean(os.environ.get("CLAUDE_PLUGIN_DATA"))
    if plugin_data:
        return (Path(plugin_data).expanduser() / ".venv").resolve()

    return (REPO_ROOT / ".venv").resolve()


def venv_python(venv_dir: Path) -> Path:
    """Path to the venv's interpreter for the current OS."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


# --------------------------------------------------------------------------- #
# Idempotency: content stamp
# --------------------------------------------------------------------------- #
def _running_identity() -> str:
    """The ABI identity (minor version + platform + arch) of the interpreter running THIS process."""
    return f"{sys.version_info[0]}.{sys.version_info[1]}\0{sys.platform}\0{platform.machine()}"


def _interp_identity(python_exe=None) -> str:
    """The ABI identity of `python_exe` — the VENV's interpreter, not whatever interpreter happens to be
    running bootstrap. Querying the venv python (not sys.*) makes the stamp track the interpreter that
    actually ABI-binds the wheels, so a DIFFERENT bootstrapping/checking interpreter (e.g. a system-Python
    upgrade between sessions, or uv picking its own python to build the venv) computes the SAME stamp the
    build wrote — no spurious full rebuild of a still-valid venv (review-M7). Falls back to the running
    interpreter when no venv python is available yet (the first build, before the venv exists)."""
    if python_exe is not None:
        code = ("import sys,platform;"
                "print(f'{sys.version_info[0]}.{sys.version_info[1]}'+chr(0)+sys.platform+chr(0)+platform.machine())")
        try:
            out = subprocess.run([str(python_exe), "-c", code], capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return _running_identity()


def compute_stamp(interp_identity: "str | None" = None) -> str:
    """Hash the inputs whose change should trigger a rebuild.

    ``pyproject.toml`` is the declared source of the engine's dependencies (``uv.lock`` is
    gitignored and so never ships in the plugin payload, and ``kg_engine`` is read off
    PYTHONPATH rather than installed, so engine source edits do not require a rebuild).

    The backing interpreter's identity (minor version + platform + arch) is folded in too:
    the venv's compiled wheels (pydantic-core, igraph, leidenalg) are ABI-bound to the
    interpreter that built them, so a same-path interpreter swap that leaves pyproject
    untouched — an unversioned stdlib-venv symlink re-pointed, a pyenv re-point, a moved
    arch — would otherwise keep the stamp matching while ``import`` crashes on a wheel
    ABI mismatch. Hashing the interpreter identity forces a clean rebuild instead.

    `interp_identity` should be the VENV interpreter's identity (see ``_interp_identity``); when omitted
    it falls back to the running interpreter — used only before any venv exists (review-M7).
    """
    ident = interp_identity if interp_identity is not None else _running_identity()
    h = hashlib.sha256()
    h.update(SCHEMA.encode())
    h.update(ident.encode())
    h.update(b"\0")
    h.update(PYPROJECT.name.encode())
    h.update(b"\0")
    h.update(PYPROJECT.read_bytes() if PYPROJECT.exists() else b"")
    return h.hexdigest()


def _current_stamp(venv_dir: Path) -> str:
    """The stamp to compare an existing venv against: hashed with the VENV interpreter's identity (not
    the running one), so a different checking interpreter computes the same stamp the build wrote (M7)."""
    py = venv_python(venv_dir)
    return compute_stamp(_interp_identity(py if py.exists() else None))


def is_ready(venv_dir: Path, stamp: str) -> bool:
    """True when the venv already satisfies the current stamp."""
    py = venv_python(venv_dir)
    ptr = venv_dir / PTR_NAME
    stamp_file = venv_dir / STAMP_NAME
    if not (py.exists() and ptr.exists() and stamp_file.exists()):
        return False
    try:
        return stamp_file.read_text(encoding="utf-8").strip() == stamp
    except OSError:
        return False


def venv_current(venv_dir: Path) -> bool:
    """True when the venv is provisioned and current right now.

    Recomputes the stamp against the VENV interpreter's identity on every call (review-M7),
    so a fresh check after another builder lands the venv compares equal.

    Cheap pre-check FIRST (review-low/perf): if the interpreter pointer / stamp markers are not
    even present yet there is nothing to compare, so return False WITHOUT computing the expensive
    ``_current_stamp`` — which spawns the venv interpreter (``_interp_identity``). A foreground
    ``_wait_for_lock`` poll re-checks readiness every ``POLL_SECS`` while another builder works,
    and the markers only appear once that build finishes, so this keeps the wait loop from
    re-spawning the interpreter on every 2s tick.
    """
    py = venv_python(venv_dir)
    if not (py.exists() and (venv_dir / PTR_NAME).exists() and (venv_dir / STAMP_NAME).exists()):
        return False
    return is_ready(venv_dir, _current_stamp(venv_dir))


# --------------------------------------------------------------------------- #
# Lock (atomic mkdir; steals abandoned locks)
# --------------------------------------------------------------------------- #
def _lock_dir(venv_dir: Path) -> Path:
    # Beside the venv, not inside it, so a half-built venv can't shadow the lock.
    return venv_dir.parent / LOCK_NAME


def _heartbeat_file(venv_dir: Path) -> Path:
    return _lock_dir(venv_dir) / "heartbeat"


# The owner token this process wrote into each lock it currently holds, keyed by the lock
# dir path. release() only rmtrees a lock whose ``info`` still carries OUR token — so a
# holder that was falsely stolen (suspend/resume past STALE_LOCK_SECS) never deletes the
# thief's fresh lock (mirrors canon.LeaseLock.release's ownership re-check, F15). Cleared on
# release; a token surviving here for a lock we no longer own simply never matches the info.
_OWNED_TOKENS: dict[str, str] = {}
_HOST = socket.gethostname()


def _new_token() -> str:
    return uuid.uuid4().hex


def _info_record(token: str) -> str:
    # host+pid drive the liveness probe; the nonce token proves ownership across a stolen lock.
    return f"pid={os.getpid()} host={_HOST} token={token} t={time.time():.0f}\n"


def _parse_info_dir(lock: Path) -> dict[str, str]:
    """Parse the ``info`` record inside lock dir `lock` into a {key: value} map.

    Works on the live lock OR a sidelined copy, so the steal/release re-checks re-read the
    exact dir they moved aside. Missing/unreadable -> {}.
    """
    try:
        text = (lock / "info").read_text("utf-8")
    except OSError:
        return {}
    rec: dict[str, str] = {}
    for tok in text.split():
        key, sep, val = tok.partition("=")
        if sep:
            rec[key] = val
    return rec


def _read_info(venv_dir: Path) -> dict[str, str]:
    """Parse the live lock's ``info`` record into a {key: value} map. Missing/unreadable -> {}."""
    return _parse_info_dir(_lock_dir(venv_dir))


def _pid_probe(rec: dict[str, str]) -> bool:
    """True if the lock's recorded holder is (possibly) alive. Mirrors canon._pid_probe: a
    pid on another host (or no host recorded) is treated as alive, and the probe is skipped
    on Windows (os.kill(pid, 0) there is CTRL_C_EVENT, not a no-op existence check)."""
    try:
        pid = int(rec.get("pid", "0"))
    except ValueError:
        pid = 0
    if not pid:
        return False
    host = rec.get("host", "")
    if host and host != _HOST:
        return True
    if not host:
        return True  # an old info record without a host can't be probed — assume alive
    if os.name == "nt":
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


def _lock_age(venv_dir: Path) -> float:
    """Seconds since the holder last proved it is alive.

    Liveness is judged by the heartbeat file (refreshed by the install loop, see
    ``heartbeat``), NOT by the lock-dir mtime: a long cold source-build (igraph/leidenalg
    from sdist) can outlast STALE_LOCK_SECS without ever touching the dir, and stealing a
    *live* holder's lock lets two installs clobber the same venv. Fall back to the dir
    mtime when no heartbeat has landed yet (the brief window right after mkdir).
    """
    hb = _heartbeat_file(venv_dir)
    lock = _lock_dir(venv_dir)
    try:
        return time.time() - hb.stat().st_mtime
    except OSError:
        try:
            return time.time() - lock.stat().st_mtime
        except OSError:
            return 0.0


def _is_stealable(venv_dir: Path) -> bool:
    """Whether the existing lock may be reclaimed: either its liveness signal has aged past
    STALE_LOCK_SECS, OR a cheap PID-liveness probe shows the recorded holder is dead on this
    host (mirrors canon.LeaseLock._rec_stale: TTL OR a failed os.kill probe). The probe makes
    a crashed background worker reclaimable in milliseconds instead of the full 30-min window.
    """
    if _lock_age(venv_dir) > STALE_LOCK_SECS:
        return True
    return not _pid_probe(_read_info(venv_dir))


def heartbeat(venv_dir: Path) -> None:
    """Stamp the lock as alive. Called periodically by the install loop so a slow but
    healthy build is never mistaken for an abandoned lock and stolen.

    If the heartbeat write fails (read-only fs, ENOSPC, AV/permission hiccup), touch the
    lock dir as a backstop so ``_lock_age``'s fallback path still advances for a live holder
    instead of freezing at the mkdir-time mtime and getting the live build stolen (review-low).
    """
    hb = _heartbeat_file(venv_dir)
    try:
        if hb.exists():
            os.utime(hb, None)
        else:
            hb.write_text(f"pid={os.getpid()} t={time.time():.0f}\n", "utf-8")
    except OSError:
        try:
            os.utime(_lock_dir(venv_dir), None)
        except OSError:
            pass


def try_acquire(venv_dir: Path) -> bool:
    lock = _lock_dir(venv_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    token = _new_token()
    try:
        lock.mkdir()
    except FileExistsError:
        if _is_stealable(venv_dir):
            # Steal discipline mirrors canon.LeaseLock.acquire's reclaim path (the lease-file
            # twin of this mkdir-dir lock); the two are parallel by design — keep in sync.
            # Steal atomically: renaming a directory is atomic, so exactly one racer
            # can move the stale lock aside (the loser finds it already gone and backs
            # off). rmtree+mkdir alone is not atomic together — two simultaneous
            # stealers could both proceed.
            #
            # The sideline name must be collision-proof: a crash between os.replace() and
            # rmtree() orphans a non-empty ``.stale-<...>`` dir, and a stealer that reused a
            # bare PID-only name would then hit ENOTEMPTY on os.replace (masked as a lost
            # race) and never reclaim. ``time_ns()`` makes every steal target unique, and we
            # sweep any pre-existing ``*.stale-*`` orphans first so they can't accumulate.
            # Reap only STALE orphans (mtime older than STALE_LOCK_SECS). A FRESH `.stale-*`/`.release-*`
            # dir may be the IN-FLIGHT sideline of a CONCURRENT stealer/releaser (names are unique by
            # pid+time_ns, so a fresh one isn't ours and isn't a crash orphan yet). rmtree-ing it out from
            # under that racer would make its own re-validation see the dir "vanished" and STEAL a lock it
            # meant to RESTORE — destroying a live holder. Gating on age spares the live racer while still
            # reaping genuine crash orphans (the unique names already prevent the ENOTEMPTY collision).
            now = time.time()
            for pattern in (f"{LOCK_NAME}.stale-*", f"{LOCK_NAME}.release-*"):
                for orphan in lock.parent.glob(pattern):
                    try:
                        if (now - orphan.stat().st_mtime) <= STALE_LOCK_SECS:
                            continue  # too fresh — may be a concurrent racer's in-flight sideline
                    except OSError:
                        continue  # vanished/unreadable under us — nothing to reap
                    shutil.rmtree(orphan, ignore_errors=True)
            sidelined = lock.parent / f"{LOCK_NAME}.stale-{os.getpid()}-{time.time_ns()}"
            try:
                os.replace(lock, sidelined)
            except OSError:
                return False  # lost the steal race; caller re-loops and waits
            # Re-validate the lock we actually moved: if the holder refreshed its heartbeat in
            # the window between our staleness read and this move, we just sidelined a LIVE
            # lock. Put it back and lose the race rather than destroy a live build's heartbeat
            # (closes the residual reclaim TOCTOU, mirroring LeaseLock._reclaim_stale).
            if not _is_stealable_dir(sidelined):
                try:
                    os.replace(sidelined, lock)
                except OSError:
                    shutil.rmtree(sidelined, ignore_errors=True)
                return False
            shutil.rmtree(sidelined, ignore_errors=True)
            try:
                lock.mkdir()
            except OSError:
                return False
        else:
            return False
    try:
        (lock / "info").write_text(_info_record(token), "utf-8")
        _OWNED_TOKENS[str(lock)] = token
    except OSError:
        # The info write failed (ENOSPC, a transient AV/permission hold). Do NOT return success holding
        # an UNOWNED lock: without the token, release() can't remove it (it leaks until the TTL), and
        # with no `info` record _pid_probe reads pid=0, so a concurrent provisioner judges this
        # just-acquired lock dead and STEALS it mid-build — two builds clobbering one venv, the exact
        # race this lock prevents. Abandon cleanly (remove the dir we hold) so the caller re-loops.
        shutil.rmtree(lock, ignore_errors=True)
        return False
    heartbeat(venv_dir)  # seed liveness immediately so a just-acquired lock is never stale
    return True


def _is_stealable_dir(lock: Path) -> bool:
    """Staleness re-check against a SPECIFIC (already sidelined) lock dir, by its own
    heartbeat/dir mtime and PID probe — so the reclaim path re-validates the exact dir it
    moved aside rather than whatever now sits at the live path."""
    hb = lock / "heartbeat"
    try:
        age = time.time() - hb.stat().st_mtime
    except OSError:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return True  # vanished under us — nothing live to protect
    if age > STALE_LOCK_SECS:
        return True
    return not _pid_probe(_parse_info_dir(lock))


def release(venv_dir: Path) -> None:
    """Release the lock — but ONLY if it is still the one THIS process acquired.

    Mirror canon.LeaseLock.release's ownership re-check (F15): a holder that was falsely
    stolen (laptop suspend/resume spanning STALE_LOCK_SECS froze its heartbeat) must not
    rmtree the thief's brand-new lock on its way out. We rename the lock aside (only one
    mover wins), confirm the MOVED ``info`` still carries our token, and only then remove
    it; otherwise we put it back untouched.
    """
    lock = _lock_dir(venv_dir)
    token = _OWNED_TOKENS.get(str(lock))
    if token is None:
        return  # we never recorded ownership of this lock — leave it alone
    sidelined = lock.parent / f"{LOCK_NAME}.release-{os.getpid()}-{time.time_ns()}"
    try:
        os.replace(lock, sidelined)
    except OSError:
        _OWNED_TOKENS.pop(str(lock), None)
        return  # already gone/reclaimed — nothing of ours to release
    if _parse_info_dir(sidelined).get("token") == token:
        shutil.rmtree(sidelined, ignore_errors=True)
        _OWNED_TOKENS.pop(str(lock), None)
        return
    # We moved a foreign/changed lock aside (a successor reclaimed the path) — restore it.
    try:
        os.replace(sidelined, lock)
    except OSError:
        shutil.rmtree(sidelined, ignore_errors=True)
    _OWNED_TOKENS.pop(str(lock), None)


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #
def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    print(f"[bootstrap] $ {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None, env=env)


def install_with_uv(venv_dir: Path, uv: str) -> None:
    """Install dependencies with uv (``uv sync --no-install-project``).

    Pin the environment location with ``UV_PROJECT_ENVIRONMENT`` and resolve from a dir
    that holds ``pyproject.toml``. In dev the project dir IS the repo root (with its
    ``uv.lock``); in an installed plugin we copy ``pyproject.toml`` next to the venv so
    uv has something to resolve. ``--no-install-project`` installs only the dependencies,
    never the ``kg_engine`` package (which is read off PYTHONPATH).
    """
    proj_dir = venv_dir.parent
    if PYPROJECT.resolve() != (proj_dir / "pyproject.toml").resolve():
        proj_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PYPROJECT, proj_dir / "pyproject.toml")
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv_dir)}
    print("[bootstrap] Installing dependencies with uv (sync --no-install-project)", flush=True)
    run([uv, "sync", "--no-install-project"], cwd=proj_dir, env=env)


def install_with_pip(venv_dir: Path) -> None:
    """Fallback when uv is absent: stdlib venv + pip install the project.

    ``pip install <repo>`` builds the ``kg-engine`` wheel via hatchling and installs its
    ``[project.dependencies]``. The bundled ``kg_engine`` lands in site-packages too, but
    is harmlessly shadowed at runtime by ``PYTHONPATH=<repo>/scripts``.
    """
    print("[bootstrap] uv not on PATH — using python -m venv + pip", flush=True)
    try:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    except Exception as exc:  # ensurepip/venv unavailable on some distros
        raise SystemExit(
            f"[bootstrap] Could not create venv: {exc}\n"
            "[bootstrap] On Debian/Ubuntu you may need: sudo apt install python3-venv"
        )
    py = venv_python(venv_dir)
    if not py.exists():
        raise SystemExit(f"[bootstrap] venv interpreter not found at {py}")
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"])
    print("[bootstrap] Installing the engine + dependencies with pip", flush=True)
    run([str(py), "-m", "pip", "install", str(REPO_ROOT)])


def _engine_env() -> dict:
    """Env for subprocessing into engine code: kg_engine resolves off PYTHONPATH, never installed."""
    return {**os.environ, "PYTHONPATH": str(SCRIPT_DIR)}


def verify_imports(py: Path) -> None:
    print("[bootstrap] Verifying core imports", flush=True)
    run([str(py), "-c", _VERIFY_IMPORTS], env=_engine_env())


def probe_leidenalg(py: Path) -> None:
    """Soft-probe the OPTIONAL ``leidenalg`` import in the freshly-built venv — advisory only.

    leidenalg installs fine but its unsigned native ``_c_leiden`` DLL can be blocked from
    LOADING by Windows Smart App Control / Application Control. At runtime that is already
    tolerated (``projector._leiden`` degrades to label propagation), so a blocked import must
    NOT abort the provision the way ``verify_imports`` would. This reports which path the engine
    will take and ALWAYS returns cleanly: it runs a NON-checking subprocess (never ``run()``,
    which is ``check=True``) and swallows every failure — the in-venv import error / DLL-load
    error is caught by the snippet, a parent-side launch failure (OSError) by the ``except``.
    """
    snippet = (
        "try:\n"
        "    import leidenalg\n"
        "    print('[bootstrap] leidenalg OK (Leiden community detection enabled)')\n"
        "except Exception as e:\n"
        "    print('[bootstrap] leidenalg unavailable (' + type(e).__name__ + ': ' + str(e)\n"
        "          + '); using label-propagation fallback (projector._leiden)')\n"
    )
    try:
        subprocess.run([str(py), "-c", snippet], check=False, env=_engine_env())
    except Exception as exc:  # noqa: BLE001 — a blocked/optional dep must never abort provisioning
        print(
            f"[bootstrap] leidenalg unavailable ({type(exc).__name__}: {exc}); "
            "using label-propagation fallback (projector._leiden)",
            flush=True,
        )


def _has_engine_marker(venv_dir: Path) -> bool:
    """True only when the dir carries an ENGINE-specific marker that bootstrap itself writes
    (``engine-python.txt`` / ``install.stamp``) — proof that THIS provisioner built it, so
    deleting it on failure is safe.

    A bare ``pyvenv.cfg`` deliberately does NOT qualify (bootstrap-1): EVERY venv has one,
    including a user's own venv that ``--venv`` / ``KG_ENGINE_VENV`` merely points at. Keying
    ownership on ``pyvenv.cfg`` would let a failed install ``rmtree`` a user's real venv. So we
    key strictly on the files bootstrap writes; a populated dir without one is treated as
    foreign user data and is never scaffolded into nor deleted.
    """
    return (venv_dir / PTR_NAME).exists() or (venv_dir / STAMP_NAME).exists()


def do_install(venv_dir: Path) -> Path:
    if not PYPROJECT.exists():
        raise SystemExit(f"[bootstrap] pyproject.toml not found at {PYPROJECT}")
    uv = shutil.which("uv")
    if uv:
        print(f"[bootstrap] Found uv at {uv} — using it for a faster install", flush=True)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    # Whether the dir was absent or EMPTY before we touched it — i.e. WE are populating it THIS run. On
    # failure such a dir is safe to rmtree even if the install died before any marker exists (a partial
    # pre-config scaffold). Without this, a scaffold we created and then failed on would be neither cleaned
    # (no marker yet) nor buildable next run (the foreign guard would see a populated markerless dir),
    # wedging the venv path until a human deletes it (bootstrap).
    try:
        ours_to_clean = not venv_dir.exists() or not any(venv_dir.iterdir())
    except OSError:
        ours_to_clean = False
    # A PRE-EXISTING, populated dir that carries NO engine marker is FOREIGN — a user's own venv (a bare
    # pyvenv.cfg does NOT make it ours; see _has_engine_marker) or unrelated data that --venv /
    # KG_ENGINE_VENV merely points at (bootstrap-1/4). Refuse to SCAFFOLD into it BEFORE writing anything,
    # so we neither pollute user data with bin/lib nor (below) ever rmtree it. DELIBERATE trade-off: a
    # half-built venv left by a HARD-KILLED previous run (pyvenv.cfg but no marker, its own cleanup never
    # ran) is also treated as foreign and so wedges this path until a human removes it — chosen over any
    # risk of deleting real user data. The COMMON half-built case (this run created it, then failed) is
    # ours_to_clean and is cleaned below.
    preexisting_foreign = (not ours_to_clean) and not _has_engine_marker(venv_dir)
    if preexisting_foreign:
        raise SystemExit(
            f"[bootstrap] refusing to provision into {venv_dir}: it already exists and is not an "
            f"engine venv (no {PTR_NAME} / {STAMP_NAME}). Point --venv / KG_ENGINE_VENV "
            f"at a dedicated path.")

    # Keep the lock alive across a slow source-build so a concurrent provisioner does not
    # mistake a healthy long install for an abandoned lock and steal it (bootstrap-1).
    stop_hb = threading.Event()

    def _pulse() -> None:
        while not stop_hb.wait(HEARTBEAT_SECS):
            heartbeat(venv_dir)

    hb_thread = threading.Thread(target=_pulse, daemon=True)
    hb_thread.start()
    try:
        if uv:
            install_with_uv(venv_dir, uv)
        else:
            install_with_pip(venv_dir)
        py = venv_python(venv_dir)
        if not py.exists():
            raise SystemExit(f"[bootstrap] venv interpreter not found at {py}")
        verify_imports(py)
        # Optional, never fatal: report whether Leiden is loadable or the engine will fall back
        # to label propagation (SAC-blocked DLL). probe_leidenalg swallows all failures.
        probe_leidenalg(py)
    except BaseException:
        # A failed/interrupted install leaves a venv with an interpreter but a partial
        # dependency graph that the next run would silently "reuse". Remove it so the next
        # provision rebuilds clean — but ONLY when it is genuinely ours: either WE created
        # the dir THIS run (ours_to_clean) or it carries an engine marker from a prior build
        # (engine-python.txt / install.stamp). A pre-existing populated dir WITHOUT an engine
        # marker is foreign and was already refused above, so we never reach here for one — but
        # the marker re-check keeps the rmtree from ever touching such a dir even if that guard
        # changed. A bare pyvenv.cfg never qualifies (bootstrap-1). The lock lives BESIDE the
        # venv (_lock_dir), so this never deletes the lock this process still holds.
        if ours_to_clean or _has_engine_marker(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)
        raise
    finally:
        stop_hb.set()
        hb_thread.join(timeout=1.0)

    # Single source of truth for the launchers, on every OS. Forward slashes work in
    # Git Bash, PowerShell, and cmd alike, so the recorded path is shell-agnostic.
    # Pointer first, then stamp (both atomic): the stamp is written STRICTLY LAST, after
    # verify_imports has succeeded, so the presence of a matching stamp implies a verified
    # venv (bootstrap-2) and a crash between pointer and stamp never fakes "ready".
    atomic_write_text(venv_dir / PTR_NAME, py.as_posix(), mkparents=False, fsync_dir=False)
    # Re-stamp with the BUILT venv's own interpreter identity (uv may have built the venv with a
    # different interpreter than the one running bootstrap), so a later --check under yet another
    # interpreter compares equal and doesn't force a spurious rebuild (review-M7).
    atomic_write_text(
        venv_dir / STAMP_NAME,
        compute_stamp(_interp_identity(py)),
        mkparents=False,
        fsync_dir=False,
    )
    print(f"[bootstrap] Engine interpreter: {py.as_posix()}", flush=True)
    print(f"[bootstrap] Wrote {venv_dir / PTR_NAME}", flush=True)
    return py


# --------------------------------------------------------------------------- #
# Reconcile (§1.8) — re-attach/re-quarantine verdicts after the venv is ready
# --------------------------------------------------------------------------- #
def maybe_reconcile(venv_dir: Path) -> None:
    """Run the canon reconcile via the engine python; best-effort, never fatal.

    A FULL sweep here is deliberate: the per-file mtime/size pre-filter is only a
    within-session optimisation, and the once-per-session full re-hash is what actually
    defeats mtime-spoofed forged verdicts (§1.8). Skipped silently when there is no
    project/canon yet (e.g. the very first cold session before anything is built).
    """
    project = _clean(os.environ.get("CLAUDE_PROJECT_DIR"))
    if not project or not (Path(project) / "canon").is_dir():
        return
    py = venv_python(venv_dir)
    if not py.exists():
        return
    snippet = (
        "import os\n"
        "from kg_engine.canon import Canon\n"
        "from kg_engine.reconciler import Reconciler\n"
        "rep = Reconciler(Canon(os.environ['CLAUDE_PROJECT_DIR'])).scan(full_sweep=True)\n"
        "if rep.requarantined:\n"
        "    print(f\"[creativity-graph] reconcile re-quarantined {len(rep.requarantined)} \"\n"
        "          f\"forged verdict(s)\")\n"
    )
    try:
        subprocess.run([str(py), "-c", snippet], check=False, env=_engine_env())
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _ok_with_reconcile(venv_dir: Path, reconcile: bool) -> int:
    """The single success post-condition: on any successful provision, reconcile if asked (§1.8)."""
    if reconcile:
        maybe_reconcile(venv_dir)
    return 0


def _wait_for_lock(venv_dir: Path, deadline: float) -> int | None:
    """Wait until we hold the provision lock or the venv is otherwise current.

    Returns 0 when another builder finished while we waited (caller must still reconcile),
    2 when the wait deadline passed without the venv becoming ready, or None once the lock
    is acquired (caller proceeds to build). Keep release in the caller's finally — this
    helper never releases.
    """
    while not try_acquire(venv_dir):
        # re-evaluate readiness against the VENV interpreter's identity each iteration (M7): once
        # another builder lands the venv, _current_stamp queries that interpreter and matches.
        # Check readiness BEFORE the deadline so a just-finished build returns ready, not 2.
        if venv_current(venv_dir):
            print("[bootstrap] Another setup just finished — engine ready.", flush=True)
            return 0
        if time.time() >= deadline:
            # We gave up waiting and the venv is NOT ready. Return NON-zero (review-low: wait-deadline):
            # a legitimately long cold source-build (igraph/leidenalg from sdist) can outlast the
            # deadline, and returning 0 here would tell the launcher "ready" and launch the server
            # against an unprovisioned venv. 2 = "still provisioning", distinct from a build failure (1).
            print(
                "[bootstrap] Another setup is still in progress past the wait deadline; "
                "it will finish in the background. Try again shortly.",
                flush=True,
            )
            return 2
        time.sleep(POLL_SECS)
    return None


def provision(venv_dir: Path, *, wait_secs: float, reconcile: bool = False) -> int:
    """Ensure the venv is current. Foreground; returns a process exit code."""
    if venv_current(venv_dir):  # compare against the VENV interpreter's identity (review-M7)
        print(f"[bootstrap] Engine already provisioned at {venv_dir}", flush=True)
        return _ok_with_reconcile(venv_dir, reconcile)

    if sys.version_info < MIN_PY:
        sys.stderr.write(
            f"[bootstrap] Need Python >= {MIN_PY[0]}.{MIN_PY[1]} to build the engine, "
            f"but this interpreter is {sys.version.split()[0]} ({sys.executable}).\n"
            "[bootstrap] Install Python 3.10+ (python.org / your package manager / "
            "`winget install Python.Python.3.12`) and start a new session.\n"
        )
        return 3

    # Serialize against any other provisioner (the SessionStart worker, more terminals,
    # the launcher racing the background hook).
    deadline = time.time() + max(0.0, wait_secs)
    waited = _wait_for_lock(venv_dir, deadline)
    if waited == 0:
        return _ok_with_reconcile(venv_dir, reconcile)
    if waited == 2:
        return 2

    try:
        if venv_current(venv_dir):  # re-check now that we hold the lock
            return _ok_with_reconcile(venv_dir, reconcile)
        do_install(venv_dir)
        print("[bootstrap] Done.", flush=True)
        return _ok_with_reconcile(venv_dir, reconcile)
    except subprocess.CalledProcessError as exc:
        # A failed pip/uv/venv command: in the foreground catch-up path (the launcher
        # racing the background build) show a clean, actionable line instead of a raw
        # traceback. The detached worker logs the same to provision.log.
        log_path = venv_dir.parent / LOG_NAME
        sys.stderr.write(
            f"[bootstrap] Install step failed (exit {exc.returncode}): "
            f"{' '.join(str(c) for c in exc.cmd)}\n"
            f"[bootstrap] See {log_path} for details, then start a new session.\n"
        )
        return 1
    finally:
        release(venv_dir)


def spawn_background(venv_dir: Path) -> int:
    """Re-spawn a fully detached worker and return immediately (non-blocking).

    Always spawn — even on a warm session where the venv is already current — because
    the worker also runs the per-session reconcile (§1.8). When the venv is ready the
    worker's ``is_ready`` fast path means it reconciles and exits in milliseconds.
    """
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    log_path = venv_dir.parent / LOG_NAME
    try:
        log = open(log_path, "ab", buffering=0)
    except OSError:
        log = subprocess.DEVNULL  # type: ignore[assignment]

    kwargs: dict = {}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--reconcile", "--venv", str(venv_dir)],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **kwargs,
        )
    finally:
        # The detached child inherited its own dup of the log fd; close the PARENT's copy so it doesn't
        # leak for the parent's lifetime (review-nit). DEVNULL is the int -1 sentinel, not a file object.
        if hasattr(log, "close"):
            try:
                log.close()
            except OSError:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the creativity-graph engine venv.")
    parser.add_argument("--venv", default=None, help="explicit venv directory")
    parser.add_argument(
        "--background",
        action="store_true",
        help="spawn a detached worker and return immediately (used by the hook)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 iff the venv is provisioned and current (matches the stamp), "
        "non-zero otherwise; prints nothing to stdout. Used by the MCP launcher to "
        "detect a STALE venv (old interpreter present but deps changed).",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="run the canon reconcile (§1.8) once the venv is ready",
    )
    parser.add_argument(
        "--wait",
        type=float,
        # Default >= STALE_LOCK_SECS (+margin) so ONE foreground run can both wait out a
        # live build AND reclaim a dead one: a hard-killed holder's heartbeat freezes, so
        # try_acquire() can only steal the lock once its age passes STALE_LOCK_SECS (1800s).
        # A shorter deadline (the old 1200s) would fire BEFORE the lock became stealable and
        # return 0 without building — silently dropping every kg_* tool for that session.
        default=STALE_LOCK_SECS + 60.0,
        help="seconds a foreground run waits for an in-flight provision",
    )
    args = parser.parse_args(argv)

    venv_dir = resolve_venv_dir(args.venv)

    if args.check:
        # Freshness probe for the launcher: silent on stdout (it shares stdout with the
        # JSON-RPC channel), exit code carries the answer. 0 == ready & current.
        return 0 if venv_current(venv_dir) else 1

    print(
        f"[bootstrap] System Python: {sys.version.split()[0]} ({sys.executable})",
        flush=True,
    )
    print(f"[bootstrap] Target venv: {venv_dir}", flush=True)

    if args.background:
        return spawn_background(venv_dir)
    # The detached worker (spawned by --background) and the default/manual path are both
    # foreground here — the worker is just this same foreground provision, re-invoked
    # detached with --reconcile, so there is no separate worker-only entrypoint flag.
    return provision(venv_dir, wait_secs=args.wait, reconcile=args.reconcile)


if __name__ == "__main__":
    raise SystemExit(main())
