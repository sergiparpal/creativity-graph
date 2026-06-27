# PROGRESS — creativity-graph build log

One line per stage with its exit-test result and any recorded metric.
Auditable end-to-end without re-reading code. Metric stages log the outcome and proceed; no human gate.

Corpus for all measured numbers: `examples/source.md` (the self-contained demo theory). Demo vault built
under `/tmp/kg-demo/` by driving the real engine (boundary → canon → projector → grounding → harness).

| Stage | Exit test | Result |
|---|---|---|
| 0 — Scaffold + env bootstrap | `claude plugin validate --strict`; MCP `kg_ping` | PASS — manifest, `.mcp.json`, cross-platform SessionStart provisioning (`provision.mjs` → `bootstrap.py`, background, `uv`-or-`venv+pip`), `kg_ping` stub all present |
| 1 — Canon + transactional writes + lease lock + reconciler | `pytest tests/test_chaos.py -q` | PASS — crash-mid-write recovers via git; stale lock reclaimed; out-of-band forged verdict re-quarantined |
| 2 — Domain pack + glossary | `python -m kg_engine.pack validate pack/pack.yaml examples/source.md` | PASS — `PackContract` valid: 6 node_types, 10 edge_types, 12 glossary terms; source_coverage 1.0, glossary_grounded_in_source 1.0 |
| 3 — Staged extractor + boundary + PII scrubber | `pytest tests/test_invariants.py tests/test_scrub_egress.py -q` | PASS — fabricated/undeclared/span-less edges rejected/demoted; truncated JSON rejected with no partial write; seeded secret never leaves via `kg_scrub`; placeholder spans restored to original in the canon (§1.9 wired live) |
| 4 — Extraction evaluation | `python scripts/f4_probe.py score labels.csv` | **precision 1.00** (gate ≥ 0.70 ✓); astrology rate 0.00; **span-support 1.00**; 12 edges labeled, per-relation `grounds` 3/3. A span-present boundary makes fabrication unreachable, so in-graph precision is high by construction. |
| 5 — Projector + derived layer + query surface | `pytest tests/test_projector.py -q` | PASS — `graph.json` round-trips through NetworkX (20 nodes / 12 edges); 8 Leiden communities; incremental reproject touches only changed edges; `kg_context` within budget (1095/2000 tok), reads precomputed ranks O(1), never computes centrality in-request |
| 6 — Grounding loop + adversarial grounder + memory of failures | `pytest tests/test_grounding.py -q` + live demo | PASS — **12/12 edges grounded** via `kg_ground`; verdicts **survive a full reproject** (12 reattached, 0 orphaned); 0 failed/rejected on this curated corpus; the `failed`-state survival + falsification-counter path is pinned by `test_grounding.py` |
| 7 — Annotation agreement + specificity harness | `python -m kg_engine.harness agreement && … specificity` | LOGGED — **Krippendorff α = 0.000** on the 12-edge sample (raw agreement 11/12; α is degenerate under extreme label prevalence — the α-paradox — so the grounding signal **stays advisory**); **specificity gate OFF** (generality_confound_detected=false, rank_churn 0.0 → specificity-weighting does not clearly separate on this small graph; degree remains the honest advisory) |
| 8 — Ideation comparison | `python -m kg_engine.harness ideation` | LOGGED — see table below; verdict: *graph condition did not clearly beat control* on the harness's diversity/novelty test, **but** the graph arm cut the unsupported-claim rate ~4× (0.083 vs 0.333) and raised utility (0.367 vs 0.167). The novelty metric (1 − source-overlap) rewards untethered text, so control "wins" novelty by inventing; the graph arm trades raw novelty for grounding. Logged; execution proceeds. |
| 9 — Hardening + packaging | `pytest tests/ -q`; `claude plugin validate --strict` | PASS — full suite **green (67 tests)**; **`claude plugin validate --strict` ✔ passes**; version `0.1.0`; component layer (5 agents, 5 commands, skill + 3 references) authored, adversarially verified against the engine source, and cross-checked (every example span verifies; every edge-id canonical; every `subagent_type` resolves). **Installed locally and the full `/kg-build` → `/kg-ground` → `/kg-query` workflow run end-to-end through the installed plugin** (see *Live validation* below). Previously-deferred items now landed (see *Stage 9 deferred items* below): headless `--backend` extraction path (`kg_engine.backend`, §2.2), edges-per-KB injection rate-limit, hardened canon path resolver (logical chroot), and CI (`.github/workflows/ci.yml` + `scripts/validate_plugin.py`). Still deliberately manual: public marketplace publish/`claude plugin tag` (an outward-facing human action — see `CLAUDE.md`). |
| Review — findings hardening | `pytest tests/ -q`; multi-agent review (each finding adversarially verified) | PASS — surfaced **2 invariant bypasses** (span-present via a forged `authored_by=deterministic` claim; the mtime-spoof re-hash sweep was never actually run) plus a range of correctness/robustness defects. All fixed; suite **green (79 tests; +12 regression in `tests/test_review_fixes.py`)**. Highlights: content-aware `is_stale` (a `kg_ground` verdict is visible to reads with no intervening commit), `MultiDiGraph` projection (parallel typed edges survive `graph.json`), reconciler audit-record **consumption** (a replayed verdict is caught), `kg_rename` data-safety, scrubber placeholder namespace + secret-regex coverage, and the lease lock now actually acquired. See `CHANGELOG.md` → *Review-findings hardening pass*. |

## Stage 8 ideation table (real harness output, demo corpus)

| condition | n | diversity | novelty | utility | unsupported_rate |
|---|---|---|---|---|---|
| control | 6 | 1.000 | 0.992 | 0.167 | 0.333 |
| graph   | 6 | 0.913 | 0.613 | 0.367 | **0.083** |
| rag     | 6 | 0.963 | 0.491 | 0.233 | 0.000 |

Verdict: `graph condition did NOT clearly beat control`. Honest reading: the graph condition is the most
*grounded* (4× fewer unsupported claims than control) and most *useful* (causal/connective density), but the
diversity/novelty gates favor the source-untethered control. Idea-generation value remains a hypothesis under
test (§1.1) — measured, labelled, not shipped as a guarantee.

## Definition-of-done numbers (this corpus)

- extractor precision: **1.00**  ·  span-support rate: **1.00**  ·  astrology (fabricated+vague): **0.00**
- annotation Krippendorff α: **0.000** (degenerate under 11/12 prevalence; signal stays advisory)
- specificity-metric verdict: **gate OFF** (degree is the honest advisory; specificity-weighted betweenness stays gated)
- ideation comparison: table above — graph arm most grounded/useful, not most "novel" by the diversity metric
- test suite: **267 passing** (`pytest tests/ -q`)

## Live validation (installed plugin, fresh vault)

The plugin was installed locally (user scope, via a single-plugin `marketplace.json`) and the full
workflow run end-to-end **through the installed plugin** — `/creativity-graph:kg-build` → `/kg-ground` →
`/kg-query` on a fresh vault (`/tmp/kg-vault`). Driving it for real surfaced and fixed four packaging/quality
bugs static review could not (committed):

1. **MCP tool namespace** — plugin tools are `mcp__plugin_<plugin>_<server>__<tool>`, not `mcp__<server>__`;
   the corrected grants let the `kg-extractor` subagent reach the boundary.
2. **userConfig env wiring** — `${CLAUDE_PLUGIN_OPTION_*}` doesn't expand in `.mcp.json` and was clobbering
   the auto-injected values; `KG_SOURCE_PATH` now uses `${user_config.source_path}` so the server reads the source.
3. **Cold-start spawn race** — the server now launches via `node scripts/launch_server.mjs` (Node always
   exists in the Claude Code runtime → spawn never fails → no spurious "needs-auth" caching; the launcher
   self-heals the venv in the foreground via `bootstrap.py`).
4. **`kg_context` query matching** — tokenize + OR-match terms so a natural-language `/kg-query` hits
   (was a 0-item whole-string `LIKE` miss).

Live run results:

- **`/kg-build`** — 31 ACCEPTED / 0 rejected; 18 nodes, 12 span-present edges; egress `kg_scrub` ran (0 redactions).
- **`/kg-ground`** — `unverified 12 → 0`: **11 grounded, 1 rejected** (generality confound caught), **2 failed**
  (adversarial counter-edges). `falsification_counters = 3`, never pruned; verdicts survive reprojection.
- **`/kg-query`** — answered with provenance + falsification counters on every edge; refused to present the
  rejected `bridges` claim as fact; labelled the structural-bridge advisory as a heuristic.

## Stage 9 deferred items (now landed)

The three items previously parked plus the explicit path-resolver hardening from the Stage 9 task list,
implemented, then hardened against a 6-dimension adversarial review (+14 tests, 53 → 67):

1. **Headless `--backend` extraction path** (`scripts/kg_engine/backend.py`, §2.2). API-key-driven
   extraction for CI / unattended runs, mirroring `agents/extractor.md`: split the source into `##`
   sections, `kg_scrub` each (egress, §1.9), call Claude (`claude-opus-4-8`, adaptive thinking,
   `output_config.format` keyed to the pack vocabulary — no removed sampling params) to get
   nodes+edges+spans, stamp the deterministic axes, and write through the **same `kg_write` boundary**.
   The API call is isolated and the client injectable, so the full pipeline is unit-tested with a fake
   client and no network (`tests/test_backend.py`). Ships as the optional `backend` extra (`anthropic`).
2. **Edges-per-KB injection rate-limit** (`boundary.py`). Net-new writable edges are capped at
   `max(64, kb·20)` across the canon; the overflow is REJECTED `rate-limited-flood` (`retryable=false`).
   Deduped edges (re-sent / already canonical) cost no budget, so idempotent `/kg-build` re-runs never
   trip it (a bug the adversarial review caught and is now regression-tested). Tunable via
   `KG_MAX_EDGES_PER_KB`; the 2.7 KB demo (~6.5 edges/KB) is far under the bar. `tests/test_hardening.py`.
3. **Hardened canon path resolver / logical chroot** (`canon.py`). `node_path` now rejects null bytes
   and asserts the resolved path stays under the canon dir (explicit vault-prefix check on top of the
   `slug()` guarantee). Covered by `tests/test_hardening.py`.
4. **CI** (`.github/workflows/ci.yml`). On every push/PR: `pytest tests/`, `pack validate`, the Stage
   7/8 harness commands, and a deterministic manifest/component check (`scripts/validate_plugin.py`,
   the hard gate). A best-effort job runs the real `claude plugin validate --strict` when the CLI is
   installable.

Still deliberately **not** automated: public marketplace publish + `claude plugin tag` — an
outward-facing, hard-to-reverse human action documented in `CLAUDE.md`.

---

## The generative layer (v0.3.0) — generate offensively, judge defensively

The second half of the system landed: deterministic discovery mechanisms propose `hypothesized`
candidates into a separate write lane (`kg_propose`), and the existing grounding loop is the post-hoc
filter (promotion via `kg_ground` requires support and upgrades provenance). Six generators
(`kg_generate`: bridge §2/§4, seed §3, compression §7, regroup §8, transplant §5, ensemble §9), the
four §8 endo operations (`kg_operate`), the §14 absorption window (`kg_absorption`), the completed
specificity-weighted-betweenness bridge metric (precomputed + gated), and the `/kg-generate` +
`/kg-perturb` commands with the `kg-generator` language agent. `/kg-experiment` gained a
`graph+generate` condition. Full suite green; MCP surface = 15 tools.

### Out of scope (deliberately not built)

- **§12 temporal triad as an automated scorer.** Scoring a candidate on whether the *problem it
  addresses is still alive* needs an exogenous "is this problem fresh?" signal the repo does not have
  (citation recency, issue activity, a domain freshness feed). A future
  `kg_generate(mechanism="temporal")` would consume such an external freshness feed — e.g. a
  `derived/freshness.json` mapping node ids to a recency/decay score sourced outside the graph — and
  rank candidates by the *triad* (alive problem × novel connection × specific claim) rather than by
  structure alone. It is intentionally **not** implemented: without the exogenous signal it would
  invent the very freshness it is meant to measure. Left as a documented extension point.
- **Embedding-based candidate generation** (`metrics_mode=with_embeddings`). The structure-only path is
  the default; the embedding extra was removed in 0.2.1 and is not a dependency here.

---

## Faster builds (v0.4.2) — Sonnet extractor + bounded parallel waves

`/kg-build` was the slowest command: it launched one `kg-extractor` per `##` section **sequentially**, and
the extractor **inherited the session model (Opus)**, so a 19-section document meant ~19 cold-started Opus
agents each emitting a large `kg_write` payload token-by-token, one after another. The bottleneck is
output-token generation × cold-start × serial. Two changes cut wall-clock without weakening any guarantee:

- **The extractor now defaults to `model: sonnet`.** The hard guarantees (verbatim-span verification,
  pack-type validation, never-forge-a-verdict) live in the `kg_write` boundary, not the model — so a faster
  model cannot break integrity; it only risks extraction *judgment*, which the Stage-4 precision gate
  (`f4_probe`, ≥ 0.70) measures. Sonnet is the speed/quality sweet spot; Haiku is deliberately not the
  default (more quarantines on dense prose). If Sonnet ever drops below the gate, revert the extractor to
  Opus and keep the parallel speedup alone.
- **The orchestrator launches one-subagent-per-section in BOUNDED PARALLEL WAVES** of `extract_wave_size`
  (new `userConfig`, default 6, range 1–10; inline override `/kg-build <source> <wave_size>` beats config
  beats default — resolved deterministically by `kg_engine.waves.resolve_wave_size`, mirrored by the
  command's pure-Bash Step 0). **One section per subagent is preserved** — collapsing sections into one
  launch would let a span be mis-attributed across sections of the same `source_file`, undetectable by the
  boundary — so span-isolation is intact; only *how many* single-section extractors run at once changed.

**Why parallel writes are safe.** FastMCP runs sync tools directly on the MCP server's single event-loop
thread, so a wave's `kg_write` calls funnel through one process and already serialize there; the canon's
single-writer lease is only ever contended *across* processes (the detached reconcile worker / headless
backend). `Canon._acquire_lock` now does a bounded retry-with-backoff (≤ `LOCK_ACQUIRE_TIMEOUT`, 30 s — well
over a full max-size wave of brief writes) so a contended writer **serializes cleanly instead of failing
fast**, while the lazy projector's read path stays strictly non-blocking. New tests cover wave-size
resolution (default/fallback/clamp/precedence + a Bash-mirror drift guard) and a full 10-writer concurrent
wave (all commit, none corrupts, none dropped). Suite green at 549 tests.

---

## Transport / cancellation resilience (v0.5.0) — survive a cancelled request and a dead engine

Over a full 19-section build the MCP server kept "disconnecting": tool calls stuck in *Running…*, the
server marked disconnected (needing `/mcp reconnect` or an app restart) — yet **every canon write actually
committed**. So the work succeeded server-side; the *result* failed to return and the process died with
**no persisted trace**. The disconnects correlated 1:1 with pressing Esc (cancelling an in-flight call). The
native deps and the projection were ruled out (a full `Projector.project(incremental=False)` over the real
113-node canon completes cleanly; reads survive — now a regression guard). Root cause: broken
cancellation/transport handling in the stdio server, made more frequent by the new concurrent build waves,
plus zero diagnostics. Fix = defense-in-depth so a lost response or a dead transport is survivable:

- **Supervisor launcher.** `launch_server.mjs` is now a persistent Node parent that logs every engine
  lifecycle event and recovers by *when* the engine died — because with `stdio:"inherit"` Node can't replay
  MCP's per-connection `initialize` handshake. A **startup** failure (crash before serving `initialize`,
  whose request is still buffered unread) is healed + relaunched in place (capped backoff 200 ms→5 s;
  crash-loop cap of 5 retries/60 s then a clean logged exit) — self-healing the cold-start race while
  keeping the parent (and the client pipe) alive. A **post-init** crash instead **exits cleanly** so the
  client reconnects with a fresh handshake, because relaunching onto the held-open, already-handshaked pipe
  would strand an *uninitialized* engine (looks alive, is dead — worse than a clean disconnect). An
  adversarial review caught that this distinction is load-bearing. The policy is the pure, exported
  `restartDecision`/`backoffFor`; the loop is `createSupervisor` with injectable deps, so the **real** loop
  is unit-tested with a fake engine (startup heal+retry, backoff, crash-loop cap, clean shutdown, post-init
  clean-exit, prompt exit on SIGTERM mid-backoff).
- **Persisted diagnostics.** `configure_logging` attaches a rotating `<KG_DATA>/server.log` (2 MB × 3)
  capturing every uncaught exception (main + worker threads), every tool-handler traceback, and every
  supervisor event — the single biggest debuggability gap, now closed.
- **Per-request isolation.** The tool envelope turns any `Exception` (incl. `BrokenPipeError`/`EOFError`/
  `ConnectionResetError`) into a structured result and keeps serving; it never swallows
  `CancelledError`/`KeyboardInterrupt`/`SystemExit`. The serve loop exits non-zero (→ supervisor relaunch)
  on an unexpected crash, 0 on a clean disconnect. A watchdog (`KG_HANDLER_TIMEOUT`, default 300 s)
  force-exits a wedged handler so the supervisor relaunches a fresh process instead of hanging.
- **Idempotent + decoupled.** `kg_write` returns a deterministic `receipt` and honours an `idempotency_key`
  (an exact retry replays the same receipt + counts, never a duplicate). Writes never touch the projection
  seam; a projection failure degrades a read (a `projection_degraded` flag over canon-derived data) instead
  of raising. New `kg_status` is a projection-FREE canon-only probe (counts, `unverified` queue, source
  section coverage) for confirming progress and resuming a partial build. An adversarial multi-agent review
  of the whole diff (4 dimensions → per-finding verification) hardened the design — the supervisor's
  startup-vs-post-init split, an idempotency-key payload-mismatch guard, the degraded flag on the structural
  reads, and several non-vacuous test strengthenings all came out of it. Suite green at 600 tests.

---

## Stage-4 precision — relation-direction fix (v0.5.2)

A real-corpus `/kg-eval` recorded **Stage-4 precision 0.61** (gate ≥ 0.70) at **span-support 0.94** — the
extractor was finding the right verbatim span but stamping the **wrong relation direction or type**: HEAD/
TAIL reversed on the directed "force" relations (`grounds` / `attacked_by` / `defends_against`), and
region-spanning relations over-claimed (`bridges` 0/5, `projects` 0/3 — instance-of / "reveals" / paired
concepts mislabeled). Because the `kg_write` boundary verifies the *span*, not the *direction*, the miss is
unreachable by a structural guard and has to be taught in the prompt. The fix is **prompt/pack only** (no
engine logic, every hard guarantee unchanged):

- `agents/extractor.md` (the model-facing lever): a new "Relation DIRECTION is load-bearing" section — a
  per-relation HEAD/TAIL role table for all ten edge types, a "don't reach for a region-spanning relation
  when the prose says something narrower" guard, and a worked reversal on a verbatim §2 span.
- `pack/pack.yaml` + `skills/.../references/pack-schema.md`: edge-type comments rewritten as HEAD/TAIL role
  definitions (the ten *names* are unchanged, so the validated pack contract — and `pack.yaml` `version`
  `0.1.0` — is untouched; comments never reach the model).
- `agents/annotator.md`: the `wrong_type` verdict now explicitly covers a **reversed direction** (right
  label, wrong order), so the precision labeling actually catches the dominant miss.

Verified in-repo: pack validates, `validate_plugin.py` versions agree, full suite green (603 passed), the
worked-reversal span verifies via `kg_engine.model.span_verifies`, and a 4-lens adversarial review
confirmed all ten taught directions match the source prose. **Not yet re-measured:** the `f4_probe ≥ 0.70`
gate must be re-run on the real build corpus — the bundled `examples/source.md` demo already scores **1.00**
and is too small (one `bridges`, one `projects` edge) to exhibit this regression, so the table row above
(Stage 4, precision 1.00) remains the demo-corpus measurement, not a refutation of the 0.61 finding.
