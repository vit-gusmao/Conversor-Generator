"""Universal intermediate representation (IR).

Every extractor (native text, OCR, form fields, table engines, layout models)
converts its output into these objects **before** anything reaches the Excel
writer.  This is the contract that makes the converter auditable: an
`element_id` produced here can be followed all the way to a cell in the
workbook.

The structures are plain dataclasses (not Pydantic) on purpose: a large PDF can
produce tens of thousands of tokens and validation overhead per token is pure
cost.  Pydantic is used at the API boundary only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# String enums kept as constants so they can be persisted without migrations
# --------------------------------------------------------------------------- #

ORIGIN_NATIVE = "native"
ORIGIN_OCR = "ocr"
ORIGIN_FORM = "form_field"

REGION_HEADER = "header"
REGION_FOOTER = "footer"
REGION_TITLE = "title"
REGION_FORM = "form"
REGION_TABLE = "table"
REGION_PARAGRAPH = "paragraph"
REGION_LIST = "list"
REGION_IMAGE = "image"
REGION_UNKNOWN = "unknown"

SHEET_RECORDS = "records"
SHEET_TABLE = "table"
SHEET_TEXT = "text"
SHEET_UNCLASSIFIED = "unclassified"
SHEET_METADATA = "metadata"
#: Derived sheet that repeats every record/table row with the union of all
#: columns, so the whole document can be filtered/sorted in one place.  It is
#: built from the other sheets, never from new extraction.
SHEET_CONSOLIDATED = "consolidated"

DEST_FIELD = "field"
DEST_TABLE = "table"
DEST_TEXT = "text"
DEST_METADATA = "metadata"
DEST_HEADER_FOOTER = "header_footer"
DEST_UNCLASSIFIED = "unclassified"
DEST_DROPPED = "dropped"


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class BBox:
    """Axis-aligned box in PDF points, origin at the top-left (visual coords)."""

    x0: float
    y0: float
    x1: float
    y1: float

    # -- construction ------------------------------------------------------
    @staticmethod
    def empty() -> "BBox":
        return BBox(0.0, 0.0, 0.0, 0.0)

    @staticmethod
    def from_ltrb(l: float, t: float, r: float, b: float) -> "BBox":
        return BBox(min(l, r), min(t, b), max(l, r), max(t, b))

    @staticmethod
    def union_all(boxes: Iterable["BBox"]) -> "BBox":
        items = [b for b in boxes if b is not None and not b.is_empty]
        if not items:
            return BBox.empty()
        return BBox(
            min(b.x0 for b in items),
            min(b.y0 for b in items),
            max(b.x1 for b in items),
            max(b.y1 for b in items),
        )

    # -- properties --------------------------------------------------------
    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def is_empty(self) -> bool:
        return self.width <= 0.01 or self.height <= 0.01

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2.0

    # -- operations --------------------------------------------------------
    def union(self, other: "BBox") -> "BBox":
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def expand(self, dx: float, dy: float | None = None) -> "BBox":
        dy = dx if dy is None else dy
        return BBox(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def intersection(self, other: "BBox") -> "BBox":
        return BBox(
            max(self.x0, other.x0),
            max(self.y0, other.y0),
            min(self.x1, other.x1),
            min(self.y1, other.y1),
        )

    def overlap_area(self, other: "BBox") -> float:
        inter = self.intersection(other)
        return 0.0 if inter.is_empty else inter.area

    def overlap_ratio(self, other: "BBox") -> float:
        """Fraction of *this* box covered by `other`."""
        if self.area <= 0:
            return 0.0
        return min(1.0, self.overlap_area(other) / self.area)

    def vertical_overlap(self, other: "BBox") -> float:
        top = max(self.y0, other.y0)
        bottom = min(self.y1, other.y1)
        return max(0.0, bottom - top)

    def horizontal_gap(self, other: "BBox") -> float:
        """Horizontal distance; negative when the boxes overlap horizontally."""
        if other.x0 >= self.x1:
            return other.x0 - self.x1
        if self.x0 >= other.x1:
            return self.x0 - other.x1
        return -min(self.x1, other.x1) + max(self.x0, other.x0)

    def vertical_gap(self, other: "BBox") -> float:
        """Vertical distance; negative when the boxes overlap vertically."""
        if other.y0 >= self.y1:
            return other.y0 - self.y1
        if self.y0 >= other.y1:
            return self.y0 - other.y1
        return -min(self.y1, other.y1) + max(self.y0, other.y0)

    def contains_point(self, x: float, y: float, tol: float = 0.0) -> bool:
        return (
            self.x0 - tol <= x <= self.x1 + tol
            and self.y0 - tol <= y <= self.y1 + tol
        )

    def contains(self, other: "BBox", tol: float = 0.5) -> bool:
        return (
            other.x0 >= self.x0 - tol
            and other.y0 >= self.y0 - tol
            and other.x1 <= self.x1 + tol
            and other.y1 <= self.y1 + tol
        )

    def to_list(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Token:
    """A word (or OCR unit) with geometry and provenance."""

    element_id: str
    text: str
    bbox: BBox
    page_number: int
    origin: str = ORIGIN_NATIVE
    font: str | None = None
    size: float | None = None
    direction: str = "ltr"
    confidence: float | None = None
    index: int = 0
    engine: str = "pdfplumber"

    @property
    def is_numeric_like(self) -> bool:
        stripped = self.text.strip()
        if not stripped:
            return False
        digits = sum(c.isdigit() for c in stripped)
        return digits > 0 and digits / max(1, len(stripped)) >= 0.5


@dataclass(slots=True)
class Line:
    words: list[Token]
    bbox: BBox
    index: int = 0

    @property
    def text(self) -> str:
        return _join_words([w.text for w in self.words])

    @property
    def size(self) -> float:
        sizes = [w.size for w in self.words if w.size]
        return float(sum(sizes) / len(sizes)) if sizes else 0.0

    @property
    def font(self) -> str | None:
        fonts = [w.font for w in self.words if w.font]
        if not fonts:
            return None
        return max(set(fonts), key=fonts.count)

    @property
    def origin(self) -> str:
        origins = {w.origin for w in self.words}
        return ORIGIN_OCR if origins == {ORIGIN_OCR} else ORIGIN_NATIVE

    @property
    def direction(self) -> str:
        return self.words[0].direction if self.words else "ltr"

    @property
    def is_upright(self) -> bool:
        return all(w.direction in {"ltr", "rtl"} for w in self.words)


@dataclass(slots=True)
class TextBlock:
    """A logical text block (paragraph, form region, title, footer...)."""

    element_id: str
    page_number: int
    bbox: BBox
    text: str
    lines: list[Line] = field(default_factory=list)
    origin: str = ORIGIN_NATIVE
    font: str | None = None
    size: float | None = None
    direction: str = "ltr"
    confidence: float | None = None
    reading_order: int = 0
    kind: str = REGION_PARAGRAPH
    engine: str = "pdfplumber"
    #: filled by the resolver: where this block ended up
    destination: str | None = None
    destination_sheet: str | None = None
    destination_cell: str | None = None

    @property
    def is_consumed(self) -> bool:
        return self.destination is not None


@dataclass(slots=True)
class Region:
    region_id: str
    page_number: int
    kind: str
    bbox: BBox
    confidence: float
    engine: str = "layout-analyzer"
    resolution: str = "region"
    text_source: str = ORIGIN_NATIVE


# --------------------------------------------------------------------------- #
# Semantic structures
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Field:
    label: str
    value: str
    page_number: int
    label_bbox: BBox
    value_bbox: BBox
    element_ids: list[str] = field(default_factory=list)
    separator: str = "colon"  # colon | equals | geometry | below | column | form
    confidence: float = 0.0
    reason_codes: list[str] = field(default_factory=list)
    multiline: bool = False
    line_index: int = 0
    section: str | None = None

    @property
    def bbox(self) -> BBox:
        return self.label_bbox.union(self.value_bbox)


@dataclass(slots=True)
class Record:
    record_id: str
    page_number: int
    fields: list[Field] = field(default_factory=list)
    section: str | None = None
    bbox: BBox = field(default_factory=BBox.empty)
    confidence: float = 0.0
    origin: str = "fields"
    order: int = 0

    def finalize(self) -> "Record":
        self.bbox = BBox.union_all([f.bbox for f in self.fields])
        if self.fields:
            self.confidence = sum(f.confidence for f in self.fields) / len(self.fields)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "page_number": self.page_number,
            "section": self.section,
            "fields": [
                {"label": f.label, "value": f.value, "confidence": round(f.confidence, 3)}
                for f in self.fields
            ],
        }


@dataclass(slots=True)
class MergedSpan:
    row: int
    col: int
    rowspan: int
    colspan: int


@dataclass(slots=True)
class TableAlternative:
    strategy: str
    n_rows: int
    n_cols: int
    confidence: float
    accepted: bool
    reason: str
    coverage: float = 0.0


@dataclass(slots=True)
class TableCell:
    row: int
    col: int
    text: str
    bbox: BBox = field(default_factory=BBox.empty)
    rowspan: int = 1
    colspan: int = 1
    confidence: float | None = None
    origin: str = ORIGIN_NATIVE


@dataclass(slots=True)
class TableStructure:
    table_id: str
    page_number: int
    bbox: BBox
    strategy: str
    rows: list[list[str]]
    headers: list[str] = field(default_factory=list)
    cells: list[TableCell] = field(default_factory=list)
    merged: list[MergedSpan] = field(default_factory=list)
    row_kinds: list[str] = field(default_factory=list)
    confidence: float = 0.0
    alternatives: list[TableAlternative] = field(default_factory=list)
    continued_from: str | None = None
    continued_on_page: int | None = None
    transposed_from: str | None = None
    column_boxes: list[BBox] = field(default_factory=list)
    row_boxes: list[BBox] = field(default_factory=list)
    row_pages: list[int] = field(default_factory=list)
    title: str | None = None
    element_ids: list[str] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=len(self.headers))

    @property
    def data_rows(self) -> int:
        return sum(1 for k in self.row_kinds if k == "data") if self.row_kinds else max(
            0, len(self.rows) - (1 if self.headers else 0)
        )


# --------------------------------------------------------------------------- #
# Audit primitives (in-memory; persisted by repositories)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Issue:
    issue_id: str
    run_id: str
    severity: str
    code: str
    message: str
    page_number: int | None = None
    region_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Decision:
    decision_id: str
    stage: str
    subject: str
    accepted: bool
    algorithm: str
    reason_codes: list[str] = field(default_factory=list)
    alternatives: int = 0
    page_number: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CoverageRow:
    page_number: int
    text_blocks: int = 0
    tokens: int = 0
    assigned: int = 0
    preserved: int = 0
    header_footer: int = 0
    unclassified: int = 0
    dropped: int = 0

    @property
    def coverage_percent(self) -> float:
        if self.tokens <= 0:
            return 100.0
        assigned = self.assigned + self.preserved + self.unclassified + self.header_footer
        return round(100.0 * assigned / self.tokens, 2)


@dataclass(slots=True)
class EngineUsage:
    engine: str
    kind: str
    regions: int = 0
    pages: int = 0
    duration_ms: int = 0
    accepted: int = 0
    rejected: int = 0


@dataclass(slots=True)
class StageTiming:
    stage: str
    started_at: str
    finished_at: str | None = None
    duration_ms: int = 0
    pages: int = 0


# --------------------------------------------------------------------------- #
# Page / document
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PageResult:
    page_number: int
    width: float
    height: float
    rotation: int = 0
    classification: str = "native"
    status: str = "ok"
    text_source: str = ORIGIN_NATIVE
    native_chars: int = 0
    ocr_chars: int = 0
    tokens: list[Token] = field(default_factory=list)
    blocks: list[TextBlock] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    tables: list[TableStructure] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    coverage: CoverageRow | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    native_words: int = 0

    @property
    def has_text(self) -> bool:
        return bool(self.tokens)


@dataclass(slots=True)
class DocumentIR:
    file_name: str
    sha256: str
    page_count: int
    pdf_version: str | None = None
    pages: list[PageResult] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    tables: list[TableStructure] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    free_text_blocks: list[TextBlock] = field(default_factory=list)
    unclassified_blocks: list[TextBlock] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    engines: dict[str, EngineUsage] = field(default_factory=dict)
    ocr_regions: int = 0

    def all_tokens(self) -> list[Token]:
        return [t for p in self.pages for t in p.tokens]

    def all_blocks(self) -> list[TextBlock]:
        return [b for p in self.pages for b in p.blocks]


# --------------------------------------------------------------------------- #
# Workbook plan (output of the resolver, input of the Excel writer)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CellPlan:
    value: Any
    display: str
    value_type: str
    header: str
    original: str | None = None
    number_format: str | None = None
    transformations: list[dict[str, str]] = field(default_factory=list)
    element_id: str | None = None
    page_number: int | None = None
    bbox: BBox | None = None
    origin: str | None = None
    engine: str | None = None
    confidence: float | None = None
    record_id: str | None = None
    field_label: str | None = None
    table_id: str | None = None


@dataclass(slots=True)
class SheetPlan:
    name: str
    kind: str
    headers: list[str] = field(default_factory=list)
    rows: list[list[CellPlan | None]] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)
    table_id: str | None = None
    order: int = 0
    notes: str | None = None
    column_widths: dict[int, float] = field(default_factory=dict)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return len(self.headers)


@dataclass(slots=True)
class WorkbookPlan:
    sheets: list[SheetPlan] = field(default_factory=list)
    title: str | None = None

    def total_cells(self) -> int:
        """Number of cells that will actually carry a value."""
        return sum(
            1 for s in self.sheets for r in s.rows for c in r if c is not None and c.value is not None
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _join_words(words: list[str]) -> str:
    """Join words keeping a single space, but avoid inserting a space before
    punctuation that is attached to the preceding word."""
    out: list[str] = []
    for word in words:
        if not word:
            continue
        if not out:
            out.append(word)
            continue
        prev = out[-1]
        if word[0] in ",.;:!?%)]}»”’" or prev.endswith(("(", "[", "{", "«", "“", "‘", "/", "-")):
            out[-1] = prev + word
        else:
            out.append(word)
    return " ".join(out)


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


__all__ = [
    "BBox",
    "Token",
    "Line",
    "TextBlock",
    "Region",
    "Field",
    "Record",
    "TableCell",
    "TableStructure",
    "TableAlternative",
    "MergedSpan",
    "Issue",
    "Decision",
    "CoverageRow",
    "EngineUsage",
    "StageTiming",
    "PageResult",
    "DocumentIR",
    "CellPlan",
    "SheetPlan",
    "WorkbookPlan",
    "median",
    "stdev",
]
