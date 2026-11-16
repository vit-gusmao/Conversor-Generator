"""End-to-end pipeline over the fixture corpus.

Every assertion is about user-visible behaviour: which sheets exist, which
headers they have, which values they contain — and that nothing was invented.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import load_workbook


def sheet_map(result):
    return {sheet.name: sheet for sheet in result.plan.sheets}


def headers_of(result, kind="records"):
    sheets = [s for s in result.plan.sheets if s.kind == kind]
    assert sheets, f"no {kind} sheet in {[s.kind for s in result.plan.sheets]}"
    return sheets[0].headers


def values_of(sheet) -> list[list[str]]:
    return [[cell.display if cell else "" for cell in row] for row in sheet.rows]


def test_single_field(pdf_paths, run_pdf):
    result, recorder = run_pdf("simple_field")
    sheets = result.plan.sheets
    assert len(sheets) == 1
    assert sheets[0].kind == "records"
    assert sheets[0].headers == ["Nome"]
    assert values_of(sheets[0]) == [["João da Silva"]]
    assert result.status in {"completed", "completed_with_warnings"}
    assert recorder.verify_chain()


def test_multiple_fields_one_record(pdf_paths, run_pdf):
    result, _ = run_pdf("multi_fields")
    sheet = result.plan.sheets[0]
    assert sheet.headers == ["Nome", "Idade", "Cidade", "Profissão"]
    assert values_of(sheet) == [["João da Silva", "25 anos", "Belo Horizonte", "Analista de Dados"]]


def test_fields_on_the_same_line(pdf_paths, run_pdf):
    result, _ = run_pdf("inline_fields")
    sheet = result.plan.sheets[0]
    assert sheet.headers == ["Nome", "Matrícula", "Cargo", "Cidade"]
    row = values_of(sheet)[0]
    assert row == ["João da Silva", "001245", "Analista de Dados", "Belo Horizonte"]
    # identifier keeps leading zeros and is stored as text
    assert sheet.rows[0][1].value == "001245"
    assert sheet.rows[0][1].value_type == "code"


def test_value_below_label(pdf_paths, run_pdf):
    result, _ = run_pdf("fields_below")
    sheet = result.plan.sheets[0]
    assert sheet.headers == ["Nome", "Cidade"]
    assert values_of(sheet) == [["João da Silva", "Belo Horizonte"]]


def test_multiline_value_stays_in_one_cell(pdf_paths, run_pdf):
    result, _ = run_pdf("multiline_value")
    sheets = result.plan.sheets
    assert len(sheets) == 1
    assert sheets[0].headers == ["Nome", "Endereço", "Cidade"]
    row = values_of(sheets[0])[0]
    assert row[1] == "Avenida Principal, número 250, bloco B, apartamento 301"


def test_geometric_two_columns(pdf_paths, run_pdf):
    result, _ = run_pdf("two_columns")
    sheet = result.plan.sheets[0]
    assert sheet.headers == ["Nome", "Data de nascimento", "Cidade", "Cargo"]
    row = values_of(sheet)[0]
    assert row[1] == "15/05/2000"
    assert sheet.rows[0][1].value_type == "date"


def test_transposed_table_becomes_a_record(pdf_paths, run_pdf):
    result, _ = run_pdf("transposed_table")
    sheet = result.plan.sheets[0]
    assert sheet.kind == "records"
    assert sheet.headers == ["Nome", "Idade", "Cidade", "Cargo"]
    assert values_of(sheet) == [["João da Silva", "25", "Belo Horizonte", "Analista"]]


def test_bordered_table_is_typed(pdf_paths, run_pdf):
    result, _ = run_pdf("bordered_table")
    sheet = result.plan.sheets[0]
    assert sheet.kind == "table"
    assert sheet.headers == ["Nome", "Cargo", "Salário"]
    money_cell = sheet.rows[0][2]
    assert money_cell.value_type == "money"
    assert money_cell.number_format == '"R$" #,##0.00'
    assert money_cell.value == pytest.approx(3500.0)


def test_free_text_is_preserved_without_invented_headers(pdf_paths, run_pdf):
    result, _ = run_pdf("free_text")
    sheet = result.plan.sheets[0]
    assert sheet.kind == "text"
    assert sheet.headers[0] == "Texto"
    text = " ".join(row[0] for row in values_of(sheet))
    assert "atividades de análise de dados" in text
    assert "Nome" not in sheet.headers


def test_mixed_document_produces_three_sheets(pdf_paths, run_pdf):
    result, _ = run_pdf("mixed_report")
    kinds = {s.kind for s in result.plan.sheets}
    assert {"records", "table", "text"} <= kinds
    records = [s for s in result.plan.sheets if s.kind == "records"][0]
    assert records.headers == ["Nome", "Matrícula", "Cargo", "Cidade"]
    assert records.rows[0][1].value == "001245"
    table = [s for s in result.plan.sheets if s.kind == "table"][0]
    assert table.headers == ["Mês", "Salário", "Bônus"]
    assert len(table.rows) == 2


def test_rotated_page_is_read_correctly(pdf_paths, run_pdf):
    result, _ = run_pdf("rotated_page")
    sheet = result.plan.sheets[0]
    assert sheet.kind == "records"
    assert sheet.headers == ["Nome", "Cidade"]
    assert values_of(sheet) == [["João da Silva", "Belo Horizonte"]]


def test_scanned_page_fails_without_inventing_data(pdf_paths, run_pdf):
    """No OCR engine installed: the scan must fail loudly, never invent text."""
    result, recorder = run_pdf("scanned_page", ocr=True, no_engine=True)
    assert result.status == "failed"
    assert result.plan.sheets == []
    assert any(issue.code == "OCR_UNAVAILABLE" for issue in recorder.issues)


def test_hybrid_page_reports_unreadable_image(pdf_paths, run_pdf):
    result, recorder = run_pdf("hybrid_page")
    assert result.status in {"completed", "completed_with_warnings"}
    assert any(issue.code == "IMAGE_REGION_NOT_RECOGNIZED" for issue in recorder.issues)


def test_image_only_page_is_partial_and_never_invented(pdf_paths, run_pdf):
    """A mixed document: page 1 native, page 2 only an image (no OCR engine)."""
    result, recorder = run_pdf("mixed_native_scan", ocr=True, no_engine=True)

    assert result.status == "completed_with_warnings"
    assert result.quality == "partial"
    assert result.error is None

    pages = {page.page_number: page for page in result.ir.pages}
    assert pages[1].text_source == "native"
    assert pages[1].status == "ok"
    assert pages[2].text_source == "none"
    assert pages[2].status == "partial"
    assert pages[2].classification == "scan"
    assert result.stats["pages_unrecognized"] == 1

    codes = {issue.code for issue in recorder.issues}
    assert {"OCR_UNAVAILABLE", "IMAGE_REGION_NOT_RECOGNIZED", "PAGE_WITHOUT_TEXT"} <= codes

    checks = {check["check"]: check["status"] for check in result.checks}
    assert checks["unrecognized_regions"] == "warn"

    # The readable page is still exported...
    assert result.plan.sheets
    exported = " ".join(
        str(cell.value)
        for sheet in result.plan.sheets
        for row in sheet.rows
        for cell in row
        if cell is not None
    )
    assert "João da Silva" in exported
    # ...and nothing from the unread image was invented.
    assert "Maria Oliveira" not in exported
    assert "Contrato 2026" not in exported


def test_blank_page_does_not_degrade_quality(pdf_paths, run_pdf):
    """An empty page is not lost content: it must not mark the run partial."""
    result, recorder = run_pdf("native_plus_blank")
    assert result.status == "completed"
    assert result.quality == "validated"
    pages = {page.page_number: page for page in result.ir.pages}
    assert pages[2].text_source == "none"
    assert pages[2].status == "ok"
    assert not any(issue.code == "PAGE_WITHOUT_TEXT" for issue in recorder.issues)


def test_encrypted_pdf_is_rejected(pdf_paths, run_pdf):
    result, _ = run_pdf("encrypted_pdf")
    assert result.status == "failed"
    assert result.error is not None and result.error.code == "ENCRYPTED_PDF"


def test_non_pdf_is_rejected(pdf_paths, run_pdf):
    result, _ = run_pdf("not_a_pdf")
    assert result.status == "failed"
    assert result.error is not None and result.error.code == "INVALID_FILE"


@pytest.mark.parametrize(
    "fixture",
    [
        "simple_field",
        "multi_fields",
        "inline_fields",
        "fields_below",
        "multiline_value",
        "two_columns",
        "bordered_table",
        "borderless_table",
        "transposed_table",
        "multipage_table",
        "free_text",
        "mixed_report",
        "rotated_page",
    ],
)
def test_workbook_never_contains_images_or_formulas(pdf_paths, run_pdf, fixture):
    result, _ = run_pdf(fixture)
    assert result.xlsx is not None
    archive = zipfile.ZipFile(io.BytesIO(result.xlsx.data))
    names = archive.namelist()
    assert not [n for n in names if n.startswith("xl/media/")]
    assert not [n for n in names if "drawing" in n]
    workbook = load_workbook(io.BytesIO(result.xlsx.data))
    for worksheet in workbook.worksheets:
        assert len(getattr(worksheet, "_images", [])) == 0
        for row in worksheet.iter_rows():
            for cell in row:
                assert cell.data_type != "f"


def test_xlsx_audit_passes_for_every_valid_fixture(pdf_paths, run_pdf):
    result, _ = run_pdf("mixed_report")
    checks = {check["check"]: check["status"] for check in result.checks}
    for name in (
        "xlsx_zip_integrity",
        "xlsx_no_media",
        "xlsx_no_image_relationships",
        "xlsx_no_formulas",
        "xlsx_values_match_plan",
        "xlsx_has_cells",
    ):
        assert checks[name] == "pass", (name, checks[name])


def test_coverage_accounts_for_every_block(pdf_paths, run_pdf):
    result, _ = run_pdf("mixed_report")
    blocks = [block for page in result.ir.pages for block in page.blocks]
    assert blocks
    assert all(block.destination is not None for block in blocks)
    assert result.stats["coverage_percent"] == 100.0


def test_text_sheet_fills_section_column(pdf_paths, run_pdf):
    """The `Seção` column must be populated, not an always-empty column."""
    result, _ = run_pdf("mixed_report")
    text_sheet = [s for s in result.plan.sheets if s.kind == "text"][0]
    sections = [row[1].value for row in text_sheet.rows if row[1] is not None]
    assert "Pagamentos" in sections
    assert "Observações" in sections


def test_rotation_work_copy_is_removed(pdf_paths):
    """The de-rotated duplicate must not survive the conversion (retention)."""
    from config import settings
    from tests.conftest import run_fixture

    run_id = "run_cleanup_check"
    result, recorder = run_fixture(pdf_paths["rotated_page"], run_id=run_id)
    assert result.status in {"completed", "completed_with_warnings"}
    # the copy was really created…
    assert any(event.event_type == "ROTATION_NORMALIZED" for event in recorder.events)
    # …and removed afterwards
    assert not (settings.data_dir / "work" / f"{run_id}.derotated.pdf").exists()


def test_audit_summary_is_reproducible(pdf_paths, run_pdf):
    result, recorder = run_pdf("mixed_report")
    assert result.summary["hash_chain_valid"] is True
    assert all(event.event_hash for event in recorder.events)
    assert result.summary["metrics"]["cells_generated"] == result.stats["cells"]


@pytest.mark.skipif(
    not __import__("engine.ocr.engines", fromlist=["GlmOcrEngine"]).GlmOcrEngine.available(),
    reason="GLM-OCR (Ollama + glm-ocr:latest) não disponível neste ambiente",
)
def test_scanned_page_is_recovered_by_glm_ocr(pdf_paths, run_pdf):
    """With GLM-OCR installed, a text-less page is recovered instead of failing."""
    result, recorder = run_pdf("scanned_page", ocr=True)

    assert result.status in {"completed", "completed_with_warnings"}
    assert result.quality != "failed"
    # text really came from the recognizer, not from a nonexistent text layer
    pages = {page.page_number: page for page in result.ir.pages}
    assert pages[1].text_source == "ocr"
    assert result.stats.get("ocr_pages", 0) >= 1

    # the engine that ran is recorded in the audit trail
    completed = [e for e in recorder.events if e.event_type == "OCR_COMPLETED"]
    assert completed, "no OCR_COMPLETED event was recorded"
    payload = completed[0].payload or {}
    assert payload.get("engine") == "glm-ocr"
    assert payload.get("model")
    assert payload.get("boxing_method") in {"projection_v1", "even_distribution", "html_grid_v1"}

    exported = " ".join(
        str(cell.value)
        for sheet in result.plan.sheets
        for row in sheet.rows
        for cell in row
        if cell is not None and cell.value is not None
    )
    assert exported.strip(), "recovered page produced no exported text"
