"""Append-only audit event log with a SHA-256 hash chain.

Design notes
------------
* Every observable step of the pipeline emits an event *before* the data it
  produces is persisted.  The event log is therefore the authoritative record
  of *why* a value exists in the workbook.
* `event_hash = sha256(sequence | event_type | stage | payload_hash | previous_hash)`.
  Tampering with any earlier row invalidates the whole chain from that point.
* A rejected candidate is **never** dropped silently: it is emitted as an event
  and, when it matters, persisted in `extraction_candidates`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from models.document import Decision, Issue

# --------------------------------------------------------------------------- #
# Event types (single vocabulary shared by backend, audit UI and docs)
# --------------------------------------------------------------------------- #

EVT_STAGE_STARTED = "STAGE_STARTED"
EVT_STAGE_COMPLETED = "STAGE_COMPLETED"
EVT_STAGE_FAILED = "STAGE_FAILED"
EVT_PROGRESS = "PROGRESS_UPDATED"
EVT_RUN_RECEIVED = "RUN_RECEIVED"
EVT_DOCUMENT_VALIDATED = "DOCUMENT_VALIDATED"
EVT_DOCUMENT_REJECTED = "DOCUMENT_REJECTED"
EVT_PAGE_ANALYZED = "PAGE_ANALYZED"
EVT_PAGE_SKIPPED = "PAGE_SKIPPED"
EVT_PAGE_FAILED = "PAGE_FAILED"
EVT_REGION_CLASSIFIED = "REGION_CLASSIFIED"
EVT_NATIVE_TEXT_EXTRACTED = "NATIVE_TEXT_EXTRACTED"
EVT_TEXT_LAYER_ASSESSED = "TEXT_LAYER_QUALITY_ASSESSED"
EVT_OCR_STARTED = "OCR_STARTED"
EVT_OCR_COMPLETED = "OCR_COMPLETED"
EVT_OCR_SKIPPED = "OCR_SKIPPED"
EVT_OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
EVT_TABLE_CANDIDATE = "TABLE_CANDIDATE_EVALUATED"
EVT_TABLE_SELECTED = "TABLE_SELECTED"
EVT_TABLE_REJECTED = "TABLE_CANDIDATE_REJECTED"
EVT_FIELD_MATCH_ACCEPTED = "FIELD_MATCH_ACCEPTED"
EVT_FIELD_MATCH_REJECTED = "FIELD_MATCH_REJECTED"
EVT_RECORD_GROUPED = "RECORD_GROUPED"
EVT_NORMALIZATION_APPLIED = "NORMALIZATION_APPLIED"
EVT_TABLE_CONTINUATION = "TABLE_CONTINUATION_LINKED"
EVT_PAGE_RECONCILED = "PAGE_RECONCILED"
EVT_SHEET_CREATED = "SHEET_CREATED"
EVT_CELL_WRITTEN = "CELL_WRITTEN"
EVT_EXCEL_GENERATED = "EXCEL_GENERATED"
EVT_XLSX_AUDIT_PASSED = "XLSX_AUDIT_PASSED"
EVT_XLSX_AUDIT_FAILED = "XLSX_AUDIT_FAILED"
EVT_COVERAGE_COMPUTED = "COVERAGE_COMPUTED"
EVT_ISSUE_RAISED = "ISSUE_RAISED"
EVT_ARTIFACT_STORED = "ARTIFACT_STORED"
EVT_RUN_COMPLETED = "RUN_COMPLETED"
EVT_RUN_FAILED = "RUN_FAILED"
EVT_RUN_CANCELLED = "RUN_CANCELLED"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"


def payload_hash(payload: dict[str, Any]) -> str:
    """Deterministic hash of a payload; key order and floats are normalised."""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_default)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _default(value: Any) -> str:  # pragma: no cover - defensive
    if hasattr(value, "to_list"):
        return str(value.to_list())
    return str(value)


#: Version 2 of the chained body: besides seq/type/stage/page/region/payload it
#: also covers the human-readable message, the severity, the extraction method
#: and the timestamp — everything an attacker would edit to rewrite the story
#: of the run.  Version 1 (the previous field set) is still accepted when
#: verifying, so chains written before this change remain verifiable.
_HASH_VERSION = 2


def canonical_timestamp(value: datetime | None) -> str | None:
    """Timezone-independent rendering of an instant.

    The chain is verified both in memory (aware UTC datetimes) and after a
    database round-trip (where SQLite hands back naive datetimes).  Hashing
    `isoformat()` directly would break as soon as the tz suffix disappears, so
    the timestamp is normalised to naive UTC before hashing.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat()


def _event_body(
    version: int,
    sequence: int,
    event_type: str,
    stage: str,
    page: int | None,
    region_id: str | None,
    payload: dict[str, Any],
    severity: str,
    message: str,
    extraction_method: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "seq": sequence,
        "type": event_type,
        "stage": stage,
        "page": page,
        "region": region_id,
        "payload": payload,
    }
    if version >= 2:
        body.update(
            {
                "severity": severity,
                "message": message,
                "extraction_method": extraction_method,
                "created_at": canonical_timestamp(created_at),
            }
        )
    return body


def event_digest(event: "AuditEvent", version: int = _HASH_VERSION) -> str:
    """Digest of an event under a given chain version."""
    return payload_hash(
        _event_body(
            version,
            event.sequence,
            event.event_type,
            event.stage,
            event.page_number,
            event.region_id,
            event.payload,
            event.severity,
            event.message,
            event.extraction_method,
            event.created_at,
        )
    )


@dataclass(slots=True)
class AuditEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    stage: str
    severity: str
    message: str
    payload: dict[str, Any]
    previous_hash: str | None
    event_hash: str
    page_number: int | None = None
    region_id: str | None = None
    extraction_method: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ProgressSink(Protocol):
    """Implemented by the run repository to persist status transitions."""

    def update(
        self,
        *,
        status: str,
        stage: str,
        progress: float,
        indeterminate: bool = False,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None: ...


class AuditRecorder:
    def __init__(self, run_id: str, sink: ProgressSink | None = None) -> None:
        self.run_id = run_id
        self.sink = sink
        self.events: list[AuditEvent] = []
        self.issues: list[Issue] = []
        self.decisions: list[Decision] = []
        self._sequence = 0
        self._prev_hash: str | None = None
        self._issue_counter = 0
        self._decision_counter = 0
        self._event_counter = 0
        self._stage_started: dict[str, datetime] = {}
        self.stage_timings: list[dict[str, Any]] = []

    # -- events ------------------------------------------------------------
    def emit(
        self,
        event_type: str,
        stage: str,
        message: str = "",
        *,
        severity: str = SEVERITY_INFO,
        page: int | None = None,
        region_id: str | None = None,
        extraction_method: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        self._sequence += 1
        self._event_counter += 1
        body = payload or {}
        created_at = datetime.now(UTC)
        digest = payload_hash(
            _event_body(
                _HASH_VERSION,
                self._sequence,
                event_type,
                stage,
                page,
                region_id,
                body,
                severity,
                message,
                extraction_method,
                created_at,
            )
        )
        chain = hashlib.sha256(f"{digest}|{self._prev_hash or ''}".encode()).hexdigest()
        event = AuditEvent(
            event_id=f"evt_{self._event_counter:06d}",
            run_id=self.run_id,
            sequence=self._sequence,
            event_type=event_type,
            stage=stage,
            severity=severity,
            message=message,
            payload=body,
            previous_hash=self._prev_hash,
            event_hash=chain,
            page_number=page,
            region_id=region_id,
            extraction_method=extraction_method,
            created_at=created_at,
        )
        self._prev_hash = chain
        self.events.append(event)
        return event

    # -- issues ------------------------------------------------------------
    def issue(
        self,
        code: str,
        message: str,
        *,
        severity: str = SEVERITY_WARNING,
        page: int | None = None,
        region_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Issue:
        self._issue_counter += 1
        issue = Issue(
            issue_id=f"iss_{self._issue_counter:05d}",
            run_id=self.run_id,
            severity=severity,
            code=code,
            message=message,
            page_number=page,
            region_id=region_id,
            context=context or {},
        )
        self.issues.append(issue)
        self.emit(
            EVT_ISSUE_RAISED,
            stage="audit",
            message=message,
            severity=severity,
            page=page,
            region_id=region_id,
            payload={"code": code, "issue_id": issue.issue_id, "context": issue.context},
        )
        return issue

    # -- decisions ---------------------------------------------------------
    def decide(
        self,
        *,
        stage: str,
        subject: str,
        accepted: bool,
        algorithm: str,
        reason_codes: list[str] | None = None,
        alternatives: int = 0,
        page: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Decision:
        self._decision_counter += 1
        decision = Decision(
            decision_id=f"dec_{self._decision_counter:05d}",
            stage=stage,
            subject=subject,
            accepted=accepted,
            algorithm=algorithm,
            reason_codes=reason_codes or [],
            alternatives=alternatives,
            page_number=page,
            payload=payload or {},
        )
        self.decisions.append(decision)
        self.emit(
            EVT_FIELD_MATCH_ACCEPTED if accepted else EVT_FIELD_MATCH_REJECTED,
            stage=stage,
            message=f"{subject} -> {'accepted' if accepted else 'rejected'} ({algorithm})",
            page=page,
            payload={
                "decision_id": decision.decision_id,
                "reason_codes": decision.reason_codes,
                "alternatives": alternatives,
                **decision.payload,
            },
        )
        return decision

    # -- stages ------------------------------------------------------------
    def stage_start(self, stage: str, message: str = "") -> None:
        self._stage_started[stage] = datetime.now(UTC)
        self.emit(EVT_STAGE_STARTED, stage, message or f"stage {stage} started")
        if self.sink:
            self.sink.update(status=_status_for_stage(stage), stage=stage, progress=0.0, message=message)

    def stage_end(self, stage: str, message: str = "", pages: int = 0) -> dict[str, Any]:
        started = self._stage_started.pop(stage, None)
        finished = datetime.now(UTC)
        duration = int((finished - started).total_seconds() * 1000) if started else 0
        record = {
            "stage": stage,
            "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat(),
            "duration_ms": duration,
            "pages": pages,
        }
        self.stage_timings.append(record)
        self.emit(
            EVT_STAGE_COMPLETED,
            stage,
            message or f"stage {stage} finished in {duration} ms",
            payload={"duration_ms": duration, "pages": pages},
        )
        return record

    # -- verification ------------------------------------------------------
    def verify_chain(self) -> bool:
        prev: str | None = None
        for event in self.events:
            if event.previous_hash != prev:
                return False
            expected = hashlib.sha256(
                f"{event_digest(event, _HASH_VERSION)}|{prev or ''}".encode()
            ).hexdigest()
            if expected != event.event_hash:
                # Chains written before the field set was widened still verify.
                expected_v1 = hashlib.sha256(
                    f"{event_digest(event, 1)}|{prev or ''}".encode()
                ).hexdigest()
                if expected_v1 != event.event_hash:
                    return False
            prev = event.event_hash
        return True

    # -- counters ----------------------------------------------------------
    @property
    def sequence(self) -> int:
        return self._sequence

    def count(self, severity: str) -> int:
        return sum(1 for i in self.issues if i.severity == severity)


_STAGE_STATUS = {
    "receive": "received",
    "validate": "validating",
    "inspect": "inspecting",
    "extract": "extracting",
    "structure": "structuring",
    "normalize": "normalizing",
    "reconcile": "reconciling",
    "generate": "generating",
    "audit": "auditing",
    "deliver": "completed",
}


def _status_for_stage(stage: str) -> str:
    return _STAGE_STATUS.get(stage, stage)


__all__ = [
    "AuditEvent",
    "AuditRecorder",
    "ProgressSink",
    "payload_hash",
    "EVT_STAGE_STARTED",
    "EVT_STAGE_COMPLETED",
    "EVT_STAGE_FAILED",
    "EVT_RUN_COMPLETED",
    "EVT_RUN_FAILED",
    "EVT_RUN_CANCELLED",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "SEVERITY_CRITICAL",
]
