"""The `graph.html` render template (R1) — a self-contained, fully-offline, zero-dependency canvas
force-layout viz, as a Python module constant (no separate `.html` asset, no packaging change).

The whole point is **three axes on independent visual channels** (never one confidence colour):
  - `epistemic_state` → edge line style/colour (solid green=grounded, dashed grey=unverified,
    RED=failed/rejected, dotted blue=hypothesized; failed/rejected edges are DRAWN, never filtered);
  - `authored_by`     → node border colour (deterministic / agent / human);
  - `provenance`      → node fill opacity (span-present opaque / inferred mid / hypothesized faint).
Node **size = degree only** (the honest advisory). The **bridge highlight** is gate-aware (a gold ring;
spec_betweenness only when `gate_on=1`, else the structural-bridge advisory) — node size is never the
bridge metric, so the generality confound is never smuggled into the most prominent visual channel.

`export.build_html` substitutes the inlined data JSON for `__KG_DATA_JSON__`. The JS uses a seeded RNG so
the rendered layout is reproducible; the HTML bytes are deterministic for a given derived state.
"""
from __future__ import annotations

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>creativity-graph — knowledge graph</title>
<style>
  :root { color-scheme: light dark; }
  html, body { margin: 0; height: 100%; font: 13px/1.4 system-ui, sans-serif; background: #fbfbfa; color: #222; }
  #c { position: fixed; inset: 0; display: block; cursor: grab; }
  #c:active { cursor: grabbing; }
  #legend { position: fixed; top: 10px; left: 10px; max-width: 320px; background: rgba(255,255,255,0.94);
            border: 1px solid #ddd; border-radius: 8px; padding: 10px 12px; box-shadow: 0 1px 6px rgba(0,0,0,0.08); }
  #legend h1 { font-size: 14px; margin: 0 0 6px; }
  #legend .muted { color: #777; font-size: 11px; margin: 0 0 8px; }
  #legend .ax { margin: 6px 0 2px; font-weight: 600; }
  #legend ul { margin: 2px 0 6px; padding-left: 16px; }
  #legend li { margin: 1px 0; }
  .sw { display: inline-block; width: 22px; height: 0; vertical-align: middle; margin-right: 6px; border-top-width: 3px; border-top-style: solid; }
  .dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; vertical-align: middle; margin-right: 6px; border: 2px solid #777; }
  #tip { position: fixed; pointer-events: none; background: #222; color: #fff; padding: 6px 8px; border-radius: 6px;
         font-size: 12px; max-width: 320px; display: none; z-index: 5; }
  #empty { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; color: #999; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="tip"></div>
<div id="legend">
  <h1>creativity-graph</h1>
  <p class="muted" id="counts"></p>
  <p class="muted">Three orthogonal axes, never one "quality" colour. Disposable render of the canon.</p>
  <div class="ax">epistemic_state — edge line</div>
  <ul>
    <li><span class="sw" id="sw-grounded"></span>grounded (solid)</li>
    <li><span class="sw" id="sw-unverified" style="border-top-style:dashed"></span>unverified (dashed)</li>
    <li><span class="sw" id="sw-failed"></span>failed / rejected (red — drawn, never pruned)</li>
    <li><span class="sw" id="sw-hypothesized" style="border-top-style:dotted"></span>hypothesized (dotted)</li>
  </ul>
  <div class="ax">authored_by — node border</div>
  <ul>
    <li><span class="dot" id="dot-deterministic"></span>deterministic</li>
    <li><span class="dot" id="dot-agent"></span>agent</li>
    <li><span class="dot" id="dot-human"></span>human</li>
  </ul>
  <div class="ax">provenance — node fill opacity</div>
  <ul>
    <li><span class="dot" id="dot-span-present" style="border-color:#777"></span>span-present (opaque)</li>
    <li><span class="dot" id="dot-inferred" style="border-color:#777;opacity:0.55"></span>inferred (mid)</li>
    <li><span class="dot" id="dot-hypothesized" style="border-color:#777;opacity:0.25"></span>hypothesized (faint)</li>
  </ul>
  <div class="ax">size = degree · <span id="bridgelegend"></span></div>
</div>
<div id="empty" style="display:none">graph is empty — build it with /kg-build</div>
<script>
"use strict";
window.__KG_DATA__ = __KG_DATA_JSON__;
(function () {
  var DATA = window.__KG_DATA__ || { nodes: [], links: [] };
  var canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
  var tip = document.getElementById("tip");

  // single source of truth for the three-axis palette — both the canvas draw and the legend
  // swatches consume it, so the documented encoding can never drift from what is rendered.
  var PALETTE = {
    edge: { grounded: "#2e7d32", unverified: "#9e9e9e", failed: "#c62828", hypothesized: "#1565c0" },
    border: { "deterministic": "#111", "agent": "#777", "human": "#7b1fa2" },
    fill: { "span-present": 1.0, "inferred": 0.55, "hypothesized": 0.25 },
    fillColor: "#00897b"
  };

  document.getElementById("counts").textContent =
    DATA.nodes.length + " nodes · " + (DATA.links || []).length + " edges";
  document.getElementById("bridgelegend").innerHTML =
    "bridge highlight (gold ring): " + (DATA.ranked_by || "structural_bridge") +
    " [gate " + (DATA.gate_on ? "ON" : "off") + "]";
  if (!DATA.nodes.length) { document.getElementById("empty").style.display = "flex"; }

  // paint the legend swatches from PALETTE (same source the canvas draws from) so the legend
  // documents exactly what is on screen — no hand-typed hex to fall out of sync.
  function paintLegend() {
    document.getElementById("sw-grounded").style.borderTopColor = PALETTE.edge.grounded;
    document.getElementById("sw-unverified").style.borderTopColor = PALETTE.edge.unverified;
    document.getElementById("sw-failed").style.borderTopColor = PALETTE.edge.failed;
    document.getElementById("sw-hypothesized").style.borderTopColor = PALETTE.edge.hypothesized;
    document.getElementById("dot-deterministic").style.borderColor = PALETTE.border.deterministic;
    document.getElementById("dot-agent").style.borderColor = PALETTE.border.agent;
    document.getElementById("dot-human").style.borderColor = PALETTE.border.human;
    document.getElementById("dot-span-present").style.background = PALETTE.fillColor;
    document.getElementById("dot-inferred").style.background = PALETTE.fillColor;
    document.getElementById("dot-hypothesized").style.background = PALETTE.fillColor;
  }
  paintLegend();

  // seeded RNG (mulberry32) so the layout is reproducible across views (diff-stable artifact)
  function mulberry32(a) { return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    var t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  var LAYOUT_SEED = 1337;  // load-bearing: fixes the diff-stable layout (do not change the value)
  var rng = mulberry32(LAYOUT_SEED);
  var TAU = 2 * Math.PI;
  // force-simulation tuning + geometry constants (named so each formula's role is legible)
  var SIM = { REPULSION: 2200, SPRING_LEN: 95, SPRING_K: 0.02, GRAVITY: 0.001, DAMPING: 0.85, COOLING: 0.992, ALPHA_FLOOR: 0.004 };
  var RADIUS_BASE = 5, RADIUS_SCALE = 4, BRIDGE_RING_GAP = 4, BRIDGE_RING_WIDTH = 2.5;

  var W = 0, H = 0;
  function resize() { W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight; }
  resize();
  var R = Math.min(W, H) * 0.4 || 300;
  var nodes = DATA.nodes.map(function (n) {
    return Object.assign({}, n, { x: (rng() - 0.5) * 2 * R + W / 2, y: (rng() - 0.5) * 2 * R + H / 2, vx: 0, vy: 0 }); });
  var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
  // Resilience: an edge endpoint with no node row (a dangling target) is synthesized as a minimal
  // placeholder here too, so the edge is DRAWN rather than silently dropped and links.length matches
  // the legend's edge count. (export._render_data does this data-side; this is defense-in-depth.)
  function ensureNode(id) {
    if (id == null) return null;
    var n = byId[id];
    if (!n) {
      n = { id: id, label: id, degree: 0, provenance: null, authored_by: null, community: null,
            bridge: false, x: (rng() - 0.5) * 2 * R + W / 2, y: (rng() - 0.5) * 2 * R + H / 2, vx: 0, vy: 0 };
      byId[id] = n; nodes.push(n);
    }
    return n;
  }
  var links = []; (DATA.links || []).forEach(function (l) {
    var s = ensureNode(l.source), t = ensureNode(l.target); if (s && t) links.push(Object.assign({}, l, { s: s, t: t })); });

  var view = { x: 0, y: 0, k: 1 }, alpha = 1;

  // node size = DEGREE ONLY (the honest advisory) — never the bridge metric
  function radius(n) { return RADIUS_BASE + Math.sqrt(n.degree || 0) * RADIUS_SCALE; }
  var UNVERIFIED = { color: PALETTE.edge.unverified, width: 1.2, dash: [6, 4] };
  function edgeStyle(l) {
    var state = l.epistemic_state, prov = l.provenance;
    if (state === "failed" || state === "rejected") return { color: PALETTE.edge.failed, width: 2.2, dash: [] };
    if (state === "grounded") return { color: PALETTE.edge.grounded, width: 1.6, dash: [] };
    if (prov === "hypothesized") return { color: PALETTE.edge.hypothesized, width: 1.2, dash: [2, 4] };
    return UNVERIFIED;
  }

  function step() {
    if (alpha < SIM.ALPHA_FLOOR) return;
    for (var i = 0; i < nodes.length; i++) {
      var a = nodes[i];
      for (var j = i + 1; j < nodes.length; j++) {
        var b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
        var f = SIM.REPULSION / d2, fx = dx / d * f, fy = dy / d * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    links.forEach(function (l) {
      var dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = (d - SIM.SPRING_LEN) * SIM.SPRING_K;
      var fx = dx / d * f, fy = dy / d * f; l.s.vx += fx; l.s.vy += fy; l.t.vx -= fx; l.t.vy -= fy;
    });
    nodes.forEach(function (n) {
      n.vx += (W / 2 - n.x) * SIM.GRAVITY; n.vy += (H / 2 - n.y) * SIM.GRAVITY; n.vx *= SIM.DAMPING; n.vy *= SIM.DAMPING;
      if (!n.fixed) { n.x += n.vx * alpha; n.y += n.vy * alpha; }
    });
    alpha *= SIM.COOLING;
  }

  function draw() {
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H);
    ctx.save(); ctx.translate(view.x, view.y); ctx.scale(view.k, view.k);
    links.forEach(function (l) {
      var style = edgeStyle(l); ctx.strokeStyle = style.color; ctx.lineWidth = style.width; ctx.setLineDash(style.dash);
      ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
    });
    ctx.setLineDash([]);
    nodes.forEach(function (n) {
      var r = radius(n);
      if (n.bridge) {  // gate-aware bridge highlight (gold ring) — size stays degree-only
        ctx.beginPath(); ctx.arc(n.x, n.y, r + BRIDGE_RING_GAP, 0, TAU);
        ctx.strokeStyle = "#f9a825"; ctx.lineWidth = BRIDGE_RING_WIDTH; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, TAU);
      ctx.globalAlpha = PALETTE.fill[n.provenance] !== undefined ? PALETTE.fill[n.provenance] : 0.55;
      ctx.fillStyle = PALETTE.fillColor; ctx.fill(); ctx.globalAlpha = 1;
      ctx.lineWidth = 2; ctx.strokeStyle = PALETTE.border[n.authored_by] || "#777"; ctx.stroke();
      ctx.fillStyle = "#333"; ctx.font = "11px system-ui, sans-serif";
      ctx.fillText(n.label || n.id, n.x + r + 3, n.y + 3);
    });
    ctx.restore();
  }

  // rAF loop that PARKS once the layout has cooled (alpha < ALPHA_FLOOR): step() already early-returns
  // there, so without parking draw() would repaint an identical static frame ~60x/s forever, pinning a
  // CPU/GPU core on an idle offline tab. We run one final draw() at the floor, then stop scheduling; the
  // interaction handlers (which bump alpha or change view) call kick() to resume.
  var running = false;
  function frame() {
    for (var i = 0; i < 3; i++) step();
    draw();
    if (alpha >= SIM.ALPHA_FLOOR) { requestAnimationFrame(frame); } else { running = false; }
  }
  function kick() { if (!running) { running = true; requestAnimationFrame(frame); } }
  kick();

  // --- interactivity: pan (drag background), zoom (wheel), drag node, hover tooltip
  function toWorld(px, py) { return { x: (px - view.x) / view.k, y: (py - view.y) / view.k }; }
  function pick(px, py) {
    var w = toWorld(px, py), best = null, bestDist = 1e9;
    nodes.forEach(function (n) { var dx = n.x - w.x, dy = n.y - w.y, d = dx * dx + dy * dy, r = radius(n) + 4;
      if (d <= r * r && d < bestDist) { bestDist = d; best = n; } });
    return best;
  }
  // hover tooltip helpers — keep the interaction state machine free of presentation logic
  function showTooltip(node, clientX, clientY) {
    tip.style.display = "block"; tip.style.left = (clientX + 12) + "px"; tip.style.top = (clientY + 12) + "px";
    tip.innerHTML = "<b>" + esc(node.label || node.id) + "</b><br>provenance: " + esc(node.provenance) +
      "<br>authored_by: " + esc(node.authored_by) + "<br>degree: " + (node.degree || 0) +
      (node.bridge ? "<br><i>bridge (" + esc(DATA.ranked_by || "structural_bridge") + ")</i>" : "");
  }
  function hideTooltip() { tip.style.display = "none"; }
  var dragNode = null, panning = false, last = null;
  canvas.addEventListener("mousedown", function (e) {
    var n = pick(e.clientX, e.clientY);
    if (n) { dragNode = n; n.fixed = true; } else { panning = true; }
    last = { x: e.clientX, y: e.clientY };
  });
  window.addEventListener("mousemove", function (e) {
    if (dragNode) { var w = toWorld(e.clientX, e.clientY); dragNode.x = w.x; dragNode.y = w.y; dragNode.vx = dragNode.vy = 0; alpha = Math.max(alpha, 0.3); kick(); }
    else if (panning && last) { view.x += e.clientX - last.x; view.y += e.clientY - last.y; last = { x: e.clientX, y: e.clientY }; kick(); }
    else {
      var n = pick(e.clientX, e.clientY);
      if (n) { showTooltip(n, e.clientX, e.clientY); } else { hideTooltip(); }
    }
  });
  window.addEventListener("mouseup", function () { if (dragNode) dragNode.fixed = false; dragNode = null; panning = false; last = null; });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault(); var scale = e.deltaY < 0 ? 1.1 : 1 / 1.1, mx = e.clientX, my = e.clientY;
    view.x = mx - (mx - view.x) * scale; view.y = my - (my - view.y) * scale; view.k *= scale; kick();
  }, { passive: false });
  window.addEventListener("resize", function () { resize(); kick(); });
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }
})();
</script>
</body>
</html>
"""
