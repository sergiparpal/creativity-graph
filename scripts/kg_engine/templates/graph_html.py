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
    <li><span class="sw" style="border-top-color:#2e7d32"></span>grounded (solid)</li>
    <li><span class="sw" style="border-top-color:#9e9e9e;border-top-style:dashed"></span>unverified (dashed)</li>
    <li><span class="sw" style="border-top-color:#c62828"></span>failed / rejected (red — drawn, never pruned)</li>
    <li><span class="sw" style="border-top-color:#1565c0;border-top-style:dotted"></span>hypothesized (dotted)</li>
  </ul>
  <div class="ax">authored_by — node border</div>
  <ul>
    <li><span class="dot" style="border-color:#111"></span>deterministic</li>
    <li><span class="dot" style="border-color:#777"></span>agent</li>
    <li><span class="dot" style="border-color:#7b1fa2"></span>human</li>
  </ul>
  <div class="ax">provenance — node fill opacity</div>
  <ul>
    <li><span class="dot" style="background:#00897b;border-color:#777"></span>span-present (opaque)</li>
    <li><span class="dot" style="background:#00897b;border-color:#777;opacity:0.55"></span>inferred (mid)</li>
    <li><span class="dot" style="background:#00897b;border-color:#777;opacity:0.25"></span>hypothesized (faint)</li>
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
  document.getElementById("counts").textContent =
    DATA.nodes.length + " nodes · " + (DATA.links || []).length + " edges";
  document.getElementById("bridgelegend").innerHTML =
    "bridge highlight (gold ring): " + (DATA.ranked_by || "structural_bridge") +
    " [gate " + (DATA.gate_on ? "ON" : "off") + "]";
  if (!DATA.nodes.length) { document.getElementById("empty").style.display = "flex"; }

  // seeded RNG (mulberry32) so the layout is reproducible across views (diff-stable artifact)
  function mulberry32(a) { return function () {
    a |= 0; a = a + 0x6D2B79F5 | 0;
    var t = Math.imul(a ^ a >>> 15, 1 | a);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  var rng = mulberry32(1337);

  var W = 0, H = 0;
  function resize() { W = canvas.width = window.innerWidth; H = canvas.height = window.innerHeight; }
  resize();
  var R = Math.min(W, H) * 0.4 || 300;
  var nodes = DATA.nodes.map(function (n) {
    return Object.assign({}, n, { x: (rng() - 0.5) * 2 * R + W / 2, y: (rng() - 0.5) * 2 * R + H / 2, vx: 0, vy: 0 }); });
  var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
  var links = []; (DATA.links || []).forEach(function (l) {
    var s = byId[l.source], t = byId[l.target]; if (s && t) links.push(Object.assign({}, l, { s: s, t: t })); });

  var view = { x: 0, y: 0, k: 1 }, alpha = 1;

  // node size = DEGREE ONLY (the honest advisory) — never the bridge metric
  function radius(n) { return 5 + Math.sqrt(n.degree || 0) * 4; }
  var FILL = { "span-present": 1.0, "inferred": 0.55, "hypothesized": 0.25 };
  var BORDER = { "deterministic": "#111", "agent": "#777", "human": "#7b1fa2" };
  function edgeStyle(l) {
    var s = l.epistemic_state, p = l.provenance;
    if (s === "failed" || s === "rejected") return { c: "#c62828", w: 2.2, dash: [] };
    if (s === "grounded") return { c: "#2e7d32", w: 1.6, dash: [] };
    if (p === "hypothesized") return { c: "#1565c0", w: 1.2, dash: [2, 4] };
    return { c: "#9e9e9e", w: 1.2, dash: [6, 4] };  // unverified
  }

  function step() {
    if (alpha < 0.004) return;
    for (var i = 0; i < nodes.length; i++) {
      var a = nodes[i];
      for (var j = i + 1; j < nodes.length; j++) {
        var b = nodes[j], dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
        var f = 2200 / d2, fx = dx / d * f, fy = dy / d * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    links.forEach(function (l) {
      var dx = l.t.x - l.s.x, dy = l.t.y - l.s.y, d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = (d - 95) * 0.02;
      var fx = dx / d * f, fy = dy / d * f; l.s.vx += fx; l.s.vy += fy; l.t.vx -= fx; l.t.vy -= fy;
    });
    nodes.forEach(function (n) {
      n.vx += (W / 2 - n.x) * 0.001; n.vy += (H / 2 - n.y) * 0.001; n.vx *= 0.85; n.vy *= 0.85;
      if (!n.fixed) { n.x += n.vx * alpha; n.y += n.vy * alpha; }
    });
    alpha *= 0.992;
  }

  function draw() {
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, W, H);
    ctx.save(); ctx.translate(view.x, view.y); ctx.scale(view.k, view.k);
    links.forEach(function (l) {
      var st = edgeStyle(l); ctx.strokeStyle = st.c; ctx.lineWidth = st.w; ctx.setLineDash(st.dash);
      ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.t.x, l.t.y); ctx.stroke();
    });
    ctx.setLineDash([]);
    nodes.forEach(function (n) {
      var r = radius(n);
      if (n.bridge) {  // gate-aware bridge highlight (gold ring) — size stays degree-only
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 4, 0, 6.2832);
        ctx.strokeStyle = "#f9a825"; ctx.lineWidth = 2.5; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 6.2832);
      ctx.globalAlpha = FILL[n.provenance] !== undefined ? FILL[n.provenance] : 0.55;
      ctx.fillStyle = "#00897b"; ctx.fill(); ctx.globalAlpha = 1;
      ctx.lineWidth = 2; ctx.strokeStyle = BORDER[n.authored_by] || "#777"; ctx.stroke();
      ctx.fillStyle = "#333"; ctx.font = "11px system-ui, sans-serif";
      ctx.fillText(n.label || n.id, n.x + r + 3, n.y + 3);
    });
    ctx.restore();
  }

  function frame() { for (var i = 0; i < 3; i++) step(); draw(); requestAnimationFrame(frame); }
  frame();

  // --- interactivity: pan (drag background), zoom (wheel), drag node, hover tooltip
  function toWorld(px, py) { return { x: (px - view.x) / view.k, y: (py - view.y) / view.k }; }
  function pick(px, py) {
    var w = toWorld(px, py), best = null, bd = 1e9;
    nodes.forEach(function (n) { var dx = n.x - w.x, dy = n.y - w.y, d = dx * dx + dy * dy, r = radius(n) + 4;
      if (d <= r * r && d < bd) { bd = d; best = n; } });
    return best;
  }
  var dragNode = null, panning = false, last = null;
  canvas.addEventListener("mousedown", function (e) {
    var n = pick(e.clientX, e.clientY);
    if (n) { dragNode = n; n.fixed = true; } else { panning = true; }
    last = { x: e.clientX, y: e.clientY };
  });
  window.addEventListener("mousemove", function (e) {
    if (dragNode) { var w = toWorld(e.clientX, e.clientY); dragNode.x = w.x; dragNode.y = w.y; dragNode.vx = dragNode.vy = 0; alpha = Math.max(alpha, 0.3); }
    else if (panning && last) { view.x += e.clientX - last.x; view.y += e.clientY - last.y; last = { x: e.clientX, y: e.clientY }; }
    else {
      var n = pick(e.clientX, e.clientY);
      if (n) {
        tip.style.display = "block"; tip.style.left = (e.clientX + 12) + "px"; tip.style.top = (e.clientY + 12) + "px";
        tip.innerHTML = "<b>" + esc(n.label || n.id) + "</b><br>provenance: " + esc(n.provenance) +
          "<br>authored_by: " + esc(n.authored_by) + "<br>degree: " + (n.degree || 0) +
          (n.bridge ? "<br><i>bridge (" + esc(DATA.ranked_by || "structural_bridge") + ")</i>" : "");
      } else { tip.style.display = "none"; }
    }
  });
  window.addEventListener("mouseup", function () { if (dragNode) dragNode.fixed = false; dragNode = null; panning = false; last = null; });
  canvas.addEventListener("wheel", function (e) {
    e.preventDefault(); var s = e.deltaY < 0 ? 1.1 : 1 / 1.1, mx = e.clientX, my = e.clientY;
    view.x = mx - (mx - view.x) * s; view.y = my - (my - view.y) * s; view.k *= s;
  }, { passive: false });
  window.addEventListener("resize", function () { resize(); });
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }
})();
</script>
</body>
</html>
"""
