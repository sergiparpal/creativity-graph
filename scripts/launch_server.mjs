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
import { existsSync, readFileSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(SCRIPT_DIR);
const SCRIPTS = join(ROOT, "scripts");

// Mirror bootstrap._clean: drop empty / whitespace / unsubstituted ${...} / bare-sentinel
// values so an unset env var never sends us to a bogus path.
function clean(value) {
  if (!value) return "";
  const v = value.trim();
  if (!v || v.startsWith("${") || v === "/.venv" || v === "/venv") return "";
  return v;
}

// Expand a leading '~' to the home dir and resolve to an absolute path — mirroring
// bootstrap.resolve_venv_dir's .expanduser().resolve(). A '~' or relative
// KG_ENGINE_VENV / CLAUDE_PLUGIN_DATA must land us in the SAME place bootstrap.py used.
function expandResolve(p) {
  if (p === "~") p = homedir();
  else if (p.startsWith("~/") || p.startsWith("~\\")) p = join(homedir(), p.slice(2));
  return isAbsolute(p) ? resolve(p) : resolve(ROOT, p);
}

// Resolve the engine venv dir with the SAME precedence as bootstrap.resolve_venv_dir,
// then pass it to bootstrap.py explicitly (--venv) so the two never disagree.
function venvDir() {
  const override = clean(process.env.KG_ENGINE_VENV);
  if (override) return expandResolve(override);
  const data = clean(process.env.CLAUDE_PLUGIN_DATA);
  if (data) return expandResolve(join(data, ".venv"));
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
const BOOTSTRAP = join(SCRIPTS, "bootstrap.py");

// Run a foreground catch-up build. CRITICAL: bootstrap.py prints [bootstrap]… and the
// inherited uv/pip output to ITS stdout; the MCP server (below) owns this process's stdout
// for JSON-RPC, so we must route the child's stdout OFF that channel. stdio = [ignore, 2,
// inherit] sends child stdout -> our fd 2 (stderr) and child stderr -> our stderr. Only the
// real server ever writes the process stdout, so its first frame is never preceded by noise.
function foregroundCatchUp(sys, force) {
  // `force` removes the install.stamp first so bootstrap's is_ready() fast-path cannot short-circuit:
  // needed for the self-heal retry below, where the stamp MATCHES but the deps are actually broken
  // (a stamp-fresh venv that still fails to import) — without this, provision would do nothing and
  // the retry would relaunch the identical broken server.
  if (force) {
    try {
      rmSync(join(dir, "install.stamp"), { force: true });
    } catch {
      /* best-effort */
    }
  }
  spawnSync(sys, [BOOTSTRAP, "--venv", dir], { stdio: ["ignore", 2, "inherit"] });
}

// The venv is fresh only when bootstrap.py --check exits 0 (interpreter present AND the
// install.stamp matches the current pyproject — node-launchers-2). Checking interpreter
// existence alone would launch a STALE venv after a deps-changing update left the old
// interpreter in place. --check prints nothing to stdout, so it is JSON-RPC-safe.
function stampFresh(sys) {
  if (!sys) return false;
  const r = spawnSync(sys, [BOOTSTRAP, "--check", "--venv", dir], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  return r.status === 0;
}

const sys = systemPython();
let py = enginePython(dir);

// Cold (no interpreter) OR stale (interpreter present but stamp out of date): run the
// foreground catch-up so the server starts against a current venv. bootstrap.py is
// idempotent and lock-serialized against the SessionStart background worker.
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

// Ensure kg_engine resolves off PYTHONPATH (.mcp.json already sets this; belt-and-braces
// for a direct/dev launch). The KG_* env vars come through from .mcp.json untouched.
const env = { ...process.env };
const parts = env.PYTHONPATH ? env.PYTHONPATH.split(delimiter) : [];
if (!parts.includes(SCRIPTS)) env.PYTHONPATH = [SCRIPTS, ...parts].join(delimiter);

// Spawn the real server. `retried` is the belt-and-braces fallback (node-launchers-2): if
// the server exits non-zero almost immediately (an import error against a half-built /
// just-updated venv that --check did not catch), run the foreground bootstrap ONCE and
// relaunch before giving up. A clean or slow exit is honoured as-is.
function launch(retried) {
  const startedAt = Date.now();
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
  child.on("exit", (code, signal) => {
    const earlyFailure = !signal && code && Date.now() - startedAt < 5000;
    if (earlyFailure && !retried && sys) {
      // One self-heal pass, FORCING a rebuild (drop the stamp) so a stamp-fresh-but-broken venv is
      // actually reinstalled rather than skipped by is_ready(), then relaunch.
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
