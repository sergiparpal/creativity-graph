---
description: Grounding's second function — import EXTERNAL structure the graph's own dynamics would resist, build it as a second construction, and cross-generate (ensemble §9) to surface bridges that exist across constructions. The only mechanism that attacks coverage.
argument-hint: "[second_source_or_graph_json]"
allowed-tools: Task, Bash, mcp__plugin_sproutgraph_sproutgraph__kg_generate, mcp__plugin_sproutgraph_sproutgraph__kg_propose, mcp__plugin_sproutgraph_sproutgraph__kg_context, mcp__plugin_sproutgraph_sproutgraph__kg_metrics
---

# /kg-perturb — perturbation & exo cross-generation (PLAN Stage 7; §9 / §15)

Every other generator is **endo**: it elaborates the graph using the graph's own structure, so it can only
ever surface what the current construction already implies. `/kg-perturb` is the **exo** move (§9): it
builds a *second construction* of the same territory — the same source under a different pack/resolution, or
a second source entirely — and cross-generates against it (the `ensemble` mechanism). The candidates it
emits are bridges that exist in one construction's structure but **not** the other's: precisely the
connections the graph's own dynamics would resist.

This is the **only** mechanism that *attacks coverage* rather than elaborating within it. Be honest about
its limit (the ensemble caveat): a second construction does not eliminate the blind spot — it **relocates**
it. You trade your construction's blind spots for a different set, and the bridges that survive both are the
ones worth grounding.

`$ARGUMENTS` (optional `$1`): a path to a **second source document** to build into a second construction, or
a path to an already-built **`graph.json`** to cross against. If omitted, perturb the current construction
in place (a re-partition), which degrades to `regroup`.

## Step 0 — confirm a primary graph exists

Call `mcp__plugin_sproutgraph_sproutgraph__kg_context(budget=2000)`. If empty, tell the user to run
`/kg-build` → `/kg-ground` (and optionally `/kg-generate`) first, and stop.

## Step 1 — obtain the SECOND construction (a second graph.json)

Resolve the runner once (dev vs runtime, per the contract), then produce a second `graph.json`:

```bash
PY=/home/sergi/Sproutgraph/.venv/bin/python          # or "${CLAUDE_PLUGIN_DATA}/.venv/bin/python"
SCRIPTS=/home/sergi/Sproutgraph/scripts               # or "${CLAUDE_PLUGIN_ROOT}/scripts"
SECOND="$1"
```

- **`$1` is already a `graph.json`** → use it directly as `SECOND_GRAPH="$1"`.
- **`$1` is a second source document** → build it headlessly into a *separate* vault and project, which
  writes its `derived/graph.json` (needs `ANTHROPIC_API_KEY`; the in-session alternative is to run
  `/kg-build` against a second `KG_PROJECT_DIR` and reuse its derived graph):
  ```bash
  export KG_PROJECT_DIR="$(mktemp -d)"; export KG_SOURCE_PATH="$1"; export KG_PACK_PATH=pack/pack.yaml
  PYTHONPATH="$SCRIPTS" "$PY" -m kg_engine.backend extract          # extract → boundary → canon → project
  SECOND_GRAPH="$KG_PROJECT_DIR/.kg-data/derived/graph.json"
  ```
- **`$1` omitted / no API key** → skip the second construction; `kg_generate` will **degrade to `regroup`**
  (a re-partition of the current construction). Surface this as a one-line note and continue — never block.

If a second graph was built, sanity-check it loaded (non-empty nodes/edges) before cross-generating.

## Step 2 — cross-generate (ensemble §9)

Call `mcp__plugin_sproutgraph_sproutgraph__kg_generate(mechanism="ensemble", second_graph="$SECOND_GRAPH", k=10)`.
It returns hypothesized candidate bridges that are adjacent in the second construction but absent in ours
(each rationale carries `perturbation=external`). With no second graph, the same call returns the `regroup`
degrade (its `note` says so) — still useful, but internal, not coverage-attacking.

## Step 3 — phrase & write to the hypothesized lane (Task → kg-generator)

Launch **kg-generator** with the candidates. Instruct it to mark each proposal as **imported structure**:
carry `perturbation=external` into the `notes`, so the slate is legible as cross-construction structure
rather than internal elaboration. It writes through `mcp__plugin_sproutgraph_sproutgraph__kg_propose`
(hypothesized/unverified, no span — never a verdict).

```
Task(subagent_type: "kg-generator", description: "Phrase exo bridges", prompt: """
  Candidates from kg_generate(ensemble): <paste candidates[]>. Phrase each as one falsifiable sentence,
  keeping source/target/relation verbatim, and tag each note with `perturbation=external` (imported from a
  second construction, not internal elaboration). Assemble ONE kg_propose payload (provenance=hypothesized,
  NO span, NO verdict) and call it. Report dispositions + the phrased slate.
""")
```

## Step 4 — report the perturbation slate

Present the ranked slate (mechanism `ensemble`, § = §9, one-sentence idea, specificity), flagged as
**imported external structure**, then `mcp__plugin_sproutgraph_sproutgraph__kg_metrics` (the new
hypothesized candidates land under `unverified`). State the caveat explicitly: *perturbation relocates the
blind spot; it does not eliminate it.* Then point at `/kg-ground` as the filter — a cross-construction bridge
earns `grounded` only by a span/citation, else it joins failure memory and binds the next generation.

## Invariants this command upholds

- Exo candidates are written `hypothesized`/`unverified`, in the separate lane, never as grounded fact.
- Generation is never gatekept by a metric (the inversion); `/kg-ground` is the post-hoc filter.
- Failure memory binds even imported structure: a candidate colliding with a known failure is dropped.
- This command never calls `kg_ground` and never forges a verdict or a span.
