"""Tests for the SourceSet value object + source-aware boundary verification (R4).

SourceSet resolves a file | dir | glob of .md/.txt into an ordered {basename → text} map and makes the
span-present check source-aware: a span must verify against a DECLARED source, and against the edge's
named source_file specifically when it has one (lenient any-source fallback when the named basename is
unknown — Stage-0 Q1). A single configured file is the trivial one-entry case, byte-identical to today.
"""
from __future__ import annotations

from pathlib import Path

from kg_engine.boundary import validate_payload
from kg_engine.model import Disposition
from kg_engine.sources import SourceSet

A_TXT = "Alpha grounds beta in the first document.\n"
B_TXT = "Gamma bridges delta across the second document.\n"


def _dir(tmp_path: Path, **files: str) -> Path:
    d = tmp_path / "src"
    d.mkdir()
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return d


# --------------------------------------------------------------------------- resolution


def test_single_file_is_one_entry_byte_identical(tmp_path: Path):
    """A single configured file is a one-entry map whose concat is byte-identical to the file text."""
    f = tmp_path / "only.md"
    f.write_text(A_TXT, encoding="utf-8")
    s = SourceSet(f)
    assert s.basenames == ["only.md"]
    assert s.texts == {"only.md": A_TXT}
    assert s.concat == A_TXT          # one file -> no join artifact
    assert len(s) == 1 and bool(s)


def test_directory_collects_md_and_txt_sorted_skipping_dotfiles(tmp_path: Path):
    d = _dir(tmp_path, **{"a.md": A_TXT, "b.txt": B_TXT, "c.rst": "ignored", ".hidden.md": "ignored"})
    s = SourceSet(d)
    assert s.basenames == ["a.md", "b.txt"]   # sorted, .rst dropped, dotfile skipped
    assert "Alpha" in s.concat and "Gamma" in s.concat


def test_glob_resolves_md_only(tmp_path: Path):
    d = _dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT, "c.txt": "skip via glob"})
    s = SourceSet(str(d / "*.md"))
    assert s.basenames == ["a.md", "b.md"]    # the .txt is excluded by the *.md glob


def test_directory_extension_match_is_case_insensitive_and_matches_glob(tmp_path: Path):
    """`/dir` and `/dir/*` must agree on an uppercase-extension file (no case-sensitive dir-vs-glob
    asymmetry): an `Upper.MD` is a declared source either way."""
    d = _dir(tmp_path, **{"a.md": A_TXT, "Upper.MD": B_TXT})
    assert SourceSet(d).basenames == SourceSet(str(d / "*")).basenames
    assert "Upper.MD" in SourceSet(d).basenames


def test_cross_dir_basename_collision_winner_is_deterministic(tmp_path: Path):
    """A `*/notes.md`-style glob with the same basename in two dirs resolves to the lexicographically-
    first full path on every filesystem (the (name, path) total order), not raw glob/iterdir order."""
    (tmp_path / "d_a").mkdir()
    (tmp_path / "d_b").mkdir()
    (tmp_path / "d_a" / "shared.md").write_text("from A\n", encoding="utf-8")
    (tmp_path / "d_b" / "shared.md").write_text("from B\n", encoding="utf-8")
    s = SourceSet(str(tmp_path / "d_*" / "shared.md"))
    assert s.basenames == ["shared.md"]
    assert s.for_file("shared.md") == "from A\n"   # d_a < d_b -> the A copy wins, deterministically


def test_nonexistent_and_none_are_empty(tmp_path: Path):
    assert SourceSet(tmp_path / "missing.md").texts == {}
    assert SourceSet(tmp_path / "missing.md").concat == ""
    assert not SourceSet(None) and SourceSet(None).concat == ""


def test_explicit_file_honored_regardless_of_extension(tmp_path: Path):
    """Back-compat: an explicitly-named single file is included even if it is not .md/.txt (the user
    pointed at it directly). The extension filter only applies to dir/glob resolution."""
    f = tmp_path / "source.markdown"
    f.write_text(A_TXT, encoding="utf-8")
    assert SourceSet(f).concat == A_TXT


def test_signature_changes_on_add_remove_edit(tmp_path: Path):
    d = _dir(tmp_path, **{"a.md": A_TXT})
    sig1 = SourceSet.signature(d)
    (d / "b.md").write_text(B_TXT, encoding="utf-8")
    sig2 = SourceSet.signature(d)
    assert sig1 != sig2                       # a new file moves the signature
    (d / "b.md").unlink()
    assert SourceSet.signature(d) == sig1     # removing it restores the prior signature


# --------------------------------------------------------------------------- verification


def test_verifies_any_source_when_no_named_file(tmp_path: Path):
    s = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    assert s.verifies("Alpha grounds beta")              # in a.md
    assert s.verifies("Gamma bridges delta")             # in b.md
    assert not s.verifies("this phrase is nowhere")


def test_verifies_named_source_is_exact(tmp_path: Path):
    s = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    # the span lives in b.md only
    assert s.verifies("Gamma bridges delta", source_file="b.md")
    assert not s.verifies("Gamma bridges delta", source_file="a.md")   # named, but absent there
    # a path is reduced to its basename
    assert s.verifies("Gamma bridges delta", source_file="some/dir/b.md")


def test_verifies_unknown_named_source_falls_back_to_any(tmp_path: Path):
    """An unknown named basename (legacy source.md / a typo) does NOT hard-fail — it falls back to any
    declared source (lenient, Stage-0 Q1)."""
    s = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    assert s.verifies("Alpha grounds beta", source_file="legacy-source.md")
    assert not s.verifies("nowhere at all", source_file="legacy-source.md")


def test_verifies_normalizes_like_span_verifies(tmp_path: Path):
    s = SourceSet(_dir(tmp_path, **{"a.md": A_TXT}))
    assert s.verifies("alpha   GROUNDS    beta")          # case + whitespace folded
    assert not s.verifies("")                              # empty span never verifies
    assert not s.verifies("   ")


def test_has_file_and_for_file(tmp_path: Path):
    s = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    assert s.has_file("a.md") and s.has_file("path/to/b.md")
    assert not s.has_file("c.md") and not s.has_file("")
    assert s.for_file("a.md") == A_TXT and s.for_file("c.md") == ""


# --------------------------------------------------------------------------- boundary integration


def test_boundary_span_in_wrong_named_file_is_rejected_not_in_named_source(tmp_path, pack):
    """A span that exists in the corpus but NOT in the edge's named source_file is rejected as
    `span-not-in-named-source` — the mis-attribution is caught, distinct from absent-everywhere."""
    sources = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    res = validate_payload(
        {"edges": [{"source": "gamma", "target": "delta", "relation": "bridges",
                    "span": "Gamma bridges delta", "source_file": "a.md", "authored_by": "agent"}]},
        pack=pack, source_text=sources.concat, sources=sources)
    e = next(r for r in res if r.kind == "edge")
    assert e.disposition == Disposition.REJECTED and e.reason == "span-not-in-named-source"


def test_boundary_span_in_correct_named_file_accepted(tmp_path, pack):
    sources = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    res = validate_payload(
        {"edges": [{"source": "gamma", "target": "delta", "relation": "bridges",
                    "span": "Gamma bridges delta", "source_file": "b.md", "authored_by": "agent"}]},
        pack=pack, source_text=sources.concat, sources=sources)
    e = next(r for r in res if r.kind == "edge")
    assert e.disposition == Disposition.ACCEPTED, e


def test_boundary_no_source_file_verifies_against_any(tmp_path, pack):
    sources = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    res = validate_payload(
        {"edges": [{"source": "gamma", "target": "delta", "relation": "bridges",
                    "span": "Gamma bridges delta", "authored_by": "agent"}]},  # no source_file
        pack=pack, source_text=sources.concat, sources=sources)
    assert next(r for r in res if r.kind == "edge").disposition == Disposition.ACCEPTED


def test_boundary_known_named_source_absent_everywhere_is_named_source_miss(tmp_path, pack):
    """A span absent everywhere but attributed to a KNOWN declared source (a.md) is still labeled the
    named-source miss (has_file is True) — the generic span-not-in-source reason is for an UNKNOWN
    named source (see test_boundary_unknown_named_source_falls_back_then_rejects_generic)."""
    sources = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    res = validate_payload(
        {"edges": [{"source": "x", "target": "y", "relation": "grounds",
                    "span": "unicorns cause gravity", "source_file": "a.md", "authored_by": "agent"}]},
        pack=pack, source_text=sources.concat, sources=sources)
    e = next(r for r in res if r.kind == "edge")
    assert e.disposition == Disposition.REJECTED and e.reason == "span-not-in-named-source"


def test_boundary_unknown_named_source_falls_back_then_rejects_generic(tmp_path, pack):
    """An UNKNOWN named source falls back to any-source; absent everywhere it is the generic
    `span-not-in-source` (has_file is False, so it is not labeled a named-source miss)."""
    sources = SourceSet(_dir(tmp_path, **{"a.md": A_TXT, "b.md": B_TXT}))
    res = validate_payload(
        {"edges": [{"source": "x", "target": "y", "relation": "grounds",
                    "span": "unicorns cause gravity", "source_file": "legacy.md", "authored_by": "agent"}]},
        pack=pack, source_text=sources.concat, sources=sources)
    e = next(r for r in res if r.kind == "edge")
    assert e.disposition == Disposition.REJECTED and e.reason == "span-not-in-source"
