"""Stage 2: the kg_explain_path read-only egress (§2 minus ILP/embeddings).

Traces the associative chain connecting concepts over GROUNDED edges ONLY, reporting the path length
as an ADVISORY `leap` — never a verdict, never written, never a score. An absent fully-grounded path
is reported honestly (the concepts are joined only through unverified/hypothesized/refuted links).
"""
from __future__ import annotations

import hashlib

from kg_engine.model import EpistemicState, edge_id

# verbatim substrings of the conftest SOURCE, so every edge span-verifies at the kg_write boundary
_S1 = "grounds the claims beneath it"
_S2 = "Entropy grounds the arrow of time"
_S3 = "Heat flows from hot to cold"
_S4 = "Degree approximates importance"


def _canon_digest(engine) -> str:
    """A byte digest of every canon note — the read-only oracle (unchanged ⇒ the egress wrote nothing)."""
    h = hashlib.sha256()
    for p in sorted(engine.canon.note_paths()):
        h.update(p.read_bytes())
    return h.hexdigest()


def _build_grounded(engine, chain) -> None:
    """chain: list of (src, relation, tgt, span). Write all edges through the boundary, then ground
    each via kg_ground so they land epistemic_state=grounded."""
    payload = {"edges": [{"source": s, "target": t, "relation": r, "span": sp, "authored_by": "agent"}
                         for s, r, t, sp in chain]}
    out = engine.kg_write(payload)
    assert out["dispositions"]["ACCEPTED"] >= len(chain), out
    for s, r, t, _sp in chain:
        engine.kg_ground(edge_id(s, r, t), "grounded")
    states = {e.id: e.epistemic_state for e in engine.canon.all_edges()}
    for s, r, t, _sp in chain:
        assert states[edge_id(s, r, t)] == EpistemicState.GROUNDED


def test_explain_path_uses_only_grounded_edges(engine):
    # a-b grounded, b-c only WRITTEN (stays unverified). The chain a..c must NOT silently route through
    # the unverified edge — an unverified link is not an explanation (§2).
    out = engine.kg_write({"edges": [
        {"source": "a", "target": "b", "relation": "grounds", "span": _S1, "authored_by": "agent"},
        {"source": "b", "target": "c", "relation": "grounds", "span": _S2, "authored_by": "agent"},
    ]})
    assert out["dispositions"]["ACCEPTED"] >= 2
    engine.kg_ground(edge_id("a", "grounds", "b"), "grounded")  # only a-b grounded
    res = engine.explain_path(["a", "c"])
    assert res["path"] == [] and res["leap"] is None
    assert res["grounded_only"] is True
    assert "reason" in res and "a" in res["reason"] and "c" in res["reason"]


def test_explain_path_two_grounded_hops(engine):
    _build_grounded(engine, [("a", "grounds", "b", _S1), ("b", "grounds", "c", _S2)])
    res = engine.explain_path(["a", "c"])
    assert res["path"] == ["a", "b", "c"]                       # the intermediate is expanded in
    assert res["leap"] == 2
    assert res["grounded_only"] is True
    assert [e["relation"] for e in res["edges"]] == ["grounds", "grounds"]
    assert res["edges"][0]["span"] == _S1                       # the real audit span travels with the hop
    assert res["edges"][1]["span"] == _S2


def test_explain_path_three_concepts_tsp_order_deterministic(engine):
    _build_grounded(engine, [("a", "grounds", "b", _S1), ("b", "grounds", "c", _S2),
                             ("c", "grounds", "d", _S3), ("d", "grounds", "e", _S4)])
    r1 = engine.explain_path(["a", "c", "e"])
    r2 = engine.explain_path(["a", "c", "e"])
    assert r1["path"] == r2["path"] and r1["path"]             # byte-stable across repeated calls + non-empty
    assert {"a", "c", "e"}.issubset(set(r1["path"]))          # the order visits every requested concept
    assert r1["leap"] == len(r1["path"]) - 1 == len(r1["edges"])
    assert r1["grounded_only"] is True


def test_explain_path_is_read_only(engine):
    _build_grounded(engine, [("a", "grounds", "b", _S1), ("b", "grounds", "c", _S2)])
    before = _canon_digest(engine)
    res = engine.explain_path(["a", "c"])
    assert before == _canon_digest(engine)                     # the egress wrote nothing
    assert res["leap"] == 2                                    # advisory present in the RESPONSE ...
    # ... but never persisted onto a canon edge: it lives only in the tool response, never the canon (G4)
    for e in engine.canon.all_edges():
        assert not hasattr(e, "leap")
    raw = "".join(p.read_text(encoding="utf-8") for p in engine.canon.note_paths())
    assert "leap" not in raw


def test_explain_path_unreachable_reports_honestly(engine):
    # two DISCONNECTED grounded edges (a-b and c-d): there is no grounded path a..c. Report it honestly,
    # never raise.
    _build_grounded(engine, [("a", "grounds", "b", _S1), ("c", "grounds", "d", _S2)])
    res = engine.explain_path(["a", "c"])
    assert res["path"] == [] and res["leap"] is None
    assert res["grounded_only"] is True
    assert "reason" in res
