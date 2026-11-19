"""Full-mode submission adapter (T6, doc 02 §7).

``GatewaySubmissionPort`` implements the same contract as the standalone
port, but every effect goes through the gateway's internal automation API:
batches/items live in Prisma there, and the gateway validates the import
(and its schedule-run linkage) before accepting a submission.

Timeout safety: every call carries the same stable client keys, so a lost
response is recovered by replaying the identical request — at-least-once
delivery with idempotent creation, never a duplicate conversion.
"""

from __future__ import annotations

from typing import Any

import httpx

from sources.submission import SubmissionResult

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class GatewaySubmissionError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class GatewaySubmissionPort:
    def __init__(
        self,
        base_url: str,
        internal_token: str,
        *,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = internal_token
        self._client = client
        self._timeout = timeout or DEFAULT_TIMEOUT

    def _request(self, method: str, path: str, *, json_body: Any = None, content: bytes | None = None,
                 headers: dict[str, str] | None = None, expect: tuple[int, ...] = (200, 201, 202)) -> Any:
        url = f"{self._base}{path}"
        all_headers = {"x-internal-token": self._token, **(headers or {})}
        try:
            if self._client is not None:
                response = self._client.request(
                    method, url, json=json_body, content=content, headers=all_headers,
                    timeout=self._timeout,
                )
            else:
                with httpx.Client(timeout=self._timeout) as session:
                    response = session.request(
                        method, url, json=json_body, content=content, headers=all_headers
                    )
        except httpx.TimeoutException as exc:
            # The caller replays with the same keys: creation is idempotent.
            raise GatewaySubmissionError(
                "GATEWAY_TIMEOUT",
                "O gateway não respondeu em tempo; a submissão será reenviada com as mesmas chaves.",
                {"path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewaySubmissionError(
                "GATEWAY_UNAVAILABLE", f"Gateway inacessível: {exc}", {"path": path}
            ) from exc
        if response.status_code not in expect:
            try:
                detail = response.json()
            except Exception:
                detail = {}
            envelope = detail.get("error", {}) if isinstance(detail, dict) else {}
            raise GatewaySubmissionError(
                envelope.get("code", "GATEWAY_ERROR"),
                envelope.get("message", f"Gateway respondeu {response.status_code}."),
                {"path": path, "status": response.status_code},
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    # ------------------------------------------------------------- contract
    def ensure_batch(self, owner_id: str, import_id: str) -> str:
        payload = self._request(
            "POST",
            "/api/v1/internal/automation/batches",
            json_body={"owner_id": owner_id, "import_id": import_id},
        )
        return str(payload["batch_id"])

    def submit_snapshot(
        self,
        owner_id: str,
        batch_id: str,
        *,
        import_id: str,
        import_item_id: int,
        relative_path: str,
        snapshot_key: str | None,
        snapshot_bytes: bytes | None,
        sha256: str,
        size_bytes: int,
        normalized_options: dict[str, Any],
    ) -> SubmissionResult:
        client_item_id = f"srcimp:{import_id}:{import_item_id}"
        filename = relative_path.rsplit("/", 1)[-1]
        # The import row is validated by the gateway before admission; a
        # schedule occurrence must also be linked when present.
        validation = self._request(
            "POST",
            "/api/v1/internal/automation/validate-import",
            json_body={"owner_id": owner_id, "import_id": import_id},
        )
        if not validation.get("valid"):
            raise GatewaySubmissionError(
                validation.get("code", "IMPORT_INVALID"),
                "O gateway recusou a importação.",
                {"import_id": import_id},
            )
        # Register is idempotent per (batch, client_item_id); replay with the
        # same key returns the existing item/conversion.
        response = self._request(
            "POST",
            f"/api/v1/internal/automation/batches/{batch_id}/items",
            json_body={
                "owner_id": owner_id,
                "client_item_id": client_item_id,
                "filename": filename,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "options": normalized_options,
                "import_id": import_id,
            },
        )
        if response.get("conversion_id"):
            return SubmissionResult(
                item_id=str(response["item_id"]),
                upload_id=str(response.get("upload_id") or ""),
                conversion_id=str(response["conversion_id"]),
            )
        # Content travels raw with the identity headers; the gateway stores it
        # and creates the conversion under the same idempotency key.
        created = self._request(
            "PUT",
            f"/api/v1/internal/automation/batches/{batch_id}/items/{client_item_id}/content",
            content=snapshot_bytes or b"",
            headers={
                "x-owner-id": owner_id,
                "x-content-sha256": sha256,
                "x-import-id": import_id,
                "x-item-id": str(response.get("item_id") or client_item_id),
                # The real name carries the extension/MIME the gateway must use
                # (the opaque client id cannot tell CSV from XLSX).
                "x-filename": filename,
                "Content-Type": "application/octet-stream",
            },
        )
        return SubmissionResult(
            item_id=str(created.get("item_id") or response.get("item_id") or client_item_id),
            upload_id=str(created.get("upload_id") or ""),
            conversion_id=str(created["conversion_id"]),
        )

    def seal_batch(self, owner_id: str, batch_id: str) -> None:
        try:
            self._request(
                "POST",
                f"/api/v1/internal/automation/batches/{batch_id}/seal",
                json_body={"owner_id": owner_id},
            )
        except GatewaySubmissionError:
            pass  # sealing is a UI affordance; the import record stays authoritative

    def reconcile(self, owner_id: str, batch_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/internal/automation/batches/{batch_id}/reconcile",
            json_body={"owner_id": owner_id},
        )

    def cancel(self, owner_id: str, batch_id: str) -> None:
        self._request(
            "POST",
            f"/api/v1/internal/automation/batches/{batch_id}/cancel",
            json_body={"owner_id": owner_id},
        )


__all__ = ["GatewaySubmissionError", "GatewaySubmissionPort"]
