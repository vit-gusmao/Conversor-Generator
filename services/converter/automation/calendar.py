"""Deterministic calendar rules (T7, doc 05 §3).

Daily rules are local wall-clock times in an explicit IANA timezone; the
conversion is validated by a UTC round-trip so a nonexistent local time (DST
spring forward) resolves to the first valid instant after it, and a repeated
local time (fall back) fires only once. Interval rules are anchored in UTC and
computed as ``anchor + n * interval`` — the previous run's duration never
shifts the calendar.

Preview and the scheduler share exactly these functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from automation.types import DailyRule, IntervalRule, validate_timezone

#: Safety bound for the DST gap scan (gaps are ≤ 2 hours in practice).
_MAX_GAP_MINUTES = 240


def _resolve_local(tz: ZoneInfo, naive_local: datetime) -> datetime:
    """Attach the timezone and validate the mapping by a UTC round-trip.

    ``zoneinfo`` alone does not reject nonexistent times; round-tripping does
    (``fold=0`` selects the first occurrence of a repeated time).
    """
    candidate = naive_local.replace(tzinfo=tz, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(tz)
    if round_trip.replace(tzinfo=None) == naive_local:
        return candidate
    # Nonexistent local time: step to the first valid local instant after it.
    probe = naive_local
    for _ in range(_MAX_GAP_MINUTES):
        probe = probe + timedelta(minutes=1)
        candidate = probe.replace(tzinfo=tz, fold=0)
        round_trip = candidate.astimezone(UTC).astimezone(tz)
        if round_trip.replace(tzinfo=None) == probe:
            return candidate
    raise ValueError(f"Horário local não resolve em {tz.key}: {naive_local}")


class Calendar:
    """Pure functions: rule + timezone + reference instant → next instant."""

    @staticmethod
    def next_after(rule: DailyRule | IntervalRule, timezone: str, after_utc: datetime) -> datetime:
        tz = validate_timezone(timezone)
        if after_utc.tzinfo is None:
            after_utc = after_utc.replace(tzinfo=UTC)

        if isinstance(rule, DailyRule):
            return Calendar._daily_next(rule, tz, after_utc)
        return Calendar._interval_next(rule, after_utc)

    @staticmethod
    def _daily_next(rule: DailyRule, tz: ZoneInfo, after_utc: datetime) -> datetime:
        hour, minute = (int(part) for part in rule.local_time.split(":"))
        local_after = after_utc.astimezone(tz)
        for day_offset in range(0, 8):
            day = (local_after + timedelta(days=day_offset)).date()
            naive = datetime(day.year, day.month, day.day, hour, minute)
            try:
                candidate = _resolve_local(tz, naive)
            except ValueError:
                continue
            instant = candidate.astimezone(UTC)
            if instant > after_utc:
                return instant
        raise ValueError("Nenhuma ocorrência diária encontrada nos próximos 8 dias.")

    @staticmethod
    def _interval_next(rule: IntervalRule, after_utc: datetime) -> datetime:
        interval = timedelta(seconds=int(rule.every_seconds))
        anchor = (
            datetime.fromisoformat(rule.anchor_at.replace("Z", "+00:00")).astimezone(UTC)
            if rule.anchor_at
            else after_utc
        )
        if after_utc < anchor:
            return anchor
        delta = after_utc - anchor
        steps = int(delta.total_seconds() // interval.total_seconds()) + 1
        return anchor + interval * steps


def next_occurrences(
    rule: DailyRule | IntervalRule, timezone: str, after_utc: datetime, *, count: int = 5
) -> list[datetime]:
    """Preview and scheduler share this exact loop."""
    occurrences: list[datetime] = []
    cursor = after_utc
    for _ in range(max(0, count)):
        cursor = Calendar.next_after(rule, timezone, cursor)
        occurrences.append(cursor)
    return occurrences


def occurrence_key(schedule_id: str, revision: int, scheduled_for_utc: datetime) -> str:
    """Globally unique identity: schedule + revision + instant."""
    instant = scheduled_for_utc.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{schedule_id}:{revision}:{instant}"


__all__ = ["Calendar", "next_occurrences", "occurrence_key"]
