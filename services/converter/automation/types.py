"""DTOs and validation for the automation domain (T1, docs 02/05).

Mirrors ``packages/contracts/src/schedules.ts``. Owner identity comes from
the session, never from the payload. Rules are deterministic: daily uses a
local wall-clock time in an explicit IANA timezone; interval uses an integer
second count between 60 and 365 days anchored in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.deps import ApiError

MISFIRE_POLICIES = ("run_once_latest", "skip")
OVERLAP_POLICIES = ("skip", "queue_one")
INTERVAL_MIN_SECONDS = 60
INTERVAL_MAX_SECONDS = 31_536_000  # 365 days

RUN_STATES = (
    "queued",
    "running",
    "waiting_children",
    "cancelling",
    "succeeded",
    "partial",
    "no_changes",
    "failed",
    "cancelled",
    "skipped",
)
RUN_TERMINAL_STATES = ("succeeded", "partial", "no_changes", "failed", "cancelled", "skipped")
RUN_PHASES = ("scan", "snapshot", "submit", "convert", "index", "finalize")
RUN_TRIGGERS = ("schedule", "manual", "recovery")

EVENT_NAMES = (
    "SCHEDULE_CREATED",
    "SCHEDULE_PAUSED",
    "OCCURRENCE_DUE",
    "MISFIRE_COALESCED",
    "RUN_CLAIMED",
    "SOURCE_SCAN_COMPLETED",
    "IMPORT_STARTED",
    "CONVERSIONS_COMPLETED",
    "INDEX_COMPLETED",
    "RUN_FINISHED",
    "RUN_CANCEL_REQUESTED",
    "LEASE_RECOVERED",
)


class DailyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["daily"]
    local_time: str


class IntervalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["interval"]
    every_seconds: int
    anchor_at: str | None = None


ScheduleRule = DailyRule | IntervalRule


class ScheduleCreate(BaseModel):
    """Body of ``POST /schedules`` (strict; owner is injected server-side)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    source_id: str
    rule: Annotated[DailyRule | IntervalRule, Field(discriminator="kind")]
    timezone: str
    misfire_policy: Literal["run_once_latest", "skip"] = "run_once_latest"
    overlap_policy: Literal["skip", "queue_one"] = "skip"
    index_collection_id: str | None = None
    options: dict[str, Any] | None = None


def _map_rule_error(exc: ValidationError) -> ApiError:
    for issue in exc.errors():
        if issue.get("type") == "extra_forbidden":
            return ApiError(
                400,
                "UNKNOWN_FIELD",
                "Campo desconhecido; agendamentos não executam comandos arbitrários.",
                details={"fields": [str(part) for part in issue.get("loc", ())]},
            )
    return ApiError(
        400,
        "INVALID_SCHEDULE_RULE",
        "Regra de agendamento inválida.",
        details={"issues": [{"path": ".".join(str(p) for p in i.get("loc", ())), "message": i.get("msg", "")} for i in exc.errors()]},
    )


def validate_schedule_rule(raw: Any) -> DailyRule | IntervalRule:
    """Parse and validate a schedule rule; every rejection is deterministic."""
    try:
        rule = DailyRule.model_validate(raw) if isinstance(raw, dict) and raw.get("kind") == "daily" else None
        if rule is None:
            rule = IntervalRule.model_validate(raw)
    except ValidationError as exc:
        raise _map_rule_error(exc) from exc
    _validate_rule_values(rule)
    return rule


def _validate_rule_values(rule: DailyRule | IntervalRule) -> None:
    if isinstance(rule, DailyRule):
        parts = rule.local_time.split(":")
        if len(parts) != 2:
            raise ApiError(
                400,
                "INVALID_SCHEDULE_RULE",
                "local_time deve estar no formato HH:mm (24 horas).",
                details={"local_time": rule.local_time},
            )
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ApiError(
                400,
                "INVALID_SCHEDULE_RULE",
                "local_time deve estar no formato HH:mm (24 horas).",
                details={"local_time": rule.local_time},
            ) from exc
        if len(parts[0]) != 2 or len(parts[1]) != 2 or not (0 <= hour <= 23) or not (0 <= minute <= 59):
            raise ApiError(
                400,
                "INVALID_SCHEDULE_RULE",
                "local_time deve ser uma hora local válida (00:00–23:59).",
                details={"local_time": rule.local_time},
            )
        return

    seconds = rule.every_seconds
    # bool is an int subclass in Python; `every_seconds: true` is not a duration.
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ApiError(
            400,
            "INVALID_SCHEDULE_RULE",
            "every_seconds deve ser um número inteiro de segundos.",
            details={"every_seconds": seconds},
        )
    if seconds < INTERVAL_MIN_SECONDS or seconds > INTERVAL_MAX_SECONDS:
        raise ApiError(
            400,
            "INVALID_SCHEDULE_RULE",
            f"every_seconds deve estar entre {INTERVAL_MIN_SECONDS} e {INTERVAL_MAX_SECONDS} "
            "(60 segundos a 365 dias).",
            details={"every_seconds": seconds, "min": INTERVAL_MIN_SECONDS, "max": INTERVAL_MAX_SECONDS},
        )


def validate_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name; Windows needs the bundled ``tzdata``."""
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ApiError(400, "INVALID_TIMEZONE", "timezone deve ser um nome IANA (ex.: America/Sao_Paulo).")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ApiError(
            400,
            "INVALID_TIMEZONE",
            f"Fuso horário desconhecido: {name}.",
            details={"timezone": name},
        ) from exc


def validate_schedule_create(raw: dict[str, Any]) -> ScheduleCreate:
    """Parse ``POST /schedules`` bodies, mapping errors to the HTTP envelope."""
    if not isinstance(raw, dict):
        raise ApiError(400, "INVALID_PAYLOAD", "O corpo deve ser um objeto JSON.")
    try:
        parsed = ScheduleCreate.model_validate(raw)
    except ValidationError as exc:
        raise _map_rule_error(exc) from exc
    _validate_rule_values(parsed.rule)
    validate_timezone(parsed.timezone)
    if not parsed.name or len(parsed.name) > 200:
        raise ApiError(400, "INVALID_SCHEDULE_RULE", "name deve ter entre 1 e 200 caracteres.")
    return parsed


@dataclass(slots=True, frozen=True)
class RunLease:
    """Fenced lease over one occurrence claim (doc 05, §6)."""

    run_id: str
    fence: int
    lease_until: datetime


@dataclass(slots=True, frozen=True)
class RunProgress:
    """Outcome of one executor advance step."""

    run_id: str
    state: str
    phase: str
    done: bool
    detail: dict[str, Any] | None = None


@dataclass(slots=True)
class ScheduleRecord:
    id: str
    owner_id: str
    name: str
    source_id: str
    enabled: bool
    archived: bool
    revision: int
    rule: dict[str, Any]
    timezone: str
    next_run_at: datetime | None
    misfire_policy: str
    overlap_policy: str
    options: dict[str, Any] | None
    index_collection_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AutomationRunRecord:
    id: str
    owner_id: str
    schedule_id: str
    schedule_revision: int
    scheduled_for: datetime
    trigger: str
    occurrence_key: str
    state: str
    phase: str | None
    import_id: str | None
    index_job_id: str | None
    attempt: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
