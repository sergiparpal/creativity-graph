"""The generative layer (PLAN_generative_layer §3): deterministic candidate generators.

This module holds **deterministic** candidate generators that read the derived graph + source +
pack and emit `hypothesized` candidates — *proposals from a discovery mechanism*, never text claims.
Each generator realises one mechanism from the source theory ("Conclusiones v6") and tags every
candidate with the § it implements.

The design contract (PLAN §1) every generator obeys:
  1. A candidate is `provenance=hypothesized`, `epistemic_state=unverified`, **with no span**. It is
     stored in a lane that can never be mistaken for grounded content.
  2. Generate offensively; judge defensively. Generation is NEVER gatekept by a quality metric — the
     existing grounding loop (`kg_ground`) is the post-hoc filter.
  3. Generality control travels with every generator (§4): structural rankings are
     specificity-weighted; compression candidates pass an MDL screen. No candidate ranks high merely
     for being generic.
  4. Failure memory binds generation (§13): a candidate whose `(source, relation, target)` (or its
     reverse) is already in `FAILURE_STATES` is dropped on sight.

Generators are **pure and read-only**: they never write the canon. The `/kg-generate` command (Stage
6) routes their output through the propose lane (`kg_propose`, Stage 1).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candidate:
    """A single machine-proposed graph element, destined for the hypothesized write lane.

    `provenance` is always `hypothesized`, `epistemic_state` always `unverified`, and there is never
    a span — these are structural proposals, not text claims, so they are not carried on the dataclass
    (the propose lane forces them).
    """
    kind: str            # "edge" | "node"
    mechanism: str       # "bridge" | "seed" | "compression" | "regroup" | "transplant" | "ensemble"
    source: str = ""     # for edges
    target: str = ""     # for edges
    relation: str = ""   # for edges (a pack edge_type)
    label: str = ""      # for nodes (e.g. a proposed compression)
    node_type: str = ""  # for nodes (a pack node_type)
    score: float = 0.0
    specificity: float = 0.0
    rationale: str = ""
    section: str = ""    # the source-theory § the mechanism implements

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)
