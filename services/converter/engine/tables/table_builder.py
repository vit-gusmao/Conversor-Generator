"""Table orchestration: candidates -> validation -> structure.

Running every engine and concatenating the output would duplicate data.  The
builder therefore scores candidates, keeps the best per region and records the
rejected ones as alternatives (visible in the audit).

Post-processing happens here: header detection, merged spans, continuation
rows, summary rows and transposed-table detection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from audit.events import (
    EVT_TABLE_CANDIDATE,
    EVT_TABLE_REJECTED,
    EVT_TABLE_SELECTED,
    AuditRecorder,
)
from engine.tables import docling_adapter
from engine.tables.hybrid import detect_hybrid
from engine.tables.lattice import RawTable, detect_lattice, detect_lattice_manual
from engine.tables.stream import detect_stream
from models.document import (
    BBox,
    MergedSpan,
    TableAlternative,
    TableCell,
    TableStructure,
    TextBlock,
    Token,
)

_TOTAL = re.compile(
    r"^\s*(total|totais|subtotal|sub-total|saldo|soma|valor total|total geral|total do m[eê]s)\b",
    re.IGNORECASE,
)
_NUMERIC = re.compile(
    r"^[\s\-+]?(?:[R$€£]\s*)?[\d.,]+%?$"
    r"|^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$"
    r"|^\(\s*[\d.,\s]+\)$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ScoredCandidate:
    raw: RawTable
    score: float
    coverage: float
    density: float
    accepted: bool = False
    reason: str = ""


def _horizontal_rules(page) -> list[float]:
    """Y positions of drawn horizontal rules, clamped to the page."""
    try:
        edges = list(getattr(page, "edges", []) or [])
        page_top = float(page.bbox[1])
        page_bottom = float(page.bbox[3])
    except Exception:  # noqa: BLE001
        return []
    values: list[float] = []
    for edge in edges:
        try:
            if edge.get("orientation") != "h":
                continue
            top = float(edge["top"])
        except (KeyError, TypeError, ValueError):
            continue
        if page_top - 2 <= top <= page_bottom + 2:
            values.append(top)
    if not values:
        return []
    values.sort()
    clustered: list[float] = [values[0]]
    for value in values[1:]:
        if value - clustered[-1] > 2.5:
            clustered.append(value)
    return clustered


def detect_tables(
    page,
    page_number: int,
    tokens: list[Token],
    blocks: list[TextBlock],
    *,
    rotation: int = 0,
    page_width: float = 595.0,
    recorder: AuditRecorder | None = None,
) -> list[TableStructure]:
    candidates = _collect_candidates(page, page_number, tokens, blocks, rotation, page_width)
    if not candidates:
        return []

    scored = [_score(raw, tokens) for raw in candidates]
    scored = [
        s
        for s in scored
        if s.raw.n_cols >= 2
        and len(s.raw.rows) >= 1
        # A single-row fragment is only credible when a grid was actually drawn
        # (a continuation chunk at the end of a document looks exactly like that).
        and (len(s.raw.rows) >= 3 or s.raw.strategy in {"lattice", "hybrid", "docling"})
    ]
    if not scored:
        return []

    scored.sort(key=lambda s: s.score, reverse=True)
    selected: list[ScoredCandidate] = []
    for candidate in scored:
        overlaps = [
            s
            for s in selected
            if _iou(s.raw.bbox, candidate.raw.bbox) >= 0.45
            or _containment(s.raw.bbox, candidate.raw.bbox) >= 0.6
        ]
        if overlaps:
            best = max(overlaps, key=lambda s: s.score)
            candidate.reason = f"overlaps accepted table ({best.raw.strategy}, score {best.score:.2f})"
            candidate.accepted = False
            continue
        candidate.accepted = True
        selected.append(candidate)

    tables: list[TableStructure] = []
    for index, candidate in enumerate(selected, start=1):
        table = _build_structure(candidate.raw, index, page_number, tokens, blocks)
        if table is None:
            candidate.accepted = False
            candidate.reason = "structure_postprocessing_failed"
            continue
        table.alternatives = [
            TableAlternative(
                strategy=other.raw.strategy,
                n_rows=len(other.raw.rows),
                n_cols=other.raw.n_cols,
                confidence=round(other.score, 3),
                accepted=other.accepted,
                reason=other.reason or ("selected" if other.accepted else "lower_score"),
                coverage=round(other.coverage, 3),
            )
            for other in scored
            if _iou(other.raw.bbox, candidate.raw.bbox) >= 0.3
        ]
        tables.append(table)

        if recorder:
            recorder.emit(
                EVT_TABLE_SELECTED,
                "structure",
                f"Tabela {table.table_id} selecionada ({table.strategy}, "
                f"{table.n_rows}x{table.n_cols})",
                page=page_number,
                payload={
                    "table_id": table.table_id,
                    "strategy": table.strategy,
                    "rows": table.n_rows,
                    "cols": table.n_cols,
                    "confidence": round(table.confidence, 3),
                    "headers": table.headers,
                    "bbox": table.bbox.to_list(),
                },
            )

    if recorder:
        for candidate in scored:
            if candidate.accepted:
                continue
            recorder.emit(
                EVT_TABLE_CANDIDATE,
                "structure",
                f"Candidato de tabela rejeitado ({candidate.raw.strategy}): {candidate.reason}",
                severity="info",
                page=page_number,
                payload={
                    "strategy": candidate.raw.strategy,
                    "engine": candidate.raw.engine,
                    "score": round(candidate.score, 3),
                    "coverage": round(candidate.coverage, 3),
                    "reason": candidate.reason,
                    "bbox": candidate.raw.bbox.to_list(),
                },
            )
    return tables


def _collect_candidates(
    page,
    page_number: int,
    tokens: list[Token],
    blocks: list[TextBlock],
    rotation: int,
    page_width: float,
) -> list[RawTable]:
    candidates: list[RawTable] = []
    if rotation == 0:
        try:
            candidates.extend(detect_lattice(page, page_number))
        except Exception:  # noqa: BLE001
            pass
        if not candidates:
            try:
                candidates.extend(detect_lattice_manual(page, page_number, tokens))
            except Exception:  # noqa: BLE001
                pass

    # A table is a page-level structure: layout blocks can cut it in half (a
    # header block, an items block, a totals block).  Detecting per block
    # missed every table that crossed a block boundary, so the whole page is
    # analysed and the detector itself separates regions by vertical gaps.
    h_rules = _horizontal_rules(page)
    try:
        candidates.extend(
            detect_stream(tokens, page_number, page_width=page_width, h_rules=h_rules)
        )
    except Exception:  # noqa: BLE001
        pass
    if rotation == 0:
        try:
            candidates.extend(
                detect_hybrid(page, page_number, tokens, page_width=page_width, h_rules=h_rules)
            )
        except Exception:  # noqa: BLE001
            pass

    # Remove duplicates produced by different engines on the same block.
    unique: list[RawTable] = []
    for candidate in candidates:
        if candidate.bbox.is_empty:
            candidate.bbox = BBox.union_all([row for row in candidate.row_boxes])
        duplicate = any(
            (_iou(candidate.bbox, kept.bbox) > 0.85 and candidate.n_cols == kept.n_cols)
            or (
                _containment(candidate.bbox, kept.bbox) >= 0.8
                and candidate.n_cols == kept.n_cols
                and abs(len(candidate.rows) - len(kept.rows)) <= 2
            )
            for kept in unique
        )
        if not duplicate:
            unique.append(candidate)
    return unique


def _score(raw: RawTable, tokens: list[Token]) -> ScoredCandidate:
    total = max(1, len(raw.rows) * max(1, raw.n_cols))
    filled = sum(1 for row in raw.rows for cell in row if cell and cell.strip())
    density = filled / total

    inside = [
        token
        for token in tokens
        if raw.bbox.contains_point(token.bbox.x_center, token.bbox.y_center, tol=2.0)
    ]
    assigned = 0
    for token in inside:
        if any(
            cell.bbox.contains_point(token.bbox.x_center, token.bbox.y_center, tol=1.5)
            for cell in raw.cells
            if not cell.missing
        ):
            assigned += 1
    coverage = assigned / max(1, len(inside)) if inside else 0.0

    size_bonus = min(1.0, (len(raw.rows) * raw.n_cols) / 24.0)
    strategy_bonus = {"lattice": 0.08, "hybrid": 0.05, "docling": 0.04}.get(raw.strategy, 0.0)
    score = 0.5 * coverage + 0.28 * density + 0.22 * size_bonus + strategy_bonus
    if raw.strategy == "stream":
        score -= 0.05  # prefer a drawn grid when both explain the region

    raw.text_coverage = round(coverage, 4)
    return ScoredCandidate(raw=raw, score=round(score, 4), coverage=coverage, density=density)


def _iou(a: BBox, b: BBox) -> float:
    inter = a.overlap_area(b)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def _containment(a: BBox, b: BBox) -> float:
    """Intersection over the smaller box: 1.0 when one contains the other."""
    inter = a.overlap_area(b)
    smaller = min(a.area, b.area)
    return inter / smaller if smaller > 0 else 0.0


# --------------------------------------------------------------------------- #
# Post-processing
# --------------------------------------------------------------------------- #


def _title_above(bbox: BBox, blocks: list[TextBlock]) -> str | None:
    """Text of a title block sitting just above the table, if any."""
    best: TextBlock | None = None
    best_gap: float | None = None
    for block in blocks:
        if block.kind != "title":
            continue
        gap = bbox.y0 - block.bbox.y1
        if gap < -1.0 or gap > 40.0:
            continue
        overlap = min(bbox.x1, block.bbox.x1) - max(bbox.x0, block.bbox.x0)
        if overlap <= 0:
            continue
        if best_gap is None or gap < best_gap:
            best = block
            best_gap = gap
    if best is None:
        return None
    text = " ".join(best.text.split())
    return text[:120] or None


def _build_structure(
    raw: RawTable,
    index: int,
    page_number: int,
    tokens: list[Token],
    blocks: list[TextBlock] | None = None,
) -> TableStructure | None:
    rows = [list(r) for r in raw.rows]
    if not rows:
        return None
    n_cols = max(len(r) for r in rows)
    for row in rows:
        while len(row) < n_cols:
            row.append("")

    rows = _trim_edges(rows)
    if len(rows) < 1 or max(len(r) for r in rows) < 2:
        return None

    rows = _merge_continuation_rows(rows)
    rows = _trim_edges(rows)
    if not rows:
        return None

    # Spans are detected on the final grid: computing them before the trims
    # made every index drift by the number of removed rows/columns.
    merged = _detect_spans(raw, rows)

    header_index, header_confidence = _header_index(rows, raw, tokens)
    headers: list[str] = []
    data_rows = rows
    if header_index is not None:
        headers = [_clean_header(cell, i) for i, cell in enumerate(rows[header_index])]
        data_rows = [r for i, r in enumerate(rows) if i != header_index]
        # A repeated header inside the body is not a record.
        data_rows = [r for r in data_rows if not _same_row(r, rows[header_index])]
    else:
        data_rows = [r for r in rows if not _generic_transposed_header(r)]

    transposed = _looks_transposed(data_rows)
    if transposed:
        headers = []
        row_kinds = ["data"] * len(data_rows)
    else:
        row_kinds = ["summary" if _TOTAL.match(r[0] if r else "") else "data" for r in data_rows]
        if headers:
            row_kinds = ["header"] + row_kinds

    strategy = "transposed" if transposed else raw.strategy
    confidence = raw.confidence
    if header_index is not None:
        confidence = min(0.98, confidence + 0.03)
    if transposed:
        confidence = min(0.98, confidence + 0.05)

    cells = [
        TableCell(
            row=cell.row,
            col=cell.col,
            text=cell.text,
            bbox=cell.bbox,
            rowspan=cell.rowspan,
            colspan=cell.colspan,
        )
        for cell in raw.cells
        if not cell.missing and cell.text
    ]
    element_ids = [
        t.element_id
        for t in tokens
        if raw.bbox.contains_point(t.bbox.x_center, t.bbox.y_center, tol=2.0)
    ]

    return TableStructure(
        table_id=f"tbl_{page_number:03d}_{index:03d}",
        page_number=page_number,
        bbox=raw.bbox,
        strategy=strategy,
        # A title immediately above the table names the sheet ("Pagamentos"
        # instead of "Tabela 2"); it is metadata, never data.
        title=_title_above(raw.bbox, blocks or []),
        rows=data_rows,
        headers=headers,
        cells=cells,
        merged=merged,
        row_kinds=row_kinds,
        confidence=round(confidence, 3),
        column_boxes=raw.col_boxes,
        row_boxes=[],
        element_ids=element_ids,
    )


def _clean_header(cell: str, index: int) -> str:
    text = " ".join((cell or "").split())
    if not text:
        return f"Coluna {index + 1}"
    return text[:120]


def _trim_edges(rows: list[list[str]]) -> list[list[str]]:
    while rows and not any(cell.strip() for cell in rows[0]):
        rows.pop(0)
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    keep = [False] * width
    for row in rows:
        for index, cell in enumerate(row):
            if cell.strip():
                keep[index] = True
    if all(keep):
        return rows
    return [[cell for index, cell in enumerate(row) if keep[index]] for row in rows]


def _header_index(
    rows: list[list[str]], raw: RawTable, tokens: list[Token]
) -> tuple[int | None, float]:
    """Header detection needs positive evidence, not just "first row is text".

    Two independent signals are accepted:

    * a body column is predominantly numeric while its first-row cell is not;
    * the first row is rendered in a bold face and the second is not.

    Without either, the first row is data — which is what makes the
    transposed-table detector able to recognise `Campo | Valor` layouts.
    """
    if len(rows) < 2:
        return None, 0.0
    first = rows[0]
    non_empty = [c for c in first if c.strip()]
    if len(non_empty) < max(2, len(first) // 2):
        return None, 0.0
    if any(_NUMERIC.match(c) for c in non_empty):
        return None, 0.0

    body = rows[1:]
    numeric_signal = False
    for column in range(len(first)):
        body_values = [r[column] for r in body if column < len(r) and r[column].strip()]
        if len(body_values) < 2:
            continue
        numeric = sum(1 for value in body_values if _NUMERIC.match(value.strip()))
        header_cell = first[column].strip()
        if numeric / len(body_values) >= 0.5 and header_cell and not _NUMERIC.match(header_cell):
            numeric_signal = True
            break
        # A column whose values carry digits under a label without any digit
        # ("Amount" / "R$ 110,00", "Qty" / "1") is a header even when the value
        # is formatted currency that _NUMERIC does not recognise.
        digits = sum(1 for value in body_values if any(ch.isdigit() for ch in value))
        if (
            header_cell
            and not any(ch.isdigit() for ch in header_cell)
            and digits / len(body_values) >= 0.8
        ):
            numeric_signal = True
            break
    if numeric_signal:
        return 0, 0.85

    if _row_is_bold(raw, 0, tokens) and not _row_is_bold(raw, 1, tokens):
        return 0, 0.8
    return None, 0.0


def _row_is_bold(raw: RawTable, row_index: int, tokens: list[Token]) -> bool:
    cells = [cell for cell in raw.cells if cell.row == row_index and not cell.missing and cell.text]
    if not cells:
        return False
    bold = 0
    total = 0
    for cell in cells:
        if cell.bbox.is_empty:
            continue
        for token in tokens:
            if not cell.bbox.contains_point(token.bbox.x_center, token.bbox.y_center, tol=0.8):
                continue
            total += 1
            if "bold" in (token.font or "").lower():
                bold += 1
    return total > 0 and bold / total >= 0.6


def _generic_transposed_header(row: list[str]) -> bool:
    if len(row) < 2:
        return False
    label = row[0].strip().lower()
    value = row[1].strip().lower()
    return label in {"campo", "campos", "atributo", "item", "descrição", "descricao", "field"} and value in {
        "valor",
        "valores",
        "value",
        "conteúdo",
        "conteudo",
        "dados",
    }


def _same_row(a: list[str], b: list[str]) -> bool:
    if len(a) != len(b):
        return False
    return all(x.strip().lower() == y.strip().lower() for x, y in zip(a, b))


def _detect_spans(raw: RawTable, rows: list[list[str]]) -> list[MergedSpan]:
    missing = {(cell.row, cell.col) for cell in raw.cells if cell.missing}
    if not missing:
        return []
    n_rows = len(rows)
    n_cols = max(len(r) for r in rows)
    spans: list[MergedSpan] = []
    claimed: set[tuple[int, int]] = set()
    for row_index in range(n_rows):
        for col_index in range(n_cols):
            if (row_index, col_index) in missing:
                continue
            # A missing cell is covered vertically when the cell *above* already
            # spans over it; otherwise it belongs to a span starting at the
            # nearest cell to its left in the same row. Extending colspan blindly
            # gave colspan=2 to a normal cell sitting left of a vertical merge.
            above_covers = (row_index, col_index) not in claimed and any(
                span.row <= row_index - 1 < span.row + span.rowspan
                and span.col <= col_index < span.col + span.colspan
                for span in spans
            )
            if above_covers:
                continue
            colspan = 1
            while (row_index, col_index + colspan) in missing and (
                row_index,
                col_index + colspan,
            ) not in claimed:
                colspan += 1
            rowspan = 1
            while all(
                (row_index + rowspan, col_index + offset) in missing
                for offset in range(colspan)
            ):
                rowspan += 1
                if rowspan > 20:
                    break
            if colspan > 1 or rowspan > 1:
                spans.append(MergedSpan(row_index, col_index, rowspan, colspan))
                for r in range(row_index, min(row_index + rowspan, n_rows)):
                    for c in range(col_index, min(col_index + colspan, n_cols)):
                        claimed.add((r, c))
    return spans


def _merge_continuation_rows(rows: list[list[str]]) -> list[list[str]]:
    """Merge a row whose first cell is empty into the row above.

    `João da Silva | Analista de` + ` | Dados` must become one record, not two.
    """
    if len(rows) < 2:
        return rows
    result: list[list[str]] = [list(rows[0])]
    for row in rows[1:]:
        previous = result[-1]
        non_empty = [c for c in row if c.strip()]
        first_empty = not (row[0].strip() if row else False)
        # A wrapped cell is a row with a *single* value aligned under a column
        # that is not the first one ("Analista de" / "Dados").  A row with two
        # or more values is a record of its own — merging it would glue
        # "Subtotal", "Total" and "Amount due" into one line.
        wrapped = len(non_empty) == 1 and first_empty
        if wrapped and any(c.strip() for c in previous):
            for index, cell in enumerate(row):
                if not cell.strip():
                    continue
                if index < len(previous):
                    previous[index] = (previous[index] + " " + cell).strip()
                else:
                    previous.append(cell.strip())
            continue
        result.append(list(row))
    return result


def _looks_transposed(rows: list[list[str]]) -> bool:
    if len(rows) < 2 or len(rows[0]) != 2:
        return False
    if len(rows) > 40:
        return False
    filled = [r for r in rows if r[0].strip() and r[1].strip()]
    if len(filled) < max(2, int(len(rows) * 0.7)):
        return False
    labels = [r[0].strip() for r in filled]
    if len(set(label.lower() for label in labels)) < len(labels) * 0.8:
        return False
    numeric_labels = sum(1 for label in labels if _NUMERIC.match(label))
    if numeric_labels > len(labels) * 0.2:
        return False
    long_labels = sum(1 for label in labels if len(label.split()) > 5)
    if long_labels > len(labels) * 0.3:
        return False
    values = [r[1].strip() for r in filled]
    has_number = any(_NUMERIC.match(v) for v in values)
    if has_number:
        # A label/value pair with at least one value that is not a number: the
        # classic "Campo | Valor" block.
        return any(not _NUMERIC.match(v) and len(v) > 3 for v in values)
    # Pure text can also be transposed: the labels are unique attribute names
    # and the values are short, non-numeric text ("Nome/Cidade/Cargo").
    if len(filled) >= 3 and all(len(v.split()) <= 4 for v in values):
        labels_are_names = all(len(label.split()) <= 4 for label in labels)
        return labels_are_names
    return False


__all__ = ["detect_tables", "ScoredCandidate"]
