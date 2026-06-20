#!/usr/bin/env bash
# MCP server launcher (§2.1 bootstrap hardening).
#
# Launching the server directly as `${CLAUDE_PLUGIN_DATA}/.venv/bin/python` races the SessionStart
# venv build: on a cold first session that path may not exist yet, the spawn fails, and Claude Code
# caches the server as "needs-auth" — dropping all kg_* tools for the whole session. Going through
# bash (which always exists) means the MCP spawn ALWAYS succeeds; this script then self-heals the venv
# if it is missing before exec'ing the real server. Warm sessions skip straight to exec.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA="${CLAUDE_PLUGIN_DATA:-$ROOT/.kg-data}"
PY="$DATA/.venv/bin/python"

if [ ! -x "$PY" ]; then
  mkdir -p "$DATA"
  cp "$ROOT/pyproject.toml" "$DATA/pyproject.toml" 2>/dev/null || true
  if command -v uv >/dev/null 2>&1; then
    ( cd "$DATA" && uv sync --no-install-project ) >/dev/null 2>&1 || true
  fi
fi

export PYTHONPATH="${PYTHONPATH:-$ROOT/scripts}"
exec "$PY" -m kg_engine.server
