# PROGRESS — creativity-graph build log

One line per stage with its exit-test result and any recorded metric (§4 of the implementation plan).
Auditable end-to-end without re-reading code. Metric stages log the outcome and proceed; no human gate.

Corpus for all measured numbers: `examples/source.md` (the self-contained demo theory). Demo vault built
under `/tmp/kg-demo/` by driving the real engine (boundary → canon → projector → grounding → harness).

| Stage | Exit test | Result |
|---|---|---|
| 0 — Scaffold + env bootstrap | `claude plugin validate --strict`; MCP `kg_ping` | PASS — manifest, `.mcp.json`, SessionStart bootstrap (diff-the-manifest `uv sync`), `kg_ping` stub all present |
| 1 — Canon + transactional writes + lease lock + reconciler | `pytest tests/test_chaos.py -q` | PASS — crash-mid-write recovers via git; stale lock reclaimed; out-of-band forged verdict re-quarantined |
| 2 — Domain pack + glossary | `python -m kg_engine.pack validate pack/pack.yaml examples/source.md` | PASS — `PackContract` valid: 6 node_types, 10 edge_types, 12 glossary terms; source_coverage 1.0, glossary_grounded_in_source 1.0 |
| 3 — Staged extractor + boundary + PII scrubber | `pytest tests/test_invariants.py tests/test_scrub_egress.py -q` | PASS — fabricated/undeclared/span-less edges rejected/demoted; truncated JSON rejected with no partial write; seeded secret never leaves via `kg_scrub`; placeholder spans restored to original in the canon (§1.9 wired live) |
| 4 — Extraction evaluation | `python scripts/f4_probe.py score labels.csv` | **precision 1.00** (gate ≥ 0.70 ✓); astrology rate 0.00; **span-support 1.00**; 12 edges labeled, per-relation `grounds` 3/3. A span-present boundary makes fabrication unreachable, so in-graph precision is high by construction. |
| 5 — Projector + derived layer + query surface | `pytest tests/test_projector.py -q` | PASS — `graph.json` round-trips through NetworkX (20 nodes / 12 edges); 8 Leiden communities; incremental reproject touches only changed edges; `kg_context` within budget (1095/2000 tok), reads precomputed ranks O(1), never computes centrality in-request |
| 6 — Grounding loop + adversarial grounder + memory of failures | `pytest tests/test_grounding.py -q` + live demo | PASS — **12/12 edges grounded** via `kg_ground`; verdicts **survive a full reproject** (12 reattached, 0 orphaned); 0 failed/rejected on this curated corpus; the `failed`-state survival + falsification-counter path is pinned by `test_grounding.py` |
| 7 — Annotation agreement + specificity harness | `python -m kg_engine.harness agreement && … specificity` | LOGGED — **Krippendorff α = 0.000** on the 12-edge sample (raw agreement 11/12; α is degenerate under extreme label prevalence — the α-paradox — so the grounding signal **stays advisory**); **specificity gate OFF** (generality_confound_detected=false, rank_churn 0.0 → specificity-weighting does not clearly separate on this small graph; degree remains the honest advisory) |
| 8 — Ideation comparison | `python -m kg_engine.harness ideation` | LOGGED — see table below; verdict: *graph condition did not clearly beat control* on the harness's diversity/novelty test, **but** the graph arm cut the unsupported-claim rate ~4× (0.083 vs 0.333) and raised utility (0.367 vs 0.167). The novelty metric (1 − source-overlap) rewards untethered text, so control "wins" novelty by inventing; the graph arm trades raw novelty for grounding. Logged; execution proceeds (§4). |
| 9 — Hardening + packaging | `pytest tests/ -q`; `claude plugin validate --strict` | PASS — full suite **green (53 tests)**; **`claude plugin validate --strict` ✔ passes**; version `0.1.0`; component layer (5 agents, 5 commands, skill + 3 references) authored, adversarially verified against the engine source, and cross-checked (every example span verifies; every edge-id canonical; every `subagent_type` resolves). **Installed locally and the full `/kg-build` → `/kg-ground` → `/kg-query` workflow run end-to-end through the installed plugin** (see *Live validation* below). Deferred to a future release: headless `--backend` CI path, edges-per-KB injection rate-limit, public marketplace publish/`tag`. |

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
- test suite: **53 passing** (`pytest tests/ -q`)

## Live validation (installed plugin, fresh vault)

The plugin was installed locally (user scope, via a single-plugin `marketplace.json`) and the full
workflow run end-to-end **through the installed plugin** — `/creativity-graph:kg-build` → `/kg-ground` →
`/kg-query` on a fresh vault (`/tmp/kg-vault`). Driving it for real surfaced and fixed four packaging/quality
bugs static review could not (committed):

1. **MCP tool namespace** — plugin tools are `mcp__plugin_<plugin>_<server>__<tool>`, not `mcp__<server>__`;
   the corrected grants let the `kg-extractor` subagent reach the boundary.
2. **userConfig env wiring** — `${CLAUDE_PLUGIN_OPTION_*}` doesn't expand in `.mcp.json` and was clobbering
   the auto-injected values; `KG_SOURCE_PATH` now uses `${user_config.source_path}` so the server reads the source.
3. **Cold-start spawn race** — the server now launches via `scripts/launch_server.sh` (bash always exists →
   spawn never fails → no spurious "needs-auth" caching; the wrapper self-heals the venv).
4. **`kg_context` query matching** — tokenize + OR-match terms so a natural-language `/kg-query` hits
   (was a 0-item whole-string `LIKE` miss).

Live run results:

- **`/kg-build`** — 31 ACCEPTED / 0 rejected; 18 nodes, 12 span-present edges; egress `kg_scrub` ran (0 redactions).
- **`/kg-ground`** — `unverified 12 → 0`: **11 grounded, 1 rejected** (generality confound caught), **2 failed**
  (adversarial counter-edges). `falsification_counters = 3`, never pruned; verdicts survive reprojection.
- **`/kg-query`** — answered with provenance + falsification counters on every edge; refused to present the
  rejected `bridges` claim as fact; labelled the structural-bridge advisory as a heuristic.
