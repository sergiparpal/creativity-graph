"""Stages 7-8: the agreement (Krippendorff alpha), specificity, and ideation machinery is real."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from kg_engine.harness import (
    _key_terms,
    _main,
    _score_condition,
    node_specificity,
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


# --------------------------------------------------------------------------- optional lightrag arm


_METRIC_KEYS = {"n", "diversity", "novelty", "utility", "unsupported_rate"}


def test_ideation_scores_lightrag_arm_when_present():
    """The optional fifth arm `lightrag` (a real GraphRAG baseline) is scored with the SAME metric keys
    as every other arm, and a graph-vs-LightRAG `lightrag_verdict` is emitted when both are present."""
    src = "entropy grounds the arrow of time and betweenness measures bridges"
    outputs = {
        "control": ["A relates to B.", "B relates to C."],
        "graph": ["Entropy bridges thermodynamics and information because it grounds the arrow of time.",
                  "Betweenness connects communities if a node is specific."],
        "rag": ["entropy grounds the arrow of time", "betweenness measures bridges"],
        "lightrag": ["A GraphRAG index links entropy to the arrow of time.",
                     "Betweenness clusters surface bridges between communities."],
    }
    res = ideation(outputs, src)
    assert "lightrag" in res["table"]
    # the lightrag row carries exactly the same metric keys as the others
    assert set(res["table"]["lightrag"]) == _METRIC_KEYS == set(res["table"]["control"])
    assert res["table"]["lightrag"]["n"] == 2
    for v in res["table"]["lightrag"].values():
        assert isinstance(v, (int, float))
    assert isinstance(res.get("lightrag_verdict"), str) and "LightRAG" in res["lightrag_verdict"]


def test_ideation_omits_lightrag_when_absent():
    """Without a lightrag arm the scorer still returns a valid four-arm table — no error, no empty row,
    and no lightrag_verdict. The default (no-LightRAG) experiment is byte-for-byte unchanged."""
    src = "entropy grounds the arrow of time and betweenness measures bridges"
    outputs = {
        "control": ["A relates to B."],
        "graph": ["Entropy bridges thermodynamics because it grounds the arrow of time."],
        "graph+generate": ["A hypothesized bridge connects betweenness and specificity."],
        "rag": ["entropy grounds the arrow of time"],
    }
    res = ideation(outputs, src)
    assert set(res["table"]) == {"control", "graph", "graph+generate", "rag"}
    assert "lightrag" not in res["table"]
    assert "lightrag_verdict" not in res
    assert isinstance(res["verdict"], str)


def test_ideation_table_is_canonically_ordered_regardless_of_input_order():
    """Arms are emitted in canonical order (…rag, lightrag) no matter the input dict order, and any
    non-canonical extra arm is tolerated (scored and appended), never an error."""
    src = "entropy grounds the arrow of time"
    outputs = {
        "lightrag": ["entropy links to time"],
        "rag": ["entropy grounds the arrow of time"],
        "graph": ["entropy bridges time because it grounds the arrow"],
        "control": ["a relates to b"],
        "experimental": ["a novel extra arm"],   # non-canonical: must still be scored
    }
    res = ideation(outputs, src)
    keys = list(res["table"])
    assert keys[:4] == ["control", "graph", "rag", "lightrag"]   # canonical order, input order ignored
    assert "experimental" in res["table"]                        # extra arm tolerated, not dropped


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
    assert node_specificity("ML", seeds, default) == seeds["ml"]
    assert node_specificity("AI", seeds, default) != node_specificity("ML", seeds, default)


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


# --------------------------------------------------------------------------- I_harness [1]


def test_cli_malformed_json_clean_diagnostic_no_demo_fallback(tmp_path, capsys):
    """A present-but-unparseable input file gets a one-line diagnostic and a non-zero exit — it must
    NOT silently fall back to scoring the demo corpus (that would report a misleading number)."""
    broken = tmp_path / "broken.json"
    broken.write_text("{ broken json", encoding="utf-8")
    rc = _main(["agreement", str(broken)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed to parse" in err and "broken.json" in err
    # the demo would have printed a krippendorff_alpha line; a clean error must not
    assert "krippendorff_alpha" not in err


def test_cli_malformed_json_clean_for_all_subcommands(tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("not json at all", encoding="utf-8")
    for cmd in ("agreement", "specificity", "ideation"):
        assert _main([cmd, str(broken)]) == 2
        assert "failed to parse" in capsys.readouterr().err


def test_cli_absent_file_still_falls_back_to_demo(tmp_path, capsys):
    """The graceful demo fallback stays reserved for the genuinely-absent-file case."""
    rc = _main(["agreement", str(tmp_path / "does-not-exist.json")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "using demo label sets" in captured.err
    assert "krippendorff_alpha" in captured.out


# --------------------------------------------------------------------------- I_harness [2]


def test_agreement_rejects_non_list_shape():
    """A top-level JSON object (single dict) instead of a list of coder dicts must raise a usage
    ValueError, not an opaque AttributeError from iterating dict keys as coders."""
    with pytest.raises(ValueError):
        agreement({"e1": "correct", "e2": "vague"})
    with pytest.raises(ValueError):
        agreement([{"u1": "correct"}, "not-a-dict"])


def test_cli_agreement_object_input_clean_error(tmp_path, capsys):
    labels = tmp_path / "labels.json"
    labels.write_text('{"e1": "correct", "e2": "vague"}', encoding="utf-8")
    rc = _main(["agreement", str(labels)])
    assert rc == 2
    assert "label_sets must be a list" in capsys.readouterr().err


# --------------------------------------------------------------------------- I_harness [3]


def test_agreement_whitespace_variants_are_one_category():
    """Cosmetically-different but semantically-identical labels ('correct' vs 'correct ') must score
    as agreement, not as two distinct nominal categories."""
    a = agreement([{"u1": "correct "}, {"u1": "correct"}])
    assert abs(a - 1.0) < 1e-9


# --------------------------------------------------------------------------- optional lightrag arm module
# These exercise ONLY the isolated arm's gating/inspection surface. They never import the `lightrag`
# package and never hit the network — per the task constraint, the real LightRAG integration is not
# unit-tested here; the harness scoring (above) covers a synthesised `lightrag` arm.

from kg_engine import lightrag_arm  # safe: no top-level lightrag import in the module


def test_lightrag_arm_off_by_default(monkeypatch):
    """With no opt-in env var the arm reports unavailable and names the opt-in as the missing piece —
    so even if the package happened to be installed, the arm stays off until KG_LIGHTRAG=1."""
    monkeypatch.delenv("KG_LIGHTRAG", raising=False)
    ok, reason = lightrag_arm.availability()
    assert ok is False
    assert "KG_LIGHTRAG" in reason


def test_lightrag_arm_check_cli_unavailable_is_clean(monkeypatch, capsys):
    """`check` always exits 0 and prints a JSON {available, reason} blob — the evaluator parses this to
    decide whether to include the arm; an unavailable arm is a normal, non-error outcome."""
    monkeypatch.delenv("KG_LIGHTRAG", raising=False)
    rc = lightrag_arm._main(["check"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["available"] is False and isinstance(out["reason"], str)


def test_lightrag_answer_cli_omits_cleanly_when_unavailable(monkeypatch, tmp_path, capsys):
    """`answer` exits with the distinct 'cleanly unavailable' code (3) and writes nothing when the arm
    is off — the evaluator treats that as 'omit the lightrag key', never a crash."""
    monkeypatch.delenv("KG_LIGHTRAG", raising=False)
    prompts = tmp_path / "p.json"
    prompts.write_text('["q1", "q2"]', encoding="utf-8")
    out = tmp_path / "answers.json"
    rc = lightrag_arm._main(["answer", "--source", "examples/source.md",
                             "--prompts", str(prompts), "--out", str(out)])
    assert rc == 3
    assert not out.exists()  # no answers written on a cleanly-unavailable arm
    err = json.loads(capsys.readouterr().err)
    assert err["available"] is False


def test_lightrag_store_lives_under_gitignored_derived_dir(monkeypatch, tmp_path):
    """The working store sits under <KG_DATA>/derived/lightrag — inside the already-gitignored derived
    tree, never the canon."""
    monkeypatch.setenv("KG_DATA", str(tmp_path))
    assert lightrag_arm.default_store_dir() == tmp_path / "derived" / "lightrag"


def test_lightrag_load_prompts_accepts_array_object_and_lines(tmp_path):
    arr = tmp_path / "a.json"; arr.write_text('["one", "two"]', encoding="utf-8")
    obj = tmp_path / "o.json"; obj.write_text('{"prompts": ["one", "two"]}', encoding="utf-8")
    lines = tmp_path / "l.txt"; lines.write_text("one\ntwo\n", encoding="utf-8")
    assert lightrag_arm._load_prompts(arr) == ["one", "two"]
    assert lightrag_arm._load_prompts(obj) == ["one", "two"]
    assert lightrag_arm._load_prompts(lines) == ["one", "two"]
