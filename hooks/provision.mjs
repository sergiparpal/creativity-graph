#!/usr/bin/env node
// SessionStart hook dispatcher (cross-platform).
//
// hooks.json registers ONE command — `node provision.mjs` — in place of the two
// launchers (`sh provision.sh` + `powershell provision.ps1`) that would otherwise BOTH
// run on every SessionStart. On each OS the wrong launcher's interpreter is absent
// (`powershell` on Linux/macOS, `sh` on native Windows), so one always failed with a
// "command not found" error — harmless under `async: true`, but noisy in hook logs.
// Node is present wherever Claude Code runs, so we detect the platform here and invoke
// only the launcher that exists.
//
// The launcher finds a Python >= 3.10 and hands off to scripts/bootstrap.py, which does
// the real, idempotent provisioning in a DETACHED background process and returns in
// milliseconds — so the synchronous wait below is cheap and the detached worker
// outlives this script.
//
// Failure is silent by design (this is a background hook): if anything goes wrong — no
// Node on PATH, no launcher, no suitable Python — the MCP launcher provisions in the
// foreground the first time the server is spawned.
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

try {
  const hooksDir = dirname(fileURLToPath(import.meta.url));
  const [cmd, args] =
    process.platform === "win32"
      ? [
          "powershell",
          ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           join(hooksDir, "provision.ps1")],
        ]
      : ["sh", [join(hooksDir, "provision.sh")]];
  // stdio ignored so the launcher never writes to the hook log; the launcher and
  // bootstrap.py handle their own (background) logging to provision.log.
  spawnSync(cmd, args, { stdio: "ignore" });
} catch {
  // never surface an error from a background hook
}
process.exit(0);
