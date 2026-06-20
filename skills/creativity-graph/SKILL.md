---
name: creativity-graph
description: >-
  Turn a non-self-grounding conceptual document into a grounded, queryable knowledge graph.
  Use this when the user wants to build, ground, query, evaluate, or experiment over a
  knowledge graph extracted from prose — anything touching grounding, provenance, spans,
  falsification, bridges/betweenness, the canon-vs-derived split, or "is this claim actually
  supported by the source?" Triggers: knowledge graph, grounding, provenance, conceptual
  document, build the graph, ground the graph, query the graph, falsification, span-present.
---

# creativity-graph — operating guide

A conceptual document does not verify itself the way code verifies against a parse tree
(`examples/source.md` §intro). A naive extractor turns such prose into convincing nonsense.
This plugin extracts a graph, then forces every non-trivial edge to earn its place against the
*original text* and against adversarial attack. A deterministic Python engine (`scripts/kg_engine`,
49 tests green) does the rule-bound work; this session and its subagents do the LANGUAGE work and
hand structured JSON back across the MCP boundary. Your job is to orchestrate, not to forge.

## The model: canon vs derived (§1.2)

- **Canon** — the human-editable source of truth. One Markdown file per node at
  `${CLAUDE_PROJECT_DIR}/canon/<node-id>.md` (YAML frontmatter + body; directed edges live in the
  source node's `edges:` block). The canon carries the grounding state.
- **Derived** — a regenerable, *disposable* projection at
  `${CLAUDE_PLUGIN_DATA}/derived/{graph.json,index.sqlite}`. The derived layer **contains nothing
  the canon does not**. Never edit it by hand; reproject from canon instead.

## The three axes (§1.3) — orthogonal, never collapsed to one scalar

| axis | values |
| --- | --- |
| `provenance` | `span-present` \| `inferred` \| `hypothesized` |
| `authored_by` | `deterministic` (parser, no LLM) \| `agent` (subagent) \| `human` (only via a verdict) |
| `epistemic_state` | `unverified` \| `grounded` \| `rejected` \| `failed` \| `obsolete` |

A high-provenance edge can still be `unverified`; a `grounded` edge can be `inferred`. Do not
collapse the three into one "quality" number.

## The workflow: build → ground → query → eval → experiment

1. **`/kg-build [source_path]`** — extract candidate nodes/edges from prose and write them through
   the boundary. Orchestrates **kg-extractor** (emits a `kg_write` payload), then optionally
   **kg-annotator** for an F4-probe label sheet. Output edges land `unverified`.
2. **`/kg-ground [node_or_query]`** — adjudicate `unverified` edges. **kg-grounder** confirms or
   rejects on the merits and stamps verdicts via `kg_ground`; **kg-adversarial-grounder** tries to
   *falsify* surviving edges, recording `attacked_by` edges + `kg_ground(verdict="failed")`.
3. **`/kg-query [question]`** — read the grounded graph: `query_graph`, `get_node`,
   `get_neighbors`, `shortest_path`, and `kg_context` (budgeted, falsification-aware) for grounded
   answers with provenance.
4. **`/kg-eval`** — run the deterministic gates: extraction precision (`f4_probe.py score`, gate
   ≥ 0.70), inter-coder agreement (`harness agreement`, Krippendorff α ≥ 0.67), and the
   bridge-metric gate (`harness specificity`). **kg-evaluator** drives these and reports verdicts.
5. **`/kg-experiment`** — control \| graph \| rag ideation comparison via `harness ideation`.

## Who does what

| command | subagent(s) | engine surface |
| --- | --- | --- |
| `/kg-build` | `kg-extractor`, `kg-annotator` | `kg_write`, `f4_probe.py` |
| `/kg-ground` | `kg-grounder`, `kg-adversarial-grounder` | `kg_ground`, `query_graph`, `kg_write` |
| `/kg-query` | (none; direct reads) | `query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `kg_context` |
| `/kg-eval` | `kg-evaluator` | `f4_probe.py`, `kg_engine.harness`, `kg_metrics` |
| `/kg-experiment` | (orchestration) | `kg_engine.harness ideation` |

The MCP server is named `creativity-graph`; tools are namespaced `mcp__plugin_creativity-graph_creativity-graph__<tool>`
(`kg_ping`, `kg_scrub`, `kg_write`, `kg_ground`, `kg_rename`, `kg_metrics`, `query_graph`,
`get_node`, `get_neighbors`, `shortest_path`, `kg_context` — ELEVEN tools). These are the ONLY
graph tools — `kg-build`, `kg-query`, etc. are slash commands, not tools.

## Core invariants — non-negotiable, enforced by the boundary

1. **span-present (§1.5).** Every non-deterministic edge MUST carry a `span` that is a *verbatim*
   substring of the source (whitespace/case-normalized). No span → `REJECTED/no-supporting-span`;
   span not in source → `REJECTED/span-not-in-source` (fabrication). **Copy spans exactly from the
   source; never paraphrase.** From `source.md`: an edge `generality → attacked_by → specificity`
   needs `span: "a more specific claim, when it holds, defeats a vaguer one"`.
2. **never-forge-a-verdict (§1.4/§1.8).** A `kg_write` payload may NOT assert
   `grounded/rejected/failed`, nor `authored_by=human`. Such payloads are `DEMOTED` to
   `unverified`/`agent`. Verdicts come ONLY through `kg_ground`. The reconciler re-quarantines any
   out-of-band verdict edit.
3. **generality confound → degree, not betweenness (§1.6).** A vague node accumulates spurious
   edges and spuriously high betweenness because it touches everything loosely. Plain **degree** is
   the honest MVP advisory (`approximates` importance); **specificity-weighted betweenness** is a
   hypothesis, GATED behind `harness specificity` until validated. Grounders MUST reject edges that
   are "true" only because they are generic or unfalsifiable (verdict reason: `vague`).
4. **memory of failures (§1.7).** `rejected`/`failed` edges are NEGATIVE INFORMATION — never
   pruned, surfaced in `kg_context.falsification_counters.failed_or_rejected_edges`. The
   adversarial grounder *creates* this signal via `attacked_by` edges plus
   `kg_ground(verdict="failed")`. A graph that forgets its mistakes drifts into nonsense.
5. **egress scrub (§1.9).** `mcp__plugin_creativity-graph_creativity-graph__kg_scrub(text)` redacts secrets (always) plus
   PII (per `sensitivity`) with CONSISTENT placeholders (`⟦SECRET:1⟧` etc.) *before* text is handed
   to a subagent; `kg_write` then RESTORES placeholder spans to the ORIGINAL text so the canon
   stores the restored original span. On the no-PII demo source it is a no-op (0 redactions).

## Boundary dispositions (what `kg_write` returns)

`ACCEPTED` (written, `unverified`) · `DEMOTED` (written, one axis downgraded) ·
`QUARANTINED` (structurally valid but untrusted, e.g. undeclared type) · `REJECTED` (not written).
`retryable=false` for SEMANTIC rejections (no-span, span-not-in-source, vague); `retryable=true`
for TRANSPORT failures (truncation, schema). Every `kg_write` payload MUST set `"complete": true` —
a false/missing flag is `REJECTED` as truncated.

## Domain pack

`pack/pack.yaml` declares the vocabulary. Types outside it → `QUARANTINED/undeclared-type`.
- node types: `compression`, `primitive`, `claim`, `metric`, `operation`, `failure`
- edge types: `grounds`, `attacked_by`, `reconciles_with`, `bridges`, `collapses_into`,
  `confounded_by`, `approximates`, `defends_against`, `projects`, `survives`

Use only these in extraction. Glossary and per-term specificity seeds live in `pack/pack.yaml` +
`pack/glossary.md`. Validate coverage with `python -m kg_engine.pack validate pack/pack.yaml examples/source.md`.

## References (load on demand)

Keep this guide tight; full detail lives in `references/` and is read only when needed:
- the canon note frontmatter + `edges:` schema and the full `kg_write` payload contract,
- the boundary disposition decision table and reason strings,
- the F4-probe label vocabulary (`correct | fabricated | vague | wrong_type`, `span_found: y|n`)
  and the eval gate thresholds,
- the deterministic CLI cheatsheet (`f4_probe.py`, `kg_engine.pack`, `kg_engine.harness`).

The authoritative data model is `ARCHITECTURE.md` and the engine source under `scripts/kg_engine`.
When in doubt about a field or symbol, grep the engine rather than guessing.
