"""T8 — durable knowledge jobs and executor recovery boundaries."""

from __future__ import annotations

import io
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook

from knowledge.job_runner import KnowledgeJobRunner
from models.db import KnowledgeIndexJobRow, get_session_factory


def _session(api_client) -> dict:
    token = api_client.post("/api/v1/auth/anonymous", json={}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return {"id": api_client.get("/api/v1/auth/me", headers=headers).json()["id"], "headers": headers}


def _long_xlsx(text: str = "conteudo para recuperacao") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Texto"
    ws.append(["Campo", "Valor"])
    ws.append(["Nota", text * 400])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _manual_occurrence(schedule_id: str, owner_id: str, key: str) -> str:
    """Create the occurrence without starting the background executor.

    The tests drive `AutomationExecutor.advance` themselves so recovery
    boundaries are deterministic (no concurrent daemon thread).
    """
    from automation.scheduler import AutomationScheduler
    from models.db import get_session_factory

    session = get_session_factory()()
    try:
        run = AutomationScheduler(worker_id="test").create_manual_occurrence(
            session, schedule_id=schedule_id, owner_id=owner_id, request_key=key
        )
        session.commit()
        return run.id
    finally:
        session.close()


def _conversion(api_client, owner: dict, tmp_path: Path) -> tuple[str, str]:
    import json

    data = _long_xlsx()
    response = api_client.post(
        "/api/v1/conversions",
        files={"file": ("doc.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"options": json.dumps({"schema_version": 3})},
        headers=owner["headers"],
    )
    conversion_id = response.json().get("conversion_id") or response.json().get("id")
    detail = {}
    for _ in range(200):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=owner["headers"]).json()
        if detail.get("status") in {"completed", "completed_with_warnings", "failed"}:
            break
        time.sleep(0.1)
    return conversion_id, detail["run"]["run_id"]


def test_running_job_is_recovered_after_crash(api_client):
    """A job left `running` by a dead process returns to the queue."""
    runner = KnowledgeJobRunner()
    session = get_session_factory()()
    try:
        row = KnowledgeIndexJobRow(
            id="idx_crash_1",
            owner_id="owner-crash",
            collection_id="col-crash",
            status="running",
            source_json={"conversion_ids": [], "source_paths": [], "scope": "documents"},
        )
        session.add(row)
        session.commit()
    finally:
        session.close()

    recovered = runner.recover()
    assert "idx_crash_1" in recovered
    session = get_session_factory()()
    try:
        row = session.get(KnowledgeIndexJobRow, "idx_crash_1")
        assert row.status == "queued"
    finally:
        session.close()


def test_completed_job_is_not_requeued(api_client):
    runner = KnowledgeJobRunner()
    session = get_session_factory()()
    try:
        row = KnowledgeIndexJobRow(
            id="idx_done_1",
            owner_id="owner-done",
            collection_id="col-done",
            status="completed",
            source_json={"conversion_ids": []},
            stats_json={"chunks": 5},
        )
        session.add(row)
        session.commit()
    finally:
        session.close()
    assert "idx_done_1" not in runner.recover()


def test_runner_indexes_real_conversion_and_records_stats(api_client, tmp_path: Path):
    owner = _session(api_client)
    conversion_id, run_id = _conversion(api_client, owner, tmp_path)
    collection = api_client.post(
        "/api/v1/knowledge/collections",
        json={"name": f"col-{run_id[:6]}"},
        headers=owner["headers"],
    ).json()["collection_id"]

    runner = KnowledgeJobRunner()
    job = runner.enqueue(
        owner_id=owner["id"], collection_id=collection, conversion_ids=[conversion_id]
    )
    for _ in range(100):
        payload = runner.get(job["job_id"], owner["id"])
        if payload["status"] in {"completed", "failed"}:
            break
        time.sleep(0.1)
    assert payload["status"] == "completed"
    assert payload["stats"]["chunks"] > 0


def test_executor_uses_frozen_snapshot_not_mutated_source(api_client, tmp_path: Path):
    """Editing the source after the occurrence exists must not change its files."""
    import uuid

    from automation.executor import AutomationExecutor

    internal = {"x-internal-token": "test-internal-token"}
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.xlsx").write_bytes(_long_xlsx())
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner["id"]},
        headers=internal,
    ).json()["id"]
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": grant, "relative_path": "docs", "kind": "directory", "name": "Docs"},
        headers=owner["headers"],
    ).json()
    schedule = api_client.post(
        "/api/v1/schedules",
        json={
            "name": "S",
            "source_id": source["id"],
            "rule": {"kind": "interval", "every_seconds": 3600, "anchor_at": "2026-11-19T11:00:00Z"},
            "timezone": "UTC",
            "options": {"schema_version": 3},
        },
        headers=owner["headers"],
    ).json()

    # Manual occurrence freezes the definition (no daemon thread: the test
    # drives the executor itself to keep the boundary deterministic).
    run_id = _manual_occurrence(schedule["id"], owner["id"], uuid.uuid4().hex)

    # Mutate the source row AFTER the occurrence was created
    api_client.patch(
        f"/api/v1/sources/{source['id']}",
        json={"version": source["version"], "relative_path": "outra-pasta"},
        headers=owner["headers"],
    )

    result = AutomationExecutor().advance(run_id, owner["id"])
    assert result.done is True
    assert result.state in {"succeeded", "partial", "no_changes"}
    # The occurrence processed the frozen snapshot (docs/), not the edit.
    detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
    assert detail["snapshot"]["source_id"] == source["id"]


def test_executor_fails_cleanly_when_root_is_revoked(api_client, tmp_path: Path):
    import uuid

    from automation.executor import AutomationExecutor

    internal = {"x-internal-token": "test-internal-token"}
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.xlsx").write_bytes(_long_xlsx())
    root_id = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner["id"]},
        headers=internal,
    ).json()["id"]
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "docs", "kind": "directory", "name": "Docs"},
        headers=owner["headers"],
    ).json()
    schedule = api_client.post(
        "/api/v1/schedules",
        json={
            "name": "S",
            "source_id": source["id"],
            "rule": {"kind": "interval", "every_seconds": 3600, "anchor_at": "2026-11-19T11:00:00Z"},
            "timezone": "UTC",
        },
        headers=owner["headers"],
    ).json()
    run_id = _manual_occurrence(schedule["id"], owner["id"], uuid.uuid4().hex)

    # Revoke the grant before the executor reads files
    api_client.delete(
        f"/internal/v1/source-roots/{root_id}",
        headers={**internal, "x-owner-id": owner["id"]},
    )

    result = AutomationExecutor().advance(run_id, owner["id"])
    assert result.done is True
    assert result.state == "failed"
    detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
    assert detail["state"] == "failed"
    assert detail["error"]["code"] == "ROOT_REVOKED", detail["error"]
