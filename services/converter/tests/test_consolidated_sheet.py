"""The download is always one workbook.

Independent tables keep their own tabs — merging them blindly would mix
unrelated rows — and one extra tab shows every record/table row with the union
of all columns, so the whole document can be sorted and filtered at once.
"""

from __future__ import annotations

import io
import zipfile

from openpyxl import load_workbook


def _structural(result):
    return [s for s in result.plan.sheets if s.kind in {"records", "table"}]


def _consolidated(result):
    return [s for s in result.plan.sheets if s.kind == "consolidated"]


def test_consolidated_sheet_holds_union_of_columns(run_pdf):
    result, _ = run_pdf("mixed_report")
    sheets = _consolidated(result)
    assert len(sheets) == 1, [sheet.kind for sheet in result.plan.sheets]
    sheet = sheets[0]

    assert sheet.name == "Todos os dados"
    assert sheet.headers[:2] == ["Aba", "Página"]
    assert len(set(sheet.headers)) == len(sheet.headers), "headers must be unique"

    structural = _structural(result)
    # one consolidated column per distinct source column, plus the two provenance ones
    union = {header.lower() for source in structural for header in source.headers}
    assert len(sheet.headers) == 2 + len(union)
    for header in union:
        assert header in {h.lower() for h in sheet.headers}

    # one row per structural row, and the origin tab is always named
    assert len(sheet.rows) == sum(source.n_rows for source in structural)
    assert {row[0].value for row in sheet.rows} == {source.name for source in structural}


def test_consolidated_row_keeps_values_and_provenance(run_pdf):
    result, _ = run_pdf("mixed_report")
    sheet = _consolidated(result)[0]
    index = {header: position for position, header in enumerate(sheet.headers)}

    record_row = next(row for row in sheet.rows if row[index["Matrícula"]] is not None)
    assert record_row[index["Matrícula"]].value == "001245"
    # provenance follows the cell, not the derived sheet
    assert record_row[index["Matrícula"]].element_id is not None

    table_row = next(row for row in sheet.rows if row[index["Mês"]] is not None)
    assert table_row[index["Salário"]].value_type == "money"


def test_single_table_document_gets_no_duplicate_tab(run_pdf):
    result, _ = run_pdf("bordered_table")
    assert _consolidated(result) == []
    assert len(result.plan.sheets) == 1


def test_workbook_is_one_file_with_every_tab(run_pdf):
    result, _ = run_pdf("mixed_report")
    assert result.xlsx is not None
    workbook = load_workbook(io.BytesIO(result.xlsx.data))
    names = workbook.sheetnames
    # the consolidated tab opens first; the origin tabs remain untouched
    assert names[0] == "Todos os dados"
    assert {"Todos os dados"} | {source.name for source in _structural(result)} <= set(names)
    with zipfile.ZipFile(io.BytesIO(result.xlsx.data)) as archive:
        assert not [name for name in archive.namelist() if name.startswith("xl/media/")]
