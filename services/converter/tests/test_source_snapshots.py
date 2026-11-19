"""T4 — snapshot capture: stability, busy files, change detection, TOCTOU."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

import pytest

from sources.snapshot import SnapshotError, capture_snapshot
from sources.types import SourceCandidate

WIN32 = sys.platform == "win32"
STABILITY_SECONDS = 0.2  # tests shorten the wait via parameter


def _candidate(root: Path, relative: str, content: bytes) -> SourceCandidate:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stat = os.stat(path)
    return SourceCandidate(
        relative_path=relative,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _capture(tmp_path: Path, root: Path, candidate, monkeypatch=None, **kwargs):
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    kwargs.setdefault("stability_seconds", STABILITY_SECONDS)
    return capture_snapshot(candidate, str(root), staging_dir=staging, **kwargs)


def test_capture_copies_and_hashes(tmp_path: Path):
    root = tmp_path / "raiz"
    content = b"conteudo estavel"
    candidate = _candidate(root, "docs/plan.xlsx", content)
    ref = _capture(tmp_path, root, candidate)
    assert ref.sha256 == hashlib.sha256(content).hexdigest()
    assert ref.size_bytes == len(content)
    stored = tmp_path / "staging" / ref.key
    assert stored.read_bytes() == content
    assert (root / "docs" / "plan.xlsx").read_bytes() == content  # original intact


def test_busy_file_reports_source_busy(tmp_path: Path):
    root = tmp_path / "raiz"
    target = root / "plan.xlsx"
    candidate = _candidate(root, "plan.xlsx", b"bloqueado pelo excel")
    try:
        import msvcrt

        handle = open(target, "r+b")
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            with pytest.raises(SnapshotError) as excinfo:
                _capture(tmp_path, root, candidate)
            assert excinfo.value.code == "SOURCE_BUSY"
        finally:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()
    except ImportError:
        pytest.skip("msvcrt unavailable")


def test_change_during_capture_is_detected(tmp_path: Path, monkeypatch):
    root = tmp_path / "raiz"
    candidate = _candidate(root, "plan.xlsx", b"versao 1")
    real_read = os.read
    state = {"fired": False}

    def mutating_read(fd, amount):
        chunk = real_read(fd, amount)
        if not state["fired"]:
            state["fired"] = True
            # rewrite the source while the snapshot handle is open
            (root / "plan.xlsx").write_bytes(b"versao 2 com tamanho bem diferente!!")
        return chunk

    monkeypatch.setattr("sources.snapshot.os.read", mutating_read)
    with pytest.raises(SnapshotError) as excinfo:
        _capture(tmp_path, root, candidate)
    assert excinfo.value.code == "SOURCE_CHANGED"


def test_junction_swapped_to_outside_is_refused(tmp_path: Path, monkeypatch):
    if not WIN32:
        pytest.skip("requires Windows reparse points")
    import _winapi

    root = tmp_path / "raiz"
    dir_a = tmp_path / "a"
    dir_b_fora = tmp_path / "fora"
    dir_a.mkdir()
    dir_b_fora.mkdir()
    (dir_a / "plan.xlsx").write_bytes(b"dentro")
    (dir_b_fora / "plan.xlsx").write_bytes(b"fora do escopo")
    junction = root / "link"
    root.mkdir(exist_ok=True)
    _winapi.CreateJunction(str(dir_a), str(junction))

    candidate = _candidate(root, "link/plan.xlsx", b"dentro")
    real_read = os.read
    state = {"fired": False}

    def swapping_read(fd, amount):
        chunk = real_read(fd, amount)
        if not state["fired"]:
            state["fired"] = True
            # swap the junction to point outside the root mid-copy
            import subprocess

            subprocess.run(["cmd", "/c", "rmdir", str(junction)], check=True)
            _winapi.CreateJunction(str(dir_b_fora), str(junction))
        return chunk

    monkeypatch.setattr("sources.snapshot.os.read", swapping_read)
    with pytest.raises(SnapshotError) as excinfo:
        _capture(tmp_path, root, candidate)
    assert excinfo.value.code in {"PATH_ESCAPED", "SOURCE_CHANGED", "SOURCE_BUSY"}
    # restore the junction so tmp cleanup works
    try:
        import subprocess

        subprocess.run(["cmd", "/c", "rmdir", str(junction)], check=False)
    except Exception:
        pass


def test_unicode_and_spaces_paths(tmp_path: Path):
    root = tmp_path / "raiz com espaços"
    content = "acentuação çãô".encode("utf-8")
    candidate = _candidate(root, "pasta çéü/planilha ã.xlsx", content)
    ref = _capture(tmp_path, root, candidate)
    assert ref.sha256 == hashlib.sha256(content).hexdigest()


def test_cancel_check_stops_capture(tmp_path: Path):
    root = tmp_path / "raiz"
    candidate = _candidate(root, "plan.xlsx", b"conteudo")
    with pytest.raises(SnapshotError) as excinfo:
        _capture(tmp_path, root, candidate, cancel_check=lambda: True)
    assert excinfo.value.code == "CANCELLED"


def test_missing_file_reports_source_missing(tmp_path: Path):
    root = tmp_path / "raiz"
    root.mkdir()
    candidate = SourceCandidate(
        relative_path="fantasma.xlsx", size_bytes=10, mtime_ns=0
    )
    with pytest.raises(SnapshotError) as excinfo:
        _capture(tmp_path, root, candidate)
    assert excinfo.value.code == "SOURCE_MISSING"
