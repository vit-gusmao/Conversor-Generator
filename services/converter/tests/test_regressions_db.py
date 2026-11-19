"""Regression tests for the durable queue, publication fencing and schema setup.

Covers the FX-2b review findings (AU2-01 .. AU2-26, S1, S2) and the T11 startup
migration safety rules: exclusive lock, consistent backup, refusal of a newer
schema, and the additive upgrade path used by the packaged desktop app.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from jobqueue.repository import JobRepository
from repositories.conversion_repository import ConversionRepository, LeaseLost

SERVICE_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _run(repo: ConversionRepository, run_id: str, conversion_id: str = "conv_reg") -> None:
    repo.create_run(run_id=run_id, conversion_id=conversion_id, engine_version="test")


def _isolate(jobs: JobRepository) -> None:
    """Finishes whatever earlier tests left queued, so claims are unambiguous."""
    while True:
        lease = jobs.claim("cleanup", lease_seconds=1)
        if lease is None:
            return
        jobs.complete(lease.job_id, lease.fence)


def _job_state(job_id: int) -> str:
    from models.db import EngineJobRow, get_session_factory

    session = get_session_factory()()
    try:
        return session.get(EngineJobRow, job_id).state
    finally:
        session.close()


def _page_count(run_id: str) -> int:
    from models.db import PageResultRow, get_session_factory

    session = get_session_factory()()
    try:
        return len(session.query(PageResultRow).filter(PageResultRow.run_id == run_id).all())
    finally:
        session.close()


def _run_row(run_id: str):
    from models.db import Run, get_session_factory

    session = get_session_factory()()
    try:
        row = session.get(Run, run_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def _alembic(database_url: str, *args: str) -> None:
    env = dict(os.environ)
    env["CONVERTER_DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(SERVICE_DIR / "alembic.ini"), *args],
        cwd=str(SERVICE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


# --------------------------------------------------------------------------- #
# AU2-01: claims are compare-and-set on the observed row
# --------------------------------------------------------------------------- #
def test_claim_is_refused_when_the_observed_queued_row_was_already_taken():
    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_reg_cas_queued")
    receipt = jobs.enqueue_once("run_reg_cas_queued")

    observed = jobs.snapshot(receipt.job_id)  # a second claimer reads the queued row
    winner = jobs.claim("worker-a")
    assert winner is not None and winner.job_id == receipt.job_id

    moment = datetime.now(UTC)
    assert jobs.compare_and_claim(observed, "worker-b", moment, moment + timedelta(seconds=60)) is False
    # The stale claimer must not have bumped the fence a second time.
    assert jobs.snapshot(receipt.job_id).fence == winner.fence


def test_stale_expired_observation_cannot_reclaim_a_renewed_lease():
    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_reg_cas_expired")
    receipt = jobs.enqueue_once("run_reg_cas_expired")

    first = jobs.claim("worker-a", lease_seconds=1)
    assert first is not None and first.job_id == receipt.job_id
    # The third claimer saw the lease expire at this moment...
    expired_view = jobs.snapshot(first.job_id)
    future = datetime.now(UTC) + timedelta(seconds=10)
    # ...but the owner renewed it before the claimer acted (same fence, new deadline).
    assert jobs.heartbeat(first.job_id, first.fence, lease_seconds=300) is True

    assert jobs.compare_and_claim(expired_view, "worker-c", future, future + timedelta(seconds=60)) is False
    assert jobs.snapshot(first.job_id).fence == first.fence


# --------------------------------------------------------------------------- #
# AU2-03: heartbeat never resurrects a finished unit
# --------------------------------------------------------------------------- #
def test_heartbeat_does_not_resurrect_a_completed_unit():
    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_reg_hb_done")
    jobs.enqueue_once("run_reg_hb_done")
    lease = jobs.claim("worker-a")
    assert lease is not None
    assert jobs.complete(lease.job_id, lease.fence) is True

    assert jobs.heartbeat(lease.job_id, lease.fence) is False
    assert _job_state(lease.job_id) == "done"


# --------------------------------------------------------------------------- #
# AU2-04: cancelling a queued run terminates its queued units
# --------------------------------------------------------------------------- #
def test_cancelling_a_queued_run_terminates_its_queued_unit():
    from models.db import EngineJobRow, get_session_factory

    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_reg_cancel_queued")
    receipt = jobs.enqueue_once("run_reg_cancel_queued")

    assert jobs.cancel_run("run_reg_cancel_queued") is True
    assert _job_state(receipt.job_id) == "cancelled"
    assert jobs.claim("worker-a") is None

    session = get_session_factory()()
    try:
        queued_for_run = (
            session.query(EngineJobRow)
            .filter(EngineJobRow.run_id == "run_reg_cancel_queued", EngineJobRow.state == "queued")
            .count()
        )
    finally:
        session.close()
    assert queued_for_run == 0, "a cancelled unit must not stay in the queued backlog"


def test_reaping_an_expired_cancelled_lease_does_not_strand_it_in_queued():
    from models.db import EngineJobRow, get_session_factory

    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_reg_reap_cancel")
    receipt = jobs.enqueue_once("run_reg_reap_cancel")
    lease = jobs.claim("worker-a", lease_seconds=1)
    assert lease is not None and lease.job_id == receipt.job_id
    jobs.cancel_run("run_reg_reap_cancel")

    jobs.reap_expired(now=datetime.now(UTC) + timedelta(minutes=5))

    session = get_session_factory()()
    try:
        row = session.get(EngineJobRow, receipt.job_id)
        state = row.state
    finally:
        session.close()
    assert state == "cancelled"


# --------------------------------------------------------------------------- #
# AU2-05 / AU2-06: a cancelled run stays cancelled
# --------------------------------------------------------------------------- #
def test_progress_after_a_confirmed_cancel_is_ignored():
    repo = ConversionRepository()
    _run(repo, "run_reg_progress_cancel")
    assert repo.confirm_cancel("run_reg_progress_cancel") is True

    repo.update_run_progress(
        "run_reg_progress_cancel", status="running", stage="extract", progress=0.5
    )

    row = _run_row("run_reg_progress_cancel")
    assert row.status == "cancelled"
    assert row.stage == "failed"
    assert row.progress == 0.0


def test_finish_run_keeps_a_cancelled_run_cancelled():
    repo = ConversionRepository()
    _run(repo, "run_reg_finish_cancel")
    repo.confirm_cancel("run_reg_finish_cancel")

    repo.finish_run(
        "run_reg_finish_cancel",
        status="completed",
        quality="validated",
        stats={},
        validation=None,
    )

    row = _run_row("run_reg_finish_cancel")
    assert row.status == "cancelled"


# --------------------------------------------------------------------------- #
# AU2-07: the publication transaction is atomic
# --------------------------------------------------------------------------- #
def test_a_failed_republication_keeps_the_previous_rows(pdf_paths, monkeypatch):
    from tests.conftest import run_fixture

    repo = ConversionRepository()
    run_id = "run_reg_atomic_publish"
    _run(repo, run_id)
    result, recorder = run_fixture(pdf_paths["mixed_report"], run_id=run_id)
    repo.persist(result, recorder)
    before = _page_count(run_id)
    assert before > 0

    # The first arbitration point passes; the commit-time one fails mid-write.
    original = ConversionRepository._assert_publishable
    calls = {"n": 0}

    def fail_second(self, session, rid):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom during publication")
        return original(self, session, rid)

    monkeypatch.setattr(ConversionRepository, "_assert_publishable", fail_second)
    with pytest.raises(RuntimeError):
        repo.persist(result, recorder)

    assert _page_count(run_id) == before, "the old publication must survive a failed rewrite"


# --------------------------------------------------------------------------- #
# AU2-02: a stale worker cannot publish over the current owner
# --------------------------------------------------------------------------- #
def test_stale_fence_cannot_publish_and_the_current_owner_can(pdf_paths):
    from tests.conftest import run_fixture

    run_id = "run_reg_fence_publish"
    repo = ConversionRepository()
    jobs = JobRepository()
    _isolate(jobs)
    _run(repo, run_id)
    jobs.enqueue_once(run_id)

    stale = jobs.claim("worker-a", lease_seconds=1)
    assert stale is not None
    current = jobs.claim("worker-b", now=datetime.now(UTC) + timedelta(seconds=10), lease_seconds=60)
    assert current is not None and current.fence > stale.fence

    result, recorder = run_fixture(pdf_paths["mixed_report"], run_id=run_id)
    with pytest.raises(LeaseLost):
        repo.persist(result, recorder, job_id=stale.job_id, fence=stale.fence)
    assert _page_count(run_id) == 0, "a stale worker must publish nothing"

    with pytest.raises(LeaseLost):
        repo.finish_run(
            run_id,
            status="completed",
            quality="validated",
            stats={},
            validation=None,
            job_id=stale.job_id,
            fence=stale.fence,
        )
    assert _run_row(run_id).status == "received"

    repo.persist(result, recorder, job_id=current.job_id, fence=current.fence)
    assert _page_count(run_id) > 0


def test_execute_run_with_a_lost_lease_publishes_nothing_and_does_not_raise():
    from worker.tasks import execute_run

    run_id = "run_reg_fence_execute"
    repo = ConversionRepository()
    jobs = JobRepository()
    _isolate(jobs)
    _run(repo, run_id)
    jobs.enqueue_once(run_id)
    stale = jobs.claim("worker-a", lease_seconds=1)
    assert stale is not None
    jobs.claim("worker-b", now=datetime.now(UTC) + timedelta(seconds=10), lease_seconds=60)

    # The storage key does not exist, so the run takes its failure path. That
    # path must not overwrite the new owner's state.
    execute_run(
        run_id=run_id,
        conversion_id="conv_reg_fence",
        storage_key="does/not/exist.pdf",
        job_id=stale.job_id,
        fence=stale.fence,
    )
    assert _run_row(run_id).status == "received"


def test_inline_mode_without_a_job_still_publishes(pdf_paths):
    from tests.conftest import run_fixture

    run_id = "run_reg_inline_publish"
    repo = ConversionRepository()
    _run(repo, run_id)
    result, recorder = run_fixture(pdf_paths["mixed_report"], run_id=run_id)
    repo.persist(result, recorder)
    assert _page_count(run_id) > 0


# --------------------------------------------------------------------------- #
# AU2-02 (propagation): lease loss reaches the pipeline's cancel check
# --------------------------------------------------------------------------- #
def test_heartbeat_loss_is_observed_by_the_running_unit(monkeypatch):
    import jobqueue.scheduler as scheduler_module
    from jobqueue.repository import JobLease
    from jobqueue.scheduler import DurableScheduler

    monkeypatch.setattr(scheduler_module, "HEARTBEAT_SECONDS", 0.01)

    class LostRepo:
        def heartbeat(self, job_id, fence, **_kwargs):
            return False

    seen: dict[str, bool] = {}

    def runner(*, lease, repo, lease_lost):
        seen["lost"] = lease_lost.wait(3.0)

    scheduler = DurableScheduler(executor=None, repository=LostRepo(), slots=1, runner=runner)
    scheduler._run_lease(
        JobLease(job_id=1, run_id="run_reg_hb_loss", unit_key="document", fence=1, attempt=1, payload_ref=None)
    )
    assert seen.get("lost") is True


def test_default_runner_cancel_check_is_true_after_lease_loss(monkeypatch):
    import worker.tasks as tasks
    from jobqueue.repository import JobLease
    from jobqueue.scheduler import _default_runner

    captured: dict = {}
    monkeypatch.setattr(tasks, "execute_run", lambda **kwargs: captured.setdefault("check", kwargs["cancel_check"]))

    class Repo:
        def cancel_requested(self, run_id):
            return False

    lost = threading.Event()
    lost.set()
    _default_runner(
        lease=JobLease(job_id=2, run_id="run_reg_default", unit_key="document", fence=3, attempt=1, payload_ref=None),
        repo=Repo(),
        lease_lost=lost,
    )
    assert captured["check"]() is True


# --------------------------------------------------------------------------- #
# S1: a timer that fires after a successful run does not record a timeout
# --------------------------------------------------------------------------- #
def test_timer_firing_after_success_does_not_record_a_failure(monkeypatch):
    import jobqueue.inline as inline
    import worker.tasks as tasks
    from jobqueue.repository import JobLease

    class FireOnCancelTimer:
        """Simulates the timer going off at the last moment, before `cancel()` returns."""

        def __init__(self, interval, function, args=()):
            self.function = function
            self.args = args
            self.daemon = False

        def start(self):
            pass

        def cancel(self):
            self.function(*self.args)

    monkeypatch.setattr(threading, "Timer", FireOnCancelTimer)
    monkeypatch.setattr(tasks, "execute_run", lambda **kwargs: None)

    class Repo:
        def __init__(self):
            self.calls: list[tuple] = []

        def complete(self, job_id, fence, **_kwargs):
            self.calls.append(("complete", job_id))
            return True

        def fail(self, job_id, fence, *, error, retry_in=None):
            self.calls.append(("fail", error))
            return True

        def cancel_requested(self, run_id):
            return False

    queue = object.__new__(inline.InlineQueue)
    queue._timeout = 60
    queue._cancelled = set()
    queue._timed_out = set()
    queue._lock = threading.Lock()
    repo = Repo()
    queue.repo = repo

    queue._run_lease(
        lease=JobLease(job_id=7, run_id="run_reg_s1", unit_key="document", fence=1, attempt=1, payload_ref=None),
        repo=repo,
    )
    assert repo.calls == [("complete", 7)]


# --------------------------------------------------------------------------- #
# S2: boolean options are parsed, not truthy-cast
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        {"ocr": {"enabled": "false"}},
        {"ocr": "false"},
        {"ocr": False},
        {"ocr_enabled": "0"},
    ],
)
def test_string_false_disables_ocr(raw):
    from worker.tasks import _options_from

    assert _options_from(raw).ocr_enabled is False


def test_string_false_disables_table_text():
    from worker.tasks import _options_from

    assert _options_from({"text": {"include_table_text": "false"}}).include_table_text is False
    assert _options_from({"text": {"include_table_text": "true"}}).include_table_text is True


# --------------------------------------------------------------------------- #
# AU2-22 / AU2-23: the materialized copy is always cleaned, storage is never moved
# --------------------------------------------------------------------------- #
def test_suffix_mismatch_copies_instead_of_moving_the_stored_object(tmp_path, monkeypatch):
    import worker.tasks as tasks
    from ingestion.registry import InputProbe
    from storage import get_storage

    storage = get_storage()
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9d\xd9\x9d\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    key = "runs/run_reg_copy/source-named-as-pdf.pdf"
    storage.put_bytes(key, png, "application/pdf")
    stored_path = storage.local_path(key)
    assert stored_path is not None and stored_path.exists()

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(
        tasks,
        "probe_input_path",
        lambda path, filename=None: InputProbe(kind="image", format="png", mime_type="image/png", supported=True),
    )
    import ingestion.image_pipeline as image_pipeline

    class _StopBeforeEngine:
        def __init__(self, **_kwargs):
            pass

        def run(self):
            raise RuntimeError("stop after the work copy is made")

    monkeypatch.setattr(image_pipeline, "ImageConversionPipeline", _StopBeforeEngine)
    tasks.execute_run(
        run_id="run_reg_copy",
        conversion_id="conv_reg_copy",
        storage_key=key,
        filename="scan.pdf",
        options={},
    )

    assert stored_path.exists(), "the stored object must survive a suffix-mismatch rename"
    assert not (tmp_path / "work" / "run_reg_copy.source").exists()
    assert not (tmp_path / "work" / "run_reg_copy.png").exists(), "the work copy must be cleaned up"


def test_early_return_still_cleans_the_materialized_copy(tmp_path, monkeypatch):
    import worker.tasks as tasks
    from ingestion.registry import InputProbe
    from storage import get_storage

    key = "runs/run_reg_unknown/unknown.bin"
    get_storage().put_bytes(key, b"not a document at all", "application/octet-stream")
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setattr(
        tasks,
        "probe_input_path",
        lambda path, filename=None: InputProbe(kind="unknown", format=None, mime_type=None, supported=False),
    )
    tasks.execute_run(run_id="run_reg_unknown", conversion_id="conv_reg_unknown", storage_key=key, options={})

    assert not any((tmp_path / "work").glob("run_reg_unknown*"))


# --------------------------------------------------------------------------- #
# AU2-12: additive columns are generic and applied by init_db
# --------------------------------------------------------------------------- #
def test_init_db_adds_every_missing_column_of_an_old_schema(tmp_path):
    from models.db import Base, init_db

    engine = create_engine(_sqlite_url(tmp_path / "old_schema.db"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE runs DROP COLUMN cancel_requested_at")
        conn.exec_driver_sql("ALTER TABLE engine_jobs DROP COLUMN cancel_requested_at")
        conn.exec_driver_sql("ALTER TABLE automation_runs DROP COLUMN cancel_requested_at")
        conn.exec_driver_sql("ALTER TABLE public_conversions DROP COLUMN options_json")

    init_db(engine)
    init_db(engine)  # idempotent

    with engine.connect() as conn:
        cols = {
            table: {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for table in ("runs", "engine_jobs", "automation_runs", "public_conversions")
        }
    assert "cancel_requested_at" in cols["runs"]
    assert "cancel_requested_at" in cols["engine_jobs"]
    assert "cancel_requested_at" in cols["automation_runs"]
    assert "options_json" in cols["public_conversions"]


def test_schema_gaps_reports_missing_tables_and_columns(tmp_path):
    from models.db import Base, schema_gaps

    engine = create_engine(_sqlite_url(tmp_path / "gaps.db"))
    Base.metadata.create_all(engine)
    assert schema_gaps(engine) == []
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE runs DROP COLUMN cancel_requested_at")
    assert "runs.cancel_requested_at" in schema_gaps(engine)


# --------------------------------------------------------------------------- #
# AU2-11: the 6d13 revision upgrades populated tables and tolerates init_db tables
# --------------------------------------------------------------------------- #
def test_populated_public_conversions_upgrades_to_head(tmp_path):
    db = tmp_path / "populated.db"
    url = _sqlite_url(db)
    _alembic(url, "upgrade", "c4f1a2b7d910")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO public_conversions (id, document_id, owner_id, status, quality, progress, stage, "
            "created_at, updated_at) VALUES ('conv_pop', 'doc_pop', 'owner_pop', 'completed', 'validated', "
            "1.0, 'done', '2026-11-19 08:00:00', '2026-11-19 08:00:00')"
        )

    _alembic(url, "upgrade", "head")

    with engine.connect() as conn:
        options = conn.execute(
            text("SELECT options_json FROM public_conversions WHERE id = 'conv_pop'")
        ).scalar_one()
    assert options == "{}"


def test_an_init_db_database_can_be_stamped_at_head(tmp_path):
    from models.db import Base

    db = tmp_path / "stamped.db"
    url = _sqlite_url(db)
    Base.metadata.create_all(create_engine(url))
    _alembic(url, "stamp", "head")
    _alembic(url, "upgrade", "head")
    _alembic(url, "current")


# --------------------------------------------------------------------------- #
# AU2-26: a '%' in the database URL survives alembic's config interpolation
# --------------------------------------------------------------------------- #
def test_alembic_accepts_a_percent_sign_in_the_database_path(tmp_path):
    db = tmp_path / "rate%.db"
    _alembic(_sqlite_url(db), "upgrade", "head")
    assert db.exists()


# --------------------------------------------------------------------------- #
# T11: startup migration safety
# --------------------------------------------------------------------------- #
def _write_old_database(path: Path, *, revision: str | None) -> None:
    """A database from an older build: everything except one newer column."""
    from models.db import Base

    engine = create_engine(_sqlite_url(path))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE automation_runs DROP COLUMN cancel_requested_at")
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        if revision is not None:
            conn.exec_driver_sql(f"INSERT INTO alembic_version (version_num) VALUES ('{revision}')")
    engine.dispose()


def test_migration_lock_blocks_a_concurrent_migration(tmp_path):
    from runtime.db_migrate import MigrationLockTimeout, exclusive_lock, run_startup_migrations

    app_data = tmp_path / "app"
    app_data.mkdir()
    db = app_data / "converter.db"
    _write_old_database(db, revision="f2a8c61b9e73")
    engine = create_engine(_sqlite_url(db))

    with exclusive_lock(app_data / ".migrate.lock", timeout=5):
        with pytest.raises(MigrationLockTimeout):
            run_startup_migrations(app_data, engine, lock_timeout=0.3)

    assert not (app_data / "backups").exists() or not list((app_data / "backups").glob("*.db"))
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA table_info(automation_runs)").fetchall()
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(automation_runs)")}
    assert "cancel_requested_at" not in cols, "a refused lock must not migrate the database"
    engine.dispose()


def test_newer_schema_is_refused_before_anything_changes(tmp_path):
    from runtime.db_migrate import SchemaTooNewError, run_startup_migrations

    app_data = tmp_path / "app"
    app_data.mkdir()
    db = app_data / "converter.db"
    engine = create_engine(_sqlite_url(db))
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('ffffffffffff')")
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    with pytest.raises(SchemaTooNewError, match="ffffffffffff"):
        run_startup_migrations(app_data, engine)

    with engine.connect() as conn:
        tables = {
            row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {"alembic_version"}, "nothing may be created in a refused database"
    assert not (app_data / "backups").exists() or not list((app_data / "backups").glob("*.db"))
    engine.dispose()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_old_schema_is_backed_up_then_migrated_and_stamped(tmp_path):
    from runtime.db_migrate import HEAD_REVISION, run_startup_migrations

    app_data = tmp_path / "app"
    app_data.mkdir()
    db = app_data / "converter.db"
    _write_old_database(db, revision="f2a8c61b9e73")
    engine = create_engine(_sqlite_url(db))

    report = run_startup_migrations(app_data, engine)

    assert report.backup is not None and report.backup.exists()
    assert report.backup.parent == app_data / "backups"
    assert report.backup.name.startswith("converter-") and report.backup.suffix == ".db"

    backup_engine = create_engine(_sqlite_url(report.backup))
    with backup_engine.connect() as conn:
        old_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(automation_runs)")}
        old_rev = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    backup_engine.dispose()
    assert "cancel_requested_at" not in old_cols, "the backup must hold the pre-migration schema"
    assert old_rev == "f2a8c61b9e73"

    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(automation_runs)")}
        rev = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    assert "cancel_requested_at" in cols
    assert rev == HEAD_REVISION
    engine.dispose()


def test_a_current_database_is_not_backed_up_again(tmp_path):
    from runtime.db_migrate import run_startup_migrations

    app_data = tmp_path / "app"
    app_data.mkdir()
    db = app_data / "converter.db"
    engine = create_engine(_sqlite_url(db))
    first = run_startup_migrations(app_data, engine)
    assert first.backup is None, "a brand-new database has nothing to back up"
    second = run_startup_migrations(app_data, engine)
    assert second.backup is None
    engine.dispose()


def test_backups_are_pruned_to_the_last_five(tmp_path):
    from runtime.db_migrate import BACKUP_KEEP, backup_sqlite

    db = tmp_path / "source.db"
    engine = create_engine(_sqlite_url(db))
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER)")
    engine.dispose()

    backup_dir = tmp_path / "backups"
    base = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    for minute in range(BACKUP_KEEP + 2):
        backup_sqlite(db, backup_dir, now=base + timedelta(minutes=minute))

    names = sorted(p.name for p in backup_dir.glob("converter-*.db"))
    assert len(names) == BACKUP_KEEP
    assert names[-1].startswith("converter-20261119T0806")  # newest kept (08:06 is the last step)


def test_head_revision_matches_the_alembic_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from runtime.db_migrate import HEAD_REVISION, KNOWN_REVISIONS

    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == [HEAD_REVISION]
    assert set(KNOWN_REVISIONS) == {rev.revision for rev in script.walk_revisions()}


def test_refused_schema_exits_with_the_configuration_status(monkeypatch):
    import desktop_entry
    from runtime.db_migrate import SchemaTooNewError

    monkeypatch.setenv("CONVERTER_INTERNAL_TOKEN", "t" * 40)
    monkeypatch.setenv("DESKTOP_BOOTSTRAP_SECRET", "t" * 40)
    monkeypatch.setenv("JWT_SECRET", "j" * 96)
    monkeypatch.setenv("DESKTOP_HOST_ENABLED", "false")

    def refuse():
        raise SchemaTooNewError("revisão ffffffffffff desconhecida")

    monkeypatch.setattr(desktop_entry, "prepare_runtime", refuse)
    assert desktop_entry.main(["--port", "0"]) == desktop_entry.EXIT_REFUSED
