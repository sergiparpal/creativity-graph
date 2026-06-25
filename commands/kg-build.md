---
description: Build the grounded knowledge graph from a source document — extract section-by-section into the canon (bounded parallel waves), then project.
argument-hint: "[source_path] [wave_size]"
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

- `$1` — the source to build from: a single **file**, or a **directory / glob** of `.md`/`.txt` files
  (R4 — multi-document). **Default**: `${CLAUDE_PLUGIN_OPTION_SOURCE_PATH}` if set, else
  `examples/source.md`. Resolve once at the top:

  ```
  SOURCE="${1:-${CLAUDE_PLUGIN_OPTION_SOURCE_PATH:-examples/source.md}}"
  ```

- `$2` — **optional inline wave size**: how many section-extractor subagents to launch CONCURRENTLY per
  wave (bounded parallelism). **Precedence: explicit `$2` > user_config > default.** When `$2` is omitted
  it falls back to `${CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE}`, then to `6`. The value is parsed to an
  integer and **clamped to 1–10** (unset / non-numeric / `< 1` → `6`; `> 10` → `10`). One section is still
  one subagent (the span-isolation property, below); the wave size only controls how many of those run at
  once. Resolved deterministically in Step 0.

## Procedure

### 0. Resolve & enumerate the source FILES (Bash)

A single file is the common case; a directory or glob builds from **every** `.md`/`.txt` member. Build the
file list, then iterate the sections **within each file**, carrying that file's **basename** as `source_file`.

```bash
SOURCE="${1:-${CLAUDE_PLUGIN_OPTION_SOURCE_PATH:-examples/source.md}}"
# Resolve the extraction WAVE SIZE: inline override ($2) > user_config > default 6; integer; clamp 1..10.
# (This pure-Bash resolution mirrors kg_engine.waves.resolve_wave_size, the unit-tested reference — no
# venv/PYTHONPATH dependency here. A present-but-invalid value falls straight to the default 6, not the
# next level down.)
WAVE_RAW="${2:-${CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE:-6}}"
case "$WAVE_RAW" in (''|*[!0-9]*) WAVE_SIZE=6 ;; (*) WAVE_SIZE=$WAVE_RAW ;; esac   # unset/non-numeric -> 6
[ "$WAVE_SIZE" -lt 1 ] 2>/dev/null && WAVE_SIZE=6      # below range -> default
[ "$WAVE_SIZE" -gt 10 ] 2>/dev/null && WAVE_SIZE=10    # above range -> clamp to max
echo "extraction wave size: $WAVE_SIZE"
# Build the list of source FILES (a single file, every .md/.txt in a directory, or a glob).
if [ -d "$SOURCE" ]; then
  FILES=$(find "$SOURCE" -maxdepth 1 -type f \( -name '*.md' -o -name '*.txt' \) ! -name '.*' | sort)
else
  FILES=$(ls -1 $SOURCE 2>/dev/null | sort)   # a single file or a shell glob
fi
[ -n "$FILES" ] || { echo "no .md/.txt source found at: $SOURCE"; exit 1; }
echo "building from:"; echo "$FILES"
# For each file, enumerate its section headings (you iterate one section per extractor launch).
for f in $FILES; do echo "== $f =="; grep -nE '^## ' "$f"; done
```

The demo corpus (`examples/source.md`) is the five-section "theory of grounded conceptual knowledge":
**§1 Compression and the cost of generality**, **§2 Provenance and the span**, **§3 Bridges and betweenness**,
**§4 Memory of failures**, **§5 The canon and the projection**. One extractor launch per `## ` section, **per
file**; pass the file's basename (e.g. `source.md`) as the extractor's `source_file`.

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

### 2. Launch the `kg-extractor` subagent per section, in BOUNDED PARALLEL WAVES (Task)

For **each** `## ` section **of each file**, launch the extractor with the **section's verbatim text** and the
**basename of the file it came from** as `source_file`. The extractor stamps every edge in that section with that
basename; with a multi-file build the boundary verifies each span against **that file specifically** (R4 — a span
attributed to the wrong file is REJECTED `span-not-in-named-source`). The extractor reads the section, emits a
single complete `kg_write` payload, and reports its dispositions back to you.

**Launch the per-section subagents CONCURRENTLY in waves of `WAVE_SIZE` (from Step 0).** Collect the full list of
`(file, section)` pairs, then process it in batches: issue `WAVE_SIZE` `Task(...)` calls **in a single message**
(so they run in parallel), wait for that whole wave to finish, then launch the next wave, until every section is
done. The default `WAVE_SIZE` is `6`; a 19-section document is therefore four waves (6 + 6 + 6 + 1) instead of 19
serial launches — the extractors' (slow) token generation overlaps across the wave, while the (brief) `kg_write`
calls all funnel through the one MCP server process and **serialize cleanly** there, so nothing is dropped or
corrupted. Ordering does **not** matter for correctness: the boundary auto-creates a placeholder node for an
edge's `source`, and a `target` may reference a node a later section creates, so edges across waves resolve
regardless of which wave lands first. Keep **one section per subagent** (never batch sections into one launch —
see the span-isolation note below).

Exact Task invocation (one per section — repeat across the wave, substituting each section's heading + body):

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

> Why one section per launch (the **span-isolation** property — unchanged by the parallel waves): it keeps each
> extractor's span-verification scoped to text it can actually see, which is what makes `span-present` (§1.5)
> checkable rather than a paraphrase. A whole-document launch invites the extractor to "remember" spans and
> fabricate them. **Parallelism is across launches, never within one**: collapsing several sections into a single
> subagent would let an extractor mis-attribute a span across sections of the same file — undetectable by the
> boundary, which verifies the span against the whole `source_file`, not the one section. Waves change *how many*
> single-section extractors run at once; they never change *what one extractor sees*.

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

After five extractor launches over the demo corpus (one wave at the default `WAVE_SIZE=6`, since the demo has
five `##` sections) you should expect ACCEPTED nodes like `compression`,
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
