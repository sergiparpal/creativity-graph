#!/usr/bin/env node
// MCP server launcher (cross-platform; §2.1 bootstrap hardening).
//
// `.mcp.json` runs `node launch_server.mjs`. Launching the server directly as `<venv>/bin/python`
// races the SessionStart venv build: on a cold first session that path may not exist yet, the spawn
// fails, and Claude Code caches the server as "needs-auth" — dropping all kg_* tools for the whole
// session. Going through Node (always present) means the MCP spawn ALWAYS succeeds; this launcher
// then self-heals the venv (foreground catch-up via bootstrap.py — uv OR stdlib venv+pip) before
// launching the real server. Warm sessions skip straight to launch. Venv/interpreter resolution
// lives in ./_engine_resolve.mjs (shared with the other launchers).
import { spawn, spawnSync } from "node:child_process";
import { rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { venvDir, systemPython, enginePython, withPythonpath, STAMP_NAME } from "./_engine_resolve.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(SCRIPT_DIR);
const SCRIPTS = join(ROOT, "scripts");
const BOOTSTRAP = join(SCRIPTS, "bootstrap.py");
const dir = venvDir(ROOT);

// A server that exits non-zero within this window of starting is treated as an early failure (an
// import error against a half-built / just-updated venv) and triggers the one self-heal retry.
const EARLY_FAILURE_MS = 5000;

// Run a foreground catch-up build. CRITICAL: bootstrap.py prints [bootstrap]… and the inherited
// uv/pip output to ITS stdout; the MCP server (below) owns this process's stdout for JSON-RPC, so we
// route the child's stdout OFF that channel. stdio = [ignore, 2, inherit] sends child stdout -> our
// fd 2 (stderr) and child stderr -> our stderr, so the server's first frame is never preceded by noise.
function foregroundCatchUp(sys, force) {
  // `force` removes the install.stamp first so bootstrap's is_ready() fast-path cannot short-circuit:
  // needed for the self-heal retry below, where the stamp MATCHES but the deps are actually broken
  // (a stamp-fresh venv that still fails to import) — without this, provision would do nothing and the
  // retry would relaunch the identical broken server.
  if (force) {
    try {
      rmSync(join(dir, STAMP_NAME), { force: true });
    } catch {
      /* best-effort */
    }
  }
  const r = spawnSync(sys, [BOOTSTRAP, "--venv", dir], { stdio: ["ignore", 2, "inherit"] });
  // Surface a spawn failure (ENOENT) or a non-zero bootstrap exit (2 = still provisioning past the wait
  // deadline; 1 = build failed) instead of silently launching against a possibly-unready venv. The
  // server's early-failure self-heal is still the net.
  if (r.error || (typeof r.status === "number" && r.status !== 0)) {
    process.stderr.write(
      "[creativity-graph] engine provisioning did not complete" +
        (r.error ? ` (${r.error.code || r.error.message})` : ` (bootstrap exit ${r.status})`) +
        "; the background worker may still be building — retry shortly if tools are missing.\n"
    );
  }
  return r;
}

// The venv is fresh only when bootstrap.py --check exits 0 (interpreter present AND install.stamp
// matches the current pyproject). Checking interpreter existence alone would launch a STALE venv after
// a deps-changing update left the old interpreter in place. --check prints nothing, so it is JSON-RPC-safe.
function stampFresh(sys) {
  if (!sys) return false;
  const r = spawnSync(sys, [BOOTSTRAP, "--check", "--venv", dir], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  return r.status === 0;
}

const sys = systemPython();
let py = enginePython(dir);

// Cold (no interpreter) OR stale (interpreter present but stamp out of date): run the foreground
// catch-up so the server starts against a current venv. bootstrap.py is idempotent and lock-serialized
// against the SessionStart background worker.
if (!py || !stampFresh(sys)) {
  if (sys) {
    foregroundCatchUp(sys);
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

// kg_engine resolves off PYTHONPATH=scripts (.mcp.json already sets this; belt-and-braces for a
// direct/dev launch). The KG_* env vars come through from .mcp.json untouched.
const env = withPythonpath(process.env, SCRIPTS);

// Signal forwarding is registered ONCE at module scope against a mutable `child` reference, so the
// self-heal retry below does not accumulate a duplicate SIGINT/SIGTERM/exit listener per attempt.
let child = null;
const forward = (sig) => {
  try {
    child?.kill(sig);
  } catch {
    /* child already gone */
  }
};
process.on("SIGINT", () => forward("SIGINT"));
process.on("SIGTERM", () => forward("SIGTERM"));
process.on("exit", () => forward("SIGTERM"));

// Spawn the real server. `retried` is the belt-and-braces fallback: if the server exits non-zero almost
// immediately (an import error against a half-built / just-updated venv that --check did not catch), run
// the foreground bootstrap ONCE (FORCING a rebuild so a stamp-fresh-but-broken venv is actually
// reinstalled) and relaunch before giving up. A clean or slow exit is honoured as-is.
function launch(retried) {
  const startedAt = Date.now();
  child = spawn(py, ["-m", "kg_engine.server"], { stdio: "inherit", env });
  child.on("error", (e) => {
    process.stderr.write(`[creativity-graph] failed to start engine server: ${e.message}\n`);
    process.exit(1);
  });
  child.on("exit", (code, signal) => {
    const earlyFailure = !signal && code && Date.now() - startedAt < EARLY_FAILURE_MS;
    if (earlyFailure && !retried && sys) {
      foregroundCatchUp(sys, true);
      const fixed = enginePython(dir);
      if (fixed) {
        py = fixed;
        launch(true);
        return;
      }
    }
    process.exit(signal ? 1 : code ?? 0);
  });
}

launch(false);
