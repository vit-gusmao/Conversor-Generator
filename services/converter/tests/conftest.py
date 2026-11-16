"""Pytest configuration and shared fixtures.

Environment is configured **before** importing the application so the cached
settings singleton points at an isolated database and storage root.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="pdf2x-tests-"))
os.environ.setdefault("CONVERTER_DATABASE_URL", f"sqlite:///{_TEST_ROOT.as_posix()}/test.db")
os.environ.setdefault("STORAGE_LOCAL_ROOT", str(_TEST_ROOT / "storage"))
os.environ.setdefault("RUNTIME_MODE", "standalone")
os.environ.setdefault("CONVERTER_OCR_ENABLED", "false")
os.environ.setdefault("CONVERTER_QUEUE", "inline")
os.environ.setdefault("CONVERTER_INTERNAL_TOKEN", "test-internal-token")
os.environ.setdefault("CONVERTER_WORKERS", "2")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from audit.events import AuditRecorder  # noqa: E402
from engine.pipeline import ConversionPipeline, PipelineOptions, PipelineResult  # noqa: E402
from tests.fixtures.generate_pdfs import generate_all  # noqa: E402


@pytest.fixture(scope="session")
def pdf_paths() -> dict[str, Path]:
    return generate_all(_TEST_ROOT / "fixtures")


@pytest.fixture(scope="session")
def test_root() -> Path:
    return _TEST_ROOT


def run_fixture(
    path: Path,
    *,
    ocr: bool = False,
    run_id: str | None = None,
    no_engine: bool = False,
) -> tuple[PipelineResult, AuditRecorder]:
    """Run the pipeline on a fixture.

    ``no_engine`` forces the "no OCR engine installed" condition so the
    degradation path is deterministic regardless of which recognizer happens
    to be installed on the machine running the suite.
    """
    run_id = run_id or f"run_test_{path.stem}"
    recorder = AuditRecorder(run_id=run_id)
    pipeline = ConversionPipeline(
        pdf_path=path,
        run_id=run_id,
        conversion_id=f"conv_{path.stem}",
        recorder=recorder,
        options=PipelineOptions(ocr_enabled=ocr),
    )
    if no_engine:
        from unittest.mock import patch

        from engine.ocr.engines import NullEngine

        with patch("engine.ocr.ocr_runner.get_engine", return_value=NullEngine()):
            return pipeline.run(), recorder
    return pipeline.run(), recorder


@pytest.fixture()
def run_pdf(pdf_paths):
    def _run(
        name: str, *, ocr: bool = False, no_engine: bool = False
    ) -> tuple[PipelineResult, AuditRecorder]:
        return run_fixture(pdf_paths[name], ocr=ocr, no_engine=no_engine)

    return _run


@pytest.fixture(scope="session")
def api_client():
    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as client:
        yield client
