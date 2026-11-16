"""Audit manifest: the serialisable summary served by `/audit` and exported.

Two independent things are reported and must never be conflated:

* **cobertura** — share of recovered text that received a documented destination.
* **precisão** — share of recovered text that matches the source, measurable
  only against a golden dataset.  The manifest reports `measured_precision`
  as `null` unless a reference was supplied.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from audit.coverage import aggregate_coverage
from audit.events import AuditRecorder
from models.document import (
    DEST_DROPPED,
    DEST_FIELD,
    DEST_HEADER_FOOTER,
    DEST_METADATA,
    DEST_TABLE,
    DEST_TEXT,
    DEST_UNCLASSIFIED,
    DocumentIR,
    WorkbookPlan,
)

_ASSIGNED = {DEST_FIELD, DEST_TABLE, DEST_METADATA}


def build_summary(
    *,
    conversion_id: str,
    run_id: str,
    status: str,
    quality: str,
    ir: DocumentIR,
    plan: WorkbookPlan,
    recorder: AuditRecorder,
    validations: list[dict[str, Any]],
    hash_chain_valid: bool,
    measured_precision: float | None = None,
) -> dict[str, Any]:
    blocks = ir.all_blocks()
    assigned = sum(1 for b in blocks if b.destination in _ASSIGNED)
    preserved = sum(1 for b in blocks if b.destination == DEST_TEXT)
    unclassified = sum(1 for b in blocks if b.destination == DEST_UNCLASSIFIED)
    header_footer = sum(1 for b in blocks if b.destination == DEST_HEADER_FOOTER)
    dropped = sum(1 for b in blocks if b.destination == DEST_DROPPED)
    unassigned = sum(1 for b in blocks if b.destination is None)

    coverage_rows = [p.coverage for p in ir.pages if p.coverage is not None]
    agg = aggregate_coverage(coverage_rows)

    engines = [
        {
            "engine": usage.engine,
            "kind": usage.kind,
            "regions": usage.regions,
            "pages": usage.pages,
            "duration_ms": usage.duration_ms,
            "accepted": usage.accepted,
            "rejected": usage.rejected,
        }
        for usage in sorted(ir.engines.values(), key=lambda u: (-u.regions, u.engine))
    ]

    decisions = [
        {
            "decision_id": d.decision_id,
            "stage": d.stage,
            "subject": d.subject,
            "accepted": d.accepted,
            "algorithm": d.algorithm,
            "reason_codes": d.reason_codes,
            "alternatives": d.alternatives,
            "page_number": d.page_number,
            "at": _event_time_for(recorder, d.decision_id),
        }
        for d in recorder.decisions
    ]

    return {
        "conversion_id": conversion_id,
        "run_id": run_id,
        "quality": quality,
        "status": status,
        "metrics": {
            "pages_analyzed": len(ir.pages),
            "pages_total": ir.page_count,
            "blocks_total": len(blocks),
            "blocks_assigned": assigned,
            "blocks_preserved": preserved,
            "blocks_unclassified": unclassified + unassigned,
            "blocks_header_footer": header_footer,
            "blocks_dropped": dropped,
            "cells_generated": plan.total_cells(),
            "tables_detected": len(ir.tables),
            "fields_detected": sum(len(r.fields) for r in ir.records),
            "records_detected": len(ir.records),
            "ocr_regions": ir.ocr_regions,
            "events_total": len(recorder.events),
            "issues_total": len(recorder.issues),
            "warnings": recorder.count("warning"),
            "errors": recorder.count("error") + recorder.count("critical"),
            "coverage_percent": agg["coverage_percent"],
            "assignment_percent": agg["assignment_percent"],
            "tokens_total": int(agg["tokens"]),
            "measured_precision": measured_precision,
        },
        "coverage": [
            {
                "page_number": r.page_number,
                "text_blocks": r.text_blocks,
                "tokens": r.tokens,
                "assigned": r.assigned,
                "preserved": r.preserved,
                "header_footer": r.header_footer,
                "unclassified": r.unclassified,
                "dropped": r.dropped,
                "coverage_percent": r.coverage_percent,
            }
            for r in coverage_rows
        ],
        "engines": engines,
        "decisions": decisions,
        "validations": validations,
        "timings": recorder.stage_timings,
        "hash_chain_valid": hash_chain_valid,
        # Chain of custody: the manifest names the exact input document, so the
        # workbook can be tied back to the file it came from.
        "source": {
            "file_name": ir.file_name,
            "sha256": getattr(ir, "sha256", None),
            "page_count": len(ir.pages),
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _event_time_for(recorder: AuditRecorder, decision_id: str) -> str:
    for event in reversed(recorder.events):
        if event.payload.get("decision_id") == decision_id:
            return event.created_at.isoformat()
    return datetime.now(UTC).isoformat()


def build_export_json(summary: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"manifest": summary}
    if extra:
        payload.update(extra)
    return payload


def build_export_text(summary: dict[str, Any], pages: list[dict[str, Any]] | None = None) -> str:
    """Human-readable audit report (plain text, no external tooling)."""
    m = summary["metrics"]
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add("RELATÓRIO DE AUDITORIA DE CONVERSÃO")
    add("=" * 78)
    add(f"Conversão : {summary['conversion_id']}")
    add(f"Execução  : {summary['run_id']}")
    add(f"Status    : {summary['status']}")
    add(f"Qualidade : {summary['quality']}")
    add(f"Gerado em : {summary['generated_at']}")
    add("")
    add("-- MÉTRICAS " + "-" * 65)
    add(f"Páginas analisadas     : {m['pages_analyzed']}/{m['pages_total']}")
    add(f"Blocos de texto        : {m['blocks_total']}")
    add(f"  atribuídos a dados   : {m['blocks_assigned']}")
    add(f"  preservados como txt : {m['blocks_preserved']}")
    add(f"  não classificados    : {m['blocks_unclassified']}")
    add(f"  cabeçalho/rodapé     : {m.get('blocks_header_footer', 0)}")
    add(f"  descartados por regra: {m['blocks_dropped']}")
    add(f"Células geradas        : {m['cells_generated']}")
    add(f"Tabelas detectadas     : {m['tables_detected']}")
    add(f"Campos detectados      : {m['fields_detected']}")
    add(f"Registros detectados   : {m['records_detected']}")
    add(f"Regiões com OCR        : {m['ocr_regions']}")
    add(f"Cobertura textual      : {m['coverage_percent']}%")
    add(f"Atribuição a dados     : {m['assignment_percent']}%")
    add(f"Eventos de auditoria   : {m['events_total']}")
    add(f"Alertas / erros        : {m['warnings']} / {m['errors']}")
    add(f"Cadeia de hash válida  : {'sim' if summary['hash_chain_valid'] else 'NÃO'}")
    if m.get("measured_precision") is not None:
        add(f"Precisão medida        : {m['measured_precision']}%")
    else:
        add("Precisão medida        : não mensurada (requer conjunto de referência)")

    add("")
    add("-- MOTORES UTILIZADOS " + "-" * 56)
    if summary["engines"]:
        for engine in summary["engines"]:
            add(
                f"{engine['engine']:<24} {engine['kind']:<10} "
                f"regiões={engine['regions']:<5} aceitos={engine['accepted']:<5} "
                f"rejeitados={engine['rejected']:<5} {engine['duration_ms']}ms"
            )
    else:
        add("(nenhum)")

    add("")
    add("-- VALIDAÇÕES " + "-" * 63)
    for check in summary["validations"]:
        add(f"[{check['status'].upper():<4}] {check['check']:<32} {check['details']}")

    add("")
    add("-- COBERTURA POR PÁGINA " + "-" * 54)
    add(f"{'pág':>4} {'blocos':>7} {'tokens':>7} {'atrib':>7} {'pres':>6} {'nc':>5} {'cob%':>7}")
    for row in summary["coverage"]:
        add(
            f"{row['page_number']:>4} {row['text_blocks']:>7} {row['tokens']:>7} "
            f"{row['assigned']:>7} {row['preserved']:>6} {row['unclassified']:>5} "
            f"{row['coverage_percent']:>7}"
        )

    add("")
    add("-- DECISÕES " + "-" * 65)
    for decision in summary["decisions"][:200]:
        verdict = "ACEITO " if decision["accepted"] else "REJEIT."
        add(
            f"{verdict} {decision['stage']:<16} {decision['subject'][:44]:<44} "
            f"{decision['algorithm']} ({', '.join(decision['reason_codes'])})"
        )

    add("")
    add("-- INCONSISTÊNCIAS " + "-" * 58)
    issues = summary.get("issues", [])
    if not issues:
        add("(nenhuma registrada)")
    for issue in issues:
        page = f"p.{issue['page_number']}" if issue.get("page_number") else "-"
        add(f"[{issue['severity'].upper():<8}] {page:<7} {issue['code']}: {issue['message']}")

    if pages:
        add("")
        add("-- DETALHE POR PÁGINA " + "-" * 56)
        for page in pages:
            add(
                f"Página {page['page_number']}: {page['classification']} / "
                f"{page['text_source']} — blocos={page['text_blocks']} tabelas={page['tables']} "
                f"campos={page['fields']}"
            )
    add("")
    add("Fim do relatório.")
    return "\n".join(lines)


def summary_as_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, default=str)


def issue_to_dict(issue) -> dict[str, Any]:
    return {
        "issue_id": issue.issue_id,
        "run_id": issue.run_id,
        "severity": issue.severity,
        "code": issue.code,
        "message": issue.message,
        "page_number": issue.page_number,
        "region_id": issue.region_id,
        "context": issue.context,
    }


def asdict_safe(obj) -> dict[str, Any]:
    return {k: v for k, v in asdict(obj).items()}


__all__ = [
    "build_summary",
    "build_export_json",
    "build_export_text",
    "summary_as_json",
    "issue_to_dict",
]
