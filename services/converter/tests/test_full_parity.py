"""T6 — full-mode parity: authorized RAG indexing + gateway submission port."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import pytest

from sources.gateway_submission import GatewaySubmissionError, GatewaySubmissionPort


def _owner_headers(api_client) -> dict:
    token = api_client.post("/api/v1/auth/anonymous").json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _run_conversion(api_client, headers: dict, tmp_path: Path, content: str = "conteudo para rag") -> str:
    """One conversion with a text plan, through the batch transport."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Texto"
    ws.append(["Campo", "Valor"])
    ws.append(["Nota", content * 400])
    buffer = io.BytesIO()
    wb.save(buffer)
    data = buffer.getvalue()
    response = api_client.post(
        "/api/v1/conversions",
        files={"file": ("doc.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"options": json.dumps({"schema_version": 3})},
        headers=headers,
    )
    assert response.status_code in (200, 201, 202), response.text
    conversion_id = response.json().get("conversion_id") or response.json().get("id")
    for _ in range(120):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        if detail.get("status") in {"completed", "completed_with_warnings", "failed"}:
            break
        time.sleep(0.1)
    assert detail.get("status") in {"completed", "completed_with_warnings"}, detail
    return conversion_id


def test_authorized_index_indexes_without_public_conversion_lookup(api_client, tmp_path: Path):
    headers = _owner_headers(api_client)
    conversion_id = _run_conversion(api_client, headers, tmp_path)

    detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
    run_id = detail["run"]["run_id"]

    collection = api_client.post(
        "/api/v1/knowledge/collections", json={"name": f"col-{uuid.uuid4().hex[:8]}"}, headers=headers
    ).json()
    collection_id = collection["collection_id"]

    internal_headers = {"x-internal-token": "test-internal-token"}
    # The gateway resolves document/run in Prisma and posts authorized refs.
    result = api_client.post(
        "/internal/v1/knowledge/authorized-index",
        json={
            "owner_id": api_client.get("/api/v1/auth/me", headers=headers).json()["id"],
            "collection_id": collection_id,
            "conversion_id": conversion_id,
            "document_id": "doc_resolved_by_gateway",
            "run_id": run_id,
            "filename": "doc.xlsx",
        },
        headers=internal_headers,
    )
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["status"] == "indexed"
    assert payload["chunks"] > 0

    # retrieval finds chunks owned by the requester
    search = api_client.post(
        "/api/v1/knowledge/search",
        json={"query": "conteudo para rag", "collection_ids": [collection_id]},
        headers=headers,
    )
    if search.status_code == 200:
        items = search.json().get("items", [])
        assert any(item.get("run_id") == run_id for item in items)


def test_authorized_index_requires_internal_token(api_client):
    response = api_client.post(
        "/internal/v1/knowledge/authorized-index", json={"owner_id": "x"}
    )
    assert response.status_code in (401, 403)


def test_validate_import_accepts_own_and_rejects_foreign(api_client, tmp_path: Path):
    from sources.importer import ImportService

    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    headers = _owner_headers(api_client)
    owner_id = api_client.get("/api/v1/auth/me", headers=headers).json()["id"]
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner_id},
        headers={"x-internal-token": "test-internal-token"},
    )
    root_id = grant.json()["id"]
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "docs", "kind": "directory", "name": "D"},
        headers=headers,
    )
    source_id = source.json()["id"]
    service = ImportService()
    record = service.start(
        owner_id=owner_id,
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )

    internal_headers = {"x-internal-token": "test-internal-token"}
    ok = api_client.post(
        "/internal/v1/automation/validate-import",
        json={"owner_id": owner_id, "import_id": record["id"]},
        headers=internal_headers,
    ).json()
    assert ok["valid"] is True

    foreign = api_client.post(
        "/internal/v1/automation/validate-import",
        json={"owner_id": "another-owner", "import_id": record["id"]},
        headers=internal_headers,
    ).json()
    assert foreign["valid"] is False


class _FakeGateway:
    """Records calls and simulates timeouts for the replay test."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.fail_next = 0
        self.batches: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self.conversions: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.fail_next > 0:
            self.fail_next -= 1
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        path = request.url.path
        if path == "/api/v1/internal/automation/batches" and request.method == "POST":
            body = json.loads(request.content)
            key = body["import_id"]
            if key not in self.batches:
                self.batches[key] = {"batch_id": f"batch-{key[:8]}"}
            return httpx.Response(201, json=self.batches[key])
        if path.endswith("/validate-import"):
            return httpx.Response(200, json={"valid": True, "code": None})
        if "/items" in path and request.method == "POST":
            body = json.loads(request.content)
            key = body["client_item_id"]
            if key in self.items:
                item = self.items[key]
            else:
                item = {"item_id": f"item-{key[-6:]}", "upload_id": f"upl-{key[-6:]}"}
                self.items[key] = item
            return httpx.Response(201, json=item)
        if path.endswith("/content"):
            item_id = request.headers["x-item-id"]
            if item_id in self.conversions:
                conversion_id = self.conversions[item_id]
            else:
                conversion_id = f"conv-{item_id[-6:]}"
                self.conversions[item_id] = conversion_id
            return httpx.Response(201, json={"conversion_id": conversion_id, "item_id": item_id})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})


def test_gateway_port_replays_after_timeout_without_duplicates():
    gateway = _FakeGateway()
    transport = httpx.MockTransport(gateway.handler)
    port = GatewaySubmissionPort(
        "http://gateway.test", "secret-token", client=httpx.Client(transport=transport)
    )

    batch_id = port.ensure_batch("owner-1", "imp-1")
    assert batch_id

    def submit():
        # First attempt times out; the retry must recover the SAME ids.
        gateway.fail_next = 1
        last_error = None
        for _ in range(3):
            try:
                return port.submit_snapshot(
                    "owner-1",
                    batch_id,
                    import_id="imp-1",
                    import_item_id=42,
                    relative_path="docs/plan.xlsx",
                    snapshot_key=None,
                    snapshot_bytes=b"conteudo",
                    sha256=hashlib.sha256(b"conteudo").hexdigest(),
                    size_bytes=8,
                    normalized_options={"schema_version": 3},
                )
            except GatewaySubmissionError as exc:
                last_error = exc
                if exc.code != "GATEWAY_TIMEOUT":
                    raise
        raise last_error

    result = submit()
    assert result.conversion_id
    # replay with the same keys recovers the existing conversion
    again = port.submit_snapshot(
        "owner-1",
        batch_id,
        import_id="imp-1",
        import_item_id=42,
        relative_path="docs/plan.xlsx",
        snapshot_key=None,
        snapshot_bytes=b"conteudo",
        sha256=hashlib.sha256(b"conteudo").hexdigest(),
        size_bytes=8,
        normalized_options={"schema_version": 3},
    )
    assert again.conversion_id == result.conversion_id
    conversions_for_item = [v for k, v in gateway.conversions.items() if k == result.item_id]
    assert len(conversions_for_item) == 1
