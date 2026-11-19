"""T8 executor: transient retries, index job reuse, lease renewal, loop dispatch.

Uses the shared test database like the other automation suites; each test
starts from empty automation tables so claims are deterministic.
"""

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
def _executor_enabled(monkeypatch):
    """Run-now is refused when AUTOMATION_EXECUTOR_ENABLED is off (AU1-13)."""
    from config import settings

    monkeypatch.setattr(settings, "automation_executor_enabled", True)


@pytest.fixture(autouse=True)
def _clean_automation_tables():
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


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _session(api_client) -> dict:
    token = api_client.post("/api/v1/auth/anonymous", json={}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return {"id": api_client.get("/api/v1/auth/me", headers=headers).json()["id"], "headers": headers}


def _xlsx(text: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["A", "B"])
    ws.append([text, 1])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _prepare(api_client, tmp_path: Path, files: dict[str, bytes]):
    owner = _session(api_client)
    root = tmp_path / f"raiz-{uuid.uuid4().hex[:6]}"
    (root / "docs").mkdir(parents=True)
    for name, data in files.items():
        (root / "docs" / name).write_bytes(data)
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
    return owner, schedule


def _manual_run(schedule_id: str, owner_id: str) -> str:
    from automation.scheduler import AutomationScheduler
    from models.db import get_session_factory

    session = get_session_factory()()
    try:
        run = AutomationScheduler(worker_id="test").create_manual_occurrence(
            session, schedule_id=schedule_id, owner_id=owner_id, request_key=uuid.uuid4().hex
        )
        session.commit()
        return run.id
    finally:
        session.close()


def _claim(scheduler, now: datetime):
    from models.db import get_session_factory

    session = get_session_factory()()
    try:
        lease = scheduler.claim_due(session, now=now, worker_id=scheduler.worker_id)
        session.commit()
        return lease
    finally:
        session.close()


def _run_row(run_id: str) -> dict:
    from automation.models import AutomationRunRow
    from models.db import get_session_factory

    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        return {
            "state": run.state,
            "attempt": run.attempt,
            "fence": run.fence,
            "error_code": run.error_code,
            "next_attempt_at": _utc(run.next_attempt_at),
            "index_job_id": run.index_job_id,
            "lease_until": _utc(run.lease_until),
        }
    finally:
        session.close()


def _api_error(status: int, code: str):
    from api.deps import ApiError

    return ApiError(status, code, f"{code} simulado.")


# ----------------------------------------------------------------- retries


def test_transient_failures_retry_at_30_120_300_then_fail(api_client, tmp_path: Path, monkeypatch):
    from automation.executor import RETRY_DELAYS, AutomationExecutor
    from automation.scheduler import AutomationScheduler
    from sources.importer import ImportService

    def unavailable(self, import_id, owner_id):  # noqa: ARG001
        raise _api_error(503, "GATEWAY_UNAVAILABLE")

    monkeypatch.setattr(ImportService, "run", unavailable)
    owner, schedule = _prepare(api_client, tmp_path, {"plan.xlsx": _xlsx("dados")})
    run_id = _manual_run(schedule["id"], owner["id"])

    clock = datetime.now(UTC)
    scheduler = AutomationScheduler(worker_id="retry")
    executor = AutomationExecutor(clock=lambda: clock)

    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        lease = _claim(scheduler, clock)
        assert lease is not None and lease.run_id == run_id
        assert lease.fence == attempt

        progress = executor.advance(run_id, owner["id"], fence=lease.fence)

        assert progress.done is False and progress.state == "queued"
        row = _run_row(run_id)
        assert row["state"] == "queued"
        assert row["attempt"] == attempt
        assert row["error_code"] == "GATEWAY_UNAVAILABLE"
        assert row["next_attempt_at"] == clock + timedelta(seconds=delay)
        assert row["lease_until"] is None

        # Not yet due: the retry must not be claimable before its delay.
        assert _claim(scheduler, clock + timedelta(seconds=delay - 1)) is None
        clock = clock + timedelta(seconds=delay)

    lease = _claim(scheduler, clock)
    assert lease is not None and lease.fence == len(RETRY_DELAYS) + 1
    progress = executor.advance(run_id, owner["id"], fence=lease.fence)

    assert progress.done is True and progress.state == "failed"
    row = _run_row(run_id)
    assert row["state"] == "failed"
    assert row["error_code"] == "GATEWAY_UNAVAILABLE"
    assert row["attempt"] == len(RETRY_DELAYS) + 1


def test_non_transient_api_error_is_terminal_on_first_attempt(api_client, tmp_path: Path, monkeypatch):
    from automation.executor import AutomationExecutor
    from automation.scheduler import AutomationScheduler
    from sources.importer import ImportService

    def rejected(self, import_id, owner_id):  # noqa: ARG001
        raise _api_error(400, "INVALID_OPTIONS")

    monkeypatch.setattr(ImportService, "run", rejected)
    owner, schedule = _prepare(api_client, tmp_path, {"plan.xlsx": _xlsx("dados")})
    run_id = _manual_run(schedule["id"], owner["id"])

    now = datetime.now(UTC)
    scheduler = AutomationScheduler(worker_id="terminal")
    lease = _claim(scheduler, now)
    progress = AutomationExecutor(clock=lambda: now).advance(run_id, owner["id"], fence=lease.fence)

    assert progress.done is True and progress.state == "failed"
    row = _run_row(run_id)
    assert row["state"] == "failed"
    assert row["error_code"] == "INVALID_OPTIONS"
    assert row["next_attempt_at"] is None


# ------------------------------------------------------------ index reuse


def _wait_terminal(api_client, owner: dict, run_id: str) -> dict:
    detail = {}
    for _ in range(200):
        detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
        if detail["state"] in {"succeeded", "partial", "no_changes", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    return detail


def _jobs_for_run(run_id: str) -> int:
    from models.db import KnowledgeIndexJobRow, get_session_factory

    session = get_session_factory()()
    try:
        rows = session.query(KnowledgeIndexJobRow).all()
        return sum(1 for row in rows if (row.source_json or {}).get("schedule_run_id") == run_id)
    finally:
        session.close()


def test_readvance_reuses_the_index_job_instead_of_enqueueing_another(api_client, tmp_path: Path):
    from automation.executor import AutomationExecutor
    from models.db import AutomationRunRow, get_session_factory

    owner, schedule = _prepare(api_client, tmp_path, {"plan.xlsx": _xlsx("rag " * 600)})
    collection = api_client.post(
        "/api/v1/knowledge/collections",
        json={"name": f"col-{uuid.uuid4().hex[:6]}"},
        headers=owner["headers"],
    ).json()["collection_id"]
    run_id = _manual_run(schedule["id"], owner["id"])
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        snapshot = dict(run.snapshot_json or {})
        snapshot["index_collection_id"] = collection
        run.snapshot_json = snapshot
        session.commit()
    finally:
        session.close()

    AutomationExecutor().advance(run_id, owner["id"])
    first_job = _run_row(run_id)["index_job_id"]
    assert first_job
    _wait_terminal(api_client, owner, run_id)

    # Simulate a crash after the job finished but before the occurrence closed.
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        run.state = "waiting_children"
        run.finished_at = None
        run.lease_until = None
        session.commit()
    finally:
        session.close()

    progress = AutomationExecutor().advance(run_id, owner["id"])

    assert progress.done is True
    assert _run_row(run_id)["index_job_id"] == first_job
    assert _jobs_for_run(run_id) == 1, "re-advance must not enqueue a second index job"


# ---------------------------------------------------------------- leases


def test_lease_keeper_renews_a_long_running_claim(api_client, tmp_path: Path):
    from automation.loop import _LeaseKeeper
    from automation.scheduler import AutomationScheduler
    from models.db import AutomationRunRow, get_session_factory

    owner, schedule = _prepare(api_client, tmp_path, {"plan.xlsx": _xlsx("dados")})
    run_id = _manual_run(schedule["id"], owner["id"])
    scheduler = AutomationScheduler(worker_id="keeper")
    now = datetime.now(UTC)
    lease = _claim(scheduler, now)

    # Shorten the lease so the renewal is observable.
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        run.lease_until = now + timedelta(seconds=5)
        session.commit()
    finally:
        session.close()

    with _LeaseKeeper(scheduler, run_id, lease.fence, interval=0.02):
        time.sleep(0.2)

    renewed = _run_row(run_id)["lease_until"]
    assert renewed > now + timedelta(seconds=60), renewed


def test_lease_keeper_stops_when_the_fence_is_lost(api_client, tmp_path: Path):
    from automation.loop import _LeaseKeeper
    from automation.scheduler import AutomationScheduler

    owner, schedule = _prepare(api_client, tmp_path, {"plan.xlsx": _xlsx("dados")})
    run_id = _manual_run(schedule["id"], owner["id"])
    scheduler = AutomationScheduler(worker_id="keeper")
    lease = _claim(scheduler, datetime.now(UTC))

    keeper = _LeaseKeeper(scheduler, run_id, lease.fence + 1, interval=0.02)
    assert keeper.renew() is False


# ------------------------------------------------------------- dispatch


def test_loop_tick_dispatches_index_jobs_queued_before_a_crash(api_client, tmp_path: Path, monkeypatch):
    from automation.loop import AutomationLoop
    from knowledge.indexer import KnowledgeIndexer
    from models.db import KnowledgeIndexJobRow, get_session_factory

    calls: list[str] = []

    def fake(self, owner_id, collection_id, conversion_id):  # noqa: ARG001
        calls.append(conversion_id)
        return {"document_id": conversion_id, "status": "indexed", "chunks": 1}

    monkeypatch.setattr(KnowledgeIndexer, "_index_conversion", fake)
    job_id = f"idx_crashed_{uuid.uuid4().hex}"
    conversion_id = f"conv-{uuid.uuid4().hex[:8]}"
    session = get_session_factory()()
    try:
        session.add(
            KnowledgeIndexJobRow(
                id=job_id,
                owner_id=f"owner-{uuid.uuid4().hex[:8]}",
                collection_id=f"col-{uuid.uuid4().hex[:8]}",
                status="queued",
                source_json={"conversion_ids": [conversion_id], "source_paths": [], "scope": "documents"},
            )
        )
        session.commit()
    finally:
        session.close()

    loop = AutomationLoop(worker_id="dispatch-test")
    loop.tick(now=datetime.now(UTC))
    loop.tick(now=datetime.now(UTC))

    assert calls.count(conversion_id) == 1
    session = get_session_factory()()
    try:
        assert session.get(KnowledgeIndexJobRow, job_id).status == "completed"
    finally:
        session.close()


# ------------------------------------------------------------ cleanup


def test_gateway_submission_stub_is_gone_from_submission_module():
    from sources import submission

    assert not hasattr(submission, "GatewaySubmissionPort")
    assert "GatewaySubmissionPort" not in submission.__all__


def _gateway_timeout():
    from sources.gateway_submission import GatewaySubmissionError

    return GatewaySubmissionError("GATEWAY_TIMEOUT", "O gateway não respondeu em tempo.", {"path": "/x"})


def _connection_refused():
    return ConnectionError("conexão recusada")


@pytest.mark.parametrize(
    ("make_exc", "expected_code"),
    [(_gateway_timeout, "GATEWAY_TIMEOUT"), (_connection_refused, "EXECUTOR_ERROR")],
    ids=["gateway-timeout-keeps-code", "connection-error-generic"],
)
def test_exhausted_transient_failure_keeps_original_code(
    api_client, tmp_path: Path, monkeypatch, make_exc, expected_code
):
    """After the last retry, a transient failure keeps its gateway code, not EXECUTOR_ERROR."""
    from automation.executor import RETRY_DELAYS, AutomationExecutor
    from automation.scheduler import AutomationScheduler
    from sources.importer import ImportService

    def flaky(self, import_id, owner_id):  # noqa: ARG001
        raise make_exc()

    monkeypatch.setattr(ImportService, "run", flaky)
    owner, schedule = _prepare(api_client, tmp_path, {"plan.xlsx": _xlsx("dados")})
    run_id = _manual_run(schedule["id"], owner["id"])

    clock = datetime.now(UTC)
    scheduler = AutomationScheduler(worker_id="exhaust")
    executor = AutomationExecutor(clock=lambda: clock)
    progress = None
    for _ in range(len(RETRY_DELAYS) + 1):
        lease = _claim(scheduler, clock)
        assert lease is not None and lease.run_id == run_id
        progress = executor.advance(run_id, owner["id"], fence=lease.fence)
        if progress.done:
            break
        clock = clock + timedelta(seconds=RETRY_DELAYS[-1])

    assert progress is not None and progress.done is True and progress.state == "failed"
    row = _run_row(run_id)
    assert row["state"] == "failed"
    assert row["error_code"] == expected_code
    assert row["next_attempt_at"] is None
