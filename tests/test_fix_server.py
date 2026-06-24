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

import pytest

from kg_engine.canon import RollbackInfo
from kg_engine.model import Node, Provenance
from kg_engine.server import _register, build_engine_from_env

# env vars that feed source/project resolution in build_engine_from_env — cleared so the host
# environment can't leak into these tests
_RESOLUTION_ENV = ("KG_SOURCE_PATH", "CLAUDE_PLUGIN_OPTION_SOURCE_PATH", "KG_PROJECT_DIR",
                   "CLAUDE_PROJECT_DIR", "KG_PACK_PATH", "KG_DATA")


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


# ---- mixed-error-architecture: one transport envelope + a logging seam --------------------------
def test_tool_result_envelopes_raised_exceptions(engine, monkeypatch):
    """A tool whose underlying engine call RAISES (e.g. a mid-read index error escaping a pure read)
    returns the uniform transport envelope {ok:False, error, error_kind} instead of crashing the MCP
    call (finding: mixed-error-architecture)."""
    tools = _wrappers(engine)

    def boom(*a, **k):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(engine, "query_graph", boom)
    out = tools["query_graph"](None, None, None)
    assert out == {"ok": False, "error": "index exploded", "error_kind": "RuntimeError"}


def test_tool_result_passes_domain_disposition_through(engine):
    """The envelope must NOT collapse a deliberate {ok:False} DOMAIN result (a refused verdict) into a
    transport error — transport ok/error and domain disposition are orthogonal axes, so a domain failure
    carries no transport `error_kind`."""
    tools = _wrappers(engine)
    dom = tools["kg_ground"]("does-not-exist", "bogus_verdict")
    assert dom["ok"] is False and "error_kind" not in dom


def test_real_fastmcp_registers_every_tool_with_schema(engine):
    """The @_tool_result envelope must not break FastMCP schema generation: register through a REAL
    FastMCP (the FakeMCP above can't catch a schema-gen regression) and assert every tool is exposed
    with its parameters intact through the functools.wraps wrapper."""
    import asyncio

    pytest.importorskip("mcp.server.fastmcp")
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("creativity-graph-test")
    _register(mcp, engine)
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert {"kg_ping", "kg_write", "kg_ground", "query_graph", "kg_context", "kg_export"} <= set(tools)
    props = set((tools["query_graph"].inputSchema.get("properties") or {}).keys())
    assert {"node_type", "relation", "epistemic_state", "limit"} <= props


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


def test_unsubstituted_source_path_placeholder_falls_back_not_taken_literally(tmp_path, monkeypatch):
    """An unconfigured `source_path` (no default in plugin.json) reaches the engine as the LITERAL
    `${user_config.source_path}` via `.mcp.json`. build_engine_from_env must treat that the same as unset
    — like `bootstrap._clean` / `launch_server.clean` — or `source_text()` reads a non-existent file and
    every agent edge fails span verification (`span-not-in-source`). With the bundled example present in the
    project, resolution falls back to it instead of the literal path."""
    for var in _RESOLUTION_ENV:
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "examples").mkdir()
    example = tmp_path / "examples" / "source.md"
    example.write_text("# demo source\n", encoding="utf-8")
    monkeypatch.setenv("KG_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("KG_SOURCE_PATH", "${user_config.source_path}")  # unsubstituted literal

    engine = build_engine_from_env()
    assert engine.source_path == example                  # fell back to the bundled example...
    assert str(engine.source_path) != "${user_config.source_path}"   # ...not taken literally
    assert engine.source_text().strip() == "# demo source"


def test_unsubstituted_source_path_with_no_fallback_is_none(tmp_path, monkeypatch):
    """A real user vault has no `examples/source.md`, so the unsubstituted placeholder must resolve to a
    clean 'no source configured' state (None) — never the literal path, which would silently reject
    every edge."""
    for var in _RESOLUTION_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KG_PROJECT_DIR", str(tmp_path))   # empty vault, no examples/
    monkeypatch.setenv("KG_SOURCE_PATH", "${user_config.source_path}")

    engine = build_engine_from_env()
    assert engine.source_path is None
    assert engine.source_text() == ""
