"""Egress PII/secret scrubbing (§1.9).

Before any source text is handed to a subagent for semantic work, redact secrets/keys (always) and
PII (per the sensitivity setting) using *consistent* placeholders so relational structure survives
(``⟦PERSON:1⟧ attacked_by ⟦PERSON:2⟧``). The mapping stays local; ``restore`` rebuilds the original
span for the canonical record. This protects the egress, not the local canon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# sensitivity tiers -> which categories are scrubbed. Secrets are ALWAYS scrubbed.
SENSITIVITY = {
    "low": {"SECRET"},
    "medium": {"SECRET", "EMAIL", "PHONE", "SSN", "CC", "IP", "CREDURL"},
    "high": {"SECRET", "EMAIL", "PHONE", "SSN", "CC", "IP", "CREDURL", "PERSON", "ADDRESS"},
}

# Order matters: most specific / highest-risk first so a secret isn't partially eaten by a weaker rule.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SECRET", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("SECRET", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),                      # AWS access key id
    ("SECRET", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),                     # GitHub tokens
    ("SECRET", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),                   # Slack tokens
    ("SECRET", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),                            # generic sk- api keys
    ("SECRET", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),  # JWT
    ("SECRET", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*")),                # bare Bearer token
    # keyword=value secrets. The key name may be embedded ([\w.-]* on both sides) so `aws_secret_access_key`
    # matches even though `_` is a word char; the value may be quoted (multi-word, kept whole) or a bare
    # run (excludes placeholder brackets so an already-redacted secret is never re-wrapped).
    ("SECRET", re.compile(
        r"(?i)[\w.-]*(?:api[_-]?key|secret|token|password|passwd|pwd)[\w.-]*\s*[:=]\s*"
        r"(?:\"[^\"]{4,}\"|'[^']{4,}'|[^\s'\"⟦⟧]{6,})")),
    ("CREDURL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.I)),  # creds in URL
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # 13-16 digit card runs; anchored on a digit at BOTH ends so a trailing space/dash is not captured
    # (which would break placeholder consistency for the same number written two ways).
    ("CC", re.compile(r"\b\d(?:[ -]?\d){12,15}\b")),
    # IPv4 and (conservative) full-form IPv6 BEFORE phone, so the phone rule can't eat IP octets first.
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("IP", re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3}[\s.-]?\d{3,4}(?!\d)")),
    # Person heuristic (high only): Title-Case bigrams, optionally preceded by a courtesy title.
    ("PERSON", re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")),
    ("PERSON", re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")),
    ("ADDRESS", re.compile(r"\b\d{1,5}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive)\b")),
]

_PLACEHOLDER_RE = re.compile(r"⟦([A-Z]+):(\d+)⟧")


@dataclass
class Scrubber:
    sensitivity: str = "medium"
    extra_terms: dict[str, list[str]] = field(default_factory=dict)  # category -> literal terms to redact

    def __post_init__(self) -> None:
        # Counter / value->placeholder / placeholder->value persist ACROSS scrub() calls on this
        # instance, so a session that scrubs section-by-section shares ONE placeholder namespace.
        # (Resetting per call made ⟦EMAIL:1⟧ collide across calls and corrupted the accumulated
        # restore map, recovering the wrong original text into the canon.)
        self._counters: dict[str, int] = {}
        self._value_to_ph: dict[tuple[str, str], str] = {}
        self._mapping: dict[str, str] = {}

    def reset(self) -> None:
        """Clear the accumulated placeholder namespace (start a fresh scrubbing session)."""
        self._counters, self._value_to_ph, self._mapping = {}, {}, {}

    def _active(self) -> set[str]:
        return SENSITIVITY.get(self.sensitivity, SENSITIVITY["medium"])

    def scrub(self, text: str) -> tuple[str, dict[str, str]]:
        """Return (scrubbed_text, mapping) where mapping is the placeholder -> original value pairs
        NEWLY created in this call (the instance accumulates the full map across calls)."""
        if text is None:
            return "", {}
        active = self._active()
        new_mapping: dict[str, str] = {}

        def placeholder(cat: str, value: str) -> str:
            key = (cat, value)
            if key in self._value_to_ph:
                return self._value_to_ph[key]
            self._counters[cat] = self._counters.get(cat, 0) + 1
            ph = f"⟦{cat}:{self._counters[cat]}⟧"
            self._value_to_ph[key] = ph
            self._mapping[ph] = value
            new_mapping[ph] = value
            return ph

        out = text
        # caller-supplied literal terms first (e.g. a names list for this corpus)
        for cat, terms in self.extra_terms.items():
            if cat not in active and cat != "SECRET":
                continue
            for term in sorted(terms, key=len, reverse=True):
                if not term:
                    continue
                out = re.sub(re.escape(term), lambda m, c=cat: placeholder(c, m.group(0)), out)

        for cat, pat in _PATTERNS:
            if cat not in active:
                continue
            out = pat.sub(lambda m, c=cat: placeholder(c, m.group(0)), out)
        return out, new_mapping

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        """Replace placeholders in `text` with their original values.

        Iterates to a fixpoint so a value that itself contains a placeholder (nested redaction) is
        fully expanded.
        """
        def repl(m: re.Match) -> str:
            return mapping.get(m.group(0), m.group(0))

        for _ in range(len(mapping) + 1):
            new = _PLACEHOLDER_RE.sub(repl, text)
            if new == text:
                return new
            text = new
        return text

    @staticmethod
    def leaks(scrubbed: str, secrets: list[str]) -> list[str]:
        """Return any seeded secret that still appears verbatim in the scrubbed text (test aid)."""
        return [s for s in secrets if s and s in scrubbed]
