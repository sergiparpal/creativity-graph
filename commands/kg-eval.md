---
description: Measure extractor precision (Stage 4) and grounding reliability (Stage 7); record the numbers in PROGRESS.md.
argument-hint: "[graph.json] (default: ${KG_DATA:-${CLAUDE_PROJECT_DIR:-.}/.kg-data}/derived/graph.json)"
allowed-tools: Task, Bash, mcp__creativity-graph__query_graph
---

# /kg-eval — extraction & grounding measurement

Run the two measurement stages over the current canon's derived graph and append the results to
`PROGRESS.md`. This command **measures**; it does not gate. No metric blocks the flow: when a stage falls
short of its threshold it **auto-iterates** (refine the pack + extractor prompt, re-extract, re-measure —
up to 3 passes) then records the **best** result and proceeds (§4, "logged and proceeds, no human gate").

- **Stage 4 — extraction precision.** Sample the derived edges, have the annotator label each against the
  source, score with `f4_probe.py`. Reports PRECISION (target `>= 0.70`), astrology rate (fabricated+vague),
  span-support rate, and per-relation precision.
- **Stage 7 — annotation agreement + specificity.** Two **independent** annotator passes over the same
  sample → Krippendorff's α (`harness agreement`, reliable `>= 0.67`); then the specificity bridge-metric
  gate verdict (`harness specificity`).

`GRAPH = $1` if given, else `${KG_DATA:-${CLAUDE_PROJECT_DIR:-.}/.kg-data}/derived/graph.json`. If it does
not exist, tell the user to run `/kg-build` first (the projector writes `<data_dir>/derived/graph.json`,
where `data_dir = ${KG_DATA:-${CLAUDE_PROJECT_DIR:-.}/.kg-data}`; runtime: under `${CLAUDE_PLUGIN_DATA}/derived`)
and stop.

## Choosing the Python interpreter

Pick **one** `PY` and a working directory, then reuse it for every Bash step below.

- **Dev (in this repo):**
  `PY=/home/sergi/creativity-graph/.venv/bin/python` (or prefix commands with `uv run`), run from
  `/home/sergi/creativity-graph`. `kg_engine` is importable as `scripts/kg_engine`, so run the harness with
  `PYTHONPATH=/home/sergi/creativity-graph/scripts`.
- **Runtime (installed plugin):**
  `PY=${CLAUDE_PLUGIN_DATA}/.venv/bin/python` with `PYTHONPATH=${CLAUDE_PLUGIN_ROOT}/scripts`; the probe is
  `${CLAUDE_PLUGIN_ROOT}/scripts/f4_probe.py`. Keep all scratch files (`labels.csv`, `label_sets.json`)
  under `${CLAUDE_PLUGIN_DATA}/derived/eval/`.

The blocks below use the dev paths; swap in the runtime paths verbatim when installed.

---

## Stage 4 — extraction precision

### 4.1 Inspect the graph (sanity, no labeling yet)

```bash
PY=/home/sergi/creativity-graph/.venv/bin/python
GRAPH="${1:-${KG_DATA:-${CLAUDE_PROJECT_DIR:-.}/.kg-data}/derived/graph.json}"
"$PY" /home/sergi/creativity-graph/scripts/f4_probe.py summary "$GRAPH"
```

This prints node/edge counts, the nodes grouped by `file_type` (distinct from the `node_type` used in the
§7.3 cross-check), the confidence mix (EXTRACTED | INFERRED | AMBIGUOUS), the top relations, and the count
of *judged* edges (INFERRED+AMBIGUOUS) — that judged slice is the precision-relevant part. The deterministic
EXTRACTED tier is span-present by construction, so the sheet excludes it by default.

### 4.2 Emit the labeling sheet

```bash
"$PY" /home/sergi/creativity-graph/scripts/f4_probe.py sheet "$GRAPH" --n 80 --out labels.csv
```

`sheet` deterministically samples (seed 42) up to 80 non-EXTRACTED edges into `labels.csv` with columns
`edge_id, source_label, target_label, relation, confidence, confidence_score, source_file, verdict,
span_found, notes`. The `verdict` and `span_found` columns are empty — the annotator fills them.

### 4.3 Label the sheet (annotator subagent)

Launch the annotator via the **Task** tool to fill `labels.csv` against the real source. The only legal
labels are the f4_probe vocabulary — do not invent others.

```
Task(
  subagent_type: "kg-annotator",
  description: "Label Stage-4 precision sheet",
  prompt: """
    Read the labeling sheet at ABS_PATH/labels.csv and the source document at
    /home/sergi/creativity-graph/examples/source.md  (runtime: ${CLAUDE_PROJECT_DIR}/canon's source).

    For EACH row, judge the (source_label, relation, target_label) claim STRICTLY against the source text
    and write two columns IN PLACE, leaving every other column untouched:

      verdict ∈ {correct | fabricated | vague | wrong_type}
        correct    — the relation is true AND specific to the source
        fabricated — not supported by the source at all (hallucinated)
        vague      — "true" only because it is generic/unfalsifiable (the generality confound, §1.6):
                     reject anything that holds for almost any pair of nodes
        wrong_type — endpoints are related but the relation label is wrong
                     (e.g. labeled `grounds` where the source only says `attacked_by`)
      span_found ∈ {y | n}
        y — there is a verbatim textual span in the source supporting the claim (the span-present check)
        n — no such span exists

    Do not paraphrase to make a span "found". A span must be an actual substring of the source.
    Write the completed CSV back to ABS_PATH/labels.csv with the same header and row order.
    Report: how many rows you labeled and the verdict counts.
  """
)
```

`ABS_PATH` is the directory holding `labels.csv` (the repo root in dev, `${CLAUDE_PLUGIN_DATA}/derived/eval`
at runtime). Annotators get `Read, Grep, Bash` — they edit the CSV directly; they have **no** graph-write
tools, so they cannot forge a verdict into the canon (verdicts reach the canon only via `kg_ground`, §1.4).

### 4.4 Score

```bash
"$PY" /home/sergi/creativity-graph/scripts/f4_probe.py score labels.csv
```

Capture from stdout:

- **PRECISION** = correct / labeled — the Stage-4 target is `>= 0.70`.
- **astrology rate** = (fabricated + vague) / labeled — the grounding risk, measured.
- **span-support rate** = span_found=y / labeled — the §1.5 span-present check.
- **per-relation precision** (rows with n>=3) — which `pack.yaml` relations the extractor confuses
  (e.g. `grounds` vs `attacked_by` vs `confounded_by`).
- the **confidence-calibration** line, if a numeric `confidence_score` is present (does it separate correct
  from wrong, or is it vocabulary, not grounding?).

### 4.5 Auto-iterate (up to 3 passes) — no human gate

If **PRECISION < 0.70**, iterate; otherwise skip to recording. Each pass:

1. Read the per-relation precision and verdict breakdown. The two failure shapes drive two fixes:
   - many `vague` → the **generality confound**: tighten `node_types` / sharpen the extractor prompt to
     reject unfalsifiable edges, and add `specificity_seeds` for the confused vague terms in
     `/home/sergi/creativity-graph/pack/pack.yaml` so they are not mistaken for content.
   - many `wrong_type` / `fabricated` → tighten the relation definitions in `pack.yaml` and the
     extractor's worked example; the boundary already REJECTS span-not-in-source, so persistent
     `fabricated` here means the prompt is over-reaching.
2. Validate the pack before re-extracting:
   ```bash
   "$PY" -m kg_engine.pack validate /home/sergi/creativity-graph/pack/pack.yaml \
       /home/sergi/creativity-graph/examples/source.md
   ```
   (Runtime: `PYTHONPATH=${CLAUDE_PLUGIN_ROOT}/scripts "$PY" -m kg_engine.pack validate ...`.)
3. Re-run `/kg-build` (re-extract → re-project → fresh `graph.json`), then redo 4.2–4.4.

Stop after the precision target is met **or** after 3 passes. Record the **best** precision seen, not the
last (§4: "record the best precision and proceed regardless").

---

## Stage 7 — annotation agreement + specificity

### 7.1 Two independent annotator passes

Reliability needs **independent** coders: launch the annotator **twice** over the *same* edge sample, each
pass blind to the other (no shared notes, no second pass seeing the first's labels). Emit two CSVs:

```bash
"$PY" /home/sergi/creativity-graph/scripts/f4_probe.py sheet "$GRAPH" --n 80 --out labels_a.csv
"$PY" /home/sergi/creativity-graph/scripts/f4_probe.py sheet "$GRAPH" --n 80 --out labels_b.csv
```

`sheet` is deterministic (seed 42), so `labels_a.csv` and `labels_b.csv` cover the *identical* edge sample —
exactly what α needs. Run two separate Task calls (one per file); do not let pass B read pass A's output.

```
Task(subagent_type: "kg-annotator", description: "Independent pass A", prompt: "<4.3 prompt, file = labels_a.csv>")
Task(subagent_type: "kg-annotator", description: "Independent pass B", prompt: "<4.3 prompt, file = labels_b.csv>")
```

### 7.2 Build the label-sets file and compute α

`harness agreement` consumes a JSON **list of coder dicts** `[{unit_id: label, ...}, {...}]` with labels in
`{correct | fabricated | vague | wrong_type}`. Convert the two CSVs (key each row by `edge_id`, value =
`verdict`) into `label_sets.json`:

```bash
"$PY" - "labels_a.csv" "labels_b.csv" > label_sets.json <<'EOF'
import csv, json, sys
def coder(p):
    with open(p, encoding="utf-8") as f:
        return {r["edge_id"]: r["verdict"].strip().lower()
                for r in csv.DictReader(f) if r.get("verdict", "").strip()}
json.dump([coder(p) for p in sys.argv[1:]], sys.stdout)
EOF

PYTHONPATH=/home/sergi/creativity-graph/scripts \
  "$PY" -m kg_engine.harness agreement label_sets.json
```

Prints `krippendorff_alpha: <x>` and a verdict line. **α >= 0.67** → the grounding signal is treated as
reliable; **α < 0.67** → the grounding signal (and any gated metric) stays advisory. Units rated by fewer
than 2 coders are ignored, so both passes must cover the same `edge_id`s — they do, by the seed-42 sample.

### 7.3 Specificity bridge-metric gate

```bash
PYTHONPATH=/home/sergi/creativity-graph/scripts \
  "$PY" -m kg_engine.harness specificity "$GRAPH" /home/sergi/creativity-graph/examples/source.md
```

Emits JSON comparing specificity-weighted betweenness against raw degree and raw betweenness over the source
corpus (§1.4/§1.6). Read these fields: `generality_confound_detected`, `rank_churn`, `top_raw_betweenness`
vs `top_specificity_weighted`, `gate_on`, and `verdict`. `gate_on: true` ("specificity-weighting earns its
place — gate ON") means the specificity-weighted bridge metric separates real bridges from vague
high-traffic nodes beyond the churn band and may be promoted out of advisory; otherwise degree stays the
honest MVP advisory and specificity-weighted betweenness remains GATED (§1.6).

> Cross-check (optional): `mcp__creativity-graph__query_graph(node_type="compression")` shows the live
> compression nodes; the specificity leaders should be these, not vague terms like `idea`/`system`.

Stage 7 does not gate either — log α and the gate verdict and proceed (§4).

---

## Record the results — append to PROGRESS.md

Append (never overwrite) a dated block to `PROGRESS.md` at the project root. Fill `<...>` from the captured
stdout; under "iterations" note how many Stage-4 passes ran and that the **best** precision is recorded.

```bash
cat >> "${CLAUDE_PROJECT_DIR:-/home/sergi/creativity-graph}/PROGRESS.md" <<EOF

## /kg-eval — $(date -u +%Y-%m-%dT%H:%M:%SZ)
graph: $GRAPH   (labeled n=<N>)

Stage 4 — extraction precision
- PRECISION (best of <K> pass(es)):  <precision>   (target >= 0.70 — recorded, non-gating)
- astrology rate (fabricated+vague): <astro>
- span-support rate (span_found=y):  <span>
- per-relation precision:            <rel1>=<p1>, <rel2>=<p2>, ...
- confidence calibration:            <gap / "n/a">
- iterations:                        <K> (pack/extractor refinements: <what changed, or "none">)

Stage 7 — agreement + specificity
- krippendorff_alpha:                <alpha>   (reliable >= 0.67 — <RELIABLE | advisory>)
- specificity gate:                  gate_on=<true|false> — <verdict>
- generality confound detected:      <true|false>; rank_churn=<churn>
EOF
```

Then print a one-line summary to the user:
`precision <p> (best/<K>) · α <alpha> (<reliable?>) · specificity gate <on|off>` — and remind them the
numbers are recorded, nothing was gated, the flow proceeds.

## Invariants this command upholds

- **Measure, never gate (§4).** Every threshold here is a *target*, not a barrier. Below target → iterate
  up to 3× then record the best and proceed. No human gate.
- **Annotators cannot forge verdicts (§1.4).** They label a CSV; they hold no `kg_write`/`kg_ground` tools.
  Precision is measured *about* the canon, not written *into* it.
- **Span-present is the honest floor (§1.5).** The span-support rate is reported alongside precision so a
  high precision built on unverifiable spans is visible.
- **Generality confound stays visible (§1.6).** The `vague` rate and the specificity gate verdict together
  say whether centrality is real or inflated; degree remains the honest advisory until the gate turns on.
