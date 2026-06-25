"""Stage 2: the domain pack validates against PackContract and a coverage check is computable."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kg_engine.pack import PackContract, _main, coverage, load_pack

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


def test_type_names_stripped_before_storage():
    # finding [1]: YAML-preserved stray whitespace (a quoted entry / NBSP) must be stripped before
    # storage so the stored vocabulary equals what the boundary matches by exact membership.
    pack = PackContract(domain="x", node_types=["claim ", " grounds"], edge_types=["supports\t"])
    assert pack.node_types == ["claim", "grounds"]
    assert pack.edge_types == ["supports"]
    # the boundary compares against the stripped value, so the clean extractor type now matches.
    assert "claim" in set(pack.node_types)


def test_whitespace_variant_duplicate_rejected():
    # finding [1]: 'claim' and ' claim ' are the same logical type post-strip — the dup guard must fire.
    with pytest.raises(ValidationError):
        PackContract(domain="x", node_types=["claim", " claim "], edge_types=["grounds"])


def test_glossary_terms_count_matches_ratio_population():
    # finding [2]: a whitespace-only glossary key is excluded from the grounding ratio's denominator,
    # so it must NOT be counted in glossary_terms — count and ratio must refer to the same population.
    pack = PackContract(
        domain="x",
        node_types=["claim"],
        edge_types=["grounds"],
        glossary={"compression": "x", "   ": "y"},
    )
    cov = coverage(pack, "compression here")
    assert cov["glossary_terms"] == 1  # the whitespace-only key is dropped, not counted
    assert cov["glossary_grounded_in_source"] == 1.0


def test_coverage_without_source_does_not_print_pack_ok(capsys):
    # finding [3]: `coverage` missing its required source must not emit the PACK OK success line.
    rc = _main(["coverage", str(PACK)])
    out = capsys.readouterr()
    assert rc == 2
    assert "PACK OK" not in out.out
    assert "coverage needs a source path" in out.err


def test_validate_prints_pack_ok(capsys):
    # the PACK OK line is still emitted on a successful validate (no regression of the success path).
    rc = _main(["validate", str(PACK)])
    out = capsys.readouterr()
    assert rc == 0
    assert "PACK OK" in out.out
