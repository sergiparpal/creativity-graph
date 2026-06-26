#!/usr/bin/env node
// MCP server launcher + SUPERVISOR (cross-platform; §2.1 bootstrap hardening + transport-resilience pass).
//
// `.mcp.json` runs `node launch_server.mjs`. Two jobs:
//
// 1. COLD-START SELF-HEAL (the original role). Launching the server directly as `<venv>/bin/python`
//    races the SessionStart venv build: on a cold first session that path may not exist yet, the spawn
//    fails, and Claude Code caches the server as "needs-auth" — dropping all kg_* tools for the whole
//    session. Going through Node (always present) means the MCP spawn ALWAYS succeeds; this launcher
//    then self-heals the venv (foreground catch-up via bootstrap.py — uv OR stdlib venv+pip) before
//    launching the real server. Warm sessions skip straight to launch.
//
// 2. SUPERVISION (the resilience pass). The Node process is a PERSISTENT PARENT that spawns the Python
//    engine as a child and supervises it, with capped exponential backoff, a crash-loop guard, and full
//    logging of every (re)launch / exit / backoff decision to `<KG_DATA>/server.log` (the SAME rotating
//    file the engine writes — see server.py:configure_logging). The restart POLICY is the pure, exported
//    `restartDecision` / `backoffFor`, unit-testable without spawning a real engine.
//
//    What it relaunches — and why NOT everything. The engine runs with `stdio: "inherit"`, so Node never
//    owns the JSON-RPC stream and CANNOT replay MCP's per-connection `initialize` handshake. That splits
//    a crash into two cases (see restartDecision for the full reasoning):
//      • STARTUP failure (crash before the engine served `initialize`, e.g. an import error against a
//        half-built venv): the client's `initialize` is still buffered, unread, on the inherited stdin, so
//        a heal + in-place relaunch genuinely self-heals the cold-start race — and the parent (hence the
//        client pipe) stays alive. This is the original §2.1 role, now with backoff + a crash-loop cap.
//      • POST-INIT crash (the engine had already answered `initialize`): a relaunched engine would start
//        UNINITIALIZED on a pipe the client already handshaked, and — because Node holds the pipe open —
//        the client neither re-handshakes nor sees an EOF, i.e. a connection that looks alive but is dead.
//        That is strictly WORSE than a clean disconnect, so we EXIT instead: the pipe closes, the client
//        detects the drop and reconnects with a fresh handshake. (Fully transparent post-init restart
//        would require Node to PROXY the stream and replay the handshake + synthesize responses for
//        in-flight request ids — a larger change, deliberately deferred.)
//    Either way the failure is now LOGGED with a full traceback (server.log) instead of vanishing.
//
// Venv/interpreter resolution lives in ./_engine_resolve.mjs (shared with the other launchers).
import { spawn, spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync, realpathSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { venvDir, systemPython, enginePython, withPythonpath, clean, STAMP_NAME } from "./_engine_resolve.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(SCRIPT_DIR);
const SCRIPTS = join(ROOT, "scripts");
const BOOTSTRAP = join(SCRIPTS, "bootstrap.py");
const dir = venvDir(ROOT);

// A server that exits non-zero within this window of starting is treated as an early failure (an
// import error against a half-built / just-updated venv) and triggers the one self-heal retry.
const EARLY_FAILURE_MS = 5000;

// ----------------------------------------------------------------------------------------------------
// Supervision policy (pure + exported so tests exercise the decision table without spawning an engine).
// ----------------------------------------------------------------------------------------------------
export const SUPERVISOR = {
  EARLY_FAILURE_MS, // crash within this window = died during STARTUP, before serving `initialize` -> relaunch
  MAX_RESTARTS: 5, // startup retries within RESTART_WINDOW_MS before we declare a crash-loop and give up
  RESTART_WINDOW_MS: 60_000,
  BACKOFF_INITIAL_MS: 200,
  BACKOFF_MAX_MS: 5_000,
};

// Capped exponential backoff: 200, 400, 800, 1600, 3200, 5000(cap), 5000, …
export function backoffFor(consecutiveFailures, S = SUPERVISOR) {
  const n = Math.max(0, consecutiveFailures);
  return Math.min(S.BACKOFF_INITIAL_MS * 2 ** n, S.BACKOFF_MAX_MS);
}

// Pure restart policy. Given how/why the child exited and the recent restart history, return the action:
//   { action: "exit",      code, reason }   — stop the supervisor (clean shutdown / post-init / crash-loop)
//   { action: "heal",      reason }         — force a venv rebuild, then relaunch (early import failure)
//   { action: "relaunch",  reason }         — relaunch after backoff (a STARTUP retry only)
// `recentRestartCount` = startup-window unexpected exits within RESTART_WINDOW_MS (the caller prunes it).
//
// CRITICAL design point (why post-init crashes EXIT rather than relaunch). The engine runs with
// stdio:"inherit": Node never owns the JSON-RPC stream, so it cannot replay the per-connection MCP
// `initialize` handshake on a relaunch. That makes the two cases fundamentally different:
//   • A crash DURING STARTUP (before the engine served `initialize`) leaves the client's `initialize`
//     request still BUFFERED, unread, on the inherited stdin — a relaunched engine reads and answers it,
//     so an in-place heal/relaunch genuinely self-heals the cold-start race (§2.1, the original role).
//   • A crash AFTER the engine already answered `initialize` cannot be papered over: a relaunched engine
//     starts uninitialized, the client (seeing Node still alive) never re-handshakes AND gets no EOF, so
//     the connection looks alive but is dead — strictly WORSE than a clean disconnect. So we EXIT: Node
//     goes away, the pipe closes, the client detects the drop and reconnects with a fresh handshake.
// `EARLY_FAILURE_MS` is the (imperfect but safe) proxy for "died before serving init" — the engine's
// import+startup is sub-second, so a crash within the window is overwhelmingly a startup failure. (Fully
// transparent post-init restart would require Node to PROXY the stream and replay the handshake — a
// larger change, deliberately deferred; see the header.)
export function restartDecision(
  { code, signal, ranForMs, shuttingDown, triedHeal, recentRestartCount },
  S = SUPERVISOR
) {
  // A clean exit: we forwarded a term signal (session ending), the child was signalled, or it returned
  // a zero/empty code. Honor it — do NOT relaunch (no thrashing on a deliberate shutdown).
  if (shuttingDown || signal || !code) {
    return { action: "exit", code: signal ? 0 : code ?? 0, reason: "clean-exit" };
  }
  // Died during STARTUP (before serving the buffered `initialize`): self-heal in place.
  if (ranForMs < S.EARLY_FAILURE_MS) {
    // A near-instant non-zero exit looks like an import error against a half-built / just-updated venv
    // (the --check fast-path can pass on a stamp-fresh-but-broken venv): force ONE rebuild before retrying.
    if (!triedHeal) return { action: "heal", reason: "early-failure-heal" };
    // Repeated startup failures: stop thrashing — exit cleanly with a logged reason.
    if (recentRestartCount >= S.MAX_RESTARTS) return { action: "exit", code: 1, reason: "crash-loop-cap" };
    return { action: "relaunch", reason: "startup-retry" };
  }
  // Crashed AFTER serving `initialize` — relaunching onto the held-open pipe would strand an uninitialized
  // engine, so exit cleanly and let the client reconnect with a fresh handshake (see the note above).
  return { action: "exit", code, reason: "post-init-exit" };
}

// Where the supervisor appends its events — resolved EXACTLY as the engine's data dir (server.py
// resolve_data_dir): KG_DATA if set, else <project>/.kg-data. In an installed plugin KG_DATA is set
// (== CLAUDE_PLUGIN_DATA), so Node and Python agree on the file; in a bare dev launch they may differ.
export function serverLogDir(env = process.env) {
  const data = clean(env.KG_DATA);
  if (data) return data;
  const proj = clean(env.KG_PROJECT_DIR) || clean(env.CLAUDE_PROJECT_DIR) || process.cwd();
  return join(proj, ".kg-data");
}

function serverLog(msg) {
  // Best-effort: a logging failure must NEVER break the supervisor. Python owns rotation of server.log;
  // the supervisor's few lines per restart ride along.
  try {
    const p = join(serverLogDir(), "server.log");
    mkdirSync(dirname(p), { recursive: true });
    appendFileSync(p, `${new Date().toISOString()} INFO supervisor [node ${process.pid}]: ${msg}\n`);
  } catch {
    /* swallow */
  }
}

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

// The supervision loop, with every side-effecting dependency injectable so a test can drive the REAL
// loop (simulate an engine-child crash, assert relaunch-within-backoff, assert the crash-loop cap exits
// cleanly) without spawning a real engine or killing the test process. main() binds the production deps.
//   spawnEngine() -> a child-process-like EventEmitter emitting "error"(err) and "exit"(code, signal)
//   exit(code)    -> terminate the supervisor (default process.exit)
//   heal()        -> force a venv rebuild + re-resolve the interpreter (early-import-failure recovery)
//   now()         -> monotonic-ish ms clock; schedule(fn, ms) -> deferred relaunch (default setTimeout)
export function createSupervisor({
  spawnEngine,
  exit = (code) => process.exit(code),
  heal = () => {},
  now = () => Date.now(),
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (handle) => clearTimeout(handle),
  log = serverLog,
  onChildSpawned = () => {},
  policy = SUPERVISOR,
} = {}) {
  let child = null;
  let startedAt = 0;
  let shuttingDown = false; // set when WE forward a term signal -> a subsequent child death is "clean"
  let consecutiveFailures = 0; // escalates the startup-retry backoff
  let restartTimes = []; // timestamps of startup-window retries, pruned to RESTART_WINDOW_MS (crash-loop guard)
  let triedHeal = false; // whether we already spent the one early-failure venv rebuild
  let pendingRelaunch = null; // a scheduled backoff relaunch, so shutdown can cancel it and exit promptly

  function markShutdown(sig) {
    shuttingDown = true;
    try {
      child?.kill?.(sig);
    } catch {
      /* child already gone */
    }
    // A SIGINT/SIGTERM arriving mid-backoff (no live child to kill) must not leave Node idling until the
    // pending relaunch timer fires — cancel it and exit promptly so the session tears down cleanly.
    if (pendingRelaunch !== null) {
      cancel(pendingRelaunch);
      pendingRelaunch = null;
      exit(0);
    }
  }

  function spawnOnce() {
    startedAt = now();
    child = spawnEngine();
    child.on("error", (e) => {
      // A spawn-level error (ENOENT, EACCES) is not a server crash-loop — the binary can't run at all,
      // so relaunching would just thrash. Log it and give up cleanly.
      log(`spawn error: ${e.message}`);
      process.stderr.write(`[creativity-graph] failed to start engine server: ${e.message}\n`);
      exit(1);
    });
    child.on("exit", (code, signal) => onChildExit(code, signal));
    onChildSpawned(child);
    return child;
  }

  function onChildExit(code, signal) {
    child = null;
    const t = now();
    const ranForMs = t - startedAt;
    restartTimes = restartTimes.filter((ts) => t - ts < policy.RESTART_WINDOW_MS);
    const decision = restartDecision(
      { code, signal, ranForMs, shuttingDown, triedHeal, recentRestartCount: restartTimes.length },
      policy
    );
    log(`engine exited code=${code} signal=${signal} ranForMs=${ranForMs} -> ${decision.reason}`);

    if (decision.action === "exit") {
      if (decision.reason === "crash-loop-cap") {
        process.stderr.write(
          `[creativity-graph] engine crash-looped during startup (${restartTimes.length} retries in ` +
            `${policy.RESTART_WINDOW_MS / 1000}s); giving up. See ${join(serverLogDir(), "server.log")}.\n`
        );
      }
      exit(decision.code);
      return;
    }

    if (decision.action === "heal") {
      triedHeal = true;
      heal(); // FORCE a rebuild so a stamp-fresh-but-broken venv is reinstalled, then re-resolve python
      log("relaunching after early-failure heal");
      spawnOnce();
      return;
    }

    // startup retry with capped exponential backoff (only reached for sub-EARLY_FAILURE_MS exits)
    restartTimes.push(t);
    const backoff = backoffFor(consecutiveFailures, policy);
    consecutiveFailures += 1;
    log(`relaunching in ${backoff}ms (consecutiveFailures=${consecutiveFailures}, recent=${restartTimes.length})`);
    pendingRelaunch = schedule(() => {
      pendingRelaunch = null;
      if (!shuttingDown) spawnOnce();
    }, backoff);
  }

  function installSignalHandlers() {
    // A forwarded SIGINT/SIGTERM means the SESSION is ending, so mark shuttingDown so the child's
    // resulting death is treated as clean (no relaunch). Registered ONCE against the mutable `child`.
    process.on("SIGINT", () => markShutdown("SIGINT"));
    process.on("SIGTERM", () => markShutdown("SIGTERM"));
    process.on("exit", () => markShutdown("SIGTERM"));
  }

  function start() {
    spawnOnce();
  }

  return { start, installSignalHandlers, markShutdown };
}

function main() {
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

  const sup = createSupervisor({
    // stdio: "inherit" — the engine owns the real JSON-RPC stdio; Node's fds ARE the client pipe, kept
    // open across restarts, so the OS-level connection is never torn down when the engine relaunches.
    spawnEngine: () => spawn(py, ["-m", "kg_engine.server"], { stdio: "inherit", env }),
    heal: () => {
      if (sys) {
        foregroundCatchUp(sys, true);
        const fixed = enginePython(dir);
        if (fixed) py = fixed;
      }
    },
  });
  sup.installSignalHandlers();
  serverLog(`supervisor up (py=${py})`);
  sup.start();
}

// Only supervise when invoked as the entry script — importing this module (the launcher tests import the
// pure helpers) must NOT spawn an engine or probe the venv. Compare REALPATHS rather than file URLs: the
// plugin runs `node <abs>/launch_server.mjs` where <abs> may traverse a symlinked cache dir (e.g.
// ~/.claude/plugins/cache/...), so a raw URL string compare could be FALSE and the server would silently
// never start. realpathSync resolves both sides to the same inode; a `node -e` import has no script argv[1]
// (so it short-circuits false) — exactly the no-spawn behavior the tests rely on.
function _invokedAsMain() {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}
if (_invokedAsMain()) {
  main();
}
