# creativity-graph — Final Code Review Report (v0.3.0)

## Executive summary

creativity-graph v0.3.0 is a well-architected, disciplined codebase: the deterministic engine/agent boundary is real, the failure-memory and forged-verdict invariants are mostly enforced *structurally*, and the full pytest suite is green on Python 3.12. This exhaustive review nonetheless surfaced a cluster of genuine integrity bugs the current tests do not catch — chiefly that **failure/verdict memory can be silently erased on two independent paths**: (1) an idempotent `kg_write` re-emit overwrites a `failed`/`rejected` edge with a fresh `unverified` one, and (2) the reconciler writes its un-forgery correction to the *slug* path instead of the file it read, leaving the forged verdict intact and self-concealing — the latter additionally **masked by a test that only asserts the report, never the on-disk state**.

Beyond those, there are concrete robustness gaps: a git-commit failure spuriously discards durably-written canon; six MCP wrappers reject explicit-`null` args the plugin's own docs recommend sending; `kg_context` can serialize ~2× its advertised token budget; `absorption()` emits non-JSON `Infinity` over the wire; and a systematic PII leak in the opt-in `sensitivity='high'` scrub tier. None are showstoppers for the current green build, but several are data-integrity or contract bugs worth fixing before the next release.

**Overall health: solid foundation, with a focused set of high-priority correctness fixes needed.**

## Totals (after dedup)

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 6 |
| Medium | 6 |
| Low | 17 |
| Nit | 15 |
| **Total** | **44** |

*(48 raw findings deduplicated to 44: transplant perf merged 2→1; kg_operate `members` merged 2→1; kg_write rollback-reporting merged 2→1; CHANGELOG header merged 2→1. The reconciler slug-path code bug and the test that masks it are kept as two distinct, cross-referenced findings.)*

All findings below are **confirmed** (each survived independent adversarial verification). None are marked uncertain.

---

## High

### H1 — kg_write silently erases failure memory when re-emitting a failed/rejected edge `[logic-error]`
`scripts/kg_engine/boundary.py:277-318`

The `collapses-into-known-failure` quarantine (boundary.py:286-288) is gated inside the `if is_hypothesized:` branch. A normal span-present/inferred edge re-emitted through `kg_write` (e.g. an idempotent `/kg-build` re-run) whose canonical id collides with an existing `failed`/`rejected` edge is only marked `deduped` and ACCEPTED (`written=True`). `canon._merge_into_existing` then does `by_id[e.id] = e` ('incoming wins', canon.py:410), overwriting the verdict with a fresh `unverified` edge. The reconciler does not restore it: `_forged` (reconciler.py:172) only policies `GROUNDABLE_STATES`; `unverified` is excluded, so the `failed→unverified` erasure is invisible. **Reproduced end-to-end.**

- **Impact:** Violates §1.7 ('rejected/failed edges are negative information — never pruned... remembers what was refuted'). The graph permanently loses a recorded refutation on an ordinary idempotent build re-run, defeating the system's central honesty guarantee.
- **Fix:** Bind the failure-collapse quarantine to **every** lane: after computing `ident`, if `ident in failure_ids or edge_id(target,relation,source) in failure_ids`, QUARANTINE with `collapses-into-known-failure` regardless of provenance (`failure_ids` is already computed at boundary.py:208 and passed in). Additionally/alternatively, make `canon._merge_into_existing` refuse to downgrade a `FAILURE_STATES` edge back to `unverified`.

### H2 — git commit failure spuriously rolls back successfully-written canon nodes `[bug]`
`scripts/kg_engine/canon.py:390-396`

Per-node `_atomic_write` calls durably land (fsync + `os.replace`) first, but `git add -A` / `git commit` run inside the same `try` block with `_git`'s default `check=True`. Any non-zero git exit (missing `user.name`/`user.email` → rc 128 *reproduced*; a rejecting pre-commit/commit-msg hook → rc 1; `.git/index.lock` contention → rc 128) raises `CalledProcessError`, caught by the bare `except Exception` and routed to `_rollback`, which reverts the already-fsynced files. Worse, `git add -A` already staged the new content, so after the byte-level rollback the index is staged-but-reverted (`git status MM`), so a later successful batch could commit the reverted content.

- **Impact:** Canonical content that successfully landed on disk is discarded because of a commit failure orthogonal to the writes; secondary index/working-tree inconsistency can poison a later commit. (`rolled_back=True` is surfaced, so not silent — but the work is lost.)
- **Fix:** Move the git add/commit **outside** the data-rollback `try` (or make commit failures non-fatal with `check=False`), exactly as `kg_rename` already does (server.py:350-351). Only `_atomic_write` loop failures should trigger `_rollback`; the canonical content lives in the markdown files, not the commit.

### H3 — Reconciler writes the un-forged correction to the slug path, leaving the forged verdict intact and self-concealing `[bug]`
`scripts/kg_engine/reconciler.py:135`

`scan()` reads each note by its actual on-disk path (the reconciler-4 fix at 106-110 deliberately avoids re-slugging), but persists a detected-forgery correction via `self.canon.write_one(node)` (line 135), which writes to `canon.node_path(node.id)` — the **slug-canonical** filename, not the file it read. For a hand-created note whose filename isn't slug-canonical (e.g. `Foo.md` holding id `Foo`; `slug('Foo')=='foo'`), the correction is written to a **new** `foo.md` while the original `Foo.md` keeps the forged `grounded`/`failed` state. The same edge id then exists on disk twice in two states, both surfaced by `all_edges()`/the projector. It is self-concealing: lines 136-138 re-stat the unchanged `Foo.md` and store its hash, so the prefilter skips it on every subsequent sweep and the forgery is never re-flagged.

- **Impact:** Defeats the exact reconciler-4 scenario the by-path read was added to support; a forged verdict on a non-canonically-named note is permanently retained and surfaced.
- **Fix:** Persist the correction back to the file actually read: write atomically to `p` directly under the lease when `p.name != node_path(node.id).name`, or `write_one` then unlink the stale `p` and re-stat the new file for `files_state`. (See **H4** for the masking test.)

### H4 — Reconciler re-quarantine test asserts only the report, never the on-disk reset — masks H3 `[bug]`
`tests/test_reconciler.py:84-96`

`test_non_canonical_filename_is_reconciled_not_skipped` writes a forged grounded edge into the non-canonical file `Foo.md`, then asserts only `forged.id in report.requarantined`. It never re-reads `Foo.md` (nor `canon.all_edges()`) to confirm the on-disk forgery was neutralized — and because of H3 it was *not*. The test passes regardless, so it does not test the invariant it claims and directly masks H3.

- **Impact:** A high-severity integrity bug ships with a green test that appears to cover it, giving false confidence.
- **Fix:** After the scan, re-read the actual on-disk note (`Foo.md`, or `canon.all_edges()`) and assert the edge's `epistemic_state` is `UNVERIFIED` there, and that no orphan `foo.md` duplicate was created. This will fail until H3 is fixed.

### H5 — Six optional MCP tool params typed `str=None`/`int=None` reject explicit null `[bug]`
`scripts/kg_engine/server.py:530, 571, 586, 594, 606, 616`

Six FastMCP wrappers annotate optional params as bare `str = None`/`int = None` instead of `X | None = None`: `kg_scrub(text)`, `kg_generate(second_graph)`, `kg_operate(target, k)`, `query_graph(node_type, relation, epistemic_state)`, `get_neighbors(relation)`, `kg_context(query)`. FastMCP builds a schema `{"type":"string","default":null}` (non-nullable type, null default). Omitting the arg works, but a client (including Claude) that serializes an unspecified optional as explicit JSON `null` triggers a pydantic `ValidationError` → `ToolError` → failed tool call. The plugin's own agent/reference docs instruct invoking these with explicit `=None` alongside another supplied arg (e.g. `kg_context(query=None, budget=2000)`), steering the client toward sending an explicit null. The engine-layer methods already use `str | None`; only the thin wrappers are wrong.

- **Impact:** Documented optional defaults become unreachable via explicit-null calls — failing tool invocations the docs actively recommend.
- **Fix:** Change the wrapper annotations to `X | None = None` to match the engine methods.

### H6 — PERSON scrub leaks a real name shadowed by a preceding Title-Case non-name word (`sensitivity='high'`) `[security]`
`scripts/kg_engine/scrub.py:97, 177-183`

The bare-bigram PERSON rule `\b[A-Z][a-z]+\s+[A-Z][a-z]+\b` matches non-overlapping. When a Title-Case word that is **not** a given name precedes a real full name (e.g. `Researcher Alan`, `Professor David`, sentence-initial `Yesterday Michael`), the regex matches the *first* bigram, `_is_personal_name` returns False on the first token, the callback returns it unchanged, and the scan resumes **past** the spared span — so the real name beginning inside it (`Alan Turing`, `David Ricardo`) is never tested. At `sensitivity='high'` the name leaks verbatim out the egress. (`Prof David`/`Dr Alan` are still caught by the separate title rule at line 96; spelled-out role nouns and sentence-initial words are not.)

- **Impact:** Defeats the §1.9 egress guarantee for a wide, realistic class of inputs at the opt-in `high` tier — any full name preceded by a capitalized non-name word. PERSON redaction is heuristic by design, but this is a systematic miss.
- **Fix:** Don't spare the whole bigram on a non-name first token. Iterate matches manually and, when sparing, advance the search position to the start of the **second** token (re-test from there); or widen the regex to allow an optional leading word and gate on whichever Title-Case token is actually a given name.

---

## Medium

### M1 — `absorption()` emits `float('inf')`, which serializes to invalid JSON over MCP `[bug]`
`scripts/kg_engine/harness.py:168, 173`

Returns `half_life=float('inf')` for isolated nodes (168) and zero-growth fertile nodes (173) — both common statuses — and the dict is returned verbatim by the `kg_absorption` tool (server.py:435,438). FastMCP's unstructured path serializes `inf` as the bareword `Infinity` (invalid per RFC 8259), failing strict `JSON.parse` on the client; the structuredContent path silently coerces it to `null`. The codebase already sanitizes non-finite floats for the rate limiter (server.py:514) — this site was missed.

- **Impact:** Any `kg_absorption` response containing an isolated/zero-growth tracked node (the expected majority once a ledger exists) can fail to parse, breaking the whole tool call.
- **Fix:** Emit `half_life = None` at both lines (the `status` field already distinguishes isolated/fertile), or sanitize non-finite floats in `server.kg_absorption` before returning.

### M2 — kg_context hypotheses lane double-counts the token budget; `approx_tokens` under-reports `[bug]`
`scripts/kg_engine/projector.py:627-633, 662`

The grounded `items[]` (627) and `hypotheses[]` (633) lanes are each filled with the **same** full `budget` as an independent per-lane cap, but the returned `approx_tokens` (662) is only the items-lane `used`; the hypotheses-lane `_hused` is discarded. A query matching many hypothesized edges (normal after `/kg-generate`) can serialize up to ~2× `budget` of JSON while reporting `approx_tokens <= budget`. `MAX_CONTEXT_TOKENS` bounds each lane independently, so the real ceiling is ~2×. The existing test asserts only `approx_tokens <= budget` on a hypothesis-free fixture, so it passes.

- **Impact:** Violates the §1.11 contract that `budget` caps approximate tokens; misleads a budget-conscious LLM caller (bounded to ~2×).
- **Fix:** Share a single running budget across both lanes (fill items, then hypotheses with cap `budget - used`) and report `approx_tokens = used + hused`. At minimum, add `_hused` into `approx_tokens`.

### M3 — Reconciler reads UTF-8 audit/state/graph files under the locale default encoding `[bug]`
`scripts/kg_engine/reconciler.py:51, 66, 187`

The audit log is written UTF-8 (server.py:280) but read back with the platform default in `_audit_counts` (66), `_load_state` (51), and `reattach_after_reproject` (187). Notes are parsed with explicit utf-8 (110), so ids decode correctly while audit/state keys do not. For non-ASCII ids (`edge_id('知识','关系','图')=='e_知识__关系__图'`), cp1252 usually mojibakes silently → keys no longer match → `_forged` re-quarantines a legitimately-grounded edge, erasing a real verdict (§1.8 violation). For rare undefined cp1252 bytes, `_audit_counts`'s `except (FileNotFoundError, OSError)` does **not** catch the `UnicodeDecodeError` (a `ValueError`), so `scan()` raises and the per-session reconcile crashes.

- **Impact:** On a non-UTF-8-locale machine (e.g. Windows cp1252) with any non-ASCII ids, the §1.8 forged-verdict defense either re-quarantines legitimate verdicts or goes dark. No test covers it.
- **Fix:** Add `encoding="utf-8"` to every `read_text()`/`json.loads(read_text())` on engine-written files (reconciler.py 51/66/187; also server.py 421/429, canon.py 60/133), and broaden the audit read's except to catch `UnicodeError`.

### M4 — kg_write reports rolled-back nodes/dispositions as written `[logic-error]`
`scripts/kg_engine/server.py:102-113`

`dispositions` and `written_nodes` are built from the boundary's **pre-write** ValidationResults; `canon.write_nodes` can return `RollbackInfo(rolled_back=True)` and restore the snapshot so nothing persists. The payload still returns `written_nodes: list(nodes)` and the ACCEPTED/DEMOTED counts unconditionally, while also setting `rolled_back: True`. The contract tells orchestrators to key off `rolled_back`, but `backend.py:276-279` accumulates dispositions and increments `n_written` without checking it, over-reporting on a rolled-back section.

- **Impact:** The success summary contradicts the rollback flag; at least one in-repo caller (the headless backend) mis-reports what landed. Reporting inconsistency, not canon corruption.
- **Fix:** When `info and info.rolled_back`, return `written_nodes: []` and re-bucket the dispositions into a `rolled_back` bucket; fix `backend.py` to honor `rolled_back`.

### M5 — PHONE scrub rule over-redacts plain whitespace/dash-separated numbers in prose `[edge-case]`
`scripts/kg_engine/scrub.py:93`

The PHONE pattern matches ordinary 6–7 digit runs and dash-separated page ranges (`pages 100-200`, bare `100200`) at default `sensitivity='medium'`, replacing them with `⟦PHONE:n⟧` in the text the extraction subagent sees. It is over-redaction, not a leak: `kg_write` restores the placeholder to the original verbatim number, so the canon span and span verification are intact. Comma-grouped/decimal/year/3-digit forms are spared, bounding the regression.

- **Impact:** Degrades extraction quality on a conceptual document that quotes figures/ranges by hiding meaningful numbers from the subagent.
- **Fix:** Require a phone-ish separator/grouping or leading `+`/country code, or a minimum digit count (~10), so bare 6–7 digit runs aren't captured; or document and accept the trade-off.

### M6 — Release checklist omits two of the four version files the CI hard gate enforces `[bug]`
`CLAUDE.md:205-208`

`validate_plugin.py` (the CI hard gate, 79-85) requires the same version string in **four** files: `plugin.json`, `marketplace.json`, `pyproject.toml` `[project].version`, and `scripts/kg_engine/__init__.py` `__version__`. CLAUDE.md's 'Bump the version' step lists only the two manifests. A maintainer following the docs bumps 2 of 4; the gate then fails on the `pyproject.toml` and `__init__.py` mismatches.

- **Impact:** Documented process contradicts the enforced invariant; every release following the docs trips CI. Fails loudly (cannot silently ship), so impact is a failed run plus friction.
- **Fix:** Update CLAUDE.md step 2 (and the parallel Pre-flight note) to list all four version-bearing files.

---

## Low

### L1 — read_node crashes on invalid-UTF-8 / malformed note, throwing unstructured errors across the MCP boundary `[edge-case]`
`scripts/kg_engine/canon.py:332-334` — `read_text("utf-8")` with no errors handler/try, unlike `all_nodes` and `_check_slug_collision`. Two MCP-reachable callers are unguarded — `kg_ground`'s node branch (server.py:176-178) and `kg_rename` (server.py:289-293) — so a `UnicodeDecodeError`/`ValueError` crosses the MCP boundary unstructured instead of as `{"ok": False, "error": ...}`. (`_owner_of_edge` at server.py:250 is already wrapped, so not vulnerable.) **Fix:** degrade consistently with `all_nodes` (typed `CanonReadError` or `errors='replace'`), or wrap the two exposed callers.

### L2 — Stale module/method docstrings describe removed git-stash rollback behavior `[readability]`
`scripts/kg_engine/canon.py:1-6` — module/`write_nodes` docstrings and the line-365 comment claim 'git-as-rollback (stash-before-reset)', but `_rollback` (419-434) only restores a per-batch byte snapshot, identically on git and non-git vaults (the file's own line-234 comment confirms 'rollback no longer stashes'). **Fix:** rewrite the three sites to describe the actual snapshot-restore mechanism.

### L3 — LeaseLock.release() read-then-act TOCTOU can delete a successor's lock after TTL lapse `[edge-case]`
`scripts/kg_engine/canon.py:164-167` — `release()` reads the record, checks ownership, then unlinks with no re-validation. If our lease lapsed past TTL and another process reclaimed the path in the window, our unlink deletes **their** lock — a single-writer breach. `acquire()` was hardened against exactly this; `release()` was not. **Fix:** mirror acquire's rename-then-verify-then-unlink, or at minimum document the post-TTL hazard.

### L4 — LeaseLock.heartbeat() acquires the lock when the file is absent instead of only refreshing `[edge-case]`
`scripts/kg_engine/canon.py:154-162` — on `_read()==None`, heartbeat blind-writes a fresh self-owned record (not acquire's O_EXCL CAS), violating 'refresh, never acquire'. Currently benign (sole caller holds the lock), but resurrects a released lock and can clobber a concurrent reclaimer's record. **Fix:** `if rec is None or not self._owned_by_self(rec): return`.

### L5 — kg_ground reads the owning node before taking the write lease (cross-process lost-update window) `[edge-case]`
`scripts/kg_engine/server.py:178-230` — the node is read fresh **outside** the lock, mutated in memory, then the lease is acquired for audit+write; `write_one` overwrites the whole node with no merge. Concurrent multi-process grounding against the same node can clobber edits to other edges/body. The `kg_write` batch path reads-under-lock-and-merges; `kg_ground` doesn't. **Fix:** acquire the lease first, then read fresh under the lease.

### L6 — Adversarial/failure parallel edges inflate the degree advisory of attacked hubs `[logic-error]`
`scripts/kg_engine/projector.py:178-198` — `_ranks` computes degree/bridge/betweenness over the full undirected projection including never-pruned `failed`/`rejected` edges, so a more heavily-refuted hub reads as *higher* degree (MultiGraph counts parallels) and is more likely surfaced (`query_graph`/`kg_context` ORDER BY degree DESC). Bounded by the per-run counter-edge cap; advisory-only. **Fix:** derive the advisory ranks from the non-failed subgraph while keeping `graph.json`/edges complete.

### L7 — _load_state crashes on valid-but-non-dict (or null sub-key) state file, silently disabling re-quarantine `[edge-case]`
`scripts/kg_engine/reconciler.py:49-53, 83-86` — catches only `FileNotFoundError/ValueError/OSError`; a valid-JSON-but-non-object state (`[1,2,3]`, `null`) or a `{"files": null}` sub-key crashes `scan()` (`AttributeError`). The crash is swallowed by `bootstrap.maybe_reconcile` and never self-heals (scan dies before `_save_state`), so the §1.8 sweep goes dark fail-open. **Fix:** `if not isinstance(state, dict): raise ValueError` inside the try, and coerce sub-keys with `state.get(k) or {}` + isinstance checks.

### L8 — Missing ANTHROPIC_API_KEY surfaces as N identical per-section auth failures `[edge-case]`
`scripts/kg_engine/backend.py:99, 265-292` — `anthropic.Anthropic()` resolves the key lazily, so construction never raises; the `AuthenticationError` fires per-section and is caught by `run()`'s `except Exception`, yielding N wasted 401s and N near-identical entries plus exit 1, instead of one actionable message. **Fix:** assert the key once in `_ensure_client` and `raise SystemExit(...)` (BaseException, propagates past the per-section handler), mirroring the missing-SDK branch.

### L9 — Default 16000 max_tokens raises ValueError if --model is overridden to a registered Opus 4/4.1 id (8192 cap) `[edge-case]`
`scripts/kg_engine/backend.py:42, 181-188, 313-320` — DEFAULT_MODEL `claude-opus-4-8` is not in the SDK's `MODEL_NONSTREAMING_TOKENS` table (safe), but overriding `--model`/`KG_BACKEND_MODEL` to an 8192-capped id while leaving 16000 max_tokens raises `ValueError` on the first `create()` of every section. **Fix:** clamp max_tokens against the table for the resolved model, pass `stream=True`, or document the constraint.

### L10 — Install stamp omits Python version/platform, so an ABI-broken venv is silently reused after an in-place interpreter upgrade `[edge-case]`
`scripts/bootstrap.py:126-152` — `compute_stamp()` hashes only SCHEMA + `pyproject.toml`, and `is_ready()` never re-runs `verify_imports`. An in-place same-path interpreter minor upgrade (unversioned stdlib-fallback venv symlink, or a pyenv re-point) leaves the stamp matching while compiled wheels (pydantic-core, igraph, leidenalg) become ABI-mismatched → crash on import. uv-built venvs (versioned symlink) mostly avoid this; recoverable next session via launch_server.mjs self-heal. **Fix:** fold `sys.version_info[:2]` and `sys.platform`/`platform.machine()` into the stamp.

### L11 — Default --wait (1200s) shorter than STALE_LOCK_SECS (1800s) `[edge-case]`
`scripts/bootstrap.py:442-456` — a HARD-killed provisioner (SIGKILL/power loss) bypasses `finally:release()`, so the lock isn't stealable until 1800s, but `provision()`'s default 1200s deadline (used by launch_server.mjs, which passes no `--wait`) fires first and returns 0 without building, dropping all `kg_*` tools for that session until a later session crosses 1800s. **Fix:** set `--wait` default ≥ STALE_LOCK_SECS (or lower the steal threshold).

### L12 — Lock-steal defeated by a leftover sidelined dir on PID reuse `[edge-case]`
`scripts/bootstrap.py:212-221` — a crash between `os.replace()` and `shutil.rmtree()` orphans a non-empty `.kg-provision.lock.stale-<pid>` permanently; a later stealer reusing that PID hits ENOTEMPTY on `os.replace`, masked as a lost steal race, so that process never reclaims. Bounded (other-PID stealers succeed; loop self-heals). **Fix:** collision-proof sideline name (`time.time_ns()`/`tempfile.mkdtemp`), rmtree any pre-existing sidelined first, and/or sweep `*.stale-*` on acquire.

### L13 — Node and shell launchers have no behavioral test coverage `[testability]`
`scripts/launch_server.mjs:1-184` (and `hooks/{provision.*,precontext.*}`) — `test_bootstrap.py` covers only the Python internals; `validate_plugin.py` checks file presence only. The §2.1 'never cache as needs-auth' invariant, JSON-RPC stdout isolation (`:103` child stdout→fd 2), the one-shot early-failure relaunch, and a hand-mirrored copy of `resolve_venv_dir` precedence live entirely in untested glue. No current defect. **Fix:** add smoke tests for venv-dir agreement with the Python source, the one-shot relaunch, and a no-index precontext returning 0 without constructing Canon.

### L14 — GitPython declared and import-verified as a core dependency but never used `[outdated-dependency]`
`pyproject.toml:16` — `GitPython>=3.1` is a hard dep and `_VERIFY_IMPORTS` imports `git` in the readiness gate, but the engine only shells out via `subprocess.run(["git", ...])`. So provisioning hard-fails if the heaviest pure-Python dep is missing, for a package with no callers. **Fix:** remove GitPython from dependencies and drop `git` from `_VERIFY_IMPORTS` (verify the git binary on PATH instead); fix the stale comment.

### L15 — ARCHITECTURE.md module API list omits 4 of the 15 MCP tools `[deprecation]`
`ARCHITECTURE.md:117-119` — the binding contract's `server` API entry lists the original 11 tools, omitting the generative-layer `kg_propose`, `kg_generate`, `kg_absorption`, `kg_operate` (server.py registers 15; CLAUDE.md says '15 tools'). Pure doc drift. **Fix:** add the four tools to the entry.

---

## Nit

### N1 — _check_slug_collision swallows all read errors and lets the write overwrite the colliding note `[edge-case]`
`scripts/kg_engine/canon.py:303-310` — an unreadable existing note at the target path is treated as 'no collision' and overwritten; with `fallback_id=node.id`, an id-less/unreadable *foreign* note that slugs to the same filename evades detection. Only bites for externally-corrupted foreign files; the self-file-corrupt common case is correctly self-healed. **Fix:** fail closed (or back up bytes) on an unreadable target; avoid `fallback_id=node.id` when reading purely for collision detection.

### N2 — scan() re-reads the entire canon a second time per sweep for the prune pass `[performance]`
`scripts/kg_engine/reconciler.py:150-151` — the baseline-prune block calls `all_nodes()` (full read+parse) plus a second `note_paths()` glob on every scan, after the main loop already parsed every changed file. Bounded (once-per-session detached worker). **Fix:** build `live_files` from the main loop's `note_paths()` and gather ids from already-parsed nodes.

### N3 — transplant recomputes G.to_undirected() per member and a loop-invariant absorption() per candidate `[performance]`
`scripts/kg_engine/generate.py:304, 321` — `absorption()` calls `G.to_undirected()` (O(V+E)) once per member inside a set comprehension, and `absorption(best_members)` is recomputed per candidate, giving ~m² full-graph copies (reproduced: ~1.1s/880 copies on an 80-node graph vs ~10–30ms for the other generators). **Fix:** hoist `und = G.to_undirected()` once into `absorption`; compute `best_absorption` once before the candidate loop.

### N4 — MCP kg_operate wrapper omits the `members` argument the engine supports `[bug]`
`scripts/kg_engine/server.py:586-591` — the wrapper declares only `(op, target, label, body, k)`, so the explicit-member collapse branch (`operations._resolve_cluster`'s highest-priority source) is unreachable through the only external entry point (confirmed: no caller anywhere supplies `members`). Collapse still works via fallbacks. **Fix:** add `members: list[str] | None = None` and forward it, or remove the unreachable branch / document the omission.

### N5 — explode_payload ignores k=0 and mis-slices on negative k `[edge-case]`
`scripts/kg_engine/operations.py:77-78` — `if k:` treats `k=0` as 'no limit' and a negative `k` drops facets from the end; `k` is unvalidated LLM input. Output is hypothesized/unverified (filtered downstream). **Fix:** `if k is not None: facets = facets[: max(0, int(k))]`, matching `open_payload`.

### N6 — slug() collapses distinct punctuation-only inputs; docstring overstates the guarantee `[edge-case]`
`scripts/kg_engine/model.py:116-125` — `slug('a/b')==slug('a-b')==slug('a b')`, `slug('!!!foo!!!')==slug('foo')`; the 'preserves distinctness' claim is true only vs the DELETE alternative. Limited impact (canon.py:298 refuses two distinct stored ids on one filename; node ids are slug-form by contract). **Fix:** soften the docstring to the real (weaker) guarantee, or fold a hash of the NFC original into the id if true distinctness is required.

### N7 — source_text() re-reads the source file from disk on every write/ground call `[performance]`
`scripts/kg_engine/server.py:67-70` — uncached `read_text()` called per `kg_write` (97) and per hypothesis-promoting `kg_ground` (194); the source is immutable for the session. Negligible (small, OS-page-cached; dominated by canon I/O + LLM). **Fix:** memoize keyed on `source_path` mtime, or read once in `__init__`.

### N8 — --run flag documented as the detached-worker entrypoint but never read `[readability]`
`scripts/bootstrap.py:528-532` — `args.run` is never inspected; the worker and manual paths are identical (per the line-568 comment). The help text misleadingly implies it gates worker-only behavior. **Fix:** remove `--run` from the parser and spawn argv, or actually branch on it.

### N9 — PYTHONPATH dedup check fails on native Windows (backslash vs forward-slash) `[bug]`
`scripts/launch_server.mjs:142-143` (and `hooks/precontext.mjs:66-67`) — `path.win32.join` is all-backslash whereas `.mcp.json` injects `/scripts`, so `parts.includes(SCRIPTS)` is false and a redundant `scripts` entry is prepended on every Windows launch. Harmless (both resolve; Python imports fine). **Fix:** normalize both sides to a canonical separator before comparing, in both files.

### N10 — precontext.py constructs a writable Canon() on every read hook, touching .git/info/exclude `[performance]`
`hooks/precontext.py:30-35` — once an index exists, every Grep/Glob/Read constructs `Canon(project)`, whose `__init__` mkdir's the canon dir and runs `_ensure_git_excludes()` (re-reading `.git/info/exclude` each call). The no-side-effect claim holds only in the cold case; a deleted canon dir is silently recreated. Negligible vs the projector work the hook does anyway. **Fix:** a read-only Projector path that doesn't instantiate a writable Canon.

### N11 — precontext.py reads JSON stdin under the locale default encoding `[edge-case]`
`hooks/precontext.py:20` — `json.load(sys.stdin)` decodes under the locale encoding; on Windows cp1252 a non-ASCII payload mojibakes (wrong/empty match) or raises `UnicodeDecodeError`, swallowed to return 0, silently disabling precontext for unicode payloads. **Fix:** `json.loads(sys.stdin.buffer.read().decode('utf-8'))` inside the try.

### N12 — Wrong exception type caught for single_source_shortest_path_length (defensive dead code) `[bug]`
`scripts/kg_engine/generate.py:170-173` — `except nx.NetworkXError`, but a missing source raises `nx.NodeNotFound` (not a subclass). Unreachable today (the node provably exists). **Fix:** `except (nx.NetworkXError, nx.NodeNotFound):`, or drop the try/except.

### N13 — Derived edges table omits verdict_by/verdict_at that the canon persists `[readability]`
`scripts/kg_engine/projector.py:290-293, 333-335` — the derived edges DDL/`_edge_row`/`_build_graph` drop both fields. Contractually allowed (the constraint is one-directional) and no current consumer reads them off the derived layer, but verdict attribution is invisible to every derived read tool. **Fix:** add the columns if it should be queryable, else add a one-line comment noting the intentional omission.

### N14 — ARCHITECTURE.md states deterministic-authorship demotion is universal, but the hypothesized lane preserves it `[logic-error]`
`ARCHITECTURE.md:56-57, 71-74` — the binding contract states the `deterministic→agent` demotion unconditionally, but `boundary.py` (171-177, 270-275) preserves `deterministic` on the hypothesized lane (only `human` is demoted) since there's no span check to bypass; pinned by `test_hypothesized.py`. The doc predates the generative layer. **Fix:** add a sentence clarifying the hypothesized-lane behavior.

### N15 — README documents metrics_mode as a two-value enum, but userConfig has no enum support and 'with_embeddings' is inert `[readability]`
`README.md:149` — `metrics_mode` is presented as `structure_only | with_embeddings`, but userConfig can't enforce an enum and the engine never branches on it (stored and echoed by `kg_ping` only), so `with_embeddings` is behaviorally identical to a typo. **Fix:** remove `with_embeddings` from the documented choices until/unless the embedding path is reimplemented.

---

## Deprecations & dependencies

- **GitPython is a dead core dependency (L14).** Declared, import-verified in the readiness gate, but the engine only uses the `git` CLI via `subprocess`. Removing it (and dropping `git` from `_VERIFY_IMPORTS`) shrinks the install/failure surface with no functional change.
- **`metrics_mode='with_embeddings'` is dead/inert (N15).** The sqlite-vss candidate generator was removed; the value is a no-op that the README still advertises as a selectable mode.
- **Stale tool-count documentation (L15, N13-context, plus the README `'11th tool'` and CHANGELOG header nits).** ARCHITECTURE.md (the *binding* contract) and README still describe the pre-generative 11-tool surface; the real surface is 15. CHANGELOG's preamble still claims version `0.2.1` (actual: `0.3.0`) and names only 2 of the 4 gated version files — see also M6.

*(The CHANGELOG and README staleness items above are tracked as low-priority documentation nits in the main listing; grouped here for the maintainer's release-hygiene pass.)*