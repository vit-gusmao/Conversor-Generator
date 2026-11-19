"""T5 — durable standalone import: idempotency, dedup, recovery, cancel."""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from pathlib import Path

import pytest
from openpyxl import Workbook

from sources.importer import ImportService
from sources.service import SourceService
from sources.submission import StandaloneSubmissionPort

INTERNAL_TOKEN = "test-internal-token"


def _session(api_client) -> dict:
    """One owner session: {'id', 'headers'}."""
    response = api_client.post("/api/v1/auth/anonymous")
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = api_client.get("/api/v1/auth/me", headers=headers)
    return {"id": me.json()["id"], "headers": headers}


def _xlsx_bytes(content: str = "dados") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["A", "B"])
    ws.append([content, 1])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _make_source(api_client, tmp_path: Path) -> tuple[dict, str, Path]:
    """Grant a root and create a source for ONE owner session."""
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner["id"]},
        headers={"x-internal-token": INTERNAL_TOKEN},
    )
    assert grant.status_code == 201, grant.text
    root_id = grant.json()["id"]
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "docs", "kind": "directory", "name": "Docs"},
        headers=owner["headers"],
    )
    assert source.status_code == 201, source.text
    return owner, source.json()["id"], root


def _start_import(api_client, owner: dict, source_id: str, body: dict) -> object:
    return api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json=body,
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    )


def _wait_import(api_client, owner: dict, import_id: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        payload = api_client.get(
            f"/api/v1/imports/{import_id}", headers=owner["headers"]
        ).json()
        if payload.get("status") in {"completed", "partial", "failed", "cancelled"}:
            return payload
        time.sleep(0.2)
    return payload


def test_same_key_same_body_replays_same_import(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    key = uuid.uuid4().hex
    body = {"options": {"schema_version": 3}}

    first = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json=body,
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text
    second = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json=body,
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["replayed"] is True
    _wait_import(api_client, owner, first.json()["id"])


def test_same_key_different_body_conflicts(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    key = uuid.uuid4().hex
    first = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json={"options": {"schema_version": 3}},
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert first.status_code == 202
    _wait_import(api_client, owner, first.json()["id"])
    conflict = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json={"options": {"schema_version": 3}, "force": True},
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert conflict.status_code == 409


def test_import_converts_files_and_records_versions(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("primeira"))

    started = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    assert started.status_code == 202, started.text
    payload = _wait_import(api_client, owner, started.json()["id"])
    assert payload["status"] == "completed", payload
    assert payload["counts"]["imported"] == 1

    items = api_client.get(
        f"/api/v1/imports/{started.json()['id']}/items", headers=owner["headers"]
    ).json()["items"]
    assert items[0]["conversion_id"]

    # unchanged: skipped next time
    again = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    payload = _wait_import(api_client, owner, again.json()["id"])
    assert payload["counts"]["skipped"] == 1 and payload["counts"]["imported"] == 0, payload

    # changed content: imported again
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("segunda"))
    third = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    payload = _wait_import(api_client, owner, third.json()["id"])
    assert payload["counts"]["imported"] == 1, payload

    # force: reprocess unchanged
    force = _start_import(
        api_client, owner, source_id, {"options": {"schema_version": 3}, "force": True}
    )
    payload = _wait_import(api_client, owner, force.json()["id"])
    assert payload["counts"]["imported"] == 1 and payload["counts"]["skipped"] == 0, payload


def test_partial_failure_marks_import_partial(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "boa.xlsx").write_bytes(_xlsx_bytes("ok"))
    (root / "docs" / "quebrada.xlsx").write_bytes(b"isto nao e um xlsx")

    started = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    payload = _wait_import(api_client, owner, started.json()["id"])
    assert payload["status"] == "partial", payload
    assert payload["counts"]["imported"] == 1 and payload["counts"]["failed"] == 1


def test_foreign_owner_cannot_see_import(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    other = _session(api_client)
    started = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    import_id = started.json()["id"]
    _wait_import(api_client, owner, import_id)
    got = api_client.get(f"/api/v1/imports/{import_id}", headers=other["headers"])
    assert got.status_code == 404


def test_recovery_after_submission_does_not_duplicate(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("recuperavel"))
    service = ImportService()

    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )
    import_id = record["id"]

    from models.db import get_session_factory

    sources = SourceService()
    session = get_session_factory()()
    try:
        source_row = sources.repository.get_source(
            session, source_id=source_id, owner_id=owner["id"]
        )
        root_row = sources.repository.get_root(
            session, root_id=source_row.root_id, owner_id=owner["id"]
        )
        definition = sources.definition_for(source_row, root_row)
    finally:
        session.close()

    normalized = service._normalize({"schema_version": 3}, "spreadsheet")
    options_hash = service._options_hash(normalized)
    # "crash" after scan+capture: run only that phase, then resume the run
    service._scan_and_capture(
        import_id, owner["id"], definition, normalized, options_hash, force=False
    )
    service.run(import_id, owner["id"])
    items = service.items(import_id, owner["id"])
    conversion_ids = [item["conversion_id"] for item in items if item["conversion_id"]]
    assert len(conversion_ids) == 1  # exactly one conversion despite the crash
    # a second full run keeps the history stable (completed import short-circuits)
    service.run(import_id, owner["id"])
    items = service.items(import_id, owner["id"])
    conversion_ids = [item["conversion_id"] for item in items if item["conversion_id"]]
    assert len(conversion_ids) == 1
