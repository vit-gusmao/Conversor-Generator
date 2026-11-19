"""T3 — spreadsheet conversion pipeline: plan, evidence and publication."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook, load_workbook

from audit.events import AuditRecorder
from engine.pipeline import PipelineOptions
from ingestion.spreadsheet_pipeline import SpreadsheetConversionPipeline


def _make_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Faturas"
    ws.append(["Documento", "Valor"])
    ws.append(["00123456789", 1234.56])
    ws.append(["Fatura", 99])
    ws["B4"] = "=SUM(B2:B3)"
    hidden = wb.create_sheet("Oculta")
    hidden.append(["secreto"])
    hidden.sheet_state = "hidden"
    wb.create_sheet("Vazia")
    wb.save(path)


def _make_csv(path: Path) -> None:
    path.write_bytes(
        "Nome;Valor\nAna;\"linha um\ne dois\"\n".encode("utf-8-sig")
    )


class _NullSink:
    def update(self, *args, **kwargs):
        pass


def _run_pipeline(path: Path, options: PipelineOptions | None = None):
    recorder = AuditRecorder(run_id="run_sheet_test")
    pipeline = SpreadsheetConversionPipeline(
        spreadsheet_path=path,
        run_id="run_sheet_test",
        conversion_id="conv_sheet_test",
        recorder=recorder,
        sink=_NullSink(),
        options=options or PipelineOptions(),
    )
    result = pipeline.run()
    return result, recorder


def _summary_codes(recorder) -> set[str]:
    return {issue.code for issue in recorder.issues}


def test_xlsx_pipeline_produces_plan_evidence_and_xlsx(tmp_path: Path):
    path = tmp_path / "faturas.xlsx"
    _make_workbook(path)
    result, recorder = _run_pipeline(path)

    assert result.ok is True
    assert result.status in {"completed", "completed_with_warnings"}
    # one SheetPlan per sheet (hidden preserved, flagged by an audit issue)
    names = [sheet.name for sheet in result.plan.sheets]
    assert names == ["Faturas", "Oculta", "Vazia"]
    faturas = result.plan.sheets[0]
    assert faturas.kind == "table"
    assert faturas.headers == ["Documento", "Valor"]
    # original values preserved: leading zeros stay text (grid excludes header)
    assert faturas.rows[0][0].original == "00123456789"
    # real cell evidence, never a fabricated bbox
    assert faturas.rows[1][0].bbox is None
    assert faturas.rows[1][0].page_number == 1
    assert faturas.rows[1][0].element_id
    # formula without cache becomes text with a warning, no invented result
    codes = _summary_codes(recorder)
    assert "FORMULA_WITHOUT_CACHE" in codes
    assert "HIDDEN_SHEET" in codes
    assert "EMPTY_SHEET" in codes
    assert result.xlsx is not None and result.xlsx.sheets >= 1


def test_xlsx_pipeline_ir_tables_enable_csv_and_txt(tmp_path: Path):
    path = tmp_path / "faturas.xlsx"
    _make_workbook(path)
    result, _ = _run_pipeline(path)
    formats = {item.format for item in result.exports}
    assert "txt" in formats
    assert "csv" in formats
    table = result.ir.tables[0]
    assert table.rows[1][0] == "00123456789"
    assert table.strategy == "spreadsheet"


def test_csv_pipeline_preserves_multiline_and_bom(tmp_path: Path):
    path = tmp_path / "dados.csv"
    _make_csv(path)
    options = PipelineOptions()
    options.spreadsheet = {
        "csv_encoding": "utf-8-sig",
        "csv_delimiter": ";",
        "sheet_names": None,
        "header_row": None,
        "formula_mode": "cached_or_text",
        "reader_version": "spreadsheet-reader-v1",
    }
    result, _ = _run_pipeline(path, options)
    assert result.ok is True
    sheet = result.plan.sheets[0]
    assert sheet.rows[0][1].display == "linha um\ne dois"


def test_sheet_selection_missing_sheet_fails_explicitly(tmp_path: Path):
    path = tmp_path / "faturas.xlsx"
    _make_workbook(path)
    options = PipelineOptions()
    options.spreadsheet = {
        "sheet_names": ["Resumo ausente"],
        "header_row": None,
        "csv_encoding": None,
        "csv_delimiter": None,
        "formula_mode": "cached_or_text",
        "reader_version": "spreadsheet-reader-v1",
    }
    result, _ = _run_pipeline(path, options)
    assert result.ok is False
    assert result.error is not None and result.error.code == "SPREADSHEET_SHEET_NOT_FOUND"


def test_empty_workbook_publishes_with_warning(tmp_path: Path):
    path = tmp_path / "vazia.xlsx"
    wb = Workbook()
    wb.create_sheet("Vazia")
    wb.save(path)
    result, recorder = _run_pipeline(path)
    assert result.ok is True
    assert "EMPTY_SHEET" in _summary_codes(recorder)
    assert result.xlsx is not None


def test_generated_xlsx_round_trips_values(tmp_path: Path):
    path = tmp_path / "faturas.xlsx"
    _make_workbook(path)
    result, _ = _run_pipeline(path)
    out = Path(tmp_path / "out.xlsx")
    out.write_bytes(result.xlsx.data)
    wb = load_workbook(out)
    assert wb["Faturas"]["A2"].value == "00123456789"
    assert wb["Faturas"]["B2"].value == 1234.56
