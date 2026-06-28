"""Hermetic smoke tests for the Node/Python launcher + hook glue.

These cover the cross-platform launcher wiring that the Python suite otherwise never
touches: that ``launch_server.mjs`` resolves the engine venv dir to the SAME place
``bootstrap.resolve_venv_dir`` does (the .mjs hand-mirrors that precedence and can drift),
that ``precontext.py`` is a true no-op when nothing has been projected (no writable Canon,
no canon dir created), and that every shipped ``.mjs`` parses under ``node --check``.

Node-dependent tests skip cleanly when ``node`` is not on PATH; nothing here installs a
venv or reaches the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_BOOT_PATH = REPO / "scripts" / "bootstrap.py"
_LAUNCH_MJS = REPO / "scripts" / "launch_server.mjs"
_PRECONTEXT_MJS = REPO / "hooks" / "precontext.mjs"
_PROVISION_MJS = REPO / "hooks" / "provision.mjs"
_CANON_MERGE_MJS = REPO / "scripts" / "canon_merge_driver.mjs"
_ENGINE_RESOLVE_MJS = REPO / "scripts" / "_engine_resolve.mjs"  # shared resolver every launcher imports
_PRECONTEXT_PY = REPO / "hooks" / "precontext.py"

NODE = shutil.which("node")


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("kg_bootstrap_launchers", _BOOT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bootstrap = _load_bootstrap()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Drop inherited provisioning env so resolution is deterministic in both engines."""
    for var in ("KG_ENGINE_VENV", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# (1) launch_server.mjs venv-dir precedence AGREES with bootstrap.resolve_venv_dir
# --------------------------------------------------------------------------- #
# Evaluate the REAL venvDir() that every launcher imports from scripts/_engine_resolve.mjs (not a
# re-implementation) so a drift in the .mjs precedence is caught. We import the actual export and call
# it with ROOT (which the launchers pass from CLAUDE_PLUGIN_ROOT), then print the chosen dir as JSON.
_HARNESS = r"""
import { venvDir } from __MODURL__;
const ROOT = process.env.CLAUDE_PLUGIN_ROOT;
process.stdout.write(JSON.stringify({ dir: venvDir(ROOT) }));
"""


def _mjs_venv_dir(root: Path, env: dict) -> Path:
    """Run the shipped venvDir() (from _engine_resolve.mjs) under `env` and return the resolved Path."""
    # Import the real module by file URL so the test binds to the SHIPPED resolver, not a copy.
    script = _HARNESS.replace("__MODURL__", json.dumps(_ENGINE_RESOLVE_MJS.as_uri()))
    full_env = {**os.environ, **env, "CLAUDE_PLUGIN_ROOT": str(root)}
    r = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, env=full_env, check=True,
    )
    return Path(json.loads(r.stdout)["dir"])


def _py_venv_dir(root: Path, env: dict, monkeypatch) -> Path:
    """bootstrap.resolve_venv_dir under the same env (REPO_ROOT pinned to `root`)."""
    monkeypatch.setattr(bootstrap, "REPO_ROOT", root)
    for k in ("KG_ENGINE_VENV", "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return bootstrap.resolve_venv_dir()


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("kind", ["override", "plugin_data", "fallback"])
def test_mjs_venv_dir_matches_bootstrap(tmp_path, monkeypatch, kind):
    root = tmp_path / "plugin_root"
    root.mkdir()
    if kind == "override":
        env = {"KG_ENGINE_VENV": str(tmp_path / "explicit-venv")}
    elif kind == "plugin_data":
        env = {"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")}
    else:  # fallback to <root>/.venv
        env = {}

    mjs = _mjs_venv_dir(root, env)
    py = _py_venv_dir(root, dict(env), monkeypatch)
    assert mjs == py


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_mjs_venv_dir_expands_tilde_like_bootstrap(tmp_path, monkeypatch):
    # A '~' override must land both engines in the same expanded home-relative place.
    root = tmp_path / "plugin_root"
    root.mkdir()
    env = {"KG_ENGINE_VENV": "~/kg-engine-venv"}
    mjs = _mjs_venv_dir(root, env)
    py = _py_venv_dir(root, dict(env), monkeypatch)
    assert mjs == py
    assert str(mjs).startswith(str(Path.home()))


# --------------------------------------------------------------------------- #
# (2) precontext.py is a true no-op with no projected index (no writable Canon)
# --------------------------------------------------------------------------- #
def _run_precontext(project: Path, data: Path, payload: dict) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project), "CLAUDE_PLUGIN_DATA": str(data)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # don't add the real engine to sys.path; force the early returns
    return subprocess.run(
        ["python3", str(_PRECONTEXT_PY)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True, env=env,
    )


def test_precontext_no_index_is_silent_no_side_effects(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "data"  # no derived/index.sqlite under here
    r = _run_precontext(project, data, {"tool_input": {"pattern": "compression"}})
    assert r.returncode == 0
    assert r.stdout.strip() == b""  # nothing injected
    # The index guard must fire BEFORE any engine construction, so the canon dir is never
    # mkdir'd as a side effect of a plain Grep/Glob/Read.
    assert not (project / "canon").exists()
    assert not (data / "derived").exists()


# --------------------------------------------------------------------------- #
# (2b) the read path the hook now uses wires the SAME source corpus as the server
# --------------------------------------------------------------------------- #
def test_read_only_projector_wires_source_like_engine(engine, source_path):
    """KGEngine.read_only_projector (the seam the PreToolUse hook now goes through) must wire the SAME
    source corpus + pack specificity seeds as a full engine, so a hook-triggered projection is identical
    to the server's — not the degraded empty-corpus derived layer the old hand-built Projector produced
    and the server then served as fresh (finding: precontext-bypasses-facade)."""
    from kg_engine.canon import Canon
    from kg_engine.projector import Projector
    from kg_engine.server import KGEngine

    pack_path = Path(__file__).resolve().parents[1] / "pack" / "pack.yaml"
    ro = KGEngine.read_only_projector(engine.project_dir, engine.data_dir,
                                      source_path=source_path, pack_path=pack_path)
    # parity with the full engine's projector wiring (the bug was the hook wiring NEITHER of these):
    assert ro._corpus() and ro._corpus() == engine.projector._corpus()   # IDF corpus is wired
    assert ro._spec_seeds() == engine.projector._spec_seeds()            # pack specificity seeds wired
    # and the degraded construction the fix replaces really does read an EMPTY corpus:
    bare = Projector(Canon(engine.project_dir, ensure_layout=False), engine.data_dir / "derived")
    assert bare._corpus() == []


# precontext.py reads stdin with an explicit UTF-8 decode (line ~20) rather than
# json.load(sys.stdin): under a non-UTF-8 locale (Windows cp1252, UTF-8 mode off) the
# latter decodes the UTF-8 hook payload through the wrong text codec, mojibaking the
# pattern (wrong/empty kg_context match) or raising UnicodeDecodeError (swallowed -> the
# whole hook silently no-ops for any unicode payload). Drive precontext.py's reader under
# exactly that hostile codec and assert the non-ASCII pattern survives round-trip.
# `python3 -X utf8=0 -I` + PYTHONIOENCODING=cp1252 reproduces the Windows default; an
# in-process json.load on text stdin would fail this where the explicit utf-8 decode passes.
_READER = (
    "import json,sys\n"
    # the exact expression precontext.main() uses to read the payload:
    'p = json.loads(sys.stdin.buffer.read().decode("utf-8"))\n'
    'sys.stdout.buffer.write(p["tool_input"]["pattern"].encode("utf-8"))\n'
)
_READER_OLD = (
    "import json,sys\n"
    "p = json.load(sys.stdin)\n"  # the buggy locale-encoded read this fix replaced
    'sys.stdout.buffer.write(p["tool_input"]["pattern"].encode("utf-8"))\n'
)


def _decode_under_cp1252(reader_src: str, pattern: str) -> bytes:
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}
    # ensure_ascii=False puts LITERAL UTF-8 bytes in the payload (not \uXXXX escapes) — the
    # shape a real hook payload has, and the only shape that exposes the locale-decode bug
    # (a pure-ASCII \u-escaped payload is codec-independent and never mojibakes).
    r = subprocess.run(
        ["python3", "-c", reader_src],
        input=json.dumps({"tool_input": {"pattern": pattern}}, ensure_ascii=False).encode("utf-8"),
        capture_output=True, env=env,
    )
    return r.stdout


def test_precontext_decodes_utf8_payload_under_nonutf8_locale():
    pattern = "café—naïve"
    # The fix's explicit utf-8 decode round-trips the non-ASCII pattern verbatim …
    assert _decode_under_cp1252(_READER, pattern) == pattern.encode("utf-8")
    # … and is genuinely necessary: the old json.load(sys.stdin) mojibakes the same bytes
    # under the cp1252 stdin codec, proving this is a real (not vacuous) regression guard.
    assert _decode_under_cp1252(_READER_OLD, pattern) != pattern.encode("utf-8")


def test_precontext_utf8_payload_no_index_is_silent(tmp_path):
    # End-to-end: a non-ASCII pattern with no projected index must still no-op cleanly
    # (decode succeeds, index guard returns 0, no canon dir created).
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "data"
    r = _run_precontext(project, data, {"tool_input": {"pattern": "café—compression—naïve"}})
    assert r.returncode == 0
    assert r.stdout.strip() == b""
    assert not (project / "canon").exists()


# --------------------------------------------------------------------------- #
# (3) PYTHONPATH dedup is separator-canonical (Windows: backslash SCRIPTS vs the
#     forward-slash `${CLAUDE_PLUGIN_ROOT}/scripts` .mcp.json injects)
# --------------------------------------------------------------------------- #
# The dedup predicate uses `sep` (the native separator), so on Linux it is a no-op and the
# Windows confound never reproduces locally. Simulate Windows by evaluating the exact
# predicate with sep="\\", a backslash SCRIPTS, and a forward-slash existing PYTHONPATH
# entry: the canonical comparison must recognise them as the SAME path and NOT prepend a
# redundant copy. The pre-fix `parts.includes(SCRIPTS)` would prepend one every launch.
_DEDUP_HARNESS = r"""
const delimiter = ";";                 // Windows PATH separator
const sep = "\\";                      // Windows native path separator
const SCRIPTS = "C:\\plugin\\scripts"; // join(ROOT,"scripts") on Windows -> backslashes
const env = { PYTHONPATH: "C:/plugin/scripts" }; // .mcp.json injects forward slashes
const canon = (p) => p.split(sep).join("/");
const parts = env.PYTHONPATH ? env.PYTHONPATH.split(delimiter) : [];
if (!parts.map(canon).includes(canon(SCRIPTS))) env.PYTHONPATH = [SCRIPTS, ...parts].join(delimiter);
process.stdout.write(JSON.stringify({ pythonpath: env.PYTHONPATH, count: env.PYTHONPATH.split(delimiter).length }));
"""


def _dedup_block(mjs_path: Path) -> str:
    """Extract the canonical-separator dedup block (the `const canon` .. PYTHONPATH-prepend line)
    from the shipped resolver so the test asserts on the SHIPPED predicate, not a copy."""
    src = mjs_path.read_text(encoding="utf-8")
    start = src.index("const canon = (p) => p.split(sep).join")
    end = src.index("PYTHONPATH = [scripts, ...parts]", start)
    end = src.index("\n", end)
    return src[start:end]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mjs", [_ENGINE_RESOLVE_MJS])
def test_pythonpath_dedup_is_separator_canonical(mjs):
    # The shipped predicate must canonicalise separators (so it survives drift).
    block = _dedup_block(mjs)
    assert "split(sep).join" in block  # uses the native separator -> forward-slash canon
    r = subprocess.run(
        [NODE, "--input-type=module", "-e", _DEDUP_HARNESS],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(r.stdout)
    # No redundant prepend: the forward-slash entry already covers the backslash SCRIPTS.
    assert out["count"] == 1
    assert out["pythonpath"] == "C:/plugin/scripts"


# --------------------------------------------------------------------------- #
# (4) every shipped .mjs parses under `node --check`
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "mjs", [_LAUNCH_MJS, _PRECONTEXT_MJS, _PROVISION_MJS, _CANON_MERGE_MJS, _ENGINE_RESOLVE_MJS]
)
def test_mjs_parses(mjs):
    r = subprocess.run([NODE, "--check", str(mjs)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# (5) launch_server.mjs SUPERVISOR — relaunch/backoff/crash-loop policy (transport-resilience pass)
# --------------------------------------------------------------------------- #
# These import the SHIPPED launcher's exported helpers by file URL and drive the REAL supervision loop
# (createSupervisor) with a fake spawner — so a simulated engine-child crash exercises the actual
# relaunch / backoff / crash-loop-cap logic without spawning a real engine or killing the test process.
# Importing the module must NOT spawn (the main-module guard); these tests prove that too.
_LAUNCH_URL = json.dumps(_LAUNCH_MJS.as_uri())


def _run_node_harness(body: str) -> dict:
    """Run a node ESM harness that imports the launcher and prints a JSON line; return the parsed dict."""
    script = f'import * as L from {_LAUNCH_URL};\n' + body
    r = subprocess.run([NODE, "--input-type=module", "-e", script],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_supervisor_decision_table():
    out = _run_node_harness(r"""
const S = L.SUPERVISOR;
const out = {};
// capped exponential backoff
out.backoff = [0,1,2,3,4,5,6].map((n) => L.backoffFor(n));
// a clean exit (code 0, our signal, or shuttingDown) never relaunches
out.code0   = L.restartDecision({code:0, signal:null, ranForMs:1e9, shuttingDown:false, triedHeal:false, recentRestartCount:0}).action;
out.signal  = L.restartDecision({code:1, signal:"SIGTERM", ranForMs:10, shuttingDown:false, triedHeal:false, recentRestartCount:0}).action;
out.shut    = L.restartDecision({code:70, signal:null, ranForMs:1e9, shuttingDown:true, triedHeal:false, recentRestartCount:0}).action;
// a STARTUP-window non-zero exit heals once, then retries in place
out.heal    = L.restartDecision({code:1, signal:null, ranForMs:200, shuttingDown:false, triedHeal:false, recentRestartCount:0}).action;
out.retry   = L.restartDecision({code:1, signal:null, ranForMs:200, shuttingDown:false, triedHeal:true,  recentRestartCount:0});
// repeated STARTUP failures trip the crash-loop cap
out.cap     = L.restartDecision({code:70, signal:null, ranForMs:200, shuttingDown:false, triedHeal:true, recentRestartCount:S.MAX_RESTARTS});
// a POST-INIT crash (ran past the startup window) EXITS cleanly with the child's code (no relaunch onto
// the held-open, already-handshaked pipe) so the client reconnects with a fresh handshake
out.postInit = L.restartDecision({code:70, signal:null, ranForMs:40000, shuttingDown:false, triedHeal:true, recentRestartCount:1});
// the readiness-marker fix: a FAST crash (well within EARLY_FAILURE_MS) that nonetheless wrote its marker
// (servedInit=true) is POST-INIT, not a startup failure -> exit cleanly, do NOT relaunch in place
out.fastServed = L.restartDecision({code:70, signal:null, ranForMs:200, shuttingDown:false, triedHeal:false, recentRestartCount:0, servedInit:true});
// ...and a shuttingDown exit returns code 0 even when the child reported a non-zero code (Windows SIGTERM
// maps to a numeric signum with signal=null) — an ordinary shutdown must never look like a crash
out.shutCode = L.restartDecision({code:143, signal:null, ranForMs:1e9, shuttingDown:true, triedHeal:false, recentRestartCount:0});
process.stdout.write(JSON.stringify(out));
""")
    assert out["backoff"] == [200, 400, 800, 1600, 3200, 5000, 5000]
    assert out["code0"] == "exit" and out["signal"] == "exit" and out["shut"] == "exit"
    assert out["heal"] == "heal"
    assert out["retry"]["action"] == "relaunch" and out["retry"]["reason"] == "startup-retry"
    assert out["cap"]["action"] == "exit" and out["cap"]["reason"] == "crash-loop-cap"
    # the key correctness point of the post-init fix: exit cleanly, do NOT relaunch a post-init crash
    assert out["postInit"]["action"] == "exit"
    assert out["postInit"]["reason"] == "post-init-exit"
    assert out["postInit"]["code"] == 70
    # a fast crash that DID serve init (marker present) is treated as post-init regardless of wall-clock
    assert out["fastServed"]["action"] == "exit"
    assert out["fastServed"]["reason"] == "post-init-exit"
    assert out["fastServed"]["code"] == 70
    # a shuttingDown exit is always code 0 (the Windows non-zero-signum-on-clean-shutdown fix)
    assert out["shutCode"]["action"] == "exit"
    assert out["shutCode"]["code"] == 0
    assert out["shutCode"]["reason"] == "clean-exit"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_supervisor_loop_relaunches_then_trips_crash_loop_cap():
    """Drive the REAL createSupervisor loop: a rapidly STARTUP-crashing engine is healed once, retried with
    backoff up to the cap, then the supervisor exits cleanly (code 1) instead of thrashing forever."""
    out = _run_node_harness(r"""
import { EventEmitter } from "node:events";
let t = 1e6;
const children = [], pending = [];
let exitCode = null, healCalls = 0;
const sup = L.createSupervisor({
  spawnEngine: () => { const c = new EventEmitter(); children.push(c); return c; },
  exit: (code) => { exitCode = code; },
  heal: () => { healCalls++; },
  now: () => t,
  schedule: (fn, ms) => pending.push([fn, ms]),
  log: () => {},
});
sup.start();
let guard = 0;
while (exitCode === null && guard++ < 50) {
  const c = children[children.length - 1];
  t += 100;                                  // each child dies during STARTUP (< EARLY_FAILURE_MS)
  c.emit("exit", 70, null);
  while (pending.length) { const [fn, ms] = pending.shift(); t += ms; fn(); }
}
process.stdout.write(JSON.stringify({ spawns: children.length, heal: healCalls, exitCode,
                                      max: L.SUPERVISOR.MAX_RESTARTS }));
""")
    assert out["heal"] == 1, "exactly one early-failure heal"
    assert out["exitCode"] == 1, "crash-loop must exit cleanly with code 1"
    assert out["spawns"] <= out["max"] + 2, "spawns bounded by the crash-loop cap"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_supervisor_clean_shutdown_does_not_relaunch():
    """A forwarded SIGTERM (session ending) marks shutdown, so the child's exit is clean and the
    supervisor does NOT relaunch — the connection-keeping parent exits with it."""
    out = _run_node_harness(r"""
import { EventEmitter } from "node:events";
const children = [], pending = [];
let exitCode = null;
const sup = L.createSupervisor({
  spawnEngine: () => { const c = new EventEmitter(); c.kill = () => {}; children.push(c); return c; },
  exit: (code) => { exitCode = code; },
  now: () => 1e6, schedule: (fn) => pending.push(fn), log: () => {},
});
sup.start();
sup.markShutdown("SIGTERM");
children[0].emit("exit", 0, "SIGTERM");
process.stdout.write(JSON.stringify({ spawns: children.length, pending: pending.length, exitCode }));
""")
    assert out["spawns"] == 1, "must not relaunch after a clean shutdown"
    assert out["pending"] == 0
    assert out["exitCode"] == 0


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_supervisor_post_init_crash_exits_cleanly_no_relaunch():
    """A crash AFTER the engine came up and served `initialize` must EXIT cleanly (with the child's code),
    NOT relaunch onto the held-open, already-handshaked pipe — so the client gets the disconnect and
    reconnects with a fresh handshake instead of being stranded against an uninitialized engine."""
    out = _run_node_harness(r"""
import { EventEmitter } from "node:events";
let t = 1e6;
const children = [], pending = [];
let exitCode = null;
const sup = L.createSupervisor({
  spawnEngine: () => { const c = new EventEmitter(); children.push(c); return c; },
  exit: (code) => { exitCode = code; },
  now: () => t, schedule: (fn) => pending.push(fn), log: () => {},
});
sup.start();
t += 40000;                                   // ran healthily past the startup window, then crashes
children[0].emit("exit", 70, null);
process.stdout.write(JSON.stringify({ spawns: children.length, pending: pending.length, exitCode }));
""")
    assert out["spawns"] == 1, "a post-init crash must NOT relaunch"
    assert out["pending"] == 0
    assert out["exitCode"] == 70, "exits with the engine's crash code so the client reconnects"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_supervisor_fast_post_init_crash_with_marker_exits_no_relaunch():
    """The readiness-marker fix end-to-end through the REAL loop: an engine that came up and served
    `initialize` (servedInit -> true) but then crashes FAST (well within EARLY_FAILURE_MS, the window that
    used to be misread as a startup failure) must EXIT cleanly with the child's code — NOT heal/relaunch in
    place onto the held-open, already-handshaked pipe. A clearMarker stub keeps the loop hermetic."""
    out = _run_node_harness(r"""
import { EventEmitter } from "node:events";
let t = 1e6;
const children = [], pending = [];
let exitCode = null, healCalls = 0, cleared = 0;
const sup = L.createSupervisor({
  spawnEngine: () => { const c = new EventEmitter(); children.push(c); return c; },
  exit: (code) => { exitCode = code; },
  heal: () => { healCalls++; },
  now: () => t,
  schedule: (fn) => pending.push(fn),
  log: () => {},
  servedInit: () => true,            // the engine wrote its readiness marker (it began serving)
  clearMarker: () => { cleared++; }, // hermetic: no real filesystem marker
});
sup.start();
t += 200;                            // crashes FAST (< EARLY_FAILURE_MS) but AFTER serving init
children[0].emit("exit", 70, null);
process.stdout.write(JSON.stringify({ spawns: children.length, pending: pending.length, heal: healCalls,
                                      cleared, exitCode }));
""")
    assert out["spawns"] == 1, "a served-init crash must NOT relaunch even when it died fast"
    assert out["heal"] == 0, "a post-init crash must not trigger a venv heal"
    assert out["pending"] == 0
    assert out["cleared"] >= 1, "the marker is cleared before (re)spawn"
    assert out["exitCode"] == 70, "exits with the engine's crash code so the client reconnects"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_supervisor_shutdown_during_backoff_exits_promptly():
    """A SIGTERM arriving while a startup-retry relaunch is pending in the backoff window must cancel that
    timer and exit promptly — not idle until the timer fires."""
    out = _run_node_harness(r"""
import { EventEmitter } from "node:events";
let t = 1e6;
const children = [];
let exitCode = null, scheduled = 0, cancelled = 0, fired = 0;
let pendingFn = null;
const sup = L.createSupervisor({
  spawnEngine: () => { const c = new EventEmitter(); c.kill = () => {}; children.push(c); return c; },
  exit: (code) => { exitCode = code; },
  heal: () => {},
  now: () => t,
  schedule: (fn) => { scheduled++; pendingFn = fn; return 42; },   // a fake timer handle
  cancel: (h) => { if (h === 42) cancelled++; },
  log: () => {},
});
sup.start();
t += 100; children[0].emit("exit", 70, null);   // 1st STARTUP crash -> one-time heal (sync relaunch)
t += 100; children[1].emit("exit", 70, null);   // 2nd STARTUP crash -> schedules a backoff relaunch
sup.markShutdown("SIGTERM");                     // SIGTERM mid-backoff
if (pendingFn) pendingFn();                      // even if the stale timer somehow fires, it must no-op
process.stdout.write(JSON.stringify({ scheduled, cancelled, exitCode, spawns: children.length }));
""")
    assert out["scheduled"] == 1
    assert out["cancelled"] == 1, "the pending relaunch timer must be cancelled on shutdown"
    assert out["exitCode"] == 0, "shutdown during backoff exits promptly"
    # 2 spawns = initial + the one-time heal relaunch; the cancelled/guarded backoff relaunch must NOT add a 3rd
    assert out["spawns"] == 2, "no further spawn after shutdown (cancelled/guarded relaunch)"


# --------------------------------------------------------------------------- #
# (5) precontext.py._clean is an EXACT mirror of bootstrap._clean — including the
#     bare-sentinel `/.venv` / `/venv` results of substituting an empty ${...} into a
#     ${VAR}/.venv template (M_tooling-5).
# --------------------------------------------------------------------------- #
def _load_precontext():
    spec = importlib.util.spec_from_file_location("kg_precontext_clean", _PRECONTEXT_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("   ", ""),
        ("${CLAUDE_PLUGIN_DATA}", ""),
        ("/.venv", ""),           # bare sentinel: empty ${VAR}/.venv -> /.venv
        ("/venv", ""),            # bare sentinel: empty ${VAR}/venv -> /venv
        ("/real/path", "/real/path"),
        ("  /real/path  ", "/real/path"),
    ],
)
def test_precontext_clean_mirrors_bootstrap(raw, expected):
    precontext = _load_precontext()
    # precontext._clean and bootstrap._clean are documented mirrors; assert they AGREE on every case,
    # so the cleaning rule stays in lock-step (the bare sentinels were the drift this fix closes).
    assert precontext._clean(raw) == expected
    assert precontext._clean(raw) == bootstrap._clean(raw)
