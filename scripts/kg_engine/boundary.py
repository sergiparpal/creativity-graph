"""The write boundary (P_write, §1.8): strict validation of extraction payloads into dispositions.

Enforces, in the deterministic tier (§1.4):
  - schema validity (pydantic); truncated/partial payloads rejected with no partial write
  - span-present (§1.5): every non-deterministic edge cites a span that verifies against the source
  - undeclared types routed to the `undeclared-type` bucket, never silently accepted (§Stage 2)
  - never-forge-a-verdict: writes can't assert grounded/rejected/failed or authored_by=human
  - the single-canonical-edge rule via dedup against existing edges
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .model import (
    AuthoredBy,
    Confidence,
    Disposition,
    Edge,
    EpistemicState,
    Node,
    Provenance,
    UNDECLARED_TYPE,
    VERDICT_STATES,
    edge_id,
    span_verifies,
)


# --------------------------------------------------------------------------- pydantic contract


class EdgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    notes: str = ""


class NodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    label: str
    node_type: str = UNDECLARED_TYPE
    file_type: str = "prose"
    provenance: Provenance = Provenance.SPAN_PRESENT
    authored_by: AuthoredBy = AuthoredBy.AGENT
    epistemic_state: EpistemicState = EpistemicState.UNVERIFIED
    body: str = ""


class WritePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[NodeIn] = Field(default_factory=list)
    edges: list[EdgeIn] = Field(default_factory=list)
    # extractors that stream set this false on the final chunk; a missing/false value on a payload
    # that *should* be terminal is treated as a truncated transport failure.
    complete: bool = True


# --------------------------------------------------------------------------- results


@dataclass
class ValidationResult:
    disposition: Disposition
    kind: str                 # "node" | "edge"
    item: Any                 # Node or Edge (the canonicalized object, when written/quarantined)
    reason: str
    retryable: bool
    identity: tuple | str = ""

    @property
    def written(self) -> bool:
        return self.disposition in (Disposition.ACCEPTED, Disposition.DEMOTED)


def _ok(kind, item, disp, reason="", retryable=False, identity=""):
    return ValidationResult(disp, kind, item, reason, retryable, identity)


# --------------------------------------------------------------------------- validate


def validate_payload(
    payload: Any,
    *,
    pack: Any = None,
    source_text: str = "",
    existing: Iterable[Edge] | None = None,
    restore=None,
) -> list[ValidationResult]:
    """Validate a raw payload dict/obj. Returns one ValidationResult per item.

    `pack` (optional) supplies `node_types` / `edge_types` sets for undeclared-type routing.
    `source_text` is the ORIGINAL (unscrubbed) source used for span verification.
    `restore` optionally maps a scrubbed span back to original text before verifying.
    `existing` is the current set of canonical edges (for dedup / single-canonical-edge).
    """
    # 1. schema / truncation -------------------------------------------------
    try:
        wp = payload if isinstance(payload, WritePayload) else WritePayload.model_validate(payload)
    except ValidationError as e:
        return [_ok("payload", None, Disposition.REJECTED, f"schema-invalid: {e.error_count()} errors", True)]

    if not wp.complete:
        # truncated/partial payload — reject whole thing, no partial write (Stage 3)
        return [_ok("payload", None, Disposition.REJECTED, "truncated-payload", True)]

    node_types = set(getattr(pack, "node_types", None) or []) if pack is not None else None
    edge_types = set(getattr(pack, "edge_types", None) or []) if pack is not None else None

    results: list[ValidationResult] = []

    # 2. nodes ---------------------------------------------------------------
    for nin in wp.nodes:
        node = _canon_node(nin)
        disp, reason = Disposition.ACCEPTED, ""
        # never-forge-a-verdict
        if EpistemicState(nin.epistemic_state) in VERDICT_STATES:
            node.epistemic_state = EpistemicState.UNVERIFIED
            disp, reason = Disposition.DEMOTED, "forged-verdict-stripped"
        if AuthoredBy(nin.authored_by) == AuthoredBy.HUMAN:
            node.authored_by = AuthoredBy.AGENT
            disp, reason = Disposition.DEMOTED, (reason + ";" if reason else "") + "human-claim-stripped"
        # undeclared type -> quarantine bucket (never silently accepted)
        if node_types is not None and node.node_type not in node_types:
            node.node_type = UNDECLARED_TYPE if node.node_type == UNDECLARED_TYPE else node.node_type
            disp = Disposition.QUARANTINED
            reason = (reason + ";" if reason else "") + "undeclared-node-type"
        results.append(_ok("node", node, disp, reason, retryable=False, identity=node.id))

    # 3. edges ---------------------------------------------------------------
    seen = {e.identity for e in (existing or [])}
    for ein in wp.edges:
        results.append(_validate_edge(ein, edge_types, source_text, restore, seen))

    return results


def _canon_node(nin: NodeIn) -> Node:
    nid = nin.id or _slug_label(nin.label)
    return Node(
        id=nid, label=nin.label, node_type=nin.node_type, file_type=nin.file_type,
        provenance=nin.provenance, authored_by=nin.authored_by,
        epistemic_state=nin.epistemic_state, body=nin.body,
    )


def _slug_label(label: str) -> str:
    from .model import slug
    return slug(label)


def _validate_edge(ein, edge_types, source_text, restore, seen) -> ValidationResult:
    edge = Edge(
        source=ein.source, target=ein.target, relation=ein.relation,
        provenance=ein.provenance, authored_by=ein.authored_by,
        epistemic_state=ein.epistemic_state, span=ein.span, source_file=ein.source_file,
        confidence=ein.confidence, confidence_score=ein.confidence_score, notes=ein.notes,
    )
    ident = edge.identity
    disp, reason = Disposition.ACCEPTED, ""

    # never-forge-a-verdict (semantic, not retryable)
    if EpistemicState(ein.epistemic_state) in VERDICT_STATES:
        edge.epistemic_state = EpistemicState.UNVERIFIED
        disp, reason = Disposition.DEMOTED, "forged-verdict-stripped"
    if AuthoredBy(ein.authored_by) == AuthoredBy.HUMAN:
        edge.authored_by = AuthoredBy.AGENT
        disp, reason = Disposition.DEMOTED, (reason + ";" if reason else "") + "human-claim-stripped"

    deterministic = edge.authored_by == AuthoredBy.DETERMINISTIC

    # span-present enforcement (§1.5) — non-deterministic edges must cite a verifying span
    if not deterministic:
        if not edge.span or not edge.span.strip():
            return _ok("edge", edge, Disposition.REJECTED, "no-supporting-span", False, ident)
        check_span = restore(edge.span) if restore else edge.span
        if not span_verifies(check_span, source_text):
            return _ok("edge", edge, Disposition.REJECTED, "span-not-in-source", False, ident)
        # restore protects the egress, not the local canon (§1.9): the canon stores the ORIGINAL
        # (unscrubbed) span, recovered from the placeholder form the subagent emitted.
        if restore and check_span != edge.span:
            edge.span = check_span
        # a verifying span justifies span-present provenance; if the agent under-claimed (inferred),
        # leave it; if it claimed span-present we keep it. hypothesized stays hypothesized.
    else:
        # deterministic edges are span-present by construction
        edge.provenance = Provenance.SPAN_PRESENT

    # undeclared edge type -> quarantine (never silently accepted)
    if edge_types is not None and edge.relation not in edge_types:
        disp = Disposition.QUARANTINED
        reason = (reason + ";" if reason else "") + "undeclared-edge-type"

    # single-canonical-edge rule: dedup
    if ident in seen and disp in (Disposition.ACCEPTED, Disposition.DEMOTED):
        reason = (reason + ";" if reason else "") + "deduped"
    seen.add(ident)

    return _ok("edge", edge, disp, reason, retryable=False, identity=ident)


def merge_results_into_nodes(results: list[ValidationResult]) -> dict[str, Node]:
    """Assemble written (ACCEPTED/DEMOTED) results into Node objects keyed by id, attaching edges to
    their source node. Quarantined/rejected items are excluded. Used by the canon writer."""
    nodes: dict[str, Node] = {}
    for r in results:
        if r.kind == "node" and r.written:
            nodes.setdefault(r.item.id, r.item)
    # single-canonical-edge rule: dedup by edge id within a source node (last write wins)
    edges_by_node: dict[str, dict[str, Edge]] = {}
    for r in results:
        if r.kind == "edge" and r.written:
            edges_by_node.setdefault(r.item.source, {})[r.item.id] = r.item
    for src, ebyid in edges_by_node.items():
        if src not in nodes:
            nodes[src] = Node(id=src, label=src)
        nodes[src].edges = list(ebyid.values())
    return nodes
