"""Batch lifecycle and resumable upload contract (standalone API).

Mirrors `apps/api/test/e2e/batches.e2e-spec.ts`.
"""

from __future__ import annotations

import hashlib
import io
import time

import pytest

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}


def _headers(device: str) -> dict[str, str]:
    return {"x-device-id": device}


def _create_batch(client, device: str, key: str):
    return client.post("/api/v1/batches", json={"client_batch_key": key}, headers=_headers(device))


def _register(client, device: str, batch_id: str, client_item_id: str, filename: str, size: int, options=None):
    return client.post(
        f"/api/v1/batches/{batch_id}/items",
        json={
            "client_item_id": client_item_id,
            "filename": filename,
            "size_bytes": size,
            "options": options,
        },
        headers=_headers(device),
    )


def _put_part(client, device: str, upload_id: str, part: int, data: bytes, sha: str | None = None):
    headers = _headers(device)
    headers["Content-Type"] = "application/octet-stream"
    headers["x-part-sha256"] = sha if sha is not None else hashlib.sha256(data).hexdigest()
    return client.put(
        f"/api/v1/uploads/{upload_id}/parts/{part}", content=data, headers=headers
    )


def _complete(client, device: str, upload_id: str, sha: str, parts: int):
    return client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        json={"sha256": sha, "total_parts": parts},
        headers=_headers(device),
    )


def _upload_document(client, device: str, batch_id: str, client_item_id: str, path):
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    registered = _register(client, device, batch_id, client_item_id, path.name, len(payload))
    assert registered.status_code == 201, registered.text
    body = registered.json()
    upload_id = body["upload_id"]
    part_size = body["part_size"]
    assert part_size > 0

    total_parts = (len(payload) + part_size - 1) // part_size
    for index in range(total_parts):
        chunk = payload[index * part_size : (index + 1) * part_size]
        receipt = _put_part(client, device, upload_id, index, chunk)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["part_number"] == index

    completed = _complete(client, device, upload_id, digest, total_parts)
    assert completed.status_code == 202, completed.text
    return completed.json(), upload_id, digest, total_parts


def _wait_item(client, device: str, batch_id: str, item_id: str, timeout: float = 90.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        listing = client.get(
            f"/api/v1/batches/{batch_id}/items?limit=200", headers=_headers(device)
        )
        assert listing.status_code == 200, listing.text
        for item in listing.json()["items"]:
            if item["item_id"] == item_id:
                last = item
                if item["status"] in {"completed", "completed_with_warnings", "failed", "cancelled"}:
                    return item
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for item: {last}")


# --------------------------------------------------------------------------- #
# Batch lifecycle
# --------------------------------------------------------------------------- #


def test_batch_replay_returns_the_same_batch(api_client):
    first = _create_batch(api_client, "dev_batch_replay", "key-abc")
    assert first.status_code == 201, first.text
    second = _create_batch(api_client, "dev_batch_replay", "key-abc")
    assert second.status_code == 201
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert second.json()["status"] == "accepting"


def test_batches_are_isolated_between_owners(api_client):
    created = _create_batch(api_client, "dev_owner_a", "shared-key").json()
    other = api_client.get(
        f"/api/v1/batches/{created['batch_id']}", headers=_headers("dev_owner_b")
    )
    assert other.status_code == 403


def test_register_item_replays_and_detects_divergence(api_client):
    batch = _create_batch(api_client, "dev_item_replay", "key-div").json()
    batch_id = batch["batch_id"]

    first = _register(api_client, "dev_item_replay", batch_id, "item-1", "a.pdf", 1024)
    assert first.status_code == 201, first.text
    assert first.json()["replayed"] is False

    same = _register(api_client, "dev_item_replay", batch_id, "item-1", "a.pdf", 1024)
    assert same.status_code == 201
    assert same.json()["replayed"] is True
    assert same.json()["item"]["item_id"] == first.json()["item"]["item_id"]
    assert same.json()["upload_id"] == first.json()["upload_id"]

    divergent = _register(api_client, "dev_item_replay", batch_id, "item-1", "a.pdf", 2048)
    assert divergent.status_code == 409
    assert divergent.json()["error"]["code"] == "IDEMPOTENT_REPLAY"


def test_unknown_extension_is_rejected(api_client):
    batch = _create_batch(api_client, "dev_item_kind", "key-kind").json()
    response = _register(api_client, "dev_item_kind", batch["batch_id"], "x-1", "notes.docx", 10)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_FILE"


def test_more_than_a_thousand_items_paginate_without_omission(api_client):
    device = "dev_many_items"
    batch = _create_batch(api_client, device, "key-many").json()
    batch_id = batch["batch_id"]

    total = 1001
    for index in range(total):
        response = _register(
            api_client, device, batch_id, f"item-{index:04d}", f"doc-{index:04d}.pdf", 100
        )
        assert response.status_code == 201, response.text

    seen: list[str] = []
    cursor = None
    for _ in range(20):
        query = f"/api/v1/batches/{batch_id}/items?limit=200"
        if cursor:
            query += f"&cursor={cursor}"
        page = api_client.get(query, headers=_headers(device))
        assert page.status_code == 200, page.text
        body = page.json()
        seen.extend(item["client_item_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            break

    assert len(seen) == total
    assert len(set(seen)) == total

    summary = api_client.get(f"/api/v1/batches/{batch_id}", headers=_headers(device)).json()
    assert summary["counts"]["total_items"] == total
    assert summary["status"] == "accepting"


def test_seal_is_blocked_while_uploads_are_pending(api_client):
    device = "dev_seal"
    batch = _create_batch(api_client, device, "key-seal").json()
    batch_id = batch["batch_id"]
    _register(api_client, device, batch_id, "pending-1", "a.pdf", 100)

    blocked = api_client.post(f"/api/v1/batches/{batch_id}/seal", headers=_headers(device))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "BATCH_HAS_PENDING_UPLOADS"
    assert blocked.json()["error"]["details"]["pending_uploads"] == 1


def test_sealed_batch_rejects_new_items_and_is_idempotent(api_client):
    device = "dev_seal_empty"
    batch = _create_batch(api_client, device, "key-seal-empty").json()
    batch_id = batch["batch_id"]

    first = api_client.post(f"/api/v1/batches/{batch_id}/seal", headers=_headers(device))
    assert first.status_code == 200, first.text
    # An empty sealed batch is complete with zero items.
    assert first.json()["status"] == "completed"

    again = api_client.post(f"/api/v1/batches/{batch_id}/seal", headers=_headers(device))
    assert again.status_code == 200
    assert again.json()["sealed_at"] == first.json()["sealed_at"]

    rejected = _register(api_client, device, batch_id, "late-1", "b.pdf", 10)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "BATCH_NOT_OPEN"


# --------------------------------------------------------------------------- #
# Resumable upload
# --------------------------------------------------------------------------- #


def test_document_uploaded_in_parts_creates_one_conversion(api_client, pdf_paths):
    device = "dev_parts"
    batch = _create_batch(api_client, device, "key-parts").json()
    batch_id = batch["batch_id"]
    path = pdf_paths["mixed_report"]

    completed, upload_id, digest, parts = _upload_document(
        api_client, device, batch_id, "doc-1", path
    )
    assert parts > 1, "the fixture must span several parts to exercise assembly"
    assert completed["conversion_id"].startswith("conv_")

    item = _wait_item(api_client, device, batch_id, completed["item_id"])
    assert item["conversion_id"] == completed["conversion_id"]
    assert item["status"] in {"completed", "completed_with_warnings"}

    # Replaying complete returns the same conversion instead of a second one.
    replay = _complete(api_client, device, upload_id, digest, parts)
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert replay.json()["conversion_id"] == completed["conversion_id"]

    listing = api_client.get(f"/api/v1/batches/{batch_id}/items", headers=_headers(device)).json()
    assert len(listing["items"]) == 1
    assert listing["items"][0]["conversion_id"] == completed["conversion_id"]


def test_identical_part_retransmission_is_a_no_op(api_client, pdf_paths):
    device = "dev_part_replay"
    batch = _create_batch(api_client, device, "key-part-replay").json()
    payload = pdf_paths["mixed_report"].read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", len(payload)).json()
    upload_id = registered["upload_id"]
    part_size = registered["part_size"]
    total_parts = (len(payload) + part_size - 1) // part_size

    chunk = payload[:part_size]
    first = _put_part(api_client, device, upload_id, 0, chunk)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    second = _put_part(api_client, device, upload_id, 0, chunk)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["received_bytes"] == first.json()["received_bytes"]
    assert second.json()["parts_received"] == 1

    # A divergent body for a confirmed part is a conflict, not an overwrite.
    divergent = _put_part(api_client, device, upload_id, 0, b"different bytes")
    assert divergent.status_code == 409
    assert divergent.json()["error"]["code"] == "UPLOAD_PART_MISMATCH"

    for index in range(1, total_parts):
        piece = payload[index * part_size : (index + 1) * part_size]
        assert _put_part(api_client, device, upload_id, index, piece).status_code == 200
    assert _complete(api_client, device, upload_id, digest, total_parts).status_code == 202


def test_part_hash_mismatch_is_rejected(api_client):
    device = "dev_part_hash"
    batch = _create_batch(api_client, device, "key-part-hash").json()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", 10).json()
    upload_id = registered["upload_id"]

    response = _put_part(api_client, device, upload_id, 0, b"0123456789", sha="0" * 64)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UPLOAD_CHECKSUM_MISMATCH"

    # Nothing was confirmed, so the session is still resumable.
    session = api_client.get(f"/api/v1/uploads/{upload_id}", headers=_headers(device)).json()
    assert session["parts_received"] == 0


def test_complete_with_a_missing_part_fails(api_client, pdf_paths):
    device = "dev_missing_part"
    batch = _create_batch(api_client, device, "key-missing").json()
    payload = pdf_paths["mixed_report"].read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", len(payload)).json()
    upload_id = registered["upload_id"]

    part_size = registered["part_size"]
    total_parts = (len(payload) + part_size - 1) // part_size
    assert _put_part(api_client, device, upload_id, 0, payload[:part_size]).status_code == 200

    response = _complete(api_client, device, upload_id, digest, total_parts)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UPLOAD_INCOMPLETE"
    assert response.json()["error"]["details"]["missing_parts"]


def test_complete_with_a_wrong_document_hash_fails(api_client, pdf_paths):
    device = "dev_doc_hash"
    batch = _create_batch(api_client, device, "key-doc-hash").json()
    payload = pdf_paths["mixed_report"].read_bytes()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", len(payload)).json()
    upload_id = registered["upload_id"]

    part_size = registered["part_size"]
    total_parts = (len(payload) + part_size - 1) // part_size
    for index in range(total_parts):
        piece = payload[index * part_size : (index + 1) * part_size]
        assert _put_part(api_client, device, upload_id, index, piece).status_code == 200

    response = _complete(api_client, device, upload_id, "1" * 64, total_parts)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UPLOAD_CHECKSUM_MISMATCH"

    # No conversion was created for a corrupt assembly.
    listing = api_client.get(f"/api/v1/batches/{batch['batch_id']}/items", headers=_headers(device)).json()
    assert listing["items"][0]["conversion_id"] is None


def test_upload_belongs_to_its_owner(api_client):
    device = "dev_upload_owner"
    batch = _create_batch(api_client, device, "key-upload-owner").json()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", 10).json()
    upload_id = registered["upload_id"]

    foreign = api_client.get(f"/api/v1/uploads/{upload_id}", headers=_headers("dev_intruder"))
    assert foreign.status_code == 403

    unknown = api_client.get("/api/v1/uploads/upl_does_not_exist", headers=_headers(device))
    assert unknown.status_code == 404


def test_image_item_only_accepts_txt(api_client):
    device = "dev_image_item"
    batch = _create_batch(api_client, device, "key-image").json()
    rejected = _register(
        api_client,
        device,
        batch["batch_id"],
        "img-1",
        "scan.png",
        100,
        options={"target_formats": ["xlsx"]},
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "UNSUPPORTED_TARGET"

    accepted = _register(
        api_client, device, batch["batch_id"], "img-2", "scan.png", 100
    )
    assert accepted.status_code == 201
    assert accepted.json()["item"]["available_formats"] == ["txt"]


def test_abort_removes_parts_and_marks_the_item(api_client, pdf_paths):
    device = "dev_abort"
    batch = _create_batch(api_client, device, "key-abort").json()
    payload = pdf_paths["mixed_report"].read_bytes()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", len(payload)).json()
    upload_id = registered["upload_id"]
    part_size = registered["part_size"]
    assert _put_part(api_client, device, upload_id, 0, payload[:part_size]).status_code == 200

    aborted = api_client.delete(f"/api/v1/uploads/{upload_id}", headers=_headers(device))
    assert aborted.status_code == 200
    assert aborted.json()["state"] == "aborted"

    item = api_client.get(
        f"/api/v1/batches/{batch['batch_id']}/items", headers=_headers(device)
    ).json()["items"][0]
    assert item["status"] == "upload_failed"
    # The failed upload no longer blocks sealing.
    sealed = api_client.post(f"/api/v1/batches/{batch['batch_id']}/seal", headers=_headers(device))
    assert sealed.status_code == 200


def test_batch_cancel_marks_items_and_blocks_inclusions(api_client):
    device = "dev_batch_cancel"
    batch = _create_batch(api_client, device, "key-cancel").json()
    batch_id = batch["batch_id"]
    _register(api_client, device, batch_id, "doc-1", "a.pdf", 100)

    cancelled = api_client.post(f"/api/v1/batches/{batch_id}/cancel", headers=_headers(device))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    rejected = _register(api_client, device, batch_id, "doc-2", "b.pdf", 100)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "BATCH_CANCELLED"


def test_complete_after_a_lost_response_does_not_create_a_second_conversion(api_client, pdf_paths):
    """A crash between "conversion created" and "session completed" must not duplicate."""
    from models.db import PublicUploadSessionRow, get_session_factory

    device = "dev_complete_crash"
    batch = _create_batch(api_client, device, "key-complete-crash").json()
    payload = pdf_paths["mixed_report"].read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    registered = _register(api_client, device, batch["batch_id"], "doc-1", "a.pdf", len(payload)).json()
    upload_id = registered["upload_id"]
    part_size = registered["part_size"]
    total_parts = (len(payload) + part_size - 1) // part_size

    for index in range(total_parts):
        piece = payload[index * part_size : (index + 1) * part_size]
        assert _put_part(api_client, device, upload_id, index, piece).status_code == 200

    first = _complete(api_client, device, upload_id, digest, total_parts)
    assert first.status_code == 202, first.text

    # Simulate the crash: the conversion exists but the session was never
    # marked completed.
    session = get_session_factory()()
    try:
        row = session.get(PublicUploadSessionRow, upload_id)
        row.state = "uploading"
        session.commit()
    finally:
        session.close()

    replay = _complete(api_client, device, upload_id, digest, total_parts)
    assert replay.status_code == 202
    assert replay.json()["replayed"] is True
    assert replay.json()["conversion_id"] == first.json()["conversion_id"]

    listing = api_client.get(
        f"/api/v1/batches/{batch['batch_id']}/items", headers=_headers(device)
    ).json()
    assert len(listing["items"]) == 1
    assert listing["items"][0]["conversion_id"] == first.json()["conversion_id"]
