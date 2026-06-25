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
from kg_engine.model import EpistemicState, Node, Provenance
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


# ---- node-promotion gate: a hypothesized NODE earns grounding only with support --------------------
def _seed_hypothesized_node(engine, nid="comp-x"):
    engine.canon.write_one(Node(id=nid, label=nid.title(), node_type="compression",
                                provenance=Provenance.HYPOTHESIZED))
    return nid


def test_hypothesized_node_grounded_without_support_is_refused(engine):
    """A hypothesized node grounded to `grounded` with no support is refused — a generated idea must
    earn grounding, mirroring the edge gate (the node path previously skipped this and left the node
    state=grounded / provenance=hypothesized)."""
    nid = _seed_hypothesized_node(engine)
    out = engine.kg_ground(nid, "grounded", kind="node")
    assert out["ok"] is False
    assert out["error"] == "hypothesis-needs-support"
    # the node was left UNTOUCHED — still hypothesized, still unverified
    node = engine.canon.read_node(nid)
    assert node.provenance == Provenance.HYPOTHESIZED
    assert node.epistemic_state == EpistemicState.UNVERIFIED


def test_hypothesized_node_promoted_with_span_upgrades_provenance(engine):
    """A verbatim source span promotes a hypothesized node: provenance upgrades to span-present and the
    node lands grounded (no stray `span` attr; the span is restated in the body)."""
    nid = _seed_hypothesized_node(engine)
    out = engine.kg_ground(nid, "grounded", kind="node",
                           support_span="A compression stands in for many observations")
    assert out["ok"] is True
    assert out["provenance_upgraded_to"] == "span-present"
    node = engine.canon.read_node(nid)
    assert node.provenance == Provenance.SPAN_PRESENT
    assert node.epistemic_state == EpistemicState.GROUNDED
    assert not hasattr(node, "span")  # no stray span attribute leaked onto the Node
    assert "A compression stands in for many observations" in node.body


def test_hypothesized_node_promoted_with_note_upgrades_to_inferred(engine):
    nid = _seed_hypothesized_node(engine)
    out = engine.kg_ground(nid, "grounded", kind="node", support_note="see external ref [42]")
    assert out["ok"] is True
    assert out["provenance_upgraded_to"] == "inferred"
    node = engine.canon.read_node(nid)
    assert node.provenance == Provenance.INFERRED
    assert node.epistemic_state == EpistemicState.GROUNDED
    assert "see external ref [42]" in node.body


def test_hypothesized_node_promotion_span_not_in_source_is_refused(engine):
    nid = _seed_hypothesized_node(engine)
    out = engine.kg_ground(nid, "grounded", kind="node",
                           support_span="this phrase is nowhere in the source text at all")
    assert out["ok"] is False
    assert out["error"] == "support-span-not-in-source"
    node = engine.canon.read_node(nid)
    assert node.provenance == Provenance.HYPOTHESIZED  # untouched


def test_non_grounded_verdict_on_hypothesized_node_skips_the_gate(engine):
    """The gate only fires for grounded — a hypothesized node can be rejected/obsoleted with no support
    (negative information / housekeeping, not a promotion)."""
    nid = _seed_hypothesized_node(engine)
    out = engine.kg_ground(nid, "rejected", kind="node")
    assert out["ok"] is True
    node = engine.canon.read_node(nid)
    assert node.epistemic_state == EpistemicState.REJECTED
    assert node.provenance == Provenance.HYPOTHESIZED  # not upgraded — no support, no promotion


def test_non_hypothesized_node_grounds_without_support(engine):
    """A normal (span-present) node still grounds with no support — the gate is hypothesized-only."""
    engine.canon.write_one(Node(id="plain", label="Plain", provenance=Provenance.SPAN_PRESENT))
    out = engine.kg_ground("plain", "grounded", kind="node")
    assert out["ok"] is True
    assert engine.canon.read_node("plain").epistemic_state == EpistemicState.GROUNDED


# ---- kind validation: a non-{node,edge} kind is rejected up front, not silently routed to edge ------
def test_invalid_kind_is_rejected_not_routed_to_edge(engine):
    out = engine.kg_ground("thermo-arrow", "grounded", kind="Node")
    assert out["ok"] is False
    assert "invalid kind" in out["error"]
    assert "expected node|edge" in out["error"]


def test_empty_kind_is_rejected(engine):
    out = engine.kg_ground("anything", "grounded", kind="")
    assert out["ok"] is False
    assert "invalid kind" in out["error"]


# ---- note-on-node: documented edge-only; passing it with kind='node' is accepted and ignored --------
def test_note_on_node_verdict_is_ignored_not_an_error(engine):
    """A `note` passed on a node verdict is documented edge-only (a Node has no notes field). It is
    silently ignored — the verdict still succeeds and the node carries no notes attribute."""
    nid = _seed_hypothesized_node(engine, "comp-note")
    out = engine.kg_ground(nid, "rejected", kind="node", note="vague: true only because generic")
    assert out["ok"] is True
    node = engine.canon.read_node(nid)
    assert not hasattr(node, "notes")  # Node has no notes field; the note is not persisted there


def test_kg_ground_wrapper_docstring_marks_note_edge_only(engine):
    """The MCP wrapper docstring must disclaim that `note` is edge-only (the inconsistency the finding
    flagged — the reference tools.md already documents it as edge-only)."""
    tools = _wrappers(engine)
    doc = (tools["kg_ground"].__doc__ or "").lower()
    assert "edge-only" in doc
    assert "note" in doc


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
