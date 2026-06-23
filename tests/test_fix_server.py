"""Regression tests for the server.py fix pass (F5, F10/M4, F13/L1, F17/L5, F31, F34).

Each test pins a previously-unguarded behaviour in the MCP tool surface:
  F5    — the thin FastMCP wrappers annotate optionals as `X | None`, so an explicit JSON null
          (which the docs recommend) no longer trips a non-nullable-schema ValidationError.
  F10   — when a kg_write batch ROLLS BACK, the response never contradicts `rolled_back: True`:
          written_nodes is [] and the accepted/demoted counts are re-bucketed into `rolled_back`.
  F13   — a corrupt (invalid-UTF-8) targeted note yields a structured {"ok": False, ...} envelope
          from kg_ground (node branch) and kg_rename, not an unstructured MCP exception.
  F31   — the kg_operate wrapper forwards `members`, so the explicit-member-collapse branch is
          reachable through the only external entry point.
"""
from __future__ import annotations

import inspect
import typing

from kg_engine.canon import RollbackInfo
from kg_engine.model import Node, Provenance
from kg_engine.server import _register


# --- a FakeMCP that captures the registered wrapper callables (no MCP client needed) ---------------
class FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _wrappers(engine):
    mcp = FakeMCP()
    _register(mcp, engine)
    return mcp.tools


def _is_optional(annotation) -> bool:
    """True when the annotation admits None (str | None / Optional[str] / etc.).

    server.py uses `from __future__ import annotations`, so annotations arrive as STRINGS (PEP 563):
    evaluate the string form before structural inspection."""
    import types as _types
    if isinstance(annotation, str):
        try:
            annotation = eval(annotation, {"str": str, "int": int, "list": list,
                                           "typing": typing, "Optional": typing.Optional})
        except Exception:
            return "None" in annotation  # last-resort textual check (e.g. "str | None")
    if annotation is type(None):
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        return type(None) in typing.get_args(annotation)
    # PEP 604 `str | None` is a types.UnionType on 3.10+
    if isinstance(annotation, getattr(_types, "UnionType", ())):  # type: ignore[arg-type]
        return type(None) in typing.get_args(annotation)
    return False


# ---- F5: the six wrappers annotate their None-defaulted optionals as nullable -------------------
def test_f5_optional_params_are_nullable(engine):
    tools = _wrappers(engine)
    expected = {
        "kg_scrub": ["text"],
        "kg_generate": ["second_graph"],
        "kg_operate": ["target", "k"],
        "query_graph": ["node_type", "relation", "epistemic_state"],
        "get_neighbors": ["relation"],
        "kg_context": ["query"],
    }
    for name, params in expected.items():
        sig = inspect.signature(tools[name])
        for p in params:
            ann = sig.parameters[p].annotation
            assert sig.parameters[p].default is None, f"{name}.{p} should default to None"
            assert _is_optional(ann), f"{name}.{p} annotation {ann!r} is not nullable (F5)"


# ---- F5: the wrappers actually accept an explicit None for those params -------------------------
def test_f5_wrappers_accept_explicit_none(engine):
    tools = _wrappers(engine)
    # kg_scrub(None) scrubs the configured source (text=None path)
    assert "scrubbed" in tools["kg_scrub"](None)
    # query_graph with explicit None filters returns a dict (no ValidationError, no crash)
    assert isinstance(tools["query_graph"](None, None, None), dict)
    # kg_context(None, ...) is the documented kg_context(query=None) call
    assert isinstance(tools["kg_context"](None, 2000), dict)
    # get_neighbors with relation=None on an absent node is an empty list, not an exception
    assert tools["get_neighbors"]("nope", None) == []


# ---- F10/M4: a rolled-back kg_write never contradicts itself ------------------------------------
def test_f10_rolled_back_write_reports_nothing_written(engine, monkeypatch):
    # Force the multi-file write to roll back: nothing persisted.
    monkeypatch.setattr(engine.canon, "write_nodes",
                        lambda *a, **k: RollbackInfo(rolled_back=True, error="boom"))
    out = engine.kg_write({"edges": [{"source": "compression", "target": "betweenness",
                                      "relation": "grounds",
                                      "span": "A compression stands in for many observations"}]})
    assert out["rolled_back"] is True
    assert out["error"] == "boom"
    # the contract: written_nodes is empty and the accepted/demoted counts are not reported as written
    assert out["written_nodes"] == []
    assert out["dispositions"].get("ACCEPTED", 0) == 0
    assert out["dispositions"].get("DEMOTED", 0) == 0
    # the would-have-been-persisted count is surfaced honestly under `rolled_back`
    assert out["dispositions"].get("rolled_back", 0) >= 1
    # and nothing actually landed in the canon
    assert engine.canon.all_edges() == []


def test_f10_successful_write_unaffected(engine):
    out = engine.kg_write({"edges": [{"source": "compression", "target": "betweenness",
                                      "relation": "grounds",
                                      "span": "A compression stands in for many observations"}]})
    assert out["rolled_back"] is False
    assert out["dispositions"]["ACCEPTED"] >= 1
    assert "rolled_back" not in out["dispositions"]  # no spurious bucket on success
    assert out["written_nodes"]


# ---- F13/L1: a corrupt targeted note yields a structured error, not an exception ----------------
def _seed_corrupt_note(engine, nid="corrupt"):
    # a valid node first so exists() is True, then clobber the file with invalid UTF-8
    engine.canon.write_one(Node(id=nid, label=nid.title()))
    engine.canon.node_path(nid).write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")


def test_f13_kg_ground_node_on_corrupt_note_is_structured(engine):
    _seed_corrupt_note(engine, "corrupt")
    out = engine.kg_ground("corrupt", "obsolete", kind="node")
    assert out["ok"] is False
    assert "unreadable" in out["error"]


def test_f13_kg_rename_on_corrupt_note_is_structured(engine):
    _seed_corrupt_note(engine, "corrupt")
    out = engine.kg_rename("corrupt", "renamed")
    assert out["ok"] is False
    assert "unreadable" in out["error"]
    assert out["old"] == "corrupt" and out["new"] == "renamed"


# ---- F31: the kg_operate wrapper forwards `members` (explicit-member collapse reachable) --------
def test_f31_kg_operate_wrapper_declares_members(engine):
    tools = _wrappers(engine)
    sig = inspect.signature(tools["kg_operate"])
    assert "members" in sig.parameters, "kg_operate wrapper omits `members` (F31)"
    assert sig.parameters["members"].default is None


def test_f31_explicit_member_collapse_reachable_through_wrapper(engine):
    # seed two real nodes so an explicit member set has something to collapse
    engine.canon.write_nodes([Node(id="alpha", label="Alpha"), Node(id="beta", label="Beta")],
                             message="seed")
    engine.projector.project()
    tools = _wrappers(engine)
    out = tools["kg_operate"]("collapse", None, "", "", ["alpha", "beta"], None)
    assert out.get("ok") is True, out
    # the explicit members produced a compression node + collapses_into edges (hypothesized lane)
    rels = {e.relation for e in engine.canon.all_edges()}
    assert "collapses_into" in rels
    assert any(e.provenance == Provenance.HYPOTHESIZED for e in engine.canon.all_edges())
