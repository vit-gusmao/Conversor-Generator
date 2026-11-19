"""Snapshot capture: private streaming copy with hash (T4, doc 04 §4).

Every open is verified against the granted root AFTER the handle exists
(TOCTOU backstop for swapped junctions) and metadata are compared before and
after the copy so a file rewritten mid-flight is discarded (SOURCE_CHANGED)
instead of producing a torn capture. Originals are never moved or deleted.
Locked files (Excel) report SOURCE_BUSY per item.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from sources.path_policy import assert_snapshot_inside_root
from sources.types import SnapshotRef


class SnapshotError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @classmethod
    def from_policy(cls, exc) -> "SnapshotError":
        return cls(exc.code, exc.message, exc.details)


COPY_CHUNK = 256 * 1024


def _identity(stat: os.stat_result) -> tuple:
    return (
        stat.st_size,
        stat.st_mtime_ns,
        getattr(stat, "st_ino", 0),
        getattr(stat, "st_dev", 0),
        getattr(stat, "st_file_attributes", 0),
    )


def capture_snapshot(
    candidate: "SourceCandidate",
    root_canonical_path: str,
    *,
    staging_dir: Path,
    stability_seconds: float = 2.0,
    cancel_check=None,
    source_path: Path | None = None,
) -> SnapshotRef:
    """Copy ``candidate`` into private staging and return its identity.

    ``source_path`` overrides the resolved location (used by the importer
    after scanning); by default it is resolved from the root + relative path.
    """
    from sources.path_policy import resolve_source_path
    from sources.types import SourceCandidate

    if not isinstance(candidate, SourceCandidate):
        raise TypeError("candidate must be a SourceCandidate")

    if cancel_check and cancel_check():
        raise SnapshotError("CANCELLED", "Captura cancelada.")

    path = source_path or resolve_source_path(root_canonical_path, candidate.relative_path)

    # Stability: two observations of size+mtime separated in time reduce
    # (never eliminate) captures of files still being written.
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise SnapshotError("SOURCE_MISSING", "O arquivo sumiu da fonte.", {"path": str(path)}) from exc
    except PermissionError as exc:
        raise SnapshotError("SOURCE_ACCESS_DENIED", "Acesso negado ao arquivo.", {"path": str(path)}) from exc

    if stability_seconds > 0:
        time.sleep(stability_seconds)
        try:
            after_wait = os.lstat(path)
        except FileNotFoundError as exc:
            raise SnapshotError("SOURCE_MISSING", "O arquivo sumiu da fonte.") from exc
        if _identity(after_wait) != _identity(before):
            raise SnapshotError(
                "SOURCE_CHANGED",
                "O arquivo estava em gravação; nova tentativa no próximo scan.",
            )

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    handle = None
    tmp_name = f".snap-{os.getpid()}-{time.monotonic_ns()}.tmp"
    tmp_path = staging_dir / tmp_name

    def _containment(final: Path) -> None:
        try:
            assert_snapshot_inside_root(final, root_canonical_path)
        except Exception as exc:  # PathPolicyError → SnapshotError for callers
            raise SnapshotError(
                "PATH_ESCAPED",
                getattr(exc, "message", str(exc)),
                getattr(exc, "details", None),
            ) from exc

    try:
        _containment(Path(os.path.realpath(path)))
        try:
            handle = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except PermissionError as exc:
            raise SnapshotError(
                "SOURCE_BUSY",
                "O arquivo está bloqueado por outro programa (ex.: Excel).",
                {"path": str(path)},
            ) from exc
        except OSError as exc:
            raise SnapshotError("SOURCE_UNREADABLE", str(exc), {"path": str(path)}) from exc

        opened_stat = os.fstat(handle)
        if _identity(opened_stat) != _identity(before):
            raise SnapshotError(
                "SOURCE_CHANGED", "O arquivo mudou entre o scan e a abertura."
            )

        digest = hashlib.sha256()
        size = 0
        with open(tmp_path, "wb") as sink:
            while True:
                if cancel_check and cancel_check():
                    raise SnapshotError("CANCELLED", "Captura cancelada.")
                try:
                    chunk = os.read(handle, COPY_CHUNK)
                except OSError as exc:
                    import errno as _errno

                    # 36 = ERROR_LOCK_VIOLATION (Windows); EACCES covers POSIX
                    if exc.errno in (_errno.EACCES, _errno.EDEADLOCK, 36):
                        raise SnapshotError(
                            "SOURCE_BUSY",
                            "O arquivo está bloqueado por outro programa (ex.: Excel).",
                            {"path": str(path)},
                        ) from exc
                    raise
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())

        after_stat = os.fstat(handle)
        if size != opened_stat.st_size or after_stat.st_mtime_ns != opened_stat.st_mtime_ns:
            raise SnapshotError(
                "SOURCE_CHANGED",
                "O arquivo mudou durante a cópia; descartada e reagendada.",
            )

        _containment(Path(os.path.realpath(path)))

        key = f"{digest.hexdigest()[:2]}/{digest.hexdigest()[:16]}.snap"
        target = staging_dir / key
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp_path, target)
        return SnapshotRef(
            key=key,
            sha256=digest.hexdigest(),
            size_bytes=size,
            relative_path=candidate.relative_path,
            mtime_ns=opened_stat.st_mtime_ns,
        )
    except SnapshotError:
        _safe_unlink(tmp_path)
        raise
    except OSError as exc:
        _safe_unlink(tmp_path)
        import errno

        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise SnapshotError(
                "DISK_FULL",
                "Espaço em disco insuficiente para o snapshot.",
                {"needed_bytes": candidate.size_bytes},
            ) from exc
        raise SnapshotError("SNAPSHOT_IO_ERROR", str(exc)) from exc
    finally:
        if handle is not None:
            os.close(handle)


def _safe_unlink(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["SnapshotError", "capture_snapshot"]
