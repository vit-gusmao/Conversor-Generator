"""Regression tests for the rules discovered by the corpus stress.

Each test pins one behaviour that a real document needed; none of them depends on
a specific file — they are general rules of the engine.
"""

from __future__ import annotations

import io

from openpyxl import load_workbook

from engine.extraction.visibility import invisible_ratio
from engine.normalization.normalizer import normalize
from engine.tables.table_builder import _looks_like_prose, _split_shifted_cells
from engine.tables.text_pattern import detect_text_tables
from models.document import BBox, Token


def _token(text: str, x0: float, y0: float, x1: float, y1: float, index: int) -> Token:
    return Token(
        element_id=f"t{index}",
        text=text,
        bbox=BBox(x0, y0, x1, y1),
        page_number=1,
        origin="ocr",
        confidence=0.9,
        index=index,
    )


# --------------------------------------------------------------------------- #
# Visible text layer
# --------------------------------------------------------------------------- #


def test_invisible_render_mode_is_detected():
    # Text shown with `3 Tr` contributes nothing to the page a human sees.
    stream = b"BT /helv 10 Tf 3 Tr (Nome: Ana) Tj ET"
    assert invisible_ratio(stream) == 1.0


def test_visible_text_is_not_flagged():
    stream = b"BT /helv 10 Tf 0 Tr (Nome: Ana) Tj ET"
    assert invisible_ratio(stream) == 0.0


# --------------------------------------------------------------------------- #
# Number parsing
# --------------------------------------------------------------------------- #


def test_separator_is_decided_per_value():
    # The same document prints table prices with dots and money with commas.
    assert normalize("153,18").value == 153.18
    assert normalize("308.36").value == 308.36
    assert normalize("R$ 15.318,00").value == 15318.0
    assert normalize("1.234.567,89").value == 1234567.89


def test_numbers_beyond_excel_precision_stay_text():
    result = normalize("171.755.343.515.169.275,00")
    assert result.value_type == "code"
    assert result.number_format == "@"
    assert result.value == "171.755.343.515.169.275,00"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def test_prose_in_columns_is_not_a_table():
    rows = [
        ["Este documento foi criado para testes de conversao de PDF", "As informacoes sao ficticias e representam formatos"],
        ["O OCR deve conservar a ordem de leitura e os valores", "Todos os dados deste arquivo sao inventados para teste"],
    ]
    assert _looks_like_prose(rows) is True


def test_table_with_numbers_is_not_prose():
    rows = [["01", "Licenca de software", "8", "89.37", "714.96"]]
    assert _looks_like_prose(rows) is False


def test_shifted_row_is_pushed_back_into_its_column():
    # The quantity stuck to the description and every row lost one boundary.
    rows = [
        ["Item", "Descricao", "Qtd", "Preco", "Total"],
        ["01", "Treinamento 2", "", "376,92", "753,84"],
        ["02", "Deslocamento 9", "", "5,83", "52,47"],
    ]
    _split_shifted_cells(rows)
    assert rows[1] == ["01", "Treinamento", "2", "376,92", "753,84"]
    assert rows[2] == ["02", "Deslocamento", "9", "5,83", "52,47"]


def test_text_pattern_rebuilds_a_table_without_geometry():
    tokens = [
        _token("Item", 10, 10, 40, 20, 1),
        _token("Descricao", 50, 10, 120, 20, 2),
        _token("Qtd", 130, 10, 150, 20, 3),
        _token("Preco", 160, 10, 200, 20, 4),
        _token("Total", 210, 10, 250, 20, 5),
    ]
    index = 6
    for row, (item, description, quantity, price, total) in enumerate(
        [
            ("01", "Servico", "8", "89.37", "714.96"),
            ("02", "Deslocamento", "5", "240.45", "1202.25"),
        ],
        start=1,
    ):
        top = 10 + row * 12
        for text, left in (
            (item, 10),
            (description, 50),
            (quantity, 130),
            (price, 160),
            (total, 210),
        ):
            tokens.append(_token(text, left, top, left + 30, top + 10, index))
            index += 1

    tables = detect_text_tables(tokens, 1)
    assert len(tables) == 1
    table = tables[0]
    assert table.strategy == "text_pattern"
    assert table.rows[0] == ["Item", "Descricao", "Qtd", "Preco", "Total"]
    assert table.rows[1] == ["01", "Servico", "8", "89.37", "714.96"]
    assert table.rows[2] == ["02", "Deslocamento", "5", "240.45", "1202.25"]


# --------------------------------------------------------------------------- #
# Workbook contract
# --------------------------------------------------------------------------- #


def test_workbook_keeps_a_number_that_excel_cannot_represent():
    from engine.excel.writer import write_workbook
    from models.document import CellPlan, SheetPlan, WorkbookPlan

    value = "171.755.343.515.169.275,00"
    sheet = SheetPlan(name="Dados", kind="records", headers=["Valor"])
    sheet.rows.append(
        [
            CellPlan(
                value=value,
                display=value,
                value_type="code",
                header="Valor",
                original=value,
                number_format="@",
            )
        ]
    )
    result = write_workbook(WorkbookPlan(sheets=[sheet]))
    workbook = load_workbook(io.BytesIO(result.data))
    cell = workbook["Dados"]["A2"]
    assert cell.value == value
    assert cell.data_type == "s"
