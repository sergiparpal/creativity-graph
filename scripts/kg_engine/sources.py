"""SourceSet (R4): resolve a file | dir | glob of ``.md``/``.txt`` into an ordered ``{basename → text}``
map and make span verification **source-aware**.

This turns the previously-dead ``Edge.source_file`` field load-bearing: a span must verify against
*some* declared source, and when an edge names a ``source_file``, against *that* file specifically (a
lenient any-source fallback when the named basename isn't among the declared sources — legacy
``source.md`` / an agent typo — per Stage-0 Q1). Single-file is the trivial one-entry case, byte-identical
to the old single-blob path, so every existing single-source canon keeps verifying unchanged.

**Markdown/text only — no PDF/media** (a lossy transcript becoming a "verbatim" span would break
span-present, §1.5, and the deterministic tier). Pure stdlib + ``model.normalize_text``; read-only.
"""
from __future__ import annotations

import glob as _glob
from pathlib import Path

from .model import normalize_text, span_present_in

_EXTS = (".md", ".txt")
_GLOB_CHARS = "*?["


def _looks_like_glob(s: str) -> bool:
    return any(ch in s for ch in _GLOB_CHARS)


def _source_order(p: Path) -> tuple:
    """The platform-stable sort key: by basename, then full path. The full-path tiebreak is what makes
    a cross-dir basename collision pick the same winner on every machine (filesystem iteration order is
    not portable)."""
    return (p.name, str(p))


def _dedupe_stable(cands: list[Path]) -> list[Path]:
    """The dedup pipeline: sort by ``_source_order``, skip dotfiles / non-files / wrong-suffix
    (case-insensitive), and dedupe by basename keeping the lexicographically-first full path. The
    case-insensitive ``.lower()`` suffix filter matches the glob branch, so ``/dir`` and ``/dir/*``
    agree on an uppercase-extension file (``UP.MD``)."""
    out: dict[str, Path] = {}
    for q in sorted(cands, key=_source_order):
        if q.name.startswith(".") or not q.is_file() or q.suffix.lower() not in _EXTS:
            continue
        out.setdefault(q.name, q)
    return list(out.values())


class SourceSet:
    """An ordered, deterministic ``{basename → raw_text}`` view over the configured source(s)."""

    def __init__(self, path: "str | Path | None" = None):
        self.path = Path(path) if path is not None else None
        self._texts: dict[str, str] = {}
        for p in self.resolve(self.path):
            try:
                self._texts[p.name] = p.read_text(encoding="utf-8")
            except (OSError, ValueError):
                # OSError = unreadable; ValueError covers UnicodeDecodeError (its superclass, NOT an
                # OSError) so a non-UTF-8 / binary / UTF-16 file among the resolved set is SKIPPED instead
                # of propagating out of the constructor and disabling the whole tool surface (review-H3).
                # Skip — never decode with errors='replace', which could forge phantom span matches.
                continue  # mirrors canon.all_nodes()'s "one bad file must not crash every read" tolerance
        # normalized form per file, computed once for source-aware verification (off the hot path)
        self._norm: dict[str, str] = {name: normalize_text(t) for name, t in self._texts.items()}

    # ---- resolution
    @classmethod
    def resolve(cls, path: "str | Path | None") -> list[Path]:
        """The ordered list of source files for ``path``.

        - an explicit **file** → just that file (ANY extension — single-file back-compat: the user
          pointed at it directly, so honor it even if it is not ``.md``/``.txt``);
        - a **directory** or **glob** → its ``.md``/``.txt`` members (case-insensitive extension),
          ordered by ``(basename, full path)``, dotfiles skipped (mirroring ``canon.note_paths``),
          deduped by basename — on a cross-dir collision the lexicographically-first full path wins
          (a stable, platform-independent order);
        - a nonexistent path → ``[]`` (so ``source_text()`` degrades to ``""`` exactly as before).
        """
        if path is None:
            return []
        p = Path(path)
        s = str(path)
        if p.is_file():
            return [p]  # explicit single file — always included, any extension
        if p.is_dir():
            cands = list(p.iterdir())  # the case-insensitive suffix filter below unifies dir + glob
        elif _looks_like_glob(s):
            # recursive=True so a `**` segment recurses into nested directories rather than being
            # treated as a single `*` (one level) — else `src/**/*.md` silently under-collects the
            # nested sources R4 is meant to gather (review-low: ** glob recursion).
            cands = [Path(x) for x in _glob.glob(s, recursive=True)]
        else:
            return []  # nonexistent literal path
        # The dir/glob candidates run through the platform-stable dedup pipeline (sort + dotfile/suffix
        # filter + first-path-wins by basename) so the resolved order is identical across machines.
        return _dedupe_stable(cands)

    @classmethod
    def signature(cls, path: "str | Path | None") -> tuple:
        """A cheap ``(basename, size, mtime_ns)`` tuple over the resolved files (no content reads).
        KGEngine keys its SourceSet cache on this so an added/removed/edited source is picked up, else the
        cache is served — keeping the resolve+read off the hot path. Uses ``st_mtime_ns`` + ``st_size``
        (not float ``st_mtime``) to match ``projector._cheap_sig``, so a same-second in-place edit
        reliably invalidates the cache rather than serving stale text past the one-projection-lag (R3)."""
        sig = []
        for p in cls.resolve(path):
            try:
                st = p.stat()
                sig.append((p.name, st.st_size, st.st_mtime_ns))
            except OSError:
                sig.append((p.name, None, None))
        return tuple(sig)

    # ---- views
    @property
    def texts(self) -> dict[str, str]:
        """The ordered ``{basename → raw_text}`` map (a copy)."""
        return dict(self._texts)

    @property
    def basenames(self) -> list[str]:
        return list(self._texts.keys())

    @property
    def concat(self) -> str:
        """All sources concatenated in deterministic order (one ``\\n`` between files). For a single
        file this is byte-identical to the file's text. Feeds the IDF corpus + the flood-budget size."""
        return "\n".join(self._texts.values())

    @property
    def normalized(self) -> str:
        """The whitespace/case-normalized concat (whole-corpus form)."""
        return normalize_text(self.concat)

    @staticmethod
    def _key(name: str) -> str:
        """The basename lookup key — normalized identically to the storage key (``p.name``), so a
        lookup can never miss a stored source by forgetting to strip the directory part."""
        return Path(name).name

    def for_file(self, name: str) -> str:
        """The raw text of one declared source by basename, or ``""`` if not present."""
        return self._texts.get(self._key(name), "")

    def has_file(self, name: str) -> bool:
        """True iff ``name``'s basename is a declared source — lets the boundary distinguish
        ``span-not-in-named-source`` (named source known, span absent there) from ``span-not-in-source``."""
        return bool(name) and self._key(name) in self._texts

    def __len__(self) -> int:
        return len(self._texts)

    def __bool__(self) -> bool:
        return bool(self._texts)

    # ---- the source-aware span check
    def verifies(self, span: str, source_file: str = "") -> bool:
        """True iff ``span`` is a normalized verbatim substring of a DECLARED source.

        When ``source_file`` names a KNOWN basename, verify against THAT file only
        (named-source-exact); when it names an UNKNOWN basename (legacy ``source.md`` / an agent typo)
        or is empty, fall back to ANY declared source (lenient — Stage-0 Q1). The check is per-file
        (never against a cross-file concat), so a span can never "verify" by straddling a file
        boundary. Delegates the normalize + empty-guard + substring atom to ``model.span_present_in``
        (the single primitive ``span_verifies`` also calls), so the §1.5 fail-closed gate cannot drift;
        this method keeps ONLY the source SELECTION over the pre-normalized ``self._norm`` texts."""
        if source_file:
            name = self._key(source_file)
            if name in self._norm:
                return span_present_in(span, self._norm[name])
            # unknown named source → lenient any-source fallback
        return any(span_present_in(span, nt) for nt in self._norm.values())
