#!/usr/bin/env node
// Canon merge-driver launcher (cross-platform; §1.2/§1.4 — the safe half of R5).
//
// Configured once as a git merge driver (see README / CLAUDE.md):
//   git config merge.kgcanon.driver "node <root>/scripts/canon_merge_driver.mjs %O %A %B"
// with `.gitattributes` routing `canon/*.md merge=kgcanon` here. git invokes us with the three
// temp-file paths (%O base, %A ours, %B theirs); we run `kg_engine.canonmerge` against them through
// the resolved engine python and inherit its exit code (0 = clean, 1 = conflicted). Node is present
// wherever Claude Code runs, so — like launch_server.mjs / precontext.mjs — this works on Windows,
// macOS, Linux, and WSL/Git-Bash alike, never bash.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(SCRIPT_DIR);
const SCRIPTS = join(ROOT, "scripts");

// Mirror bootstrap._clean / launch_server.clean: drop empty / whitespace / unsubstituted ${...} /
// bare-sentinel values so an unset env var never sends us to a bogus venv path.
function clean(value) {
  if (!value) return "";
  const v = value.trim();
  if (!v || v.startsWith("${") || v === "/.venv" || v === "/venv") return "";
  return v;
}

// Expand a leading '~' and resolve to absolute — mirroring bootstrap.resolve_venv_dir.
function expandResolve(p) {
  if (p === "~") p = homedir();
  else if (p.startsWith("~/") || p.startsWith("~\\")) p = join(homedir(), p.slice(2));
  return isAbsolute(p) ? resolve(p) : resolve(ROOT, p);
}

// Resolve the engine venv dir with the SAME precedence as bootstrap.resolve_venv_dir / launch_server.
function venvDir() {
  const override = clean(process.env.KG_ENGINE_VENV);
  if (override) return expandResolve(override);
  const data = clean(process.env.CLAUDE_PLUGIN_DATA);
  if (data) return expandResolve(join(data, ".venv"));
  return join(ROOT, ".venv");
}

// The interpreter pointer (engine-python.txt) is the cross-platform source of truth; fall back to the
// conventional venv path, then to any system Python >= 3.10 on PATH that can ALSO import the engine's
// PyYAML runtime dependency. A merge can run outside a Claude Code session (a plain `git merge` in a
// checkout), so the system-python fallback keeps the driver usable when no engine venv was ever
// provisioned (kg_engine resolves off PYTHONPATH=scripts) — but only if that interpreter actually has
// PyYAML, which kg_engine.canonmerge imports transitively (model.py `import yaml`); otherwise it would
// crash with an opaque ModuleNotFoundError, so we reject it and fall through to the clean "no engine
// python" conflict path instead (review-M8).
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
  if (existsSync(conventional)) return conventional;
  return systemPython();
}

function systemPython() {
  const cands =
    process.platform === "win32" ? ["py", "python", "python3"] : ["python3", "python", "py"];
  // require BOTH Python >= 3.10 AND an importable PyYAML (the engine's one third-party runtime dep the
  // merge module pulls in) — an ImportError exits non-zero, so a dep-less interpreter is rejected (M8).
  const probe = "import sys, yaml; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)";
  for (const c of cands) {
    const r = spawnSync(c, ["-c", probe], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

const py = enginePython(venvDir());
if (!py) {
  process.stderr.write(
    "[creativity-graph] canon merge driver: no engine python and no Python >= 3.10 on PATH; " +
      "leaving the merge to git's default driver.\n"
  );
  // Exit non-zero so git reports the canon file as conflicted rather than silently taking one side.
  process.exit(1);
}

// Ensure kg_engine resolves off PYTHONPATH=scripts (it is never pip-installed). Compare on a
// forward-slash canonical form so a backslash SCRIPTS vs a forward-slash existing entry doesn't
// double-prepend on Windows (mirrors launch_server.mjs / precontext.mjs).
const env = { ...process.env };
const canon = (p) => p.split(sep).join("/");
const parts = env.PYTHONPATH ? env.PYTHONPATH.split(delimiter) : [];
if (!parts.map(canon).includes(canon(SCRIPTS))) env.PYTHONPATH = [SCRIPTS, ...parts].join(delimiter);

// Pass git's [%O, %A, %B] straight through; inherit stdio so the driver's stderr notes reach the user.
const r = spawnSync(py, ["-m", "kg_engine.canonmerge", ...process.argv.slice(2)], {
  stdio: "inherit",
  env,
});
process.exit(r.status === null ? 1 : r.status);
