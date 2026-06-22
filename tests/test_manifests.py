"""Regression tests for the config/manifest review-findings fix pass (cluster: config-deps).

These pin the packaging + plugin manifest + CI invariants that drifted from reality:

  * backend extra floor must be the SDK release that actually ships ``output_config`` on
    ``messages.create()`` — the floor `0.69`/`0.73` was wrong (only the beta ``output_format``
    landed there); ``backend.py`` calls ``output_config={"format": ...}``, GA'd in 0.77.0.
  * the ``embeddings`` (sqlite-vss) and ``treesitter`` extras were unused, unmaintained, and
    backed unimplemented modes — declaring them only adds a broken install path. They are gone,
    and nothing in the engine imports them.
  * ``plugin.json`` userConfig must not advertise ``domain``: the engine reads no env var for it,
    so it is dead config that misleadingly looks like it shapes the pack vocabulary.
  * the cross-platform install promise (scripts/bootstrap.py) must be exercised on Windows + macOS
    in CI, not Linux-only.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _extras() -> dict:
    return _pyproject()["project"]["optional-dependencies"]


# ---- config-manifests-1: backend extra floor is the version that ships output_config ----
def test_backend_extra_floor_ships_output_config():
    backend = _extras()["backend"]
    spec = next(s for s in backend if s.replace(" ", "").lower().startswith("anthropic"))
    # parse the lower bound off `anthropic>=X.Y[.Z]`
    floor = spec.split(">=", 1)[1].strip()
    major, minor = (int(x) for x in floor.split(".")[:2])
    # output_config landed on the standard messages.create() in 0.77.0 (2026-01-29); 0.73.0 only
    # had the beta output_format param, which backend.py does not use.
    assert (major, minor) >= (0, 77), f"backend anthropic floor {floor!r} predates output_config (0.77.0)"


def test_backend_actually_uses_output_config():
    # the floor only matters because the code uses the post-0.77 surface — pin that link so the
    # two cannot silently drift apart.
    src = (ROOT / "scripts" / "kg_engine" / "backend.py").read_text(encoding="utf-8")
    assert "output_config" in src and "output_format" not in src


# ---- config-manifests-3/4: the unused, broken extras are gone (and nothing imports them) ----
@pytest.mark.parametrize("removed", ["embeddings", "treesitter"])
def test_broken_extra_removed(removed):
    assert removed not in _extras(), f"{removed!r} extra is unused/unmaintained and must not be declared"


@pytest.mark.parametrize("token", ["sqlite_vss", "tree_sitter", "tree-sitter", "sqlite-vss"])
def test_removed_extras_have_no_importers(token):
    # if any of these ever gets imported, the right fix is to re-pin a working version, not to
    # silently ship a dependency the install can't satisfy — so guard against a dangling import.
    for base in ("scripts", "tests"):
        for py in (ROOT / base).rglob("*.py"):
            if py.name == "test_manifests.py":
                continue  # this file mentions the tokens in assertions
            assert token not in py.read_text(encoding="utf-8"), f"{token!r} imported in {py}"


# ---- config-manifests-7: plugin.json must not advertise dead `domain` config ----
def test_plugin_userconfig_has_no_dead_domain():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    user_config = plugin["userConfig"]
    assert "domain" not in user_config, "domain maps to no engine env var — misleading dead config"
    # the fields that remain are the ones the engine actually reads (server.py:build_engine_from_env)
    assert set(user_config) == {"source_path", "sensitivity", "metrics_mode"}


# ---- config-manifests-6: CI exercises the cross-platform install promise ----
def test_ci_matrix_covers_windows_and_macos():
    pytest.importorskip("yaml")
    import yaml

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]
    declared = set(matrix.get("os", [])) | {row["os"] for row in matrix.get("include", [])}
    assert {"windows-latest", "macos-latest"} <= declared, f"CI is not cross-OS: {declared}"
