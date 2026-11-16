"""Hybrid table strategy: real horizontal rules + alignment-derived columns.

Many reports draw only the row separators (or only the outer frame).  Lattice
alone fails because it needs a full grid; stream alone loses the exact row
boundaries.  The hybrid combines them: rows come from the drawn rules, columns
from repeated text alignment.
"""

from __future__ import annotations

from models.document import BBox, Token, median

from .lattice import RawCell, RawTable
from .stream import detect_stream, _cluster_segments, _rows, _split_segments


def _candidate_from_rules(
    page,
    tokens: list[Token],
    page_number: int,
    page_width: float,
    h_rules: list[float],
) -> RawTable | None:
    """Column boxes for a ruled band, derived from the text inside it."""
    if len(h_rules) < 3:
        return None
    top, bottom = min(h_rules), max(h_rules)
    inside = [t for t in tokens if top - 1 <= t.bbox.y_center <= bottom + 1]
    if len(inside) < 4:
        return None
    rows = _rows(inside)
    gap = max(9.0, page_width * 0.014)
    seg_rows = []
    for row in rows:
        segments = _split_segments(row, gap)
        if len(segments) >= 2:
            seg_rows.append((row, segments))
    if len(seg_rows) < 2:
        return None
    columns = _cluster_segments(seg_rows)
    if len(columns) < 2:
        return None
    boxes = [BBox(c.x0, top, c.x1, bottom) for c in columns]
    return RawTable(
        page_number=page_number,
        bbox=BBox(min(b.x0 for b in boxes), top, max(b.x1 for b in boxes), bottom),
        rows=[],
        col_boxes=boxes,
        strategy="hybrid",
        engine="rules-plus-alignment",
        confidence=0.6,
    )


def detect_hybrid(
    page,
    page_number: int,
    tokens: list[Token],
    *,
    page_width: float = 595.0,
    h_rules: list[float] | None = None,
) -> list[RawTable]:
    try:
        edges = list(getattr(page, "edges", []) or [])
    except Exception:  # noqa: BLE001
        return []

    stream_tables = detect_stream(
        tokens, page_number, page_width=page_width, h_rules=h_rules
    )
    if not stream_tables:
        # Partially bordered table: horizontal rules exist but the alignment
        # detector found nothing (few rows, uneven cells). Columns are derived
        # here from the text inside the ruled band, so the hybrid strategy does
        # not depend on stream succeeding first.
        synthetic = _candidate_from_rules(page, tokens, page_number, page_width, h_rules or [])
        if synthetic is not None:
            stream_tables = [synthetic]

    results: list[RawTable] = []
    for candidate in stream_tables:
        ys = _horizontal_rules(edges, candidate.bbox)
        if len(ys) < 3:
            continue
        col_boxes = candidate.col_boxes
        if len(col_boxes) < 2:
            continue
        rows: list[list[str]] = []
        cells: list[RawCell] = []
        row_boxes: list[BBox] = []
        for r_index, (y0, y1) in enumerate(zip(ys, ys[1:])):
            if y1 - y0 < 3:
                continue
            values: list[str] = []
            boxes: list[BBox] = []
            for c_index, col in enumerate(col_boxes):
                box = BBox(col.x0, y0, col.x1, y1)
                text = _text_in(tokens, box)
                values.append(text)
                boxes.append(box)
                cells.append(RawCell(r_index, c_index, text, box))
            rows.append(values)
            row_boxes.append(BBox.union_all(boxes))
        if len(rows) < 2:
            continue
        filled = sum(1 for row in rows for cell in row if cell.strip())
        density = filled / max(1, len(rows) * len(col_boxes))
        if density < 0.3:
            continue
        results.append(
            RawTable(
                page_number=page_number,
                bbox=BBox.union_all(row_boxes),
                rows=rows,
                cells=cells,
                row_boxes=row_boxes,
                col_boxes=col_boxes,
                strategy="hybrid",
                engine="lines+alignment",
                confidence=round(0.7 + 0.2 * density, 3),
            )
        )
    return results


def _horizontal_rules(edges: list[dict], bbox: BBox) -> list[float]:
    ys: list[float] = []
    for edge in edges:
        try:
            if edge.get("orientation") != "h":
                continue
            top = float(edge["top"])
            x0 = float(edge["x0"])
            x1 = float(edge["x1"])
        except (KeyError, TypeError, ValueError):
            continue
        if top < bbox.y0 - 3 or top > bbox.y1 + 3:
            continue
        if x1 < bbox.x0 or x0 > bbox.x1:
            continue
        ys.append(top)
    if not ys:
        return []
    ys.sort()
    clustered: list[list[float]] = [[ys[0]]]
    for value in ys[1:]:
        if value - clustered[-1][-1] <= 2.5:
            clustered[-1].append(value)
        else:
            clustered.append([value])
    return [round(median(c), 2) for c in clustered]


def _text_in(tokens: list[Token], box: BBox) -> str:
    inside = [t for t in tokens if box.contains_point(t.bbox.x_center, t.bbox.y_center, tol=0.5)]
    inside.sort(key=lambda t: (round(t.bbox.y_center, 1), t.bbox.x0))
    return " ".join(t.text for t in inside).strip()


__all__ = ["detect_hybrid"]
