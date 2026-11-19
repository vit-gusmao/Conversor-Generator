"""Schedule/occurrence HTTP routes (T7/T8, doc 02 §6).

Public routes are owner-scoped; occurrence events replay with SSE. The
calendar and executor never run in this module — routes only read/write state
through the services.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import StreamingResponse

from api.deps import ApiError, current_user, get_db, internal_guard
from automation.scheduler import AutomationScheduler
from automation.service import ScheduleService

router = APIRouter(prefix="/api/v1", tags=["schedules"])
internal_router = APIRouter(prefix="/internal/v1", tags=["internal"])


def _service() -> ScheduleService:
    return ScheduleService()


@router.get("/schedules")
def list_schedules(
    user: dict[str, Any] = Depends(current_user), session=Depends(get_db)
) -> dict[str, Any]:
    return {"items": _service().list(session, owner_id=user["id"])}


@router.post("/schedules", status_code=201)
def create_schedule(
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    payload.pop("owner_id", None)
    payload.pop("ownerId", None)
    return _service().create(session, owner_id=user["id"], payload=payload)


@router.post("/schedules/preview")
def preview_schedule(
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    del user
    return _service().preview(payload)


@router.get("/schedules/{schedule_id}")
def get_schedule(
    schedule_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    payload = _service().get(session, schedule_id=schedule_id, owner_id=user["id"])
    if payload is None:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
    return payload


@router.patch("/schedules/{schedule_id}")
@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: str,
    payload: dict[str, Any] = Body(default={}),
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    payload.pop("owner_id", None)
    payload.pop("ownerId", None)
    revision = payload.pop("revision", None)
    if not isinstance(revision, int):
        raise ApiError(400, "INVALID_PAYLOAD", "Edição exige o campo 'revision'.")
    updated = _service().update(
        session,
        schedule_id=schedule_id,
        owner_id=user["id"],
        expected_revision=revision,
        changes=payload,
    )
    if updated is None:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
    return updated


@router.delete("/schedules/{schedule_id}")
def archive_schedule(
    schedule_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    archived = _service().archive(session, schedule_id=schedule_id, owner_id=user["id"])
    if not archived:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
    return {"archived": True}


@router.post("/schedules/{schedule_id}/pause")
def pause_schedule(
    schedule_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    return _service().pause(session, schedule_id=schedule_id, owner_id=user["id"])


@router.post("/schedules/{schedule_id}/resume")
def resume_schedule(
    schedule_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    return _service().resume(session, schedule_id=schedule_id, owner_id=user["id"])


@router.post("/schedules/{schedule_id}/run-now", status_code=202)
def run_now(
    schedule_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(400, "INVALID_PAYLOAD", "O header Idempotency-Key é obrigatório.")
    from automation.run_service import AutomationRunService
    from automation.scheduler import ClaimError

    scheduler = AutomationScheduler(worker_id="api-run-now")
    try:
        run = scheduler.create_manual_occurrence(
            session,
            schedule_id=schedule_id,
            owner_id=user["id"],
            request_key=idempotency_key,
        )
    except ClaimError as exc:
        code = 404 if exc.code == "SCHEDULE_NOT_FOUND" else 409
        raise ApiError(code, exc.code, exc.message) from exc
    session.commit()
    AutomationRunService().start_async(run.id, user["id"])
    return {"run_id": run.id, "state": run.state}


@router.get("/schedules/{schedule_id}/runs")
def list_runs(
    schedule_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    return {"items": AutomationRunService().list_runs(session, owner_id=user["id"], schedule_id=schedule_id)}


@router.get("/schedule-runs/{run_id}")
def get_run(
    run_id: str,
    user: dict[str, Any] = Depends(current_user),
    session=Depends(get_db),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    payload = AutomationRunService().get_run(session, owner_id=user["id"], run_id=run_id)
    if payload is None:
        raise ApiError(404, "RUN_NOT_FOUND", "Ocorrência inexistente ou de outro dono.")
    return payload


@router.get("/schedule-runs/{run_id}/events")
def run_events(
    run_id: str,
    user: dict[str, Any] = Depends(current_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    accept: str | None = Header(default=None),
    after: int = 0,
    session=Depends(get_db),
):
    """Events with content negotiation.

    ``Accept: text/event-stream`` gets the SSE stream (replay via
    ``Last-Event-ID``); anything else gets a JSON page, which is what the SPA
    polls. Same data, same sequence numbers.
    """
    from automation.run_service import AutomationRunService

    service = AutomationRunService()
    if service.get_run(session, owner_id=user["id"], run_id=run_id) is None:
        raise ApiError(404, "RUN_NOT_FOUND", "Ocorrência inexistente ou de outro dono.")
    cursor = int(last_event_id) if (last_event_id and last_event_id.isdigit()) else int(after)

    if not accept or "text/event-stream" not in accept:
        return {"items": service.events(session, run_id=run_id, after=cursor)}
    after = cursor

    def stream():
        import time as _time

        cursor = after
        idle = 0
        while True:
            events = service.events(session, run_id=run_id, after=cursor)
            for event in events:
                cursor = event["sequence"]
                yield (
                    f"id: {event['sequence']}\n"
                    f"event: {event['type']}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                )
            run_payload = service.get_run(session, owner_id=user["id"], run_id=run_id)
            if run_payload is None:
                break
            if run_payload["state"] in {"succeeded", "partial", "no_changes", "failed", "cancelled", "skipped"} and not events:
                yield "event: done\ndata: {}\n\n"
                break
            yield ": heartbeat\n\n"
            idle += 1
            _time.sleep(1)
            if idle > 900:
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/schedule-runs/{run_id}/cancel", status_code=202)
def cancel_run(
    run_id: str,
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    return AutomationRunService().request_cancel(run_id, user["id"])


# ---------------------------------------------------------------------------
# Internal: same surface for the full-mode gateway proxy
# ---------------------------------------------------------------------------


@internal_router.get("/schedules")
def internal_list_schedules(
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    return {"items": _service().list(session, owner_id=_owner(x_owner_id))}


@internal_router.post("/schedules", status_code=201)
def internal_create_schedule(
    payload: dict[str, Any] = Body(default={}),
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    payload.pop("owner_id", None)
    return _service().create(session, owner_id=_owner(x_owner_id), payload=payload)


@internal_router.get("/schedules/{schedule_id}")
def internal_get_schedule(
    schedule_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    payload = _service().get(session, schedule_id=schedule_id, owner_id=_owner(x_owner_id))
    if payload is None:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
    return payload


@internal_router.patch("/schedules/{schedule_id}")
@internal_router.put("/schedules/{schedule_id}")
def internal_update_schedule(
    schedule_id: str,
    payload: dict[str, Any] = Body(default={}),
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    revision = payload.pop("revision", None)
    if not isinstance(revision, int):
        raise ApiError(400, "INVALID_PAYLOAD", "Edição exige o campo 'revision'.")
    updated = _service().update(
        session,
        schedule_id=schedule_id,
        owner_id=_owner(x_owner_id),
        expected_revision=revision,
        changes=payload,
    )
    if updated is None:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
    return updated


@internal_router.delete("/schedules/{schedule_id}")
def internal_archive_schedule(
    schedule_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    archived = _service().archive(session, schedule_id=schedule_id, owner_id=_owner(x_owner_id))
    if not archived:
        raise ApiError(404, "SCHEDULE_NOT_FOUND", "Agenda inexistente ou de outro dono.")
    return {"archived": True}


@internal_router.post("/schedules/{schedule_id}/{action}")
def internal_schedule_action(
    schedule_id: str,
    action: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    owner_id = _owner(x_owner_id)
    if action == "pause":
        return _service().pause(session, schedule_id=schedule_id, owner_id=owner_id)
    if action == "resume":
        return _service().resume(session, schedule_id=schedule_id, owner_id=owner_id)
    if action == "run-now":
        from automation.run_service import AutomationRunService

        key = idempotency_key or "internal-run-now"
        scheduler = AutomationScheduler(worker_id="internal")
        from automation.scheduler import ClaimError

        try:
            run = scheduler.create_manual_occurrence(
                session, schedule_id=schedule_id, owner_id=owner_id, request_key=key
            )
        except ClaimError as exc:
            code = 404 if exc.code == "SCHEDULE_NOT_FOUND" else 409
            raise ApiError(code, exc.code, exc.message) from exc
        session.commit()
        AutomationRunService().start_async(run.id, owner_id)
        return {"run_id": run.id, "state": run.state}
    raise ApiError(404, "NOT_FOUND", "Ação desconhecida.")


@internal_router.get("/schedules/{schedule_id}/runs")
def internal_list_runs(
    schedule_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    return {
        "items": AutomationRunService().list_runs(
            session, owner_id=_owner(x_owner_id), schedule_id=schedule_id
        )
    }


@internal_router.get("/schedule-runs/{run_id}")
def internal_get_run(
    run_id: str,
    x_owner_id: str | None = Header(default=None),
    session=Depends(get_db),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    payload = AutomationRunService().get_run(session, owner_id=_owner(x_owner_id), run_id=run_id)
    if payload is None:
        raise ApiError(404, "RUN_NOT_FOUND", "Ocorrência inexistente ou de outro dono.")
    return payload


@internal_router.get("/schedule-runs/{run_id}/events")
def internal_run_events(
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    x_owner_id: str | None = Header(default=None),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
    owner_id = _owner(x_owner_id)
    service = AutomationRunService()
    return {"items": service.events_owner(run_id, owner_id, after=after)}


@internal_router.post("/schedule-runs/{run_id}/cancel", status_code=202)
def internal_cancel_run(
    run_id: str,
    x_owner_id: str | None = Header(default=None),
    _guard: None = Depends(internal_guard),
) -> dict[str, Any]:
    from automation.run_service import AutomationRunService

    return AutomationRunService().request_cancel(run_id, _owner(x_owner_id))


def _owner(owner_id: str | None) -> str:
    if not owner_id:
        raise ApiError(400, "INVALID_PAYLOAD", "x-owner-id é obrigatório.")
    return owner_id


__all__ = ["router", "internal_router"]
