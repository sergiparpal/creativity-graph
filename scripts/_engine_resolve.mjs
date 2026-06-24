// Shared engine-venv / interpreter resolution for the Node launchers
// (launch_server.mjs, canon_merge_driver.mjs, precontext.mjs).
//
// Single source of truth for what used to be copy-pasted across all three launchers (kept in
// sync only by "mirrors X" comments): the env-value cleaner, '~'/relative expansion, the
// venv-dir precedence (KG_ENGINE_VENV > CLAUDE_PLUGIN_DATA/.venv > <root>/.venv), the
// engine-python pointer read + conventional-path fallback, a system-Python>=3.10 probe, and the
// PYTHONPATH=scripts prepend. Resolution order / the sentinel list now change in ONE place.
//
// Node is present wherever Claude Code runs, so these launchers work on Windows, macOS, Linux,
// and WSL/Git-Bash alike (never bash) — see each launcher's own header for its specific role.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, isAbsolute, join, resolve, sep } from "node:path";

// Filenames bootstrap.py writes into the venv dir (kept identical to bootstrap's PTR_NAME / STAMP_NAME).
export const PTR_NAME = "engine-python.txt"; // cross-platform interpreter pointer
export const STAMP_NAME = "install.stamp"; // content hash of pyproject (readiness gate)

// Drop empty / whitespace / unsubstituted ${...} / bare-sentinel env values so an unset var never
// sends us to a bogus path. (Mirror of bootstrap._clean.)
export function clean(value) {
  if (!value) return "";
  const v = value.trim();
  if (!v || v.startsWith("${") || v === "/.venv" || v === "/venv") return "";
  return v;
}

// Expand a leading '~' to the home dir and resolve to an absolute path. An ABSOLUTE or '~'
// override lands where bootstrap.resolve_venv_dir's .expanduser().resolve() does; a RELATIVE
// override is resolved against `root` here vs bootstrap's process CWD (a documented edge —
// overrides are normally absolute, so this is not the common path).
export function expandResolve(p, root) {
  if (p === "~") p = homedir();
  else if (p.startsWith("~/") || p.startsWith("~\\")) p = join(homedir(), p.slice(2));
  return isAbsolute(p) ? resolve(p) : resolve(root, p);
}

// Resolve the engine venv dir with the SAME precedence as bootstrap.resolve_venv_dir, then the
// launcher passes it to bootstrap.py explicitly (--venv) so the two never disagree.
export function venvDir(root, env = process.env) {
  const override = clean(env.KG_ENGINE_VENV);
  if (override) return expandResolve(override, root);
  const data = clean(env.CLAUDE_PLUGIN_DATA);
  if (data) return expandResolve(join(data, ".venv"), root);
  return join(root, ".venv");
}

// A system Python >= 3.10 that can ALSO import every module in `requireImports`. launch_server
// needs only 'sys'; the canon merge driver additionally needs 'yaml' (PyYAML) — kg_engine.canonmerge
// imports it transitively (model.py `import yaml`), so a dep-less interpreter is rejected and the
// driver falls through to the clean "no engine python" conflict path instead of crashing (M8).
export function systemPython(requireImports = ["sys"]) {
  const cands =
    process.platform === "win32" ? ["py", "python", "python3"] : ["python3", "python", "py"];
  const probe =
    `import ${requireImports.join(", ")}; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)`;
  for (const c of cands) {
    const r = spawnSync(c, ["-c", probe], { stdio: "ignore" });
    if (r.status === 0) return c;
  }
  return null;
}

// Resolve the engine interpreter: the pointer (PTR_NAME) bootstrap writes is the cross-platform
// source of truth; fall back to the conventional venv path. `onMissing` (optional) supplies a final
// fallback when neither exists — the merge driver passes () => systemPython(["sys","yaml"]) so a
// plain `git merge` outside a Claude Code session still works; launch_server / precontext pass
// nothing and get null.
export function enginePython(dir, onMissing = null) {
  try {
    const p = readFileSync(join(dir, PTR_NAME), "utf8").trim();
    if (p && existsSync(p)) return p;
  } catch {
    /* pointer not written yet */
  }
  const conventional =
    process.platform === "win32"
      ? join(dir, "Scripts", "python.exe")
      : join(dir, "bin", "python");
  if (existsSync(conventional)) return conventional;
  return onMissing ? onMissing() : null;
}

// Return a COPY of `env` with `scripts` ensured on PYTHONPATH (kg_engine is never pip-installed).
// Compare on a forward-slash canonical form so a backslash `scripts` vs a forward-slash existing
// entry (as .mcp.json injects `${CLAUDE_PLUGIN_ROOT}/scripts`) doesn't double-prepend on Windows.
export function withPythonpath(env, scripts) {
  const out = { ...env };
  const canon = (p) => p.split(sep).join("/");
  const parts = out.PYTHONPATH ? out.PYTHONPATH.split(delimiter) : [];
  if (!parts.map(canon).includes(canon(scripts))) {
    out.PYTHONPATH = [scripts, ...parts].join(delimiter);
  }
  return out;
}
