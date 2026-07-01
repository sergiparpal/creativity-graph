"""Regression tests for harness CLI/shape-guard fixes.

1. `ideation` with `outputs` as a list (or top-level array) raises a clean ValueError, not an
   opaque AttributeError from `_ordered_conditions(...).items()`.
2. `specificity` with a GIVEN-but-missing source path emits an error/notice instead of silently
   scoring the demo IDF corpus.
"""
from __future__ import annotations

import pytest

from kg_engine import harness


# --- #1: ideation shape guard --------------------------------------------------------------------


def test_ideation_list_input_raises_valueerror_not_attributeerror():
    with pytest.raises(ValueError):
        harness.ideation(["A is connected to B.", "C bridges D."], "some source text")


def test_ideation_non_dict_scalar_raises_valueerror():
    with pytest.raises(ValueError):
        harness.ideation("not a dict", "src")


def test_ideation_dict_input_still_works():
    res = harness.ideation({"control": ["A is connected to B."], "graph": ["A bridges B."]},
                           "entropy grounds time")
    assert "table" in res and "control" in res["table"] and "graph" in res["table"]


def test_ideation_cli_list_surfaces_clean_usage_error(tmp_path, capsys):
    p = tmp_path / "outputs.json"
    # top-level "outputs" is a list -> obc becomes a list -> must be a clean usage error (rc 2)
    p.write_text('{"source": "s", "outputs": ["a", "b"]}', encoding="utf-8")
    rc = harness._main(["ideation", str(p)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "[harness]" in err
    assert "AttributeError" not in err


# --- #2: specificity given-but-missing source ----------------------------------------------------


def test_specificity_cli_missing_given_source_errors(tmp_path, capsys):
    # a real graph.json so the graph load succeeds; the source path is GIVEN but does not exist.
    gpath = tmp_path / "graph.json"
    gpath.write_text('{"directed": true, "nodes": [{"id": "a", "label": "system"},'
                     '{"id": "b", "label": "entropy"}, {"id": "c", "label": "time"}],'
                     '"links": [{"source": "a", "target": "b"}, {"source": "a", "target": "c"}]}',
                     encoding="utf-8")
    missing = tmp_path / "does_not_exist.md"
    rc = harness._main(["specificity", str(gpath), str(missing)])
    # must NOT silently score the demo corpus and return 0 — a clean _LoadError -> rc 2 + stderr notice
    assert rc == 2
    err = capsys.readouterr().err
    assert "source path not found" in err


def test_specificity_cli_no_source_arg_still_falls_back_with_notice(tmp_path, capsys):
    gpath = tmp_path / "graph.json"
    gpath.write_text('{"directed": true, "nodes": [{"id": "a", "label": "system"},'
                     '{"id": "b", "label": "entropy"}, {"id": "c", "label": "time"}],'
                     '"links": [{"source": "a", "target": "b"}, {"source": "a", "target": "c"}]}',
                     encoding="utf-8")
    rc = harness._main(["specificity", str(gpath)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "demo IDF corpus" in err
