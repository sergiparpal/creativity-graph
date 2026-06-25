"""End-to-end smoke of the whole generative loop (PLAN Stage 9): build → ground → generate(all) →
propose → ground the hypotheses (with support) → query → experiment. Asserts each step's structured
output shape and the version stamp, so the full pipeline is exercised non-interactively.
"""
from __future__ import annotations

from kg_engine import __version__
from kg_engine.harness import ideation
from kg_engine.model import EpistemicState, Provenance


def _build(engine):
    """BUILD: span-verifying edges drawn from the configured source (mimics /kg-build extractor output)."""
    out = engine.kg_write({"edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "grounds the claims beneath it", "authored_by": "agent"},
        {"source": "betweenness", "target": "generality-confound", "relation": "confounded_by",
         "span": "Betweenness is confounded by the generality confound", "authored_by": "agent"},
        {"source": "specificity-weighted-betweenness", "target": "bridge", "relation": "reconciles_with",
         "span": "Specificity-weighted betweenness reconciles with the bridge intuition", "authored_by": "agent"},
        {"source": "degree", "target": "importance", "relation": "approximates",
         "span": "Degree approximates importance", "authored_by": "agent"},
        {"source": "failed", "target": "negative-information", "relation": "defends_against",
         "span": "negative information and defends against re-proposal", "authored_by": "agent"},
        {"source": "entropy", "target": "arrow", "relation": "grounds",
         "span": "Entropy grounds the arrow of time", "authored_by": "agent"},
    ]})
    assert out["dispositions"]["ACCEPTED"] >= 5, out
    return out


def test_end_to_end_generative_loop(engine):
    # version stamp (release gate)
    assert engine.kg_ping()["version"] == __version__ == "0.4.1"

    # 1. BUILD
    _build(engine)

    # 2. GROUND a couple of the span-present edges
    for e in engine.canon.all_edges():
        if e.relation in ("grounds", "approximates"):
            assert engine.kg_ground(e.id, "grounded")["ok"]
    grounded = [e for e in engine.canon.all_edges() if e.epistemic_state == EpistemicState.GROUNDED]
    assert grounded

    # 3. GENERATE (all mechanisms) — read-only, hypothesized proposals, no span
    gen = engine.kg_generate("all", k=10)
    assert gen["count"] >= 1 and gen["gate_on"] in (0, 1)
    edge_cands = [c for c in gen["candidates"] if c["kind"] == "edge"]
    assert edge_cands and all("span" not in c for c in gen["candidates"])

    # 4. PROPOSE the edge candidates into the hypothesized lane
    prop = engine.kg_propose({"edges": [{"source": c["source"], "target": c["target"],
                                         "relation": c["relation"]} for c in edge_cands]})
    assert prop["propose_lane"] and prop["dispositions"]["ACCEPTED"] >= 1
    hyp = [e for e in engine.canon.all_edges() if e.provenance == Provenance.HYPOTHESIZED]
    assert hyp and all(e.epistemic_state == EpistemicState.UNVERIFIED and e.span == "" for e in hyp)

    # 5. GROUND a hypothesis WITH support — promotion upgrades provenance (earned grounding)
    promoted = engine.kg_ground(hyp[0].id, "grounded", support_span="grounds the claims beneath it")
    assert promoted["ok"] and promoted["provenance_upgraded_to"] == "span-present"
    after = next(e for e in engine.canon.all_edges() if e.id == hyp[0].id)
    assert after.epistemic_state == EpistemicState.GROUNDED and after.provenance == Provenance.SPAN_PRESENT
    # a hypothesis without support is refused (the gate holds)
    if len(hyp) > 1:
        assert engine.kg_ground(hyp[1].id, "grounded")["error"] == "hypothesis-needs-support"

    # 6. QUERY — the two lanes are segregated
    ctx = engine.kg_context(budget=5000)
    item_provs = {i["provenance"] for i in ctx["items"]}
    assert "hypothesized" not in item_provs                       # grounded lane excludes proposals
    assert all(h["provenance"] == "hypothesized" for h in ctx["hypotheses"])
    assert ctx["advisory"]["bridge_metric"]["nodes"]              # the completed bridge metric is present

    # 7. EXPERIMENT — ideation table includes graph+generate with its own verdict
    src = engine.source_text()
    outputs = {
        "control": ["A relates to B.", "B relates to C."],
        "graph": ["Betweenness is confounded by the generality confound, so a vague node inflates it.",
                  "Degree approximates importance more honestly than betweenness."],
        "graph+generate": [
            "Betweenness might bridge memory-of-failures because a metric scoring nodes between "
            "confirmation and refutation connects them.",
            "Specificity-weighted betweenness could reconcile with degree where rarity and connection agree.",
            "Compression collapses observations; its absorption half-life predicts which survive grounding."],
        "rag": ["Entropy grounds the arrow of time.", "Heat flows from hot to cold."],
    }
    res = ideation(outputs, src)
    assert set(res["table"]) == {"control", "graph", "graph+generate", "rag"}
    assert "verdict" in res and "generate_verdict" in res         # graph+generate scored separately
    for cond in res["table"].values():
        assert 0.0 <= cond["unsupported_rate"] <= 1.0
