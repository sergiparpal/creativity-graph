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
import subprocess
import sys
import tempfile
import threading
import time
import venv
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent      # <repo>/scripts
REPO_ROOT = SCRIPT_DIR.parent                     # <repo>
PYPROJECT = REPO_ROOT / "pyproject.toml"          # the dependency source of truth

MIN_PY = (3, 10)                    # matches pyproject's requires-python = ">=3.10"
PTR_NAME = "engine-python.txt"      # interpreter pointer, written inside the venv dir
STAMP_NAME = "install.stamp"        # content hash of the install inputs
LOCK_NAME = ".kg-provision.lock"    # atomic lock dir, kept beside the venv
STALE_LOCK_SECS = 30 * 60           # treat a lock older than this as abandoned
LOG_NAME = "provision.log"          # where the detached worker logs
SCHEMA = "1"                        # bump to force every venv to rebuild

# Modules the engine must be able to import for the MCP server to come up. python-igraph
# imports as ``igraph``; pyyaml as ``yaml``. ``kg_engine`` resolves off PYTHONPATH. (Git is
# used only via the ``git`` CLI through subprocess in canon.py — no ``import git`` — so the
# ``git`` module is intentionally absent here and from [project.dependencies].)
_VERIFY_IMPORTS = (
    "import mcp, pydantic, networkx, igraph, leidenalg, yaml, kg_engine; "
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


# --------------------------------------------------------------------------- #
# Lock (atomic mkdir; steals abandoned locks)
# --------------------------------------------------------------------------- #
def _lock_dir(venv_dir: Path) -> Path:
    # Beside the venv, not inside it, so a half-built venv can't shadow the lock.
    return venv_dir.parent / LOCK_NAME


def _heartbeat_file(venv_dir: Path) -> Path:
    return _lock_dir(venv_dir) / "heartbeat"


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


def heartbeat(venv_dir: Path) -> None:
    """Stamp the lock as alive. Called periodically by the install loop so a slow but
    healthy build is never mistaken for an abandoned lock and stolen."""
    hb = _heartbeat_file(venv_dir)
    try:
        if hb.exists():
            os.utime(hb, None)
        else:
            hb.write_text(f"pid={os.getpid()} t={time.time():.0f}\n", "utf-8")
    except OSError:
        pass


def try_acquire(venv_dir: Path) -> bool:
    lock = _lock_dir(venv_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        age = _lock_age(venv_dir)
        if age > STALE_LOCK_SECS:
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
            for orphan in lock.parent.glob(f"{LOCK_NAME}.stale-*"):
                shutil.rmtree(orphan, ignore_errors=True)
            sidelined = lock.parent / f"{LOCK_NAME}.stale-{os.getpid()}-{time.time_ns()}"
            try:
                os.replace(lock, sidelined)
            except OSError:
                return False  # lost the steal race; caller re-loops and waits
            shutil.rmtree(sidelined, ignore_errors=True)
            try:
                lock.mkdir()
            except OSError:
                return False
        else:
            return False
    try:
        (lock / "info").write_text(f"pid={os.getpid()} t={time.time():.0f}\n", "utf-8")
    except OSError:
        pass
    heartbeat(venv_dir)  # seed liveness immediately so a just-acquired lock is never stale
    return True


def release(venv_dir: Path) -> None:
    shutil.rmtree(_lock_dir(venv_dir), ignore_errors=True)


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


def verify_imports(py: Path) -> None:
    print("[bootstrap] Verifying core imports", flush=True)
    env = {**os.environ, "PYTHONPATH": str(SCRIPT_DIR)}
    run([str(py), "-c", _VERIFY_IMPORTS], env=env)


def _atomic_write_text(path: Path, text: str) -> None:
    """Crash-safe text write (temp + fsync + os.replace).

    The pointer/stamp are the engine's readiness gate, so a half-written stamp
    (crash/disk-full mid-write) must never be left behind — it would either fake
    "ready" forever or never rebuild. Writing atomically guarantees readers see either
    the old file or the complete new one.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _looks_like_our_venv(venv_dir: Path) -> bool:
    """True when the dir is a venv this provisioner owns/built (so deleting it on failure
    is safe). We refuse to rmtree an arbitrary pre-existing user dir that --venv /
    KG_ENGINE_VENV merely points at: a populated, non-venv path must never be clobbered."""
    return (
        (venv_dir / "pyvenv.cfg").exists()
        or (venv_dir / PTR_NAME).exists()
        or (venv_dir / STAMP_NAME).exists()
    )


def do_install(venv_dir: Path, stamp: str) -> Path:
    if not PYPROJECT.exists():
        raise SystemExit(f"[bootstrap] pyproject.toml not found at {PYPROJECT}")
    uv = shutil.which("uv")
    if uv:
        print(f"[bootstrap] Found uv at {uv} — using it for a faster install", flush=True)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    # Did this dir already exist (and look like an unrelated user path) before we touched
    # it? If so, never rmtree it on failure (bootstrap-4: --venv may point at user data).
    preexisting_foreign = venv_dir.exists() and not _looks_like_our_venv(venv_dir)
    # Refuse to SCAFFOLD a venv into a populated dir we don't own, BEFORE writing anything — so we
    # neither delete (handled below) nor pollute user data with pyvenv.cfg/bin/lib. An empty foreign
    # dir is fine to build into.
    if preexisting_foreign and any(venv_dir.iterdir()):
        raise SystemExit(
            f"[bootstrap] refusing to provision into {venv_dir}: it already exists and is not an "
            f"engine venv (no pyvenv.cfg / {PTR_NAME} / {STAMP_NAME}). Point --venv / KG_ENGINE_VENV "
            f"at a dedicated path.")

    # Keep the lock alive across a slow source-build so a concurrent provisioner does not
    # mistake a healthy long install for an abandoned lock and steal it (bootstrap-1).
    stop_hb = threading.Event()

    def _pulse() -> None:
        while not stop_hb.wait(STALE_LOCK_SECS / 4):
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
    except BaseException:
        # A failed/interrupted install leaves a venv with an interpreter but a partial
        # dependency graph that the next run would silently "reuse". Remove it so the
        # next provision rebuilds clean — but ONLY when it is a venv we own; a --venv /
        # KG_ENGINE_VENV pointed at a pre-existing populated user dir is left untouched.
        # The lock lives BESIDE the venv (_lock_dir), so this never deletes the lock this
        # process still holds.
        if not preexisting_foreign and _looks_like_our_venv(venv_dir):
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
    _atomic_write_text(venv_dir / PTR_NAME, py.as_posix())
    # Re-stamp with the BUILT venv's own interpreter identity (uv may have built the venv with a
    # different interpreter than the one running bootstrap), so a later --check under yet another
    # interpreter compares equal and doesn't force a spurious rebuild (review-M7).
    _atomic_write_text(venv_dir / STAMP_NAME, compute_stamp(_interp_identity(py)))
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
    env = {**os.environ, "PYTHONPATH": str(SCRIPT_DIR)}
    try:
        subprocess.run([str(py), "-c", snippet], check=False, env=env)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def provision(venv_dir: Path, *, wait_secs: float, reconcile: bool = False) -> int:
    """Ensure the venv is current. Foreground; returns a process exit code."""
    stamp = _current_stamp(venv_dir)  # compare against the VENV interpreter's identity (review-M7)
    if is_ready(venv_dir, stamp):
        print(f"[bootstrap] Engine already provisioned at {venv_dir}", flush=True)
        if reconcile:
            maybe_reconcile(venv_dir)
        return 0

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
    while not try_acquire(venv_dir):
        # re-evaluate readiness against the VENV interpreter's identity each iteration (M7): once
        # another builder lands the venv, _current_stamp queries that interpreter and matches.
        if is_ready(venv_dir, _current_stamp(venv_dir)):
            print("[bootstrap] Another setup just finished — engine ready.", flush=True)
            if reconcile:
                maybe_reconcile(venv_dir)
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
        time.sleep(2.0)

    try:
        if is_ready(venv_dir, _current_stamp(venv_dir)):  # re-check now that we hold the lock
            if reconcile:
                maybe_reconcile(venv_dir)
            return 0
        do_install(venv_dir, stamp)
        print("[bootstrap] Done.", flush=True)
        if reconcile:
            maybe_reconcile(venv_dir)
        return 0
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
        return 0 if is_ready(venv_dir, _current_stamp(venv_dir)) else 1

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
