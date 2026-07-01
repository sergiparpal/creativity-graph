"""Regression tests for group probe_validate_backend_lightrag (a11).

Covers four defects:
  1. f4_probe.main exits cleanly (SystemExit) on a corrupt graph.json instead of a raw traceback.
  2. validate_plugin._check_marketplace handles a malformed 'plugins' shape without raising.
  3. backend._exit_code is non-zero when zero edges were accepted (edgeless / all-rejected build).
  4. lightrag_arm._source_signature / _needs_rebuild detect a changed source (pure helper, no package).
"""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_backend():
    """Import kg_engine.backend, stubbing the heavy `.server` dependency if it can't import (so the pure
    `_exit_code` helper is testable in isolation, independent of the projection stack)."""
    import sys
    import types

    try:
        from kg_engine import backend
        return backend
    except Exception:
        stub = types.ModuleType("kg_engine.server")
        stub.KGEngine = object
        stub.build_engine_from_env = lambda **k: None
        sys.modules["kg_engine.server"] = stub
        from kg_engine import backend
        return backend


# --- #1 f4_probe on a corrupt json exits cleanly -----------------------------
def test_f4_probe_corrupt_json_exits_cleanly(tmp_path, monkeypatch):
    f4_probe = _load_module("kg_f4_probe_a11", "scripts/f4_probe.py")

    bad = tmp_path / "graph.json"
    bad.write_text("{ this is not valid json ", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["f4_probe.py", "summary", str(bad)])
    with pytest.raises(SystemExit) as ei:
        f4_probe.main()
    # a clean message, not exit-code 0 / a raw JSONDecodeError traceback
    assert ei.value.code not in (0, None)
    assert "could not read" in str(ei.value.code)


def test_f4_probe_corrupt_csv_exits_cleanly(tmp_path, monkeypatch):
    import csv

    f4_probe = _load_module("kg_f4_probe_a11b", "scripts/f4_probe.py")

    # a CSV that trips csv.Error at read time (an unterminated quoted field with a huge run)
    bad = tmp_path / "labels.csv"
    bad.write_text('edge_id,verdict\n"' + "x" * (csv.field_size_limit() + 10), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["f4_probe.py", "score", str(bad)])
    with pytest.raises(SystemExit) as ei:
        f4_probe.main()
    assert ei.value.code not in (0, None)


# --- #2 validate_plugin marketplace check tolerates a malformed shape ---------
def _load_validate_plugin():
    return _load_module("validate_plugin_a11", "scripts/validate_plugin.py")


def test_marketplace_plugins_not_a_list(monkeypatch):
    vp = _load_validate_plugin()
    monkeypatch.setattr(vp, "_load_json", lambda rel, errs: {"plugins": "oops-a-string"})
    errors: list[str] = []
    vp._check_marketplace("1.2.3", errors)  # must not raise
    assert any("plugins" in e for e in errors)


def test_marketplace_entry_not_a_dict(monkeypatch):
    vp = _load_validate_plugin()
    # a list mixing a bare string with a well-formed entry: the string must not raise on .get()
    monkeypatch.setattr(vp, "_load_json",
                        lambda rel, errs: {"plugins": ["junk", {"name": vp.PLUGIN_NAME, "version": "1.2.3"}]})
    errors: list[str] = []
    vp._check_marketplace("1.2.3", errors)  # must not raise
    assert any("malformed plugin entry" in e for e in errors)
    # the valid entry still validated (version matches → no version-mismatch error)
    assert not any("version mismatch" in e for e in errors)


def test_marketplace_wellformed_still_passes(monkeypatch):
    vp = _load_validate_plugin()
    monkeypatch.setattr(vp, "_load_json",
                        lambda rel, errs: {"plugins": [{"name": vp.PLUGIN_NAME, "version": "9.9.9"}]})
    errors: list[str] = []
    vp._check_marketplace("9.9.9", errors)
    assert errors == []


# --- #3 backend exit code non-zero when zero edges accepted ------------------
def test_backend_exit_code_edgeless_is_nonzero():
    backend = _load_backend()

    # nodes landed, every edge REJECTED, no rollback, no failed sections → edgeless graph
    out = {
        "failed_sections": [],
        "dispositions": {"ACCEPTED": 3, "DEMOTED": 0, "QUARANTINED": 0, "REJECTED": 7},
        "metrics": {"nodes": 3, "edges": 0},
    }
    assert backend._exit_code(out) == 1


def test_backend_exit_code_all_blocked_no_writes():
    backend = _load_backend()

    out = {
        "failed_sections": [],
        "dispositions": {"ACCEPTED": 0, "DEMOTED": 0, "QUARANTINED": 2, "REJECTED": 5},
        "metrics": {"nodes": 0, "edges": 0},
    }
    assert backend._exit_code(out) == 1


def test_backend_exit_code_failed_sections_is_nonzero():
    backend = _load_backend()

    out = {
        "failed_sections": [{"title": "x", "error": "boom"}],
        "dispositions": {"ACCEPTED": 5, "DEMOTED": 0, "QUARANTINED": 0, "REJECTED": 0},
        "metrics": {"nodes": 5, "edges": 4},
    }
    assert backend._exit_code(out) == 1


def test_backend_exit_code_healthy_is_zero():
    backend = _load_backend()

    out = {
        "failed_sections": [],
        "dispositions": {"ACCEPTED": 6, "DEMOTED": 1, "QUARANTINED": 0, "REJECTED": 2},
        "metrics": {"nodes": 5, "edges": 4},
    }
    assert backend._exit_code(out) == 0


# --- #4 lightrag signature helper detects a changed source -------------------
def test_lightrag_signature_changes_with_source(tmp_path):
    from kg_engine import lightrag_arm

    src = tmp_path / "source.md"
    src.write_text("original content", encoding="utf-8")
    sig1 = lightrag_arm._source_signature(src)
    # rewrite with different-length content → size (and mtime_ns) change → signature changes
    src.write_text("original content, now noticeably longer", encoding="utf-8")
    sig2 = lightrag_arm._source_signature(src)
    assert sig1 != sig2


def test_lightrag_needs_rebuild_on_changed_source(tmp_path):
    from kg_engine import lightrag_arm

    wd = tmp_path / "store"
    wd.mkdir()
    src = tmp_path / "source.md"
    src.write_text("v1", encoding="utf-8")

    # no index marker yet → must rebuild
    assert lightrag_arm._needs_rebuild(wd, src) is True

    # simulate a completed build: marker present + signature recorded
    (wd / lightrag_arm._INDEX_MARKER).write_text("{}", encoding="utf-8")
    (wd / lightrag_arm._SIGNATURE_FILE).write_text(lightrag_arm._source_signature(src), encoding="utf-8")
    assert lightrag_arm._needs_rebuild(wd, src) is False  # fresh, reuse cache

    # source changes → stale, must rebuild despite the marker being present
    src.write_text("v2 is different and longer", encoding="utf-8")
    assert lightrag_arm._needs_rebuild(wd, src) is True


def test_lightrag_needs_rebuild_missing_signature(tmp_path):
    from kg_engine import lightrag_arm

    wd = tmp_path / "store"
    wd.mkdir()
    src = tmp_path / "source.md"
    src.write_text("v1", encoding="utf-8")
    # marker present but NO recorded signature (an old-format cache) → treat as stale
    (wd / lightrag_arm._INDEX_MARKER).write_text("{}", encoding="utf-8")
    assert lightrag_arm._needs_rebuild(wd, src) is True
