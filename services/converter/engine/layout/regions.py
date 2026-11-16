"""Region classification.

A page is not one thing.  It may contain a letterhead, a form, a table and a
footer with three different treatments.  Regions are therefore classified
individually, using combined signals: position, font size, separators, borders,
repetition across pages and detected semantic structures.
"""

from __future__ import annotations

import re
from collections import Counter

from models.document import (
    REGION_FOOTER,
    REGION_FORM,
    REGION_HEADER,
    REGION_IMAGE,
    REGION_LIST,
    REGION_PARAGRAPH,
    REGION_TABLE,
    REGION_TITLE,
    REGION_UNKNOWN,
    BBox,
    Field,
    PageResult,
    Region,
    TableStructure,
    TextBlock,
    median,
)

_PAGE_NUMBER = re.compile(
    r"^\s*(?:p[áa]g(?:ina)?\.?\s*)?\d{1,4}\s*(?:/|de)\s*\d{1,4}\s*$|^\s*-?\s*\d{1,4}\s*-?\s*$",
    re.IGNORECASE,
)
_LIST_MARKER = re.compile(r"^\s*(?:[•\-\u2013\u2014*]|\(?\d{1,2}[.)]|[a-z][.)])\s+", re.IGNORECASE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def detect_repeated_marginals(pages: list[PageResult], band: float = 0.10) -> set[str]:
    """Texts that repeat in the top/bottom band across pages (letterheads,
    running titles, footers).  Includes page-number-like strings."""
    counter: Counter[str] = Counter()
    for page in pages:
        seen: set[str] = set()
        for block in page.blocks:
            in_band = block.bbox.y1 <= page.height * band or block.bbox.y0 >= page.height * (1.0 - band)
            if not in_band:
                continue
            key = _norm(block.text)
            if len(key) >= 2 and key not in seen:
                seen.add(key)
                counter[key] += 1
    threshold = max(2, int(len(pages) * 0.5))
    return {text for text, count in counter.items() if count >= threshold}


def preclassify_blocks(
    page: PageResult,
    blocks: list[TextBlock],
    repeated_marginals: set[str],
) -> None:
    """Classify header/footer/title/list before field extraction.

    Without this pass a running footer such as ``Página: 3 de 12`` would be
    parsed as a field named "Página" and leak into the workbook.
    """
    sizes = [b.size for b in blocks if b.size]
    median_size = median(sizes) if sizes else 10.0
    for block in blocks:
        block.kind = classify_block(block, page, repeated_marginals, median_size)


def build_regions(
    page: PageResult,
    blocks: list[TextBlock],
    tables: list[TableStructure],
    fields: list[Field],
    repeated_marginals: set[str],
) -> list[Region]:
    regions: list[Region] = []
    heights = [b.size for b in blocks if b.size]
    median_size = median(heights) if heights else 10.0
    index = 0

    def add(kind: str, box: BBox, confidence: float, engine: str = "layout-analyzer") -> None:
        nonlocal index
        index += 1
        regions.append(
            Region(
                region_id=f"reg_{page.page_number:03d}_{index:04d}",
                page_number=page.page_number,
                kind=kind,
                bbox=box,
                confidence=round(confidence, 3),
                engine=engine,
            )
        )

    table_boxes = [t.bbox for t in tables]
    field_boxes = [f.bbox for f in fields]

    for block in blocks:
        # "Is the block inside the table?" — the block's own area is what must be
        # covered. Asking the other way round (fraction of the table covered by
        # one line) never reaches 0.55 for a single row of a table, so table
        # blocks were classified as paragraphs and their rows became sections.
        table_hit = next((tb for tb in table_boxes if block.bbox.overlap_ratio(tb) >= 0.55), None)
        if table_hit is not None:
            block.kind = REGION_TABLE
            continue

        field_hits = sum(1 for fb in field_boxes if fb.overlap_ratio(block.bbox) >= 0.5)
        if field_hits >= 1:
            block.kind = REGION_FORM
            add(REGION_FORM, block.bbox, min(0.99, 0.6 + field_hits * 0.1))
            continue

        kind = classify_block(block, page, repeated_marginals, median_size)
        block.kind = kind
        confidence = {
            REGION_HEADER: 0.95,
            REGION_FOOTER: 0.95,
            REGION_TITLE: 0.8,
            REGION_LIST: 0.7,
            REGION_PARAGRAPH: 0.6,
        }.get(kind, 0.5)
        add(kind, block.bbox, confidence)

    # Table regions are added once per table, using the engine that produced them.
    for table in tables:
        add(REGION_TABLE, table.bbox, table.confidence, engine=table.strategy)

    page.regions = regions
    return regions


def classify_block(
    block: TextBlock,
    page: PageResult,
    repeated_marginals: set[str],
    median_size: float,
) -> str:
    text = block.text.strip()
    normalized = _norm(text)
    in_top = block.bbox.y1 <= page.height * 0.10
    in_bottom = block.bbox.y0 >= page.height * 0.90

    if (in_top or in_bottom) and (
        normalized in repeated_marginals or _PAGE_NUMBER.match(text) or len(text) <= 3
    ):
        return REGION_HEADER if in_top else REGION_FOOTER
    if (in_top or in_bottom) and normalized in repeated_marginals:
        return REGION_HEADER if in_top else REGION_FOOTER

    lines = [line for line in block.lines if line.text.strip()]
    is_short_block = len(lines) <= 2 and len(text) <= 90
    larger = (block.size or 0) >= max(median_size * 1.18, median_size + 1.2)
    if is_short_block and larger and not _PAGE_NUMBER.match(text):
        return REGION_TITLE

    if len(lines) >= 2 and sum(1 for l in lines if _LIST_MARKER.match(l.text)) >= max(2, len(lines) // 2):
        return REGION_LIST

    if block.bbox.area > 0 and block.bbox.width < page.width * 0.06 and block.bbox.height > page.height * 0.3:
        # vertical text (axis label)
        return REGION_PARAGRAPH

    return REGION_PARAGRAPH


def add_image_regions(page: PageResult, image_boxes: list[BBox]) -> None:
    """Register image areas that carry no extracted text (never exported)."""
    index = len(page.regions)
    for box in image_boxes:
        if any(b.bbox.overlap_ratio(box) > 0.6 for b in page.blocks):
            continue
        index += 1
        page.regions.append(
            Region(
                region_id=f"reg_{page.page_number:03d}_{index:04d}",
                page_number=page.page_number,
                kind=REGION_IMAGE,
                bbox=box,
                confidence=0.6,
                engine="image-detector",
            )
        )


def page_summary(page: PageResult) -> dict[str, object]:
    kinds: Counter[str] = Counter(r.kind for r in page.regions)
    return {
        "page_number": page.page_number,
        "classification": page.classification,
        "status": page.status,
        "text_source": page.text_source,
        "native_chars": page.native_chars,
        "ocr_chars": page.ocr_chars,
        "text_blocks": len(page.blocks),
        "tables": len(page.tables),
        "fields": len(page.fields),
        "regions": dict(kinds),
        "unknown_regions": kinds.get(REGION_UNKNOWN, 0),
    }


__all__ = [
    "preclassify_blocks",
    "build_regions",
    "classify_block",
    "detect_repeated_marginals",
    "add_image_regions",
    "page_summary",
]
