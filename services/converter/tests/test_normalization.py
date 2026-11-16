"""Normalisation rules — the part that decides Excel cell types."""

from __future__ import annotations

from datetime import date

import pytest

from engine.normalization.normalizer import LocaleProfile, detect_locale, normalize

PT = LocaleProfile(decimal_sep=",", thousand_sep=".", date_order="dmy")
EN = LocaleProfile(decimal_sep=".", thousand_sep=",", date_order="mdy")


@pytest.mark.parametrize(
    ("raw", "expected_type", "expected_value"),
    [
        ("João da Silva", "text", "João da Silva"),
        ("25", "integer", 25),
        ("25 anos", "text", "25 anos"),
        ("R$ 3.500,00", "money", 3500.0),
        ("1.250,75", "decimal", 1250.75),
        ("12,5%", "percentage", 0.125),
        ("001245", "code", "001245"),
        ("123.456.789-00", "identifier", "123.456.789-00"),
        ("15/05/2000", "date", date(2000, 5, 15)),
        ("01/2026", "competencia", "01/2026"),
        ("-350,00", "decimal", -350.0),
        ("(350,00)", "money", -350.0),
        ("", "empty", None),
    ],
)
def test_pt_br_values(raw, expected_type, expected_value):
    result = normalize(raw, locale=PT)
    assert result.value_type == expected_type, (raw, result.value_type, result.display)
    if expected_type == "money":
        assert result.value == pytest.approx(expected_value)
        if any(symbol in raw for symbol in ("R$", "US$", "$", "€", "£")):
            assert "R$" in result.display
    else:
        assert result.value == expected_value


def test_original_text_is_always_preserved():
    result = normalize("R$ 3.500,00", locale=PT)
    assert result.original == "R$ 3.500,00"
    assert result.value == pytest.approx(3500.0)


def test_identifier_keeps_leading_zeros_even_with_code_header():
    result = normalize("000123", header="Matrícula", locale=PT)
    assert result.value == "000123"
    assert result.value_type in {"code", "identifier"}
    assert result.number_format == "@"


def test_document_header_forces_text_for_numeric_ids():
    result = normalize("12345678900", header="CPF", locale=PT)
    assert isinstance(result.value, str)
    assert result.value == "12345678900"


def test_date_order_detected_from_document():
    texts = ["15/05/2000", "31/12/1999", "02/01/2020"]
    profile = detect_locale(texts)
    assert profile.date_order == "dmy"

    texts_en = ["05/15/2000", "12/31/1999", "01/02/2020"]
    profile_en = detect_locale(texts_en)
    assert profile_en.date_order == "mdy"


def test_currency_symbols_and_formats():
    assert normalize("US$ 1,234.56", locale=EN).value == pytest.approx(1234.56)
    result = normalize("R$ 3.500,00", locale=PT)
    assert result.number_format == '"R$" #,##0.00'


def test_formula_injection_is_neutralised():
    result = normalize("=SUM(A1:A2)", locale=PT)
    assert result.value_type == "text"
    assert result.value == "=SUM(A1:A2)"
    assert any(t["rule"] == "formula_injection_guard" for t in result.transformations)


def test_percentage_uses_fraction_and_format():
    result = normalize("12,5%", locale=PT)
    assert result.value == pytest.approx(0.125)
    assert result.number_format == "0.00%"


def test_ambiguous_date_is_kept_as_text_when_invalid_for_locale():
    result = normalize("31/13/2020", locale=PT)
    assert result.value_type == "text"


def test_decimal_with_two_places_is_not_integer():
    assert normalize("150,00", locale=PT).value_type == "decimal"
    assert normalize("1.500,00", locale=PT).value == pytest.approx(1500.0)
