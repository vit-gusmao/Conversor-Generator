"""Upgrade plan contract tests (T1).

Loads the shared fixtures from ``packages/contracts/fixtures/`` and asserts
that the Python engine accepts/rejects exactly the same option payloads and
DTO shapes as the TypeScript contract. The TypeScript mirror lives in
``apps/api/src/modules/conversions/options.spec.ts``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.deps import ApiError
from api.service import _normalize_options
from automation.types import ScheduleCreate, validate_schedule_create
from sources.types import SourceCreate, validate_source_create

FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "fixtures"


def _load(name: str) -> dict:
    with (FIXTURE_ROOT / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _code(exc: ApiError) -> str:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return str(detail.get("error", {}).get("code", ""))


OPTIONS_CASES = _load("upgrade-contracts.json")["cases"]
SOURCE_CASES = _load("upgrade-sources.json")["cases"]


@pytest.mark.parametrize("case", OPTIONS_CASES, ids=[c["id"] for c in OPTIONS_CASES])
def test_options_fixture(case: dict):
    """Every shared fixture case must normalize identically to the TS contract."""
    if "expected_error" in case:
        with pytest.raises(ApiError) as excinfo:
            _normalize_options(case["input"], case["kind"])
        assert _code(excinfo.value) == case["expected_error"]["py"]
        return

    normalized = _normalize_options(case["input"], case["kind"])
    assert normalized == case["expected"]


@pytest.mark.parametrize("case", SOURCE_CASES, ids=[c["id"] for c in SOURCE_CASES])
def test_sources_schedules_fixture(case: dict):
    """Source/schedule create DTOs must accept/reject exactly like the TS contract."""
    if case["kind"] == "source_create":
        if "expected_error" in case:
            with pytest.raises(ApiError) as excinfo:
                validate_source_create(case["input"])
            assert _code(excinfo.value) == case["expected_error"]["py"]
            return
        parsed = validate_source_create(case["input"])
        assert isinstance(parsed, SourceCreate)
        for key, value in case["expected"].items():
            assert getattr(parsed, key) == value
        assert not hasattr(parsed, "owner_id")
        return

    if "expected_error" in case:
        with pytest.raises(ApiError) as excinfo:
            validate_schedule_create(case["input"])
        assert _code(excinfo.value) == case["expected_error"]["py"]
        return

    parsed = validate_schedule_create(case["input"])
    assert isinstance(parsed, ScheduleCreate)
    for key, value in case["expected"].items():
        actual = getattr(parsed, key)
        if hasattr(actual, "model_dump"):
            actual = actual.model_dump()
        assert actual == value
    assert not hasattr(parsed, "owner_id")


def test_v2_canonical_hash_is_stable():
    """A v2 payload must keep producing the same normalized dict as before v3."""
    legacy = _normalize_options(
        {
            "target_formats": ["xlsx"],
            "ocr": False,
            "text_newline": "crlf",
            "page_separator": "marker",
            "csv_profile": "excel_br",
        },
        "pdf",
    )
    assert legacy["schema_version"] == 2
    assert "spreadsheet" not in legacy
    assert legacy["ocr"]["enabled"] is False
    assert legacy["text"] == {
        "newline": "crlf",
        "page_separator": "marker",
        "include_table_text": True,
    }
    assert legacy["csv"]["profile"] == "excel_br"


def test_reader_version_is_part_of_v3_output():
    """The normalized v3 output pins the spreadsheet reader version for fingerprints."""
    normalized = _normalize_options({"schema_version": 3}, "spreadsheet")
    reader = normalized["spreadsheet"]["reader_version"]
    assert isinstance(reader, str) and reader
