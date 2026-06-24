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
PLUGIN_NAME = "creativity-graph"


def _grep_version(rel: str, pattern: str, errors: list[str]) -> str | None:
    p = ROOT / rel
    if not p.exists():
        errors.append(f"missing file: {rel}")
        return None
    m = re.search(pattern, p.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None

REQUIRED_AGENTS = ["extractor", "grounder", "annotator", "adversarial-grounder", "evaluator",
                   "generator"]
REQUIRED_COMMANDS = ["kg-build", "kg-ground", "kg-query", "kg-eval", "kg-experiment", "kg-generate",
                     "kg-perturb", "kg-view"]
REQUIRED_FILES = ("skills/creativity-graph/SKILL.md",
                  "scripts/kg_engine/server.py", "pack/pack.yaml",
                  # cross-platform install system: SessionStart dispatcher + OS launchers,
                  # the self-provisioning bootstrap, the Node MCP launcher, and the
                  # PreToolUse precontext launcher (all referenced by .mcp.json/hooks.json).
                  "scripts/bootstrap.py", "scripts/launch_server.mjs",
                  "hooks/provision.mjs", "hooks/provision.sh", "hooks/provision.ps1",
                  "hooks/precontext.mjs", "hooks/precontext.py",
                  # semantic canon merge driver (R5): the engine module, its Node launcher, and the
                  # .gitattributes that routes canon/*.md through it.
                  "scripts/kg_engine/canonmerge.py", "scripts/canon_merge_driver.mjs",
                  ".gitattributes")


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


def _require_files(rels: list[str], errors: list[str]) -> None:
    """Append a uniform 'missing file: {rel}' error for every relative path absent under ROOT."""
    for rel in rels:
        if not (ROOT / rel).exists():
            errors.append(f"missing file: {rel}")


def _check_plugin(errors: list[str]) -> str | None:
    """Validate plugin.json's required fields and version; return the version string (or None)."""
    plugin = _load_json(".claude-plugin/plugin.json", errors)
    if plugin is None:
        return None
    for key in ("name", "version", "description"):
        if not plugin.get(key):
            errors.append(f"plugin.json missing '{key}'")
    version = plugin.get("version")
    if version is not None and not isinstance(version, str):
        errors.append(f"plugin.json 'version' must be a string, got {type(version).__name__}")
        return None
    if isinstance(version, str) and not _SEMVER.match(version):
        errors.append(f"plugin.json 'version' is not valid SemVer: {version!r}")
    return version


def _check_mcp(errors: list[str]) -> None:
    """Validate that .mcp.json declares the creativity-graph server."""
    mcp = _load_json(".mcp.json", errors)
    if mcp is not None and PLUGIN_NAME not in (mcp.get("mcpServers") or {}):
        errors.append(f".mcp.json: no '{PLUGIN_NAME}' server declared")


def _check_hooks(errors: list[str]) -> None:
    """Validate hooks.json's top-level shape and required event arrays."""
    hooks = _load_json("hooks/hooks.json", errors)
    if hooks is None:
        return
    # structural check, not just JSON-validity (review-nit): the runtime expects a "hooks" object
    # carrying the SessionStart (provision) and PreToolUse (precontext) events as non-empty arrays.
    h = hooks.get("hooks")
    if not isinstance(h, dict):
        errors.append("hooks.json: top-level 'hooks' object missing")
    else:
        for evt in ("SessionStart", "PreToolUse"):
            if not isinstance(h.get(evt), list) or not h.get(evt):
                errors.append(f"hooks.json: missing/empty '{evt}' hook array")


def _check_marketplace(version: str | None, errors: list[str]) -> None:
    """Validate that every marketplace.json creativity-graph entry agrees with the plugin version."""
    market = _load_json(".claude-plugin/marketplace.json", errors)
    if market is not None and version is not None:
        # check EVERY matching entry, not just the first — a duplicate with a wrong version must fail
        mvs = [p.get("version") for p in market.get("plugins", []) if p.get("name") == PLUGIN_NAME]
        if not mvs:
            errors.append(f"marketplace.json: no '{PLUGIN_NAME}' plugin entry")
        for mv in mvs:
            if mv != version:
                errors.append(f"version mismatch: plugin.json={version} marketplace.json entry={mv}")


def _check_engine_versions(version: str | None, errors: list[str]) -> None:
    """Validate that pyproject.toml and the package __init__ version agree with the plugin manifest."""
    # The quote class accepts BOTH single- and double-quoted strings (valid TOML/Python) — a
    # single-quoted version must not slip past the agreement check (review-low). A version line that
    # is absent or unmatched is itself an ERROR, not a silent skip, so a reformatted/missing version
    # can't pass CI.
    if version is None:
        return
    for rel, pat, label in (
        ("pyproject.toml", r'''^\s*version\s*=\s*["']([^"']+)["']''', "pyproject.toml"),
        ("scripts/kg_engine/__init__.py", r'''__version__\s*=\s*["']([^"']+)["']''', "kg_engine.__version__"),
    ):
        found = _grep_version(rel, pat, errors)
        if found is None:
            if (ROOT / rel).exists():  # file present but no parseable version line
                errors.append(f"could not find a version string in {rel}")
        elif found != version:
            errors.append(f"version mismatch: plugin.json={version} {label}={found}")


def _check_components(errors: list[str]) -> None:
    """Validate that every required agent and command markdown file is present."""
    _require_files([f"agents/{stem}.md" for stem in REQUIRED_AGENTS], errors)
    _require_files([f"commands/{stem}.md" for stem in REQUIRED_COMMANDS], errors)


def main() -> int:
    errors: list[str] = []

    version = _check_plugin(errors)
    _check_mcp(errors)
    _check_hooks(errors)
    _check_marketplace(version, errors)
    _check_engine_versions(version, errors)
    _check_components(errors)
    _require_files(list(REQUIRED_FILES), errors)

    if errors:
        print("PLUGIN VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PLUGIN OK: {PLUGIN_NAME} v{version} — manifests parse, all components present, versions agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
