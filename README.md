# creativity-graph

A Claude Code plugin that turns a *non-self-grounding* conceptual document into a
**grounded, queryable knowledge graph** — a human-editable canon with three-axis provenance,
a span-present write boundary, a grounding loop with memory of failures, and a regenerable
NetworkX/SQLite derived layer.

It is **not an idea generator.** The graph does not assert that an idea is good. Idea value
is a *hypothesis under test*: every non-trivial edge starts `unverified`, earns a verdict only
when a grounder (agent or human) re-checks it against the source, and edges that fail are kept
forever as negative information. Whether the graph *actually* helps ideation is itself a
question you measure — see `/kg-experiment` (§Stage 8).

---

## What it is

A prose theory does not verify itself the way code verifies against a parse tree. Its claims
"sound right," so a naive extractor turns it into convincing nonsense: vague nodes that touch
everything, edges no one ever checked, confident verdicts no one ever earned. This plugin
exists to make that failure mode *structurally impossible*.

A deterministic Python engine (`scripts/kg_engine`, 140 tests green) does the work that must be
exact — schema validation, span verification, verdict stamping, projection, scrubbing. The
Claude Code session and its subagents do the **language** work — reading prose, proposing typed
edges, copying spans, arguing the adversarial case — and hand structured JSON back across an
**MCP boundary** that refuses anything it cannot ground.

---

## The canon-vs-derived architecture (§1.2)

Two layers, one source of truth:

- **Canon** — `${CLAUDE_PROJECT_DIR}/canon/<node-id>.md`. One human-editable Markdown file per
  node (YAML frontmatter + free body); directed edges live in the source node's `edges:` block.
  The canon carries the grounding state. It lives **in your project, not in the plugin**, is
  diffable, and is the single source of truth. You can hand-edit it.
- **Derived** — `${CLAUDE_PLUGIN_DATA}/derived/{graph.json,index.sqlite}`. A NetworkX node-link
  graph plus a SQLite index, regenerated from the canon by the projector. It is **disposable**
  and **contains nothing the canon does not**. Delete it and reproject; a verdict in the canon
  *survives* reprojection because the reconciler re-attaches it.

The derived layer precomputes only honest, cheap signals: local **degree** (advisory) and a
labelled **structural-bridge** signal (a node whose neighbors span ≥2 Leiden communities).

---

## The three axes (§1.3) — orthogonal, never collapsed to one scalar

A claim is not "good" or "bad" on a single number. Three independent axes:

| axis | values | answers |
|---|---|---|
| `provenance` | `span-present` \| `inferred` \| `hypothesized` | *Is there a verbatim span to check?* |
| `authored_by` | `deterministic` \| `agent` \| `human` | *Who put this here?* |
| `epistemic_state` | `unverified` \| `grounded` \| `rejected` \| `failed` \| `obsolete` | *Has it survived checking?* |

A span-present, agent-authored edge that is still `unverified` is a perfectly ordinary, honest
object: well-sourced, but not yet a verdict.

---

## The anti-nonsense invariants

These are enforced by the engine — agents cannot opt out:

1. **span-present (§1.5).** Every non-deterministic edge MUST carry a `span` that is a verbatim
   substring of the source (whitespace/case-normalized). No span → `REJECTED/no-supporting-span`.
   A span not found in the source → `REJECTED/span-not-in-source` (fabrication). Spans are copied
   exactly, never paraphrased.
2. **never-forge-a-verdict (§1.4/§1.8).** A write payload may **not** assert
   `grounded`/`rejected`/`failed`, nor `authored_by=human`. Such a payload is `DEMOTED` (verdict
   reset to `unverified`; human stripped to agent). Verdicts are applied **only** through
   `kg_ground`, which stamps `verdict_by`/`verdict_at` and appends an audit record. The
   reconciler re-quarantines any out-of-band verdict edit that lacks a matching audit record.
3. **generality confound → degree advisory + gated specificity metric (§1.6).** A vague node
   accumulates spurious edges and spuriously high betweenness — it "looks central while
   explaining nothing." So **degree** is the honest MVP advisory, and
   *specificity-weighted betweenness* stays **gated** until validated by the harness. Grounders
   must reject edges that are "true" only because they are generic/unfalsifiable
   (verdict reason: `vague`).
4. **memory of failures (§1.7).** `rejected`/`failed` edges are **negative information** — never
   pruned by the projector, surfaced in `kg_context` as `falsification_counters`. The adversarial
   grounder *creates* these: typed `attacked_by` edges plus `kg_ground(verdict="failed")`. A claim
   that collapses into a known failure is rejected on sight.
5. **validation at two points.** Extraction precision is gated **at build time**
   (`f4_probe.py score` ≥ 0.70) and the bridge metric is gated **at ground time**
   (`kg_engine.harness specificity`) before any specificity-weighted ranking is trusted.
6. **PII scrub on egress (§1.9).** `kg_scrub` (the 11th tool) wraps `Scrubber(sensitivity)`:
   secrets (always) + PII (per sensitivity) are redacted with consistent placeholders
   (`⟦SECRET:1⟧` etc.) before text is handed to a subagent. Egress scrubbing is now wired into
   `kg_write` too: span verification restores placeholder spans to the **original** source text
   for the canon (the boundary stores the restored original span), so scrubbing never breaks
   grounding.

> The reason these are invariants and not lint rules: the boundary returns one of
> `ACCEPTED | DEMOTED | QUARANTINED | REJECTED` per item, and the canon is only ever written
> through it.

---

## Install & enable

This is a Claude Code plugin. Add it via the `claude` CLI / plugin marketplace, then enable it:

```bash
claude plugin install creativity-graph@sergiparpal   # from your marketplace
# or, for local development, point Claude Code at this repo as a plugin source.
```

**Updating to the latest version.** If you don't have the latest version of the plugin installed,
update it by running:

```bash
claude plugin update creativity-graph@sergiparpal
```

On `SessionStart` a cross-platform hook (`hooks/provision.mjs` → an OS launcher →
`scripts/bootstrap.py`) provisions an isolated engine venv under `${CLAUDE_PLUGIN_DATA}/.venv` **in a
detached background process**, so it never blocks the session. It uses `uv` when present and falls back
to the stdlib `venv` + `pip` otherwise — only Python ≥3.10 and Node (always present in Claude Code) are
required, on Windows, macOS, Linux, or WSL/Git-Bash. The MCP server (`.mcp.json` → `node
scripts/launch_server.mjs`) self-heals the venv in the foreground if it is spawned before the build
finishes, so it starts cleanly on a fresh machine. See *Installation system* in `CLAUDE.md` for the
full chain.

### userConfig (`.claude-plugin/plugin.json`)

| option | values | default | effect |
|---|---|---|---|
| `source_path` | absolute path | — | the document the graph is built and grounded against. |
| `sensitivity` | `low` \| `medium` \| `high` | `medium` | egress scrubbing: `low` = secrets only; `medium` = + structured PII; `high` = + person/address heuristics. |
| `metrics_mode` | `structure_only` \| `with_embeddings` | `structure_only` | `structure_only` uses graph structure as the bridge signal; `with_embeddings` is accepted but currently inert (the former `sqlite-vss` candidate generator was removed). |

Confirm the server sees your config:

```
mcp__plugin_creativity-graph_creativity-graph__kg_ping()
→ {name, version, metrics_mode, sensitivity, pack_loaded}
```

---

## Component layout

```
creativity-graph/
├── .claude-plugin/plugin.json     # manifest + userConfig
├── .mcp.json                      # MCP server "creativity-graph" (node → launch_server.mjs)
├── commands/                      # slash commands (the orchestration layer)
│   ├── kg-build.md                # /kg-build   — extract → canon → project
│   ├── kg-ground.md               # /kg-ground  — grounding loop + adversarial red-team
│   ├── kg-query.md                # /kg-query   — answer with provenance + counters
│   ├── kg-eval.md                 # /kg-eval    — extractor precision + α reliability (Stages 4/7)
│   └── kg-experiment.md           # /kg-experiment — blind ideation eval (Stage 8)
├── agents/                        # subagents (the language layer)
│   ├── extractor.md               # kg-extractor          → kg_write
│   ├── grounder.md                # kg-grounder           → kg_ground (grounded/rejected)
│   ├── adversarial-grounder.md    # kg-adversarial-grounder → attacked_by + kg_ground(failed)
│   ├── annotator.md               # kg-annotator          → f4_probe labels / α label passes
│   └── evaluator.md               # kg-evaluator          → blind ideation experiment (control|graph|rag)
├── skills/creativity-graph/       # SKILL.md operating guide + references/
├── pack/{pack.yaml,glossary.md}   # the declared vocabulary
├── hooks/                         # SessionStart provisioning + PreToolUse context (cross-platform)
│   ├── hooks.json
│   ├── provision.mjs              # SessionStart dispatcher → provision.sh / provision.ps1
│   ├── provision.sh provision.ps1 # OS launchers → bootstrap.py --background
│   └── precontext.mjs precontext.py
├── examples/source.md             # the demo corpus (a theory of grounded knowledge)
├── scripts/
│   ├── kg_engine/                 # the deterministic engine
│   │   ├── model.py boundary.py canon.py reconciler.py
│   │   └── projector.py scrub.py pack.py harness.py server.py
│   ├── bootstrap.py               # cross-platform self-provisioning installer (uv | venv+pip)
│   ├── launch_server.mjs          # Node MCP launcher (pointer + foreground catch-up)
│   └── f4_probe.py                # extraction-precision scorer CLI
└── tests/                         # pytest suite
```

---

## The workflow

```
/kg-build  →  /kg-ground  →  /kg-query
                   │
        /kg-eval   │   /kg-experiment
   (is it accurate?)   (does it actually help?)
```

### `/kg-build [source_path]` — extract → canon → project
Drives the **kg-extractor** subagent section by section over the (scrubbed) source. Each
section yields a `kg_write` payload of typed nodes and typed edges, every non-deterministic
edge carrying a verbatim span. The boundary accepts/demotes/quarantines/rejects each item; the
command then projects the canon into the derived layer and reports `kg_metrics`. Build-time gate:
run `f4_probe.py score` and require precision ≥ 0.70 before trusting the graph.

### `/kg-ground [query-or-node-filter]` — earn the verdicts (§1.6/§1.7/§1.8)
Drains the queue of `unverified` edges. The **kg-grounder** re-reads each cited span and stamps
`grounded` or `rejected` via `kg_ground` — rejecting relations that are true only because they
are vague/unfalsifiable. The **kg-adversarial-grounder** red-teams hub nodes: it proposes the
strongest typed `attacked_by` counter-edges and, where a claim is genuinely falsified, sets the
attacked edge to `failed`. Those failures become never-pruned negative information, surfaced in
`kg_context.falsification_counters`.

### `/kg-query <question>` — answer from the graph, not from priors
Answers strictly **against the canon**, attaching provenance, epistemic state, and falsification
counters to every supporting edge. Uses `kg_context`, `query_graph`, `get_node`,
`get_neighbors`, and `shortest_path`. An ungrounded edge is reported as such, not laundered into
a confident answer.

### `/kg-eval [graph.json]` — is it accurate? (Stages 4 & 7)
Measures the two things that must be true before you trust the graph: **extraction precision**
and **grounding reliability**. The **kg-annotator** labels extracted edges into a `f4_probe`
CSV (`correct | fabricated | vague | wrong_type`, `span_found`); `f4_probe.py score` reports
precision against the ≥ 0.70 gate. For reliability it produces an *independent* second label
pass and `kg_engine.harness agreement` returns Krippendorff α against the ≥ 0.67 bar. The
numbers are recorded, not hand-waved.

### `/kg-experiment [prompts_path]` — is the graph actually useful? (Stage 8)
A **blind** ideation experiment across three conditions — `control | graph | rag` — scored by
`kg_engine.harness ideation`. This is where "idea value is a hypothesis under test" becomes a
measurement rather than a slogan.

---

## The MCP tool surface

Server name `creativity-graph` ⇒ tools are namespaced `mcp__plugin_creativity-graph_creativity-graph__<tool>`. These
**eleven** tools (`kg_ping`, `kg_scrub`, `kg_write`, `kg_ground`, `kg_rename`, `kg_metrics`,
`query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `kg_context`) are the **only** graph
tools (no `kg_build`/`kg_query`/`kg_project` tools exist — those are slash commands).

| tool | purpose |
|---|---|
| `kg_ping()` | `{name, version, metrics_mode, sensitivity, pack_loaded}` — health + config. |
| `kg_scrub(text=None)` | the §1.9 **egress** scrub → `{scrubbed, redactions, sensitivity, categories}`; redacts secrets (always) + PII (per sensitivity) with consistent placeholders (`⟦SECRET:1⟧` etc.) before text reaches a subagent. No-op (0 redactions) on the no-PII demo source. |
| `kg_write(payload)` | the span-present write boundary → `{dispositions, details[], written_nodes[], rolled_back, error}`; egress scrubbing is wired in here too — placeholder spans are restored to the original source text for the canon. |
| `kg_ground(target_id, verdict, kind, note)` | **the only way to set a verdict** (always attributed to the agent — `by` is not a parameter); `verdict ∈ {grounded, rejected, failed, obsolete}`, `kind ∈ {edge, node}`. |
| `kg_rename(old_id, new_id)` | rename a node and re-key its edges. |
| `kg_metrics()` | `{nodes, edges, edges_by_epistemic_state}`. |
| `query_graph(node_type, relation, epistemic_state, limit)` | filtered `{nodes[], edges[]}`. |
| `get_node(node_id)` | a node dict with its incident edges. |
| `get_neighbors(node_id, relation)` | `[edge dicts]`. |
| `shortest_path(source, target)` | `{path: [node_ids] | null}`. |
| `kg_context(query, budget)` | budgeted context pack: `{items[], approx_tokens, budget, falsification_counters:{failed_or_rejected_edges}, advisory:{signal:"structural-bridge", note, nodes[]}}`. |

### The write payload (Pydantic; extra fields forbidden)

What the extractor emits to `kg_write`, grounded in `examples/source.md`:

```jsonc
{
  "nodes": [
    {"label": "Compression", "node_type": "compression", "file_type": "prose",
     "provenance": "span-present", "authored_by": "agent", "epistemic_state": "unverified",
     "body": "A single idea that stands in for many observations."}
  ],
  "edges": [
    {"source": "generality-confound", "target": "specificity", "relation": "attacked_by",
     "provenance": "span-present", "authored_by": "agent", "epistemic_state": "unverified",
     "span": "a more specific claim, when it holds, defeats a vaguer one",   // VERBATIM substring of the source
     "source_file": "source.md", "confidence": "INFERRED", "confidence_score": 0.6, "notes": ""}
  ],
  "complete": true   // MUST be true; false/missing ⇒ REJECTED as truncated
}
```

- `id` is optional (slugged from `label`). `edge.id` is derived deterministically as
  `e_{source}__{relation}__{target}` (slugged); identity is `(source, relation, target)`.
- The boundary auto-creates a placeholder node for an edge's `source` if absent from `nodes[]`;
  targets may reference not-yet-created nodes.
- `retryable=false` for semantic rejections (no-span, span-not-in-source); `retryable=true` for
  transport failures (truncation, schema-invalid) — so the orchestrator knows whether to retry.

---

## The domain pack (`pack/pack.yaml`)

The declared vocabulary. Types outside these lists are **QUARANTINED** as `undeclared-type`,
never silently accepted.

- **node_types:** `compression`, `primitive`, `claim`, `metric`, `operation`, `failure`
- **edge_types:** `grounds`, `attacked_by`, `reconciles_with`, `bridges`, `collapses_into`,
  `confounded_by`, `approximates`, `defends_against`, `projects`, `survives`

The pack also seeds per-term specificity (IDF-like) so vague terms (`idea` 0.4, `thing` 0.2) are
not mistaken for bridges, while rare terms (`betweenness` 2.4, `specificity` 2.2) can be.
Validate it:

```bash
python -m kg_engine.pack validate pack/pack.yaml examples/source.md   # PackContract + coverage
```

---

## Development

Run from the repo with the engine venv (`/home/sergi/creativity-graph/.venv/bin/python`) or
`uv run`:

```bash
uv sync                                  # provision the engine venv (dev; the plugin runtime uses scripts/bootstrap.py)
uv run pytest tests/ -q                  # → 140 passed
claude plugin validate --strict          # validate the plugin manifest + components
```

Deterministic CLIs used by the commands/agents:

```bash
# Extraction precision (build-time gate)
python scripts/f4_probe.py summary derived/graph.json
python scripts/f4_probe.py sheet   derived/graph.json --n 80 --out labels.csv
python scripts/f4_probe.py score   labels.csv          # PRECISION (gate ≥ 0.70), astrology rate, span-support rate

# Harness (ground-time / experiment gates) — all emit JSON
python -m kg_engine.harness agreement   label_sets.json    # Krippendorff α (≥ 0.67 reliable)
python -m kg_engine.harness specificity derived/graph.json examples/source.md   # bridge-metric gate verdict
python -m kg_engine.harness ideation    outputs.json       # control|graph|rag scoring
```

`f4_probe` verdict vocabulary (the only labels an annotator may emit):
`correct | fabricated | vague | wrong_type`, plus `span_found: y | n`.

### Engine modules (`from kg_engine import ...`)

`model` (enums + `Node`/`Edge` + `span_verifies`) · `boundary` (`validate_payload`) ·
`canon` (`Canon`, atomic git-backed writes) · `reconciler` (re-attach verdicts, re-quarantine
forgeries) · `projector` (`project`, `kg_context`) · `scrub` (`Scrubber`) · `pack`
(`PackContract`, `coverage`) · `harness` (`agreement`/`specificity`/`ideation`) · `server`
(`KGEngine` + FastMCP tool registration).

---

## License

MIT © Sergi Parpal
