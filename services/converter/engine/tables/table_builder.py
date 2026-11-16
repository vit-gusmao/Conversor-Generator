"""Table orchestration: candidates -> validation -> structure.

Running every engine and concatenating the output would duplicate data.  The
builder therefore scores candidates, keeps the best per region and records the
rejected ones as alternatives (visible in the audit).

Post-processing happens here: header detection, merged spans, continuation
rows, summary rows and transposed-table detection.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from audit.events import (
    EVT_TABLE_CANDIDATE,
    EVT_TABLE_REJECTED,
    EVT_TABLE_SELECTED,
    AuditRecorder,
)
from engine.tables import docling_adapter
from engine.tables.hybrid import detect_hybrid
from engine.tables.text_pattern import (
    _QUANTITY_LABELS,
    _close_to_label,
    detect_text_tables,
)
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
    median,
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

    page_height = float(getattr(page, 'height', 0.0) or 0.0)
    scored = [_score(raw, tokens, page_height=page_height) for raw in candidates]
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

    ocr_page = bool(tokens) and all(
        getattr(token, "origin", "") == "ocr" for token in tokens
    )
    tables = _prefer_named_reading(tables, prefer_text_pattern=ocr_page)

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

    # Always offered, independent of pixels: a header line naming the columns
    # followed by lines that end in numbers.  Only grids with no hole are kept —
    # a hole means the split was wrong and values would land in the wrong
    # columns — and the post-selection decides which reading represents the
    # region.
    try:
        candidates.extend(
            candidate
            for candidate in detect_text_tables(tokens, page_number)
            if _raw_is_clean(candidate)
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


def _mostly_generic(headers: list[str]) -> bool:
    if not headers:
        return True
    generic = sum(1 for header in headers if re.fullmatch(r"Coluna \d+", header or ""))
    return generic >= max(1, len(headers) // 2)


def _raw_is_clean(raw: RawTable) -> bool:
    """A text-pattern grid must have every cell filled."""
    rows = raw.rows[1:] if len(raw.rows) > 1 else []
    if len(rows) < 2:
        return False
    width = max(len(row) for row in rows)
    if width < 3:
        return False
    for row in rows:
        if len(row) != width:
            return False
        if any(not str(cell).strip() for cell in row):
            return False
    return True


def _pattern_is_clean(table: TableStructure) -> bool:
    """The text-pattern grid is usable only when no cell is missing.

    It infers boundaries from the text, so a hole in the middle of a row means
    the split was wrong (values would land in the wrong columns).  Only clean
    grids are allowed to replace a geometrical one.
    """
    if len(table.rows) < 3 or not table.headers:
        return False
    for row in table.rows:
        if len(row) != len(table.headers):
            return False
        if any(not str(cell).strip() for cell in row):
            return False
    numeric_tails = sum(
        1 for row in table.rows if str(row[-1]).strip() and any(ch.isdigit() for ch in str(row[-1]))
    )
    return numeric_tails >= 2


def _same_region(a: BBox, b: BBox) -> bool:
    """Two candidates describe the same table when one contains the other.

    A geometric grid often covers only the rows (the header band was not ruled),
    while the text-pattern reading spans header + rows: IoU alone would call them
    different regions.
    """
    return _iou(a, b) >= 0.4 or _containment(a, b) >= 0.6


def _consistent_grid(table: TableStructure) -> bool:
    """True when the grid width matches its own header labels.

    A degraded scan can produce a six-column grid under five labels; the cells
    are then shifted and no column can be trusted.  The text-pattern reading
    (built from the text alone) is the better answer in that case.
    """
    if not table.headers:
        return False
    if len(table.headers) != table.n_cols:
        return False
    return not any(re.fullmatch(r"Coluna \d+", header or "") for header in table.headers)


def _prefer_named_reading(
    tables: list[TableStructure], *, prefer_text_pattern: bool = False
) -> list[TableStructure]:
    """Keep the reading whose columns are actually named.

    A degraded scan can produce a geometric grid whose columns are all
    "Coluna N" while the text-pattern reading of the same region names them
    (Item, Descrição, Qtd, Preço, Total).  Only one table represents a region;
    the one that tells the reader what each column is wins, and the geometric
    table is kept when *it* is the one with real headers.
    """
    named = [
        table
        for table in tables
        if table.strategy == "text_pattern"
        and not _mostly_generic(table.headers)
        and _pattern_is_clean(table)
    ]
    if not named:
        return tables
    kept: list[TableStructure] = []
    for table in tables:
        if table.strategy == "text_pattern":
            explained = any(
                other is not table and _same_region(other.bbox, table.bbox)
                and not _mostly_generic(other.headers)
                and (not prefer_text_pattern or _consistent_grid(other))
                for other in tables
            )
            if explained:
                continue
            kept.append(table)
            continue
        needs_better_reading = _mostly_generic(table.headers) or (
            prefer_text_pattern and not _consistent_grid(table)
        )
        if needs_better_reading:
            replacement = next(
                (candidate for candidate in named if _same_region(candidate.bbox, table.bbox)),
                None,
            )
            if replacement is not None:
                continue
        kept.append(table)
    return kept


def _score(raw: RawTable, tokens: list[Token], *, page_height: float = 0.0) -> ScoredCandidate:
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
    strategy_bonus = {"lattice": 0.12, "hybrid": 0.05, "docling": 0.04}.get(raw.strategy, 0.0)
    score = 0.5 * coverage + 0.28 * density + 0.22 * size_bonus + strategy_bonus
    if raw.strategy == "stream":
        score -= 0.05  # prefer a drawn grid when both explain the region
    # A "table" that covers the whole page without a drawn grid is the page
    # itself: the alignment engines happily turn a form plus its items list into
    # one big grid, which then swallows the fields.  Real grids may be tall; an
    # inferred one that tall is a false positive.
    if raw.strategy in {"stream", "hybrid"} and page_height and page_height > 0:
        if raw.bbox.height / page_height >= 0.8:
            score -= 0.3

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

    # The grid may not contain the header row at all (the OCR saw the header as
    # one line above the ruled area).  The document still names its columns, so
    # that line is used instead of exporting "Coluna N".
    header_from_text = _header_line_above(raw, tokens, max(len(r) for r in rows))

    # A header drawn without inner vertical rules arrives as a single cell
    # ("Item Descricao Qtd Preco Total").  Without splitting it the table has
    # no header at all and every column is named "Coluna N".
    _unglue_header_row(rows, raw, tokens)
    # The header may reveal more columns than the rows carried (a lost column
    # boundary): re-pad the body to the final width before repairing cells.
    final_width = max(len(row) for row in rows)
    for row in rows:
        while len(row) < final_width:
            row.append("")
    _split_shifted_cells(rows)

    # Spans are detected on the final grid: computing them before the trims
    # made every index drift by the number of removed rows/columns.
    merged = _detect_spans(raw, rows)

    header_index, header_confidence = _header_index(rows, raw, tokens)
    headers: list[str] = []
    if header_index is None and header_from_text:
        headers = list(header_from_text)
    data_rows = rows
    if header_index is not None:
        headers = [_clean_header(cell, i) for i, cell in enumerate(rows[header_index])]
        data_rows = [r for i, r in enumerate(rows) if i != header_index]
        # A repeated header inside the body is not a record.
        data_rows = [r for r in data_rows if not _same_row(r, rows[header_index])]
    else:
        data_rows = [r for r in rows if not _generic_transposed_header(r)]

    if _looks_like_prose(data_rows):
        # A running text column (or an address block) is not a table: exporting
        # it as one turns paragraphs into cells and hides the text sheet.  The
        # body is what matters — a short header above prose proves nothing.
        return None

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


def _looks_like_prose(rows: list[list[str]]) -> bool:
    """True when the grid is running text laid out in columns, not a table.

    Prose has no numeric column and its cells are sentences; a real table always
    has short cells somewhere (or a number).  The test is intentionally narrow:
    it only rejects grids where the majority of cells are long sentences.
    """
    cells = [cell.strip() for row in rows for cell in row if cell.strip()]
    if not cells:
        return True
    # A grid whose cells carry several "Label:" markers is a form laid out in
    # columns, not a table: exporting it hides every field behind a fake cell
    # (and the field engine never sees those lines, because they are "inside a
    # table").
    label_cells = sum(1 for cell in cells if len(re.findall(r"\S+[:=]", cell)) >= 2)
    if label_cells >= 2:
        return True
    if any(_NUMERIC.match(cell) for cell in cells):
        return False
    wordy = sum(1 for cell in cells if len(cell.split()) >= 8)
    return wordy / len(cells) >= 0.6


def _column_gap(box: BBox, x: float) -> float:
    """Distance from *x* to a column box (0 when inside it)."""
    if box.x0 <= x <= box.x1:
        return 0.0
    return box.x0 - x if x < box.x0 else x - box.x1


def _glued_cell_bbox(raw: RawTable, text: str) -> BBox | None:
    target = " ".join(text.split())
    for cell in raw.cells:
        if cell.missing or not cell.text:
            continue
        if " ".join(cell.text.split()) == target:
            return cell.bbox
    return None


_NUMERIC_TAIL = re.compile(
    r"^(?P<head>.+?\S)\s+(?P<tail>[-+]?(?:[R$€£]\s*)?\d[\d.,]*)$"
)


def _is_numeric_header(cell: str) -> bool:
    label = _header_key_free(cell)
    return label in {
        "qtd", "qtde", "qnt", "quantidade", "qt",
        "preco", "preço", "valor", "vlr", "vl", "unitario", "unitário",
        "total", "subtotal", "desconto", "peso", "km",
    }


def _header_key_free(cell: str) -> str:
    text = unicodedata.normalize("NFKD", (cell or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text)


def _shift_rows_right_by_one(
    rows: list[list[str]], numeric_column: list[bool], header: list[str]
) -> None:
    """Repair a grid whose data rows lost one column boundary.

    In a receipt the quantity sits so close to the description that the two
    arrive as one cell, and every row then holds one value fewer than the header
    (the last column stays empty).  The whole row is shifted, so the repair is an
    *insertion*: the trailing number of the first cell that is followed by a
    number becomes its own column and everything to its right moves one column.

    Only fires with strong evidence: at least two data rows, the same number of
    filled cells in every one of them, and the header clearly having one more
    column than the rows.
    """
    data = rows[1:] if len(rows) > 1 else []
    if len(data) < 2 or not numeric_column:
        return
    width = max(len(row) for row in rows)
    filled = [sum(1 for cell in row if cell.strip()) for row in data]
    if len(set(filled)) != 1 or filled[0] >= width:
        return
    header_filled = sum(1 for cell in header if str(cell).strip())
    if header_filled <= filled[0]:
        return
    for row in data:
        for column in range(len(row) - 1):
            cell = row[column]
            if not cell.strip() or not str(row[column + 1]).strip():
                continue
            match = _NUMERIC_TAIL.match(cell.strip())
            if not match:
                continue
            head, tail = match.group("head").strip(), match.group("tail").strip()
            if not head or _NUMERIC.match(head):
                continue
            row[column] = head
            row.insert(column + 1, tail)
            while len(row) > width:
                row.pop()
            break


def _split_shifted_cells(rows: list[list[str]]) -> None:
    """Move a number that stuck to the previous cell into its own column.

    A receipt prints ``01 Treinamento 2 376.92 753.84``; the gap between the
    description and the quantity is smaller than the column gap, so the grid
    arrives as ``["01", "Treinamento 2", "", "376,92", "753,84"]``.  The
    numeric column on the right is empty (or its header says "Qtd") and the
    cell on the left ends with a number: moving the trailing number is the only
    reading consistent with the header.
    """
    if len(rows) < 2:
        return
    width = max(len(row) for row in rows)
    numeric_column: list[bool] = []
    for column in range(width):
        values = [row[column].strip() for row in rows if column < len(row) and row[column].strip()]
        numeric_column.append(
            bool(values) and sum(1 for value in values if _NUMERIC.match(value)) / len(values) >= 0.6
        )
    header = rows[0] if rows else []
    _shift_rows_right_by_one(rows, numeric_column, header)
    for row in rows:
        for column in range(width - 1):
            target = column + 1
            header_hint = target < len(header) and _is_numeric_header(header[target])
            if not (numeric_column[target] or header_hint):
                continue
            if target < len(row) and row[target].strip():
                continue
            cell = row[column] if column < len(row) else ""
            match = _NUMERIC_TAIL.match(cell.strip())
            if not match:
                continue
            head, tail = match.group("head").strip(), match.group("tail").strip()
            if not head or _NUMERIC.match(head):
                continue
            row[column] = head
            row[target] = tail


def _unglue_header_row(rows: list[list[str]], raw: RawTable, tokens: list[Token]) -> None:
    """Split a header row that a merged cell glued into one column.

    Layouts drawn with horizontal rules only (or with a header band that has no
    inner vertical rules) arrive as ``["Item Descricao Qtd Preco Total", "", ...]``.
    Redistribution is deterministic and conservative:

    1. one word per column when the word count equals the column count;
    2. otherwise the words keep their geometry and are placed in the column
       that contains their centre.

    Anything ambiguous leaves the row untouched: inventing a header is worse
    than exporting "Coluna N".
    """
    if not rows:
        return
    first = rows[0]
    width = len(first)
    filled = [index for index, cell in enumerate(first) if cell.strip()]
    if width < 3 or len(filled) != 1:
        return
    text = first[filled[0]]
    words = text.split()
    if len(words) < 2:
        return
    if len(words) == width:
        rows[0] = words
        return

    columns = list(getattr(raw, "col_boxes", []) or [])
    if len(columns) != width:
        # The grid has fewer columns than the header labels.  A receipt prints
        # "Item Descricao Qtd Preco Total" as one cell while the data rows lost
        # the Qtd boundary: the labels are the only evidence of the real width,
        # so the row is split one word per column and the caller pads the body.
        if (
            len(words) > width
            and len(words) <= width + 2
            and all(word.isalpha() and len(word) <= 14 for word in words)
        ):
            rows[0] = words
        return
    box = _glued_cell_bbox(raw, text)
    if box is None:
        return
    inside = [
        token
        for token in tokens
        if box.contains_point(token.bbox.x_center, token.bbox.y_center, tol=2.0)
    ]
    if len(inside) < 2:
        return
    buckets: list[list[str]] = [[] for _ in range(width)]
    for token in sorted(inside, key=lambda item: item.bbox.x0):
        target = min(range(width), key=lambda index: _column_gap(columns[index], token.bbox.x_center))
        buckets[target].append(token.text)
    placed = sum(1 for bucket in buckets if bucket)
    if placed < max(2, width // 2):
        return
    rows[0] = [" ".join(bucket).strip() for bucket in buckets]


def _header_line_above(raw: RawTable, tokens: list[Token], width: int) -> list[str] | None:
    """Column labels from the text line sitting immediately above the grid."""
    if width < 2 or not tokens:
        return None
    from engine.tables.text_pattern import _QUANTITY_LABELS, _close_to_label

    heights = [token.bbox.height for token in tokens if token.bbox.height > 0]
    tolerance = max(2.0, (median(heights) or 8.0) * 0.6)
    groups: list[list[Token]] = []
    for token in sorted(tokens, key=lambda item: (item.bbox.y_center, item.bbox.x0)):
        for group in groups:
            if abs(group[0].bbox.y_center - token.bbox.y_center) <= tolerance:
                group.append(token)
                break
        else:
            groups.append([token])

    best: tuple[float, list[Token]] | None = None
    for group in groups:
        group.sort(key=lambda item: item.bbox.x0)
        bottom = max(token.bbox.y1 for token in group)
        gap = raw.bbox.y0 - bottom
        if gap < -2.0 or gap > 140.0:
            continue
        # The recogniser may return the whole header as one token
        # ("Item Descrição Qtd Preco Total"); the labels are still there.
        words = [word for token in group for word in token.text.split()]
        known = sum(1 for word in words if _close_to_label(word, _QUANTITY_LABELS))
        if known < 3 or len(words) < 3:
            continue
        if any(word.endswith((":", "=")) for word in words):
            # A form line, not a column header.
            continue
        if best is None or gap < best[0]:
            best = (gap, words)
    if best is None:
        return None
    words = best[1]
    if len(words) > width:
        return None
    return words + [f"Coluna {index + 1}" for index in range(len(words), width)]


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
    # A first row made only of column labels is a header, whatever the numeric
    # signals say: OCR pages often lose the ruled header band and the labels end
    # up as the first data row ("Coluna 1..5" otherwise).
    known_labels = sum(
        1
        for cell in non_empty
        if _close_to_label(cell, _QUANTITY_LABELS) and not any(ch.isdigit() for ch in cell)
    )
    if known_labels >= 3:
        return 0, 0.8
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
