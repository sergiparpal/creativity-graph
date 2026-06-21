"""Domain pack + glossary (§Stage 2): teaches the engine the vocabulary and relation types.

A pack declares the node/edge types the theory actually uses, a glossary of defined terms, and
per-term specificity seeds (IDF over the corpus). Types absent from the pack are routed to the
``undeclared-type`` bucket by the boundary — never silently accepted.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .model import normalize_text


class PackContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str
    version: str = "0.1.0"
    node_types: list[str] = Field(min_length=1)
    edge_types: list[str] = Field(min_length=1)
    glossary: dict[str, str] = Field(default_factory=dict)        # term -> definition
    specificity_seeds: dict[str, float] = Field(default_factory=dict)  # term -> IDF/specificity

    @field_validator("node_types", "edge_types")
    @classmethod
    def _nonempty_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("types must be unique")
        if any(not t or not t.strip() for t in v):
            raise ValueError("type names must be non-empty")
        return v

    @field_validator("domain")
    @classmethod
    def _domain_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("domain must be non-empty")
        return v

    @field_validator("specificity_seeds")
    @classmethod
    def _seeds_finite(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [t for t, s in v.items() if not math.isfinite(s)]
        if bad:
            raise ValueError(f"specificity seeds must be finite numbers: {bad}")
        return v

    @model_validator(mode="after")
    def _types_disjoint(self) -> "PackContract":
        overlap = sorted(set(self.node_types) & set(self.edge_types))
        if overlap:
            raise ValueError(f"a type may not be both a node_type and an edge_type: {overlap}")
        return self


def load_pack(path: str | Path) -> PackContract:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return PackContract.model_validate(data)


def coverage(pack: PackContract, source_text: str) -> dict:
    """Fraction of the source's defined terms present in the glossary, and the converse.

    A 'defined term' in the source is heuristically any quoted phrase or bold/`code` term; we also
    measure how many glossary terms actually occur in the source (groundedness of the glossary).
    """
    norm_src = normalize_text(source_text)
    defined = _defined_terms(source_text)
    # ignore empty/whitespace glossary keys: normalize_text("") == "" is a substring of everything and
    # would otherwise count as "grounded" against any source, inflating the metric.
    glossary_norms = [n for n in (normalize_text(t) for t in pack.glossary) if n]
    glossary_terms = set(glossary_norms)
    in_glossary = sum(1 for t in defined if normalize_text(t) in glossary_terms)
    glossary_in_source = sum(1 for t in glossary_norms if t in norm_src)
    n_def = max(len(defined), 1)
    n_gloss = max(len(glossary_norms), 1)
    return {
        "source_defined_terms": len(defined),
        "glossary_terms": len(pack.glossary),
        "source_terms_in_glossary": in_glossary,
        "source_coverage": round(in_glossary / n_def, 3),
        "glossary_grounded_in_source": round(glossary_in_source / n_gloss, 3),
    }


# Quoted/bold/code "defined terms". Quote caps match the 60-char post-filter below so a long bold or
# quoted phrase isn't silently dropped by an inconsistent inner cap.
_DEFN_RE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\"([^\"]{3,60})\"|“([^”]{3,60})”")


def _defined_terms(text: str) -> set[str]:
    terms = set()
    for m in _DEFN_RE.finditer(text):
        term = next((g for g in m.groups() if g), "").strip()
        if term and len(term) <= 60:
            terms.add(term)
    return terms


def _main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"validate", "coverage"}:
        print("usage: python -m kg_engine.pack validate <pack.yaml> [source]", file=sys.stderr)
        return 2
    cmd = argv[0]
    path = argv[1] if len(argv) > 1 else "pack/pack.yaml"
    try:
        pack = load_pack(path)
    except Exception as e:  # noqa: BLE001
        print(f"PACK INVALID: {e}", file=sys.stderr)
        return 1
    print(f"PACK OK: domain={pack.domain!r} node_types={len(pack.node_types)} "
          f"edge_types={len(pack.edge_types)} glossary={len(pack.glossary)}")
    src = argv[2] if len(argv) > 2 else None
    if cmd == "coverage" or src:
        if not src:
            print("coverage needs a source path", file=sys.stderr)
            return 2
        cov = coverage(pack, Path(src).read_text(encoding="utf-8"))
        for k, v in cov.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
