"""Schedule application service (T7): create/edit/pause/resume with preview."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from api.deps import ApiError
from automation.calendar import Calendar, next_occurrences
from automation.models import AutomationScheduleRow
from automation.repository import AutomationRepository
from automation.types import (
    ScheduleCreate,
    validate_schedule_create,
    validate_timezone,
)
from sources.repository import SourceRepository


def build_snapshot(
    schedule: AutomationScheduleRow, definition: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Freeze the definition the executor must honour for THIS occurrence.

    The full source definition (root, relative path, filters, recursion and
    version) is copied — not just the source id — so later edits never change
    which files an older occurrence processes. Root revocation still wins at
    open time.
    """
    return {
        "schedule_id": schedule.id,
        "owner_id": schedule.owner_id,
        "source_id": schedule.source_id,
        "source_definition": definition,
        "revision": schedule.revision,
        "rule": dict(schedule.rule_json or {}),
        "timezone": schedule.timezone,
        "options": dict(schedule.options_json or {}),
        "index_collection_id": schedule.index_collection_id,
        "overlap_policy": schedule.overlap_policy,
        "misfire_policy": schedule.misfire_policy,
    }


class ScheduleService:
    def __init__(self) -> None:
        self.repository = AutomationRepository()
        self.sources = SourceRepository()

    # ------------------------------------------------------------------ CRUD
    def create(self, session: Session, *, owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        create: ScheduleCreate = validate_schedule_create(payload)
        source = self.sources.get_source(session, source_id=create.source_id, owner_id=owner_id)
        if source is None:
            raise ApiError(
                404,
                "SOURCE_NOT_FOUND",
                "Fonte inexistente ou de outro dono.",
                details={"source_id": create.source_id},
            )
        if not source.enabled:
            raise ApiError(409, "SOURCE_DISABLED", "A fonte está desativada.")
        rule_json = create.rule.model_dump()
        timezone = validate_timezone(create.timezone).key
        next_run = Calendar.next_after(create.rule, timezone, datetime.now(UTC))
        schedule = self.repository.create_schedule(
            session,
            owner_id=owner_id,
            name=create.name,
            source_id=create.source_id,
            rule=rule_json,
            timezone=timezone,
            next_run_at=next_run,
            misfire_policy=create.misfire_policy,
            overlap_policy=create.overlap_policy,
            options=create.options or {},
            index_collection_id=create.index_collection_id,
        )
        return self.schedule_payload(schedule)

    def get(self, session: Session, *, schedule_id: str, owner_id: str) -> dict[str, Any] | None:
        schedule = self.repository.get_schedule(session, schedule_id=schedule_id, owner_id=owner_id)
        return self.schedule_payload(schedule) if schedule else None

    def list(self, session: Session, *, owner_id: str) -> list[dict[str, Any]]:
        return [
            self.schedule_payload(schedule)
            for schedule in self.repository.list_schedules(session, owner_id=owner_id)
        ]

    def update(
        self,
        session: Session,
        *,
        schedule_id: str,
        owner_id: str,
        expected_revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        schedule = self.repository.get_schedule(session, schedule_id=schedule_id, owner_id=owner_id)
        if schedule is None:
            return None
        updates: dict[str, Any] = {}
        rule = None
        timezone = schedule.timezone

        if "name" in changes:
            name = str(changes["name"] or "").strip()
            if not name or len(name) > 200:
                raise ApiError(400, "INVALID_SCHEDULE_RULE", "name deve ter entre 1 e 200 caracteres.")
            updates["name"] = name
        if "rule" in changes:
            from automation.types import validate_schedule_rule

            rule = validate_schedule_rule(changes["rule"])
            updates["rule_json"] = rule.model_dump()
        if "timezone" in changes:
            timezone = validate_timezone(str(changes["timezone"])).key
            updates["timezone"] = timezone
        if "source_id" in changes and changes["source_id"] != schedule.source_id:
            source = self.sources.get_source(
                session, source_id=str(changes["source_id"]), owner_id=owner_id
            )
            if source is None:
                raise ApiError(404, "SOURCE_NOT_FOUND", "Fonte inexistente ou de outro dono.")
            updates["source_id"] = source.id
        for key in ("options", "index_collection_id"):
            if key in changes:
                updates["options_json" if key == "options" else key] = changes[key]
        for key in ("misfire_policy", "overlap_policy"):
            if key in changes:
                value = str(changes[key])
                allowed = {"run_once_latest", "skip"} if key == "misfire_policy" else {"skip", "queue_one"}
                if value not in allowed:
                    raise ApiError(400, "INVALID_SCHEDULE_RULE", f"{key} inválido.")
                updates[key] = value

        # Edits recalculate the future from the edit instant; occurrences
        # already created keep their frozen snapshot.
        next_run = None
        if rule is not None or "timezone" in updates:
            effective_rule = rule or self._rule_of(schedule)
            next_run = Calendar.next_after(effective_rule, timezone, datetime.now(UTC))

        updated = self.repository.update_schedule(
            session,
            schedule_id=schedule_id,
            owner_id=owner_id,
            expected_revision=expected_revision,
            changes=updates,
            next_run_at=next_run,
            recompute_next_run=next_run is not None,
        )
        if updated is None:
            raise ApiError(
                409,
                "REVISION_CONFLICT",
                "Revisão da agenda divergente: releia e reaplique.",
                details={"schedule_id": schedule_id, "expected_revision": expected_revision},
            )
        return self.schedule_payload(updated)

    def pause(self, session: Session, *, schedule_id: str, owner_id: str) -> dict[str, Any]:
        schedule = self.repository.pause_schedule(
            session, schedule_id=schedule_id, owner_id=owner_id
        )
        if schedule is None:
            raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
        return self.schedule_payload(schedule)

    def resume(self, session: Session, *, schedule_id: str, owner_id: str) -> dict[str, Any]:
        schedule = self.repository.get_schedule(session, schedule_id=schedule_id, owner_id=owner_id)
        if schedule is None:
            raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
        rule = self._rule_of(schedule)
        next_run = Calendar.next_after(rule, schedule.timezone, datetime.now(UTC))
        resumed = self.repository.resume_schedule(
            session, schedule_id=schedule_id, owner_id=owner_id, next_run_at=next_run
        )
        if resumed is None:
            raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
        return self.schedule_payload(resumed)

    def archive(self, session: Session, *, schedule_id: str, owner_id: str) -> bool:
        # Archiving a schedule is always allowed: other schedules sharing the
        # same source keep working, and history is preserved.
        return self.repository.archive_schedule(session, schedule_id=schedule_id, owner_id=owner_id)

    # --------------------------------------------------------------- preview
    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        create = validate_schedule_create(payload)
        timezone = validate_timezone(create.timezone).key
        occurrences = next_occurrences(
            create.rule, timezone, datetime.now(UTC), count=5
        )
        return {
            "timezone": timezone,
            "rule": create.rule.model_dump(),
            "next_occurrences": [instant.isoformat() for instant in occurrences],
        }

    # ---------------------------------------------------------------- helpers
    def schedule_payload(self, schedule: AutomationScheduleRow) -> dict[str, Any]:
        return {
            "id": schedule.id,
            "owner_id": schedule.owner_id,
            "name": schedule.name,
            "source_id": schedule.source_id,
            "enabled": bool(schedule.enabled),
            "archived": bool(schedule.archived),
            "revision": schedule.revision,
            "rule": dict(schedule.rule_json or {}),
            "timezone": schedule.timezone,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "misfire_policy": schedule.misfire_policy,
            "overlap_policy": schedule.overlap_policy,
            "options": dict(schedule.options_json or {}),
            "index_collection_id": schedule.index_collection_id,
            "created_at": schedule.created_at.isoformat(),
            "updated_at": schedule.updated_at.isoformat(),
        }

    @staticmethod
    def _rule_of(schedule: AutomationScheduleRow):
        from automation.types import DailyRule, IntervalRule

        payload = schedule.rule_json or {}
        if payload.get("kind") == "daily":
            return DailyRule(kind="daily", local_time=str(payload["local_time"]))
        return IntervalRule(
            kind="interval",
            every_seconds=int(payload.get("every_seconds") or 60),
            anchor_at=payload.get("anchor_at"),
        )


__all__ = ["ScheduleService", "build_snapshot"]
