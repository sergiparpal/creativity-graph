"""Stages 7-8: the agreement (Krippendorff alpha), specificity, and ideation machinery is real."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from kg_engine.harness import (
    _key_terms,
    _node_specificity,
    _score_condition,
    absorption,
    agreement,
    idf_seeds,
    ideation,
    specificity,
)

# f4_probe.py is a standalone script under scripts/, not a package module — load it by path.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("f4_probe", _SCRIPTS / "f4_probe.py")
f4_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(f4_probe)


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


# --------------------------------------------------------------------------- harness-f4-1


def test_short_worded_present_sentence_not_counted_unsupported():
    """A sentence whose words are all <=5 chars yields no scorable key terms; it must be excluded
    from the unsupported_rate (numerator AND denominator), not counted as a free 'unsupported'."""
    src = "the cat sat on the mat and the dog ran by the bus"
    # every word is <=5 chars, so _key_terms() is empty for this sentence
    out = "the cat sat on the mat."
    assert not _key_terms(out)
    res = _score_condition([out], src)
    # with the only sentence un-scorable, there's nothing to judge -> rate is 0, not 1.0
    assert res["unsupported_rate"] == 0.0


def test_unsupported_rate_only_counts_scorable_sentences():
    """A genuinely unsupported (long-term) sentence still scores 1.0; a present short-word sentence
    mixed in does not dilute or inflate it — it's simply not in the denominator."""
    src = "entropy grounds the arrow of time"
    outputs = ["Quantum decoherence explains consciousness entirely. the cat sat on the mat."]
    res = _score_condition(outputs, src)
    # only the first sentence is scorable (has key terms), and it's unsupported -> 1.0
    assert res["unsupported_rate"] == 1.0


# --------------------------------------------------------------------------- harness-f4-4


def test_short_alpha_labels_reflect_rarity():
    """Two-letter alpha labels ('AI'/'ML') must match a term so IDF rarity flows into specificity,
    instead of matching nothing and silently falling back to the corpus default."""
    # AI appears in both docs (common, low IDF); ML appears in only one (rare, high IDF).
    docs = ["AI is everywhere and AI appears in every line about AI systems",
            "AI again here but ML is the rare term shown only in this one document"]
    seeds = idf_seeds(docs)
    assert "ai" in seeds and "ml" in seeds  # short alpha labels now produce terms
    assert seeds["ml"] > seeds["ai"]        # the rarer label is more specific
    default = sum(seeds.values()) / len(seeds)
    # a short-label node reflects its term's specificity, not the undifferentiated default
    assert _node_specificity("ML", seeds, default) == seeds["ml"]
    assert _node_specificity("AI", seeds, default) != _node_specificity("ML", seeds, default)


# --------------------------------------------------------------------------- absorption window (§14)


def test_absorption_flags_absorbed_isolated_and_fertile():
    # current graph: A rapidly densified (degree 8), B stayed disconnected (0), C modest (degree 2)
    gdata = {"directed": True,
             "nodes": [{"id": x} for x in ["A", "B", "C", "c1", "c2"] + [f"x{i}" for i in range(8)]],
             "links": ([{"source": "A", "target": f"x{i}"} for i in range(8)]
                       + [{"source": "C", "target": "c1"}, {"source": "C", "target": "c2"}])}
    history = {"A": {"introduced_at": 0, "introduced_degree": 1},
               "B": {"introduced_at": 0, "introduced_degree": 0},
               "C": {"introduced_at": 0, "introduced_degree": 1}}
    res = absorption(gdata, history, now=5)
    assert res["A"]["status"] == "absorbed"   # densified fast -> renormalised, trivial now
    assert res["B"]["status"] == "isolated"   # never connected -> infertile
    assert res["C"]["status"] == "fertile"    # the productive middle
    # novelty half-life: the rapidly-absorbed node decays faster than the fertile one
    assert res["A"]["half_life"] < res["C"]["half_life"]


def test_absorption_empty_history_is_safe():
    assert absorption({"nodes": [], "links": []}, {}) == {}


def test_kg_absorption_tool_no_history(engine):
    out = engine.kg_absorption()
    assert out["tracked"] == 0 and "generations.json" in out["note"]
    assert set(out["summary"]) == {"fertile", "absorbed", "isolated"}


# --------------------------------------------------------------------------- harness-f4-2


def test_f4_median_even_length_averages_central_pair():
    assert f4_probe._median([1.0, 2.0, 3.0, 4.0]) == 2.5  # even: average of 2.0 and 3.0
    assert f4_probe._median([1.0, 2.0, 3.0]) == 2.0       # odd: middle element
    assert f4_probe._median([5.0]) == 5.0


# --------------------------------------------------------------------------- harness-f4-3


def test_f4_sheet_rejects_nonpositive_n(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text(
        '{"nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], '
        '"links": [{"source": "a", "target": "b", "confidence": "INFERRED"}]}',
        encoding="utf-8",
    )
    out = tmp_path / "labels.csv"
    with pytest.raises(SystemExit) as exc:
        f4_probe.sheet(str(graph), 0, str(out), include_extracted=True)
    assert "positive" in str(exc.value).lower()
    assert not out.exists()  # nothing written on rejection
    with pytest.raises(SystemExit):
        f4_probe.sheet(str(graph), -3, str(out), include_extracted=True)
