"""Unit tests for label/value segmentation."""

from __future__ import annotations

import pytest

from engine.fields.label_detector import is_label_like, label_only, split_label_value
from models.document import BBox, Line, Token


def make_line(parts: list[tuple[str, float, float]], y: float = 100.0, size: float = 11.0, bold: bool = False) -> Line:
    """parts = [(text, x0, x1), ...] on a single baseline."""
    tokens = []
    for index, (text, x0, x1) in enumerate(parts):
        tokens.append(
            Token(
                element_id=f"t{index}",
                text=text,
                bbox=BBox(x0, y, x1, y + size),
                page_number=1,
                font="Helvetica-Bold" if bold else "Helvetica",
                size=size,
                index=index,
            )
        )
    box = BBox.union_all(t.bbox for t in tokens)
    return Line(words=tokens, bbox=box)


def words_from(text: str, start: float = 72.0, size: float = 11.0) -> list[tuple[str, float, float]]:
    parts = []
    x = start
    for word in text.split():
        width = len(word) * size * 0.5
        parts.append((word, x, x + width))
        x += width + size * 0.28
    return parts


def test_label_like_shapes():
    assert is_label_like("Nome")
    assert is_label_like("Data de nascimento")
    assert is_label_like("Código de Ocorrência")
    assert not is_label_like("123456")
    assert not is_label_like("R$ 3.500,00")
    assert not is_label_like("• item da lista")
    assert not is_label_like("")


def test_single_pair_same_line():
    line = make_line(words_from("Nome: João da Silva"))
    segments = split_label_value(line)
    assert len(segments) == 1
    assert segments[0].label == "Nome"
    assert segments[0].value == "João da Silva"
    assert "same_line" in segments[0].reason_codes


def test_four_pairs_on_one_line():
    line = make_line(
        words_from("Nome: João da Silva Matrícula: 001245 Cargo: Analista de Dados Cidade: Belo Horizonte")
    )
    segments = split_label_value(line)
    pairs = [(s.label, s.value) for s in segments]
    assert pairs == [
        ("Nome", "João da Silva"),
        ("Matrícula", "001245"),
        ("Cargo", "Analista de Dados"),
        ("Cidade", "Belo Horizonte"),
    ]


def test_equals_separator():
    line = make_line(words_from("Protocolo = 2026-0001"))
    segments = split_label_value(line)
    assert segments and segments[0].label == "Protocolo"
    assert segments[0].value == "2026-0001"
    assert segments[0].separator == "equals"


def test_time_is_not_a_separator():
    line = make_line(words_from("Início 10:30 Fim 11:45"))
    assert split_label_value(line) == []


def test_label_only_line():
    line = make_line(words_from("Nome:"))
    segment = label_only(line)
    assert segment is not None
    assert segment.label == "Nome"
    assert segment.value == ""
    assert "value_below" in segment.reason_codes


def test_multiline_label_at_start_includes_full_label():
    line = make_line(words_from("Receita do Mercado Externo: R$ 15.000,00"))
    segments = split_label_value(line)
    assert segments[0].label == "Receita do Mercado Externo"
    assert segments[0].value == "R$ 15.000,00"


def test_font_transition_is_recorded():
    parts = [("Nome:", 72.0, 105.0), ("João", 120.0, 145.0)]
    tokens = [
        Token("a", "Nome:", BBox(72, 100, 105, 111), 1, font="Helvetica-Bold", size=11),
        Token("b", "João", BBox(120, 100, 145, 111), 1, font="Helvetica", size=11),
    ]
    line = Line(words=tokens, bbox=BBox(72, 100, 145, 111))
    segments = split_label_value(line)
    assert segments[0].label == "Nome"
    assert "font_style_transition" in segments[0].reason_codes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Nome: João da Silva Idade: 25 anos", [("Nome", "João da Silva"), ("Idade", "25 anos")]),
        ("CPF: 123.456.789-00 RG: 12.345.678-9", [("CPF", "123.456.789-00"), ("RG", "12.345.678-9")]),
    ],
)
def test_dense_pairs(text, expected):
    line = make_line(words_from(text))
    assert [(s.label, s.value) for s in split_label_value(line)] == expected


def make_block(element_id: str, text: str, page: int, order: int, kind: str = "paragraph"):
    from models.document import TextBlock

    line = make_line(words_from(text))
    return TextBlock(
        element_id=element_id,
        page_number=page,
        bbox=line.bbox,
        text=text,
        lines=[line],
        kind=kind,
        reading_order=order,
    )


def test_field_line_is_not_a_section_title():
    """`Nome: João` is a field, never a section header.

    Regression: it used to become the section of the next block, which split a
    single record into two sheets named after the field value.
    """
    from engine.fields.field_extractor import section_map

    blocks = [
        make_block("b1", "Nome: João", page=1, order=0),
        make_block("b2", "Cargo: Analista", page=1, order=1),
    ]
    sections = section_map(blocks)
    assert sections["b1"] is None
    assert sections["b2"] is None


def test_section_titles_still_work():
    from engine.fields.field_extractor import section_map

    blocks = [
        make_block("b1", "DADOS PESSOAIS", page=1, order=0),
        make_block("b2", "Nome: João", page=1, order=1),
        make_block("b3", "Pagamentos", page=1, order=2),
    ]
    sections = section_map(blocks)
    assert sections["b2"] == "DADOS PESSOAIS"
    assert sections["b3"] == "DADOS PESSOAIS"


def test_sections_do_not_bleed_across_pages():
    """`reading_order` restarts per page; the section map must respect the page."""
    from engine.fields.field_extractor import section_map

    blocks = [
        make_block("p1t", "SECAO A", page=1, order=0),
        make_block("p1f", "Nome: João", page=1, order=1),
        make_block("p2t", "SECAO B", page=2, order=0),
        make_block("p2f", "Nome: Maria", page=2, order=1),
    ]
    sections = section_map(blocks)
    assert sections["p1f"] == "SECAO A"
    assert sections["p2f"] == "SECAO B"

