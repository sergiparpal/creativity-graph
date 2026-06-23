"""Regression tests for the egress scrubber (§1.9): two narrow correctness bugs.

F6  — the bare PERSON bigram rule scanned non-overlapping, so a non-name Title-Case word immediately
      before a real full name ("Researcher Alan Turing") matched the FIRST bigram, was spared by the
      given-name gate, and the scan resumed PAST the spared span — leaking the real name verbatim at
      sensitivity='high'. The fix rescans from the second token when the first is a non-name.
F11/M5 — at the default sensitivity='medium' the PHONE pattern matched bare 6-7 digit prose runs and
      dash-separated page ranges ("pages 100-200", bare "100200"), over-redacting figures into ⟦PHONE⟧
      (restored on write, but it degrades extraction). The fix requires phone-ish structure.
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
