"""Reading order reconstruction (recursive XY-cut).

A PDF does not have to store words in the order a human reads them.  A naive
extraction of a two-column page interleaves the columns; this module detects
whitespace rivers and rebuilds the correct order.

Important distinction, enforced downstream: **visual** columns determine reading
order only.  **Semantic** columns (attributes of a record) are decided by the
field/table engines.  Confusing the two produces a spreadsheet that looks like
the PDF but is structurally wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.document import BBox, Line, TextBlock, median

_MAX_DEPTH = 6


@dataclass(slots=True)
class ColumnSpan:
    index: int
    x0: float
    x1: float
    blocks: int


def assign_reading_order(
    blocks: list[TextBlock], page_width: float, page_height: float
) -> list[TextBlock]:
    if not blocks:
        return []
    ordered = _xy_cut(list(blocks), page_width, page_height, depth=0)
    for index, block in enumerate(ordered):
        block.reading_order = index
    return ordered


def _xy_cut(items: list[TextBlock], page_width: float, page_height: float, depth: int) -> list[TextBlock]:
    if len(items) <= 1 or depth >= _MAX_DEPTH:
        return sorted(items, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))

    vertical = _split_vertical(items, page_width)
    if vertical is not None:
        left, right = vertical
        return _xy_cut(left, page_width, page_height, depth + 1) + _xy_cut(
            right, page_width, page_height, depth + 1
        )

    horizontal = _split_horizontal(items, page_height)
    if horizontal is not None:
        top, bottom = horizontal
        return _xy_cut(top, page_width, page_height, depth + 1) + _xy_cut(
            bottom, page_width, page_height, depth + 1
        )

    return sorted(items, key=lambda b: (round(b.bbox.y0, 1), b.bbox.x0))


def _split_vertical(items: list[TextBlock], page_width: float):
    threshold = max(14.0, page_width * 0.035)
    candidates = sorted({round(b.bbox.x0, 1) for b in items} | {round(b.bbox.x1, 1) for b in items})
    best: tuple[float, list[TextBlock], list[TextBlock]] | None = None
    for cut in candidates:
        left = [b for b in items if b.bbox.x1 <= cut + 0.5]
        right = [b for b in items if b.bbox.x0 >= cut - 0.5]
        if not left or not right or len(left) + len(right) != len(items):
            continue
        gap = min(b.bbox.x0 for b in right) - max(b.bbox.x1 for b in left)
        if gap < threshold:
            continue
        # A real column split must not be a single small block floating to the side.
        if min(len(left), len(right)) == 1:
            small = left[0] if len(left) == 1 else right[0]
            if small.bbox.width < page_width * 0.12:
                continue
        if best is None or gap > best[0]:
            best = (gap, left, right)
    if best is None:
        return None
    return best[1], best[2]


def _split_horizontal(items: list[TextBlock], page_height: float):
    threshold = max(12.0, page_height * 0.018)
    candidates = sorted({round(b.bbox.y0, 1) for b in items} | {round(b.bbox.y1, 1) for b in items})
    best: tuple[float, list[TextBlock], list[TextBlock]] | None = None
    for cut in candidates:
        top = [b for b in items if b.bbox.y1 <= cut + 0.5]
        bottom = [b for b in items if b.bbox.y0 >= cut - 0.5]
        if not top or not bottom or len(top) + len(bottom) != len(items):
            continue
        gap = min(b.bbox.y0 for b in bottom) - max(b.bbox.y1 for b in top)
        if gap < threshold:
            continue
        if best is None or gap > best[0]:
            best = (gap, top, bottom)
    if best is None:
        return None
    return best[1], best[2]


# --------------------------------------------------------------------------- #
# Column detection for the field engine
# --------------------------------------------------------------------------- #


def detect_columns(lines: list[Line], page_width: float) -> list[ColumnSpan]:
    """Cluster line start positions into visual columns.

    Returned spans are intentionally coarse; the field engine refines them with
    per-line gap analysis.
    """
    if not lines:
        return []
    starts = sorted(line.bbox.x0 for line in lines if line.bbox.width > 2)
    if not starts:
        return []
    tolerance = max(8.0, page_width * 0.012)
    clusters: list[list[float]] = []
    for start in starts:
        if clusters and abs(start - median(clusters[-1])) <= tolerance:
            clusters[-1].append(start)
        else:
            clusters.append([start])

    spans: list[ColumnSpan] = []
    for index, cluster in enumerate(clusters):
        anchor = median(cluster)
        members = [l for l in lines if abs(l.bbox.x0 - anchor) <= tolerance * 1.6]
        if len(members) < 2:
            continue
        x1 = max(l.bbox.x1 for l in members)
        spans.append(ColumnSpan(index=len(spans), x0=anchor, x1=x1, blocks=len(members)))
    return spans


def split_line_by_gap(line: Line, min_gap: float) -> list[tuple[list, BBox]]:
    """Split a line into horizontal segments separated by a wide gap."""
    if not line.words:
        return []
    groups: list[list] = [[line.words[0]]]
    for previous, word in zip(line.words, line.words[1:]):
        gap = word.bbox.x0 - previous.bbox.x1
        if gap >= min_gap:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [(group, BBox.union_all(w.bbox for w in group)) for group in groups]


__all__ = [
    "assign_reading_order",
    "detect_columns",
    "split_line_by_gap",
    "ColumnSpan",
]
