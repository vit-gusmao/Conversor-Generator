"""Streaming enumeration of eligible files in a source (T4, doc 04).

The scanner never materializes the whole tree: candidates stream from an
``os.scandir`` walk with cycle protection (reparse points are never followed
on recursion) and default exclusions (``~$*``, ``*.tmp``, output dirs).
A missing or inaccessible source is *unavailable* — distinct from empty.
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

from sources.path_policy import resolve_source_path
from sources.types import SourceCandidate, SourceDefinitionSnapshot

DEFAULT_EXCLUDES = ["~$*", "*.tmp", "~wrd*"]

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class SourceUnavailable(Exception):
    """The root/relative path is missing or cannot be accessed."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def scan_source(
    definition: SourceDefinitionSnapshot,
    cancel_check=None,
):
    """Yield :class:`SourceCandidate` entries eligible under the definition."""
    base = resolve_source_path(
        definition.root_canonical_path, definition.relative_path, allow_empty=True
    )
    root_dir = resolve_source_path(definition.root_canonical_path, "", allow_empty=True)
    if not base.exists():
        raise SourceUnavailable(
            "SOURCE_NOT_FOUND",
            f"A fonte não existe neste momento: {definition.relative_path}",
            details={"relative_path": definition.relative_path},
        )
    try:
        if base.is_dir():
            yield from _walk_directory(base, root_dir, definition, cancel_check)
        else:
            stat = base.stat()
            yield SourceCandidate(
                relative_path=definition.relative_path.replace("\\", "/"),
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
    except PermissionError as exc:
        raise SourceUnavailable(
            "SOURCE_ACCESS_DENIED",
            "A fonte não pôde ser acessada com as permissões atuais.",
            details={"error": str(exc)},
        ) from exc
    except NotADirectoryError as exc:
        raise SourceUnavailable(
            "SOURCE_NOT_FOUND",
            "O caminho da fonte não é um arquivo nem uma pasta.",
        ) from exc


def _walk_directory(base: Path, root_dir: Path, definition: SourceDefinitionSnapshot, cancel_check):
    include = list(definition.include_patterns or [])
    exclude = list(definition.exclude_patterns or []) + DEFAULT_EXCLUDES
    recursive = definition.recursive
    base_str = str(base)
    root_str = str(root_dir)

    def excluded(name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in exclude)

    def matches(name: str) -> bool:
        return not include or any(fnmatch.fnmatch(name, pattern) for pattern in include)

    stack: list[Path] = [base]
    while stack:
        if cancel_check and cancel_check():
            return
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            if cancel_check and cancel_check():
                return
            name = entry.name
            if excluded(name):
                continue
            is_dir = False
            reparse = False
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                if sys.platform == "win32":
                    st = entry.stat(follow_symlinks=False)
                    reparse = bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
                elif entry.is_symlink():
                    reparse = True
            except (OSError, PermissionError):
                continue
            if is_dir:
                # Reparse points are never followed: a junction cannot become
                # an entry point into another tree (and cannot create cycles).
                if recursive and not reparse:
                    stack.append(Path(entry.path))
                continue
            if not matches(name):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except (OSError, PermissionError):
                continue
            relative = os.path.relpath(entry.path, root_str).replace("\\", "/")
            yield SourceCandidate(
                relative_path=relative,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )


__all__ = ["DEFAULT_EXCLUDES", "SourceUnavailable", "scan_source"]
