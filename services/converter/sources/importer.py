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

from sqlalchemy import and_, or_, select

from api.deps import ApiError
from models.db import (
    PublicConversionRow,
    SourceFileVersionRow,
    SourceImportItemRow,
    SourceImportRow,
    get_session_factory,
)
from sources.gateway_submission import GatewaySubmissionError
from sources.path_policy import PathPolicyError, resolve_source_path
from sources.repository import SourceRepository
from sources.scanner import SourceUnavailable, scan_source
from sources.service import SourceService
from sources.snapshot import SnapshotError, capture_snapshot
from sources.submission import StandaloneSubmissionPort
from sources.types import ImportRequest, SourceCandidate, SourceDefinitionSnapshot

ACTIVE_IMPORT_STATUSES = {"pending", "scanning", "submitting", "converting", "indexing"}
SOURCE_LEASE_SECONDS = 120
MAX_SNAPSHOTS_IN_FLIGHT = 2
MAX_TRANSIENT_RETRIES = 3
#: Failed-import codes that ``run()`` resumes instead of returning as final.
GATEWAY_UNAVAILABLE_CODE = "IMPORT_GATEWAY_UNAVAILABLE"
RESUMABLE_ERROR_CODES = frozenset({"IMPORT_INCOMPLETE", GATEWAY_UNAVAILABLE_CODE})

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


def _is_transient_submission(exc: BaseException) -> bool:
    """Transient gateway/network failure that a later attempt can clear.

    Delegates to the executor's rule so the importer and the occurrence retry
    schedule always agree on what is retryable.
    """
    if not isinstance(exc, (GatewaySubmissionError, ConnectionError, TimeoutError)):
        return False
    from automation.executor import _is_transient  # same rule as the retry scheduler

    return _is_transient(exc)


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
        self._assert_imports_enabled()
        session = get_session_factory()()
        try:
            source = self.repository.get_source(session, source_id=source_id, owner_id=owner_id)
            if source is None:
                raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
            if not source.enabled:
                raise ApiError(409, "SOURCE_DISABLED", "A fonte está desativada.")
            root = self.repository.get_root(session, root_id=source.root_id, owner_id=owner_id)
            if root is None or root.revoked_at is not None:
                raise ApiError(409, "ROOT_REVOKED", "A concessão da raiz foi revogada.")

            definition = self.sources.definition_for(source, root)
            active = self._active_import_for_source(session, source_id, idempotency_key)
            if active is not None:
                raise ApiError(
                    409,
                    "SOURCE_IMPORT_BUSY",
                    "Já existe uma importação ativa para esta fonte.",
                    details={"import_id": active.id},
                )

            options = request.get("options") or {}
            # Options are normalized with the kind the source actually holds:
            # a PDF from a source must not adopt v3 spreadsheet semantics.
            normalized = self._normalize_for_source(options, definition)
            # The source is part of the payload: the same key on another
            # source is a conflict, never a replay of someone else's import.
            payload = {
                "source_id": source_id,
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
        self._assert_imports_enabled()
        session = get_session_factory()()
        try:
            root = self.repository.get_root(
                session, root_id=definition.root_id, owner_id=owner_id
            )
            if root is None or root.revoked_at is not None:
                raise ApiError(409, "ROOT_REVOKED", "A concessão da raiz foi revogada.")

            # Same rule as start(): the frozen definition decides the kind.
            options = self._normalize_for_source(request.get("options") or {}, definition)
            payload = {
                "source_id": definition.source_id,
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
                    # An import left with items still in flight (`partial`, or
                    # `failed` when nothing finished yet) stays resumable:
                    # re-running reconciles those items instead of leaving the
                    # import stuck forever. A transient gateway outage is also
                    # resumable: the same keys make the replay idempotent.
                    resumable = (
                        record.status in {"partial", "failed"}
                        and record.error_code in RESUMABLE_ERROR_CODES
                    )
                    if not resumable:
                        return self.import_payload(record)
                record.status = "scanning"
                record.lease_owner = f"standalone:{os.getpid()}"
                record.lease_until = datetime.now(UTC) + timedelta(seconds=SOURCE_LEASE_SECONDS)
                record.fence += 1
                session.commit()
                definition = SourceDefinitionSnapshot.from_json(record.definition_json)
                options = record.options_json or {}
                options_hash = self._options_hash(options)
                # Automation occurrences let the executor own the retry schedule.
                automation = record.schedule_run_id is not None
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
                if _is_transient_submission(exc):
                    message = getattr(exc, "message", None) or str(exc)
                    self._fail(import_id, owner_id, GATEWAY_UNAVAILABLE_CODE, message)
                    if automation:
                        # Re-raised unchanged: the executor classifies it as
                        # transient and reschedules; the next advance() resumes
                        # this import (status failed + resumable code).
                        raise
                else:
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
        scanned: set[str] = set()
        for candidate in scan_source(definition):
            self._assert_not_cancelled(import_id, owner_id)
            scanned.add(candidate.relative_path)
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
        # Reaching this line means the walk completed: an unavailable root
        # raised before any candidate, and a cancel raised inside the loop.
        self._flag_vanished_origins(owner_id, definition.source_id, scanned)
        self._mark_status(import_id, owner_id, "submitting", total=len(captured))

    def _flag_vanished_origins(self, owner_id: str, source_id: str, scanned: set[str]) -> None:
        """Flag heads whose origin was imported before but is absent from this full scan."""
        from knowledge.repository import KnowledgeRepository

        session = get_session_factory()()
        try:
            imported = set(
                session.execute(
                    select(SourceFileVersionRow.relative_path).where(
                        SourceFileVersionRow.owner_id == owner_id,
                        SourceFileVersionRow.source_id == source_id,
                    ).distinct()
                ).scalars().all()
            )
        finally:
            session.close()
        vanished = sorted(imported - scanned)
        if vanished:
            KnowledgeRepository().mark_source_missing(owner_id, source_id, vanished)

    def _capture_batch(
        self, import_id, owner_id, definition, batch, options, options_hash, *,
        force, staging_dir, captured,
    ) -> None:
        session = get_session_factory()()
        missing: list[str] = []
        try:
            from config import settings as runtime_settings

            max_bytes = runtime_settings.max_bytes()
            for candidate in batch:
                self._assert_not_cancelled(import_id, owner_id)
                if max_bytes and candidate.size_bytes > max_bytes:
                    # Never copy a file the admission path would reject anyway.
                    client_item_id = f"big:{candidate.relative_path}:{candidate.size_bytes}"
                    if not self._has_item(session, import_id, client_item_id):
                        item = self.repository.add_import_item(
                            session,
                            import_id=import_id,
                            relative_path=candidate.relative_path,
                            client_item_id=client_item_id,
                            size_bytes=candidate.size_bytes,
                            mtime_ns=candidate.mtime_ns,
                        )
                        item.status = "failed"
                        item.error_code = "FILE_TOO_LARGE"
                        item.error_message = (
                            f"O arquivo excede o limite de {max_bytes // (1024 * 1024)} MiB."
                        )
                    continue
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
                        try:
                            path = resolve_source_path(
                                definition.root_canonical_path,
                                candidate.relative_path,
                                allow_empty=True,
                            )
                        except PathPolicyError as exc:
                            # A refused name is an item fact, never an aborted import.
                            raise SnapshotError.from_policy(exc) from exc
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
                        if exc.code == "SOURCE_MISSING":
                            # Flagged after this batch's session closes (no lock contention).
                            missing.append(candidate.relative_path)
                        client_item_id = f"err:{candidate.relative_path}:{attempts}"
                        if not self._has_item(session, import_id, client_item_id):
                            item = self.repository.add_import_item(
                                session,
                                import_id=import_id,
                                relative_path=candidate.relative_path,
                                client_item_id=client_item_id,
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
                    client_item_id = f"skip:{candidate.relative_path}:{ref.sha256[:12]}"
                    if not self._has_item(session, import_id, client_item_id):
                        item = self.repository.add_import_item(
                            session,
                            import_id=import_id,
                            relative_path=candidate.relative_path,
                            client_item_id=client_item_id,
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
        if missing:
            from knowledge.repository import KnowledgeRepository

            KnowledgeRepository().mark_source_missing(owner_id, definition.source_id, missing)

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
            record = self.repository.get_import(session, import_id=import_id, owner_id=owner_id)
            batch_id = record.batch_id if record is not None else None
        finally:
            session.close()

        # A resumed run may have nothing left to submit (every item already
        # submitted before a crash). It must still wait for the conversions
        # and record versions, so only the submit loop is skipped.
        if items:
            batch_id = self.submission.ensure_batch(owner_id, import_id)
            self._set_batch(import_id, owner_id, batch_id)
            self._submit_items(import_id, owner_id, batch_id, items, options)
            self.submission.seal_batch(owner_id, batch_id)
        self._mark_status(import_id, owner_id, "converting")

        # Wait for terminal conversions (inline queue executes quickly; the
        # poll keeps the executor thread small and resumable).
        deadline = _time.monotonic() + 120
        while _time.monotonic() < deadline:
            if self._cancelled(import_id, owner_id):
                if batch_id is not None:
                    self.submission.cancel(owner_id, batch_id)
                self._mark_status(import_id, owner_id, "cancelled")
                return
            progress = self._conversion_progress(import_id, owner_id)
            if progress["pending"] == 0:
                break
            _time.sleep(0.2)
        self._mark_status(import_id, owner_id, "converting")
        self._record_versions(import_id, owner_id, definition, options)

    def _submit_items(self, import_id, owner_id, batch_id, items, options) -> None:
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
            # The upload is confirmed: the private copy is no longer needed.
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                pass

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
                if item.status not in {"submitted", "queued"}:
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
    def _has_item(self, session, import_id: str, client_item_id: str) -> bool:
        """Deterministic item keys make a re-run hit the same row: reuse it."""
        return self.repository.get_import_item(
            session, import_id=import_id, client_item_id=client_item_id
        ) is not None

    @staticmethod
    def _assert_imports_enabled() -> None:
        from config import settings as runtime_settings

        if not getattr(runtime_settings, "source_imports_enabled", True):
            raise ApiError(
                409,
                "FEATURE_DISABLED",
                "Importações por caminho estão desabilitadas neste runtime.",
            )

    @staticmethod
    def _active_import_for_source(session, source_id: str, idempotency_key: str):
        """An import holds the source while it is running, or while it is queued.

        A ``pending`` row has no lease yet, so it counts as active for one
        lease window after creation: a queued import is never overlapped, and
        an abandoned one cannot block the source forever.
        """
        now = datetime.now(UTC)
        pending_window = now - timedelta(seconds=SOURCE_LEASE_SECONDS)
        return session.execute(
            select(SourceImportRow)
            .where(
                SourceImportRow.source_id == source_id,
                SourceImportRow.idempotency_key != idempotency_key,
                SourceImportRow.status.in_(ACTIVE_IMPORT_STATUSES),
                or_(
                    SourceImportRow.lease_until > now,
                    and_(
                        SourceImportRow.status == "pending",
                        SourceImportRow.created_at > pending_window,
                    ),
                ),
            )
            .limit(1)
        ).scalars().first()

    def _conversion_progress(self, import_id: str, owner_id: str) -> dict[str, int]:
        session = get_session_factory()()
        try:
            items = session.execute(
                select(SourceImportItemRow).where(SourceImportItemRow.import_id == import_id)
            ).scalars().all()
            pending = 0
            for item in items:
                if item.status not in {"submitted", "queued"}:
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
                # A cooperative cancel is a distinct terminal state, never a
                # failure: the user asked for it and history stays coherent.
                record.status = "cancelled" if code == "CANCELLED" else "failed"
                if code != "CANCELLED":
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

    def _normalize_for_source(
        self, options: dict[str, Any], definition: SourceDefinitionSnapshot
    ) -> dict[str, Any]:
        """Pick the kind from the source's declared formats.

        A source restricted to spreadsheets normalizes as v3; a mixed/PDF
        source keeps the legacy behaviour so per-file probing decides.
        """
        include = [str(pattern).lower() for pattern in (definition.include_patterns or [])]
        only_spreadsheets = bool(include) and all(
            any(token in pattern for token in (".xlsx", ".csv", "xlsx", "csv")) for pattern in include
        )
        kind = "spreadsheet" if only_spreadsheets else "pdf"
        return self._normalize(options, kind)

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
