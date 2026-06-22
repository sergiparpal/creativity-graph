"""Headless extraction backend (§2.2, Stage 9): API-key-driven extraction for CI.

The normal path is "the LLM is the session" (§2.2) — the Claude Code session and its subagents do
the semantic work in-session, no API keys. This module is the *headless* alternative: it drives the
same extract → scrub → boundary → canon → project pipeline without an interactive session, by
calling the Claude API directly. It exists so CI (or any unattended run) can rebuild and re-extract
the graph from source.

It mirrors ``agents/extractor.md``: read the (scrubbed) source section by section, emit pack-typed
nodes and edges with a verbatim supporting span per edge, and hand each section's payload through the
same ``kg_write`` boundary the interactive flow uses. The deterministic axes
(``authored_by=agent``, ``epistemic_state=unverified``, ``provenance=span-present``,
``confidence=INFERRED``) are stamped here exactly as ``extractor.md`` tells the subagent to set them,
so the boundary sees identical payloads. The model only supplies the language work: which concepts
are nodes, which relations are edges, and the verbatim span proving each relation.

Model: ``claude-opus-4-8`` by default. Structured output via ``output_config.format`` (a json_schema
keyed to the pack vocabulary) so each section payload is always valid, parseable JSON. Adaptive
thinking; no sampling parameters (removed on Opus 4.7+). The actual API call is isolated in
``extract_section`` and the client is injectable, so the whole pipeline is unit-testable with a fake
client and no network.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .server import KGEngine, build_engine_from_env

DEFAULT_MODEL = "claude-opus-4-8"
# Dense sections + adaptive thinking (which counts toward the budget) can blow past a small cap and
# truncate mid-JSON at max_tokens. 16000 gives ample headroom while staying under the SDK's
# non-streaming ~10-minute timeout guard (which raises on much larger non-streamed values). Override
# with --max-tokens / KG_BACKEND_MAX_TOKENS if a section needs more.
DEFAULT_MAX_TOKENS = 16000

# Pack vocabulary fallback if no pack is loaded (keeps the schema valid; the boundary still
# quarantines anything off-vocabulary).
_FALLBACK_NODE_TYPES = ["compression", "primitive", "claim", "metric", "operation", "failure"]
_FALLBACK_EDGE_TYPES = [
    "grounds", "attacked_by", "reconciles_with", "bridges", "collapses_into",
    "confounded_by", "approximates", "defends_against", "projects", "survives",
]

SYSTEM_PROMPT = """\
You are kg-extractor, the headless extraction backend of the creativity-graph plugin. You turn a
non-self-grounding conceptual document into structured JSON: typed nodes, typed edges, and — for
every edge — a VERBATIM span from the source that proves the relation came from the text, not from
your own invention. You do the LANGUAGE work only; a deterministic engine validates and persists.

THE SPAN INVARIANT (this is the whole job): every edge MUST carry a `span` that is a verbatim
substring of the section text you are given. Copy it EXACTLY — do not paraphrase, summarize, fix
grammar, or strip markup (e.g. keep `*attacked_by*` asterisks). A span not present in the source is
rejected as a fabrication. The span must contain the relation between the two endpoints; prefer the
tightest substring that still names both ideas and the relation.

NODES: give each a stable slug `id` (lowercase, hyphenated, e.g. `generality-confound`), a human
`label`, a `node_type` from the pack, and a short `body` drawn from the text.

EDGES: `source` and `target` are node ids (slugs), `relation` is from the pack, `span` is the
verbatim proof, `confidence_score` is a float hint in [0,1]. A target may reference a node defined in
another section; the boundary placeholders it.

Use ONLY the pack vocabulary supplied in the schema. If the prose expresses a relation not in the
list, map it to the nearest pack relation only if the text truly supports it, otherwise drop it — do
not invent a type. Process exactly the one section you are given; do not invent content from other
sections. Return only the structured JSON object.
"""


@dataclass
class BackendExtractor:
    """Drives section-by-section extraction over the Claude API through the KGEngine boundary."""

    engine: KGEngine
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    client: Any = None  # injectable; defaults to anthropic.Anthropic() on first use
    _schema: dict = field(default=None, repr=False)

    # ---- client -----------------------------------------------------------
    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            import anthropic  # noqa: PLC0415 — optional dependency, imported lazily
        except ImportError as e:  # pragma: no cover - exercised only without the extra installed
            raise SystemExit(
                "the headless backend needs the 'anthropic' SDK: "
                "uv sync --extra backend  (or  pip install 'kg-engine[backend]')"
            ) from e
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        return self.client

    # ---- pack-keyed structured-output schema ------------------------------
    def section_schema(self) -> dict:
        if self._schema is not None:
            return self._schema
        node_types = list(getattr(self.engine.pack, "node_types", None) or _FALLBACK_NODE_TYPES)
        edge_types = list(getattr(self.engine.pack, "edge_types", None) or _FALLBACK_EDGE_TYPES)
        self._schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "node_type": {"type": "string", "enum": node_types},
                            "body": {"type": "string"},
                        },
                        "required": ["id", "label", "node_type", "body"],
                    },
                },
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source": {"type": "string"},
                            "target": {"type": "string"},
                            "relation": {"type": "string", "enum": edge_types},
                            "span": {"type": "string"},
                            "confidence_score": {"type": "number"},
                        },
                        "required": ["source", "target", "relation", "span", "confidence_score"],
                    },
                },
            },
            "required": ["nodes", "edges"],
        }
        return self._schema

    # ---- source slicing ---------------------------------------------------
    @staticmethod
    def split_sections(text: str) -> list[tuple[str, str]]:
        """Split a Markdown source into (title, body) sections at top-level ``##`` headers.

        Any preamble before the first ``##`` becomes a leading ("", preamble) section so it is not
        dropped. Mirrors the extractor agent's "one ## section per payload" rule.
        """
        parts: list[tuple[str, str]] = []
        cur_title, cur_lines = "", []
        for line in text.splitlines():
            m = re.match(r"^##\s+(.*)$", line)
            if m:
                if cur_lines:
                    parts.append((cur_title, "\n".join(cur_lines)))
                cur_title, cur_lines = m.group(1).strip(), [line]
            else:
                cur_lines.append(line)
        if cur_lines:
            parts.append((cur_title, "\n".join(cur_lines)))
        return parts

    def source_file_name(self) -> str:
        return self.engine.source_path.name if self.engine.source_path else "source.md"

    # ---- one API call per section -----------------------------------------
    def extract_section(self, scrubbed_text: str, title: str = "") -> dict:
        """Call the model on one (already-scrubbed) section; return the raw {nodes, edges} JSON."""
        client = self._ensure_client()
        user = (
            f"Extract the knowledge graph for this section"
            + (f' ("{title}")' if title else "")
            + ". Copy every span verbatim from this exact text:\n\n"
            + scrubbed_text
        )
        resp = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": self.section_schema()}},
            messages=[{"role": "user", "content": user}],
        )
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            cat = getattr(details, "category", None)
            expl = getattr(details, "explanation", None)
            raise RuntimeError(
                f"model refused extraction for section {title!r}"
                + (f" (category={cat})" if cat else "")
                + (f": {expl}" if expl else ""))
        if stop == "max_tokens":
            # structured output truncated mid-JSON; surface a diagnosable error rather than a raw
            # JSONDecodeError on the partial payload.
            raise RuntimeError(
                f"extraction truncated at max_tokens for section {title!r}; raise --max-tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise RuntimeError(f"no text block in model response for section {title!r}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # structured output should always be valid JSON, but surface a diagnosable error (with
            # the stop_reason and a snippet) rather than a bare JSONDecodeError if the model returns
            # malformed text.
            snippet = text[:200] + ("…" if len(text) > 200 else "")
            raise RuntimeError(
                f"model returned non-JSON for section {title!r} "
                f"(stop_reason={stop!r}): {e}; got: {snippet!r}") from e

    # ---- stamp the deterministic axes the boundary expects ----------------
    def _stamp(self, raw: dict) -> dict:
        sf = self.source_file_name()
        nodes = [
            {
                "id": n.get("id"),
                "label": n.get("label", n.get("id", "")),
                "node_type": n.get("node_type", "undeclared-type"),
                "file_type": "prose",
                "provenance": "span-present",
                "authored_by": "agent",
                "epistemic_state": "unverified",
                "body": n.get("body", ""),
            }
            for n in raw.get("nodes", [])
        ]
        edges = [
            {
                # tolerate a malformed model edge missing a required key: emit empties so the boundary
                # rejects it cleanly rather than crashing the whole run with a KeyError
                "source": e.get("source", ""),
                "target": e.get("target", ""),
                "relation": e.get("relation", ""),
                "span": e.get("span", ""),
                "source_file": sf,
                "provenance": "span-present",
                "authored_by": "agent",
                "epistemic_state": "unverified",
                "confidence": "INFERRED",
                "confidence_score": e.get("confidence_score"),
            }
            for e in raw.get("edges", [])
        ]
        return {"nodes": nodes, "edges": edges, "complete": True}

    # ---- full pipeline ----------------------------------------------------
    def run(self, source_path: str | os.PathLike | None = None) -> dict:
        """Extract the whole source, write through the boundary, then project. Returns a summary."""
        if source_path:
            self.engine.source_path = Path(source_path)
        src = self.engine.source_text()
        if not src.strip():
            raise SystemExit("no source text: set --source or KG_SOURCE_PATH")

        totals: Counter = Counter()
        sections = self.split_sections(src)
        n_written = 0
        failed_sections: list[dict] = []
        try:
            for title, body in sections:
                if not body.strip():
                    continue
                # A transient API error (or a RuntimeError on refusal / max_tokens / non-JSON) for one
                # section must not abort the whole multi-section run: isolate it, record it, and keep
                # going so the sections that did land are not lost.
                try:
                    # §1.9 egress scrub before the text reaches the model; kg_write restores spans for the canon.
                    scrubbed = self.engine.kg_scrub(body)["scrubbed"]
                    raw = self.extract_section(scrubbed, title)
                    result = self.engine.kg_write(self._stamp(raw), message=f"backend:{title or 'preamble'}")
                    for k, v in result["dispositions"].items():
                        totals[k] += v
                    n_written += 1
                except Exception as e:  # noqa: BLE001 — isolate any per-section failure
                    failed_sections.append({"title": title or "preamble", "error": f"{type(e).__name__}: {e}"})
        finally:
            # Always reconcile the derived layer with whatever landed, even if a section raised — the
            # canon may have been partially updated and must not be left with a stale projection.
            self.engine.projector.project()  # build/refresh the derived layer
        return {
            "model": self.model,
            "sections": n_written,
            "dispositions": dict(totals),
            "failed_sections": failed_sections,
            "metrics": self.engine.kg_metrics(),
        }


# --------------------------------------------------------------------------- CLI


def _build_engine(args: argparse.Namespace) -> KGEngine:
    # CLI flags override env, but pack auto-discovery, sensitivity/metrics, and the flood rate limit
    # all resolve through the single shared builder so the headless path never diverges from the server.
    return build_engine_from_env(project=args.project, data=args.data,
                                 source=args.source, pack=args.pack)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kg_engine.backend",
                                 description="Headless API-driven extraction for CI (§2.2).")
    ap.add_argument("command", choices=["extract"], help="the operation to run")
    ap.add_argument("--source", help="source document path (else KG_SOURCE_PATH / configured)")
    ap.add_argument("--project", help="canon vault dir (else KG_PROJECT_DIR / cwd)")
    ap.add_argument("--data", help="derived-layer data dir (else <project>/.kg-data)")
    ap.add_argument("--pack", help="pack.yaml path (else KG_PACK_PATH / <project>/pack/pack.yaml)")
    ap.add_argument("--model", default=os.environ.get("KG_BACKEND_MODEL", DEFAULT_MODEL),
                    help=f"Claude model id (default {DEFAULT_MODEL})")
    try:
        _max_tokens_default = int(os.environ.get("KG_BACKEND_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    except ValueError:  # a non-integer env value must not crash with a raw traceback
        _max_tokens_default = DEFAULT_MAX_TOKENS
    ap.add_argument("--max-tokens", type=int, default=_max_tokens_default,
                    help=f"per-section output cap (default {DEFAULT_MAX_TOKENS})")
    args = ap.parse_args(argv)

    engine = _build_engine(args)
    extractor = BackendExtractor(engine, model=args.model, max_tokens=args.max_tokens)
    out = extractor.run()
    print(json.dumps(out, indent=2))
    # The derived layer is already projected (run() does it in a finally); a non-zero exit only
    # signals that some sections failed so a CI run doesn't go green on a partial extraction.
    return 1 if out.get("failed_sections") else 0


if __name__ == "__main__":
    raise SystemExit(main())
