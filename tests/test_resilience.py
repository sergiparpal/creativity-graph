"""Transport / cancellation resilience pass — the defense-in-depth that makes the MCP server
crash-proof, self-healing, idempotent, resumable, and projection-decoupled.

Covers (priority order mirrors the task):
  #1 — the tool envelope aborts ONLY the failing request (BrokenPipe/EOF/ConnectionReset become a
       structured result; the next call is served) and NEVER swallows cooperative cancellation
       (CancelledError/KeyboardInterrupt/SystemExit propagate). A partially-applied write is atomic.
  #3 — uncaught exceptions + handler errors land in <KG_DATA>/server.log with a full traceback, bounded
       by rotation.
  #4 — kg_status is fast + projection-FREE (never creates/refreshes the derived db) and reports coverage.
  #5 — re-sending an identical payload (with an idempotency key) replays the SAME receipt + counts and
       creates no duplicates; the receipt is deterministic from the payload.
  #6 — writes never trigger projection; a projection failure DEGRADES a read (flag) instead of raising.
  #7 — the handler watchdog trips on a wedged handler and forces a (clean, supervisor-relaunchable) exit.
  #8 — regression guard for the RULED-OUT cause: a full Projector.project(incremental=False) over a real
       fixture canon (networkx + igraph + leidenalg) completes without error and reads survive.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

import kg_engine.canon as canon_mod
import kg_engine.server as S
from kg_engine.model import EpistemicState


# --- a FakeMCP that captures the registered wrapper callables (no MCP client needed) --------------
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
    S._register(mcp, engine)
    return mcp.tools


# A real span from the conftest SOURCE, so the boundary ACCEPTS the edge (not REJECTED:span-not-in-source).
_SPAN = "A compression stands in for many observations and grounds the claims beneath it"


def _grounding_payload():
    return {
        "nodes": [{"id": "compression", "label": "compression", "node_type": "compression"},
                  {"id": "claim", "label": "claim", "node_type": "claim"}],
        "edges": [{"source": "compression", "relation": "grounds", "target": "claim",
                   "span": _SPAN, "provenance": "span-present"}],
    }


# ---------------------------------------------------------------------------------------------------
# #1 — per-request guard: survive a broken transport, serve the next request, never swallow cancel
# ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("exc", [BrokenPipeError("pipe"), EOFError("eof"),
                                 ConnectionResetError("reset"), RuntimeError("boom")])
def test_tool_envelope_survives_transport_error_and_serves_next(exc):
    """A BrokenPipeError / EOFError / ConnectionResetError (or any Exception) raised inside a handler
    becomes a structured {ok:False,...} result instead of bubbling into the serve loop, and the SAME
    wrapped tool serves the very next call — only that one request was aborted."""
    state = {"n": 0}

    @S._tool_result
    def kg_thing():
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        return {"ok": True, "served": state["n"]}

    first = kg_thing()
    second = kg_thing()
    assert first == {"ok": False, "error": str(exc), "error_kind": type(exc).__name__}
    assert second == {"ok": True, "served": 2}  # the server kept serving


@pytest.mark.parametrize("exc_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_tool_envelope_never_swallows_cooperative_cancellation(exc_type):
    """The envelope catches Exception, NOT BaseException: a CancelledError / KeyboardInterrupt /
    SystemExit MUST propagate so the framework's per-request cancel and process shutdown still work
    (swallowing a CancelledError would hang the cancel)."""
    @S._tool_result
    def kg_thing():
        raise exc_type()

    with pytest.raises(exc_type):
        kg_thing()


def test_partially_applied_write_is_atomic(engine, monkeypatch):
    """A write interrupted mid-batch (a crash between the per-file atomic writes) leaves the canon either
    fully committed or cleanly absent — never half-applied. Re-using the same crash-injection as the
    Stage-1 chaos suite, driven through the kg_write boundary."""
    real = canon_mod._atomic_write

    def boom(path, text):
        if path.name == "claim.md":
            raise OSError("simulated crash mid-write")
        return real(path, text)

    monkeypatch.setattr(canon_mod, "_atomic_write", boom)
    out = engine.kg_write(_grounding_payload())
    assert out["rolled_back"] is True
    assert out["written_nodes"] == []
    # nothing persisted: the batch was rolled back to the pre-write snapshot (scoped to its own files)
    assert not engine.canon.exists("compression")
    assert not engine.canon.exists("claim")
    # the dispositions never contradict rolled_back:True — the would-be-written counts are re-bucketed
    assert out["dispositions"]["ACCEPTED"] == 0
    assert out["dispositions"].get("rolled_back", 0) >= 1


# ---------------------------------------------------------------------------------------------------
# #5 — idempotent write receipt
# ---------------------------------------------------------------------------------------------------
def test_idempotent_write_same_receipt_no_duplicates(engine, monkeypatch):
    # spy on the actual canon write so the replay's no-op is proven, not assumed (the dispositions of a
    # replay trivially equal the original because it returns the cached object — so assert the WRITE side).
    real_write = engine.canon.write_nodes
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real_write(*a, **k)

    monkeypatch.setattr(engine.canon, "write_nodes", counting)
    payload = _grounding_payload()
    r1 = engine.kg_write(payload, idempotency_key="abc")
    assert calls["n"] == 1                       # the first call actually wrote
    r2 = engine.kg_write(payload, idempotency_key="abc")
    assert calls["n"] == 1                       # NON-VACUOUS: the replay did NOT re-enter the write path
    assert r2["idempotent_replay"] is True
    assert "idempotent_replay" not in r1         # the first call is a real write, not a replay
    assert r1["receipt"] == r2["receipt"]
    assert r2["dispositions"]["ACCEPTED"] == r1["dispositions"]["ACCEPTED"] >= 1
    # the receipt is deterministic from the payload alone (no key, fresh static call)
    assert S.KGEngine._payload_receipt(payload) == r1["receipt"]
    # and no duplicate edges were created by the retry
    assert len([e for e in engine.canon.all_edges() if e.relation == "grounds"]) == 1


def test_idempotency_key_reuse_with_different_payload_is_not_dropped(engine):
    """Reusing a key with a DIFFERENT payload (a caller contract violation) must NOT silently drop the
    second write or replay the stale receipt — it processes the new payload normally."""
    first = _grounding_payload()
    second = {
        "nodes": [{"id": "degree", "label": "degree", "node_type": "metric"},
                  {"id": "betweenness", "label": "betweenness", "node_type": "metric"}],
        "edges": [{"source": "degree", "relation": "approximates", "target": "betweenness",
                   "span": "Degree approximates importance", "provenance": "span-present"}],
    }
    r1 = engine.kg_write(first, idempotency_key="dup")
    r2 = engine.kg_write(second, idempotency_key="dup")
    assert r2.get("idempotent_replay") is not True       # not a replay of the first
    assert r2["receipt"] != r1["receipt"]                 # the new payload's own receipt
    # BOTH writes landed — the second was not dropped
    rels = {e.relation for e in engine.canon.all_edges()}
    assert {"grounds", "approximates"} <= rels


def test_idempotent_replay_without_key_still_dedups(engine):
    """Even WITHOUT a key, a re-send is idempotent by canonical id (the boundary dedups), so no
    duplicates appear — the key only additionally guarantees identical RESPONSE counts."""
    payload = _grounding_payload()
    engine.kg_write(payload)
    engine.kg_write(payload)
    assert len([e for e in engine.canon.all_edges() if e.relation == "grounds"]) == 1


def test_rolled_back_write_is_not_cached(engine, monkeypatch):
    """A rolled-back batch must NOT be cached under its key: a retry should be allowed to actually
    write, not replay the transient failure."""
    real = canon_mod._atomic_write
    fail = {"on": True}

    def maybe_boom(path, text):
        if fail["on"] and path.name == "claim.md":
            raise OSError("transient")
        return real(path, text)

    monkeypatch.setattr(canon_mod, "_atomic_write", maybe_boom)
    r1 = engine.kg_write(_grounding_payload(), idempotency_key="k")
    assert r1["rolled_back"] is True
    fail["on"] = False  # the transient failure clears
    r2 = engine.kg_write(_grounding_payload(), idempotency_key="k")
    assert r2.get("idempotent_replay") is not True  # not a replay of the failure
    assert r2["rolled_back"] is False
    assert engine.canon.exists("claim")


# ---------------------------------------------------------------------------------------------------
# #4 — kg_status: projection-free + coverage
# ---------------------------------------------------------------------------------------------------
def test_kg_status_is_projection_free(engine, monkeypatch):
    """kg_status must read the canon only — never trigger or refresh the derived db. Spy on project():
    it must not be called, and the derived index must not be created on disk."""
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        raise AssertionError("kg_status triggered a projection!")

    monkeypatch.setattr(engine.projector, "project", spy)
    engine.kg_write(_grounding_payload())
    st = engine.kg_status()
    assert calls["n"] == 0
    assert not (engine.data_dir / "derived" / "index.sqlite").exists()
    assert st["derived_present"] is False
    assert st["nodes"] == 2 and st["edges"] == 1
    assert st["unverified_edges"] == 1
    assert st["edges_by_epistemic_state"].get("unverified") == 1


def test_kg_status_reports_section_coverage(vault, tmp_path):
    """Coverage marks which source `##` sections already have an ANCHORED edge — the resume signal."""
    src = tmp_path / "source.md"
    src.write_text("# T\n\n## Alpha\nA compression grounds the claims beneath it.\n\n"
                   "## Beta\nDegree approximates importance.\n", encoding="utf-8")
    from pathlib import Path
    pack = Path(__file__).resolve().parents[1] / "pack" / "pack.yaml"
    eng = S.KGEngine(vault, source_path=src, pack_path=pack)
    out = eng.kg_write({
        "nodes": [{"id": "compression", "label": "compression", "node_type": "compression"},
                  {"id": "claim", "label": "claim", "node_type": "claim"}],
        "edges": [{"source": "compression", "relation": "grounds", "target": "claim",
                   "span": "A compression grounds the claims beneath it", "provenance": "span-present"}],
    })
    assert out["dispositions"]["ACCEPTED"] >= 1  # guard: the fixture write actually landed
    cov = {s["title"]: s["covered"] for s in eng.kg_status()["coverage"]["sections"]}
    assert cov["Alpha"] is True
    assert cov["Beta"] is False  # no anchored edge there yet -> not extracted


def test_kg_status_registered_in_tool_surface(engine):
    tools = _wrappers(engine)
    assert "kg_status" in tools
    out = tools["kg_status"]()
    assert out["ok"] is True and "coverage" in out


# ---------------------------------------------------------------------------------------------------
# #6 — projection decoupled from writes; a projection failure degrades a read instead of raising
# ---------------------------------------------------------------------------------------------------
def test_writes_never_trigger_projection(engine, monkeypatch):
    """kg_write / kg_propose / kg_ground touch only the canon — a broken projector must not block or
    fail a write."""
    def boom(*a, **k):
        raise RuntimeError("projection must not be on the write path")

    monkeypatch.setattr(engine.projector, "project", boom)
    w = engine.kg_write(_grounding_payload())
    assert w["rolled_back"] is False and w["dispositions"]["ACCEPTED"] >= 1
    # a hypothesized propose + a verdict also avoid projection
    p = engine.kg_propose({"nodes": [{"id": "idea", "label": "idea", "node_type": "compression"}]})
    assert p["rolled_back"] is False
    g = engine.kg_ground("e_compression__grounds__claim", "grounded")
    assert g["ok"] is True
    # the fourth canon-only write, kg_rename, must also avoid projection
    rn = engine.kg_rename("claim", "claim_renamed")
    assert rn["ok"] is True


def test_degraded_flag_surfaced_on_get_node_and_shortest_path(engine, monkeypatch):
    """A degraded derived layer must not masquerade as a genuine 'not found' / 'no path' on the structural
    reads — get_node (incl. on a miss) and shortest_path carry the projection_degraded flag (review-M2)."""
    engine.kg_write(_grounding_payload())

    def boom(*a, **k):
        raise RuntimeError("boom-projection")

    monkeypatch.setattr(engine.projector, "project", boom)
    node = engine.get_node("compression")  # exists in canon but the degraded derived layer is empty
    assert isinstance(node, dict) and "projection_degraded" in node
    sp = {"path": engine.shortest_path("compression", "claim")}
    if engine._projection_degraded:
        sp["projection_degraded"] = engine._projection_degraded
    assert "projection_degraded" in sp
    # exercise the MCP wrapper path too (it is where shortest_path's flag is attached)
    tools = _wrappers(engine)
    assert "projection_degraded" in tools["shortest_path"]("compression", "claim")
    gn = tools["get_node"]("compression")
    assert "projection_degraded" in gn


def test_projection_failure_degrades_read_not_crash(engine, monkeypatch):
    """A reprojection that raises must DEGRADE the read (canon-derived/empty data + a projection_degraded
    flag), never crash the tool."""
    engine.kg_write(_grounding_payload())

    def boom(*a, **k):
        raise RuntimeError("boom-projection")

    monkeypatch.setattr(engine.projector, "project", boom)
    ctx = engine.kg_context("compression")  # must not raise
    assert isinstance(ctx, dict)
    assert "boom-projection" in (ctx.get("projection_degraded") or "")
    assert engine._projection_degraded and "boom-projection" in engine._projection_degraded
    # query_graph degrades too, and kg_status echoes the flag
    assert "projection_degraded" in engine.query_graph()
    assert "boom-projection" in (engine.kg_status()["projection_degraded"] or "")


# ---------------------------------------------------------------------------------------------------
# #3 — rotating server log captures tracebacks; rotation cap holds
# ---------------------------------------------------------------------------------------------------
def test_configure_logging_writes_traceback(tmp_path):
    path = S.configure_logging(tmp_path)
    try:
        assert path == tmp_path / "server.log"
        try:
            raise ValueError("boom-traceback-marker")
        except ValueError:
            S.logger.error("handler failed", exc_info=True)
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "server.log").read_text(encoding="utf-8")
        assert "handler failed" in text
        assert "boom-traceback-marker" in text
        assert "Traceback (most recent call last)" in text
    finally:
        _detach_kg_log_handlers()


def test_handler_exception_lands_in_server_log_via_tool_envelope(tmp_path, monkeypatch, engine):
    """END-TO-END for verification #3: an exception raised inside a real tool, routed through the actual
    _tool_result envelope, lands in <KG_DATA>/server.log with the tool name AND a full traceback (not just
    a direct logger.error call)."""
    S.configure_logging(tmp_path)
    try:
        def raiser():
            raise RuntimeError("boom-handler-marker")

        monkeypatch.setattr(engine, "kg_metrics", raiser)
        tools = _wrappers(engine)
        out = tools["kg_metrics"]()                      # goes through @_tool_result
        assert out["ok"] is False and out["error_kind"] == "RuntimeError"
        for h in logging.getLogger().handlers:
            h.flush()
        text = (tmp_path / "server.log").read_text(encoding="utf-8")
        assert "kg_metrics" in text                      # the tool name the envelope logs
        assert "boom-handler-marker" in text             # the exception message
        assert "Traceback (most recent call last)" in text
    finally:
        _detach_kg_log_handlers()


def test_configure_logging_is_idempotent(tmp_path):
    S.configure_logging(tmp_path)
    S.configure_logging(tmp_path)
    try:
        kg = [h for h in logging.getLogger().handlers if getattr(h, "_kg_server_log", False)]
        assert len(kg) == 1  # repeated calls replace, never accumulate
    finally:
        _detach_kg_log_handlers()


def test_server_log_rotation_cap_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SERVER_LOG_MAX_BYTES", 1024)
    monkeypatch.setattr(S, "SERVER_LOG_BACKUP_COUNT", 2)
    S.configure_logging(tmp_path)
    try:
        for i in range(2000):
            S.logger.warning("rotation-line-%05d padding-padding-padding-padding", i)
        for h in logging.getLogger().handlers:
            h.flush()
        logs = sorted(p.name for p in tmp_path.glob("server.log*"))
        # at most the live file + BACKUP_COUNT rotated copies
        assert len(logs) <= 1 + 2, logs
        for p in tmp_path.glob("server.log*"):
            assert p.stat().st_size <= 1024 * 4  # bounded near maxBytes (one record may overrun slightly)
    finally:
        _detach_kg_log_handlers()


def _detach_kg_log_handlers():
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_kg_server_log", False):
            root.removeHandler(h)
            h.close()


# ---------------------------------------------------------------------------------------------------
# #7 — handler watchdog
# ---------------------------------------------------------------------------------------------------
def test_watchdog_trips_on_wedged_handler_and_names_it():
    tripped = []
    wd = S._Watchdog(timeout=0.05, on_trip=lambda: tripped.append(True), poll=0.02).start()
    try:
        wd.enter("kg_write")
        deadline = time.monotonic() + 2.0
        while not tripped and time.monotonic() < deadline:
            time.sleep(0.02)
        assert tripped == [True]
        hit = wd.overdue()
        assert hit and hit[0] == "kg_write"
    finally:
        wd.exit()
        wd.stop()


def test_watchdog_does_not_trip_on_fast_handler():
    tripped = []
    wd = S._Watchdog(timeout=1.0, on_trip=lambda: tripped.append(True), poll=0.02).start()
    try:
        wd.enter("kg_ping")
        time.sleep(0.05)
        wd.exit()
        time.sleep(0.1)
        assert tripped == []
    finally:
        wd.stop()


def test_tool_envelope_feeds_active_watchdog(monkeypatch):
    """The tool envelope enters/exits the active watchdog around every handler — even when it raises —
    so a wedged handler is observed, without changing any wrapper signature."""
    class FakeWD:
        def __init__(self):
            self.entered, self.exits = [], 0

        def enter(self, name):
            self.entered.append(name)

        def exit(self):
            self.exits += 1

    fake = FakeWD()
    monkeypatch.setattr(S, "_WATCHDOG", fake)

    @S._tool_result
    def kg_boom():
        raise RuntimeError("x")

    kg_boom()
    assert fake.entered == ["kg_boom"]
    assert fake.exits == 1  # exit() runs in finally even on error


def test_disabled_watchdog_does_not_start():
    wd = S._Watchdog(timeout=0).start()
    assert wd._thread is None


# ---------------------------------------------------------------------------------------------------
# #8 — REGRESSION GUARD for the ruled-out cause: a full projection over a real fixture canon
# ---------------------------------------------------------------------------------------------------
def test_full_projection_over_fixture_canon_completes(engine):
    """Documents the diagnosed-and-ruled-out cause: the native deps + community detection are NOT the
    crash. Import networkx/igraph/leidenalg and run a FULL (non-incremental) projection over a populated
    fixture canon — it must complete without error, and the read tools must survive."""
    pytest.importorskip("networkx")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    import networkx, igraph, leidenalg  # noqa: F401 — assert they actually import/load

    # Pin the NATIVE Leiden partition directly, bypassing projector._leiden's try/except fallback: a
    # broken native dep raises HERE, where the full projection below would silently degrade to label
    # propagation and hide it. This is what makes the guard actually rule out the native-dep hypothesis.
    import igraph as ig
    import leidenalg as la
    _g = ig.Graph(n=3, edges=[(0, 1), (1, 2)], directed=False)
    _part = la.find_partition(_g, la.RBConfigurationVertexPartition, seed=42)
    assert len(_part.membership) == 3

    engine.kg_write(_grounding_payload())
    engine.kg_write({
        "nodes": [{"id": "betweenness", "label": "betweenness", "node_type": "metric"},
                  {"id": "degree", "label": "degree", "node_type": "metric"}],
        "edges": [{"source": "degree", "relation": "approximates", "target": "betweenness",
                   "span": "Degree approximates importance", "provenance": "span-present"}],
    })
    report = engine.projector.project(incremental=False)
    assert report is not None
    # reads survive a full rebuild
    assert isinstance(engine.query_graph(), dict)
    assert isinstance(engine.kg_context("compression"), dict)
    assert engine.kg_metrics()["nodes"] >= 2


# ---------------------------------------------------------------------------------------------------
# small: data-dir / log-path resolution mirrors the engine
# ---------------------------------------------------------------------------------------------------
def test_resolve_data_dir_honours_kg_data(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_DATA", str(tmp_path / "d"))
    assert S.resolve_data_dir() == tmp_path / "d"
    assert S.server_log_path() == tmp_path / "d" / "server.log"


def test_resolve_data_dir_defaults_under_project(monkeypatch, tmp_path):
    monkeypatch.delenv("KG_DATA", raising=False)
    monkeypatch.setenv("KG_PROJECT_DIR", str(tmp_path / "proj"))
    assert S.resolve_data_dir() == tmp_path / "proj" / ".kg-data"
