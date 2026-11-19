"""DTOs and validation for the sources domain (T1).

Mirrors ``packages/contracts/src/sources.ts``. Owner identity always comes
from the session/credential, never from the payload: create DTOs forbid an
``owner_id`` field outright (fixture ``foreign_owner_ignored``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from api.deps import ApiError

SOURCE_KINDS = ("file", "directory")
SOURCE_READER_VERSION = "spreadsheet-reader-v1"


class SourceCreate(BaseModel):
    """Body of ``POST /sources`` (strict: unknown fields are client bugs)."""

    model_config = ConfigDict(extra="forbid")

    root_id: str
    relative_path: str
    kind: Literal["file", "directory"]
    name: str
    recursive: bool = False
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None


def _map_validation_error(exc: ValidationError, context: str) -> ApiError:
    for issue in exc.errors():
        if issue.get("type") == "extra_forbidden":
            return ApiError(
                400,
                "UNKNOWN_FIELD",
                f"Campo desconhecido em {context}; a identidade do dono vem da sessão.",
                details={"fields": [str(part) for part in issue.get("loc", ())]},
            )
        if issue.get("type") == "literal_error" and "kind" in issue.get("loc", ()):
            return ApiError(
                400,
                "INVALID_SOURCE",
                "kind aceita apenas 'file' (arquivo único) ou 'directory' (pasta).",
                details={"kind": issue.get("input")},
            )
    return ApiError(
        400,
        "INVALID_PAYLOAD",
        f"Cadastro de fonte inválido: {exc.error_count()} problema(s).",
        details={"issues": [{"path": ".".join(str(p) for p in i.get("loc", ())), "message": i.get("msg", "")} for i in exc.errors()]},
    )


def validate_source_create(raw: dict[str, Any]) -> SourceCreate:
    """Parse ``POST /sources`` bodies, mapping errors to the HTTP envelope."""
    if not isinstance(raw, dict):
        raise ApiError(400, "INVALID_PAYLOAD", "O corpo deve ser um objeto JSON.")
    try:
        parsed = SourceCreate.model_validate(raw)
    except ValidationError as exc:
        raise _map_validation_error(exc, "fonte") from exc
    _validate_source_semantics(parsed)
    return parsed


def _validate_source_semantics(parsed: SourceCreate) -> None:
    relative = parsed.relative_path.replace("\\", "/")
    if not relative or relative.startswith("/"):
        raise ApiError(
            400,
            "INVALID_SOURCE",
            "relative_path deve ser um caminho relativo não vazio dentro da raiz concedida.",
            details={"relative_path": parsed.relative_path},
        )
    parts = [part for part in relative.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ApiError(
            400,
            "INVALID_SOURCE",
            "Travessia ('..') não é permitida em relative_path.",
            details={"relative_path": parsed.relative_path},
        )
    if not parts:
        raise ApiError(
            400,
            "INVALID_SOURCE",
            "relative_path deve nomear um arquivo ou pasta dentro da raiz concedida.",
            details={"relative_path": parsed.relative_path},
        )
    for group, patterns in (("include_patterns", parsed.include_patterns), ("exclude_patterns", parsed.exclude_patterns)):
        if patterns is None:
            continue
        if not isinstance(patterns, list) or len(patterns) > 64:
            raise ApiError(400, "INVALID_SOURCE", f"{group} aceita no máximo 64 padrões.")
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern or len(pattern) > 255:
                raise ApiError(400, "INVALID_SOURCE", f"Padrão inválido em {group}.")


@dataclass(slots=True, frozen=True)
class SourceDefinitionSnapshot:
    """Immutable definition copied into imports/occurrences (doc 02, §5).

    The executor consumes this copy — it never re-reads mutable source rows to
    decide which files belong to an older execution. Root grant revocation
    still wins at open time.
    """

    source_id: str
    root_id: str
    root_canonical_path: str
    relative_path: str
    kind: str
    recursive: bool
    include_patterns: list[str] | None
    exclude_patterns: list[str] | None
    version: int

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "root_id": self.root_id,
            "root_canonical_path": self.root_canonical_path,
            "relative_path": self.relative_path,
            "kind": self.kind,
            "recursive": self.recursive,
            "include_patterns": list(self.include_patterns) if self.include_patterns else None,
            "exclude_patterns": list(self.exclude_patterns) if self.exclude_patterns else None,
            "version": self.version,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "SourceDefinitionSnapshot":
        return cls(
            source_id=str(payload["source_id"]),
            root_id=str(payload["root_id"]),
            root_canonical_path=str(payload["root_canonical_path"]),
            relative_path=str(payload["relative_path"]),
            kind=str(payload["kind"]),
            recursive=bool(payload["recursive"]),
            include_patterns=payload.get("include_patterns"),
            exclude_patterns=payload.get("exclude_patterns"),
            version=int(payload["version"]),
        )


@dataclass(slots=True)
class ImportRequest:
    """Options for one import execution (manual or scheduled)."""

    options: dict[str, Any] = field(default_factory=dict)
    force: bool = False
    collection_id: str | None = None


IMPORT_STATUSES = (
    "pending",       # created, not yet scanning
    "scanning",
    "submitting",
    "converting",
    "indexing",
    "completed",
    "partial",
    "failed",
    "cancelled",
)
IMPORT_TERMINAL_STATUSES = ("completed", "partial", "failed", "cancelled")
IMPORT_ITEM_STATUSES = (
    "pending",
    "captured",
    "skipped",
    "uploading",
    "queued",
    "converting",
    "completed",
    "failed",
    "cancelled",
)


@dataclass(slots=True, frozen=True)
class SourceCandidate:
    """One eligible file found by the scanner (metadata only, no content)."""

    relative_path: str
    size_bytes: int
    mtime_ns: int


@dataclass(slots=True, frozen=True)
class SnapshotRef:
    """A captured, private copy of one candidate file plus its identity."""

    key: str
    sha256: str
    size_bytes: int
    relative_path: str
    mtime_ns: int


@dataclass(slots=True, frozen=True)
class SourceValidation:
    """Result of ``POST /sources/{id}/validate`` (sampled, no conversion)."""

    source_id: str
    ok: bool
    available: bool
    code: str | None
    message: str | None
    sample: list[SourceCandidate]
    estimated_files: int
    estimated_bytes: int
    warnings: list[str]


@dataclass(slots=True, frozen=True)
class BatchRef:
    batch_id: str


@dataclass(slots=True, frozen=True)
class ConversionRef:
    item_id: str
    upload_id: str
    conversion_id: str


@dataclass(slots=True, frozen=True)
class ImportItemProgress:
    batch_id: str
    finished_items: int
    total_items: int
    failed_items: int
