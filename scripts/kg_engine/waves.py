"""Deterministic resolver for the `/kg-build` extraction wave size (orchestration knob, not engine config).

`/kg-build` launches one `kg-extractor` subagent per `##` section, in BOUNDED PARALLEL WAVES whose size
this resolves. It is an ORCHESTRATION parameter consumed by the command/skill, never by `KGEngine` — the
engine reads no env var for it (cf. `tests/test_manifests.py`). The runtime resolution actually happens in
the command's pure-Bash Step 0 (no venv/PYTHONPATH dependency); this module is the authoritative,
unit-tested REFERENCE that the Bash mirrors (the same Python/non-Python mirroring pattern as
`canonmerge.py` ↔ `Canon._merge_into_existing`), and is also runnable as `python -m kg_engine.waves [arg]`
for CI / any python-having caller.

Precedence: explicit inline arg > user_config (`CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE`) > default. A
value that is present but invalid at a given level falls straight to the default (it does NOT cascade to
the next level) — mirroring the Bash `${2:-${ENV:-6}}` then validate/clamp. Validation: parse to an
integer; unset / non-numeric / below the minimum → the default; above the maximum → clamp to the maximum.
"""
from __future__ import annotations

import os

DEFAULT_WAVE_SIZE = 6
MIN_WAVE_SIZE = 1
MAX_WAVE_SIZE = 10
WAVE_ENV = "CLAUDE_PLUGIN_OPTION_EXTRACT_WAVE_SIZE"


def resolve_wave_size(arg=None, env=None, *, default: int = DEFAULT_WAVE_SIZE,
                      lo: int = MIN_WAVE_SIZE, hi: int = MAX_WAVE_SIZE) -> int:
    """Resolve the bounded wave size from an inline override and the user_config value.

    This is the REFERENCE for the command's pure-Bash Step 0, which is what actually runs — so the rules
    here mirror that Bash EXACTLY (the only realistic inputs on which a looser Python would silently drift):

    - **Precedence (Bash `${2:-${ENV:-6}}`):** the inline `arg` wins when it is a non-empty string — even
      whitespace-only, which is a *present* value and does NOT cascade to `env` (only a truly empty/unset
      `arg` falls through to `env`, then to `default`).
    - **Validation (Bash `case … in ''|*[!0-9]*`):** the chosen value must be a non-empty run of **ASCII
      digits** — anything with a sign, decimal point, or surrounding/embedded whitespace (`'+5'`, `' 4 '`,
      `'6.5'`) is invalid and resolves to `default` (it does not cascade to the next level).
    - **Clamp:** an all-digit value `< lo` → `default`; `> hi` → `hi`. Always returns an int in `[lo, hi]`.

    (The Bash and this reference agree on every input up to the shell's integer max; an absurd ≥19-digit
    literal is out of scope — Bash cannot clamp past `intmax_t` — and is never a realistic wave size.)
    """
    raw = arg if (arg is not None and str(arg) != "") else env
    if raw is None or str(raw) == "":
        return default
    s = str(raw)
    if not (s.isascii() and s.isdigit()):  # ASCII digits only — matches the Bash `*[!0-9]*` reject
        return default
    n = int(s)
    if n < lo:
        return default
    return min(n, hi)


def main(argv=None) -> int:
    """`python -m kg_engine.waves [wave_size]` → print the resolved wave size. Reads the inline override
    from argv[1] (if given) and the user_config value from the environment, so the command's Bash can
    delegate to the identical logic when a venv python is on PATH."""
    import sys
    argv = sys.argv if argv is None else argv
    arg = argv[1] if len(argv) > 1 else None
    print(resolve_wave_size(arg, os.environ.get(WAVE_ENV)))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim over resolve_wave_size
    raise SystemExit(main())
