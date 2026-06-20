# Implementation Plan — `creativity-graph` Claude Code plugin

A staged, self-advancing build plan for a Claude Code plugin that turns a non-self-grounding
conceptual document into a rigorously-grounded, queryable knowledge graph. The plan is **self-contained**:
everything needed to build correctly — the conceptual model, the technical requirements, the layout, the
stages, and the evaluation scorer — is in this document. No external file is required.

The plan is written to be executed end-to-end by Claude Code with no blocking human gate between stages:
each stage advances automatically when its **automated exit test** passes. The only human touchpoints are
(a) one-time enable-time configuration prompts and (b) occasional one-keystroke confirmations surfaced in
the CLI that apply a default if unanswered, so execution never stalls for more than a moment.

---

## 1. Design foundations (self-contained)

This section is the complete conceptual model the implementation must honor. It is the reason for every
invariant in the stages below; no other document is needed.

### 1.1 The problem and the stance
The target is a *conceptual* document — dense theory whose claims do not self-ground (unlike code, where a
parse tree is ground truth). A naive extractor over such prose produces plausible-but-unfounded edges; a
graph without grounding becomes a machine for producing convincing nonsense. The plugin is therefore
**infrastructure for grounded conceptual knowledge**, not an idea generator. Idea-generation value
(bridges, serendipity) is a *hypothesis under test*, gated behind validation, never shipped as a guarantee.

### 1.2 Canon vs derived
- **Canon**: human-editable Markdown notes (one per node; relations live in `edges:` blocks and in edge
  notes). The canon is the single source of truth and carries the grounding state. It is versioned in git.
- **Derived**: a regenerable projection — a NetworkX node-link `graph.json` (portable interchange) and a
  SQLite index (fast queries, ranks, optional embeddings). The derived layer is disposable: rebuildable
  from the canon at any time, and it must never hold anything the canon does not.

### 1.3 The three axes (every node and edge carries all three)
- **provenance**: `span-present` (cites a verifiable textual span in the source) | `inferred` (asserted
  without a verbatim span) | `hypothesized` (proposed by a discovery mechanism, e.g. structural or
  embedding adjacency).
- **authored_by**: `deterministic` (produced by a parser, not a language model) | `agent` (produced by a
  subagent) | `human` (a person's verdict).
- **epistemic_state**: `unverified` | `grounded` (passed the grounding loop) | `rejected` | `failed`
  (actively falsified) | `obsolete` (superseded).

The axes are orthogonal: an edge can be span-present yet rejected, or inferred yet grounded. The
combinations carry meaning and must not be collapsed into a single confidence scalar.

### 1.4 The three tiers
- **Deterministic** (hard guarantees): schema validity, span-present enforcement, the single-canonical-edge
  rule, "derived contains nothing the canon does not", and "a human verdict is never forged out-of-band".
- **Advisory** (honest heuristics, never guarantees): indexed local **degree** and a labelled
  **structural-bridge** signal (nodes joining ≥2 communities). Surfaced as hints, clearly labelled.
- **Hypothesis-under-test** (gated, off until validated): specificity-weighted betweenness as the *real*
  bridge metric, and any serendipity/creative query.

### 1.5 span-present (the core anti-nonsense invariant)
Every non-deterministic edge must cite a textual span that verifies against the source. The boundary
rejects edges with no supporting span. File-level provenance is not enough; the verifiable span is the check.

### 1.6 The generality confound (why degree is the MVP rank, not betweenness)
Vague, general nodes accumulate spurious connections and spuriously high betweenness — they look like
important bridges for empty reasons. Raw betweenness is therefore the confounded metric and is gated; the
validated metric weights betweenness by term specificity (IDF over the corpus). Until that is validated,
the cheap, honest advisory is **degree** plus a labelled structural-bridge signal.

### 1.7 The grounding loop and memory of failures
Candidate edges enter a queue; the grounding step verifies the span, checks the relation is specific (not
generic), and assigns a verdict. **Grounded** edges become trusted; **rejected** and **failed** edges are
recorded as non-regenerable *negative information* (memory of failures) — never pruned, and surfaced in
context as falsification counters. A graph that only grows and forgets its failures drifts into nonsense;
the failure memory is what keeps it honest.

### 1.8 Validation-at-two-points (the boundary)
Writes are validated at the MCP boundary (`P_write`). Because an agent or a human can also edit the canon
directly, a **reconciler** re-validates the actual files against the last validated state (`P_reconcile`)
at session start and before any projection. An out-of-band change is detected and re-validated, never
silently trusted; in particular an out-of-band `epistemic_state` transition (a forged verdict) is
re-quarantined. Durability against outright deletion is not an in-app guarantee — it relies on git history
and an off-machine backup.

### 1.9 PII on egress
The source may be private. Before any text is handed to a subagent for semantic work, a deterministic
local scrubber redacts secrets/keys (always) and PII (per a sensitivity setting) using **consistent
placeholders** so relational structure survives (`⟦PERSON:1⟧ relates_to ⟦PERSON:2⟧`), restoring against
the original locally. The scrub protects the egress, not the local canon.

### 1.10 Adopted patterns vs our differentiators
This design borrows proven patterns from graphify (a shipped knowledge-graph tool for codebases) where
they are better and compatible, and keeps our own (possibly unproven) design where that is the bet:

- **Adopt** (extraction / derived / interface layers): deterministic-first staged extraction (parse
  structured inputs with tree-sitter as the strongest provenance tier; language model only for prose);
  content-hash caching to skip unchanged inputs; **NetworkX node-link** `graph.json` as the interchange
  format and its exporters (Obsidian / Neo4j / GraphML); **Leiden** community detection; a concrete MCP
  tool surface (`get_neighbors`, `shortest_path`, …); and a git **union-merge** driver for the *derived*
  artifact.
- **Keep ours** (canon / epistemic layers): the human-editable canon with persistent verdicts (a purely
  derived graph is fine for our derived layer, rejected for the canon); the three axes (vs a single
  confidence tag); the grounding loop + never-forge-a-verdict + quarantine; span-present; the gated
  specificity metric (vs ungated degree ranking); memory of failures; per-domain packs (vs a general
  extractor); and the validation harness.

The seam: borrowed patterns sit in extraction and in the derived/interface layers; the canon and the
epistemic rigor are ours. Nothing in the derived layer touches the human verdicts in the canon.

---

## 2. Technical requirements (Claude Code plugin)

A Claude Code plugin is a self-contained directory whose only required file is the manifest at
`.claude-plugin/plugin.json`. Every other component lives at the **plugin root** (never inside
`.claude-plugin/`). This plugin uses six component types:

| Component | Location | Role here |
|---|---|---|
| Manifest | `.claude-plugin/plugin.json` | name, version, `mcpServers`, `hooks`, `agents`, `userConfig` |
| MCP server | `.mcp.json` | the graph engine (Python) exposing the tool surface |
| Hooks | `hooks/hooks.json` | `SessionStart` env bootstrap + reconcile; `PreToolUse` graph-context injection |
| Agents | `agents/*.md` | extractor, grounder, annotator, adversarial-grounder, evaluator subagents |
| Skill | `skills/creativity-graph/SKILL.md` | top-level orchestration + the build/ground/query workflow |
| Commands | `commands/*.md` | `/kg-build`, `/kg-ground`, `/kg-query`, `/kg-eval`, `/kg-experiment` |
| Scripts | `scripts/` (Python pkg) | boundary, canon, reconciler, projector, scrubber, server, harness |

### 2.1 Runtime and environment
- **Claude Code**, current version (supports `/plugin`, `claude plugin validate`, `--plugin-dir`,
  `--debug`, `/reload-plugins`). Develop locally as a skills-directory plugin: scaffold with
  `claude plugin init creativity-graph --with skills agents hooks mcp`, which creates
  `~/.claude/skills/creativity-graph/` and loads it as `creativity-graph@skills-dir` next session with no
  install step. Iterate, then package for a marketplace at the end.
- **Python 3.10+ managed by `uv`**, installed into the plugin's persistent data directory so it survives
  plugin updates. Bootstrap with a `SessionStart` hook using the diff-the-manifest pattern: compare the
  bundled `pyproject.toml` to a copy in `${CLAUDE_PLUGIN_DATA}` and run `uv sync` only when they differ.
- **Path variables**: reference bundled code with `${CLAUDE_PLUGIN_ROOT}` (the install dir; ephemeral —
  never write state here); put the venv, the SQLite index, and caches under `${CLAUDE_PLUGIN_DATA}`
  (`~/.claude/plugins/data/<id>/`, survives updates). The target project (the canon vault) is
  `${CLAUDE_PROJECT_DIR}`.

### 2.2 The LLM is the session
Semantic extraction and grounding are performed by Claude Code itself (the session and its subagents) — no
API keys are required for in-session operation. The Python scripts handle the deterministic work (parsing,
validation, persistence, metrics); the subagents do the language work and hand structured JSON back through
the boundary. A headless `--backend` path with API keys is added only in the final hardening stage, for CI.

### 2.3 Python dependencies (installed via `uv` into `${CLAUDE_PLUGIN_DATA}`)
`mcp` (the Python MCP SDK / FastMCP, for the server), `pydantic>=2` (the boundary contract), `networkx`
(NetworkX node-link `graph.json` interchange), `python-igraph` + `leidenalg` (Leiden community detection),
`tree_sitter` + language grammars (deterministic extraction of any structured inputs; optional for pure
prose), `sqlite-vss` (vector index in the derived layer; the embedding layer is gated/optional),
`GitPython` or `git` via subprocess (canon atomicity, audit, union-merge), `pytest` (test suites). `uv`
itself is the package/venv manager.

### 2.4 Interfaces the plugin exposes
- **MCP tool surface** (graphify-shaped + our grounding semantics): `query_graph`, `get_node`,
  `get_neighbors`, `shortest_path`, plus `kg_context` (grounding-aware, provenance-carrying,
  token-budgeted), `kg_write` (extraction → boundary), `kg_ground` (apply a verdict), `kg_rename`,
  `kg_metrics`.
- **MCP elicitation**: tools may request a single user input mid-call (the `Elicitation` lifecycle event),
  used only for rare, genuinely ambiguous decisions; every elicitation declares a default applied
  automatically if unanswered, so the flow continues.
- **`userConfig`** (manifest): enable-time prompts for `domain`, `source_path`, `sensitivity`
  (`low|medium|high`, controls scrubbing aggressiveness), and `metrics_mode`
  (`structure_only|with_embeddings`). Values are available as `${user_config.KEY}` and
  `CLAUDE_PLUGIN_OPTION_<KEY>`.

---

## 3. Repository layout

```
creativity-graph/
├── .claude-plugin/
│   └── plugin.json
├── .mcp.json
├── pyproject.toml                 # uv-managed deps for the engine
├── hooks/
│   └── hooks.json                 # SessionStart bootstrap+reconcile; PreToolUse context inject
├── agents/
│   ├── extractor.md
│   ├── grounder.md
│   ├── annotator.md
│   ├── adversarial-grounder.md
│   └── evaluator.md
├── skills/
│   └── creativity-graph/
│       ├── SKILL.md
│       └── references/            # loaded on demand (contract, pack schema, tool docs)
├── commands/
│   ├── kg-build.md
│   ├── kg-ground.md
│   ├── kg-query.md
│   ├── kg-eval.md
│   └── kg-experiment.md
├── scripts/kg_engine/
│   ├── __init__.py
│   ├── boundary.py                # Pydantic contract + validate (span-present, 3-axis, dispositions)
│   ├── canon.py                   # Markdown canon I/O; temp+rename; git-as-rollback; lease lock
│   ├── reconciler.py              # mtime/size pre-filter + full-sweep; re-attach verdicts post-rebuild
│   ├── projector.py               # canon → node-link graph.json + SQLite + Leiden + ranks
│   ├── scrub.py                   # PII/secret egress scrubbing with consistent placeholders
│   ├── pack.py                    # domain pack + glossary loader/validator (PackContract)
│   ├── harness.py                 # specificity harness, annotation agreement, ideation scoring
│   └── server.py                  # the MCP server (tool surface + elicitation)
├── scripts/f4_probe.py            # the extraction scorer — create verbatim from Appendix A
├── tests/
│   ├── test_invariants.py         # property tests for the boundary invariants
│   ├── test_chaos.py              # crash-mid-write, stale-lock, OOB-verdict, fuzzing
│   └── fixtures/
├── pack/
│   ├── pack.yaml                  # the conceptual-theory domain pack (built in Stage 2)
│   └── glossary.md
├── CHANGELOG.md
└── README.md
```

The **canon vault** (the human-editable Markdown graph with three-axis frontmatter) and the derived
artifacts (`graph.json`, the SQLite index, git history) live under `${CLAUDE_PROJECT_DIR}` in the user's
project, not inside the plugin.

---

## 4. Autonomous execution protocol

Claude Code executes the stages in order and advances itself. The protocol is mechanical:

1. **Run the stage.** Build the listed files; implement the listed behavior.
2. **Run the stage's exit test** (a concrete command). It is fully automated and prints pass/fail plus any
   metric.
3. **On pass**, commit (`feat(stageN): <summary>`) and proceed to the next stage immediately. No human
   sign-off.
4. **On fail**, fix and re-run the exit test, up to the stage's stated retry budget. Stages with a measured
   threshold (extraction precision, agreement, the metric harness) carry an **auto-iteration loop**: improve
   the relevant input (the pack, the extraction prompt) and re-measure, up to the stated number of
   iterations, then record the best result and proceed. The plan never halts on a metric; it records the
   outcome and moves on.
5. **Checkpoints** are optional and non-blocking. Where a stage notes a checkpoint, surface a single
   `userConfig` value or one MCP elicitation with a stated default. If the user answers in the CLI, use it;
   if not, apply the default after a brief wait and continue. A checkpoint never gates the exit test.

Keep a running `PROGRESS.md` at the project root: one line per stage with its exit-test result and any
recorded metric, so the run is auditable end-to-end without re-reading code.

---

## 5. Stages

### Stage 0 — Scaffold and environment bootstrap

**Goal.** A loadable plugin with a working Python engine venv.

**Tasks.**
- `claude plugin init creativity-graph --with skills agents hooks mcp`. Fill `plugin.json`: `name`,
  `version` (omit during dev so each commit is a new version; set it for release), `description`, `author`,
  `license`, and the `userConfig` block (`domain`, `source_path`, `sensitivity`, `metrics_mode`).
- Author `pyproject.toml` with the dependencies in §2.3.
- Write the `SessionStart` hook (`hooks/hooks.json`): diff `${CLAUDE_PLUGIN_ROOT}/pyproject.toml` against
  `${CLAUDE_PLUGIN_DATA}/pyproject.toml`; on difference, copy the manifest into `${CLAUDE_PLUGIN_DATA}` and
  run `uv sync`. This installs the venv once and re-syncs only on dependency changes.
- Write `.mcp.json`: one server `creativity-graph` launched as
  `uv run --project "${CLAUDE_PLUGIN_DATA}" python "${CLAUDE_PLUGIN_ROOT}/scripts/kg_engine/server.py"`,
  with `env` setting `KG_PROJECT_DIR=${CLAUDE_PROJECT_DIR}` and `KG_DATA=${CLAUDE_PLUGIN_DATA}`.
- Stub `server.py` with a single `kg_ping` tool returning a version string.

**Exit test.** `claude plugin validate ./creativity-graph --strict` passes; `claude --debug` shows the
plugin loaded and the MCP server initialized; calling `kg_ping` returns the version. Retry budget: 5.

**Checkpoint (non-blocking).** At enable time, `userConfig` prompts for `domain` (default: "conceptual
theory"), `source_path`, `sensitivity` (default `medium`), `metrics_mode` (default `structure_only`).

---

### Stage 1 — Canon + transactional writes + lease lock + reconciler (Core I/O)

**Goal.** A crash-safe, single-writer canonical layer. This is the foundation; everything writes through it.

**Tasks.**
- `canon.py`: read/write Markdown notes with three-axis frontmatter (`provenance`, `authored_by`,
  `epistemic_state`, §1.3) plus `edges:` blocks. Single-file writes are temp-file + atomic `os.rename`.
- Multi-file mutations use git-as-rollback: write all files, then one commit; on any failure
  `git stash push -u` then `git reset --hard HEAD` (stash-before-reset preserves any parallel human edits),
  and surface the stash.
- Reclaimable lease lock (`.kg-session-lock` = `{pid, host, acquired_at, ttl, heartbeat_at}`): acquire if
  absent or stale (dead pid or expired heartbeat); heartbeat during long operations.
- `reconciler.py`: an `mtime`/`size` pre-filter backed by a periodic full re-hash sweep (the pre-filter is
  for performance; the sweep defeats mtime spoofing, §1.8); on an out-of-band change, re-validate through
  the boundary. The reconciler also runs **after any derived-layer rebuild** to re-attach grounding
  verdicts to surviving edges and surface verdicts orphaned by edges that disappeared.

**Exit test.** `pytest tests/test_chaos.py -q` passes: a kill mid-write recovers to a consistent vault via
git; a stale lock is always reclaimed; an out-of-band `epistemic_state` edit is re-quarantined on
reconcile. Retry budget: 8.

---

### Stage 2 — Domain pack and glossary

**Goal.** A pack that teaches the engine the vocabulary and relation types of the conceptual-theory domain,
authored from the source document.

**Tasks.**
- Read the source document at `${user_config.source_path}`. Derive the domain pack: the node types
  (compressions/primitives vs claims vs operations), the edge types the theory actually uses (e.g.
  `attacked_by`, `reconciles_with`, `bridges`, `collapses_into`), the glossary of defined terms, and
  per-term specificity/IDF seeds from term frequency in the corpus.
- `pack.py`: load and validate `pack/pack.yaml` against `PackContract` (a Pydantic model). Node/edge types
  not in the pack are routed to an `undeclared-type` bucket, never silently accepted.

**Exit test.** `python -m kg_engine.pack validate pack/pack.yaml` passes `PackContract`; a coverage check
reports the fraction of the source's defined terms present in the glossary and logs it. Retry budget: 5.

**Checkpoint (non-blocking).** One elicitation: "Pack inferred N node types and M edge types from the
source — proceed? [Y/n]" Default `Y` after a brief wait.

---

### Stage 3 — Staged extractor + boundary + PII scrubber

**Goal.** Source text → validated, grounded candidate edges in the canon, with private data protected on
egress.

**Tasks.**
- `scrub.py`: before any text is handed to a subagent for semantic work, redact secrets/keys (always) and
  PII (per `${user_config.sensitivity}`) using consistent placeholders that preserve relational structure
  (`⟦PERSON:1⟧ attacked_by ⟦PERSON:2⟧`); keep the mapping local; restore against the original for the
  canonical span (§1.9).
- `agents/extractor.md`: a subagent that reads the (scrubbed) source section by section and emits the
  contract JSON — nodes, typed edges, and for each edge a supporting textual span. Deterministic inputs
  (any code/SQL/structured files) are parsed by `tree_sitter` first and emitted as `deterministic`/
  `span-present` edges with no language model; prose goes to the subagent.
- `boundary.py`: strict Pydantic validation. Reject truncated/partial payloads. Enforce span-present (every
  non-deterministic edge cites a span that verifies against the source, §1.5). Tag the three axes. Return
  structured dispositions (`ACCEPTED|DEMOTED|QUARANTINED|REJECTED`, `reason`, `retryable:false` for semantic
  outcomes, §1.8).

**Exit test.** `pytest tests/test_invariants.py -q` passes: fabricated edges, undeclared types, and
span-less edges are rejected/demoted in the fixture set; truncated JSON is rejected with no partial write;
a seeded secret never appears in any text leaving the scrubber. Retry budget: 8.

---

### Stage 4 — Extraction evaluation

**Goal.** A measured precision number for the extractor on this corpus, with the pack auto-tuned to clear
the bar.

**Tasks.**
- Run the extractor over the full source into the canon, then project to `graph.json` (Stage 5's projector
  produces it; for this stage a minimal node-link dump of the canon edges is sufficient if the projector is
  not yet built).
- `agents/annotator.md`: a subagent that takes a sample of the extracted edges and labels each
  `correct | fabricated | vague | wrong_type`, recording whether a supporting span is present, judging each
  edge strictly against the source text. Produce the gold set as the labeled CSV.
- Create `scripts/f4_probe.py` **verbatim from Appendix A**, then score: `summary` to inspect, `sheet` to
  emit the labeling CSV for the annotator to fill, `score` to compute precision, the fabricated+vague rate,
  the span-support rate, precision per relation type, and the confidence-calibration check.

**Exit test.** `python scripts/f4_probe.py score labels.csv` prints precision. If precision ≥ 0.70, proceed.
If below, **auto-iterate**: refine the pack (tighten types, add specificity seeds for the confused terms)
and the extractor prompt, re-extract, re-label, re-score — up to 3 iterations — then record the best
precision in `PROGRESS.md` and proceed regardless. No human gate.

---

### Stage 5 — Projector, derived layer, query surface

**Goal.** A fast queryable projection of the canon, in the standard interchange format, with the
graphify-shaped tool surface.

**Tasks.**
- `projector.py`: canon → NetworkX node-link `graph.json` (the portable interchange) + a SQLite index (the
  queryable derived layer). Run Leiden (`leidenalg`) for communities. Precompute, off the hot path, the
  ranks: indexed local **degree** (the cheap advisory) and a labelled **structural-bridge** signal (nodes
  joining ≥2 Leiden communities, §1.4/§1.6). Reprojection is **incremental** from the git diff between
  `built_from_commit` and `HEAD`; a mismatch marks the SQLite stale and triggers it.
- If `${user_config.metrics_mode} == with_embeddings`, store node embeddings in `sqlite-vss` as candidate
  generators only; otherwise use structure-as-signal.
- `server.py`: implement `query_graph`, `get_node`, `get_neighbors`, `shortest_path`, and `kg_context`.
  `kg_context` reads precomputed ranks O(1), carries provenance + epistemic tier + falsification counters,
  and is hard-capped to a token budget (default ~2000), filled `grounded → span-present → inferred`.
- `hooks/hooks.json`: a `PreToolUse` hook on `Grep|Glob|Read` of type `mcp_tool` that calls `kg_context` to
  bias the session toward the graph (query-first behavior).

**Exit test.** `pytest tests/test_projector.py -q`: `graph.json` round-trips through NetworkX; an
incremental reproject after a one-edge change touches only that edge; `kg_context` returns within budget and
never computes centrality in-request; `get_neighbors`/`shortest_path` return correct results on a fixture
graph. Retry budget: 8.

---

### Stage 6 — Grounding loop + adversarial grounder + memory of failures

**Goal.** Candidate edges become grounded knowledge; weak ones are attacked; failures are remembered.

**Tasks.**
- `agents/grounder.md`: a subagent that walks the queue of `inferred`/`span-present` edges, verifies each
  span against the source, checks the relation is specific (not the generality confound, §1.6), and assigns
  a verdict through `kg_ground` — promoting solid edges to `grounded` and rejecting unsupported ones.
  Verdicts persist as the canonical grounding state and survive reprojection via the reconciler (§1.8).
- `agents/adversarial-grounder.md`: for each hub candidate, generate the strongest counter-edges and
  falsifying questions; surface contradictions as typed `attacked_by` edges. Bounded by a per-run cap.
- Memory of failures (§1.7): rejected and falsified edges are recorded as non-regenerable negative
  information (`epistemic_state: failed`), never pruned, surfaced in `kg_context` as counters.

**Exit test.** `pytest tests/test_grounding.py -q`: the grounder drains a fixture queue and assigns
verdicts; a verdict survives a full reproject (the reconciler re-attaches it); the adversarial grounder
emits counter-edges for a seeded hub; a `failed` edge survives a pruning pass and appears as a counter in
`kg_context`. Retry budget: 8.

**Checkpoint (non-blocking).** When the grounder hits a genuinely ambiguous node merge, one elicitation:
"Merge ⟦A⟧ and ⟦B⟧? [y/N]" Default `N` (keep separate) after a brief wait.

---

### Stage 7 — Annotation agreement + specificity harness

**Goal.** Establish whether the grounding signal is reliable, and whether the specificity-weighted bridge
metric earns its place.

**Tasks.**
- `agents/annotator.md` produces **independent** label sets over the same edge sample (separate passes that
  do not see each other). `harness.py` computes Krippendorff's α across the passes.
- The specificity harness: compare specificity-weighted betweenness against raw degree and raw betweenness
  across the corpus, measuring whether it separates real bridges from vague high-traffic nodes beyond a
  churn band (§1.6).

**Exit test.** `python -m kg_engine.harness agreement && python -m kg_engine.harness specificity` print α
and the metric verdict. If α ≥ 0.67, the grounding signal is treated as reliable and the bridge metric is
gated on/off by the specificity result; if α < 0.67, the specificity metric stays advisory. Either way the
result is logged in `PROGRESS.md` and execution proceeds. No human gate.

---

### Stage 8 — Ideation comparison

**Goal.** Measure whether the grounded graph improves idea generation over plain and over flat retrieval.

**Tasks.**
- `agents/evaluator.md`: run three conditions on a fixed set of ideation prompts drawn from the domain —
  (a) **control** (no graph), (b) **graph** (with `kg_context` + the structural-bridge advisory), (c)
  **RAG** (flat retrieval over the same source). Collect the outputs with condition labels withheld.
- `harness.py` scores the pooled, shuffled outputs for diversity, novelty, and apparent utility, and flags
  unsupported claims, then reveals the condition labels and reports per-condition results.

**Exit test.** `python -m kg_engine.harness ideation` runs the three conditions, scores them, and prints a
per-condition table plus a verdict (does the graph condition produce more diverse/useful ideas without more
unsupported claims?). The verdict is logged in `PROGRESS.md`; execution proceeds regardless. No human gate.

**Checkpoint (non-blocking).** One elicitation before the run: "Use the 12 default ideation prompts, or
supply your own? [default/custom]" Default `default` after a brief wait.

---

### Stage 9 — Hardening and packaging

**Goal.** A validated, installable, versioned plugin.

**Tasks.**
- Full chaos/adversarial suite green (`pytest tests/ -q`): crash recovery, stale-lock, OOB-verdict, ID
  fuzzing, truncated payload, migration rollback, scrub leakage.
- Logical chroot in the canon path; hardened path resolver (null-byte/encoding safe, explicit vault-prefix
  check); edges-per-KB-of-source rate limit against injection flooding; the periodic full-sweep cadence.
- Add a headless `--backend` path (API keys) for CI extraction; wire `claude plugin validate --strict` into
  CI.
- Set `version` in `plugin.json`, write `CHANGELOG.md`, create a marketplace entry, and `claude plugin tag`
  the release.

**Exit test.** `pytest tests/ -q` fully green; `claude plugin validate ./creativity-graph --strict` passes;
a clean install via `--plugin-dir` loads every component (`claude plugin details creativity-graph` lists the
skills, agents, hooks, and MCP server). Retry budget: 8.

---

## 6. Definition of done

- The plugin installs and loads cleanly; the MCP server starts; all component types are present.
- Running `/kg-build` on the source produces a grounded canon, a derived `graph.json` + SQLite index, and a
  populated `PROGRESS.md` with the recorded numbers from Stages 4, 7, and 8.
- `/kg-query` answers questions against the graph with provenance and falsification counters attached.
- The full test suite is green and `claude plugin validate --strict` passes.
- `PROGRESS.md` contains, for this corpus: extractor precision, span-support rate, annotation α, the
  specificity-metric verdict, and the ideation comparison table.

## 7. Build order summary

`Stage 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9`, each advancing on its own automated exit test, with metric
stages auto-iterating to clear their bar and logging the outcome rather than halting. Begin at Stage 0.

---

## Appendix A — `scripts/f4_probe.py` (create verbatim)

Create this file exactly as below. It turns the extractor's output `graph.json` (NetworkX node-link) into
precision numbers by sampling edges, labeling them against the source, and scoring. It is the exit-test
tool for Stage 4 and is fully self-contained (standard library only).

```python
#!/usr/bin/env python3
"""
f4_probe.py — measure the extractor's precision on a non-self-grounding conceptual document.

Turns the extractor's output graph.json (NetworkX node-link) into precision numbers by sampling
edges, labeling each against the source, and scoring. Also reports the astrology rate
(fabricated+vague), the span-support rate, precision per relation type, and — if edges carry a
numeric confidence — whether that confidence actually predicts correctness.

Usage:
  python f4_probe.py summary  graph.json
  python f4_probe.py sheet    graph.json --n 80 --out labels.csv
  # fill `verdict` (correct|fabricated|vague|wrong_type) and `span_found` (y|n) in labels.csv
  python f4_probe.py score    labels.csv

VERDICT vocabulary (the only judgment that matters):
  correct     - the relation is true and specific to the source
  fabricated  - the relation is not supported by the source at all (hallucinated)
  vague       - "true" only because it's generic/unfalsifiable (the generality confound)
  wrong_type  - endpoints related, but the relation label is wrong
span_found (y/n): is there an actual textual span in the source supporting it (the span-present check)?
"""

import csv
import json
import random
import sys
from collections import Counter, defaultdict

EXTRACTED, INFERRED, AMBIGUOUS = "EXTRACTED", "INFERRED", "AMBIGUOUS"
SHEET_COLS = [
    "edge_id", "source_label", "target_label", "relation",
    "confidence", "confidence_score", "source_file",
    "verdict", "span_found", "notes",
]
GOOD = {"correct"}
ASTROLOGY = {"fabricated", "vague"}


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    nodes = data.get("nodes", [])
    edges = data.get("links", data.get("edges", []))
    id2label = {n.get("id"): n.get("label", n.get("id")) for n in nodes}
    id2type = {n.get("id"): n.get("file_type", "?") for n in nodes}
    return nodes, edges, id2label, id2type


def summary(path):
    nodes, edges, _, id2type = load(path)
    print(f"nodes: {len(nodes)}   edges: {len(edges)}\n")

    by_type = Counter(n.get("file_type", "?") for n in nodes)
    print("nodes by file_type:")
    for k, v in by_type.most_common():
        print(f"  {k:12} {v}")

    by_conf = Counter(e.get("confidence", "?") for e in edges)
    print("\nedges by confidence:")
    for k, v in by_conf.most_common():
        print(f"  {k:12} {v}")

    by_rel = Counter(e.get("relation", "?") for e in edges)
    print(f"\ntop relations ({len(by_rel)} distinct):")
    for k, v in by_rel.most_common(15):
        print(f"  {v:4}  {k}")

    scores = [e.get("confidence_score") for e in edges
              if e.get("confidence") == INFERRED and e.get("confidence_score") is not None]
    if scores:
        scores.sort()
        print(f"\nINFERRED confidence_score: n={len(scores)} "
              f"min={scores[0]:.2f} median={scores[len(scores)//2]:.2f} max={scores[-1]:.2f}")

    judged = sum(1 for e in edges if e.get("confidence") in (INFERRED, AMBIGUOUS))
    print(f"\njudged edges (INFERRED+AMBIGUOUS): {judged} / {len(edges)} "
          f"({100*judged/max(len(edges),1):.0f}%) <- the precision-relevant part")


def sheet(path, n, out, include_extracted):
    _, edges, id2label, _ = load(path)
    pool = list(enumerate(edges))
    if not include_extracted:
        pool = [(i, e) for i, e in pool if e.get("confidence") != EXTRACTED]
    if not pool:
        sys.exit("no edges to label (try --include-extracted)")
    random.seed(42)
    random.shuffle(pool)
    pick = pool[:n]

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLS)
        w.writeheader()
        for i, e in pick:
            w.writerow({
                "edge_id": i,
                "source_label": id2label.get(e.get("source"), e.get("source")),
                "target_label": id2label.get(e.get("target"), e.get("target")),
                "relation": e.get("relation", ""),
                "confidence": e.get("confidence", ""),
                "confidence_score": e.get("confidence_score", ""),
                "source_file": e.get("source_file", ""),
                "verdict": "", "span_found": "", "notes": "",
            })
    print(f"wrote {len(pick)} edges to {out}")
    print("fill `verdict` (correct|fabricated|vague|wrong_type) and `span_found` (y|n), then:")
    print(f"  python f4_probe.py score {out}")


def score(path):
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("verdict", "").strip()]
    if not rows:
        sys.exit("no labeled rows (fill the `verdict` column first)")

    n = len(rows)
    verdicts = Counter(r["verdict"].strip().lower() for r in rows)
    correct = sum(verdicts[v] for v in GOOD)
    astro = sum(verdicts[v] for v in ASTROLOGY)
    span_y = sum(1 for r in rows if r.get("span_found", "").strip().lower() == "y")

    print(f"labeled edges: {n}\n")
    print(f"PRECISION (correct / labeled):        {correct/n:.2f}   <- exit gate is >= 0.70")
    print(f"astrology rate (fabricated+vague):    {astro/n:.2f}   <- the grounding risk, measured")
    print(f"span-support rate (span_found=y):     {span_y/n:.2f}   <- the span-present check")

    print("\nverdict breakdown:")
    for v, c in verdicts.most_common():
        print(f"  {v:12} {c:4}  ({100*c/n:.0f}%)")

    per_rel = defaultdict(lambda: [0, 0])
    for r in rows:
        ok = r["verdict"].strip().lower() in GOOD
        per_rel[r.get("relation", "?")][0] += ok
        per_rel[r.get("relation", "?")][1] += 1
    print("\nprecision per relation (n>=3):")
    for rel, (ok, tot) in sorted(per_rel.items(), key=lambda x: -x[1][1]):
        if tot >= 3:
            print(f"  {ok/tot:.2f}  ({ok}/{tot})  {rel}")

    # does a numeric confidence (if present) predict correctness?
    cs_correct, cs_wrong = [], []
    for r in rows:
        try:
            cs = float(r.get("confidence_score", ""))
        except (ValueError, TypeError):
            continue
        (cs_correct if r["verdict"].strip().lower() in GOOD else cs_wrong).append(cs)
    if cs_correct and cs_wrong:
        mc, mw = sum(cs_correct)/len(cs_correct), sum(cs_wrong)/len(cs_wrong)
        print("\nconfidence calibration (does the numeric confidence predict correctness?):")
        print(f"  mean confidence_score | correct edges:   {mc:.2f}")
        print(f"  mean confidence_score | incorrect edges: {mw:.2f}")
        gap = mc - mw
        verdict = ("scores track correctness — the confidence means something"
                   if gap >= 0.10 else
                   "scores DON'T separate correct from wrong — confidence is vocabulary, not grounding")
        print(f"  gap: {gap:+.2f}  ->  {verdict}")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    if cmd == "summary":
        summary(path)
    elif cmd == "sheet":
        n = int(args[args.index("--n") + 1]) if "--n" in args else 80
        out = args[args.index("--out") + 1] if "--out" in args else "labels.csv"
        sheet(path, n, out, "--include-extracted" in args)
    elif cmd == "score":
        score(path)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
```
