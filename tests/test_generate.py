"""Generative layer (PLAN Stage 3): deterministic generators emit well-formed hypothesized
Candidates, generality-controlled, never colliding with failure memory.

Stage 0 seeds only the import smoke; Stage 3 fills the mechanism tests.
"""
from __future__ import annotations

from kg_engine.generate import Candidate


def test_candidate_dataclass_importable():
    c = Candidate(kind="edge", mechanism="bridge", source="a", target="b", relation="bridges")
    assert c.kind == "edge" and c.mechanism == "bridge"
    # provenance/epistemic/span are NOT fields — they are forced by the propose lane, never carried.
    d = c.to_dict()
    assert "span" not in d and "provenance" not in d and "epistemic_state" not in d
