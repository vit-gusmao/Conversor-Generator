"""Durable import execution (T5, doc 04 §4).

The import row is the authority: items, attempts, snapshots and conversion
links persist in the database, so any crash resumes without duplicating work.
The scan never materializes the whole tree; snapshots are bounded (two in
flight) and deduplicated by ``(source, relative_path, sha256, options_hash)``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from api.deps import ApiError
from models.db import (
    PublicConversionRow,
    SourceImportItemRow,
    SourceImportRow,
    get_session_factory,
)
from sources.repository import SourceRepository
from sources.scanner import SourceUnavailable, scan_source
from sources.service import SourceService
from sources.snapshot import SnapshotError, capture_snapshot
from sources.submission import StandaloneSubmissionPort
from sources.types import ImportRequest, SourceDefinitionSnapshot

ACTIVE_IMPORT_STATUSES = {"pending", "scanning", "submitting", "converting", "indexing"}
SOURCE_LEASE_SECONDS = 120
MAX_SNAPSHOTS_IN_FLIGHT = 2
MAX_TRANSIENT_RETRIES = 3

# Module-level lock map: in-process serialization per source (standalone runs
# one runtime per profile; the lease column covers cross-process safety).
_source_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(source_id: str) -> threading.Lock:
    with _locks_guard:
        if source_id not in _source_locks:
            _source_locks[source_id] = threading.Lock()
        return _source_locks[source_id]


def canonical_request_hash(request: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ImportService:
    def __init__(self, submission: StandaloneSubmissionPort | None = None) -> None:
        self.repository = SourceRepository()
        self.sources = SourceService()
        self.submission = submission or StandaloneSubmissionPort()

    # ------------------------------------------------------------------ api
    def start(
        self,
        *,
        owner_id: str,
        source_id: str,
        request: dict[str, Any],
        idempotency_key: str,
        schedule_run_id: str | None = None,
    ) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            source = self.repository.get_source(session, source_id=source_id, owner_id=owner_id)
            if source is None:
                raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
            root = self.repository.get_root(session, root_id=source.root_id, owner_id=owner_id)
            if root is None or root.revoked_at is not None:
                raise ApiError(409, "ROOT_REVOKED", "A concessão da raiz foi revogada.")

            request_hash = canonical_request_hash(request)
            definition = self.sources.definition_for(source, root)

            active = session.execute(
                select(SourceImportRow).where(
                    SourceImportRow.source_id == source_id,
                    SourceImportRow.status.in_(ACTIVE_IMPORT_STATUSES),
                    SourceImportRow.lease_until > datetime.now(UTC),
                    SourceImportRow.idempotency_key != idempotency_key,
                )
            ).scalar_one_or_none()
            if active is not None:
                raise ApiError(
                    409,
                    "SOURCE_IMPORT_BUSY",
                    "Já existe uma importação ativa para esta fonte.",
                    details={"import_id": active.id},
                )

            options = request.get("options") or {}
            normalized = self._normalize(options, "spreadsheet")
            payload = {
                "options": normalized,
                "force": bool(request.get("force")),
                "collection_id": request.get("collection_id"),
            }
            payload_hash = canonical_request_hash(payload)
            existing = self.repository.record_import(
                session,
                owner_id=owner_id,
                source_id=source_id,
                source_version=source.version,
                idempotency_key=idempotency_key,
                request_hash=payload_hash,
                definition=definition.to_json(),
                options=normalized,
                force=bool(request.get("force")),
                collection_id=request.get("collection_id"),
                schedule_run_id=schedule_run_id,
            )
            if existing.request_hash != payload_hash:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "A mesma chave foi usada com outro corpo.",
                    details={"import_id": existing.id},
                )
            session.commit()
            return self.import_payload(existing, replayed=True)
        finally:
            session.close()

    def start_from_snapshot(
        self,
        *,
        owner_id: str,
        definition: SourceDefinitionSnapshot,
        request: dict[str, Any],
        idempotency_key: str,
        schedule_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Occurrence path: the frozen definition is the authority.

        The executor never re-reads mutable source rows to decide which files
        an older occurrence processes. The root grant is still checked before
        any file is opened (revocation wins over the snapshot).
        """
        session = get_session_factory()()
        try:
            root = self.repository.get_root(
                session, root_id=definition.root_id, owner_id=owner_id
            )
            if root is None or root.revoked_at is not None:
                raise ApiError(409, "ROOT_REVOKED", "A concessão da raiz foi revogada.")

            options = self._normalize(request.get("options") or {}, "spreadsheet")
            payload = {
                "options": options,
                "force": bool(request.get("force")),
                "collection_id": request.get("collection_id"),
            }
            request_hash = canonical_request_hash(payload)
            record = self.repository.record_import(
                session,
                owner_id=owner_id,
                source_id=definition.source_id,
                source_version=definition.version,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                definition=definition.to_json(),
                options=options,
                force=bool(request.get("force")),
                collection_id=request.get("collection_id"),
                schedule_run_id=schedule_run_id,
            )
            if record.request_hash != request_hash:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "A mesma chave foi usada com outro corpo.",
                    details={"import_id": record.id},
                )
            session.commit()
            return self.import_payload(record, replayed=True)
        finally:
            session.close()

    def run(self, import_id: str, owner_id: str) -> dict[str, Any]:
        """Execute (or resume) an import to a terminal state."""
        with _lock_for(owner_id):
            session = get_session_factory()()
            try:
                record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
                if record is None:
                    raise ApiError(404, "IMPORT_NOT_FOUND", "Importação inexistente.")
                if record.status in {"completed", "partial", "failed", "cancelled"}:
                    return self.import_payload(record)
                record.status = "scanning"
                record.lease_owner = f"standalone:{os.getpid()}"
                record.lease_until = datetime.now(UTC) + timedelta(seconds=SOURCE_LEASE_SECONDS)
                record.fence += 1
                session.commit()
                definition = SourceDefinitionSnapshot.from_json(record.definition_json)
                options = record.options_json or {}
                options_hash = self._options_hash(options)
            finally:
                session.close()

            try:
                self._scan_and_capture(import_id, owner_id, definition, options, options_hash, force=bool(record.force))
                self._submit_and_wait(import_id, owner_id, definition, options)
            except SnapshotError as exc:
                self._fail(import_id, owner_id, exc.code, exc.message)
            except ApiError as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                envelope = detail.get("error", {})
                code = envelope.get("code", "IMPORT_FAILED")
                self._fail(import_id, owner_id, code, envelope.get("message", str(exc)))
            except SourceUnavailable as exc:
                self._fail(import_id, owner_id, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - daemon threads never die silently
                import traceback

                self._fail(
                    import_id,
                    owner_id,
                    "IMPORT_INTERNAL_ERROR",
                    f"{exc}\n{traceback.format_exc(limit=6)}",
                )

            session = get_session_factory()()
            try:
                record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
                if record is not None and record.status not in {"completed", "partial", "failed", "cancelled"}:
                    self._finalize_counts(session, record)
                    session.commit()
                return self.import_payload(record)
            finally:
                session.close()

    def run_async(self, import_id: str, owner_id: str) -> None:
        thread = threading.Thread(
            target=self.run, args=(import_id, owner_id), daemon=True, name=f"import-{import_id[:8]}"
        )
        thread.start()

    def cancel(self, import_id: str, owner_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            if record is None:
                raise ApiError(404, "IMPORT_NOT_FOUND", "Importação inexistente.")
            record.cancel_requested_at = datetime.now(UTC)
            session.commit()
            return {"cancel_requested": True}
        finally:
            session.close()

    def get(self, import_id: str, owner_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            if record is None:
                raise ApiError(404, "IMPORT_NOT_FOUND", "Importação inexistente ou de outro dono.")
            return self.import_payload(record)
        finally:
            session.close()

    def items(self, import_id: str, owner_id: str) -> list[dict[str, Any]]:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            if record is None:
                raise ApiError(404, "IMPORT_NOT_FOUND", "Importação inexistente ou de outro dono.")
            rows = session.execute(
                select(SourceImportItemRow)
                .where(SourceImportItemRow.import_id == import_id)
                .order_by(SourceImportItemRow.id)
            ).scalars().all()
            return [
                {
                    "id": row.id,
                    "relative_path": row.relative_path,
                    "status": row.status,
                    "size_bytes": row.size_bytes,
                    "sha256": row.sha256,
                    "upload_id": row.upload_id,
                    "conversion_id": row.conversion_id,
                    "error_code": row.error_code,
                    "error_message": row.error_message,
                    "attempts": row.attempts,
                }
                for row in rows
            ]
        finally:
            session.close()

    # ------------------------------------------------------------- execution
    def _scan_and_capture(
        self,
        import_id: str,
        owner_id: str,
        definition: SourceDefinitionSnapshot,
        options: dict[str, Any],
        options_hash: str,
        *,
        force: bool,
    ) -> None:
        from api.service import _normalize_options

        self._assert_not_cancelled(import_id, owner_id)
        # Revocation wins over the frozen snapshot: no reads without a grant.
        check = get_session_factory()()
        try:
            root = self.repository.get_root(
                check, root_id=definition.root_id, owner_id=owner_id
            )
            if root is None or root.revoked_at is not None:
                raise ApiError(409, "ROOT_REVOKED", "A concessão da raiz foi revogada.")
        finally:
            check.close()
        staging_dir = self._staging_dir()

        captured: list[tuple[Any, Any]] = []
        in_flight: list[Any] = []
        batch: list[SourceCandidate] = []
        for candidate in scan_source(definition):
            self._assert_not_cancelled(import_id, owner_id)
            batch.append(candidate)
            if len(batch) >= 50:
                self._capture_batch(
                    import_id, owner_id, definition, batch, options, options_hash,
                    force=force, staging_dir=staging_dir, captured=captured,
                )
                batch = []
        if batch:
            self._capture_batch(
                import_id, owner_id, definition, batch, options, options_hash,
                force=force, staging_dir=staging_dir, captured=captured,
            )
        self._mark_status(import_id, owner_id, "submitting", total=len(captured))

    def _capture_batch(
        self, import_id, owner_id, definition, batch, options, options_hash, *,
        force, staging_dir, captured,
    ) -> None:
        from sources.path_policy import resolve_source_path

        session = get_session_factory()()
        try:
            known = None
            for candidate in batch:
                self._assert_not_cancelled(import_id, owner_id)
                known_sha, known_hash = self.repository.latest_processed_sha256(
                    session,
                    owner_id=owner_id,
                    source_id=definition.source_id,
                    relative_path=candidate.relative_path,
                )
                attempts = 0
                ref = None
                while ref is None:
                    attempts += 1
                    try:
                        path = resolve_source_path(
                            definition.root_canonical_path,
                            candidate.relative_path,
                            allow_empty=True,
                        )
                        ref = capture_snapshot(
                            candidate,
                            definition.root_canonical_path,
                            staging_dir=staging_dir,
                            stability_seconds=0.5,
                            cancel_check=lambda: self._cancelled(import_id, owner_id),
                            source_path=path,
                        )
                    except SnapshotError as exc:
                        import time

                        if exc.code in {"SOURCE_CHANGED", "SOURCE_BUSY"} and attempts < MAX_TRANSIENT_RETRIES:
                            time.sleep(0.2)
                            continue
                        item = self.repository.add_import_item(
                            session,
                            import_id=import_id,
                            relative_path=candidate.relative_path,
                            client_item_id=f"err:{candidate.relative_path}:{attempts}",
                            size_bytes=candidate.size_bytes,
                            mtime_ns=candidate.mtime_ns,
                        )
                        item.status = "failed"
                        item.error_code = exc.code
                        item.error_message = exc.message
                        item.attempts = attempts
                        break
                if ref is None:
                    continue
                # Dedup decides AFTER capture: the identity needs the current
                # content hash — a skipped file must not shadow real changes.
                if not force and known_sha == ref.sha256 and known_hash == options_hash:
                    # Same content + same options: already processed; drop the
                    # private copy (staging is cleaned by the retention job).
                    try:
                        (staging_dir / ref.key).unlink(missing_ok=True)
                    except OSError:
                        pass
                    item = self.repository.add_import_item(
                        session,
                        import_id=import_id,
                        relative_path=candidate.relative_path,
                        client_item_id=f"skip:{candidate.relative_path}:{ref.sha256[:12]}",
                        size_bytes=ref.size_bytes,
                        mtime_ns=ref.mtime_ns,
                        sha256=ref.sha256,
                    )
                    item.status = "skipped"
                    continue
                client_item_id = f"item:{candidate.relative_path}:{ref.sha256[:12]}"
                existing = self.repository.get_import_item(
                    session, import_id=import_id, client_item_id=client_item_id
                )
                if existing is not None:
                    # Resume: the same content version already has its item.
                    if existing.status == "captured":
                        captured.append((existing, ref))
                    continue
                item = self.repository.add_import_item(
                    session,
                    import_id=import_id,
                    relative_path=candidate.relative_path,
                    client_item_id=client_item_id,
                    size_bytes=ref.size_bytes,
                    mtime_ns=ref.mtime_ns,
                    sha256=ref.sha256,
                    snapshot_key=ref.key,
                )
                item.status = "captured"
                captured.append((item, ref))
            session.commit()
        finally:
            session.close()

    def _submit_and_wait(
        self,
        import_id: str,
        owner_id: str,
        definition: SourceDefinitionSnapshot,
        options: dict[str, Any],
    ) -> None:
        import time as _time

        session = get_session_factory()()
        try:
            items = session.execute(
                select(SourceImportItemRow)
                .where(
                    SourceImportItemRow.import_id == import_id,
                    SourceImportItemRow.status == "captured",
                )
                .order_by(SourceImportItemRow.id)
            ).scalars().all()
        finally:
            session.close()

        if not items:
            return

        batch_id = self.submission.ensure_batch(owner_id, import_id)
        self._set_batch(import_id, owner_id, batch_id)

        storage = self._staging_dir()
        for item in items:
            self._assert_not_cancelled(import_id, owner_id)
            snapshot_path = storage / item.snapshot_key if item.snapshot_key else None
            if snapshot_path is None or not snapshot_path.exists():
                # Snapshot perdido (limpeza/crash): recaptura no próximo run.
                self._mark_item_failed(import_id, owner_id, item.id, "SNAPSHOT_MISSING", "Snapshot não encontrado.")
                continue
            content = snapshot_path.read_bytes()
            try:
                result = self.submission.submit_snapshot(
                    owner_id,
                    batch_id,
                    import_id=import_id,
                    import_item_id=item.id,
                    relative_path=item.relative_path,
                    snapshot_key=item.snapshot_key,
                    snapshot_bytes=content,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    normalized_options=options,
                )
            except ApiError as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                envelope = detail.get("error", {})
                code = envelope.get("code", "SUBMISSION_FAILED")
                self._mark_item_failed(
                    import_id, owner_id, item.id, code, envelope.get("message", str(exc))
                )
                continue
            self._mark_item_submitted(import_id, owner_id, item.id, result)

        self.submission.seal_batch(owner_id, batch_id)
        self._mark_status(import_id, owner_id, "converting")

        # Wait for terminal conversions (inline queue executes quickly; the
        # poll keeps the executor thread small and resumable).
        deadline = _time.monotonic() + 120
        while _time.monotonic() < deadline:
            if self._cancelled(import_id, owner_id):
                self.submission.cancel(owner_id, batch_id)
                self._mark_status(import_id, owner_id, "cancelled")
                return
            progress = self._conversion_progress(import_id, owner_id)
            if progress["pending"] == 0:
                break
            _time.sleep(0.2)
        self._mark_status(import_id, owner_id, "finalize")
        self._record_versions(import_id, owner_id, definition, options)

    def _record_versions(
        self, import_id: str, owner_id: str, definition: SourceDefinitionSnapshot, options: dict[str, Any]
    ) -> None:
        options_hash = self._options_hash(options)
        session = get_session_factory()()
        try:
            items = session.execute(
                select(SourceImportItemRow).where(SourceImportItemRow.import_id == import_id)
            ).scalars().all()
            for item in items:
                if item.status != "submitted":
                    continue
                conversion = session.get(PublicConversionRow, item.conversion_id) if item.conversion_id else None
                status = conversion.status if conversion is not None else "queued"
                if status in {"completed", "completed_with_warnings"}:
                    self.repository.upsert_source_file_version(
                        session,
                        owner_id=owner_id,
                        source_id=definition.source_id,
                        relative_path=item.relative_path,
                        sha256=item.sha256,
                        options_hash=options_hash,
                        import_id=import_id,
                        conversion_id=item.conversion_id,
                    )
                    item.status = "completed"
                elif status == "failed":
                    item.status = "failed"
                    item.error_code = "CONVERSION_FAILED"
                else:
                    # Still converting: stays resumable (checked on next run).
                    item.status = "queued"
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            self._finalize_counts(session, record)
            session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------- helpers
    def _conversion_progress(self, import_id: str, owner_id: str) -> dict[str, int]:
        session = get_session_factory()()
        try:
            items = session.execute(
                select(SourceImportItemRow).where(SourceImportItemRow.import_id == import_id)
            ).scalars().all()
            pending = 0
            for item in items:
                if item.status != "submitted":
                    continue
                conversion = session.get(PublicConversionRow, item.conversion_id) if item.conversion_id else None
                if conversion is None or conversion.status not in {
                    "completed", "completed_with_warnings", "failed", "cancelled"
                }:
                    pending += 1
            return {"pending": pending}
        finally:
            session.close()

    def _finalize_counts(self, session, record) -> None:
        items = session.execute(
            select(SourceImportItemRow).where(SourceImportItemRow.import_id == record.id)
        ).scalars().all()
        record.total_files = len(items)
        record.imported_files = sum(1 for item in items if item.status == "completed")
        record.skipped_files = sum(1 for item in items if item.status == "skipped")
        record.failed_files = sum(1 for item in items if item.status == "failed")
        pending = sum(
            1 for item in items if item.status in {"queued", "submitted", "captured", "pending"}
        )
        if record.cancel_requested_at is not None and pending:
            record.status = "cancelled"
        elif pending:
            record.status = "partial" if record.imported_files or record.skipped_files else "failed"
            record.error_code = "IMPORT_INCOMPLETE"
            record.error_message = f"{pending} arquivo(s) ainda não terminaram; retomável."
        elif record.failed_files:
            record.status = "partial" if record.imported_files else "failed"
        else:
            record.status = "completed"
        record.lease_until = None

    def _mark_item_failed(self, import_id, owner_id, item_id, code, message) -> None:
        session = get_session_factory()()
        try:
            item = session.get(SourceImportItemRow, item_id)
            if item is not None:
                item.status = "failed"
                item.error_code = code
                item.error_message = message
                item.attempts += 1
            session.commit()
        finally:
            session.close()

    def _mark_item_submitted(self, import_id, owner_id, item_id, result) -> None:
        session = get_session_factory()()
        try:
            item = session.get(SourceImportItemRow, item_id)
            if item is not None:
                item.status = "submitted"
                item.upload_id = result.upload_id
                item.conversion_id = result.conversion_id
            session.commit()
        finally:
            session.close()

    def _mark_status(self, import_id, owner_id, status, total: int | None = None) -> None:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            if record is not None:
                record.status = status
                if total is not None:
                    record.total_files = total
                record.lease_until = datetime.now(UTC) + timedelta(seconds=SOURCE_LEASE_SECONDS)
                session.commit()
        finally:
            session.close()

    def _set_batch(self, import_id, owner_id, batch_id) -> None:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            if record is not None:
                record.batch_id = batch_id
                session.commit()
        finally:
            session.close()

    def _fail(self, import_id, owner_id, code, message) -> None:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            if record is not None and record.status not in {"completed", "partial", "cancelled"}:
                record.status = "failed"
                record.error_code = code
                record.error_message = message
                record.lease_until = None
                session.commit()
        finally:
            session.close()

    def _cancelled(self, import_id: str, owner_id: str) -> bool:
        session = get_session_factory()()
        try:
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            return bool(record is not None and record.cancel_requested_at is not None)
        finally:
            session.close()

    def _assert_not_cancelled(self, import_id, owner_id) -> None:
        if self._cancelled(import_id, owner_id):
            raise SnapshotError("CANCELLED", "Importação cancelada.")

    def _staging_dir(self) -> Any:
        from pathlib import Path

        from config import settings

        path = Path(settings.data_dir) / "sources" / "staging"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _normalize(self, options: dict[str, Any], kind: str) -> dict[str, Any]:
        from api.service import _normalize_options

        return _normalize_options(options or {}, kind)

    def _options_hash(self, options: dict[str, Any]) -> str:
        payload = dict(options)
        payload.setdefault("reader_version", "spreadsheet-reader-v1")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def import_payload(self, record, replayed: bool = False) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_id": record.owner_id,
            "source_id": record.source_id,
            "source_version": record.source_version,
            "status": record.status,
            "batch_id": record.batch_id,
            "counts": {
                "total": record.total_files,
                "imported": record.imported_files,
                "skipped": record.skipped_files,
                "failed": record.failed_files,
            },
            "error": (
                {"code": record.error_code, "message": record.error_message}
                if record.error_code
                else None
            ),
            "replayed": replayed,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        }


__all__ = ["ImportService", "canonical_request_hash"]
