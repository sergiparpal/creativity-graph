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
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

try {
  const HOOKS_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/hooks
  const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(HOOKS_DIR);
  const SCRIPTS = join(ROOT, "scripts");

  // Mirror bootstrap.resolve_venv_dir (and launch_server.mjs): drop empty / unsubstituted
  // ${...} / bare-sentinel values, expand a leading '~', and resolve to absolute — so a
  // '~' or relative KG_ENGINE_VENV / CLAUDE_PLUGIN_DATA finds the SAME venv bootstrap built
  // instead of silently looking in the wrong place.
  const clean = (value) => {
    if (!value) return "";
    const v = value.trim();
    if (!v || v.startsWith("${") || v === "/.venv" || v === "/venv") return "";
    return v;
  };
  const expandResolve = (p) => {
    if (p === "~") p = homedir();
    else if (p.startsWith("~/") || p.startsWith("~\\")) p = join(homedir(), p.slice(2));
    return isAbsolute(p) ? resolve(p) : resolve(ROOT, p);
  };
  const override = clean(process.env.KG_ENGINE_VENV);
  const data = clean(process.env.CLAUDE_PLUGIN_DATA);
  const dir = override
    ? expandResolve(override)
    : data
      ? expandResolve(join(data, ".venv"))
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

  // Compare on a canonical forward-slash form: SCRIPTS uses the native separator (backslash
  // on Windows) but .mcp.json injects `${CLAUDE_PLUGIN_ROOT}/scripts` with forward slashes,
  // so a raw includes() would never match on Windows and prepend a redundant entry each call.
  const env = { ...process.env };
  const canon = (p) => p.split(sep).join("/");
  const parts = env.PYTHONPATH ? env.PYTHONPATH.split(delimiter) : [];
  if (!parts.map(canon).includes(canon(SCRIPTS))) env.PYTHONPATH = [SCRIPTS, ...parts].join(delimiter);

  // stdin (the tool payload) -> precontext.py; its stdout (the additionalContext JSON)
  // -> this hook's stdout. stderr is dropped so a stray traceback never reaches the log.
  // This runs on EVERY Grep/Glob/Read, so cap it: a slow/hung engine must never stall the
  // tool. On timeout spawnSync kills the child and returns; we stay silent (best-effort).
  spawnSync(py, [join(HOOKS_DIR, "precontext.py")], {
    stdio: ["inherit", "inherit", "ignore"],
    env,
    timeout: 5000,
    killSignal: "SIGKILL",
  });
} catch {
  /* never break a Grep/Glob/Read on a hook error */
}
process.exit(0);
