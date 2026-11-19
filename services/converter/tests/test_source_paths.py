"""T4 — path policy and scanner for authorized sources.

Windows real reparse-point tests run on this platform; they are skipped
elsewhere (and reported as not run there).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sources.path_policy import (
    PathPolicyError,
    is_remote_path,
    resolve_source_path,
)
from sources.scanner import SourceUnavailable, scan_source

WIN32 = sys.platform == "win32"


def _root(tmp_path: Path, name: str = "raiz") -> Path:
    root = tmp_path / name
    (root / "financeiro").mkdir(parents=True)
    (root / "financeiro" / "dados.xlsx").write_bytes(b"planilha")
    (root / "leia-me.txt").write_bytes(b"texto")
    return root


def _def(root: Path, relative: str = "", *, kind: str = "directory", recursive: bool = True,
         include=None, exclude=None):
    from sources.types import SourceDefinitionSnapshot

    return SourceDefinitionSnapshot(
        source_id="src_test",
        root_id="root_test",
        root_canonical_path=str(root),
        relative_path=relative,
        kind=kind,
        recursive=recursive,
        include_patterns=include,
        exclude_patterns=exclude,
        version=1,
    )


def test_resolve_inside_root(tmp_path: Path):
    root = _root(tmp_path)
    resolved = resolve_source_path(str(root), "financeiro/dados.xlsx")
    assert resolved == root / "financeiro" / "dados.xlsx"


def test_similar_prefix_is_not_inside(tmp_path: Path):
    root = _root(tmp_path)
    sibling = tmp_path / "raiz2"
    sibling.mkdir()
    (sibling / "roubo.txt").write_bytes(b"fora")
    with pytest.raises(PathPolicyError):
        resolve_source_path(str(root), "../raiz2/roubo.txt")


def test_traversal_and_absolute_are_rejected(tmp_path: Path):
    root = _root(tmp_path)
    for bad in ["..", "a/../../b", "C:/Windows", "\\\\servidor\\share", "nul", "a:b"]:
        with pytest.raises(PathPolicyError):
            resolve_source_path(str(root), bad)


def test_device_namespace_and_ads_rejected(tmp_path: Path):
    root = _root(tmp_path)
    for bad in ["\\\\.\\C:\\x", "arquivo.txt:stream"]:
        with pytest.raises(PathPolicyError):
            resolve_source_path(str(root), bad)


def test_remote_roots_rejected(tmp_path: Path):
    # UNC root is refused at resolve time even before any file access.
    with pytest.raises(PathPolicyError):
        resolve_source_path("\\\\servidor\\compartilhamento", "docs")
    assert is_remote_path(Path("\\\\servidor\\share")) is True


def test_scanner_enumerates_recursively_with_patterns(tmp_path: Path):
    root = _root(tmp_path)
    (root / "financeiro" / "sub").mkdir()
    (root / "financeiro" / "sub" / "profundo.csv").write_bytes(b"csv")
    (root / "~$temporario.xlsx").write_bytes(b"lock")
    candidates = list(scan_source(_def(root, "financeiro", include=["*.xlsx", "*.csv"])))
    names = {candidate.relative_path for candidate in candidates}
    assert names == {"financeiro/dados.xlsx", "financeiro/sub/profundo.csv"}


def test_scanner_single_file_source(tmp_path: Path):
    root = _root(tmp_path)
    candidates = list(scan_source(_def(root, "financeiro/dados.xlsx", kind="file")))
    assert [candidate.relative_path for candidate in candidates] == ["financeiro/dados.xlsx"]


def test_empty_directory_is_empty_not_unavailable(tmp_path: Path):
    root = tmp_path / "raiz"
    (root / "vazio").mkdir(parents=True)
    candidates = list(scan_source(_def(root, "vazio")))
    assert candidates == []


def test_missing_root_is_unavailable(tmp_path: Path):
    root = tmp_path / "raiz"
    root.mkdir()
    with pytest.raises(SourceUnavailable):
        list(scan_source(_def(root, "nao-existe")))


def test_junction_escape_is_refused(tmp_path: Path):
    if not WIN32:
        pytest.skip("requires Windows reparse points")
    import _winapi

    root = _root(tmp_path)
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "segredo.xlsx").write_bytes(b"roubado")
    junction = root / "atalho"
    _winapi.CreateJunction(str(outside), str(junction))
    candidates = list(scan_source(_def(root, "", recursive=True, include=["*.xlsx"])))
    # junctions are never followed by the scanner
    assert all("atalho" not in candidate.relative_path for candidate in candidates)


def test_scanner_survives_deleted_file_between_scan_and_read(tmp_path: Path):
    root = _root(tmp_path)
    (root / "volatil.xlsx").write_bytes(b"logo some")
    iterator = scan_source(_def(root))
    first = next(iterator)
    assert first.size_bytes > 0
    (root / "volatil.xlsx").unlink()
    # remaining candidates still enumerate without crashing
    assert list(iterator) is not None
