"""T7/T8 — schedule API, executor states and occurrence history."""

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


def _session(api_client) -> dict:
    token = api_client.post("/api/v1/auth/anonymous", json={}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = api_client.get("/api/v1/auth/me", headers=headers).json()
    return {"id": me["id"], "headers": headers}


def _xlsx(content: str = "dados") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["A", "B"])
    ws.append([content, 1])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _source(api_client, tmp_path: Path) -> tuple[dict, str, Path]:
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner["id"]},
        headers=INTERNAL,
    )
    assert grant.status_code == 201, grant.text
    source = api_client.post(
        "/api/v1/sources",
        json={
            "root_id": grant.json()["id"],
            "relative_path": "docs",
            "kind": "directory",
            "name": "Docs",
        },
        headers=owner["headers"],
    )
    assert source.status_code == 201, source.text
    return owner, source.json()["id"], root


def _schedule_body(source_id: str, **overrides) -> dict:
    body = {
        "name": "Diária",
        "source_id": source_id,
        "rule": {"kind": "daily", "local_time": "08:00"},
        "timezone": "America/Sao_Paulo",
        "misfire_policy": "run_once_latest",
        "overlap_policy": "skip",
        "options": {"schema_version": 3},
        "index_collection_id": None,
    }
    body.update(overrides)
    return body


def test_schedule_crud_and_preview(api_client, tmp_path: Path):
    owner, source_id, _ = _source(api_client, tmp_path)

    preview = api_client.post(
        "/api/v1/schedules/preview",
        json=_schedule_body(source_id),
        headers=owner["headers"],
    )
    assert preview.status_code == 200, preview.text
    occurrences = preview.json()["next_occurrences"]
    assert len(occurrences) == 5
    assert all(instant.endswith("+00:00") for instant in occurrences)

    created = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    )
    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["revision"] == 1
    assert schedule["next_run_at"]

    listed = api_client.get("/api/v1/schedules", headers=owner["headers"]).json()["items"]
    assert any(item["id"] == schedule["id"] for item in listed)

    updated = api_client.patch(
        f"/api/v1/schedules/{schedule['id']}",
        json={"revision": 1, "name": "Diária v2"},
        headers=owner["headers"],
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2

    stale = api_client.patch(
        f"/api/v1/schedules/{schedule['id']}",
        json={"revision": 1, "name": "Concorrente"},
        headers=owner["headers"],
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

    paused = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/pause", headers=owner["headers"]
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False

    resumed = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/resume", headers=owner["headers"]
    )
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True


def test_foreign_owner_cannot_touch_schedule(api_client, tmp_path: Path):
    owner, source_id, _ = _source(api_client, tmp_path)
    other = _session(api_client)
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()

    assert api_client.get(
        f"/api/v1/schedules/{schedule['id']}", headers=other["headers"]
    ).status_code == 404
    assert api_client.post(
        f"/api/v1/schedules/{schedule['id']}/pause", headers=other["headers"]
    ).status_code == 404
    assert api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**other["headers"], "Idempotency-Key": "x1"},
    ).status_code in (404, 409)


def test_schedule_requires_existing_source(api_client, tmp_path: Path):
    owner = _session(api_client)
    response = api_client.post(
        "/api/v1/schedules",
        json=_schedule_body("src_inexistente"),
        headers=owner["headers"],
    )
    assert response.status_code == 404


def test_invalid_rule_and_timezone_rejected(api_client, tmp_path: Path):
    owner, source_id, _ = _source(api_client, tmp_path)
    bad_interval = api_client.post(
        "/api/v1/schedules",
        json=_schedule_body(source_id, rule={"kind": "interval", "every_seconds": 30}),
        headers=owner["headers"],
    )
    assert bad_interval.status_code == 400
    assert bad_interval.json()["error"]["code"] == "INVALID_SCHEDULE_RULE"

    bad_tz = api_client.post(
        "/api/v1/schedules",
        json=_schedule_body(source_id, timezone="Marte/Olympus"),
        headers=owner["headers"],
    )
    assert bad_tz.status_code == 400
    assert bad_tz.json()["error"]["code"] == "INVALID_TIMEZONE"


def test_run_now_executes_occurrence_end_to_end(api_client, tmp_path: Path):
    owner, source_id, root = _source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx("manual"))
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()

    response = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    for _ in range(200):
        detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
        if detail["state"] in {"succeeded", "partial", "no_changes", "failed", "cancelled", "skipped"}:
            break
        time.sleep(0.15)
    assert detail["state"] == "succeeded", detail
    assert detail["import_id"]

    # history and events
    runs = api_client.get(
        f"/api/v1/schedules/{schedule['id']}/runs", headers=owner["headers"]
    ).json()["items"]
    assert any(item["id"] == run_id for item in runs)

    events = api_client.get(
        f"/api/v1/schedule-runs/{run_id}/events", headers=owner["headers"]
    )
    if events.status_code == 200 and "text/event-stream" not in events.headers.get("content-type", ""):
        assert isinstance(events.json(), (dict, list))


def test_run_now_is_idempotent_by_key(api_client, tmp_path: Path):
    owner, source_id, root = _source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx())
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    key = uuid.uuid4().hex
    first = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": key},
    ).json()
    second = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": key},
    ).json()
    assert second["run_id"] == first["run_id"]


def test_no_changes_state_when_source_is_unchanged(api_client, tmp_path: Path):
    owner, source_id, root = _source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx("estavel"))
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()

    def run_now(key: str) -> dict:
        run_id = api_client.post(
            f"/api/v1/schedules/{schedule['id']}/run-now",
            headers={**owner["headers"], "Idempotency-Key": key},
        ).json()["run_id"]
        for _ in range(200):
            detail = api_client.get(
                f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]
            ).json()
            if detail["state"] in {
                "succeeded", "partial", "no_changes", "failed", "cancelled", "skipped"
            }:
                return detail
            time.sleep(0.15)
        return detail

    assert run_now(uuid.uuid4().hex)["state"] == "succeeded"
    assert run_now(uuid.uuid4().hex)["state"] == "no_changes"


def test_cancel_is_durable_and_idempotent(api_client, tmp_path: Path):
    owner, source_id, root = _source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx())
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    run_id = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    ).json()["run_id"]

    first = api_client.post(f"/api/v1/schedule-runs/{run_id}/cancel", headers=owner["headers"])
    assert first.status_code == 202
    second = api_client.post(f"/api/v1/schedule-runs/{run_id}/cancel", headers=owner["headers"])
    assert second.status_code == 202
    # cancelling twice never errors nor erases a published result
    detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
    assert detail["state"] in {"cancelling", "cancelled", "succeeded", "partial", "no_changes"}


def test_loop_tick_creates_and_executes_due_occurrence(api_client, tmp_path: Path):
    from datetime import datetime as _dt

    from automation.loop import AutomationLoop

    owner, source_id, root = _source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx("loop"))
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()

    loop = AutomationLoop(poll_seconds=60, worker_id="test-loop")
    # Force the schedule due by running the tick at a time after next_run_at.
    from models.db import AutomationScheduleRow, get_session_factory

    session = get_session_factory()()
    try:
        row = session.get(AutomationScheduleRow, schedule["id"])
        row.next_run_at = _dt(2026, 11, 19, 11, 0, tzinfo=UTC)
        session.commit()
    finally:
        session.close()

    summary = loop.tick(now=_dt(2026, 11, 19, 12, 0, tzinfo=UTC))
    # Other tests may leave due schedules behind: assert progress, not a
    # global count.
    assert summary["created"] >= 1
    assert summary["claimed"] >= 1

    runs = api_client.get(
        f"/api/v1/schedules/{schedule['id']}/runs", headers=owner["headers"]
    ).json()["items"]
    assert len(runs) == 1
    assert runs[0]["state"] in {"succeeded", "partial", "no_changes"}


def test_loop_recovery_requeues_abandoned_occurrence(api_client, tmp_path: Path):
    from datetime import datetime as _dt

    from automation.loop import AutomationLoop
    from models.db import AutomationRunRow, get_session_factory

    owner, source_id, root = _source(api_client, tmp_path)
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    run_id = _manual_occurrence_for(schedule["id"], owner["id"], "recover-1")
    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        run.state = "running"
        run.lease_until = _dt(2026, 11, 1, 10, 0, tzinfo=UTC)  # long expired
        session.commit()
    finally:
        session.close()

    loop = AutomationLoop(worker_id="test-loop")
    summary = loop.recover()
    assert summary["occurrences"] >= 1
    detail = api_client.get(f"/api/v1/schedule-runs/{run_id}", headers=owner["headers"]).json()
    assert detail["state"] == "queued"


def _manual_occurrence_for(schedule_id: str, owner_id: str, key: str) -> str:
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


def test_run_now_queues_without_thread_when_executor_disabled(api_client, tmp_path: Path, monkeypatch):
    """Flag off: the occurrence is queued for an external runner, no thread starts."""
    from automation.run_service import AutomationRunService
    from config import settings
    from models.db import AutomationRunRow, get_session_factory

    monkeypatch.setattr(settings, "automation_executor_enabled", False)
    calls: list[str] = []
    monkeypatch.setattr(
        AutomationRunService,
        "start_async",
        lambda self, run_id, owner_id: calls.append(run_id) or True,
    )

    owner, source_id, _ = _source(api_client, tmp_path)
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    response = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["executor_active"] is False
    assert body["state"] == "queued"
    assert calls == []

    session = get_session_factory()()
    try:
        run = session.get(AutomationRunRow, body["run_id"])
        assert run.state == "queued"
        assert run.lease_until is None
    finally:
        session.close()


def test_run_now_starts_executor_when_enabled(api_client, tmp_path: Path, monkeypatch):
    from automation.run_service import AutomationRunService

    calls: list[str] = []
    real_start = AutomationRunService.start_async

    def spy(self, run_id, owner_id):
        calls.append(run_id)
        return real_start(self, run_id, owner_id)

    monkeypatch.setattr(AutomationRunService, "start_async", spy)

    owner, source_id, root = _source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx("ligado"))
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    response = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["executor_active"] is True
    assert calls == [body["run_id"]]


def test_run_now_maps_feature_disabled_to_queued_202(api_client, tmp_path: Path, monkeypatch):
    """If the executor refuses at start time, the request still answers 202 (never 500)."""
    from automation.run_service import AutomationRunService
    from automation.types import FeatureDisabled

    def refuse(self, run_id, owner_id):
        raise FeatureDisabled()

    monkeypatch.setattr(AutomationRunService, "start_async", refuse)

    owner, source_id, _ = _source(api_client, tmp_path)
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    response = api_client.post(
        f"/api/v1/schedules/{schedule['id']}/run-now",
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 202, response.text
    assert response.json()["executor_active"] is False
    assert response.json()["state"] == "queued"


def test_internal_run_now_queues_when_executor_disabled(api_client, tmp_path: Path, monkeypatch):
    from automation.run_service import AutomationRunService
    from config import settings

    monkeypatch.setattr(settings, "automation_executor_enabled", False)
    calls: list[str] = []
    monkeypatch.setattr(
        AutomationRunService,
        "start_async",
        lambda self, run_id, owner_id: calls.append(run_id) or True,
    )

    owner, source_id, _ = _source(api_client, tmp_path)
    schedule = api_client.post(
        "/api/v1/schedules", json=_schedule_body(source_id), headers=owner["headers"]
    ).json()
    response = api_client.post(
        f"/internal/v1/schedules/{schedule['id']}/run-now",
        headers={**INTERNAL, "x-owner-id": owner["id"], "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["executor_active"] is False
    assert body["state"] == "queued"
    assert calls == []
