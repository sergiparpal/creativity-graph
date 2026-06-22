#!/bin/sh
# SessionStart launcher (POSIX: sh / Git Bash / WSL / macOS / Linux).
#
# Thin launcher: find a Python >= 3.10 and hand off to scripts/bootstrap.py, which does
# the real, idempotent, concurrency-safe provisioning in a detached background process.
# All the heavy logic lives in bootstrap.py.
#
# Invoked by hooks/provision.mjs (the cross-platform SessionStart dispatcher, run with
# "async": true) on non-Windows platforms; provision.ps1 covers native Windows. Also
# runnable directly by a developer.
set -u

ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$ROOT" ]; then
  # Dev fallback (`--plugin-dir .` without the var, or run by hand): hooks/ -> repo root.
  ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." 2>/dev/null && pwd)" || exit 0
fi
# Prepend uv's usual install dir; ${HOME:-} guards minimal/CI/MSYS shells where HOME is
# unset (set -u would otherwise abort here). Harmless if the dir is absent.
[ -n "${HOME:-}" ] && export PATH="$HOME/.local/bin:$PATH"

BOOT="$ROOT/scripts/bootstrap.py"
[ -f "$BOOT" ] || exit 0

# NB: no pointer-only fast path here. The interpreter pointer (engine-python.txt)
# survives a plugin update that changes dependencies, so short-circuiting on its
# existence would skip the background rebuild on exactly the update case it's meant to
# handle. Instead always hand off to `bootstrap.py --background`, which checks the
# content STAMP (the real freshness gate) and returns in milliseconds when current.

# Find a Python >= 3.10. Prefer names likely to be a real CPython on each OS.
PY=""
for cand in python3 python py python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  fi
done

# No suitable Python: stay silent here (this is a background hook). The MCP launcher
# surfaces a clear, actionable message the first time the server is spawned.
[ -n "$PY" ] || exit 0

exec "$PY" "$BOOT" --background
