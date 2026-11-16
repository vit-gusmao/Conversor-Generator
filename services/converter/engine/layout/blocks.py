"""Stage 5 — layout reconstruction: words -> lines -> blocks.

The output preserves geometry.  A block is a *visual* unit (a paragraph, a form
area, a title) — never a semantic one.  Semantic interpretation happens later
and must not rely on the order in which a PDF happened to store its operators.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.document import BBox, Line, TextBlock, Token, median


@dataclass(slots=True)
class LayoutResult:
    lines: list[Line]
    blocks: list[TextBlock]
    median_height: float
    median_size: float
    page_width: float
    page_height: float


def group_lines(tokens: list[Token], page_number: int, *, tolerance: float | None = None) -> list[Line]:
    if not tokens:
        return []
    heights = [t.bbox.height for t in tokens if t.bbox.height > 0]
    median_height = median(heights) or 8.0
    tol = tolerance if tolerance is not None else max(1.8, median_height * 0.62)

    ordered = sorted(tokens, key=lambda t: (t.bbox.y_center, t.bbox.x0))
    clusters: list[list[Token]] = []
    centers: list[float] = []
    for token in ordered:
        y = token.bbox.y_center
        best_index = -1
        best_distance = tol + 1.0
        for index, center in enumerate(centers):
            distance = abs(center - y)
            if distance <= tol and distance < best_distance:
                # require some vertical overlap, otherwise two adjacent rows merge
                cluster_box = BBox.union_all(t.bbox for t in clusters[index])
                overlap = cluster_box.vertical_overlap(token.bbox)
                if overlap >= min(cluster_box.height, token.bbox.height) * 0.45:
                    best_index = index
                    best_distance = distance
        if best_index >= 0:
            clusters[best_index].append(token)
            members = clusters[best_index]
            centers[best_index] = sum(t.bbox.y_center for t in members) / len(members)
        else:
            clusters.append([token])
            centers.append(y)

    lines: list[Line] = []
    for index, cluster in enumerate(sorted(clusters, key=lambda c: (min(t.bbox.y0 for t in c), min(t.bbox.x0 for t in c)))):
        cluster.sort(key=lambda t: t.bbox.x0)
        box = BBox.union_all(t.bbox for t in cluster)
        lines.append(Line(words=cluster, bbox=box, index=index))
    return lines


def build_blocks(
    lines: list[Line],
    page_number: int,
    *,
    page_width: float,
    page_height: float,
) -> list[TextBlock]:
    if not lines:
        return []
    heights = [line.bbox.height for line in lines if line.bbox.height > 0]
    median_height = median(heights) or 10.0
    sizes = [line.size for line in lines if line.size]
    median_size = median(sizes) or median_height

    ordered = sorted(lines, key=lambda line: (line.bbox.y0, line.bbox.x0))
    groups: list[list[Line]] = []
    for line in ordered:
        placed = False
        if groups:
            current = groups[-1]
            current_box = BBox.union_all(l.bbox for l in current)
            gap = current_box.vertical_gap(line.bbox)
            horizontal_overlap = _x_overlap_ratio(current_box, line.bbox)
            current_sizes = [l.size for l in current if l.size]
            current_size = median(current_sizes) if current_sizes else median_size
            size_changed = bool(line.size and current_size and abs(line.size - current_size) > 1.8)
            if size_changed and gap >= -0.5:
                # A visible font-size change starts a new block (title vs body).
                groups.append([line])
                continue
            if gap <= max(3.0, median_height * 1.15) and horizontal_overlap >= 0.35:
                current.append(line)
                placed = True
            elif gap <= median_height * 0.2 and horizontal_overlap >= 0.15:
                current.append(line)
                placed = True
        if not placed:
            groups.append([line])

    blocks: list[TextBlock] = []
    for block_index, group in enumerate(groups):
        box = BBox.union_all(l.bbox for l in group)
        texts = [l.text for l in group if l.text.strip()]
        if not texts:
            continue
        fonts = [l.font for l in group if l.font]
        sizes = [l.size for l in group if l.size]
        block = TextBlock(
            element_id=f"blk_{page_number:03d}_{block_index + 1:04d}",
            page_number=page_number,
            bbox=box,
            text="\n".join(texts),
            lines=group,
            origin="ocr" if all(l.origin == "ocr" for l in group) else "native",
            # The engine follows the text: a block read by OCR must never claim
            # to come from the native parser.
            engine=_block_engine(group),
            font=max(set(fonts), key=fonts.count) if fonts else None,
            size=round(median(sizes), 2) if sizes else None,
            direction="ltr",
            confidence=(
                round(sum(w.confidence or 0 for l in group for w in l.words) / max(1, sum(len(l.words) for l in group)), 3)
                if any(l.origin == "ocr" for l in group)
                else 1.0
            ),
            reading_order=block_index,
        )
        blocks.append(block)
    return blocks


def _block_engine(group) -> str:
    """Reader that produced most of the block's words (OCR engine or parser)."""
    engines = [w.engine for line in group for w in line.words if getattr(w, "engine", None)]
    if not engines:
        return "pdfplumber"
    return max(set(engines), key=engines.count)


def _x_overlap_ratio(a: BBox, b: BBox) -> float:
    if a.width <= 0 or b.width <= 0:
        return 0.0
    overlap = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    return overlap / min(a.width, b.width)


def build_layout(tokens: list[Token], page_number: int, page_width: float, page_height: float) -> LayoutResult:
    lines = group_lines(tokens, page_number)
    blocks = build_blocks(lines, page_number, page_width=page_width, page_height=page_height)
    heights = [t.bbox.height for t in tokens if t.bbox.height > 0]
    sizes = [t.size for t in tokens if t.size]
    return LayoutResult(
        lines=lines,
        blocks=blocks,
        median_height=median(heights) or 8.0,
        median_size=median(sizes) or 8.0,
        page_width=page_width,
        page_height=page_height,
    )


__all__ = ["LayoutResult", "group_lines", "build_blocks", "build_layout"]
