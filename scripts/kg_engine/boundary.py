"""The write boundary (P_write, §1.8): strict validation of extraction payloads into dispositions.

Enforces, in the deterministic tier (§1.4):
  - schema validity (pydantic); truncated/partial payloads rejected with no partial write
  - span-present (§1.5): every non-deterministic edge cites a span that verifies against the source
  - undeclared types routed to the `undeclared-type` bucket, never silently accepted (§Stage 2)
  - never-forge-a-verdict: writes can't assert grounded/rejected/failed or authored_by=human
  - the single-canonical-edge rule via dedup against existing edges
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .model import (
    AuthoredBy,
    Confidence,
    Disposition,
    Edge,
    EpistemicState,
    FAILURE_STATES,
    Node,
    Provenance,
    UNDECLARED_TYPE,
    VERDICT_STATES,
    edge_id,
    normalize_text,
)

# A span must be a verbatim anchor, not a degenerate one: a 1-char span ('a') is a substring of
# almost any prose and meets span-present letter-of-the-law while citing nothing. Require a minimum
# of real (non-whitespace) characters so the structural guarantee stays meaningful (§1.5).
MIN_SPAN_CHARS = 4

# Anti-injection-flooding rate limit (§Stage 9): a hostile or oversized source must not be able to
# stuff the canon with unbounded edges. The budget scales with source size but has a floor so normal
# small corpora are never affected; writable edges past the budget are REJECTED `rate-limited-flood`.
DEFAULT_MAX_EDGES_PER_KB = 20.0
MIN_EDGE_BUDGET = 64


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
    # A single-shot payload is complete by default (kg_write delivers one validated JSON object, so an
    # omitted flag means "this is the whole thing"). A streaming producer must set complete=False on a
    # non-final / truncated chunk to force a whole-payload rejection with no partial write (§Stage 3).
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
    existing_node_ids: Iterable[str] | None = None,
    restore=None,
    max_edges_per_kb: float | None = DEFAULT_MAX_EDGES_PER_KB,
) -> list[ValidationResult]:
    """Validate a raw payload dict/obj. Returns one ValidationResult per item.

    `pack` (optional) supplies `node_types` / `edge_types` sets for undeclared-type routing.
    `source_text` is the ORIGINAL (unscrubbed) source used for span verification.
    `restore` optionally maps a scrubbed span back to original text before verifying.
    `existing` is the current set of canonical edges (for dedup / single-canonical-edge).
    `existing_node_ids` are the ids the canon already holds, so the node flood guard is seeded
    canon-wide (mirroring the edge baseline) AND charges only NET-NEW node ids — re-emitting an
    existing node grows the canon by zero, so (like a deduped edge) it must neither consume budget
    nor be flooded; otherwise an idempotent re-build of a canon already at its node budget would be
    rejected wholesale (§Stage 9).
    `max_edges_per_kb` caps writable edges at `max(MIN_EDGE_BUDGET, kb*rate)` across the canon
    (existing + this payload), defeating injection flooding (§Stage 9); pass None to disable.
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

    # flood budget (§Stage 9): a single ceiling on net-new writable items, applied independently to
    # nodes and to edges so a hostile/oversized payload can't stuff the canon with either.
    budget = None
    if max_edges_per_kb is not None:
        budget = max(MIN_EDGE_BUDGET, int(len(source_text) / 1024.0 * max_edges_per_kb))

    results: list[ValidationResult] = []

    # 2. nodes ---------------------------------------------------------------
    seen_nodes = set(existing_node_ids or [])      # canon-wide dedup set, like `seen` for edges
    existing_node_count = len(seen_nodes)
    written_nodes = 0
    for nin in wp.nodes:
        node = _canon_node(nin)
        disp, reason = Disposition.ACCEPTED, ""
        # never-forge-a-state: a write may assert only `unverified`. grounded/rejected/failed/obsolete
        # all flow ONLY through kg_ground; reset any other claimed state.
        if EpistemicState(nin.epistemic_state) != EpistemicState.UNVERIFIED:
            node.epistemic_state = EpistemicState.UNVERIFIED
            disp, reason = Disposition.DEMOTED, "forged-verdict-stripped"
        # never-forge-authorship: only the in-process parser is `deterministic` and only a real person
        # is `human`; a write payload claiming either is an untrusted forge -> demote to `agent`. On the
        # HYPOTHESIZED lane (PLAN Stage 1) there is no span-present check to bypass, so a deterministic
        # DISCOVERY mechanism may legitimately author a candidate node — preserve `deterministic` and
        # demote only the (still-forgeable) `human` claim.
        claimed = AuthoredBy(nin.authored_by)
        is_hypothesized = node.provenance == Provenance.HYPOTHESIZED
        if claimed == AuthoredBy.HUMAN or (claimed != AuthoredBy.AGENT and not is_hypothesized):
            node.authored_by = AuthoredBy.AGENT
            tag = "human-claim-stripped" if claimed == AuthoredBy.HUMAN else "deterministic-claim-stripped"
            disp = Disposition.DEMOTED
            reason = (reason + ";" if reason else "") + tag
        # undeclared type -> quarantine bucket (never silently accepted)
        if node_types is not None and node.node_type not in node_types:
            disp = Disposition.QUARANTINED
            reason = (reason + ";" if reason else "") + "undeclared-node-type"
        # flood guard: cap NET-NEW writable nodes. A node id already in the canon (or repeated in this
        # payload) grows the canon by zero, so it costs no budget and is never flooded — mirroring the
        # edge "deduped costs zero" rule so an idempotent re-build never trips the limiter.
        if budget is not None and disp in (Disposition.ACCEPTED, Disposition.DEMOTED):
            if node.id in seen_nodes:
                reason = (reason + ";" if reason else "") + "deduped"
            elif existing_node_count + written_nodes >= budget:
                disp, reason = Disposition.REJECTED, "rate-limited-flood"
            else:
                written_nodes += 1
                seen_nodes.add(node.id)
        results.append(_ok("node", node, disp, reason, retryable=False, identity=node.id))

    # 3. edges ---------------------------------------------------------------
    existing_list = list(existing or [])
    # dedup on the CANONICAL edge id (the slug), the same key the canon merge and disk use — keying
    # on the raw (source,relation,target) tuple here while the canon keys on the slugged id let the
    # two disagree, silently dropping an "accepted" edge in the merge (boundary-1 / §1.4).
    seen = {e.id for e in existing_list}
    # the flood baseline counts only LIVE growth: never-pruned failure memory (rejected/failed, §1.7)
    # must not consume the budget and starve legitimate new writes.
    existing_count = sum(1 for e in existing_list if e.epistemic_state not in FAILURE_STATES)
    # failure-memory ids (PLAN invariant 5 / §13): canonical edge ids that have been refuted
    # (rejected/failed). A HYPOTHESIZED candidate whose own id — OR its reverse — collides with one is a
    # claim that collapses into a known failure, and is quarantined on sight so generation can't re-propose
    # what was already refuted. Failure memory binds generation.
    failure_ids = {e.id for e in existing_list if e.epistemic_state in FAILURE_STATES}
    written = 0
    norm_source = normalize_text(source_text)  # normalize the source ONCE, not per edge
    for ein in wp.edges:
        r = _validate_edge(ein, edge_types, norm_source, restore, seen, failure_ids)
        # rate limit: once the canon-wide writable-edge budget is exhausted, reject the overflow as a
        # flood rather than letting it grow the graph unbounded (§Stage 9). Only NET-NEW edges are
        # charged: a deduped edge (already in the canon or repeated in this payload) grows the canon by
        # zero, so it must neither consume budget nor be flooded — otherwise an idempotent re-build that
        # re-emits existing edges would spuriously trip the limiter.
        if budget is not None and r.written and "deduped" not in r.reason:
            if existing_count + written >= budget:
                r = _ok("edge", r.item, Disposition.REJECTED, "rate-limited-flood", False, r.identity)
            else:
                written += 1
        results.append(r)

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


def _validate_edge(ein, edge_types, norm_source, restore, seen, failure_ids) -> ValidationResult:
    edge = Edge(
        source=ein.source, target=ein.target, relation=ein.relation,
        provenance=ein.provenance, authored_by=ein.authored_by,
        epistemic_state=ein.epistemic_state, span=ein.span, source_file=ein.source_file,
        confidence=ein.confidence, confidence_score=ein.confidence_score, notes=ein.notes,
    )
    ident = edge.id  # the canonical (slugged) identity used by dedup, the canon merge, and disk
    disp, reason = Disposition.ACCEPTED, ""
    is_hypothesized = edge.provenance == Provenance.HYPOTHESIZED

    # clamp the confidence hint into [0,1]; drop NaN/inf so it can't poison downstream calibration
    if edge.confidence_score is not None:
        edge.confidence_score = (min(1.0, max(0.0, edge.confidence_score))
                                 if math.isfinite(edge.confidence_score) else None)

    # never-forge-a-state (semantic, not retryable): only `unverified` may be asserted by a write;
    # grounded/rejected/failed/obsolete flow ONLY through kg_ground. This binds EVERY lane — a
    # hypothesized candidate that arrives with a verdict is demoted exactly like a text claim (PLAN
    # Stage 1, never-forge-a-verdict), so promotion still flows only through kg_ground.
    if EpistemicState(ein.epistemic_state) != EpistemicState.UNVERIFIED:
        edge.epistemic_state = EpistemicState.UNVERIFIED
        disp, reason = Disposition.DEMOTED, "forged-verdict-stripped"
    # never-forge-authorship. On the span-present/inferred lane an extractor that self-declares
    # `deterministic` would otherwise skip span-present (§1.5 anti-bypass) — only the in-process parser
    # is deterministic, so demote `deterministic`/`human` -> `agent`. On the HYPOTHESIZED lane there is
    # no span-present check to bypass, so a deterministic DISCOVERY mechanism may legitimately author a
    # candidate — preserve `deterministic` and demote only the (still-forgeable) `human` claim.
    claimed = AuthoredBy(ein.authored_by)
    if claimed == AuthoredBy.HUMAN or (claimed != AuthoredBy.AGENT and not is_hypothesized):
        edge.authored_by = AuthoredBy.AGENT
        tag = "human-claim-stripped" if claimed == AuthoredBy.HUMAN else "deterministic-claim-stripped"
        disp = Disposition.DEMOTED
        reason = (reason + ";" if reason else "") + tag

    if is_hypothesized:
        # the hypothesized lane (PLAN Stage 1): a discovery-mechanism PROPOSAL, never a text claim, so
        # it carries NO span. Ignore any span the caller supplied and store it empty (the simpler of the
        # two documented paths — contract.md §"the propose lane"); the span-present invariant (§1.5)
        # does not apply here, and there is no fabrication risk because nothing claims textual support.
        edge.span = ""
        # invariant 5 (PLAN §13): a candidate that collapses into a known failure — its own identity OR
        # its reverse already lives in FAILURE_STATES — is rejected on sight. Quarantine it (never merged
        # into trusted canon) so failure memory binds generation.
        rev = edge_id(edge.target, edge.relation, edge.source)
        if ident in failure_ids or rev in failure_ids:
            return _ok("edge", edge, Disposition.QUARANTINED, "collapses-into-known-failure", False, ident)
    else:
        # span-present enforcement (§1.5). Every non-hypothesized edge reaching this boundary is `agent`
        # (the authorship demotion above strips any `deterministic`/`human` claim), so there is no
        # deterministic bypass branch — every such edge must cite a verifying span.
        if not edge.span or not edge.span.strip():
            return _ok("edge", edge, Disposition.REJECTED, "no-supporting-span", False, ident)
        check_span = restore(edge.span) if restore else edge.span
        ns = normalize_text(check_span)
        if not ns or ns not in norm_source:  # span-present (§1.5), against the pre-normalized source
            return _ok("edge", edge, Disposition.REJECTED, "span-not-in-source", False, ident)
        # reject a degenerate anchor (a 1-char span is in almost any prose): require a meaningful floor
        # of real characters so span-present cites something, not just any substring (boundary-5 / §1.5).
        if len(ns.replace(" ", "")) < MIN_SPAN_CHARS:
            return _ok("edge", edge, Disposition.REJECTED, "span-too-short", False, ident)
        # restore protects the egress, not the local canon (§1.9): the canon stores the ORIGINAL
        # (unscrubbed) span, recovered from the placeholder form the subagent emitted.
        if restore and check_span != edge.span:
            edge.span = check_span
        # a verifying span justifies span-present provenance; if the agent under-claimed (inferred),
        # leave it; if it claimed span-present we keep it.

    # undeclared edge type -> quarantine (never silently accepted) — applies to every lane
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
