---
description: Generate hypothesized idea candidates from the graph's structure (bridge|seed|compression|regroup|transplant|ensemble|periphery), phrase them via the language layer, and write them to the hypothesized lane — then hand off to /kg-ground as the filter.
argument-hint: "[mechanism-set] [k]"
allowed-tools: Task, Bash, mcp__plugin_sproutgraph_sproutgraph__kg_generate, mcp__plugin_sproutgraph_sproutgraph__kg_propose, mcp__plugin_sproutgraph_sproutgraph__kg_operate, mcp__plugin_sproutgraph_sproutgraph__kg_context, mcp__plugin_sproutgraph_sproutgraph__kg_metrics, mcp__plugin_sproutgraph_sproutgraph__kg_absorption
---

# /kg-generate — the generative layer (PLAN Stages 3–6)

This is the *generation* half of the system. It turns the grounded graph from a **verification machine**
into an **idea-generation machine** — without weakening one anti-nonsense guarantee. The whole design
rests on **the inversion** (PLAN §1.2): **generate offensively, judge defensively.** Entry into the graph
is NEVER gatekept by a quality metric — every candidate is written `hypothesized`/`unverified`. The
*existing* grounding loop (`/kg-ground`) is the filter, applied **after**. The portico that used to stand
at the door of imagination is moved to after generation.

A generated candidate enters as `provenance=hypothesized`, `epistemic_state=unverified`, **with no span**.
It is a *proposal from a discovery mechanism*, never a text claim — stored in a lane that can never be
mistaken for grounded content. Promotion to `grounded` happens ONLY through `kg_ground`, and only when a
grounder supplies a span or citation (which *upgrades* its provenance) — see `/kg-query`/`/kg-ground`.

`$ARGUMENTS` (optional): a mechanism set (`default` | `all` | a single mechanism name) and an integer `k`.

## Step 0 — confirm a graph exists

Call `mcp__plugin_sproutgraph_sproutgraph__kg_context(budget=2000)`. If `items[]` **and**
`advisory.bridge_metric.nodes[]` are both empty, there is no graph to generate from — tell the user to run
`/kg-build` → `/kg-ground` first, and stop. Otherwise note `advisory.bridge_metric.gate_on` (whether
specificity-weighted betweenness is the trusted bridge signal this projection) and carry it into the report.

## Step 1 — mechanism set (NON-BLOCKING survey, §"Autonomy contract")

Ask the user exactly once, then proceed with the default if they don't redirect:

> **Run the default mechanism set `{bridge, seed, compression}` or all seven? [default/all]** (default: `default`)

- `default` (or no answer, or `$1` empty) → the three default mechanisms.
- `all` → all seven (`bridge, seed, compression, regroup, transplant, ensemble, periphery`). `periphery` (§5)
  sources candidates from the graph's **low-degree** nodes — the periphery the hub-seeking mechanisms ignore.
- a single mechanism name → just that one.

This is a **non-blocking checkpoint**: invite a reply, but never make progress conditional on it. Default
`k=10` unless `$2` gives an integer.

## Step 2 — generate the structural candidates (read-only)

Call `mcp__plugin_sproutgraph_sproutgraph__kg_generate(mechanism=<chosen>, k=<k>)` (use
`mechanism="all"` for the full set; it runs the default set on `"default"`). It returns
`{mechanism, k, gate_on, count, candidates[], note}`. Each candidate carries
`{kind, mechanism, source, target, relation, label, node_type, score, specificity, rationale, section, convergence}`.
`convergence` is **advisory** — the number of *distinct* mechanisms that independently proposed the same edge
(≥1). It is a **ranking prior for the grounding queue** (which hypotheses to ground first), **never** a score,
**never** a verdict, and **never** written onto a canon edge; it does not change the slate's `score`/ranking.
This call is **READ-ONLY** — nothing is written yet. If `count==0`, report that the structure surfaced no
candidates for the chosen mechanisms (the demo graph is small; try `all`, a larger `k`, or `/kg-perturb`),
and stop. Note the `note` (e.g. ensemble degraded to regroup with no second construction).

## Step 3 — phrase & label the candidates (Task → kg-generator)

Launch the **kg-generator** subagent via the **Task** tool, handing it the candidate list. It is the
LANGUAGE layer: it phrases each candidate as a one-sentence idea, supplies a `label`+`body` for proposed
`compression`/`primitive` nodes (whose label is blank by design), and writes the transplant "hidden
commitments to audit" notes (§5). It does **not** invent structure and does **not** set verdicts.

```
Task(subagent_type: "kg-generator", description: "Phrase + label candidates", prompt: """
  Here are the structural candidates from kg_generate: <paste candidates[]>.
  For each: (a) phrase it as one falsifiable sentence keeping source/target/relation verbatim;
  (b) for kind:"node" (compression/primitive) supply a SPECIFIC label + a one-paragraph body whose
  prediction would earn the node its keep (§7); (c) for transplants, write the hidden-commitments-to-audit
  note (§5). Then assemble ONE kg_propose payload (provenance=hypothesized, NO span, NO verdict) and call
  kg_propose. Report dispositions + the phrased idea slate. Nothing is judged — these are hypotheses.
""")
```

The kg-generator owns `kg_propose`; it writes the hypothesized lane. (For purely structural ops —
collapse/explode/regroup/open — you may instead call
`mcp__plugin_sproutgraph_sproutgraph__kg_operate` directly; it also routes through the propose lane.)

## Step 4 — record the generation (for the absorption window, §14)

After the propose call returns, append a generation record to the absorption ledger so `kg_absorption`
can later score how long each new node stays perturbing (fertile vs absorbed vs isolated):

```bash
DERIVED="${CLAUDE_PLUGIN_DATA:-.kg-data}/derived"; mkdir -p "$DERIVED"
GEN="$DERIVED/generations.json"
# best-effort: bump the generation counter and add each newly-proposed node id with its current degree
# (read from kg_context advisory or query_graph). Shape: {"generation": N, "tracked": {id: {introduced_at, introduced_degree, mechanism}}}
[ -f "$GEN" ] || printf '{"generation":0,"tracked":{}}\n' > "$GEN"
# (merge new entries with the chosen mechanism; introduced_at = the new generation counter)
```

This step is best-effort — never block the slate on it.

## Step 5 — emit the ranked idea slate + report metrics

Present a ranked **idea slate** to the user, one row per accepted candidate:

```
| # | mechanism | § | idea (one sentence) | specificity | provenance |
|---|-----------|---|---------------------|-------------|------------|
| 1 | seed      | §3| Betweenness and memory-of-failures may be abnormally connectable … | 1.83 | hypothesized |
```

Then call `mcp__plugin_sproutgraph_sproutgraph__kg_metrics` and report the new
`edges_by_epistemic_state` — the hypothesized candidates show up under `unverified`. State `gate_on` from
Step 0 (was the slate ranked by the confound-corrected `spec_betweenness` or the honest fallback?).

## Step 6 — the inversion, made explicit

State plainly: **nothing has been judged yet.** Every candidate is `hypothesized`/`unverified`. `/kg-ground`
now acts as the *filter* over the hypothesized lane — a grounder promotes a candidate to `grounded` ONLY by
supplying a span or external citation (which upgrades its provenance from `hypothesized` to
`span-present`/`inferred`), and rejects the rest into failure memory (§1.7), which then *binds* the next
generation (a candidate that collapses into a known failure is dropped on sight, invariant 5).

Optionally chain into `/kg-ground` restricted to the freshly-proposed hypothesized lane
(`epistemic_state=unverified, provenance=hypothesized`).

## Invariants this command upholds

- **The hypothesized lane is separate, never privileged.** Candidates are written `hypothesized`, never
  presented as grounded, never carrying a span.
- **Generation is never gatekept by a quality metric** (the inversion) — it always emits and writes; only
  `/kg-ground` filters, afterward.
- **Generality control travels with every mechanism** (§4): structural rankings are spec-weighted, the
  gate decides whether `spec_betweenness` is trusted, compressions pass an MDL screen.
- **Failure memory binds generation** (§13): the engine drops any candidate whose identity (or its
  reverse) already lives in `failed`/`rejected`.
- This command **never** calls `kg_ground` and **never** forges a verdict or a span.
