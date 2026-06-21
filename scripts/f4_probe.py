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


def load(path):
    with open(path, encoding="utf-8-sig") as f:  # utf-8-sig tolerates a BOM-prefixed export
        data = json.load(f)
    nodes = data.get("nodes", [])
    edges = data.get("links", data.get("edges", []))
    id2label = {n.get("id"): n.get("label", n.get("id")) for n in nodes}
    id2type = {n.get("id"): n.get("file_type", "?") for n in nodes}
    return nodes, edges, id2label, id2type


def summary(path):
    nodes, edges, _, id2type = load(path)
    print(f"nodes: {len(nodes)}   edges: {len(edges)}\n")

    by_type = Counter(n.get("file_type", "?") for n in nodes)
    print("nodes by file_type:")
    for k, v in by_type.most_common():
        print(f"  {k:12} {v}")

    by_conf = Counter(e.get("confidence", "?") for e in edges)
    print("\nedges by confidence:")
    for k, v in by_conf.most_common():
        print(f"  {k:12} {v}")

    by_rel = Counter(e.get("relation", "?") for e in edges)
    print(f"\ntop relations ({len(by_rel)} distinct):")
    for k, v in by_rel.most_common(15):
        print(f"  {v:4}  {k}")

    scores = [e.get("confidence_score") for e in edges
              if e.get("confidence") == INFERRED and e.get("confidence_score") is not None]
    if scores:
        scores.sort()
        print(f"\nINFERRED confidence_score: n={len(scores)} "
              f"min={scores[0]:.2f} median={scores[len(scores)//2]:.2f} max={scores[-1]:.2f}")

    judged = sum(1 for e in edges if e.get("confidence") in (INFERRED, AMBIGUOUS))
    print(f"\njudged edges (INFERRED+AMBIGUOUS): {judged} / {len(edges)} "
          f"({100*judged/max(len(edges),1):.0f}%) <- the precision-relevant part")


def sheet(path, n, out, include_extracted):
    _, edges, id2label, _ = load(path)
    pool = list(enumerate(edges))
    if not include_extracted:
        pool = [(i, e) for i, e in pool if e.get("confidence") != EXTRACTED]
    if not pool:
        sys.exit("no edges to label (try --include-extracted)")
    random.seed(42)
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


def score(path):
    with open(path, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("verdict", "").strip()]
    if not rows:
        sys.exit("no labeled rows (fill the `verdict` column first)")

    n = len(rows)
    verdicts = Counter(r["verdict"].strip().lower() for r in rows)
    unknown = {v: c for v, c in verdicts.items() if v not in VALID_VERDICTS}
    if unknown:
        print(f"WARNING: {sum(unknown.values())} row(s) carry an unrecognized verdict "
              f"{dict(unknown)} (expected {sorted(VALID_VERDICTS)}) — these are excluded from `correct` "
              f"but still count toward the denominator, deflating precision. Fix them.", file=sys.stderr)
    correct = sum(verdicts[v] for v in GOOD)
    astro = sum(verdicts[v] for v in ASTROLOGY)
    span_y = sum(1 for r in rows if r.get("span_found", "").strip().lower() in TRUE_VALUES)

    print(f"labeled edges: {n}\n")
    print(f"PRECISION (correct / labeled):        {correct/n:.2f}   <- exit gate is >= 0.70")
    print(f"astrology rate (fabricated+vague):    {astro/n:.2f}   <- the grounding risk, measured")
    print(f"span-support rate (span_found=y):     {span_y/n:.2f}   <- the span-present check")

    print("\nverdict breakdown:")
    for v, c in verdicts.most_common():
        print(f"  {v:12} {c:4}  ({100*c/n:.0f}%)")

    per_rel = defaultdict(lambda: [0, 0])
    for r in rows:
        ok = r["verdict"].strip().lower() in GOOD
        per_rel[r.get("relation", "?")][0] += ok
        per_rel[r.get("relation", "?")][1] += 1
    print("\nprecision per relation (n>=3):")
    for rel, (ok, tot) in sorted(per_rel.items(), key=lambda x: -x[1][1]):
        if tot >= 3:
            print(f"  {ok/tot:.2f}  ({ok}/{tot})  {rel}")

    # does a numeric confidence (if present) predict correctness?
    cs_correct, cs_wrong = [], []
    for r in rows:
        try:
            cs = float(r.get("confidence_score", ""))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(cs):  # 'NaN'/'inf' parse to a float but would poison the calibration mean
            continue
        (cs_correct if r["verdict"].strip().lower() in GOOD else cs_wrong).append(cs)
    if cs_correct and cs_wrong:
        mc, mw = sum(cs_correct)/len(cs_correct), sum(cs_wrong)/len(cs_wrong)
        print("\nconfidence calibration (does the numeric confidence predict correctness?):")
        print(f"  mean confidence_score | correct edges:   {mc:.2f}")
        print(f"  mean confidence_score | incorrect edges: {mw:.2f}")
        gap = mc - mw
        verdict = ("scores track correctness — the confidence means something"
                   if gap >= 0.10 else
                   "scores DON'T separate correct from wrong — confidence is vocabulary, not grounding")
        print(f"  gap: {gap:+.2f}  ->  {verdict}")


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
                  "--include-extracted" in args)
        elif cmd == "score":
            score(path)
        else:
            sys.exit(__doc__)
    except FileNotFoundError as e:
        sys.exit(f"file not found: {e.filename}")


if __name__ == "__main__":
    main()
