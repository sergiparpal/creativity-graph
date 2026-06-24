# creativity-graph — Exhaustive Code Review (2026-06-24)

## Scope & method

Full-codebase review of `creativity-graph` at `main` (HEAD `9fb1ee5`), ~6,500 LOC Python + Node/shell
launchers + manifests. Conducted as a multi-agent sweep: **24 subsystem reviewers → per-module adversarial
verification against the real code → independent re-derivation of the top findings**. Every finding below
**survived adversarial verification**; the two Critical and three High findings were additionally
**reproduced and re-read by hand** against the live code paths (line refs confirmed).

**Baseline:** full `pytest` suite **green — 372 tests, Python 3.12**. (The stale `.pytest_cache/lastfailed`
entries reference *deleted* scratch repros — `test_explode_child_ids_can_collide…`,
`test_collapse_on_dangling_target_sweeps…`, `test_canon_span_edit_unchanged_source_is_missed` — not real
failures. Two of those scenarios turn out to be **real, uncovered bugs**; see M5 and L-coverage notes.)

This is the **second** exhaustive pass. The prior review (`CODE_REVIEW_2026-06-23.md`, v0.3.0, 44 findings)
was fully resolved in `c4c9f63`. This pass focuses on **new bugs** and **regressions**, with emphasis on the
**R1–R6 additions that landed *after* that review and were never reviewed** (`export.py`,
`templates/graph_html.py`, `sources.py`, `canonmerge.py`, the projector R3 staleness path, `kg_agenda`).
None of the prior 44 are re-reported.

## Totals

| Severity | Count |
|---|---|
| Critical | 2 |
| High | 3 |
| Medium | 8 |
| Low | 29 |
| Nit | 6 |
| **Total** | **48** |

*(49 confirmed findings → 48 after merging the duplicate verdict-erasure pair: the boundary-side
`failure_ids`-scope finding and the canon-side `incoming wins` finding are two ends of the same bug — **C1**.)*

The single most important fix is **C1**: it silently and permanently destroys a `kg_ground` verdict on the
documented-idempotent `/kg-build` re-run — the exact mirror of the previously-fixed H1, applied then only to
the *failure* half of the state space. The headline integrity guarantee (§1.8 verdict durability) is
currently defeated for `grounded`/`obsolete` edges.

---

## Critical

### C1 — Idempotent re-build silently erases a `grounded`/`obsolete` verdict; verdict-survival protection was scoped only to `FAILURE_STATES` `[invariant-violation]`
`boundary.py:217, 337-338` + `canon.py:478-481` + `reconciler.py:255, 267-284`

The boundary's re-emit verdict-survival guard protects only the **failure** half of the `kg_ground`-owned
state space. `boundary.py:217` builds `failure_ids` from `FAILURE_STATES = {rejected, failed}`, and the
span-present re-emit quarantine at `boundary.py:337-338` fires only `if ident in failure_ids`. A `grounded`
(or `obsolete`) edge id is **not** in that set, so re-emitting the identical span-present edge — exactly what
an idempotent `/kg-build` re-run does (`CLAUDE.md`: *"Re-emitting the same edge updates rather than
duplicates (idempotent builds)"*) — is **ACCEPTED** (`reason=deduped`, `written=True`), flows through
`merge_results_into_nodes`, and reaches `Canon._merge_into_existing`, where **`by_id[e.id] = e  # incoming
wins`** (`canon.py:480`) physically overwrites the grounded edge — and its `verdict_by`/`verdict_at` — with
the fresh `unverified` one.

The reconciler **cannot** rescue it: `_forged` returns `False` immediately for the now-`unverified` edge
because `unverified not in GROUNDABLE_STATES` (`reconciler.py:255`) — it polices transitions *into* a verdict,
never *away* from one — and `reattach_after_reproject` (`reconciler.py:267-284`) only **counts/reports**
orphaned verdicts; it never re-applies a lost verdict from the audit log.

Tellingly, the **out-of-process mirror** of this merge — the `canonmerge.py` git driver — explicitly *keeps* a
verdict that agrees on both sides (`canonmerge.py:87-100`). The two mirror paths disagree exactly on verdict
preservation; the in-process one erases what the git driver protects.

- **Verified (hand-reproduced):** `kg_write(edge)` → `kg_ground(eid,'grounded')` → full reconcile sweep →
  `kg_write(same payload)` ⇒ disposition `ACCEPTED reason=deduped`, persisted edge `epistemic_state=unverified`,
  `verdict_by=None`; a subsequent full sweep does **not** restore it (permanent). `obsolete` takes the same
  path (`obsolete ∈ GROUNDABLE_STATES` but `∉ FAILURE_STATES`). The existing
  `test_fix_boundary_model.py:112-125` (`test_reemit_non_failed_existing_edge_still_deduped`) *codifies the
  buggy ACCEPTED-on-grounded-re-emit behavior* — so the asymmetry was never an intended design choice.
- **Impact:** Silent, permanent loss of a `kg_ground` verdict on a normal documented path — defeats §1.8.
- **Fix:** Protect the **full** `kg_ground`-owned set on re-emit, not just the failure subset. Simplest:
  rename `failure_ids` → `protected_ids` built from `GROUNDABLE_STATES` (already exported from `model.py`) and
  quarantine the span-present re-emit on that single set (keep the *reverse*-id collapse confined to the
  hypothesized lane as today). Defense-in-depth: have `_merge_into_existing` refuse to downgrade a
  verdict-bearing edge back to `unverified` (carry `verdict_by/at` forward), mirroring the `canonmerge`
  driver's rule. Update the test that codifies the bug; add `test_reemit_grounded_edge_quarantined_and_verdict_survives`.

### C2 — `merge_results_into_nodes` keys edge attachment on the raw `edge.source`, not the slug — phantom node → whole `kg_write` batch silently rolled back `[bug]`
`boundary.py:353-369` (defect at 364, 366-367)

`merge_results_into_nodes` attaches each written edge to its source node using the **raw** string
`r.item.source` (`edges_by_node.setdefault(r.item.source, {})`, line 364) and tests membership with the raw
string (line 366). But every other layer keys on the **slug**: node id is `slug(label)` (`_canon_node`, line
240), `edge_id = e_{slug(source)}__…` (`model.py:133`), node files are `slug(id).md` (`canon.py:386`), and
dedup keys on the slugged edge id. `Edge.source` is stored verbatim/un-slugged (`model.py`), so when an edge
references its endpoint by the **human label** rather than the pre-slugged id (e.g. `source='Free Energy
Principle'`), line 366 finds it absent from `nodes={'free-energy-principle': …}` and line 367 fabricates a
**second** `Node(id='Free Energy Principle')`. Both resolve to the same file `free-energy-principle.md`; the
real node is written first, then `_check_slug_collision` (`canon.py:474`) raises and `write_nodes`
**rolls back the entire batch** — every node and edge silently discarded, surfaced only as a confusing
`"node id slug collision"` message.

- **Verified (hand-reproduced end-to-end):** payload `{nodes:[{label:'Free Energy Principle'},
  {label:'Active Inference'}], edges:[{source:'Free Energy Principle',…}]}` ⇒ `rolled_back=True`,
  `error="node id slug collision: 'Free Energy Principle' and 'free-energy-principle' both map to
  free-energy-principle.md"`, **zero files on disk**. On the contract-documented placeholder path
  (`contract.md §4`, auto-create a placeholder source node) there is no rollback but the persisted note's
  frontmatter `id='Free Energy Principle'` **diverges** from its filename `free-energy-principle.md` — a latent
  corruption where a later slug-keyed write creates a distinct logical node over the same file.
- **Reachability:** The contract says edge `source`/`target` *should* be the slug id, so a strictly-conforming
  extractor avoids C2 — but the endpoint is an LLM subagent that naturally emits labels, and the
  placeholder-by-label path is explicitly documented. The failure is catastrophic and silent when triggered.
- **Fix:** Canonicalize the attachment key to the slug, exactly as `edge_id`/`node_path`/dedup do:
  `edges_by_node.setdefault(_slug_label(r.item.source), {})[r.item.id] = r.item`, and create placeholders as
  `nodes.setdefault(src, Node(id=src, label=src))` with `src` already slugged. The edge's stored `source`
  field and slug-derived id are untouched, so on-disk identity/dedup are unchanged.

---

## High

### H1 — `export.py` HTML inlining: `</`-only escaping is defeated by the `<!--<script>` script-data-double-escape state `[security/leak]`
`export.py:86`

`build_html` inlines render data as `window.__KG_DATA__ = <payload>;` inside a literal `<script>` block and
sanitizes **only** `</` → `<\/` (`payload = json.dumps(data, sort_keys=True).replace("</", "<\\/")`). The
inlined fields include node `label` and edge `relation`/`span` — free text copied verbatim from frontmatter /
source (`label` is `fm.get('label','')` with no HTML scrubbing; `kg_scrub` is PII-only; spans are verbatim
source substrings). Per the WHATWG tokenizer, inside a `<script>` element `<!--` enters *script-data-escaped*
and a subsequent slash-less `<script` enters *script-data-double-escaped*, in which `</script>` does **not**
close the element. The `</`-only escape never touches `<!--` or the slash-less `<script`, so a label
`<!--<script>` reaches the payload intact and **swallows the template's real `</script>`** — guaranteed
render corruption from adversarial source content, and a script-injection primitive in the local `file://`
origin. The existing `test_export.py::test_html_escapes_script_close_in_labels` covers only the naive
`evil</script>` case (which `</` *does* catch).

- **Fix (standard):** escape all markup-significant characters, not just `</`:
  `json.dumps(data, sort_keys=True).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')`
  (optionally U+2028/U+2029). The `\uXXXX` forms round-trip to the identical parsed JS value but leave no
  literal `<`/`>` for the HTML tokenizer, defeating both `</script>` and `<!--<script>`. Add a regression test
  with label `'<!--<script>'`. *(Note: L-report flags the same untrusted-text-inlining gap in
  `GRAPH_REPORT.md` via backticks/embedded HTML — fix both together.)*

### H2 — `is_stale()` ignores a schema-outdated derived DB → permanent `OperationalError` on reads after a plugin upgrade `[bug]`
`projector.py:555-568`

`is_stale()` (the per-read staleness gate; sole read-side caller is `_ensure_projected`) decides reprojection
from only the cheap `(name,size,mtime)` signature and, on a move, the per-node content hash. It **never**
consults `_schema_outdated()`. So a derived `index.sqlite` built **before** the Stage-2 node columns
(`betweenness`/`spec_betweenness`/`specificity`/`gate_on`) with **unchanged** canon content makes `is_stale()`
return `False` (cheap-sig short-circuit, line 562) — no reprojection runs, so neither the `_connect()`
schema-heal (write path only) nor the `_schema_outdated()` force is reached. Read tools then hit the outdated
table through `_ro()` (line 571, a separate connection with **no** schema-heal): `kg_context`'s
`SELECT betweenness,spec_betweenness,specificity FROM nodes` raises `OperationalError: no such column:
betweenness` (so do `kg_agenda`, `kg_export`). Because canon is unchanged, `is_stale()` stays `False`
**forever** — the crash is permanent for a read-only vault until the user happens to edit the canon.

- **Verified:** with canon unchanged, `is_stale()==False` while `_schema_outdated()==True`, and `kg_context()`
  raises `no such column: betweenness` (`query_graph` survives via `SELECT *`).
- **Scenario:** upgrading the plugin over an existing `${CLAUDE_PLUGIN_DATA}/derived` (the derived DB survives
  upgrades; only the venv rebuilds on a `pyproject` change). Self-heals on any canon edit; derived layer is
  rebuildable — so High, not Critical.
- **Fix:** gate `is_stale()` on the schema right after the existence checks:
  `if self._schema_outdated(): return True`. `_schema_outdated()` is a single `PRAGMA table_info(nodes)` (O(1)),
  cheap enough to front every read; it forces a full reproject that heals the schema.

### H3 — A non-UTF-8 / binary source file crashes `SourceSet` construction → disables the entire tool surface `[edge-case]`
`sources.py:34-38`

`SourceSet.__init__` does `p.read_text(encoding="utf-8")` guarded only by `except OSError`. **`UnicodeDecodeError`
is a subclass of `ValueError`, not `OSError`**, so a non-UTF-8 / UTF-16 / binary `.md`/`.txt` among the
resolved set propagates out of the constructor. R4's whole purpose is pointing `source_path` at a directory or
glob, so one odd-encoded file among many is a realistic input. `KGEngine.source_set()` memoizes only on
success (the cache assignment is *after* `SourceSet(...)` evaluates), so the crash **repeats on every call**.
`source_set()`/`source_text()` are on the hot path of `kg_write`, `kg_ground`, every lazy projection (R3
advisory + IDF corpus), and `kg_export` — so one bad file silently disables the whole graph. The docstring
even claims it *"mirrors `canon.all_nodes()`'s tolerance"*, but `all_nodes()` uses `except Exception`
precisely so one bad note can't crash every read.

- **Verified:** `issubclass(UnicodeDecodeError, OSError) is False`; a `0xff` byte propagates from `read_text`.
- **Fix:** broaden the catch to `except (OSError, ValueError):` (or `except Exception:` to truly mirror
  `all_nodes()`). **Skip** the undecodable file rather than decoding with `errors='replace'` — byte
  substitution could alter content and create phantom span matches.

---

## Medium

**M1 — Reconcile sweep aborts vault-wide (and deletes the note) on a non-canonical correction that slug-collides
with a distinct canonical note** `[bug]` — `reconciler.py:203-216`. The correction path unlinks the original
(`Foo.md`) *before* `write_one` → `_check_slug_collision` raises `ValueError` when a distinct canonical `foo.md`
exists; the only `try/except` in the loop wraps just the read, so the **whole sweep aborts mid-loop** with the
note already destroyed. The per-session reconcile runs with `check=False`, so the non-zero exit is swallowed and
the §1.8 re-quarantine sweep silently stops for the **entire vault** every session while the colliding pair
co-exists — forged verdicts on other notes survive forever. *Fix:* write/stat the canonical path first and only
unlink on success; wrap the unlink+write block in `try/except` so a colliding note degrades to skip-and-retry.

**M2 — Specificity gate is decided over the FULL graph (failed/rejected included) while the betweenness it gates
is computed over the live subgraph** `[logic-error]` — `projector.py:221-238`. Stored
`betweenness`/`spec_betweenness` are computed over `und` (the live subgraph excluding `failed`/`rejected`, per
§1.7), but the gate verdict (line 234) calls `_specificity_gate(_node_link_data(G), corpus)` with the **full**
`MultiDiGraph` including every refuted counter-edge — which `harness.specificity` re-runs betweenness over with
no failed-edge filtering. So the adversarial grounder's `failed` counter-edges (the exact edges §1.7 excludes
from centrality) can flip `gate_on`, which then governs ranking of a live-subgraph `spec_betweenness` the gate
never measured. *Fix:* build the gate payload from the live subgraph: `_specificity_gate(_node_link_data(live),
corpus)` (`live` already carries node attrs incl. `label`).

**M3 — Grounding drain re-projects (full betweenness) on every `kg_ground` call, defeating the O(1) owner index**
`[performance]` — `server.py:276-293` (cost in `projector.py:306,221`). `_owner_of_edge` calls
`_ensure_projected()` *before* the O(1) index lookup. Each prior `kg_ground` bumps `node.updated_at`, which
feeds `_file_hash`, so `is_stale()` reliably returns `True` on the next call; `_project_locked`
unconditionally runs `_ranks(G)` → `nx.betweenness_centrality(und)` (O(V·E)) + the global specificity gate.
Draining N edges costs ~N full betweenness computations (O(N·V·E)) — re-introducing exactly the quadratic cost
the `owner_of_edge` index was added to remove. The existing scan fallback (`server.py:290-292`) resolves the
owner with **no** projection, so the call is pure overhead. *Fix:* drop the `_ensure_projected()` inside
`_owner_of_edge`; query the index read-only and fall through to the canon scan on a miss.

**M4 — `_effective_max_tokens` misses the SDK's time-based non-streaming guard (~21,333 tokens); a moderate
`--max-tokens` override fails every section pre-flight** `[edge-case]` — `backend.py:179-197`. The clamp only
considers `MODEL_NONSTREAMING_TOKENS`. The Anthropic SDK (verified against floor 0.77.0 and installed 0.111.0)
applies a second guard: with `stream` unset and default timeout, `messages.create()` raises
`ValueError("Streaming is required …")` when `max_tokens > 21333`. `claude-opus-4-8` is absent from the cap
table (cap `None`), so a user who raises `--max-tokens 22000` / `KG_BACKEND_MAX_TOKENS=22000` — which the
tool's own truncation message ("raise --max-tokens") invites — makes the **first `create()` of every section**
raise pre-flight; `run()`'s `except Exception` records each as a failed section, producing nothing and exiting
1. *Fix:* also clamp to the time floor `int(600*128000/3600)=21333` (further `min` with the table cap when
present), or pass an explicit `timeout=`, or stream when above the floor.

**M5 — `collapse` on a community-less / `-1` target sweeps all unrelated danglers into one bogus compression**
`[logic-error]` — `operations.py:43-44`. The target branch
`return _community_members(G, _attr(G, target, "community", -1))` does **not** exclude community `-1`.
`load_graph` auto-creates dangling edge-target nodes attribute-less (community `-1`), and the projection writes
`community=-1` for danglers. Passing a dangling target makes `_community_members(G, -1)` return **every**
community-less node, and `collapse_payload` mints one `compression` whose body asserts they form a coherent
cluster. The default branch (lines 45-51) already filters `c != -1` — proving `-1` means "no community" — so
the omission in the target branch is a confirmed asymmetry (matches the deleted `test_collapse_on_dangling_
target_sweeps_all_danglers` repro). Bounded by the hypothesized/unverified lane the grounding loop filters, so
Medium not High. *Fix:* `cid = _attr(G, target, "community", -1); return _community_members(G, cid) if cid != -1
else []`.

**M6 — `regroup` degrades to a meaningless O(n²) candidate explosion when `_repartition` falls back to the
identity (all-singleton) partition** `[edge-case]` — `generate.py:255-269, 390-395`. If both the
leidenalg/igraph branch *and* `asyn_lpa_communities` raise, `_repartition` returns every node its own
community; the regroup skip `if … new_comm.get(v) == nu: continue` then never fires, so every non-adjacent
intra-community pair becomes a "newly-visible bridge" with degree-noise scores and arbitrary top-k. Trigger is
rare (both community algos must fail), no crash. *Fix:* detect the degenerate repartition
(`len(set(new_comm.values())) == und.number_of_nodes()` with >1 node) and return `[]`, or have `_repartition`
signal the identity fallback explicitly.

**M7 — Content stamp tracks the *bootstrapping/checking* interpreter, not the *venv's* interpreter — forces
spurious full rebuilds and mis-targets the ABI guarantee** `[logic-error]` — `bootstrap.py:145-156, 583`.
`compute_stamp()` folds in `sys.version_info`/`platform.machine()` of whatever interpreter runs `bootstrap.py`,
but the docstring claims it protects the *venv's* compiled-wheel ABI. `install_with_uv()` never passes
`--python`, so `uv` may pick a different interpreter to build the venv; and a system-Python upgrade between
sessions makes the next `--check` (run under the new interpreter) recompute a different hash and trigger a full
rebuild of a still-working venv (its interpreter is pinned in `pyvenv.cfg`). *Fix:* derive the interpreter
identity from the venv's python (query it once post-build, persist it) and compare that on `--check`, not the
running interpreter.

**M8 — Merge driver's system-python fallback can lack the engine's PyYAML dependency (import crash on a bare
clone)** `[edge-case]` — `canon_merge_driver.mjs:61-74`. `enginePython()` falls back to `systemPython()`,
advertised as keeping the driver usable "even when no engine venv was ever provisioned", but `systemPython()`
only checks `>= (3,10)` — not that the engine's third-party deps import. `canonmerge.py` → `model.py` does
`import yaml` (PyYAML, not stdlib), so on a fresh clone whose bare system Python lacks PyYAML the driver crashes
with `ModuleNotFoundError` before `main()` runs. It **fails safe** (no data loss; git marks the file
conflicted) but the user gets an opaque traceback instead of the promised graceful merge. *Fix:* strengthen the
probe to also `import yaml` under `PYTHONPATH=scripts` so a dep-less interpreter falls through to the honest
"no engine python" branch — or soften the comment.

---

## Low (29)

Robustness, version-floor, and contract-consistency gaps. Each is individually confirmed.

| Location | Cat | Issue |
|---|---|---|
| `model.py:109-113` | bug | `span_verifies()` fails open for a Cf-only/zero-width span (`normalize_text` drops Cf → `''` ∈ any string). **Latent only** — all *production* paths (`sources.verifies`, boundary, server, projector) independently guard with `not ns`; the helper has no live callers, but it contradicts its own §1.5 docstring. Fix: guard on the normalized span. |
| `model.py:213-215` | edge-case | `__post_init__` synthesizes a fresh `utcnow()` for missing `created_at/updated_at` at *parse* time → two parses of a timestamp-less hand-authored note differ → churns `_file_hash` → redundant reprojection until written back. Fix: exclude the two timestamps from the staleness hash. |
| `canon.py:498-503` | race/atomicity | `_rollback` restore writes are non-atomic and not fsynced, unlike the rest of the module. |
| `projector.py:759-789` | performance | `kg_context.stale_verdicts` list is uncapped and bypasses the token budget. |
| `projector.py:150-160,471-477` | edge-case | IDF corpus and the R3 source hash read from two different inputs (`source_text` vs `source_set`) — can diverge under R4. |
| `generate.py:338-344` | logic-error | `mechanism="all"` emits duplicate edge candidates (regroup ≡ degraded ensemble) with the same `edge_id` under two mechanism names. |
| `operations.py:41-42,56-57` | edge-case | `_resolve_cluster` doesn't dedup explicit `members`; duplicates pass the `>=2` guard as a degenerate single-node compression. |
| `operations.py:43-51` | edge-case | Explicit-but-missing collapse `target` silently collapses the largest community instead of signalling a bad target. |
| `operations.py:78-79,102` | edge-case | `int(k)` raises `ValueError` on non-numeric `k` in `explode_payload`/`open_payload` (defense-in-depth gap vs unvalidated MCP input). |
| `harness.py:158,162,164` | edge-case | `absorption()` crashes on malformed `generations.json` the server passes unvalidated (`int()` coercion + `.get()` on non-dict records). |
| `boundary.py:239-245,306,326-327` | edge-case | `restore()` is applied only to `edge.span`, never to node labels/ids or endpoint names — a high-tier PERSON/ADDRESS placeholder can persist verbatim into the canon. |
| `scrub.py:178-184` | edge-case | `extra_terms` for a category inactive at the current tier are silently dropped — a caller-supplied redaction list does nothing at lower tiers. |
| `pack.py:27,45-51` | logic-error | `specificity_seeds` is validated and stored but never consumed by any engine path (dead config). |
| `sources.py:63-64` | edge-case | `**` in a glob source path is treated as a single `*` (no recursion) — silently under-collects nested sources. |
| `backend.py:213` | version-floor | `thinking={"type":"adaptive"}` isn't in the typed `ThinkingConfigParam` union at the `anthropic>=0.77` floor (type-checker mismatch; no runtime break). |
| `canonmerge.py:211-223` | race/atomicity | `main()` writes the merged result non-atomically and has no top-level fail-open guard. |
| `canonmerge.py:191-194` | edge-case | An unparseable-but-nonempty base loses its real body, spuriously conflicting an otherwise one-sided body edit. |
| `export.py:147,161-162` | security/leak | `GRAPH_REPORT.md` inlines raw labels/relations/spans without neutralizing backticks/embedded HTML (the Markdown twin of H1). |
| `server.py:385` | race/atomicity | `kg_rename`: old-note `unlink` can escape as an unstructured MCP exception instead of `{ok:False}`. |
| `bootstrap.py:467-481,565-575` | edge-case | A legitimately `>1860s` honest build can exceed the foreground `--wait` deadline; the waiter returns `0` (success) without building → server launches against an unready venv. |
| `launch_server.mjs:37-41` | logic-error | Comment overstates fidelity: relative `CLAUDE_PLUGIN_DATA`/`KG_ENGINE_VENV` resolve against repo-root here vs cwd in bootstrap. |
| `launch_server.mjs:91-104` | edge-case | `foregroundCatchUp` ignores `spawnSync` ENOENT/error result — bootstrap failure cause not surfaced. |
| `hooks/precontext.py:27-33` | logic-error | Resolves data/project dirs from a different env-var set than the server — diverges when `KG_PROJECT_DIR`/`KG_DATA` overrides are used. |
| `hooks/precontext.py:27-33` | edge-case | Doesn't strip unsubstituted `${...}`/sentinel env values the way every sibling launcher does. |
| `validate_plugin.py:82-85` | logic-error | Version cross-check silently **skips** when a version line is absent/reformatted in `pyproject.toml`/`__init__.py` (CI gate can pass on a missing version). |
| `validate_plugin.py:80-81` | edge-case | Version regex matches only double-quoted strings; single-quoted (valid TOML/Python) versions bypass the agreement check. |
| `pyproject.toml:14-15` | dependency | `python-igraph>=0.11`/`leidenalg>=0.10` floors span the igraph `0.x → 1.0` major bump; a swallowed API break would silently degrade Leiden detection (and the bridge advisory). Consider an upper bound or a version-probe. |
| `.claude-plugin/plugin.json:12-16` | logic-error | `userConfig.source_path` declared `type:"file"` cannot select the documented directory/glob (R4) inputs. |
| `projector.py:915-921` | coverage | Agenda edgeless-communities: the failed/rejected crossing-skip branch (invariant 6) has **no test**. |

## Nit (6)

| Location | Issue |
|---|---|
| `generate.py` (many) | Dead `pack`/`corpus` parameters on every mechanism; compression docstring says "corpus mean" for a value computed from the graph, not the corpus arg. |
| `pack.py:93-96` | `_term_in_text` docstring overclaims robustness for terms with leading/trailing non-word characters. |
| `bootstrap.py:518,533-542` | Background worker's detached-log file handle is never closed in the parent (fd held for the parent's lifetime). |
| `validate_plugin.py:66` | `hooks.json` is loaded for JSON-validity but its parsed content is never structurally checked. |
| `f4_probe.py:176-177` | `score()` per-relation table keys off the sheet's relation text; a missing column collapses every row into one `'?'` bucket. |
| `projector.py:894,899` | Agenda hub detector: the `grounded/decided==0.5` ratio boundary and the just-below-hub negative are unpinned by tests. |

---

## Cross-cutting observations

- **Verdict-durability is the recurring fault line.** The prior review's marquee bug (H1) was failure-memory
  erasure on re-emit; **C1 is its un-fixed twin for positive verdicts**. The fix landed on `FAILURE_STATES`
  only. Recommend treating the *entire* `GROUNDABLE_STATES` set uniformly at the boundary **and** adding the
  merge-layer downgrade-refusal as defense-in-depth, so neither the boundary nor a direct `write_nodes` caller
  can reset a verdict.
- **Test-suite gaps that mirror the prior H4 masking-test pattern:** the deleted scratch repros
  (`*collapse_on_dangling*`, `*explode_child_ids*`, `*canon_span_edit_unchanged_source*`) were removed without
  leaving permanent coverage — M5 (collapse/danglers) is a **real, now-uncovered** bug. `test_fix_boundary_
  model.py:112-125` actively **asserts C1's buggy behavior**. Add the C1/M5 regressions and the invariant-6
  agenda-skip test (L).
- **Encoding/robustness on the new R4 path is thin:** H3 (UTF-8), L (`**` glob recursion), L (IDF-vs-R3 source
  divergence) all cluster in `sources.py`. The R4 ingestion would benefit from a small fuzz/edge-case test set.
- **No deprecated Python idioms / no >3.10 floor violations** were found in engine source (the one
  version-floor item, `backend.py:213`, is a typed-union mismatch, not a runtime break). Dependency pins are
  sane; the only flag is the unbounded igraph major-bump window (L).
- **`server.py` JSON-serializability and explicit-null handling** (the prior review's recurring complaint) are
  now clean — the verifiers confirmed non-finite floats are dropped at the boundary and the wrappers accept the
  documented `null` args. The new performance regression is M3 (reproject-on-drain), not a contract bug.

## Suggested fix order

1. **C1** — verdict erasure on re-build (full `GROUNDABLE_STATES` protection + merge-layer downgrade refusal + tests).
2. **C2** — slug the edge-attachment key in `merge_results_into_nodes`.
3. **H2 / H3** — one-line schema-gate in `is_stale()`; broaden the `SourceSet` decode catch. (Both are cheap, high-value robustness fixes on normal paths.)
4. **H1 + L-report** — escape `<`/`>`/`&` for the HTML *and* neutralize Markdown report inlining.
5. **M1** — make the reconcile sweep resilient (the vault-wide-outage one).
6. **M2, M3, M4, M5** — gate-graph consistency; drop reproject-on-drain; clamp the token time-floor; the dangler-collapse guard.
7. Sweep the Low/Nit list opportunistically; prioritize the two `validate_plugin.py` CI-gate holes (a missing/single-quoted version currently passes CI silently).

*All findings survived adversarial verification; the 2 Critical + 3 High were additionally hand-reproduced and
re-read against the live code. The independent third-pass double-check and the completeness critic did not
complete (session token limit) — so a handful of Low/Nit items rest on two-pass (review + per-module verify)
confirmation rather than three; the high-severity findings are fully verified.*
