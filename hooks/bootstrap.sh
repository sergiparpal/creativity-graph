#!/usr/bin/env bash
# SessionStart hook: install/refresh the engine venv (diff-the-manifest), then reconcile the canon.
# Best-effort: never block or fail the session.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[ -z "$ROOT" ] && exit 0
DATA="${CLAUDE_PLUGIN_DATA:-$ROOT/.kg-data}"
mkdir -p "$DATA"

# diff-the-manifest: only (re)sync when the bundled deps change. A separate sentinel records a
# SUCCESSFUL sync, so a sync that fails (or uv missing) is retried next session instead of being
# silently marked done — and the "synced" line is printed only when the sync actually succeeded.
SENTINEL="$DATA/.synced-pyproject.toml"
if ! cmp -s "$ROOT/pyproject.toml" "$SENTINEL" 2>/dev/null; then
  cp "$ROOT/pyproject.toml" "$DATA/pyproject.toml" 2>/dev/null || true  # uv reads it from $DATA
  if command -v uv >/dev/null 2>&1 && ( cd "$DATA" && uv sync --no-install-project ) >/dev/null 2>&1; then
    cp "$ROOT/pyproject.toml" "$SENTINEL" 2>/dev/null || true
    echo "[creativity-graph] engine venv synced"
  fi
fi

# Reconcile the canon at session start (P_reconcile, §1.8). Use a FULL sweep here: the per-file
# mtime/size pre-filter is only a within-session optimisation, and the once-per-session full re-hash
# is what actually defeats mtime-spoofed forged verdicts. Never blocking.
PY="$DATA/.venv/bin/python"
if [ -x "$PY" ] && [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "${CLAUDE_PROJECT_DIR}/canon" ]; then
  PYTHONPATH="$ROOT/scripts" "$PY" - <<'PYEOF' 2>/dev/null || true
import os
from kg_engine.canon import Canon
from kg_engine.reconciler import Reconciler
rep = Reconciler(Canon(os.environ["CLAUDE_PROJECT_DIR"])).scan(full_sweep=True)
if rep.requarantined:
    print(f"[creativity-graph] reconcile re-quarantined {len(rep.requarantined)} forged verdict(s)")
PYEOF
fi
exit 0
