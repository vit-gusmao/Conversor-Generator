"""Stage 13 — reconciliation across pages and between engines.

Two distinct problems:

1. **Table continuation** — a table that starts on page 1 and continues on page
   2 must become one sheet, with the repeated header dropped.
2. **Extraction duplication** — two engines may describe the same region; a
   region id must be consumed once.

Continuity is inferred from combined evidence (column count, header similarity,
value types, geometry, page adjacency).  When the evidence is weak the tables
stay separate: joining wrongly is worse than a second sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from audit.events import EVT_TABLE_CONTINUATION, AuditRecorder
from models.document import Record, TableStructure

_NUMERIC = re.compile(r"^-?[\d.,]+%?$")


@dataclass(slots=True)
class ReconciliationResult:
    tables: list[TableStructure] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    merges: list[dict] = field(default_factory=list)
    duplicates_removed: int = 0
    issues: list[dict] = field(default_factory=list)


def reconcile(
    tables: list[TableStructure],
    records: list[Record],
    *,
    recorder: AuditRecorder | None = None,
) -> ReconciliationResult:
    result = ReconciliationResult()
    result.tables = _merge_tables(tables, recorder, result.merges)
    result.records, result.duplicates_removed = _dedupe_records(records)
    return result


def _merge_tables(
    tables: list[TableStructure],
    recorder: AuditRecorder | None,
    merges: list[dict],
) -> list[TableStructure]:
    ordered = sorted(tables, key=lambda t: (t.page_number, t.bbox.y0))
    merged: list[TableStructure] = []
    last_page: dict[int, int] = {}

    for table in ordered:
        target: TableStructure | None = None
        best_score = 0.0
        best_reasons: list[str] = []
        for candidate in merged:
            if last_page[id(candidate)] != table.page_number - 1:
                continue
            score, reasons = _continuity_score(candidate, table)
            if score >= 0.62 and score > best_score:
                # Several tables can start on the previous page: the best
                # match wins, not the first one above the threshold.
                target = candidate
                best_score = score
                best_reasons = reasons
        if target is None:
            merged.append(table)
            last_page[id(table)] = table.page_number
            table.row_pages = [table.page_number] * len(table.rows)
            continue

        score, reasons = best_score, best_reasons
        added = _append_rows(target, table)
        last_page[id(target)] = table.page_number
        merge_info = {
            "from_table": target.table_id,
            "continued_table": table.table_id,
            "page": table.page_number,
            "score": round(score, 3),
            "reason_codes": reasons,
            "rows_added": added,
        }
        merges.append(merge_info)
        if recorder:
            recorder.emit(
                EVT_TABLE_CONTINUATION,
                "reconcile",
                f"Tabela {table.table_id} (p.{table.page_number}) unificada em "
                f"{target.table_id} — {added} linha(s)",
                page=table.page_number,
                payload=merge_info,
            )
        if added == 0:
            recorder and recorder.issue(
                "CONTINUATION_EMPTY",
                f"A tabela da página {table.page_number} foi considerada continuação, mas não "
                "acrescentou linhas.",
                severity="warning",
                page=table.page_number,
            )
    return merged


def _append_rows(target: TableStructure, table: TableStructure) -> int:
    rows = [list(row) for row in table.rows]
    # Only a repeated *header* is dropped. A data row that happens to equal the
    # last row of the previous page is a legitimate record (two employees can
    # share a name and a value) and must never be deleted here.
    if rows and target.headers and _same_header(rows[0], target.headers):
        rows = rows[1:]
    target.rows.extend(rows)
    target.row_pages.extend([table.page_number] * len(rows))
    target.element_ids.extend(table.element_ids)
    target.continued_on_page = table.page_number
    target.merged.extend(table.merged)
    target.confidence = round(min(0.98, target.confidence + 0.01), 3)
    return len(rows)


def _continuity_score(a: TableStructure, b: TableStructure) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    if a.n_cols != b.n_cols:
        return 0.0, ["column_count_mismatch"]
    score += 0.25
    reasons.append("same_column_count")

    if a.headers and b.headers:
        similarity = _header_similarity(a.headers, b.headers)
        if similarity < 0.55:
            return 0.0, ["header_mismatch"]
        score += 0.3 * similarity
        reasons.append("header_similarity")
    elif a.headers or b.headers:
        score += 0.05
        reasons.append("partial_header")
    else:
        score += 0.12
        reasons.append("no_headers")

    type_score = _type_compatibility(a, b)
    if type_score < 0.4:
        return 0.0, ["type_mismatch"]
    score += 0.2 * type_score
    reasons.append("type_compatibility")

    if score >= 0.55:
        reasons.append("page_adjacent")
        score += 0.1

    if a.column_boxes and b.column_boxes and len(a.column_boxes) == len(b.column_boxes):
        deltas = [
            abs(x.x0 - y.x0)
            for x, y in zip(a.column_boxes, b.column_boxes)
        ]
        if deltas and max(deltas) <= 12.0:
            score += 0.13
            reasons.append("column_geometry_match")
        elif deltas and max(deltas) > 40.0:
            score -= 0.1
            reasons.append("column_geometry_diverges")

    return max(0.0, min(1.0, score)), reasons


def _header_similarity(a: list[str], b: list[str]) -> float:
    left = {_norm(h) for h in a if h.strip()}
    right = {_norm(h) for h in b if h.strip()}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9áéíóúâêôãõç ]", " ", text.lower()).split())


def _type_compatibility(a: TableStructure, b: TableStructure) -> float:
    left = _numeric_ratio(a)
    right = _numeric_ratio(b)
    return 1.0 - abs(left - right)


def _numeric_ratio(table: TableStructure) -> float:
    cells = [c for row in table.rows for c in row if c.strip()]
    if not cells:
        return 0.0
    numeric = sum(1 for c in cells if _NUMERIC.match(c.strip()))
    return numeric / len(cells)


def _same_header(row: list[str], headers: list[str]) -> bool:
    if len(row) != len(headers):
        return False
    return all(_norm(x) == _norm(y) for x, y in zip(row, headers))


def _same_row(a: list[str], b: list[str]) -> bool:
    if len(a) != len(b):
        return False
    return all(_norm(x) == _norm(y) for x, y in zip(a, b))


def _dedupe_records(records: list[Record]) -> tuple[list[Record], int]:
    kept: list[Record] = []
    removed = 0
    for record in records:
        duplicate = False
        for existing in kept:
            if existing.page_number != record.page_number:
                continue
            if existing.bbox.overlap_ratio(record.bbox) < 0.7:
                continue
            if _signature(existing) == _signature(record):
                duplicate = True
                break
        if duplicate:
            removed += 1
        else:
            kept.append(record)
    return kept, removed


def _signature(record: Record) -> tuple:
    return tuple(sorted((_norm(f.label), _norm(f.value)) for f in record.fields if f.value.strip()))


__all__ = ["reconcile", "ReconciliationResult"]
