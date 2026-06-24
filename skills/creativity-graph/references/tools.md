# Reference: tools & CLIs

Load on demand. The MCP **tool surface** (graph mutation + read), then the deterministic **CLI surface**
(`f4_probe.py`, `kg_engine.pack`, `kg_engine.harness`). Every name, signature, and return shape below
mirrors `scripts/kg_engine/server.py` + `scripts/kg_engine/projector.py`. Nothing here is invented — if a
field is missing, grep the engine source, don't guess.

---

## 1 · MCP tool surface

A plugin-bundled MCP server's tools are namespaced `mcp__plugin_<plugin>_<server>__<tool>` — here both the
plugin and the server are named `creativity-graph`, so every tool is `mcp__plugin_creativity-graph_creativity-graph__<tool>`
(use this exact form in agent `tools:` / command `allowed-tools:` grants). These **sixteen** are the **only**
graph tools — the eleven verify/read tools (§1.1–§1.11) plus the four generative-layer tools (§1.12–§1.15)
plus the read-only `kg_agenda` (§1.16). There is no `kg_build` / `kg_query` / `kg_project` MCP tool — those
are slash commands (`/kg-build`, …) that *orchestrate* these tools.

Mutation tools (`kg_write`, `kg_propose`, `kg_ground`, `kg_rename`) write the **canon** (human-editable Markdown,
the single source of truth) — `kg_propose` (§1.12) is the hypothesized write lane and `kg_operate` (§1.14) writes
through it. Read tools (`get_node`, `get_neighbors`, `shortest_path`, `query_graph`, `kg_context`) and the
generative reads (`kg_generate` §1.13, `kg_absorption` §1.15) read the **derived** layer; they call
`_ensure_projected()` first, which reprojects only if `index.sqlite`/`graph.json` is missing or
`projector.is_stale()` — a content-driven check (a cheap per-note `(name, size, mtime)` signature pre-gate,
then an authoritative per-node content-hash comparison), regardless of git HEAD, so an uncommitted edit
still reprojects. The derived layer contains nothing the canon does not (§1.2) and never prunes failure
memory (§1.7).

### 1.1 `mcp__plugin_creativity-graph_creativity-graph__kg_ping()`

Health check / config probe. No args.

```json
{"name": "creativity-graph", "version": "<__version__>", "metrics_mode": "structure_only",
 "sensitivity": "medium", "pack_loaded": true}
```

`pack_loaded` is `true` only when `pack/pack.yaml` validated as a `PackContract` at startup. `metrics_mode`
is `structure_only` by default (centrality stays advisory; the specificity-weighted bridge metric is gated,
§1.4/§1.6).

### 1.2 `mcp__plugin_creativity-graph_creativity-graph__kg_scrub(text=None)`

The §1.9 **egress scrub**. Redacts **secrets (always)** + **PII (per `sensitivity`)** with **CONSISTENT
placeholders** (`⟦SECRET:1⟧`, `⟦EMAIL:1⟧`, …) before any text is handed to a subagent for semantic work.
Pass `text` to scrub a snippet, or omit to scrub the configured source. It accumulates the session
placeholder→original mapping so that `kg_write` then **RESTORES** placeholder spans to the **ORIGINAL** text
for the canon — the boundary stores the restored original span, so the scrub protects the egress, not the
local canon.

```json
{"scrubbed": "<text with placeholders>", "redactions": 0, "sensitivity": "medium", "categories": []}
```

- `scrubbed` — the text the subagent should see (original where nothing matched).
- `redactions` — count of distinct placeholders introduced.
- `sensitivity` — the engine's configured sensitivity (`kg_ping().sensitivity`); gates which PII categories
  are redacted (secrets are always redacted).
- `categories` — sorted distinct redaction categories present (e.g. `["EMAIL", "SECRET"]`), `[]` when none.

For the no-PII demo source (`examples/source.md`), `kg_scrub` is a **no-op**: `redactions: 0`,
`categories: []`, and `scrubbed` equals the source verbatim.

### 1.3 `mcp__plugin_creativity-graph_creativity-graph__kg_write(payload: dict)`

The boundary (§1.5). Validates an extraction payload, writes ACCEPTED/DEMOTED nodes & edges to the canon,
quarantines or rejects the rest. `payload` is the write contract (see `references/contract.md` / the shared
contract): `{nodes:[…], edges:[…], complete:true}`. **`complete` MUST be `true`** or the whole payload is
REJECTED as `truncated-payload`.

```json
{
  "dispositions": {"ACCEPTED": 3, "DEMOTED": 1, "QUARANTINED": 0, "REJECTED": 2},
  "details": [
    {"kind": "edge", "id": "e_generality-confound__attacked-by__specificity",
     "disposition": "ACCEPTED", "reason": "", "retryable": false},
    {"kind": "edge", "id": "e_x__grounds__y",
     "disposition": "REJECTED", "reason": "span-not-in-source", "retryable": false}
  ],
  "written_nodes": ["generality-confound", "specificity", "compression"],
  "rolled_back": false,
  "error": null
}
```

- `dispositions` — counts keyed by every `Disposition` value: `ACCEPTED | DEMOTED | QUARANTINED | REJECTED`.
- `details[]` — one per validated item: `kind` (`node`|`edge`), `id` (the derived edge id
  `e_{source}__{relation}__{target}`, or `null`), `disposition`, `reason` (e.g. `no-supporting-span`,
  `span-not-in-source`, `span-not-in-named-source` (R4: span present in the corpus but not in the edge's
  named `source_file`), `truncated-payload`, `schema-invalid`, `forged-verdict-stripped`,
  `human-claim-stripped`, `undeclared-node-type`, `undeclared-edge-type`), `retryable` (**`false`** for SEMANTIC rejections — no-span,
  span-not-in-source; **`true`** for TRANSPORT — truncation, schema-invalid).
- `written_nodes[]` — node ids actually committed (includes boundary-auto-created placeholder source nodes).
- `rolled_back` / `error` — `rolled_back` is `true` (and `error` carries the failure message) when the multi-file canon write could not commit and was rolled back.

A write may never set a non-`unverified` state or claim parser/human authorship: such payloads are
**DEMOTED** — any verdict or `obsolete` is reset to `unverified` (`forged-verdict-stripped`); `human` →
`agent` (`human-claim-stripped`); `deterministic` → `agent` (`deterministic-claim-stripped`, so an
extractor can't dodge span-present by self-declaring parser authorship). None are accepted as-is.

### 1.4 `mcp__plugin_creativity-graph_creativity-graph__kg_ground(target_id, verdict, kind="edge", note="", support_span="", support_note="")`

**The ONLY path that may set a verdict** (§1.4/§1.8). Stamps the epistemic_state and appends a `ground.audit`
record so the reconciler treats the transition as legitimate.

- `target_id: str` — an edge id (default `kind="edge"`) or node id (`kind="node"`).
- `verdict: str` — one of `VALID_VERDICTS = {grounded, rejected, failed, obsolete}` (lower-cased internally).
- `kind: str = "edge"` — `edge` or `node`.
- `note: str = ""` — appended to the edge's `notes` (e.g. the rejection reason `vague` for a generality-confound
  edge that is "true" only because it is generic/unfalsifiable, §1.6).
- `support_span: str = ""` / `support_note: str = ""` — **promotion support** (Stage 8). To move a
  `hypothesized` edge to `grounded` you MUST supply one, and it **upgrades the edge's provenance**:
  `support_span` (a verbatim source substring, span-verified) → `span-present`; `support_note` (an external
  citation, no span) → `inferred`. Ignored for non-hypothesized edges and for any verdict other than `grounded`.

```json
{"ok": true, "key": "e_generality-confound__attacked-by__specificity",
 "from": "unverified", "to": "grounded", "by": "agent"}
```

A promoted hypothesis adds `"provenance_upgraded_to": "span-present" | "inferred"` to the success return.
On failure: `{"ok": false, "error": "invalid verdict 'maybe'"}` / `"node not found"` / `"edge not found"`.
Promotion-specific refusals: `hypothesis-needs-support` (grounding a `hypothesized` edge with neither
`support_span` nor `support_note`), `support-span-not-in-source`, `support-span-too-short`.
For an edge, also sets `verdict_by` (always `agent` via this tool — a human verdict cannot be forged
through the tool surface) and `verdict_at`. Note: the return `key` for a node verdict is `node:<id>`;
for an edge it is the edge id.

> Adversarial grounding (§1.7): the adversarial grounder adds `attacked_by` edges then calls
> `kg_ground(target_id=<edge>, verdict="failed")`. Failed/rejected edges are NEGATIVE INFORMATION — never
> pruned, surfaced by `kg_context.falsification_counters`.

### 1.5 `mcp__plugin_creativity-graph_creativity-graph__kg_rename(old_id, new_id)`

Renames a node and rewrites every edge endpoint (`source`/`target`) referencing it, preserving the
single-canonical-edge rule. Both ids are slugged.

```json
{"ok": true, "old": "betweeness", "new": "betweenness",
 "touched": ["betweenness", "generality-confound", "specificity"]}
```

Failure: `{"ok": false, "error": "node not found"}` or `"target id exists"`. `ok` is `false` (with `error: "rename rolled back: …"`) if the multi-file write had to roll back.

### 1.6 `mcp__plugin_creativity-graph_creativity-graph__kg_metrics()`

Cheap summary counts straight off the canon (no projection). No args.

```json
{"nodes": 24, "edges": 41, "edges_by_epistemic_state": {"unverified": 30, "grounded": 7, "failed": 4}}
```

`edges_by_epistemic_state` keys are whatever `EpistemicState` values are present
(`unverified|grounded|rejected|failed|obsolete`).

### 1.7 `mcp__plugin_creativity-graph_creativity-graph__query_graph(node_type=None, relation=None, epistemic_state=None, limit=50)`

Filtered read of the derived index. Nodes filtered by `node_type` and/or `epistemic_state`, **ordered by
precomputed `degree` DESC** (the honest MVP advisory, §1.6), capped at `limit`. Edges filtered by `relation`,
capped at `limit`. All filters optional.

```json
{
  "nodes": [
    {"id": "compression", "label": "Compression", "node_type": "compression", "file_type": "prose",
     "provenance": "span-present", "authored_by": "agent", "epistemic_state": "unverified",
     "degree": 6, "community": 0, "bridge_communities": 2, "structural_bridge": 1}
  ],
  "edges": [
    {"id": "e_generality-confound__attacked-by__specificity", "source": "generality-confound",
     "target": "specificity", "relation": "attacked_by", "provenance": "span-present",
     "authored_by": "agent", "epistemic_state": "unverified",
     "span": "a more specific claim, when it holds, defeats a vaguer one", "source_file": "source.md",
     "confidence": "INFERRED", "confidence_score": 0.6}
  ]
}
```

Node rows carry precomputed rank columns: `degree`, `community` (Leiden membership, `-1` if none),
`bridge_communities` (count of distinct communities among neighbours), `structural_bridge` (`1` iff
`bridge_communities >= 2`). Because the read does `SELECT *`, rows also carry the Stage-2 generative
columns — `betweenness`, the confound-corrected `spec_betweenness`, per-node `specificity`, and `gate_on`
— trusted as a ranking signal only when the specificity gate is ON (§1.6); until then they are advisory.
Valid `node_type` filters are the pack's declared types
(`compression|primitive|claim|metric|operation|failure`); `relation` filters the declared edge types
(`grounds|attacked_by|reconciles_with|bridges|collapses_into|confounded_by|approximates|defends_against|projects|survives`).

### 1.8 `mcp__plugin_creativity-graph_creativity-graph__get_node(node_id)`

One node row + its incident edges (both `source=` and `target=` matches).

```json
{
  "id": "specificity", "label": "Specificity", "node_type": "compression", "file_type": "prose",
  "provenance": "span-present", "authored_by": "agent", "epistemic_state": "unverified",
  "degree": 4, "community": 0, "bridge_communities": 1, "structural_bridge": 0,
  "edges": [
    {"id": "e_generality-confound__attacked-by__specificity", "source": "generality-confound",
     "target": "specificity", "relation": "attacked_by", "provenance": "span-present",
     "authored_by": "agent", "epistemic_state": "unverified", "span": "...", "source_file": "source.md",
     "confidence": "INFERRED", "confidence_score": 0.6}
  ]
}
```

Returns `{"error": "not found"}` when the id is unknown.

### 1.9 `mcp__plugin_creativity-graph_creativity-graph__get_neighbors(node_id, relation=None)`

A **list** (not a dict) of edge dicts incident to `node_id` (as `source` OR `target`), optionally filtered by
`relation`. Each element has the same shape as an edge row above. Empty list if the node has no incident edges.

### 1.10 `mcp__plugin_creativity-graph_creativity-graph__shortest_path(source, target)`

BFS over the derived edge list, treated as **undirected** (no centrality is computed).

```json
{"path": ["generality-confound", "specificity", "betweenness"]}
```

`{"path": ["x"]}` when `source == target`; `{"path": null}` when no path exists.

### 1.11 `mcp__plugin_creativity-graph_creativity-graph__kg_context(query=None, budget=2000)`

The **grounding-aware, provenance-carrying, token-budgeted** context tool — the one to call before reasoning
over the graph. Reads precomputed ranks **O(1)**; it **NEVER computes centrality in-request** (centrality is
precomputed off the hot path by the projector). `query` (optional) does a `LIKE` filter over edge
`source|target|relation|span`. `budget` (default `2000`) caps approximate tokens (`len(json)//4` per item).

Priority fill order (best context first, until the budget is spent): **grounded edges first**, then
`span-present` provenance, then `inferred`, then by `confidence_score` DESC. The grounded `items[]` and the
hypothesized `hypotheses[]` lanes share **one** running budget (§1.11): hypotheses fill only what the items
lane left, and `approx_tokens` reports the true total across both.

```json
{
  "items": [
    {"id": "e_compression__grounds__claim", "source": "compression", "target": "claim",
     "relation": "grounds", "provenance": "span-present", "authored_by": "agent",
     "epistemic_state": "grounded", "span": "...", "confidence": "INFERRED", "confidence_score": 0.82}
  ],
  "hypotheses": [
    {"id": "e_entropy__bridges__time", "source": "entropy", "target": "time", "relation": "bridges",
     "provenance": "hypothesized", "authored_by": "deterministic", "epistemic_state": "unverified",
     "span": "", "confidence": "AMBIGUOUS", "confidence_score": 0.5}
  ],
  "approx_tokens": 1840,
  "budget": 2000,
  "falsification_counters": {"failed_or_rejected_edges": 4},
  "advisory": {
    "signal": "structural-bridge",
    "note": "advisory heuristic, not a guarantee",
    "nodes": [
      {"id": "compression", "label": "Compression", "degree": 6, "bridge_communities": 2}
    ],
    "bridge_metric": {
      "gate_on": 0,
      "ranked_by": "structural_bridge",
      "note": "gated: spec_betweenness stays advisory; ranking by structural-bridge/degree (§1.6)",
      "nodes": [
        {"id": "compression", "label": "Compression", "degree": 6, "betweenness": 0.21,
         "spec_betweenness": 0.46, "specificity": 2.0}
      ]
    }
  }
}
```

- `items[]` — budget-trimmed **grounded/text-claim** edge records (note: `source_file` is omitted from context
  items, unlike `query_graph`/`get_node` edge rows).
- `hypotheses[]` — the **SEPARATE** hypothesized lane (Stage 8 query segregation): machine proposals from
  `/kg-generate`, `provenance=hypothesized`, never mixed into the grounded `items[]`. A hypothesis becomes a
  fact only after `kg_ground` promotes it with support (§1.4).
- `approx_tokens` — tokens actually used across **both** lanes (`<= budget`).
- `falsification_counters.failed_or_rejected_edges` — count of edges in `FAILURE_STATES`
  (`rejected` + `failed`). **Memory of failures (§1.7): surfaced here, never pruned.** A non-zero counter is a
  signal that the graph already knows what was refuted; don't re-propose it.
- `advisory` — the **labelled structural-bridge** signal: `signal:"structural-bridge"`, an explicit
  `note:"advisory heuristic, not a guarantee"`, and up to 10 `nodes` with `structural_bridge=1` ordered by
  `degree` DESC. Treat as a hint, not a metric — the specificity-weighted bridge metric is GATED until the
  harness validates it (§1.4/§1.6). A structural bridge that is vague is the generality confound, not a real
  bridge.
- `advisory.bridge_metric` — the completed bridge metric (Stage 2): `gate_on` (`0`/`1`), `ranked_by`
  (`spec_betweenness` when the gate is ON, else `structural_bridge`), a `note`, and up to 10 `nodes` carrying
  **both** `betweenness` and the confound-corrected `spec_betweenness` so a reader sees the correction. Until
  the harness turns the gate on (`gate_on:1`), the trusted ranking stays the structural-bridge/degree advisory.

---

## 1A · The generative layer (§2–§14)

The four tools below are the *offensive* half (the inversion: **generate offensively, judge defensively**).
`kg_generate`/`kg_absorption` are read-only structural reads; `kg_propose`/`kg_operate` write through the
**hypothesized** lane only — they can never set a verdict or forge a text anchor. A candidate becomes grounded
knowledge solely when `kg_ground` (§1.4) promotes it with support.

### 1.12 `mcp__plugin_creativity-graph_creativity-graph__kg_propose(payload)`

The **hypothesized write lane** (PLAN Stage 1). A thin, explicit alias over `kg_write` that forces every item
to `provenance=hypothesized` and **REFUSES** any item arriving with a text-claim provenance
(`span-present`/`inferred`) with reason `propose-lane-text-claim` — text claims belong on `kg_write`. Accepted
items transit the SAME `validate_payload`, so the hypothesized-lane rules apply (no span required; forged
verdicts demoted; failure-collapse `QUARANTINED/collapses-into-known-failure`; pack vocabulary enforced;
`authored_by=deterministic` **preserved** here, `human` demoted to `agent`).

Returns the `kg_write` shape plus two fields:

```json
{"dispositions": {"ACCEPTED": 2, "DEMOTED": 0, "QUARANTINED": 1, "REJECTED": 1},
 "details": [ … ], "written_nodes": [ … ], "rolled_back": false, "error": null,
 "propose_lane": true, "refused_text_claims": 1}
```

`refused_text_claims` counts the call-site `propose-lane-text-claim` refusals (folded into `details[]` and the
`REJECTED` count).

### 1.13 `mcp__plugin_creativity-graph_creativity-graph__kg_generate(mechanism="bridge", k=10, second_graph=None)`

The **discovery engine** (PLAN Stage 3). **READ-ONLY** — projects if stale, reads precomputed ranks O(1),
dispatches to the chosen mechanism, and returns ranked candidates. It never writes; `/kg-generate` routes the
candidates through `kg_propose`.

- `mechanism` — `bridge` (§2/§4) | `seed` (§3 residual `c − E[c|d]`) | `compression` (§7 dense-cluster MDL) |
  `regroup` (§8 re-partition bridges) | `transplant` (§5 hub pattern) | `ensemble` (§9 cross two
  constructions), or `all`/`default`.
- `k: int = 10` — max candidates returned (ranked).
- `second_graph: str | None` — path to a second construction's `graph.json` for `ensemble`; without one,
  `ensemble`/`all` **degrades to `regroup`** and says so in `note` (run `/kg-perturb` to supply one).

```json
{"mechanism": "bridge", "k": 10, "gate_on": 0, "count": 2, "note": "",
 "candidates": [
   {"kind": "edge", "mechanism": "bridge", "source": "entropy", "target": "time", "relation": "bridges",
    "label": "", "node_type": "", "score": 0.81, "specificity": 2.1,
    "rationale": "cross-community pair, generality-controlled", "section": "§4"}
 ]}
```

Each candidate is a `Candidate` dict: `{kind, mechanism, source, target, relation, label, node_type, score,
specificity, rationale, section}` (`provenance` is always `hypothesized`, never carried — the propose lane
forces it).

### 1.14 `mcp__plugin_creativity-graph_creativity-graph__kg_operate(op, target=None, label="", body="", members=None, k=None)`

The **four §8 endo operations** (PLAN Stage 4), each persisting its result **through the propose lane**
(`kg_propose`), so everything lands `hypothesized`/`unverified` with no span.

- `op` — `collapse` (cluster → a new compression node + `collapses_into` edges; `members` names an explicit
  member set, else the cluster is inferred from `target`) | `explode` (a node → latent facet children) |
  `regroup` (persist §8 re-partition bridges) | `open` (a new primitive + attachment points).
- `target`, `label`, `body`, `members`, `k` — operation-specific (see the docstrings); unused ones are ignored.

Returns the `kg_propose` shape with `{ok: true, op, info}` merged in. On a bad op or nothing to operate on:
`{"ok": false, "op": "collapse", "error": "no structure to operate on", "info": …}` or
`{"ok": false, "error": "unknown op 'foo'; expected collapse|explode|regroup|open"}`.

### 1.15 `mcp__plugin_creativity-graph_creativity-graph__kg_absorption()`

The **§14 absorption window** (PLAN Stage 5). For each node grounded *from* a hypothesis, scores how long it
stayed perturbing before the graph renormalised, so the slate can prefer the fertile middle. Reads the derived
graph plus the `derived/generations.json` ledger that `/kg-generate` appends to. No args.

```json
{"tracked": 3, "summary": {"fertile": 1, "absorbed": 1, "isolated": 1},
 "nodes": {"compression": {"half_life": 2.0, "status": "fertile"}},
 "note": ""}
```

`status ∈ fertile | absorbed | isolated`. With no ledger yet, `tracked` is `0` and `note` explains that
`/kg-generate` has not started tracking the window (never an error).

### 1.16 `mcp__plugin_creativity-graph_creativity-graph__kg_agenda(limit=5)`

**Read-only structural "suggested questions"** (R6). Reads ONLY precomputed derived columns (node ranks +
edge provenance/state) and returns ~`limit` structural gaps, split into two lanes that mirror `kg_context`'s
`items[]`/`hypotheses[]`:

```json
{"answerable_now": [{"detector": "well-grounded", "lane": "answerable_now", "focus": ["compression"],
                     "question": "'compression' is a well-grounded hub (degree 4, 4 grounded) — how do its neighbours (claim, …) interrelate?",
                     "signals": {"degree": 4, "structural_bridge": 1, "betweenness": 0.3, "spec_betweenness": 0.2, "specificity": 0.7}}],
 "blocked_on_grounding": [{"detector": "under-grounded-hub", "lane": "blocked_on_grounding", "focus": ["betweenness"],
                           "question": "Hub 'betweenness' (degree 5) is under-grounded — only 1/5 of its edges are grounded. Drain its unverified queue (/kg-ground) to trust it.", "signals": {…}}],
 "count": 2, "limit": 5, "gate_on": 0, "ranked_by": "structural_bridge",
 "note": "structural suggestions — a heuristic, not a guarantee. …"}
```

- **Detectors**: `orphan` (degree 0), `hypothesized-only` (every live edge a proposal — always **blocked**,
  never laundered into answerable), `under-grounded-hub`, `well-grounded` (the only **answerable_now** kind),
  `edgeless-communities` (a disconnected cluster). The `answerable_now` vs `blocked_on_grounding` split is the
  honesty move: a question you cannot ground-back-honestly surfaces as blocked.
- **Ranking** mirrors `kg_context`'s gate-aware switch — `spec_betweenness` **only** when `gate_on=1`, else the
  `structural_bridge`/degree advisory; **never** raw betweenness as lead. `ranked_by` reports which.
- **Read-only / measure-never-gate**: it asserts no edges, copies no spans, stamps no verdicts; the question
  text is session-time only and never written to the canon. It is a **heuristic, not a guarantee** — it
  suggests where to look or what to ground next; it never answers or acts. `limit` is clamped to `[1, 50]`.

---

## 2 · Deterministic CLI surface

Run via Bash. **Dev**: repo venv `/home/sergi/creativity-graph/.venv/bin/python` (or `uv run`). **Runtime**:
`${CLAUDE_PLUGIN_DATA}/.venv/bin/python` with `PYTHONPATH=${CLAUDE_PLUGIN_ROOT}/scripts`. The `kg_engine.*`
module CLIs require that `PYTHONPATH`; `f4_probe.py` is a standalone script. None of these gate the pipeline —
each prints a number + verdict; the orchestration logs it and proceeds (§4).

### 2.1 `f4_probe.py` — extraction precision scorer

Operates on a derived `graph.json` (NetworkX node-link; reads `links` or `edges`). Three subcommands.

```bash
python scripts/f4_probe.py summary "$GRAPH"                     # shape of the graph
python scripts/f4_probe.py sheet   "$GRAPH" --n 80 --out labels.csv   # sample edges to label
python scripts/f4_probe.py score   labels.csv                  # precision / astrology / span-support
```

- **`summary <graph.json>`** — prints node/edge counts, nodes by `file_type`, edges by `confidence`
  (`EXTRACTED|INFERRED|AMBIGUOUS`), top relations, the `INFERRED` `confidence_score` distribution
  (min/median/max), and the count of *judged* edges (`INFERRED+AMBIGUOUS`) — the precision-relevant slice.
- **`sheet <graph.json> --n <N> --out <csv>`** — random-samples (seed 42) up to `N` non-`EXTRACTED` edges into
  a CSV with columns `edge_id, source_label, target_label, relation, confidence, confidence_score,
  source_file, verdict, span_found, notes`. Add `--include-extracted` to also sample deterministic edges.
  An annotator then fills two columns:
  - `verdict` ∈ **`correct | fabricated | vague | wrong_type`** (the only allowed labels).
  - `span_found` ∈ **`y | n`** (the span-present check).
- **`score <labels.csv>`** — reads rows with a filled `verdict` and prints:
  - `PRECISION (correct / labeled)` — **exit gate is `>= 0.70`**.
  - `astrology rate (fabricated+vague)` — the grounding risk, measured.
  - `span-support rate (span_found=y)` — the span-present rate.
  - verdict breakdown, precision per relation (n>=3), and confidence calibration (mean `confidence_score` for
    correct vs incorrect; a gap `>= 0.10` means the score tracks correctness, else it is "vocabulary, not
    grounding").

`vague` is the generality confound made measurable: a relation "true" only because it is generic/unfalsifiable.

### 2.2 `python -m kg_engine.pack` — pack validation + glossary coverage

```bash
python -m kg_engine.pack validate pack/pack.yaml            # PackContract validation only
python -m kg_engine.pack validate pack/pack.yaml examples/source.md   # validate + coverage
python -m kg_engine.pack coverage pack/pack.yaml examples/source.md   # coverage (source required)
```

`validate` loads the YAML as a `PackContract` (Pydantic, `extra="forbid"`; `node_types`/`edge_types` must be
non-empty + unique). On success prints `PACK OK: domain=… node_types=N edge_types=M glossary=K`; on failure
`PACK INVALID: <error>` to stderr (exit 1). If a source path is given (always for `coverage`), also prints
`coverage(...)`:

```
PACK OK: domain='conceptual theory' node_types=6 edge_types=10 glossary=12
  source_defined_terms: 10
  glossary_terms: 12
  source_terms_in_glossary: 10
  source_coverage: 1.0
  glossary_grounded_in_source: 1.0
```

- `source_coverage` — fraction of the source's *defined terms* (bold/`code`/quoted phrases) present in the
  glossary.
- `glossary_grounded_in_source` — fraction of glossary terms that actually occur in the source (don't invent
  vocabulary the source never uses).

### 2.3 `python -m kg_engine.harness` — agreement · specificity · ideation

Deterministic measurement over data the subagents produce. Three subcommands, each reads/writes JSON. If the
optional path is missing, each falls back to a built-in demo and notes it on stderr.

#### `agreement [label_sets.json]`

Nominal **Krippendorff's alpha** across independent coders. **Input JSON is a LIST of coder dicts**, one per
coder, mapping `unit_id -> label`; units rated by `<2` coders are ignored. Labels are the f4_probe verdict
vocabulary `correct | fabricated | vague | wrong_type`.

```json
[
  {"e1": "correct", "e2": "vague", "e3": "correct"},
  {"e1": "correct", "e2": "vague", "e3": "fabricated"}
]
```

Prints `krippendorff_alpha: <a>` and `verdict: RELIABLE (>=0.67)` or `BELOW THRESHOLD — grounding signal stays
advisory`. Threshold **`>= 0.67`** = reliable inter-annotator agreement.

#### `specificity [graph.json] [source.md]`

The **bridge-metric gate** (§1.4/§1.6). Compares specificity-weighted betweenness vs raw degree vs raw
betweenness over the derived graph, using IDF seeds from the source corpus (or a demo corpus). Args default to
`derived/graph.json` and the demo corpus. Emits JSON:

```json
{
  "n": 24,
  "mean_specificity": 1.42,
  "specificity_spread": 1.9,
  "betweenness_leader_specificity": 0.91,
  "top_raw_betweenness": ["system", "idea", "specificity"],
  "top_specificity_weighted": ["specificity", "betweenness", "reconciler"],
  "rank_churn": 0.4,
  "generality_confound_detected": true,
  "gate_on": true,
  "verdict": "specificity-weighting earns its place — gate ON"
}
```

`gate_on` is `true` only when the generality confound is detected (raw-betweenness leaders are vaguer than
average), rank churn `> 0.2`, **and** the node specificities actually spread (a degenerate corpus where every specificity is equal keeps the gate closed). Until this returns `gate_on:true` on real data, the specificity-weighted
bridge metric stays advisory and `kg_context` exposes only the structural-bridge heuristic. (Graphs with
`< 3` nodes return `{"gate_on": false, "reason": "graph too small", "n": …}`.)

#### `ideation [outputs.json]`

Scores pooled ideation outputs per condition (the value-of-the-graph experiment). **Input JSON**:

```json
{
  "outputs": {
    "control": ["A is connected to B."],
    "graph":   ["A bridges B and C because entropy grounds time."],
    "rag":     ["A relates to B somehow."]
  },
  "source": "<full source text for novelty/unsupported scoring>"
}
```

(`source` optional; if the top-level object isn't `{outputs, source}` it is treated as the
outputs-by-condition map directly.) Emits a per-condition `table` with `n, diversity, novelty, utility,
unsupported_rate` and a `verdict` comparing **graph vs control**:

```json
{
  "table": {
    "control": {"n": 5, "diversity": 0.71, "novelty": 0.62, "utility": 0.3, "unsupported_rate": 0.2},
    "graph":   {"n": 5, "diversity": 0.83, "novelty": 0.74, "utility": 0.6, "unsupported_rate": 0.18},
    "rag":     {"n": 5, "diversity": 0.7,  "novelty": 0.55, "utility": 0.2, "unsupported_rate": 0.25}
  },
  "verdict": "graph condition produced more diverse/novel ideas without more unsupported claims"
}
```

The `graph` condition "wins" only if it is `>=` control on diversity AND novelty, **strictly greater** on at least one of them, AND its `unsupported_rate` is no more than `control + 0.05` — i.e. measurably more/better ideas **without** more unsupported claims (an exact tie on both axes is not a win).

---

## 3 · Quick map

| You want to… | Use |
|---|---|
| check the server is up / pack loaded | `kg_ping()` |
| write extracted nodes/edges | `kg_write(payload)` (boundary, §1.5) |
| set a verdict (grounded/rejected/failed/obsolete) | `kg_ground(...)` — the **only** way |
| fix a node id everywhere | `kg_rename(old, new)` |
| cheap counts | `kg_metrics()` |
| browse by type/relation/state, ranked by degree | `query_graph(...)` |
| one node + its edges | `get_node(id)` |
| a node's edges (list) | `get_neighbors(id, relation=?)` |
| connect two nodes | `shortest_path(a, b)` |
| budgeted, grounding-aware context (+ failures + bridges) | `kg_context(query=?, budget=?)` |
| propose hypothesized candidates (the offensive lane) | `kg_propose(payload)` |
| generate structural idea candidates (read-only) | `kg_generate(mechanism=?, k=?, second_graph=?)` |
| run a §8 endo operation (collapse/explode/regroup/open) | `kg_operate(op, …)` |
| score the §14 absorption window | `kg_absorption()` |
| score extraction precision | `f4_probe.py summary|sheet|score` |
| validate the pack / glossary coverage | `kg_engine.pack validate|coverage` |
| inter-annotator agreement | `kg_engine.harness agreement` |
| bridge-metric gate verdict | `kg_engine.harness specificity` |
| value-of-the-graph experiment | `kg_engine.harness ideation` |
