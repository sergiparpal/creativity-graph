#!/usr/bin/env node
// MCP server launcher (cross-platform; §2.1 bootstrap hardening).
//
// `.mcp.json` runs `node launch_server.mjs`. Node is present wherever Claude Code runs,
// so this single command works on Windows, macOS, Linux, and WSL/Git-Bash alike — the
// old `bash launch_server.sh` could not start the server on native Windows.
//
// Launching the server directly as `<venv>/bin/python` races the SessionStart venv
// build: on a cold first session that path may not exist yet, the spawn fails, and
// Claude Code caches the server as "needs-auth" — dropping all kg_* tools for the whole
// session. Going through Node (always present) means the MCP spawn ALWAYS succeeds; this
// launcher then self-heals the venv (foreground catch-up via bootstrap.py — uv OR
// stdlib venv+pip, no hard uv requirement) before launching the real server. Warm
// sessions skip straight to launch.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(SCRIPT_DIR);
const SCRIPTS = join(ROOT, "scripts");

// Resolve the engine venv dir with the SAME precedence as bootstrap.resolve_venv_dir,
// then pass it to bootstrap.py explicitly (--venv) so the two never disagree.
function venvDir() {
  if (process.env.KG_ENGINE_VENV) return process.env.KG_ENGINE_VENV;
  if (process.env.CLAUDE_PLUGIN_DATA) return join(process.env.CLAUDE_PLUGIN_DATA, ".venv");
  return join(ROOT, ".venv");
}

// The interpreter pointer (engine-python.txt) is the single cross-platform source of
// truth written by bootstrap.py; fall back to the conventional path if it is absent.
function enginePython(dir) {
  try {
    const p = readFileSync(join(dir, "engine-python.txt"), "utf8").trim();
    if (p && existsSync(p)) return p;
  } catch {
    /* pointer not written yet */
  }
  const conventional =
    process.platform === "win32"
      ? join(dir, "Scripts", "python.exe")
      : join(dir, "bin", "python");
  return existsSync(conventional) ? conventional : null;
}

// A system Python >= 3.10 to drive the foreground catch-up build.
function systemPython() {
  const cands =
    process.platform === "win32"
      ? ["py", "python", "python3"]
      : ["python3", "python", "py"];
  const probe = "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)";
  for (const c of cands) {
    const r = spawnSync(c, ["-c", probe], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

const dir = venvDir();
let py = enginePython(dir);

if (!py) {
  // Cold session: build the venv in the foreground so the server can start. bootstrap.py
  // is idempotent and lock-serialized against the SessionStart background worker.
  const sys = systemPython();
  if (sys) {
    spawnSync(sys, [join(SCRIPTS, "bootstrap.py"), "--venv", dir], { stdio: "inherit" });
    py = enginePython(dir);
  }
}

if (!py) {
  process.stderr.write(
    "[creativity-graph] engine venv is not provisioned and no Python >= 3.10 was found " +
      "on PATH. Install Python 3.10+ and start a new session.\n"
  );
  process.exit(1);
}

// Ensure kg_engine resolves off PYTHONPATH (.mcp.json already sets this; belt-and-braces
// for a direct/dev launch). The KG_* env vars come through from .mcp.json untouched.
const env = { ...process.env };
const parts = env.PYTHONPATH ? env.PYTHONPATH.split(delimiter) : [];
if (!parts.includes(SCRIPTS)) env.PYTHONPATH = [SCRIPTS, ...parts].join(delimiter);

const child = spawn(py, ["-m", "kg_engine.server"], { stdio: "inherit", env });
const forward = (sig) => {
  try {
    child.kill(sig);
  } catch {
    /* child already gone */
  }
};
process.on("SIGINT", () => forward("SIGINT"));
process.on("SIGTERM", () => forward("SIGTERM"));
process.on("exit", () => forward("SIGTERM"));
child.on("error", (e) => {
  process.stderr.write(`[creativity-graph] failed to start engine server: ${e.message}\n`);
  process.exit(1);
});
child.on("exit", (code, signal) => process.exit(signal ? 1 : code ?? 0));
