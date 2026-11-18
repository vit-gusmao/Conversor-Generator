"""Full-document guarantee: every page has an outcome.

These tests are opt-in (`PDF2X_RUN_LARGE=1`) because they generate a real PDF
with thousands of pages and run the whole engine. They are the evidence for
"no page ceiling", so they assert per-page markers, not just a finished status.
"""

from __future__ import annotations

import os
import time

import pytest

from audit.events import AuditRecorder
from engine.pipeline import ConversionPipeline, PipelineOptions
from tests.fixtures.generate_large_pdf import generate_marked_pdf, page_marker

PAGES = int(os.environ.get("PDF2X_LARGE_PAGES", "501"))


def _enabled() -> bool:
    return os.environ.get("PDF2X_RUN_LARGE") == "1"


pytestmark = pytest.mark.skipif(
    not _enabled(), reason="defina PDF2X_RUN_LARGE=1 para gerar e processar documentos grandes"
)


def _peak_rss_mb() -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
    except Exception:  # noqa: BLE001
        return None


def _run(path, run_id: str, *, ocr: bool = False):
    recorder = AuditRecorder(run_id=run_id)
    pipeline = ConversionPipeline(
        pdf_path=path,
        run_id=run_id,
        conversion_id=f"conv_{run_id}",
        recorder=recorder,
        options=PipelineOptions(ocr_enabled=ocr),
    )
    started = time.perf_counter()
    result = pipeline.run()
    return result, recorder, time.perf_counter() - started


def test_every_page_of_a_large_document_has_an_outcome(tmp_path):
    source = generate_marked_pdf(tmp_path / "large.pdf", PAGES)
    result, recorder, elapsed = _run(source, f"run_large_{PAGES}")

    assert result.status in {"completed", "completed_with_warnings"}, result.error
    assert result.ir.page_count == PAGES
    assert len(result.ir.pages) == PAGES

    # Absolute numbering: 1..N, each exactly once.
    numbers = [page.page_number for page in result.ir.pages]
    assert numbers == list(range(1, PAGES + 1))
    assert result.stats["pages_total"] == PAGES
    assert result.stats["pages_processed"] == PAGES
    assert result.stats["pages_failed"] == 0

    # No silent truncation anywhere in the audit trail.
    assert not [issue for issue in recorder.issues if issue.code == "PAGES_NOT_PROCESSED"]

    # The text plan covers every page and carries the last page's marker.
    assert result.text_plan is not None
    planned = {page.page_number for page in result.text_plan.pages}
    assert planned == set(range(1, PAGES + 1))
    last_page = next(page for page in result.text_plan.pages if page.page_number == PAGES)
    joined = "\n".join(segment.text for segment in last_page.segments)
    assert page_marker(PAGES) in joined

    # A first-page marker proves the beginning was not skipped either.
    first_page = next(page for page in result.text_plan.pages if page.page_number == 1)
    assert page_marker(1) in "\n".join(segment.text for segment in first_page.segments)

    print(
        f"\n[large-document] pages={PAGES} elapsed={elapsed:.1f}s "
        f"rate={PAGES / elapsed:.1f} pages/s rss={_peak_rss_mb()} MB"
    )


def test_a_legacy_page_configuration_cannot_truncate(tmp_path, monkeypatch):
    """P1: an old 500-page setting must not cut a 501-page document."""
    from config import settings

    monkeypatch.setattr(settings, "converter_max_pages", 500)
    monkeypatch.setattr(settings, "upload_max_pages", 500)
    monkeypatch.setattr(settings, "large_document_pipeline_enabled", True)

    pages = 501
    source = generate_marked_pdf(tmp_path / "legacy.pdf", pages)
    result, recorder, _elapsed = _run(source, "run_large_legacy")

    assert result.status in {"completed", "completed_with_warnings"}, result.error
    assert len(result.ir.pages) == pages
    assert result.stats["pages_failed"] == 0
    assert not [issue for issue in recorder.issues if issue.code == "PAGES_NOT_PROCESSED"]
    assert max(page.page_number for page in result.ir.pages) == pages


def test_page_windows_do_not_lose_numbering(tmp_path):
    """The extraction window size is a scheduling knob, not a page ceiling."""
    source = generate_marked_pdf(tmp_path / "window.pdf", 60)
    result, _recorder, _elapsed = _run(source, "run_large_window")
    assert [page.page_number for page in result.ir.pages] == list(range(1, 61))
