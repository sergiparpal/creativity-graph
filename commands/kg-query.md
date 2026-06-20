---
description: Answer a question against the knowledge graph with provenance and falsification counters attached.
argument-hint: <question>
allowed-tools: mcp__creativity-graph__kg_context, mcp__creativity-graph__query_graph, mcp__creativity-graph__get_node, mcp__creativity-graph__get_neighbors, mcp__creativity-graph__shortest_path
---

You are answering a question **against the grounded knowledge graph**, not against your own prior
knowledge. The graph is the human-editable canon projected into a derived index; every edge carries three
orthogonal axes (provenance / authored_by / epistemic_state) and the engine never prunes its failures. Your
job is to answer the question while **faithfully surfacing the grounding state of every claim you lean on**.

Question: **$ARGUMENTS**

## What you may NOT do
- Do **not** present a hypothesized or inferred edge as fact. An edge is only a *fact in the canon* when its
  `epistemic_state` is `grounded`. Everything else is a candidate, a downgrade, or negative information.
- Do **not** present the structural-bridge advisory as a centrality truth. It is a degree-based heuristic and
  is **confounded_by** the generality confound (§1.6) — a vague node sits on many paths for empty reasons.
- Do **not** invent edges, nodes, spans, or verdicts. If the graph does not contain the answer, say so and
  point at what *is* there.

## Procedure

### 1. Get grounded, token-budgeted context
Call `mcp__creativity-graph__kg_context(query=$ARGUMENTS)`. This is the primary source. It returns:
- `items[]` — edges ordered **grounded → span-present → inferred**, each carrying
  `{id, source, target, relation, provenance, authored_by, epistemic_state, span, confidence, confidence_score}`.
- `approx_tokens`, `budget` — the fill is capped (default budget 2000); note if you were truncated.
- `falsification_counters: {failed_or_rejected_edges}` — the **memory of failures** (§1.7). NEVER omit this.
- `advisory: {signal: "structural-bridge", note: "advisory heuristic, not a guarantee", nodes: [{id, label, degree, bridge_communities}]}`.

If `query` matches nothing, the items list may be empty even though the graph is non-empty — fall back to the
structural lookups below before concluding "not in the graph."

### 2. Structural lookups (only when the question is structural)
Use these to follow specific relationships rather than to re-rank context:
- `mcp__creativity-graph__query_graph(node_type=…, relation=…, epistemic_state=…, limit=…)` — e.g.
  `query_graph(epistemic_state="grounded")` for the trustworthy subgraph, or
  `query_graph(relation="attacked_by")` to enumerate adversarial edges. Node types are
  `compression | primitive | claim | metric | operation | failure`; relations are
  `grounds | attacked_by | reconciles_with | bridges | collapses_into | confounded_by | approximates | defends_against | projects | survives`.
- `mcp__creativity-graph__get_node(node_id)` — one node plus its incident edges (use slug IDs, e.g. `compression`).
- `mcp__creativity-graph__get_neighbors(node_id, relation=…)` — incident edges, optionally one relation.
- `mcp__creativity-graph__shortest_path(source, target)` — `{path: [node_ids] | null}` over the derived graph.
  A path is a *structural* connection only; it is **not** evidence the chain of claims is grounded.

### 3. Answer with provenance attached
For every edge you cite, surface its axes inline. A compact convention:

> `compression --grounds--> claim`  **[span-present / grounded]** — "*A compression that survives specific
> attack is said to*"

Rules:
- The bracket is always `[<provenance> / <epistemic_state>]`. Quote the `span` verbatim when the edge has one
  (never paraphrase a span — it is a literal substring of `examples/source.md`, §1.5).
- Lead with `grounded` edges. Flag `unverified` edges as **candidates, not facts**. Flag `inferred` /
  `hypothesized` provenance explicitly. If `authored_by` is `agent`, it has not yet been confirmed by a verdict.

### 4. Always report the falsification counters
Close with the negative information, even if the count is zero:

> **Falsification counters:** `failed_or_rejected_edges = <n>` — rejected/failed edges are kept as negative
> information (§1.7) and are never pruned. A non-zero count means the graph remembers claims it refuted.

If the question touches a node that has incident `failed`/`rejected` edges (check via `get_node` /
`query_graph(epistemic_state="failed")`), name them — a claim that `collapses_into` a known failure is
rejected on sight (§1.7).

### 5. Present the structural-bridge advisory — clearly LABELLED
If `advisory.nodes` is non-empty and relevant, present it under an explicit heading, never as fact:

> **Advisory (heuristic, NOT a guarantee):** structural-bridge signal flags `betweenness`, `compression`
> (by degree). Per the engine: "*advisory heuristic, not a guarantee*." Degree is the honest MVP proxy that
> `approximates` importance; specificity-weighted betweenness is **gated until validated** (§1.6), so do not
> treat these as confirmed bridges.

## Worked shape (from examples/source.md)
For a question like *"What grounds a compression?"*:

1. `kg_context(query="grounds compression")` → an item such as
   `{source:"specificity", target:"compression", relation:"attacked_by", provenance:"span-present", epistemic_state:"unverified", span:"a more specific claim, when it holds, defeats a vaguer one"}`.
2. Answer:
   - `specificity --attacked_by--> compression` **[span-present / unverified]** — candidate, not yet a verdict.
   - If a `grounds` edge is present and `grounded`, that is the fact to lead with.
3. **Falsification counters:** `failed_or_rejected_edges = 0`.
4. **Advisory (heuristic, NOT a guarantee):** structural-bridge nodes by degree, if any.

Keep the answer tight and §-referenced. The deliverable is a **grounded** answer: every claim tagged with its
provenance and epistemic state, failures surfaced, and the bridge advisory labelled as a heuristic.
