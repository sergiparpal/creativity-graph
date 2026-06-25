"""Regression tests for the egress scrubber (§1.9): narrow correctness bugs.

F6  — the bare PERSON bigram rule scanned non-overlapping, so a non-name Title-Case word immediately
      before a real full name ("Researcher Alan Turing") matched the FIRST bigram, was spared by the
      given-name gate, and the scan resumed PAST the spared span — leaking the real name verbatim at
      sensitivity='high'. The fix rescans from the second token when the first is a non-name.
F11/M5 — at the default sensitivity='medium' the PHONE pattern matched bare 6-7 digit prose runs and
      dash-separated page ranges ("pages 100-200", bare "100200"), over-redacting figures into ⟦PHONE⟧
      (restored on write, but it degrades extraction). The fix requires phone-ish structure.
M4  — a literal ⟦CAT:N⟧ placeholder in section B's prose was over-expanded by the consumer's restore
      map when section A had already used that number for a real redaction. The boundary restores ONLY
      from each scrub() call's RETURNED mapping, so the fix emits an identity entry there for any literal
      and skips reserved numbers cumulatively across calls so the namespaces never overlap forward.
"""
from __future__ import annotations

from kg_engine.scrub import Scrubber


# --- F6: a non-name leading word must not shield the real full name beginning inside the bigram -----

def test_person_leading_nonname_does_not_shield_real_name():
    # "Researcher" is Title-Case but not a given name; the old non-overlapping scan matched
    # "Researcher Alan", spared it, and skipped past — leaking "Alan Turing". It must now redact.
    scrubbed, _ = Scrubber("high").scrub("Researcher Alan Turing said it works.")
    assert "Alan Turing" not in scrubbed, scrubbed
    assert "⟦PERSON" in scrubbed, scrubbed
    # the non-name leading word is left verbatim
    assert "Researcher" in scrubbed, scrubbed


def test_person_sentence_initial_nonname_does_not_shield_real_name():
    # Sentence-initial "Yesterday" capitalizes a non-name; the real name must still be caught.
    scrubbed, _ = Scrubber("high").scrub("Yesterday Michael Smith left.")
    assert "Michael Smith" not in scrubbed, scrubbed
    assert "⟦PERSON" in scrubbed, scrubbed
    assert "Yesterday" in scrubbed, scrubbed


def test_person_title_word_nonname_does_not_shield_real_name():
    # "Professor" (lowercase-internal title word, not in the courtesy-title rule) before a real name.
    scrubbed, _ = Scrubber("high").scrub("Professor David Ricardo wrote that.")
    assert "David Ricardo" not in scrubbed, scrubbed
    assert "⟦PERSON" in scrubbed, scrubbed


def test_person_leading_genuine_name_still_redacts():
    # A full name with NO leading word still redacts (the original happy path).
    scrubbed, _ = Scrubber("high").scrub("Alan Turing proved it.")
    assert "Alan Turing" not in scrubbed and "⟦PERSON" in scrubbed, scrubbed


def test_person_nonname_bigram_alone_is_left():
    # A pure non-name Title-Case bigram (a concept) with nothing to rescan is untouched.
    scrubbed, _ = Scrubber("high").scrub("Creative Destruction reshapes the market.")
    assert scrubbed == "Creative Destruction reshapes the market.", scrubbed


def test_person_placeholder_consistency_after_rescan():
    # Rescanning past a spared non-name must not break placeholder id consistency: the same name gets
    # one id, and restore() round-trips the original text.
    src = "Researcher Alan Turing met Alan Turing; then Michael Smith arrived."
    sc = Scrubber("high")
    scrubbed, mapping = sc.scrub(src)
    assert scrubbed.count("⟦PERSON:1⟧") == 2, scrubbed   # both "Alan Turing" share id 1
    assert "⟦PERSON:2⟧" in scrubbed, scrubbed             # "Michael Smith" is id 2
    assert Scrubber.restore(scrubbed, mapping) == src, scrubbed


# --- F11/M5: bare digit runs / page ranges are NOT phones; real phone formats still are --------------

def test_phone_does_not_redact_dash_page_range():
    # "pages 100-200" is a figure range, not a phone number.
    scrubbed, _ = Scrubber("medium").scrub("See pages 100-200 for the proof.")
    assert "⟦PHONE" not in scrubbed, scrubbed
    assert scrubbed == "See pages 100-200 for the proof.", scrubbed


def test_phone_does_not_redact_bare_six_digit_run():
    # A bare 6-digit number in prose must survive (no phone structure).
    scrubbed, _ = Scrubber("medium").scrub("Run number 123456 completed.")
    assert "⟦PHONE" not in scrubbed, scrubbed
    assert scrubbed == "Run number 123456 completed.", scrubbed


def test_phone_does_not_redact_bare_seven_digit_run():
    scrubbed, _ = Scrubber("medium").scrub("Entry 1234567 in the log.")
    assert "⟦PHONE" not in scrubbed, scrubbed
    assert scrubbed == "Entry 1234567 in the log.", scrubbed


def test_phone_still_redacts_real_us_number():
    scrubbed, _ = Scrubber("medium").scrub("Call 415-555-2671 to reach support.")
    assert "415-555-2671" not in scrubbed and "⟦PHONE" in scrubbed, scrubbed


def test_phone_still_redacts_international_number():
    scrubbed, _ = Scrubber("medium").scrub("Reach the desk at +44 20 7946 0958 today.")
    assert "7946 0958" not in scrubbed and "⟦PHONE" in scrubbed, scrubbed


def test_phone_still_redacts_parenthesized_area_code():
    scrubbed, _ = Scrubber("medium").scrub("Dial (415) 555-2671 now.")
    assert "555-2671" not in scrubbed and "⟦PHONE" in scrubbed, scrubbed


# --- M4: a literal placeholder must round-trip ACROSS calls via the consumer's RETURNED-map restore --

def _server_restore_map(*calls):
    """Mimic the MCP server: accumulate ONLY each scrub() call's returned new_mapping (server._scrub_map
    is built from the returned maps, never from scrubber._mapping), then return (outputs, restore_map)."""
    sc = Scrubber("high")
    outs, restore_map = [], {}
    for text in calls:
        out, mapping = sc.scrub(text)
        restore_map.update(mapping)
        outs.append(out)
    return outs, restore_map


def test_literal_placeholder_round_trips_across_calls_via_returned_map():
    # Section A's prose explains the redaction syntax (a LITERAL ⟦EMAIL:1⟧); section B then has a real
    # email. The boundary restores from the RETURNED maps only — the literal must NOT expand, and the
    # real email must take a DIFFERENT id (the reserved number is skipped cumulatively).
    a = "The marker ⟦EMAIL:1⟧ denotes a redacted email in our docs."
    b = "Now mail the real address bob@acme.com today."
    (out_a, out_b), restore_map = _server_restore_map(a, b)
    # the literal survived scrub() verbatim ...
    assert out_a == a, out_a
    # ... the real email got id 2, never colliding with the reserved literal id 1 ...
    assert "⟦EMAIL:2⟧" in out_b and "⟦EMAIL:1⟧" not in out_b, out_b
    assert "bob@acme.com" not in out_b, out_b
    # ... and BOTH round-trip through the server-style restore map (the boundary path).
    assert Scrubber.restore(out_a, restore_map) == a, Scrubber.restore(out_a, restore_map)
    assert Scrubber.restore(out_b, restore_map) == b, Scrubber.restore(out_b, restore_map)


def test_literal_placeholder_identity_is_in_returned_map():
    # The protection lives in the RETURNED mapping (what the boundary consults), not just _mapping.
    _, mapping = Scrubber("high").scrub("Doc marker ⟦PERSON:1⟧ explained.")
    assert mapping.get("⟦PERSON:1⟧") == "⟦PERSON:1⟧", mapping


def test_reset_clears_reserved_literal_namespace():
    # reset() must also drop the cumulative reserved-literal set, or a fresh session would keep skipping
    # numbers it has no reason to. (reset() is uncalled today; this pins its documented full-clear.)
    sc = Scrubber("high")
    sc.scrub("marker ⟦EMAIL:5⟧ here")
    assert "⟦EMAIL:5⟧" in sc._reserved_placeholders
    sc.reset()
    assert sc._reserved_placeholders == set() and sc._mapping == {} and sc._counters == {}
