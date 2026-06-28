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
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:  # import only for typing; the boundary duck-types .verifies/.has_file at runtime
    from .sources import SourceSet

from .model import (
    AuthoredBy,
    Confidence,
    Disposition,
    Edge,
    EpistemicState,
    FAILURE_STATES,
    GROUNDABLE_STATES,
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


def _apply_forge_guards(item, claimed_state, claimed_author, is_hypothesized) -> tuple[Disposition, str]:
    """Strip a forged verdict and forged authorship on a Node or Edge in place (§1.4/§1.8).

    Single-sources the never-forge gates that both nodes and edges enforce identically, so the rule
    can never silently drift between the two item types. Mutates `item.epistemic_state` /
    `item.authored_by` (the attrs both Node and Edge expose) and returns the resulting `(disposition,
    reason)`. The `is_hypothesized` branch is the ONLY node-vs-edge difference.
    """
    disp, reason = Disposition.ACCEPTED, ""
    # never-forge-a-state: a write may assert only `unverified`. grounded/rejected/failed/obsolete
    # all flow ONLY through kg_ground; reset any other claimed state.
    if EpistemicState(claimed_state) != EpistemicState.UNVERIFIED:
        item.epistemic_state = EpistemicState.UNVERIFIED
        disp, reason = Disposition.DEMOTED, "forged-verdict-stripped"
    # never-forge-authorship: only the in-process parser is `deterministic` and only a real person
    # is `human`; a write payload claiming either is an untrusted forge -> demote to `agent`. On the
    # HYPOTHESIZED lane (PLAN Stage 1) there is no span-present check to bypass, so a deterministic
    # DISCOVERY mechanism may legitimately author a candidate — preserve `deterministic` and demote
    # only the (still-forgeable) `human` claim.
    claimed = AuthoredBy(claimed_author)
    if claimed == AuthoredBy.HUMAN or (claimed != AuthoredBy.AGENT and not is_hypothesized):
        item.authored_by = AuthoredBy.AGENT
        tag = "human-claim-stripped" if claimed == AuthoredBy.HUMAN else "deterministic-claim-stripped"
        disp = Disposition.DEMOTED
        reason = (reason + ";" if reason else "") + tag
    return disp, reason


class _FloodBudget:
    """Stage 9 flood limiter: charge only NET-NEW writable items against a budget, deduped items free.

    Unifies the 'net-new only, deduped free' cap arithmetic shared by the node and edge lanes. The
    deliberately-different baselines (the node lane has no failure states; the edge baseline excludes
    FAILURE_STATES per §1.7) stay explicit constructor args — the helper unifies only the arithmetic.
    """

    def __init__(self, baseline: int, budget: int) -> None:
        self.budget = budget
        self.written = 0
        self._baseline = baseline

    def fits(self, is_deduped: bool) -> bool:
        """Return whether a writable item fits; charge one net-new slot when it does. Deduped items
        cost zero (never charged, always fit), so an idempotent re-build never trips the limiter."""
        if is_deduped:
            return True
        if self._baseline + self.written >= self.budget:
            return False
        self.written += 1
        return True


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
    sources: "SourceSet | None" = None,
) -> list[ValidationResult]:
    """Validate a raw payload dict/obj. Returns one ValidationResult per item.

    `pack` (optional) supplies `node_types` / `edge_types` sets for undeclared-type routing.
    `source_text` is the ORIGINAL (unscrubbed) source used for span verification AND for the flood
    budget below; for a multi-file `sources` it is the concat (size only — verification is per-file).
    `sources` (R4, optional): a SourceSet making span verification source-aware — a span must verify
    against a DECLARED source, and against the edge's named `source_file` specifically when it has one
    (lenient any-source fallback when the named basename is unknown). `sources=None` preserves the exact
    single-blob behavior against `source_text`, so every existing direct call site is unchanged.
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
    # node flood baseline = raw count of existing ids (the node lane has no failure-state exemption,
    # unlike the edge baseline below).
    node_budget = None if budget is None else _FloodBudget(len(seen_nodes), budget)
    for node_in in wp.nodes:
        node = _canon_node(node_in)
        # restore the egress-scrubbed placeholders in the HUMAN-FACING fields so the canon stores the
        # ORIGINAL text (§1.9), exactly as the edge span is restored below. Scoped to label/body (free
        # text); the node id and edge endpoints stay on the form the subagent emitted so identity
        # linkage (id == slug it was attached by) is preserved (review-low: restore-only-span).
        if restore is not None:
            node.label = restore(node.label)
            node.body = restore(node.body)
        # never-forge-a-state + never-forge-authorship (§1.4/§1.8), single-sourced with the edge lane.
        is_hypothesized = node.provenance == Provenance.HYPOTHESIZED
        disp, reason = _apply_forge_guards(node, node_in.epistemic_state, node_in.authored_by, is_hypothesized)
        # undeclared type -> quarantine bucket (never silently accepted)
        if node_types is not None and node.node_type not in node_types:
            disp = Disposition.QUARANTINED
            reason = (reason + ";" if reason else "") + "undeclared-node-type"
        # flood guard: cap NET-NEW writable nodes. A node id already in the canon (or repeated in this
        # payload) grows the canon by zero, so it costs no budget and is never flooded — mirroring the
        # edge "deduped costs zero" rule so an idempotent re-build never trips the limiter.
        if node_budget is not None and disp in (Disposition.ACCEPTED, Disposition.DEMOTED):
            is_deduped = node.id in seen_nodes
            if is_deduped:
                reason = (reason + ";" if reason else "") + "deduped"
            elif not node_budget.fits(is_deduped):
                disp, reason = Disposition.REJECTED, "rate-limited-flood"
            else:
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
    # verdict-durability (review-C1, §1.8): the POSITIVE half of the kg_ground-owned state space
    # (grounded / obsolete — GROUNDABLE_STATES minus the failure half). A re-emit of an edge already in
    # one of these must be quarantined exactly like the failure half, or the canon's "incoming wins"
    # merge (canon._merge_into_existing) silently overwrites the verdict with this fresh `unverified`
    # edge on a normal idempotent /kg-build re-run. failure_ids was already protected; this closes the
    # symmetric grounded/obsolete gap on EVERY lane.
    verdict_ids = {e.id for e in existing_list
                   if e.epistemic_state in GROUNDABLE_STATES and e.epistemic_state not in FAILURE_STATES}
    # edge flood baseline excludes FAILURE_STATES (never-pruned failure memory must not consume the
    # budget, §1.7) — deliberately different from the node baseline above.
    edge_budget = None if budget is None else _FloodBudget(existing_count, budget)
    # single-blob fallback: normalize the source ONCE (skipped when a SourceSet drives verification,
    # which keeps its own per-file normalized cache).
    norm_source = "" if sources is not None else normalize_text(source_text)
    for edge_in in wp.edges:
        r = _validate_edge(edge_in, edge_types, norm_source, restore, seen, failure_ids,
                           verdict_ids=verdict_ids, sources=sources)
        # rate limit: once the canon-wide writable-edge budget is exhausted, reject the overflow as a
        # flood rather than letting it grow the graph unbounded (§Stage 9). Only NET-NEW edges are
        # charged: a deduped edge (already in the canon or repeated in this payload) grows the canon by
        # zero, so it must neither consume budget nor be flooded — otherwise an idempotent re-build that
        # re-emits existing edges would spuriously trip the limiter.
        if edge_budget is not None and r.written and not edge_budget.fits("deduped" in r.reason):
            # mirror the node lane: an id only counts as `seen` (a free dedup) once a copy was actually
            # written. A flood-rejected NET-NEW edge was provisionally added to `seen` by _validate_edge;
            # discard it here so a SECOND identical copy can't take the zero-cost dedup branch and slip
            # past the full budget (cap-bypass via in-payload duplication).
            seen.discard(r.identity)
            r = _ok("edge", r.item, Disposition.REJECTED, "rate-limited-flood", False, r.identity)
        results.append(r)

    return results


def _canon_node(node_in: NodeIn) -> Node:
    # Slug the resolved id so the node lane, the edge-source attachment key, and node_path's filename
    # all agree on ONE canonical key. A caller-supplied non-slug id (e.g. "FEP") was previously stored
    # RAW while the edge lane keyed its source on slug() — the two then diverge ("FEP" vs "fep") onto
    # the same fep.md and trip _check_slug_collision, rolling back the whole kg_write batch. node_path
    # already slugs the id for the filename, so the raw id was never honored on disk anyway.
    nid = _slug_label(node_in.id or node_in.label)
    return Node(
        id=nid, label=node_in.label, node_type=node_in.node_type, file_type=node_in.file_type,
        provenance=node_in.provenance, authored_by=node_in.authored_by,
        epistemic_state=node_in.epistemic_state, body=node_in.body,
    )


def _slug_label(label: str) -> str:
    from .model import slug
    return slug(label)


def _verify_span(edge, check_span, sources, norm_source) -> str | None:
    """Span-present enforcement (§1.5): return a REJECTED reason string, or None when the span verifies.

    Runs the empty/blank -> source-aware-vs-blob verification -> span-too-short floor checks in that
    order (a reordering could let a fabricated span through). Verification is on `check_span`, the
    RESTORED (original, unscrubbed) text; the caller restores before calling.
    """
    if not edge.span or not edge.span.strip():
        return "no-supporting-span"
    normalized_span = normalize_text(check_span)
    # span-present (§1.5). Source-aware (R4) when a SourceSet is supplied: the span must verify
    # against a DECLARED source, and against the edge's NAMED source_file specifically when it has
    # one. Split the reject so a mis-attributed span (present in the corpus, absent in the named
    # file) is `span-not-in-named-source`, distinct from `span-not-in-source` (absent everywhere).
    if sources is not None:
        if not normalized_span or not sources.verifies(check_span, source_file=edge.source_file):
            return ("span-not-in-named-source"
                    if (edge.source_file and sources.has_file(edge.source_file))
                    else "span-not-in-source")
    elif not normalized_span or normalized_span not in norm_source:  # single-blob fallback against the pre-normalized source
        return "span-not-in-source"
    # reject a degenerate anchor (a 1-char span is in almost any prose): require a meaningful floor
    # of real characters so span-present cites something, not just any substring (boundary-5 / §1.5).
    if len(normalized_span.replace(" ", "")) < MIN_SPAN_CHARS:
        return "span-too-short"
    return None


def _durability_quarantine(edge, canonical_id, rev, failure_ids, verdict_ids,
                           *, check_reverse: bool) -> ValidationResult | None:
    """Failure-/verdict-memory guard (§1.7/§1.8): QUARANTINE an edge that collapses into a known
    failure or verdict, else None. Returns the single-sourced QUARANTINED result so the reason
    strings live in one place.

    `check_reverse` makes the lane asymmetry explicit: the HYPOTHESIZED lane checks both the id AND
    its reverse (a candidate that re-proposes a refuted claim in either direction is dead), while the
    span-present lane checks ONLY `canonical_id` — a span-present edge has genuine textual support for
    ITS OWN direction, so the reverse edge is a distinct honest claim, not a re-proposal of the refuted
    one. The span-present lane also guards the POSITIVE half (grounded/obsolete via `verdict_ids`):
    re-emitting such an edge would otherwise dedup-and-accept, then the canon's "incoming wins" merge
    resets the verdict to this fresh `unverified` edge on a routine idempotent /kg-build re-run.
    """
    if canonical_id in failure_ids or (check_reverse and rev in failure_ids):
        return _ok("edge", edge, Disposition.QUARANTINED, "collapses-into-known-failure", False, canonical_id)
    if not check_reverse and canonical_id in verdict_ids:
        return _ok("edge", edge, Disposition.QUARANTINED, "collapses-into-known-verdict", False, canonical_id)
    return None


def _validate_edge(edge_in, edge_types, norm_source, restore, seen, failure_ids, *, verdict_ids,
                   sources=None) -> ValidationResult:
    edge = Edge(
        source=edge_in.source, target=edge_in.target, relation=edge_in.relation,
        provenance=edge_in.provenance, authored_by=edge_in.authored_by,
        epistemic_state=edge_in.epistemic_state, span=edge_in.span, source_file=edge_in.source_file,
        confidence=edge_in.confidence, confidence_score=edge_in.confidence_score, notes=edge_in.notes,
    )
    canonical_id = edge.id  # the canonical (slugged) identity used by dedup, the canon merge, and disk
    is_hypothesized = edge.provenance == Provenance.HYPOTHESIZED

    # reject a degenerate endpoint: an empty / whitespace / punctuation-only source, relation, or
    # target has NO word character, so slug() falls back to the literal "node" and edge_id aliases
    # distinct edges onto one canonical id/file (e.g. edge_id('', 'grounds', '') == edge_id('---',
    # 'grounds', '---')). Reject before that aliasing can dedup-merge unrelated claims (§1.4).
    for role, value in (("source", edge.source), ("relation", edge.relation), ("target", edge.target)):
        if not re.search(r"\w", value or "", re.UNICODE):
            return _ok("edge", edge, Disposition.REJECTED, f"empty-{role}", False, canonical_id)

    # clamp the confidence hint into [0,1]; drop NaN/inf so it can't poison downstream calibration
    if edge.confidence_score is not None:
        edge.confidence_score = (min(1.0, max(0.0, edge.confidence_score))
                                 if math.isfinite(edge.confidence_score) else None)

    # never-forge-a-state + never-forge-authorship (§1.4/§1.8), single-sourced with the node lane. This
    # binds EVERY lane — a hypothesized candidate that arrives with a verdict is demoted exactly like a
    # text claim, so promotion still flows only through kg_ground.
    disp, reason = _apply_forge_guards(edge, edge_in.epistemic_state, edge_in.authored_by, is_hypothesized)

    rev = edge_id(edge.target, edge.relation, edge.source)
    if is_hypothesized:
        # the hypothesized lane (PLAN Stage 1): a discovery-mechanism PROPOSAL, never a text claim, so
        # it carries NO span. Ignore any span the caller supplied and store it empty (the simpler of the
        # two documented paths — contract.md §"the propose lane"); the span-present invariant (§1.5)
        # does not apply here, and there is no fabrication risk because nothing claims textual support.
        edge.span = ""
        # invariant 5 (PLAN §13): a candidate that collapses into a known failure — its own identity OR
        # its reverse already lives in FAILURE_STATES — is rejected on sight (check_reverse=True) so
        # failure memory binds generation.
        q = _durability_quarantine(edge, canonical_id, rev, failure_ids, verdict_ids, check_reverse=True)
        if q is not None:
            return q
    else:
        # span-present enforcement (§1.5). Every non-hypothesized edge reaching this boundary is `agent`
        # (the authorship demotion above strips any `deterministic`/`human` claim), so there is no
        # deterministic bypass branch — every such edge must cite a verifying span.
        check_span = restore(edge.span) if restore else edge.span
        rej = _verify_span(edge, check_span, sources, norm_source)
        if rej is not None:
            return _ok("edge", edge, Disposition.REJECTED, rej, False, canonical_id)
        # restore protects the egress, not the local canon (§1.9): the canon stores the ORIGINAL
        # (unscrubbed) span, recovered from the placeholder form the subagent emitted.
        if restore and check_span != edge.span:
            edge.span = check_span
        # provenance is deliberately left as the agent declared it — a verifying span does NOT
        # auto-promote inferred -> span-present (verifying the span is the epistemic_state axis's job,
        # not provenance's; the three axes stay orthogonal).
        # failure/verdict memory binds re-extraction too (§1.7/§1.8): quarantine an id that already
        # carries a failed/rejected OR grounded/obsolete verdict, or the canon's "incoming wins" merge
        # would silently overwrite it with this fresh `unverified` edge on an idempotent /kg-build
        # re-run. check_reverse=False: a span-present edge has genuine support for ITS OWN direction,
        # so the reverse is a distinct honest claim, not a re-proposal of the refuted one.
        q = _durability_quarantine(edge, canonical_id, rev, failure_ids, verdict_ids, check_reverse=False)
        if q is not None:
            return q

    # undeclared edge type -> quarantine (never silently accepted) — applies to every lane
    if edge_types is not None and edge.relation not in edge_types:
        disp = Disposition.QUARANTINED
        reason = (reason + ";" if reason else "") + "undeclared-edge-type"

    # single-canonical-edge rule: dedup. Only a WRITABLE disposition seeds `seen` (mirroring the node
    # lane's `seen_nodes`, which adds only on a net-new written+fitting node). A QUARANTINED/REJECTED
    # edge is never merged into trusted canon, so it must NOT seed the dedup set: otherwise a later
    # same-canonical-id WRITABLE edge takes the zero-cost "deduped" branch and slips past the §Stage-9
    # flood cap — e.g. a QUARANTINED case-variant twin (relation "Grounds" vs declared "grounds", both
    # slugging to "grounds") would buy a free real edge.
    writable = disp in (Disposition.ACCEPTED, Disposition.DEMOTED)
    if canonical_id in seen and writable:
        reason = (reason + ";" if reason else "") + "deduped"
    if writable:
        seen.add(canonical_id)

    return _ok("edge", edge, disp, reason, retryable=False, identity=canonical_id)


def merge_results_into_nodes(results: list[ValidationResult]) -> dict[str, Node]:
    """Assemble written (ACCEPTED/DEMOTED) results into Node objects keyed by id, attaching edges to
    their source node. Quarantined/rejected items are excluded. Used by the canon writer."""
    nodes: dict[str, Node] = {}
    for r in results:
        if r.kind == "node" and r.written:
            nodes[r.item.id] = r.item  # last write wins — parity with the edge dedup below
    # single-canonical-edge rule: dedup by edge id within a source node (last write wins). Key the
    # attachment on the SLUG of edge.source — the same canonical key node ids, edge_id, node files, and
    # dedup all use — NOT the raw source string. Keying on the raw label (e.g. 'Free Energy Principle')
    # while nodes are keyed by slug ('free-energy-principle') fabricates a phantom Node(id=<raw label>)
    # that slug-collides with the real node onto one file, tripping _check_slug_collision and rolling
    # back the ENTIRE kg_write batch — silent total data loss (review-C2).
    edges_by_node: dict[str, dict[str, Edge]] = {}
    labels: dict[str, str] = {}
    for r in results:
        if r.kind == "edge" and r.written:
            src = _slug_label(r.item.source)
            edges_by_node.setdefault(src, {})[r.item.id] = r.item
            labels.setdefault(src, r.item.source)  # a readable label for an auto-created placeholder
    for src, ebyid in edges_by_node.items():
        if src not in nodes:
            nodes[src] = Node(id=src, label=labels.get(src, src))  # id == slug, matching its filename
        nodes[src].edges = list(ebyid.values())
    return nodes
