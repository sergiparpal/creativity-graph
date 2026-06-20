"""Stage 2: the domain pack validates against PackContract and a coverage check is computable."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kg_engine.pack import PackContract, coverage, load_pack

PACK = Path(__file__).resolve().parents[1] / "pack" / "pack.yaml"
SRC = Path(__file__).resolve().parents[1] / "examples" / "source.md"


def test_pack_validates():
    pack = load_pack(PACK)
    assert pack.domain
    assert "grounds" in pack.edge_types and "compression" in pack.node_types
    assert pack.glossary  # non-empty glossary


def test_pack_rejects_duplicate_types():
    with pytest.raises(ValidationError):
        PackContract(domain="x", node_types=["a", "a"], edge_types=["b"])


def test_pack_rejects_extra_fields():
    with pytest.raises(ValidationError):
        PackContract(domain="x", node_types=["a"], edge_types=["b"], bogus=1)


def test_coverage_reports_fractions():
    pack = load_pack(PACK)
    cov = coverage(pack, SRC.read_text())
    assert 0.0 <= cov["glossary_grounded_in_source"] <= 1.0
    # most glossary terms are drawn from the source, so groundedness should be substantial
    assert cov["glossary_grounded_in_source"] >= 0.5, cov
    assert cov["glossary_terms"] == len(pack.glossary)
