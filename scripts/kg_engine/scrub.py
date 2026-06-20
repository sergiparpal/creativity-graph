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
    # value class excludes the placeholder brackets so an already-redacted secret is never re-wrapped
    ("SECRET", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\"⟦⟧]{6,})")),
    ("CREDURL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.I)),  # creds in URL
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CC", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3}[\s.-]?\d{3,4}(?!\d)")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
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

    def _active(self) -> set[str]:
        return SENSITIVITY.get(self.sensitivity, SENSITIVITY["medium"])

    def scrub(self, text: str) -> tuple[str, dict[str, str]]:
        """Return (scrubbed_text, mapping) where mapping is placeholder -> original value."""
        active = self._active()
        mapping: dict[str, str] = {}
        value_to_ph: dict[tuple[str, str], str] = {}
        counters: dict[str, int] = {}

        def placeholder(cat: str, value: str) -> str:
            key = (cat, value)
            if key in value_to_ph:
                return value_to_ph[key]
            counters[cat] = counters.get(cat, 0) + 1
            ph = f"⟦{cat}:{counters[cat]}⟧"
            value_to_ph[key] = ph
            mapping[ph] = value
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
        return out, mapping

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
