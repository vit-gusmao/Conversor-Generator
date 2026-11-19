"""Standalone parity with the full-mode gateway (`apps/api`) for three behaviours.

1. `complete` on an expired upload session is refused with UPLOAD_EXPIRED (410),
   the same rule `PUT` already applies.
2. `retry` with scope `upload` is allowed only while the item is still in an
   upload state that can resume (UPLOAD_RESUMABLE_STATUSES); otherwise 409
   UPLOAD_STATE_CONFLICT with `reason: upload_not_resumable`.
3. The conversion events stream honours `run_id`: one run's events only, ended by
   that run's terminal event; without it, ended by the active run's terminal event.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from api.service import ConversionService
from jobqueue import get_queue
from models.db import (
    PublicBatchItemRow,
    PublicConversionRow,
    PublicUploadSessionRow,
    get_session_factory,
)


def _h(device: str) -> dict[str, str]:
    return {"x-device-id": device}


def _create_batch(client, device: str, key: str):
    return client.post("/api/v1/batches", json={"client_batch_key": key}, headers=_h(device))


def _register(client, device: str, batch_id: str, client_item_id: str, filename: str, size: int):
    return client.post(
        f"/api/v1/batches/{batch_id}/items",
        json={"client_item_id": client_item_id, "filename": filename, "size_bytes": size},
        headers=_h(device),
    )


def _send_parts(client, device: str, upload_id: str, part_size: int, payload: bytes) -> None:
    total = max(1, -(-len(payload) // part_size))
    for index in range(total):
        chunk = payload[index * part_size : (index + 1) * part_size]
        receipt = client.put(
            f"/api/v1/uploads/{upload_id}/parts/{index}",
            content=chunk,
            headers={
                **_h(device),
                "Content-Type": "application/octet-stream",
                "x-part-sha256": hashlib.sha256(chunk).hexdigest(),
            },
        )
        assert receipt.status_code == 200, receipt.text


def _list_items(client, device: str, batch_id: str) -> list[dict]:
    response = client.get(f"/api/v1/batches/{batch_id}/items?limit=200", headers=_h(device))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _item(client, device: str, batch_id: str, item_id: str) -> dict:
    return next(item for item in _list_items(client, device, batch_id) if item["item_id"] == item_id)


def _session_row(upload_id: str) -> PublicUploadSessionRow:
    session = get_session_factory()()
    try:
        return session.get(PublicUploadSessionRow, upload_id)
    finally:
        session.close()


def _expire_upload(upload_id: str) -> None:
    session = get_session_factory()()
    try:
        row = session.get(PublicUploadSessionRow, upload_id)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
    finally:
        session.close()


def _conversion_active_run(conversion_id: str) -> str | None:
    session = get_session_factory()()
    try:
        return session.get(PublicConversionRow, conversion_id).active_run_id
    finally:
        session.close()


def _stream_ids(body: str) -> list[int]:
    return [int(line.split(":", 1)[1]) for line in body.splitlines() if line.startswith("id:")]


def _event_names(body: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in body.splitlines() if line.startswith("event:")]


# --------------------------------------------------------------------------- #
# 1. complete refuses an expired upload session
# --------------------------------------------------------------------------- #


def test_complete_refuses_an_expired_upload_without_creating_a_conversion(api_client, pdf_paths):
    device = "dev_parity_expired"
    batch_id = _create_batch(api_client, device, "parity-expired").json()["batch_id"]
    payload = pdf_paths["simple_field"].read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    registered = _register(api_client, device, batch_id, "doc-1", "expired.pdf", len(payload))
    assert registered.status_code == 201, registered.text
    body = registered.json()
    upload_id, item_id = body["upload_id"], body["item"]["item_id"]
    _send_parts(api_client, device, upload_id, body["part_size"], payload)

    _expire_upload(upload_id)
    total = max(1, -(-len(payload) // body["part_size"]))
    complete = api_client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        json={"sha256": sha, "total_parts": total},
        headers=_h(device),
    )

    assert complete.status_code == 410, complete.text
    assert complete.json()["error"]["code"] == "UPLOAD_EXPIRED"
    stored = _item(api_client, device, batch_id, item_id)
    assert stored["conversion_id"] is None
    assert stored["status"] != "queued"
    assert _session_row(upload_id).state != "completed"


# --------------------------------------------------------------------------- #
# 2. retry scope "upload" only while the upload can resume
# --------------------------------------------------------------------------- #


def test_upload_retry_on_a_cancelled_item_without_conversion_is_a_conflict(api_client):
    device = "dev_parity_cancelled_retry"
    batch_id = _create_batch(api_client, device, "parity-cancelled").json()["batch_id"]
    registered = _register(api_client, device, batch_id, "doc-1", "cancelled.pdf", 1024)
    assert registered.status_code == 201, registered.text
    item_id = registered.json()["item"]["item_id"]
    cancelled = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/cancel", headers=_h(device)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert _item(api_client, device, batch_id, item_id)["status"] == "cancelled"

    retry = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/retry",
        json={"scope": "upload"},
        headers=_h(device),
    )

    assert retry.status_code == 409, retry.text
    error = retry.json()["error"]
    assert error["code"] == "UPLOAD_STATE_CONFLICT"
    assert error["details"]["reason"] == "upload_not_resumable"
    assert error["details"]["status"] == "cancelled"
    assert _item(api_client, device, batch_id, item_id)["status"] == "cancelled"


def test_upload_retry_resumes_pending_and_aborted_uploads(api_client):
    device = "dev_parity_resumable"
    batch_id = _create_batch(api_client, device, "parity-resumable").json()["batch_id"]
    registered = _register(api_client, device, batch_id, "doc-1", "resume.pdf", 1024)
    assert registered.status_code == 201, registered.text
    upload_id = registered.json()["upload_id"]
    item_id = registered.json()["item"]["item_id"]

    # pending_upload is resumable.
    pending = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/retry",
        json={"scope": "upload"},
        headers=_h(device),
    )
    assert pending.status_code == 200, pending.text

    # upload_failed is a resumable state, so the status gate lets it through.
    # Abort clears the item's upload id (in both modes), so the retry then stops
    # at UPLOAD_NOT_FOUND, not at the state check.
    aborted = api_client.delete(f"/api/v1/uploads/{upload_id}", headers=_h(device))
    assert aborted.status_code == 200, aborted.text
    assert _item(api_client, device, batch_id, item_id)["status"] == "upload_failed"
    resumed = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/retry",
        json={"scope": "upload"},
        headers=_h(device),
    )
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["error"]["code"] == "UPLOAD_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 3. conversion events honour run_id
# --------------------------------------------------------------------------- #


def test_run_scoped_stream_is_not_closed_by_an_older_run_failure(api_client, pdf_paths, monkeypatch):
    """A failed run, then a retry: the new run's stream must not end on the old failure.

    Jobs are not executed (submit is stubbed) so each run's events are written by
    the test, in a known order: 1 queued(run1), 2 failed(run1), 3 queued(run2),
    4 completed(run2).
    """
    device = "dev_parity_run_stream"
    monkeypatch.setattr(get_queue(), "submit", lambda run_id, payload: None)
    created = api_client.post(
        "/api/v1/conversions",
        files={"file": ("runs.pdf", pdf_paths["simple_field"].read_bytes(), "application/pdf")},
        headers=_h(device),
    )
    assert created.status_code == 202, created.text
    conversion_id = created.json()["conversion_id"]
    first_run = _conversion_active_run(conversion_id)
    assert first_run

    repo = ConversionService().repo
    repo.finish_run(
        first_run,
        status="failed",
        quality="failed",
        stats={},
        validation=None,
        error_code="ENGINE_FAILED",
        error_message="Falha simulada.",
    )
    retry = api_client.post(f"/api/v1/conversions/{conversion_id}/retry", headers=_h(device))
    assert retry.status_code == 202, retry.text
    second_run = _conversion_active_run(conversion_id)
    assert second_run and second_run != first_run

    # The old run's stream is closed by its own terminal failure.
    old = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events?run_id={first_run}", headers=_h(device)
    )
    assert old.status_code == 200, old.text
    old_ids = _stream_ids(old.text)
    assert _event_names(old.text)[-1] == "failed"
    failed_seq = old_ids[-1]

    # The new run's stream is open while the new run is still queued. The old
    # failure is not its event, so the stream must stay open past it.
    result: dict[str, object] = {}

    def follow_new_run() -> None:
        result["response"] = api_client.get(
            f"/api/v1/conversions/{conversion_id}/events?run_id={second_run}", headers=_h(device)
        )

    worker = threading.Thread(target=follow_new_run, daemon=True)
    worker.start()
    time.sleep(1.5)
    assert worker.is_alive(), "run-scoped stream ended on an older run's failure"

    repo.finish_run(
        second_run,
        status="completed",
        quality="high",
        stats={},
        validation=None,
    )
    worker.join(timeout=30)
    assert not worker.is_alive(), "run-scoped stream did not end on its own terminal event"

    new = result["response"]
    assert new.status_code == 200, new.text
    new_ids = _stream_ids(new.text)
    assert new_ids and all(seq > failed_seq for seq in new_ids)
    assert _event_names(new.text)[-1] == "completed"
    assert "event: failed" not in new.text

    # Without run_id the stream follows the whole conversion and ends on the
    # current run's terminal event, after having replayed the old failure.
    whole = api_client.get(f"/api/v1/conversions/{conversion_id}/events", headers=_h(device))
    assert whole.status_code == 200, whole.text
    assert failed_seq in _stream_ids(whole.text)
    assert _event_names(whole.text)[-1] == "completed"


def test_run_id_is_validated_and_scoped_before_streaming(api_client, pdf_paths, monkeypatch):
    device = "dev_parity_run_validation"
    other = "dev_parity_run_validation_other"
    monkeypatch.setattr(get_queue(), "submit", lambda run_id, payload: None)
    created = api_client.post(
        "/api/v1/conversions",
        files={"file": ("validate.pdf", pdf_paths["simple_field"].read_bytes(), "application/pdf")},
        headers=_h(device),
    )
    assert created.status_code == 202, created.text
    conversion_id = created.json()["conversion_id"]

    malformed = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events?run_id=not%20valid!", headers=_h(device)
    )
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["error"]["code"] == "INVALID_OPTION"

    unknown = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events?run_id=run_does_not_exist", headers=_h(device)
    )
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "NOT_FOUND"

    foreign = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events?run_id=run_does_not_exist", headers=_h(other)
    )
    assert foreign.status_code == 403, foreign.text
