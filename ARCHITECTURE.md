# Architecture & shared contract — `kg_engine`

The single source of truth for the data model, the boundary semantics, and every module's public API.
Tests and implementations both bind to this.

## Canon note format

One Markdown file per **node**, named `<node_id>.md`, living under the canon vault
(`${KG_PROJECT_DIR}/canon/`). YAML frontmatter + free body. Directed **edges** live in the source node's
`edges:` block.

```markdown
---
id: thermo-arrow
label: "Thermodynamic arrow of time"
node_type: compression          # from the pack; unknown types -> 'undeclared-type'
file_type: prose                # prose | code | sql | ...   (for projector/probe)
provenance: span-present        # span-present | inferred | hypothesized
authored_by: agent              # deterministic | agent | human
epistemic_state: unverified     # unverified | grounded | rejected | failed | obsolete
created_at: "2026-06-20T..."
updated_at: "2026-06-20T..."
edges:
  - id: e_thermo-arrow__grounds__entropy
    target: entropy
    relation: grounds
    provenance: span-present
    authored_by: agent
    epistemic_state: unverified
    span: "the arrow of time is grounded in the increase of entropy"
    source_file: "source.md"
    confidence: INFERRED        # EXTRACTED | INFERRED | AMBIGUOUS  (graphify tier, used by f4_probe)
    confidence_score: 0.62      # float | null
    verdict_by: null            # human | agent | null  (only set via kg_ground)
    verdict_at: null
    notes: ""
---
Body prose (the node's definition). May restate cited spans.
```

## The three axes (§1.3) — orthogonal, never collapsed to one scalar

- `provenance`: `span-present` | `inferred` | `hypothesized`
- `authored_by`: `deterministic` | `agent` | `human`
- `epistemic_state`: `unverified` | `grounded` | `rejected` | `failed` | `obsolete`

## Edge identity & single-canonical-edge rule (deterministic tier, §1.4)

Identity = `(source_id, relation, target_id)`. The boundary deduplicates: a second accepted edge with the
same identity updates the existing one, never creates a duplicate. `edge.id` is derived deterministically:
`e_{source}__{relation}__{target}` (slugged).

## Boundary dispositions (§1.8) — `validate()` returns one per item

- `ACCEPTED`  — valid; span verifies; type declared. Written to canon, `epistemic_state=unverified`.
- `DEMOTED`   — written, but a claimed axis is downgraded. Cases: claimed `authored_by=human` → demote to
  `agent` (a write payload may not forge a human verdict); claimed `authored_by=deterministic` → demote to
  `agent` on the span-present/inferred lane (a parser-authored, span-exempt edge would bypass §1.5) but
  **preserved** on the hypothesized lane (no span check to bypass — a discovery mechanism may legitimately
  author a candidate); payload set `epistemic_state` to any non-`unverified` state (a verdict **or**
  `obsolete`) → reset to `unverified` (those flow only through `kg_ground`).
- `QUARANTINED` — structurally valid but untrusted; not merged into trusted canon. Cases: undeclared
  node/edge type (routed to the `undeclared-type` bucket, never silently accepted); reconciler-detected
  out-of-band epistemic_state transition (forged verdict re-quarantined).
- `REJECTED`  — hard fail, not written. Cases: no supporting span (`no-supporting-span`); span not found in
  source (`span-not-in-source`, fabrication); degenerate/too-short span (`span-too-short`); truncated/partial payload; schema-invalid.

`retryable=false` for **semantic** rejections (no-span, span-not-in-source, vague); `retryable=true` for
**transport** failures (truncation, schema). Reason string always set.

## span-present enforcement (§1.5, the anti-nonsense invariant)

- An `authored_by=deterministic` edge is span-present by construction (parser-exact) **only when it comes
  from the in-process parser**. A *write payload* cannot self-declare `deterministic` to skip the span
  check: the boundary demotes that claim to `agent`, so the edge then needs a verifying span like any
  other. (Span-present must be unreachable-around, not opt-out.) **Exception — the hypothesized lane:** a
  `hypothesized` candidate carries no span and so has no span-present check to bypass, so a deterministic
  *discovery mechanism* may legitimately author it — the boundary **preserves** `deterministic` there and
  demotes only the (still-forgeable) `human` claim to `agent`.
- Every agent edge MUST carry a non-empty `span`. Missing → `REJECTED/no-supporting-span`.
- The span must verify against the **original** source text (whitespace-normalized, case-insensitive
  substring). Restore scrubber placeholders before verifying. Not found → `REJECTED/span-not-in-source`.

## Never-forge-a-verdict (§1.4, §1.8)

A write payload may not assert a verdict. `epistemic_state ∈ {grounded,rejected,failed}` in a write →
demoted to `unverified` (every lane, including hypothesized). `authored_by=human` in a write → demoted to
`agent`. A claimed `authored_by=deterministic` is demoted to `agent` on the span-present/inferred lane (it
would bypass the span check); on the **hypothesized** lane there is no span check to bypass, so a
deterministic discovery-mechanism author is **preserved** there. Verdicts are applied ONLY through
`kg_ground`, which stamps `verdict_by`, `verdict_at`, and appends an audit record. The reconciler
re-quarantines any out-of-band epistemic_state transition that lacks a matching audit record.

## Memory of failures (§1.7)

`epistemic_state ∈ {rejected,failed}` edges are negative information: never pruned by the projector,
surfaced in `kg_context` as falsification counters.

## Derived layer (§1.2) — "contains nothing the canon does not"

`projector.py`: canon → NetworkX node-link `graph.json` + SQLite index. Leiden communities (igraph +
leidenalg, with a label-propagation fallback if unavailable). Precomputed ranks: local **degree** (cheap
advisory) and a labelled **structural-bridge** signal (node whose neighbors span ≥2 Leiden communities,
§1.4/§1.6). Incremental reproject keyed by a **per-node content hash** of (frontmatter + body): a node
whose hash changed is re-emitted; staleness (`is_stale`) uses a cheap (file-count + newest-mtime) pre-gate and, when that moves, an
authoritative per-node content-hash comparison, so an uncommitted change — a `kg_ground` verdict, a hand
edit, or a non-git vault — still reprojects.

## Module public API (imports: `from kg_engine import ...`)

- `model`: enums `Provenance, AuthoredBy, EpistemicState, Disposition, Confidence`; dataclasses `Node`,
  `Edge`; `edge_id(src,rel,tgt)`; `normalize_text(s)`; `span_verifies(span, source_text) -> bool`;
  frontmatter (de)serialization `node_to_markdown`/`node_from_markdown` (+ `Node.frontmatter()`).
- `boundary`: pydantic `EdgeIn, NodeIn, WritePayload`; `Disposition` result `ValidationResult(disposition, kind, item, reason, retryable, identity)`; `validate_payload(payload, *, pack, source_text, existing) -> list[result]`.
- `canon`: `Canon(vault_dir)` with `read_node`, `write_nodes(nodes, *, message)` (atomic + git rollback),
  `all_nodes`, `all_edges`; `LeaseLock(path, ttl)` with `acquire/heartbeat/release/is_stale`.
- `reconciler`: `Reconciler(canon, state_path)` with `scan(full_sweep=False) -> ReconcileReport`;
  `reattach_after_reproject(graph_json) -> OrphanReport`.
- `scrub`: `Scrubber(sensitivity)` with `scrub(text) -> (scrubbed, mapping)`; `restore(text, mapping)`.
- `pack`: pydantic `PackContract`; `load_pack(path) -> PackContract`; `coverage(pack, source_text) -> dict`.
- `projector`: `Projector(canon, derived_dir)` with `project(incremental=True) -> ProjectReport`;
  `kg_context(query=None, budget=2000) -> dict`.
- `harness`: `agreement(label_sets) -> alpha`; `specificity(graph, corpus) -> verdict`;
  `ideation(outputs_by_condition) -> table`.
- `server`: `KGEngine` facade wrapping the above + FastMCP tool registration — all 15 tools: `kg_ping`,
  `kg_scrub`, `query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `kg_context`, `kg_write`,
  `kg_ground`, `kg_rename`, `kg_metrics`, plus the four generative-layer tools `kg_propose`,
  `kg_generate`, `kg_operate`, `kg_absorption`.

All filesystem state goes under `${KG_DATA}` (derived, caches, locks may live with canon under
`${KG_PROJECT_DIR}`); `${CLAUDE_PLUGIN_ROOT}` is read-only bundled code.
