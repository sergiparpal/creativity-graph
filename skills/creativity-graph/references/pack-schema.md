# Reference — the domain pack schema (`pack/pack.yaml`)

A **domain pack** teaches the engine the vocabulary of *one* theory: the node and edge types its prose
actually uses, the defined terms (glossary), and per-term specificity seeds. It is the declared
allow-list at the MCP boundary. Types absent from the pack are not rejected and not silently accepted —
they are routed to the **`undeclared-type`** quarantine bucket so a human decides whether to extend the
pack (§Stage 2). Authoritative source: `scripts/kg_engine/pack.py`.

The server loads the pack at startup from `KG_PACK_PATH`, falling back to `${CLAUDE_PROJECT_DIR}/pack/pack.yaml`
(`server.py`). `kg_ping()` reports `pack_loaded: true|false`. A malformed pack does **not** crash the
server — it loads as `None` and *every* type then passes the undeclared check (nothing is quarantined for
type), so always run `validate` after editing the pack.

---

## 1. `PackContract` — the fields (Pydantic, `extra="forbid"`)

From `pack.py`, `class PackContract(BaseModel)`:

| field                | type                | constraint                                  | meaning |
|----------------------|---------------------|---------------------------------------------|---------|
| `domain`             | `str`               | required                                    | human label of the theory, e.g. `"conceptual theory"` |
| `version`            | `str`               | default `"0.1.0"`                            | pack revision |
| `node_types`         | `list[str]`         | **`min_length=1`**, unique, non-empty names | the only node types the boundary will ACCEPT |
| `edge_types`         | `list[str]`         | **`min_length=1`**, unique, non-empty names | the only relation types the boundary will ACCEPT |
| `glossary`           | `dict[str, str]`    | default `{}`                                | `term -> definition` |
| `specificity_seeds`  | `dict[str, float]`  | default `{}`                                | `term -> IDF-like score` (higher = rarer/more specific) |

`extra="forbid"` means an unknown top-level key (typo, stray field) makes the pack **invalid** — the
loader raises and `validate` prints `PACK INVALID: ...` (exit 1). The `_nonempty_unique` validator runs
on both type lists: a duplicate type → `types must be unique`; a blank/whitespace type → `type names must
be non-empty`. Two further validators also reject a pack: a name appearing in both `node_types` and `edge_types` → `a type may not be both a node_type and an edge_type`; and a non-finite `specificity_seeds` value (NaN/inf) → `specificity seeds must be finite numbers`.

`load_pack(path)` reads the YAML and calls `PackContract.model_validate(data)`. `data or {}` means an
empty file validates the *empty* dict, which then fails on the missing required `domain`/`node_types`/
`edge_types`.

---

## 2. The real `pack/pack.yaml` — node and edge types

These are the declared types for the demo corpus (`examples/source.md`). **Use exactly these in every
example, write, and grounding** — anything else quarantines.

### `node_types` (6) — §1.4: compressions/primitives vs claims vs operations
```yaml
node_types:
  - compression      # an idea standing in for many observations (predicts to earn its keep)
  - primitive        # an irreducible defined term
  - claim            # an assertion relating nodes
  - metric           # a measurement (degree, betweenness, specificity)
  - operation        # a process the theory performs (grounding, projection, reconciliation)
  - failure          # a recorded falsified claim (negative information)
```

### `edge_types` (10) — relation types drawn from the source prose
```yaml
edge_types:
  # Directed edges read  HEAD <relation> TAIL.  Reversing HEAD/TAIL was the dominant Stage-4 miss
  # (precision 0.61, span-support 0.94: the span is right, the direction/type is wrong). These per-type
  # comments are human-reference only — they never reach the model; agents/extractor.md is the live lever.
  - grounds          # HEAD = evidence/foundation, TAIL = claim it supports. NOT part-of.
  - attacked_by      # HEAD = victim (vaguer), TAIL = attacker (stronger/more specific).
  - reconciles_with  # HEAD resolves a real tension TAIL left open. NOT mere complement/contrast.
  - bridges          # HEAD joins two separate communities. NOT instance-of/evidence-of, NOT paired maxims.
  - collapses_into   # HEAD reduces to / is subsumed by TAIL. NOT "is a clause/risk of", NOT "opposite extreme of".
  - confounded_by    # HEAD's apparent value is inflated by TAIL.
  - approximates     # HEAD = cheap proxy, TAIL = the truer target it stands in for. NOT instance-of, NOT "related but distinct".
  - defends_against  # HEAD = remedy/defense, TAIL = threat/problem. The problem is the TAIL.
  - projects         # HEAD = regenerable projection, TAIL = source it derives from. NOT "reveals / makes-detectable".
  - survives         # HEAD = claim/info that persists, TAIL = the operation it survives (TAIL must be an `operation`).
```

**Relation direction is load-bearing (§Stage 4).** Every directed edge reads `HEAD <relation> TAIL`; the
right two endpoints in the **wrong order** score `wrong_type` and were the single largest Stage-4 miss
(precision 0.61 at span-support 0.94 — the span verifies but the direction/type is wrong, so the boundary
cannot catch it). The per-type comments above name the HEAD role; the authoritative, **model-facing**
version is the HEAD/TAIL role table in `agents/extractor.md` (these YAML comments are human-reference only
and are discarded by `yaml.safe_load`, so changing the names — never the comments — is what changes the
boundary's allow-list).

Each edge type is grounded in a verbatim relation in `source.md` (§1, span-present invariant). The
relation words are wrapped in markdown emphasis in the source, and `normalize_text` does not strip the
`*`, so a verbatim span must include the asterisks exactly — e.g. the real substrings
`is therefore *attacked_by* specificity`, `it is *confounded_by*`, and `plain **degree** is the honest
advisory that *approximates* importance`. (An asterisk-free paraphrase such as "Generality is attacked by
specificity" is *not* a verifiable span.)

### `glossary` (12 terms)
`term -> definition`, e.g. `compression`, `generality confound`, `span-present`, `inferred`, `bridge`,
`betweenness`, `specificity-weighted betweenness`, `degree`, `failed`, `negative information`, `canon`,
`derived`. `pack.yaml:glossary` is the *authoritative* machine copy; `pack/glossary.md` is the
human-readable companion.

---

## 3. How undeclared types route to the `undeclared-type` bucket

The boundary (`boundary.py`, called from `kg_write`) builds two sets from the loaded pack:

```python
node_types = set(getattr(pack, "node_types", None) or []) if pack is not None else None
edge_types = set(getattr(pack, "edge_types", None) or []) if pack is not None else None
```

Then, per element:

- **Node** whose `node_type` is not in `node_types` →
  `Disposition.QUARANTINED`, reason `undeclared-node-type`.
- **Edge** whose `relation` is not in `edge_types` →
  `Disposition.QUARANTINED`, reason `undeclared-edge-type`.

QUARANTINED ≠ REJECTED: the element is structurally valid (it may even carry a verifying span) but its
*type* is untrusted, so it is held in the undeclared-type bucket rather than written into the trusted
graph. This is distinct from the semantic/transport REJECTED cases (`no-supporting-span`, `span-not-in-source`,
`truncated-payload`, `schema-invalid`). When `pack is None` (no pack, or a malformed pack that failed to load), both
sets are `None` and the type check is skipped entirely — **nothing quarantines for type**. That is why a
silent pack load failure is dangerous: extend or fix the pack, never rely on the fallback.

**To clear a quarantine you do not edit the bucket — you extend the pack** (add the type to
`node_types`/`edge_types`), re-validate, and re-run the write so the element is re-evaluated and ACCEPTED.

---

## 4. Authoring / extending a pack from a source

A pack is *authored from* a source document, not invented. Procedure:

1. **Read the source** and list the relation verbs it actually uses in prose (e.g. *grounds*,
   *attacked_by*, *confounded_by*). Each becomes an `edge_type`. Do not add relations the theory never
   performs — unused types are noise and weaken the allow-list.
2. **Classify the noun-concepts** into a small closed set of `node_types` (here: compression / primitive /
   claim / metric / operation / failure). Keep it minimal; a sprawling type list defeats the point of an
   allow-list.
3. **Fill the `glossary`** from the source's *defined terms* — bold `**term**`, `` `code` ``, or quoted
   `"phrase"` spans (the patterns `_DEFN_RE` matches, see §5). Each definition should paraphrase the
   source's own definition so it is grounded in the text.
4. **Seed `specificity_seeds`** for terms you already know are vague vs rare (see §6). The harness
   recomputes true IDF over the corpus; seeds are starting hints so vague terms aren't mistaken for
   bridges before validation.
5. **Validate + check coverage** (§5). Iterate until `source_coverage` and `glossary_grounded_in_source`
   are both high.

**Extending an existing pack** (the common case, triggered by an `undeclared-type` quarantine): add the
single missing type to the right list, keeping types unique and non-empty; add any new defined term to the
glossary; bump `version`; re-validate; re-run the write. Because `extra="forbid"` and the uniqueness
validator are strict, a fat-fingered edit fails loudly rather than corrupting the graph.

---

## 5. The coverage check

`coverage(pack, source_text)` in `pack.py` measures whether the pack and source agree, in both
directions. It normalizes text via `model.normalize_text` and extracts the source's *defined terms* with:

```python
_DEFN_RE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\"([^\"]{3,60})\"|“([^”]{3,60})”")
```

i.e. bold, inline-code, and short straight/curly-quoted phrases. The quoted-phrase alternatives cap the
match in-pattern at **3–60 chars** (`{3,60}`), deliberately aligned with the 60-char post-filter so a long
quote isn't silently dropped by an inconsistent inner cap; `_defined_terms` then applies the matching
post-filter that drops any extracted term (bold, code, or quote) longer than **60 chars** (`len(term) <= 60`). It
returns:

| key                          | meaning |
|------------------------------|---------|
| `source_defined_terms`       | count of defined terms found in the source |
| `glossary_terms`             | `len(pack.glossary)` |
| `source_terms_in_glossary`   | how many source-defined terms appear in the glossary |
| **`source_coverage`**        | `source_terms_in_glossary / source_defined_terms` — *did the pack capture the theory's vocabulary?* |
| **`glossary_grounded_in_source`** | fraction of glossary terms that actually occur in the source — *is the glossary grounded, or did it drift / hallucinate terms the text never uses?* |

Both ratios use `max(n, 1)` denominators so an empty source/glossary can't divide by zero. Low
`source_coverage` ⇒ the pack is missing terms the source defines (under-fit). Low
`glossary_grounded_in_source` ⇒ the glossary defines terms not in the source (drift / invented
vocabulary) — the same class of error the whole plugin exists to prevent.

### Running it
```bash
# validate only (PackContract): exit 0 = OK, 1 = invalid, 2 = usage
python -m kg_engine.pack validate pack/pack.yaml

# validate + coverage (pass a source path)
python -m kg_engine.pack validate pack/pack.yaml examples/source.md
# (dev: /home/sergi/creativity-graph/.venv/bin/python -m kg_engine.pack ...  or  uv run)
```

Real output for the demo pack + source (a fully grounded pack — both ratios at 1.0):
```
PACK OK: domain='conceptual theory' node_types=6 edge_types=10 glossary=12
  source_defined_terms: 10
  glossary_terms: 12
  source_terms_in_glossary: 10
  source_coverage: 1.0
  glossary_grounded_in_source: 1.0
```

> Note: a source path supplied as the 3rd positional arg triggers coverage even under the `validate`
> subcommand (`if cmd == "coverage" or src:`). The `coverage` subcommand *requires* a source or exits 2.

---

## 6. Specificity / IDF and the generality confound (§1.6)

`specificity_seeds` is `term -> float`, higher = rarer/more specific. From the real pack:

```yaml
specificity_seeds:
  idea: 0.4
  system: 0.4
  thing: 0.2          # vague nouns: low — must NOT be mistaken for bridges
  claim: 0.8
  betweenness: 2.4
  specificity: 2.2
  compression: 2.0
  generality: 1.6
  confound: 2.1
  reconciler: 2.3
  falsification: 2.3  # rare/technical: high
```

**Why this exists.** The generality confound (§1.6) is the failure mode where a *vague* node accumulates
spurious connections, sits on many shortest paths "for empty reasons," and so scores high raw
**betweenness** while explaining nothing. Raw betweenness is `confounded_by` generality. The engine's
defense is to weight betweenness by term **specificity** so a genuine bridge (rare, specific terms)
out-scores a vague hub.

**How the harness uses it** (`harness.py`):
- `idf_seeds(documents)` computes IDF per term over the corpus sections: `log(n / (1 + df)) + 1.0`.
  Seeds are *starting hints*; the harness recomputes true IDF over the actual corpus and overrides them.
- `_node_specificity(label, seeds, default)` = mean seed over the label's words (default = mean of all
  seeds when a word is unseen / label has no words).
- `specificity(graph_data, corpus)` compares **raw degree**, **raw betweenness**, and
  **specificity-weighted betweenness** (`btw[n] * spec[n]`). It declares the confound present when the
  top raw-betweenness leaders are *vaguer than the graph average*
  (`betweenness_leader_specificity < mean_specificity`), and turns the gate on only when weighting also
  churns the leaderboard AND the specificity scores actually spread (`gate_on = confound and rank_churn > 0.2 and has_spread`, where `has_spread = spread > 1e-9` — a degenerate corpus with uniform specificity keeps the gate closed).

```bash
python -m kg_engine.harness specificity derived/graph.json examples/source.md   # JSON verdict
```

**The gate.** Specificity-weighted betweenness is a **hypothesis**, GATED until this harness verdict
(`gate_on=True`) validates it. Until then plain **degree** is the honest MVP advisory that
`approximates` importance — `kg_context` surfaces degree, never an unvalidated bridge score. Seeds let the
pack author bias the *priors* so obvious vague terms (`thing` 0.2, `idea`/`system` 0.4) can't masquerade
as bridges even before IDF is computed. Grounders apply the same principle by hand: reject an edge that is
"true" only because it is generic/unfalsifiable (`kg_ground` verdict reason `vague`, §3).

---

## 7. Quick checklist before committing a pack edit

- [ ] `python -m kg_engine.pack validate pack/pack.yaml examples/source.md` → `PACK OK` (exit 0).
- [ ] No `PACK INVALID` — no duplicate/blank types, no stray top-level keys (`extra="forbid"`).
- [ ] `source_coverage` high (pack captures the source's defined terms).
- [ ] `glossary_grounded_in_source` high (no invented terms absent from the source).
- [ ] New types added to *both* the YAML and any examples that previously quarantined.
- [ ] `version` bumped.
- [ ] `kg_ping().pack_loaded == true` after restart (a silent load failure disables type quarantine).
