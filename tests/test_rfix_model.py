"""Regression tests for the model_boundary fix group.

Covers:
  1. Edge.from_dict drops None-valued keys (a hand-edited `source: null` / `relation: null` no longer
     coerces to the literal string "None"); a null `source` resolves to the owning-node id.
  2. The degenerate-endpoint guard rejects an underscore-only endpoint (slug() collapses '_' to the
     "node" fallback, which would alias distinct edges).
  3. Non-fatal slug-collision detection: two labels differing beyond separator/case that collapse onto
     one node id surface a warning, and slug() stays deterministically non-injective on identity.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from kg_engine.boundary import validate_payload
from kg_engine.model import Disposition, Edge, Node, slug
from kg_engine.pack import load_pack


@pytest.fixture
def pack():
    # self-contained (does not rely on the shared conftest fixture) so this group's regression suite
    # runs even if an unrelated module fails to import.
    return load_pack(Path(__file__).resolve().parents[1] / "pack" / "pack.yaml")

SRC = ("A compression grounds the claims beneath it. Betweenness is confounded by the generality "
       "confound. Heat flows from hot to cold.")


def _by_target(results):
    return {r.item.target: r for r in results if r.kind == "edge"}


def _nodes(results):
    return [r for r in results if r.kind == "node"]


# ---------------------------------------------------------------- fix #1: None-valued keys dropped

def test_edge_from_dict_null_source_resolves_to_owner():
    e = Edge.from_dict({"source": None, "target": "b", "relation": "grounds"}, source="owner-node")
    assert e.source == "owner-node"        # resolved default applies, not the null
    assert e.source != "None"              # the old bug: str(None) -> "None"


def test_edge_from_dict_missing_source_still_resolves():
    # the pre-existing behavior (absent key -> resolved source) is preserved by the drop+setdefault
    e = Edge.from_dict({"target": "b", "relation": "grounds"}, source="owner-node")
    assert e.source == "owner-node"


def test_node_edge_null_source_resolves_via_node():
    # the real integration path: Node.__post_init__ builds edges via Edge.from_dict(e, source=self.id)
    n = Node(id="owner", edges=[{"source": None, "target": "b", "relation": "grounds", "span": "x"}])
    assert n.edges[0].source == "owner"
    assert n.edges[0].source != "None"


def test_edge_from_dict_null_optional_key_falls_back_to_default():
    # a null on a defaulted optional key drops to the dataclass default instead of the string "None"
    e = Edge.from_dict({"source": "a", "target": "b", "relation": "grounds", "notes": None})
    assert e.notes == ""


# ------------------------------------------------------- fix #2: underscore-only endpoint rejected

def test_underscore_only_source_rejected(pack):
    res = validate_payload(
        {"edges": [{"source": "_", "target": "b", "relation": "grounds",
                    "span": "A compression grounds the claims beneath it", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.REJECTED and e.reason == "empty-source"


def test_underscore_only_relation_rejected(pack):
    res = validate_payload(
        {"edges": [{"source": "compression", "target": "b", "relation": "___",
                    "span": "A compression grounds the claims beneath it", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    e = _by_target(res)["b"]
    assert e.disposition == Disposition.REJECTED and e.reason == "empty-relation"


def test_underscore_aliasing_premise():
    # documents WHY the guard must reject: '_' matches \w (old test) but slug() collapses it to "node"
    assert re.search(r"\w", "_") is not None          # the old, too-lenient test would pass '_'
    assert re.search(r"[^\W_]", "_") is None           # the new test correctly fails '_'
    assert slug("_") == "node"                          # so an underscore endpoint aliases


def test_word_endpoint_still_accepted(pack):
    # guard must not over-reject: a real content endpoint is still fine
    res = validate_payload(
        {"edges": [{"source": "compression", "target": "claim", "relation": "grounds",
                    "span": "A compression grounds the claims beneath it", "authored_by": "agent"}]},
        pack=pack, source_text=SRC)
    assert _by_target(res)["claim"].disposition == Disposition.ACCEPTED


# ------------------------------------------------------ fix #3: slug-collision detection + determinism

def test_slug_non_injective_deterministic():
    # identity MUST stay stable/non-injective on trailing punctuation (do NOT change slug output)
    assert slug("C++") == slug("C#") == slug("C") == "c"


def test_colliding_labels_flag_warning():
    res = validate_payload(
        {"nodes": [{"label": "C++"}, {"label": "C"}]}, pack=None, source_text="")
    nodes = _nodes(res)
    assert all(n.item.id == "c" for n in nodes)                      # identity unchanged, both -> 'c'
    assert any("slug-collision-warning" in n.reason for n in nodes)  # collapse surfaced
    # never rejected on the warning alone
    assert all(n.disposition != Disposition.REJECTED for n in nodes)


def test_separator_only_variants_do_not_flag():
    # slug's INTENDED unification ('a b' / 'a-b' both -> 'a-b') must not raise a false collision
    res = validate_payload(
        {"nodes": [{"label": "a b"}, {"label": "a-b"}]}, pack=None, source_text="")
    nodes = _nodes(res)
    assert all(n.item.id == "a-b" for n in nodes)
    assert all("slug-collision-warning" not in n.reason for n in nodes)
