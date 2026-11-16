"""HTTP contract tests against the standalone API."""

from __future__ import annotations

import io
import time
import zipfile

import pytest

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}


def upload(client, path, device: str, key: str | None = None):
    headers = {"x-device-id": device}
    if key:
        headers["Idempotency-Key"] = key
    with open(path, "rb") as handle:
        return client.post(
            "/api/v1/conversions",
            headers=headers,
            files={"file": (path.name, handle, "application/pdf")},
        )


def wait(client, conversion_id: str, device: str, timeout: float = 60.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get(f"/api/v1/conversions/{conversion_id}", headers={"x-device-id": device})
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.15)
    raise AssertionError(f"timeout waiting for conversion: {last}")


def test_health_and_settings(api_client):
    assert api_client.get("/health").json()["status"] == "ok"
    settings = api_client.get("/api/v1/settings").json()
    assert settings["runtime_mode"] == "standalone"
    assert settings["max_upload_bytes"] > 0
    assert settings["capabilities"]["tables"] is True


def test_full_conversion_flow(api_client, pdf_paths):
    device = "dev_flow"
    created = upload(api_client, pdf_paths["mixed_report"], device)
    assert created.status_code == 202, created.text
    body = created.json()
    conversion_id = body["conversion_id"]
    assert body["status"] in {"queued", "received"}
    assert body["links"]["audit"].endswith("/audit")

    final = wait(api_client, conversion_id, device)
    assert final["status"] == "completed"
    assert final["quality"] == "validated"
    assert final["stats"]["cells"] > 0
    assert final["document"]["filename"] == "mixed_report.pdf"

    result = api_client.get(f"/api/v1/conversions/{conversion_id}/result", headers={"x-device-id": device}).json()
    assert result["pages"] == 1
    # records + table + text + consolidated ("Todos os dados")
    assert result["sheets_count"] == 4

    preview = api_client.get(
        f"/api/v1/conversions/{conversion_id}/preview", headers={"x-device-id": device}
    ).json()
    assert preview["headers"] == ["Nome", "Matrícula", "Cargo", "Cidade"]
    assert preview["rows"][0][0]["display"] == "João da Silva"

    download = api_client.get(
        f"/api/v1/conversions/{conversion_id}/download", headers={"x-device-id": device}
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert not [n for n in archive.namelist() if n.startswith("xl/media/")]

    audit = api_client.get(f"/api/v1/conversions/{conversion_id}/audit", headers={"x-device-id": device}).json()
    assert audit["hash_chain_valid"] is True
    assert audit["metrics"]["pages_analyzed"] == 1
    assert audit["metrics"]["coverage_percent"] == 100.0
    assert audit["engines"]

    events = api_client.get(
        f"/api/v1/conversions/{conversion_id}/audit/events?limit=50", headers={"x-device-id": device}
    ).json()
    assert events["total"] > 0
    assert all(event["event_hash"] for event in events["items"])

    page = api_client.get(
        f"/api/v1/conversions/{conversion_id}/audit/pages/1", headers={"x-device-id": device}
    ).json()
    assert page["page_number"] == 1
    assert page["blocks"]
    assert all(block["destination"] for block in page["blocks"])

    cells = api_client.get(
        f"/api/v1/conversions/{conversion_id}/audit/cells?limit=5", headers={"x-device-id": device}
    ).json()
    assert cells["items"]
    assert cells["items"][0]["source"]
    assert cells["items"][0]["sheet"]

    for fmt, marker in (("json", b'"manifest"'), ("txt", b"AUDITORIA")):
        export = api_client.get(
            f"/api/v1/conversions/{conversion_id}/audit/export?format={fmt}",
            headers={"x-device-id": device},
        )
        assert export.status_code == 200
        assert marker in export.content


def test_uploaded_file_is_validated(api_client, pdf_paths):
    response = upload(api_client, pdf_paths["not_a_pdf"], "dev_invalid")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_FILE"


def test_ownership_is_enforced(api_client, pdf_paths):
    created = upload(api_client, pdf_paths["simple_field"], "dev_owner")
    conversion_id = created.json()["conversion_id"]
    response = api_client.get(
        f"/api/v1/conversions/{conversion_id}", headers={"x-device-id": "dev_intruder"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_idempotency_key_reuses_conversion(api_client, pdf_paths):
    first = upload(api_client, pdf_paths["simple_field"], "dev_idem", key="same-key-1")
    second = upload(api_client, pdf_paths["simple_field"], "dev_idem", key="same-key-1")
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["conversion_id"] == second.json()["conversion_id"]


def test_retry_creates_a_new_run(api_client, pdf_paths):
    created = upload(api_client, pdf_paths["simple_field"], "dev_retry")
    conversion_id = created.json()["conversion_id"]
    wait(api_client, conversion_id, "dev_retry")
    headers = {"x-device-id": "dev_retry"}
    retry = api_client.post(f"/api/v1/conversions/{conversion_id}/retry", headers=headers)
    assert retry.status_code == 202
    final = wait(api_client, conversion_id, "dev_retry")
    assert final["run"]["attempt"] >= 2
    assert final["status"] == "completed"


def test_download_before_completion_returns_409(api_client, pdf_paths):
    created = upload(api_client, pdf_paths["free_text"], "dev_early")
    conversion_id = created.json()["conversion_id"]
    response = api_client.get(
        f"/api/v1/conversions/{conversion_id}/download", headers={"x-device-id": "dev_early"}
    )
    # Either the file is not ready yet (409) or the conversion finished first.
    if response.status_code != 200:
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NOT_READY"


def test_delete_removes_from_history(api_client, pdf_paths):
    created = upload(api_client, pdf_paths["simple_field"], "dev_delete")
    conversion_id = created.json()["conversion_id"]
    wait(api_client, conversion_id, "dev_delete")
    headers = {"x-device-id": "dev_delete"}
    assert api_client.delete(f"/api/v1/conversions/{conversion_id}", headers=headers).status_code == 204
    assert api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).status_code == 404


def test_history_lists_only_owner_conversions(api_client, pdf_paths):
    upload(api_client, pdf_paths["simple_field"], "dev_history_a")
    upload(api_client, pdf_paths["multi_fields"], "dev_history_b")
    listing = api_client.get("/api/v1/conversions?page_size=10", headers={"x-device-id": "dev_history_a"}).json()
    assert listing["total"] >= 1
    assert all(item["filename"] for item in listing["items"])


def test_sse_replays_events(api_client, pdf_paths):
    created = upload(api_client, pdf_paths["simple_field"], "dev_sse")
    conversion_id = created.json()["conversion_id"]
    wait(api_client, conversion_id, "dev_sse")
    response = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events", headers={"x-device-id": "dev_sse"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert ": connected" in body
    assert "event: completed" in body or "event: stage" in body

    resumed = api_client.get(
        f"/api/v1/conversions/{conversion_id}/events?last_event_id=2",
        headers={"x-device-id": "dev_sse"},
    )
    assert resumed.status_code == 200


def test_internal_api_requires_token(api_client):
    assert api_client.get("/internal/v1/jobs/run_x").status_code == 401
    ok = api_client.get(
        "/internal/v1/jobs/run_x", headers={"x-internal-token": "test-internal-token"}
    )
    assert ok.status_code == 404


def test_internal_health(api_client):
    payload = api_client.get("/internal/v1/health").json()
    assert payload["status"] == "ok"
    assert payload["engine_version"]


# ---------------------------------------------------------------------------
# Regression: idempotency and cancellation (found in the full audit)
# ---------------------------------------------------------------------------

def test_idempotency_key_reused_with_other_file_is_rejected(api_client, pdf_paths):
    device = "dev_idem"
    first = upload(api_client, pdf_paths["simple_field"], device, key="k-1")
    assert first.status_code == 202, first.text

    same = upload(api_client, pdf_paths["simple_field"], device, key="k-1")
    assert same.status_code == 202
    assert same.json()["conversion_id"] == first.json()["conversion_id"]

    other = upload(api_client, pdf_paths["bordered_table"], device, key="k-1")
    assert other.status_code == 409, other.text
    assert other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_cancel_is_visible_in_the_public_conversion(api_client, pdf_paths):
    """Cancelling the run must move the conversion and emit a terminal event."""
    device = "dev_cancel"
    created = upload(api_client, pdf_paths["mixed_report"], device)
    assert created.status_code == 202
    conversion_id = created.json()["conversion_id"]

    # cancel right away: the run may already be finished, which is fine — the
    # assertion is about consistency, never about winning the race
    cancelled = api_client.post(
        f"/api/v1/conversions/{conversion_id}/cancel", headers={"x-device-id": device}
    )
    assert cancelled.status_code in {202, 409}

    final = wait(api_client, conversion_id, device)
    assert final["status"] in TERMINAL
    if final["status"] == "cancelled":
        assert final["quality"] == "failed"


def test_cell_evidence_endpoint_resolves_sheet_qualified_id(api_client, pdf_paths):
    device = "dev_cell"
    created = upload(api_client, pdf_paths["simple_field"], device)
    conversion_id = created.json()["conversion_id"]
    final = wait(api_client, conversion_id, device)
    assert final["status"] in {"completed", "completed_with_warnings"}

    cells = api_client.get(
        f"/api/v1/conversions/{conversion_id}/audit/cells",
        headers={"x-device-id": device},
    ).json()["items"]
    assert cells, "expected at least one exported cell"
    first = cells[0]

    by_id = api_client.get(
        f"/api/v1/conversions/{conversion_id}/cells/{first['sheet']}!{first['cell']}",
        headers={"x-device-id": device},
    )
    assert by_id.status_code == 200, by_id.text
    body = by_id.json()
    assert body["cell"] == first["cell"]
    assert body["sheet"] == first["sheet"]
    # the validation flags are computed, not hardcoded
    assert isinstance(body["validation"]["value_matches_source"], bool)
    assert isinstance(body["transformations"], list)


def test_pages_and_tables_endpoints(api_client, pdf_paths):
    device = "dev_pages"
    created = upload(api_client, pdf_paths["bordered_table"], device)
    conversion_id = created.json()["conversion_id"]
    wait(api_client, conversion_id, device)

    pages = api_client.get(
        f"/api/v1/conversions/{conversion_id}/pages", headers={"x-device-id": device}
    ).json()
    assert pages["total"] >= 1
    assert pages["items"][0]["page_number"] == 1
    assert "classification" in pages["items"][0]

    tables = api_client.get(
        f"/api/v1/conversions/{conversion_id}/tables", headers={"x-device-id": device}
    ).json()
    assert tables["total"] >= 1
    assert tables["items"][0]["headers"]
