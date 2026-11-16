"""Regression tests for defects found in the full project audit.

Each test names the defect it locks down, so a future change that reintroduces
it fails here instead of silently corrupting a conversion.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

from audit.coverage import aggregate_coverage, build_coverage
from engine.excel.writer import write_workbook
from engine.fields.record_grouper import group_records
from engine.normalization.normalizer import detect_locale, normalize
from engine.structure.resolver import _residual_text
from engine.tables.stream import detect_stream
from models.document import (
    DEST_FIELD,
    BBox,
    CellPlan,
    CoverageRow,
    Field,
    Line,
    PageResult,
    SheetPlan,
    TextBlock,
    Token,
    WorkbookPlan,
)


def _plan_with(values: list[str]) -> WorkbookPlan:
    sheet = SheetPlan(name="Dados", kind="records", headers=["Texto"])
    for value in values:
        sheet.rows.append(
            [CellPlan(value=value, display=value, value_type="text", header="Texto", original=value)]
        )
    return WorkbookPlan(sheets=[sheet])


# ---------------------------------------------------------------------------
# Excel: Formula Injection (was: text starting with "=" became a live formula)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", ["=SUM(1,2)", "+1+1", "-2+2", "@SUM(A1)"])
def test_dangerous_text_is_written_as_literal_string(payload):
    result = write_workbook(_plan_with([payload]))
    sheet = load_workbook(io.BytesIO(result.data)).active
    cell = sheet["A2"]
    assert cell.data_type == "s", f"{payload!r} was written as {cell.data_type}"
    assert cell.value == payload


def test_formula_injection_does_not_execute_after_reopen():
    result = write_workbook(_plan_with(["=HYPERLINK(\"http://x\",\"y\")"]))
    cell = load_workbook(io.BytesIO(result.data)).active["A2"]
    assert cell.data_type == "s"
    assert cell.value.startswith("=")


def test_truncated_cells_report_their_references():
    long_text = "x" * 33_000
    result = write_workbook(_plan_with([long_text]))
    assert result.truncated_cells == 1
    assert result.truncated_refs == ["Dados!A2"]


# ---------------------------------------------------------------------------
# Coverage: a block with no destination must not count as covered
# ---------------------------------------------------------------------------

def _page_with_block(destination: str | None) -> PageResult:
    token = Token(
        element_id="tok_1",
        text="texto",
        bbox=BBox(0, 0, 10, 10),
        page_number=1,
    )
    block = TextBlock(
        element_id="blk_1",
        page_number=1,
        bbox=BBox(0, 0, 10, 10),
        text="texto",
        lines=[Line(words=[token], bbox=BBox(0, 0, 10, 10))],
        destination=destination,
    )
    page = PageResult(page_number=1, width=100, height=100, blocks=[block])
    return page


def test_block_without_destination_reduces_coverage():
    page = _page_with_block(destination=None)
    rows = build_coverage([page])
    stats = aggregate_coverage(rows)
    assert stats["dropped"] == 1.0
    assert stats["coverage_percent"] == 0.0


def test_explicitly_unclassified_block_still_counts_as_covered():
    page = _page_with_block(destination="unclassified")
    stats = aggregate_coverage(build_coverage([page]))
    assert stats["coverage_percent"] == 100.0


def test_destination_without_tokens_keeps_percent_sane():
    row = CoverageRow(page_number=1)
    assert row.coverage_percent == 100.0


# ---------------------------------------------------------------------------
# Fields: a repeated label inside one record must not split the record
# ---------------------------------------------------------------------------

def _field(label: str, value: str, y: float, index: int) -> Field:
    return Field(
        label=label,
        value=value,
        page_number=1,
        label_bbox=BBox(0, y, 40, y + 10),
        value_bbox=BBox(45, y, 100, y + 10),
        element_ids=[f"e{index}"],
        confidence=0.9,
        reason_codes=["same_line"],
    )


def test_repeated_label_stays_in_the_same_record():
    fields = [
        _field("Nome", "João", 10, 1),
        _field("Telefone", "31999990000", 22, 2),
        _field("Telefone", "31988880000", 34, 3),
    ]
    records = group_records(fields, page_number=1)
    assert len(records) == 1
    assert [f.label for f in records[0].fields] == ["Nome", "Telefone", "Telefone"]


def test_restarting_the_record_opens_a_new_one():
    fields = [
        _field("Nome", "João", 10, 1),
        _field("Cargo", "Analista", 22, 2),
        _field("Nome", "Maria", 34, 3),
        _field("Cargo", "Assistente", 46, 4),
    ]
    records = group_records(fields, page_number=1)
    assert len(records) == 2
    assert [r.fields[0].value for r in records] == ["João", "Maria"]


# ---------------------------------------------------------------------------
# Tables: one "Total:" cell must not discard the whole borderless table
# ---------------------------------------------------------------------------

def _table_token(text: str, x: float, y: float) -> Token:
    return Token(
        element_id=f"t_{text}_{x}_{y}",
        text=text,
        bbox=BBox(x, y, x + 40, y + 10),
        page_number=1,
    )


def test_table_with_a_total_row_is_still_a_table():
    tokens: list[Token] = []
    rows = [
        ("Produto", "Qtd", "Preço"),
        ("Teclado", "2", "150,00"),
        ("Mouse", "3", "80,00"),
        ("Total:", "", "230,00"),
    ]
    for row_index, cells in enumerate(rows):
        y = 100 + row_index * 20
        for col, text in enumerate(cells):
            if text:
                tokens.append(_table_token(text, 20 + col * 120, y))
    tables = detect_stream(tokens, page_number=1)
    assert tables, "a single 'Total:' cell must not disqualify the table"


def test_form_like_page_is_not_a_table():
    tokens: list[Token] = []
    for row_index, (label, value) in enumerate(
        [("Nome:", "João"), ("Cidade:", "BH"), ("Cargo:", "Analista"), ("Idade:", "30")]
    ):
        y = 100 + row_index * 20
        tokens.append(_table_token(label, 20, y))
        tokens.append(_table_token(value, 160, y))
    assert detect_stream(tokens, page_number=1) == []


# ---------------------------------------------------------------------------
# Structure: residual text of a partially used block is preserved
# ---------------------------------------------------------------------------

def test_residual_lines_are_kept_when_the_block_is_partially_used():
    line_used = Line(
        words=[Token(element_id="a", text="Nome: João", bbox=BBox(0, 0, 100, 10), page_number=1)],
        bbox=BBox(0, 0, 100, 10),
    )
    line_free = Line(
        words=[Token(element_id="b", text="Observação final.", bbox=BBox(0, 20, 100, 30), page_number=1)],
        bbox=BBox(0, 20, 100, 30),
    )
    block = TextBlock(
        element_id="blk",
        page_number=1,
        bbox=BBox(0, 0, 100, 30),
        text="Nome: João Observação final.",
        lines=[line_used, line_free],
    )
    residual = _residual_text(block, [BBox(0, 0, 100, 10)])
    assert residual == "Observação final."


# ---------------------------------------------------------------------------
# Normalization: currency symbol, header hint and ambiguous dates
# ---------------------------------------------------------------------------

@pytest.fixture()
def locale():
    return detect_locale(["15/03/2026", "R$ 1.234,56"])


@pytest.mark.parametrize(
    ("text", "symbol"),
    [("US$ 1,234.56", "US$"), ("€ 2.500,00", "€"), ("£ 10,00", "£"), ("R$ 3.500,00", "R$")],
)
def test_currency_symbol_is_preserved(locale, text, symbol):
    value = normalize(text, header="Valor", locale=locale)
    assert value.value_type == "money"
    assert value.display.startswith(symbol)
    assert symbol in (value.number_format or "")


def test_money_header_promotes_plain_number(locale):
    value = normalize("3500,00", header="Salário", locale=locale)
    assert value.value_type == "money"
    assert value.number_format and "R$" in value.number_format


def test_ambiguous_date_has_lower_confidence(locale):
    ambiguous = normalize("01/02/2026", header="Data", locale=locale)
    clear = normalize("15/03/2026", header="Data", locale=locale)
    assert ambiguous.value_type == clear.value_type == "date"
    assert ambiguous.confidence < clear.confidence


# ---------------------------------------------------------------------------
# Reconciliation: a legitimate row must survive, and the best target wins
# ---------------------------------------------------------------------------

def _table(table_id: str, page: int, headers: list[str], rows: list[list[str]]) -> object:
    from models.document import TableStructure

    return TableStructure(
        table_id=table_id,
        page_number=page,
        bbox=BBox(0, 0, 100, 100),
        strategy="stream",
        rows=rows,
        headers=headers,
    )


def test_repeated_data_row_is_not_deleted_across_pages():
    from engine.reconciliation.merger import _merge_tables

    page1 = _table("t1", 1, ["Nome", "Valor"], [["Ana", "10"], ["Bia", "20"]])
    page2 = _table("t2", 2, ["Nome", "Valor"], [["Bia", "20"], ["Caio", "30"]])
    merged = _merge_tables([page1, page2], None, [])
    assert len(merged) == 1
    names = [row[0] for row in merged[0].rows]
    # "Bia" opens page 2 and also closes page 1: it is a real record
    assert names == ["Ana", "Bia", "Bia", "Caio"]


def test_repeated_header_is_still_removed_across_pages():
    from engine.reconciliation.merger import _merge_tables

    page1 = _table("t1", 1, ["Nome", "Valor"], [["Ana", "10"]])
    page2 = _table("t2", 2, ["Nome", "Valor"], [["Nome", "Valor"], ["Bia", "20"]])
    merged = _merge_tables([page1, page2], None, [])
    assert len(merged) == 1
    assert [row[0] for row in merged[0].rows] == ["Ana", "Bia"]


def test_best_continuation_target_is_chosen():
    from engine.reconciliation.merger import _merge_tables

    # Two tables on page 1; page 2 continues the second one.
    a = _table("a", 1, ["Nome", "Valor"], [["Ana", "10"]])
    b = _table("b", 1, ["Produto", "Qtd", "Preço"], [["Mouse", "3", "80,00"]])
    c = _table("c", 2, ["Produto", "Qtd", "Preço"], [["Teclado", "2", "150,00"]])
    merged = _merge_tables([a, b, c], None, [])
    by_id = {t.table_id: t for t in merged}
    assert len(merged) == 2
    assert [row[0] for row in by_id["b"].rows] == ["Mouse", "Teclado"]
    assert [row[0] for row in by_id["a"].rows] == ["Ana"]


# ---------------------------------------------------------------------------
# Universal robustness: characters Excel cannot store, locale, wrapped rows
# ---------------------------------------------------------------------------

def test_control_characters_do_not_abort_the_workbook():
    payload = "WKFTQAHW\x000002"
    result = write_workbook(_plan_with([payload, "Nov 15\x00Dec 15"]))
    sheet = load_workbook(io.BytesIO(result.data)).active
    assert sheet["A2"].value == "WKFTQAHW\ufffd0002"
    assert result.sanitized_chars == 2
    assert result.sanitized_refs == ["Dados!A2", "Dados!A3"]


def test_sanitize_covers_the_full_illegal_set():
    from engine.excel.writer import sanitize_text

    for code in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)):
        cleaned = sanitize_text("a" + chr(code) + "b")
        assert cleaned == "a\ufffdb", f"0x{code:02x} não foi substituído"
    # legal characters must survive untouched
    assert sanitize_text("a\tb\nc\rd") == "a\tb\nc\rd"


def test_locale_detects_plain_decimals():
    from engine.normalization.normalizer import detect_locale

    english = detect_locale(["Invoice", "R$110.00", "R$110.00", "Total R$110.00"])
    assert english.decimal_sep == "."
    brazilian = detect_locale(["Nota fiscal", "R$ 110,00", "Total R$ 110,00"])
    assert brazilian.decimal_sep == ","


def test_wrapped_row_merges_but_a_two_cell_row_does_not():
    from engine.tables.table_builder import _merge_continuation_rows

    merged = _merge_continuation_rows(
        [["João", "Analista de"], ["", "Dados"]]
    )
    assert merged == [["João", "Analista de Dados"]]

    kept = _merge_continuation_rows(
        [["Claude Pro", "", "1"], ["", "Subtotal", "110"]]
    )
    assert kept == [["Claude Pro", "", "1"], ["", "Subtotal", "110"]]
