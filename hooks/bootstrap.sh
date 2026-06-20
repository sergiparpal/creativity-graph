#!/usr/bin/env bash
# SessionStart hook: install/refresh the engine venv (diff-the-manifest), then reconcile the canon.
# Best-effort: never block or fail the session.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[ -z "$ROOT" ] && exit 0
DATA="${CLAUDE_PLUGIN_DATA:-$ROOT/.kg-data}"
mkdir -p "$DATA"

# diff-the-manifest: only (re)sync when the bundled deps change.
if ! cmp -s "$ROOT/pyproject.toml" "$DATA/pyproject.toml" 2>/dev/null; then
  cp "$ROOT/pyproject.toml" "$DATA/pyproject.toml" 2>/dev/null || true
  if command -v uv >/dev/null 2>&1; then
    ( cd "$DATA" && uv sync --no-install-project ) >/dev/null 2>&1 || true
  fi
  echo "[creativity-graph] engine venv synced"
fi

# Reconcile the canon at session start (P_reconcile, §1.8) — pre-filter sweep, never blocking.
PY="$DATA/.venv/bin/python"
if [ -x "$PY" ] && [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "${CLAUDE_PROJECT_DIR}/canon" ]; then
  PYTHONPATH="$ROOT/scripts" "$PY" - <<'PYEOF' 2>/dev/null || true
import os
from kg_engine.canon import Canon
from kg_engine.reconciler import Reconciler
rep = Reconciler(Canon(os.environ["CLAUDE_PROJECT_DIR"])).scan(full_sweep=False)
if rep.requarantined:
    print(f"[creativity-graph] reconcile re-quarantined {len(rep.requarantined)} forged verdict(s)")
PYEOF
fi
exit 0
