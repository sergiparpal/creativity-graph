#!/usr/bin/env node
// Canon merge-driver launcher (cross-platform; §1.2/§1.4 — the safe half of R5).
//
// Configured once as a git merge driver (see README / CLAUDE.md):
//   git config merge.kgcanon.driver "node <root>/scripts/canon_merge_driver.mjs %O %A %B"
// with `.gitattributes` routing `canon/*.md merge=kgcanon` here. git invokes us with the three
// temp-file paths (%O base, %A ours, %B theirs); we run `kg_engine.canonmerge` against them through the
// resolved engine python and inherit its exit code (0 = clean, 1 = conflicted). Venv/interpreter
// resolution lives in ./_engine_resolve.mjs (shared with the other launchers).
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { venvDir, enginePython, systemPython, withPythonpath } from "./_engine_resolve.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const ROOT = process.env.CLAUDE_PLUGIN_ROOT || dirname(SCRIPT_DIR);
const SCRIPTS = join(ROOT, "scripts");

// Resolve the engine python via the pointer/conventional venv path; if no venv was ever provisioned (a
// plain `git merge` outside a Claude Code session), fall back to a system Python >= 3.10 that ALSO has
// PyYAML — kg_engine.canonmerge imports it transitively (model.py `import yaml`), so a dep-less
// interpreter is rejected and we fall through to the clean "no engine python" conflict path (M8).
const py = enginePython(venvDir(ROOT), () => systemPython(["sys", "yaml"]));
if (!py) {
  process.stderr.write(
    "[sproutgraph] canon merge driver: no engine python and no Python >= 3.10 on PATH; " +
      "leaving the merge to git's default driver.\n"
  );
  // Exit non-zero so git reports the canon file as conflicted rather than silently taking one side.
  process.exit(1);
}

// kg_engine resolves off PYTHONPATH=scripts (it is never pip-installed).
const env = withPythonpath(process.env, SCRIPTS);

// Pass git's [%O, %A, %B] straight through; inherit stdio so the driver's stderr notes reach the user.
// A timeout caps a wedged canonmerge so it can never hang `git merge` indefinitely (review-low); a
// timed-out OR failed-to-spawn (ENOENT) run sets r.error -> emit a one-line note (mirroring the no-python
// branch) and exit non-zero so git reports the file conflicted rather than silently taking one side.
const r = spawnSync(py, ["-m", "kg_engine.canonmerge", ...process.argv.slice(2)], {
  stdio: "inherit",
  env,
  timeout: 60000,
});
if (r.error) {
  process.stderr.write(
    "[sproutgraph] canon merge driver: failed to run the engine " +
      `(${r.error.code || r.error.message}); leaving the merge to git's default driver.\n`
  );
  process.exit(1);
}
process.exit(r.status === null ? 1 : r.status);
