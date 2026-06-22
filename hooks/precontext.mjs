#!/usr/bin/env node
// PreToolUse hook launcher (cross-platform).
//
// hooks.json runs `node precontext.mjs` on Grep/Glob/Read. Node is present wherever
// Claude Code runs, so this replaces the old `sh -c '<venv>/bin/python …'` command,
// which could neither find `sh` nor `bin/python` on native Windows.
//
// It finds the engine interpreter via the pointer bootstrap.py writes and hands the hook
// payload to precontext.py (stdin in, additionalContext JSON out — both passed through
// with stdio: "inherit"). It is a pure best-effort performance hook: if the venv is not
// provisioned yet, or anything goes wrong, it stays silent and never blocks the tool.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

try {
  const HOOKS_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/hooks
  const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(HOOKS_DIR);
  const SCRIPTS = join(ROOT, "scripts");

  const dir = process.env.KG_ENGINE_VENV
    ? process.env.KG_ENGINE_VENV
    : process.env.CLAUDE_PLUGIN_DATA
      ? join(process.env.CLAUDE_PLUGIN_DATA, ".venv")
      : join(ROOT, ".venv");

  // Resolve the engine python via the pointer; conventional path as a fallback. No
  // foreground catch-up here — precontext is best-effort; if the venv is not ready we
  // simply inject no context this turn.
  let py = null;
  try {
    const p = readFileSync(join(dir, "engine-python.txt"), "utf8").trim();
    if (p && existsSync(p)) py = p;
  } catch {
    /* pointer not written yet */
  }
  if (!py) {
    const conventional =
      process.platform === "win32"
        ? join(dir, "Scripts", "python.exe")
        : join(dir, "bin", "python");
    if (existsSync(conventional)) py = conventional;
  }
  if (!py) process.exit(0);

  const env = { ...process.env };
  const parts = env.PYTHONPATH ? env.PYTHONPATH.split(delimiter) : [];
  if (!parts.includes(SCRIPTS)) env.PYTHONPATH = [SCRIPTS, ...parts].join(delimiter);

  // stdin (the tool payload) -> precontext.py; its stdout (the additionalContext JSON)
  // -> this hook's stdout. stderr is dropped so a stray traceback never reaches the log.
  spawnSync(py, [join(HOOKS_DIR, "precontext.py")], {
    stdio: ["inherit", "inherit", "ignore"],
    env,
  });
} catch {
  /* never break a Grep/Glob/Read on a hook error */
}
process.exit(0);
