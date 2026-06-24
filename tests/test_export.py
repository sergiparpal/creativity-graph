"""Tests for the human-facing exporter (R1) — kg_export / graph.html / GRAPH_REPORT.md.

Pure render/serialize logic (_render_data / _report_md / _bridge_set) is tested with synthetic derived
rows; the engine path is exercised for both artifacts being written, self-contained offline HTML, and
READ-ONLY invariance (graph.json + index.sqlite + canon + audit byte-unchanged — projector stays the sole
writer of the derived layer; the exporter only writes its two disposable artifacts).
"""
from __future__ import annotations

import json

from kg_engine.export import _bridge_set, _render_data, _report_md, build_html, export
from kg_engine.model import edge_id
from kg_engine.templates.graph_html import HTML_TEMPLATE


def _n(nid, *, degree=0, provenance="span-present", authored_by="agent", community=0,
       structural_bridge=0, spec_betweenness=0.0, gate_on=0, label=None):
    return {"id": nid, "label": label or nid, "degree": degree, "provenance": provenance,
            "authored_by": authored_by, "community": community, "structural_bridge": structural_bridge,
            "spec_betweenness": spec_betweenness, "gate_on": gate_on}


def _e(eid, src, tgt, *, relation="grounds", epistemic_state="grounded", provenance="span-present",
       source_file="s.md", span="x"):
    return {"id": eid, "source": src, "target": tgt, "relation": relation,
            "epistemic_state": epistemic_state, "provenance": provenance, "source_file": source_file,
            "span": span}


# --------------------------------------------------------------------------- render data (the HTML payload)


def test_three_axes_on_independent_fields():
    model = {"nodes": [_n("a", provenance="inferred", authored_by="human")],
             "edges": [_e("e1", "a", "b", epistemic_state="unverified", provenance="inferred")],
             "gate_on": 0, "stale_verdicts": []}
    d = _render_data(model)
    node = d["nodes"][0]
    link = d["links"][0]
    # the three axes are SEPARATE fields, never collapsed to one scalar
    assert node["provenance"] == "inferred" and node["authored_by"] == "human"     # two node axes
    assert link["epistemic_state"] == "unverified" and link["provenance"] == "inferred"  # two edge axes
    assert "provenance" in node and "authored_by" in node and node["provenance"] != node["authored_by"]


def test_failed_and_rejected_edges_are_rendered_not_filtered():
    model = {"nodes": [_n("a", degree=2), _n("b"), _n("c")],
             "edges": [_e("e1", "a", "b", epistemic_state="failed"),
                       _e("e2", "a", "c", epistemic_state="rejected"),
                       _e("e3", "a", "b", epistemic_state="grounded")],
             "gate_on": 0, "stale_verdicts": []}
    states = {l["epistemic_state"] for l in _render_data(model)["links"]}
    assert {"failed", "rejected", "grounded"} <= states   # §1.7 — negative information is DRAWN


def test_node_size_is_degree_only_no_bridge_metric_in_payload():
    model = {"nodes": [_n("a", degree=4, spec_betweenness=0.9, structural_bridge=1)],
             "edges": [], "gate_on": 0, "stale_verdicts": []}
    node = _render_data(model)["nodes"][0]
    assert node["degree"] == 4                       # the size channel
    # the bridge metric is NEVER in the per-node payload, so size can ONLY be degree
    assert "spec_betweenness" not in node and "betweenness" not in node
    # and the template's radius function is degree-only
    assert "n.degree" in HTML_TEMPLATE and "DEGREE ONLY" in HTML_TEMPLATE


def test_bridge_highlight_is_gate_aware():
    # gate OFF -> the honest structural_bridge advisory; spec_betweenness is gated out
    nodes = [_n("sb", structural_bridge=1, spec_betweenness=0.0),
             _n("sp", structural_bridge=0, spec_betweenness=0.9)]
    assert _bridge_set(nodes, 0) == {"sb"}
    assert _bridge_set(nodes, 1) == {"sp"}            # gate ON -> the confound-corrected spec_betweenness
    off = _render_data({"nodes": nodes, "edges": [], "gate_on": 0, "stale_verdicts": []})
    on = _render_data({"nodes": nodes, "edges": [], "gate_on": 1, "stale_verdicts": []})
    assert off["ranked_by"] == "structural_bridge" and on["ranked_by"] == "spec_betweenness"
    assert {n["id"] for n in off["nodes"] if n["bridge"]} == {"sb"}
    assert {n["id"] for n in on["nodes"] if n["bridge"]} == {"sp"}


def test_render_data_is_deterministic():
    model = {"nodes": [_n("b"), _n("a")], "edges": [_e("e2", "b", "a"), _e("e1", "a", "b")],
             "gate_on": 0, "stale_verdicts": []}
    d1 = json.dumps(_render_data(model), sort_keys=True)
    d2 = json.dumps(_render_data(model), sort_keys=True)
    assert d1 == d2
    assert [n["id"] for n in _render_data(model)["nodes"]] == ["a", "b"]   # sorted by id


# --------------------------------------------------------------------------- report


def test_report_counts_come_from_kg_metrics():
    metrics = {"nodes": 7, "edges": 9, "edges_by_epistemic_state": {"grounded": 5, "failed": 1}}
    md = _report_md(metrics, [_n("a")], [_e("e1", "a", "b")], [], 0)
    assert "**Nodes:** 7" in md and "**Edges:** 9" in md
    assert "grounded 5" in md and "failed 1" in md


def test_report_lists_falsification_stale_and_source_files():
    nodes = [_n("a", community=0), _n("b", community=0)]
    edges = [_e("e1", "a", "b", epistemic_state="failed", relation="attacked_by", source_file="a.md"),
             _e("e2", "a", "b", epistemic_state="grounded", source_file="b.md")]
    stale = [{"edge_id": "e2", "reason": "span-no-longer-in-source"}]
    md = _report_md({"nodes": 2, "edges": 2, "edges_by_epistemic_state": {}}, nodes, edges, stale, 0)
    assert "Falsification memory" in md and "a --attacked_by--> b" in md   # §1.7 surfaced
    assert "Stale verdicts" in md and "e2" in md                            # R3
    assert "`a.md`: 1 edge" in md and "`b.md`: 1 edge" in md                # R4 per-file counts


# --------------------------------------------------------------------------- engine path (read-only)


def _build(engine):
    engine.kg_write({"edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "A compression stands in for many observations and grounds the claims beneath it",
         "provenance": "span-present", "authored_by": "agent"},
        {"source": "betweenness", "target": "generality-confound", "relation": "attacked_by",
         "span": "Betweenness is confounded by the generality confound",
         "provenance": "span-present", "authored_by": "agent"}]})
    engine.kg_ground(edge_id("compression", "grounds", "claim"), "grounded")
    engine.kg_ground(edge_id("betweenness", "attacked_by", "generality-confound"), "failed")
    engine.kg_propose({"edges": [{"source": "degree", "target": "importance", "relation": "approximates"}]})
    engine.projector.project()


def _derived_bytes(engine):
    p = engine.projector
    return (p.graph_path.read_bytes() if p.graph_path.exists() else b"",
            p.db_path.read_bytes() if p.db_path.exists() else b"")


def test_both_artifacts_written_non_empty_and_offline(engine):
    _build(engine)
    out = engine.kg_export("all")
    assert out["ok"] and out["kind"] == "all"
    html = engine.projector.derived / "graph.html"
    report = engine.projector.derived / "GRAPH_REPORT.md"
    assert html.exists() and report.exists()
    htext = html.read_text(encoding="utf-8")
    rtext = report.read_text(encoding="utf-8")
    assert len(htext) > 500 and len(rtext) > 200
    # self-contained + fully offline: no network, no external script
    assert "http://" not in htext and "https://" not in htext
    assert "<script src" not in htext
    assert "window.__KG_DATA__" in htext
    # the failed edge is present in the inlined data (drawn, never filtered)
    assert '"epistemic_state": "failed"' in htext
    # the report's counts agree with kg_metrics
    m = engine.kg_metrics()
    assert f"**Edges:** {m['edges']}" in rtext and f"**Nodes:** {m['nodes']}" in rtext


def test_kg_export_is_read_only_on_projector_files_and_canon(engine):
    _build(engine)  # ends with a fresh project()
    canon_before = {p.name: p.read_bytes() for p in engine.canon.note_paths()}
    audit_before = engine._audit_path().read_bytes() if engine._audit_path().exists() else b""
    derived_before = _derived_bytes(engine)

    engine.kg_export("all")

    canon_after = {p.name: p.read_bytes() for p in engine.canon.note_paths()}
    audit_after = engine._audit_path().read_bytes() if engine._audit_path().exists() else b""
    assert canon_after == canon_before          # never writes the canon
    assert audit_after == audit_before          # never stamps a verdict
    assert _derived_bytes(engine) == derived_before   # projector stays the SOLE writer of graph.json/index.sqlite


def test_html_escapes_script_close_in_labels(engine):
    """A node label containing `</script>` must NOT break out of the inlined <script> block (every `<`/`>`
    is \\uXXXX-escaped), so the document has exactly one real closing </script> tag (the template's)."""
    from kg_engine.model import Node
    engine.canon.write_nodes([Node(id="x", label="evil</script><b>pwn")], message="seed")
    engine.projector.project()
    engine.kg_export("html")
    htext = (engine.projector.derived / "graph.html").read_text(encoding="utf-8")
    assert htext.count("</script>") == 1       # only the legitimate closing tag survives
    assert "evil</script>" not in htext        # the raw breakout sequence is gone
    assert "\\u003c/script\\u003e" in htext    # the label's close-tag was unicode-escaped in the data


def test_html_escapes_script_data_double_escape_breakout(engine):
    """review-H1: a `<!--<script>` label must NOT defeat the inlining via the WHATWG
    script-data-double-escape state (which a `</`-only escape would miss, swallowing the template's real
    </script>). Every `<`/`>` is \\uXXXX-escaped, so no literal markup survives and the close still fires."""
    from kg_engine.model import Node
    engine.canon.write_nodes([Node(id="x", label="<!--<script>")], message="seed")
    engine.projector.project()
    engine.kg_export("html")
    htext = (engine.projector.derived / "graph.html").read_text(encoding="utf-8")
    assert "<!--<script>" not in htext         # the dangerous sequence never appears literally
    assert htext.count("</script>") == 1       # the template's close is not swallowed
    assert "\\u003c" in htext                   # angle brackets were unicode-escaped in the inlined data


def test_export_dispatch_kinds(engine):
    _build(engine)
    assert export(engine, "html")["report_path"] is None
    assert export(engine, "report")["html_path"] is None
    bad = export(engine, "bogus")
    assert bad["ok"] is False and "unknown kind" in bad["error"]


def test_cli_all_smoke_and_bad_kind(engine, monkeypatch):
    import kg_engine.server as server_mod
    import kg_engine.export as export_mod
    _build(engine)
    monkeypatch.setattr(server_mod, "build_engine_from_env", lambda **kw: engine)
    assert export_mod._main(["all"]) == 0
    assert (engine.projector.derived / "graph.html").exists()
    assert export_mod._main(["bogus"]) == 2   # usage error
