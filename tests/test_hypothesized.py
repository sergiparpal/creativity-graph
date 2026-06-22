"""The hypothesized write lane (PLAN Stage 1): the boundary accepts span-less hypothesized items as
a distinct lane while keeping every span-present/inferred guarantee intact.

Stage 0 seeds only the import smoke; Stage 1 fills the lane tests.
"""
from __future__ import annotations

from kg_engine.boundary import validate_payload  # noqa: F401  (used by Stage 1 tests)


def test_boundary_importable():
    assert callable(validate_payload)
