# The WRITE / BOUNDARY contract (reference)

The precise specification the extractor and grounders bind to. This mirrors the engine source
(`scripts/kg_engine/boundary.py`, `model.py`) and `ARCHITECTURE.md`. When the prose here and the engine
ever disagree, the engine wins — grep `scripts/kg_engine/` rather than guessing.

`mcp__plugin_creativity-graph_creativity-graph__kg_write(payload)` is the ONLY path from language into the canon. It writes nothing
until `validate_payload` (§1.8) classifies every node and edge into a **disposition**. This file is the spec
for that payload and that classification.

Before extraction, the §1.9 **egress scrub** `mcp__plugin_creativity-graph_creativity-graph__kg_scrub(text=None) ->
{scrubbed, redactions, sensitivity, categories}` redacts secrets (always) + PII (per `sensitivity`) with
**consistent** placeholders (`⟦SECRET:1⟧` etc.) so relational structure survives, then hands the scrubbed
text to the subagent. `kg_write` **restores** the placeholder spans to the ORIGINAL text before writing, so
the boundary stores the restored original span in the canon (the span still verifies against the unscrubbed
source, §5). For a no-PII source the scrub is a no-op (`redactions=0`). The full tool surface adds the
generative layer's tools on top of the original eleven: `kg_ping`, `kg_scrub`, `kg_write`, **`kg_propose`**
(§5a), `kg_ground`, `kg_rename`, `kg_metrics`, `kg_status`, `query_graph`, `get_node`, `get_neighbors`, `shortest_path`,
`kg_context`, `kg_agenda` (read-only structural agenda), `kg_export` (read-only human-facing render) (plus
`kg_generate`, `kg_operate`, `kg_absorption` — see the generative-layer references) — eighteen in all.

---

## 1. The write payload (Pydantic `WritePayload`, `extra="forbid"`)

Unknown keys at any level are a schema error → the whole payload is `REJECTED/schema-invalid`. Emit exactly
these fields, no more.

```jsonc
{
  "nodes": [ /* NodeIn */ ],
  "edges": [ /* EdgeIn */ ],
  "complete": true            // terminal-payload flag; see §1.3
}
```

### 1.1 `NodeIn` fields

| field            | type            | default          | notes |
|------------------|-----------------|------------------|-------|
| `id`             | str \| null     | slug of `label`  | optional; if absent, derived via `slug(label)` (lowercase, non-word chars → `-`, then runs of space/`_`/`-` collapsed to a single `-`) |
| `label`          | str             | **required**     | human-readable name |
| `node_type`      | str             | `undeclared-type`| MUST be a declared pack `node_types` value or it quarantines (§3) |
| `file_type`      | str             | `prose`          | `prose` \| `code` \| `sql` \| … (used by projector/probe) |
| `provenance`     | enum            | `span-present`   | `span-present` \| `inferred` \| `hypothesized` |
| `authored_by`    | enum            | `agent`          | `deterministic` \| `agent` \| `human` — never emit `human` (§4) |
| `epistemic_state`| enum            | `unverified`     | never emit a verdict (§4) |
| `body`           | str             | `""`             | the node's definition; may restate cited spans |

### 1.2 `EdgeIn` fields

| field             | type           | default      | notes |
|-------------------|----------------|--------------|-------|
| `source`          | str            | **required** | source **node id** (slug) |
| `target`          | str            | **required** | target **node id** (slug); may reference a not-yet-created node |
| `relation`        | str            | **required** | MUST be a declared pack `edge_types` value or it quarantines (§3) |
| `provenance`      | enum           | `inferred`   | `span-present` \| `inferred` \| `hypothesized` |
| `authored_by`     | enum           | `agent`      | extractors emit `agent`; never `human` (§4) |
| `epistemic_state` | enum           | `unverified` | never emit a verdict (§4) |
| `span`            | str            | `""`         | **VERBATIM** substring of the source — the §1.5 check. See §5 |
| `source_file`     | str            | `""`         | **load-bearing (R4):** the basename of the file the span came from (e.g. `"source.md"`). With a multi-file source the span is verified against THIS file specifically; omit it for a single-source build (verifies against any declared source). See §5 |
| `confidence`      | enum           | `INFERRED`   | `EXTRACTED` \| `INFERRED` \| `AMBIGUOUS` (graphify tier, read by `f4_probe`) |
| `confidence_score`| float \| null  | `null`       | |
| `notes`           | str            | `""`         | |

There is no `id` on `EdgeIn`. The boundary derives it (§6). There is no `verdict_by`/`verdict_at` on the
write contract — those are stamped only by `kg_ground`.

Node/edge creation: the boundary **auto-creates a placeholder source node** if an edge's `source` is absent
from `nodes[]`. Targets may dangle (reference a node created in a later write).

### 1.3 `complete`

`complete: true` marks a **terminal** payload. A streaming extractor sets `false` on non-final chunks.
A payload that is `false` or omits `complete` (the field defaults to `true`, but an extractor that *should*
be terminal and sends `false`) is treated as a **transport truncation** → whole payload `REJECTED/truncated-payload`,
**no partial write**. Always send `complete: true` on the final, whole payload.

---

## 2. The three axes (§1.3) — orthogonal; never collapse to one scalar

| axis              | values |
|-------------------|--------|
| `provenance`      | `span-present` \| `inferred` \| `hypothesized` |
| `authored_by`     | `deterministic` (parser, no LLM) \| `agent` (subagent) \| `human` (only via a person's verdict) |
| `epistemic_state` | `unverified` \| `grounded` \| `rejected` \| `failed` \| `obsolete` |

These are independent. A span-present, agent-authored edge can be unverified; grounding moves only
`epistemic_state`, and only via `kg_ground`.

---

## 3. Dispositions — `validate_payload` returns one `ValidationResult` per item

`kg_write` returns `{dispositions, details[], written_nodes[], rolled_back, error}` where `dispositions`
is the count per bucket and each `details[]` entry carries `{kind, id, disposition, reason, retryable}`.

### ACCEPTED
Valid; span verifies (§5); type declared (§3.5). Written to canon with `epistemic_state=unverified`.
`reason=""`.

### DEMOTED
Written, but one axis was downgraded. Reasons (joined by `;` when several fire):
- `forged-verdict-stripped` — payload set `epistemic_state` to any non-`unverified` state (a verdict
  `grounded`/`rejected`/`failed`, **or** `obsolete`); reset to `unverified` (§4).
- `human-claim-stripped` — payload set `authored_by=human`; reset to `agent` (§4).
- `deterministic-claim-stripped` — payload set `authored_by=deterministic`; reset to `agent`. Only the
  in-process parser is deterministic; a write can't self-declare it to skip the span-present check (§5),
  so the edge then needs a verifying span like any agent edge.

(`ARCHITECTURE.md` also lists a span-present→inferred provenance demotion at the boundary; the agent
extractor should not rely on it — claim only what the span supports.)

### QUARANTINED
Structurally valid but untrusted; not merged into trusted canon, routed to the `undeclared-type` bucket.
Reasons:
- `undeclared-node-type` — `node_type` not in the pack's `node_types`.
- `undeclared-edge-type` — `relation` not in the pack's `edge_types`.
- `collapses-into-known-failure` — a re-emitted edge whose canonical id already lives in `FAILURE_STATES`
  (`rejected`/`failed`); on the hypothesized lane its **reverse** counts too. Failure memory binds
  re-extraction (§1.7).
- `collapses-into-known-verdict` — a text-claim re-emit of an edge already `grounded`/`obsolete`; held so
  the canon's incoming-wins merge can't reset the verdict to a fresh `unverified` edge on an idempotent
  `/kg-build` re-run (§1.8).

The reconciler also **re-quarantines** any out-of-band `epistemic_state` transition (a forged verdict
edited straight into canon, bypassing `kg_ground`).

### REJECTED — not written
| reason                | retryable | meaning |
|-----------------------|-----------|---------|
| `no-supporting-span`  | false     | non-deterministic edge had empty/whitespace `span` (§5) |
| `span-not-in-source`  | false     | `span` does not verify against **any** declared source — **fabrication** (§5) |
| `span-not-in-named-source` | false | `span` is in the corpus but **not** in the file named by `source_file` — mis-attributed (R4, §5). Fix `source_file` to the file the span is really from |
| `span-too-short`      | false     | `span` found but shorter than 4 non-whitespace chars — degenerate anchor (§5) |
| `truncated-payload`   | true      | `complete` was false (§1.3) — transport failure, whole payload dropped |
| `schema-invalid: N errors` | true | Pydantic rejected the shape (extra/missing/mistyped field) |
| `rate-limited-flood`  | false     | net-new writable **edges or nodes** past the per-payload budget `max(64, kb·20)` — anti-injection cap (§Stage 9) |

`retryable=false` ⇒ **semantic** failure: do not resend the same item; fix the span or drop the edge.
`retryable=true` ⇒ **transport** failure: re-emit the corrected/whole payload.

### 3.5 Composite reasons & `deduped`
Reasons stack with `;`. Order seen in `_validate_edge`: verdict/authorship demotions first (verdict reset,
`human`/`deterministic` → `agent`), then span checks (which can short-circuit to REJECTED), then
undeclared-type, then dedup. A `deduped` marker is appended
**only** when the edge is otherwise ACCEPTED or DEMOTED and its identity (§6) already exists — the
single-canonical-edge rule (§1.4) updates the existing edge rather than creating a duplicate. A QUARANTINED
or REJECTED edge is never tagged `deduped`.

---

## 4. never-forge-a-verdict (§1.4 / §1.8) — hard invariant

A `kg_write` payload may **not** assert `grounded`/`rejected`/`failed` (in `epistemic_state`) nor
`authored_by=human`. The boundary does not error — it **silently demotes** (DEMOTED, §3), so a forged
verdict is wasted, not honored.

Verdicts come **only** from
`mcp__plugin_creativity-graph_creativity-graph__kg_ground(target_id, verdict, kind, note, support_span, support_note)`
with `verdict ∈ {grounded, rejected, failed, obsolete}`, which stamps `verdict_by`/`verdict_at` and appends an
audit record. The reconciler re-quarantines any verdict that appears in canon without a matching audit
record. **Extractors emit `unverified` only.** Promoting a **hypothesized** edge to `grounded` *requires*
support, which upgrades its provenance: `support_span` (a verbatim source substring → `span-present`) or
`support_note` (an external citation → `inferred`); without either the promotion is refused with
`hypothesis-needs-support` (§5a).

---

## 5. span-present enforcement (§1.5) — the anti-nonsense gate

Every **non-deterministic** (agent-authored) edge MUST carry a non-empty `span` that verifies against the
**original** source text. Verification (`span_verifies`) is a **normalized substring** test:
- whitespace collapsed, leading/trailing trimmed;
- case-folded (case-insensitive);
- curly quotes/dashes folded to ASCII (`'`/`"`/`-`) and non-breaking space → space.

So you do not need to match exact casing or whitespace, but you **must not paraphrase, summarize, or
reorder** — the span has to be a literal contiguous run of source words. Copy it straight out of
`source.md`.

- empty / whitespace-only `span` → `REJECTED/no-supporting-span` (not retryable).
- present but not found in **any** declared source → `REJECTED/span-not-in-source` (not retryable — fabrication).
- present in the corpus but **not** in the file named by `source_file` → `REJECTED/span-not-in-named-source` (not retryable — mis-attributed; R4). Set `source_file` to the basename of the file the span is really from (omit it to verify against any declared source).
- present and found but fewer than 4 non-whitespace characters → `REJECTED/span-too-short` (not retryable — a degenerate 1-char anchor cites nothing).

Verification is always against the ORIGINAL (unscrubbed) source. When the subagent saw scrubbed text (§1.9
`kg_scrub`), it emits the placeholder span; `kg_write` restores it to the original via the scrub mapping
before the substring check, and the canon stores the restored original span.

A `kg_write` payload claiming `authored_by=deterministic` does **not** skip span-present: on the text-claim
lane the boundary **DEMOTES** it to `agent` (`deterministic-claim-stripped`, §3), so the edge then needs a
verifying span like any agent edge — only the *in-process parser* is truly deterministic, and a parser-exact
edge never travels through a write payload to begin with. (On the `hypothesized` lane there is no span check
to bypass, so a deterministic *discovery mechanism* keeps `deterministic`, §5a.) Agents never emit
`deterministic`.

The grounders apply a further semantic test the boundary cannot: an edge whose span is technically present
but is "true" only because it is generic/unfalsifiable (the **generality confound**, §1.6) should be
rejected via `kg_ground(verdict="rejected")` with reason `vague`. The adversarial grounder records refuted
claims with `attacked_by` edges + `kg_ground(verdict="failed")`; failed/rejected edges are **negative
information** (§1.7), never pruned, surfaced by `kg_context` as `falsification_counters`.

---

## 5a. The three provenance lanes & the propose lane (PLAN Stage 1)

`provenance` selects which lane an item travels through the boundary:

| lane | provenance | span | who writes it | boundary behaviour |
|------|-----------|------|---------------|--------------------|
| **text claim** | `span-present` | **required**, verbatim | extractor / grounder via `kg_write` | full §5 span-present enforcement |
| **text claim** | `inferred` | **required**, verbatim | extractor via `kg_write` | full §5 span-present enforcement |
| **proposal** | `hypothesized` | **none** (ignored, stored empty) | a discovery mechanism via `kg_propose` | no span check; failure-collapse quarantine |

A `hypothesized` item is a **proposal from a discovery mechanism** (a structural/embedding adjacency), never
a text claim. The boundary accepts it **without a span**: any `span` supplied is ignored and stored empty (the
simpler of the two documented paths — there is no `hypothesized-with-span` rejection). Every other guarantee
still binds the lane:

- **never-forge-a-verdict** — a hypothesized item arriving with any non-`unverified` `epistemic_state` is
  `DEMOTED` (`forged-verdict-stripped`), exactly like a text claim. Promotion flows ONLY through `kg_ground`.
- **authorship** — the hypothesized lane has no span-present check to bypass, so a deterministic *discovery
  mechanism* may legitimately author a candidate: `authored_by=deterministic` is **preserved** here (it is
  demoted on the text-claim lanes). `authored_by=human` is still forgeable and is demoted to `agent`.
- **pack vocabulary** — an off-pack `relation`/`node_type` still `QUARANTINED/undeclared-*-type`.
- **failure memory binds generation (invariant 5, §13)** — a hypothesized edge whose canonical identity
  **or its reverse** already lives in `FAILURE_STATES` (`rejected`/`failed`) is `QUARANTINED` with reason
  `collapses-into-known-failure`: a claim that collapses into a known failure is rejected on sight, never
  merged into trusted canon.

`mcp__plugin_creativity-graph_creativity-graph__kg_propose(payload)` is a thin, explicit alias over `kg_write`
that keeps the lanes legible at the call site: it forces every item to `provenance=hypothesized`, and any item
that arrives **explicitly** claiming `span-present`/`inferred` is REFUSED with reason `propose-lane-text-claim`
(text claims belong on `kg_write`). The accepted items then transit the SAME `validate_payload`, so the rules
above apply uniformly. The return adds `{propose_lane: true, refused_text_claims: N}` to the `kg_write` shape.
**Generate offensively; judge defensively (PLAN §1.2):** `kg_propose` never gates on a quality metric — every
candidate is written `hypothesized/unverified`; the grounding loop is the post-hoc filter.

---

## 6. Edge identity (§1.4)

```
edge.id = e_{slug(source)}__{slug(relation)}__{slug(target)}
```

Identity is the triple `(source, relation, target)`. A second ACCEPTED/DEMOTED edge with the same identity
updates the existing one (`deduped`, §3.5) — never a duplicate. `slug()` lowercases, maps non-word
characters to `-`, and collapses runs of spaces/`_`/`-` into a single `-`.

Example: an edge `{source: "generality-confound", relation: "attacked_by", target: "specificity"}` has id
`e_generality-confound__attacked-by__specificity` (`slug` collapses the `_` in `attacked_by` to `-`).

---

## 7. Declared vocabulary (`pack/pack.yaml`)

Anything outside these lists QUARANTINES as undeclared-type (§3). Use these in extraction.

- **node_types:** `compression`, `primitive`, `claim`, `metric`, `operation`, `failure`
- **edge_types:** `grounds`, `attacked_by`, `reconciles_with`, `bridges`, `collapses_into`, `confounded_by`,
  `approximates`, `defends_against`, `projects`, `survives`

---

## 8. Canon note format (§ "Canon note format") — what a written node looks like on disk

One Markdown file per **node**, `<node_id>.md`, under `${CLAUDE_PROJECT_DIR}/canon/`. YAML frontmatter +
free body. Directed **edges** live in the source node's `edges:` block. `derived/` contains nothing the canon
does not — the canon is the single human-editable source of truth.

```markdown
---
id: generality-confound
label: "Generality confound"
node_type: compression          # pack node_types; unknown -> undeclared-type
file_type: prose                # prose | code | sql | ...
provenance: span-present        # span-present | inferred | hypothesized
authored_by: agent              # deterministic | agent | human
epistemic_state: unverified     # unverified | grounded | rejected | failed | obsolete
created_at: "2026-06-20T..."
updated_at: "2026-06-20T..."
edges:
  - id: e_generality-confound__attacked-by__specificity
    target: specificity
    relation: attacked_by
    provenance: span-present
    authored_by: agent
    epistemic_state: unverified
    span: "Generality is therefore *attacked_by* specificity"
    source_file: "source.md"
    confidence: INFERRED        # EXTRACTED | INFERRED | AMBIGUOUS
    confidence_score: 0.6       # float | null
    verdict_by: null            # human | agent | null — set ONLY by kg_ground
    verdict_at: null
    notes: ""
---
Body prose (the node's definition). May restate cited spans.
```

`verdict_by`/`verdict_at` are absent from the write contract (§1) and `null` until `kg_ground` stamps them.

---

## 9. A correct payload (grounded in `examples/source.md`)

Source sentence (verbatim — `examples/source.md` wraps relation words in markdown emphasis, which
`normalize_text` does NOT strip): *"...is therefore \*attacked_by\* specificity — a more specific claim, when
it holds, defeats a vaguer one that merely overlaps it. A compression that survives specific attack is said
to \*grounds\* the claims beneath it."*

```jsonc
{
  "nodes": [
    {"id": "generality-confound", "label": "Generality confound", "node_type": "compression",
     "file_type": "prose", "provenance": "span-present", "authored_by": "agent",
     "epistemic_state": "unverified",
     "body": "The failure mode where a vague idea accumulates spurious connections."},
    {"id": "specificity", "label": "Specificity", "node_type": "primitive",
     "provenance": "span-present", "authored_by": "agent", "epistemic_state": "unverified", "body": ""}
  ],
  "edges": [
    {"source": "generality-confound", "target": "specificity", "relation": "attacked_by",
     "provenance": "span-present", "authored_by": "agent", "epistemic_state": "unverified",
     "span": "Generality is therefore *attacked_by* specificity",
     "source_file": "source.md", "confidence": "INFERRED", "confidence_score": 0.6, "notes": ""}
  ],
  "complete": true
}
```

Expected: both nodes ACCEPTED; the edge ACCEPTED (span verifies; `attacked_by` declared). All land at
`epistemic_state=unverified`. Grounding happens later via `kg_ground`.

### Anti-patterns (what each does)
- `"epistemic_state": "grounded"` on the edge → DEMOTED `forged-verdict-stripped`, reset to `unverified` (§4).
- `"authored_by": "human"` → DEMOTED `human-claim-stripped`, reset to `agent` (§4).
- `"span": "specificity beats generality"` (paraphrase, not in source) → REJECTED `span-not-in-source` (§5).
- missing `span` on an agent edge → REJECTED `no-supporting-span` (§5).
- `"relation": "refutes"` (not in `edge_types`) → QUARANTINED `undeclared-edge-type` (§3).
- `"weight": 0.9` (extra key) → REJECTED `schema-invalid` for the whole payload (§1).
- `"complete": false` → REJECTED `truncated-payload`, nothing written (§1.3).
