"""Request/response schemas for the HTTP layer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AnonymousRequest(BaseModel):
    name: str | None = None


class EnqueueRequest(BaseModel):
    run_id: str
    conversion_id: str
    storage_key: str
    filename: str = "documento.pdf"
    options: dict[str, Any] = Field(default_factory=dict)


class CancelRequest(BaseModel):
    reason: str | None = None


class HookPayload(BaseModel):
    run_id: str | None = None
    conversion_id: str | None = None
    status: str | None = None
    stage: str | None = None
    progress: float | None = None
    quality: str | None = None
    message: str | None = None
    stats: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Conversion options v2 / exports
# --------------------------------------------------------------------------- #


class OcrOptions(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    languages: list[str] = Field(default_factory=lambda: ["por", "eng"])
    dpi: int | None = Field(default=None, ge=72, le=600)
    strategy: str = "auto"


class TextOptions(BaseModel):
    model_config = {"extra": "forbid"}

    newline: str = "lf"
    page_separator: str = "form_feed"
    include_table_text: bool = True


class CsvOptions(BaseModel):
    model_config = {"extra": "forbid"}

    profile: str = "interoperable"
    delimiter: str | None = None
    formula_policy: str = "safe"
    table_ids: list[str] | None = None
    value_mode: str = "original"


class ConversionOptionsV2(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: int = 2
    target_formats: list[str] = Field(default_factory=lambda: ["xlsx", "txt", "csv"])
    ocr: OcrOptions = Field(default_factory=OcrOptions)
    text: TextOptions = Field(default_factory=TextOptions)
    csv: CsvOptions = Field(default_factory=CsvOptions)


class ExportRequest(BaseModel):
    model_config = {"extra": "forbid"}

    format: str
    options: dict[str, Any] = Field(default_factory=dict)


class CollectionCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    scope: str = "documents"


class IndexRequest(BaseModel):
    model_config = {"extra": "forbid"}

    collection_id: str | None = None
    collection_name: str | None = None
    conversion_ids: list[str] = Field(default_factory=list)
    source_paths: list[str] = Field(default_factory=list)
    scope: str = "documents"


class ConversationCreate(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, max_length=200)
    collection_id: str | None = None
    document_id: str | None = None
    run_id: str | None = None


class ChatMessageRequest(BaseModel):
    model_config = {"extra": "forbid"}

    content: str = Field(min_length=1, max_length=20_000)
    collection_id: str | None = None
    document_id: str | None = None
    run_id: str | None = None
    request_id: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=8, ge=1, le=20)


__all__ = [
    "AnonymousRequest",
    "EnqueueRequest",
    "CancelRequest",
    "HookPayload",
    "ConversionOptionsV2",
    "OcrOptions",
    "TextOptions",
    "CsvOptions",
    "ExportRequest",
    "CollectionCreate",
    "IndexRequest",
    "ConversationCreate",
    "ChatMessageRequest",
]

# --------------------------------------------------------------------------- #
# Batches and resumable uploads
# --------------------------------------------------------------------------- #


class CreateBatchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    client_batch_key: str = Field(min_length=1, max_length=128)


class RegisterBatchItemRequest(BaseModel):
    model_config = {"extra": "forbid"}

    client_item_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    options: dict[str, Any] | None = None


class CompleteUploadRequest(BaseModel):
    model_config = {"extra": "forbid"}

    sha256: str = Field(min_length=64, max_length=64)
    total_parts: int = Field(ge=0)


class BatchRetryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    scope: str = "conversion"

