# creativity-graph

A Claude Code plugin that turns a *non-self-grounding* conceptual document into a
**grounded, queryable knowledge graph** — a human-editable canon with three-axis provenance,
a span-present write boundary, a grounding loop with memory of failures, and a regenerable
NetworkX/SQLite derived layer.

It both **generates** ideas and **grounds** them — in that order, and never confusing the two.
The graph *generates offensively*: deterministic discovery mechanisms (bridges, residual
connectability, compression, re-partition, hub transplant, cross-construction ensemble, periphery
exploration) propose candidates into a separate **hypothesized** lane, never gatekept by a quality metric. Then it
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
finishes, so it starts cleanly on a fresh machine. The launcher is a persistent **supervisor**: it logs
every engine lifecycle event to `<KG_DATA>/server.log`, self-heals a crashed engine, isolates a
cancelled request to that one call (the connection stays alive), and turns a dropped stdio transport
into a clean automatic reconnect — no manual `/mcp reconnect`. See
*Installation system* in `CLAUDE.md` for the full chain.

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

## Tutorial — using creativity-graph (no jargon)

A plain-English walkthrough for using the plugin **after it's installed and configured** (the two sections
above). No coding — you'll type a few short commands and ask questions in normal words.

### What this plugin does, in one breath

You give it a document full of ideas — a theory, an essay, a set of notes. It reads that document and builds
a **map of the ideas and how they connect** — and it **checks every connection against your original text**
instead of taking it on faith. It will never tell you "this idea is good." It only tells you which claims
actually hold up against what you wrote, and which ones don't.

Think of it as a careful research assistant who refuses to repeat a claim back to you unless they can point
to the exact sentence that supports it.

> **What "checking" means here — and what it doesn't.** This plugin guarantees **fidelity to your source
> document**, not **truth about the world**. Every check confirms that a connection is actually supported by
> the exact words you wrote — never that what you wrote is correct. If your source is wrong, the map
> faithfully encodes the same error. The checker reads only your document; it never accesses the web or any
> outside knowledge.

### Two ways things happen

1. **Commands you type yourself.** These start with a slash (`/`), like `/kg-build`. You type them straight
   into Claude Code — these are the main buttons you press.
2. **Helpers Claude runs for you.** These have names like `kg_ping` (no slash). You *don't* type these — you
   just ask Claude in plain English ("check that the creativity-graph server is running") and it runs the
   right helper behind the scenes. The slash commands use these helpers automatically.

**Rule of thumb: anything with a `/`, you type. Anything without a `/`, you just ask for in normal words.**

> **About the brackets below.** `<angle brackets>` mean a detail you **must** fill in; `[square brackets]`
> mean an **optional** detail you can leave off for a sensible default. (Same notation as the rest of this
> README.)

### The steps

**Step 1 — Point it at your document.** The map is built from one specific document, set as the `source_path`
option on the configure screen (see *The install config screen* and the *userConfig* table just above).
Just trying it out? The repo ships a demo document at `examples/source.md`.

**Step 2 — Check it's awake.** Before building, make sure the plugin is connected and running. There's no
command to type — just ask Claude something like:

> "Check that the creativity-graph server is running and show me its settings."

Claude runs the health check (`kg_ping`) and reports back the version and your settings. (You can also type
`/mcp` to see every connected server and confirm `creativity-graph` is in the list.)

**Step 3 — Build the map.**

```
/kg-build [source_path]
```

Leave `[source_path]` off to use the document from your settings (Step 1); add a path to build from a
different file just this once. Claude reads your document section by section — several sections at once, so
it's quick — and builds the map. Every connection it draws has to quote an exact phrase from your document;
if it can't, that connection is thrown out. When it finishes, you have a first draft of the map. *(There's
also an optional speed setting, `extract_wave_size`, in the config table above — you can ignore it to start.)*

**Step 4 (optional) — Check how faithfully the map reflects your source.**

```
/kg-eval [graph.json]
```

Grades how faithfully the map reflects your source *before* you rely on it (leave the bracket off to grade
the current map) — it measures extraction fidelity, not whether your source is right. If the score is too
low, the map isn't a faithful rendering of your document yet, and you'd want to revisit your source document.

**Step 5 — Ground the connections in the source.**

```
/kg-ground [query-or-node-filter]
```

The heart of the plugin. It goes through every connection that hasn't been checked yet and decides whether
your source actually supports it — keeping the supported ones, rejecting the vague or unsupported ones, and
even actively trying to *knock down* the strongest claims using the source itself. Anything that fails is kept on record as a known weakness, **not** quietly
deleted. Leave the bracket off to check everything not yet checked; add a topic or area to check just that part.

**Step 6 — Ask questions.**

```
/kg-query <question>
```

The question is **required** — it's the thing you want answered. For example:

```
/kg-query What does the document say about compression?
```

Claude answers using only what's in the map, and for every part of its answer it shows you the supporting
evidence and whether that evidence has been grounded against the source. If something hasn't been checked
yet, it says so instead of pretending it's solid.

**Step 7 (optional) — Test whether it actually helps.**

```
/kg-experiment [prompts_path]
```

Runs a fair test of whether the map genuinely helps you come up with ideas, compared to not using it — so you
find out whether it's worth the effort instead of assuming (leave the bracket off to use the built-in prompts).

### A few things worth knowing

- **Your map is a set of plain files you can edit by hand.** They live in a `canon` folder inside your
  project, one file per idea. Nothing is hidden in a black box.
- **You can't break it by deleting the working copy.** The plugin keeps a separate, regenerable copy for
  speed; if it ever gets messy, you can rebuild it and lose nothing.
- **The usual order is:** point at a document → build → (check) → ground → ask questions. The two optional
  steps (`/kg-eval`, `/kg-experiment`) are about measuring quality, not everyday use.

### Going further — turn the map into an idea generator (optional)

So far you've **built** a map and **grounded** it. You can also flip it around: instead of only
*checking* what's already in your document, ask the map to **propose new ideas** from the way its concepts
connect — and then ground those the exact same way. The design idea is **generate freely, judge
strictly**: new candidates are never accepted just because they look clever; they only "stick" once grounding
finds support for them in your document, and the ones that fail are remembered, not deleted. Three optional commands:

- **`/kg-generate [mechanism] [k]`** — ask the map to suggest new idea candidates from its own structure (for
  example, surprising links between distant topics, a single idea that could absorb a whole cluster of
  others, or — via the **periphery** mechanism — links *out of* the map's neglected, low-degree edges that the
  hub-seeking mechanisms ignore). They land as clearly-marked *proposals* — then the very next `/kg-ground` is
  the filter that keeps only the ones it can support. Each proposal also shows a `convergence` count (how many
  distinct mechanisms independently suggested it) — an advisory hint for which to ground first, never a verdict
  and only allowed to *reorder* grounding once a harness check proves it predicts grounding success.
- **`/kg-query`** can also trace **`kg_explain_path`** — *how did these two concepts come to be connected, over
  links that have actually been verified?* It walks **grounded edges only** and reports the chain length as an
  advisory "creative-leap" signal; if the only connection runs through unverified guesses, it says so honestly.
- **`/kg-perturb [second_source_or_pack]`** — build a *second* version of the map (a different angle on the
  same material, or a second document) and cross-compare, to surface connections the first map would have
  missed on its own.
- **`/kg-view [html|report|all]`** — produce a self-contained, offline **`graph.html`** you can open in a
  browser, plus a written **`GRAPH_REPORT.md`**, so you can *see* the map (rejected claims and all) instead
  of only querying it. It's a read-only snapshot — it never changes the map.

### Command cheat sheet

The first five are the everyday flow; the last three are the optional "going further" commands above.

| Command | What it does | The detail in brackets |
| --- | --- | --- |
| `/kg-build [source_path]` | Build the map of ideas | *Optional:* a document to build from (defaults to your settings) |
| `/kg-ground [query-or-node-filter]` | Ground the connections in the source | *Optional:* limit it to one topic or area (defaults to everything) |
| `/kg-query <question>` | Ask a question, answered from the map | **Required:** your question |
| `/kg-eval [graph.json]` | Grade how faithfully the map reflects the source | *Optional:* which map file to grade (defaults to the current one) |
| `/kg-experiment [prompts_path]` | Test whether the map really helps | *Optional:* a file of test prompts (defaults to the built-in set) |
| `/kg-generate [mechanism] [k]` | Propose new idea candidates from the map | *Optional:* which mechanism, and how many (defaults to a sensible set) |
| `/kg-perturb [second_source_or_pack]` | Stress-test coverage with a second map | *Optional:* a second document or setup (defaults to a re-angle of the same source) |
| `/kg-view [html\|report\|all]` | Make a visual + written view of the map | *Optional:* which artifact to render (defaults to both) |

Remember: the status check (`kg_ping`) and the other behind-the-scenes helpers have **no** slash; you just ask
Claude for them in plain words.

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
│   └── evaluator.md               # kg-evaluator          → blind ideation experiment (control|graph|graph+generate|rag · +optional lightrag)
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
│   ├── launch_server.mjs          # Node MCP supervisor (pointer + catch-up; logs lifecycle, self-heals)
│   └── f4_probe.py                # extraction-precision scorer CLI
└── tests/                         # pytest suite
```

---

## The MCP tool surface

Server name `creativity-graph` ⇒ tools are namespaced `mcp__plugin_creativity-graph_creativity-graph__<tool>`. The
**sixteen** verify/read tools (`kg_ping`, `kg_scrub`, `kg_write`, `kg_ground`, `kg_rename`, `kg_merge`, `kg_metrics`,
`kg_status`, `query_graph`, `get_node`, `get_neighbors`, `shortest_path`, `kg_explain_path`, `kg_context`, `kg_agenda`,
`kg_export`) plus the **four** generative-layer tools (`kg_propose` — the hypothesized write lane; `kg_generate` — the
discovery mechanisms; `kg_operate` — the §8 endo operations; `kg_absorption` — the §14 absorption window) are the
**twenty** and **only** graph tools (no `kg_build`/`kg_query`/`kg_project` tools exist — those are slash
commands).

| tool | purpose |
|---|---|
| `kg_ping()` | `{name, version, metrics_mode, sensitivity, pack_loaded}` — health + config. |
| `kg_scrub(text=None)` | the §1.9 **egress** scrub → `{scrubbed, redactions, sensitivity, categories}`; redacts secrets (always) + PII (per sensitivity) with consistent placeholders (`⟦SECRET:1⟧` etc.) before text reaches a subagent. No-op (0 redactions) on the no-PII demo source. |
| `kg_write(payload, idempotency_key=None)` | the span-present write boundary → `{dispositions, details[], written_nodes[], rolled_back, error, receipt}`; egress scrubbing is wired in here too — placeholder spans are restored to the original source text for the canon. Every response carries a deterministic `receipt` (a hash of the payload's target ids); an optional `idempotency_key` makes a retry of a write whose transport response was lost a true no-op that replays the same receipt (`idempotent_replay: true`) — never a duplicate. |
| `kg_ground(target_id, verdict, kind, note)` | **the only way to set a verdict** (always attributed to the agent — `by` is not a parameter); `verdict ∈ {grounded, rejected, failed, obsolete}`, `kind ∈ {edge, node}`. |
| `kg_rename(old_id, new_id)` | rename a node and re-key its edges (STRICT: refuses if `new_id` already exists). |
| `kg_merge(from_id, into_id)` | deliberately **merge** `from_id` into an existing `into_id`, then retire `from_id`. Rewrites every endpoint and **dedups** colliding edges — `failed`/`rejected` negative info is sticky (never pruned), else `grounded`>`unverified`, the verbatim span + verdict note are kept, and no verdict/span is ever forged; drops the self-loops the rewrite makes; refuses a merge across two different declared `node_type`s. → `{ok, from, into, touched[], edges_rewritten, edges_deduped[], self_loops_dropped[], nodes, edges}`. |
| `kg_metrics()` | `{nodes, edges, edges_by_epistemic_state}`. |
| `kg_status()` | cheap, **projection-FREE** status + coverage probe (reads only the canon, never opens the derived db) → `{ok, version, nodes, edges, edges_by_epistemic_state, nodes_by_epistemic_state, unverified_edges, coverage:{files[],sections[]}, derived_present, projection_degraded}`; reports the `unverified` grounding-queue size and which source files/`##` sections already have an anchored edge — for confirming progress and **resuming a partial build** after a transport hiccup. |
| `query_graph(node_type, relation, epistemic_state, limit)` | filtered `{nodes[], edges[]}`. |
| `get_node(node_id)` | a node dict with its incident edges. |
| `get_neighbors(node_id, relation)` | `[edge dicts]`. |
| `shortest_path(source, target)` | `{path: [node_ids] | null}`. |
| `kg_explain_path(nodes)` | **read-only** egress (§2) → `{path[], edges[{source,target,relation,span}], leap, grounded_only, reason?}`; traces the associative chain connecting `nodes` over **grounded edges only** (>2 nodes ordered by a deterministic TSP over the grounded shortest-path closure). `leap` (= path length) is an **advisory** "creative-leap" signal — never a verdict, never written, never a score. Empty `path`/`leap=null` with a `reason` when no fully-grounded path exists. |
| `kg_context(query, budget)` | budgeted context pack: `{items[]` (grounded), `hypotheses[]` (the separate hypothesized lane), `approx_tokens, budget, falsification_counters:{failed_or_rejected_edges}, advisory:{signal:"structural-bridge", note, nodes[], bridge_metric, stale_verdicts[]}}`. |
| `kg_agenda(limit=5)` | **read-only** structural "suggested questions" (R6) → `{answerable_now[]` (well-grounded), `blocked_on_grounding[]` (orphans / hypothesized-only / under-grounded hubs / disconnected clusters)`, ranked_by, gate_on, count, note}`. Suggests, never acts; heuristic, not a guarantee. |
| `kg_export(kind="all")` | **read-only** human-facing render (R1) → `{ok, kind, html_path, report_path}`; writes a self-contained offline `graph.html` (three axes on independent visual channels; failed/rejected edges drawn) + `GRAPH_REPORT.md` under the derived dir. `kind ∈ {html, report, all}`. Disposable view, never a write to the canon. |
| `kg_propose(payload)` | the **hypothesized** write lane → the `kg_write` shape `+ {propose_lane, refused_text_claims}`; forces `provenance=hypothesized`, refuses text claims. |
| `kg_generate(mechanism, k, second_graph)` | **read-only** discovery → `{mechanism, k, gate_on, count, candidates[], note}`; `bridge\|seed\|compression\|regroup\|transplant\|ensemble\|periphery` (or `all`/`default`). Each candidate carries an advisory `convergence` (# distinct mechanisms that independently proposed it) — a ranking prior for the grounding queue, never a score and never written to the canon. |
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
python -m kg_engine.harness ideation    outputs.json       # control|graph|graph+generate|rag scoring (+optional lightrag arm)
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
`harness` (`agreement`/`specificity`/`ideation`/`convergence`) · `generate` (`run_generators` — the seven
discovery mechanisms) · `operations` (the four §8 endo ops) · `export` (`build_html`/`build_report`/`export` —
R1 human-facing render) · `canonmerge` (semantic git merge driver — mirror of
`Canon._merge_into_existing`) · `backend` (`BackendExtractor` — headless extract) · `server`
(`KGEngine` + FastMCP tool registration).

---

## License

MIT © Sergi Parpal
