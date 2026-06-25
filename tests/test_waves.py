"""Tests for the /kg-build extraction wave-size resolver (kg_engine.waves).

Covers the deterministic Python reference (resolve_wave_size + the `python -m kg_engine.waves` CLI) AND a
drift-guard that runs the command's ACTUAL pure-Bash mirror (extracted from commands/kg-build.md) over the
same matrix, so the two implementations can never diverge on a realistic input.

Contract: precedence inline-arg > user_config(env) > default(6); parse to int; unset/non-numeric/<1 ->
default; >10 -> clamp to 10; always in [1, 10].
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kg_engine.waves import (
    DEFAULT_WAVE_SIZE,
    MAX_WAVE_SIZE,
    MIN_WAVE_SIZE,
    WAVE_ENV,
    main,
    resolve_wave_size,
)

ROOT = Path(__file__).resolve().parents[1]


def test_constants_are_the_documented_bounds():
    assert (DEFAULT_WAVE_SIZE, MIN_WAVE_SIZE, MAX_WAVE_SIZE) == (6, 1, 10)
    assert WAVE_ENV == "CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE"


def test_default_when_unset():
    assert resolve_wave_size() == 6
    assert resolve_wave_size(None, None) == 6
    assert resolve_wave_size("", "") == 6
    assert resolve_wave_size("   ", "   ") == 6  # whitespace-only is treated as unset


@pytest.mark.parametrize("raw", ["abc", "3x", "6.5", "0x4", "", "   ", "+", "-"])
def test_non_numeric_falls_back_to_default(raw):
    assert resolve_wave_size(raw) == 6
    assert resolve_wave_size(None, raw) == 6


@pytest.mark.parametrize("raw", ["0", "-1", "-10"])
def test_below_minimum_falls_back_to_default(raw):
    # < 1 resolves to the DEFAULT (6), not clamped up to the minimum (1)
    assert resolve_wave_size(raw) == 6


@pytest.mark.parametrize("raw,expected", [("11", 10), ("100", 10), ("99999", 10)])
def test_above_maximum_clamps_to_ten(raw, expected):
    assert resolve_wave_size(raw) == expected
    assert resolve_wave_size(None, raw) == expected


@pytest.mark.parametrize("raw,expected", [("1", 1), ("5", 5), ("6", 6), ("9", 9), ("10", 10)])
def test_in_range_passes_through(raw, expected):
    assert resolve_wave_size(raw) == expected


def test_whitespace_padded_is_invalid_like_the_bash_runtime():
    # the runtime is the command's pure-Bash, which rejects any non-ASCII-digit char (incl. surrounding
    # whitespace and a leading sign) -> default. The reference mirrors it EXACTLY, so a padded/signed
    # value resolves to the default 6, not its inner number (this is what actually happens at runtime).
    assert resolve_wave_size("  4  ") == 6
    assert resolve_wave_size(" 4") == 6
    assert resolve_wave_size("4 ") == 6
    assert resolve_wave_size("+5") == 6


def test_int_and_leading_zero_inputs_resolve_to_their_value():
    assert resolve_wave_size(7) == 7        # an int arg (not a str) is accepted
    assert resolve_wave_size(99) == 10
    assert resolve_wave_size(0) == 6
    assert resolve_wave_size("07") == 7     # leading zeros are read base-10 (matches Bash's [ ] compare)
    assert resolve_wave_size("007") == 7


def test_precedence_inline_arg_beats_env_beats_default():
    assert resolve_wave_size("3", "8") == 3      # explicit arg wins
    assert resolve_wave_size(None, "8") == 8     # no arg -> env
    assert resolve_wave_size("", "8") == 8       # empty arg -> env
    assert resolve_wave_size(None, None) == 6    # neither -> default


def test_invalid_explicit_arg_falls_to_default_not_env():
    # a present-but-invalid arg resolves to the default; it does NOT cascade down to the env value
    assert resolve_wave_size("abc", "8") == 6
    assert resolve_wave_size("0", "8") == 6


# ---- the CLI shim ----------------------------------------------------------
def test_cli_prints_resolved_value(capsys, monkeypatch):
    monkeypatch.delenv(WAVE_ENV, raising=False)
    assert main(["waves", "3"]) == 0
    assert capsys.readouterr().out.strip() == "3"


def test_cli_clamps_and_reads_env(capsys, monkeypatch):
    monkeypatch.setenv(WAVE_ENV, "9")
    assert main(["waves"]) == 0                      # no arg -> env
    assert capsys.readouterr().out.strip() == "9"
    assert main(["waves", "11"]) == 0                # arg beats env, clamped
    assert capsys.readouterr().out.strip() == "10"


# ---- drift guard: the command's pure-Bash mirror must agree with the resolver ----
def _extract_command_bash() -> str:
    """Pull the four wave-size resolution lines out of commands/kg-build.md so the test exercises the
    REAL snippet (not a copy), then append an `echo` so we can read the resolved value."""
    text = (ROOT / "commands" / "kg-build.md").read_text(encoding="utf-8")
    prefixes = ('WAVE_RAW=', 'case "$WAVE_RAW"', '[ "$WAVE_SIZE" -lt', '[ "$WAVE_SIZE" -gt')
    lines = [ln for ln in text.splitlines() if ln.strip().startswith(prefixes)]
    assert len(lines) == 4, f"expected 4 resolution lines in kg-build.md, found {len(lines)}: {lines}"
    return "\n".join(lines) + '\necho "$WAVE_SIZE"\n'


def _run_bash(script: str, arg, env_val):
    env = {"PATH": __import__("os").environ.get("PATH", "")}
    if env_val is not None:
        env[WAVE_ENV] = env_val
    argv = ["bash", "-c", script, "bash", "SRC"]
    if arg is not None:
        argv.append(arg)
    out = subprocess.run(argv, capture_output=True, text=True, env=env, check=True)
    return out.stdout.strip()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable (e.g. Windows runner)")
@pytest.mark.parametrize("arg,env_val", [
    ("1", None), ("5", None), ("6", None), ("10", None), ("7", None),
    ("11", None), ("100", None), ("0", None), ("-1", None), ("-3", None),
    ("abc", None), ("3x", None),
    (None, "8"), (None, "11"), (None, "0"), (None, "abc"), (None, None),
    ("3", "8"), ("abc", "8"), ("0", "8"),
    # the previously-divergent inputs the reference was tightened to mirror (signed / whitespace-padded /
    # whitespace-only-cascades / leading-zero) — they must now AGREE on every one:
    ("+5", None), ("  4  ", None), (" 4", None), ("4 ", None),
    ("   ", "8"), ("07", None), ("007", None), ("010", None),
])
def test_command_bash_mirror_matches_resolver(arg, env_val):
    script = _extract_command_bash()
    bash_result = int(_run_bash(script, arg, env_val))
    assert bash_result == resolve_wave_size(arg, env_val), (
        f"bash and resolver disagree for arg={arg!r} env={env_val!r}: "
        f"bash={bash_result} resolver={resolve_wave_size(arg, env_val)}")
    assert MIN_WAVE_SIZE <= bash_result <= MAX_WAVE_SIZE
