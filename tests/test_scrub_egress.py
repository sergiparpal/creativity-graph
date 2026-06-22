"""Egress scrub wiring (§1.9): kg_scrub redacts before egress; kg_write restores spans for the canon.

The scrubber existed and was unit-tested in isolation; these tests pin the LIVE path — that the engine
actually invokes it, so a seeded secret never leaves via kg_scrub and a placeholder-bearing span the
subagent emits is restored to the original (unscrubbed) text when written to the canon.
"""
from __future__ import annotations

import time

import pytest

from kg_engine.scrub import Scrubber
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


# --- scrub-1: the keyword=value SECRET rule must not backtrack catastrophically (ReDoS) ----------

def test_keyword_secret_rule_no_redos_on_long_run():
    # A ~100k-char unbroken word/base64 run after `api_key=`. With a floating greedy [\w.-]* prefix the
    # SECRET keyword=value rule backtracked O(N^2) at every position; the anchored/bounded prefix is linear.
    run = "A" + "b1Cd2" * 20000  # ~100k chars, no whitespace/break
    text = f"api_key={run} trails here"
    start = time.monotonic()
    scrubbed, _ = Scrubber("low").scrub(text)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"scrub took {elapsed:.2f}s on a 100k run (catastrophic backtracking)"
    assert run not in scrubbed, "the long secret value leaked"


# --- scrub-3: secret formats beyond the prefixes are fully redacted (no surviving digit fragment) -

# Realistic secret SHAPES, assembled from fragments so no contiguous vendor-key literal is committed
# (GitHub push protection blocks those) while the scrubber still sees the whole token at runtime.
@pytest.mark.parametrize("token", [
    "sk-" + "ant-api03-abc123DEF456ghi789JKL012mno345PQR678stu",  # Anthropic-style
    "sk_" + "live_" + "51ABCdef456GHI789jkl012MNO",               # Stripe-live-style
    "sk_" + "test_" + "51ABCdef456GHI789jkl012MNO",               # Stripe-test-style
    "glpat" + "-ABCdef123456GHIjkl789mno",                        # GitLab-PAT-style
    "AIza" + "SyA1234567890abcdefghijklmnopqrstuv1",              # Google-API-key-style
    "deadbeef0123456789abcdef0123456789abcdef",                   # long hex fallback (not a vendor shape)
])
def test_secret_tokens_fully_redacted(token):
    # Each token used to fall through to PHONE/CC, which redact only a DIGIT SUBSTRING of the secret —
    # leaving the rest verbatim while looking redacted. The SECRET class must consume the WHOLE token.
    scrubbed, _ = Scrubber("high").scrub(f"the credential is {token} in config")
    assert "⟦SECRET" in scrubbed, scrubbed
    assert token not in scrubbed, "the full secret leaked"
    # no >=4-digit fragment of the secret survives verbatim
    import re
    for frag in re.findall(r"\d{4,}", token):
        assert frag not in scrubbed, f"digit fragment {frag!r} of the secret survived: {scrubbed!r}"


# --- scrub-2: a literal placeholder already in the source round-trips unchanged ------------------

def test_preexisting_placeholder_round_trips_unchanged():
    # The prose literally contains a ⟦EMAIL:1⟧-shaped substring; scrub() must not later let restore()
    # rewrite it into a redacted value (canon corruption), and a real email must still be redacted.
    src = "The token ⟦EMAIL:1⟧ is a redaction marker; mail real@example.com about it."
    scrubber = Scrubber("high")
    scrubbed, mapping = scrubber.scrub(src)
    assert "⟦EMAIL:1⟧" in scrubbed, "the literal placeholder was rewritten"
    assert "real@example.com" not in scrubbed, "the real email was not redacted"
    assert Scrubber.restore(scrubbed, mapping) == src, "literal placeholder did not round-trip"


# --- scrub-4: PERSON is conservative — concept bigrams survive, titled/known names are redacted ---

def test_person_rule_spares_concept_bigram_at_high():
    # Ordinary Title-Case concept bigrams must NOT be redacted (disastrous for conceptual documents).
    scrubbed, _ = Scrubber("high").scrub("Creative Destruction reshapes the market.")
    assert scrubbed == "Creative Destruction reshapes the market.", scrubbed


def test_person_rule_redacts_titled_name_at_high():
    # An actual titled name IS redacted at the same (default-high) sensitivity.
    scrubbed, _ = Scrubber("high").scrub("Dr Alan Turing proved it.")
    assert "Alan Turing" not in scrubbed and "⟦PERSON" in scrubbed, scrubbed
