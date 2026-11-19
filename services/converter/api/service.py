"""Application service for the standalone public API.

It mirrors the NestJS controllers one-to-one so the contract does not change
with the runtime mode.  All rules live here: ownership, idempotency, state
transitions, document storage and job dispatch.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from api.deps import ApiError
from api.serialization import iso_utc
from config import settings
from exports.base import ExportError, ExportOptions
from exports.registry import run_export
from exports.text_plan import plan_from_payload
from ingestion.registry import probe_input_bytes
from jobqueue import get_queue
from models.db import (
    IdempotencyKeyRow,
    PublicConversionRow,
    PublicDocumentRow,
    PublicEventRow,
    Run,
    get_session_factory,
)
from models.document import BBox, DocumentIR, TableStructure
from repositories.conversion_repository import ConversionRepository
from storage import get_storage
from version import ENGINE_VERSION

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}

_UNSAFE = re.compile(r"[^\w.\- ()]+", re.UNICODE)


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_filename(name: str | None, *, default_ext: str | None = None) -> str:
    base = (name or "documento").replace("\\", "/").split("/")[-1]
    base = _UNSAFE.sub("_", base).strip(" .") or "documento"
    if "." not in base:
        base = f"{base}.{(default_ext or 'pdf').lower()}"
    return base[:200]


def _extension_for(probe) -> str:
    if probe.kind == "spreadsheet":
        return ".csv" if (probe.format or "").upper() == "CSV" else ".xlsx"
    if probe.kind == "pdf":
        return ".pdf"
    fmt = (probe.format or "img").lower()
    return {
        "jpeg": ".jpg",
        "jpeg2000": ".jp2",
        "tiff": ".tif",
        "pnm": ".pnm",
        "ico": ".ico",
    }.get(fmt, f".{fmt}")


def _request_hash(digest: str, options: dict[str, Any]) -> str:
    payload = json.dumps(
        {"sha256": digest, "options": options}, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


#: Keys that used to cap the document. Their presence is always rejected: the
#: default flow has no page ceiling and a request must not reintroduce one.
_CEILING_KEYS = ("max_pages", "maxPages", "max_pages_per_document", "page_limit")

#: Legacy flat aliases still accepted from older standalone clients.
_LEGACY_KEYS = frozenset(
    {
        "target_formats",
        "ocr",
        "ocr_enabled",
        "dpi",
        "text_newline",
        "page_separator",
        "include_table_text",
        "csv_profile",
        "csv_delimiter",
        "csv_table_ids",
        "csv_formula_policy",
        "csv_value_mode",
    }
)


def _from_legacy_options(raw: dict[str, Any]) -> dict[str, Any]:
    """Rewrites the pre-v2 flat shape into the nested contract (mirrors TS)."""
    nested: dict[str, Any] = {}
    if "target_formats" in raw:
        nested["target_formats"] = raw["target_formats"]

    ocr: dict[str, Any] = {}
    if isinstance(raw.get("ocr"), bool):
        ocr["enabled"] = raw["ocr"]
    if isinstance(raw.get("ocr_enabled"), bool):
        ocr["enabled"] = raw["ocr_enabled"]
    if isinstance(raw.get("ocr"), dict):
        ocr.update(raw["ocr"])
    if raw.get("dpi") is not None:
        ocr["dpi"] = raw["dpi"]
    if ocr:
        nested["ocr"] = ocr

    text: dict[str, Any] = {}
    if raw.get("text_newline") is not None:
        text["newline"] = raw["text_newline"]
    if raw.get("page_separator") is not None:
        text["page_separator"] = raw["page_separator"]
    if isinstance(raw.get("include_table_text"), bool):
        text["include_table_text"] = raw["include_table_text"]
    if isinstance(raw.get("text"), dict):
        text.update(raw["text"])
    if text:
        nested["text"] = text

    csv: dict[str, Any] = {}
    if raw.get("csv_profile") is not None:
        csv["profile"] = raw["csv_profile"]
    if raw.get("csv_delimiter") is not None:
        csv["delimiter"] = raw["csv_delimiter"]
    if raw.get("csv_table_ids") is not None:
        csv["table_ids"] = raw["csv_table_ids"]
    if raw.get("csv_formula_policy") is not None:
        csv["formula_policy"] = raw["csv_formula_policy"]
    if raw.get("csv_value_mode") is not None:
        csv["value_mode"] = raw["csv_value_mode"]
    if isinstance(raw.get("csv"), dict):
        csv.update(raw["csv"])
    if csv:
        nested["csv"] = csv

    # v3 section travels through the legacy rewrite untouched.
    if isinstance(raw.get("spreadsheet"), dict):
        nested["spreadsheet"] = raw["spreadsheet"]

    return nested


def _normalize_options(
    raw: dict[str, Any] | None,
    kind: str,
    *,
    allow_stamped: bool = False,
) -> dict[str, Any]:
    """Normalize options; ``allow_stamped`` re-accepts stored (server-stamped)
    payloads whose ``spreadsheet.reader_version`` was filled by this server.
    Client input never carries the stamp — that stays a rejection."""
    raw = dict(raw or {})

    for key in _CEILING_KEYS:
        if raw.get(key) is not None:
            raise ApiError(
                400,
                "INVALID_OPTION",
                "O limite de páginas por requisição não é suportado: o documento "
                "é processado por inteiro.",
                details={"option": key},
            )

    declared_version = raw.get("schema_version")
    if declared_version is not None and declared_version not in (2, 3):
        raise ApiError(
            400,
            "INVALID_OPTION",
            "schema_version aceita 2 (legado) ou 3 (entrada de planilha).",
            details={"schema_version": declared_version},
        )

    # v3 is opt-in by content: a spreadsheet input or an explicit spreadsheet
    # section upgrades the payload; anything else keeps the legacy v2 shape
    # byte-for-byte so existing fingerprints stay stable.
    is_v3 = kind == "spreadsheet" or isinstance(raw.get("spreadsheet"), dict)

    unknown = (
        set(raw)
        - {"schema_version", "target_formats", "ocr", "text", "csv", "spreadsheet"}
        - _LEGACY_KEYS
        - set(_CEILING_KEYS)
    )
    if unknown:
        raise ApiError(
            400,
            "INVALID_OPTION",
            f"Opções desconhecidas: {sorted(unknown)}.",
            details={
                "allowed": ["schema_version", "target_formats", "ocr", "text", "csv", "spreadsheet"],
            },
        )

    raw = _from_legacy_options(raw)

    # Images have no spreadsheet writer, so the default must already be TXT
    # (an explicit XLSX/CSV is still an error below).
    default_targets = ["txt"] if kind == "image" else settings.target_formats()
    target = raw.get("target_formats") or default_targets
    if isinstance(target, str):
        target = [item.strip() for item in target.split(",") if item.strip()]
    if not isinstance(target, list) or not target:
        raise ApiError(400, "INVALID_OPTION", "target_formats deve ser uma lista não vazia.")
    allowed_targets = {"xlsx", "txt", "csv"}
    invalid = [item for item in target if str(item).lower() not in allowed_targets]
    if invalid:
        raise ApiError(
            400,
            "UNSUPPORTED_FORMAT",
            f"Formatos de saída não suportados: {invalid}.",
            details={"allowed": sorted(allowed_targets)},
        )
    target = [str(item).lower() for item in target]
    if kind == "image" and any(item != "txt" for item in target):
        raise ApiError(
            400,
            "UNSUPPORTED_TARGET",
            "Imagens produzem TXT nesta versão. Escolha target_formats=['txt'].",
            details={"target_formats": target, "supported": ["txt"]},
        )

    ocr = raw.get("ocr") or {}
    text = raw.get("text") or {}
    csv = raw.get("csv") or {}
    if not isinstance(ocr, dict) or not isinstance(text, dict) or not isinstance(csv, dict):
        raise ApiError(400, "INVALID_OPTION", "ocr, text e csv devem ser objetos.")

    allowed_ocr = {"enabled", "languages", "dpi", "strategy"}
    allowed_text = {"newline", "page_separator", "include_table_text"}
    allowed_csv = {"profile", "delimiter", "formula_policy", "table_ids", "value_mode"}
    for section, allowed in (("ocr", allowed_ocr), ("text", allowed_text), ("csv", allowed_csv)):
        extra = set(raw.get(section) or {}) - allowed
        if extra:
            raise ApiError(
                400,
                "INVALID_OPTION",
                f"Opções desconhecidas em {section}: {sorted(extra)}.",
                details={"allowed": sorted(allowed)},
            )

    if text.get("newline") not in {None, "lf", "crlf"}:
        raise ApiError(400, "INVALID_OPTION", "text.newline deve ser 'lf' ou 'crlf'.")
    if text.get("page_separator") not in {None, "form_feed", "marker", "none"}:
        raise ApiError(400, "INVALID_OPTION", "text.page_separator inválido.")
    if csv.get("profile") not in {None, "interoperable", "excel_br"}:
        raise ApiError(400, "INVALID_OPTION", "csv.profile deve ser 'interoperable' ou 'excel_br'.")
    if csv.get("formula_policy") not in {None, "safe", "raw"}:
        raise ApiError(400, "INVALID_OPTION", "csv.formula_policy deve ser 'safe' ou 'raw'.")
    if csv.get("value_mode") not in {None, "original", "canonical"}:
        raise ApiError(400, "INVALID_OPTION", "csv.value_mode deve ser 'original' ou 'canonical'.")

    normalized = {
        "schema_version": 2,
        "target_formats": target,
        "ocr": {
            "enabled": bool(ocr.get("enabled", settings.converter_ocr_enabled)),
            "languages": ocr.get("languages") or ["por", "eng"],
            "dpi": ocr.get("dpi"),
            "strategy": ocr.get("strategy") or "auto",
        },
        "text": {
            "newline": text.get("newline") or "lf",
            "page_separator": text.get("page_separator") or "form_feed",
            "include_table_text": bool(text.get("include_table_text", True)),
        },
        "csv": {
            "profile": csv.get("profile") or "interoperable",
            "delimiter": csv.get("delimiter"),
            "formula_policy": csv.get("formula_policy") or "safe",
            "table_ids": csv.get("table_ids"),
            "value_mode": csv.get("value_mode") or "original",
        },
    }

    if not is_v3:
        return normalized

    normalized["schema_version"] = 3
    normalized["spreadsheet"] = _normalize_spreadsheet_options(
        raw.get("spreadsheet") or {}, allow_stamped=allow_stamped
    )
    return normalized


# `reader_version` is stamped by the server, never accepted from the client:
# it must reflect the engine that actually reads the file, not a client claim.
SPREADSHEET_READER_VERSION = "spreadsheet-reader-v1"
_ALLOWED_SPREADSHEET = {"sheet_names", "header_row", "csv_encoding", "csv_delimiter", "formula_mode"}


def _normalize_spreadsheet_options(
    section: dict[str, Any], *, allow_stamped: bool = False
) -> dict[str, Any]:
    allowed_keys = set(_ALLOWED_SPREADSHEET)
    if allow_stamped:
        # Stored payloads carry the server's own stamp; re-canonicalize it.
        allowed_keys = allowed_keys | {"reader_version"}
    extra = set(section) - allowed_keys
    if extra:
        raise ApiError(
            400,
            "INVALID_OPTION",
            f"Opções desconhecidas em spreadsheet: {sorted(extra)}.",
            details={"allowed": sorted(_ALLOWED_SPREADSHEET)},
        )

    sheet_names = section.get("sheet_names")
    if sheet_names is not None:
        if (
            not isinstance(sheet_names, list)
            or not sheet_names
            or len(sheet_names) > 1024
            or any(not isinstance(name, str) or not name or len(name) > 255 for name in sheet_names)
        ):
            raise ApiError(
                400,
                "INVALID_OPTION",
                "spreadsheet.sheet_names deve ser uma lista de 1 a 1024 nomes de abas.",
                details={"sheet_names": sheet_names},
            )
        sheet_names = list(sheet_names)

    header_row = section.get("header_row")
    if header_row is not None and not isinstance(header_row, bool):
        raise ApiError(400, "INVALID_OPTION", "spreadsheet.header_row deve ser booleano ou nulo.")

    csv_encoding = section.get("csv_encoding")
    if csv_encoding is not None:
        if not isinstance(csv_encoding, str) or not (2 <= len(csv_encoding) <= 40):
            raise ApiError(
                400,
                "INVALID_OPTION",
                "spreadsheet.csv_encoding deve ser um nome de encoding (2–40 caracteres).",
                details={"csv_encoding": csv_encoding},
            )
        try:
            import codecs

            codecs.lookup(csv_encoding)
        except LookupError as exc:
            raise ApiError(
                400,
                "INVALID_OPTION",
                f"Encoding desconhecido: {csv_encoding}.",
                details={"csv_encoding": csv_encoding},
            ) from exc

    csv_delimiter = section.get("csv_delimiter")
    if csv_delimiter is not None:
        if not isinstance(csv_delimiter, str) or len(csv_delimiter) != 1 or ord(csv_delimiter) > 127:
            raise ApiError(
                400,
                "INVALID_OPTION",
                "spreadsheet.csv_delimiter deve ser um único caractere ASCII.",
                details={"csv_delimiter": csv_delimiter},
            )

    formula_mode = section.get("formula_mode") or "cached_or_text"
    if formula_mode != "cached_or_text":
        raise ApiError(
            400,
            "INVALID_OPTION",
            "spreadsheet.formula_mode aceita apenas 'cached_or_text': fórmulas nunca são "
            "recalculadas; sem cache em Texto, viram texto com aviso.",
            details={"formula_mode": formula_mode},
        )

    return {
        "sheet_names": sheet_names,
        "header_row": header_row,
        "csv_encoding": csv_encoding,
        "csv_delimiter": csv_delimiter,
        "formula_mode": formula_mode,
        "reader_version": SPREADSHEET_READER_VERSION,
    }


def _estimate_pages(data: bytes) -> int | None:
    """Page count from the PDF reader.

    The previous regex over the raw bytes missed pages stored in object
    streams (returned None, so the limit was skipped) and over-counted
    duplicated objects. The parser is authoritative; the regex is only a
    fallback for files pypdf cannot open.
    """
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data), strict=False).pages) or None
    except Exception:  # noqa: BLE001
        pass
    try:
        count = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return count or None
    except Exception:  # noqa: BLE001
        return None


class ConversionService:
    def __init__(self) -> None:
        self.repo = ConversionRepository()
        self.queue = get_queue()

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    def create(
        self,
        *,
        data: bytes,
        filename: str,
        owner_id: str,
        idempotency_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not data:
            raise ApiError(400, "INVALID_FILE", "O arquivo enviado está vazio.")
        if len(data) > settings.max_bytes():
            raise ApiError(
                400,
                "FILE_TOO_LARGE",
                f"O arquivo excede o limite de {settings.max_bytes() // (1024 * 1024)} MiB.",
            )
        probe = probe_input_bytes(data, filename)
        if probe.kind == "unknown":
            raise ApiError(
                400,
                "INVALID_FILE",
                "O arquivo enviado não é um PDF, imagem ou planilha reconhecida.",
            )
        if probe.kind == "spreadsheet" and not probe.supported:
            raise ApiError(
                400,
                "INVALID_FILE",
                probe.reason or "Planilha inválida.",
                details={"format": probe.format},
            )
        if probe.kind == "image" and not probe.supported:
            raise ApiError(
                415,
                "CODEC_UNAVAILABLE",
                f"O codec para {probe.format} não está instalado nesta máquina.",
                details={"format": probe.format},
            )

        normalized = _normalize_options(options, probe.kind)
        filename = _safe_filename(filename, default_ext=probe.format)
        digest = hashlib.sha256(data).hexdigest()

        storage = get_storage()
        extension = _extension_for(probe)
        key = f"documents/{owner_id}/{digest}{extension}"
        if not storage.exists(key):
            storage.put_bytes(key, data, probe.mime_type or "application/octet-stream")

        # The page count is recorded for progress/budget purposes only.
        # There is no ceiling: a document of any length is accepted.
        pages = _estimate_pages(data) if probe.kind == "pdf" else None

        return self.create_from_stored(
            owner_id=owner_id,
            storage_key=key,
            sha256=digest,
            size_bytes=len(data),
            filename=filename,
            mime_type=probe.mime_type or "application/octet-stream",
            normalized=normalized,
            page_count=pages,
            idempotency_key=idempotency_key,
        )

    def create_from_stored(
        self,
        *,
        owner_id: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
        filename: str,
        mime_type: str,
        normalized: dict[str, Any],
        page_count: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Creates the conversion for an object that is already in storage.

        Used by the resumable-upload path and by the legacy multipart endpoint,
        so both produce exactly the same rows, run and queue message.
        """
        request_hash = _request_hash(sha256, normalized)
        session = get_session_factory()()
        try:
            if idempotency_key:
                existing = self._idempotent(session, owner_id, idempotency_key, request_hash)
                if existing is not None:
                    # A replay is not a new admission: it must never fail
                    # because the queue filled up in the meantime.
                    return existing

            document = PublicDocumentRow(
                id=f"doc_{uuid.uuid4().hex}",
                filename=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                sha256=sha256,
                storage_key=storage_key,
                page_count=page_count,
                owner_id=owner_id,
            )
            conversion = PublicConversionRow(
                id=f"conv_{uuid.uuid4().hex}",
                document_id=document.id,
                owner_id=owner_id,
                status="queued",
                quality="pending",
                progress=0.0,
                stage="receive",
                active_run_id=None,
                options_json=normalized,
            )
            session.add(document)
            session.flush()
            session.add(conversion)
            session.flush()
            conversion_id = conversion.id

            run_id, payload = self._stage_run(
                session,
                conversion,
                storage_key=storage_key,
                filename=filename,
                options=normalized,
            )

            # The idempotency row is part of the same transaction as the
            # conversion: a lost race rolls everything back, so the loser never
            # leaves a queued run behind.
            if idempotency_key:
                session.add(
                    IdempotencyKeyRow(
                        owner_id=owner_id,
                        key=idempotency_key[:128],
                        conversion_id=conversion_id,
                        request_hash=request_hash,
                    )
                )
            try:
                session.commit()
            except IntegrityError:
                # Two requests with the same key raced: the unique constraint
                # picked a winner and this request replays that conversion
                # instead of creating a duplicate or failing with 500.
                session.rollback()
                if idempotency_key:
                    existing = self._idempotent(session, owner_id, idempotency_key, request_hash)
                    if existing is not None:
                        return existing
                raise
            except Exception:
                session.rollback()
                raise

            # Only the request that committed the rows admits the job.
            self.queue.submit(run_id, payload)
            return {
                "conversion_id": conversion_id,
                "status": "queued",
                "message": "Conversão recebida e enfileirada.",
                "links": _links(conversion_id),
            }
        finally:
            session.close()

    def _idempotent(self, session, owner_id: str, key: str, request_hash: str) -> dict[str, Any] | None:
        row = session.execute(
            select(IdempotencyKeyRow).where(
                IdempotencyKeyRow.owner_id == owner_id, IdempotencyKeyRow.key == key[:128]
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.request_hash and row.request_hash != request_hash:
            # Reusing the same key with a different file or different options
            # would silently return the wrong artifact.
            raise ApiError(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "A chave de idempotência já foi usada para outro arquivo ou conjunto de opções.",
                details={"idempotency_key": key[:128]},
            )
        conversion = session.get(PublicConversionRow, row.conversion_id)
        if conversion is None:
            return None
        run = session.get(Run, conversion.active_run_id) if conversion.active_run_id else None
        return {
            "conversion_id": conversion.id,
            "status": run.status if run else conversion.status,
            "message": "Requisição idempotente reaproveitada.",
            "links": _links(conversion.id),
        }

    def _stage_run(
        self,
        session,
        conversion: PublicConversionRow,
        *,
        storage_key: str,
        filename: str,
        options: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Adds the run and its first event to the session. Does not commit.

        Returns the run id and the queue payload. The caller submits the job
        only after its transaction has committed, so a rolled-back admission
        never reaches a worker.
        """
        attempt = 1
        if conversion.active_run_id:
            previous = session.get(Run, conversion.active_run_id)
            attempt = (previous.attempt + 1) if previous else 1
        run_id = f"run_{uuid.uuid4().hex}"
        effective_options = options or (conversion.options_json or {})
        run = Run(
            id=run_id,
            conversion_id=conversion.id,
            attempt=attempt,
            status="queued",
            stage="receive",
            progress=0.0,
            quality="pending",
            engine_version=ENGINE_VERSION,
            config_json=effective_options,
            started_at=_now(),
        )
        session.add(run)
        # The run row must exist before the conversion points at it.
        session.flush()
        conversion.active_run_id = run_id
        conversion.status = "queued"
        conversion.stage = "receive"
        conversion.progress = 0.0
        conversion.error_code = None
        conversion.error_message = None
        conversion.updated_at = _now()
        session.add(
            PublicEventRow(
                conversion_id=conversion.id,
                sequence=_next_sequence(session, conversion.id),
                type="status",
                status="queued",
                stage="receive",
                progress=0.0,
                progress_indeterminate=False,
                message="Conversão enfileirada.",
                payload_json={"attempt": attempt},
            )
        )
        payload = {
            "conversion_id": conversion.id,
            "storage_key": storage_key,
            "filename": filename,
            "options": effective_options,
        }
        return run_id, payload

    # ------------------------------------------------------------------ #
    # Read
    # ------------------------------------------------------------------ #
    def list(
        self,
        *,
        owner_id: str,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            query = (
                select(PublicConversionRow, PublicDocumentRow)
                .join(PublicDocumentRow, PublicConversionRow.document_id == PublicDocumentRow.id)
                .where(
                    PublicConversionRow.owner_id == owner_id,
                    PublicConversionRow.deleted_at.is_(None),
                )
            )
            if status:
                query = query.where(PublicConversionRow.status == status)
            if q:
                query = query.where(PublicDocumentRow.filename.ilike(f"%{q}%"))
            total = session.execute(
                select(func.count()).select_from(query.subquery())
            ).scalar() or 0
            rows = session.execute(
                query.order_by(PublicConversionRow.created_at.desc())
                .offset(max(0, (page - 1) * page_size))
                .limit(page_size)
            ).all()
            items = []
            for conversion, document in rows:
                stats = conversion.stats_json or {}
                items.append(
                    {
                        "conversion_id": conversion.id,
                        "status": conversion.status,
                        "progress": conversion.progress,
                        "quality": conversion.quality,
                        "filename": document.filename,
                        "page_count": document.page_count,
                        "size_bytes": document.size_bytes,
                        "tables": stats.get("tables"),
                        "cells": stats.get("cells"),
                        "created_at": iso_utc(conversion.created_at),
                        "updated_at": iso_utc(conversion.updated_at),
                    }
                )
            return {"items": items, "total": total, "page": page, "page_size": page_size}
        finally:
            session.close()

    def get(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            document = session.get(PublicDocumentRow, conversion.document_id)
            run = session.get(Run, conversion.active_run_id) if conversion.active_run_id else None
            return _detail(conversion, document, run)
        finally:
            session.close()

    def result(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id, require_terminal=True)
        summary = self.repo.result_summary(run)
        if summary is None:
            raise ApiError(404, "NOT_FOUND", "Resultado não disponível.")
        return summary

    def result_overview(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        """Compact result payload: safe for a million-cell workbook."""
        run_id = self._active_run(owner_id=owner_id, conversion_id=conversion_id)
        overview = self.repo.result_overview(run_id)
        if overview is None:
            raise ApiError(404, "NOT_FOUND", "Resultado não encontrado.")
        return overview

    def sheets(
        self,
        *,
        owner_id: str,
        conversion_id: str,
        cursor: str | None = None,
        limit: int = 50,
        q: str | None = None,
    ) -> dict[str, Any]:
        run_id = self._active_run(owner_id=owner_id, conversion_id=conversion_id)
        page = self.repo.sheets_page(run_id, cursor=cursor, limit=limit, q=q)
        if page is None:
            raise ApiError(404, "NOT_FOUND", "Conversão sem abas geradas.")
        return page

    def sheet_pages(
        self,
        *,
        owner_id: str,
        conversion_id: str,
        sheet_id: str,
        cursor: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        run_id = self._active_run(owner_id=owner_id, conversion_id=conversion_id)
        page = self.repo.sheet_source_pages(run_id, sheet_id, cursor=cursor, limit=limit)
        if page is None:
            raise ApiError(404, "NOT_FOUND", "Aba não encontrada.")
        return page

    def preview(
        self, *, owner_id: str, conversion_id: str, sheet: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id, require_terminal=True)
        preview = self.repo.preview(run, sheet, offset, limit)
        if preview is None:
            raise ApiError(404, "NOT_FOUND", "Prévia não disponível.")
        return preview

    def download_stream(
        self, *, owner_id: str, conversion_id: str
    ) -> tuple[Any, str, str]:
        """Same checks as `download`, but returns a file-like object."""
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            if conversion.status in {"failed", "cancelled"}:
                raise ApiError(
                    409,
                    "NOT_READY",
                    "A conversão não foi concluída com sucesso.",
                    details={"status": conversion.status},
                )
            document = session.get(PublicDocumentRow, conversion.document_id)
            run_id = conversion.active_run_id
        finally:
            session.close()
        if not run_id:
            raise ApiError(409, "NOT_READY", "A conversão ainda não foi executada.")
        self._assert_publishable(owner_id, conversion_id, run_id)
        if self.repo.xlsx_key(run_id) is None:
            raise ApiError(409, "NOT_READY", "O arquivo XLSX ainda não está disponível.")
        stream = self.repo.xlsx_stream(run_id)
        if stream is None:
            raise ApiError(409, "NOT_READY", "O arquivo XLSX ainda não está disponível.")
        name = (document.filename if document else "conversao").rsplit(".", 1)[0]
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return stream, f"{name}.xlsx", media

    def download(self, *, owner_id: str, conversion_id: str) -> tuple[bytes, str, str]:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            if conversion.status in {"failed", "cancelled"}:
                raise ApiError(
                    409,
                    "NOT_READY",
                    "A conversão não foi concluída com sucesso.",
                    details={"status": conversion.status},
                )
            document = session.get(PublicDocumentRow, conversion.document_id)
            run_id = conversion.active_run_id
        finally:
            session.close()
        if not run_id:
            raise ApiError(409, "NOT_READY", "A conversão ainda não foi executada.")
        self._assert_publishable(owner_id, conversion_id, run_id)
        data = self.repo.xlsx_bytes(run_id)
        if data is None:
            raise ApiError(409, "NOT_READY", "O arquivo XLSX ainda não está disponível.")
        name = (document.filename if document else "conversao").rsplit(".", 1)[0]
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return data, f"{name}.xlsx", media

    # ------------------------------------------------------------------ #
    # Exports (TXT/CSV)
    # ------------------------------------------------------------------ #
    def exports(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        run_id = self._active_run(owner_id, conversion_id, require_terminal=True)
        items = self.repo.exports(run_id)
        for item in items:
            item["download_url"] = (
                f"/api/v1/conversions/{conversion_id}/exports/{item['export_id']}/download"
                if item["publication_status"] == "approved"
                else None
            )
        return {"items": items, "total": len(items)}

    def download_export(
        self, *, owner_id: str, conversion_id: str, export_id: str
    ) -> tuple[bytes, str, str]:
        run_id = self._active_run(owner_id, conversion_id, require_terminal=True)
        self._assert_publishable(owner_id, conversion_id, run_id)
        meta = self.repo.export_by_id(run_id, export_id)
        if meta is None:
            raise ApiError(404, "NOT_FOUND", "Exportação não encontrada.")
        if meta["publication_status"] != "approved":
            raise ApiError(
                409,
                "ARTIFACT_QUARANTINED",
                "Este artefato foi reprovado na auditoria e não pode ser baixado.",
                details={"publication_status": meta["publication_status"]},
            )
        try:
            data = get_storage().get_bytes(meta["storage_key"])
        except Exception as exc:  # noqa: BLE001
            raise ApiError(404, "NOT_FOUND", "O artefato não está mais disponível.") from exc
        if meta.get("sha256") and hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise ApiError(
                409,
                "ARTIFACT_HASH_MISMATCH",
                "O arquivo não corresponde ao hash registrado na auditoria.",
            )
        return data, meta["filename"], meta["mime_type"]

    def create_export(
        self,
        *,
        owner_id: str,
        conversion_id: str,
        fmt: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Produce an artifact from stored IR data — never re-runs OCR."""
        run_id = self._active_run(owner_id, conversion_id, require_terminal=True)
        self._assert_publishable(owner_id, conversion_id, run_id)
        return self.create_export_for_run(run_id, fmt, options)

    def create_export_for_run(
        self, run_id: str, fmt: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        fmt = (fmt or "").lower()
        if fmt not in {"txt", "csv"}:
            raise ApiError(
                400,
                "UNSUPPORTED_FORMAT",
                "Formato de exportação deve ser 'txt' ou 'csv'.",
                details={"available": ["txt", "csv"]},
            )
        raw = dict(options or {})
        export_options = ExportOptions(
            format=fmt,
            encoding=str(raw.get("encoding") or "utf-8"),
            newline=str(raw.get("newline") or "lf"),
            page_separator=str(raw.get("page_separator") or "form_feed"),
            csv_profile=str(raw.get("csv_profile") or raw.get("profile") or "interoperable"),
            csv_delimiter=raw.get("csv_delimiter") or raw.get("delimiter"),
            formula_policy=str(raw.get("formula_policy") or "safe"),
            table_ids=raw.get("table_ids"),
            value_mode=str(raw.get("value_mode") or "original"),
        )
        try:
            if fmt == "txt":
                payload = self.repo.text_plan(run_id)
                if payload is None:
                    raise ApiError(
                        409,
                        "TEXT_PLAN_MISSING",
                        "O plano textual desta execução não está disponível.",
                    )
                export = run_export(
                    "txt", ir=None, text_plan=plan_from_payload(payload), options=export_options
                )
            else:
                tables = self.repo.tables(run_id)["items"]
                ir = DocumentIR(file_name="export.csv", sha256="", page_count=0)
                ir.tables = [
                    TableStructure(
                        table_id=item["table_id"],
                        page_number=item["page_number"],
                        bbox=BBox.empty(),
                        strategy=item.get("strategy") or "stored",
                        rows=item.get("rows") or [],
                        headers=item.get("headers") or [],
                    )
                    for item in tables
                ]
                export = run_export("csv", ir=ir, options=export_options)
        except ExportError as exc:
            raise ApiError(400, exc.code, exc.message, details=exc.details) from exc
        return self.repo.store_export(run_id, export, publication_status="approved")

    def text_page(
        self,
        *,
        owner_id: str,
        conversion_id: str,
        page: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        run_id = self._active_run(owner_id, conversion_id, require_terminal=True)
        self._assert_publishable(owner_id, conversion_id, run_id)
        return self.text_for_run(run_id, page=page, offset=offset, limit=limit)

    def text_for_run(
        self, run_id: str, *, page: int | None, offset: int, limit: int
    ) -> dict[str, Any]:
        payload = self.repo.text_plan(run_id)
        if payload is None:
            raise ApiError(404, "NOT_FOUND", "Texto não disponível para esta conversão.")
        plan = plan_from_payload(payload)
        pages = plan.pages
        if page is not None:
            pages = [item for item in pages if item.page_number == page]
            if not pages:
                raise ApiError(404, "NOT_FOUND", f"Página {page} não encontrada.")
        body = "\n".join(
            segment.text for item in pages for segment in item.segments
        )
        start = max(0, offset)
        end = start + max(1, min(limit, 200_000))
        return {
            "page": page,
            "offset": start,
            "limit": end - start,
            "chars": len(body),
            "text": body[start:end],
            "pages": [
                {
                    "page_number": item.page_number,
                    "status": item.status,
                    "chars": item.char_count,
                    "has_text": item.has_text,
                }
                for item in plan.pages
            ],
            "warnings": plan.warnings,
        }

    def _assert_publishable(self, owner_id: str, conversion_id: str, run_id: str) -> None:
        run = self.repo.get_run(run_id) or {}
        stats = run.get("stats") or {}
        status = stats.get("publication_status")
        if status == "quarantined":
            raise ApiError(
                409,
                "ARTIFACT_QUARANTINED",
                "O artefato foi reprovado por uma falha fatal de integridade e não pode ser baixado.",
                details={"publication_status": status},
            )

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #
    def audit(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        summary = self.repo.audit_summary(run)
        if summary is None:
            raise ApiError(404, "NOT_FOUND", "Auditoria não disponível.")
        return summary

    def audit_events(self, *, owner_id: str, conversion_id: str, **filters) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        return self.repo.audit_events(run, **filters)

    def audit_page(self, *, owner_id: str, conversion_id: str, page: int) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        data = self.repo.page_audit(run, page)
        if data is None:
            raise ApiError(404, "NOT_FOUND", f"Página {page} não encontrada nesta execução.")
        return data

    def pages(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        return self.repo.pages(run)

    def tables(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        return self.repo.tables(run)

    def audit_cells(self, *, owner_id: str, conversion_id: str, sheet: str | None, cell: str | None, limit: int) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        return self.repo.cells(run, sheet=sheet, cell=cell, limit=limit)

    def cell_evidence(self, *, owner_id: str, conversion_id: str, cell_id: str) -> dict[str, Any]:
        """Provenance of a single cell, addressed as ``Sheet!C2`` or ``C2``."""
        run = self._active_run(owner_id, conversion_id)
        sheet, cell = _split_cell_id(cell_id)
        data = self.repo.cells(run, sheet=sheet, cell=cell, limit=1)
        if not data["items"]:
            raise ApiError(404, "NOT_FOUND", f"Célula {cell_id} não encontrada nesta execução.")
        return data["items"][0]

    def audit_issues(self, *, owner_id: str, conversion_id: str, severity: str | None) -> dict[str, Any]:
        run = self._active_run(owner_id, conversion_id)
        return self.repo.issues(run, severity)

    def audit_export(self, *, owner_id: str, conversion_id: str, fmt: str) -> tuple[bytes, str, str]:
        run = self._active_run(owner_id, conversion_id)
        return self.repo.export(run, fmt)

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def retry(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            if conversion.status not in TERMINAL:
                raise ApiError(409, "ALREADY_ACTIVE", "A conversão já está em andamento.")
            document = session.get(PublicDocumentRow, conversion.document_id)
            if document is None:
                raise ApiError(404, "NOT_FOUND", "Documento original não encontrado.")
            storage_key = document.storage_key
            filename = document.filename

            # Claim the terminal conversion atomically. Two retries that both
            # read the same terminal status race here: only one UPDATE matches
            # the WHERE clause, the other loses and is told the work is active.
            claimed = session.execute(
                update(PublicConversionRow)
                .where(
                    PublicConversionRow.id == conversion.id,
                    PublicConversionRow.status.in_(TERMINAL),
                )
                .values(status="queued")
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                session.rollback()
                raise ApiError(409, "ALREADY_ACTIVE", "A conversão já está em andamento.")
            session.refresh(conversion)

            run_id, payload = self._stage_run(
                session,
                conversion,
                storage_key=storage_key,
                filename=filename,
                options=conversion.options_json or {},
            )
            session.commit()
            conversion_ref = conversion.id
        finally:
            session.close()

        self.queue.submit(run_id, payload)
        return {
            "conversion_id": conversion_ref,
            "status": "queued",
            "message": "Nova execução criada.",
            "links": _links(conversion_ref),
        }

    def cancel(self, *, owner_id: str, conversion_id: str) -> dict[str, Any]:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            if conversion.status in TERMINAL:
                raise ApiError(409, "ALREADY_TERMINAL", "A conversão já foi finalizada.")
            run_id = conversion.active_run_id
        finally:
            session.close()
        if run_id:
            self.queue.cancel(run_id)
            self.repo.cancel_run(run_id)
        return {"conversion_id": conversion_id, "status": "cancelled", "message": "Cancelamento solicitado."}

    def delete(self, *, owner_id: str, conversion_id: str) -> None:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            conversion.deleted_at = _now()
            conversion.updated_at = _now()
            session.commit()
            run_id = conversion.active_run_id
        finally:
            session.close()
        if run_id:
            for key in (f"runs/{run_id}/output.xlsx", f"runs/{run_id}/audit.json.gz"):
                try:
                    get_storage().delete(key)
                except Exception:  # noqa: BLE001
                    pass

    def events(self, *, owner_id: str, conversion_id: str, since: int) -> list[dict[str, Any]]:
        self._active_or_any(owner_id, conversion_id)
        return self.repo.events_since(conversion_id, since)

    def status_of(self, *, owner_id: str, conversion_id: str) -> str:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            return conversion.status
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _load(self, session, owner_id: str, conversion_id: str) -> PublicConversionRow:
        conversion = session.get(PublicConversionRow, conversion_id)
        if conversion is None or conversion.deleted_at is not None:
            raise ApiError(404, "NOT_FOUND", "Conversão não encontrada.")
        if conversion.owner_id != owner_id:
            raise ApiError(403, "FORBIDDEN", "Você não tem acesso a esta conversão.")
        return conversion

    def _active_or_any(self, owner_id: str, conversion_id: str) -> PublicConversionRow:
        session = get_session_factory()()
        try:
            return self._load(session, owner_id, conversion_id)
        finally:
            session.close()

    def _active_run(self, owner_id: str, conversion_id: str, *, require_terminal: bool = False) -> str:
        session = get_session_factory()()
        try:
            conversion = self._load(session, owner_id, conversion_id)
            if require_terminal and conversion.status not in TERMINAL:
                raise ApiError(409, "NOT_READY", "A conversão ainda não foi concluída.")
            if not conversion.active_run_id:
                raise ApiError(409, "NOT_READY", "Nenhuma execução iniciada.")
            return conversion.active_run_id
        finally:
            session.close()


def _next_sequence(session, conversion_id: str) -> int:
    last = session.execute(
        select(func.max(PublicEventRow.sequence)).where(PublicEventRow.conversion_id == conversion_id)
    ).scalar()
    return int(last or 0) + 1


def _split_cell_id(cell_id: str) -> tuple[str | None, str]:
    """Split ``Sheet!C2`` into (sheet, cell); bare ``C2`` has no sheet."""
    raw = (cell_id or "").strip()
    if not raw:
        raise ApiError(400, "INVALID_CELL_ID", "Informe a célula no formato 'Aba!C2' ou 'C2'.")
    if "!" in raw:
        sheet, _, cell = raw.partition("!")
        if not sheet or not cell:
            raise ApiError(400, "INVALID_CELL_ID", "Informe a célula no formato 'Aba!C2'.")
        return sheet, cell
    return None, raw


def _links(conversion_id: str) -> dict[str, str]:
    base = f"/api/v1/conversions/{conversion_id}"
    return {
        "status": base,
        "events": f"{base}/events",
        "result": f"{base}/result",
        "preview": f"{base}/preview",
        "download": f"{base}/download",
        "audit": f"{base}/audit",
        "pages": f"{base}/pages",
        "tables": f"{base}/tables",
        "self": base,
    }


def _detail(conversion, document, run) -> dict[str, Any]:
    stats = run.stats_json if run and run.stats_json else (conversion.stats_json or {})
    validation = run.validation_json if run and run.validation_json else conversion.validation_json
    status = run.status if run else conversion.status
    stage = run.stage if run else conversion.stage
    progress = run.progress if run else conversion.progress
    indeterminate = bool(getattr(run, "progress_indeterminate", False)) if run else False
    return {
        "conversion_id": conversion.id,
        "status": status,
        "stage": stage,
        "progress": round(float(progress or 0.0), 4),
        "progress_indeterminate": indeterminate,
        "quality": run.quality if run else conversion.quality,
        "document": {
            "id": document.id,
            "filename": document.filename,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
            "sha256": document.sha256,
            "page_count": document.page_count,
            "created_at": iso_utc(document.created_at),
        },
        # A conversion can exist before its first run is recorded; the detail
        # is still meaningful without one.
        "run": (
            {
                "run_id": run.id,
                "attempt": run.attempt,
                "started_at": iso_utc(run.started_at),
                "finished_at": iso_utc(run.finished_at),
            }
            if run is not None
            else None
        ),
        "stats": stats or None,
        "validation": validation,
        "error": (
            {
                "code": run.error_code,
                "message": run.error_message,
                "detail": None,
                "retryable": run.error_code not in {"CANCELLED", "INVALID_FILE", "ENCRYPTED_PDF"},
            }
            if run and run.error_code
            else None
        ),
        "created_at": iso_utc(conversion.created_at),
        "updated_at": iso_utc(conversion.updated_at),
        "links": _links(conversion.id),
    }


__all__ = ["ConversionService"]
