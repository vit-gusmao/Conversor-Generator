"""T7 — deterministic calendar rules (daily/interval, DST, clock rollback)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from automation.calendar import Calendar, next_occurrences
from automation.types import DailyRule, IntervalRule

SP = "America/Sao_Paulo"
NY = "America/New_York"


def test_daily_fires_at_local_wall_clock():
    rule = DailyRule(kind="daily", local_time="08:00")
    after = datetime(2026, 11, 19, 11, 0, tzinfo=UTC)  # 08:00 in São Paulo
    nxt = Calendar.next_after(rule, SP, after)
    assert nxt == datetime(2026, 11, 20, 11, 0, tzinfo=UTC)


def test_daily_preview_returns_five_times():
    rule = DailyRule(kind="daily", local_time="08:00")
    occurrences = next_occurrences(rule, SP, datetime(2026, 11, 19, 0, 0, tzinfo=UTC), count=5)
    assert len(occurrences) == 5
    assert occurrences[0] == datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
    assert all(
        later - earlier == timedelta(days=1)
        for earlier, later in zip(occurrences, occurrences[1:])
    )


def test_daily_is_calendar_not_fixed_seconds():
    """A DST transition shifts UTC instants while the local time stays 08:00."""
    rule = DailyRule(kind="daily", local_time="08:00")
    # US DST starts 2027-03-14; the UTC instant moves by one hour.
    before = datetime(2027, 3, 13, 13, 0, tzinfo=UTC)  # 08:00 EST
    nxt = Calendar.next_after(rule, NY, before)
    assert nxt == datetime(2027, 3, 14, 12, 0, tzinfo=UTC)  # 08:00 EDT


def test_nonexistent_local_time_uses_first_valid_instant():
    """2027-03-14 02:30 does not exist in New York (spring forward)."""
    rule = DailyRule(kind="daily", local_time="02:30")
    after = datetime(2027, 3, 13, 12, 0, tzinfo=UTC)
    nxt = Calendar.next_after(rule, NY, after)
    # Round-trip validation shifts to the first valid local instant (03:00 EDT).
    assert nxt == datetime(2027, 3, 14, 7, 0, tzinfo=UTC)


def test_repeated_local_time_runs_once():
    """2027-11-07 01:30 happens twice in New York (fall back)."""
    rule = DailyRule(kind="daily", local_time="01:30")
    after = datetime(2027, 11, 6, 12, 0, tzinfo=UTC)
    first = Calendar.next_after(rule, NY, after)
    second = Calendar.next_after(rule, NY, first)
    assert first == datetime(2027, 11, 7, 5, 30, tzinfo=UTC)  # first occurrence (EDT)
    assert second == datetime(2027, 11, 8, 6, 30, tzinfo=UTC)  # next day (EST)
    # never two runs on the same local day
    assert second.date() > first.date()


def test_interval_is_anchored_not_duration_dependent():
    rule = IntervalRule(
        kind="interval", every_seconds=3600, anchor_at="2026-11-19T11:00:00Z"
    )
    anchor = datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
    assert Calendar.next_after(rule, "UTC", anchor) == anchor + timedelta(hours=1)
    # a late execution does not shift the calendar
    late = anchor + timedelta(hours=3, minutes=17)
    nxt = Calendar.next_after(rule, "UTC", late)
    assert nxt == anchor + timedelta(hours=4)


def test_clock_rollback_never_repeats_a_recorded_occurrence():
    rule = IntervalRule(
        kind="interval", every_seconds=60, anchor_at="2026-11-19T11:00:00Z"
    )
    recorded = datetime(2026, 11, 19, 11, 5, tzinfo=UTC)
    # The clock moves BACK two minutes: next must still follow the recorded run.
    rolled_back = datetime(2026, 11, 19, 11, 3, tzinfo=UTC)
    nxt = Calendar.next_after(rule, "UTC", max(recorded, rolled_back))
    assert nxt > recorded


def test_interval_requires_anchor_or_uses_after():
    rule = IntervalRule(kind="interval", every_seconds=900, anchor_at=None)
    after = datetime(2026, 11, 19, 11, 0, tzinfo=UTC)
    assert Calendar.next_after(rule, "UTC", after) == after + timedelta(seconds=900)


def test_invalid_timezone_is_rejected():
    from api.deps import ApiError

    rule = DailyRule(kind="daily", local_time="08:00")
    with pytest.raises(ApiError):
        Calendar.next_after(rule, "Marte/Olympus", datetime.now(UTC))
