"""T5 — durable standalone import: idempotency, dedup, recovery, cancel."""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from pathlib import Path

import pytest
from openpyxl import Workbook

import sources.importer as importer_module
from automation.executor import _is_transient
from knowledge.repository import KnowledgeRepository
from models.db import get_session_factory
from sources.gateway_submission import GatewaySubmissionError
from sources.importer import ImportService
from sources.models import SourceKnowledgeHeadRow
from sources.service import SourceService
from sources.snapshot import SnapshotError
from sources.submission import StandaloneSubmissionPort

INTERNAL_TOKEN = "test-internal-token"


def _session(api_client) -> dict:
    """One owner session: {'id', 'headers'}."""
    response = api_client.post("/api/v1/auth/anonymous")
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = api_client.get("/api/v1/auth/me", headers=headers)
    return {"id": me.json()["id"], "headers": headers}


def _xlsx_bytes(content: str = "dados") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["A", "B"])
    ws.append([content, 1])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _make_source(api_client, tmp_path: Path) -> tuple[dict, str, Path]:
    """Grant a root and create a source for ONE owner session."""
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": owner["id"]},
        headers={"x-internal-token": INTERNAL_TOKEN},
    )
    assert grant.status_code == 201, grant.text
    root_id = grant.json()["id"]
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "docs", "kind": "directory", "name": "Docs"},
        headers=owner["headers"],
    )
    assert source.status_code == 201, source.text
    return owner, source.json()["id"], root


def _start_import(api_client, owner: dict, source_id: str, body: dict) -> object:
    return api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json=body,
        headers={**owner["headers"], "Idempotency-Key": uuid.uuid4().hex},
    )


def _wait_import(api_client, owner: dict, import_id: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        payload = api_client.get(
            f"/api/v1/imports/{import_id}", headers=owner["headers"]
        ).json()
        if payload.get("status") in {"completed", "partial", "failed", "cancelled"}:
            return payload
        time.sleep(0.2)
    return payload


def test_same_key_same_body_replays_same_import(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    key = uuid.uuid4().hex
    body = {"options": {"schema_version": 3}}

    first = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json=body,
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text
    second = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json=body,
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["replayed"] is True
    _wait_import(api_client, owner, first.json()["id"])


def test_same_key_different_body_conflicts(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    key = uuid.uuid4().hex
    first = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json={"options": {"schema_version": 3}},
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert first.status_code == 202
    _wait_import(api_client, owner, first.json()["id"])
    conflict = api_client.post(
        f"/api/v1/sources/{source_id}/imports",
        json={"options": {"schema_version": 3}, "force": True},
        headers={**owner["headers"], "Idempotency-Key": key},
    )
    assert conflict.status_code == 409


def test_import_converts_files_and_records_versions(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("primeira"))

    started = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    assert started.status_code == 202, started.text
    payload = _wait_import(api_client, owner, started.json()["id"])
    assert payload["status"] == "completed", payload
    assert payload["counts"]["imported"] == 1

    items = api_client.get(
        f"/api/v1/imports/{started.json()['id']}/items", headers=owner["headers"]
    ).json()["items"]
    assert items[0]["conversion_id"]

    # unchanged: skipped next time
    again = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    payload = _wait_import(api_client, owner, again.json()["id"])
    assert payload["counts"]["skipped"] == 1 and payload["counts"]["imported"] == 0, payload

    # changed content: imported again
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("segunda"))
    third = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    payload = _wait_import(api_client, owner, third.json()["id"])
    assert payload["counts"]["imported"] == 1, payload

    # force: reprocess unchanged
    force = _start_import(
        api_client, owner, source_id, {"options": {"schema_version": 3}, "force": True}
    )
    payload = _wait_import(api_client, owner, force.json()["id"])
    assert payload["counts"]["imported"] == 1 and payload["counts"]["skipped"] == 0, payload


def test_partial_failure_marks_import_partial(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "boa.xlsx").write_bytes(_xlsx_bytes("ok"))
    (root / "docs" / "quebrada.xlsx").write_bytes(b"isto nao e um xlsx")

    started = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    payload = _wait_import(api_client, owner, started.json()["id"])
    assert payload["status"] == "partial", payload
    assert payload["counts"]["imported"] == 1 and payload["counts"]["failed"] == 1


def test_foreign_owner_cannot_see_import(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    other = _session(api_client)
    started = _start_import(api_client, owner, source_id, {"options": {"schema_version": 3}})
    import_id = started.json()["id"]
    _wait_import(api_client, owner, import_id)
    got = api_client.get(f"/api/v1/imports/{import_id}", headers=other["headers"])
    assert got.status_code == 404


def test_recovery_after_submission_does_not_duplicate(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("recuperavel"))
    service = ImportService()

    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )
    import_id = record["id"]

    from models.db import get_session_factory

    sources = SourceService()
    session = get_session_factory()()
    try:
        source_row = sources.repository.get_source(
            session, source_id=source_id, owner_id=owner["id"]
        )
        root_row = sources.repository.get_root(
            session, root_id=source_row.root_id, owner_id=owner["id"]
        )
        definition = sources.definition_for(source_row, root_row)
    finally:
        session.close()

    normalized = service._normalize({"schema_version": 3}, "spreadsheet")
    options_hash = service._options_hash(normalized)
    # "crash" after scan+capture: run only that phase, then resume the run
    service._scan_and_capture(
        import_id, owner["id"], definition, normalized, options_hash, force=False
    )
    service.run(import_id, owner["id"])
    items = service.items(import_id, owner["id"])
    conversion_ids = [item["conversion_id"] for item in items if item["conversion_id"]]
    assert len(conversion_ids) == 1  # exactly one conversion despite the crash
    # a second full run keeps the history stable (completed import short-circuits)
    service.run(import_id, owner["id"])
    items = service.items(import_id, owner["id"])
    conversion_ids = [item["conversion_id"] for item in items if item["conversion_id"]]
    assert len(conversion_ids) == 1


def test_cancel_before_scan_ends_as_cancelled_not_failed(api_client, tmp_path: Path):
    """A cooperative cancel is a distinct terminal state (not a failure)."""
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    service = ImportService()
    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )
    service.cancel(record["id"], owner["id"])
    result = service.run(record["id"], owner["id"])
    assert result["status"] == "cancelled"
    assert result["error"] is None


# ------------------------------------------------ gateway outage is resumable
class _FlakySubmission:
    """Real standalone port, except ``submit_snapshot`` fails ``failures`` times."""

    def __init__(self, failures: int, error: Exception) -> None:
        self.inner = StandaloneSubmissionPort()
        self.failures = failures
        self.error = error
        self.submit_calls = 0

    def ensure_batch(self, owner_id, import_id):
        return self.inner.ensure_batch(owner_id, import_id)

    def submit_snapshot(self, *args, **kwargs):
        self.submit_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return self.inner.submit_snapshot(*args, **kwargs)

    def seal_batch(self, owner_id, batch_id):
        return self.inner.seal_batch(owner_id, batch_id)

    def reconcile(self, owner_id, batch_id):
        return self.inner.reconcile(owner_id, batch_id)

    def cancel(self, owner_id, batch_id):
        return self.inner.cancel(owner_id, batch_id)


def _gateway_down() -> GatewaySubmissionError:
    return GatewaySubmissionError("GATEWAY_UNAVAILABLE", "Gateway inacessível", {"path": "/x"})


def test_transient_gateway_outage_leaves_automation_import_resumable(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("gateway"))
    submission = _FlakySubmission(1, _gateway_down())
    service = ImportService(submission=submission)
    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
        schedule_run_id=f"run-{uuid.uuid4().hex[:12]}",
    )
    import_id = record["id"]

    # The original exception escapes so the executor's retry schedule applies.
    with pytest.raises(GatewaySubmissionError) as info:
        service.run(import_id, owner["id"])
    assert _is_transient(info.value)
    state = service.get(import_id, owner["id"])
    assert state["status"] == "failed"
    assert state["error"]["code"] == "IMPORT_GATEWAY_UNAVAILABLE"

    # The next occurrence attempt resumes the same import: no duplicate item or conversion.
    resumed = service.run(import_id, owner["id"])
    assert resumed["status"] == "completed", resumed
    items = service.items(import_id, owner["id"])
    assert len(items) == 1
    assert items[0]["status"] == "completed"
    assert items[0]["conversion_id"]
    assert submission.submit_calls == 2


def test_repeated_resume_after_outage_does_not_duplicate_conversions(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "a.xlsx").write_bytes(_xlsx_bytes("primeiro"))
    (root / "docs" / "b.xlsx").write_bytes(_xlsx_bytes("segundo"))
    submission = _FlakySubmission(1, TimeoutError("gateway lento"))
    service = ImportService(submission=submission)
    import_id = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
        schedule_run_id=f"run-{uuid.uuid4().hex[:12]}",
    )["id"]

    with pytest.raises(TimeoutError):
        service.run(import_id, owner["id"])
    assert service.get(import_id, owner["id"])["error"]["code"] == "IMPORT_GATEWAY_UNAVAILABLE"

    first = service.run(import_id, owner["id"])
    assert first["status"] == "completed", first
    conversions_after_resume = sorted(
        item["conversion_id"] for item in service.items(import_id, owner["id"])
    )
    assert len(conversions_after_resume) == 2

    # A further run of the finished import changes nothing.
    service.run(import_id, owner["id"])
    assert sorted(
        item["conversion_id"] for item in service.items(import_id, owner["id"])
    ) == conversions_after_resume


def test_manual_gateway_outage_returns_failed_state_without_raising(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("manual"))
    service = ImportService(submission=_FlakySubmission(1, ConnectionError("recusada")))
    import_id = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )["id"]

    result = service.run(import_id, owner["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "IMPORT_GATEWAY_UNAVAILABLE"


# ------------------------------------------------ vanished origins
def _seed_heads(owner_id: str, source_id: str, collection_id: str, paths: list[str]) -> None:
    session = get_session_factory()()
    try:
        for path in paths:
            session.add(
                SourceKnowledgeHeadRow(
                    owner_id=owner_id,
                    collection_id=collection_id,
                    source_id=source_id,
                    relative_path=path,
                    active_document_id=f"doc-{uuid.uuid4().hex[:8]}",
                    active_run_id=f"run-{uuid.uuid4().hex[:8]}",
                )
            )
        session.commit()
    finally:
        session.close()


def _head_flags(owner_id: str, source_id: str) -> dict[str, bool]:
    session = get_session_factory()()
    try:
        rows = session.query(SourceKnowledgeHeadRow).filter_by(owner_id=owner_id, source_id=source_id)
        return {row.relative_path: bool(row.source_missing) for row in rows}
    finally:
        session.close()


def test_deleted_origin_flags_its_head_on_the_next_full_import(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "a.xlsx").write_bytes(_xlsx_bytes("a"))
    (root / "docs" / "b.xlsx").write_bytes(_xlsx_bytes("b"))
    service = ImportService()
    first = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    assert service.run(first["id"], owner["id"])["status"] == "completed"

    collection = KnowledgeRepository().create_collection(owner_id=owner["id"], name="Base")["collection_id"]
    _seed_heads(owner["id"], source_id, collection, ["docs/a.xlsx", "docs/b.xlsx"])

    (root / "docs" / "b.xlsx").unlink()
    second = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    assert service.run(second["id"], owner["id"])["status"] == "completed"

    assert _head_flags(owner["id"], source_id) == {"docs/a.xlsx": False, "docs/b.xlsx": True}


def test_file_gone_mid_capture_flags_only_that_head(api_client, tmp_path: Path, monkeypatch):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "a.xlsx").write_bytes(_xlsx_bytes("a"))
    (root / "docs" / "b.xlsx").write_bytes(_xlsx_bytes("b"))
    service = ImportService()
    first = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    assert service.run(first["id"], owner["id"])["status"] == "completed"
    collection = KnowledgeRepository().create_collection(owner_id=owner["id"], name="Base")["collection_id"]
    _seed_heads(owner["id"], source_id, collection, ["docs/a.xlsx", "docs/b.xlsx"])

    original = importer_module.capture_snapshot

    def vanished(candidate, *args, **kwargs):
        if candidate.relative_path.endswith("b.xlsx"):
            raise SnapshotError("SOURCE_MISSING", "O arquivo sumiu da fonte.")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(importer_module, "capture_snapshot", vanished)
    second = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    service.run(second["id"], owner["id"])

    assert _head_flags(owner["id"], source_id) == {"docs/a.xlsx": False, "docs/b.xlsx": True}
