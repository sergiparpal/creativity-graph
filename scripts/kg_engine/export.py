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
from collections import Counter
from pathlib import Path

from .atomicio import atomic_write_text as _atomic_write
from .templates.graph_html import HTML_TEMPLATE

_FAILURE = ("failed", "rejected")

# Reader-facing limits. _BRIDGE_TOP_N is coordinated with the projector's bridge LIMIT (kg_context),
# which inlines the same magnitude in SQL with no shared constant — keep in sync with projector bridge LIMIT.
_BRIDGE_TOP_N = 10
_COMMUNITY_PREVIEW = 8        # community-member preview count (the slice AND the "…"-more threshold must match)
_LIST_CAP = 50               # falsification / stale-verdict list caps
_SPAN_TRUNCATE = 80          # span truncation width
_UNATTRIBUTED = "(unattributed)"  # R4 fallback key for an edge with no declared source_file


def _ranked_by(gate_on) -> str:
    """The bridge-highlight ranking signal name for this projection's gate state — the single source of
    truth for the gate-aware legend label shared by the HTML and the report (mirrors kg_context's switch
    in projector.py; that cross-file twin is a deliberate engine/render seam, intentionally not shared)."""
    return "spec_betweenness" if gate_on else "structural_bridge"


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
        return {n["id"] for n in ranked[:_BRIDGE_TOP_N]}
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
            "ranked_by": _ranked_by(gate_on)}


# --------------------------------------------------------------------------- artifacts


def build_html(engine) -> Path:
    """Render the self-contained offline ``graph.html`` (data inlined, no network, no ``<script src>``)
    and atomically write it under the derived dir. Returns its path. Read-only on the projector files."""
    data = _render_data(_load_render_model(engine))
    # Inline the data as JSON inside a <script> block. Escape EVERY markup-significant character, not just
    # "</" — a "</"-only escape is defeated by the script-data-double-escape state: a label like
    # "<!--<script>" enters that state and swallows the template's real </script>, breaking the render and
    # opening a script-injection vector from untrusted source content (review-H1). The \uXXXX forms are a
    # no-op for the parsed JS value (they round-trip to the same object) but leave no literal </>& for the
    # HTML tokenizer, defeating both </script> and <!--<script>.
    payload = (json.dumps(data, sort_keys=True)
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    html = HTML_TEMPLATE.replace("__KG_DATA_JSON__", payload)
    out = engine.projector.derived / "graph.html"
    _atomic_write(out, html)
    return out


def _axis_breakdown(items: list, key: str) -> dict:
    out: dict = {}
    for it in items:
        out[it.get(key)] = out.get(it.get(key), 0) + 1
    return out


def _summary_lines(metrics: dict) -> list:
    """The header + ``## Summary`` section: headline node/edge counts and the epistemic_state breakdown,
    sourced from ``kg_metrics`` so they cannot drift from the canon."""
    lines: list = []
    lines.append("# Knowledge graph report")
    lines.append("")
    lines.append("> Disposable artifact regenerated by `kg_export` / `/kg-view`. A **read-only** render of the "
                 "derived layer — nothing here is authoritative; the human-editable canon is. Failed/rejected "
                 "edges are kept (§1.7, never pruned).")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- **Nodes:** {metrics.get('nodes', 0)}")
    lines.append(f"- **Edges:** {metrics.get('edges', 0)}")
    by_state = metrics.get("edges_by_epistemic_state", {}) or {}
    if by_state:
        lines.append("- **Edges by epistemic_state:** "
                     + ", ".join(f"{k} {v}" for k, v in sorted(by_state.items())))
    return lines


def _legend_lines(ranked_by: str, gate_on: int) -> list:
    """The ``## The three axes`` legend, mirroring the HTML channels (epistemic_state/authored_by/
    provenance) plus the gate-aware bridge-highlight signal name."""
    lines: list = []
    lines.append("")
    lines.append("## The three axes (legend, mirrors the HTML)")
    lines.append("- **epistemic_state** → edge line: solid green = grounded · dashed = unverified · "
                 "**red = failed/rejected** · dotted = hypothesized.")
    lines.append("- **authored_by** → node border: deterministic · agent · human.")
    lines.append("- **provenance** → node fill opacity: span-present (opaque) · inferred (mid) · hypothesized (faint).")
    lines.append(f"- **Node size = degree** (the honest advisory). **Bridge highlight = {ranked_by}** "
                 f"(gate {'ON' if gate_on else 'off'}).")
    return lines


def _community_lines(nodes: list, edges: list) -> list:
    """The ``## Communities`` per-community breakdown by the three axes, with an intra-community edge map."""
    lines: list = []
    lines.append("")
    lines.append("## Communities")
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
        lines.append("_(no nodes)_")
    for c in sorted(comms, key=lambda x: (x is None, x)):
        members = comms[c]
        prov = _axis_breakdown(members, "provenance")
        auth = _axis_breakdown(members, "authored_by")
        est = _axis_breakdown(edges_by_comm.get(c, []), "epistemic_state")
        names = ", ".join(f"`{_escape_md(m.get('label') or m['id'])}`"
                          for m in sorted(members, key=lambda m: m["id"])[:_COMMUNITY_PREVIEW])
        more = " …" if len(members) > _COMMUNITY_PREVIEW else ""
        lines.append(f"### Community {c} — {len(members)} node(s)")
        lines.append(f"- members: {names}{more}")
        lines.append(f"- provenance: {_fmt_counts(prov)}")
        lines.append(f"- authored_by: {_fmt_counts(auth)}")
        lines.append(f"- intra-community edges by epistemic_state: {_fmt_counts(est) or '—'}")
    return lines


def _falsification_lines(edges: list) -> list:
    """The ``## Falsification memory`` section: failed/rejected edges, kept forever (§1.7, never pruned)."""
    fails = [e for e in edges if e.get("epistemic_state") in _FAILURE]
    lines: list = []
    lines.append("")
    lines.append(f"## Falsification memory (§1.7 — never pruned): {len(fails)}")
    if fails:
        for e in sorted(fails, key=lambda e: e.get("id") or "")[:_LIST_CAP]:
            lines.append(f"- `{_escape_md(e.get('source'))} --{_escape_md(e.get('relation'))}--> "
                         f"{_escape_md(e.get('target'))}` "
                         f"[{e.get('epistemic_state')}]" + (f" — `{_truncate(e.get('span'))}`" if e.get("span") else ""))
    else:
        lines.append("_(none — nothing refuted yet)_")
    return lines


def _stale_lines(stale: list) -> list:
    """The ``## Stale verdicts`` section (R3 — span no longer in source)."""
    lines: list = []
    lines.append("")
    lines.append(f"## Stale verdicts (R3 — span no longer in source): {len(stale)}")
    if stale:
        for s in stale[:_LIST_CAP]:
            lines.append(f"- `{_escape_md(s.get('edge_id'))}` — `{_escape_md(s.get('reason'))}`")
    else:
        lines.append("_(none)_")
    return lines


def _source_file_lines(edges: list) -> list:
    """The ``## Source files`` section (R4 — edges per declared source_file)."""
    by_file = Counter(e.get("source_file") or _UNATTRIBUTED for e in edges)
    lines: list = []
    lines.append("")
    lines.append("## Source files (R4 — edges per declared source)")
    for f, n in sorted(by_file.items()):
        # escape at render time (the Counter is keyed on the raw source_file so distinct files stay
        # distinct); source_file is agent-controlled free text — the one untrusted field that had skipped
        # _escape_md, so a backtick/markup-bearing value could break the code span (review-low).
        lines.append(f"- `{_escape_md(f)}`: {n} edge(s)")
    lines.append("")
    return lines


def _report_md(metrics: dict, nodes: list, edges: list, stale: list, gate_on: int) -> str:
    """The GRAPH_REPORT.md body. Headline counts come from ``kg_metrics`` (so they cannot drift from the
    canon); the per-community axis breakdowns, falsification list, R3 stale verdicts and R4 per-file edge
    counts come from the derived rows. Pure string build — a short concatenation of per-section helpers,
    each testable in isolation."""
    lines: list = []
    lines += _summary_lines(metrics)
    lines += _legend_lines(_ranked_by(gate_on), gate_on)
    lines += _community_lines(nodes, edges)
    lines += _falsification_lines(edges)
    lines += _stale_lines(stale)
    lines += _source_file_lines(edges)
    return "\n".join(lines)


def _fmt_counts(d: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(d.items(), key=lambda kv: str(kv[0]))) if d else ""


def _escape_md(s) -> str:
    """Neutralize the characters that let an untrusted label/relation/span/reason escape its rendering
    context in GRAPH_REPORT.md: the backtick (which would break out of a code span), the HTML angle
    brackets ``<``/``>`` (raw-HTML injection when the .md is rendered as HTML), and newlines. Uses
    visually-close lookalikes so the text stays legible (review-low).

    NOTE: this does NOT neutralize markdown link/emphasis metacharacters (``[ ] ( ) * _``), which are
    extremely common in legitimate prose and must stay intact. Those are only *active* outside a code
    span, so EVERY caller renders untrusted values INSIDE a backtick code span (where markdown is
    inert); the escaped backtick above is what guarantees the value cannot break back out. Do not
    inline an ``_escape_md`` result as bare markdown text."""
    return (str(s if s is not None else "")
            .replace("`", "ʼ").replace("<", "‹").replace(">", "›").replace("\n", " ").strip())


def _truncate(s: str, n: int = _SPAN_TRUNCATE) -> str:
    s = _escape_md(s)
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
