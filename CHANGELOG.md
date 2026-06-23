# Changelog

All notable changes to the **creativity-graph** Claude Code plugin are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); the current release is the topmost
version section below.

The plugin turns a non-self-grounding conceptual document into a grounded, queryable knowledge
graph: a human-editable Markdown **canon** (the single source of truth, §1.2), a span-present
write **boundary**, a **grounding loop** with memory of failures, and a regenerable
NetworkX/SQLite **derived** layer. The deterministic Python engine (`scripts/kg_engine`) does the
hard guarantees; the Claude Code session and its subagents do the language work and hand structured
JSON back across the MCP boundary.

## [Unreleased]

### Fixed
- **Cross-platform: reconciler correction of a non-canonically named note now works on case-insensitive
  filesystems (macOS/Windows).** When a hand-created note used a non-canonical filename (`Foo.md` for id
  `Foo`, slug `foo.md`), the reconciler wrote the un-forgery correction to the canonical path and *then*
  unlinked the "stale original" — but on a case-insensitive filesystem `Foo.md` and `foo.md` are the
  **same** file, so the unlink removed the file `write_one` had just rewritten (next `stat` →
  `FileNotFoundError`), and a case-preserving replace kept the stale `Foo.md` name regardless (CI red on the
  macOS/Windows matrix; Linux, being case-sensitive, unaffected). The reconciler now detects a non-canonical
  note by directory-entry **name** (not by resolved path, whose casing is unreliable there) and **unlinks the
  original before** the canonical write, so `write_one` creates a fresh, correctly-cased `foo.md`. Regression
  test added (`tests/test_fix_reconciler.py`) asserting the unlink-before-write ordering.

## [0.3.1] — 2026-06-23

### Fixed — exhaustive review hardening

A whole-codebase review surfaced 44 verified findings (0 critical, 6 high, 6 medium, the rest low/nit);
all are fixed here, each with a regression test. No feature or contract changes — the public surface,
the three axes, and every anti-nonsense guarantee are unchanged.

**Integrity (high).**
- The write boundary now **quarantines a re-emitted edge whose identity is already `failed`/`rejected`**
  on the span-present/inferred lane too (`collapses-into-known-failure`), so an idempotent re-build can
  no longer overwrite a refutation with a fresh `unverified` edge (§1.7).
- `canon.write_nodes` moves the `git add`/`commit` **out of the data-rollback path**: a commit failure
  (unset identity, a rejecting hook, index-lock contention) no longer discards already-fsynced canon.
- The reconciler persists a forged-verdict correction **to the note it actually read** (and unlinks the
  stale non-canonical original) instead of the slug path, so a hand-named note can no longer keep a
  forged verdict in a self-concealing duplicate.
- Six MCP tool wrappers accept explicit `null` (`X | None = None`); the `high`-sensitivity PERSON
  scrubber no longer leaks a name shadowed by a leading Title-Case non-name word.

**Robustness (medium).** `kg_absorption` emits JSON-safe `null` (not `Infinity`); `kg_context` shares
one token budget across the grounded/hypothesized lanes and reports the true total; the reconciler reads
audit/state/graph as UTF-8 and degrades on a locale mismatch instead of crashing; `kg_write` reports an
empty write set on rollback (and the headless backend honors it); the PHONE scrubber no longer
over-redacts bare prose numbers; the release checklist lists all four version-bearing files the gate
enforces.

**Hardening (low/nit).** Structured MCP errors on a corrupt note; `LeaseLock` release/heartbeat TOCTOU
fixes and lease-before-read in `kg_ground`; the rank advisory excludes never-pruned `failed`/`rejected`
edges; `_load_state` tolerates a non-dict state file; clearer headless backend failures (single
`SystemExit` on a missing API key, `max_tokens` clamp); bootstrap stamp folds interpreter identity,
`--wait` outlasts `STALE_LOCK_SECS`, orphan-proof lock steal, dead `--run` removed; **GitPython dropped**
(unused — the engine shells out to the `git` CLI); `transplant` O(m²) hoisting; `explode` `k`-clamping;
Windows `PYTHONPATH` dedup; UTF-8 stdin in the precontext hook; the reconciler no longer re-parses the
whole canon a second time per full sweep; the precontext read hook constructs a side-effect-free
`Canon` (no canon-dir `mkdir` / `.git/info/exclude` rewrite per Grep/Glob/Read); and doc/tool-count
syncs across `ARCHITECTURE.md`, `README.md`, and this changelog.

## [0.3.0] — 2026-06-22

### Added — The generative layer

The plugin gains its second half: the mechanisms that turn the grounded graph from a **verification
machine** into an **idea-generation machine** — without weakening one anti-nonsense guarantee. The
design rests on **the inversion**: *generate offensively, judge defensively.* Entry into the graph is
never gatekept by a quality metric; the existing grounding loop is the filter, applied **after**.

- **The hypothesized write lane (`kg_propose`).** A generated candidate enters as
  `provenance=hypothesized`, `epistemic_state=unverified`, **with no span** — a proposal from a discovery
  mechanism, never a text claim, stored in a lane that can never be mistaken for grounded content. The
  write boundary accepts span-less hypothesized items while keeping every `span-present`/`inferred`
  guarantee intact, preserves `authored_by=deterministic` for a genuine discovery mechanism, still demotes
  forged verdicts, and **quarantines** any candidate that collapses into a known failure
  (`collapses-into-known-failure`, invariant 5). `kg_propose` refuses text claims with
  `propose-lane-text-claim`.
- **The completed bridge metric.** The projector now precomputes `betweenness`,
  specificity-weighted `spec_betweenness`, `specificity`, and a per-projection `gate_on` (via
  `harness.specificity`), off the hot path. `kg_context.advisory.bridge_metric` ranks by the
  confound-corrected `spec_betweenness` when the gate earns it, else falls back to the honest
  structural-bridge/degree advisory (§1.6). A legacy index is migrated with a forced rebuild.
- **Six deterministic generators + `kg_generate` (read-only).** `bridge` (§2/§4), `seed` (§3 — the
  positive residual `c − E[c|d]`, never `d×c`), `compression` (§7 — dense clusters passing an MDL +
  specificity screen → a new node the language layer names), `regroup` (§8 — bridges invisible under the
  prior partition), `transplant` (§5 — a hub's reorganising pattern into the most absorptive community),
  `ensemble` (§9 — cross two constructions). Every generator is generality-controlled and drops
  candidates colliding with failure memory.
- **The four §8 endo operations (`kg_operate`).** `collapse` / `explode` / `regroup` / `open`, each
  writing hypothesized structure through the propose lane — never a verdict, never a span.
- **The absorption window (`kg_absorption`, §14).** Scores each grounded-from-hypothesized node as
  `fertile | absorbed | isolated` with a novelty half-life, so a slate can prefer the productive middle.
- **Promotion upgrades provenance.** `kg_ground` gains `support_span` / `support_note`: a hypothesis
  becomes `grounded` ONLY with support, which upgrades its provenance (`hypothesized → span-present` for a
  verbatim source span, else `inferred`); without support the promotion is refused
  `hypothesis-needs-support`. `kg_context` returns a **separate** `hypotheses[]` block; the grounded
  `items[]` lane never contains a proposal.
- **Workflow + orchestration.** New `/kg-generate` command + `kg-generator` (language-only) agent, and
  `/kg-perturb` (§9/§15 exo cross-generation). The skill workflow becomes
  `build → ground → generate → ground → query`. `/kg-experiment` gains a `graph+generate` condition with
  its own verdict. The MCP surface grows to **fifteen** tools.

## [0.2.1] — 2026-06-22

### Fixed — exhaustive review hardening sweep

A multi-agent review of the whole codebase (each finding adversarially verified, then the fixes
re-verified) closed a set of correctness, edge-case, and dependency issues. Highlights:

- **Falsification-memory integrity (the honesty guarantee).** The batch-write rollback now restores
  **only the failing batch's files** from a pre-write snapshot instead of a repo-wide
  `git reset --hard HEAD`, so a failed `kg_write`/`kg_rename` no longer silently discards unrelated
  uncommitted grounding verdicts or hand edits. The grounding audit log is git-excluded. `kg_rename`
  migrates a renamed edge/node's verdict↔audit linkage so a grounded/`failed` edge is no longer
  re-quarantined at the next reconcile. The reconciler prunes the dead `epistemic` baseline of
  deleted edges (closing a delete→recreate→forge bypass) and now polices out-of-band `obsolete`.
- **Edge/node identity.** `slug()` NFC-normalizes and maps punctuation to a separator (instead of
  deleting it), so visually-identical or punctuation-differing endpoints no longer collide or fork;
  the write boundary dedups on the canonical `edge.id`, matching the canon merge and disk.
- **Single-writer + projection safety.** The lease lock now heartbeats during long batches and uses an
  atomic compare-and-swap to reclaim a stale lock; lazy reprojection is serialized under the lease and
  the staleness check is a cheap per-file `(name,size,mtime)` digest pre-gate (no git fork / full parse
  on the read hot path). `kg_ground`/`kg_rename` hold the lease across the audit-append+write+compensate
  sequence.
- **Egress scrub.** Removed catastrophic-backtracking in the secret regex; broadened secret coverage so
  bespoke keys are redacted whole instead of partially leaked by the phone/CC rules; the Title-Case
  "person" rule no longer mass-redacts ordinary concept terms; pre-existing placeholder-shaped text
  round-trips.
- **Resilience & rate limits.** A malformed enum / edge entry in a canon note is coerced/skipped instead
  of dropping the whole node from every read; the node flood budget is seeded canon-wide yet exempts
  idempotent re-emission and never-pruned failure memory; the context budget is clamped.
- **Headless backend.** Per-section extraction failures are isolated and the derived layer is always
  reprojected; non-JSON output is surfaced clearly; `max_tokens` default raised.
- **Dependencies / manifests.** Raised the `anthropic` floor to `>=0.77` (first GA of `output_config`),
  removed the unused/unmaintained `embeddings` and `treesitter` extras, added Windows/macOS to CI, and
  dropped the unwired `userConfig.domain` knob.

### Changed — cross-platform, background, `uv`-optional install system

Replaced the bash-only, blocking, `uv`-required provisioning with a cross-platform one modelled on the
sibling `creativity-amplifier` plugin. It now runs on **Windows, macOS, Linux, and WSL/Git-Bash** with
only Python ≥3.10 and Node (always present in the Claude Code runtime) required.

- **New `scripts/bootstrap.py`** — the single source of truth for building the engine venv: resolves the
  venv dir (`--venv` > `$KG_ENGINE_VENV` > `$CLAUDE_PLUGIN_DATA/.venv` > `<repo>/.venv`), installs
  **dependencies only** with `uv sync --no-install-project` when `uv` is on PATH, **else falls back to
  the stdlib `venv` + `pip install <repo>`** (no hard `uv` dependency). Idempotent via a content
  `install.stamp` (hash of `pyproject.toml`), concurrency-safe via an atomic lock dir that steals stale
  locks, writes a cross-platform interpreter pointer `<venv>/engine-python.txt`, and removes a half-built
  venv on failure. `kg_engine` keeps resolving off `PYTHONPATH=scripts` (never installed), so engine
  source edits need no rebuild.
- **New SessionStart chain** — `hooks/provision.mjs` (Node dispatcher) → `hooks/provision.{sh,ps1}` (OS
  launchers) → `bootstrap.py --background`, a **detached** worker that returns in milliseconds and no
  longer blocks the session. The per-session canon reconcile (§1.8) moved into the worker
  (`bootstrap.py --reconcile`).
- **New `scripts/launch_server.mjs`** — the MCP server now launches via `node` (was `bash`), so the
  spawn succeeds on every OS; it resolves the engine python via the pointer and self-heals the venv in
  the foreground on a cold first session before running `kg_engine.server`.
- **New `hooks/precontext.mjs`** — the PreToolUse context hook is now a Node launcher (was
  `sh -c '<venv>/bin/python …'`), resolving the interpreter via the pointer.
- **Removed** `hooks/bootstrap.sh` and `scripts/launch_server.sh` (superseded). `.mcp.json` and
  `hooks/hooks.json` rewired to the Node entrypoints; `scripts/validate_plugin.py` updated to assert the
  new component set; `tests/test_bootstrap.py` adds hermetic coverage (path resolution, stamp, readiness,
  lock, failure cleanup). **140 tests green.**

### Fixed — Python 3.10 + Windows compatibility (CI matrix)

The cross-platform CI matrix (Windows + macOS, Python 3.10 and 3.12) surfaced two portability bugs:
`tests/test_manifests.py` imported `tomllib` (stdlib only on 3.11+) and now parses `pyproject.toml` as
text, so the suite runs on the declared `requires-python = ">=3.10"` floor; and `LeaseLock._pid_probe`
called `os.kill(pid, 0)`, which on Windows is not a no-op existence check — the liveness probe is now
skipped on Windows (falling back to heartbeat/TTL staleness) and the dead-pid reclaim assertion is
POSIX-only.

### Documentation — re-synced the docs with the engine and manifests

A per-file fan-out audit of every doc against the engine at HEAD (each finding adversarially verified)
corrected the drift the install-system and hardening commits left behind. Removed references to features
that no longer exist (the `embeddings`/`treesitter` extras, the `userConfig.domain` knob, the `sqlite-vss`
candidate generator, the `tree_sitter` parser, `kg_ground`'s `by` parameter, and `kg_write`'s `stash_ref`
return key — now `error`); refreshed stale test counts and API facts (`ValidationResult` fields, `slug()`
mapping, `is_stale`, the specificity/ideation gate conditions, the `span-too-short` rejection, the
`complete`-flag default, and `query_graph`'s node-only `epistemic_state` scope); fixed the skill's
subagent/tool mappings; and restored the missing `kg_scrub` tool and `evaluator.md` agent to the
component listings. Touched `README`, `ARCHITECTURE`, `CLAUDE.md`, `PROGRESS`, the skill `SKILL.md` +
references, and the agent docs.

## [0.2.0] — 2026-06-21

Hardening release. Cuts the **review-findings hardening pass** (below) on top of the initial 0.1.0
build: two invariant bypasses closed, a range of correctness/robustness fixes, and 12 new regression
tests (79 green). **Breaking:** the `kg_ground` MCP tool dropped its `by` parameter (verdicts via the
tool are always attributed to the agent — a human verdict can no longer be forged through the tool
surface), and `kg_write` now demotes a forged `authored_by=deterministic` claim to `agent`.

The initial end-to-end build. Engine: **79 tests green**.
Components: **5 agents, 5 commands**, 1 skill (+3 on-demand references), the `SessionStart`/`PreToolUse`
hooks, and the `creativity-graph` MCP server. Each stage advanced on its own automated exit test.
The plugin was then **installed locally and the full workflow run end-to-end** (see *Packaging hardening
+ live validation* below).

### Review-findings hardening pass

An exhaustive multi-agent review (per-module + cross-cutting, each finding adversarially verified)
surfaced two invariant bypasses and a set of correctness/robustness defects. All were fixed; 12
regression tests were added (`tests/test_review_fixes.py`).

- **Fixed (critical) — span-present was bypassable.** `boundary.py` now demotes a write payload's
  `authored_by=deterministic` claim to `agent` (like `human`), so an extractor can no longer skip span
  verification by self-declaring parser authorship. `epistemic_state` stripping was generalised from
  the three verdicts to *any* non-`unverified` state, so `obsolete` can't be forged on a write either.
- **Fixed (critical) — the mtime-spoof defence never ran.** The `SessionStart` reconcile now runs a
  **full re-hash sweep** (`scan(full_sweep=True)`); the per-file mtime/size pre-filter is only a
  within-session optimisation.
- **Fixed (critical) — stale reads.** `Projector.is_stale()` now compares per-node content hashes (not
  just `built_from_commit`), so a `kg_ground` verdict (written with no commit) and non-git vaults
  reproject on the next read.
- **Fixed (high) — `graph.json` dropped parallel typed edges.** The projector builds a `MultiDiGraph`,
  so two edges sharing `(source, target)` but differing in `relation` both survive.
- **Fixed (high) — projection crash on human-edited timestamps.** `Node` now coerces
  `id`/`label`/`created_at`/`updated_at` to `str`, so an unquoted YAML datetime no longer raises
  `TypeError` when the projector hashes the frontmatter.
- **Fixed (high) — one malformed canon note crashed every read/scan.** `all_nodes()` and the reconciler
  sweep skip an unreadable note instead of propagating.
- **Fixed (high) — forged-verdict re-quarantine was replay-able.** The reconciler now *consumes* audit
  records (counts, not set membership), so replaying a previously-audited verdict out-of-band is caught.
- **Fixed (high) — `kg_rename` could lose a node** (old note unlinked even on rollback) and left stale
  edge ids / dangling endpoints; it now rewrites every endpoint + id, writes verbatim (no re-merge),
  and only deletes the old note on success.
- **Fixed (high) — secret leaks in the scrubber** (multi-word/quoted values and underscored key names
  like `aws_secret_access_key`); added bare-`Bearer` and IPv6 coverage; IP now scrubs before phone.
- **Fixed (medium) — scrubber placeholder collision** across `kg_scrub` calls corrupted the restore map
  (recovered the wrong original into the canon); the `Scrubber` now keeps one placeholder namespace per
  session. The write boundary also caps net-new **nodes** (not just edges), demotes a forged `human`
  verdict author on `kg_ground` (the MCP tool drops the `by` arg), span-verification applies Unicode NFC
  normalisation and strips zero-width chars, and `confidence_score` is clamped to `[0,1]` (NaN dropped).
- **Fixed (medium/low) — input & ops hardening:** `KG_MAX_EDGES_PER_KB=nan/inf` no longer crashes
  `kg_write`; `query_graph` clamps a negative `LIMIT`; `kg_context` escapes SQL `LIKE` wildcards; the
  atomic writer fsyncs the parent dir; the lease lock is now actually acquired (re-entrant, O_EXCL) and
  excluded from git; `validate_plugin.py` rejects a non-string version and cross-checks
  `pyproject.toml`/`__init__` versions; `f4_probe.py` validates the verdict vocabulary, parses flags
  safely, drops non-finite confidences, and reads BOM-tolerant CSV; `bootstrap.sh` only records a
  successful sync; the headless backend surfaces refusal `stop_details`. Bumped the `anthropic`
  dependency floor for the backend's `output_config`/adaptive-thinking usage.

### Stage 0 — Scaffold + environment bootstrap

- Added the plugin manifest `.claude-plugin/plugin.json` with the `userConfig` enable-time prompts:
  `domain` (default "conceptual theory"), `source_path`, `sensitivity` (`low|medium|high`, default
  `medium`), `metrics_mode` (`structure_only|with_embeddings`, default `structure_only`).
- Added `.mcp.json` registering one server `creativity-graph`, launched with `uv run` against the
  persistent data dir, exporting `KG_PROJECT_DIR=${CLAUDE_PROJECT_DIR}` and `KG_DATA=${CLAUDE_PLUGIN_DATA}`.
- Added `pyproject.toml` (uv-managed engine deps) and the `SessionStart` bootstrap
  (`hooks/bootstrap.sh`): diff the bundled `pyproject.toml` against the copy in `${CLAUDE_PLUGIN_DATA}`
  and run `uv sync` only on difference, so the venv installs once and re-syncs only on dependency changes.
- Stubbed `scripts/kg_engine/server.py` exposing `mcp__creativity-graph__kg_ping` →
  `{name, version, metrics_mode, sensitivity, pack_loaded}`.
- **Exit test:** `claude plugin validate ./creativity-graph --strict` passes; the plugin loads and
  `kg_ping` returns the version.

### Stage 1 — Canon + transactional writes + lease lock + reconciler

- `scripts/kg_engine/model.py`: the three orthogonal axes (`Provenance`, `AuthoredBy`,
  `EpistemicState`, §1.3) plus `Disposition`, `Confidence`; `Node`/`Edge` dataclasses; deterministic
  `edge_id(src, rel, tgt)` = `e_{source}__{relation}__{target}` (slugged); `normalize_text`,
  `span_verifies`, and Markdown frontmatter (de)serialization.
- `scripts/kg_engine/canon.py`: one Markdown note per node with three-axis frontmatter and an `edges:`
  block. Single-file writes are temp-file + atomic `os.rename`; multi-file mutations use
  git-as-rollback (`git stash push -u` before `git reset --hard HEAD`, preserving parallel human
  edits and surfacing the stash). Reclaimable `LeaseLock` (`acquire`/`heartbeat`/`release`/`is_stale`).
- `scripts/kg_engine/reconciler.py`: `mtime`/`size` pre-filter backed by a periodic full re-hash
  sweep (the sweep defeats mtime spoofing, §1.8); re-validates out-of-band changes through the
  boundary; `reattach_after_reproject` re-attaches grounding verdicts to surviving edges and surfaces
  orphaned verdicts. **An out-of-band `epistemic_state` transition (a forged verdict) is re-quarantined.**
- **Exit test:** `pytest tests/test_chaos.py` — crash-mid-write recovers via git, a stale lock is
  always reclaimed, an out-of-band verdict edit is re-quarantined on reconcile.

### Stage 2 — Domain pack + glossary

- Authored `pack/pack.yaml` from `examples/source.md`: node types
  `compression, primitive, claim, metric, operation, failure`; edge types
  `grounds, attacked_by, reconciles_with, bridges, collapses_into, confounded_by, approximates,
  defends_against, projects, survives`; a glossary of defined terms; and per-term `specificity_seeds`
  (IDF-like hints so vague terms are not mistaken for bridges, §1.6).
- Added `pack/glossary.md`.
- `scripts/kg_engine/pack.py`: `PackContract` (Pydantic), `load_pack`, and `coverage(pack, source_text)`.
  Types outside the pack route to the `undeclared-type` bucket — never silently accepted.
- **Exit test:** `python -m kg_engine.pack validate pack/pack.yaml [source.md]` passes `PackContract`
  and reports glossary coverage of the source's defined terms.

### Stage 3 — Staged extractor + boundary + PII scrubber

- `scripts/kg_engine/scrub.py`: `Scrubber(sensitivity)` redacts secrets/keys (always) and PII (per
  `sensitivity`) using consistent placeholders that preserve relational structure
  (`⟦PERSON:1⟧ attacked_by ⟦PERSON:2⟧`); the mapping stays local and `restore` rebuilds the original
  for span verification (§1.9). The scrub protects egress, not the local canon.
- The egress scrub is now wired into the live path: a new `mcp__creativity-graph__kg_scrub` tool runs
  the §1.9 egress redaction with consistent placeholders (`⟦SECRET:1⟧` etc.) before text reaches a
  subagent, and `kg_write` restores placeholder spans to the original for the canon (the boundary
  stores the restored original span). The MCP tool surface is now **eleven** tools.
- `scripts/kg_engine/boundary.py`: strict Pydantic `WritePayload`/`NodeIn`/`EdgeIn` (extra fields
  forbidden) and `validate_payload(...) -> [ValidationResult]`. Enforces span-present (§1.5: every
  non-deterministic edge cites a verbatim span that verifies against the source), tags the three axes,
  and returns dispositions `ACCEPTED | DEMOTED | QUARANTINED | REJECTED` with `reason` and
  `retryable` (`false` for semantic rejections, `true` for transport failures). A truncated payload
  (`complete` not `true`) is rejected with no partial write; a forged verdict or `authored_by=human`
  is **demoted** to `unverified`/`agent`, never forged (§1.4/§1.8).
- `agents/extractor.md` (subagent `kg-extractor`): reads the scrubbed source section by section and
  emits the contract JSON — nodes, pack-typed edges, and a verbatim supporting span per edge — to
  `mcp__creativity-graph__kg_write`.
- **Exit test:** `pytest tests/test_invariants.py` — fabricated/undeclared/span-less edges are
  rejected or demoted; truncated JSON is rejected; a seeded secret never leaves the scrubber.

### Stage 4 — Extraction evaluation (`f4_probe`)

- Added `scripts/f4_probe.py` (verbatim from Appendix A; standard library only): `summary` to
  inspect, `sheet --n --out` to emit the labeling CSV, `score` to compute **precision** (exit gate
  ≥ 0.70), the astrology rate (fabricated+vague), the span-support rate, precision per relation, and
  a confidence-calibration check.
- `agents/annotator.md` (subagent `kg-annotator`): labels a sample of extracted edges
  `correct | fabricated | vague | wrong_type` and records `span_found` (`y|n`), judged strictly
  against the source — producing the gold CSV `f4_probe score` consumes.
- **Exit test:** `python scripts/f4_probe.py score labels.csv` prints precision; on a miss the pack
  and extractor prompt auto-iterate (≤ 3) and the best result is recorded — the flow never halts on a
  metric.

### Stage 5 — Projector + derived layer + query surface

- `scripts/kg_engine/projector.py`: `Projector(canon, derived_dir).project(incremental=True)`
  renders the canon to `${CLAUDE_PLUGIN_DATA}/derived/graph.json` (NetworkX node-link interchange) +
  `index.sqlite`. Runs Leiden communities (`leidenalg`, label-propagation fallback); precomputes,
  off the hot path, indexed local **degree** (the cheap advisory) and a labelled **structural-bridge**
  signal (nodes joining ≥2 communities, §1.4/§1.6). Reprojection is incremental from the git diff
  `built_from_commit..HEAD`; a mismatch marks SQLite stale. **The derived layer contains nothing the
  canon does not (§1.2).**
- `scripts/kg_engine/server.py`: the read/query surface — `query_graph`, `get_node`, `get_neighbors`,
  `shortest_path`, and `kg_context(query, budget=2000)`, which reads precomputed ranks O(1) (never
  computing centrality in-request), carries provenance + epistemic tier, is hard-capped to a token
  budget (filled grounded → span-present → inferred), and surfaces
  `falsification_counters.failed_or_rejected_edges` plus a labelled `structural-bridge` advisory.
- `hooks/precontext.py`: a `PreToolUse` hook biasing the session toward the graph (query-first).
- Commands `commands/kg-build.md` (`/kg-build`) and `commands/kg-query.md` (`/kg-query`) orchestrate
  extract → write → project and answer questions with provenance + falsification counters attached.
- **Exit test:** `pytest tests/test_projector.py` — `graph.json` round-trips through NetworkX, a
  one-edge incremental reproject touches only that edge, `kg_context` returns within budget,
  `get_neighbors`/`shortest_path` are correct on a fixture graph.

### Stage 6 — Grounding loop + adversarial grounder + memory of failures

- `mcp__creativity-graph__kg_ground(target_id, verdict, by, kind, note)` — the **only** way to set a
  verdict (`grounded | rejected | failed | obsolete`), stamping `verdict_by`/`verdict_at` and an audit
  record; verdicts survive reprojection via the reconciler (§1.8).
- `agents/grounder.md` (subagent `kg-grounder`): walks the `unverified` edge queue, re-verifies each
  span, checks the relation is specific (rejects edges "true" only because generic/unfalsifiable, the
  generality confound, §1.6), and applies verdicts through `kg_ground`.
- `agents/adversarial-grounder.md` (subagent `kg-adversarial-grounder`): for each hub candidate,
  generates the strongest counter-edges and falsifying questions as typed `attacked_by` edges and
  `kg_ground(verdict="failed")`, bounded by a per-run cap.
- Memory of failures (§1.7): `rejected`/`failed` edges are non-regenerable negative information —
  never pruned by the projector, surfaced in `kg_context` as falsification counters.
- Command `commands/kg-ground.md` (`/kg-ground`) drives the grounding + adversarial passes.
- **Exit test:** `pytest tests/test_grounding.py` — the grounder drains a fixture queue, a verdict
  survives a full reproject, the adversarial grounder emits counter-edges for a seeded hub, and a
  `failed` edge survives a pruning pass and appears as a counter in `kg_context`.

### Stage 7 — Annotation agreement + specificity harness

- `scripts/kg_engine/harness.py` `agreement(...)`: Krippendorff's α across **independent**
  annotator label sets over the same edge sample (CLI consumes a JSON list of coder dicts with labels
  `correct|fabricated|vague|wrong_type`). α ≥ 0.67 ⇒ the grounding signal is treated as reliable.
- `harness.py` `specificity(...)`: compares specificity-weighted betweenness against raw degree and
  raw betweenness and emits the bridge-metric gate verdict (JSON). The real bridge metric stays
  **gated** until the harness validates it; degree + the labelled structural-bridge signal remain the
  honest advisory (§1.6).
- **Exit test:** `python -m kg_engine.harness agreement && python -m kg_engine.harness specificity`
  print α and the metric verdict; the outcome is logged and execution proceeds.

### Stage 8 — Ideation comparison

- `harness.py` `ideation(...)`: scores three conditions — **control** (no graph), **graph**
  (`kg_context` + the structural-bridge advisory), **RAG** (flat retrieval over the same source) — on
  a fixed prompt set, with labels withheld then revealed, scoring diversity/novelty/apparent utility
  and flagging unsupported claims (CLI consumes `{"outputs": {"control":[…], "graph":[…], "rag":[…]},
  "source": "<text>"}`).
- Command `commands/kg-experiment.md` (`/kg-experiment`) runs and reports the comparison.
- **Exit test:** `python -m kg_engine.harness ideation` prints the per-condition table plus a verdict;
  the result is logged and execution proceeds.

### Stage 9 — Hardening + packaging + component layer

- Full suite green (`pytest tests/`): crash recovery, stale-lock, OOB-verdict, ID fuzzing, truncated
  payload, scrub leakage (`tests/test_chaos.py`, `test_invariants.py`, `test_grounding.py`,
  `test_projector.py`, `test_pack.py`, `test_harness.py`).
- **Component layer wired here** — the markdown that connects the session to the engine:
  - **Agents:** `extractor.md` (`kg-extractor`), `grounder.md` (`kg-grounder`),
    `annotator.md` (`kg-annotator`), `adversarial-grounder.md` (`kg-adversarial-grounder`),
    `evaluator.md` (`kg-evaluator`).
  - **Commands:** `/kg-build`, `/kg-ground`, `/kg-query`, `/kg-eval`, `/kg-experiment` — orchestrate the
    agents via the `Task` tool and the MCP tools.
  - **Skill:** `skills/creativity-graph/SKILL.md` (the build → ground → query operating guide) with
    on-demand `references/` (`contract.md`, `pack-schema.md`, `tools.md`).
  - **Hooks:** `hooks/hooks.json` wiring `SessionStart` (`bootstrap.sh`) and `PreToolUse`
    (`precontext.py`).
- **Exit test:** `pytest tests/` fully green and `claude plugin validate ./creativity-graph --strict`
  passes; a clean install lists every component (skills, agents, hooks, MCP server).

### Packaging hardening + live validation (v0.1.0)

After `claude plugin validate --strict` passed, the plugin was installed locally — user scope, via a
single-plugin `marketplace.json` — and the full workflow run end-to-end **through the installed plugin**
on a fresh vault: `/creativity-graph:kg-build` → `/kg-ground` → `/kg-query`. (Plugin commands are
namespaced `/creativity-graph:<command>`.) Running it for real surfaced four packaging/quality bugs that
static review could not — each fixed and re-verified:

- **MCP tool namespace.** A plugin-bundled server's tools are namespaced
  `mcp__plugin_<plugin>_<server>__<tool>` (here `mcp__plugin_creativity-graph_creativity-graph__kg_write`),
  **not** `mcp__creativity-graph__kg_write`. Every agent `tools:` / command `allowed-tools:` grant (and
  doc reference) used the short form, so the `kg-extractor` subagent received no graph tools. Swept all
  references to the correct prefix.
- **userConfig → server environment.** `${CLAUDE_PLUGIN_OPTION_*}` does **not** expand inside `.mcp.json`
  (only `${user_config.KEY}`, `${CLAUDE_PLUGIN_ROOT|DATA}`, `${CLAUDE_PROJECT_DIR}` do), and two
  self-referential `CLAUDE_PLUGIN_OPTION_*` mappings were *clobbering* the values Claude Code auto-injects
  — so the server saw no source and rejected every span. `KG_SOURCE_PATH` now uses
  `${user_config.source_path}`; sensitivity/metrics_mode come from the auto-injected `CLAUDE_PLUGIN_OPTION_*`.
- **Cold-start spawn race.** Pointing `.mcp.json` `command` straight at
  `${CLAUDE_PLUGIN_DATA}/.venv/bin/python` let a first-session spawn fail before the `SessionStart` hook
  finished building the venv; Claude Code cached that as "needs-auth" and dropped every `kg_*` tool for the
  session. The server now launches via **`scripts/launch_server.sh`** (bash always exists → the MCP spawn
  always succeeds; the wrapper self-heals the venv before `exec`'ing the server).
- **`kg_context` query matching.** A natural-language `/kg-query` matched the *whole* question string as one
  `LIKE` substring and returned zero items (the command fell back to structural lookups). `kg_context` now
  **tokenizes the query and OR-matches each term** (≥3 chars, deduped) across source/target/relation/span;
  single-term and empty queries are unchanged; a no-match query still returns nothing.
- **Manifest.** `userConfig` does not support `enum`/`options` (rejected by `--strict`) — dropped them; set
  `version 0.1.0`; `source_path` is a `file`-typed option. Added `.claude-plugin/marketplace.json`.

**Live results (demo corpus, fresh vault):**

- `/kg-build` — 31 ACCEPTED / 0 DEMOTED / 0 QUARANTINED / 0 REJECTED; **18 nodes, 12 edges**, every edge
  `span-present` with a verifying verbatim span; egress `kg_scrub` ran (0 redactions, PII-free demo).
- `/kg-ground` — queue drained `unverified 12 → 0`: **11 grounded, 1 rejected** (the generality confound —
  a generic definitional gloss correctly refused, §1.6), **2 failed** (adversarial counter-edges that did
  not hold). `falsification_counters.failed_or_rejected_edges = 3`, never pruned; verdicts survive a full
  reprojection (reconciler re-attach).
- `/kg-query` — answered with `[provenance/epistemic_state]` on every edge and the falsification counters
  attached; **refused to present the rejected `bridges` claim as fact**, and labelled the structural-bridge
  advisory as a heuristic.

### Stage 9 deferred items (now implemented)

The items Stage 9 had parked for a future release, plus the explicit path-resolver hardening from the
Stage 9 task list — implemented, then hardened against a 6-dimension adversarial review. Engine tests:
**53 → 67** (+14).

- **Headless `--backend` extraction path** (`scripts/kg_engine/backend.py`, §2.2). The non-interactive
  counterpart to "the LLM is the session": API-key-driven extraction for CI. It mirrors
  `agents/extractor.md` — split the source into `##` sections, `kg_scrub` each (egress, §1.9), call the
  Claude API (`claude-opus-4-8` default; adaptive thinking; `output_config.format` json_schema keyed to
  the pack vocabulary; no removed sampling params) for nodes/edges/spans, stamp the deterministic axes,
  and write through the **same `kg_write` boundary** the in-session flow uses. The API call is isolated
  and the client injectable, so `tests/test_backend.py` exercises the full pipeline (split → extract →
  stamp → boundary → canon → project) with a fake client and no network, and asserts the request is
  Opus-4.x-compliant. Ships as the optional `backend` extra (`anthropic>=0.40`); `python -m
  kg_engine.backend extract`.
- **Edges-per-KB injection rate-limit** (`scripts/kg_engine/boundary.py`). `validate_payload` caps
  **net-new** writable edges at `max(64, kb·20)` across the canon; overflow is REJECTED
  `rate-limited-flood` (`retryable=false`). Deduped edges (re-sent or already canonical) grow the canon
  by zero and so cost no budget — idempotent `/kg-build` re-runs never trip the limiter (a
  double-counting bug the adversarial review caught; now regression-tested). Tunable via
  `KG_MAX_EDGES_PER_KB` (threaded through `KGEngine`); pass `max_edges_per_kb=None` to disable. The
  2.7 KB demo (~6.5 edges/KB) is far below the bar, so normal builds are unaffected.
- **Hardened canon path resolver / logical chroot** (`scripts/kg_engine/canon.py`). `Canon.node_path`
  now rejects null bytes and verifies the resolved path stays under the canon dir — the explicit
  vault-prefix check on top of the structural `slug()` guarantee.
- **CI** (`.github/workflows/ci.yml` + `scripts/validate_plugin.py`). On push/PR: full `pytest`, `pack
  validate`, the Stage 7/8 harness commands, and a deterministic manifest/component check (the hard
  gate — parses every manifest, checks each component exists, and that the plugin/marketplace versions
  agree). A best-effort job runs the real `claude plugin validate --strict` when the CLI is installable.
- The one remaining outward-facing step is left deliberately manual (see `CLAUDE.md`): public
  marketplace publish + `claude plugin tag`.

### Documentation

Streamlined the prose docs to `README` + `ARCHITECTURE` + `PROGRESS` + `CHANGELOG` + a new `CLAUDE.md`;
the full `.md` set was then audited and verified free of dangling links.

- **Removed `IMPLEMENTATION-PLAN-creativity-graph-claude-code.md`** — the pre-build design doc,
  superseded by the shipped engine, `ARCHITECTURE.md`, and this changelog; no code, CI, or manifest
  referenced it. The stale prose pointers to it in `ARCHITECTURE`/`PROGRESS`/`CHANGELOG` were scrubbed.
- **Removed `RELEASE.md`**, folding the maintainer release checklist (pre-flight validation,
  dual-manifest version bump, optional headless rebuild, manual publish/tag) into the new **`CLAUDE.md`**;
  the publish-step mentions in `PROGRESS`/`CHANGELOG` now point there.
- **Fixed** a dangling reference in `skills/creativity-graph/references/tools.md`: the write-contract
  pointer now targets the existing `references/contract.md` (it previously named a sibling reference
  file that did not exist).

[Unreleased]: https://github.com/sergiparpal/creativity-graph/commits/main
