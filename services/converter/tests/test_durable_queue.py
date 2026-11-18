"""Durable queue: leases, fences, cancellation and restart recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobqueue.repository import JobRepository
from repositories.conversion_repository import ConversionRepository, RunCancelled


def _run(repo: ConversionRepository, run_id: str, conversion_id: str = "conv_durable") -> None:
    """A job row always belongs to a run; create the parent first."""
    repo.create_run(run_id=run_id, conversion_id=conversion_id, engine_version="test")


def _isolate(jobs: JobRepository) -> None:
    """Finishes whatever earlier tests left queued, so claims are unambiguous."""
    while True:
        lease = jobs.claim("cleanup", lease_seconds=1)
        if lease is None:
            return
        jobs.complete(lease.job_id, lease.fence)


def test_enqueue_is_idempotent_per_unit():
    jobs = JobRepository()
    _run(ConversionRepository(), "run_durable_1")
    first = jobs.enqueue_once("run_durable_1", payload_ref='{"a":1}')
    second = jobs.enqueue_once("run_durable_1", payload_ref='{"a":2}')

    assert first.created is True
    assert second.created is False
    assert second.job_id == first.job_id


def test_a_reclaimed_unit_is_queued_again():
    jobs = JobRepository()
    _run(ConversionRepository(), "run_durable_reclaim")
    jobs.enqueue_once("run_durable_reclaim")
    lease = jobs.claim("worker-a")
    assert lease is not None
    jobs.complete(lease.job_id, lease.fence)

    again = jobs.enqueue_once("run_durable_reclaim")
    assert again.created is False
    assert again.state == "queued"


def test_claims_are_exclusive_and_carry_a_fence():
    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_durable_claim")
    jobs.enqueue_once("run_durable_claim")

    first = jobs.claim("worker-a")
    second = jobs.claim("worker-b")

    assert first is not None
    assert second is None, "one unit must not be leased twice"
    assert first.fence >= 1


def test_expired_lease_is_reassigned_and_the_old_fence_is_rejected():
    jobs = JobRepository()
    _isolate(jobs)
    _run(ConversionRepository(), "run_durable_expiry")
    jobs.enqueue_once("run_durable_expiry")

    first = jobs.claim("worker-a", lease_seconds=1)
    assert first is not None

    # Simulate the lease expiring without a heartbeat.
    future = datetime.now(UTC) + timedelta(seconds=10)
    second = jobs.claim("worker-b", now=future, lease_seconds=60)
    assert second is not None
    assert second.fence > first.fence

    # The stale worker can no longer publish or complete.
    assert jobs.complete(first.job_id, first.fence) is False
    assert jobs.heartbeat(first.job_id, first.fence) is False

    # The current owner can.
    assert jobs.heartbeat(second.job_id, second.fence) is True
    assert jobs.complete(second.job_id, second.fence) is True


def test_cancelled_job_is_not_claimable():
    repo = ConversionRepository()
    jobs = JobRepository()
    _isolate(jobs)
    _run(repo, "run_durable_cancel")
    jobs.enqueue_once("run_durable_cancel")

    assert jobs.cancel_run("run_durable_cancel") is True
    assert jobs.cancel_requested("run_durable_cancel") is True
    assert jobs.claim("worker-a") is None


def test_backlog_reports_counts_and_oldest_age():
    jobs = JobRepository()
    _run(ConversionRepository(), "run_durable_backlog")
    jobs.enqueue_once("run_durable_backlog")
    backlog = jobs.backlog()
    assert backlog.get("queued", 0) >= 1
    assert "oldest_queued_seconds" in backlog


def test_publication_is_refused_after_a_confirmed_cancel(pdf_paths):
    """The engine decides: a cancel that arrived before publishing wins."""
    from tests.conftest import run_fixture

    run_id = "run_durable_publish"
    repo = ConversionRepository()
    # The run row must exist for the cancellation to be recorded against it.
    _run(repo, run_id)

    result, recorder = run_fixture(pdf_paths["mixed_report"], run_id=run_id)

    # A cancellation confirmed while the pipeline was running.
    repo.confirm_cancel(run_id)

    with pytest.raises(RunCancelled):
        repo.persist(result, recorder)

    # Nothing was published for that run: no workbook, no sheet, no cell.
    assert repo.xlsx_bytes(run_id) is None
    summary = repo.result_summary(run_id)
    assert summary is not None
    assert summary["sheets"] == []


def test_confirmed_cancel_is_visible_and_terminal():
    repo = ConversionRepository()
    _run(repo, "run_durable_confirm")
    assert repo.confirm_cancel("run_durable_confirm") is True
    assert repo.cancel_requested("run_durable_confirm") is True

    row = repo.get_run("run_durable_confirm")
    assert row is not None
    assert row["status"] == "cancelled"
