---
description: Stage 8/9 — blind ideation experiment (control|graph|graph+generate|rag); score with the harness and append the verdict to PROGRESS.md
argument-hint: "[prompts_path]"
allowed-tools: Task, Bash, mcp__plugin_creativity-graph_creativity-graph__kg_context
---

# /kg-experiment — does the graph actually help ideation? (§Stage 8, §4)

This command runs the one experiment that justifies the whole pipeline: a **blind A/B/C/D** over a fixed
set of ideation prompts, in four conditions that differ ONLY in what context the idea-generator is given.

- **control** — the prompt alone, no source, no graph.
- **graph** — the prompt + a `kg_context` pack drawn from the **grounded** knowledge graph (`items[]` only).
- **graph+generate** — the prompt + the graph pack **plus the hypothesized slate** (`kg_context.hypotheses[]`
  / a `/kg-generate` run): the generative layer's proposals, clearly flagged as unverified candidates.
  Tests whether *generating* lifts ideation beyond grounded context alone (PLAN Stage 9).
- **rag** — the prompt + a naive flat-text retrieval slice of the source document (the strawman to beat).
- **lightrag** *(optional, off by default)* — the prompt answered from a real, published **GraphRAG**
  baseline (LightRAG) built over the **same** `examples/source.md` corpus. A stronger strawman than flat
  `rag`: it lets the central claim stand against a genuine graph-retrieval system, not only against grep.
  This arm is **add-only and opt-in** — see "The optional lightrag arm" below. When it is not enabled the
  experiment runs its original four arms, unchanged.

The generator is BLIND to which condition it is in (it receives only an opaque context block), so the
scoring is not contaminated by the model "trying harder" for one arm. Scoring is **deterministic** —
`python -m kg_engine.harness ideation` computes diversity / novelty / utility / unsupported_rate per
condition and a single verdict. Per §4 **no metric gates execution**: this command logs the table + verdict
to `PROGRESS.md` and returns, regardless of whether graph wins.

The headline question the verdict answers:
> **Does the `graph` condition beat `control` on diversity + novelty WITHOUT raising the unsupported-claim rate?**
> (the harness allows `graph.unsupported_rate <= control.unsupported_rate + 0.05` slack — beating control by
> hallucinating more is not a win.)

`$ARGUMENTS` (optional `$1`): a path to a JSON or newline-delimited file of custom ideation prompts. If
omitted, the 12 default prompts below are used.

---

## Step 0 — environment

Resolve the Python runner once and reuse it (dev vs. runtime, per the contract):

```bash
# dev (this repo):
PY=/home/sergi/creativity-graph/.venv/bin/python
SCRIPTS=/home/sergi/creativity-graph/scripts
# runtime (installed plugin), if the above venv is absent:
#   PY="${CLAUDE_PLUGIN_DATA}/.venv/bin/python"
#   SCRIPTS="${CLAUDE_PLUGIN_ROOT}/scripts"
PYTHONPATH="$SCRIPTS" "$PY" -m kg_engine.harness ideation 2>&1 | head -1   # smoke-test: harness importable
```

Confirm the graph is queryable before spending tokens generating ideas:

- Call **`mcp__plugin_creativity-graph_creativity-graph__kg_context`** with `budget=2000` and no `query`. If it returns an empty
  `items[]`, there is no grounded graph to test — tell the user to run `/kg-build` then `/kg-ground` first,
  and stop. Note the `falsification_counters.failed_or_rejected_edges`; a graph with **zero** recorded
  failures (§1.7) is suspect and the `graph` arm will look artificially clean — surface that caveat in the
  final report.

### The optional `lightrag` arm (opt-in, off by default)

A fifth arm, `lightrag`, compares the graph against a **real published GraphRAG baseline** instead of only
the flat-grep `rag` strawman. It is **off by default** and entirely add-only: when it is not enabled, this
command runs exactly the original four arms and nothing changes.

To enable it, all three must hold (the evaluator probes this with
`python -m kg_engine.lightrag_arm check`, which prints `{"available": …, "reason": …}`):

1. **Install** the optional dependency: `pip install lightrag-hku` (or `pip install -e ".[lightrag]"` from
   the repo). It is **not** part of the default install.
2. **Opt in** with the env var `KG_LIGHTRAG=1` (the arm stays off even if the package is installed unless you
   set this). Optional tuning: `KG_LIGHTRAG_QUERY_MODE` (default `mix`).
3. **Provide credentials**: `OPENAI_API_KEY` — LightRAG's default LLM + embedding backend is OpenAI, so this
   arm makes **network + paid API calls**. That cost is the price of the stronger baseline; it is why the arm
   is opt-in, not automatic.

When enabled, the evaluator builds a LightRAG index over the **same** `examples/source.md` corpus the `rag`
arm uses (so the comparison is fair), under a **gitignored, disposable** working store in the derived dir, and
answers each prompt through LightRAG's own retrieval — it never reads our graph's structure. When **not**
enabled, the `lightrag` arm is omitted and the harness simply scores the four arms present. Either way the run
proceeds; the arm never blocks the experiment.

---

## Step 1 — prompt set (non-blocking checkpoint)

Ask the user exactly once, then proceed with the default if they don't redirect:

> **Use the 12 default prompts, or supply your own? [default/custom]** (default: `default`)

- `default` (or no answer / `$1` empty) → use the **12 default ideation prompts** below.
- `custom` → read prompts from `$1` (one prompt per line, or a JSON array). If `$1` is missing while the
  user said `custom`, ask for the path; otherwise fall back to default rather than blocking.

This is a **non-blocking checkpoint** (§4): a reply is invited but not required to make progress.

**The 12 default prompts** (grounded in the demo corpus — compression, span/provenance, bridges/betweenness,
memory-of-failures, canon/projection; see `examples/source.md`):

1. Propose a new failure mode, distinct from the generality confound, by which a vague node could earn spurious centrality.
2. Suggest a concept that bridges "compression" and "memory of failures" and say what edge type would connect them.
3. What specific claim, if it held, would *attack_by* the idea that span-present provenance always grounds better than inference?
4. Invent a metric other than betweenness or degree that a genuine bridge would score high on and a vague hub would not.
5. Describe an operation that turns a rejected/failed edge back into negative information the graph can defend against.
6. Where might "specificity-weighted betweenness" *reconcile_with* raw degree rather than replace it?
7. Name a case where the derived projection *could* legitimately contain something the canon does not — or argue it never can.
8. Propose a primitive that "compression" *collapses_into* under extreme generality, and the test that detects the collapse.
9. What would a claim that *defends_against* re-proposing a known failure look like, concretely?
10. Suggest how a verdict could *survive* reprojection if the reconciler were removed — or prove it cannot.
11. Identify two communities of ideas in the source and the single node most likely to *bridge* them.
12. Propose a confound, parallel to the generality confound, that afflicts *novelty* scoring rather than centrality.

---

## Step 2 — generate outputs in four blind conditions (Task → kg-evaluator)

Launch the **kg-evaluator** subagent via the **Task** tool. It owns blinding, context assembly, and idea
generation; this command never sees which arm is which until the JSON comes back keyed by condition.

`Task(subagent_type: "kg-evaluator", description: "blind ideation A/B/C/D", prompt: …)` — instruct it to:

1. For **each** of the N prompts, build four context blocks:
   - **control** → no context.
   - **graph** → the result of `mcp__plugin_creativity-graph_creativity-graph__kg_context(query=<prompt>, budget=2000)`, rendered as
     opaque text — the **grounded** `items[]` only. Carry through the pack's `advisory` (e.g.
     `signal:"structural-bridge"`, `bridge_metric`) and the `falsification_counters` so the generator can
     *avoid* re-proposing failed edges (§1.7) — but it MUST NOT fabricate verdicts or spans; this arm only
     *reads* the graph.
   - **graph+generate** → the graph block PLUS the same call's `hypotheses[]` (or a `/kg-generate` slate),
     rendered as opaque text and clearly marked *unverified candidates*. This arm reads grounded context
     AND the generative proposals; it still must not fabricate verdicts or spans.
   - **rag** → a naive flat slice of `examples/source.md`: the top text chunks by keyword overlap with the
     prompt, no graph structure. This is the honest strawman.
2. Present these four blocks to the generator **without labels** (shuffle; refer to them only as context A/B/C/D),
   generate one idea per (prompt × condition), then **de-shuffle** when emitting JSON.
   - **lightrag (optional)** → NOT one of the blinded context blocks — its answers come from the external
     LightRAG system, not from the evaluator's own generation. Run it separately via the isolated helper
     (`python -m kg_engine.lightrag_arm check`, then `… answer --source examples/source.md --prompts … --out …`)
     and collect its `answers[]` into `outputs.lightrag`, in the same prompt order. **Include the key only when
     the arm is available** (`KG_LIGHTRAG=1` + `lightrag-hku` + `OPENAI_API_KEY`); otherwise omit it. It reads
     the same prose corpus as `rag`, through LightRAG's retrieval — never our graph's structure.
3. Emit a single JSON object in EXACTLY the shape `harness ideation` consumes — write it to
   `${CLAUDE_PLUGIN_DATA:-/tmp}/derived/ideation_outputs.json`:

```json
{
  "outputs": {
    "control":        ["…one string per prompt…"],
    "graph":          ["…"],
    "graph+generate": ["…"],
    "rag":            ["…"],
    "lightrag":       ["… (OPTIONAL — present only when the lightrag arm was enabled+available) …"]
  },
  "source": "<the full text of examples/source.md>"
}
```

`harness ideation` scores every condition key it finds. When `graph+generate` is present it emits a second
**`generate_verdict`** alongside the headline graph-vs-control `verdict`: whether *generating* (graph context
+ the hypothesized slate) lifted diversity/novelty beyond control without materially more unsupported
claims, and whether it exceeded `graph` alone. When the optional `lightrag` arm is present it emits a
**`lightrag_verdict`** (graph-vs-LightRAG): whether the grounded `graph` arm beat the published GraphRAG
baseline. All of these are absent when their arm is — a missing optional arm is never an error.

Constraints to put in the Task prompt: each **present** `outputs` list has the **same length** (= number of
prompts); one output string per prompt per condition; `source` is the verbatim source text so the harness can
compute `novelty` (n-gram overlap with source) and `unsupported_rate` (sentences whose key terms never appear
in source). The evaluator returns the **file path** it wrote.

---

## Step 3 — score deterministically (Bash → harness)

```bash
OUT="${CLAUDE_PLUGIN_DATA:-/tmp}/derived/ideation_outputs.json"
PYTHONPATH="$SCRIPTS" "$PY" -m kg_engine.harness ideation "$OUT"
```

`harness ideation` prints a JSON object:

```json
{
  "table": {
    "control":        {"n": 12, "diversity": 0.71, "novelty": 0.34, "utility": 0.20, "unsupported_rate": 0.41},
    "graph":          {"n": 12, "diversity": 0.83, "novelty": 0.52, "utility": 0.60, "unsupported_rate": 0.38},
    "graph+generate": {"n": 12, "diversity": 0.88, "novelty": 0.61, "utility": 0.64, "unsupported_rate": 0.40},
    "rag":            {"n": 12, "diversity": 0.69, "novelty": 0.40, "utility": 0.30, "unsupported_rate": 0.55}
  },
  "verdict": "graph condition produced more diverse/novel ideas without more unsupported claims",
  "generate_verdict": "graph+generate beat control on diversity/novelty without more unsupported claims"
}
```

What each column means (all in `_score_condition`):
- **diversity** — distinct trigrams / total trigrams pooled across that arm's outputs (vocabulary spread).
- **novelty** — `1 − (trigram overlap with source)`, averaged; how far ideas travel from the source text.
- **utility** — density of reasoning/connective markers (`because`, `if`, `therefore`, `bridge`, `connect`),
  capped at 1.0; a rough proxy for "actually does inferential work."
- **unsupported_rate** — fraction of ≥4-word sentences whose key terms never appear in the source; the
  hallucination guard. **Higher is worse.**

The headline **verdict** strings (from `harness.ideation`, graph-vs-control) are:
- `"graph condition produced more diverse/novel ideas without more unsupported claims"` → graph wins.
- `"graph condition did NOT clearly beat control"` → it did not. (Or `"insufficient data"` if a list is empty.)

When the `graph+generate` arm is present, a second **`generate_verdict`** (graph+generate-vs-control) is one of:
- `"graph+generate beat control on diversity/novelty without more unsupported claims"` → generating helped.
- `"graph+generate did NOT clearly beat control"` → it did not.

When the optional `lightrag` arm is present, a **`lightrag_verdict`** (graph-vs-LightRAG) is one of:
- `"graph condition beat the LightRAG GraphRAG baseline on diversity/novelty without more unsupported claims"`
  → grounding beat the published GraphRAG, not just flat grep.
- `"graph condition did NOT clearly beat the LightRAG GraphRAG baseline"` → it did not.

(Both the `lightrag` table row and `lightrag_verdict` are present **only** when the optional arm was run; when
it was not enabled the harness emits the original four-arm table with no `lightrag_verdict` and no error.)

---

## Step 4 — append to PROGRESS.md (then return, win or lose — §4)

Render the harness JSON as a Markdown table and append it, with the verdict and a UTC timestamp, to
`${CLAUDE_PROJECT_DIR}/PROGRESS.md` (create the file with an `# Experiment log` header if absent). Use a
heredoc so the run is reproducible from the log:

```bash
PROGRESS="${CLAUDE_PROJECT_DIR:-.}/PROGRESS.md"
[ -f "$PROGRESS" ] || printf '# Experiment log\n\n' > "$PROGRESS"
{
  printf '\n## kg-experiment (Stage 8) — %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'Prompts: %s (%s)\n\n' "$N_PROMPTS" "$PROMPT_SOURCE"   # e.g. "12 (default)" or "8 (custom: $1)"
  printf '| condition | n | diversity | novelty | utility | unsupported_rate |\n'
  printf '|-----------|---|-----------|---------|---------|------------------|\n'
  # one row per condition, values pulled from the harness JSON above
  printf '| control        | %s | %s | %s | %s | %s |\n' "$c_n"  "$c_div"  "$c_nov"  "$c_util"  "$c_unsup"
  printf '| graph          | %s | %s | %s | %s | %s |\n' "$g_n"  "$g_div"  "$g_nov"  "$g_util"  "$g_unsup"
  printf '| graph+generate | %s | %s | %s | %s | %s |\n' "$gg_n" "$gg_div" "$gg_nov" "$gg_util" "$gg_unsup"
  printf '| rag            | %s | %s | %s | %s | %s |\n' "$r_n"  "$r_div"  "$r_nov"  "$r_util"  "$r_unsup"
  # OPTIONAL row — emit only if the lightrag arm was present in the harness table (else skip it):
  [ -n "$lr_n" ] && printf '| lightrag       | %s | %s | %s | %s | %s |\n' "$lr_n" "$lr_div" "$lr_nov" "$lr_util" "$lr_unsup"
  printf '\n**Verdict:** %s\n' "$VERDICT"
  printf '\n**Generate verdict:** %s\n' "$GEN_VERDICT"
  # OPTIONAL — only when the harness emitted a lightrag_verdict:
  [ -n "$LIGHTRAG_VERDICT" ] && printf '\n**LightRAG verdict:** %s\n' "$LIGHTRAG_VERDICT"
} >> "$PROGRESS"
```

Then print a one-paragraph summary to the user:

- The verdict, verbatim, plus the head-to-head: `graph` vs `control` on diversity / novelty / unsupported_rate.
- Whether `graph` also beat the `rag` strawman (the honest comparison — beating control is the bar, beating
  rag is the bonus).
- If the optional `lightrag` arm ran: the `lightrag_verdict` — whether `graph` beat the **published GraphRAG
  baseline**, the strongest comparison available (beating LightRAG, not just flat `rag`, is what shows
  grounding-with-falsification earns its keep against real graph retrieval).
- The **caveat from Step 0** if `falsification_counters.failed_or_rejected_edges == 0`: a graph with no
  recorded failures (§1.7) makes `graph` look cleaner than it has earned; recommend running `/kg-ground` with
  the adversarial grounder before trusting a `graph`-wins verdict.
- Path to the appended log: `${CLAUDE_PROJECT_DIR}/PROGRESS.md`.

**Execution proceeds regardless of the verdict (§4).** A `graph did NOT clearly beat control` result is a
legitimate, logged outcome — it is *negative information* about the pipeline itself, not an error. Do not
retry, re-shuffle to fish for a win, or hide the row. Report it and stop.
