#!/usr/bin/env python3
"""Deterministic structural validation of the plugin (stdlib only).

A CI-safe stand-in for `claude plugin validate --strict` that runs anywhere: it parses every
manifest, checks that each declared component file exists, and verifies the plugin/marketplace
versions agree. The real `claude plugin validate --strict` runs as a best-effort CI job; this is the
hard gate that always runs. Exit 0 = OK; nonzero = the failures are printed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].+)?$")  # SemVer-ish (allows pre-release / build metadata)


def _grep_version(rel: str, pattern: str, errors: list[str]) -> str | None:
    p = ROOT / rel
    if not p.exists():
        errors.append(f"missing file: {rel}")
        return None
    m = re.search(pattern, p.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None

REQUIRED_AGENTS = ["extractor", "grounder", "annotator", "adversarial-grounder", "evaluator"]
REQUIRED_COMMANDS = ["kg-build", "kg-ground", "kg-query", "kg-eval", "kg-experiment"]


def _load_json(rel: str, errors: list[str]) -> dict | None:
    p = ROOT / rel
    if not p.exists():
        errors.append(f"missing file: {rel}")
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"invalid JSON in {rel}: {e}")
        return None


def main() -> int:
    errors: list[str] = []

    plugin = _load_json(".claude-plugin/plugin.json", errors)
    version = None
    if plugin is not None:
        for key in ("name", "version", "description"):
            if not plugin.get(key):
                errors.append(f"plugin.json missing '{key}'")
        version = plugin.get("version")
        if version is not None and not isinstance(version, str):
            errors.append(f"plugin.json 'version' must be a string, got {type(version).__name__}")
            version = None
        elif isinstance(version, str) and not _SEMVER.match(version):
            errors.append(f"plugin.json 'version' is not valid SemVer: {version!r}")

    mcp = _load_json(".mcp.json", errors)
    if mcp is not None and "creativity-graph" not in (mcp.get("mcpServers") or {}):
        errors.append(".mcp.json: no 'creativity-graph' server declared")

    _load_json("hooks/hooks.json", errors)

    market = _load_json(".claude-plugin/marketplace.json", errors)
    if market is not None and version is not None:
        # check EVERY matching entry, not just the first — a duplicate with a wrong version must fail
        mvs = [p.get("version") for p in market.get("plugins", []) if p.get("name") == "creativity-graph"]
        if not mvs:
            errors.append("marketplace.json: no 'creativity-graph' plugin entry")
        for mv in mvs:
            if mv != version:
                errors.append(f"version mismatch: plugin.json={version} marketplace.json entry={mv}")

    # versions in pyproject.toml and the package __init__ must agree with the plugin manifest too
    if version is not None:
        py_v = _grep_version("pyproject.toml", r'^\s*version\s*=\s*"([^"]+)"', errors)
        init_v = _grep_version("scripts/kg_engine/__init__.py", r'__version__\s*=\s*"([^"]+)"', errors)
        if py_v is not None and py_v != version:
            errors.append(f"version mismatch: plugin.json={version} pyproject.toml={py_v}")
        if init_v is not None and init_v != version:
            errors.append(f"version mismatch: plugin.json={version} kg_engine.__version__={init_v}")

    for stem in REQUIRED_AGENTS:
        if not (ROOT / "agents" / f"{stem}.md").exists():
            errors.append(f"missing agent: agents/{stem}.md")
    for stem in REQUIRED_COMMANDS:
        if not (ROOT / "commands" / f"{stem}.md").exists():
            errors.append(f"missing command: commands/{stem}.md")

    for rel in ("skills/creativity-graph/SKILL.md", "scripts/launch_server.sh",
                "scripts/kg_engine/server.py", "pack/pack.yaml",
                "hooks/bootstrap.sh", "hooks/precontext.py"):  # hook-referenced scripts must exist too
        if not (ROOT / rel).exists():
            errors.append(f"missing file: {rel}")

    if errors:
        print("PLUGIN VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PLUGIN OK: creativity-graph v{version} — manifests parse, all components present, versions agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
