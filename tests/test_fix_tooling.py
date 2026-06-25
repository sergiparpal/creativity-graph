"""Regression tests for the M_tooling fix pass (f4_probe edge_id traceability).

f4_probe.sheet() must write each link's REAL deterministic ``id`` (the canonical
``e_{slug(src)}__{slug(rel)}__{slug(tgt)}`` handle back to the canon) into the ``edge_id``
column, not the positional enumerate index — the index changes across re-projections and
cannot be resolved to a canon edge after a rebuild reorders edges.
"""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_F4_PATH = REPO / "scripts" / "f4_probe.py"


def _load_f4():
    spec = importlib.util.spec_from_file_location("kg_f4_probe_fix", _F4_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f4 = _load_f4()


def _write_graph(path: Path, links: list[dict], nodes: list[dict]) -> None:
    path.write_text(json.dumps({"nodes": nodes, "links": links}), encoding="utf-8")


def test_sheet_writes_real_edge_id_not_positional_index(tmp_path):
    nodes = [{"id": "n_a", "label": "Alpha"}, {"id": "n_b", "label": "Beta"}]
    links = [
        {"id": "e_alpha__supports__beta", "source": "n_a", "target": "n_b",
         "relation": "supports", "confidence": "INFERRED"},
        {"id": "e_beta__refutes__alpha", "source": "n_b", "target": "n_a",
         "relation": "refutes", "confidence": "INFERRED"},
    ]
    graph = tmp_path / "graph.json"
    out = tmp_path / "labels.csv"
    _write_graph(graph, links, nodes)

    f4.sheet(str(graph), n=10, out=str(out), include_extracted=True)

    with open(out, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    written = {r["edge_id"] for r in rows}
    # the canonical deterministic ids are written verbatim — not "0"/"1" positional indices
    assert written == {"e_alpha__supports__beta", "e_beta__refutes__alpha"}


def test_sheet_falls_back_to_index_when_link_lacks_id(tmp_path):
    # a link with no `id` (degenerate/legacy export) still gets a value: the positional index.
    nodes = [{"id": "n_a", "label": "Alpha"}, {"id": "n_b", "label": "Beta"}]
    links = [{"source": "n_a", "target": "n_b", "relation": "supports", "confidence": "INFERRED"}]
    graph = tmp_path / "graph.json"
    out = tmp_path / "labels.csv"
    _write_graph(graph, links, nodes)

    f4.sheet(str(graph), n=10, out=str(out), include_extracted=True)

    with open(out, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    # falls back to the positional index 0 (as a string in the CSV) — never empty
    assert rows[0]["edge_id"] == "0"
