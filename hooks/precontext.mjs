#!/usr/bin/env node
// PreToolUse hook launcher (cross-platform).
//
// hooks.json runs `node precontext.mjs` on Grep/Glob/Read. It finds the engine interpreter (via
// ../scripts/_engine_resolve.mjs, shared with the other launchers) and hands the hook payload to
// precontext.py (stdin in, additionalContext JSON out — both passed through with stdio:"inherit"). It
// is a pure best-effort performance hook: if the venv is not provisioned yet, or anything goes wrong,
// it stays silent and never blocks the tool.
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { venvDir, enginePython, withPythonpath } from "../scripts/_engine_resolve.mjs";

// This runs on EVERY Grep/Glob/Read, so cap the engine call: a slow/hung engine must never stall the
// tool. On timeout spawnSync kills the child and returns; we stay silent (best-effort).
const ENGINE_TIMEOUT_MS = 5000;

try {
  const HOOKS_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/hooks
  const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(HOOKS_DIR);
  const SCRIPTS = join(ROOT, "scripts");

  // No foreground catch-up here — precontext is best-effort; if the venv is not ready we simply inject
  // no context this turn.
  const py = enginePython(venvDir(ROOT));
  if (!py) process.exit(0);

  const env = withPythonpath(process.env, SCRIPTS);

  // stdin (the tool payload) -> precontext.py; its stdout (the additionalContext JSON) -> this hook's
  // stdout. stderr is dropped so a stray traceback never reaches the log.
  spawnSync(py, [join(HOOKS_DIR, "precontext.py")], {
    stdio: ["inherit", "inherit", "ignore"],
    env,
    timeout: ENGINE_TIMEOUT_MS,
    killSignal: "SIGKILL",
  });
} catch {
  /* never break a Grep/Glob/Read on a hook error */
}
process.exit(0);
