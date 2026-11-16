"""Text artifact plan: the textual IR that feeds the TXT exporter.

The plan is derived from the page IR *before* tabular normalisation, so the TXT
file preserves the spelling found in the document.  It never contains an
invented title, a summary or model-generated prose.

Ordering rules:
* blocks keep the reading order already computed by the layout stage;
* tables are rendered as tab-separated lines at their vertical position;
* blocks that overlap a detected table are not repeated (the table owns them);
* a failed page has no text but is kept in the manifest as a failure;
* a genuinely blank page keeps its separator without being reported as loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.document import DocumentIR, PageResult, TableStructure, TextBlock


@dataclass(slots=True)
class TextSegment:
    text: str
    page_number: int
    kind: str  # text | table
    element_id: str | None = None
    table_id: str | None = None
    order: int = 0
    offset_start: int = 0
    offset_end: int = 0


@dataclass(slots=True)
class TextPage:
    page_number: int
    status: str
    segments: list[TextSegment] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(segment.text) for segment in self.segments)

    @property
    def has_text(self) -> bool:
        return any(segment.text.strip() for segment in self.segments)


@dataclass(slots=True)
class TextArtifactPlan:
    pages: list[TextPage]
    source_sha256: str | None = None
    source_name: str | None = None
    encoding: str = "utf-8"
    newline: str = "lf"
    page_separator: str = "form_feed"
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def segment_count(self) -> int:
        return sum(len(page.segments) for page in self.pages)

    def iter_segments(self):
        for page in self.pages:
            yield from page.segments


def build_text_plan(ir: DocumentIR, *, include_table_text: bool = True) -> TextArtifactPlan:
    pages: list[TextPage] = []
    warnings: list[dict[str, Any]] = []

    for page in sorted(ir.pages, key=lambda item: item.page_number):
        text_page = _page_plan(page, include_table_text=include_table_text)
        pages.append(text_page)
        if page.status == "failed":
            warning = {
                "code": "PAGE_NOT_REPRESENTED",
                "page_number": page.page_number,
                "message": (
                    f"A página {page.page_number} falhou no processamento e não possui "
                    "representação textual. Ela permanece registrada como falha."
                ),
            }
            text_page.warnings.append(warning)
            warnings.append(warning)
        elif not text_page.has_text and page.classification not in {"blank", "empty"}:
            warning = {
                "code": "PAGE_WITHOUT_TEXT",
                "page_number": page.page_number,
                "message": f"A página {page.page_number} não produziu texto recuperável.",
            }
            text_page.warnings.append(warning)
            warnings.append(warning)

    return TextArtifactPlan(
        pages=pages,
        source_sha256=ir.sha256,
        source_name=ir.file_name,
        warnings=warnings,
    )


def _page_plan(page: PageResult, *, include_table_text: bool) -> TextPage:
    result = TextPage(page_number=page.page_number, status=page.status)
    if page.status == "failed":
        return result

    table_boxes = [table.bbox for table in page.tables]
    blocks = [
        block
        for block in page.blocks
        if not any(block.bbox.overlap_ratio(box) >= 0.55 for box in table_boxes)
    ]

    items: list[tuple[tuple[float, int, float], str, Any]] = []
    for block in blocks:
        items.append(
            (
                (float(block.reading_order), 0, block.bbox.x0),
                "block",
                block,
            )
        )
    if include_table_text:
        for table in page.tables:
            anchor = sum(1 for block in blocks if block.bbox.y_center < table.bbox.y0)
            items.append(((float(anchor), 1, table.bbox.x0), "table", table))

    items.sort(key=lambda item: item[0])
    order = 0
    for _key, kind, item in items:
        if kind == "block":
            text = _block_text(item)
            if not text.strip():
                continue
            result.segments.append(
                TextSegment(
                    text=text,
                    page_number=page.page_number,
                    kind="text",
                    element_id=item.element_id,
                    order=order,
                )
            )
        else:
            text = _table_text(item)
            if not text.strip():
                continue
            result.segments.append(
                TextSegment(
                    text=text,
                    page_number=page.page_number,
                    kind="table",
                    table_id=item.table_id,
                    order=order,
                )
            )
        order += 1
    return result


def _block_sort_key(block: TextBlock) -> tuple[int, float, float]:
    return (block.reading_order, block.bbox.y0, block.bbox.x0)


def _block_text(block: TextBlock) -> str:
    if block.lines:
        lines = [line.text.rstrip() for line in block.lines if line.text.strip()]
        if lines:
            return "\n".join(lines)
    return (block.text or "").rstrip()


def _table_text(table: TableStructure) -> str:
    rows: list[list[str]] = []
    if table.headers:
        rows.append([_cell_text(cell) for cell in table.headers])
    for row in table.rows:
        rows.append([_cell_text(cell) for cell in row])
    cleaned = []
    for row in rows:
        while row and not row[-1].strip():
            row = row[:-1]
        if any(cell.strip() for cell in row):
            cleaned.append("\t".join(row))
    return "\n".join(cleaned)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", " ").replace("\n", " ").replace("\t", " ").strip()


def plan_from_payload(payload: dict[str, Any]) -> TextArtifactPlan:
    """Rebuild a stored plan (persisted as ``text_plan.json.gz``)."""
    pages = []
    for raw_page in payload.get("pages", []):
        segments = [
            TextSegment(
                text=str(item.get("text") or ""),
                page_number=int(raw_page.get("page_number") or 0),
                kind=str(item.get("kind") or "text"),
                element_id=item.get("element_id"),
                table_id=item.get("table_id"),
                order=int(item.get("order") or 0),
            )
            for item in raw_page.get("segments", [])
        ]
        pages.append(
            TextPage(
                page_number=int(raw_page.get("page_number") or 0),
                status=str(raw_page.get("status") or "ok"),
                segments=segments,
                warnings=list(raw_page.get("warnings") or []),
            )
        )
    return TextArtifactPlan(
        pages=pages,
        source_sha256=payload.get("source_sha256"),
        source_name=payload.get("source_name"),
        encoding=str(payload.get("encoding") or "utf-8"),
        newline=str(payload.get("newline") or "lf"),
        page_separator=str(payload.get("page_separator") or "form_feed"),
        warnings=list(payload.get("warnings") or []),
    )


__all__ = ["TextSegment", "TextPage", "TextArtifactPlan", "build_text_plan", "plan_from_payload"]
