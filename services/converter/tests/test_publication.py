"""Publication policy: fatal integrity failures quarantine the artifact."""

from __future__ import annotations

from engine.validation.publication import publication_report, publication_status


def test_warning_only_is_publishable():
    checks = [
        {"check": "text_coverage", "status": "warn", "details": "98%"},
        {"check": "unrecognized_regions", "status": "warn", "details": "1 página"},
        {"check": "xlsx_no_formulas", "status": "pass", "details": "ok"},
    ]
    assert publication_status(checks=checks, quality="partial") == "approved"
    report = publication_report(checks, "partial")
    assert report["download_allowed"] is True
    assert report["fatal_count"] == 0


def test_integrity_failure_is_quarantined():
    checks = [
        {"check": "xlsx_no_media", "status": "fail", "details": "1 imagem"},
        {"check": "xlsx_zip_integrity", "status": "pass", "details": "ok"},
    ]
    assert publication_status(checks=checks, quality="partial") == "quarantined"
    report = publication_report(checks, "partial")
    assert report["download_allowed"] is False
    assert report["fatal_checks"][0]["check"] == "xlsx_no_media"


def test_failed_quality_never_publishes():
    assert publication_status(checks=[], quality="failed") == "quarantined"
