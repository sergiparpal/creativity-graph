"""Egress PII/secret scrubbing (§1.9).

Before any source text is handed to a subagent for semantic work, redact secrets/keys (always) and
PII (per the sensitivity setting) using *consistent* placeholders so relational structure survives
(``⟦PERSON:1⟧ attacked_by ⟦PERSON:2⟧``). The mapping stays local; ``restore`` rebuilds the original
span for the canonical record. This protects the egress, not the local canon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The single declared set of valid redaction categories. Every category label used in SENSITIVITY,
# _PATTERNS, and the placeholder() allocator must be a member; the import-time asserts below trip on a
# typo (e.g. "CRED_URL") so a mistyped/orphaned category fails LOUDLY instead of silently disabling
# scrubbing for that category at some sensitivity tier (the worst failure mode for a safety gate).
ALL_CATEGORIES = frozenset({
    "SECRET", "EMAIL", "PHONE", "SSN", "CC", "IP", "CREDURL", "PERSON", "ADDRESS",
})

# sensitivity tiers -> which categories are scrubbed. Secrets are ALWAYS scrubbed.
SENSITIVITY = {
    "low": {"SECRET"},
    "medium": {"SECRET", "EMAIL", "PHONE", "SSN", "CC", "IP", "CREDURL"},
    "high": {"SECRET", "EMAIL", "PHONE", "SSN", "CC", "IP", "CREDURL", "PERSON", "ADDRESS"},
}
assert all(s <= ALL_CATEGORIES for s in SENSITIVITY.values()), \
    "SENSITIVITY tier names a category outside ALL_CATEGORIES"

# A small lexicon of common given names. The bare PERSON bigram rule fires only when the first token
# is a recognized given name (or a courtesy title precedes it / a caller supplies extra_terms), so a
# Title-Case *concept* bigram ("Creative Destruction", "Knowledge Graph") is NOT mistaken for a name —
# critical for the conceptual-document input class this engine targets (scrub-4).
_GIVEN_NAMES = frozenset({
    "aaron", "adam", "adrian", "alan", "albert", "alex", "alexander", "alexandra", "alice", "alicia",
    "amanda", "amy", "andrea", "andrew", "angela", "ann", "anna", "anne", "anthony", "arthur", "ava",
    "barbara", "ben", "benjamin", "beth", "bob", "bobby", "brandon", "brian", "bruce", "carl", "carlos",
    "carol", "caroline", "catherine", "charles", "charlie", "chris", "christine", "christopher", "claire",
    "claudia", "daniel", "dave", "david", "dennis", "diana", "donald", "donna", "dorothy", "douglas",
    "edward", "elaine", "eleanor", "elizabeth", "emily", "emma", "eric", "ethan", "eugene", "evelyn",
    "frances", "frank", "fred", "gary", "george", "gerald", "grace", "greg", "gregory", "hannah",
    "harold", "harry", "heather", "helen", "henry", "isabella", "jack", "jacob", "james", "jane", "janet",
    "jason", "jean", "jeff", "jeffrey", "jennifer", "jeremy", "jerry", "jessica", "joan", "joe", "john",
    "johnny", "jonathan", "jordan", "jose", "joseph", "joshua", "joyce", "juan", "judith", "judy", "julia",
    "julie", "justin", "karen", "katherine", "kathleen", "kathryn", "keith", "kelly", "ken", "kenneth",
    "kevin", "kim", "kimberly", "larry", "laura", "lawrence", "lewis", "linda", "lisa", "liz", "logan",
    "louis", "lucas", "lucy", "luke", "margaret", "maria", "marie", "marilyn", "mark", "martha", "martin",
    "mary", "matthew", "megan", "melissa", "michael", "michelle", "mike", "mildred", "nancy", "natalie",
    "nathan", "nicholas", "nicole", "noah", "norma", "oliver", "olivia", "pamela", "patricia", "patrick",
    "paul", "peter", "philip", "phillip", "rachel", "ralph", "randy", "raymond", "rebecca", "richard",
    "rick", "robert", "robin", "roger", "ronald", "rose", "roy", "russell", "ruth", "ryan", "samuel",
    "sandra", "sara", "sarah", "scott", "sean", "sharon", "shirley", "simon", "sophia", "stephanie",
    "stephen", "steve", "steven", "susan", "teresa", "terry", "theresa", "thomas", "tim", "timothy",
    "todd", "tom", "tony", "tyler", "victoria", "vincent", "virginia", "walter", "wayne", "william", "zoe",
})


def _is_personal_name(match: str) -> bool:
    """Bare-bigram PERSON gate (scrub-4): redact only if the first token is a known given name."""
    first = match.split()[0].lower()
    return first in _GIVEN_NAMES


# Order matters: most specific / highest-risk first so a secret isn't partially eaten by a weaker rule.
# Every SECRET-class rule precedes EMAIL/PHONE/CC so a structured secret is consumed WHOLE — never left
# with only a digit fragment redacted by the phone/CC rule while the rest leaks verbatim (scrub-3).
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SECRET", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("SECRET", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),                      # AWS access key id
    ("SECRET", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),                     # GitHub tokens
    ("SECRET", re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}\b")),                       # GitLab PAT
    ("SECRET", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),                   # Slack tokens
    ("SECRET", re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{20,}\b")),             # Stripe secret keys
    # Anthropic sk-ant-… / OpenAI sk-… / other sk- keys: allow `_`/`-` inside so the hyphenated
    # Anthropic form is consumed whole (the old [A-Za-z0-9]{20,} stopped at the first `-`).
    ("SECRET", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),                          # sk- api keys (incl. sk-ant-)
    ("SECRET", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),                          # Google API key
    ("SECRET", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),  # JWT
    ("SECRET", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*")),                # bare Bearer token
    # keyword=value secrets. The key fragment is BOUNDED and the leading word-char run is ANCHORED
    # (no floating greedy [\w.-]* prefix) so a long base64/hex/word run can't drive O(N^2) backtracking
    # at every position (scrub-1). The value may be quoted (multi-word, kept whole) or a bare run
    # (excludes placeholder brackets so an already-redacted secret is never re-wrapped).
    ("SECRET", re.compile(
        r"(?i)(?<![\w.-])[\w.-]{0,40}?(?:api[_-]?key|secret|token|password|passwd|pwd)[\w.-]{0,40}?\s*[:=]\s*"
        r"(?:\"[^\"]{4,}\"|'[^']{4,}'|[^\s'\"⟦⟧]{6,})")),
    # Natural-language keyword forms the `[:=]`-anchored rule above can't span (an intervening linking
    # verb or a plain space breaks its bounded `[\w.-]{0,40}?` key fragment): "the password is hunter2",
    # "api token abc123def" (scrub-3-nl). Same keyword list; the value is a single bounded run excluding
    # placeholder brackets. To keep prose over-redaction low, the value is taken only when EITHER a
    # linking-verb connector signals intent ("<kw> is/was/equals <value>"), OR — for the connector-less
    # "<kw> <value>" form — the value carries a digit (an entropy signal, so "the token works" is spared
    # while "token abc123def" is not). Anchored at the keyword (no floating prefix), so it stays linear.
    ("SECRET", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\s+(?:"
        r"(?:is|was|are|were|equals?)\s+([^\s'\"⟦⟧]{6,})"          # linking verb -> permissive value
        r"|(?=[^\s'\"⟦⟧]*\d)([^\s'\"⟦⟧]{6,})"                     # bare space -> value must carry a digit
        r")")),
    # Generic high-entropy fallback: a long unbroken (>=32 char) token the named rules above didn't
    # catch, so a bespoke key never falls through to a weaker PII rule that would redact only a digit
    # fragment of it (scrub-3). REQUIRES a digit AND a letter (an actual high-entropy mix) and excludes
    # hyphens, so ordinary long prose — hyphenated compounds, all-letter CamelCase / snake_case
    # identifiers — is NOT mass-redacted (that over-redaction degraded extraction; the digit+letter
    # requirement keeps key/base64/hex tokens while sparing words). The trailing `(?!@)` keeps it from
    # eating a long machine-generated EMAIL local part and leaving the @domain to leak (scrub-5): the
    # named EMAIL rule below then claims the whole address as one ⟦EMAIL⟧.
    ("SECRET", re.compile(r"\b(?=[A-Za-z0-9_]*[0-9])(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{32,}\b(?!@)")),
    # Base64-shaped high-entropy fallback (scrub-4): a >=32-char run over the base64 alphabet (incl. the
    # '/' and '+' the `[A-Za-z0-9_]` fallback above excludes), with optional '=' padding, so a bare
    # base64 secret — e.g. an AWS *secret* access key like "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    # which the named AWS rule (access key ID only) misses — is consumed whole instead of being split at
    # the first '/' and leaking. REQUIRES a digit AND a letter (same anti-prose guard as above), and at
    # least one '/'/'+' so it doesn't merely duplicate the `_`-alphabet fallback (which already fires on
    # pure-alnum runs). The `(?!@)` mirrors scrub-5 so an email is never half-eaten.
    ("SECRET", re.compile(
        r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/]*[0-9])(?=[A-Za-z0-9+/]*[A-Za-z])(?=[A-Za-z0-9]*[+/])"
        r"[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=@])")),
    ("CREDURL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.I)),  # creds in URL
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # 13-16 digit card runs; anchored on a digit at BOTH ends so a trailing space/dash is not captured
    # (which would break placeholder consistency for the same number written two ways).
    ("CC", re.compile(r"\b\d(?:[ -]?\d){12,15}\b")),
    # IPv4 and (conservative) full-form IPv6 BEFORE phone, so the phone rule can't eat IP octets first.
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("IP", re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b")),
    # Phone numbers, but NOT bare 6-7 digit prose runs or dash-separated page ranges (F11/M5): the old
    # two-group `\d{3}[\s.-]?\d{3,4}` matched "100-200" and even separator-less "100200", over-redacting
    # ordinary figures into ⟦PHONE⟧ (restored on write, but it degraded extraction). A real number must
    # carry phone-ish structure — a `+`/country-code prefix, a parenthesized area code, OR three
    # separator-grouped runs (>=10 digits) — so a bare two-group number is no longer enough to qualify.
    ("PHONE", re.compile(
        r"(?<!\d)(?:"
        r"\+\d{1,3}[\s.-]?(?:\(\d{2,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,4}){1,3}"  # +country (area)? grp grp …
        r"|\(\d{2,4}\)[\s.-]?\d{3}[\s.-]?\d{3,4}"                                  # (415) 555-2671
        r"|\d{3}[\s.-]\d{3}[\s.-]\d{4}"                                            # 415-555-2671 (3-3-4, >=10 digits)
        r")(?!\d)")),
    # Person heuristic (high only): a courtesy title is sufficient; a bare Title-Case bigram is gated
    # behind the given-name lexicon (_is_personal_name) so concept bigrams survive (scrub-4).
    ("PERSON", re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b")),
    # Groups (first | gap | second) so scrub() can resume at the SECOND token's start when the first is
    # a non-name (F6): a real full name beginning inside a spared bigram is still re-tested, not skipped.
    ("PERSON", re.compile(r"\b([A-Z][a-z]+)(\s+)([A-Z][a-z]+)\b")),  # gated by _is_personal_name in scrub()
    ("ADDRESS", re.compile(r"\b\d{1,5}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive)\b")),
]

# The bare PERSON bigram pattern is the second-from-last rule; it (and only it) is gated by the
# lexicon. Bound by position but asserted by pattern so a future reorder/insertion fails LOUDLY here
# instead of silently gating the wrong rule (e.g. turning off PERSON or mis-gating ADDRESS).
_BARE_PERSON_RE = _PATTERNS[-2][1]
assert _BARE_PERSON_RE.pattern == r"\b([A-Z][a-z]+)(\s+)([A-Z][a-z]+)\b", \
    "PERSON rule order changed — re-bind _BARE_PERSON_RE to the bare Title-Case bigram pattern"

assert {cat for cat, _ in _PATTERNS} <= ALL_CATEGORIES, \
    "_PATTERNS names a category outside ALL_CATEGORIES"

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
        # Every ⟦CAT:N⟧-shaped string that has appeared as a LITERAL in any call's source prose, kept
        # CUMULATIVELY across calls. The allocator skips any of these so a freshly-issued placeholder can
        # never collide with a literal a later restore would over-expand (scrub-2/M4). It also drives the
        # identity entries emitted into each call's returned mapping (see scrub()).
        self._reserved_placeholders: set[str] = set()
        # Each extra_terms category is emitted VERBATIM as the ⟦CAT:N⟧ placeholder category, which
        # restore() only recovers when it matches _PLACEHOLDER_RE's [A-Z]+ grammar. A lowercase/mixed/
        # non-ASCII category would emit a placeholder restore can never match -> the original text is
        # never recovered (canon corruption). Fail LOUDLY at construction instead of silently corrupting.
        for cat in self.extra_terms:
            if not (cat.isascii() and cat.isalpha() and cat.isupper()):
                raise ValueError(
                    f"extra_terms category {cat!r} must be uppercase ASCII letters (the ⟦CAT:N⟧ grammar)")
        # Precompile caller-supplied literal terms ONCE per instance instead of re.compile-per-term
        # per scrub() call (perf-#21). Each term becomes re.compile(re.escape(term)) — byte-identical
        # match semantics to the old `re.sub(re.escape(term), ...)` — and the longest-first ordering
        # (sorted by len, descending) plus the per-category iteration order are preserved exactly, so
        # WHICH spans get redacted, and their precedence, are unchanged. Empty terms are dropped (same
        # as the old `if not term: continue`).
        self._extra_term_pats: list[tuple[str, re.Pattern]] = [
            (cat, re.compile(re.escape(term)))
            for cat, terms in self.extra_terms.items()
            for term in sorted(terms, key=len, reverse=True)
            if term
        ]

    def reset(self) -> None:
        """Clear the accumulated placeholder namespace (start a fresh scrubbing session).

        WARNING: this INVALIDATES every previously-issued placeholder — after reset() the counter
        restarts at 1, so ``⟦EMAIL:1⟧`` may be re-issued for a different value. A consumer that keeps a
        SEPARATE restore accumulator (e.g. the MCP server's ``_scrub_map``) MUST clear it in lockstep, or
        a reused placeholder will restore to the wrong original (scrub-reset). It is currently uncalled.
        """
        self._counters, self._value_to_ph, self._mapping = {}, {}, {}
        self._reserved_placeholders = set()

    def _active(self) -> set[str]:
        return SENSITIVITY.get(self.sensitivity, SENSITIVITY["medium"])

    def _scrub_bare_person(self, out: str, alloc) -> str:
        """Redact bare Title-Case bigrams that look like personal names, rewriting `out` (scrub-4).

        Only redact a bigram whose first token is a known given name; leave concept bigrams
        untouched. F6: re.sub() scans non-overlapping, so sparing a non-name FIRST token
        ("Researcher Alan", "Yesterday Michael") would skip past the real name that begins inside the
        spared span. Iterate manually and, on a spared non-name bigram, resume at the SECOND token's
        start (m.start(3)) so "Alan Turing"/"Michael Smith" is still re-tested. `alloc` is the shared
        placeholder allocator from scrub() so placeholder consistency spans every redaction path.
        """
        pat = _BARE_PERSON_RE
        pieces: list[str] = []
        pos = 0
        while True:
            m = pat.search(out, pos)
            if m is None:
                pieces.append(out[pos:])
                break
            if _is_personal_name(m.group(0)):
                pieces.append(out[pos:m.start()])
                pieces.append(alloc("PERSON", m.group(0)))
                pos = m.end()
            else:
                # keep the non-name first token + gap verbatim; rescan from the second token.
                resume_at = m.start(3)
                pieces.append(out[pos:resume_at])
                pos = resume_at
        return "".join(pieces)

    def scrub(self, text: str) -> tuple[str, dict[str, str]]:
        """Return (scrubbed_text, mapping) where mapping is the placeholder -> original value pairs
        NEWLY created in this call (the instance accumulates the full map across calls)."""
        if text is None:
            return "", {}
        active = self._active()
        new_mapping: dict[str, str] = {}

        # scrub-2/M4: a ⟦CAT:N⟧-shaped substring already present in the SOURCE prose must round-trip
        # unchanged. The consumer (the MCP server) restores ONLY from the mapping each call RETURNS, so
        # protecting a literal requires an IDENTITY entry in `new_mapping` itself — writing it solely to
        # self._mapping (which the boundary never consults) leaves the literal unprotected at restore
        # time. Reserve every literal CUMULATIVELY so (a) restore() never rewrites a literal placeholder
        # into a redacted value (canon corruption), and (b) placeholder() below never hands a freshly
        # allocated ⟦CAT:N⟧ that has EVER occurred as a literal (which restore() would then over-expand).
        preexisting = {m.group(0) for m in _PLACEHOLDER_RE.finditer(text)}
        self._reserved_placeholders |= preexisting
        for ph in preexisting:
            existing = self._mapping.get(ph)
            if existing is None or existing == ph:
                # Not yet (or only identity-) mapped: protect it by identity-mapping in BOTH the
                # accumulated map and this call's RETURNED map, so the consumer's restore leaves it alone.
                self._mapping[ph] = ph
                new_mapping[ph] = ph
            # else: an UNAVOIDABLE collision — this string was already allocated as a real redaction in
            # an EARLIER call (so the consumer already maps it to that value). We cannot retroactively
            # un-map it; per the scrub-2 contract we leave it ambiguous rather than (re)mapping it here,
            # and the allocator's reserved-set skip prevents this collision from ever recurring forward.

        def placeholder(cat: str, value: str) -> str:
            key = (cat, value)
            if key in self._value_to_ph:
                return self._value_to_ph[key]
            # Skip any number whose placeholder string has EVER been a literal in the source (scrub-2/M4),
            # so the two namespaces can never overlap and restore can never expand a literal.
            while True:
                self._counters[cat] = self._counters.get(cat, 0) + 1
                ph = f"⟦{cat}:{self._counters[cat]}⟧"
                if ph not in self._reserved_placeholders:
                    break
            self._value_to_ph[key] = ph
            self._mapping[ph] = value
            new_mapping[ph] = value
            return ph

        def sub_with(cat: str, pattern: re.Pattern, s: str) -> str:
            """Replace every whole match of `pattern` in `s` with a placeholder for category `cat`."""
            return pattern.sub(lambda m: placeholder(cat, m.group(0)), s)

        out = text
        # caller-supplied literal terms first (e.g. a names list for this corpus). These are an EXPLICIT
        # redaction request, so honor them at EVERY tier — a lower sensitivity must not silently drop a
        # caller's own term list (review-low). Pattern-based categories below still respect `active`.
        # Patterns are precompiled once in __post_init__ (longest-first, same category order); this loop
        # only runs .sub() so nothing is recompiled per call (perf-#21).
        for cat, pat in self._extra_term_pats:
            out = sub_with(cat, pat, out)

        for cat, pat in _PATTERNS:
            if cat not in active:
                continue
            if pat is _BARE_PERSON_RE:
                out = self._scrub_bare_person(out, placeholder)
            else:
                out = sub_with(cat, pat, out)
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
