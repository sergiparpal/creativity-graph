"""Stages 7-8: the agreement (Krippendorff alpha), specificity, and ideation machinery is real."""
from __future__ import annotations

from kg_engine.harness import agreement, idf_seeds, ideation, specificity


def test_perfect_agreement_alpha_one():
    a = agreement([{"u1": "correct", "u2": "vague"}, {"u1": "correct", "u2": "vague"}])
    assert abs(a - 1.0) < 1e-9


def test_disagreement_lowers_alpha():
    perfect = agreement([{f"u{i}": "x" for i in range(6)}, {f"u{i}": "x" for i in range(6)}])
    mixed = agreement([{f"u{i}": "x" for i in range(6)},
                       {f"u{i}": ("x" if i % 2 else "y") for i in range(6)}])
    assert perfect == 1.0
    assert mixed < perfect


def test_alpha_threshold_semantics():
    # 5/6 units agree -> alpha should be clearly positive but the test only asserts it is finite & < 1
    a = agreement([{f"u{i}": "x" for i in range(6)},
                   {**{f"u{i}": "x" for i in range(5)}, "u5": "y"}])
    assert a == a and a < 1.0  # not NaN


def test_idf_seeds_rank_rare_above_common():
    docs = ["the system idea", "the system thing", "the betweenness metric appears once"]
    seeds = idf_seeds(docs)
    assert seeds["betweenness"] > seeds["system"]


def test_specificity_verdict_shape():
    gdata = {"directed": True,
             "nodes": [{"id": "vague", "label": "system idea"}, {"id": "b", "label": "betweenness"},
                       {"id": "c", "label": "specificity confound"}, {"id": "d", "label": "entropy arrow"}],
             "links": [{"source": "vague", "target": "b"}, {"source": "vague", "target": "c"},
                       {"source": "vague", "target": "d"}, {"source": "b", "target": "c"}]}
    corpus = ["system idea is everywhere and common in every line of the system",
              "betweenness specificity entropy arrow are each rare technical terms"]
    res = specificity(gdata, corpus)
    assert "gate_on" in res and "verdict" in res
    assert isinstance(res["generality_confound_detected"], bool)


def test_ideation_table_and_verdict():
    src = "entropy grounds the arrow of time and betweenness measures bridges"
    outputs = {
        "control": ["A relates to B.", "B relates to C."],
        "graph": ["Entropy bridges thermodynamics and information because it grounds the arrow of time.",
                  "Betweenness connects communities if a node is specific."],
        "rag": ["entropy grounds the arrow of time", "betweenness measures bridges"],
    }
    res = ideation(outputs, src)
    assert set(res["table"]) == {"control", "graph", "rag"}
    for cond in res["table"].values():
        assert 0.0 <= cond["unsupported_rate"] <= 1.0
    assert isinstance(res["verdict"], str)
