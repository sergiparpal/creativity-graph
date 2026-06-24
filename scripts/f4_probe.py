#!/usr/bin/env python3
"""
f4_probe.py — measure the extractor's precision on a non-self-grounding conceptual document.

Turns the extractor's output graph.json (NetworkX node-link) into precision numbers by sampling
edges, labeling each against the source, and scoring. Also reports the astrology rate
(fabricated+vague), the span-support rate, precision per relation type, and — if edges carry a
numeric confidence — whether that confidence actually predicts correctness.

Usage:
  python f4_probe.py summary  graph.json
  python f4_probe.py sheet    graph.json --n 80 --out labels.csv
  # fill `verdict` (correct|fabricated|vague|wrong_type) and `span_found` (y|n) in labels.csv
  python f4_probe.py score    labels.csv

VERDICT vocabulary (the only judgment that matters):
  correct     - the relation is true and specific to the source
  fabricated  - the relation is not supported by the source at all (hallucinated)
  vague       - "true" only because it's generic/unfalsifiable (the generality confound)
  wrong_type  - endpoints related, but the relation label is wrong
span_found (y/n): is there an actual textual span in the source supporting it (the span-present check)?
"""

import csv
import json
import math
import random
import sys
from collections import Counter, defaultdict

EXTRACTED, INFERRED, AMBIGUOUS = "EXTRACTED", "INFERRED", "AMBIGUOUS"
SHEET_COLS = [
    "edge_id", "source_label", "target_label", "relation",
    "confidence", "confidence_score", "source_file",
    "verdict", "span_found", "notes",
]
GOOD = {"correct"}
ASTROLOGY = {"fabricated", "vague"}
VALID_VERDICTS = GOOD | ASTROLOGY | {"wrong_type"}
TRUE_VALUES = {"y", "yes", "true", "1"}  # accepted spellings for span_found

PRECISION_GATE = 0.70    # advisory exit gate printed by score() (this script measures, never enforces)
CALIBRATION_GAP = 0.10   # mean-confidence gap that counts as "scores track correctness"
MIN_RELATION_N = 3       # don't show a per-relation precision under this many labeled edges
SAMPLE_SEED = 42         # fixed seed so sheet() samples reproducibly
TOP_RELATIONS = 15       # cap on the top-relations list in summary()


def _verdict(row):
    """The verdict cell of a label row, normalized (stripped + lowercased)."""
    return row.get("verdict", "").strip().lower()


def _is_correct(row):
    """True iff the row's normalized verdict counts as correct."""
    return _verdict(row) in GOOD


def _span_found(row):
    """True iff the row's span_found cell is one of the accepted true spellings."""
    return row.get("span_found", "").strip().lower() in TRUE_VALUES


def _flag_str(args, name, default):
    if name not in args:
        return default
    i = args.index(name)
    if i + 1 >= len(args):
        sys.exit(f"{name} requires a value")
    return args[i + 1]


def _flag_int(args, name, default):
    raw = _flag_str(args, name, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer, got {raw!r}")


def _median(sorted_vals):
    """True median of an already-sorted, non-empty list: average the two central elements on even
    length rather than reporting the upper-middle one (finding harness-f4-2)."""
    m = len(sorted_vals)
    mid = m // 2
    if m % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def load(path):
    with open(path, encoding="utf-8-sig") as f:  # utf-8-sig tolerates a BOM-prefixed export
        data = json.load(f)
    nodes = data.get("nodes", [])
    edges = data.get("links", data.get("edges", []))
    id2label = {n.get("id"): n.get("label", n.get("id")) for n in nodes}
    id2type = {n.get("id"): n.get("file_type", "?") for n in nodes}
    return nodes, edges, id2label, id2type


def _print_counter(title, counter, top=None, row=lambda k, v: f"  {k:12} {v}"):
    """Print a titled Counter as most_common rows; `top` caps the rows, `row` formats each."""
    print(title)
    for k, v in counter.most_common(top):
        print(row(k, v))


def summary(path):
    nodes, edges, _, id2type = load(path)
    print(f"nodes: {len(nodes)}   edges: {len(edges)}\n")

    by_type = Counter(n.get("file_type", "?") for n in nodes)
    _print_counter("nodes by file_type:", by_type)

    by_conf = Counter(e.get("confidence", "?") for e in edges)
    _print_counter("\nedges by confidence:", by_conf)

    by_rel = Counter(e.get("relation", "?") for e in edges)
    _print_counter(f"\ntop relations ({len(by_rel)} distinct):", by_rel,
                   top=TOP_RELATIONS, row=lambda k, v: f"  {v:4}  {k}")

    scores = [e.get("confidence_score") for e in edges
              if e.get("confidence") == INFERRED and e.get("confidence_score") is not None]
    if scores:
        scores.sort()
        print(f"\nINFERRED confidence_score: n={len(scores)} "
              f"min={scores[0]:.2f} median={_median(scores):.2f} max={scores[-1]:.2f}")

    judged = sum(1 for e in edges if e.get("confidence") in (INFERRED, AMBIGUOUS))
    print(f"\njudged edges (INFERRED+AMBIGUOUS): {judged} / {len(edges)} "
          f"({100*judged/max(len(edges),1):.0f}%) <- the precision-relevant part")


def sheet(path, n, out, include_extracted):
    # n<=0 would slice an empty/mis-sliced pool and write a 0-row sheet while exiting success
    # (finding harness-f4-3). Reject it loudly so the labeling run can't silently produce nothing.
    if n <= 0:
        sys.exit(f"--n must be a positive integer, got {n}")
    _, edges, id2label, _ = load(path)
    pool = list(enumerate(edges))
    if not include_extracted:
        pool = [(i, e) for i, e in pool if e.get("confidence") != EXTRACTED]
    if not pool:
        sys.exit("no edges to label (try --include-extracted)")
    random.seed(SAMPLE_SEED)
    random.shuffle(pool)
    pick = pool[:n]

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLS)
        w.writeheader()
        for i, e in pick:
            w.writerow({
                "edge_id": i,
                "source_label": id2label.get(e.get("source"), e.get("source")),
                "target_label": id2label.get(e.get("target"), e.get("target")),
                "relation": e.get("relation", ""),
                "confidence": e.get("confidence", ""),
                "confidence_score": e.get("confidence_score", ""),
                "source_file": e.get("source_file", ""),
                "verdict": "", "span_found": "", "notes": "",
            })
    print(f"wrote {len(pick)} edges to {out}")
    print("fill `verdict` (correct|fabricated|vague|wrong_type) and `span_found` (y|n), then:")
    print(f"  python f4_probe.py score {out}")


def _load_labeled_rows(path):
    """Read the label CSV, keeping only rows with a non-empty verdict cell."""
    with open(path, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if _verdict(r)]


def _report_headline(rows, n):
    """Print the precision / astrology / span-support headline block."""
    verdicts = Counter(_verdict(r) for r in rows)
    unknown = {v: c for v, c in verdicts.items() if v not in VALID_VERDICTS}
    if unknown:
        print(f"WARNING: {sum(unknown.values())} row(s) carry an unrecognized verdict "
              f"{dict(unknown)} (expected {sorted(VALID_VERDICTS)}) — these are excluded from `correct` "
              f"but still count toward the denominator, deflating precision. Fix them.", file=sys.stderr)
    correct = sum(verdicts[v] for v in GOOD)
    astro = sum(verdicts[v] for v in ASTROLOGY)
    span_y = sum(1 for r in rows if _span_found(r))

    print(f"labeled edges: {n}\n")
    print(f"PRECISION (correct / labeled):        {correct/n:.2f}   <- exit gate is >= {PRECISION_GATE:.2f}")
    print(f"astrology rate (fabricated+vague):    {astro/n:.2f}   <- the grounding risk, measured")
    print(f"span-support rate (span_found=y):     {span_y/n:.2f}   <- the span-present check")
    return verdicts


def _report_verdict_breakdown(verdicts, n):
    """Print the per-verdict count breakdown."""
    print("\nverdict breakdown:")
    for v, c in verdicts.most_common():
        print(f"  {v:12} {c:4}  ({100*c/n:.0f}%)")


def _report_per_relation(rows):
    """Print precision per relation type for relations with enough labeled edges."""
    per_rel = defaultdict(lambda: [0, 0])
    has_relation = any("relation" in r for r in rows)
    for r in rows:
        ok = _is_correct(r)
        rel = r.get("relation") or "?"
        per_rel[rel][0] += ok
        per_rel[rel][1] += 1
    print(f"\nprecision per relation (n>={MIN_RELATION_N}):")
    if not has_relation:
        # don't present a single bogus "?" bucket as a per-relation breakdown when the sheet simply has
        # no 'relation' column — say so instead (review-nit).
        print("  (no 'relation' column in the sheet — per-relation breakdown unavailable)")
    else:
        for rel, (ok, tot) in sorted(per_rel.items(), key=lambda x: -x[1][1]):
            if tot >= MIN_RELATION_N:
                print(f"  {ok/tot:.2f}  ({ok}/{tot})  {rel}")


def _report_calibration(rows):
    """Print whether a numeric confidence_score (if present) predicts correctness."""
    scores_for_correct, scores_for_wrong = [], []
    for r in rows:
        try:
            score = float(r.get("confidence_score", ""))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(score):  # 'NaN'/'inf' parse to a float but would poison the calibration mean
            continue
        (scores_for_correct if _is_correct(r) else scores_for_wrong).append(score)
    if scores_for_correct and scores_for_wrong:
        mean_correct = sum(scores_for_correct)/len(scores_for_correct)
        mean_wrong = sum(scores_for_wrong)/len(scores_for_wrong)
        print("\nconfidence calibration (does the numeric confidence predict correctness?):")
        print(f"  mean confidence_score | correct edges:   {mean_correct:.2f}")
        print(f"  mean confidence_score | incorrect edges: {mean_wrong:.2f}")
        gap = mean_correct - mean_wrong
        calibration_msg = ("scores track correctness — the confidence means something"
                           if gap >= CALIBRATION_GAP else
                           "scores DON'T separate correct from wrong — confidence is vocabulary, not grounding")
        print(f"  gap: {gap:+.2f}  ->  {calibration_msg}")


def score(path):
    rows = _load_labeled_rows(path)
    if not rows:
        sys.exit("no labeled rows (fill the `verdict` column first)")

    n = len(rows)
    verdicts = _report_headline(rows, n)
    _report_verdict_breakdown(verdicts, n)
    _report_per_relation(rows)
    _report_calibration(rows)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    try:
        if cmd == "summary":
            summary(path)
        elif cmd == "sheet":
            sheet(path, _flag_int(args, "--n", 80), _flag_str(args, "--out", "labels.csv"),
                  include_extracted="--include-extracted" in args)
        elif cmd == "score":
            score(path)
        else:
            sys.exit(__doc__)
    except FileNotFoundError as e:
        sys.exit(f"file not found: {e.filename}")


if __name__ == "__main__":
    main()
