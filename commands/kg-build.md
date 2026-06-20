---
description: Build the grounded knowledge graph from a source document — extract section-by-section into the canon, then project.
argument-hint: "[source_path]"
allowed-tools: Task, Bash, mcp__plugin_creativity-graph_creativity-graph__kg_scrub, mcp__plugin_creativity-graph_creativity-graph__kg_metrics, mcp__plugin_creativity-graph_creativity-graph__kg_context, mcp__plugin_creativity-graph_creativity-graph__query_graph
---

# /kg-build — orchestrate the BUILD

You are the **build orchestrator**. You turn a non-self-grounding conceptual document into a grounded,
queryable graph by driving the `kg-extractor` subagent over the source **section by section**, then forcing the
derived projection and reporting what landed. You do the *coordination*; the extractor does the *language work*;
the engine does the *deterministic work* behind the `kg_write` boundary.

You do **not** call `kg_write` yourself, and you **never** assert a verdict — verdicts come only from `kg_ground`
(out of scope for this command). See §1.4 / §1.8 (never-forge-a-verdict).

## Inputs

- `$1` — the source path to build from. **Default**: `${CLAUDE_PLUGIN_OPTION_SOURCE_PATH}` if set, else
  `examples/source.md`. Resolve once at the top:

  ```
  SOURCE="${1:-${CLAUDE_PLUGIN_OPTION_SOURCE_PATH:-examples/source.md}}"
  ```

## Procedure

### 0. Resolve & sanity-check the source (Bash)

```bash
SOURCE="${1:-${CLAUDE_PLUGIN_OPTION_SOURCE_PATH:-examples/source.md}}"
test -f "$SOURCE" || { echo "source not found: $SOURCE"; exit 1; }
echo "building from: $SOURCE"
# Enumerate the section headings so you can iterate one section at a time.
grep -nE '^## ' "$SOURCE"
```

The demo corpus (`examples/source.md`) is the five-section "theory of grounded conceptual knowledge":
**§1 Compression and the cost of generality**, **§2 Provenance and the span**, **§3 Bridges and betweenness**,
**§4 Memory of failures**, **§5 The canon and the projection**. One extractor launch per `## ` section.

### 1. Egress scrub — the §1.9 egress point (now WIRED)

You are reading a **source on disk** and handing *section text* to a subagent. The §1.9 egress scrub is the
engine's `scrub.py`, now exposed as the MCP tool `mcp__plugin_creativity-graph_creativity-graph__kg_scrub`. It is real and wired into the
flow as **Step 0** of each section:

1. **Step 0 — scrub before egress.** Call `mcp__plugin_creativity-graph_creativity-graph__kg_scrub(text=<section body>)` to get the
   `scrubbed` source. This redacts secrets (always) and PII (per sensitivity) into **consistent placeholders**
   (`⟦SECRET:1⟧`, `⟦EMAIL:1⟧`, …) before any text crosses the egress to a subagent. It returns
   `{scrubbed, redactions, sensitivity, categories}`. For the no-PII demo source (`examples/source.md`),
   `kg_scrub` is a **no-op** — `redactions: 0` and the scrubbed text equals the original.
2. **Hand the SCRUBBED text to `kg-extractor`.** The subagent only ever sees the scrubbed (placeholder) form, so
   it emits spans in **scrubbed form** when a redaction fell inside a span.
3. **`kg_write` RESTORES the spans to the original for the canon.** The boundary maps each placeholder span back
   to the original text before span verification and stores the **restored original** span in the canon. The
   scrub protects the **egress**, not the local canon — the canonical record keeps the true text.

So the scrub does run for §1.9 egress protection, but it runs **before** the extractor (Step 0), not silently
inside `kg_write`; the boundary's role is the **restore**. You do **not** post-process spans yourself — the
extractor copies spans **verbatim** (§1.5) from the scrubbed text it was given, and the boundary restores and
validates them. Do not paraphrase or "clean up" the section text you pass in; a mangled span will be **REJECTED**
as `span-not-in-source` (fabrication).

### 2. Launch the `kg-extractor` subagent per section (Task)

For **each** `## ` section, launch the extractor with the **section's verbatim text** and its source filename.
The extractor reads the section, emits a single complete `kg_write` payload, and reports its dispositions back to
you. Run sections **sequentially** so later sections can reference node IDs created earlier (the boundary
auto-creates placeholder nodes for an edge's `source`, and targets may reference not-yet-created nodes — so order
is for legibility, not correctness).

Exact Task invocation (repeat per section, substituting the heading + body):

```
Task(
  subagent_type: "kg-extractor",
  description: "Extract §1 Compression",
  prompt: """
You are extracting ONE section of the source document into the canon via mcp__plugin_creativity-graph_creativity-graph__kg_write.

source_file: source.md          # basename of $SOURCE — used as edge.source_file
section: "## 1. Compression and the cost of generality"

SECTION TEXT (verbatim — copy spans EXACTLY from this; never paraphrase):
<<<
A **compression** is a single idea that stands in for many observations; it earns its keep only when it
predicts. The **generality confound** is the failure mode where a vague idea accumulates spurious
connections: because it touches everything loosely, it looks central while explaining nothing. Generality
is therefore *attacked_by* specificity — a more specific claim, when it holds, defeats a vaguer one that
merely overlaps it. A compression that survives specific attack is said to *grounds* the claims beneath it.
>>>

Follow your system contract: declared node_types (compression, primitive, claim, metric, operation, failure)
and edge_types (grounds, attacked_by, reconciles_with, bridges, collapses_into, confounded_by, approximates,
defends_against, projects, survives) only — anything else is QUARANTINED with a per-item
details[].reason of `undeclared-node-type` (nodes) or `undeclared-edge-type` (edges). Every
non-deterministic edge MUST carry a verbatim "span" that is a substring of the section text above (§1.5), or it
is REJECTED. Do NOT set epistemic_state to a verdict and do NOT set authored_by=human (§1.4) — those are DEMOTED
("forged-verdict-stripped" / "human-claim-stripped"). Emit exactly one payload with "complete": true and return
the kg_write result (dispositions, details[], written_nodes[], rolled_back).
"""
)
```

> Why one section per launch: it keeps each extractor's span-verification scoped to text it can actually see, which
> is what makes `span-present` (§1.5) checkable rather than a paraphrase. A whole-document launch invites the
> extractor to "remember" spans and fabricate them.

Collect each launch's returned `kg_write` result: the `dispositions` counts
(**ACCEPTED / DEMOTED / QUARANTINED / REJECTED**), `details[]`, `written_nodes[]`, and `rolled_back`.

### 3. Force / confirm the derived projection

The canon is the single source of truth; the derived layer is regenerable and **projects** the canon (§5). The
read tools project **lazily** — they only rebuild the derived layer when it is stale. Confirm the build landed:

1. `mcp__plugin_creativity-graph_creativity-graph__kg_metrics()` — reads the **canon** directly and returns
   `{nodes, edges, edges_by_epistemic_state}`. This is your authoritative count of what the extractors wrote.
   Freshly written edges are `unverified` (no verdicts asserted at build time), so expect
   `edges_by_epistemic_state` to be dominated by `unverified`.
2. `mcp__plugin_creativity-graph_creativity-graph__kg_context(budget=2000)` — this **lazily projects** (rebuilds the derived layer if
   stale) and returns `{items[], approx_tokens, budget, falsification_counters:{failed_or_rejected_edges},
   advisory:{signal:"structural-bridge", note, nodes[]}}`. Calling it both *forces* the projection and confirms
   the derived layer agrees with the canon. At build time `falsification_counters.failed_or_rejected_edges` will
   typically be 0 — failures are negative information created later by an adversarial grounder via `kg_ground`
   (§1.7).

Optionally spot-check structure with `mcp__plugin_creativity-graph_creativity-graph__query_graph(node_type="compression")` or
`mcp__plugin_creativity-graph_creativity-graph__query_graph(epistemic_state="unverified", limit=50)` to eyeball the written nodes/edges.

### 4. Report

Summarize the build back to the user:

- **Dispositions** — summed across all section launches: ACCEPTED / DEMOTED / QUARANTINED / REJECTED, and for any
  REJECTED, the reason from `details[]` (`no-supporting-span`, `span-not-in-source`, `truncated-payload`,
  `schema-invalid`). Note `retryable=false` for SEMANTIC rejections (no-span, span-not-in-source) — those are
  extractor errors, not transport; `retryable=true` for TRANSPORT (truncation, schema).
- **Node / edge counts** — from `kg_metrics()`: `nodes`, `edges`, and the `edges_by_epistemic_state` breakdown.
- **Span-support** — every ACCEPTED non-deterministic edge carries a verifiable span by construction (the
  boundary rejects spanless edges). Call this out as the build's grounding guarantee, and surface any DEMOTED
  edges (a forged verdict or human claim was stripped back to `unverified` / `agent`).
- **Falsification counters** — from `kg_context`: `falsification_counters.failed_or_rejected_edges` (expected 0 at
  build; non-zero only after grounding).

## Worked example (against `examples/source.md`)

After five extractor launches over the demo corpus you should expect ACCEPTED nodes like `compression`,
`generality-confound`, `specificity`, `bridge`, `betweenness`, `specificity-weighted-betweenness`, `degree`,
`canon`, `derived`, and ACCEPTED edges such as:

- `generality-confound --attacked_by--> specificity`
  span: `a more specific claim, when it holds, defeats a vaguer one`
- `betweenness --confounded_by--> generality-confound`
  span: `because a vague node sits on many paths for empty reasons`
- `specificity-weighted-betweenness --reconciles_with--> bridge`
  span: `weighting each node by the rarity of its terms`
- `degree --approximates--> importance`
  span: `plain **degree** is the honest advisory that *approximates* importance`
- `derived --projects--> canon`  (span: `The derived layer *projects* the canon`)

> Note on spans: `examples/source.md` wraps relation words in markdown emphasis (`*attacked_by*`, `*projects*`,
> …) and `normalize_text` does **not** strip `*`, so a span must either be asterisk-free clean prose (as above) or
> include the asterisks verbatim — never an asterisk-stripped `attacked_by` clause.

All emitted with `provenance: span-present`, `authored_by: agent`, `epistemic_state: unverified`. Edge IDs are
derived deterministically as `e_{source}__{relation}__{target}`, where `slug()` collapses underscores **and**
spaces to hyphens — e.g. `e_generality-confound__attacked-by__specificity` (the `attacked_by` relation slugs to
`attacked-by` in the id). After the build, `/kg-ground` (adversarial grounding) and
`/kg-query` (read) take over; **nothing here sets a verdict.**

## Failure modes to watch (and how you, the orchestrator, respond)

- **A section returns all REJECTED with `span-not-in-source`** → the section text you pasted into the Task prompt
  was altered (markdown stripped, whitespace mangled). Re-launch that section with the **verbatim** body. Do not
  hand-edit spans yourself.
- **`rolled_back: true`** on a launch → the whole payload was atomic-rejected (e.g. `truncated-payload`). This is
  `retryable=true`; re-launch the extractor for that section.
- **High QUARANTINED count** → the extractor used types outside the pack vocabulary (per-item
  `details[].reason` is `undeclared-node-type` / `undeclared-edge-type`; the offending node lands in the
  `undeclared-type` node_type bucket value). This is a pack-coverage gap, not a build error; report it so the pack
  (`pack/pack.yaml`) can be extended, then
  validated with `python -m kg_engine.pack validate pack/pack.yaml "$SOURCE"`.
