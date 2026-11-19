"""Regression tests for engine/data-path defects found in the FX-2a audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from audit.events import AuditRecorder
from audit.manifest import build_summary
from models.document import DocumentIR, WorkbookPlan


class _NoReverseList(list):
    """Event list that refuses reverse iteration (AU2-21 quadratic lookup guard)."""

    def __reversed__(self):  # pragma: no cover - only reached by the old code
        raise AssertionError("audit events must not be reverse-scanned per decision")


def test_decision_timestamps_come_from_matching_event_without_reverse_scan():
    recorder = AuditRecorder(run_id="run_manifest")
    for index in range(5):
        recorder.decide(stage="field", subject=f"campo {index}", accepted=True, algorithm="rule")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for offset, event in enumerate(recorder.events):
        event.created_at = base + timedelta(seconds=offset)
    expected = {
        event.payload["decision_id"]: event.created_at.isoformat()
        for event in recorder.events
        if "decision_id" in event.payload
    }
    recorder.events = _NoReverseList(recorder.events)

    summary = build_summary(
        conversion_id="conv",
        run_id="run_manifest",
        status="completed",
        quality="ok",
        ir=DocumentIR(file_name="a.pdf", sha256="x", page_count=1),
        plan=WorkbookPlan(sheets=[]),
        recorder=recorder,
        validations=[],
        hash_chain_valid=True,
    )

    assert len(summary["decisions"]) == 5
    for decision in summary["decisions"]:
        assert decision["at"] == expected[decision["decision_id"]]


class _FakeS3Client:
    """Stands in for boto3: reads the file object the way upload_fileobj does."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes, dict]] = []

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):  # noqa: N803
        data = b""
        while True:
            chunk = fileobj.read(4096)
            if not chunk:
                break
            data += chunk
        self.uploads.append((bucket, key, data, ExtraArgs or {}))


def test_s3_put_stream_hands_boto3_a_readable_file_object():
    # AU2-18: upload_fileobj needs a file-like object with read(), not a generator.
    import hashlib
    import io

    from storage.s3 import S3Storage

    client = _FakeS3Client()
    storage = S3Storage.__new__(S3Storage)  # bypass boto3 client construction
    storage.bucket = "bucket-test"
    storage._client = client
    payload = bytes(range(256)) * 9000  # > one 1 MiB read, exercises chunking logic

    stored = storage.put_stream("docs/a.pdf", io.BytesIO(payload), "application/pdf")

    bucket, key, data, extra = client.uploads[0]
    assert (bucket, key) == ("bucket-test", "docs/a.pdf")
    assert data == payload
    assert extra == {"ContentType": "application/pdf"}
    assert stored.size_bytes == len(payload)
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
