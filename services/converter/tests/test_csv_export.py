"""CSV export: profiles, escaping, formula policy and multi-table ZIP."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from exports.base import ExportError, ExportOptions
from exports.csv_writer import export_csv
from models.document import BBox, DocumentIR, TableStructure


def _ir(*tables: TableStructure) -> DocumentIR:
    return DocumentIR(
        file_name="dados.pdf", sha256="abc", page_count=1, tables=list(tables)
    )


def _table(table_id: str, rows: list[list[str]], headers: list[str]) -> TableStructure:
    return TableStructure(
        table_id=table_id,
        page_number=1,
        bbox=BBox.empty(),
        strategy="lattice",
        rows=rows,
        headers=headers,
    )


def test_single_table_csv_interoperable_profile():
    ir = _ir(_table("t1", [["Ana", "1,50"]], ["Nome", "Valor"]))
    result = export_csv(ir, ExportOptions(format="csv"))
    assert result.filename.endswith(".csv")
    assert result.data[:3] != b"\xef\xbb\xbf"  # no BOM
    text = result.data.decode("utf-8")
    assert text.startswith("Nome,Valor\r\n")
    assert 'Ana,"1,50"' in text  # the decimal comma is preserved inside quotes


def test_excel_br_profile_uses_semicolon_and_bom():
    ir = _ir(_table("t1", [["Ana", "1,50"]], ["Nome", "Valor"]))
    result = export_csv(ir, ExportOptions(format="csv", csv_profile="excel_br"))
    assert result.data.startswith(b"\xef\xbb\xbf")
    text = result.data.decode("utf-8-sig")
    assert "Ana;1,50" in text


def test_formula_neutralisation_does_not_break_negative_numbers():
    ir = _ir(
        _table(
            "t1",
            [["=2+2", "-10", "+55", "@cmd", "  =SUM(A1)"]],
            ["a", "b", "c", "d", "e"],
        )
    )
    result = export_csv(ir, ExportOptions(format="csv", formula_policy="safe"))
    text = result.data.decode("utf-8")
    assert "'=2+2" in text
    assert "'+55" in text
    assert "'@cmd" in text
    assert "'  =SUM(A1)" in text
    assert ",-10," in text  # a typed negative number is not a formula
    assert any(item["code"] == "CSV_FORMULA_NEUTRALIZED" for item in result.warnings)


def test_raw_policy_keeps_formulas():
    ir = _ir(_table("t1", [["=2+2"]], ["a"]))
    result = export_csv(ir, ExportOptions(format="csv", formula_policy="raw"))
    assert "=2+2" in result.data.decode("utf-8")
    assert result.warnings == []


def test_multiple_tables_produce_zip_with_manifest():
    ir = _ir(
        _table("t1", [["1"]], ["A"]),
        _table("t2", [["2"]], ["B"]),
    )
    result = export_csv(ir, ExportOptions(format="csv"))
    assert result.filename.endswith(".zip")
    archive = zipfile.ZipFile(io.BytesIO(result.data))
    names = archive.namelist()
    assert "manifest.json" in names
    assert len([name for name in names if name.endswith(".csv")]) == 2
    manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest["tables"]) == 2
    assert len(manifest["tables"]) == len(manifest["files"])


def test_no_tables_raises_specific_error():
    with pytest.raises(ExportError) as info:
        export_csv(_ir(), ExportOptions(format="csv"))
    assert info.value.code == "NO_TABLES_FOUND"
    assert "txt" in info.value.details["available_formats"]


def test_selected_table_only():
    ir = _ir(_table("t1", [["1"]], ["A"]), _table("t2", [["2"]], ["B"]))
    result = export_csv(ir, ExportOptions(format="csv", table_ids=["t2"]))
    assert result.filename.endswith(".csv")
    assert "B" in result.data.decode("utf-8")
    assert "A" not in result.data.decode("utf-8")
