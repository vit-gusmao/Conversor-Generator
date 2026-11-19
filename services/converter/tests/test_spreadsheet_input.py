"""T3 — spreadsheet input detection, safety budgets and CSV dialect handling."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from ingestion.base import IngestionError
from ingestion.spreadsheet_input import (
    SPREADSHEET_READER_VERSION,
    SpreadsheetOptions,
    probe_spreadsheet,
    read_spreadsheet,
)


def _make_xlsx(path: Path, *, hidden_sheet: bool = True) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Faturas"
    ws.append(["Documento", "Valor"])
    ws.append(["00123456789", 1234.56])
    ws.append(["2026-11-19", None])
    # formula without cached value: openpyxl stores the formula text only
    ws["B4"] = "=SUM(B2:B3)"
    ws.merge_cells("A5:B5")
    ws["A5"] = "Total consolidado"

    summary = wb.create_sheet("Resumo")
    summary.append(["Chave", "Qtd"])
    summary.append(["A", 2])

    if hidden_sheet:
        hidden = wb.create_sheet("Oculta")
        hidden.append(["x"])
        hidden.sheet_state = "hidden"

    wb.create_sheet("Vazia")  # empty sheet is valid, with a warning
    wb.save(path)


def test_probe_accepts_real_xlsx(tmp_path: Path):
    path = tmp_path / "planilha.xlsx"
    _make_xlsx(path)
    probe = probe_spreadsheet(path, "xlsx", None)
    assert probe.kind == "spreadsheet"
    assert probe.format == "xlsx"
    assert probe.supported is True
    assert "Faturas" in probe.sheet_names
    assert "Oculta" in probe.sheet_names


def test_probe_rejects_generic_zip_renamed_to_xlsx(tmp_path: Path):
    path = tmp_path / "fake.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("arquivo.txt", "isso não é uma planilha")
    probe = probe_spreadsheet(path, "xlsx", None)
    assert probe.supported is False
    assert probe.reason_code == "NOT_SPREADSHEET"


def test_probe_rejects_corrupted_xlsx(tmp_path: Path):
    path = tmp_path / "quebrada.xlsx"
    _make_xlsx(path)
    data = bytearray(path.read_bytes())
    data[100:200] = b"\x00" * 100
    path.write_bytes(bytes(data))
    probe = probe_spreadsheet(path, "xlsx", None)
    assert probe.supported is False
    assert probe.reason_code in {"NOT_SPREADSHEET", "SPREADSHEET_CORRUPT"}


@pytest.mark.parametrize("suffix", ["xls", "xlsm", "xlsb", "ods"])
def test_unsupported_formats_fail_explicitly(tmp_path: Path, suffix: str):
    path = tmp_path / f"plan.{suffix}"
    path.write_bytes(b"qualquer coisa")
    with pytest.raises(IngestionError) as excinfo:
        probe_spreadsheet(path, suffix, None)
    assert excinfo.value.code == "UNSUPPORTED_SPREADSHEET_FORMAT"


def test_xlsx_zip_budget_rejects_expansion_bomb(tmp_path: Path):
    path = tmp_path / "bomba.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", "<z>" + "0" * 2_000_000 + "</z>")
    with pytest.raises(IngestionError) as excinfo:
        probe_spreadsheet(path, "xlsx", None)
    assert excinfo.value.code == "SPREADSHEET_BUDGET_EXCEEDED"


def test_read_xlsx_preserves_values_addresses_and_warnings(tmp_path: Path):
    path = tmp_path / "planilha.xlsx"
    _make_xlsx(path)
    doc = read_spreadsheet(path, SpreadsheetOptions())
    names = [sheet.name for sheet in doc.sheets]
    assert names == ["Faturas", "Resumo", "Oculta", "Vazia"]
    faturas = doc.sheets[0]
    assert faturas.rows[1][0].original == "00123456789"  # leading zeros kept as text
    assert faturas.rows[1][0].cell_address == "A2"
    assert faturas.rows[1][1].value == 1234.56
    formula_cell = faturas.rows[3][1]
    assert formula_cell.formula_text == "=SUM(B2:B3)"
    assert formula_cell.value is None  # no cached result: never invent one
    assert any(w["code"] == "FORMULA_WITHOUT_CACHE" for w in doc.warnings)
    assert any(w["code"] == "HIDDEN_SHEET" for w in doc.warnings)
    assert any(w["code"] == "EMPTY_SHEET" for w in doc.warnings)
    assert any(
        cell.is_merged_anchor or cell.merged_in_anchor is not None
        for row in faturas.rows
        for cell in row
        if cell is not None
    )
    assert doc.reader_version == SPREADSHEET_READER_VERSION


def test_sheet_selection_filters_by_name(tmp_path: Path):
    path = tmp_path / "planilha.xlsx"
    _make_xlsx(path)
    doc = read_spreadsheet(path, SpreadsheetOptions(sheet_names=["Resumo"]))
    assert [sheet.name for sheet in doc.sheets] == ["Resumo"]


def _write_csv(path: Path, encoding: str = "utf-8-sig") -> None:
    content = "Nome;Observação\nAna;\"primeira linha\ne segunda linha\"\nBruno;ok\n"
    path.write_bytes(content.encode(encoding))


def test_read_csv_bom_semicolon_and_multiline(tmp_path: Path):
    path = tmp_path / "dados.csv"
    _write_csv(path)
    doc = read_spreadsheet(
        path,
        SpreadsheetOptions(csv_encoding="utf-8-sig", csv_delimiter=";"),
    )
    sheet = doc.sheets[0]
    assert sheet.name == "dados"
    assert sheet.rows[0][0].value == "Nome"
    assert sheet.rows[1][1].value == "primeira linha\ne segunda linha"
    # provenance: physical line range of the multiline record
    assert sheet.rows[1][1].physical_lines == (2, 3)
    assert doc.dialect == ";"
    assert doc.detected_encoding == "utf-8-sig"


def test_csv_unknown_encoding_is_explicit(tmp_path: Path):
    path = tmp_path / "dados.csv"
    path.write_bytes(b"Nome;Nota\nAna;10\n")
    with pytest.raises(IngestionError) as excinfo:
        read_spreadsheet(path, SpreadsheetOptions(csv_encoding="latin-42"))
    assert excinfo.value.code == "INVALID_PAYLOAD" or excinfo.value.code == "CSV_ENCODING_REQUIRED"


def test_csv_ambiguous_dialect_requires_explicit_choice(tmp_path: Path):
    path = tmp_path / "ambiguo.csv"
    path.write_bytes("a;b,c\nd;e,f\n".encode("utf-8"))
    probe = probe_spreadsheet(path, "csv", None)
    assert probe.supported is False
    assert probe.reason_code == "CSV_DIALECT_REQUIRED"


def test_csv_non_utf8_without_declaration_requires_encoding(tmp_path: Path):
    path = tmp_path / "cp1252.csv"
    path.write_bytes("Nome;Cidade\nAna;São Paulo\n".encode("cp1252"))
    probe = probe_spreadsheet(path, "csv", None)
    assert probe.supported is False
    assert probe.reason_code == "CSV_ENCODING_REQUIRED"


def test_negative_number_and_formula_prefix_stay_text(tmp_path: Path):
    path = tmp_path / "seguro.csv"
    path.write_bytes("txt;num\n=cmd();-5\n".encode("utf-8"))
    doc = read_spreadsheet(
        path, SpreadsheetOptions(csv_encoding="utf-8", csv_delimiter=";")
    )
    row = doc.sheets[0].rows[1]
    assert row[0].value == "=cmd()"
    assert row[1].value == "-5"


def test_far_away_declared_cell_is_budget_rejected_not_oom(tmp_path: Path):
    """A tiny file declaring XFD1048576 must be rejected, never allocate it."""
    path = tmp_path / "declarada.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Extrema"
    ws["A1"] = "ok"
    ws["XFD1048576"] = "fim"
    wb.save(path)

    probe = probe_spreadsheet(path, "xlsx", None)
    assert probe.supported is True

    with pytest.raises(IngestionError) as excinfo:
        read_spreadsheet(path, SpreadsheetOptions())
    assert excinfo.value.code == "SPREADSHEET_BUDGET_EXCEEDED"
    assert excinfo.value.details["limit_area"] == 50_000_000


def test_wide_but_legit_sheet_reads_with_ragged_rows(tmp_path: Path):
    """A normal file with a wide column keeps working (rows are ragged)."""
    path = tmp_path / "larga.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "cabecalho"
    ws["Z2"] = "valor distante"
    wb.save(path)
    doc = read_spreadsheet(path, SpreadsheetOptions())
    sheet = doc.sheets[0]
    assert len(sheet.rows) == 2
    assert len(sheet.rows[0]) == 1  # not padded to column Z
    assert sheet.rows[1][25].value == "valor distante"
