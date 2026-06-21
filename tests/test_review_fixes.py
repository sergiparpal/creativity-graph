"""Regression tests for the review-findings fix pass.

Each test pins a previously-unguarded behaviour: invariant bypasses (span-present, never-forge-state),
projection correctness (parallel edges, datetime frontmatter), content-aware staleness, the reconciler
replay defence, the scrubber placeholder namespace, rename data-safety, and input hardening.
"""
from __future__ import annotations

import json

from kg_engine.boundary import MIN_EDGE_BUDGET, validate_payload
from kg_engine.model import (Disposition, Edge, EpistemicState, Node, edge_id,
                             node_from_markdown, node_to_markdown, span_verifies)
from kg_engine.projector import Projector
from kg_engine.reconciler import GROUND_AUDIT, Reconciler
from kg_engine.scrub import Scrubber

SRC = ("Heat flows from hot to cold. Entropy grounds the arrow of time. "
       "A compression stands in for many observations and grounds the claims beneath it. "
       "Degree approximates importance.")


# ---- C1: span-present cannot be bypassed by a forged `deterministic` authorship claim ----
def test_deterministic_claim_cannot_bypass_span(pack):
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds", "authored_by": "deterministic"}]},
        pack=pack, source_text=SRC)
    e = next(r for r in res if r.kind == "edge")
    assert e.disposition == Disposition.REJECTED and e.reason == "no-supporting-span"
    assert e.item.authored_by.value == "agent"


# ---- a non-`unverified` state (e.g. obsolete) is stripped on a write ----
def test_obsolete_state_is_stripped_on_write(pack):
    res = validate_payload(
        {"edges": [{"source": "a", "target": "b", "relation": "grounds",
                    "span": "Heat flows from hot to cold",
                    "epistemic_state": "obsolete", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = next(r for r in res if r.kind == "edge")
    assert e.item.epistemic_state == EpistemicState.UNVERIFIED
    assert "forged-verdict-stripped" in e.reason


# ---- node floods are capped the same way edge floods are ----
def test_node_flood_is_capped():
    payload = {"nodes": [{"label": f"n{i}", "node_type": "compression"}
                         for i in range(MIN_EDGE_BUDGET + 5)]}
    res = validate_payload(payload, source_text="x", pack=None)
    flooded = [r for r in res if r.kind == "node" and r.reason == "rate-limited-flood"]
    written = [r for r in res if r.kind == "node" and r.written]
    assert len(written) == MIN_EDGE_BUDGET and len(flooded) == 5


# ---- Unicode NFC / zero-width: a verbatim span in another composition form still verifies ----
def test_span_verifies_across_unicode_forms():
    nfc = "café grounds entropy"   # é as U+00E9
    nfd = "café grounds entropy"  # é as e + U+0301 combining accent
    assert span_verifies(nfc, nfd) and span_verifies(nfd, nfc)
    assert span_verifies("hot to cold", "hot​ to cold")  # zero-width space ignored


# ---- a human-edited unquoted timestamp must not crash projection / staleness ----
def test_unquoted_timestamp_does_not_crash_projection(canon):
    (canon.notes_dir / "x.md").write_text(
        "---\nid: x\nlabel: X\ncreated_at: 2026-06-20T10:00:00\n"
        "updated_at: 2026-06-20T10:00:00\nedges: []\n---\nbody\n", encoding="utf-8")
    proj = Projector(canon)
    rep = proj.project()  # would raise TypeError on json.dumps(datetime) before the fix
    assert rep.n_nodes >= 1
    assert proj.is_stale() in (True, False)  # is_stale hashes frontmatter — must be computable


# ---- MultiDiGraph: two edges sharing (source,target) but differing in relation both survive ----
def test_parallel_typed_edges_survive_projection(canon):
    canon.write_nodes([
        Node(id="a", label="A", edges=[
            Edge(source="a", target="b", relation="grounds", span="s"),
            Edge(source="a", target="b", relation="attacked_by", span="s"),
        ]),
        Node(id="b", label="B"),
    ], message="seed")
    proj = Projector(canon)
    rep = proj.project()
    assert rep.n_edges == 2
    data = json.loads(proj.graph_path.read_text())
    assert sorted(e["relation"] for e in data["links"]) == ["attacked_by", "grounds"]


# ---- one malformed note is skipped, not fatal, for reads and the reconciler sweep ----
def test_malformed_note_does_not_crash_reads(canon):
    (canon.notes_dir / "good.md").write_text(node_to_markdown(Node(id="good", label="Good")),
                                             encoding="utf-8")
    (canon.notes_dir / "bad.md").write_text("---\nfoo: [1, 2\n---\nbody\n", encoding="utf-8")  # bad YAML
    assert "good" in {n.id for n in canon.all_nodes()}
    assert Reconciler(canon).scan(full_sweep=True).scanned >= 2  # survives the malformed note


# ---- the reconciler catches a REPLAYED (already-audited) verdict ----
def test_replayed_verdict_is_requarantined(canon):
    edge = Edge(source="a", target="b", relation="grounds", span="x")
    canon.write_nodes([Node(id="a", label="A", edges=[edge])], message="seed")
    recon = Reconciler(canon)
    recon.scan(full_sweep=True)  # baseline

    def set_state(state):
        node = node_from_markdown(canon.node_path("a").read_text())
        node.edges[0].epistemic_state = state
        canon.node_path("a").write_text(node_to_markdown(node))

    # one legitimate grounding: audit record THEN flip -> not requarantined (consumes the record)
    (canon.root / GROUND_AUDIT).write_text(
        json.dumps({"key": edge.id, "from": "unverified", "to": "grounded", "by": "agent"}) + "\n")
    set_state(EpistemicState.GROUNDED)
    assert edge.id not in recon.scan(full_sweep=True).requarantined

    # revert, then REPLAY grounded out-of-band with NO new audit record -> caught
    set_state(EpistemicState.UNVERIFIED)
    recon.scan(full_sweep=True)
    set_state(EpistemicState.GROUNDED)
    assert edge.id in recon.scan(full_sweep=True).requarantined


# ---- kg_rename rewrites every endpoint + edge id, removes the old note, no dangling duplicates ----
def test_kg_rename_rewrites_and_preserves(engine):
    engine.kg_write({
        "nodes": [{"id": "old", "label": "Old"}, {"id": "x", "label": "X"}],
        "edges": [
            {"source": "old", "target": "x", "relation": "grounds",
             "span": "Heat flows from hot to cold", "authored_by": "agent"},
            {"source": "x", "target": "old", "relation": "approximates",
             "span": "Degree approximates importance", "authored_by": "agent"},
        ]})
    out = engine.kg_rename("old", "new")
    assert out["ok"] is True
    assert not engine.canon.exists("old") and engine.canon.exists("new")
    edges = engine.canon.all_edges()
    assert all("old" not in (e.source, e.target) for e in edges)          # no stale endpoint
    assert all(e.id == edge_id(e.source, e.relation, e.target) for e in edges)  # ids match endpoints
    x_edges = engine.canon.read_node("x").edges
    assert sorted((e.source, e.target) for e in x_edges) == [("x", "new")]  # no dangling x->old


# ---- the scrubber keeps ONE placeholder namespace across calls (no cross-call collision) ----
def test_scrubber_no_cross_call_collision():
    s = Scrubber("medium")
    _, m1 = s.scrub("mail alice@a.com")
    _, m2 = s.scrub("mail bob@b.com")
    assert set(m1).isdisjoint(set(m2))  # distinct placeholders across calls
    acc = {**m1, **m2}
    assert Scrubber.restore("mail ⟦EMAIL:1⟧", acc) == "mail alice@a.com"


# ---- a kg_ground verdict (write_one, no commit) is visible to reads (content-aware staleness) ----
def test_ground_verdict_visible_to_reads_without_commit(engine):
    engine.kg_write({"edges": [{
        "source": "compression", "target": "claim", "relation": "grounds",
        "span": "A compression stands in for many observations and grounds the claims beneath it",
        "authored_by": "agent"}]})
    eid = edge_id("compression", "grounds", "claim")
    engine.get_node("compression")           # prime the derived layer
    engine.kg_ground(eid, "grounded")        # write_one, no commit -> HEAD unchanged
    nb = engine.get_neighbors("compression")
    assert next(e["epistemic_state"] for e in nb if e["id"] == eid) == "grounded"


# ---- the engine clamps the verdict author; a stray `by` can't masquerade as a verdict author ----
def test_kg_ground_clamps_unknown_author(engine):
    engine.kg_write({"edges": [{
        "source": "degree", "target": "importance", "relation": "approximates",
        "span": "Degree approximates importance", "authored_by": "agent"}]})
    eid = edge_id("degree", "approximates", "importance")
    out = engine.kg_ground(eid, "grounded", by="root")  # bogus author
    assert out["by"] == "agent"
