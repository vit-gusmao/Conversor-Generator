"""Image -> TXT pipeline: explicit failure without OCR, real TXT with one."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from PIL import Image

from audit.events import AuditRecorder
from engine.ocr.base import OcrWord
from ingestion.image_pipeline import ImageConversionPipeline
from models.db import init_db


@pytest.fixture(autouse=True)
def _db():
    init_db()


class FakeEngine:
    name = "fake-engine"
    version = "1.0"
    kind = "ocr"

    @classmethod
    def available(cls) -> bool:
        return True

    def recognize(self, image, *, mode: str = "page", lang: str | None = None):
        return [
            OcrWord("Olá", (10, 10, 60, 30), 0.99),
            OcrWord("mundo", (70, 10, 140, 30), 0.98),
        ]


class UnavailableEngine(FakeEngine):
    name = "none"

    def recognize(self, image, *, mode: str = "page", lang: str | None = None):
        raise AssertionError("should not be called")


def _write_image(tmp_path):
    path = tmp_path / "entrada.png"
    Image.new("RGB", (200, 100), (255, 255, 255)).save(path)
    return path


def test_image_with_ocr_produces_txt(tmp_path):
    path = _write_image(tmp_path)
    recorder = AuditRecorder(run_id="run_img_ok")
    pipeline = ImageConversionPipeline(
        image_path=path,
        run_id="run_img_ok",
        conversion_id="conv_img_ok",
        recorder=recorder,
    )
    with patch("ingestion.image_pipeline.ocr_capability", return_value={"available": True}), patch(
        "engine.ocr.ocr_runner.get_engine", return_value=FakeEngine()
    ):
        result = pipeline.run()
    assert result.status == "completed", result.error
    assert result.xlsx is None
    assert [item.format for item in result.exports] == ["txt"]
    text = result.exports[0].data.decode("utf-8")
    assert "Olá" in text and "mundo" in text
    assert result.text_plan is not None and result.text_plan.page_count == 1


def test_image_without_ocr_fails_instead_of_empty_success(tmp_path):
    path = _write_image(tmp_path)
    recorder = AuditRecorder(run_id="run_img_noocr")
    pipeline = ImageConversionPipeline(
        image_path=path,
        run_id="run_img_noocr",
        conversion_id="conv_img_noocr",
        recorder=recorder,
    )
    with patch("ingestion.image_pipeline.ocr_capability", return_value={"available": False}):
        result = pipeline.run()
    assert result.status == "failed"
    assert result.error is not None and result.error.code == "OCR_UNAVAILABLE"
    assert result.exports == []


def test_image_with_no_recognized_text_is_not_success(tmp_path):
    path = _write_image(tmp_path)
    recorder = AuditRecorder(run_id="run_img_empty")

    class EmptyEngine(FakeEngine):
        def recognize(self, image, *, mode="page", lang=None):
            return []

    pipeline = ImageConversionPipeline(
        image_path=path,
        run_id="run_img_empty",
        conversion_id="conv_img_empty",
        recorder=recorder,
    )
    with patch("ingestion.image_pipeline.ocr_capability", return_value={"available": True}), patch(
        "engine.ocr.ocr_runner.get_engine", return_value=EmptyEngine()
    ):
        result = pipeline.run()
    assert result.status == "failed"
    assert result.error is not None and result.error.code == "NO_TEXT_RECOVERED"
    assert result.exports == []
