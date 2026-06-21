"""Stage 9 deferred item: the headless API-driven extraction backend (§2.2).

The Claude API call is isolated and the client is injectable, so the full pipeline (split → extract →
stamp axes → boundary → canon → project) is exercised with a fake client and no network. The fake
also lets us assert the request is Opus-4.x-compliant (adaptive thinking, structured output, no
removed sampling params).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kg_engine.backend import BackendExtractor

# Spans below are verbatim substrings of the conftest SOURCE so they pass the span-present boundary.
_PAYLOAD = {
    "nodes": [
        {"id": "compression", "label": "Compression", "node_type": "compression", "body": "stands in"},
        {"id": "claim", "label": "Claim", "node_type": "claim", "body": "an assertion"},
        {"id": "betweenness", "label": "Betweenness", "node_type": "metric", "body": "centrality"},
        {"id": "generality-confound", "label": "Generality confound", "node_type": "failure", "body": "vague"},
    ],
    "edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "A compression stands in for many observations and grounds the claims beneath it",
         "confidence_score": 0.6},
        {"source": "betweenness", "target": "generality-confound", "relation": "confounded_by",
         "span": "Betweenness is confounded by the generality confound", "confidence_score": 0.6},
    ],
}


class _FakeMessages:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        data = self._payloads.pop(0)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="thinking", text=""),
                     SimpleNamespace(type="text", text=json.dumps(data))],
        )


class _FakeClient:
    def __init__(self, payloads):
        self.messages = _FakeMessages(payloads)


def test_split_sections_keeps_preamble_and_headers():
    text = "intro line\n## First\nbody one\n## Second\nbody two\n"
    parts = BackendExtractor.split_sections(text)
    titles = [t for t, _ in parts]
    assert titles == ["", "First", "Second"]
    assert "intro line" in parts[0][1]
    assert parts[1][1].startswith("## First")


def test_section_schema_is_keyed_to_pack_vocabulary(engine):
    schema = BackendExtractor(engine, client=_FakeClient([])).section_schema()
    node_enum = schema["properties"]["nodes"]["items"]["properties"]["node_type"]["enum"]
    edge_enum = schema["properties"]["edges"]["items"]["properties"]["relation"]["enum"]
    assert set(node_enum) == set(engine.pack.node_types)
    assert set(edge_enum) == set(engine.pack.edge_types)
    # structured-output schemas must be closed objects
    assert schema["additionalProperties"] is False
    assert schema["properties"]["edges"]["items"]["additionalProperties"] is False


def test_run_writes_through_boundary_and_projects(engine):
    extractor = BackendExtractor(engine, client=_FakeClient([_PAYLOAD]))
    out = extractor.run()

    assert out["sections"] == 1
    assert out["dispositions"]["ACCEPTED"] >= 2
    # the boundary stamped + persisted the edges
    edges = engine.canon.all_edges()
    assert {e.relation for e in edges} == {"grounds", "confounded_by"}
    # every persisted edge carries the deterministic axes the backend stamps
    for e in edges:
        assert e.authored_by.value == "agent"
        assert e.epistemic_state.value == "unverified"
        assert e.provenance.value == "span-present"
        assert e.source_file == "source.md"
    # the derived layer was projected
    assert (engine.projector.db_path).exists()


def test_request_is_opus_4x_compliant(engine):
    fake = _FakeClient([_PAYLOAD])
    BackendExtractor(engine, client=fake, model="claude-opus-4-8").run()
    call = fake.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    # removed-on-4.7+ sampling params must never be sent
    for forbidden in ("temperature", "top_p", "top_k"):
        assert forbidden not in call


def test_refusal_is_surfaced(engine):
    class _Refuser:
        messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[]))

    extractor = BackendExtractor(engine, client=_Refuser())
    with pytest.raises(RuntimeError, match="refused"):
        extractor.run()


def test_truncated_output_is_surfaced(engine):
    class _Truncator:
        messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="text", text='{"nodes": [')]))  # truncated JSON

    extractor = BackendExtractor(engine, client=_Truncator())
    with pytest.raises(RuntimeError, match="truncated"):
        extractor.run()


def test_cli_engine_resolves_project_pack_and_rate(tmp_path, monkeypatch):
    """The shared builder the CLI uses auto-discovers <project>/pack/pack.yaml and honors
    KG_MAX_EDGES_PER_KB (regression for the --project rebuild that dropped both)."""
    import shutil
    from pathlib import Path
    from kg_engine.server import build_engine_from_env

    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "pack").mkdir()
    shutil.copy(repo / "pack" / "pack.yaml", tmp_path / "pack" / "pack.yaml")
    (tmp_path / "source.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("KG_MAX_EDGES_PER_KB", "7")

    eng = build_engine_from_env(project=str(tmp_path), source=str(tmp_path / "source.md"))
    assert eng.pack is not None            # <project>/pack/pack.yaml auto-discovered
    assert eng.max_edges_per_kb == 7.0     # KG_MAX_EDGES_PER_KB honored on the CLI path
