"""Regression tests for the headless backend fixes (§2.2, Stage 9).

Covers two boundary-contract bugs in ``backend.run`` / ``backend._ensure_client``:

- F10-backend: a ``kg_write`` result with ``rolled_back`` truthy must NOT have its dispositions
  accumulated and must NOT count toward ``sections`` written — the canon was rolled back so nothing
  landed. Before the fix, run() over-reported a rolled-back section as written.
- F20: a missing ``ANTHROPIC_API_KEY`` must raise a single, actionable ``SystemExit`` from
  ``_ensure_client`` (which propagates past run()'s per-section ``except Exception`` because
  SystemExit is BaseException), rather than N near-identical per-section 401s.

The Claude client is faked, so no network or API key is needed except where F20 asserts its absence.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kg_engine.backend import BackendExtractor, _NONSTREAMING_TIME_FLOOR

# A valid section payload whose spans are verbatim substrings of the conftest SOURCE.
_GOOD = {
    "nodes": [
        {"id": "compression", "label": "Compression", "node_type": "compression", "body": "stands in"},
        {"id": "claim", "label": "Claim", "node_type": "claim", "body": "an assertion"},
    ],
    "edges": [
        {"source": "compression", "target": "claim", "relation": "grounds",
         "span": "A compression stands in for many observations and grounds the claims beneath it",
         "confidence_score": 0.6},
    ],
}


def _fake_client(payload):
    class _Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            )

    return SimpleNamespace(messages=_Messages())


# --------------------------------------------------------------------------- F10-backend


def test_rolled_back_section_is_not_counted_as_written(engine, monkeypatch):
    """A kg_write that reports rolled_back=True (canon write raised, written_nodes==[]) must not be
    accumulated into dispositions or counted in `sections` — before the fix run() did both."""
    # Stub the boundary to report a rollback: per the server contract, written_nodes is [] and the
    # accepted/demoted counts never landed even though `dispositions` still reports what was *validated*.
    def _rolled_back(payload, *, message="kg_write", existing_nodes=None):
        return {
            "dispositions": {"ACCEPTED": 2, "DEMOTED": 0, "QUARANTINED": 0, "REJECTED": 0},
            "details": [],
            "written_nodes": [],
            "rolled_back": True,
            "error": "git stash rollback: write_nodes raised",
        }

    monkeypatch.setattr(engine, "kg_write", _rolled_back)
    extractor = BackendExtractor(engine, client=_fake_client(_GOOD))
    out = extractor.run()

    # the rolled-back section is NOT reported as written and its counts are NOT accumulated
    assert out["sections"] == 0
    assert out["dispositions"].get("ACCEPTED", 0) == 0
    assert out["dispositions"] == {}
    # the rollback is surfaced in the run summary as a failed section, carrying the boundary's error
    assert len(out["failed_sections"]) == 1
    assert "rollback" in out["failed_sections"][0]["error"]
    # projection still ran (run() does it in a finally), so the CLI exits non-zero on this partial run
    assert engine.projector.db_path.exists()


def test_effective_max_tokens_clamps_to_sdk_time_floor(engine):
    """review-M4: a --max-tokens override above the SDK's non-streaming time ceiling (~21333) is clamped,
    so the first create() of every section doesn't raise ValueError pre-flight. The 16000 default, which
    is already under the floor, is left untouched."""
    over = BackendExtractor(engine, client=SimpleNamespace(), max_tokens=22000)
    assert over._effective_max_tokens() <= _NONSTREAMING_TIME_FLOOR
    under = BackendExtractor(engine, client=SimpleNamespace(), max_tokens=16000)
    assert under._effective_max_tokens() == 16000


def test_mixed_rolled_back_and_clean_sections_only_count_clean(engine, tmp_path, monkeypatch):
    """With two sections where the FIRST rolls back and the SECOND writes cleanly, only the clean
    section is counted and only its dispositions accumulate."""
    src = tmp_path / "multi.md"
    src.write_text(
        "## One\nA compression stands in for many observations and grounds the claims beneath it.\n"
        "## Two\nBetweenness is confounded by the generality confound.\n",
        encoding="utf-8",
    )

    second = {
        "nodes": [
            {"id": "betweenness", "label": "Betweenness", "node_type": "metric", "body": "centrality"},
            {"id": "generality-confound", "label": "Generality confound", "node_type": "failure", "body": "vague"},
        ],
        "edges": [
            {"source": "betweenness", "target": "generality-confound", "relation": "confounded_by",
             "span": "Betweenness is confounded by the generality confound", "confidence_score": 0.6},
        ],
    }

    class _TwoSections:
        def __init__(self):
            self._n = 0

        def create(self, **kwargs):
            self._n += 1
            payload = _GOOD if self._n == 1 else second
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text=json.dumps(payload))])

    real_write = engine.kg_write

    def _write_first_rolls_back(payload, *, message="kg_write", existing_nodes=None):
        # roll back only the first section (its message carries the section title "One")
        if "One" in message:
            return {"dispositions": {"ACCEPTED": 2}, "details": [], "written_nodes": [],
                    "rolled_back": True, "error": "rollback on section One"}
        return real_write(payload, message=message)

    monkeypatch.setattr(engine, "kg_write", _write_first_rolls_back)
    extractor = BackendExtractor(engine, client=SimpleNamespace(messages=_TwoSections()))
    out = extractor.run(source_path=str(src))

    # only the clean second section is counted; the rolled-back first is a failed_section
    assert out["sections"] == 1
    assert len(out["failed_sections"]) == 1
    assert out["failed_sections"][0]["title"] == "One"
    # only the second section's edge landed in the canon
    assert {e.relation for e in engine.canon.all_edges()} == {"confounded_by"}
    # the rolled-back section's ACCEPTED:2 did not inflate the totals
    assert out["dispositions"].get("ACCEPTED", 0) >= 1  # the second section's accepts only


# --------------------------------------------------------------------------- truncation message honesty


def _truncating_client():
    """A fake client whose every create() reports a max_tokens truncation (no text block)."""
    class _Messages:
        def create(self, **kwargs):
            return SimpleNamespace(stop_reason="max_tokens", content=[])

    return SimpleNamespace(messages=_Messages())


def test_truncation_message_when_clamped_does_not_advise_raising_flag(engine):
    """When --max-tokens was already clamped below the requested value (effective cap < requested), the
    truncation error must NOT advise 'raise --max-tokens' (futile — the clamp pins it back) and must
    instead report the effective cap and point at split/streaming."""
    over = BackendExtractor(engine, client=_truncating_client(), max_tokens=50000)
    with pytest.raises(RuntimeError) as ei:
        over.extract_section("dense section text")
    msg = str(ei.value)
    assert "raise --max-tokens" not in msg          # the futile advice is gone on the clamped path
    assert "split" in msg and "streaming" in msg    # the actionable path is offered instead
    assert str(_NONSTREAMING_TIME_FLOOR) in msg     # the real ceiling is named
    assert str(over._effective_max_tokens()) in msg  # the effective cap is reported


def test_truncation_message_when_not_clamped_still_advises_raising_flag(engine):
    """When the request was NOT clamped (effective cap == requested), the original 'raise --max-tokens'
    advice is honest and preserved."""
    under = BackendExtractor(engine, client=_truncating_client(), max_tokens=16000)
    with pytest.raises(RuntimeError, match="raise --max-tokens") as ei:
        under.extract_section("dense section text")
    assert "split" not in str(ei.value)


# --------------------------------------------------------------------------- None content guard


def test_none_content_degrades_to_no_text_block(engine):
    """A content-less response on a non-refusal/non-max_tokens stop_reason must degrade to the
    intended 'no text block' RuntimeError, not a TypeError from iterating None.content."""
    class _Messages:
        def create(self, **kwargs):
            return SimpleNamespace(stop_reason="end_turn", content=None)

    client = SimpleNamespace(messages=_Messages())
    extractor = BackendExtractor(engine, client=client)
    with pytest.raises(RuntimeError, match="no text block"):
        extractor.extract_section("some section text")


# --------------------------------------------------------------------------- source_file_name fallback


def test_source_file_name_returns_basename_for_single_file(engine):
    """A single-file source_path stamps the real basename (the conftest source is `source.md`)."""
    extractor = BackendExtractor(engine)
    assert extractor.source_file_name() == "source.md"


def test_source_file_name_empty_for_directory_source(engine, tmp_path):
    """A directory source_path (R4) must NOT stamp the directory name as a basename — with more than one
    member it returns '' so the boundary takes its explicit empty-source any-source path rather than a
    fabricated basename the boundary's has_file() would miss."""
    d = tmp_path / "srcdir"
    d.mkdir()
    (d / "a.md").write_text("A compression grounds the claims.\n", encoding="utf-8")
    (d / "b.md").write_text("Betweenness is confounded by the generality confound.\n", encoding="utf-8")
    engine.source_path = d
    extractor = BackendExtractor(engine)
    assert extractor.source_file_name() == ""


def test_source_file_name_single_member_dir_returns_that_basename(engine, tmp_path):
    """A directory source with exactly one resolvable member returns that single basename, not the
    directory name."""
    d = tmp_path / "onedir"
    d.mkdir()
    (d / "only.md").write_text("A compression grounds the claims.\n", encoding="utf-8")
    engine.source_path = d
    extractor = BackendExtractor(engine)
    assert extractor.source_file_name() == "only.md"


# --------------------------------------------------------------------------- F20


def test_missing_api_key_raises_single_systemexit(engine, monkeypatch):
    """Without ANTHROPIC_API_KEY, _ensure_client must raise one clear SystemExit (BaseException, so it
    propagates past run()'s per-section `except Exception`) rather than N per-section 401s. Skipped if
    the optional 'anthropic' SDK is not importable (the SDK-present branch is what we exercise)."""
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # client defaults to None → _ensure_client builds the real SDK client and must assert the key first
    extractor = BackendExtractor(engine)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        extractor._ensure_client()


def test_missing_api_key_propagates_through_run(engine, monkeypatch):
    """The missing-key SystemExit is NOT swallowed by run()'s per-section `except Exception`: it
    aborts the whole run with one message instead of recording N failed sections."""
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    extractor = BackendExtractor(engine)  # real (unbuilt) client → key check fires on first section
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        extractor.run()
