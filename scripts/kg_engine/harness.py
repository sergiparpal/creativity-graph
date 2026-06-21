"""Validation harness (§Stages 7-8): annotation agreement, the specificity metric, ideation scoring.

Everything here is deterministic measurement over data the subagents produce. No metric gates the
pipeline — each prints a number and a verdict; the orchestration logs it and proceeds (§4).
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# --------------------------------------------------------------------------- Krippendorff's alpha


def agreement(label_sets: list[dict]) -> float:
    """Nominal Krippendorff's alpha across independent coders.

    `label_sets`: one dict per coder, mapping unit_id -> label. Units rated by <2 coders are ignored.
    Returns alpha in (-inf, 1]; 1.0 = perfect agreement, 0 = chance, <0 = systematic disagreement.
    """
    # gather ratings per unit
    per_unit: dict = defaultdict(list)
    for coder in label_sets:
        for unit, label in coder.items():
            if label is not None and str(label).strip() != "":
                per_unit[unit].append(str(label))
    units = {u: vals for u, vals in per_unit.items() if len(vals) >= 2}
    if not units:
        return float("nan")

    # coincidence matrix
    o: dict = defaultdict(float)
    for vals in units.values():
        m = len(vals)
        cnt = Counter(vals)
        for c in cnt:
            for k in cnt:
                pairs = cnt[c] * (cnt[c] - 1) if c == k else cnt[c] * cnt[k]
                o[(c, k)] += pairs / (m - 1)

    cats = sorted({c for (c, _) in o})
    n_c = {c: sum(o[(c, k)] for k in cats) for c in cats}
    n = sum(n_c.values())
    if n <= 1:
        return float("nan")

    # nominal metric: 1 if c != k else 0
    do = sum(o[(c, k)] for c in cats for k in cats if c != k) / n
    de = sum(n_c[c] * n_c[k] for c in cats for k in cats if c != k) / (n * (n - 1))
    if de == 0:
        return 1.0
    return 1.0 - do / de


# --------------------------------------------------------------------------- IDF / specificity

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def idf_seeds(documents: list[str]) -> dict[str, float]:
    """IDF per term over a corpus of documents (sections). Higher = more specific/rarer."""
    n = max(len(documents), 1)
    df: Counter = Counter()
    for doc in documents:
        for term in set(w.lower() for w in _WORD.findall(doc)):
            df[term] += 1
    return {t: math.log(n / (1 + d)) + 1.0 for t, d in df.items()}


def _node_specificity(label: str, seeds: dict[str, float], default: float) -> float:
    terms = [w.lower() for w in _WORD.findall(label or "")]
    if not terms:
        return default
    return sum(seeds.get(t, default) for t in terms) / len(terms)


def specificity(graph_data: dict, corpus: list[str]) -> dict:
    """Compare specificity-weighted betweenness vs raw degree vs raw betweenness.

    Verdict: does specificity-weighting separate real bridges from vague high-traffic nodes beyond a
    churn band? If yes, the gated metric (§1.4/§1.6) earns its place (`gate_on=True`).
    """
    import networkx as nx
    from .projector import node_link_graph

    G = node_link_graph(graph_data).to_undirected()
    if G.number_of_nodes() < 3:
        return {"gate_on": False, "reason": "graph too small", "n": G.number_of_nodes()}

    seeds = idf_seeds(corpus) if corpus else {}
    default = (sum(seeds.values()) / len(seeds)) if seeds else 1.0
    labels = {n: (G.nodes[n].get("label") or n) for n in G.nodes()}
    spec = {n: _node_specificity(labels[n], seeds, default) for n in G.nodes()}

    btw = nx.betweenness_centrality(G)
    deg = dict(G.degree())
    weighted = {n: btw[n] * spec[n] for n in G.nodes()}

    def topk(d, k=5):
        return [n for n, _ in sorted(d.items(), key=lambda x: -x[1])[:k]]

    top_btw, top_w = topk(btw), topk(weighted)
    # generality confound: do raw-betweenness leaders skew toward low specificity?
    mean_spec = sum(spec.values()) / len(spec)
    btw_leader_spec = sum(spec[n] for n in top_btw) / max(len(top_btw), 1)
    confound = btw_leader_spec < mean_spec  # leaders are vaguer than average -> confound present
    churn = len(set(top_btw) ^ set(top_w)) / (2 * max(len(top_btw), 1))  # rank movement
    # if every node has the same specificity (no IDF spread — e.g. a tiny corpus where the default
    # dominates), the weighting can't separate anything and the gate must stay closed regardless of
    # incidental churn. Surface the spread so a degenerate run is legible, not silently "gate ON".
    spread = (max(spec.values()) - min(spec.values())) if spec else 0.0
    has_spread = spread > 1e-9
    gate_on = bool(confound and churn > 0.2 and has_spread)
    return {
        "n": G.number_of_nodes(),
        "mean_specificity": round(mean_spec, 3),
        "specificity_spread": round(spread, 3),
        "betweenness_leader_specificity": round(btw_leader_spec, 3),
        "top_raw_betweenness": top_btw,
        "top_specificity_weighted": top_w,
        "rank_churn": round(churn, 3),
        "generality_confound_detected": confound,
        "gate_on": gate_on,
        "verdict": ("specificity-weighting earns its place — gate ON" if gate_on
                    else ("specificity is uniform (corpus too small / no IDF spread) — stays advisory"
                          if not has_spread
                          else "specificity-weighting does not clearly separate — stays advisory")),
    }


# --------------------------------------------------------------------------- ideation scoring

_SENT = re.compile(r"[.!?]+\s+")


def _ngrams(text: str, n=3) -> set:
    words = [w.lower() for w in _WORD.findall(text)]
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _score_condition(outputs: list[str], source_text: str) -> dict:
    if not outputs:
        return {"n": 0, "diversity": 0.0, "novelty": 0.0, "utility": 0.0, "unsupported_rate": 0.0}
    all_ng: Counter = Counter()
    for o in outputs:
        all_ng.update(_ngrams(o))
    diversity = (len(all_ng) / max(sum(all_ng.values()), 1))
    src_ng = _ngrams(source_text)
    novelties, util, unsupported = [], [], []
    src_norm = source_text.lower()
    for o in outputs:
        ong = _ngrams(o)
        overlap = len(ong & src_ng) / max(len(ong), 1)
        # an empty/too-short output has no n-grams; score it 0 novelty rather than a free 1.0, so a
        # condition can't game the experiment by emitting blank or one-word "ideas".
        novelties.append(1.0 - overlap if ong else 0.0)
        util.append(min(1.0, len(re.findall(r"\bbecause\b|\bif\b|\btherefore\b|\bbridge|\bconnect", o.lower())) / 5))
        sents = [s for s in _SENT.split(o) if len(s.split()) >= 4]
        if sents:
            unsup = sum(1 for s in sents if not any(t in src_norm for t in _key_terms(s)))
            unsupported.append(unsup / len(sents))
    return {
        "n": len(outputs),
        "diversity": round(diversity, 3),
        "novelty": round(sum(novelties) / len(novelties), 3),
        "utility": round(sum(util) / len(util), 3),
        "unsupported_rate": round(sum(unsupported) / max(len(unsupported), 1), 3),
    }


def _key_terms(sentence: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(sentence) if len(w) > 5][:3]


def ideation(outputs_by_condition: dict, source_text: str = "") -> dict:
    """Score pooled outputs per condition (control | graph | rag) and emit a verdict."""
    table = {cond: _score_condition(outs, source_text) for cond, outs in outputs_by_condition.items()}
    g, c = table.get("graph"), table.get("control")
    verdict = "insufficient data"
    if g and c and g["n"] and c["n"]:
        no_regression = (g["diversity"] >= c["diversity"] and g["novelty"] >= c["novelty"]
                         and g["unsupported_rate"] <= c["unsupported_rate"] + 0.05)
        # require a STRICT improvement on at least one axis: an exact tie is not a graph win
        strict_gain = (g["diversity"] > c["diversity"] or g["novelty"] > c["novelty"])
        better = no_regression and strict_gain
        verdict = ("graph condition produced more diverse/novel ideas without more unsupported claims"
                   if better else "graph condition did NOT clearly beat control")
    return {"table": table, "verdict": verdict}


# --------------------------------------------------------------------------- CLI


def _demo_corpus() -> list[str]:
    return ["entropy grounds the arrow of time", "betweenness measures bridges",
            "specificity weights betweenness by term rarity", "the generality confound inflates vague nodes"]


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m kg_engine.harness {agreement|specificity|ideation} [path...]", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "agreement":
        path = argv[1] if len(argv) > 1 else None
        if path and Path(path).exists():
            label_sets = json.loads(Path(path).read_text())
        else:
            label_sets = [{"e1": "correct", "e2": "vague", "e3": "correct"},
                          {"e1": "correct", "e2": "vague", "e3": "fabricated"}]
            print("[harness] no labels file; using demo label sets", file=sys.stderr)
        a = agreement(label_sets)
        print(f"krippendorff_alpha: {a:.3f}")
        print(f"verdict: {'RELIABLE (>=0.67)' if a >= 0.67 else 'BELOW THRESHOLD — grounding signal stays advisory'}")
        return 0
    if cmd == "specificity":
        gpath = argv[1] if len(argv) > 1 else "derived/graph.json"
        spath = argv[2] if len(argv) > 2 else None
        if Path(gpath).exists():
            gdata = json.loads(Path(gpath).read_text())
        else:
            gdata = {"directed": True, "nodes": [{"id": "a", "label": "system"}, {"id": "b", "label": "entropy"},
                     {"id": "c", "label": "time"}, {"id": "d", "label": "thermodynamic arrow"}],
                     "links": [{"source": "a", "target": "b"}, {"source": "a", "target": "c"},
                               {"source": "b", "target": "d"}, {"source": "c", "target": "d"}]}
            print("[harness] no graph.json; using demo graph", file=sys.stderr)
        corpus = [Path(spath).read_text()] if spath and Path(spath).exists() else _demo_corpus()
        res = specificity(gdata, corpus)
        print(json.dumps(res, indent=2))
        return 0
    if cmd == "ideation":
        path = argv[1] if len(argv) > 1 else None
        if path and Path(path).exists():
            blob = json.loads(Path(path).read_text())
            src = blob.get("source", "")
            # when outputs aren't nested under "outputs", treat the rest of the blob as conditions but
            # never let the top-level "source" string leak in as a fake (char-iterated) condition.
            obc = blob.get("outputs", {k: v for k, v in blob.items() if k != "source"})
        else:
            obc = {"control": ["A is connected to B."], "graph": ["A bridges B and C because entropy grounds time."],
                   "rag": ["A relates to B somehow."]}
            src = "entropy grounds the arrow of time"
            print("[harness] no outputs file; using demo outputs", file=sys.stderr)
        res = ideation(obc, src)
        print(json.dumps(res, indent=2))
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
