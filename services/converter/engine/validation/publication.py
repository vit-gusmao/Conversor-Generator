"""Publication policy: warnings are not failures.

A conversion that opens a file is not necessarily safe to publish.  This module
separates *fatal* integrity failures from *warnings* and decides whether an
artifact may be downloaded.

Fatal means: the file is corrupt, contains media/formulas, is missing planned
cells, or the plan exceeds a hard format limit.  A partial OCR page is a
warning: the file is valid, the coverage is lower, and the user must be told.
"""

from __future__ import annotations

from typing import Any

FATAL_CHECKS = frozenset(
    {
        "xlsx_zip_integrity",
        "xlsx_required_parts",
        "xlsx_no_media",
        "xlsx_no_image_relationships",
        "xlsx_no_images_object",
        "xlsx_reopen",
        "xlsx_sheet_names",
        "xlsx_no_formulas",
        "xlsx_has_cells",
        "xlsx_sheets_present",
        "xlsx_values_match_plan",
        "excel_limits",
        "identifiers_preserved",
    }
)

POLICY_VERSION = "publication-policy-v1"


def classify(check: dict[str, Any]) -> str:
    if check.get("status") != "fail":
        return "informational"
    return "fatal" if check.get("check") in FATAL_CHECKS else "warning"


def fatal_failures(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [check for check in checks if classify(check) == "fatal"]


def publication_status(*, checks: list[dict[str, Any]], quality: str) -> str:
    """Return ``approved``, ``quarantined`` (fatal) or ``expired``."""
    if quality == "failed" or fatal_failures(checks):
        return "quarantined"
    return "approved"


def publication_report(checks: list[dict[str, Any]], quality: str) -> dict[str, Any]:
    fatal = fatal_failures(checks)
    return {
        "policy_version": POLICY_VERSION,
        "status": publication_status(checks=checks, quality=quality),
        "fatal_count": len(fatal),
        "fatal_checks": [
            {"check": item.get("check"), "details": item.get("details")} for item in fatal
        ],
        "warning_count": sum(
            1 for item in checks if item.get("status") == "warn" or classify(item) == "warning"
        ),
        "download_allowed": publication_status(checks=checks, quality=quality) == "approved",
    }


__all__ = [
    "FATAL_CHECKS",
    "POLICY_VERSION",
    "classify",
    "fatal_failures",
    "publication_status",
    "publication_report",
]
