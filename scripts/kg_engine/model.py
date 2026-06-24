"""Core data model for the canon: the three axes, Node/Edge, span verification, frontmatter I/O.

This module is dependency-free (stdlib + pyyaml) and is the spine every other module binds to.
See ARCHITECTURE.md for the full contract.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import yaml

# libyaml's C SafeLoader is ~8x faster than the pure-Python SafeLoader and is hit on every canon PARSE
# (all_nodes / reproject / kg_write baseline) — the dominant read-path cost (model-1). The fallback is
# MANDATORY: pyyaml>=6 does not guarantee the C extension on every platform/wheel (musl, some ARM, sdist).
# CSafeLoader is semantically identical to SafeLoader for our scalar/list/map frontmatter, so spans/
# verdicts/edge_id/epistemic_state parse identically. The DUMP side deliberately stays on pure-Python
# safe_dump: CSafeDumper escapes non-BMP scalars (emoji / supplementary-plane CJK, e.g. inside a span)
# that safe_dump emits literally — lossless but byte-different — and the write path is far less hot than
# the parse path, so we keep canon bytes identical (clean git diffs, canonmerge byte-stability).
try:
    from yaml import CSafeLoader as _YamlLoader
except ImportError:  # pragma: no cover - exercised only on a libyaml-less build
    from yaml import SafeLoader as _YamlLoader

# --------------------------------------------------------------------------- axes (§1.3)


class Provenance(str, Enum):
    SPAN_PRESENT = "span-present"  # cites a verifiable textual span in the source
    INFERRED = "inferred"          # asserted without a verbatim span (relation is an inference)
    HYPOTHESIZED = "hypothesized"  # proposed by a discovery mechanism (structural/embedding adjacency)


class AuthoredBy(str, Enum):
    DETERMINISTIC = "deterministic"  # produced by a parser, not a language model
    AGENT = "agent"                  # produced by a subagent
    HUMAN = "human"                  # a person's verdict


class EpistemicState(str, Enum):
    UNVERIFIED = "unverified"
    GROUNDED = "grounded"    # passed the grounding loop
    REJECTED = "rejected"    # grounding rejected it
    FAILED = "failed"        # actively falsified (memory of failures, §1.7)
    OBSOLETE = "obsolete"    # superseded


class Confidence(str, Enum):
    """graphify-style extraction tier, surfaced by f4_probe. Orthogonal to the three axes."""
    EXTRACTED = "EXTRACTED"   # deterministic / parser
    INFERRED = "INFERRED"     # language-model judgement
    AMBIGUOUS = "AMBIGUOUS"


class Disposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    DEMOTED = "DEMOTED"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"


# Verdict states may never be asserted by a write; only kg_ground may set them.
VERDICT_STATES = {EpistemicState.GROUNDED, EpistemicState.REJECTED, EpistemicState.FAILED}
# Every state kg_ground may stamp (a verdict OR the `obsolete` lifecycle transition). This is the
# single source of truth shared by the boundary (which demotes any of these on a write), the server
# (kg_ground's accepted verdicts), and the reconciler (which re-quarantines any of these reached
# out-of-band without a matching audit record). Keeping it in one place stops the three from drifting.
GROUNDABLE_STATES = VERDICT_STATES | {EpistemicState.OBSOLETE}
# Negative information that the projector must never prune (§1.7).
FAILURE_STATES = {EpistemicState.REJECTED, EpistemicState.FAILED}
UNDECLARED_TYPE = "undeclared-type"
# The three shared provenance axes that must be unwrapped from Enum to str on egress (§1.3). Stated
# once so Edge.to_dict and Node.frontmatter cannot drift; a raw Enum leak here would break the
# projector's json.dumps(frontmatter) content hash. Edge adds `confidence` on top of these.
_AXIS_FIELDS = ("provenance", "authored_by", "epistemic_state")


def coerce_enum(cls, value, default):
    """Coerce a (possibly hand-edited / typo'd) string to an enum member, falling back to a safe
    default instead of raising. A malformed enum value in a canon note must not blow up parsing and
    make the whole node disappear from every read (§1.2)."""
    if isinstance(value, cls):
        return value
    try:
        return cls(value)
    except ValueError:
        return default


def coerce_axes(obj, *, provenance_default: "Provenance") -> None:
    """Coerce the three provenance axes on `obj` in place, with the fixed safe defaults shared by
    Edge/Node: authored_by → AGENT and epistemic_state → UNVERIFIED (the never-forge-a-verdict net —
    a malformed verdict is demoted, never invented), and the caller-supplied provenance default (the
    only axis whose default differs between Edge and Node)."""
    obj.provenance = coerce_enum(Provenance, obj.provenance, provenance_default)
    obj.authored_by = coerce_enum(AuthoredBy, obj.authored_by, AuthoredBy.AGENT)
    obj.epistemic_state = coerce_enum(EpistemicState, obj.epistemic_state, EpistemicState.UNVERIFIED)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- text / span

_WS = re.compile(r"\s+")


def normalize_text(s: str) -> str:
    """Whitespace-collapsed, case-folded form used for span verification.

    Applies Unicode NFC normalization (so a verbatim span in NFC matches a source stored as NFD, e.g.
    macOS/HFS+ paths and copy-paste), strips zero-width / format controls (BOM, ZWSP, word joiner),
    and folds curly quotes/dashes to ASCII so a paraphrase-free span still matches a source that
    differs only by typographic or composition-form normalization.
    """
    if s is None:
        return ""
    s = unicodedata.normalize("NFC", s)
    # drop zero-width / format controls (Cf) that are invisible but break a substring match
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = (
        s.replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace(" ", " ")
    )
    return _WS.sub(" ", s).strip().casefold()


def span_present_in(span: str, normalized_text: str) -> bool:
    """Core span-present test (§1.5): normalized `span` is a substring of ALREADY-normalized
    `normalized_text`. Guards on the normalized span — a Cf-only span normalizes to '' and must fail
    closed, not match every string (mirror in sources.verifies)."""
    ns = normalize_text(span)
    if not ns:
        return False
    return ns in normalized_text


def span_verifies(span: str, source_text: str) -> bool:
    """True iff `span` appears (normalized substring) in `source_text`. The span-present check (§1.5)."""
    # Guard on the NORMALIZED span, not the raw one: a span of only zero-width / format (Cf) characters
    # survives `str.strip()` (it is non-whitespace) but normalize_text() drops all Cf, leaving '' — and
    # '' is a substring of every string, so the raw guard would fail OPEN. Mirror sources.verifies (the
    # production sibling) which already guards on the normalized form (review-low: span_verifies Cf-only).
    return span_present_in(span, normalize_text(source_text))


def slug(s: str) -> str:
    # NFC-normalize first so visually-identical strings in different composition forms (NFD from
    # macOS/HFS+ copy-paste vs NFC) produce the SAME slug — otherwise the same logical node/edge
    # forks into two ids/filenames and dedup (§1.4) silently fails.
    s = unicodedata.normalize("NFC", str(s)).strip().lower()
    # MAP punctuation to a separator rather than DELETING it. This is the WEAKER of two guarantees,
    # not perfect injectivity: punctuation and separators all collapse to a single '-', so
    # punctuation-only variants are *intentionally* unified — slug('a/b')==slug('a-b')==slug('a b')
    # and slug('!!!foo!!!')==slug('foo'). What mapping (vs deleting) buys is that distinct inputs
    # which differ by a SEPARATING mark stay distinct ('a/b' vs 'ab', 'I/O' vs 'IO', 'foo.bar' vs
    # 'foobar' do NOT collapse onto one id/filename, as deletion would conflate them).
    s = re.sub(r"[^\w\s-]", "-", s)
    return re.sub(r"[\s_-]+", "-", s).strip("-") or "node"


def edge_id(source: str, relation: str, target: str) -> str:
    """Deterministic identity of an edge (single-canonical-edge rule, §1.4)."""
    return f"e_{slug(source)}__{slug(relation)}__{slug(target)}"


# --------------------------------------------------------------------------- dataclasses


@dataclass
class Edge:
    source: str
    target: str
    relation: str
    provenance: Provenance = Provenance.INFERRED
    authored_by: AuthoredBy = AuthoredBy.AGENT
    epistemic_state: EpistemicState = EpistemicState.UNVERIFIED
    span: str = ""
    source_file: str = ""
    confidence: Confidence = Confidence.INFERRED
    confidence_score: float | None = None
    verdict_by: str | None = None
    verdict_at: str | None = None
    notes: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        # tolerate a typo'd/unknown enum string in a hand-edited note: coerce to a safe default rather
        # than raise (a single bad field must not make the whole node vanish from every read, §1.2).
        # UNVERIFIED is the safe epistemic default — it demotes a malformed verdict, never invents one.
        coerce_axes(self, provenance_default=Provenance.INFERRED)
        self.confidence = coerce_enum(Confidence, self.confidence, Confidence.INFERRED)
        # endpoints/relation may arrive as non-str from YAML; the id is a deterministic function of
        # them (single-canonical-edge rule). Always recompute so a stored/forged id can never diverge
        # from (source, relation, target).
        self.source, self.relation, self.target = str(self.source), str(self.relation), str(self.target)
        self.id = edge_id(self.source, self.relation, self.target)

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.source, self.relation, self.target)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in _AXIS_FIELDS + ("confidence",):
            d[k] = d[k].value if isinstance(d[k], Enum) else d[k]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, source: str | None = None) -> "Edge":
        d = dict(d)
        if source is not None:
            d.setdefault("source", source)
        # tolerate unknown keys
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Node:
    id: str
    label: str = ""
    node_type: str = UNDECLARED_TYPE
    file_type: str = "prose"
    provenance: Provenance = Provenance.SPAN_PRESENT
    authored_by: AuthoredBy = AuthoredBy.AGENT
    epistemic_state: EpistemicState = EpistemicState.UNVERIFIED
    created_at: str = ""
    updated_at: str = ""
    body: str = ""
    edges: list[Edge] = field(default_factory=list)

    def __post_init__(self) -> None:
        coerce_axes(self, provenance_default=Provenance.SPAN_PRESENT)
        # YAML may coerce id/label/timestamps to non-str (numbers, booleans, datetime). Coerce to str
        # so frontmatter() stays JSON-serializable (the projector hashes json.dumps(frontmatter)) and a
        # falsy-but-present label (0 / false) is not mistaken for "missing" and overwritten by the id.
        self.id = str(self.id)
        self.label = self.id if self.label in (None, "") else str(self.label)
        now = utcnow()
        self.created_at = str(self.created_at) if self.created_at else now
        self.updated_at = str(self.updated_at) if self.updated_at else now
        self.edges = [e if isinstance(e, Edge) else Edge.from_dict(e, source=self.id) for e in self.edges]

    def frontmatter(self) -> dict[str, Any]:
        # Explicit ordered dict — key order is load-bearing for byte-identical canon (clean git diffs,
        # canonmerge). The three axis cells are built from _AXIS_FIELDS (stated once with Edge.to_dict)
        # but spliced here in their fixed position so the order is unchanged.
        return {
            "id": self.id,
            "label": self.label,
            "node_type": self.node_type,
            "file_type": self.file_type,
            **{k: getattr(self, k).value for k in _AXIS_FIELDS},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "edges": [e.to_dict() for e in self.edges],
        }


# --------------------------------------------------------------------------- markdown I/O

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def node_to_markdown(node: Node) -> str:
    fm = yaml.safe_dump(node.frontmatter(), sort_keys=False, allow_unicode=True, width=1000)
    body = node.body.rstrip("\n")
    return f"---\n{fm}---\n\n{body}\n" if body else f"---\n{fm}---\n"


def node_from_markdown(text: str, *, fallback_id: str | None = None) -> Node:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("note has no YAML frontmatter")
    fm = yaml.load(m.group(1), Loader=_YamlLoader) or {}
    body = m.group(2).strip("\n")
    # Skip a malformed edge entry (e.g. a hand-edited `- just a string` scalar instead of a mapping)
    # rather than letting it raise and take the whole node — including its failed/rejected counter-
    # edges (§1.7) — out of every read.
    edges = [Edge.from_dict(e, source=fm.get("id", fallback_id))
             for e in (fm.get("edges") or []) if isinstance(e, dict)]
    # Mirror Edge.from_dict: filter to known Node fields and drop None values so each absent/None key
    # falls through to the dataclass field default + __post_init__ coercion — the single source of every
    # static default (incl. "prose", and the enum defaults via coerce_axes). Then inject the two values
    # that are NOT plain field defaults: the resolved `id` (fm id > fallback_id > slug(label) precedence)
    # and the pre-parsed `edges` (with the malformed-entry skipping above), plus the parsed body.
    known = {f for f in Node.__dataclass_fields__}
    fields = {k: v for k, v in fm.items() if k in known and v is not None}
    fields["id"] = fm.get("id") or fallback_id or slug(fm.get("label", "node"))
    fields["body"] = body
    fields["edges"] = edges
    return Node(**fields)
