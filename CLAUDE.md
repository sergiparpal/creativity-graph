# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`creativity-graph` is a **Claude Code plugin** that turns a non-self-grounding conceptual document
into a grounded, queryable knowledge graph. It is built in two halves that meet at an MCP boundary:

- A **deterministic Python engine** (`scripts/kg_engine/`) owns everything that must be exact —
  schema validation, span verification, verdict stamping, projection, scrubbing, metric scoring.
  These are *structural* guarantees, not lint rules: the things the system forbids are unreachable
  through the boundary.
- The **Claude Code session and its subagents** do the language work — reading prose, proposing
  typed edges, copying verbatim spans, arguing the adversarial case — and hand structured JSON back
  across the boundary, which refuses anything it cannot ground.

`README.md` explains the *why*. `ARCHITECTURE.md` is the authoritative data-model/boundary contract
that the engine and tests both bind to — read it before changing anything in `model.py`,
`boundary.py`, `canon.py`, or `reconciler.py`. Section markers like `§1.5` throughout the code and
docs refer to that shared conceptual model (not to any external file).

## Common commands

Development runs through `uv` from the repo root. `uv run …` syncs the venv and installs the project
editable (`kg_engine` ← `scripts/kg_engine`, per `[tool.hatch.build.targets.wheel]`), so imports and
`python -m kg_engine.*` resolve. (The *plugin runtime* instead uses `uv sync --no-install-project`
plus `PYTHONPATH=scripts` — see `hooks/bootstrap.sh` and `.mcp.json`; that's a runtime detail, not how
you develop.)

```sh
# Environment
uv sync                                   # or: pip install -e ".[dev,backend]"
# extras: dev (pytest) · backend (anthropic) · embeddings (sqlite-vss) · treesitter

# Tests (pytest config in pyproject: testpaths=["tests"], addopts="-q")
uv run pytest                             # full suite
uv run pytest tests/test_invariants.py    # one file
uv run pytest tests/test_grounding.py::test_failed_edge_not_pruned_and_surfaced   # one test

# Engine CLIs (measurement / validation — all deterministic)
uv run python -m kg_engine.pack validate pack/pack.yaml examples/source.md   # pack + glossary coverage
uv run python -m kg_engine.harness agreement   <label_sets.json>             # Krippendorff α
uv run python -m kg_engine.harness specificity <graph.json> <source.md>      # bridge-metric gate verdict
uv run python -m kg_engine.harness ideation     <outputs.json>               # value-of-graph experiment
uv run python scripts/f4_probe.py score labels.csv                           # extraction precision
uv run python -m kg_engine.backend extract                                   # headless build (needs ANTHROPIC_API_KEY)

# Plugin validation
uv run python scripts/validate_plugin.py   # stdlib-only structural hard gate (manifests, components, version agreement)
claude plugin validate ./ --strict         # the real validator, if the CLI is installed
```

**No linter or formatter is configured** (no ruff/black/mypy/flake8). CI (`.github/workflows/ci.yml`)
runs `pytest`, the pack validation, `validate_plugin.py`, and a best-effort `claude plugin validate
--strict`.

## Architecture

### Canon vs. derived — single source of truth + regenerable index
The **canon** is human-editable Markdown, one file per node at `${CLAUDE_PROJECT_DIR}/canon/<node-id>.md`
(YAML frontmatter with the three axes + an `edges:` block, plus free body text). The **derived layer**
(`${CLAUDE_PLUGIN_DATA}/derived/{graph.json,index.sqlite}`) is a NetworkX/SQLite projection that
"contains nothing the canon does not" — disposable and rebuildable. `projector.py` precomputes only
O(1) signals off the hot path (local degree; structural-bridge status from Leiden communities) and is
the **only** writer of the derived layer; read tools rebuild it lazily when stale (content-hash per
node). Verdicts survive a reprojection because `reconciler.py` re-attaches them from an audit log.

### Three-axis provenance — orthogonal, never collapsed to one scalar
Every node/edge carries three independent axes (`model.py` enums); a single "quality" number is
deliberately *not* a thing:
- **provenance** — `span-present` (verbatim citable) | `inferred` (asserted, no span) | `hypothesized` (structural/embedding adjacency)
- **authored_by** — `deterministic` (parser) | `agent` (LLM) | `human`
- **epistemic_state** — `unverified` | `grounded` | `rejected` | `failed` | `obsolete`

A span-present, agent-authored, `unverified` edge is a perfectly normal, honest object. "Is there a
textual anchor?", "who made it?", and "has it survived checking?" are read separately.

### The write boundary and its invariants (`boundary.py`)
The canon is written **only** through `kg_write`, which validates each item into one of four
dispositions — `ACCEPTED` / `DEMOTED` / `QUARANTINED` / `REJECTED` — enforcing:
- **span-present (§1.5):** every non-deterministic edge must carry a `span` that is a normalized
  verbatim substring of the source. No span → `REJECTED:no-supporting-span`; span not found →
  `REJECTED:span-not-in-source` (fabrication). Paraphrasing is fabrication.
- **never-forge-a-verdict (§1.4/§1.8):** extractors emit `unverified` only. A payload claiming a
  verdict (or `authored_by=human`) is silently **DEMOTED**, not honored. Verdicts flow **only**
  through `kg_ground`; out-of-band edits to `epistemic_state` are re-quarantined by the reconciler.
- **deterministic edge identity:** `edge_id = e_{slug(source)}__{slug(relation)}__{slug(target)}`.
  Re-emitting the same edge updates rather than duplicates (idempotent builds).
- **pack vocabulary:** types outside `pack/pack.yaml` (`node_types`/`edge_types`) are `QUARANTINED`,
  never merged into trusted canon.
- **rate limit:** net-new writable edges are capped (`max(64, kb·20)`); deduped edges cost zero, so
  idempotent re-runs never trip it.

`canon.py` makes writes crash-safe (atomic temp+replace for single files; git-stash-as-rollback for
multi-file mutations) and guards the vault with a reclaimable `LeaseLock`.

### Grounding loop with memory of failures
Non-deterministic edges start `unverified`. The grounder re-verifies each span and stamps
`grounded`/`rejected`; the adversarial grounder red-teams hubs with typed `attacked_by` counter-edges
and marks genuinely refuted claims `failed`. **`rejected`/`failed` edges are negative information —
never pruned**, surfaced forever in `kg_context.falsification_counters`. This is what keeps the graph
honest: it remembers what was refuted.

### Bridges and the generality confound
A vague node accumulates spurious high-degree edges and rides many paths for empty reasons. So
**degree is the honest advisory**; specificity-weighted betweenness is computed but **gated** behind
`harness.py` (it only earns promotion when an IDF-specificity check detects the confound *and* rank
churn exceeds the threshold). Grounders reject edges that are "true" only because they are generic
(verdict reason `vague`).

## In-session pipeline

Slash command → subagent(s) → MCP tools. The `creativity-graph` skill (`skills/creativity-graph/`)
bundles these; its `references/{contract,tools,pack-schema}.md` are loaded on demand.

| Command | Subagent(s) | What happens |
|---|---|---|
| `/kg-build` | `kg-extractor` | Section-by-section extraction → `kg_scrub` (egress) → `kg_write` boundary; lands `unverified` edges with verbatim spans |
| `/kg-ground` | `kg-grounder`, `kg-adversarial-grounder` | Drain the unverified queue (`grounded`/`rejected`); red-team hubs, write counter-edges, mark `failed` — all via `kg_ground` |
| `/kg-query` | — | `kg_context` (budgeted, grounding-aware) + structural reads; answers cite all three axes and report falsification counters |
| `/kg-eval` | `kg-annotator` | Stage 4 extraction precision (`f4_probe`) + Stage 7 inter-coder α and the specificity gate (`harness`) |
| `/kg-experiment` | `kg-evaluator` | Blind A/B/C ideation (control vs graph vs rag), scored by `harness ideation` |

Evaluation **measures, never gates** (§4): below-threshold results iterate up to 3× then record the
best and proceed — no human gate blocks the flow. Results are appended to `PROGRESS.md`.

## MCP tool surface (11 tools)

Namespaced `mcp__plugin_creativity-graph_creativity-graph__<tool>`:
- **Mutations (write canon):** `kg_write` (the boundary), `kg_ground` (the *sole* verdict gateway — stamps `verdict_by`/`verdict_at` + audit record), `kg_rename`.
- **Reads (lazily project, then serve derived):** `query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `kg_context`.
- **Utility:** `kg_ping`, `kg_metrics`, `kg_scrub` (egress PII/secret redaction with consistent placeholders; `kg_write` restores placeholders to original text for the canon).

## Configuration

`plugin.json` `userConfig` → engine env (read in `server.py:build_engine_from_env`):
`source_path` → `KG_SOURCE_PATH` (default `examples/source.md`); `sensitivity` →
`CLAUDE_PLUGIN_OPTION_SENSITIVITY` (default `medium`); `metrics_mode` →
`CLAUDE_PLUGIN_OPTION_METRICS_MODE` (default `structure_only`). `.mcp.json` also hardcodes
`KG_PROJECT_DIR`, `KG_DATA`, `KG_PACK_PATH`, and `PYTHONPATH=…/scripts`. **Gotcha:** `domain` exists
in the `userConfig` schema for the enable-time UI but is *not* wired to any env var — the engine never
reads it.

## Releasing

Maintainer checklist for cutting a public release. Everything up to step 4 is automated and
reproducible; **step 4 (public publish + tag) is an outward-facing action a human runs
deliberately** — it is intentionally not automated and not performed by tooling on your behalf.

### 1. Pre-flight (automated)

```sh
pip install -e ".[dev,backend]"
pytest tests/ -q                                   # full suite must be green
python -m kg_engine.pack validate pack/pack.yaml examples/source.md
python scripts/validate_plugin.py                  # manifests parse, components present, versions agree
claude plugin validate ./ --strict                 # the real validator, if the CLI is installed
```

CI (`.github/workflows/ci.yml`) runs the first four on every push/PR; `claude plugin validate
--strict` runs as a best-effort job (it needs the Claude Code CLI, which may be unavailable in a
generic runner — `scripts/validate_plugin.py` is the hard gate).

### 2. Bump the version

Set the **same** version string in both manifests (the structural validator enforces they agree):

- `.claude-plugin/plugin.json` → `version`
- `.claude-plugin/marketplace.json` → the `creativity-graph` entry's `version`

Follow SemVer. Update `CHANGELOG.md`: move items out of `[Unreleased]` under the new version.

### 3. Optional — refresh the graph headlessly (CI / no session)

The in-session path (`/kg-build`) needs no API keys. For an unattended rebuild (e.g. a release
artifact built in CI), use the headless backend with an `ANTHROPIC_API_KEY`:

```sh
export ANTHROPIC_API_KEY=sk-...
export KG_PROJECT_DIR=/path/to/vault
export KG_SOURCE_PATH=examples/source.md
export KG_PACK_PATH=pack/pack.yaml
python -m kg_engine.backend extract            # extract → boundary → canon → project
```

### 4. Publish + tag (manual, outward-facing — run by a human)

These steps push to an external marketplace and are **not** automated:

1. Commit the version bump and changelog on `main`.
2. Create the marketplace entry in the **public** marketplace repo (the bundled
   `.claude-plugin/marketplace.json` is a single-plugin **local/dev** marketplace for
   `--plugin-dir` installs, not the public listing).
3. Tag the release: `claude plugin tag creativity-graph <version>`.
4. `git tag vX.Y.Z && git push --tags`.

Publishing is hard to reverse and makes the release publicly installable — do it only when steps 1–3
are green and the version is final.
