"""HTTP contract for the upgrade endpoints (capabilities, artifacts, text)."""

from __future__ import annotations

import time

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}


def _upload(client, path, device: str = "dev_upgrade"):
    with open(path, "rb") as handle:
        return client.post(
            "/api/v1/conversions",
            headers={"x-device-id": device},
            files={"file": (path.name, handle, "application/pdf")},
        )


def _wait(client, conversion_id: str, device: str = "dev_upgrade", timeout: float = 90.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(
            f"/api/v1/conversions/{conversion_id}", headers={"x-device-id": device}
        ).json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.15)
    raise AssertionError(f"timeout: {last}")


def test_capabilities_declare_formats_and_codecs(api_client):
    payload = api_client.get("/api/v1/capabilities").json()
    formats = {item["format"] for item in payload["outputs"]}
    assert {"xlsx", "txt", "csv"} <= formats
    assert payload["inputs"]
    assert "limits" in payload and payload["limits"]["max_bytes"] > 0


def test_exports_and_text_share_the_same_run(api_client, pdf_paths):
    created = _upload(api_client, pdf_paths["mixed_report"])
    assert created.status_code == 202, created.text
    conversion_id = created.json()["conversion_id"]
    final = _wait(api_client, conversion_id)
    assert final["status"] in {"completed", "completed_with_warnings"}

    exports = api_client.get(
        f"/api/v1/conversions/{conversion_id}/exports", headers={"x-device-id": "dev_upgrade"}
    )
    assert exports.status_code == 200
    items = exports.json()["items"]
    formats = {item["format"] for item in items}
    assert "txt" in formats and "csv" in formats

    txt = next(item for item in items if item["format"] == "txt")
    download = api_client.get(
        f"/api/v1/conversions/{conversion_id}/exports/{txt['export_id']}/download",
        headers={"x-device-id": "dev_upgrade"},
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/plain")
    assert "João" in download.content.decode("utf-8")

    text = api_client.get(
        f"/api/v1/conversions/{conversion_id}/text", headers={"x-device-id": "dev_upgrade"}
    )
    assert text.status_code == 200
    assert "João" in text.json()["text"]


def test_render_alternative_txt_without_reprocessing(api_client, pdf_paths):
    created = _upload(api_client, pdf_paths["mixed_report"], device="dev_export")
    conversion_id = created.json()["conversion_id"]
    _wait(api_client, conversion_id, device="dev_export")

    response = api_client.post(
        f"/api/v1/conversions/{conversion_id}/exports",
        headers={"x-device-id": "dev_export"},
        json={"format": "txt", "options": {"newline": "crlf", "page_separator": "marker"}},
    )
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["options"]["newline"] == "crlf"
    download = api_client.get(
        f"/api/v1/conversions/{conversion_id}/exports/{artifact['export_id']}/download",
        headers={"x-device-id": "dev_export"},
    )
    content = download.content.decode("utf-8")
    assert "\r\n" in content
    assert "--- Página 1 ---" in content


def test_invalid_export_target_is_rejected(api_client, pdf_paths):
    created = _upload(api_client, pdf_paths["mixed_report"], device="dev_invalid")
    conversion_id = created.json()["conversion_id"]
    _wait(api_client, conversion_id, device="dev_invalid")
    response = api_client.post(
        f"/api/v1/conversions/{conversion_id}/exports",
        headers={"x-device-id": "dev_invalid"},
        json={"format": "pdf", "options": {}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"UNSUPPORTED_FORMAT", "INVALID_OPTION"}


def test_models_status_never_exposes_secrets(api_client):
    status = api_client.get("/api/v1/models/status").json()
    assert "enabled" in status and "available" in status
    assert "api_key" not in str(status).lower()


def test_internal_parity_routes_require_token(api_client):
    assert api_client.get("/internal/v1/capabilities").status_code == 401
    headers = {"x-internal-token": "test-internal-token"}
    capabilities = api_client.get("/internal/v1/capabilities", headers=headers)
    assert capabilities.status_code == 200
    assert any(item["format"] == "txt" for item in capabilities.json()["outputs"])

    status = api_client.get("/internal/v1/models/status", headers=headers)
    assert status.status_code == 200
    assert "enabled" in status.json()

    owner_headers = {**headers, "x-owner-id": "internal_owner"}
    collections = api_client.get("/internal/v1/knowledge/collections", headers=owner_headers)
    assert collections.status_code == 200
    assert collections.json()["total"] >= 0
