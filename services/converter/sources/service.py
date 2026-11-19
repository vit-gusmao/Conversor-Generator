"""Source application service (T4): owner-checked operations over the repo.

The grant (root concession) is a local administrative operation — never a
free public route. Browser owners may only use roots already granted to
them; the desktop adds grants through its own authenticated channel (T12).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from sources.path_policy import PathPolicyError, resolve_source_path
from sources.repository import SourceRepository
from sources.scanner import SourceUnavailable, scan_source
from sources.types import SourceCandidate, SourceValidation

MAX_SAMPLE_FILES = 100


class SourceService:
    def __init__(self, repository: SourceRepository | None = None) -> None:
        self.repository = repository or SourceRepository()

    # ----------------------------------------------------------------- roots
    def grant_root(
        self,
        session: Session,
        *,
        owner_id: str,
        canonical_path: str,
        granted_by: str,
        runtime_id: str = "local",
        label: str | None = None,
        grant_channel: str = "local-admin",
    ) -> dict[str, Any]:
        from sources.path_policy import validate_root_path

        try:
            root_path = validate_root_path(canonical_path)
        except PathPolicyError as exc:
            raise self._api_error(exc) from exc
        if not root_path.exists() or not root_path.is_dir():
            from api.deps import ApiError

            raise ApiError(
                400,
                "ROOT_UNAVAILABLE",
                "A pasta da raiz não existe ou não é um diretório.",
                details={"root": canonical_path},
            )
        # Store the real location: later containment checks compare resolved
        # file paths, so a junction/subst/8.3 alias would never match. The
        # resolved target is validated again: a link may lead to a UNC share.
        real_root = os.path.realpath(str(root_path))
        try:
            validate_root_path(real_root)
        except PathPolicyError as exc:
            raise self._api_error(exc) from exc
        root = self.repository.create_root(
            session,
            owner_id=owner_id,
            runtime_id=runtime_id,
            canonical_path=real_root,
            granted_by=granted_by,
            label=label,
            grant_channel=grant_channel,
        )
        return {
            "id": root.id,
            "owner_id": owner_id,
            "runtime_id": runtime_id,
            "label": label,
            "created_at": root.created_at.isoformat(),
        }

    def list_roots(self, session: Session, *, owner_id: str) -> list[dict[str, Any]]:
        roots = self.repository.list_roots(session, owner_id=owner_id)
        items = []
        for root in roots:
            available = bool(root.revoked_at is None and Path(root.canonical_path).exists())
            items.append(
                {
                    "id": root.id,
                    "label": root.label,
                    "runtime_id": root.runtime_id,
                    "runtime_available": available,
                    # The absolute path is shown only to its own owner.
                    "display_path": root.canonical_path,
                    "created_at": root.created_at.isoformat(),
                }
            )
        return items

    def revoke_root(self, session: Session, *, root_id: str, owner_id: str) -> bool:
        return self.repository.revoke_root(session, root_id=root_id, owner_id=owner_id)

    # --------------------------------------------------------------- sources
    def create_source(self, session: Session, *, owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from sources.types import validate_source_create

        from api.deps import ApiError

        create = validate_source_create(payload)
        root = self.repository.get_root(session, root_id=create.root_id, owner_id=owner_id)
        if root is None:
            raise ApiError(
                404,
                "ROOT_NOT_FOUND",
                "Raiz concedida inexistente para este dono.",
                details={"root_id": create.root_id},
            )
        try:
            resolve_source_path(root.canonical_path, create.relative_path, allow_empty=True)
        except PathPolicyError as exc:
            raise self._api_error(exc) from exc
        if not Path(root.canonical_path, *create.relative_path.replace("\\", "/").split("/")).exists():
            raise ApiError(
                400,
                "SOURCE_UNAVAILABLE",
                "O caminho da fonte não existe na raiz concedida.",
                details={"relative_path": create.relative_path},
            )
        source = self.repository.create_source(
            session,
            owner_id=owner_id,
            root_id=root.id,
            relative_path=create.relative_path,
            kind=create.kind,
            name=create.name,
            recursive=create.recursive,
            include_patterns=create.include_patterns,
            exclude_patterns=create.exclude_patterns,
        )
        return self.source_payload(source, root)

    def get_source(self, session: Session, *, source_id: str, owner_id: str) -> dict[str, Any] | None:
        source = self.repository.get_source(session, source_id=source_id, owner_id=owner_id)
        if source is None:
            return None
        root = self.repository.get_root(session, root_id=source.root_id, owner_id=owner_id)
        return self.source_payload(source, root)

    def list_sources(self, session: Session, *, owner_id: str) -> list[dict[str, Any]]:
        items = []
        for source in self.repository.list_sources(session, owner_id=owner_id):
            root = self.repository.get_root(session, root_id=source.root_id, owner_id=owner_id)
            items.append(self.source_payload(source, root))
        return items

    def validate_source(self, session: Session, *, source_id: str, owner_id: str) -> SourceValidation:
        """Sampled validation; never starts a conversion."""
        source = self.repository.get_source(session, source_id=source_id, owner_id=owner_id)
        if source is None:
            from api.deps import ApiError

            raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
        root = self.repository.get_root(session, root_id=source.root_id, owner_id=owner_id)
        if root is None or root.revoked_at is not None:
            return SourceValidation(
                source_id=source_id,
                ok=False,
                available=False,
                code="ROOT_REVOKED",
                message="A concessão da raiz foi revogada.",
                sample=[],
                estimated_files=0,
                estimated_bytes=0,
                warnings=["Raiz revogada: novas leituras estão bloqueadas."],
            )
        definition = self.definition_for(source, root)
        try:
            candidates: list[SourceCandidate] = []
            warnings: list[str] = []
            total_files = 0
            total_bytes = 0
            for candidate in scan_source(definition):
                total_files += 1
                total_bytes += candidate.size_bytes
                if len(candidates) < MAX_SAMPLE_FILES:
                    candidates.append(candidate)
            return SourceValidation(
                source_id=source_id,
                ok=True,
                available=True,
                code=None,
                message=None,
                sample=candidates,
                estimated_files=total_files,
                estimated_bytes=total_bytes,
                warnings=warnings,
            )
        except SourceUnavailable as exc:
            return SourceValidation(
                source_id=source_id,
                ok=False,
                available=False,
                code=exc.code,
                message=exc.message,
                sample=[],
                estimated_files=0,
                estimated_bytes=0,
                warnings=[],
            )
        except PathPolicyError as exc:
            return SourceValidation(
                source_id=source_id,
                ok=False,
                available=False,
                code=exc.code,
                message=exc.message,
                sample=[],
                estimated_files=0,
                estimated_bytes=0,
                warnings=[],
            )

    def update_source(
        self, session: Session, *, source_id: str, owner_id: str, version: int, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        # The same policy as creation: a typo or traversal must not be stored.
        if "relative_path" in changes or "kind" in changes:
            from api.deps import ApiError
            from sources.types import validate_source_create

            current = self.repository.get_source(session, source_id=source_id, owner_id=owner_id)
            if current is None:
                return None
            root = self.repository.get_root(session, root_id=current.root_id, owner_id=owner_id)
            if root is None:
                raise ApiError(404, "ROOT_NOT_FOUND", "Raiz concedida inexistente para este dono.")
            candidate = {
                "root_id": current.root_id,
                "relative_path": changes.get("relative_path", current.relative_path),
                "kind": changes.get("kind", current.kind),
                "name": changes.get("name", current.name),
                "recursive": changes.get("recursive", bool(current.recursive)),
                "include_patterns": changes.get("include_patterns", current.include_patterns),
                "exclude_patterns": changes.get("exclude_patterns", current.exclude_patterns),
            }
            parsed = validate_source_create(candidate)
            try:
                resolved = resolve_source_path(
                    root.canonical_path, parsed.relative_path, allow_empty=True
                )
            except PathPolicyError as exc:
                raise self._api_error(exc) from exc
            if not resolved.exists():
                raise ApiError(
                    400,
                    "SOURCE_UNAVAILABLE",
                    "O caminho da fonte não existe na raiz concedida.",
                    details={"relative_path": parsed.relative_path},
                )
        updated = self.repository.update_source(
            session,
            source_id=source_id,
            owner_id=owner_id,
            expected_version=version,
            changes=changes,
        )
        if updated is None:
            return None
        root = self.repository.get_root(session, root_id=updated.root_id, owner_id=owner_id)
        return self.source_payload(updated, root)

    def archive_source(self, session: Session, *, source_id: str, owner_id: str) -> bool:
        return self.repository.archive_source(session, source_id=source_id, owner_id=owner_id)

    # --------------------------------------------------------------- helpers
    def definition_for(self, source, root) -> Any:
        from sources.types import SourceDefinitionSnapshot

        return SourceDefinitionSnapshot(
            source_id=source.id,
            root_id=root.id,
            root_canonical_path=root.canonical_path,
            relative_path=source.relative_path,
            kind=source.kind,
            recursive=bool(source.recursive),
            include_patterns=source.include_patterns,
            exclude_patterns=source.exclude_patterns,
            version=source.version,
        )

    def source_payload(self, source, root) -> dict[str, Any]:
        return {
            "id": source.id,
            "owner_id": source.owner_id,
            "root_id": source.root_id,
            "relative_path": source.relative_path,
            "kind": source.kind,
            "name": source.name,
            "recursive": bool(source.recursive),
            "include_patterns": source.include_patterns,
            "exclude_patterns": source.exclude_patterns,
            "enabled": bool(source.enabled),
            "version": source.version,
            "runtime_id": root.runtime_id if root else "local",
            # Revocation wins: a source under a revoked root is unavailable.
            "runtime_available": bool(
                root is not None
                and root.revoked_at is None
                and Path(root.canonical_path).exists()
            ),
            "created_at": source.created_at.isoformat(),
            "updated_at": source.updated_at.isoformat(),
        }

    @staticmethod
    def _api_error(exc: PathPolicyError):
        from api.deps import ApiError

        return ApiError(400, exc.code, exc.message, details=exc.details)


__all__ = ["SourceService", "MAX_SAMPLE_FILES"]
