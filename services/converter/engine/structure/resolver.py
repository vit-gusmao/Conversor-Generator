"""Stage 10/14 — structure resolution.

Turns records and tables into the *workbook plan*: which sheet, which header,
which row, which cell — and marks every source block with its destination so
coverage can be audited.

Rules applied here:

* one record = one row, one field = one column (dynamic headers);
* independent tables = independent sheets;
* tables detected as transposed become fields, not two columns;
* text that is not a field or a table is preserved, never dropped;
* repeated labels inside a record become `Label 1`, `Label 2`;
* Excel limits are respected by splitting, never by truncating.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from audit.events import EVT_SHEET_CREATED, AuditRecorder
from audit.evidence import column_letter
from engine.fields.field_extractor import section_map
from engine.normalization.normalizer import (
    LocaleProfile,
    NormalizedValue,
    canonical_header,
    normalize,
)
from models.document import (
    DEST_FIELD,
    DEST_HEADER_FOOTER,
    DEST_METADATA,
    DEST_TABLE,
    DEST_TEXT,
    DEST_UNCLASSIFIED,
    SHEET_RECORDS,
    SHEET_TABLE,
    SHEET_TEXT,
    SHEET_UNCLASSIFIED,
    BBox,
    CellPlan,
    DocumentIR,
    Field,
    Record,
    SheetPlan,
    TableStructure,
    TextBlock,
    WorkbookPlan,
)

MAX_EXCEL_ROWS = 1_048_575
MAX_EXCEL_COLS = 16_384
MAX_CELL_CHARS = 32_767

_INVALID_SHEET = re.compile(r"[\[\]:*?/\\]")


@dataclass(slots=True)
class ResolutionResult:
    plan: WorkbookPlan
    sheet_pages: dict[str, list[int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def build_workbook(
    ir: DocumentIR,
    *,
    locale: LocaleProfile | None = None,
    recorder: AuditRecorder | None = None,
) -> ResolutionResult:
    locale = locale or LocaleProfile.default()
    result = ResolutionResult(plan=WorkbookPlan(title=_document_title(ir)))
    used_names: set[str] = set()

    block_for = _block_lookup(ir)
    blocks_by_id = {block.element_id: block for block in ir.all_blocks()}
    records = _records_with_transposed(ir)
    record_sheets = _build_record_sheets(records, locale, used_names, recorder, block_for, blocks_by_id)
    table_sheets = _build_table_sheets(
        ir.tables, locale, used_names, recorder, block_for, _field_boxes(records)
    )

    result.plan.sheets.extend(record_sheets)
    result.plan.sheets.extend(table_sheets)

    # Free text and unclassified blocks are computed *after* destinations are
    # known, so already-consumed text is not duplicated.
    consumed = _mark_structural_destinations(ir, result.plan)

    text_sheet = _build_text_sheet(
        ir, consumed, used_names, locale, _consumed_boxes(result.plan), result.plan
    )
    if text_sheet is not None:
        result.plan.sheets.append(text_sheet)

    unclassified = _build_unclassified_sheet(ir, used_names)
    if unclassified is not None:
        result.plan.sheets.append(unclassified)

    _mark_remaining_destinations(ir, text_sheet, unclassified, result.plan)
    _reindex(result.plan)
    _apply_excel_limits(result.plan, recorder)
    return result


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


def _records_with_transposed(ir: DocumentIR) -> list[Record]:
    records = list(ir.records)
    for table in ir.tables:
        if table.strategy != "transposed":
            continue
        fields = []
        for row in table.rows:
            if len(row) < 2:
                continue
            label, value = row[0].strip(), row[1].strip()
            if not label or not value:
                continue
            fields.append(
                Field(
                    label=label,
                    value=value,
                    page_number=table.page_number,
                    label_bbox=table.bbox,
                    value_bbox=table.bbox,
                    separator="form",
                    confidence=table.confidence,
                    reason_codes=["transposed_table"],
                    section=None,
                )
            )
        if fields:
            record = Record(
                record_id=f"rec_transposed_{table.table_id}",
                page_number=table.page_number,
                fields=fields,
                section=None,
                origin="transposed",
            )
            records.append(record.finalize())
    return records


def _block_lookup(ir: DocumentIR):
    by_page: dict[int, list[TextBlock]] = {}
    for block in ir.all_blocks():
        by_page.setdefault(block.page_number, []).append(block)

    def lookup(page_number: int | None, bbox: BBox | None) -> str | None:
        if page_number is None or bbox is None:
            return None
        best: TextBlock | None = None
        best_score = 0.0
        for block in by_page.get(page_number, []):
            intersection = block.bbox.overlap_area(bbox)
            denominator = min(block.bbox.area, bbox.area)
            score = (intersection / denominator) if denominator > 0 else 0.0
            if score > best_score:
                best_score = score
                best = block
        return best.element_id if best is not None and best_score >= 0.4 else None

    return lookup


def _build_record_sheets(
    records: list[Record],
    locale: LocaleProfile,
    used_names: set[str],
    recorder: AuditRecorder | None,
    block_for,
    blocks_by_id: dict[str, TextBlock],
) -> list[SheetPlan]:
    if not records:
        return []
    groups = _group_records(records)
    sheets: list[SheetPlan] = []
    for index, group in enumerate(groups):
        if not group.records:
            continue
        name = _sheet_name_for_group(group, index, used_names)
        sheet = _records_to_sheet(name, group.records, locale, block_for, blocks_by_id)
        if sheet.rows:
            sheets.append(sheet)
            if recorder:
                recorder.emit(
                    EVT_SHEET_CREATED,
                    "normalize",
                    f"Aba '{sheet.name}' criada com {sheet.n_rows} registro(s) e {sheet.n_cols} coluna(s)",
                    payload={
                        "sheet": sheet.name,
                        "kind": sheet.kind,
                        "rows": sheet.n_rows,
                        "columns": sheet.headers,
                        "pages": sheet.source_pages,
                    },
                )
    return sheets


@dataclass(slots=True)
class _RecordGroup:
    records: list[Record] = field(default_factory=list)
    section: str | None = None
    labels: set[str] = field(default_factory=set)


def _group_records(records: list[Record]) -> list[_RecordGroup]:
    groups: list[_RecordGroup] = []
    for record in records:
        signature = {_canon(column) for column in _record_label_set(record)}
        target: _RecordGroup | None = None
        best_overlap = 0.0
        for group in groups:
            # Records of the same shape belong to one table even when a heading
            # ("Funcionário 1", "Funcionário 2") put them in different sections:
            # the plan asks for two *rows*, not two sheets. The section stays as
            # the sheet name, taken from the first group that has one.
            overlap = _jaccard(group.labels, signature)
            if overlap >= 0.6 and overlap > best_overlap:
                target = group
                best_overlap = overlap
            elif group.section and record.section and group.section == record.section:
                target = group
                break
        if target is None:
            target = _RecordGroup(section=record.section, labels=set())
            groups.append(target)
        target.records.append(record)
        target.labels |= signature
        if not target.section and record.section:
            target.section = record.section
    return groups


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _record_label_set(record: Record) -> list[str]:
    return [f.label for f in record.fields]


def _canon(label: str) -> str:
    return " ".join(label.lower().replace(":", "").split())


def _records_to_sheet(
    name: str,
    records: list[Record],
    locale: LocaleProfile,
    block_for,
    blocks_by_id: dict[str, TextBlock],
) -> SheetPlan:
    # Column order and duplicate handling.
    order: list[str] = []
    display: dict[str, str] = {}
    max_occurrence: dict[str, int] = {}
    for record in records:
        counts: dict[str, int] = {}
        for field in record.fields:
            key = _canon(field.label)
            counts[key] = counts.get(key, 0) + 1
            max_occurrence[key] = max(max_occurrence.get(key, 1), counts[key])
            if key not in display:
                display[key] = canonical_header(field.label)
                order.append(key)

    headers: list[str] = []
    for key in order:
        occurrences = max_occurrence.get(key, 1)
        if occurrences > 1:
            for index in range(1, occurrences + 1):
                headers.append(f"{display[key]} {index}")
        else:
            headers.append(display[key])
    headers = _unique_headers(headers)

    sheet = SheetPlan(
        name=name,
        kind=SHEET_RECORDS,
        headers=headers,
        source_pages=sorted({r.page_number for r in records}),
    )

    for record in records:
        row: list[CellPlan | None] = [None] * len(headers)
        counts = {}
        for field in record.fields:
            key = _canon(field.label)
            occurrence = counts.get(key, 0) + 1
            counts[key] = occurrence
            column_index = _column_for(order, max_occurrence, key, occurrence)
            if column_index is None or column_index >= len(headers):
                continue
            normalized = normalize(field.value, header=display.get(key), locale=locale)
            source_block = blocks_by_id.get(block_for(field.page_number, field.bbox) or "")
            row[column_index] = _cell_from_normalized(
                normalized,
                header=headers[column_index],
                record_id=record.record_id,
                field_label=field.label,
                element_id=block_for(field.page_number, field.bbox),
                page_number=field.page_number,
                bbox=field.bbox,
                # Provenance must follow the text: a cell read by OCR can never
                # be reported as native.
                origin=source_block.origin if source_block is not None else None,
                engine=source_block.engine if source_block is not None else None,
                confidence=source_block.confidence if source_block is not None else None,
            )
        sheet.rows.append(row)
    return sheet


def _column_for(order: list[str], max_occurrence: dict[str, int], key: str, occurrence: int) -> int | None:
    if key not in order:
        return None
    base = 0
    for previous in order:
        if previous == key:
            return base + occurrence - 1
        base += max_occurrence.get(previous, 1)
    return None


def _unique_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for header in headers:
        base = header or "Campo"
        count = seen.get(base, 0) + 1
        seen[base] = count
        result.append(base if count == 1 else f"{base} ({count})")
    return result


def _cell_from_normalized(
    normalized: NormalizedValue,
    *,
    header: str,
    record_id: str | None,
    field_label: str | None,
    element_id: str | None,
    page_number: int | None,
    bbox: BBox | None,
    table_id: str | None = None,
    origin: str | None = None,
    engine: str | None = None,
    confidence: float | None = None,
) -> CellPlan:
    display = normalized.display
    if len(display) > MAX_CELL_CHARS:
        display = display[: MAX_CELL_CHARS - 1] + "…"
    return CellPlan(
        value=normalized.value,
        display=display,
        value_type=normalized.value_type,
        header=header,
        original=normalized.original,
        number_format=normalized.number_format,
        transformations=normalized.transformations,
        element_id=element_id,
        page_number=page_number,
        bbox=bbox,
        origin=origin,
        # `engine` names the reader that produced the characters (pdfplumber,
        # glm-ocr, ...) when it is known; the resolver only decided the layout.
        engine=engine or ("semantic-field-resolver" if field_label else "table-builder"),
        confidence=confidence if confidence is not None else normalized.confidence,
        record_id=record_id,
        field_label=field_label,
        table_id=table_id,
    )


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-zà-ÿ]+", (text or "").lower()) if w]


def _consumed_words(plan: WorkbookPlan) -> dict[int, dict[str, int]]:
    """Word multiset already delivered to record/table cells, per page."""
    counters: dict[int, dict[str, int]] = {}
    for sheet in plan.sheets:
        if sheet.kind not in {SHEET_RECORDS, SHEET_TABLE}:
            continue
        for row in sheet.rows:
            for cell in row:
                if cell is None or cell.page_number is None:
                    continue
                text = str(cell.original if cell.original is not None else cell.display or "")
                counter = counters.setdefault(cell.page_number, {})
                for word in _words(text):
                    counter[word] = counter.get(word, 0) + 1
    return counters


def _mostly_duplicated(text: str, counter: dict[str, int], *, threshold: float = 0.8) -> bool:
    """True when the text adds nothing that is not already in a cell."""
    words = _words(text)
    if not words:
        return True
    if not counter:
        return False
    available = dict(counter)
    matched = 0
    for word in words:
        if available.get(word, 0) > 0:
            available[word] -= 1
            matched += 1
    return matched / len(words) >= threshold


def _field_boxes(records: list[Record]) -> dict[int, list[BBox]]:
    boxes: dict[int, list[BBox]] = {}
    for record in records:
        for item in record.fields:
            boxes.setdefault(item.page_number, []).append(item.bbox)
    return boxes


def _covered_by_fields(table: TableStructure, boxes: list[BBox]) -> bool:
    """True when a table is just the same region the field engine already read."""
    area = table.bbox.area
    if area <= 0 or not boxes:
        return False
    covered = sum(table.bbox.overlap_area(box) for box in boxes)
    return covered / area >= 0.6


def _build_table_sheets(
    tables: list[TableStructure],
    locale: LocaleProfile,
    used_names: set[str],
    recorder: AuditRecorder | None,
    block_for,
    field_boxes: dict[int, list[BBox]] | None = None,
) -> list[SheetPlan]:
    field_boxes = field_boxes or {}
    sheets: list[SheetPlan] = []
    index = 0
    for table in tables:
        if table.strategy == "transposed":
            continue
        if not any(any(cell.strip() for cell in row) for row in table.rows):
            # A table with no data row would become a sheet containing only its
            # header: an empty sheet is a defect, not an output.
            continue
        if _covered_by_fields(table, field_boxes.get(table.page_number, [])):
            # The same region already became structured fields: exporting it
            # again as a table would duplicate every value in the workbook.
            continue
        index += 1
        name = _sanitize_sheet_name(table.title or f"Tabela {index}", used_names)
        headers = [canonical_header(h) for h in table.headers] or [
            f"Coluna {i + 1}" for i in range(table.n_cols)
        ]
        headers = _unique_headers(headers)
        sheet = SheetPlan(
            name=name,
            kind=SHEET_TABLE,
            headers=headers,
            source_pages=sorted({p for p in table.row_pages}) or [table.page_number],
            table_id=table.table_id,
        )
        merged_pages = sorted({p for p in table.row_pages}) or [table.page_number]
        single_page = len(merged_pages) <= 1
        for row_index, row in enumerate(table.rows):
            row_page = (
                table.row_pages[row_index]
                if row_index < len(table.row_pages)
                else table.page_number
            )
            cells: list[CellPlan | None] = []
            for column_index in range(len(headers)):
                raw = row[column_index] if column_index < len(row) else ""
                if not raw.strip():
                    cells.append(None)
                    continue
                cell_bbox = (
                    _cell_bbox(table, row_index, column_index) if single_page else table.bbox
                )
                normalized = normalize(raw, header=headers[column_index], locale=locale)
                cells.append(
                    _cell_from_normalized(
                        normalized,
                        header=headers[column_index],
                        record_id=None,
                        field_label=None,
                        element_id=block_for(row_page, cell_bbox),
                        page_number=row_page,
                        bbox=cell_bbox,
                        table_id=table.table_id,
                    )
                )
            sheet.rows.append(cells)
        sheets.append(sheet)
        if recorder:
            recorder.emit(
                EVT_SHEET_CREATED,
                "normalize",
                f"Aba '{sheet.name}' criada a partir da tabela {table.table_id} ({table.strategy})",
                page=table.page_number,
                payload={
                    "sheet": sheet.name,
                    "kind": sheet.kind,
                    "table_id": table.table_id,
                    "rows": sheet.n_rows,
                    "columns": headers,
                    "strategy": table.strategy,
                },
            )
    return sheets


def _cell_bbox(table: TableStructure, row_index: int, column_index: int) -> BBox:
    """Cell provenance box.

    Row boxes are not aligned with the final data rows (the header row and
    continuation rows are removed), so the cell geometry uses the column span
    plus the table's vertical extent.  The table id and the cell address remain
    the precise provenance keys.
    """
    if table.column_boxes and column_index < len(table.column_boxes):
        column = table.column_boxes[column_index]
        if column.width > 0:
            return BBox(column.x0, table.bbox.y0, column.x1, table.bbox.y1)
    return table.bbox


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def _mark_structural_destinations(ir: DocumentIR, plan: WorkbookPlan) -> set[str]:
    """Mark blocks consumed by records/tables; return consumed element ids."""
    consumed: set[str] = set()
    cell_index: dict[str, tuple[str, str]] = {}
    consumed_boxes: dict[int, list[BBox]] = {}
    for sheet in plan.sheets:
        if sheet.kind not in {SHEET_RECORDS, SHEET_TABLE}:
            continue
        for row_index, row in enumerate(sheet.rows, start=2):
            for col_index, cell in enumerate(row, start=1):
                if cell is None or not cell.element_id:
                    continue
                cell_index.setdefault(cell.element_id, (sheet.name, f"{column_letter(col_index)}{row_index}"))
                if cell.bbox is not None and cell.page_number is not None:
                    consumed_boxes.setdefault(cell.page_number, []).append(cell.bbox)

    for block in ir.all_blocks():
        location = cell_index.get(block.element_id)
        if location:
            block.destination = DEST_FIELD if _is_record_sheet(plan, location[0]) else DEST_TABLE
            block.destination_sheet, block.destination_cell = location
            # A block can hold several lines while only some of them produced a
            # field/cell.  Consume it only when every line was placed; the rest
            # is preserved line by line in the text sheet (no silent loss).
            boxes = consumed_boxes.get(block.page_number, [])
            if not _residual_text(block, boxes):
                consumed.add(block.element_id)
            continue
        if block.kind in {"header", "footer"}:
            block.destination = DEST_HEADER_FOOTER
            consumed.add(block.element_id)
            continue
        if block.kind == "title":
            block.destination = DEST_METADATA
            consumed.add(block.element_id)
    return consumed


def _consumed_boxes(plan: WorkbookPlan) -> dict[int, list[BBox]]:
    """Source boxes already delivered to record/table cells, by page."""
    boxes: dict[int, list[BBox]] = {}
    for sheet in plan.sheets:
        if sheet.kind not in {SHEET_RECORDS, SHEET_TABLE}:
            continue
        for row in sheet.rows:
            for cell in row:
                if cell is None or cell.bbox is None or cell.page_number is None:
                    continue
                boxes.setdefault(cell.page_number, []).append(cell.bbox)
    return boxes


def _residual_text(block: TextBlock, consumed_boxes: list[BBox]) -> str:
    """Text of the block's lines that were not placed in a record/table cell."""
    if not block.lines or not consumed_boxes:
        return ""
    kept: list[str] = []
    for line in block.lines:
        if not line.text.strip():
            continue
        area = line.bbox.area
        if area <= 0:
            continue
        # A line may span several cells (one per column): the coverage is the
        # sum over every consumed box, not the best single box, otherwise no
        # multi-column row would ever count as placed.
        covered = sum(line.bbox.overlap_area(box) for box in consumed_boxes) / area
        if covered < 0.6:
            kept.append(line.text)
    return " ".join(kept).strip()


def _is_record_sheet(plan: WorkbookPlan, name: str) -> bool:
    return any(s.name == name and s.kind == SHEET_RECORDS for s in plan.sheets)


def _build_text_sheet(
    ir: DocumentIR,
    consumed: set[str],
    used_names: set[str],
    locale: LocaleProfile,
    consumed_boxes: dict[int, list[BBox]] | None = None,
    plan: WorkbookPlan | None = None,
) -> SheetPlan | None:
    consumed_boxes = consumed_boxes or {}
    consumed_words = _consumed_words(plan) if plan is not None else {}
    entries: list[tuple[TextBlock, str]] = []
    for block in ir.all_blocks():
        raw = block.text.strip()
        if not raw or not _is_meaningful_text(block.text):
            continue
        if block.element_id in consumed:
            # every line of this block was placed in a record/table cell
            continue
        if block.destination in {DEST_FIELD, DEST_TABLE}:
            # partially placed: only the lines outside the used cells remain
            residual = _residual_text(block, consumed_boxes.get(block.page_number, []))
            if not residual:
                continue
            if _mostly_duplicated(residual, consumed_words.get(block.page_number, {})):
                # Every word is already in a record/table cell: repeating it in
                # the text sheet would duplicate the workbook content.
                continue
            block.destination = DEST_TEXT
            entries.append((block, residual))
            continue
        full_text = " ".join(block.text.split())
        if _mostly_duplicated(full_text, consumed_words.get(block.page_number, {}), threshold=0.9):
            # Same words already delivered to cells: the sheet must not repeat
            # the workbook content.
            continue
        entries.append((block, full_text))
    if not entries:
        return None
    name = _sanitize_sheet_name("Texto", used_names)
    sheet = SheetPlan(name=name, kind=SHEET_TEXT, headers=["Texto", "Seção", "Página"])
    sections = section_map(ir.all_blocks())
    for block, text in sorted(entries, key=lambda item: (item[0].page_number, item[0].reading_order)):
        section = sections.get(block.element_id)
        # Free text stays text: a paragraph that happens to be all digits must
        # not become a numeric cell ("110001100011000" is not a quantity).
        normalized = NormalizedValue(
            value=text,
            display=text,
            value_type="text",
            original=text,
            number_format="@",
            confidence=0.9,
        )
        sheet.rows.append(
            [
                _cell_from_normalized(
                    normalized,
                    header="Texto",
                    record_id=None,
                    field_label=None,
                    element_id=block.element_id,
                    page_number=block.page_number,
                    bbox=block.bbox,
                    origin=block.origin,
                ),
                CellPlan(
                    value=section,
                    display=section or "",
                    value_type="text" if section else "empty",
                    header="Seção",
                    original=section,
                    number_format="@",
                    element_id=block.element_id,
                    page_number=block.page_number,
                    record_id=None,
                    field_label=None,
                ),
                CellPlan(
                    value=block.page_number,
                    display=str(block.page_number),
                    value_type="integer",
                    header="Página",
                    original=str(block.page_number),
                    number_format="#,##0",
                    element_id=block.element_id,
                    page_number=block.page_number,
                ),
            ]
        )
        sheet.source_pages = sorted(set(sheet.source_pages) | {block.page_number})
    return sheet


def _is_meaningful_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    letters = sum(ch.isalpha() for ch in stripped)
    return letters >= 3


def _build_unclassified_sheet(ir: DocumentIR, used_names: set[str]) -> SheetPlan | None:
    blocks = [
        block
        for block in ir.all_blocks()
        if block.destination is None and block.text.strip() and not _is_meaningful_text(block.text)
    ]
    if not blocks:
        return None
    name = _sanitize_sheet_name("Não classificado", used_names)
    sheet = SheetPlan(name=name, kind=SHEET_UNCLASSIFIED, headers=["Texto", "Página", "Motivo"])
    for block in blocks:
        sheet.rows.append(
            [
                CellPlan(
                    value=block.text.strip(),
                    display=block.text.strip(),
                    value_type="text",
                    header="Texto",
                    original=block.text,
                    number_format="@",
                    element_id=block.element_id,
                    page_number=block.page_number,
                ),
                CellPlan(
                    value=block.page_number,
                    display=str(block.page_number),
                    value_type="integer",
                    header="Página",
                    original=str(block.page_number),
                    number_format="#,##0",
                ),
                CellPlan(
                    value="Sem estrutura reconhecida",
                    display="Sem estrutura reconhecida",
                    value_type="text",
                    header="Motivo",
                    original="Sem estrutura reconhecida",
                    number_format="@",
                ),
            ]
        )
    return sheet


def _mark_remaining_destinations(
    ir: DocumentIR,
    text_sheet: SheetPlan | None,
    unclassified: SheetPlan | None,
    plan: WorkbookPlan,
) -> None:
    text_cells: dict[str, str] = {}
    if text_sheet is not None:
        for row_index, row in enumerate(text_sheet.rows, start=2):
            if row and row[0] and row[0].element_id:
                text_cells[row[0].element_id] = f"A{row_index}"
    unclassified_cells: dict[str, str] = {}
    if unclassified is not None:
        for row_index, row in enumerate(unclassified.rows, start=2):
            if row and row[0] and row[0].element_id:
                unclassified_cells[row[0].element_id] = f"A{row_index}"

    for block in ir.all_blocks():
        if block.destination is not None:
            continue
        if block.element_id in text_cells:
            block.destination = DEST_TEXT
            block.destination_sheet = text_sheet.name if text_sheet else None
            block.destination_cell = text_cells[block.element_id]
        elif block.element_id in unclassified_cells:
            block.destination = DEST_UNCLASSIFIED
            block.destination_sheet = unclassified.name if unclassified else None
            block.destination_cell = unclassified_cells[block.element_id]
        elif block.text.strip():
            block.destination = DEST_TEXT
            block.destination_sheet = text_sheet.name if text_sheet else None
        else:
            block.destination = DEST_UNCLASSIFIED


# --------------------------------------------------------------------------- #
# Names, limits, helpers
# --------------------------------------------------------------------------- #


def _sheet_name_for_group(group: _RecordGroup, index: int, used_names: set[str]) -> str:
    if group.section:
        return _sanitize_sheet_name(group.section, used_names)
    if len(group.records) > 1:
        return _sanitize_sheet_name("Registros", used_names)
    return _sanitize_sheet_name("Dados", used_names)


def _sanitize_sheet_name(name: str, used_names: set[str]) -> str:
    base = _INVALID_SHEET.sub(" ", (name or "Planilha").strip())
    base = " ".join(base.split())[:31].strip() or "Planilha"
    candidate = base
    counter = 2
    while candidate.lower() in used_names:
        suffix = f" ({counter})"
        candidate = (base[: 31 - len(suffix)] + suffix).strip()
        counter += 1
    used_names.add(candidate.lower())
    return candidate


def _document_title(ir: DocumentIR) -> str | None:
    title = ir.metadata.get("Title") or ir.metadata.get("title")
    if title and str(title).strip():
        return str(title).strip()[:200]
    return None


def _reindex(plan: WorkbookPlan) -> None:
    for index, sheet in enumerate(plan.sheets):
        sheet.order = index
        if not sheet.headers:
            width = max((len(row) for row in sheet.rows), default=0)
            sheet.headers = [f"Coluna {i + 1}" for i in range(width)]


def _apply_excel_limits(plan: WorkbookPlan, recorder: AuditRecorder | None = None) -> None:
    expanded: list[SheetPlan] = []
    for sheet in plan.sheets:
        dropped_columns = max(0, len(sheet.headers) - MAX_EXCEL_COLS)
        if dropped_columns and recorder is not None:
            # XLSX has 16,384 columns: the overflow cannot be represented, so it
            # is reported with its size instead of vanishing.
            recorder.issue(
                "SHEET_COLUMNS_EXCEEDED",
                f"A aba '{sheet.name}' tinha {len(sheet.headers)} colunas; as "
                f"{dropped_columns} excedentes do limite XLSX não foram exportadas.",
                severity="warning",
                context={"limit": MAX_EXCEL_COLS, "dropped": dropped_columns},
            )
        if len(sheet.rows) <= MAX_EXCEL_ROWS and len(sheet.headers) <= MAX_EXCEL_COLS:
            expanded.append(sheet)
            continue
        chunk_size = MAX_EXCEL_ROWS
        for chunk_index in range(0, len(sheet.rows), chunk_size):
            chunk = sheet.rows[chunk_index : chunk_index + chunk_size]
            index = chunk_index // chunk_size + 1
            expanded.append(
                SheetPlan(
                    name=f"{sheet.name[:24]} {index}",
                    kind=sheet.kind,
                    headers=list(sheet.headers[:MAX_EXCEL_COLS]),
                    rows=[row[:MAX_EXCEL_COLS] for row in chunk],
                    source_pages=list(sheet.source_pages),
                    table_id=sheet.table_id,
                    notes=sheet.notes,
                )
            )
    plan.sheets = expanded


__all__ = ["build_workbook", "ResolutionResult"]
