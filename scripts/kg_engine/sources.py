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

from .model import normalize_text

_EXTS = (".md", ".txt")
_GLOB_CHARS = "*?["


def _looks_like_glob(s: str) -> bool:
    return any(ch in s for ch in _GLOB_CHARS)


class SourceSet:
    """An ordered, deterministic ``{basename → raw_text}`` view over the configured source(s)."""

    def __init__(self, path: "str | Path | None" = None):
        self.path = Path(path) if path is not None else None
        self._texts: dict[str, str] = {}
        for p in self.resolve(self.path):
            try:
                self._texts[p.name] = p.read_text(encoding="utf-8")
            except OSError:
                continue  # an unreadable file is skipped, mirroring canon.all_nodes()'s tolerance
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
            cands = [Path(x) for x in _glob.glob(s)]
        else:
            return []  # nonexistent literal path
        out: dict[str, Path] = {}
        # Total, platform-stable order: by basename, then full path. glob()/iterdir() order is
        # filesystem-dependent, so without the full-path tiebreak a cross-dir basename collision (a
        # `*/notes.md`-style glob) would pick a winner by raw directory-iteration order and diverge
        # across machines. Sorting by (name, str(path)) makes the lexicographically-first full path win
        # everywhere; the dedup keeps that one. The case-insensitive `.lower()` suffix filter matches
        # the glob branch, so `/dir` and `/dir/*` agree on an uppercase-extension file (`UP.MD`).
        for q in sorted(cands, key=lambda x: (x.name, str(x))):
            if q.name.startswith(".") or not q.is_file() or q.suffix.lower() not in _EXTS:
                continue
            out.setdefault(q.name, q)
        return list(out.values())

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

    def for_file(self, name: str) -> str:
        """The raw text of one declared source by basename, or ``""`` if not present."""
        return self._texts.get(Path(name).name, "")

    def has_file(self, name: str) -> bool:
        """True iff ``name``'s basename is a declared source — lets the boundary distinguish
        ``span-not-in-named-source`` (named source known, span absent there) from ``span-not-in-source``."""
        return bool(name) and Path(name).name in self._texts

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
        boundary. Normalization is identical to ``model.span_verifies`` (the single-blob check)."""
        norm = normalize_text(span)
        if not norm:
            return False
        if source_file:
            name = Path(source_file).name
            if name in self._norm:
                return norm in self._norm[name]
            # unknown named source → lenient any-source fallback
        return any(norm in nt for nt in self._norm.values())
