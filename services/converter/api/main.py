"""FastAPI application.

Serves two surfaces from one process:

* `/internal/v1` — consumed by the NestJS gateway in `full` mode;
* `/api/v1`      — the same public contract, used directly in `standalone` mode.

In standalone mode the built frontend (`apps/web/dist`) is also served, so a
single `uvicorn` process gives a complete, working product.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.batch_routes import router as batch_router
from api.desktop_guard import DesktopHostGuard
from api.internal_routes import router as internal_router
from api.knowledge_routes import router as knowledge_router
from api.public_routes import router as public_router
from api.upload_routes import router as upload_router
from config import SERVICE_DIR, settings
from logging_setup import configure_logging
from models.db import init_db
from version import ENGINE_VERSION

logger = logging.getLogger(__name__)
REPO_ROOT = SERVICE_DIR.parents[1]
# Packaged run: the SPA ships as an external resource next to the executable.
WEB_DIST = Path(os.environ.get("WEB_DIST_DIR") or (REPO_ROOT / "apps" / "web" / "dist"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    settings.ensure_dirs()
    init_db()
    insecure = settings.insecure_default_secrets
    if insecure:
        logger.warning(
            "segredos com valor padrão de desenvolvimento em uso (%s): "
            "defina-os antes de expor o serviço",
            ", ".join(insecure),
        )
    try:
        from jobqueue import get_queue

        queue = get_queue()
        logger.info("converter ready: queue=%s storage=%s ocr=%s", queue.name, settings.storage_backend, settings.converter_ocr_enabled)
    except Exception:  # noqa: BLE001
        logger.exception("queue initialisation failed")

    # Calendar/executor loop: standalone/local desktop runtime only. The full
    # deployment runs `python -m automation.cli` as its own service instead.
    automation_loop = None
    if getattr(settings, "automation_executor_enabled", False) and settings.runtime_mode == "standalone":
        try:
            from automation.loop import AutomationLoop

            automation_loop = AutomationLoop(poll_seconds=settings.automation_poll_seconds)
            automation_loop.start()
            logger.info("automation executor enabled (poll=%ss)", settings.automation_poll_seconds)
        except Exception:  # noqa: BLE001
            logger.exception("automation loop failed to start")

    yield
    if automation_loop is not None:
        try:
            automation_loop.stop()
        except Exception:  # noqa: BLE001
            pass
    try:
        from jobqueue import get_queue

        get_queue().shutdown()
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="Conversor Generator — motor de conversão",
        version=ENGINE_VERSION,
        description=(
            "Motor local de conversão de PDF e imagens em XLSX, TXT e CSV com "
            "proveniência completa. Nenhuma imagem é exportada; todo texto "
            "recuperado recebe um destino auditável."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # Desktop mode is same-origin (the SPA is served by this process): no CORS
    # at all, so no wildcard or foreign origin can read responses.
    if not settings.desktop_host_enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins or ["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "X-Device-Id",
                "Content-Disposition",
                "Last-Event-ID",
                "Retry-After",
                "X-Request-ID",
            ],
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):  # noqa: ANN001
        detail = exc.detail
        headers = getattr(exc, "headers", None)
        if isinstance(detail, dict) and "error" in detail:
            return JSONResponse(content=detail, status_code=exc.status_code, headers=headers)
        return JSONResponse(
            content={"error": {"code": "HTTP_ERROR", "message": str(detail), "details": {}}},
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):  # noqa: ANN001
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Requisição inválida.",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN001
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL",
                    "message": "Falha inesperada no servidor.",
                    "details": {},
                }
            },
        )

    app.include_router(public_router)
    app.include_router(batch_router)
    app.include_router(upload_router)
    app.include_router(knowledge_router)
    from api.schedule_routes import internal_router as schedule_internal_router
    from api.schedule_routes import router as schedule_router
    from api.source_routes import internal_router as source_internal_router
    from api.source_routes import router as source_router

    app.include_router(source_router)
    app.include_router(source_internal_router)
    app.include_router(schedule_router)
    app.include_router(schedule_internal_router)
    app.include_router(internal_router)

    @app.get("/health", tags=["system"], include_in_schema=False)
    def root_health():
        return {"status": "ok", "version": ENGINE_VERSION}

    @app.get("/", include_in_schema=False)
    def root():
        if (WEB_DIST / "index.html").exists():
            return FileResponse(WEB_DIST / "index.html")
        return {
            "service": "pdf2x-converter",
            "version": ENGINE_VERSION,
            "api": "/api/v1",
            "docs": "/docs",
        }

    if (WEB_DIST / "index.html").exists():
        if (WEB_DIST / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # Never let the SPA fallback swallow an API route that does not
            # exist: a mistyped internal path must be a JSON 404, not HTML 200.
            if full_path.startswith(("api/", "internal/")):
                raise HTTPException(status_code=404, detail="Not Found")
            candidate = (WEB_DIST / full_path).resolve()
            if full_path and candidate.is_file() and WEB_DIST.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    if settings.desktop_host_enabled and settings.desktop_bootstrap_secret:
        # Registered last so it is the outermost middleware: nothing runs before
        # the host credential, Host and Origin checks pass.
        app.add_middleware(
            DesktopHostGuard,
            secret=settings.desktop_bootstrap_secret,
            enabled=True,
        )

    return app


app = create_app()


__all__ = ["app", "create_app"]
