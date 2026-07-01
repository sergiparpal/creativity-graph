"""Regression tests for scrub.py CC (17-19 digit PANs) and compressed-IPv6 redaction fixes."""
from __future__ import annotations

from kg_engine.scrub import Scrubber


def _scrub(text: str, sensitivity: str = "medium") -> str:
    return Scrubber(sensitivity=sensitivity).scrub(text)[0]


# ---- CC: PANs longer than 16 digits must not leak (was {12,15} => 13-16 only) ----

def test_17_digit_pan_redacted():
    pan = "1" * 17
    out = _scrub(f"card {pan} on file")
    assert pan not in out
    assert "⟦CC:" in out


def test_19_digit_pan_redacted():
    pan = "4" * 19
    out = _scrub(f"pay {pan} now")
    assert pan not in out
    assert "⟦CC:" in out


def test_space_grouped_19_digit_card_redacted():
    pan = "1234 5678 9012 3456 789"  # 19 digits, space-grouped
    out = _scrub(f"card is {pan}.")
    assert "1234 5678 9012 3456 789" not in out
    assert "⟦CC:" in out


def test_16_digit_card_still_redacted():
    pan = "4111111111111111"
    out = _scrub(f"visa {pan}")
    assert pan not in out
    assert "⟦CC:" in out


# ---- IPv6: '::'-compressed forms must be redacted ----

def test_compressed_ipv6_2001_db8_redacted():
    out = _scrub("connect to 2001:db8::1 please")
    assert "2001:db8::1" not in out
    assert "⟦IP:" in out


def test_loopback_ipv6_redacted():
    out = _scrub("bind ::1 locally")
    assert "::1" not in out
    assert "⟦IP:" in out


def test_link_local_ipv6_redacted():
    out = _scrub("iface fe80::1 up")
    assert "fe80::1" not in out
    assert "⟦IP:" in out


def test_bare_double_colon_redacted():
    out = _scrub("route :: default")
    # the bare '::' any-address form is consumed
    assert "route ⟦IP:" in out


def test_full_form_ipv6_still_redacted():
    addr = "2001:0db8:0000:0000:0000:0000:0000:0001"
    out = _scrub(f"host {addr}")
    assert addr not in out
    assert "⟦IP:" in out


# ---- No over-redaction of ordinary prose / times / short number lists ----

def test_plain_sentence_not_over_redacted():
    text = "Creative destruction reshapes the market over time."
    assert _scrub(text) == text


def test_clock_time_not_redacted():
    text = "The meeting starts at 12:30 sharp."
    out = _scrub(text)
    assert "12:30" in out
    assert "⟦IP:" not in out


def test_short_number_list_not_redacted():
    text = "Steps 1, 2, 3 and figures 10 20 30 remain."
    out = _scrub(text)
    assert out == text
    assert "⟦CC:" not in out
    assert "⟦IP:" not in out
