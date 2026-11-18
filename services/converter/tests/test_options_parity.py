"""Option parity between the public contract and the engine.

The same fixture is exercised on the TypeScript side
(`apps/api/src/modules/conversions/options.spec.ts`); both must agree on the
accepted shapes and on the rejection of any attempt to cap pages.
"""

from __future__ import annotations

import pytest

from api.service import _normalize_options
from api.deps import ApiError


def _code(exc: ApiError) -> str:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return str(detail.get("error", {}).get("code", ""))


def test_pdf_keeps_the_selected_formats():
    options = _normalize_options({"target_formats": ["txt"]}, "pdf")
    assert options["target_formats"] == ["txt"]


def test_pdf_defaults_to_all_formats():
    assert _normalize_options(None, "pdf")["target_formats"] == ["xlsx", "txt", "csv"]


def test_image_defaults_to_txt():
    assert _normalize_options(None, "image")["target_formats"] == ["txt"]


def test_image_with_xlsx_is_rejected():
    with pytest.raises(ApiError) as excinfo:
        _normalize_options({"target_formats": ["xlsx"]}, "image")
    assert _code(excinfo.value) == "UNSUPPORTED_TARGET"


@pytest.mark.parametrize("key", ["max_pages", "maxPages", "page_limit", "max_pages_per_document"])
def test_page_ceiling_is_always_an_invalid_option(key):
    with pytest.raises(ApiError) as excinfo:
        _normalize_options({key: 500}, "pdf")
    assert _code(excinfo.value) == "INVALID_OPTION"


def test_null_ceiling_is_not_a_ceiling():
    # Sending `max_pages: null` is the same as not sending it.
    assert _normalize_options({"max_pages": None}, "pdf")["schema_version"] == 2


def test_legacy_flat_shape_is_accepted():
    options = _normalize_options(
        {
            "target_formats": ["xlsx"],
            "ocr": False,
            "text_newline": "crlf",
            "page_separator": "marker",
            "csv_profile": "excel_br",
        },
        "pdf",
    )
    assert options["target_formats"] == ["xlsx"]
    assert options["ocr"]["enabled"] is False
    assert options["text"]["newline"] == "crlf"
    assert options["text"]["page_separator"] == "marker"
    assert options["csv"]["profile"] == "excel_br"


def test_unknown_option_is_rejected():
    with pytest.raises(ApiError) as excinfo:
        _normalize_options({"not_a_real_option": 1}, "pdf")
    assert _code(excinfo.value) == "INVALID_OPTION"


def test_unknown_nested_option_is_rejected():
    with pytest.raises(ApiError) as excinfo:
        _normalize_options({"text": {"invented": True}}, "pdf")
    assert _code(excinfo.value) == "INVALID_OPTION"


def test_nested_v2_shape_is_preserved():
    options = _normalize_options(
        {
            "schema_version": 2,
            "target_formats": ["txt", "csv"],
            "ocr": {"enabled": True, "languages": ["por"], "dpi": 200, "strategy": "ocr"},
            "text": {"newline": "lf", "page_separator": "none", "include_table_text": False},
            "csv": {
                "profile": "interoperable",
                "delimiter": ";",
                "formula_policy": "safe",
                "table_ids": ["t1"],
                "value_mode": "canonical",
            },
        },
        "pdf",
    )
    assert options["target_formats"] == ["txt", "csv"]
    assert options["ocr"]["dpi"] == 200
    assert options["ocr"]["strategy"] == "ocr"
    assert options["text"]["page_separator"] == "none"
    assert options["text"]["include_table_text"] is False
    assert options["csv"]["delimiter"] == ";"
    assert options["csv"]["value_mode"] == "canonical"


def test_unsupported_target_format_is_rejected():
    with pytest.raises(ApiError) as excinfo:
        _normalize_options({"target_formats": ["docx"]}, "pdf")
    assert _code(excinfo.value) == "UNSUPPORTED_FORMAT"
