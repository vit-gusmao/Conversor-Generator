"""T8 review fixes: fenced finalization, durable cancellation, RAG waiting."""

from __future__ import annotations

import io
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook

INTERNAL = {"x-internal-token": "test-internal-token"}


@pytest.fixture(autouse=True)
def _clean_automation_tables():
    """Shared test DB: reset automation rows so claims are deterministic."""
    from sqlalchemy import delete as _delete

    from automation.models import AutomationEventRow, AutomationRunRow, AutomationScheduleRow
    from models.db import get_session_factory, init_db

    init_db()
    session = get_session_factory()()
    try:
        session.execute(_delete(AutomationEventRow))
        session.execute(_delete(AutomationRunRow))
        session.execute(_delete(AutomationScheduleRow))
        session.commit()
    finally:
        session.close()
    yield


def _session(api_client) -> dict:
    token = api_client.post("/api/v1/auth/anonymous", json={}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return {"id": api_client.get("/api/v1/auth/me", headers=headers).json()["id"], "headers": headers}


def _xlsx(text: str = "dados") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["A", "B"])
    ws.append([text, 1])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _prepare(api_client, tmp_path: Path, content: str | None = "dados"):
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    if content is not None:
        (root / "docs" / "plan.xlsx").write_bytes(_xlsx(content))
    root_id = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner["id"]},
        headers=INTERNAL,
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
    return owner, source, schedule, root


def _manual_run(schedule_id: str, owner_id: str, key: str) -> str:
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


def test_stale_worker_cannot_finalize_reclaimed_run(api_client, tmp_path: Path):
    """Fence bump by recovery makes the old worker's write a no-op."""
    from automation.executor import AutomationExecutor
    from automation.scheduler import AutomationScheduler
    from models.db import AutomationRunRow, get_session_factory

    owner, _, schedule, _ = _prepare(api_client, tmp_path)
    run_id = _manual_run(schedule["id"], owner["id"], uuid.uuid4().hex)

    scheduler = AutomationScheduler(worker_id="w1")
    session = get_session_factory()()
    try:
        lease = scheduler.claim_due(session, worker_id="w1")
        session.commit()
        assert lease is not None and lease.fence == 1
    finally:
        session.close()

    # Recovery bumps the fence (the attempt now belongs to a new worker).
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        run.fence = 2
        run.lease_until = datetime.now(UTC) + timedelta(seconds=120)
        session.commit()
    finally:
        session.close()

    result = AutomationExecutor().advance(run_id, owner["id"], fence=1)
    # The stale worker returns its own progress but writes nothing terminal.
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        assert run.state == "running"  # untouched by the stale attempt
        assert run.finished_at is None
    finally:
        session.close()
    assert result.run_id == run_id


def test_cancel_before_start_finishes_cancelled_and_persists(api_client, tmp_path: Path):
    from automation.executor import AutomationExecutor
    from models.db import AutomationRunRow, get_session_factory

    owner, _, schedule, _ = _prepare(api_client, tmp_path)
    run_id = _manual_run(schedule["id"], owner["id"], uuid.uuid4().hex)

    response = api_client.post(f"/api/v1/schedule-runs/{run_id}/cancel", headers=owner["headers"])
    assert response.status_code == 202

    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        assert run.cancel_requested_at is not None  # durable, not just state
    finally:
        session.close()

    result = AutomationExecutor().advance(run_id, owner["id"])
    assert result.state == "cancelled"
    detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
    assert detail["state"] == "cancelled"


def test_cancel_queued_run_is_reclaimed_by_the_scheduler(api_client, tmp_path: Path):
    """A `cancelling` run without lease is claimable (it used to wedge)."""
    from automation.scheduler import AutomationScheduler
    from models.db import AutomationRunRow, get_session_factory

    owner, _, schedule, _ = _prepare(api_client, tmp_path)
    run_id = _manual_run(schedule["id"], owner["id"], uuid.uuid4().hex)
    api_client.post(f"/api/v1/schedule-runs/{run_id}/cancel", headers=owner["headers"])

    scheduler = AutomationScheduler(worker_id="w-cancel")
    session = get_session_factory()()
    try:
        lease = scheduler.claim_due(session, worker_id="w-cancel")
        session.commit()
        assert lease is not None and lease.run_id == run_id
    finally:
        session.close()


def test_recovery_keeps_cancellation_and_does_not_execute(api_client, tmp_path: Path):
    from automation.loop import AutomationLoop
    from automation.executor import AutomationExecutor
    from models.db import AutomationRunRow, get_session_factory

    owner, _, schedule, root = _prepare(api_client, tmp_path)
    run_id = _manual_run(schedule["id"], owner["id"], uuid.uuid4().hex)
    api_client.post(f"/api/v1/schedule-runs/{run_id}/cancel", headers=owner["headers"])

    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        run.state = "cancelling"
        run.lease_until = datetime(2026, 11, 1, tzinfo=UTC)  # expired
        session.commit()
    finally:
        session.close()

    AutomationLoop(worker_id="recover").recover()
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        assert run.state == "cancelling"  # not rewritten to queued
    finally:
        session.close()

    result = AutomationExecutor().advance(run_id, owner["id"])
    assert result.state == "cancelled"


def test_index_request_keeps_run_in_waiting_children_then_succeeds(api_client, tmp_path: Path):
    """A RAG-enabled occurrence is not finished as `partial` prematurely."""
    from automation.executor import AutomationExecutor
    from knowledge.job_runner import KnowledgeJobRunner
    from models.db import AutomationRunRow, get_session_factory

    owner, source, schedule, root = _prepare(api_client, tmp_path, content="rag " * 600)
    collection = api_client.post(
        "/api/v1/knowledge/collections",
        json={"name": f"col-{uuid.uuid4().hex[:6]}"},
        headers=owner["headers"],
    ).json()["collection_id"]

    run_id = _manual_run(schedule["id"], owner["id"], uuid.uuid4().hex)
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        snapshot = dict(run.snapshot_json or {})
        snapshot["index_collection_id"] = collection
        run.snapshot_json = snapshot
        session.commit()
    finally:
        session.close()

    progress = AutomationExecutor().advance(run_id, owner["id"])
    # The index job runs in its own thread: it may already have finalized the
    # occurrence by the time advance returns. Either way it must never be
    # reported as `partial` merely because indexing was queued.
    assert progress.state in {"waiting_children", "succeeded"}, progress.state
    assert progress.state != "partial"

    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        job_id = run.index_job_id
    finally:
        session.close()
    assert job_id

    # The durable job completes and finalizes the waiting occurrence. The
    # job runs in its own thread, so poll until the occurrence is terminal
    # instead of assuming one specific intermediate state.
    for _ in range(200):
        detail = api_client.get(
            f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]
        ).json()
        if detail["state"] in {
            "succeeded", "partial", "no_changes", "failed", "cancelled"
        }:
            break
        time.sleep(0.05)
    assert detail["state"] == "succeeded", detail
