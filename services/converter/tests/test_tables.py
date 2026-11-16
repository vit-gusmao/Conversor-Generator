"""Table detection: lattice, stream, headers, transposition, continuations."""

from __future__ import annotations

import pytest

from audit.events import AuditRecorder
from engine.extraction.native_text import extract_native
from engine.layout.blocks import build_layout
from engine.layout.reading_order import assign_reading_order
from engine.tables.table_builder import detect_tables


def tables_for(path):
    import pdfplumber

    recorder = AuditRecorder(run_id="test_tables")
    with pdfplumber.open(str(path)) as pdf:
        found = []
        for index, page in enumerate(pdf.pages, start=1):
            native = extract_native(page, index)
            layout = build_layout(native.tokens, index, native.width, native.height)
            blocks = assign_reading_order(layout.blocks, native.width, native.height)
            found.extend(
                detect_tables(
                    page,
                    index,
                    native.tokens,
                    blocks,
                    rotation=native.rotation,
                    page_width=native.width,
                    recorder=recorder,
                )
            )
        return found


def test_bordered_table_is_reconstructed(pdf_paths):
    tables = tables_for(pdf_paths["bordered_table"])
    assert len(tables) == 1
    table = tables[0]
    assert table.strategy in {"lattice", "hybrid"}
    assert table.headers == ["Nome", "Cargo", "Salário"]
    assert table.rows == [
        ["João da Silva", "Analista", "R$ 3.500,00"],
        ["Maria Oliveira", "Assistente", "R$ 2.800,00"],
        ["Pedro Santos", "Supervisor", "R$ 4.200,00"],
    ]


def test_borderless_table_uses_alignment(pdf_paths):
    tables = tables_for(pdf_paths["borderless_table"])
    assert len(tables) == 1
    assert tables[0].strategy == "stream"
    assert tables[0].headers == ["PRODUTO", "QTD", "PREÇO"]
    assert len(tables[0].rows) == 4
    assert tables[0].rows[3][0] == "Cabo HDMI"


def test_two_column_labels_become_transposed(pdf_paths):
    tables = tables_for(pdf_paths["two_columns"])
    assert len(tables) == 1
    assert tables[0].strategy == "transposed"
    assert tables[0].headers == []


def test_transposed_table_detection(pdf_paths):
    tables = tables_for(pdf_paths["transposed_table"])
    assert tables and tables[0].strategy == "transposed"


def test_prose_is_not_a_table(pdf_paths):
    assert tables_for(pdf_paths["free_text"]) == []


def test_form_with_colons_is_not_a_table(pdf_paths):
    assert tables_for(pdf_paths["fields_below"]) == []


def test_multipage_tables_are_consolidated(pdf_paths, run_pdf):
    result, _ = run_pdf("multipage_table")
    table_sheets = [s for s in result.plan.sheets if s.kind == "table"]
    assert len(table_sheets) == 1
    sheet = table_sheets[0]
    assert sheet.n_rows == 6
    assert sheet.source_pages == [1, 2, 3]
    names = [row[0].value for row in sheet.rows if row[0]]
    assert names == ["João", "Maria", "Pedro", "Ana", "Carla", "Bruno"]


def test_repeated_header_is_not_a_record(pdf_paths, run_pdf):
    result, _ = run_pdf("multipage_table")
    header_sheet = [s for s in result.plan.sheets if s.kind == "table"][0]
    first_cells = [row[0].value for row in header_sheet.rows if row[0]]
    assert "Nome" not in first_cells


def test_no_duplicate_table_sheets_for_bordered_document(pdf_paths, run_pdf):
    result, _ = run_pdf("bordered_table")
    assert [s.name for s in result.plan.sheets] == ["Tabela 1"]


@pytest.mark.parametrize("fixture", ["bordered_table", "borderless_table", "multipage_table"])
def test_tables_have_provenance(pdf_paths, run_pdf, fixture):
    result, _ = run_pdf(fixture)
    sheet = [s for s in result.plan.sheets if s.kind == "table"][0]
    cells = [cell for row in sheet.rows for cell in row if cell is not None]
    assert cells
    assert all(cell.table_id for cell in cells)
    assert all(cell.element_id for cell in cells)
