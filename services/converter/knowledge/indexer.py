"""Authorized, idempotent indexing.

Only explicitly selected sources are indexed:
* conversions owned by the session user;
* local study files explicitly named by path, restricted to the optional ``study/`` tree.

Directory scanning, ``.env``, databases, binaries and the full repository tree
are never indexed.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import SERVICE_DIR
from knowledge.chunker import chunk_study_text, chunk_text_plan
from knowledge.repository import KnowledgeRepository
from models.db import PublicConversionRow, PublicDocumentRow, get_session_factory
from repositories.conversion_repository import ConversionRepository

PROJECT_ROOT = SERVICE_DIR.parent.parent
STUDY_ROOT = PROJECT_ROOT / "study"
STUDY_EXTENSIONS = {".md", ".txt", ".json", ".csv"}
MAX_STUDY_BYTES = 4 * 1024 * 1024

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="knowledge-index")


class IndexingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class KnowledgeIndexer:
    def __init__(self, repository: KnowledgeRepository | None = None) -> None:
        self.repo = repository or KnowledgeRepository()
        self.conversions = ConversionRepository()

    # -- public ------------------------------------------------------------
    def submit(self, *, owner_id: str, request: dict[str, Any]) -> dict[str, Any]:
        collection = self.repo.ensure_collection(
            owner_id=owner_id,
            collection_id=request.get("collection_id"),
            name=request.get("collection_name"),
            scope=request.get("scope") or "documents",
        )
        job = self.repo.create_job(
            owner_id=owner_id,
            collection_id=collection["collection_id"],
            source={
                "conversion_ids": request.get("conversion_ids") or [],
                "source_paths": request.get("source_paths") or [],
                "scope": request.get("scope") or "documents",
            },
        )
        _executor.submit(self.run_job, job["job_id"], owner_id)
        return job

    def run_job(self, job_id: str, owner_id: str) -> None:
        job = self.repo.get_job(owner_id, job_id)
        if job is None or job["status"] not in {"queued", "failed"}:
            return
        self.repo.update_job(job_id, status="running")
        source = job["source"]
        try:
            stats = self.index(
                owner_id=owner_id,
                collection_id=job["collection_id"],
                conversion_ids=source.get("conversion_ids") or [],
                source_paths=source.get("source_paths") or [],
            )
            self.repo.update_job(job_id, status="completed", stats=stats)
        except IndexingError as exc:
            self.repo.update_job(
                job_id, status="failed", error_code=exc.code, error_message=exc.message
            )
        except Exception as exc:  # noqa: BLE001 - job failure is recorded, not hidden
            self.repo.update_job(
                job_id, status="failed", error_code="INDEX_FAILED", error_message=str(exc)
            )

    def index(
        self,
        *,
        owner_id: str,
        collection_id: str,
        conversion_ids: list[str],
        source_paths: list[str],
    ) -> dict[str, Any]:
        if not conversion_ids and not source_paths:
            raise IndexingError(
                "NO_SOURCES", "Selecione pelo menos uma conversão ou um arquivo de estudo."
            )
        documents: list[dict[str, Any]] = []
        total_chunks = 0

        for conversion_id in conversion_ids:
            result = self._index_conversion(owner_id, collection_id, conversion_id)
            documents.append(result)
            total_chunks += result["chunks"]

        for source_path in source_paths:
            result = self._index_study_file(owner_id, collection_id, source_path)
            documents.append(result)
            total_chunks += result["chunks"]

        failures = [item for item in documents if item["status"] != "indexed"]
        return {
            "documents": documents,
            "chunks": total_chunks,
            "failed": len(failures),
            "indexed_at": datetime.now(UTC).isoformat(),
        }

    # -- conversion --------------------------------------------------------
    def _index_conversion(self, owner_id: str, collection_id: str, conversion_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            conversion = session.get(PublicConversionRow, conversion_id)
            if conversion is None or conversion.deleted_at is not None:
                raise IndexingError("CONVERSION_NOT_FOUND", f"Conversão {conversion_id} não encontrada.")
            if conversion.owner_id != owner_id:
                raise IndexingError("FORBIDDEN", "A conversão pertence a outro usuário.")
            document = session.get(PublicDocumentRow, conversion.document_id)
            run_id = conversion.active_run_id
            filename = document.filename if document else None
        finally:
            session.close()

        if not run_id:
            return {
                "document_id": conversion_id,
                "status": "skipped",
                "reason": "A conversão ainda não possui execução.",
                "chunks": 0,
            }
        payload = self.conversions.text_plan(run_id)
        if payload is None:
            return {
                "document_id": conversion_id,
                "run_id": run_id,
                "status": "skipped",
                "reason": "Plano textual não disponível (conversão anterior ao upgrade).",
                "chunks": 0,
            }

        from exports.text_plan import plan_from_payload

        plan = plan_from_payload(payload)
        drafts = chunk_text_plan(
            plan,
            owner_id=owner_id,
            collection_id=collection_id,
            document_id=conversion.document_id,
            run_id=run_id,
        )
        for draft in drafts:
            draft.metadata.update(
                {
                    "document_name": filename,
                    "run_id": run_id,
                    "conversion_id": conversion_id,
                    "source_type": payload.get("source_type") or "pdf",
                }
            )
        count = self.repo.replace_document_chunks(
            owner_id=owner_id,
            collection_id=collection_id,
            document_id=conversion.document_id,
            drafts=drafts,
        )
        return {
            "document_id": conversion.document_id,
            "conversion_id": conversion_id,
            "run_id": run_id,
            "name": filename,
            "status": "indexed",
            "chunks": count,
        }

    def index_authorized(
        self,
        *,
        owner_id: str,
        collection_id: str,
        conversion_id: str,
        document_id: str,
        run_id: str,
        filename: str | None,
    ) -> dict[str, Any]:
        """Index from gateway-verified references (full mode).

        In full mode the conversion lives in the gateway's Prisma database,
        not in ``public_conversions``: the gateway resolves ownership and the
        ``conversion → document/run`` links, then hands the authorized refs to
        this method. This endpoint never trusts free ``conversion_id`` input
        from a public client.
        """
        if not run_id:
            return {
                "document_id": document_id,
                "conversion_id": conversion_id,
                "status": "skipped",
                "reason": "A conversão ainda não possui execução.",
                "chunks": 0,
            }
        payload = self.conversions.text_plan(run_id)
        if payload is None:
            return {
                "document_id": document_id,
                "conversion_id": conversion_id,
                "run_id": run_id,
                "status": "skipped",
                "reason": "Plano textual não disponível (conversão anterior ao upgrade).",
                "chunks": 0,
            }

        from exports.text_plan import plan_from_payload

        plan = plan_from_payload(payload)
        drafts = chunk_text_plan(
            plan,
            owner_id=owner_id,
            collection_id=collection_id,
            document_id=document_id,
            run_id=run_id,
        )
        for draft in drafts:
            draft.metadata.update(
                {
                    "document_name": filename,
                    "run_id": run_id,
                    "conversion_id": conversion_id,
                    "source_type": payload.get("source_type") or "pdf",
                }
            )
        count = self.repo.replace_document_chunks(
            owner_id=owner_id,
            collection_id=collection_id,
            document_id=document_id,
            drafts=drafts,
        )
        return {
            "document_id": document_id,
            "conversion_id": conversion_id,
            "run_id": run_id,
            "name": filename,
            "status": "indexed",
            "chunks": count,
        }

    # -- study -------------------------------------------------------------
    def _index_study_file(self, owner_id: str, collection_id: str, source_path: str) -> dict[str, Any]:
        path = (STUDY_ROOT / source_path).resolve() if not Path(source_path).is_absolute() else Path(source_path).resolve()
        try:
            path.relative_to(STUDY_ROOT.resolve())
        except ValueError as exc:
            raise IndexingError(
                "PATH_NOT_ALLOWED",
                "Somente arquivos dentro de study/ podem ser indexados por caminho.",
            ) from exc
        if not path.exists() or not path.is_file():
            raise IndexingError("FILE_NOT_FOUND", f"Arquivo não encontrado: {source_path}")
        if path.suffix.lower() not in STUDY_EXTENSIONS:
            raise IndexingError(
                "UNSUPPORTED_FILE",
                f"Extensão não indexável: {path.suffix}. Permitidas: "
                + ", ".join(sorted(STUDY_EXTENSIONS)),
            )
        if path.stat().st_size > MAX_STUDY_BYTES:
            raise IndexingError("FILE_TOO_LARGE", "O arquivo de estudo excede 4 MiB.")
        relative = path.relative_to(STUDY_ROOT.resolve()).as_posix()
        document_id = "study_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
        text = path.read_text(encoding="utf-8", errors="replace")
        drafts = chunk_study_text(
            text,
            owner_id=owner_id,
            collection_id=collection_id,
            document_id=document_id,
            source_path=relative,
        )
        count = self.repo.replace_document_chunks(
            owner_id=owner_id,
            collection_id=collection_id,
            document_id=document_id,
            drafts=drafts,
        )
        return {
            "document_id": document_id,
            "name": relative,
            "status": "indexed",
            "chunks": count,
        }


__all__ = ["KnowledgeIndexer", "IndexingError", "STUDY_ROOT"]
