# Implementation Plan — The Generative Layer for `creativity-graph`

**Target repo:** `sergiparpal/creativity-graph` (plugin version `0.2.1`, engine `kg-engine` 0.2.1)
**Audience:** an autonomous coding agent (Claude Code).
**Deliverable:** the *generation* half of the system — the mechanisms from the source theory
(`examples/source.md` / "Conclusiones v6") that turn the existing grounded graph from a
verification machine into an idea-generation machine, **without** weakening any of the existing
anti-nonsense guarantees.

This document is self-contained. Do not request additional design documents. Execute the stages in
order. Each stage ends with a machine-checkable **Definition of Done (DoD)**; when the DoD passes,
**proceed to the next stage automatically** — do not pause for human sign-off. The only permitted
human touchpoints are the **non-blocking surveys** described in §"Autonomy contract", which have
defaults and must never block execution for more than a brief moment.

---

## 0. Orientation — what already exists (do not rebuild it)

Read these before writing code; the plan references them by exact name.

- **Three axes** (`scripts/kg_engine/model.py`): `Provenance ∈ {span-present, inferred,
  hypothesized}`, `AuthoredBy ∈ {deterministic, agent, human}`, `EpistemicState ∈ {unverified,
  grounded, rejected, failed, obsolete}`. Constants `VERDICT_STATES`, `GROUNDABLE_STATES`,
  `FAILURE_STATES`. **`Provenance.HYPOTHESIZED` already exists** and is documented as "proposed by a
  discovery mechanism (structural/embedding adjacency)" — but nothing in the codebase ever emits it.
  This plan is, in large part, *finishing the lane the model already declared.*
- **Write boundary** (`scripts/kg_engine/boundary.py`, `validate_payload`): enforces the
  span-present invariant for non-deterministic edges; returns one `Disposition ∈ {ACCEPTED, DEMOTED,
  QUARANTINED, REJECTED}` per item; demotes forged verdicts.
- **Projector** (`scripts/kg_engine/projector.py`): canon → NetworkX `MultiDiGraph` + SQLite index.
  `_ranks()` computes Leiden communities, **degree**, and **bridge_communities** (→
  `structural_bridge=1` when a node's neighbours span ≥2 communities). It does **not** compute
  betweenness. `kg_context()` reads precomputed columns only (no centrality in-request) and already
  surfaces an `advisory.structural-bridge` block. SQLite `nodes` columns today: `id, label,
  node_type, file_type, provenance, authored_by, epistemic_state, degree, community,
  bridge_communities, structural_bridge`.
- **Harness** (`scripts/kg_engine/harness.py`): `idf_seeds()`, `_node_specificity()`, and
  `specificity(graph_data, corpus)` — which already computes **specificity-weighted betweenness vs
  raw betweenness** and returns a `gate_on` verdict. This is a *measurement* today; nothing wires its
  result back into the live graph. `ideation()` scores `control|graph|rag`.
- **Server / tool surface** (`scripts/kg_engine/server.py`, `KGEngine` + `_register`): 11 MCP tools
  (`kg_ping, kg_scrub, kg_write, kg_ground, kg_rename, kg_metrics, query_graph, get_node,
  get_neighbors, shortest_path, kg_context`). `kg_ground` is the **only** verdict path.
- **Pack** (`pack/pack.yaml`): node_types `compression, primitive, claim, metric, operation,
  failure`; edge_types `grounds, attacked_by, reconciles_with, bridges, collapses_into,
  confounded_by, approximates, defends_against, projects, survives`; `specificity_seeds` (IDF hints).
- **Commands** (`commands/`): `kg-build, kg-ground, kg-query, kg-eval, kg-experiment`. **No
  `kg-generate`.**
- **Agents** (`agents/`): `extractor, grounder, adversarial-grounder, annotator, evaluator`. **No
  `generator`.**
- **Tests** (`tests/`, ~140 green): fixtures in `conftest.py` — `vault` (git-backed temp canon),
  `engine`, `pack`, `source_path`, helpers `make_node`, `make_edge`.

### How to run (dev environment)

```bash
# Resolve the runner once per shell. Prefer uv; fall back to the dev venv.
PY="$(command -v uv >/dev/null 2>&1 && echo 'uv run python' || echo '/home/sergi/creativity-graph/.venv/bin/python')"
SCRIPTS="$(pwd)/scripts"

uv sync                                    # provision deps (idempotent)
PYTHONPATH="$SCRIPTS" $PY -m pytest tests/ -q     # full suite — must be green before Stage 1
claude plugin validate --strict            # manifest + component validation
```

When a stage says "run the suite", it means `PYTHONPATH="$SCRIPTS" $PY -m pytest tests/ -q`.
When it says "validate manifests", it means `claude plugin validate --strict`.

---

## 1. The design contract — why generation does NOT reintroduce "astrology"

The original system was built defensively: every edge must earn a verdict against a verbatim span,
and failures are kept forever. The risk in adding generation is that span-less, machine-proposed
edges become indistinguishable from grounded knowledge — exactly the "beautiful machine for
producing astrologies" the source warns about (§10). The following invariants make generation safe,
and **every stage's DoD re-asserts them**:

1. **The hypothesized lane is separate, not privileged.** A generated candidate enters as
   `provenance=hypothesized`, `epistemic_state=unverified`, **with no span**. It is a *proposal from
   a discovery mechanism*, never a text claim. It is stored, queried, and displayed in a channel
   that can never be mistaken for grounded content.

2. **Generate offensively; judge defensively (the inversion).** Entry into the graph is **never**
   gatekept by a quality metric — `/kg-generate` always emits candidates and writes them as
   hypothesized. The *existing* grounding loop (`kg_ground`, `kg-grounder`,
   `kg-adversarial-grounder`) is the filter, applied **after**. The portico that used to stand at the
   door of imagination is moved to after generation.

3. **Promotion requires support.** A hypothesized edge can become `grounded` **only** through
   `kg_ground`, and only when a grounder supplies a span or an external citation — which **upgrades
   its provenance** from `hypothesized` to `span-present`/`inferred`. If no support is found it is
   `rejected` and joins failure memory. A hypothesized edge is never groundable in place without
   support (this preserves the never-forge-a-verdict rule).

4. **Generality control travels with every generator** (§4 "generality is the universal confound").
   Structural rankings used for generation are specificity-weighted; compression candidates must pass
   an MDL screen (§7); no generator may rank a candidate highly merely because it is generic.

5. **Failure memory binds generation** (§13). Before emitting a candidate, a generator drops any
   candidate edge whose `(source, relation, target)` (or its reverse) already exists in
   `FAILURE_STATES`. A claim that collapses into a known failure is rejected on sight.

6. **Reflexive budget** (§16). Generation is a first-class allocation, not something gated to death.
   "Too much falsification kills generation; too little produces astrologies." Concretely:
   `/kg-generate` and grounding are *separate* steps, and generation is never skipped because the
   graph is "not clean enough yet."

---

## 2. Autonomy contract (no blocking human verification)

- **Stage gates are machine-checked.** Each stage's DoD is a `pytest` selection plus a CLI smoke
  command with an asserted result. When both pass, continue without asking.
- **Non-blocking surveys only.** Where a choice genuinely benefits from the user (e.g. which
  mechanism set to run), ask **once** using a single short question with a stated default, then
  **proceed with the default** if there is no immediate answer. Pattern (reuse the style already in
  `commands/kg-experiment.md`, Step 1):
  > **Run the default mechanism set `{bridge, seed, compression}` or all six? [default/all]**
  > (default: `default`)
  Never re-ask, never wait indefinitely, never make progress conditional on the answer.
- **On a failing DoD**, the agent fixes forward within the same stage (it does not escalate to the
  user). Only if a stage is blocked by something outside the repo (missing network, missing
  `ANTHROPIC_API_KEY` for the optional headless path) does it surface a one-line note and continue
  with the parts that do not need it.
- **Commit per stage** on a feature branch `feat/generative-layer` with a message naming the stage.

---

## 3. Stages

> Each stage lists: **Goal · Files · Work · DoD**. Write tests in the same stage as the code.

### Stage 0 — Branch, baseline, scaffolding

**Goal:** a green baseline and empty module/test scaffolding so later stages only add behaviour.

**Files:** new `scripts/kg_engine/generate.py` (stub), new `tests/test_generate.py` (stub),
new `tests/test_hypothesized.py` (stub), branch `feat/generative-layer`.

**Work:**
1. `git checkout -b feat/generative-layer`.
2. Run the full suite; record the green baseline count. If it is not green on a clean checkout, stop
   and fix the environment (this is the only stage allowed to surface an environment problem).
3. Create `generate.py` with a module docstring stating it holds **deterministic** candidate
   generators that read the derived graph + source + pack and emit `hypothesized` candidates, plus a
   typed `Candidate` dataclass:
   ```python
   @dataclass
   class Candidate:
       kind: str            # "edge" | "node"
       mechanism: str       # "bridge" | "seed" | "compression" | "regroup" | "transplant" | "ensemble"
       source: str = ""     # for edges
       target: str = ""     # for edges
       relation: str = ""   # for edges (a pack edge_type)
       label: str = ""      # for nodes (e.g. a proposed compression)
       node_type: str = ""  # for nodes (a pack node_type)
       score: float = 0.0
       specificity: float = 0.0
       rationale: str = ""
       section: str = ""    # the source-theory § the mechanism implements
       # provenance is always hypothesized; epistemic_state always unverified; no span.
   ```
4. Add empty test files importing the module so collection succeeds.

**DoD:**
- `PYTHONPATH="$SCRIPTS" $PY -m pytest tests/ -q` green (baseline count unchanged + the new empty
  files collected).
- `PYTHONPATH="$SCRIPTS" $PY -c "from kg_engine.generate import Candidate; print('ok')"` prints `ok`.

---

### Stage 1 — The hypothesized write lane (engine + boundary)

**Goal:** let the boundary accept span-less `hypothesized` items as a distinct, clearly-flagged lane,
while keeping every existing guarantee for `span-present`/`inferred` items intact.

**Files:** `scripts/kg_engine/boundary.py`, `scripts/kg_engine/model.py` (only if a helper is
needed), `scripts/kg_engine/server.py`, `tests/test_hypothesized.py`,
`skills/creativity-graph/references/contract.md`.

**Work:**
1. In `validate_payload`, branch on `provenance`:
   - `span-present` / `inferred`: **unchanged** — the span invariant still applies exactly as today.
   - `hypothesized`: **accept without a span**. Force `epistemic_state=unverified` (a hypothesized
     item that arrives with any `GROUNDABLE_STATE` is `DEMOTED`, reusing the existing
     never-forge-a-verdict path and reason). Preserve `authored_by` (`agent` or `deterministic`).
     Require that `relation`/`node_type` is in the pack (off-vocabulary → `QUARANTINED`, unchanged).
   - Add a new rejection reason `hypothesized-with-span` **only if** you choose to forbid spans on
     hypothesized items; simpler and acceptable: ignore any span on a hypothesized item and store it
     empty. Pick the simpler path and document it in `contract.md`.
2. Apply **invariant 5** here: in the boundary (or a thin helper it calls) drop a hypothesized edge
   whose identity or reverse identity is already in `FAILURE_STATES` among `existing`. Surface it as
   `QUARANTINED` with reason `collapses-into-known-failure`.
3. Add a dedicated write entry point. Prefer extending `kg_write` to accept hypothesized items in the
   same payload (the boundary already discriminates per item), and add a **server tool** `kg_propose`
   as a thin, explicit alias that sets a payload-level expectation of hypothesized provenance and
   refuses any `span-present`/`inferred` item with reason `propose-lane-text-claim` (so the two lanes
   stay legible at the call site). Register it in `_register` and in `KGEngine`.
4. Update `references/contract.md`: document the three-lane provenance semantics and the propose lane.

**DoD (`tests/test_hypothesized.py`):**
- A hypothesized edge **without** a span is `ACCEPTED`, stored with `provenance=hypothesized`,
  `epistemic_state=unverified`, empty span.
- A hypothesized edge arriving with `epistemic_state=grounded` is `DEMOTED` to `unverified`.
- A `span-present` edge without a span is still `REJECTED/no-supporting-span` (no regression).
- A hypothesized edge matching a `failed`/`rejected` identity is `QUARANTINED/collapses-into-known-failure`.
- `kg_propose` rejects a `span-present` item with `propose-lane-text-claim`.
- Full suite green; manifests validate.

---

### Stage 2 — Precompute betweenness + specificity-weighted betweenness (un-gate §2/§4)

**Goal:** complete the partially-implemented bridge metric. Move it from a harness-only *measurement*
to a precomputed, **auto-gated** rank the generators can read O(1).

**Files:** `scripts/kg_engine/projector.py`, `scripts/kg_engine/harness.py` (import its
`idf_seeds`/`_node_specificity`, do not duplicate), `tests/test_projector.py`.

**Work:**
1. Add SQLite `nodes` columns: `betweenness REAL, spec_betweenness REAL, specificity REAL,
   gate_on INTEGER`. Bump the schema and the `_write_full`/`_write_incremental` row writers and the
   `_node_row` tuple accordingly. (Adding columns means a full rebuild path; ensure
   `do_full` triggers when the prior schema lacks the columns — detect missing columns and force a
   rebuild.)
2. In `_ranks()`, after communities/degree/bridges, compute off the hot path:
   - `betweenness = nx.betweenness_centrality(undirected)`.
   - `specificity[n] = _node_specificity(label[n], idf_seeds(corpus), default)` where `corpus` is the
     source split into sections (read once from `self.canon`/source; if the projector lacks the
     source text, accept a `corpus` argument threaded from `KGEngine.source_text()` split on `\n## `).
   - `spec_betweenness[n] = betweenness[n] * specificity[n]`.
3. Compute the **gate** once per projection by calling `harness.specificity(graph_data, corpus)` and
   store `gate_on` (1/0) in `meta` and on each node row. The gate decides whether
   `spec_betweenness` is *trusted for ranking*; both raw and weighted values are always stored.
4. Surface in `kg_context`: extend the existing `advisory` block to add a `bridge_metric` entry
   `{gate_on, ranked_by: "spec_betweenness" if gate_on else "structural_bridge", nodes:[...top by the
   trusted signal...]}`. Keep the `structural-bridge` advisory for backward compatibility.

**DoD (`tests/test_projector.py`):**
- After projecting a fixture graph, `nodes` rows carry finite `betweenness`, `spec_betweenness`,
  `specificity`, and a 0/1 `gate_on`.
- A vague high-traffic node has high `betweenness` but lower `spec_betweenness` than a specific bridge
  (the confound is visibly corrected).
- Smoke: build the demo (`examples/source.md`), then
  `kg_context(budget=2000)["advisory"]["bridge_metric"]` returns a non-empty `nodes` list.
- Full suite green.

---

### Stage 3 — `generate.py` deterministic generators + `kg_generate` tool

**Goal:** the core generation engine. Pure, deterministic functions over the derived graph that emit
ranked `Candidate`s, each tagged with the source-theory § it realises. Read-only: generators do **not**
write the canon (that is Stage 6's command, via the propose lane).

**Files:** `scripts/kg_engine/generate.py`, `scripts/kg_engine/server.py`, `tests/test_generate.py`.

**Work — implement each mechanism as a function `def <name>(G, *, pack, corpus, failures, k) ->
list[Candidate]`:**

1. **`bridges` (§2/§4 — generate from the bridges).** Rank non-adjacent node pairs that lie across
   communities by a bridging score using precomputed `spec_betweenness` of the endpoints and their
   community distance; propose a `bridges` edge between the top pairs. Respects the gate: if
   `gate_on=0`, fall back to `structural_bridge`-flagged endpoints. This is Swanson's
   literature-based discovery realised structurally.

2. **`seed` (§3 — the residual, not the product).** For each candidate pair compute graph distance
   `d` (shortest-path length over the derived graph) and a connectability proxy `c` (e.g. common
   neighbours / Adamic–Adar). Fit the expected `c` as a monotone-decreasing function of `d` over all
   sampled pairs, and score each pair by the **positive residual** `c - E[c | d]` — "abnormally
   connectable for its distance." Emit the top residual pairs as candidate `bridges` edges. Do **not**
   multiply `d × c` (the source explicitly rejects that as double-counting one tension).

3. **`compression` (§7 — new nodes, not new edges).** Detect dense subgraphs (e.g. communities or
   k-core clusters) whose members share many typed relations, and propose a new `compression` node
   that `collapses_into`-links the members. Apply an **MDL screen**: only emit the compression if a
   crude description-length estimate (bits to encode the subgraph's edges) *drops* when the members
   are re-expressed via the single compression node; reject "vague" compressions whose
   `specificity` is below the corpus mean. The label is left blank for the language layer (Stage 6)
   to name.

4. **`regroup` (§8 — re-partition surfaces invisible bridges).** Re-run Leiden at a *different*
   resolution (or with a different seed / objective) and diff the community assignment against the
   stored one; any pair that becomes cross-community under the new partition but was intra-community
   before is a candidate `bridges` edge that "was invisible under the prior partition." This is the
   generative use of the freedom of resolution.

5. **`transplant` (§5 — hubs as macro-bridges).** Take a high-degree hub from community A and propose
   importing its *reorganising pattern* into community B that lacks it: emit candidate edges mirroring
   the hub's outgoing relation profile onto B's nodes, tagged with a `rationale` that names the hub's
   **hidden commitments** to audit (the language layer expands this in Stage 6). Mark direction:
   transfer is asymmetric, so prefer transplanting *into* the community with higher absorption
   capacity (proxy: higher mean specificity / lower density), and flag the reverse as risky.

6. **`ensemble` (§9 — exo: cross constructions).** Given an optional second derived graph (a second
   pack or a re-extraction at a different resolution, supplied by `/kg-perturb` in Stage 7), emit
   candidate edges that exist in one construction's structure but not the other's — the bridges the
   graph's *own* dynamics would resist. With only one construction available, this degrades to
   `regroup`.

**Cross-cutting rules every generator obeys:**
- Output `provenance=hypothesized`, `epistemic_state=unverified`, no span.
- Drop any candidate in `failures` (invariant 5).
- Carry `specificity` and a one-line `rationale`; set `section` to the § implemented.
- Deterministic ordering (stable sort by `score`, tie-break by id) so runs are reproducible.

**Tool:** register `kg_generate(mechanism: str = "bridge", k: int = 10, second_graph: str = None) ->
dict` in `KGEngine` + `_register`. It projects if stale, reads precomputed ranks, dispatches to the
generator(s) (`mechanism="all"` runs the default set), and returns
`{mechanism, candidates:[...], gate_on, note}`. **Read-only** — it never writes the canon.

**DoD (`tests/test_generate.py`):**
- Each mechanism returns ≥1 well-formed `Candidate` on a fixture graph, all `hypothesized`, none with
  a span, none colliding with a seeded `failed` edge.
- `seed` ranks an "abnormally connectable for its distance" pair above a "trivially close" pair.
- `compression` rejects a low-specificity cluster and accepts a high-specificity one (MDL screen).
- `regroup` surfaces a pair that is intra-community at the default resolution and cross-community at
  another.
- Smoke: `kg_generate(mechanism="all", k=5)` over the demo returns candidates for each mechanism.
- Full suite green; manifests validate.

---

### Stage 4 — The four endo operations (§8) as canon-mutating ops

**Goal:** expose `open / collapse / explode / regroup` as operations that *write* the canon through
the propose lane, turning the read-only candidates of Stage 3 into persisted hypothesized structure.

**Files:** `scripts/kg_engine/generate.py` (operation wrappers) or a new
`scripts/kg_engine/operations.py`, `scripts/kg_engine/server.py`, `tests/test_operations.py`.

**Work:** implement, each producing a hypothesized payload routed through `kg_propose`:
- **collapse(subgraph) → compression node** (`collapses_into` edges from members to the new node).
- **explode(node) → subgraph** (propose the latent sub-structure of a node as hypothesized children).
- **regroup()** persists the §8 re-partition's newly-visible bridges from Stage 3's `regroup`.
- **open() → primitive** proposes a `primitive` node opening territory the current vocabulary cannot
  express (language layer names it in Stage 6); the structural part proposes the attachment points.
Register one tool `kg_operate(op: str, target: str = None, ...) -> dict`.

**DoD (`tests/test_operations.py`):**
- `collapse` over a fixture cluster writes a `compression` node + `collapses_into` edges, all
  hypothesized/unverified, and a subsequent `kg_metrics` reflects them.
- `explode` is the inverse shape (node → hypothesized children).
- Operations never set a verdict and never write a span.
- Full suite green.

---

### Stage 5 — Absorption window (§14) — novelty has a half-life

**Goal:** track how long a hypothesized→grounded candidate stays perturbing before the graph
renormalises around it, so the slate can prefer the fertile middle (neither trivially absorbed nor
isolated).

**Files:** `scripts/kg_engine/harness.py` (add `absorption(...)`), `scripts/kg_engine/server.py`,
`tests/test_harness.py`.

**Work:** add `absorption(graph_data, history)` that, given the graph and a record of when candidates
were proposed/grounded (read from the git/audit log timeline or a lightweight `derived/generations.json`
the generate command appends to), scores each grounded-from-hypothesized node on:
- **decay**: how fast its neighbourhood densified after introduction (fast densification ⇒ it became
  trivial — low remaining novelty), and
- **isolation**: whether it stayed disconnected (infertile).
Return per-node `{half_life, status: "fertile"|"absorbed"|"isolated"}`. Expose `kg_absorption()`.

**DoD:**
- `absorption` flags a rapidly-densified node as `absorbed` and a disconnected one as `isolated` on a
  synthetic history fixture.
- Full suite green.

---

### Stage 6 — `/kg-generate` command + `kg-generator` agent (orchestration + language layer)

**Goal:** the user-facing generative workflow, and the language layer that turns structural candidates
into phrased ideas, names compressions/primitives, and audits transplant commitments (§5).

**Files:** new `commands/kg-generate.md`, new `agents/kg-generator.md`,
`skills/creativity-graph/SKILL.md` (add the generate workflow), `README.md`.

**Work:**
1. **`agents/kg-generator.md`** — frontmatter `name: kg-generator`, tools `Read, Grep,
   mcp__plugin_creativity-graph_creativity-graph__kg_generate,
   mcp__plugin_creativity-graph_creativity-graph__kg_context,
   mcp__plugin_creativity-graph_creativity-graph__kg_propose`. Its job is **language only**: given a
   structural `Candidate`, (a) phrase it as a one-sentence idea, (b) for compression/primitive
   candidates supply a `label` and `body`, (c) for transplants, write the "hidden commitments to
   audit" note. It never sets verdicts and never invents structure — the endpoints/mechanism come
   from `kg_generate`.
2. **`commands/kg-generate.md`** — frontmatter `allowed-tools: Task, Bash,
   mcp__...__kg_generate, mcp__...__kg_propose, mcp__...__kg_operate, mcp__...__kg_context,
   mcp__...__kg_metrics`. Procedure:
   - **Step 0**: confirm a graph exists (`kg_context`); if empty, tell the user to run
     `/kg-build` → `/kg-ground` and stop.
   - **Step 1 (non-blocking survey)**: mechanism set + `k` (default `{bridge, seed, compression}`,
     `k=10`).
   - **Step 2**: call `kg_generate` for the chosen mechanisms; collect candidates.
   - **Step 3**: launch `kg-generator` to phrase/label the candidates (language layer).
   - **Step 4**: write them via `kg_propose` (hypothesized lane).
   - **Step 5**: emit a ranked **idea slate** to the user with mechanism, rationale, specificity, and
     the §-of-theory each realises; report `kg_metrics` (note the new hypothesized count).
   - **Step 6 (the inversion, explicit)**: state that nothing has been judged yet, and that
     `/kg-ground` will now act as the *filter* over the hypothesized lane. Optionally chain into
     `/kg-ground` restricted to `epistemic_state=unverified, provenance=hypothesized`.
3. Update `SKILL.md` workflow diagram to: `/kg-build → /kg-ground → /kg-generate → /kg-ground →
   /kg-query`, and note generation is offensive, grounding is the defensive filter.

**DoD:**
- `claude plugin validate --strict` passes (new command + agent manifests well-formed).
- `tests/test_manifests.py` green (extend it to assert the new command/agent are discovered and
  declare only existing tools).
- Smoke (scripted, non-interactive): build demo → `kg_generate(all, k=5)` → `kg_propose` the result →
  `kg_metrics` shows hypothesized edges/nodes present and `unverified`.
- Full suite green.

---

### Stage 7 — `/kg-perturb` command (§15 perturb + §9 exo/ensemble)

**Goal:** grounding's *second* function — perturbation. Import external structure the graph's own
dynamics would resist, and cross-generate against it.

**Files:** new `commands/kg-perturb.md`, `scripts/kg_engine/server.py` (a `kg_ensemble_graph(path)`
helper that loads a second `graph.json`), `tests/test_generate.py` (ensemble path).

**Work:**
- `/kg-perturb [second_source_or_pack]`: build a *second* construction (re-extract the same source
  under a different pack/resolution, or extract a second source), project it to a second `graph.json`,
  then run `kg_generate(mechanism="ensemble", second_graph=<path>)` to emit hypothesized bridges that
  exist across constructions. Tag these candidates `rationale` with `perturbation=external` so the
  slate marks them as imported structure, not internal elaboration.
- Make explicit in the command text that this is the only mechanism that *attacks coverage* (§9) — and
  that it relocates the blind spot rather than eliminating it (ensemble caveat).

**DoD:**
- `kg_generate(mechanism="ensemble", second_graph=<demo-as-its-own-second-graph>)` returns candidates
  and degrades gracefully (to `regroup`) when no second graph is supplied.
- `claude plugin validate --strict` passes; `tests/test_manifests.py` green.
- Full suite green.

---

### Stage 8 — Query/context segregation + grounding upgrades provenance

**Goal:** make the hypothesized lane visibly separate at read time, and make promotion upgrade
provenance — closing the loop so generated ideas can become grounded knowledge *only* by earning it.

**Files:** `scripts/kg_engine/projector.py` (`kg_context`), `scripts/kg_engine/server.py`
(`kg_ground`), `commands/kg-query.md`, `agents/grounder.md`, `tests/test_grounding.py`,
`tests/test_review_sweep.py`.

**Work:**
1. `kg_context`: return a **separate** `hypotheses[]` block (hypothesized, unverified items) distinct
   from grounded `items[]`. Grounded answers must never include hypothesized edges in `items[]`.
2. `/kg-query` + `kg-grounder`: when grounding a `hypothesized` edge, the grounder must supply a span
   (from the source) or an external citation; `kg_ground` then sets the verdict **and** upgrades
   `provenance` (`hypothesized → span-present` if a verbatim span is supplied, else `inferred`). If no
   support is found → `rejected` (joins failure memory). Add a `support_span` / `support_note`
   parameter to `kg_ground` used only on the hypothesized→grounded transition; without it, grounding a
   hypothesized edge to `grounded` is refused with reason `hypothesis-needs-support`.
3. Update `agents/grounder.md` to describe the hypothesized queue and the support requirement.

**DoD (`tests/test_grounding.py`):**
- `kg_context` separates `hypotheses[]` from `items[]`; a hypothesized edge never appears in `items[]`.
- `kg_ground(target, "grounded")` on a hypothesized edge **without** support is refused
  (`hypothesis-needs-support`).
- `kg_ground(target, "grounded", support_span=<verbatim>)` promotes it and sets
  `provenance=span-present`.
- `kg_ground(target, "rejected")` moves a hypothesis into failure memory; a later generator drops the
  same candidate (invariant 5 round-trip).
- Full suite green; manifests validate.

---

### Stage 9 — End-to-end, experiment wiring, release

**Goal:** prove the whole loop and let the experiment measure whether the generative layer actually
helps — answering the project's original question with data.

**Files:** `commands/kg-experiment.md`, `scripts/kg_engine/harness.py` (extend `ideation` conditions),
`CHANGELOG.md`, `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` (version bump),
`README.md`, `PROGRESS.md`.

**Work:**
1. Add a `graph+generate` condition to `/kg-experiment`: same as `graph` but the context pack includes
   the hypothesized slate from `/kg-generate`. `harness.ideation` already scores arbitrary condition
   keys; extend its verdict to compare `graph+generate` vs `graph` vs `control` (diversity + novelty
   up, unsupported_rate not materially up).
2. Write an **e2e smoke script** (non-interactive, committed under `tests/` or `scripts/`):
   build demo → ground → generate(all) → propose → ground the hypotheses (those with support) →
   query → experiment. Assert each step's structured output shape.
3. Bump version `0.2.1 → 0.3.0` in `plugin.json`, `marketplace.json`, `pyproject.toml`; add a
   CHANGELOG `0.3.0` entry titled "The generative layer". Update README's "What it is" to state the
   plugin now *generates* (offensively) and *grounds* (defensively), with the workflow diagram.

**DoD:**
- Full suite green (every prior stage's tests + the e2e smoke).
- `claude plugin validate --strict` passes.
- `kg_ping()` reports version `0.3.0`.
- The e2e smoke runs end-to-end and asserts a non-empty hypothesized slate, at least one promoted
  hypothesis, and a populated `ideation` table including `graph+generate`.

---

## 4. Invariant regression checklist (re-run mentally at the end of every stage)

1. `span-present` / `inferred` edges still require a verbatim span (Stage 1 must not weaken this).
2. No tool other than `kg_ground` can set a `GROUNDABLE_STATE`; hypothesized items demote forged
   verdicts exactly as text items do.
3. Hypothesized edges never appear in grounded `items[]` / `/kg-query` answers; they live in
   `hypotheses[]`.
4. Every generator is generality-controlled (spec-weighting / MDL); no candidate ranks high merely
   for being generic.
5. Failure memory is never pruned and is consulted before emitting candidates.
6. Generation entry is never gatekept by a quality metric; grounding is the post-hoc filter.
7. The derived layer still contains nothing the canon does not; hypothesized structure lives in the
   canon (carrying its hypothesized provenance), not only in `derived/`.

If any check fails, fix it within the current stage before proceeding.

## 5. Out of scope (do not build)

- The §12 temporal triad as an automated scorer (it needs an exogenous "is this problem still alive?"
  signal the repo does not have). Leave a one-paragraph note in `PROGRESS.md` describing how a future
  `kg_generate(mechanism="temporal")` would consume an external freshness feed; do not implement it.
- Embedding-based candidate generation (`metrics_mode=with_embeddings`) — keep `structure_only` as the
  default path; the optional embedding extra was removed in 0.2.1 and is not a dependency here.
