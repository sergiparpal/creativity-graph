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
        # Strip BEFORE the uniqueness/empty checks and store the stripped form: the boundary matches
        # types by exact set membership, so a YAML-preserved stray space (e.g. quoted `"claim "` or a
        # trailing NBSP) must not survive into the stored vocabulary — otherwise every item of that
        # type is silently QUARANTINED, and the whitespace variant would also defeat the dup guard.
        v = [t.strip() for t in v]
        if any(not t for t in v):
            raise ValueError("type names must be non-empty")
        if len(set(v)) != len(v):
            raise ValueError("types must be unique")
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
    # word-boundary match, not raw substring: a short term ('io','ml','a') must not count as grounded
    # because it appears INSIDE an unrelated word ('ratio','html'), which inflated the metric (model-pack-4).
    glossary_in_source = sum(1 for t in glossary_norms if _term_in_text(t, norm_src))
    return {
        "source_defined_terms": len(defined),
        # the count must match the population `glossary_grounded_in_source` is scored over: report the
        # non-empty post-normalization terms, not the raw dict (a whitespace-only key was excluded from
        # the ratio's denominator and would otherwise inflate the count against the ratio).
        "glossary_terms": len(glossary_norms),
        "source_terms_in_glossary": in_glossary,
        "source_coverage": _safe_ratio(in_glossary, len(defined)),
        "glossary_grounded_in_source": _safe_ratio(glossary_in_source, len(glossary_norms)),
    }


def _safe_ratio(num: int, denom: int, ndigits: int = 3) -> float:
    """num/denom rounded to ndigits, guarding a zero denominator (an empty population reads as 0.0)."""
    return round(num / max(denom, 1), ndigits)


def _term_in_text(term: str, text: str) -> bool:
    """True iff `term` occurs in `text` not embedded inside a larger word (so 'io' matches 'io' but not
    'ratio'). Uses `\\w`-boundary lookarounds; this is exact for a term that BEGINS and ENDS with a word
    character. For a term whose own first/last char is itself a non-word char, the boundary check is
    applied against that punctuation, so matching there is best-effort rather than guaranteed (review-nit)."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


# Quoted/bold/code "defined terms". The quote alternatives carry a 3-char floor; bold/code do not, so
# `**a**` yields the 1-char term 'a' while `"a"` is dropped — the quote floor is intentional, since a
# bare bold/code marker is a stronger defined-term signal than a 1-2 char quoted fragment.
_MIN_TERM_LEN = 3
_MAX_TERM_LEN = 60
_DEFN_RE = re.compile(
    rf"\*\*(.+?)\*\*|`([^`]+)`"
    rf"|\"([^\"]{{{_MIN_TERM_LEN},{_MAX_TERM_LEN}}})\""
    rf"|“([^”]{{{_MIN_TERM_LEN},{_MAX_TERM_LEN}}})”"
)


def _defined_terms(text: str) -> set[str]:
    terms = set()
    for m in _DEFN_RE.finditer(text):
        term = next((g for g in m.groups() if g), "").strip()
        if term and len(term) <= _MAX_TERM_LEN:
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
    src = argv[2] if len(argv) > 2 else None
    # `validate` also reports coverage when a source is supplied (the optional `[source]` arg); the
    # `coverage` command always does, requiring one. Validate the required source path BEFORE printing
    # the PACK OK success line, so a failed `coverage` run never emits a success signal to stdout.
    want_coverage = cmd == "coverage" or src
    if want_coverage and not src:
        print("coverage needs a source path", file=sys.stderr)
        return 2
    print(f"PACK OK: domain={pack.domain!r} node_types={len(pack.node_types)} "
          f"edge_types={len(pack.edge_types)} glossary={len(pack.glossary)}")
    if want_coverage:
        # R4: accept a file, directory, or glob of .md/.txt (a single file still works, byte-identical).
        from .sources import SourceSet
        source_set = SourceSet(src)
        if not source_set:
            print(f"no .md/.txt source found at: {src}", file=sys.stderr)
            return 2
        cov = coverage(pack, source_set.concat)
        for k, v in cov.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
