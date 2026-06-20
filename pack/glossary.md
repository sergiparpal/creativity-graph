# Glossary — conceptual-theory domain pack

Defined terms of the demo corpus (`examples/source.md`). The pack loader treats `pack.yaml:glossary` as
authoritative; this file is the human-readable companion (Stage 2).

- **compression** — a single idea standing in for many observations; earns its keep only when it predicts.
- **generality confound** — vague ideas accumulate spurious connections and look central while explaining
  little; the reason raw betweenness is gated and degree is the MVP advisory (§1.6).
- **span-present** — a claim that cites a verbatim textual span a reader can verify (the anti-nonsense
  invariant, §1.5).
- **inferred** — a claim asserted without a verifiable span.
- **bridge** — a node joining two otherwise separate communities of ideas.
- **betweenness** — how often a node lies on shortest paths; the natural but confounded bridge metric.
- **specificity-weighted betweenness** — betweenness weighted by term rarity; the gated bridge metric.
- **degree** — count of a node's connections; the honest advisory for importance.
- **failed** — a claim actively falsified; recorded as negative information, never pruned (§1.7).
- **negative information** — the record of what was refuted, which defends against re-proposal.
- **canon** — the human-editable source of truth carrying the grounding state (§1.2).
- **derived** — the regenerable, disposable projection of the canon.
