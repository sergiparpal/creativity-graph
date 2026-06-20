# A theory of grounded conceptual knowledge

This is a self-contained demo source for the creativity-graph plugin: a dense, *non-self-grounding*
conceptual document. Its claims do not verify themselves the way code verifies against a parse tree, so
it is exactly the kind of text a naive extractor turns into convincing nonsense.

## 1. Compression and the cost of generality

A **compression** is a single idea that stands in for many observations; it earns its keep only when it
predicts. The **generality confound** is the failure mode where a vague idea accumulates spurious
connections: because it touches everything loosely, it looks central while explaining nothing. Generality
is therefore *attacked_by* specificity — a more specific claim, when it holds, defeats a vaguer one that
merely overlaps it. A compression that survives specific attack is said to *grounds* the claims beneath it.

## 2. Provenance and the span

A claim is **span-present** when it cites a verbatim textual span that a reader can check; it is
**inferred** when it is asserted without such a span. Span-present provenance *grounds* a claim far more
strongly than inference, because the verifiable span is the check. A claim with no supporting span is not
weakly grounded — it is ungrounded, and the boundary *rejects* it.

## 3. Bridges and betweenness

A **bridge** is a node that joins two otherwise separate communities of ideas. Raw **betweenness** counts
how often a node lies on shortest paths, and it is the natural bridge metric — but it is *confounded_by*
the generality confound, because a vague node sits on many paths for empty reasons. **Specificity-weighted
betweenness** *reconciles_with* the bridge intuition by weighting each node by the rarity of its terms, so a
genuine bridge *bridges* communities while a vague hub does not. Until validated, specificity-weighted
betweenness is a hypothesis, and plain **degree** is the honest advisory that *approximates* importance.

## 4. Memory of failures

A graph that only grows and forgets its mistakes drifts into nonsense. A **failed** claim — one actively
falsified — is *negative information*: it must be recorded and never pruned, because the record of what was
refuted *defends_against* the graph re-proposing it. Thus falsification *grounds* trust as much as
confirmation does: a claim that *collapses_into* a known failure is rejected on sight.

## 5. The canon and the projection

The **canon** is the human-editable source of truth that carries the grounding state; the **derived**
projection is regenerable and disposable. The derived layer *projects* the canon and must contain nothing
the canon does not. A verdict in the canon *survives* reprojection, because the reconciler re-attaches it.
