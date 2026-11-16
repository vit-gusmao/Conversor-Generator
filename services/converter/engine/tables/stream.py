"""Table detection — stream / alignment strategy (no drawn borders).

Approach: split every text row into *segments* at wide horizontal gaps, then
cluster segments across rows into columns.  Clustering segments (rather than
single word coordinates) handles both left-aligned text and right-aligned
numbers, which is the common failure mode of naive anchor histograms.

A block is only accepted as a table when:

* at least 4 rows produce at least 2 segments each;
* at least 2 columns appear in at least 40% of the rows;
* the block does not look like a form (`Label:` markers);
* cell density is high enough (a paragraph does not become a table).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.tables.lattice import RawTable
from models.document import BBox, Token, median

MIN_ROWS = 4
MIN_COLS = 2


@dataclass(slots=True)
class _Row:
    words: list[Token]
    bbox: BBox
    y: float


@dataclass(slots=True)
class _Segment:
    words: list[Token]
    bbox: BBox
    row_index: int = 0


@dataclass(slots=True)
class _Column:
    x0: float
    x1: float
    count: int = 0

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2.0


def _rows(tokens: list[Token]) -> list[_Row]:
    if not tokens:
        return []
    heights = [t.bbox.height for t in tokens if t.bbox.height > 0]
    tol = max(1.6, (median(heights) or 8.0) * 0.55)
    ordered = sorted(tokens, key=lambda t: (t.bbox.y_center, t.bbox.x0))
    rows: list[list[Token]] = []
    centers: list[float] = []
    for token in ordered:
        y = token.bbox.y_center
        placed = False
        for index, center in enumerate(centers):
            if abs(center - y) <= tol:
                rows[index].append(token)
                centers[index] = sum(t.bbox.y_center for t in rows[index]) / len(rows[index])
                placed = True
                break
        if not placed:
            rows.append([token])
            centers.append(y)
    result = []
    for row_tokens in rows:
        row_tokens.sort(key=lambda t: t.bbox.x0)
        box = BBox.union_all(t.bbox for t in row_tokens)
        result.append(_Row(row_tokens, box, box.y_center))
    result.sort(key=lambda r: r.y)
    return result


def _split_segments(row: _Row, gap_threshold: float) -> list[_Segment]:
    if not row.words:
        return []
    groups: list[list[Token]] = [[row.words[0]]]
    for previous, word in zip(row.words, row.words[1:]):
        gap = word.bbox.x0 - previous.bbox.x1
        if gap >= gap_threshold:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [
        _Segment(words=group, bbox=BBox.union_all(w.bbox for w in group)) for group in groups
    ]


def _belongs(segment: _Segment, column: _Column) -> bool:
    """Same column if the horizontal ranges overlap substantially."""
    overlap = max(0.0, min(segment.bbox.x1, column.x1) - max(segment.bbox.x0, column.x0))
    width = min(segment.bbox.width, column.x1 - column.x0)
    if width > 0 and overlap / width >= 0.35:
        return True
    if abs(segment.bbox.x1 - column.x1) <= 6.0:
        return True
    return abs(segment.bbox.x0 - column.x0) <= 6.0


def _cluster_segments(seg_rows: list[tuple[_Row, list[_Segment]]]) -> list[_Column]:
    columns: list[_Column] = []
    for _row, segments in seg_rows:
        for segment in segments:
            target = None
            for column in columns:
                if _belongs(segment, column):
                    target = column
                    break
            if target is None:
                columns.append(_Column(x0=segment.bbox.x0, x1=segment.bbox.x1, count=1))
                continue
            target.count += 1
            target.x0 = min(target.x0, segment.bbox.x0)
            target.x1 = max(target.x1, segment.bbox.x1)
    columns.sort(key=lambda c: c.center)
    return columns


def detect_stream(
    tokens: list[Token],
    page_number: int,
    *,
    page_width: float = 595.0,
    h_rules: list[float] | None = None,
) -> list[RawTable]:
    """Detect borderless tables, one candidate per vertically contiguous band.

    Columns are clustered **inside each band**, never across the page: mixing a
    two-column address block with the invoice table below it used to produce a
    grid whose columns matched nothing (every value landed in the wrong cell).
    """
    rows = _rows(tokens)
    if len(rows) < MIN_ROWS:
        return []

    heights = [r.bbox.height for r in rows if r.bbox.height > 0] or [10.0]
    median_height = median(heights)
    gap_threshold = max(9.0, page_width * 0.014)

    tables: list[RawTable] = []
    for band in _vertical_bands(rows, median_height * 2.2, h_rules or []):
        table = _table_from_band(band, page_number, gap_threshold)
        if table is not None:
            tables.append(table)
    return tables


def _vertical_bands(
    rows: list[_Row], max_gap: float, h_rules: list[float]
) -> list[list[_Row]]:
    """Split rows into blocks separated by a gap larger than one line.

    A drawn horizontal rule inside the gap means both sides belong to the same
    table (the usual "items above, totals below" layout), so the band is not
    split there.
    """
    bands: list[list[_Row]] = []
    current: list[_Row] = []
    for row in rows:
        if current:
            gap_start = current[-1].bbox.y1
            gap_end = row.bbox.y0
            gap = gap_end - gap_start
            if gap > max_gap:
                # A rule bridges the gap only when it is a separator *between*
                # the two blocks; a gap of several lines is a different region
                # even if a page border happens to sit inside it.
                bridged = gap <= max_gap * 4 and _rule_inside(h_rules, gap_start, gap_end)
                if not bridged:
                    bands.append(current)
                    current = []
        current.append(row)
    if current:
        bands.append(current)
    return bands


def _rule_inside(rules: list[float], start: float, end: float) -> bool:
    return any(start - 1.0 <= rule <= end + 1.0 for rule in rules)


def _table_from_band(
    band: list[_Row], page_number: int, gap_threshold: float
) -> RawTable | None:
    if len(band) < MIN_ROWS:
        return None

    seg_rows: list[tuple[_Row, list[_Segment]]] = []
    for row in band:
        segments = _split_segments(row, gap_threshold)
        if len(segments) >= 2:
            seg_rows.append((row, segments))
    if len(seg_rows) < MIN_ROWS:
        return None

    # Form documents mark every line with an explicit label ("Nome: valor").
    # A single "Total:" cell inside a real table must not disqualify it, so the
    # signal is the proportion of marked rows, not the presence of one marker.
    marked_rows = sum(
        1
        for row, _segments in seg_rows
        if any(word.text.strip().endswith((":", "=")) for word in row.words)
    )
    if marked_rows >= max(2, int(len(seg_rows) * 0.6)):
        return None

    columns = _cluster_segments(seg_rows)
    required = max(2, int(len(seg_rows) * 0.4))
    columns = [c for c in columns if c.count >= required]
    if len(columns) < MIN_COLS:
        return None

    grid: list[list[str]] = []
    row_boxes: list[BBox] = []
    for row, segments in seg_rows:
        values = ["" for _ in columns]
        for segment in segments:
            best = _best_column(segment, columns)
            if best is None:
                continue
            text = " ".join(w.text for w in segment.words)
            values[best] = (values[best] + " " + text).strip() if values[best] else text
        if sum(1 for value in values if value.strip()) < 2:
            continue
        grid.append(values)
        row_boxes.append(row.bbox)

    if len(grid) < MIN_ROWS:
        return None

    filled = sum(1 for row in grid for cell in row if cell.strip())
    density = filled / max(1, len(grid) * len(columns))
    if density < 0.4:
        return None
    if not _consistent_columns(grid) or not _looks_tabular(grid):
        return None

    return RawTable(
        page_number=page_number,
        bbox=BBox.union_all(row_boxes),
        rows=grid,
        row_boxes=row_boxes,
        col_boxes=[BBox(c.x0, row_boxes[0].y0, c.x1, row_boxes[-1].y1) for c in columns],
        strategy="stream",
        engine="alignment-clustering",
        confidence=round(min(0.95, 0.55 + 0.35 * density), 3),
    )


def _looks_tabular(rows: list[list[str]]) -> bool:
    """Reject prose laid out in visual columns.

    A two-column address block or a two-column article looks exactly like a
    borderless table to an alignment clustering algorithm: same rows, several
    segments per row.  What separates them is the content — a table has short
    cells and/or a numeric column, prose has long sentences in every column.
    """
    cells = [cell.strip() for row in rows for cell in row if cell.strip()]
    if not cells:
        return False
    numbers = sum(
        1
        for cell in cells
        if any(ch.isdigit() for ch in cell) and len(cell) <= 24 and not cell.endswith((".", ",", ";"))
    )
    if numbers / len(cells) >= 0.2:
        return True
    average_length = sum(len(cell) for cell in cells) / len(cells)
    shortest = min(len(cell) for cell in cells)
    # Prose: every cell is a long phrase. A text table (names, roles) still has
    # short cells somewhere.
    return average_length <= 30.0 or shortest <= 12


def _best_column(segment: _Segment, columns: list[_Column]) -> int | None:
    best: tuple[float, int] | None = None
    for index, column in enumerate(columns):
        if not _belongs(segment, column):
            continue
        overlap = max(0.0, min(segment.bbox.x1, column.x1) - max(segment.bbox.x0, column.x0))
        if best is None or overlap > best[0]:
            best = (overlap, index)
    if best is not None:
        return best[1]

    # A label can sit between two columns: right-aligned totals ("Subtotal" next
    # to the amount column) belong to the nearest column. Dropping the segment
    # silently deleted real content from the sheet.
    nearest: tuple[float, int] | None = None
    for index, column in enumerate(columns):
        distance = min(
            abs(segment.bbox.x0 - column.x1), abs(column.x0 - segment.bbox.x1)
        ) if segment.bbox.x1 <= column.x0 or segment.bbox.x0 >= column.x1 else 0.0
        if nearest is None or distance < nearest[0]:
            nearest = (distance, index)
    if nearest is not None and nearest[0] <= 120.0:
        return nearest[1]
    return None


def _consistent_columns(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    n_cols = max(len(r) for r in rows)
    if n_cols < MIN_COLS:
        return False
    counts = [0] * n_cols
    for row in rows:
        for index, cell in enumerate(row):
            if cell.strip():
                counts[index] += 1
    return sum(1 for count in counts if count >= max(2, len(rows) * 0.4)) >= MIN_COLS


__all__ = ["detect_stream"]
