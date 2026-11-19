"""Regression tests for the automation audit (FX-1c: AU1-*, S4, S5, S15, S17).

Each test pins one defect so it cannot come back silently. Tests share the
isolated test database with the other automation suites, so every test starts
from empty automation tables.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import delete, event, select

from config import settings

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = {"x-internal-token": "test-internal-token"}
T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_automation_tables(monkeypatch):
    from automation.models import AutomationEventRow, AutomationRunRow, AutomationScheduleRow
    from models.db import get_session_factory, init_db

    init_db()
    session = get_session_factory()()
    try:
        session.execute(delete(AutomationEventRow))
        session.execute(delete(AutomationRunRow))
        session.execute(delete(AutomationScheduleRow))
        session.commit()
    finally:
        session.close()
    # Run-now is gated by the executor flag; these tests exercise it enabled.
    monkeypatch.setattr(settings, "automation_executor_enabled", True)
    yield


def _factory():
    from models.db import get_session_factory

    return get_session_factory()


def _make_schedule(
    *,
    owner: str = "owner-a",
    source_id: str = "src-missing",
    misfire: str = "run_once_latest",
    next_run_at: datetime = datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
    rule: dict | None = None,
) -> str:
    from automation.repository import AutomationRepository

    session = _factory()()
    try:
        schedule = AutomationRepository().create_schedule(
            session,
            owner_id=owner,
            name="Agenda",
            source_id=source_id,
            rule=rule or {"kind": "interval", "every_seconds": 3600, "anchor_at": "2026-11-19T11:00:00Z"},
            timezone="UTC",
            next_run_at=next_run_at,
            misfire_policy=misfire,
        )
        session.commit()
        return schedule.id
    finally:
        session.close()


def _make_manual_run(schedule_id: str, owner: str, key: str | None = None) -> str:
    from automation.scheduler import AutomationScheduler

    session = _factory()()
    try:
        run = AutomationScheduler(worker_id="test").create_manual_occurrence(
            session, schedule_id=schedule_id, owner_id=owner, request_key=key or uuid.uuid4().hex
        )
        session.commit()
        return run.id
    finally:
        session.close()


def _run_row(run_id: str):
    """Fresh read: SQLite hands back naive datetimes, as in production."""
    from automation.models import AutomationRunRow

    session = _factory()()
    try:
        run = session.get(AutomationRunRow, run_id)
        if run is None:
            return None
        session.expunge(run)
        return run
    finally:
        session.close()


# ------------------------------------------------------------- AU1-01 imports


@pytest.mark.parametrize(
    "module",
    [
        "automation.cli",
        "sources.repository",
        "automation.loop",
        "automation.service",
        "automation.run_service",
        "automation.cleanup",
        "models.db",
    ],
)
def test_modules_import_in_a_fresh_interpreter(module: str):
    """models.db re-exports the automation/sources models at its bottom; each
    package must import cleanly as the first thing a process touches."""
    code = (
        "import sys; sys.path.insert(0, %r); import %s; print('OK')" % (str(ROOT), module)
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "OK" in completed.stdout


# ------------------------------------------------- AU1-02 naive next_run / S1


def test_skip_policy_with_naive_next_run_at_does_not_abort_tick():
    """SQLite returns naive datetimes; comparing them with an aware `now`
    raised TypeError and aborted the whole calendar tick."""
    from automation.calendar import occurrence_key
    from automation.scheduler import AutomationScheduler

    schedule_id = _make_schedule(misfire="skip", next_run_at=datetime(2026, 11, 19, 11, 0, tzinfo=UTC))

    session = _factory()()
    try:
        created = AutomationScheduler(worker_id="t").create_due_occurrences(session, now=T0)
        session.commit()
        assert len(created) == 1
        assert created[0].state == "skipped"
        # The occurrence key is the UTC instant, never host-local time.
        assert created[0].occurrence_key == occurrence_key(
            schedule_id, 1, datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
        )
    finally:
        session.close()


def test_failing_schedule_does_not_discard_other_schedules_occurrences(monkeypatch):
    """One bad schedule is logged and skipped; occurrences already created for
    other schedules in the same pass stay committed (no shared rollback)."""
    from automation.models import AutomationRunRow
    from automation.scheduler import AutomationScheduler

    good = _make_schedule(owner="owner-a", next_run_at=datetime(2026, 11, 19, 10, 0, tzinfo=UTC))
    bad = _make_schedule(owner="owner-b", next_run_at=datetime(2026, 11, 19, 10, 30, tzinfo=UTC))

    original = AutomationScheduler._advance_and_record

    def flaky(self, session, *, schedule_id, **kwargs):
        if schedule_id == bad:
            raise RuntimeError("falha simulada na agenda ruim")
        return original(self, session, schedule_id=schedule_id, **kwargs)

    monkeypatch.setattr(AutomationScheduler, "_advance_and_record", flaky)

    session = _factory()()
    try:
        created = AutomationScheduler(worker_id="t").create_due_occurrences(session, now=T0)
    finally:
        session.close()
    assert len(created) == 1

    check = _factory()()
    try:
        rows = list(check.execute(select(AutomationRunRow)).scalars())
        assert [row.schedule_id for row in rows] == [good]
    finally:
        check.close()


# ------------------------------------------------------ AU1-10 UTC on the wire


def test_schedule_payload_timestamps_carry_explicit_utc_offset():
    from automation.models import AutomationScheduleRow
    from automation.service import ScheduleService

    schedule_id = _make_schedule()
    session = _factory()()
    try:
        row = session.get(AutomationScheduleRow, schedule_id)
        assert row.next_run_at.tzinfo is None  # the SQLite round trip
        payload = ScheduleService().schedule_payload(row)
    finally:
        session.close()
    assert payload["next_run_at"].endswith("+00:00")
    assert payload["created_at"].endswith("+00:00")
    assert payload["updated_at"].endswith("+00:00")


def test_run_payload_timestamps_carry_explicit_utc_offset():
    from automation.run_service import AutomationRunService

    schedule_id = _make_schedule()
    run_id = _make_manual_run(schedule_id, "owner-a")
    session = _factory()()
    try:
        from automation.models import AutomationRunRow

        row = session.get(AutomationRunRow, run_id)
        payload = AutomationRunService.payload(row)
        assert payload["scheduled_for"].endswith("+00:00")
        assert payload["created_at"].endswith("+00:00")
        assert payload["updated_at"].endswith("+00:00")
    finally:
        session.close()


# -------------------------------------------------- AU1-11 / AU1-35 cleanup


def _insert_run(run_id: str, *, state: str, finished_at: datetime | None, created_at: datetime) -> None:
    from automation.models import AutomationRunRow

    session = _factory()()
    try:
        session.add(
            AutomationRunRow(
                id=run_id,
                owner_id="owner-a",
                schedule_id="sch_cleanup",
                schedule_revision=1,
                scheduled_for=created_at,
                trigger="schedule",
                occurrence_key=f"sch_cleanup:1:{run_id}",
                snapshot_json={},
                state=state,
                phase="finalize",
                finished_at=finished_at,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.commit()
    finally:
        session.close()


def test_history_cleanup_handles_naive_finish_times():
    """Finish times come back naive from SQLite; the cutoff is aware."""
    from automation.cleanup import cleanup_history
    from automation.models import AutomationRunRow

    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    _insert_run("arun_old_ok", state="succeeded", finished_at=now - timedelta(days=120), created_at=now - timedelta(days=120))
    _insert_run("arun_old_fail", state="failed", finished_at=now - timedelta(days=10), created_at=now - timedelta(days=10))
    _insert_run("arun_recent_fail", state="failed", finished_at=now - timedelta(days=2), created_at=now - timedelta(days=2))

    result = cleanup_history(now=now)  # must not raise TypeError
    assert result["runs_removed"] >= 2

    check = _factory()()
    try:
        remaining = {row.id for row in check.execute(select(AutomationRunRow)).scalars()}
    finally:
        check.close()
    assert "arun_old_ok" not in remaining
    assert "arun_old_fail" not in remaining  # failures keep 7 days, not 90
    assert "arun_recent_fail" in remaining


def test_snapshot_cleanup_removes_captured_items_of_terminal_failed_import(tmp_path: Path, monkeypatch):
    from automation.cleanup import cleanup_snapshots
    from config import settings as cfg
    from models.db import SourceImportItemRow, SourceImportRow

    staging = Path(cfg.data_dir) / "sources" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    key = f"regress-{uuid.uuid4().hex}.bin"
    (staging / key).write_bytes(b"snapshot")
    import_id = f"imp_{uuid.uuid4().hex}"

    session = _factory()()
    try:
        session.add(
            SourceImportRow(
                id=import_id,
                owner_id="owner-cleanup",
                source_id="src-cleanup",
                source_version=1,
                idempotency_key=uuid.uuid4().hex,
                request_hash="0" * 64,
                status="failed",
                definition_json={},
                options_json={},
            )
        )
        session.flush()
        session.add(
            SourceImportItemRow(
                import_id=import_id,
                relative_path="a.pdf",
                client_item_id="c1",
                snapshot_key=key,
                status="captured",
            )
        )
        session.commit()
    finally:
        session.close()

    cleanup_snapshots()
    assert not (staging / key).exists()


# --------------------------------------------------------- AU1-12 fence/lease


def test_long_run_past_its_lease_still_finalizes_with_valid_fence():
    """The fence, not the wall-clock lease, decides who may finalize. A run
    longer than LEASE_SECONDS whose claim was never reclaimed must finish."""
    from automation.executor import AutomationExecutor
    from automation.models import AutomationRunRow
    from automation.scheduler import AutomationScheduler

    schedule_id = _make_schedule()
    run_id = _make_manual_run(schedule_id, "owner-a")
    scheduler = AutomationScheduler(worker_id="w-long")
    session = _factory()()
    try:
        lease = scheduler.claim_due(session, now=T0, worker_id="w-long")
        session.commit()
        assert lease is not None and lease.run_id == run_id
        fence = lease.fence
        # Simulate a cancellation requested while the attempt is alive.
        row = session.get(AutomationRunRow, run_id)
        row.cancel_requested_at = T0
        session.commit()
    finally:
        session.close()

    # The attempt has been running for 200 s: well past the 120 s lease.
    executor = AutomationExecutor(clock=lambda: T0 + timedelta(seconds=200))
    progress = executor.advance(run_id, "owner-a", fence=fence)

    assert progress.state == "cancelled"
    persisted = _run_row(run_id)
    assert persisted.state == "cancelled"
    assert persisted.finished_at is not None


# ----------------------------------------------------- AU1-13 run-now guard


def test_start_async_refuses_when_executor_disabled(monkeypatch):
    from automation.run_service import AutomationRunService
    from automation.types import FeatureDisabled

    monkeypatch.setattr(settings, "automation_executor_enabled", False)
    schedule_id = _make_schedule(owner="owner-a")
    run_id = _make_manual_run(schedule_id, "owner-a")

    with pytest.raises(FeatureDisabled) as info:
        AutomationRunService().start_async(run_id, "owner-a")
    assert info.value.code == "FEATURE_DISABLED"

    # Nothing was claimed: the occurrence is still queued for a later executor.
    assert _run_row(run_id).state == "queued"


def test_start_async_claims_before_advancing_and_blocks_double_advance(monkeypatch):
    from automation.executor import AutomationExecutor
    from automation.run_service import AutomationRunService
    from automation.scheduler import AutomationScheduler

    calls: list[tuple[str, int | None]] = []

    def fake_advance(self, run_id, owner_id=None, *, fence=None):
        calls.append((run_id, fence))

    monkeypatch.setattr(AutomationExecutor, "advance", fake_advance)

    schedule_id = _make_schedule(owner="owner-a")
    run_id = _make_manual_run(schedule_id, "owner-a")

    assert AutomationRunService().start_async(run_id, "owner-a") is True
    for _ in range(100):
        if calls:
            break
        time.sleep(0.02)
    assert calls and calls[0][0] == run_id and calls[0][1] == 1

    persisted = _run_row(run_id)
    assert persisted.state == "running"
    assert persisted.fence == 1
    assert persisted.lease_owner

    # A second trigger (or the loop) cannot claim the same occurrence again.
    assert AutomationRunService().start_async(run_id, "owner-a") is False
    session = _factory()()
    try:
        # Real clock: the lease written by start_async is live right now.
        assert AutomationScheduler(worker_id="loop").claim_due(session) is None
    finally:
        session.close()
    assert len(calls) == 1


def test_start_async_rejects_another_owners_run(monkeypatch):
    from automation.executor import AutomationExecutor
    from automation.run_service import AutomationRunService

    monkeypatch.setattr(AutomationExecutor, "advance", lambda *a, **k: None)
    schedule_id = _make_schedule(owner="owner-a")
    run_id = _make_manual_run(schedule_id, "owner-a")
    assert AutomationRunService().start_async(run_id, "owner-b") is False
    assert _run_row(run_id).state == "queued"


# ----------------------------------------------------- AU1-34 naive key / UTC


def test_occurrence_key_treats_naive_instants_as_utc():
    from automation.calendar import occurrence_key

    naive = datetime(2026, 11, 19, 12, 0)
    aware = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    assert occurrence_key("sch_x", 1, naive) == occurrence_key("sch_x", 1, aware)
    assert occurrence_key("sch_x", 1, naive).endswith("20261119T120000000000Z")


# ------------------------------------------------------ AU1-36 owner scoping


def test_manual_run_request_key_is_scoped_to_the_owner():
    from automation.scheduler import AutomationScheduler, ClaimError

    schedule_id = _make_schedule(owner="owner-a")
    key = "same-request-key"
    run_id = _make_manual_run(schedule_id, "owner-a", key=key)

    session = _factory()()
    try:
        with pytest.raises(ClaimError) as info:
            AutomationScheduler(worker_id="t").create_manual_occurrence(
                session, schedule_id=schedule_id, owner_id="owner-b", request_key=key
            )
        assert info.value.code == "SCHEDULE_NOT_FOUND"
    finally:
        session.close()
    assert _run_row(run_id).owner_id == "owner-a"


# ------------------------------------------------------- S4 atomic revision


def test_update_schedule_revision_check_is_atomic(monkeypatch):
    """A caller holding a stale read must not overwrite a newer revision even
    if the read it made was stale when the write happens."""
    from automation.models import AutomationScheduleRow
    from automation.repository import AutomationRepository

    schedule_id = _make_schedule(owner="owner-rev")

    stale_session = _factory()()
    stale = stale_session.get(AutomationScheduleRow, schedule_id)
    assert stale.revision == 1

    # Another writer wins first.
    winner = _factory()()
    try:
        AutomationRepository().update_schedule(
            winner, schedule_id=schedule_id, owner_id="owner-rev",
            expected_revision=1, changes={"name": "Vencedora"},
        )
        winner.commit()
    finally:
        winner.close()

    repo = AutomationRepository()
    monkeypatch.setattr(repo, "get_schedule", lambda *a, **k: stale)
    result = repo.update_schedule(
        stale_session, schedule_id=schedule_id, owner_id="owner-rev",
        expected_revision=1, changes={"name": "Atrasada"},
    )
    stale_session.rollback()
    stale_session.close()
    assert result is None

    check = _factory()()
    try:
        row = check.get(AutomationScheduleRow, schedule_id)
        assert row.name == "Vencedora"
        assert row.revision == 2
    finally:
        check.close()


# ---------------------------------------------------- S5 conditional claim


def test_a_claimed_occurrence_cannot_be_claimed_again_and_fence_bumps_once():
    """The claim is a fenced conditional update: after a committed claim, a
    second claimant gets nothing and the fence advanced exactly once."""
    from automation.scheduler import AutomationScheduler

    run_id = _make_manual_run(_make_schedule(owner="owner-c"), "owner-c")

    session = _factory()()
    try:
        winner = AutomationScheduler(worker_id="w-a").claim_due(session, now=T0)
        session.commit()
    finally:
        session.close()
    assert winner is not None and winner.run_id == run_id

    session = _factory()()
    try:
        loser = AutomationScheduler(worker_id="w-b").claim_due(session, now=T0)
        session.commit()
    finally:
        session.close()
    assert loser is None
    persisted = _run_row(run_id)
    assert persisted.fence == 1 and persisted.attempt == 1


# ------------------------------------------------ S15 waiting_children guard


def test_waiting_children_does_not_overwrite_a_concurrent_cancelling():
    from automation.executor import AutomationExecutor
    from automation.models import AutomationRunRow

    run_id = _make_manual_run(_make_schedule(owner="owner-d"), "owner-d")
    session = _factory()()
    try:
        row = session.get(AutomationRunRow, run_id)
        row.state = "cancelling"
        row.fence = 3
        session.commit()
    finally:
        session.close()

    moved = AutomationExecutor()._mark_waiting_children(run_id, 3)
    assert moved is False
    assert _run_row(run_id).state == "cancelling"


def test_waiting_children_marks_a_running_occurrence_under_its_fence():
    from automation.executor import AutomationExecutor
    from automation.models import AutomationRunRow

    run_id = _make_manual_run(_make_schedule(owner="owner-e"), "owner-e")
    session = _factory()()
    try:
        row = session.get(AutomationRunRow, run_id)
        row.state = "running"
        row.fence = 4
        session.commit()
    finally:
        session.close()

    assert AutomationExecutor()._mark_waiting_children(run_id, 99) is False  # stale fence
    assert AutomationExecutor()._mark_waiting_children(run_id, 4) is True
    assert _run_row(run_id).state == "waiting_children"


# ------------------------------------------------------- S17 recovery scope


def test_recover_queries_only_non_terminal_occurrences():
    from automation.loop import AutomationLoop
    from models.db import get_engine

    _make_manual_run(_make_schedule(owner="owner-f"), "owner-f")
    statements: list[str] = []
    engine = get_engine()

    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "automation_runs" in statement and statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        AutomationLoop(worker_id="recover-scope").recover()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert statements, "recover() must read automation_runs"
    assert all("state IN" in statement or "state in" in statement for statement in statements), statements


# ------------------------------------------------ AU1-33 options at the edge


def _source_for_api(api_client, tmp_path: Path) -> tuple[dict, str]:
    token = api_client.post("/api/v1/auth/anonymous", json={}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    owner_id = api_client.get("/api/v1/auth/me", headers=headers).json()["id"]
    root = tmp_path / f"raiz-{uuid.uuid4().hex[:6]}"
    (root / "docs").mkdir(parents=True)
    wb = Workbook()
    wb.active.title = "Plan"
    wb.active.append(["A"])
    wb.active.append(["x"])
    buffer = io.BytesIO()
    wb.save(buffer)
    (root / "docs" / "plan.xlsx").write_bytes(buffer.getvalue())
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner_id},
        headers=INTERNAL,
    )
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": grant.json()["id"], "relative_path": "docs", "kind": "directory", "name": "Docs"},
        headers=headers,
    ).json()
    return {"id": owner_id, "headers": headers}, source["id"]


def _body(source_id: str, **extra) -> dict:
    body = {
        "name": "Opções",
        "source_id": source_id,
        "rule": {"kind": "interval", "every_seconds": 3600, "anchor_at": "2026-11-19T11:00:00Z"},
        "timezone": "UTC",
    }
    body.update(extra)
    return body


def test_schedule_rejects_unknown_options_with_invalid_option(api_client, tmp_path: Path):
    owner, source_id = _source_for_api(api_client, tmp_path)
    response = api_client.post(
        "/api/v1/schedules",
        json=_body(source_id, options={"nao_existe": 1}),
        headers=owner["headers"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_OPTION"


def test_valid_schedule_options_are_stored_raw_and_run_through_the_importer(api_client, tmp_path: Path):
    """Stored options must re-normalize cleanly when the occurrence runs. The
    normalizer's own output (spreadsheet `reader_version` stamp) is rejected on
    a second pass, so the raw validated input is what is stored."""
    from sources.importer import ImportService
    from sources.types import SourceDefinitionSnapshot

    owner, source_id = _source_for_api(api_client, tmp_path)
    created = api_client.post(
        "/api/v1/schedules",
        json=_body(source_id, options={"target_formats": ["txt"]}),
        headers=owner["headers"],
    )
    assert created.status_code == 201, created.text
    stored = created.json()["options"]
    assert stored == {"target_formats": ["txt"]}

    definition = SourceDefinitionSnapshot.from_json(
        {
            "source_id": source_id,
            "root_id": "root",
            "root_canonical_path": str(tmp_path),
            "relative_path": "docs",
            "kind": "directory",
            "recursive": False,
            "include_patterns": None,
            "exclude_patterns": None,
            "version": 1,
        }
    )
    normalized = ImportService()._normalize_for_source(stored, definition)
    assert normalized["target_formats"] == ["txt"]


def test_update_with_null_options_is_normalized_not_a_server_error(api_client, tmp_path: Path):
    owner, source_id = _source_for_api(api_client, tmp_path)
    created = api_client.post("/api/v1/schedules", json=_body(source_id), headers=owner["headers"]).json()
    response = api_client.patch(
        f"/api/v1/schedules/{created['id']}",
        json={"revision": created["revision"], "options": None},
        headers=owner["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["options"] == {}


def test_update_rejects_invalid_options(api_client, tmp_path: Path):
    owner, source_id = _source_for_api(api_client, tmp_path)
    created = api_client.post("/api/v1/schedules", json=_body(source_id), headers=owner["headers"]).json()
    response = api_client.patch(
        f"/api/v1/schedules/{created['id']}",
        json={"revision": created["revision"], "options": {"nao_existe": True}},
        headers=owner["headers"],
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_OPTION"
