# creativity-graph

A Claude Code plugin that turns a *non-self-grounding* conceptual document into a
**grounded, queryable knowledge graph** — a human-editable canon with three-axis provenance,
a span-present write boundary, a grounding loop with memory of failures, and a regenerable
NetworkX/SQLite derived layer.

It both **generates** ideas and **grounds** them — in that order, and never confusing the two.
The graph *generates offensively*: deterministic discovery mechanisms (bridges, residual
connectability, compression, re-partition, hub transplant, cross-construction ensemble) propose
candidates into a separate **hypothesized** lane, never gatekept by a quality metric. Then it
*judges defensively*: the **same** grounding loop is the filter, applied afterward. A generated
candidate is a *hypothesis under test* — `provenance=hypothesized`, `epistemic_state=unverified`,
**no span** — and becomes grounded knowledge only when a grounder supplies support, which *upgrades*
its provenance; the rest are kept forever as negative information that binds the next generation.
Whether generation *actually* helps ideation is itself a question you measure — see
`/kg-experiment` (§Stage 8). The portico that stood at the door of imagination is moved to after it.

---

## What it is

A prose theory does not verify itself the way code verifies against a parse tree. Its claims
"sound right," so a naive extractor turns it into convincing nonsense: vague nodes that touch
everything, edges no one ever checked, confident verdicts no one ever earned. This plugin
exists to make that failure mode *structurally impossible*.

A deterministic Python engine (`scripts/kg_engine`, 496 tests green) does the work that must be
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
6. **PII scrub on egress (§1.9).** `kg_scrub` (the egress scrub tool) wraps `Scrubber(sensitivity)`:
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

This is a Claude Code plugin. From inside Claude Code (works in both the **CLI** and the
**Desktop** app), run these two commands:

```
/plugin marketplace add sergiparpal/creativity-graph
/plugin install creativity-graph@sergiparpal
```

The first command registers this repo as a plugin marketplace (Claude Code reads
`.claude-plugin/marketplace.json` from `github.com/sergiparpal/creativity-graph`); the second
installs the `creativity-graph` plugin from it. Restart the session if prompted so the plugin's
hooks and MCP server load.

> For **local development** instead of installing from GitHub, point Claude Code at this checkout:
> `claude --plugin-dir /path/to/creativity-graph`.

**Updating to the latest version.** If you don't have the latest version installed, update it from
inside Claude Code:

```
/plugin marketplace update sergiparpal
/plugin install creativity-graph@sergiparpal
```

On `SessionStart` a cross-platform hook (`hooks/provision.mjs` → an OS launcher →
`scripts/bootstrap.py`) provisions an isolated engine venv under `${CLAUDE_PLUGIN_DATA}/.venv` **in a
detached background process**, so it never blocks the session. It uses `uv` when present and falls back
to the stdlib `venv` + `pip` otherwise — only Python ≥3.10 and Node (always present in Claude Code) are
required, on Windows, macOS, Linux, or WSL/Git-Bash. The MCP server (`.mcp.json` → `node
scripts/launch_server.mjs`) self-heals the venv in the foreground if it is spawned before the build
finishes, so it starts cleanly on a fresh machine. See *Installation system* in `CLAUDE.md` for the
full chain.

### Troubleshooting install

**`/plugin install` says `Plugin "creativity-graph" not found in marketplace "sergiparpal"` — even
though `/plugin marketplace add` reported success.** This is a **stale marketplace cache**, not a
problem with the published manifest. `marketplace add` reports "Successfully added" even when it reuses
an existing cached clone of the marketplace (under `~/.claude/plugins/marketplaces/<name>/`), so an old
clone that predates the plugin entry hides it from the install lookup. Fix it in order:

```
# 1. Refresh the cached manifest, then retry the install:
/plugin marketplace update sergiparpal
/plugin install creativity-graph@sergiparpal

# 2. Still "not found"? Remove and re-add the marketplace fresh:
/plugin marketplace remove sergiparpal
/plugin marketplace add sergiparpal/creativity-graph
/plugin install creativity-graph@sergiparpal
```

If it *still* fails after that, delete the cached clone (`rm -rf
~/.claude/plugins/marketplaces/sergiparpal`) and restart Claude Code — the in-session marketplace
registry isn't always refreshed until restart — then redo step 2. As a last resort, confirm the machine
can actually reach GitHub (a failed clone silently falls back to cache); anything other than `200` here
is a network/access problem on that machine, not the repo:

```sh
curl -sS -o /dev/null -w "%{http_code}\n" \
  https://raw.githubusercontent.com/sergiparpal/creativity-graph/main/.claude-plugin/marketplace.json
```

### Multi-machine / multi-branch canon merges (optional)

The canon is one Markdown file per node, so two machines (or two branches) editing the **same** node
hand `git` a textual 3-way merge that mangles the `edges:` list and — worse — can silently keep one
side's grounding verdict. A semantic merge driver ships for this: it unions edges by their deterministic
`edge_id` and, when both sides carry the same edge at a **different** `epistemic_state`, resolves the
merged edge to **`unverified`** (clearing `verdict_by`/`verdict_at`) — never to either side's verdict. It
is the out-of-process mirror of the in-engine `Canon._merge_into_existing`.

It is **not** auto-installed (pure git plumbing; the plugin never writes to your repo's git config).
Opt in once per canon-vault clone — add the routing line to the vault's `.gitattributes` (this repo
already ships one) and register the driver:

```sh
echo 'canon/*.md merge=kgcanon' >> .gitattributes
git config merge.kgcanon.name   "creativity-graph canon merge"
git config merge.kgcanon.driver "node ${CLAUDE_PLUGIN_ROOT}/scripts/canon_merge_driver.mjs %O %A %B"
# (substitute the absolute path to the plugin checkout if ${CLAUDE_PLUGIN_ROOT} isn't exported in your shell)
```

After a merge: edges are preserved, conflicting verdicts are demoted to `unverified` — **re-ground the
demoted edges** (`/kg-ground`) to re-earn the verdict. The driver can only ever *write* `unverified` on
a conflict, so it cannot forge a verdict; a verdict that survives a clean merge with no local audit
record is re-quarantined by the per-session reconciler anyway. (Sharing verdicts *across* machines — a
syncable audit log — is a deliberately deferred follow-up; see `CHANGELOG.md`.)

### The install config screen ("Configure creativity-graph")

`/plugin install creativity-graph@sergiparpal` opens a **Configure creativity-graph** screen
listing the four options below and a final **Save configuration** row:

```
❯ Source document path
  Egress sensitivity
  Metrics mode
  Extraction wave size

  Save configuration
```

**These are free-text fields, not menus — there are no preset choices to pick.** Claude Code's
`userConfig` schema has no `enum`/options support, and all four keys are declared `"type":
"string"`, so each renders as a text box you type into (not a selectable list). Move between rows
with **↑/↓**, type a value into a field — or leave it blank to accept its default — then choose
**Save configuration**. Pressing Enter on a row *without* typing just leaves that field at its
default and moves on: harmless for the bottom two, **but not for the source path** (see below).
The **userConfig** table just below is the per-option reference; in short:

- **Source document path — you must set this.** Type the **absolute** path to the Markdown/text
  file you want graphed (or a directory/glob of `.md`/`.txt` files), e.g.
  `/home/you/notes/theory.md`. It has **no default**. Leave it blank and the build runs *without
  error* but produces an **empty, unusable graph**: with no source text to verify against, every
  extracted edge is rejected `span-not-in-source`. (The engine's `examples/source.md` fallback is
  resolved against *your* project directory — where that file does not exist — so it only ever
  fires when you run from inside this repo checkout, never for an installed plugin.) Always enter
  an absolute path here.
- **Egress sensitivity — optional, defaults to `medium`.** Leave blank, or type exactly one of
  `low` / `medium` / `high`. An unrecognized word silently behaves as `medium` (though `kg_ping`
  echoes back whatever you typed), so stick to the three words.
- **Metrics mode — optional, defaults to `structure_only`.** Leave blank. `structure_only` is the
  only value that does anything; `with_embeddings` is **inert** (the engine never branches on it —
  the former `sqlite-vss` path was removed).
- **Extraction wave size — optional, defaults to `6`.** How many section-extractor subagents
  `/kg-build` launches concurrently per wave (bounded parallelism). Type an integer **1–10**; higher
  is faster but applies more rate-limit/lock pressure. Unset / non-numeric / `< 1` falls back to `6`;
  `> 10` clamps to `10`. Unlike the other three, this is an **orchestration** knob the `/kg-build`
  command consumes — the engine never reads it — so changing it needs no server restart, and a one-off
  run can override it inline (`/kg-build <source> <wave_size>`) without touching config.

**Changing it after install.** The values are stored in `settings.json` under
`pluginConfigs["creativity-graph@sergiparpal"].options`. To change them, edit that block (user
scope `~/.claude/settings.json`, or project scope `.claude/settings.json`) and run
`/reload-plugins`, or re-open the configure screen from the `/plugin` menu. Confirm the server
picked up the change with `kg_ping()`, which echoes `{metrics_mode, sensitivity, …}`.

### userConfig (`.claude-plugin/plugin.json`)

| option | values | default | effect |
|---|---|---|---|
| `source_path` | absolute path — a **file, directory, or glob** of `.md`/`.txt` | **none — set this** | the document(s) the graph is built and grounded against. A single file is the common case; a **directory or glob** (R4) builds from every `.md`/`.txt` member, and each edge's span is verified against the specific file it came from (`source_file`). **Effectively required:** there is no default, so until you set it the graph has nothing to verify spans against. *(Markdown/text only — no PDF/media.)* |
| `sensitivity` | `low` \| `medium` \| `high` | `medium` | egress scrubbing: `low` = secrets only; `medium` = + structured PII; `high` = + person/address heuristics. |
| `metrics_mode` | `structure_only` | `structure_only` | the only effective value: graph structure is the bridge signal. The engine never branches on this (it is stored and echoed by `kg_ping` only), and there is no enum constraint — an embeddings path is **not implemented** (the former `sqlite-vss` candidate generator was removed), so any other value is inert. |
| `extract_wave_size` | integer **1–10** (as a string) | `6` | how many `kg-extractor` subagents `/kg-build` launches **concurrently per wave** (bounded parallelism). An **orchestration** knob the command/skill consumes (`${CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE}`) — the engine never reads it, so it is the one `userConfig` key with no `build_engine_from_env` read. Unset / non-numeric / `< 1` → `6`; `> 10` → `10`. Override inline for one run with `/kg-build <source> <wave_size>` (precedence: inline arg > this option > default). Higher = faster but more rate-limit/lock pressure; a wave's brief `kg_write` calls funnel through the one single-threaded MCP server and serialize there, with the canon's single-writer lease as the cross-process safety guarantee, so a build is never dropped or corrupted regardless of size. |

> ⚠️ **Set `source_path` first.** It has no default. If it is left unconfigured, every extracted edge fails
> the span-present check (`REJECTED: span-not-in-source`) because there is no source text to verify against —
> the graph builds but is empty/unusable. Point it at the absolute path of the document you want grounded
> (the bundled `examples/source.md` is only used as a fallback when you run from inside this repo).

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
│   ├── kg-generate.md             # /kg-generate — discovery mechanisms → hypothesized lane
│   ├── kg-perturb.md              # /kg-perturb — external structure + ensemble cross-generation
│   ├── kg-query.md                # /kg-query   — answer with provenance + counters
│   ├── kg-view.md                 # /kg-view    — render graph.html + GRAPH_REPORT.md (read-only)
│   ├── kg-eval.md                 # /kg-eval    — extractor precision + α reliability (Stages 4/7)
│   └── kg-experiment.md           # /kg-experiment — blind ideation eval (Stage 8)
├── agents/                        # subagents (the language layer)
│   ├── extractor.md               # kg-extractor          → kg_write
│   ├── grounder.md                # kg-grounder           → kg_ground (grounded/rejected)
│   ├── adversarial-grounder.md    # kg-adversarial-grounder → attacked_by + kg_ground(failed)
│   ├── generator.md               # kg-generator          → phrase/name candidates → kg_propose
│   ├── annotator.md               # kg-annotator          → f4_probe labels / α label passes
│   └── evaluator.md               # kg-evaluator          → blind ideation experiment (control|graph|graph+generate|rag)
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
│   │   ├── atomicio.py groundaudit.py graphio.py   # IO/durability leaves (atomic writes · §1.8 audit log · node-link IO)
│   │   ├── projector.py scrub.py pack.py harness.py sources.py   # sources.py = R4 multi-doc SourceSet
│   │   ├── generate.py operations.py   # the generative layer (discovery mechanisms + §8 endo ops)
│   │   ├── export.py templates/graph_html.py   # R1 human-facing render (kg_export → graph.html)
│   │   ├── canonmerge.py              # R5 semantic git merge driver for per-node canon
│   │   └── backend.py server.py        # headless extract CLI + FastMCP server
│   ├── bootstrap.py               # cross-platform self-provisioning installer (uv | venv+pip)
│   ├── launch_server.mjs          # Node MCP launcher (pointer + foreground catch-up)
│   └── f4_probe.py                # extraction-precision scorer CLI
└── tests/                         # pytest suite
```

---

## The workflow

```
/kg-build  →  /kg-ground  →  /kg-generate  →  /kg-ground  →  /kg-query
                   │         (generate            (the filter)
                   │          offensively)
        /kg-eval   │   /kg-experiment
   (is it accurate?)   (does it actually help?)
```

**The inversion:** the first half *verifies* (every edge earns a verdict against a span); the second half
*generates* (discovery mechanisms propose `hypothesized` candidates into a separate lane). Generation is
**offensive** — never gatekept by a quality metric — and the **same** grounding loop is the **defensive
filter**, applied afterward. The portico that stood at the door of imagination is moved to after it.

### `/kg-build [source_path] [wave_size]` — extract → canon → project
Drives the **kg-extractor** subagent (defaults to **Sonnet**, fast + cheap) over the (scrubbed)
source — **one subagent per `##` section**, launched in **bounded parallel waves** of `wave_size`
(default `6`, range 1–10; see `extract_wave_size` above). Keeping one section per subagent preserves
span-isolation (each extractor can only cite text it was actually shown); the waves just run several of
those at once, so a 19-section doc is a few waves instead of 19 serial agents. Each section yields a
`kg_write` payload of typed nodes and typed edges, every non-deterministic edge carrying a verbatim
span. The boundary accepts/demotes/quarantines/rejects each item — and a wave's brief writes funnel
through the one single-threaded MCP server (so they serialize there), with the canon's single-writer lease
guarding against cross-process contention, so nothing is dropped or corrupted no matter the wave size.
The command then projects the canon into the derived layer and reports `kg_metrics`. Build-time gate:
run `f4_probe.py score` and require precision ≥ 0.70 before trusting the graph.

### `/kg-ground [query-or-node-filter]` — earn the verdicts (§1.6/§1.7/§1.8)
Drains the queue of `unverified` edges. The **kg-grounder** re-reads each cited span and stamps
`grounded` or `rejected` via `kg_ground` — rejecting relations that are true only because they
are vague/unfalsifiable. The **kg-adversarial-grounder** red-teams hub nodes: it proposes the
strongest typed `attacked_by` counter-edges and, where a claim is genuinely falsified, sets the
attacked edge to `failed`. Those failures become never-pruned negative information, surfaced in
`kg_context.falsification_counters`.

### `/kg-generate [mechanism] [k]` — turn the graph into an idea generator (§2–§9)
Runs the deterministic **discovery mechanisms** over the derived graph and writes their proposals into a
separate **hypothesized lane** — never gatekept by a metric (the inversion). `kg_generate` emits ranked
structural candidates from: **bridge** (§2/§4 — cross-community pairs, generality-controlled),
**seed** (§3 — the positive residual `c − E[c|d]`, "abnormally connectable for its distance"),
**compression** (§7 — dense clusters passing an MDL + specificity screen → a new node),
**regroup** (§8 — bridges invisible under the prior partition), **transplant** (§5 — a hub's reorganising
pattern imported into the most absorptive community), **ensemble** (§9 — cross two constructions). The
**kg-generator** subagent phrases and names them; they land `hypothesized`/`unverified` via `kg_propose`
(or the §8 endo operations `kg_operate`: collapse/explode/regroup/open). The very next `/kg-ground` is the
filter — a candidate is promoted to `grounded` ONLY when a grounder supplies a span/citation (which
*upgrades* its provenance), else it joins failure memory, which then binds the next generation.

### `/kg-perturb [second_source_or_pack]` — import external structure (§9/§15)
Grounding's *second* function. Builds a **second construction** (the same source under a different
pack/resolution, or a second source), then cross-generates (`ensemble`) to surface bridges that exist
across constructions — the structure the graph's own dynamics would resist. This is the only mechanism
that *attacks coverage*; it relocates the blind spot rather than eliminating it.

### `/kg-query <question>` — answer from the graph, not from priors
Answers strictly **against the canon**, attaching provenance, epistemic state, and falsification
counters to every supporting edge. Uses `kg_context`, `query_graph`, `get_node`,
`get_neighbors`, and `shortest_path`. An ungrounded edge is reported as such, not laundered into
a confident answer.

### `/kg-view [html|report|all]` — eyeball the graph (read-only)
Renders two **disposable, human-facing** artifacts under the derived dir via `kg_export`: a
self-contained, fully-offline **`graph.html`** (vanilla-JS force layout encoding the three axes on
**independent** visual channels — `epistemic_state`→edge line, `authored_by`→node border,
`provenance`→fill opacity; **size = degree**; failed/rejected edges **drawn**, never filtered; a
gate-aware bridge highlight) and **`GRAPH_REPORT.md`** (counts straight from `kg_metrics`, per-community
axis breakdowns, the never-pruned falsification memory, R3 stale verdicts, R4 per-file edge counts). A
*view*, never a write — it cannot forge a verdict or touch the canon.

### `/kg-eval [graph.json]` — is it accurate? (Stages 4 & 7)
Measures the two things that must be true before you trust the graph: **extraction precision**
and **grounding reliability**. The **kg-annotator** labels extracted edges into a `f4_probe`
CSV (`correct | fabricated | vague | wrong_type`, `span_found`); `f4_probe.py score` reports
precision against the ≥ 0.70 gate. For reliability it produces an *independent* second label
pass and `kg_engine.harness agreement` returns Krippendorff α against the ≥ 0.67 bar. The
numbers are recorded, not hand-waved.

### `/kg-experiment [prompts_path]` — is the graph actually useful? (Stage 8/9)
A **blind** ideation experiment across four conditions — `control | graph | graph+generate | rag` — scored by
`kg_engine.harness ideation`. The `graph+generate` arm (grounded context **plus** the hypothesized slate from
`/kg-generate`) tests whether the generative layer lifts ideation beyond grounded context alone; the harness
emits a second `generate_verdict` for it. This is where "idea value is a hypothesis under test" becomes a
measurement rather than a slogan.

---

## The MCP tool surface

Server name `creativity-graph` ⇒ tools are namespaced `mcp__plugin_creativity-graph_creativity-graph__<tool>`. The
**thirteen** verify/read tools (`kg_ping`, `kg_scrub`, `kg_write`, `kg_ground`, `kg_rename`, `kg_metrics`,
`query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `kg_context`, `kg_agenda`, `kg_export`) plus the
**four** generative-layer tools (`kg_propose` — the hypothesized write lane; `kg_generate` — the discovery
mechanisms; `kg_operate` — the §8 endo operations; `kg_absorption` — the §14 absorption window) are the
**seventeen** and **only** graph tools (no `kg_build`/`kg_query`/`kg_project` tools exist — those are slash
commands).

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
| `kg_context(query, budget)` | budgeted context pack: `{items[]` (grounded), `hypotheses[]` (the separate hypothesized lane), `approx_tokens, budget, falsification_counters:{failed_or_rejected_edges}, advisory:{signal:"structural-bridge", note, nodes[], bridge_metric, stale_verdicts[]}}`. |
| `kg_agenda(limit=5)` | **read-only** structural "suggested questions" (R6) → `{answerable_now[]` (well-grounded), `blocked_on_grounding[]` (orphans / hypothesized-only / under-grounded hubs / disconnected clusters)`, ranked_by, gate_on, count, note}`. Suggests, never acts; heuristic, not a guarantee. |
| `kg_export(kind="all")` | **read-only** human-facing render (R1) → `{ok, kind, html_path, report_path}`; writes a self-contained offline `graph.html` (three axes on independent visual channels; failed/rejected edges drawn) + `GRAPH_REPORT.md` under the derived dir. `kind ∈ {html, report, all}`. Disposable view, never a write to the canon. |
| `kg_propose(payload)` | the **hypothesized** write lane → the `kg_write` shape `+ {propose_lane, refused_text_claims}`; forces `provenance=hypothesized`, refuses text claims. |
| `kg_generate(mechanism, k, second_graph)` | **read-only** discovery → `{mechanism, k, gate_on, count, candidates[], note}`; `bridge\|seed\|compression\|regroup\|transplant\|ensemble`. |
| `kg_operate(op, …)` | the four §8 endo ops (`collapse\|explode\|regroup\|open`) — write via the propose lane → the `kg_propose` shape `+ {ok, op, info}`. |
| `kg_absorption()` | the §14 absorption window → `{tracked, summary, nodes:{id:{half_life, status}}, note}`. |

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
uv run pytest tests/ -q                  # → 496 passed
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
python -m kg_engine.harness ideation    outputs.json       # control|graph|graph+generate|rag scoring
```

`f4_probe` verdict vocabulary (the only labels an annotator may emit):
`correct | fabricated | vague | wrong_type`, plus `span_found: y | n`.

### Engine modules (`from kg_engine import ...`)

`model` (enums + `Node`/`Edge` + `span_verifies`) · `boundary` (`validate_payload`) ·
`canon` (`Canon`, atomic git-backed writes) · `reconciler` (re-attach verdicts, re-quarantine
forgeries) · `groundaudit` (`GroundAuditLog` — the §1.8 grounding-audit log the reconciler reads for
forge detection) · `atomicio` (`atomic_write_bytes`/`atomic_write_text`) · `graphio` (node-link IO —
`_node_link_data`/`node_link_graph`/`node_attr`) · `projector` (`project`, `kg_context`) · `scrub`
(`Scrubber`) · `pack` (`PackContract`, `coverage`) · `sources` (`SourceSet` — R4 multi-doc ingestion) ·
`harness` (`agreement`/`specificity`/`ideation`) · `generate` (`run_generators` — the six discovery
mechanisms) · `operations` (the four §8 endo ops) · `export` (`build_html`/`build_report`/`export` —
R1 human-facing render) · `canonmerge` (semantic git merge driver — mirror of
`Canon._merge_into_existing`) · `backend` (`BackendExtractor` — headless extract) · `server`
(`KGEngine` + FastMCP tool registration).

---

## License

MIT © Sergi Parpal
