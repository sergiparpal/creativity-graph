"""Human-facing exporter (R1): a deterministic, READ-ONLY render of the derived layer into two fresh,
disposable artifacts under ``${KG_DATA}/derived/`` — a self-contained offline ``graph.html`` and a
``GRAPH_REPORT.md``.

It consumes ONLY the derived layer (the SQLite node/edge rows + their precomputed rank columns, via the
shared read-only ``Projector._agenda_reader()`` seam R6 introduced) and ``engine.kg_metrics()`` (so the
report's headline counts can never drift from the canon). It never reads prose, never writes through
``kg_write``/``kg_ground``, and never ``_atomic_write``s ``graph.json``/``index.sqlite`` — ``projector.py``
stays the sole writer of the derived index. Pure render/serialize; no new "quality" scalar.

Three ways in: ``python -m kg_engine.export html|report|all`` (CLI), ``kg_export`` (a thin read-only MCP
tool), and ``/kg-view``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .canon import _atomic_write
from .templates.graph_html import HTML_TEMPLATE

_FAILURE = ("failed", "rejected")


# --------------------------------------------------------------------------- read model (read-only)


def _load_render_model(engine) -> dict:
    """Read the derived layer through the shared read-only seam. Returns plain dicts + the per-projection
    gate state and the R3 stale-verdict list — never touches the canon or the prose source."""
    nodes, edges = engine.projector._agenda_reader()
    gate_on = int(next((n.get("gate_on") for n in nodes if n.get("gate_on") is not None), 0) or 0)
    stale = engine.projector._read_meta().get("stale_verdicts", []) or []
    return {"nodes": nodes, "edges": edges, "gate_on": gate_on, "stale_verdicts": stale}


def _bridge_set(nodes: list, gate_on: int) -> set:
    """The gate-aware bridge-highlight set, mirroring kg_context's switch (projector.kg_context): when the
    gate is OFF use the honest binary ``structural_bridge`` advisory; when it is ON (the specificity
    weighting earned promotion) use the confound-corrected ``spec_betweenness`` (top by that signal). Node
    SIZE is degree-only elsewhere — the bridge metric never drives size, so the confound can't sneak in."""
    if gate_on:
        ranked = sorted((n for n in nodes if (n.get("spec_betweenness") or 0) > 0),
                        key=lambda n: (n.get("spec_betweenness") or 0, n.get("id")), reverse=True)
        return {n["id"] for n in ranked[:10]}
    return {n["id"] for n in nodes if n.get("structural_bridge")}


def _render_data(model: dict) -> dict:
    """The data inlined into the HTML as ``window.__KG_DATA__`` — the three axes kept on INDEPENDENT
    fields (provenance / authored_by per node; epistemic_state / provenance per edge), node ``degree`` for
    size, and a gate-aware per-node ``bridge`` flag. Deterministic: nodes/links sorted by id."""
    gate_on = int(model.get("gate_on") or 0)
    bridges = _bridge_set(model["nodes"], gate_on)
    nodes = [{
        "id": n["id"],
        "label": n.get("label") or n["id"],
        "degree": n.get("degree") or 0,            # SIZE channel — the honest advisory
        "provenance": n.get("provenance"),         # FILL-opacity channel
        "authored_by": n.get("authored_by"),       # BORDER channel
        "community": n.get("community"),
        "bridge": n["id"] in bridges,              # gate-aware highlight (never the size channel)
    } for n in sorted(model["nodes"], key=lambda n: n["id"])]
    links = [{
        "source": e.get("source"),
        "target": e.get("target"),
        "relation": e.get("relation"),
        "epistemic_state": e.get("epistemic_state"),  # LINE-style channel (failed/rejected DRAWN)
        "provenance": e.get("provenance"),
    } for e in sorted(model["edges"], key=lambda e: e.get("id") or "")]
    return {"nodes": nodes, "links": links, "gate_on": gate_on,
            "ranked_by": ("spec_betweenness" if gate_on else "structural_bridge")}


# --------------------------------------------------------------------------- artifacts


def build_html(engine) -> Path:
    """Render the self-contained offline ``graph.html`` (data inlined, no network, no ``<script src>``)
    and atomically write it under the derived dir. Returns its path. Read-only on the projector files."""
    data = _render_data(_load_render_model(engine))
    # Inline the data as JSON inside a <script> block. A label/relation containing the literal
    # "</script>" would otherwise close the script tag early (HTML-injection); escaping "</" to "<\/"
    # is a no-op for the JS value (\/ is just /) but keeps the HTML parser from seeing a close tag.
    payload = json.dumps(data, sort_keys=True).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__KG_DATA_JSON__", payload)
    out = engine.projector.derived / "graph.html"
    _atomic_write(out, html)
    return out


def _axis_breakdown(items: list, key: str) -> dict:
    out: dict = {}
    for it in items:
        out[it.get(key)] = out.get(it.get(key), 0) + 1
    return out


def _report_md(metrics: dict, nodes: list, edges: list, stale: list, gate_on: int) -> str:
    """The GRAPH_REPORT.md body. Headline counts come from ``kg_metrics`` (so they cannot drift from the
    canon); the per-community axis breakdowns, falsification list, R3 stale verdicts and R4 per-file edge
    counts come from the derived rows. Pure string build — testable in isolation."""
    L: list = []
    L.append("# Knowledge graph report")
    L.append("")
    L.append("> Disposable artifact regenerated by `kg_export` / `/kg-view`. A **read-only** render of the "
             "derived layer — nothing here is authoritative; the human-editable canon is. Failed/rejected "
             "edges are kept (§1.7, never pruned).")
    L.append("")
    L.append("## Summary")
    L.append(f"- **Nodes:** {metrics.get('nodes', 0)}")
    L.append(f"- **Edges:** {metrics.get('edges', 0)}")
    by_state = metrics.get("edges_by_epistemic_state", {}) or {}
    if by_state:
        L.append("- **Edges by epistemic_state:** "
                 + ", ".join(f"{k} {v}" for k, v in sorted(by_state.items())))
    ranked_by = "spec_betweenness" if gate_on else "structural_bridge"
    L.append("")
    L.append("## The three axes (legend, mirrors the HTML)")
    L.append("- **epistemic_state** → edge line: solid green = grounded · dashed = unverified · "
             "**red = failed/rejected** · dotted = hypothesized.")
    L.append("- **authored_by** → node border: deterministic · agent · human.")
    L.append("- **provenance** → node fill opacity: span-present (opaque) · inferred (mid) · hypothesized (faint).")
    L.append(f"- **Node size = degree** (the honest advisory). **Bridge highlight = {ranked_by}** "
             f"(gate {'ON' if gate_on else 'off'}).")

    # per-community breakdown by the three axes
    L.append("")
    L.append("## Communities")
    comms: dict = {}
    for n in nodes:
        comms.setdefault(n.get("community"), []).append(n)
    edges_by_comm: dict = {}
    node_comm = {n["id"]: n.get("community") for n in nodes}
    for e in edges:
        c = node_comm.get(e.get("source"))
        if c is not None and c == node_comm.get(e.get("target")):
            edges_by_comm.setdefault(c, []).append(e)
    if not comms:
        L.append("_(no nodes)_")
    for c in sorted(comms, key=lambda x: (x is None, x)):
        members = comms[c]
        prov = _axis_breakdown(members, "provenance")
        auth = _axis_breakdown(members, "authored_by")
        est = _axis_breakdown(edges_by_comm.get(c, []), "epistemic_state")
        names = ", ".join((m.get("label") or m["id"]) for m in sorted(members, key=lambda m: m["id"])[:8])
        more = " …" if len(members) > 8 else ""
        L.append(f"### Community {c} — {len(members)} node(s)")
        L.append(f"- members: {names}{more}")
        L.append(f"- provenance: {_fmt(prov)}")
        L.append(f"- authored_by: {_fmt(auth)}")
        L.append(f"- intra-community edges by epistemic_state: {_fmt(est) or '—'}")

    # falsification memory (never pruned)
    fails = [e for e in edges if e.get("epistemic_state") in _FAILURE]
    L.append("")
    L.append(f"## Falsification memory (§1.7 — never pruned): {len(fails)}")
    if fails:
        for e in sorted(fails, key=lambda e: e.get("id") or "")[:50]:
            L.append(f"- `{e.get('source')} --{e.get('relation')}--> {e.get('target')}` "
                     f"[{e.get('epistemic_state')}]" + (f" — {_short(e.get('span'))}" if e.get("span") else ""))
    else:
        L.append("_(none — nothing refuted yet)_")

    # R3 stale verdicts
    L.append("")
    L.append(f"## Stale verdicts (R3 — span no longer in source): {len(stale)}")
    if stale:
        for s in stale[:50]:
            L.append(f"- `{s.get('edge_id')}` — {s.get('reason')}")
    else:
        L.append("_(none)_")

    # R4 per-source-file edge counts
    by_file: dict = {}
    for e in edges:
        by_file[e.get("source_file") or "(unattributed)"] = by_file.get(e.get("source_file") or "(unattributed)", 0) + 1
    L.append("")
    L.append("## Source files (R4 — edges per declared source)")
    for f, n in sorted(by_file.items()):
        L.append(f"- `{f}`: {n} edge(s)")
    L.append("")
    return "\n".join(L)


def _fmt(d: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(d.items(), key=lambda kv: str(kv[0]))) if d else ""


def _short(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def build_report(engine) -> Path:
    """Render GRAPH_REPORT.md and atomically write it under the derived dir. Returns its path."""
    model = _load_render_model(engine)
    md = _report_md(engine.kg_metrics(), model["nodes"], model["edges"],
                    model["stale_verdicts"], model["gate_on"])
    out = engine.projector.derived / "GRAPH_REPORT.md"
    _atomic_write(out, md)
    return out


# --------------------------------------------------------------------------- dispatch + CLI


def export(engine, kind: str = "all") -> dict:
    """Build the requested artifact(s) (``html`` | ``report`` | ``all``). Read-only — consumes the derived
    layer, writes only its two disposable artifacts. Returns ``{ok, kind, html_path, report_path}``."""
    kind = (kind or "all").lower()
    if kind not in ("html", "report", "all"):
        return {"ok": False, "kind": kind, "error": f"unknown kind {kind!r}; expected html|report|all",
                "html_path": None, "report_path": None}
    html_path = build_html(engine) if kind in ("html", "all") else None
    report_path = build_report(engine) if kind in ("report", "all") else None
    return {"ok": True, "kind": kind,
            "html_path": str(html_path) if html_path else None,
            "report_path": str(report_path) if report_path else None}


def _main(argv: list) -> int:
    kind = (argv[0] if argv else "all").lower()
    if kind not in ("html", "report", "all"):
        print("usage: python -m kg_engine.export [html|report|all]", file=sys.stderr)
        return 2
    from .server import build_engine_from_env
    engine = build_engine_from_env()
    engine._ensure_projected()  # project-if-stale, then render the derived layer (never writes the canon)
    out = export(engine, kind)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
