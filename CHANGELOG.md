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

## [0.6.0] — 2026-06-30

Three additions, all **below the grounding boundary** — additive, backward-compatible, and respecting
every anti-nonsense invariant (no forged verdicts, no span-less written edges, no composite scalar,
generation never gated on a metric, fully deterministic).

### Added

- **`periphery` discovery mechanism (§5 "Explore the Periphery").** A seventh deterministic generator
  that sources candidates from the graph's **low-degree** nodes — the periphery the hub-seeking
  mechanisms (`bridge`/`transplant`) and the residual-rich `seed` pairs structurally never reach. For
  each peripheral source it proposes a `bridges` edge (an existing pack edge_type — no new type) to a
  non-adjacent anchor maximising shared-neighbour connectability, tie-broken toward the more specific
  anchor (§4 generality control) and failure-memory-aware (§13). The peripheral band is **adaptive** —
  the bottom quartile of the live degree distribution (25th-percentile by the nearest-rank rule:
  deterministic, no interpolation, corpus-size-independent). It is **ALL-only** (in `ALL_SET`, not
  `DEFAULT_SET`), so the default `/kg-generate` slate and every golden expectation stay byte-identical.
  Pure, read-only, hypothesized-lane only.
- **`kg_explain_path` read-only egress (§2).** A new MCP tool (the 20th) + `KGEngine.explain_path` that
  traces the associative chain connecting two-or-more concepts **over `grounded` edges only**, carrying
  each hop's relation + verbatim span for audit, and reports the path edge-count as an **advisory**
  `leap` ("creative-leap" / creative-distance) signal. For >2 nodes the visiting order comes from a
  deterministic nearest-neighbour walk (a TSP approximation) over the grounded shortest-path closure —
  byte-stable across processes via a (distance, id) tie-break, no external solver, no new dependency.
  When no fully-grounded path exists it returns an empty
  path + an honest `reason` (itself informative: the concepts are joined only through unverified /
  hypothesized / refuted links). `leap` is **never** a verdict, **never** written to the canon, **never**
  a score — it lives only in the tool response.
- **Advisory `convergence` count across mechanisms (§4 "CONVERGENT", adapted).** When `kg_generate` runs
  multiple mechanisms, each surviving candidate now carries `convergence` — the number of *distinct*
  mechanisms that independently proposed the same (orientation-independent) edge. It is a **ranking prior
  for the grounding queue** (which hypotheses to ground first), **never** folded into `Candidate.score`
  and **never** written onto a canon edge. A new **`harness.convergence`** gate (structurally mirroring
  `harness.specificity`) decides — from a history of past candidates' convergence + grounding outcome —
  whether higher convergence actually predicts grounding success before convergence is ever allowed to
  *reorder* the queue; until the gate passes it is displayed but decides nothing (advisory by default).
  The degraded-ensemble path (no second construction) is folded back into `regroup` so it can never
  inflate the count. CLI: `python -m kg_engine.harness convergence`.

### Deferred

- **Persisted convergence history → automatic projector-level gate.** This release keeps the convergence
  gate a *harness verdict the `/kg-ground` command consults and reports*, not a stored projector flag —
  the engine does not yet persist grounding history in a harness-readable form. Persisting that history
  so the gate can be computed automatically at projection time (as the specificity gate's `gate_on` is)
  is a deliberately deferred follow-up.

## [0.5.4] — 2026-06-28

### Added

- **Stronger model pinned on the two adversarial reasoning roles.** `agents/grounder.md` (verifies the
  unverified queue, rejects vague/unfalsifiable relations) and `agents/adversarial-grounder.md`
  (red-teams hub nodes, sets genuinely falsified edges to `failed`) now carry `model: opus` in their
  frontmatter — the `opus` alias, mirroring how `extractor.md` already pins `model: sonnet`. These are
  the roles where model capability most changes a verdict; previously they silently inherited the
  session default. Frontmatter-only change: no agent body, tool list, or graph data touched, and no
  new verdict/edge path — verdicts still flow only through `kg_ground`. `extractor.md` stays on
  `sonnet`; no other agent gains a `model:` line.
- **Optional `lightrag` GraphRAG arm in the blind ideation experiment (§Stage 8).** A fifth experiment
  arm, `lightrag`, compares the graph against a real, published **GraphRAG** baseline (LightRAG,
  `lightrag-hku`) instead of only the flat-grep `rag` strawman — so "grounding-with-falsification is
  worth it" can stand against a strong graph-retrieval system. It is **add-only and off by default**:
  enabled only when `KG_LIGHTRAG=1` **and** the `lightrag-hku` package is installed **and**
  `OPENAI_API_KEY` is set; otherwise the evaluator omits the arm and the original four-arm experiment
  runs unchanged. The arm is built from the **same** `examples/source.md` corpus the `rag` arm uses and
  is **structure-blind** — it reads flat prose through LightRAG's own retrieval and never touches the
  canon's `epistemic_state`, bridges, falsification counters, degree, or any `kg_*` output. The
  integration is quarantined in a new isolated module `scripts/kg_engine/lightrag_arm.py` (no other
  engine module imports it; the `lightrag` import is function-local) exposing a `check`/`answer` CLI;
  its working store lives under the gitignored derived dir and is disposable. `harness.ideation` is now
  arm-tolerant — it scores every condition key present (canonical order, missing optional arms simply
  absent, never an error) and emits a `lightrag_verdict` (graph-vs-LightRAG) when the arm is present.
  `evaluator.md` / `commands/kg-experiment.md` document the opt-in (install, env vars, cost); a new
  `lightrag` extra is declared in `pyproject.toml`. New no-network coverage in `tests/test_harness.py`.

### Fixed

- **Lease release no longer orphans the lock on a transient Windows sharing violation.** On Windows
  `os.replace()` of the lock file raises `ERROR_SHARING_VIOLATION` (a `PermissionError`) while another
  session momentarily has it open for read — which the spinning waiters of a full parallel `/kg-build`
  wave do constantly (Python's `open()` does not grant `FILE_SHARE_DELETE`). `LeaseLock.release()` caught
  that as `except (FileNotFoundError, OSError): return`, mistaking a *transient* violation for "already
  gone" and returning **without dropping the lock**. Because the orphaned record names a foreign host
  (never pid-probed stale, only TTL-stale), it then blocked every other waiter for the full 120 s TTL —
  far past the 30 s acquire budget — so a whole wave's writers spuriously failed with `canon vault is
  locked by another live session`. The rename-aside CAS in `release()` and `_reclaim_stale()` now retries
  the transient violation within a bounded budget (`LOCK_REPLACE_RETRY_TIMEOUT`, the reader closes within
  ms), `FileNotFoundError` still propagates immediately (genuinely reclaimed), and a *persistent* error
  still degrades gracefully (the TTL is the backstop). Fixes the flaky Windows CI failure of
  `test_full_wave_of_concurrent_writers_all_commit_none_corrupted`; new coverage in `tests/test_fix_canon.py`.
- **A whole node (and its §1.7 failure memory) no longer vanishes from every read on a hand-edited
  null `id:`.** `node_from_markdown` resolved the node id twice with different fallbacks: the edge-source
  default used `fm.get("id", fallback_id)` (which returns `None` when the `id:` key is *present but null*,
  a plausible hand-edit `id:` / `id: null`), while the node id itself used `... or fallback_id or ...`. A
  source-less edge under such a note then hit `Edge.__init__` with `source=None` and raised, and
  `Canon.all_nodes` swallows a per-note parse error — so the entire node, including any never-pruned
  `failed`/`rejected` counter-edges, silently disappeared from every read (§1.2/§1.7). The id is now
  resolved once and reused for both. New coverage in `tests/test_fix_reconciler.py`.
- **Edge-only writes no longer clobber a node's human label with its bare id.** `Canon._merge_into_existing`
  had `cur.label = node.label or cur.label`, which never fired its guard — `Node.__post_init__` defaults an
  empty label to the id, so `node.label` is never falsy. Any edge-only merge (a grounder counter-edge, a
  later extraction wave referencing an earlier node) whose auto-created placeholder is labelled with the id
  therefore overwrote the existing label (e.g. `Free Energy Principle` → `fep`). Now guarded the same way as
  `node_type` (`if node.label and node.label != node.id`).
- **Self-loop edges no longer inflate `degree` or falsely flag `structural_bridge`.** `und.neighbors(n)`
  includes `n` itself for a self-loop, so the persisted distinct-neighbour `degree` (which drives
  `query_graph` ranking, the `kg_agenda` hub detector, and the bridge advisory) counted a node's own
  self-loop as a connection. Both the degree and bridge-community computations now exclude the node from
  its own neighbour set (`projector.py`).
- **`bootstrap.py` no longer installs into — or deletes on failure — a user's pre-existing venv.**
  `_looks_like_our_venv` treated *any* virtualenv as "ours" (every venv has `pyvenv.cfg`), so pointing
  `KG_ENGINE_VENV`/`--venv` at an existing venv would scaffold deps into it and `shutil.rmtree` it on a
  failed install. Ownership for the purpose of deletion is now keyed only on the engine's own markers
  (`engine-python.txt` / `install.stamp`) or "created this run", never bare `pyvenv.cfg`; a populated
  foreign venv is refused up front. New coverage in `tests/test_bootstrap.py`.
- **The MCP supervisor distinguishes a fast post-init engine crash from a startup crash.** A tool call
  that crashed the engine within the 5 s early-failure window was misclassified as a startup failure and
  relaunched in place onto the already-handshaked stdio pipe, stranding the client on a dead-but-alive
  connection. The engine now writes a readiness marker (`.engine-ready`, via a FastMCP lifespan) as its
  serve loop comes up; `launch_server.mjs` consults it as the authoritative post-init-vs-startup signal
  (wall-clock remains the fallback when the marker is absent), so a post-init crash now exits cleanly and
  the client reconnects with a fresh handshake. New coverage in `tests/test_launchers.py`.
- **The optional `lightrag` experiment arm runs init and all queries on a single event loop.** It
  previously ran `initialize_storages()` in one `asyncio.run` loop (which then closed) and each query in a
  fresh loop, so every `aquery` awaited cross-loop against loop-bound storage and would fail whenever the
  arm was enabled; it also never released the storages and surfaced a missing source as a raw traceback.
  Init + queries now share one loop with `finalize_storages()` in a `finally`, and a bad `--source` maps to
  the clean "arm unavailable" exit code.
- **Review-driven robustness & hygiene hardening pass** (no behaviour change on the happy path): tool
  error strings are now scrubbed to the §1.9 egress standard before crossing back to the session (a raw
  exception could quote a vault path or un-scrubbed canon content); idempotent `kg_write` replays
  deep-copy their cached receipt; `kg_status` coverage scanning is bounded (no files×sections×spans
  worst case); `kg_ground` strips/canonicalizes its `verdict`/`kind`/node-id inputs like the other
  mutators; the canon git commit drops `--allow-empty` (no more empty no-op commits); `canonmerge`'s
  surrogate-escape decode now fails *open* instead of crashing on an encode mismatch; `slug()` escapes
  Windows reserved device names (`con`, `nul`, `com1`, …); `_rollback` never raises mid-rollback;
  `graphio`'s reader fallback is symmetric with the writer's `links` key; the projector closes its SQLite
  connection on a schema-heal failure, re-checks columns inside the transaction, degrades on a source-read
  error, and `shortest_path` uses an O(V+E) predecessor-map BFS; several JSON reads pin `encoding="utf-8"`;
  `generate`'s bridge-strength is precomputed once per node; `scrub` validates `extra_terms` categories
  against the placeholder grammar; the audit log compensates an orphan record when the write *raises* (not
  only when it returns failure); the interpreter-probe `spawnSync` calls carry timeouts; a Windows
  `SIGTERM` shutdown exits the supervisor with code 0; and the canon merge driver bounds its subprocess
  and reports a spawn error.

### Security

- **The §1.8 grounding-verdict replay defense is now durable across loss of the reconcile-state cache.**
  The spend ledger (`consumed`) and re-quarantine baseline (`epistemic`) lived only in the git-ignored,
  fail-open `.kg-reconcile-state.json` cache. Deleting it reset `consumed` to empty while the append-only
  audit log survived, so every historical `kg_ground` record looked unspent again — letting a
  previously-grounded-then-demoted edge be re-forged out of band and accepted as a "legitimate transition"
  (an attacker within §1.8's own threat model could delete an obviously-disposable cache file rather than
  the trust-rooted log). The reconciler now writes a durable spend-ledger **checkpoint** into the
  append-only audit log (the trust root) at the end of each full sweep and **recovers** `consumed`/
  `epistemic` from it when the cache is missing, so an already-spent record can no longer justify a replay.
  Recovery merges safe-high (over-strict re-quarantine, never a missed forgery), genuine grounded verdicts
  survive cache loss, and a first-ever run with no checkpoint is unchanged. New coverage in
  `tests/test_fix_reconciler.py` (replay caught, legit verdict preserved) and `tests/test_groundaudit.py`.

## [0.5.3] — 2026-06-27

A new write tool: **`kg_merge`** — the deliberate node-merge that `kg_rename` (deliberately) refuses. A
grounding/reconcile pass that finds two nodes naming the same concept (e.g. `dpp` and `dpp-selection`, both
carrying a grounded `defends_against → collapse-toward-typical` edge) previously had no honest way to
collapse them: `kg_rename` errors on a target-id collision, `kg_operate` only writes new hypothesized
structure, and there was no merge/delete primitive — so the only "cleanup" path forged a verdict. `kg_merge`
closes that gap without weakening `kg_rename` or touching verdict semantics.

### Added

- **`kg_merge(from_id, into_id)` (MCP tool + `KGEngine.kg_merge`).** Folds `from_id` into the existing
  `into_id` (both must exist) and retires `from_id`. It is a **distinct verb** from `kg_rename`, which stays
  strict (still errors on `target id exists`) — a name collision is never silently a merge.
  - **Edge-id collision ⇒ dedup, never duplicate/error.** When the `from_id`→`into_id` rewrite collapses
    two edges onto one canonical `edge_id`, they are coalesced with negative-information-sticky precedence:
    if either side is `failed`/`rejected` the merged edge keeps that state (§1.7 — never pruned), else
    `grounded` beats `unverified`; the verbatim span + stored verdict note are kept and `span-present`
    provenance is preferred. The merged state is always one a real edge already held — **no verdict or span
    is ever forged, upgraded, or invented** (§1.4/§1.8). The dedup is order-insensitive (deterministic).
  - **Self-loops** the rewrite creates (`source == target`) are dropped and reported.
  - **Typing guard:** keeps `into_id`'s `node_type`/`label`; **refuses** a merge across two *different
    declared* node types (`node_type conflict — refusing to merge`) so a wrong merge can't corrupt typing.
  - **Verdict survival:** a surviving verdict whose `edge_id` changed is re-keyed via the same id-migrating
    `GroundAuditLog` record as `kg_rename`, so the reconciler's §1.8 forgery sweep does not re-quarantine it.
    Like the other writers, `kg_merge` touches only the canon — never the projection seam.
  - Returns `{ok, from, into, touched[], edges_rewritten, edges_deduped[], self_loops_dropped[], nodes, edges}`.
- **`/kg-ground` Stage-6 merge checkpoint now executes the merge** via `kg_merge` (added to the command's
  `allowed-tools`) instead of deferring it to an out-of-band `kg_rename`/reconcile step.
- **Tests:** `tests/test_merge.py` — collision dedup (grounded > unverified, span/note kept), sticky
  `failed`/`rejected` negative information, self-loop drop, node-type-conflict refusal (no mutation),
  `kg_rename` still strict on target-exists, determinism/idempotency, and verdict-survives-reconcile.

The MCP tool surface is now **19 tools** (was 18). No existing tool's semantics changed; `kg_ground`
remains the sole verdict channel.

## [0.5.2] — 2026-06-27

A Stage-4 extraction-precision fix: tighten how the LLM extractor assigns relation **direction** and
**type**. Prompt/pack only — no engine logic changed; every hard guarantee still lives in the `kg_write`
boundary. The diagnosed dominant Stage-4 miss (precision 0.61 at span-support 0.94) was a *correct
verbatim span* carried on a **reversed** directed relation (`grounds`/`attacked_by`/`defends_against`) or
an **over-claimed** region-spanning one (`bridges` 0/5, `projects` 0/3 — instance-of / "reveals" /
paired concepts mislabeled). The boundary verifies the span, not the direction, so the direction has to
be taught in the prompt.

### Changed

- **`agents/extractor.md` — new "Relation DIRECTION is load-bearing" section.** A per-relation HEAD/TAIL
  role table covering all ten edge types; a "do not reach for a region-spanning relation when the prose
  says something narrower" guard (an instance offered as evidence → `grounds` while a bare taxonomic
  *is-a* is dropped; paired concepts emit no edge unless a real tension is stated; "reveals" ≠
  `projects`; complement/contrast ≠ `reconciles_with`); and a worked reversal anchored on a verbatim §2
  span (`Span-present provenance *grounds* a claim`).
- **`pack/pack.yaml` — edge-type comments rewritten as HEAD/TAIL role definitions.** The ten edge-type
  *names* are unchanged, so the validated pack contract is identical and `pack.yaml` `version` stays
  `0.1.0`; only the human-reference `#` comments changed (they document the same directional rule the
  extractor enforces, and never reach the model).

The precision gate (`f4_probe`, ≥ 0.70) must be re-measured against the real build corpus: the bundled
`examples/source.md` demo already scores 1.00 and is too small (one `bridges`, one `projects` edge) to
exhibit the regression this fix targets.

## [0.5.1] — 2026-06-26

A follow-up to the transport/cancellation resilience pass: close one more way the MCP server could
"disconnect" — a `git` subprocess wedging a tool handler until the watchdog force-exits the engine
(**exit 71**).

### Fixed

- **`projector._head()` can no longer wedge projection on a non-git canon or a hung `git`.** `_head()`
  runs inside `_project_locked` on every real reprojection and shelled out to `git rev-parse HEAD` with
  no timeout, no stdin redirect, and no non-git guard. In the **detached MCP server process** that git
  call could block forever when the canon lives on a non-git filesystem (e.g. a cloud-synced `Documents`
  folder) or when git tried to prompt with no attached console — the handler then exceeded
  `KG_HANDLER_TIMEOUT` and the supervisor watchdog killed the engine with **exit 71**, closing the MCP
  connection on the next *stale* reprojection (every `kg_export`/`kg_metrics`/`kg_generate`/… after any
  canon change). `_head()` now returns `""` immediately when the canon has no `.git` (never forking
  git), and hardens the git-repo case with `timeout=5`, `stdin=DEVNULL`, and
  `GIT_TERMINAL_PROMPT=0`/`GIT_OPTIONAL_LOCKS=0`, degrading to `""` on any timeout/spawn failure.
- **Same hardening applied to every other git invocation in the engine.** `canon._git` (used by the
  best-effort canon-commit path of `kg_write`/`kg_rename` and by `_git_ok`) now bounds the wait, detaches
  stdin, and disables terminal prompts/optional locks, degrading a hung/absent git to a non-zero result
  (every caller passes `check=False` and reads `.returncode`, so a wedged git reads as "git unavailable"
  → skip the commit, never a hung handler). `canonmerge._git_merge_file` (the merge-driver 3-way merge)
  gains the same `timeout`/`stdin=DEVNULL` posture and now also recovers from `TimeoutExpired`.

## [0.5.0] — 2026-06-26

A **transport / cancellation resilience** pass. Over a full 19-section build the MCP server would
"disconnect" / hang — tool calls stuck in *Running…*, the server marked disconnected, needing a manual
`/mcp reconnect` or app restart — even though every canon write actually committed to disk. The work
succeeded server-side; the *result* failed to return and the process died with no trace. Root cause:
broken cancellation/transport handling in the stdio server (a client cancelling a request mid-flight, now
more frequent because `/kg-build` fires several concurrent requests per wave), compounded by **zero
persisted diagnostics**. This release is defense-in-depth: make the plugin crash-proof, self-healing,
idempotent, resumable, and projection-decoupled so a lost response or a dead transport is survivable.

### Added

- **`scripts/launch_server.mjs` is now a SUPERVISOR, not a one-shot launcher.** The Node process is a
  persistent parent that spawns the Python engine as a child, logs every lifecycle event, and recovers
  according to *when* the engine died — because with `stdio:"inherit"` Node cannot replay MCP's
  per-connection `initialize` handshake, so the two cases are genuinely different:
  - A **startup failure** (crash before the engine served `initialize`, e.g. an import error against a
    half-built venv) leaves the client's `initialize` buffered, unread, on the inherited stdin — so a
    one-time venv **heal + in-place relaunch** (capped exponential backoff 200 ms → 5 s; **crash-loop
    guard** of ≤ 5 startup retries / 60 s then a clean logged exit) self-heals the cold-start race while
    keeping the parent — and the client pipe — alive.
  - A **post-init crash** (the engine had already answered `initialize`) **exits cleanly** instead of
    relaunching: relaunching onto the held-open, already-handshaked pipe would strand an *uninitialized*
    engine (the client never re-handshakes and gets no EOF — a connection that looks alive but is dead,
    *worse* than a clean disconnect). Exiting closes the pipe so the client detects the drop and reconnects
    with a fresh handshake. (Fully transparent post-init restart would need Node to *proxy* the stream and
    replay the handshake — a larger change, deliberately deferred.)

  The restart **policy is the pure, exported `restartDecision` / `backoffFor`**, unit-tested by driving the
  real `createSupervisor` loop with a fake engine (startup heal+retry, backoff, crash-loop cap, clean
  shutdown, post-init clean-exit, and prompt exit on a SIGTERM mid-backoff).
- **Rotating server log at `<KG_DATA>/server.log`** (`server.py:configure_logging`, next to `provision.log`).
  Every uncaught exception (main + worker threads, via `sys`/`threading` excepthooks), every tool-handler
  error (full traceback), and every supervisor (re)launch / exit / backoff decision now land in a
  size-bounded, rotated file (2 MB × 3). This was the single biggest debuggability gap — the whole crash
  class was previously invisible.
- **`kg_status` — a cheap, projection-FREE status + coverage probe (18th MCP tool).** Reads the canon only
  (never triggers or refreshes the derived db), returning node/edge counts, edges by epistemic state, the
  still-`unverified` grounding-queue size, and which source files / `##` sections already have an anchored
  edge — so a partial build can be **confirmed and resumed** after any transport hiccup without grepping the
  filesystem. Granted to `/kg-build` for exactly that resume use.
- **A handler watchdog (`KG_HANDLER_TIMEOUT`, default 300 s; 0 disables).** FastMCP runs sync tools directly
  on the event loop, so a wedged handler (a deadlocked write, a runaway projection) blocks everything with
  no recovery. An observer thread dumps every thread's stack to the log and forces a clean process exit so
  the supervisor relaunches a fresh process — never a half-dead *Running…* state. Crash-safe canon I/O +
  idempotent receipts make the hard exit recoverable.

### Changed

- **`kg_write` is now idempotent with a deterministic receipt.** Every response carries a `receipt` (a hash
  of the payload's target ids — same payload → same receipt, across restarts), and an optional
  `idempotency_key` makes re-sending an identical write (after a lost transport response) a **true no-op
  that replays the SAME receipt + dispositions** (`idempotent_replay: true`) instead of a confusing
  all-deduped second pass. Validation is never weakened — only an exact repeat key short-circuits; without a
  key the write is still idempotent by canonical id. A rolled-back batch is never cached, so a transient
  failure can be retried for real.
- **The read path degrades instead of crashing on a projection failure.** A reprojection that raises (a
  sqlite hiccup, a native-dep blowup in community detection, a corrupt derived db) now logs, sets a
  `projection_degraded` flag, materialises an empty-schema derived layer, and serves canon-derived/empty
  data with the flag — rather than surfacing an exception. **Writes never come through the projection seam**
  (`kg_write` / `kg_propose` / `kg_ground` / `kg_rename` touch only the canon), so projection can never
  block or fail a write (now regression-tested).

### Fixed

- **A cancelled / broken-pipe request aborts ONLY that request; the server keeps serving.** The uniform tool
  envelope (`_tool_result`) turns a `BrokenPipeError` / `EOFError` / `ConnectionResetError` (or any
  `Exception`) into a structured `{ok:false, …}` result + a logged traceback instead of letting it bubble
  into the serve loop, and the next call is served normally. It deliberately catches `Exception`, **not**
  `BaseException`, so cooperative cancellation (`asyncio.CancelledError`) and shutdown
  (`KeyboardInterrupt`/`SystemExit`) still propagate. The serve loop logs and exits with a distinct non-zero
  code on an unexpected crash (so the supervisor relaunches) and exits 0 on a clean client disconnect.
- **Graceful native-dep degradation kept as hygiene.** Community detection already falls back from
  `leidenalg` to label propagation; a regression guard now imports `networkx`/`igraph`/`leidenalg` and runs
  a full `Projector.project(incremental=False)` over a fixture canon, documenting that the native deps are
  **not** the crash cause (the ruled-out hypothesis).

## [0.4.2] — 2026-06-26

### Added

- **`/kg-build` now extracts in BOUNDED PARALLEL WAVES instead of strictly section-by-section.** The
  orchestrator still launches **one `kg-extractor` subagent per `##` section** (the span-isolation property
  — a section's text is the only text that extractor can see, which is what makes `span-present` (§1.5)
  checkable rather than a paraphrase), but it now launches them concurrently in waves of `WAVE_SIZE` (issue
  `WAVE_SIZE` `Task` calls in one batch, await the wave, launch the next), so a 19-section document is four
  waves instead of 19 serial cold-started agents. The slow part — each extractor's token-by-token `kg_write`
  payload generation — overlaps across the wave; the brief `kg_write` calls funnel through the one
  single-threaded MCP server process and serialize there, so nothing is dropped or corrupted. No grounding
  guarantee changes: parallelism is *across* launches, never *within* one (collapsing sections into a single
  subagent would let a span be mis-attributed across sections of the same `source_file`, undetectable by the
  boundary — so that is explicitly forbidden).
- **New `extract_wave_size` plugin option (`userConfig`, `"type": "string"`, default `"6"`, range 1–10).** It
  sets how many section-extractors `/kg-build` runs at once. It is an ORCHESTRATION knob consumed by the
  command/skill, **not** the engine (it is the one `userConfig` key with no read in
  `build_engine_from_env`, by design), surfaced like `source_path` as
  `${CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE}`. `/kg-build` also accepts an inline override (`$2`) so a one-off
  run can change it without editing config — **precedence: inline arg > user_config > default**. Resolution is
  deterministic and unit-tested (`kg_engine.waves.resolve_wave_size`; the command's pure-Bash Step 0 mirrors
  it, with a drift-guard test): unset / non-numeric / `< 1` → `6`; `> 10` → clamp to `10`.

### Changed

- **`kg-extractor` now defaults to Sonnet (`model: sonnet` in `agents/extractor.md`).** Previously the agent
  had no `model:` field and inherited the session model (Opus), so a parallel wave meant a fleet of Opus
  agents each emitting a large JSON payload. Sonnet is the speed/quality sweet spot for this nuanced
  extraction; the hard guarantees (verbatim-span verification, pack-type validation, never-forge-a-verdict)
  are enforced by the `kg_write` boundary regardless of model, so a faster model cannot weaken integrity — it
  only affects extraction *judgment*, which the Stage-4 precision gate (`f4_probe`, ≥ 0.70) measures. (Haiku
  is deliberately *not* the default — more quarantines/noise on dense prose — and remains an opt-in only if
  the precision gate stays green.)
- **The canon single-writer lease now waits (bounded retry-with-backoff) under cross-process contention
  instead of failing fast.** `Canon._acquire_lock` previously raised `RuntimeError("canon vault is locked by
  another live session")` the instant `LeaseLock.acquire()` found the lease held by another live session. A
  parallel `/kg-build` wave funnels every write through one server process (serialized on FastMCP's event
  loop, so same-process re-acquire is idempotent and never contends), but the detached per-session reconcile
  worker / headless backend *are* separate processes; a writer that now finds the lease taken retries with
  exponential backoff up to `LOCK_ACQUIRE_TIMEOUT` (30 s, well over a full max-size wave of brief writes)
  before surfacing the error, so near-simultaneous writers SERIALIZE cleanly rather than one failing. The
  re-entrancy guard (`_lock_depth`) is unchanged, and the lazy projector's `try_acquire_lock()` stays
  strictly non-blocking so a read never stalls behind a write. A dead holder is still reclaimed via staleness,
  not waited on.
- **`source_path` is now a required plugin option (`"required": true` in `.claude-plugin/plugin.json`).** It
  has no default, and the install/config screen previously let it be left blank — which the engine resolves to
  empty source text, so every extracted edge fails span verification (`REJECTED:span-not-in-source`) and
  `/kg-build` silently produces an empty, unusable graph. Marking it `required` makes Claude Code's configure
  screen enforce a value up front instead of failing quietly at build time. Kept `type: "string"` (not
  `"file"`/`"directory"`) so the directory/glob multi-doc inputs (R4) still validate. `build_engine_from_env`'s
  `<project>/examples/source.md` fallback is unchanged — it still fires only when running from inside the repo
  checkout (where that file exists), since it resolves against `KG_PROJECT_DIR`/`CLAUDE_PROJECT_DIR`, not the
  plugin root.

### Documentation

- **Documented the install-time configure screen in the README.** A new *"The install config screen"*
  subsection under *Install & enable* explains that the `userConfig` options (`source_path`, `sensitivity`,
  `metrics_mode`, and — added later in this release — `extract_wave_size`) are free-text fields, not menus —
  Claude Code's `userConfig` schema has no `enum`/options support — what to type in each, that `source_path`
  must be set, and how to reconfigure after install (`pluginConfigs[…].options` in `settings.json` +
  `/reload-plugins`).
- **Clarified the `source_path` "default" in `CLAUDE.md`.** The Configuration section now notes `source_path`
  is `required: true` with no default, and that the `examples/source.md` fallback resolves against
  `KG_PROJECT_DIR`/`CLAUDE_PROJECT_DIR` (not the plugin root) — so it only fires inside the repo checkout, and an
  installed plugin with a blank path gets empty source text.
- **Documented `extract_wave_size` in the README config section.** The `userConfig` table and the
  install-config-screen walkthrough now list the fourth option alongside `source_path`/`sensitivity`/
  `metrics_mode`, with its 1–10 range, default `6`, and the `/kg-build [source_path] [wave_size]` inline
  override.

## [0.4.1] — 2026-06-25

### Fixed

- **Windows Smart App Control no longer breaks engine provisioning by blocking leidenalg's native DLL.**
  On a fresh install where Windows Smart App Control / Application Control is enforced
  (`VerifiedAndReputablePolicyState = 1`), `scripts/bootstrap.py` installed all dependencies fine (cp314
  wheels included) but then failed the import-verification step: leidenalg's unsigned native `_c_leiden`
  DLL is blocked from **loading** (reputation-based — igraph's DLL loads, leidenalg's does not) with
  `ImportError: DLL load failed while importing _c_leiden: "An Application Control policy has blocked this
  file"`. Because `verify_imports` treated leidenalg as a **mandatory** import, that aborted `do_install`,
  which then `rmtree`'d the half-built venv — so provisioning never completed and the `creativity-graph`
  MCP server never came up ("1 error during load", no `kg_*` tools). This contradicted the **runtime**,
  where leidenalg has long been **optional**: `projector._leiden` wraps the import in `try/except` and
  degrades to label-propagation community detection when it can't load. The bootstrap now matches that
  contract: `leidenalg` is **removed from the mandatory `_VERIFY_IMPORTS` set** (which keeps `mcp`,
  `pydantic`, `networkx`, `igraph`, `yaml`, `kg_engine` as hard requirements), and a **separate soft probe
  (`probe_leidenalg`)** reports `leidenalg OK (Leiden community detection enabled)` or `leidenalg
  unavailable (<ExceptionType>: <msg>); using label-propagation fallback (projector._leiden)` and **never
  causes a non-zero exit** (it runs a non-checking subprocess and swallows the in-venv DLL-load error and
  any parent-side launch failure). `leidenalg` **stays a hard `[project.dependencies]` entry** — it
  installs cleanly; only its DLL load is blocked under SAC — so the engine still uses Leiden wherever SAC
  permits and degrades gracefully where it does not. **No Windows security setting is touched and disabling
  SAC is never suggested** — the engine runs degraded instead. Coverage added in `tests/test_bootstrap.py`
  (mandatory set excludes leidenalg, the probe swallows a launch failure and a real-interpreter probe, and
  `do_install` completes — writing `engine-python.txt` + `install.stamp` — even when the probe reports
  leidenalg unavailable).

### Documentation

- **Troubleshooting note for `Plugin "creativity-graph" not found in marketplace "sergiparpal"` after a
  successful `marketplace add`.** Documented in the README *Install & enable* section that this is a
  stale marketplace cache (`marketplace add` reports success even when it reuses a cached clone that
  predates the plugin entry), with the ordered fix — `marketplace update` → remove + re-add → delete
  `~/.claude/plugins/marketplaces/<name>/` and restart — and a `curl` reachability check to rule out a
  silent clone-failure fallback to cache.

## [0.4.0] — 2026-06-25

### Added

- **Semantic git merge driver for the canon (the safe half of multi-machine support).** Two machines or
  branches editing the *same* node previously handed `git` a line-based 3-way merge that mangled the
  `edges:` block and could silently keep one side's grounding verdict. `scripts/kg_engine/canonmerge.py`
  is now a git merge driver — the out-of-process mirror of `Canon._merge_into_existing` — that unions
  edges by their deterministic `edge_id` and, for an edge present on both sides at a **different**
  `epistemic_state`, resolves the merged edge to **`unverified`** (clearing `verdict_by`/`verdict_at`),
  never to either side's verdict. It is **structurally incapable of forging a verdict** (the only
  `epistemic_state` it can write on a conflict is `unverified`), so it needs no audit log; a verdict that
  survives a clean merge with no local audit record is re-quarantined by the reconciler's full sweep
  (§1.8). Shipped as `scripts/canon_merge_driver.mjs` (cross-platform Node launcher → resolved engine
  python, never bash) routed by a new `.gitattributes` (`canon/*.md merge=kgcanon`). It is **not**
  auto-installed — a clone opts in once with `git config merge.kgcanon.driver …` (pure git plumbing, no
  per-session git-config writes); after a merge, conflicting verdicts demote to `unverified` and the
  edges are re-grounded. Cross-machine verdict *preservation* (a syncable, replay-safe audit log) is a
  deliberately deferred spike. Coverage in `tests/test_canonmerge.py` (edge union, equal-verdict
  preservation, verdict-conflict demotion forge-proof over every cross-state pair, body 3-way merge,
  malformed fail-open, and a git end-to-end merge).
- **Multi-document / source-aware ingestion (`.md`/`.txt`).** `KG_SOURCE_PATH` (the `source_path`
  userConfig) now resolves a **single file, a directory, or a glob** of `.md`/`.txt` into an ordered
  `{basename → text}` map (the new `SourceSet`, `scripts/kg_engine/sources.py`), and span verification is
  now **source-aware**: a span must verify against a **declared** source, and — when an edge carries a
  `source_file` — against **that file specifically** (a lenient any-source fallback when the named basename
  is unknown, e.g. a legacy `source.md` or a typo). This turns the previously-dead `Edge.source_file` field
  **load-bearing**, *strengthening* span-present (§1.5): the boundary now splits a mis-attributed span
  (present in the corpus, absent in the named file) into a new `span-not-in-named-source` reject, distinct
  from `span-not-in-source` (absent everywhere). The headless backend extracts per file (stamping each
  edge's `source_file`), the projector's IDF corpus spans every file, and `pack validate … <dir|glob>`
  works. **Single-file builds are byte-identical** to before (a one-entry `SourceSet`), and direct
  `validate_payload` callers without a `SourceSet` keep the exact single-blob behavior. **Markdown/text
  only — no PDF/media** (a lossy transcript as a "verbatim" span would break span-present). Coverage in
  `tests/test_sources.py` plus extensions to `test_invariants.py` / `test_grounding.py`.
- **Source-staleness advisory for verdicts (span-divergence only).** A grounded/`failed` span-present
  edge's stored `span` is a snapshot taken at verdict time; if the source is later edited so that span no
  longer appears, nothing previously re-flagged the verdict. The projector now recomputes a purely
  **read-only** advisory off the hot path — re-verifying each grounded/`failed` span-present edge against
  its **own** `source_file` via the R4 `SourceSet` (per-file, never a global concat, so a multi-file vault
  never false-flags an edge whose span lives in a non-default file) — and surfaces the divergent ids as
  `kg_context.advisory.stale_verdicts = [{edge_id, reason: "span-no-longer-in-source"}]`. It **never**
  mutates a verdict (re-grounding stays a `kg_ground` decision — never-forge-a-verdict, measure-never-gate);
  the binary advisory carries no decaying "trust erodes with time" scalar. The recompute is gated behind a
  persisted source-content hash (a pure source edit is **one-projection-lagged** — `is_stale`, which fronts
  every read, is deliberately left source-blind), and a re-grounding clears the flag on the next projection.
  `/kg-ground` gains a **Stage 0b — Drain stale verdicts** step. Coverage extends `tests/test_projector.py`
  (single- and multi-file divergence, unverified/inferred never flagged, failed-with-missing-span, no-mutate,
  persisted-and-reused, no-source) and `tests/test_grounding.py` (re-grounding clears).
- **`kg_agenda` — read-only structural "suggested questions" (16th MCP tool).** A new read-only tool that
  reads **only** precomputed derived columns (node ranks + edge provenance/state — never the canon, never a
  span) and returns ~5 structural gaps, each a templated question, split into **`answerable_now[]`** (well-
  grounded neighbourhoods) vs **`blocked_on_grounding[]`** (orphans, hypothesized-only neighbourhoods,
  under-grounded hubs, disconnected clusters) — mirroring `kg_context`'s `items[]`/`hypotheses[]`. The split
  is the honesty move: a hypothesized-only neighbourhood surfaces as **blocked**, never as answerable. It is
  ranked by the **existing** honest gate-aware signal (`spec_betweenness` only when `gate_on=1`, else the
  structural-bridge/degree advisory; never raw betweenness as lead) — no new "interestingness" scalar is
  minted. It **asserts no edges, copies no spans, stamps no verdicts** (measure-never-gate — it suggests,
  never acts; the question text is session-time only and never written to the canon), reading through a new
  shared read-only `Projector._agenda_reader()` seam (opened `PRAGMA query_only`). Surfaced as an MCP tool and
  as optional steps in `/kg-query` (orientation) and `/kg-ground` (the `blocked_on_grounding[]` lane is an
  alternative prioritization read). MCP tool surface grows **15 → 16**. Coverage in `tests/test_agenda.py`
  (every detector, the two-lane split, gate-aware ranking, specificity down-weight, read-only invariance,
  limit clamp, empty graph) plus `tests/test_manifests.py::test_kg_agenda_registered`.
- **`kg_export` — human-facing HTML viz + `GRAPH_REPORT.md` (17th MCP tool, zero-dependency).** A new
  read-only exporter (`scripts/kg_engine/export.py`) that projects-if-stale, then consumes **only** the
  derived layer (through R6's shared `Projector._agenda_reader()` seam) plus `kg_metrics`, and writes two
  **fresh, disposable** artifacts under `${KG_DATA}/derived/`: a self-contained, fully-offline `graph.html`
  (vanilla-JS canvas force layout, data inlined, no network, no `<script src>`, no new package — the template
  is a Python module constant in `kg_engine/templates/`) and `GRAPH_REPORT.md`. **The differentiator:** the
  three orthogonal axes are encoded on **independent visual channels**, never one "confidence" colour —
  `epistemic_state`→edge line (solid grounded · dashed unverified · **red failed/rejected** · dotted
  hypothesized), `authored_by`→node border, `provenance`→node fill opacity. **Node size = degree** (the honest
  advisory); the bridge highlight is **gate-aware** (`spec_betweenness` only when `gate_on=1`, else the
  structural-bridge advisory — size is never the bridge metric, so the generality confound is never smuggled
  into the most prominent channel). **Failed/rejected edges are drawn, never filtered** — falsification memory
  made eyeball-able (§1.7). The report's headline counts come straight from `kg_metrics` (cannot drift) and it
  surfaces per-community axis breakdowns, the falsification list, R3's stale verdicts, and R4's per-source-file
  edge counts. **Read-only / measure-never-gate**: it never reads prose, never writes through
  `kg_write`/`kg_ground`, and never `_atomic_write`s `graph.json`/`index.sqlite` (`projector.py` stays their
  sole writer) — it cannot forge a verdict or bypass span-present. Shipped three ways: `python -m
  kg_engine.export html|report|all`, the `kg_export` MCP tool, and the `/kg-view` command. MCP tool surface
  grows **16 → 17**. Coverage in `tests/test_export.py`.

### Changed

- **Uniform MCP transport-error envelope across all 17 tools.** The tools previously exposed four
  inconsistent error shapes to a client (mutations `{ok:false, error}`; `get_node` `{error:…}` with no
  `ok` key; reads `None`; and `query_graph`/`kg_context`/`get_neighbors` letting a mid-read SQLite/NetworkX
  error raise straight through as an MCP-level exception). A `functools.wraps` `_tool_result` decorator
  stacked under every `@mcp.tool()` now turns a **raised** exception into a uniform
  `{ok:false, error, error_kind}` envelope (plus a logged warning), while success returns — including the
  deliberate `{ok:false}` domain dispositions and the reads' own `{path:…}`/`{error:"not found"}`/list/`None`
  shapes — pass through **unchanged**, so transport ok/error and domain disposition stay two orthogonal axes
  and the never-stall contract holds. `wraps` keeps each signature, so FastMCP still builds the correct tool
  schema (verified through a real FastMCP, not just the test harness). Adds the engine's first logging seam
  (`logging.getLogger("kg_engine")`): the two silent `except Exception: pass` index fallbacks (edge-owner
  lookup, `kg_metrics`) now `logger.debug` before falling back.
- **Internal — new leaf modules, decoupling, and hot-path perf (no observable behavior change).** Three
  dependency-free leaves were lifted out of larger modules: `groundaudit.py` (`GroundAuditLog` — the §1.8
  grounding-audit durability protocol `append`/`truncate`/`audited_write`, formerly inline in the `KGEngine`
  facade), `graphio.py` (the NetworkX node-link adapters `_node_link_data`/`node_link_graph` + a public
  `node_attr`, breaking the `projector`↔`harness` import cycle so all three import downward), and
  `atomicio.py` (`atomic_write_bytes`/`atomic_write_text`, single-sourcing the temp+fsync+replace core
  previously duplicated across `canon`/`projector`/`bootstrap`); plus a shared `scripts/_engine_resolve.mjs`
  for venv/interpreter/`PYTHONPATH` resolution and the retirement of three cross-module coupling smells. On
  the hot path, betweenness is now recomputed only on a full rebuild or a genuine live-topology change (a
  sha256 over the failure-filtered structure), eliminating the O(V·E) recompute on every `/kg-ground` drain;
  the canon parse that `is_stale()` performs is shared into `project()` instead of parsed twice; and `CSafeLoader`
  fronts the parse path. All §1.x invariants preserved (114 + 3 maintainability/perf findings resolved).

### Fixed — second exhaustive review (48 findings)

A full-codebase review of the R1–R6 additions (which landed after the v0.3.0 review) plus a regression
sweep. All findings adversarially verified; the high-severity ones reproduced by hand. 20 new regression
tests; the full suite is green (392).

- **Verdict durability on idempotent re-build (critical, §1.8).** A normal `/kg-build` re-run that
  re-emitted an already-`grounded` (or `obsolete`) edge was `ACCEPTED`/`deduped`, and the canon's
  "incoming wins" merge then overwrote the verdict with a fresh `unverified` edge — silently destroying a
  `kg_ground` verdict the reconciler could not restore. The boundary's re-emit protection was scoped to
  `FAILURE_STATES` only; it now covers the full `GROUNDABLE_STATES` on the extraction lane
  (`collapses-into-known-verdict`), the generation lane is deliberately left free to re-propose grounded
  structure, and `Canon._merge_into_existing` carries a verdict forward rather than downgrading it to
  `unverified` (defense-in-depth; the reconciler's legitimate demote goes through `write_one`, unaffected).
- **`kg_write` batch loss on a label-form edge source (critical).** `merge_results_into_nodes` attached
  edges keyed on the raw `edge.source` while nodes were keyed by slug, fabricating a phantom node that
  slug-collided onto one file and rolled back the **entire** batch (silent total data loss). Attachment now
  keys on the slug, matching `edge_id`/node files/dedup; an auto-created placeholder gets `id == slug`.
- **HTML inlining hardened (high).** `export.py` escaped only `</`, which the `<!--<script>`
  script-data-double-escape state defeats; every `<`/`>`/`&` is now `\uXXXX`-escaped. The Markdown report
  neutralizes backticks/angle brackets too.
- **Post-upgrade read crash (high).** `is_stale()` now consults `_schema_outdated()`, so a derived
  `index.sqlite` built before the Stage-2 node columns reprojects instead of crashing every read with
  `no such column: betweenness` (permanently, on a read-only vault).
- **Non-UTF-8 source no longer disables the tool surface (high).** `SourceSet` caught only `OSError`, but
  `UnicodeDecodeError` is a `ValueError`; a binary/UTF-16 file among an R4 directory now skips instead of
  propagating out of the constructor (uncached) and crashing `kg_write`/`kg_ground`/projection/export.
- **Mediums:** reconcile sweep no longer aborts vault-wide (and deletes a note) on a slug collision with a
  distinct canonical note; the specificity gate is decided over the same live (failure-excluded) subgraph
  it ranks; `kg_ground` no longer triggers a full betweenness reproject per call (drain is back to ~O(1)
  per edge); the backend clamps `max_tokens` to the SDK's non-streaming time floor (~21 333) so a moderate
  override no longer fails every section pre-flight; `collapse` on a community-less/dangling target no
  longer sweeps all danglers into one bogus compression; `regroup` short-circuits a degenerate
  all-singleton repartition instead of an O(n²) explosion; the canon merge driver's system-python fallback
  now requires PyYAML.
- **Lows/nits:** pack `specificity_seeds` are now actually consumed (merged over the corpus IDF);
  `span_verifies` no longer fails open on a zero-width-only span; the content-staleness hash ignores
  timestamps; `kg_context` caps the R3 stale-verdict list; `absorption()` tolerates a malformed
  `generations.json`; egress `extra_terms` are honored at every tier; `**` globs recurse; the bootstrap
  stamp tracks the **venv** interpreter (no spurious cross-interpreter rebuilds) and a past-deadline wait
  no longer reports "ready"; `precontext.py` resolves `KG_PROJECT_DIR`/`KG_DATA` like the server and strips
  `${…}` sentinels; `validate_plugin.py` accepts single-quoted versions and fails on a missing version
  line; the canon merge driver writes atomically and fails open; `source_path` userConfig is `string`
  (so a directory/glob is selectable); igraph/leidenalg gain upper bounds; plus assorted comment/docstring
  corrections.

### Fixed — third exhaustive review (49 findings)

A later full-codebase review (53 candidate issues; 49 confirmed after adversarial per-finding
verification) of the post-R1–R6 engine. All fixed here with regression tests; the suite grew 414 → 496.

- **Verdict-integrity / §1.8 audit-log forge-proofing.** `_forged` drains an idempotent re-ground's
  surplus audit record so it can never later justify an out-of-band forgery; the reconcile sweep re-folds
  the audit log fresh and reads-under-lease before re-quarantine, so a verdict applied mid-sweep is not
  reverted or clobbered; `GroundAuditLog.truncate()` reports success and `audited_write` raises
  `OrphanAuditError` on an un-truncatable orphan (routed through the MCP error envelope) instead of silently
  reporting a clean rollback (`append`/`truncate` fsync the parent dir; the batch loop is
  compensation-guarded); `canon._merge_into_existing` carries a preserved verdict's span/provenance/notes
  (not just its state), closing a `kg_propose` re-proposal leak.
- **The degraded derived layer the PreToolUse hook silently served.** `hooks/precontext.py` hand-built a
  `Projector` with no `source_set`/`specificity_seeds`/`metrics_mode`, so a hook-triggered projection
  computed the IDF/specificity gate, `spec_betweenness`, and the R3 stale-verdict scan against an **empty**
  corpus and wrote a `cheap_sig` identical to the server's — and because `is_stale()` is source-blind, the
  server then served that degraded derived layer as fresh until the next canon write. Both construction
  sites now collapse into one `_wire_projector` / `KGEngine.read_only_projector` seam, so the hook reads the
  same corpus and specificity seeds as a full engine.
- **Cross-platform / locks.** `git merge-file` stdout decoded `encoding="utf-8"` (Windows mojibake); the
  bootstrap lock gains a pid-liveness probe, post-steal heartbeat re-validation, and ownership-verified
  release (mirroring `LeaseLock`).
- **Egress scrub.** Cumulative reserved-literal tracking + identity entries prevent over-expanding a literal
  placeholder on restore; added base64/AWS-secret + natural-language-keyword rules; EMAIL claims a long
  machine local-part ahead of the high-entropy fallback.
- **Mediums/lows:** an in-payload duplicate of a flood-rejected net-new edge can no longer bypass the
  rate-limit cap; `project()`'s contention branch heals an outdated schema (reads no longer crash on `no
  such column`) and `ORDER BY`/`LIMIT` get a deterministic id tiebreak; `kg_ground` applies the
  hypothesis-promotion gate to nodes and validates `kind`; `model` folds U+0130 before casefold for span
  matching and raises the documented `ValueError` on non-dict frontmatter; `canon` reaps transient dotfiles
  on the full sweep and restores a live lock on replace failure; `canonmerge` resolves equal-state edges
  incoming-wins (aligned with `canon.py`); `export` escapes `source_file` and stops the layout loop when it
  cools; `harness`/`pack`/`backend`/`f4_probe`/`validate_plugin`/CI/launcher hardening.

## [0.3.3] — 2026-06-23

### Fixed
- **Out-of-box usability: an unconfigured `source_path` no longer silently breaks span verification.**
  `source_path` has no default in `plugin.json`, so when it is left unset Claude Code passes the **literal**
  string `${user_config.source_path}` through `.mcp.json` as `KG_SOURCE_PATH`. `build_engine_from_env` took
  that literal as a real path: `source_text()` then read a non-existent file and returned `""`, so **every**
  agent edge failed the span-present check (`REJECTED:span-not-in-source`) — the graph built but was unusable,
  and silently. The resolver now treats an empty **or** unsubstituted `${...}` env value as unset (mirroring
  `bootstrap._clean` / `launch_server.clean`, which already strip the same values), applied to
  `KG_SOURCE_PATH` / `KG_PROJECT_DIR` / `KG_DATA` / `KG_PACK_PATH` — so the documented `examples/source.md`
  fallback can fire and an unconfigured source surfaces as a clean "no source" state. Found by an
  adversarial cross-platform install audit; regression tests added (`tests/test_fix_server.py`).

## [0.3.2] — 2026-06-23

### Fixed
- **Cross-platform: reconciler correction of a non-canonically named note now works on case-insensitive
  filesystems (macOS/Windows).** When a hand-created note used a non-canonical filename (`Foo.md` for id
  `Foo`, slug `foo.md`), the reconciler wrote the un-forgery correction to the canonical path and *then*
  unlinked the "stale original" — but on a case-insensitive filesystem `Foo.md` and `foo.md` are the
  **same** file, so the unlink removed the file `write_one` had just rewritten (next `stat` →
  `FileNotFoundError`), and a case-preserving replace kept the stale `Foo.md` name regardless (CI red on the
  macOS/Windows matrix; Linux, being case-sensitive, unaffected). The reconciler now detects a non-canonical
  note by comparing the directory-entry name to the canonical **slug** name (`f"{slug(id)}.md"`, with no
  filesystem resolution — `Path.resolve()` returns the existing on-disk casing on Windows, which had masked
  the difference) and **unlinks the original before** the canonical write, so `write_one` creates a fresh,
  correctly-cased `foo.md`. Green across the full Linux/macOS/Windows CI matrix; regression test added
  (`tests/test_fix_reconciler.py`) asserting the unlink-before-write ordering.

### Documentation
- Synced the living docs with the v0.3.x engine. Documented all **fifteen** MCP tools in
  `references/tools.md` (the four generative-layer tools `kg_propose` / `kg_generate` / `kg_operate` /
  `kg_absorption`, the `kg_ground` `support_span` / `support_note` promotion params, and the `kg_context`
  `hypotheses[]` lane + `advisory.bridge_metric`); corrected the **four**-condition ideation experiment
  (`control | graph | graph+generate | rag`) across the README, the `/kg-experiment` command, and the
  `kg-evaluator` agent; and fixed stale claims (content-hash reprojection staleness, in-memory byte-snapshot
  rollback, the deterministic-claim span demotion, `generate.py`/`operations.py`/`backend.py` in the module
  lists, the test count) across `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`, `SKILL.md`, and the skill
  references.

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
