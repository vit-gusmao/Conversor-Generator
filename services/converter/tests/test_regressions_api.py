"""Regression tests for the FX-1b public API audit fixes.

Each test names the finding it guards (AU1-xx / S-xx). They are written to
fail on the pre-fix code and pass once the behaviour is corrected.
"""

from __future__ import annotations

import glob
import hashlib
import io
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from sqlalchemy import func, select

from api.deps import DEVICE_COOKIE, ApiError
from api.serialization import content_disposition, iso_utc
from api.service import ConversionService, _detail, _normalize_options
from jobqueue import get_queue
from models.db import PublicBatchItemRow, PublicConversionRow, Run, get_session_factory

INTERNAL = {"x-internal-token": "test-internal-token"}
TERMINAL_ITEM = {"completed", "completed_with_warnings", "failed", "cancelled"}


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


def _upload_bytes(client, device: str, batch_id: str, client_item_id: str, filename: str, payload: bytes):
    """Registers and transfers every part; returns (upload_id, item_id, sha, parts)."""
    registered = _register(client, device, batch_id, client_item_id, filename, len(payload))
    assert registered.status_code == 201, registered.text
    body = registered.json()
    upload_id = body["upload_id"]
    part_size = body["part_size"]
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
    return upload_id, body["item"]["item_id"], hashlib.sha256(payload).hexdigest(), total


def _complete(client, device: str, upload_id: str, sha: str, parts: int):
    return client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        json={"sha256": sha, "total_parts": parts},
        headers=_h(device),
    )


def _list_items(client, device: str, batch_id: str, query: str = "") -> dict:
    response = client.get(f"/api/v1/batches/{batch_id}/items{query}", headers=_h(device))
    assert response.status_code == 200, response.text
    return response.json()


def _wait_item_terminal(client, device: str, batch_id: str, item_id: str, timeout: float = 90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in _list_items(client, device, batch_id, "?limit=200")["items"]:
            if item["item_id"] == item_id and item["status"] in TERMINAL_ITEM:
                return item
        time.sleep(0.2)
    raise AssertionError("item did not reach a terminal status in time")


def _force_stored_status(item_id: str, status: str) -> None:
    session = get_session_factory()()
    try:
        item = session.get(PublicBatchItemRow, item_id)
        item.status = status
        session.commit()
    finally:
        session.close()


def _force_conversion_status(conversion_id: str, status: str) -> None:
    session = get_session_factory()()
    try:
        conversion = session.get(PublicConversionRow, conversion_id)
        conversion.status = status
        session.commit()
    finally:
        session.close()


def _run_count(conversion_id: str) -> int:
    session = get_session_factory()()
    try:
        return int(
            session.execute(
                select(func.count()).select_from(Run).where(Run.conversion_id == conversion_id)
            ).scalar()
            or 0
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# AU1-03 terminal batch status
# --------------------------------------------------------------------------- #


def test_sealed_batch_with_warning_items_reaches_a_terminal_status():
    """AU1-03: `with_warnings` must count toward the terminal total."""
    from api.batch_service import _batch_status

    status = _batch_status(
        {"total_items": 2, "completed": 1, "with_warnings": 1, "failed": 0, "cancelled": 0},
        sealed=True,
        cancel_requested=False,
    )
    assert status != "processing"
    # Warnings are reviewable outcomes and keep the batch flagged (current policy).
    assert status == "completed_with_errors"


def test_sealed_batch_with_upload_failed_items_reaches_a_terminal_status():
    """AU1-03 (adjacent): upload_failed is a terminal outcome once sealed."""
    from api.batch_service import _batch_status

    status = _batch_status(
        {"total_items": 2, "completed": 1, "upload_failed": 1},
        sealed=True,
        cancel_requested=False,
    )
    assert status == "completed_with_errors"


def test_sealed_batch_of_clean_completions_is_completed():
    from api.batch_service import _batch_status

    status = _batch_status({"total_items": 2, "completed": 2}, sealed=True, cancel_requested=False)
    assert status == "completed"


# --------------------------------------------------------------------------- #
# AU1-04 derived item status in cancel / retry
# --------------------------------------------------------------------------- #


def test_cancel_batch_keeps_finished_items_finished(api_client, pdf_paths):
    """AU1-04: cancelling a batch must not overwrite completed items."""
    device = "dev_reg_cancel_batch"
    batch_id = _create_batch(api_client, device, "reg-cancel-batch").json()["batch_id"]
    payload = pdf_paths["mixed_report"].read_bytes()
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "doc-1", "report.pdf", payload
    )
    assert _complete(api_client, device, upload_id, sha, parts).status_code == 202
    finished = _wait_item_terminal(api_client, device, batch_id, item_id)
    assert finished["status"] in {"completed", "completed_with_warnings"}

    cancel = api_client.post(f"/api/v1/batches/{batch_id}/cancel", headers=_h(device))
    assert cancel.status_code == 200, cancel.text

    kept = _list_items(api_client, device, batch_id)["items"][0]
    assert kept["status"] == finished["status"]


def test_cancel_item_on_a_finished_item_keeps_its_status(api_client, pdf_paths):
    """AU1-04: cancel_item on a finished item is a no-op for its status."""
    device = "dev_reg_cancel_item"
    batch_id = _create_batch(api_client, device, "reg-cancel-item").json()["batch_id"]
    payload = pdf_paths["mixed_report"].read_bytes()
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "doc-1", "report.pdf", payload
    )
    assert _complete(api_client, device, upload_id, sha, parts).status_code == 202
    finished = _wait_item_terminal(api_client, device, batch_id, item_id)

    response = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/cancel", headers=_h(device)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == finished["status"]


def test_retry_conversion_of_a_finished_item_is_not_reported_busy(api_client, pdf_paths):
    """AU1-04: a finished conversion must be retryable, not CONVERSION_BUSY."""
    device = "dev_reg_retry_conv"
    batch_id = _create_batch(api_client, device, "reg-retry-conv").json()["batch_id"]
    payload = pdf_paths["mixed_report"].read_bytes()
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "doc-1", "report.pdf", payload
    )
    assert _complete(api_client, device, upload_id, sha, parts).status_code == 202
    _wait_item_terminal(api_client, device, batch_id, item_id)

    retry = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/retry",
        json={"scope": "conversion"},
        headers=_h(device),
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["conversion"]["conversion_id"]


# --------------------------------------------------------------------------- #
# AU1-20 upload-scope retry on converted items; replay repairs upload status
# --------------------------------------------------------------------------- #


def test_upload_scope_retry_on_a_converted_item_is_rejected(api_client, pdf_paths):
    """AU1-20: a converted item must not be pushed back into the upload phase."""
    device = "dev_reg_retry_upload"
    batch_id = _create_batch(api_client, device, "reg-retry-upload").json()["batch_id"]
    payload = pdf_paths["mixed_report"].read_bytes()
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "doc-1", "report.pdf", payload
    )
    assert _complete(api_client, device, upload_id, sha, parts).status_code == 202
    _wait_item_terminal(api_client, device, batch_id, item_id)

    retry = api_client.post(
        f"/api/v1/batches/{batch_id}/items/{item_id}/retry",
        json={"scope": "upload"},
        headers=_h(device),
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["code"] == "ALREADY_CONVERTED"
    stored = _list_items(api_client, device, batch_id)["items"][0]
    assert stored["status"] in TERMINAL_ITEM


def test_complete_replay_repairs_an_upload_phase_status_on_a_converted_item(api_client, pdf_paths):
    """AU1-20: replaying complete must not leave a converted item in 'uploading'."""
    device = "dev_reg_replay_repair"
    batch_id = _create_batch(api_client, device, "reg-replay-repair").json()["batch_id"]
    payload = pdf_paths["mixed_report"].read_bytes()
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "doc-1", "report.pdf", payload
    )
    assert _complete(api_client, device, upload_id, sha, parts).status_code == 202
    _wait_item_terminal(api_client, device, batch_id, item_id)

    # Simulates rows damaged by the previous retry-upload behaviour.
    _force_stored_status(item_id, "uploading")

    replay = _complete(api_client, device, upload_id, sha, parts)
    assert replay.status_code == 202, replay.text
    assert replay.json()["replayed"] is True
    stored = _list_items(api_client, device, batch_id)["items"][0]
    assert stored["status"] not in {"uploading", "pending_upload", "upload_failed"}


# --------------------------------------------------------------------------- #
# AU1-21 status filter applied before the page limit
# --------------------------------------------------------------------------- #


def test_status_filter_is_applied_before_the_page_limit(api_client):
    device = "dev_reg_filter"
    batch_id = _create_batch(api_client, device, "reg-filter").json()["batch_id"]
    ids = []
    for index in range(4):
        registered = _register(api_client, device, batch_id, f"f-{index}", f"f{index}.pdf", 1024)
        assert registered.status_code == 201, registered.text
        ids.append(registered.json()["item"]["item_id"])
    for cancelled in (ids[0], ids[2]):
        response = api_client.post(
            f"/api/v1/batches/{batch_id}/items/{cancelled}/cancel", headers=_h(device)
        )
        assert response.status_code == 200, response.text

    first = _list_items(api_client, device, batch_id, "?status=pending_upload&limit=1")
    assert [item["item_id"] for item in first["items"]] == [ids[1]]
    assert first["next_cursor"] is not None

    second = _list_items(
        api_client,
        device,
        batch_id,
        f"?status=pending_upload&limit=1&cursor={first['next_cursor']}",
    )
    assert [item["item_id"] for item in second["items"]] == [ids[3]]
    assert second["next_cursor"] is None


# --------------------------------------------------------------------------- #
# AU1-18 no size ceiling on resumable registration
# --------------------------------------------------------------------------- #


def test_resumable_registration_has_no_product_size_ceiling(api_client):
    device = "dev_reg_big"
    batch_id = _create_batch(api_client, device, "reg-big").json()["batch_id"]
    response = _register(api_client, device, batch_id, "big-1", "big.pdf", 60 * 1024 * 1024)
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# AU1-05 invalid spreadsheet on complete; AU1-06 probe without a local path
# --------------------------------------------------------------------------- #


def test_invalid_spreadsheet_on_complete_is_a_400_and_cleans_up(api_client):
    """AU1-05: an unsupported spreadsheet is a 400 with no AttributeError."""
    from storage import get_storage

    device = "dev_reg_bad_csv"
    batch_id = _create_batch(api_client, device, "reg-bad-csv").json()["batch_id"]
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "bad-1", "bad.csv", b"\x00\x01\x02\xff\xfe"
    )
    response = _complete(api_client, device, upload_id, sha, parts)
    assert response.status_code == 400, response.text
    # The probe's own reason code is surfaced; INVALID_FILE only when it has none.
    from ingestion.registry import probe_input_bytes

    probe = probe_input_bytes(bytes([0, 1, 2, 255, 254]), "bad.csv")
    expected = getattr(probe, "reason_code", None) or "INVALID_FILE"
    assert response.json()["error"]["code"] == expected
    assert not get_storage().exists(f"uploads/{upload_id}/assembled.bin")
    stored = _list_items(api_client, device, batch_id)["items"][0]
    assert stored["status"] == "upload_failed"


class _NoLocalPathStorage:
    """Stands in for an object store: no key maps to a local filesystem path."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def local_path(self, key):  # noqa: ARG002
        return None

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_upload_probe_works_when_storage_has_no_local_path(api_client, pdf_paths, monkeypatch):
    """AU1-06: a non-filesystem backend must still complete a PDF upload."""
    import api.upload_service as upload_service
    from storage import get_storage

    monkeypatch.setattr(upload_service, "get_storage", lambda: _NoLocalPathStorage(get_storage()))
    probe_glob = os.path.join(tempfile.gettempdir(), "pdf2x-probe-*")
    before = set(glob.glob(probe_glob))

    device = "dev_reg_s3_probe"
    batch_id = _create_batch(api_client, device, "reg-s3-probe").json()["batch_id"]
    payload = pdf_paths["mixed_report"].read_bytes()
    upload_id, item_id, sha, parts = _upload_bytes(
        api_client, device, batch_id, "doc-1", "report.pdf", payload
    )
    response = _complete(api_client, device, upload_id, sha, parts)
    assert response.status_code == 202, response.text
    assert response.json()["conversion_id"].startswith("conv_")
    assert set(glob.glob(probe_glob)) - before == set(), "probe temp file was not removed"


# --------------------------------------------------------------------------- #
# AU1-19 idempotent create that loses a race must not queue a second run
# --------------------------------------------------------------------------- #


def test_idempotent_create_that_loses_a_race_does_not_queue_a_second_run(monkeypatch):
    queue = get_queue()
    submitted: list[str] = []
    monkeypatch.setattr(queue, "submit", lambda run_id, payload: submitted.append(run_id))

    owner = f"usr_race_{uuid.uuid4().hex[:10]}"
    key = f"race-{uuid.uuid4().hex}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    kwargs = {
        "owner_id": owner,
        "storage_key": f"documents/{owner}/{digest}.pdf",
        "sha256": digest,
        "size_bytes": 10,
        "filename": "race.pdf",
        "mime_type": "application/pdf",
        "normalized": _normalize_options(None, "pdf"),
        "page_count": None,
        "idempotency_key": key,
    }
    service = ConversionService()
    winner = service.create_from_stored(**kwargs)
    assert len(submitted) == 1

    real_idempotent = ConversionService._idempotent
    lookups = {"count": 0}

    def first_lookup_misses(self, *args, **kw):
        # The loser passed the pre-check before the winner committed.
        lookups["count"] += 1
        if lookups["count"] == 1:
            return None
        return real_idempotent(self, *args, **kw)

    monkeypatch.setattr(ConversionService, "_idempotent", first_lookup_misses)
    loser = service.create_from_stored(**kwargs)

    assert loser["conversion_id"] == winner["conversion_id"]
    assert len(submitted) == 1, "the losing request must not submit a second run"


# --------------------------------------------------------------------------- #
# S3 concurrent retry of a terminal conversion creates exactly one run
# --------------------------------------------------------------------------- #


def test_concurrent_retry_of_a_terminal_conversion_creates_one_run(monkeypatch):
    from api.deps import ApiError as _ApiError

    queue = get_queue()
    submitted: list[str] = []
    monkeypatch.setattr(queue, "submit", lambda run_id, payload: submitted.append(run_id))

    owner = f"usr_retry_{uuid.uuid4().hex[:10]}"
    digest = hashlib.sha256(owner.encode()).hexdigest()
    created = ConversionService().create_from_stored(
        owner_id=owner,
        storage_key=f"documents/{owner}/{digest}.pdf",
        sha256=digest,
        size_bytes=10,
        filename="retry.pdf",
        mime_type="application/pdf",
        normalized=_normalize_options(None, "pdf"),
        page_count=None,
    )
    conversion_id = created["conversion_id"]
    _force_conversion_status(conversion_id, "failed")
    assert _run_count(conversion_id) == 1

    real_load = ConversionService._load
    raced = {"done": False}

    def racing_load(self, session, owner_id, conversion_id_):
        conversion = real_load(self, session, owner_id, conversion_id_)
        if not raced["done"]:
            # Another request claims the same terminal conversion first.
            raced["done"] = True
            ConversionService().retry(owner_id=owner_id, conversion_id=conversion_id_)
        return conversion

    monkeypatch.setattr(ConversionService, "_load", racing_load)
    with pytest.raises(_ApiError) as excinfo:
        ConversionService().retry(owner_id=owner, conversion_id=conversion_id)

    assert excinfo.value.status_code == 409
    assert _run_count(conversion_id) == 2, "only the winning retry may create a run"


# --------------------------------------------------------------------------- #
# S2 detail without an active run
# --------------------------------------------------------------------------- #


def test_detail_without_an_active_run_returns_the_conversion_without_run():
    created = datetime(2026, 1, 2, 3, 4, 5)
    conversion = SimpleNamespace(
        id="conv_detail",
        status="queued",
        stage="receive",
        progress=0.0,
        quality="pending",
        stats_json=None,
        validation_json=None,
        active_run_id=None,
        document_id="doc_detail",
        created_at=created,
        updated_at=created,
    )
    document = SimpleNamespace(
        id="doc_detail",
        filename="a.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="0" * 64,
        page_count=1,
        created_at=created,
    )
    detail = _detail(conversion, document, None)
    assert detail["run"] is None
    assert detail["conversion_id"] == "conv_detail"
    assert detail["created_at"] == "2026-01-02T03:04:05+00:00"


# --------------------------------------------------------------------------- #
# AU1-10 explicit UTC offsets
# --------------------------------------------------------------------------- #


def test_iso_utc_treats_naive_as_utc_and_normalizes_offsets():
    assert iso_utc(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05+00:00"
    local = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert iso_utc(local) == "2026-01-02T03:00:00+00:00"
    assert iso_utc(None) is None


def test_conversion_timestamps_carry_an_explicit_offset(api_client, pdf_paths):
    device = "dev_iso_conv"
    payload = pdf_paths["mixed_report"].read_bytes()
    created = api_client.post(
        "/api/v1/conversions",
        files={"file": ("ts.pdf", payload, "application/pdf")},
        headers=_h(device),
    )
    assert created.status_code == 202, created.text
    conversion_id = created.json()["conversion_id"]

    listing = api_client.get("/api/v1/conversions", headers=_h(device)).json()
    listed = next(item for item in listing["items"] if item["conversion_id"] == conversion_id)
    assert listed["created_at"].endswith("+00:00")
    assert listed["updated_at"].endswith("+00:00")

    detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=_h(device)).json()
    assert detail["created_at"].endswith("+00:00")
    assert detail["document"]["created_at"].endswith("+00:00")


# --------------------------------------------------------------------------- #
# AU1-22 Content-Disposition with non-latin-1 filenames
# --------------------------------------------------------------------------- #


def test_content_disposition_is_latin1_safe_and_carries_utf8():
    name = "relatório 文档.xlsx"
    value = content_disposition(name)
    value.encode("latin-1")  # must not raise
    assert value.startswith("attachment;")
    assert "filename*=UTF-8''" in value
    assert unquote(value.split("filename*=UTF-8''", 1)[1]) == name


def test_content_disposition_neutralizes_quotes_and_separators():
    value = content_disposition('a"b\\c\r\n.pdf')
    assert value.count('"') == 2
    assert "\\" not in value.split(";", 1)[1].split("filename*")[0]
    assert "\r" not in value and "\n" not in value


def test_conversion_download_with_non_latin_name_does_not_500(api_client, monkeypatch):
    from api import public_routes

    def fake_stream(*, owner_id, conversion_id):  # noqa: ARG001
        return io.BytesIO(b"xlsx-bytes"), "文档-1.xlsx", (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    monkeypatch.setattr(public_routes.service, "download_stream", fake_stream)
    response = api_client.get("/api/v1/conversions/conv_fake/download", headers=_h("dev_cd_dl"))
    assert response.status_code == 200
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


def test_export_download_with_non_latin_name_does_not_500(api_client, monkeypatch):
    from api import public_routes

    def fake_export(*, owner_id, conversion_id, export_id):  # noqa: ARG001
        return b"texto", "exportação 表.txt", "text/plain; charset=utf-8"

    monkeypatch.setattr(public_routes.service, "download_export", fake_export)
    response = api_client.get(
        "/api/v1/conversions/conv_fake/exports/exp_fake/download", headers=_h("dev_cd_exp")
    )
    assert response.status_code == 200
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


# --------------------------------------------------------------------------- #
# AU1-25 root grants are committed by the route itself
# --------------------------------------------------------------------------- #


def test_grant_and_revoke_root_persist_without_the_teardown_commit(api_client, tmp_path):
    """AU1-25: the route commits; the session is closed without committing here."""
    from api.deps import get_db
    from api.main import app
    from sources.service import SourceService

    def session_without_teardown_commit():
        session = get_session_factory()()
        try:
            yield session
        finally:
            session.close()

    owner = f"usr_root_{uuid.uuid4().hex[:10]}"
    root_dir = tmp_path / "root-grant"
    root_dir.mkdir()
    app.dependency_overrides[get_db] = session_without_teardown_commit
    try:
        granted = api_client.post(
            "/internal/v1/source-roots",
            json={"owner_id": owner, "canonical_path": str(root_dir)},
            headers=INTERNAL,
        )
        assert granted.status_code == 201, granted.text
        root_id = granted.json()["id"]

        check = get_session_factory()()
        try:
            visible = [root["id"] for root in SourceService().list_roots(check, owner_id=owner)]
        finally:
            check.close()
        assert visible == [root_id]

        revoked = api_client.delete(
            f"/internal/v1/source-roots/{root_id}",
            headers={**INTERNAL, "x-owner-id": owner},
        )
        assert revoked.status_code == 200, revoked.text

        check = get_session_factory()()
        try:
            after = [r for r in SourceService().list_roots(check, owner_id=owner) if r["id"] == root_id]
        finally:
            check.close()
        assert not after or after[0]["runtime_available"] is False
    finally:
        app.dependency_overrides.pop(get_db, None)


# --------------------------------------------------------------------------- #
# AU1-26 SSE routes do not hold a request session for the stream
# --------------------------------------------------------------------------- #


def test_sse_routes_do_not_hold_a_request_session_for_the_stream():
    from fastapi.routing import APIRoute

    from api.batch_routes import router as batch_router
    from api.deps import current_user, get_db
    from api.public_routes import router as public_router
    from api.schedule_routes import router as schedule_router

    sse_paths = {
        "/api/v1/conversions/{conversion_id}/events",
        "/api/v1/batches/{batch_id}/events",
        "/api/v1/schedule-runs/{run_id}/events",
    }
    forbidden = {get_db, current_user}

    def walk(dependant):
        for sub in dependant.dependencies:
            yield sub.call
            yield from walk(sub)

    found: set[str] = set()
    for router in (public_router, batch_router, schedule_router):
        for route in router.routes:
            if isinstance(route, APIRoute) and route.path in sse_paths and "GET" in route.methods:
                found.add(route.path)
                held = set(walk(route.dependant)) & forbidden
                assert not held, f"{route.path} keeps a request session for the whole stream"
    assert found == sse_paths


# --------------------------------------------------------------------------- #
# S18 conversion SSE honours the device cookie
# --------------------------------------------------------------------------- #


def test_conversion_events_honor_the_device_cookie(api_client, pdf_paths):
    device = "dev_sse_cookie"
    payload = pdf_paths["mixed_report"].read_bytes()
    created = api_client.post(
        "/api/v1/conversions",
        files={"file": ("sse.pdf", payload, "application/pdf")},
        headers=_h(device),
    )
    assert created.status_code == 202, created.text
    conversion_id = created.json()["conversion_id"]

    deadline = time.time() + 90
    while time.time() < deadline:
        status = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=_h(device)).json()
        if status["status"] in {"completed", "completed_with_warnings", "failed", "cancelled"}:
            break
        time.sleep(0.2)

    stream = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events",
        cookies={DEVICE_COOKIE: device},
    )
    assert stream.status_code == 200
    assert "event: error" not in stream.text
    assert "data: {" in stream.text


# --------------------------------------------------------------------------- #
# AU1-31 knowledge search rejects a non-integer top_k with a 400
# --------------------------------------------------------------------------- #


def test_knowledge_search_rejects_a_non_integer_top_k(api_client):
    response = api_client.post(
        "/api/v1/knowledge/search",
        json={"query": "contrato", "top_k": "abc"},
        headers=_h("dev_knowledge_topk"),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_OPTION"


# --------------------------------------------------------------------------- #
# AU1-29 pause-all requires an owner
# --------------------------------------------------------------------------- #


def test_internal_pause_without_an_owner_is_rejected(api_client):
    response = api_client.post("/internal/v1/automation/pause", headers=INTERNAL)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_internal_pause_with_an_owner_is_scoped(api_client):
    owner = f"usr_pause_{uuid.uuid4().hex[:10]}"
    response = api_client.post(
        "/internal/v1/automation/pause", headers={**INTERNAL, "x-owner-id": owner}
    )
    assert response.status_code == 200, response.text
    assert response.json()["paused"] == 0


