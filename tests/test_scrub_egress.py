"""Egress scrub wiring (§1.9): kg_scrub redacts before egress; kg_write restores spans for the canon.

The scrubber existed and was unit-tested in isolation; these tests pin the LIVE path — that the engine
actually invokes it, so a seeded secret never leaves via kg_scrub and a placeholder-bearing span the
subagent emits is restored to the original (unscrubbed) text when written to the canon.
"""
from __future__ import annotations

from kg_engine.server import KGEngine

SECRET = "sk-abcdefghijklmnop0123456789"  # generic sk- api key (>=20 alnum)


def _engine(tmp_path):
    src = tmp_path / "source.md"
    src.write_text(
        f"Acme authenticates with {SECRET} to reach the service. "
        "The token grounds access to the cluster.\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    # no pack_path -> no type gating, so any declared relation is accepted
    return KGEngine(tmp_path, data, source_path=src, sensitivity="medium")


def test_kg_scrub_never_leaks_the_secret(tmp_path):
    eng = _engine(tmp_path)
    out = eng.kg_scrub()
    assert SECRET not in out["scrubbed"], "secret leaked through the egress scrub"
    assert out["redactions"] >= 1
    assert "⟦SECRET" in out["scrubbed"]


def test_kg_write_restores_placeholder_span_to_original(tmp_path):
    eng = _engine(tmp_path)
    scrubbed = eng.kg_scrub()["scrubbed"]
    # the placeholder the subagent would see in the scrubbed egress
    ph = scrubbed.split("authenticates with ", 1)[1].split(" to reach", 1)[0]
    assert ph.startswith("⟦SECRET")
    # the subagent emits a span in SCRUBBED form (it never saw the real secret)
    scrubbed_span = f"Acme authenticates with {ph} to reach the service"
    payload = {
        "nodes": [{"label": "Acme"}, {"label": "service"}],
        "edges": [{
            "source": "acme", "target": "service", "relation": "uses",
            "provenance": "span-present", "authored_by": "agent",
            "span": scrubbed_span, "source_file": "source.md",
        }],
        "complete": True,
    }
    res = eng.kg_write(payload)
    assert res["dispositions"]["ACCEPTED"] >= 1, res
    # the canon stores the ORIGINAL (restored) span, with the real secret recovered locally
    edges = eng.canon.all_edges()
    span = next(e.span for e in edges if e.relation == "uses")
    assert SECRET in span, "canon span was not restored to the original"
    assert "⟦SECRET" not in span


def test_unscrubbed_session_is_unaffected(tmp_path):
    # without a prior kg_scrub, restore is a no-op: a verbatim span is stored as-is
    eng = _engine(tmp_path)
    payload = {
        "edges": [{
            "source": "token", "target": "access", "relation": "grounds",
            "provenance": "span-present", "authored_by": "agent",
            "span": "The token grounds access to the cluster", "source_file": "source.md",
        }],
        "complete": True,
    }
    res = eng.kg_write(payload)
    assert res["dispositions"]["ACCEPTED"] >= 1, res
    span = next(e.span for e in eng.canon.all_edges() if e.relation == "grounds")
    assert span == "The token grounds access to the cluster"
