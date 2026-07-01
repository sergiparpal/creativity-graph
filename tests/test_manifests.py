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

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_validate_plugin():
    spec = importlib.util.spec_from_file_location(
        "kg_validate_plugin_fix", ROOT / "scripts" / "validate_plugin.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pyproject_text() -> str:
    # parse pyproject.toml as TEXT, not via tomllib: tomllib is stdlib only on 3.11+ and the project
    # supports (and CI exercises) 3.10. The assertions below need only a couple of pins/keys.
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


# ---- config-manifests-1: backend extra floor is the version that ships output_config ----
def test_backend_extra_floor_ships_output_config():
    m = re.search(r"anthropic\s*>=\s*(\d+)\.(\d+)", _pyproject_text())
    assert m, "no `anthropic>=X.Y` floor pin found in pyproject.toml"
    major, minor = int(m.group(1)), int(m.group(2))
    # output_config landed on the standard messages.create() in 0.77.0 (2026-01-29); 0.73.0 only
    # had the beta output_format param, which backend.py does not use.
    assert (major, minor) >= (0, 77), f"backend anthropic floor {major}.{minor} predates output_config (0.77.0)"


def test_backend_actually_uses_output_config():
    # the floor only matters because the code uses the post-0.77 surface — pin that link so the
    # two cannot silently drift apart.
    src = (ROOT / "scripts" / "kg_engine" / "backend.py").read_text(encoding="utf-8")
    assert "output_config" in src and "output_format" not in src


# ---- config-manifests-3/4: the unused, broken extras are gone (and nothing imports them) ----
@pytest.mark.parametrize("removed", ["embeddings", "treesitter"])
def test_broken_extra_removed(removed):
    # the extra would appear as a `name = [...]` key under [project.optional-dependencies]
    assert not re.search(rf"(?m)^\s*{removed}\s*=", _pyproject_text()), \
        f"{removed!r} extra is unused/unmaintained and must not be declared"


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
    # `source_path`/`sensitivity`/`metrics_mode` are the keys the engine actually reads
    # (server.py:build_engine_from_env); `extract_wave_size` is an ORCHESTRATION knob the /kg-build
    # command/skill consumes (kg_engine.waves.resolve_wave_size), never the engine — so it is the one
    # userConfig key with no corresponding env read in build_engine_from_env, and that is intentional.
    assert set(user_config) == {"source_path", "sensitivity", "metrics_mode", "extract_wave_size"}


# ---- config-manifests-6: CI exercises the cross-platform install promise ----
def test_ci_matrix_covers_windows_and_macos():
    pytest.importorskip("yaml")
    import yaml

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    matrix = ci["jobs"]["test"]["strategy"]["matrix"]
    declared = set(matrix.get("os", [])) | {row["os"] for row in matrix.get("include", [])}
    assert {"windows-latest", "macos-latest"} <= declared, f"CI is not cross-OS: {declared}"


# ---- M_tooling-2: the `test` job sets up Node so the launcher tests actually run ----
def test_ci_test_job_sets_up_node():
    pytest.importorskip("yaml")
    import yaml

    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = ci["jobs"]["test"]["steps"]
    # the Node-dependent launcher tests skip silently when node is absent; the job must provision it
    # itself rather than rely on the runner image happening to preinstall it.
    assert any(str(s.get("uses", "")).startswith("actions/setup-node") for s in steps), \
        "the `test` job must set up Node so tests/test_launchers.py is not silently skipped"


# ---- M_tooling-3: pyproject version cross-check is scoped to the [project] table ----
def test_pyproject_version_check_is_project_table_scoped(tmp_path, monkeypatch):
    vp = _load_validate_plugin()
    # a stray `version = "9.9.9"` line under another table ABOVE [project] must NOT shadow the
    # [project] version: a line-anchored re.search would lock onto 9.9.9 and validate the wrong line.
    toml = (
        '[build-system]\n'
        'requires = ["hatchling"]\n'
        'version = "9.9.9"\n'   # stray line that the old regex would have matched first
        '\n'
        '[project]\n'
        'name = "kg-engine"\n'
        'version = "0.3.3"\n'
    )
    (tmp_path / "pyproject.toml").write_text(toml, encoding="utf-8")
    monkeypatch.setattr(vp, "ROOT", tmp_path)
    errors: list[str] = []
    assert vp._grep_project_version("pyproject.toml", errors) == "0.3.3"
    assert errors == []


def test_pyproject_version_check_missing_project_version_is_none(tmp_path, monkeypatch):
    vp = _load_validate_plugin()
    # [project] present but carrying no version line -> None (the caller turns that into an error,
    # never a silent pass on a stray line elsewhere).
    toml = '[tool.foo]\nversion = "1.2.3"\n\n[project]\nname = "kg-engine"\n'
    (tmp_path / "pyproject.toml").write_text(toml, encoding="utf-8")
    monkeypatch.setattr(vp, "ROOT", tmp_path)
    assert vp._grep_project_version("pyproject.toml", []) is None


def test_pyproject_version_check_matches_real_repo():
    # sanity: the scoped getter still finds the real [project] version in the shipped pyproject.
    vp = _load_validate_plugin()
    found = vp._grep_project_version("pyproject.toml", [])
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert found == plugin["version"]


# ---- generative-layer manifests: the new command/agent are discovered and declare only real tools ----

_NS = "mcp__plugin_sproutgraph_sproutgraph__"


def _registered_mcp_tools() -> set:
    """The set of MCP tool basenames the server actually registers (every @mcp.tool() in _register)."""
    src = (ROOT / "scripts" / "kg_engine" / "server.py").read_text(encoding="utf-8")
    # tolerate intervening decorators between @mcp.tool() and the def (e.g. the @_tool_result transport
    # envelope) so the scrape stays robust to wrapper decorators.
    return set(re.findall(r"@mcp\.tool\(\)\s*\n\s*(?:@[\w.]+\s*\n\s*)*def\s+(\w+)\s*\(", src))


def _frontmatter(path):
    import yaml
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", path.read_text(encoding="utf-8"), re.DOTALL)
    assert m, f"{path} has no YAML frontmatter"
    return yaml.safe_load(m.group(1))


def _declared_mcp_tools(fm) -> set:
    raw = fm.get("allowed-tools") or fm.get("tools") or ""
    if isinstance(raw, list):
        raw = ",".join(raw)
    return {t.strip()[len(_NS):] for t in raw.split(",") if _NS in t}


def test_kg_generate_command_discovered_and_valid():
    pytest.importorskip("yaml")
    fm = _frontmatter(ROOT / "commands" / "kg-generate.md")
    assert fm.get("description")
    declared = _declared_mcp_tools(fm)
    assert {"kg_generate", "kg_propose"} <= declared      # the command's core surface
    assert declared <= _registered_mcp_tools(), f"unknown tools: {declared - _registered_mcp_tools()}"


def test_kg_generator_agent_discovered_and_valid():
    pytest.importorskip("yaml")
    fm = _frontmatter(ROOT / "agents" / "generator.md")
    assert fm.get("name") == "kg-generator" and fm.get("description")
    declared = _declared_mcp_tools(fm)
    assert {"kg_generate", "kg_propose"} <= declared
    # the language layer must NOT hold a verdict tool — generation never grounds
    assert "kg_ground" not in declared
    assert declared <= _registered_mcp_tools()


def test_all_commands_and_agents_declare_only_existing_tools():
    pytest.importorskip("yaml")
    valid = _registered_mcp_tools()
    for path in sorted((ROOT / "commands").glob("*.md")) + sorted((ROOT / "agents").glob("*.md")):
        declared = _declared_mcp_tools(_frontmatter(path))
        assert declared <= valid, f"{path.name} declares unknown MCP tools: {declared - valid}"


def test_generative_layer_tools_are_registered():
    valid = _registered_mcp_tools()
    assert {"kg_propose", "kg_generate", "kg_operate", "kg_absorption"} <= valid


def test_kg_status_registered():
    """Transport-resilience pass: the projection-free status/coverage probe is registered as an
    @mcp.tool() (an 18th tool) so a session can confirm progress / resume a partial build without
    grepping the filesystem."""
    assert "kg_status" in _registered_mcp_tools()


def test_kg_agenda_registered():
    """R6: the read-only agenda tool is registered as a 16th @mcp.tool() (register-before-reference —
    kg-query.md / kg-ground.md grant it, and test_all_commands_and_agents_declare_only_existing_tools
    requires declared ⊆ registered)."""
    assert "kg_agenda" in _registered_mcp_tools()


def test_kg_export_registered_and_kg_view_command_valid():
    """R1: the read-only exporter is registered as the 17th @mcp.tool(), and /kg-view declares only it."""
    pytest.importorskip("yaml")
    assert "kg_export" in _registered_mcp_tools()
    fm = _frontmatter(ROOT / "commands" / "kg-view.md")
    assert fm.get("description")
    declared = _declared_mcp_tools(fm)
    assert declared == {"kg_export"}            # /kg-view grants ONLY the read-only export tool
    assert declared <= _registered_mcp_tools()
