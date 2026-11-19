"""Path authorization policy for granted roots (T4, doc 04 §3).

Containment is checked by path components, never by textual prefix:
``C:\\Dados2`` is not inside ``C:\\Dados``. Device namespaces, alternate data
streams, traversal and special names are refused. UNC/remote roots are
rejected in this first version; a mapped network drive is detected at grant
time. Environment variables and commands received from forms are never
expanded.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PureWindowsPath

from sources.types import SourceDefinitionSnapshot


class PathPolicyError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _windows_drive(path: PureWindowsPath) -> str | None:
    return path.drive or None


def is_remote_path(path: Path) -> bool:
    """True for UNC shares and mapped network drives (Windows)."""
    text = str(path)
    if text.startswith("\\\\"):
        return True
    pure = PureWindowsPath(text)
    if sys.platform == "win32" and pure.drive and len(pure.drive) == 2 and pure.drive.endswith(":"):
        try:
            import ctypes

            type_code = ctypes.windll.kernel32.GetDriveTypeW(pure.drive + "\\")
            return type_code == 4  # DRIVE_REMOTE
        except Exception:
            return False
    return False


def _reject(code: str, message: str, details: dict | None = None) -> PathPolicyError:
    return PathPolicyError(code, message, details)


def validate_root_path(canonical_path: str) -> Path:
    """Grants: the root itself must be a local, absolute, existing directory."""
    if not canonical_path or len(canonical_path) > 1024:
        raise _reject("INVALID_ROOT", "Caminho da raiz inválido.")
    if "\x00" in canonical_path:
        raise _reject("INVALID_ROOT", "Caminho inválido (byte NUL).")
    pure = PureWindowsPath(canonical_path) if sys.platform == "win32" else Path(canonical_path)
    if sys.platform == "win32":
        if canonical_path.startswith("\\\\"):
            raise _reject(
                "REMOTE_PATH_UNSUPPORTED",
                "Compartilhamentos de rede (UNC) não são suportados nesta versão.",
                details={"root": canonical_path},
            )
        drive = _windows_drive(pure)
        if drive:
            root_path = Path(canonical_path)
            if is_remote_path(root_path):
                raise _reject(
                    "REMOTE_PATH_UNSUPPORTED",
                    "Unidades de rede mapeadas não são suportadas nesta versão.",
                    details={"root": canonical_path},
                )
    root = Path(canonical_path)
    if not root.is_absolute():
        raise _reject("INVALID_ROOT", "A raiz concedida deve ser um caminho absoluto.")
    return root


def resolve_source_path(
    root_canonical_path: str, relative_path: str, *, allow_empty: bool = False
) -> Path:
    """Resolve ``relative_path`` inside the granted root with all checks.

    Returns the real, resolved path. Raises :class:`PathPolicyError` when the
    combination is not authorized — zero bytes are ever read outside the root.
    """
    root = validate_root_path(root_canonical_path)

    if not relative_path:
        if allow_empty:
            # The root itself (directory source at the base).
            return root
        raise _reject("INVALID_SOURCE_PATH", "relative_path inválido.")
    if len(relative_path) > 1024:
        raise _reject("INVALID_SOURCE_PATH", "relative_path inválido.")
    if "\x00" in relative_path:
        raise _reject("INVALID_SOURCE_PATH", "relative_path contém byte NUL.")

    text = relative_path.replace("\\", "/")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise _reject(
            "INVALID_SOURCE_PATH",
            "relative_path deve ser relativo à raiz concedida.",
            details={"relative_path": relative_path},
        )

    components: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise _reject(
                "TRAVERSAL_DENIED",
                "Travessia ('..') não é permitida em relative_path.",
                details={"relative_path": relative_path},
            )
        if sys.platform == "win32":
            lowered = part.lower()
            if lowered in {"con", "prn", "aux", "nul"} or lowered.startswith(("com", "lpt")):
                raise _reject(
                    "SPECIAL_PATH_DENIED",
                    f"Nome especial de dispositivo não é permitido: {part}.",
                    details={"component": part},
                )
            if ":" in part:
                raise _reject(
                    "ADS_DENIED",
                    "Alternate Data Streams / prefixos de unidade não são permitidos.",
                    details={"component": part},
                )
            if part.startswith("\\\\"):
                raise _reject(
                    "DEVICE_NAMESPACE_DENIED",
                    "Namespaces de dispositivo não são permitidos.",
                    details={"component": part},
                )
        components.append(part)

    if not components:
        raise _reject(
            "INVALID_SOURCE_PATH",
            "relative_path deve nomear um arquivo ou pasta dentro da raiz.",
            details={"relative_path": relative_path},
        )

    candidate = root.joinpath(*components)
    resolved = Path(os.path.normpath(str(candidate)))

    # Component-based containment on the normalized forms.
    root_parts = _casefold_parts(root)
    resolved_parts = _casefold_parts(resolved)
    if resolved_parts[: len(root_parts)] != root_parts:
        raise _reject(
            "OUTSIDE_ROOT",
            "O caminho resolve fora da raiz concedida.",
            details={"relative_path": relative_path},
        )
    return resolved


def _casefold_parts(path: Path) -> tuple[str, ...]:
    parts = [part for part in path.parts if part not in ("", ".", "..")]
    if sys.platform == "win32":
        parts = [part.rstrip("\\") for part in parts]
        return tuple(part.casefold() for part in parts)
    return tuple(parts)


def assert_snapshot_inside_root(final_path: Path, root_canonical_path: str) -> None:
    """Post-open containment proof (TOCTOU backstop for swapped junctions)."""
    root_parts = _casefold_parts(Path(root_canonical_path))
    final_parts = _casefold_parts(Path(final_path))
    if final_parts[: len(root_parts)] != root_parts:
        raise _reject(
            "PATH_ESCAPED",
            "O caminho aberto saiu da raiz concedida (reparse point trocado?).",
            details={"final_path": str(final_path)},
        )


def definition_snapshot_resolves(definition: SourceDefinitionSnapshot, relative_path: str) -> Path:
    """Resolve a candidate path using a frozen definition snapshot."""
    return resolve_source_path(definition.root_canonical_path, relative_path, allow_empty=True)


__all__ = [
    "PathPolicyError",
    "assert_snapshot_inside_root",
    "definition_snapshot_resolves",
    "is_remote_path",
    "resolve_source_path",
    "validate_root_path",
]
