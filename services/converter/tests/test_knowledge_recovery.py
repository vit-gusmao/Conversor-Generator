"""T8 — durable knowledge jobs and executor recovery boundaries."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from knowledge.job_runner import LEASE_SECONDS, KnowledgeJobRunner
from models.db import KnowledgeIndexJobRow, get_session_factory, init_db


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


# --------------------------------------------------------------------------- #
# Lease / fence / dispatch (WP-C1-T8-INDEX-JOBS)
# --------------------------------------------------------------------------- #


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture
def jobs_db():
    init_db()
    yield


@pytest.fixture
def index_fakes(monkeypatch):
    """Record each per-conversion indexing call; tests choose the outcome."""
    from knowledge.indexer import KnowledgeIndexer

    calls: list[tuple[str, str]] = []
    outcomes: dict[str, str] = {}

    def fake(self, owner_id, collection_id, conversion_id):  # noqa: ARG001
        calls.append((collection_id, conversion_id))
        status = outcomes.get(conversion_id, "indexed")
        return {
            "document_id": conversion_id,
            "status": status,
            "chunks": 1 if status == "indexed" else 0,
        }

    monkeypatch.setattr(KnowledgeIndexer, "_index_conversion", fake)
    return SimpleNamespace(calls=calls, outcomes=outcomes)


def _insert_job(
    *,
    status: str = "queued",
    conversion_ids: tuple[str, ...] = (),
    fence: int = 0,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    job_id: str | None = None,
    extra_source: dict | None = None,
) -> dict:
    job_id = job_id or f"idx_lease_{uuid.uuid4().hex}"
    owner_id = f"owner-{uuid.uuid4().hex[:8]}"
    collection_id = f"col-{uuid.uuid4().hex[:8]}"
    source = {"conversion_ids": list(conversion_ids), "source_paths": [], "scope": "documents"}
    source.update(extra_source or {})
    session = get_session_factory()()
    try:
        session.add(
            KnowledgeIndexJobRow(
                id=job_id,
                owner_id=owner_id,
                collection_id=collection_id,
                status=status,
                source_json=source,
                fence=fence,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
            )
        )
        session.commit()
    finally:
        session.close()
    return {"job_id": job_id, "owner_id": owner_id, "collection_id": collection_id}


def _state(job_id: str) -> dict:
    session = get_session_factory()()
    try:
        row = session.get(KnowledgeIndexJobRow, job_id)
        return {
            "status": row.status,
            "fence": row.fence,
            "attempt": row.attempt,
            "lease_owner": row.lease_owner,
            "lease_expires_at": _utc(row.lease_expires_at),
            "stats": row.stats_json,
            "error_code": row.error_code,
        }
    finally:
        session.close()


def test_claim_is_compare_and_set(jobs_db):
    runner = KnowledgeJobRunner()
    job = _insert_job()
    now = datetime.now(UTC)

    first = runner.claim_job(job["job_id"], worker_id="worker-a", now=now)
    second = runner.claim_job(job["job_id"], worker_id="worker-b", now=now)

    assert first is not None and first.fence == 1
    assert second is None, "a queued job must be leased by exactly one worker"
    state = _state(job["job_id"])
    assert state["status"] == "running"
    assert state["lease_owner"] == "worker-a"
    assert state["attempt"] == 1
    assert state["lease_expires_at"] == now + timedelta(seconds=LEASE_SECONDS)


def test_crashed_job_is_recovered_and_dispatched_exactly_once(jobs_db, index_fakes):
    now = datetime.now(UTC)
    job = _insert_job(
        conversion_ids=("c1", "c2"),
        status="running",
        fence=1,
        lease_owner="dead-worker",
        lease_expires_at=now - timedelta(seconds=5),
    )
    runner = KnowledgeJobRunner()

    runner.run_pending(worker_id="worker-b", now=now, limit=500)
    runner.run_pending(worker_id="worker-c", now=now, limit=500)

    mine = [conversion for collection, conversion in index_fakes.calls if collection == job["collection_id"]]
    assert mine == ["c1", "c2"], "each conversion indexed once, by one dispatcher"
    state = _state(job["job_id"])
    assert state["status"] == "completed"
    assert state["fence"] == 2
    assert state["attempt"] == 1


def test_stale_worker_cannot_overwrite_a_reclaimed_job(jobs_db, index_fakes):
    runner = KnowledgeJobRunner()
    job = _insert_job(conversion_ids=("c1",))
    job_id = job["job_id"]
    t0 = datetime.now(UTC)

    stale = runner.claim_job(job_id, worker_id="worker-a", now=t0)
    later = t0 + timedelta(seconds=LEASE_SECONDS + 1)
    assert job_id in runner.recover(now=later)
    fresh = runner.claim_job(job_id, worker_id="worker-b", now=later)

    assert fresh.fence == stale.fence + 1
    assert runner.heartbeat(job_id, stale.fence, now=later) is False
    assert runner.finish_claim(job_id, stale.fence, status="completed", stats={"chunks": 99}, error=None) is False
    state = _state(job_id)
    assert state["status"] == "running"
    assert state["lease_owner"] == "worker-b"
    assert state["stats"] is None

    assert runner.finish_claim(job_id, fresh.fence, status="completed", stats={"chunks": 1}, error=None) is True
    assert _state(job_id)["status"] == "completed"


def test_recover_requeues_only_expired_or_unleased_jobs(jobs_db):
    now = datetime.now(UTC)
    live = _insert_job(status="running", fence=1, lease_owner="alive", lease_expires_at=now + timedelta(seconds=60))
    dead = _insert_job(status="running", fence=1, lease_owner="dead", lease_expires_at=now - timedelta(seconds=1))

    recovered = KnowledgeJobRunner().recover(now=now)

    assert dead["job_id"] in recovered
    assert live["job_id"] not in recovered
    assert _state(live["job_id"])["status"] == "running"
    assert _state(dead["job_id"])["status"] == "queued"


def test_heartbeat_extends_the_lease_only_for_the_current_fence(jobs_db):
    runner = KnowledgeJobRunner()
    job = _insert_job()
    now = datetime.now(UTC)
    lease = runner.claim_job(job["job_id"], worker_id="worker-a", now=now)

    later = now + timedelta(seconds=90)
    assert runner.heartbeat(job["job_id"], lease.fence, now=later) is True
    assert _state(job["job_id"])["lease_expires_at"] == later + timedelta(seconds=LEASE_SECONDS)
    assert runner.heartbeat(job["job_id"], lease.fence + 1, now=later) is False


def test_some_documents_failing_marks_the_job_partial(jobs_db, index_fakes):
    runner = KnowledgeJobRunner()
    job = _insert_job(conversion_ids=("ok-1", "bad-1"))
    index_fakes.outcomes["bad-1"] = "skipped"
    now = datetime.now(UTC)
    lease = runner.claim_job(job["job_id"], worker_id="worker-a", now=now)

    runner.execute(lease, now=now)

    state = _state(job["job_id"])
    assert state["status"] == "partial"
    assert state["error_code"] == "INDEX_PARTIAL"
    assert state["stats"]["chunks"] == 1
    assert state["stats"]["failed"] == 1


def test_every_document_failing_marks_the_job_failed(jobs_db, index_fakes):
    runner = KnowledgeJobRunner()
    job = _insert_job(conversion_ids=("bad-1",))
    index_fakes.outcomes["bad-1"] = "skipped"
    now = datetime.now(UTC)
    lease = runner.claim_job(job["job_id"], worker_id="worker-a", now=now)

    runner.execute(lease, now=now)

    assert _state(job["job_id"])["status"] == "failed"


@pytest.mark.parametrize(
    ("outcome", "expected_run_state"),
    [("skipped", "partial"), ("indexed", "succeeded")],
)
def test_job_outcome_closes_the_waiting_occurrence(jobs_db, index_fakes, outcome, expected_run_state):
    from automation.models import AutomationRunRow

    runner = KnowledgeJobRunner()
    job = _insert_job(conversion_ids=("ok-1", "second-1"))
    index_fakes.outcomes["second-1"] = outcome
    run_id = f"arun_{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    session = get_session_factory()()
    try:
        session.add(
            AutomationRunRow(
                id=run_id,
                owner_id=job["owner_id"],
                schedule_id="sched_test",
                schedule_revision=1,
                scheduled_for=now,
                trigger="manual",
                occurrence_key=f"test:{run_id}",
                snapshot_json={},
                state="waiting_children",
                phase="index",
                index_job_id=job["job_id"],
            )
        )
        session.commit()
    finally:
        session.close()

    lease = runner.claim_job(job["job_id"], worker_id="worker-a", now=now)
    runner.execute(lease, now=now)

    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        assert run.state == expected_run_state
        assert run.finished_at is not None
    finally:
        session.close()


def test_index_job_lease_is_renewed_while_a_conversion_is_still_indexing(jobs_db, monkeypatch):
    """A long conversion must not let the lease lapse under a live claim."""
    import knowledge.job_runner as job_runner
    from knowledge.indexer import KnowledgeIndexer

    monkeypatch.setattr(job_runner, "HEARTBEAT_SECONDS", 0.02)
    runner = KnowledgeJobRunner()
    job = _insert_job(conversion_ids=("slow-1",))
    lease = runner.claim_job(job["job_id"], worker_id="worker-a")

    heartbeats: list[str] = []
    original_heartbeat = KnowledgeJobRunner.heartbeat

    def counting_heartbeat(self, job_id, fence, *, now=None):
        heartbeats.append(job_id)
        return original_heartbeat(self, job_id, fence, now=now)

    def slow(self, owner_id, collection_id, conversion_id):  # noqa: ARG001
        time.sleep(0.25)
        return {"document_id": conversion_id, "status": "indexed", "chunks": 1}

    monkeypatch.setattr(KnowledgeJobRunner, "heartbeat", counting_heartbeat)
    monkeypatch.setattr(KnowledgeIndexer, "_index_conversion", slow)
    runner.execute(lease)

    # One heartbeat before the conversion plus renewals while it was still
    # running: without the keeper there would be only the first one.
    assert len(heartbeats) >= 4, heartbeats
    assert _state(job["job_id"])["status"] == "completed"


def test_create_is_idempotent_for_a_deterministic_job_id(jobs_db):
    runner = KnowledgeJobRunner()
    job_id = f"idx_det_{uuid.uuid4().hex}"
    first = runner.create(owner_id="owner-det", collection_id="col-det", conversion_ids=["x"], job_id=job_id)
    second = runner.create(owner_id="owner-det", collection_id="col-det", conversion_ids=["x"], job_id=job_id)

    assert first["job_id"] == second["job_id"] == job_id
    session = get_session_factory()()
    try:
        assert session.get(KnowledgeIndexJobRow, job_id) is not None
    finally:
        session.close()


def test_legacy_in_process_jobs_are_not_leased_by_the_dispatcher(jobs_db):
    """User-facing index jobs run on their own thread: lease recovery must skip them."""
    from knowledge.indexer import KnowledgeIndexer

    job_id = f"idx_legacy_{uuid.uuid4().hex}"
    _insert_job(
        job_id=job_id,
        status="running",
        conversion_ids=("c1",),
        extra_source={"dispatch": KnowledgeIndexer.LEGACY_DISPATCH},
    )

    runner = KnowledgeJobRunner()
    assert job_id not in runner.recover(now=datetime.now(UTC) + timedelta(hours=1))
    assert _state(job_id)["status"] == "running"
    assert runner.claim_job(job_id, worker_id="worker-x") is None


def test_additive_columns_are_applied_to_an_existing_database(tmp_path: Path):
    from sqlalchemy import create_engine

    from models.db import _ensure_additive_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE knowledge_index_jobs (id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(64), status VARCHAR(24))"
        )
        conn.exec_driver_sql("CREATE TABLE automation_runs (id VARCHAR(64) PRIMARY KEY, state VARCHAR(32))")
        conn.exec_driver_sql("CREATE TABLE public_conversions (id VARCHAR(64) PRIMARY KEY)")

    _ensure_additive_columns(engine)
    _ensure_additive_columns(engine)  # idempotent

    with engine.connect() as conn:
        job_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(knowledge_index_jobs)")}
        run_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(automation_runs)")}
    assert {"attempt", "fence", "lease_owner", "lease_expires_at"} <= job_cols
    assert "next_attempt_at" in run_cols


def test_alembic_head_adds_lease_and_retry_columns(tmp_path: Path):
    from sqlalchemy import create_engine

    service_dir = Path(__file__).resolve().parents[1]
    db = tmp_path / "head.db"
    url = f"sqlite:///{db.as_posix()}"
    env = {**os.environ, "CONVERTER_DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(service_dir / "alembic.ini"), "upgrade", "head"],
        cwd=str(service_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(url)
    with engine.connect() as conn:
        job_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(knowledge_index_jobs)")}
        run_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(automation_runs)")}
    assert {"attempt", "fence", "lease_owner", "lease_expires_at"} <= job_cols
    assert "next_attempt_at" in run_cols
