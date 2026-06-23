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
# Evaluate the REAL venvDir() out of launch_server.mjs (not a re-implementation) so a drift
# in the .mjs precedence is caught. The launcher self-executes launch() at module load, so
# we splice out only its self-contained resolution helpers (clean/expandResolve/venvDir),
# wire ROOT from CLAUDE_PLUGIN_ROOT (which the launcher already honours), and print the
# chosen dir as JSON.
_HARNESS = r"""
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
const ROOT = process.env.CLAUDE_PLUGIN_ROOT;
__BODY__
process.stdout.write(JSON.stringify({ dir: venvDir() }));
"""


def _mjs_venv_dir(root: Path, env: dict) -> Path:
    """Run launch_server.mjs's actual venvDir() under `env` and return the resolved Path."""
    src = _LAUNCH_MJS.read_text(encoding="utf-8")
    # Splice the contiguous helper block: `function clean(` .. end of `function venvDir(){…}`.
    start = src.index("function clean(")
    end = src.index("function venvDir(")
    end = src.index("\n}", end) + len("\n}")
    body = src[start:end]
    script = _HARNESS.replace("__BODY__", body)
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
    """Extract the canonical-separator dedup block (the `const canon` .. includes() line)
    from a launcher so the test asserts on the SHIPPED predicate, not a copy."""
    src = mjs_path.read_text(encoding="utf-8")
    start = src.index("const canon = (p) => p.split(sep).join")
    end = src.index("env.PYTHONPATH = [SCRIPTS, ...parts]", start)
    end = src.index("\n", end)
    return src[start:end]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mjs", [_LAUNCH_MJS, _PRECONTEXT_MJS])
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
@pytest.mark.parametrize("mjs", [_LAUNCH_MJS, _PRECONTEXT_MJS, _PROVISION_MJS])
def test_mjs_parses(mjs):
    r = subprocess.run([NODE, "--check", str(mjs)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
