---
description: Drain the grounding queue — verdict unverified edges, attack hub candidates, then report verdict counts and falsification memory.
argument-hint: "[query-or-node-filter]"
allowed-tools: Task, mcp__plugin_creativity-graph_creativity-graph__kg_metrics, mcp__plugin_creativity-graph_creativity-graph__kg_context, mcp__plugin_creativity-graph_creativity-graph__query_graph
---

# /kg-ground — grounding loop + adversarial grounder + memory of failures (§1.6/§1.7/§1.8, plan Stage 6)

Candidate edges are written `unverified` by `/kg-build`. This command turns them into *grounded knowledge*:
weak edges are rejected, genuinely falsified ones are remembered as negative information, and vague hubs get
attacked. Verdicts are the canonical grounding state and the ONLY way to set one is `kg_ground` (§1.4/§1.8) —
this command never forges a verdict; it dispatches the two grounder subagents and then *reports*.

Optional `$ARGUMENTS`: a focus query / node filter (e.g. `betweenness`, `compression`). Empty = whole queue.

## Stage 0 — Survey the queue (read-only)

Find what is actually pending before spending any subagent budget.

1. `mcp__plugin_creativity-graph_creativity-graph__kg_metrics()` → record the baseline `edges_by_epistemic_state` map (keys are the
   `epistemic_state` values: `unverified | grounded | rejected | failed | obsolete`). The count under
   `unverified` is the grounding queue depth.
2. `mcp__plugin_creativity-graph_creativity-graph__query_graph(epistemic_state="unverified", limit=50)` → the concrete edge backlog
   the first grounder must verdict. If `$ARGUMENTS` is non-empty, also call
   `mcp__plugin_creativity-graph_creativity-graph__kg_context(query="$ARGUMENTS")` and read its `advisory` block —
   `advisory.signal == "structural-bridge"`, `advisory.note`, `advisory.nodes[]` — to learn which nodes the
   structural-bridge heuristic flags as bridges (the honest advisory is degree; specificity-weighted
   betweenness stays GATED until validated, §1.6). Those `advisory.nodes[]` plus any high-degree nodes are
   the **hub candidates** for Stage 2.

If the `unverified` count is 0 and there are no hub candidates, stop and report "queue empty — nothing to
ground". Otherwise proceed.

## Stage 1 — Verdict the unverified edges (kg-grounder)

Launch the `kg-grounder` subagent via the Task tool to drain the `unverified` queue. It walks each pending
edge, re-checks the `span` is a verbatim substring of `examples/source.md`, confirms the `relation` is
specific (NOT "true" only because it is generic/unfalsifiable — that is the generality confound, §1.6, whose
verdict reason is `vague`), and calls `kg_ground(target_id=<edge_id>, verdict=...)` for each.

```
Task(
  subagent_type: "kg-grounder",
  description: "Verdict unverified edges",
  prompt: """
    Drain the grounding queue. For every edge currently in epistemic_state=unverified
    (focus filter: $ARGUMENTS — empty means the whole queue), decide a verdict and apply it
    via kg_ground(target_id=<edge_id>, verdict=<grounded|rejected>, note=<one-line reason>):
      - grounded : the span verifies verbatim against examples/source.md AND the relation is
                   specific and falsifiable (a more specific claim could have defeated it).
      - rejected : no supporting span, span not in source (fabrication), OR the edge is "true"
                   only because it is vague/unfalsifiable (the generality confound, §1.6) —
                   use note reason `vague` for the latter.
    Do NOT write verdicts into any kg_write payload (§1.4: forged verdicts are stripped/DEMOTED);
    kg_ground is the only verdict channel. Report a per-edge table: edge_id, verdict, reason.
  """
)
```

The kg-grounder owns `kg_ground`; this command does not call it. Verdicts persist in the canon and survive a
full reproject because the reconciler re-attaches them (§1.8).

## Stage 2 — Attack the hubs (kg-adversarial-grounder)

After the first grounder returns, launch the `kg-adversarial-grounder` subagent via the Task tool over the
hub candidates from Stage 0. For each hub it constructs the strongest counter-edges and falsifying questions,
emits them as typed `attacked_by` edges (the declared edge type — see `pack/pack.yaml`), and marks any edge
it genuinely falsifies as `failed`. This is bounded by a per-run cap so a vague hub cannot spawn unbounded
attacks.

```
Task(
  subagent_type: "kg-adversarial-grounder",
  description: "Attack hub candidates",
  prompt: """
    Hub candidates (from kg_context advisory.nodes[] + high-degree nodes): <list from Stage 0>.
    For each hub, generate the strongest falsifying counter-edges:
      1. Write attacked_by counter-edges (relation: attacked_by) via the extraction/kg_write path,
         each carrying a VERBATIM span from examples/source.md (§1.5 — no span => REJECTED).
         Example from the source: generality-confound is attacked_by specificity
         (span: "a more specific claim, when it holds, defeats a vaguer one").
      2. For any existing edge your counter genuinely refutes, call
         kg_ground(target_id=<edge_id>, verdict="failed", note=<what refuted it>).
    Respect the per-run attack cap. A `failed` edge is negative information (§1.7): never pruned,
    surfaced later as a falsification counter. Report: counter-edges written, edges failed.
  """
)
```

`failed` and `rejected` edges are NEGATIVE INFORMATION (§1.7) — never pruned, and surfaced in `kg_context` as
falsification counters. That is the whole point of the memory-of-failures layer: falsification grounds trust
as much as confirmation (source §4).

## Stage 3 — Report (read-only)

Both subagents have applied verdicts. Re-read the engine and present the delta — do NOT re-verdict anything.

1. `mcp__plugin_creativity-graph_creativity-graph__kg_metrics()` → show the new `edges_by_epistemic_state`. Print a before/after diff
   against the Stage 0 baseline for `unverified` (should drop), `grounded`, `rejected`, and `failed`.
2. `mcp__plugin_creativity-graph_creativity-graph__kg_context(query="$ARGUMENTS")` → read `falsification_counters` and report
   `falsification_counters.failed_or_rejected_edges` as the size of the negative-information memory. Also note
   `approx_tokens` vs `budget` if context was requested for downstream use.

Render a summary like:

```
Grounding pass complete.
  queue drained : unverified  N → M
  verdicts      : grounded +g, rejected +r, failed +f
  memory        : falsification_counters.failed_or_rejected_edges = K  (never pruned, §1.7)
  hubs attacked : <hub list> → C attacked_by counter-edges
```

## §Stage 6 — Merge checkpoint (NON-BLOCKING)

If either grounder reports a genuinely ambiguous node merge (two nodes that may be the same concept), surface
exactly ONE elicitation and do not block the pass:

```
Merge A and B? [y/N]
```

Default is **N** (keep the nodes separate) after a brief wait — proceed without merging unless the human
explicitly answers `y`. (Merging, if accepted, is a `kg_rename`/reconcile concern handled outside this
read-and-dispatch command.)

## Invariants this command upholds

- It NEVER calls `kg_ground` itself and NEVER forges a verdict in a write payload (§1.4/§1.8). Verdicts come
  only from the two grounder subagents via `kg_ground`.
- It uses only the declared vocabulary: edge type `attacked_by`, `epistemic_state` values
  `unverified|grounded|rejected|failed`, verdict reason `vague` for generality-confound rejections (§1.6).
- Rejected and failed edges are kept forever and reported as `falsification_counters.failed_or_rejected_edges`
  (§1.7); this command surfaces that count rather than pruning anything.
