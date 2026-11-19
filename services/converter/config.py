"""Application configuration.

Every value is read from environment variables (see the repository `.env.example`
for the full list).  Nothing here requires a network service: the defaults are
chosen so that the whole converter runs with a single `uvicorn` process, SQLite
and the local filesystem.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_DIR = Path(__file__).resolve().parent


def _env_files() -> tuple[str, ...]:
    """Load the monorepo `.env` first, then the service-local one."""
    candidates = [SERVICE_DIR.parent.parent / ".env", SERVICE_DIR / ".env"]
    return tuple(str(p) for p in candidates if p.exists()) or (".env",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- runtime -----------------------------------------------------------
    runtime_mode: str = Field(default="standalone", alias="RUNTIME_MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    converter_port: int = Field(default=8000, alias="CONVERTER_PORT")
    web_origin: str = Field(default="http://localhost:5173", alias="WEB_ORIGIN")
    allow_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000", alias="ALLOWED_ORIGINS"
    )

    # -- storage -----------------------------------------------------------
    converter_database_url: str = Field(
        default="sqlite:///./data/converter.db", alias="CONVERTER_DATABASE_URL"
    )
    storage_backend: str = Field(default="local", alias="STORAGE_BACKEND")
    storage_local_root: str = Field(default="./data/storage", alias="STORAGE_LOCAL_ROOT")
    s3_endpoint: str | None = Field(default=None, alias="S3_ENDPOINT")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")
    s3_access_key: str | None = Field(default=None, alias="S3_ACCESS_KEY")
    s3_secret_key: str | None = Field(default=None, alias="S3_SECRET_KEY")
    s3_bucket: str = Field(default="pdf2x", alias="S3_BUCKET")
    s3_secure: bool = Field(default=False, alias="S3_SECURE")

    # -- queue -------------------------------------------------------------
    converter_queue: str = Field(default="inline", alias="CONVERTER_QUEUE")
    converter_workers: int = Field(default=2, alias="CONVERTER_WORKERS")
    #: Whole-document wall-clock limit. 0 disables it: a document is no longer
    #: bounded by time, only each OCR call and each unit have their own timeout.
    converter_task_timeout_seconds: int = Field(
        default=0, alias="CONVERTER_TASK_TIMEOUT_SECONDS"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # -- limits ------------------------------------------------------------
    #: Deprecated page ceiling. `None` (the default) means the whole document
    #: is processed. A positive value is *not* applied while the full-document
    #: pipeline is enabled; it is only honoured as a legacy compatibility cap.
    converter_max_pages: int | None = Field(default=None, alias="CONVERTER_MAX_PAGES")
    converter_max_pdf_bytes: int = Field(
        default=52_428_800, alias="CONVERTER_MAX_PDF_BYTES"
    )
    converter_render_dpi: int = Field(default=300, alias="CONVERTER_RENDER_DPI")
    converter_ocr_enabled: bool = Field(default=True, alias="CONVERTER_OCR_ENABLED")
    converter_ocr_min_confidence: float = Field(
        default=0.45, alias="CONVERTER_OCR_MIN_CONFIDENCE"
    )
    converter_ocr_max_pixels: int = Field(
        default=40_000_000, alias="CONVERTER_OCR_MAX_PIXELS"
    )
    converter_max_fallback_attempts: int = Field(
        default=2, alias="CONVERTER_MAX_FALLBACK_ATTEMPTS"
    )
    # -- generalized ingestion --------------------------------------------
    #: Frame/pixel budgets are resource guards, not page-count ceilings.
    converter_max_image_frames: int = Field(default=500, alias="CONVERTER_MAX_IMAGE_FRAMES")
    converter_max_image_megapixels: float = Field(
        default=40.0, alias="CONVERTER_MAX_IMAGE_MEGAPIXELS"
    )
    converter_max_job_megapixels: float = Field(
        default=200.0, alias="CONVERTER_MAX_JOB_MEGAPIXELS"
    )

    # -- large documents / batches -----------------------------------------
    #: Master switch for incremental full-document processing.
    large_document_pipeline_enabled: bool = Field(
        default=True, alias="LARGE_DOCUMENT_PIPELINE_ENABLED"
    )
    batch_upload_enabled: bool = Field(default=True, alias="BATCH_UPLOAD_ENABLED")
    #: `None` means "no product ceiling on files per batch".
    max_files_per_batch: int | None = Field(default=None, alias="MAX_FILES_PER_BATCH")
    upload_part_bytes: int = Field(default=8_388_608, alias="UPLOAD_PART_BYTES")
    upload_concurrency: int = Field(default=3, alias="UPLOAD_CONCURRENCY")
    upload_session_ttl_hours: int = Field(default=24, alias="UPLOAD_SESSION_TTL_HOURS")
    #: Reuse per-page checkpoints when the same run is re-executed after a crash.
    checkpoint_reuse_enabled: bool = Field(default=True, alias="CHECKPOINT_REUSE_ENABLED")
    #: Pages processed per checkpoint window. Affects fairness/latency only.
    converter_page_window: int = Field(default=25, alias="CONVERTER_PAGE_WINDOW")
    #: Timeout of one OCR call, in seconds (a unit, not the whole document).
    converter_ocr_call_timeout_seconds: int = Field(
        default=300, alias="CONVERTER_OCR_CALL_TIMEOUT_SECONDS"
    )
    converter_target_formats: str = Field(
        default="xlsx,txt,csv", alias="CONVERTER_TARGET_FORMATS"
    )

    # -- assistant (local Qwen3.5-4B via Ollama) --------------------------
    # --- Upgrade feature flags (published through /capabilities) ------------
    # Enabling/disabling never deletes definitions or history; disabled
    # features reject new work and explain the state.
    source_imports_enabled: bool = Field(default=True, alias="SOURCE_IMPORTS_ENABLED")
    spreadsheet_input_enabled: bool = Field(default=True, alias="SPREADSHEET_INPUT_ENABLED")
    automation_executor_enabled: bool = Field(
        default=False, alias="AUTOMATION_EXECUTOR_ENABLED"
    )
    desktop_host_enabled: bool = Field(default=False, alias="DESKTOP_HOST_ENABLED")
    automation_poll_seconds: float = Field(default=5.0, alias="AUTOMATION_POLL_SECONDS")
    automation_history_retention_days: int = Field(
        default=90, alias="AUTOMATION_HISTORY_RETENTION_DAYS"
    )
    automation_failure_retention_days: int = Field(
        default=7, alias="AUTOMATION_FAILURE_RETENTION_DAYS"
    )

    assistant_enabled: bool = Field(default=False, alias="ASSISTANT_ENABLED")
    assistant_provider: str = Field(default="ollama", alias="ASSISTANT_PROVIDER")
    assistant_host: str = Field(default="127.0.0.1", alias="ASSISTANT_HOST")
    assistant_port: int = Field(default=11434, alias="ASSISTANT_PORT")
    assistant_model: str = Field(default="qwen3.5:4b", alias="ASSISTANT_MODEL")
    assistant_context_tokens: int = Field(default=8192, alias="ASSISTANT_CONTEXT_TOKENS")
    assistant_max_output_tokens: int = Field(
        default=1024, alias="ASSISTANT_MAX_OUTPUT_TOKENS"
    )
    assistant_timeout_seconds: int = Field(default=120, alias="ASSISTANT_TIMEOUT_SECONDS")
    assistant_max_concurrent: int = Field(default=1, alias="ASSISTANT_MAX_CONCURRENT")
    #: Qwen3.5 emits a separate `thinking` stream by default. It is disabled so
    #: the answer content is not starved of tokens; enable only for debugging.
    assistant_thinking: bool = Field(default=False, alias="ASSISTANT_THINKING")
    embedding_model: str = Field(default="nomic-embed-text", alias="EMBEDDING_MODEL")
    assistant_require_sources: bool = Field(default=True, alias="ASSISTANT_REQUIRE_SOURCES")

    # -- ocr: GLM-OCR (VLM local via Ollama, GPU) ---------------------------
    # The model runs 100% locally; Ollama
    # offloads every layer to the GPU automatically when VRAM allows.
    glmocr_enabled: bool = Field(default=True, alias="GLMOCR_ENABLED")
    glmocr_host: str = Field(default="127.0.0.1", alias="GLMOCR_HOST")
    glmocr_port: int = Field(default=11434, alias="GLMOCR_PORT")
    glmocr_model: str = Field(default="glm-ocr:latest", alias="GLMOCR_MODEL")
    glmocr_request_timeout: int = Field(default=300, alias="GLMOCR_REQUEST_TIMEOUT")
    glmocr_num_ctx: int = Field(default=8192, alias="GLMOCR_NUM_CTX")
    glmocr_num_predict: int = Field(default=4096, alias="GLMOCR_NUM_PREDICT")
    glmocr_keep_alive: str = Field(default="30m", alias="GLMOCR_KEEP_ALIVE")
    glmocr_num_gpu: int | None = Field(default=None, alias="GLMOCR_NUM_GPU")
    glmocr_confidence: float = Field(default=0.90, alias="GLMOCR_CONFIDENCE")
    glmocr_probe_cache_seconds: int = Field(default=60, alias="GLMOCR_PROBE_CACHE_SECONDS")

    # -- security ----------------------------------------------------------
    converter_internal_token: str = Field(
        default="dev-internal-token-change-me", alias="CONVERTER_INTERNAL_TOKEN"
    )
    jwt_secret: str = Field(default="dev-jwt-secret-change-me", alias="JWT_SECRET")
    jwt_expires_in_seconds: int = Field(default=604800, alias="JWT_EXPIRES_IN_SECONDS")
    auth_mode: str = Field(default="optional", alias="AUTH_MODE")

    # -- retry/retention ---------------------------------------------------
    retention_pdf_days: int = Field(default=7, alias="RETENTION_PDF_DAYS")
    retention_xlsx_days: int = Field(default=30, alias="RETENTION_XLSX_DAYS")
    retention_audit_days: int = Field(default=90, alias="RETENTION_AUDIT_DAYS")
    artifact_compression: bool = Field(default=True, alias="ARTIFACT_COMPRESSION")

    # -- integration -------------------------------------------------------
    converter_public_url: str = Field(
        default="http://localhost:8000", alias="CONVERTER_PUBLIC_URL"
    )
    public_api_url: str = Field(default="http://localhost:3000", alias="PUBLIC_API_URL")
    converter_callback_url: str | None = Field(
        default=None, alias="CONVERTER_CALLBACK_URL"
    )
    upload_max_bytes: int = Field(default=52_428_800, alias="UPLOAD_MAX_BYTES")
    #: Deprecated page ceiling (see `converter_max_pages`).
    upload_max_pages: int | None = Field(default=None, alias="UPLOAD_MAX_PAGES")

    @field_validator("runtime_mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        v = (v or "standalone").lower()
        if v not in {"standalone", "full"}:
            raise ValueError("RUNTIME_MODE must be 'standalone' or 'full'")
        return v

    @field_validator("storage_backend")
    @classmethod
    def _storage(cls, v: str) -> str:
        v = (v or "local").lower()
        if v not in {"local", "s3"}:
            raise ValueError("STORAGE_BACKEND must be 'local' or 's3'")
        return v

    @property
    def glmocr_base_url(self) -> str:
        return f"http://{self.glmocr_host}:{self.glmocr_port}"

    @property
    def assistant_base_url(self) -> str:
        return f"http://{self.assistant_host}:{self.assistant_port}"

    def target_formats(self) -> list[str]:
        raw = (self.converter_target_formats or "").split(",")
        allowed = {"xlsx", "txt", "csv"}
        result = [item.strip().lower() for item in raw if item.strip()]
        return [item for item in result if item in allowed] or ["xlsx"]

    @property
    def insecure_default_secrets(self) -> list[str]:
        """Secrets still set to their documented development defaults.

        They are kept as defaults so the standalone mode needs zero setup, but
        a deployment that exposes the service must replace them.
        """
        insecure: list[str] = []
        if self.converter_internal_token == "dev-internal-token-change-me":
            insecure.append("CONVERTER_INTERNAL_TOKEN")
        if self.jwt_secret == "dev-jwt-secret-change-me":
            insecure.append("JWT_SECRET")
        return insecure

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allow_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.converter_database_url.startswith("sqlite")

    @property
    def data_dir(self) -> Path:
        if self.is_sqlite:
            raw = self.converter_database_url.replace("sqlite:///", "")
            p = Path(raw)
            if not p.is_absolute():
                p = SERVICE_DIR / p
            return p.parent
        return SERVICE_DIR / "data"

    @property
    def storage_dir(self) -> Path:
        p = Path(self.storage_local_root)
        if not p.is_absolute():
            p = SERVICE_DIR / p
        return p

    def max_pages(self) -> int | None:
        """Effective page ceiling, or `None` for "process everything".

        The full-document pipeline never truncates. A positive value in the
        environment is kept only so an operator that explicitly disables the
        new pipeline keeps the old behaviour; it also produces a deprecation
        warning (see `deprecated_page_limits`).
        """
        if self.large_document_pipeline_enabled:
            return None
        configured = [v for v in (self.converter_max_pages, self.upload_max_pages) if v]
        return min(configured) if configured else None

    @property
    def deprecated_page_limits(self) -> dict[str, int]:
        """Configured page ceilings that no longer apply to the default flow."""
        found: dict[str, int] = {}
        if self.converter_max_pages:
            found["CONVERTER_MAX_PAGES"] = int(self.converter_max_pages)
        if self.upload_max_pages:
            found["UPLOAD_MAX_PAGES"] = int(self.upload_max_pages)
        return found

    def max_bytes(self) -> int:
        return min(self.converter_max_pdf_bytes, self.upload_max_bytes)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.storage_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()

__all__ = ["Settings", "get_settings", "settings", "SERVICE_DIR"]
