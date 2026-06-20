# Reference: tools & CLIs

Load on demand. The MCP **tool surface** (graph mutation + read), then the deterministic **CLI surface**
(`f4_probe.py`, `kg_engine.pack`, `kg_engine.harness`). Every name, signature, and return shape below
mirrors `scripts/kg_engine/server.py` + `scripts/kg_engine/projector.py`. Nothing here is invented — if a
field is missing, grep the engine source, don't guess.

---

## 1 · MCP tool surface

Server name is `creativity-graph`, so every tool is namespaced `mcp__creativity-graph__<tool>`. These eleven
are the **only** graph tools. There is no `kg_build` / `kg_query` / `kg_project` MCP tool — those are slash
commands (`/kg-build`, …) that *orchestrate* these tools.

Mutation tools (`kg_write`, `kg_ground`, `kg_rename`) write the **canon** (human-editable Markdown, the single
source of truth). Read tools (`get_node`, `get_neighbors`, `shortest_path`, `query_graph`, `kg_context`) read
the **derived** layer; they call `_ensure_projected()` first, which reprojects only if `index.sqlite` is
missing or stale (`built_from_commit != HEAD`). The derived layer contains nothing the canon does not (§1.2)
and never prunes failure memory (§1.7).

### 1.1 `mcp__creativity-graph__kg_ping()`

Health check / config probe. No args.

```json
{"name": "creativity-graph", "version": "<__version__>", "metrics_mode": "structure_only",
 "sensitivity": "medium", "pack_loaded": true}
```

`pack_loaded` is `true` only when `pack/pack.yaml` validated as a `PackContract` at startup. `metrics_mode`
is `structure_only` by default (centrality stays advisory; the specificity-weighted bridge metric is gated,
§1.4/§1.6).

### 1.2 `mcp__creativity-graph__kg_scrub(text=None)`

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

### 1.3 `mcp__creativity-graph__kg_write(payload: dict)`

The boundary (§1.5). Validates an extraction payload, writes ACCEPTED/DEMOTED nodes & edges to the canon,
quarantines or rejects the rest. `payload` is the write contract (see `references/payload.md` / the shared
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
  "stash_ref": null
}
```

- `dispositions` — counts keyed by every `Disposition` value: `ACCEPTED | DEMOTED | QUARANTINED | REJECTED`.
- `details[]` — one per validated item: `kind` (`node`|`edge`), `id` (the derived edge id
  `e_{source}__{relation}__{target}`, or `null`), `disposition`, `reason` (e.g. `no-supporting-span`,
  `span-not-in-source`, `truncated-payload`, `schema-invalid`, `forged-verdict-stripped`,
  `human-claim-stripped`, `undeclared-node-type`, `undeclared-edge-type`), `retryable` (**`false`** for SEMANTIC rejections — no-span,
  span-not-in-source; **`true`** for TRANSPORT — truncation, schema-invalid).
- `written_nodes[]` — node ids actually committed (includes boundary-auto-created placeholder source nodes).
- `rolled_back` / `stash_ref` — set when the canon write stashed instead of committing.

A write may never set a verdict or `authored_by=human`: such payloads are **DEMOTED** (verdict reset to
`unverified` → `forged-verdict-stripped`; human → `agent` → `human-claim-stripped`), never accepted as-is.

### 1.4 `mcp__creativity-graph__kg_ground(target_id, verdict, by="agent", kind="edge", note="")`

**The ONLY path that may set a verdict** (§1.4/§1.8). Stamps the epistemic_state and appends a `ground.audit`
record so the reconciler treats the transition as legitimate.

- `target_id: str` — an edge id (default `kind="edge"`) or node id (`kind="node"`).
- `verdict: str` — one of `VALID_VERDICTS = {grounded, rejected, failed, obsolete}` (lower-cased internally).
- `by: str = "agent"` — recorded as `verdict_by` on the edge (use a person's name for a human verdict — that
  is the only legitimate route to a human-authored state).
- `kind: str = "edge"` — `edge` or `node`.
- `note: str = ""` — appended to the edge's `notes` (e.g. the rejection reason `vague` for a generality-confound
  edge that is "true" only because it is generic/unfalsifiable, §1.6).

```json
{"ok": true, "key": "e_generality-confound__attacked-by__specificity",
 "from": "unverified", "to": "grounded", "by": "agent"}
```

On failure: `{"ok": false, "error": "invalid verdict 'maybe'"}` / `"node not found"` / `"edge not found"`.
For an edge, also sets `verdict_by` and `verdict_at`. Note: the return `key` for a node verdict is
`node:<id>`; for an edge it is the edge id.

> Adversarial grounding (§1.7): the adversarial grounder adds `attacked_by` edges then calls
> `kg_ground(target_id=<edge>, verdict="failed")`. Failed/rejected edges are NEGATIVE INFORMATION — never
> pruned, surfaced by `kg_context.falsification_counters`.

### 1.5 `mcp__creativity-graph__kg_rename(old_id, new_id)`

Renames a node and rewrites every edge endpoint (`source`/`target`) referencing it, preserving the
single-canonical-edge rule. Both ids are slugged.

```json
{"ok": true, "old": "betweeness", "new": "betweenness",
 "touched": ["betweenness", "generality-confound", "specificity"]}
```

Failure: `{"ok": false, "error": "node not found"}` or `"target id exists"`. `ok` is `false` if the write had
to stash.

### 1.6 `mcp__creativity-graph__kg_metrics()`

Cheap summary counts straight off the canon (no projection). No args.

```json
{"nodes": 24, "edges": 41, "edges_by_epistemic_state": {"unverified": 30, "grounded": 7, "failed": 4}}
```

`edges_by_epistemic_state` keys are whatever `EpistemicState` values are present
(`unverified|grounded|rejected|failed|obsolete`).

### 1.7 `mcp__creativity-graph__query_graph(node_type=None, relation=None, epistemic_state=None, limit=50)`

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
`bridge_communities >= 2`). Valid `node_type` filters are the pack's declared types
(`compression|primitive|claim|metric|operation|failure`); `relation` filters the declared edge types
(`grounds|attacked_by|reconciles_with|bridges|collapses_into|confounded_by|approximates|defends_against|projects|survives`).

### 1.8 `mcp__creativity-graph__get_node(node_id)`

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

### 1.9 `mcp__creativity-graph__get_neighbors(node_id, relation=None)`

A **list** (not a dict) of edge dicts incident to `node_id` (as `source` OR `target`), optionally filtered by
`relation`. Each element has the same shape as an edge row above. Empty list if the node has no incident edges.

### 1.10 `mcp__creativity-graph__shortest_path(source, target)`

BFS over the derived edge list, treated as **undirected** (no centrality is computed).

```json
{"path": ["generality-confound", "specificity", "betweenness"]}
```

`{"path": ["x"]}` when `source == target`; `{"path": null}` when no path exists.

### 1.11 `mcp__creativity-graph__kg_context(query=None, budget=2000)`

The **grounding-aware, provenance-carrying, token-budgeted** context tool — the one to call before reasoning
over the graph. Reads precomputed ranks **O(1)**; it **NEVER computes centrality in-request** (centrality is
precomputed off the hot path by the projector). `query` (optional) does a `LIKE` filter over edge
`source|target|relation|span`. `budget` (default `2000`) caps approximate tokens (`len(json)//4` per item).

Priority fill order (best context first, until the budget is spent): **grounded edges first**, then
`span-present` provenance, then `inferred`, then by `confidence_score` DESC.

```json
{
  "items": [
    {"id": "e_compression__grounds__claim", "source": "compression", "target": "claim",
     "relation": "grounds", "provenance": "span-present", "authored_by": "agent",
     "epistemic_state": "grounded", "span": "...", "confidence": "INFERRED", "confidence_score": 0.82}
  ],
  "approx_tokens": 1840,
  "budget": 2000,
  "falsification_counters": {"failed_or_rejected_edges": 4},
  "advisory": {
    "signal": "structural-bridge",
    "note": "advisory heuristic, not a guarantee",
    "nodes": [
      {"id": "compression", "label": "Compression", "degree": 6, "bridge_communities": 2}
    ]
  }
}
```

- `items[]` — budget-trimmed edge records (note: `source_file` is omitted from context items, unlike
  `query_graph`/`get_node` edge rows).
- `approx_tokens` — tokens actually used (`<= budget`).
- `falsification_counters.failed_or_rejected_edges` — count of edges in `FAILURE_STATES`
  (`rejected` + `failed`). **Memory of failures (§1.7): surfaced here, never pruned.** A non-zero counter is a
  signal that the graph already knows what was refuted; don't re-propose it.
- `advisory` — the **labelled structural-bridge** signal: `signal:"structural-bridge"`, an explicit
  `note:"advisory heuristic, not a guarantee"`, and up to 10 `nodes` with `structural_bridge=1` ordered by
  `degree` DESC. Treat as a hint, not a metric — the specificity-weighted bridge metric is GATED until the
  harness validates it (§1.4/§1.6). A structural bridge that is vague is the generality confound, not a real
  bridge.

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
  source_defined_terms: 18
  glossary_terms: 12
  source_terms_in_glossary: 9
  source_coverage: 0.5
  glossary_grounded_in_source: 0.833
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
average) **and** rank churn `> 0.2`. Until this returns `gate_on:true` on real data, the specificity-weighted
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

The `graph` condition "wins" only if it is `>=` control on diversity AND novelty AND its `unsupported_rate`
is no more than `control + 0.05` — i.e. more/better ideas **without** more unsupported claims.

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
| score extraction precision | `f4_probe.py summary|sheet|score` |
| validate the pack / glossary coverage | `kg_engine.pack validate|coverage` |
| inter-annotator agreement | `kg_engine.harness agreement` |
| bridge-metric gate verdict | `kg_engine.harness specificity` |
| value-of-the-graph experiment | `kg_engine.harness ideation` |
