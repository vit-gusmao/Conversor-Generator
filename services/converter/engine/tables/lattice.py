"""Table detection — lattice (borders) strategy.

Primary engine: `pdfplumber.find_tables` with the `lines` strategy, which is a
mature implementation of the geometric lattice algorithm (edges -> snap/join ->
intersections -> cells).

Secondary engine: an own grid builder used when pdfplumber returns nothing but
the page clearly has a drawn grid (e.g. partially recognised rectangles).  The
fallback is intentionally conservative: it only fires when at least two
vertical and two horizontal rules exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models.document import BBox, Token, median


@dataclass(slots=True)
class RawCell:
    row: int
    col: int
    text: str
    bbox: BBox
    rowspan: int = 1
    colspan: int = 1
    missing: bool = False


@dataclass(slots=True)
class RawTable:
    page_number: int
    bbox: BBox
    rows: list[list[str]]
    cells: list[RawCell] = field(default_factory=list)
    row_boxes: list[BBox] = field(default_factory=list)
    col_boxes: list[BBox] = field(default_factory=list)
    strategy: str = "lattice"
    engine: str = "pdfplumber"
    confidence: float = 0.0
    text_coverage: float = 0.0

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)


LATTICE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "snap_x_tolerance": 3,
    "snap_y_tolerance": 3,
    "join_tolerance": 3,
    "join_x_tolerance": 3,
    "join_y_tolerance": 3,
    "intersection_tolerance": 4,
    "intersection_x_tolerance": 4,
    "intersection_y_tolerance": 4,
    "edge_min_length": 8,
    "min_words_vertical": 1,
    "min_words_horizontal": 1,
    "text_x_tolerance": 2,
    "text_y_tolerance": 3,
}


def detect_lattice(page, page_number: int) -> list[RawTable]:
    tables: list[RawTable] = []
    try:
        found = page.find_tables(LATTICE_SETTINGS)
    except Exception:  # noqa: BLE001
        found = []

    for table in found:
        try:
            rows_raw = getattr(table, "rows", []) or []
        except Exception:  # noqa: BLE001
            continue
        if len(rows_raw) < 1:
            continue
        try:
            texts = table.extract() or []
        except Exception:  # noqa: BLE001
            texts = []

        rows: list[list[str]] = []
        cells: list[RawCell] = []
        row_boxes: list[BBox] = []
        col_edges: dict[int, list[float]] = {}

        for r_index, row in enumerate(rows_raw):
            # pdfplumber >= 0.11 exposes Row.cells as a list of bbox tuples
            # (or None for merged cells); the text comes from table.extract().
            raw_cells = list(getattr(row, "cells", []) or [])
            text_row = texts[r_index] if r_index < len(texts) else []
            values: list[str] = []
            boxes: list[BBox] = []
            for c_index, bbox in enumerate(raw_cells):
                text = ""
                if c_index < len(text_row) and text_row[c_index] is not None:
                    text = str(text_row[c_index]).strip()
                if bbox is None:
                    values.append("")
                    cells.append(RawCell(r_index, c_index, "", BBox.empty(), missing=True))
                    continue
                try:
                    x0, top, x1, bottom = bbox
                    box = BBox(float(x0), float(top), float(x1), float(bottom))
                except (TypeError, ValueError):
                    values.append(text)
                    continue
                values.append(text)
                boxes.append(box)
                cells.append(RawCell(r_index, c_index, text, box))
                col_edges.setdefault(c_index, []).extend([box.x0, box.x1])
            rows.append(values)
            row_boxes.append(BBox.union_all(boxes) if boxes else BBox.empty())

        if not rows or max(len(r) for r in rows) < 2:
            continue
        bbox = BBox(*[float(v) for v in table.bbox])
        col_boxes = [
            BBox(min(xs), bbox.y0, max(xs), bbox.y1) for xs in col_edges.values() if xs
        ]
        tables.append(
            RawTable(
                page_number=page_number,
                bbox=bbox,
                rows=rows,
                cells=cells,
                row_boxes=row_boxes,
                col_boxes=col_boxes,
                strategy="lattice",
                engine="pdfplumber",
                confidence=0.9,
            )
        )
    return tables


def detect_lattice_manual(page, page_number: int, tokens: list[Token]) -> list[RawTable]:
    """Own grid reconstruction from drawn edges (fallback)."""
    try:
        edges = list(getattr(page, "edges", []) or [])
    except Exception:  # noqa: BLE001
        return []
    if len(edges) < 4:
        return []

    page_box = _page_box(page)
    horizontal: list[float] = []
    vertical: list[float] = []
    for edge in edges:
        try:
            orientation = edge.get("orientation")
            if orientation == "h":
                position = float(edge["top"])
                if page_box is not None and not _inside(position, page_box[1], page_box[3]):
                    # Background frames are often drawn larger than the page
                    # (HTML->PDF renders); they are decoration, not table rules.
                    continue
                horizontal.append(position)
            elif orientation == "v":
                position = float(edge["x0"])
                if page_box is not None and not _inside(position, page_box[0], page_box[2]):
                    continue
                vertical.append(position)
        except (KeyError, TypeError, ValueError):
            continue

    h_lines = _cluster(horizontal, tolerance=2.5)
    v_lines = _cluster(vertical, tolerance=2.5)
    if len(h_lines) < 2 or len(v_lines) < 2:
        return []

    top, bottom = min(h_lines), max(h_lines)
    if bottom - top < 10:
        return []
    left, right = min(v_lines), max(v_lines)
    if right - left < 20:
        return []

    rows_data: list[list[str]] = []
    cells: list[RawCell] = []
    row_boxes: list[BBox] = []
    for y0, y1 in zip(h_lines, h_lines[1:]):
        if y1 - y0 < 4:
            continue
        # The index must be the position in `rows_data`, not in the raw pairs:
        # skipping thin rows used to shift every span by one.
        r_index = len(rows_data)
        values: list[str] = []
        boxes: list[BBox] = []
        for c_index, (x0, x1) in enumerate(zip(v_lines, v_lines[1:])):
            if x1 - x0 < 4:
                continue
            cell_box = BBox(x0, y0, x1, y1)
            text = _words_in_box(tokens, cell_box)
            values.append(text)
            boxes.append(cell_box)
            cells.append(RawCell(r_index, c_index, text, cell_box))
        if values:
            rows_data.append(values)
            row_boxes.append(BBox.union_all(boxes))

    if len(rows_data) < 2 or max(len(r) for r in rows_data) < 2:
        return []
    if page_box is not None and _is_page_frame(left, top, right, bottom, page_box):
        return []
    if page_box is not None and _has_giant_cells(rows_data, row_boxes, page_box):
        # A frame drawn around the content produces cells hundreds of points
        # tall: they are page decoration swept into a grid, not table cells.
        return []
    rows_data = _drop_empty(rows_data)
    if len(rows_data) < 2 or max(len(r) for r in rows_data) < 2:
        return []
    if _density(rows_data) < 0.4:
        return []
    if not _cells_hold_table_text(rows_data):
        return []
    bbox = BBox(left, top, right, bottom)
    return [
        RawTable(
            page_number=page_number,
            bbox=bbox,
            rows=rows_data,
            cells=cells,
            row_boxes=row_boxes,
            col_boxes=[BBox(x0, top, x1, bottom) for x0, x1 in zip(v_lines, v_lines[1:])],
            strategy="lattice",
            engine="native-grid",
            confidence=0.75,
        )
    ]


def _has_giant_cells(rows: list[list[str]], row_boxes: list[BBox], page_box) -> bool:
    page_height = max(page_box[3] - page_box[1], 1.0)
    for box in row_boxes:
        if box.is_empty:
            continue
        if box.height > page_height * 0.08:
            return True
    return False


def _drop_empty(rows: list[list[str]]) -> list[list[str]]:
    """Remove empty rows and columns left by the page frame."""
    kept_rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not kept_rows:
        return []
    width = max(len(row) for row in kept_rows)
    keep_columns = [
        index
        for index in range(width)
        if any(index < len(row) and row[index].strip() for row in kept_rows)
    ]
    return [[row[i] if i < len(row) else "" for i in keep_columns] for row in kept_rows]


def _density(rows: list[list[str]]) -> float:
    cells = len(rows) * max((len(r) for r in rows), default=0)
    if cells <= 0:
        return 0.0
    filled = sum(1 for row in rows for cell in row if cell.strip())
    return filled / cells


def _page_box(page) -> tuple[float, float, float, float] | None:
    try:
        x0, top, x1, bottom = page.bbox
        return float(x0), float(top), float(x1), float(bottom)
    except (AttributeError, TypeError, ValueError):
        return None


def _inside(position: float, low: float, high: float, tolerance: float = 2.0) -> bool:
    return (low - tolerance) <= position <= (high + tolerance)


def _is_page_frame(left: float, top: float, right: float, bottom: float, page_box) -> bool:
    """True when the "grid" is just the page frame (a background rectangle)."""
    page_x0, page_top, page_x1, page_bottom = page_box
    page_width = max(page_x1 - page_x0, 1.0)
    page_height = max(page_bottom - page_top, 1.0)
    return (
        (right - left) >= page_width * 0.95
        and (bottom - top) >= page_height * 0.95
    )


def _cells_hold_table_text(rows: list[list[str]], *, max_average: float = 60.0) -> bool:
    """Reject grids whose cells contain prose instead of short values.

    A background rectangle turned into a grid swallows whole paragraphs into a
    few giant cells. Real table cells are short: a label, a number, a name.
    """
    cells = [cell.strip() for row in rows for cell in row if cell.strip()]
    if len(cells) < 4:
        return False
    average = sum(len(cell) for cell in cells) / len(cells)
    multi_line = sum(1 for cell in cells if cell.count("\n") >= 3)
    return average <= max_average and multi_line <= len(cells) * 0.3


def _cluster(values: list[float], tolerance: float) -> list[float]:
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [round(median(cluster), 2) for cluster in clusters]


def _words_in_box(tokens: list[Token], box: BBox) -> str:
    inside = [
        t
        for t in tokens
        if box.contains_point(t.bbox.x_center, t.bbox.y_center, tol=0.6)
    ]
    inside.sort(key=lambda t: (round(t.bbox.y_center, 1), t.bbox.x0))
    return " ".join(t.text for t in inside).strip()


__all__ = ["RawTable", "RawCell", "detect_lattice", "detect_lattice_manual", "LATTICE_SETTINGS"]
