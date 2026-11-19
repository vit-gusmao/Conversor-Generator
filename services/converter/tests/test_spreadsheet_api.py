"""T3 — spreadsheet admission through the HTTP API (single-shot + multipart).

The reader/pipeline unit tests prove parsing; these tests prove the API
surface: probe by content, kind-specific option normalization, part upload,
conversion result and exports that reference real sheets.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook




def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Faturas"
    ws.append(["Documento", "Valor"])
    ws.append(["00123456789", 1234.56])
    ws.append(["Fatura B", 99])
    wb.save_path = None
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _csv_bytes() -> bytes:
    return "Nome;Valor\nAna;\"linha um\ne dois\"\n".encode("utf-8-sig")


def _device_headers(api_client) -> dict:
    response = api_client.post("/api/v1/auth/anonymous")
    assert response.status_code in (200, 201), response.text
    payload = response.json()
    token = payload.get("access_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def test_single_shot_xlsx_conversion(api_client):
    headers = _device_headers(api_client)
    data = _xlsx_bytes()
    response = api_client.post(
        "/api/v1/conversions",
        files={"file": ("faturas.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"options": json.dumps({"schema_version": 3, "spreadsheet": {"sheet_names": ["Faturas"]}})},
        headers=headers,
    )
    assert response.status_code in (200, 201, 202), response.text
    payload = response.json()
    conversion_id = payload.get("conversion_id") or payload.get("id")
    assert conversion_id

    # wait for the inline queue to drain
    import time

    status = None
    detail = {}
    for _ in range(100):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        status = detail.get("status")
        if status in {"completed", "completed_with_warnings", "failed"}:
            break
        time.sleep(0.1)
    assert status in {"completed", "completed_with_warnings"}, detail

    overview = api_client.get(f"/api/v1/conversions/{conversion_id}/result", headers=headers).json()
    sheet_names = {sheet["name"] for sheet in overview.get("sheets", [])}
    assert "Faturas" in sheet_names

    downloads = api_client.get(f"/api/v1/conversions/{conversion_id}/exports", headers=headers).json()
    formats = {item.get("format") for item in downloads.get("items", downloads if isinstance(downloads, list) else [])} \
        if isinstance(downloads, (list, dict)) else set()
    assert formats & {"txt", "csv"}, formats


def test_single_shot_generic_zip_is_rejected(api_client):
    headers = _device_headers(api_client)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("nota.txt", "não é planilha")
    response = api_client.post(
        "/api/v1/conversions",
        files={"file": ("fake.xlsx", buffer.getvalue(), "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 400
    detail = response.json()
    assert detail["error"]["code"] in {"INVALID_FILE", "NOT_SPREADSHEET"}


def test_batch_multipart_csv_conversion(api_client):
    headers = _device_headers(api_client)
    key = uuid.uuid4().hex
    batch = api_client.post(
        "/api/v1/batches",
        json={"client_batch_key": key},
        headers=headers,
    )
    assert batch.status_code in (200, 201), batch.text
    batch_id = batch.json()["batch_id"]

    csv_data = _csv_bytes()
    register = api_client.post(
        f"/api/v1/batches/{batch_id}/items",
        json={
            "client_item_id": "item-csv-1",
            "filename": "dados.csv",
            "size_bytes": len(csv_data),
            "options": {
                "schema_version": 3,
                "spreadsheet": {"csv_encoding": "utf-8-sig", "csv_delimiter": ";"},
            },
        },
        headers=headers,
    )
    assert register.status_code in (200, 201), register.text
    item = register.json()
    upload_id = item["upload_id"]
    part_size = item["part_size"]

    # send the single part (raw streamed body, as the transport contract defines)
    part = api_client.put(
        f"/api/v1/uploads/{upload_id}/parts/0",
        content=csv_data,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert part.status_code in (200, 201), part.text

    complete = api_client.post(
        f"/api/v1/uploads/{upload_id}/complete",
        json={"sha256": __import__("hashlib").sha256(csv_data).hexdigest(), "total_parts": 1},
        headers=headers,
    )
    assert complete.status_code in (200, 201, 202), complete.text
    conversion_id = complete.json().get("conversion_id")
    assert conversion_id

    import time

    detail = {}
    for _ in range(100):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        if detail.get("status") in {"completed", "completed_with_warnings", "failed"}:
            break
        time.sleep(0.1)
    assert detail.get("status") in {"completed", "completed_with_warnings"}, detail
    assert detail["document"]["filename"].endswith(".csv")


def test_batch_item_unknown_extension_rejected(api_client):
    headers = _device_headers(api_client)
    batch = api_client.post(
        "/api/v1/batches",
        json={"client_batch_key": uuid.uuid4().hex},
        headers=headers,
    )
    batch_id = batch.json()["batch_id"]
    register = api_client.post(
        f"/api/v1/batches/{batch_id}/items",
        json={
            "client_item_id": "item-xls",
            "filename": "planilha.xls",
            "size_bytes": 10,
        },
        headers=headers,
    )
    assert register.status_code == 400
    assert register.json()["error"]["code"] in {"UNSUPPORTED_FORMAT", "INVALID_FILE", "UNSUPPORTED_KIND"}
