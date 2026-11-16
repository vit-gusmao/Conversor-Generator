"""Pre-delivery validation of the workbook plan.

These checks run *before* the XLSX is written and again on the produced file
(`engine/excel/auditor.py`).  They never fix data silently: they produce
`pass | fail | warn | skip` results that end up in the audit manifest and drive
the final quality state.
"""

from __future__ import annotations

from dataclasses import dataclass

from audit.coverage import aggregate_coverage
from models.document import (
    SHEET_CONSOLIDATED,
    SHEET_RECORDS,
    DocumentIR,
    WorkbookPlan,
)


@dataclass(slots=True)
class ValidationCheck:
    check: str
    status: str
    details: str
    measured: str | None = None
    expected: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "check": self.check,
            "status": self.status,
            "details": self.details,
            "measured": self.measured,
            "expected": self.expected,
        }


def validate_plan(plan: WorkbookPlan, ir: DocumentIR) -> list[ValidationCheck]:
    checks: list[ValidationCheck] = []

    sheets = plan.sheets
    non_empty = [s for s in sheets if s.rows]
    checks.append(
        ValidationCheck(
            check="sheets_not_empty",
            status="pass" if len(non_empty) == len(sheets) and sheets else "fail",
            details=f"{len(non_empty)} de {len(sheets)} abas possuem linhas",
            measured=str(len(non_empty)),
            expected=str(len(sheets)),
        )
    )

    duplicate_headers = []
    for sheet in sheets:
        normalised = [_norm(h) for h in sheet.headers]
        if len(set(normalised)) != len(normalised):
            duplicate_headers.append(sheet.name)
    checks.append(
        ValidationCheck(
            check="headers_unique",
            status="pass" if not duplicate_headers else "warn",
            details="Cabeçalhos duplicados: " + ", ".join(duplicate_headers)
            if duplicate_headers
            else "Todos os cabeçalhos são únicos por aba",
            measured=str(len(duplicate_headers)),
            expected="0",
        )
    )

    record_sheets = [s for s in sheets if s.kind == SHEET_RECORDS]
    empty_records = sum(
        1 for s in record_sheets for row in s.rows if not any(c is not None for c in row)
    )
    checks.append(
        ValidationCheck(
            check="records_complete",
            status="pass" if empty_records == 0 else "warn",
            details=f"{empty_records} linha(s) de registro sem nenhuma célula preenchida",
            measured=str(empty_records),
            expected="0",
        )
    )

    identifiers = [
        cell
        for sheet in sheets
        for row in sheet.rows
        for cell in row
        if cell is not None and cell.value_type in {"code", "identifier", "competencia"}
    ]
    broken_identifiers = [c for c in identifiers if not isinstance(c.value, str)]
    checks.append(
        ValidationCheck(
            check="identifiers_preserved",
            status="pass" if not broken_identifiers else "fail",
            details=f"{len(identifiers)} identificador(es) mantidos como texto",
            measured=str(len(broken_identifiers)),
            expected="0 convertidos",
        )
    )

    formula_cells = [
        cell
        for sheet in sheets
        for row in sheet.rows
        for cell in row
        if cell is not None and isinstance(cell.value, str) and cell.value.startswith("=")
    ]
    checks.append(
        ValidationCheck(
            check="no_formula_cells",
            status="pass" if not formula_cells else "warn",
            details=f"{len(formula_cells)} célula(s) preservadas como texto por iniciarem com '='",
            measured=str(len(formula_cells)),
            expected="0 fórmulas",
        )
    )

    coverage = aggregate_coverage([p.coverage for p in ir.pages if p.coverage is not None])
    coverage_ok = coverage["coverage_percent"] >= 99.0
    checks.append(
        ValidationCheck(
            check="text_coverage",
            status="pass" if coverage_ok else "warn",
            details=f"Cobertura textual de {coverage['coverage_percent']}% "
            f"({int(coverage['assigned'])} tokens atribuídos)",
            measured=f"{coverage['coverage_percent']}%",
            expected="100%",
        )
    )

    malformed_tables = [
        t for t in ir.tables if t.strategy != "transposed" and (t.n_cols < 2 or len(t.rows) < 1)
    ]
    checks.append(
        ValidationCheck(
            check="tables_structure",
            status="pass" if not malformed_tables else "warn",
            details=f"{len(ir.tables)} tabela(s) reconhecidas, {len(malformed_tables)} degenerada(s)",
            measured=str(len(malformed_tables)),
            expected="0",
        )
    )

    # Provenance is measured on the sheets that carry extraction results: the
    # consolidated tab is a derived copy (its "Aba"/"Página" columns are added
    # by the resolver), so counting it would dilute a real provenance failure.
    source_sheets = [sheet for sheet in sheets if sheet.kind != SHEET_CONSOLIDATED]
    cells_without_source = [
        cell
        for sheet in source_sheets
        for row in sheet.rows
        for cell in row
        if cell is not None and cell.element_id is None
    ]
    total_cells = sum(1 for s in source_sheets for r in s.rows for c in r if c is not None)
    ratio = (len(cells_without_source) / total_cells) if total_cells else 0.0
    checks.append(
        ValidationCheck(
            check="cell_provenance",
            status="pass" if ratio <= 0.05 else "warn",
            details=f"{len(cells_without_source)} de {total_cells} células sem origem identificada",
            measured=f"{round(ratio * 100, 2)}%",
            expected="<= 5%",
        )
    )

    too_wide = [s.name for s in sheets if len(s.headers) > 16_384]
    too_long = [s.name for s in sheets if len(s.rows) > 1_048_575]
    checks.append(
        ValidationCheck(
            check="excel_limits",
            status="pass" if not too_wide and not too_long else "fail",
            details="Dentro dos limites do formato .xlsx"
            if not too_wide and not too_long
            else f"Abas acima do limite: {too_wide + too_long}",
            measured=str(len(too_wide) + len(too_long)),
            expected="0",
        )
    )

    # A page that yielded no text (image-only without OCR, unreadable scan, or
    # a parser failure) must never be reported as "validated": the workbook is
    # usable but the document is only partially represented.  Failed pages are
    # counted explicitly: they are the clearest form of missing content and
    # used to be excluded from the denominator.
    failed_pages = [page for page in ir.pages if page.status == "failed"]
    unrecovered = [
        page
        for page in ir.pages
        if page.status == "partial"
        or (
            not page.tokens
            and page.status != "failed"
            and (page.metrics.get("images", 0) or page.classification in {"scan", "image"})
        )
    ]
    checks.append(
        ValidationCheck(
            check="pages_failed",
            status="fail" if failed_pages else "pass",
            details=(
                f"{len(failed_pages)} página(s) falharam e não possuem representação: "
                f"{[p.page_number for p in failed_pages]}"
                if failed_pages
                else "Nenhuma página falhou"
            ),
            measured=str(len(failed_pages)),
            expected="0",
        )
    )
    checks.append(
        ValidationCheck(
            check="unrecognized_regions",
            status="pass" if not unrecovered else "warn",
            details=(
                "Todas as páginas produziram texto"
                if not unrecovered
                else f"{len(unrecovered)} página(s) sem texto recuperado: "
                f"{[p.page_number for p in unrecovered]}"
            ),
            measured=str(len(unrecovered)),
            expected="0",
        )
    )
    unread_image_regions = [
        page
        for page in ir.pages
        if page.status != "failed"
        and not page.ocr_chars
        and page.metrics.get("images", 0)
        and page.metrics.get("image_coverage", 0.0) >= 0.12
    ]
    checks.append(
        ValidationCheck(
            check="image_regions_unread",
            status="pass" if not unread_image_regions else "warn",
            details=(
                "Nenhuma região de imagem ficou sem reconhecimento"
                if not unread_image_regions
                else f"{len(unread_image_regions)} página(s) possuem região de imagem não "
                f"reconhecida: {[p.page_number for p in unread_image_regions]}"
            ),
            measured=str(len(unread_image_regions)),
            expected="0",
        )
    )

    expected = int(ir.page_count or len(ir.pages))
    checks.append(
        ValidationCheck(
            check="pages_accounted",
            status="pass" if len(ir.pages) >= expected else "fail",
            details=(
                f"{len(ir.pages)} de {expected} página(s) original(is) presentes no resultado"
            ),
            measured=str(len(ir.pages)),
            expected=str(expected),
        )
    )

    return checks


def quality_from_checks(checks: list[ValidationCheck], has_content: bool) -> str:
    if not has_content:
        return "failed"
    if any(check.status == "fail" for check in checks):
        return "partial"
    if any(check.status == "warn" for check in checks):
        return "partial"
    return "validated"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


__all__ = ["ValidationCheck", "validate_plan", "quality_from_checks"]
