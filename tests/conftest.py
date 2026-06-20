"""Shared fixtures: a git-backed temp canon vault and a configured KGEngine."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kg_engine.canon import Canon
from kg_engine.model import Edge, Node
from kg_engine.pack import load_pack
from kg_engine.server import KGEngine

SOURCE = """\
A compression stands in for many observations and grounds the claims beneath it.
The generality confound inflates vague nodes. Betweenness is confounded by the generality confound.
Specificity-weighted betweenness reconciles with the bridge intuition. Degree approximates importance.
A failed claim is negative information and defends against re-proposal. The canon grounds trust.
Entropy grounds the arrow of time. Heat flows from hot to cold.
"""


def _git_init(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "--allow-empty", "-m", "init"], check=True)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    _git_init(tmp_path)
    return tmp_path


@pytest.fixture
def source_path(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("src") / "source.md"
    p.write_text(SOURCE, encoding="utf-8")
    return p


@pytest.fixture
def canon(vault: Path) -> Canon:
    return Canon(vault)


@pytest.fixture
def pack():
    return load_pack(Path(__file__).resolve().parents[1] / "pack" / "pack.yaml")


@pytest.fixture
def engine(vault: Path, source_path: Path) -> KGEngine:
    pack_path = Path(__file__).resolve().parents[1] / "pack" / "pack.yaml"
    return KGEngine(vault, source_path=source_path, pack_path=pack_path)


def make_node(nid: str, edges=None, **kw) -> Node:
    return Node(id=nid, label=kw.pop("label", nid), edges=edges or [], **kw)


def make_edge(src: str, rel: str, tgt: str, span: str = "", **kw) -> Edge:
    return Edge(source=src, target=tgt, relation=rel, span=span, **kw)
