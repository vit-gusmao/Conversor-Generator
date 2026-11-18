"""Page checkpoints: serialize one extracted page and load it back.

A resumed run must not repeat OCR for pages it already finished. The artifact
holds exactly what the later stages consume — tokens, blocks, regions and
tables — and nothing that is recomputed (fields, records, coverage).

`schema_version` and `options_hash` are part of the checkpoint key: changing
the extraction schema or the request options invalidates old pages instead of
mixing incompatible results.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Any

from models.document import (
    BBox,
    Line,
    PageResult,
    Region,
    TableAlternative,
    TableCell,
    TableStructure,
    TextBlock,
    Token,
)

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Serialize
# --------------------------------------------------------------------------- #


def _bbox(value: BBox | None) -> list[float] | None:
    if value is None:
        return None
    return [round(value.x0, 3), round(value.y0, 3), round(value.x1, 3), round(value.y1, 3)]


def _to_bbox(value: Any) -> BBox:
    if not value:
        return BBox.empty()
    return BBox.from_ltrb(float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _token(token: Token) -> dict[str, Any]:
    return {
        "element_id": token.element_id,
        "text": token.text,
        "bbox": _bbox(token.bbox),
        "page_number": token.page_number,
        "origin": token.origin,
        "font": token.font,
        "size": token.size,
        "direction": token.direction,
        "confidence": token.confidence,
        "index": token.index,
        "engine": token.engine,
    }


def serialize_page(page: PageResult) -> dict[str, Any]:
    tokens = page.tokens or []
    index_of = {id(token): position for position, token in enumerate(tokens)}

    def word_indices(words: Any) -> list[int]:
        indices: list[int] = []
        for word in words or []:
            position = index_of.get(id(word))
            if position is not None:
                indices.append(position)
        return indices

    return {
        "schema_version": SCHEMA_VERSION,
        "page": {
            "page_number": page.page_number,
            "width": page.width,
            "height": page.height,
            "rotation": page.rotation,
            "classification": page.classification,
            "status": page.status,
            "text_source": page.text_source,
            "native_chars": page.native_chars,
            "ocr_chars": page.ocr_chars,
            "native_words": page.native_words,
            "metrics": page.metrics,
        },
        "tokens": [_token(token) for token in tokens],
        "blocks": [
            {
                "element_id": block.element_id,
                "bbox": _bbox(block.bbox),
                "text": block.text,
                "origin": block.origin,
                "font": block.font,
                "size": block.size,
                "direction": block.direction,
                "confidence": block.confidence,
                "reading_order": block.reading_order,
                "kind": block.kind,
                "engine": block.engine,
                "destination": block.destination,
                "destination_sheet": block.destination_sheet,
                "destination_cell": block.destination_cell,
                "lines": [
                    {
                        "bbox": _bbox(line.bbox),
                        "index": line.index,
                        "words": word_indices(line.words),
                    }
                    for line in block.lines
                ],
            }
            for block in page.blocks
        ],
        "regions": [
            {
                "region_id": region.region_id,
                "kind": region.kind,
                "bbox": _bbox(region.bbox),
                "confidence": region.confidence,
                "engine": region.engine,
                "resolution": region.resolution,
                "text_source": region.text_source,
            }
            for region in page.regions
        ],
        "tables": [
            {
                "table_id": table.table_id,
                "bbox": _bbox(table.bbox),
                "strategy": table.strategy,
                "rows": [list(row) for row in table.rows],
                "headers": table.headers,
                "cells": [
                    {
                        "row": cell.row,
                        "col": cell.col,
                        "text": cell.text,
                        "bbox": _bbox(cell.bbox),
                        "rowspan": cell.rowspan,
                        "colspan": cell.colspan,
                        "confidence": cell.confidence,
                        "origin": cell.origin,
                    }
                    for cell in table.cells
                ],
                "merged": [
                    {"row": span.row, "col": span.col, "rowspan": span.rowspan, "colspan": span.colspan}
                    for span in table.merged
                ],
                "row_kinds": table.row_kinds,
                "confidence": table.confidence,
                "alternatives": [
                    {
                        "strategy": alternative.strategy,
                        "n_rows": alternative.n_rows,
                        "n_cols": alternative.n_cols,
                        "confidence": alternative.confidence,
                        "accepted": alternative.accepted,
                        "reason": alternative.reason,
                        "coverage": alternative.coverage,
                    }
                    for alternative in table.alternatives
                ],
                "continued_from": table.continued_from,
                "continued_on_page": table.continued_on_page,
                "transposed_from": table.transposed_from,
                "column_boxes": [_bbox(box) for box in table.column_boxes],
                "row_boxes": [_bbox(box) for box in table.row_boxes],
                "row_pages": table.row_pages,
                "title": table.title,
                "element_ids": table.element_ids,
            }
            for table in page.tables
        ],
    }


# --------------------------------------------------------------------------- #
# Deserialize
# --------------------------------------------------------------------------- #


def deserialize_page(payload: dict[str, Any]) -> PageResult:
    meta = payload["page"]
    tokens = [
        Token(
            element_id=item["element_id"],
            text=item["text"],
            bbox=_to_bbox(item.get("bbox")),
            page_number=int(item.get("page_number") or meta["page_number"]),
            origin=item.get("origin") or "native",
            font=item.get("font"),
            size=item.get("size"),
            direction=item.get("direction") or "ltr",
            confidence=item.get("confidence"),
            index=int(item.get("index") or 0),
            engine=item.get("engine") or "pdfplumber",
        )
        for item in payload.get("tokens", [])
    ]

    blocks: list[TextBlock] = []
    for item in payload.get("blocks", []):
        lines = [
            Line(
                words=[tokens[position] for position in line.get("words", []) if 0 <= position < len(tokens)],
                bbox=_to_bbox(line.get("bbox")),
                index=int(line.get("index") or 0),
            )
            for line in item.get("lines", [])
        ]
        blocks.append(
            TextBlock(
                element_id=item["element_id"],
                page_number=int(meta["page_number"]),
                bbox=_to_bbox(item.get("bbox")),
                text=item.get("text") or "",
                lines=lines,
                origin=item.get("origin") or "native",
                font=item.get("font"),
                size=item.get("size"),
                direction=item.get("direction") or "ltr",
                confidence=item.get("confidence"),
                reading_order=int(item.get("reading_order") or 0),
                kind=item.get("kind") or "block",
                engine=item.get("engine") or "",
                destination=item.get("destination"),
                destination_sheet=item.get("destination_sheet"),
                destination_cell=item.get("destination_cell"),
            )
        )

    regions = [
        Region(
            region_id=item["region_id"],
            page_number=int(meta["page_number"]),
            kind=item.get("kind") or "unknown",
            bbox=_to_bbox(item.get("bbox")),
            confidence=float(item.get("confidence") or 0.0),
            engine=item.get("engine") or "",
            resolution=item.get("resolution") or "region",
            text_source=item.get("text_source") or "native",
        )
        for item in payload.get("regions", [])
    ]

    tables = [
        TableStructure(
            table_id=item["table_id"],
            page_number=int(meta["page_number"]),
            bbox=_to_bbox(item.get("bbox")),
            strategy=item.get("strategy") or "stream",
            rows=[list(row) for row in (item.get("rows") or [])],
            headers=list(item.get("headers") or []),
            cells=[
                TableCell(
                    row=int(cell.get("row") or 0),
                    col=int(cell.get("col") or 0),
                    text=cell.get("text") or "",
                    bbox=_to_bbox(cell.get("bbox")),
                    rowspan=int(cell.get("rowspan") or 1),
                    colspan=int(cell.get("colspan") or 1),
                    confidence=float(cell.get("confidence") or 0.0),
                    origin=cell.get("origin") or "native",
                )
                for cell in item.get("cells", [])
            ],
            merged=[
                _merge_span(span) for span in item.get("merged", [])
            ],
            row_kinds=list(item.get("row_kinds") or []),
            confidence=float(item.get("confidence") or 0.0),
            alternatives=[
                TableAlternative(
                    strategy=alternative.get("strategy") or "",
                    n_rows=int(alternative.get("n_rows") or 0),
                    n_cols=int(alternative.get("n_cols") or 0),
                    confidence=float(alternative.get("confidence") or 0.0),
                    accepted=bool(alternative.get("accepted")),
                    reason=alternative.get("reason") or "",
                    coverage=float(alternative.get("coverage") or 0.0),
                )
                for alternative in item.get("alternatives", [])
            ],
            continued_from=item.get("continued_from"),
            continued_on_page=item.get("continued_on_page"),
            transposed_from=item.get("transposed_from"),
            column_boxes=[_to_bbox(box) for box in item.get("column_boxes", [])],
            row_boxes=[_to_bbox(box) for box in item.get("row_boxes", [])],
            row_pages=list(item.get("row_pages") or []),
            title=item.get("title"),
            element_ids=list(item.get("element_ids") or []),
        )
        for item in payload.get("tables", [])
    ]

    page = PageResult(
        page_number=int(meta["page_number"]),
        width=float(meta.get("width") or 0.0),
        height=float(meta.get("height") or 0.0),
        rotation=int(meta.get("rotation") or 0),
        classification=meta.get("classification") or "native",
        status=meta.get("status") or "ok",
        text_source=meta.get("text_source") or "native",
        native_chars=int(meta.get("native_chars") or 0),
        ocr_chars=int(meta.get("ocr_chars") or 0),
        native_words=int(meta.get("native_words") or 0),
        metrics=dict(meta.get("metrics") or {}),
    )
    page.tokens = tokens
    page.blocks = blocks
    page.regions = regions
    page.tables = tables
    return page


def _merge_span(span: dict[str, Any]):
    from models.document import MergedSpan

    return MergedSpan(
        row=int(span.get("row") or 0),
        col=int(span.get("col") or 0),
        rowspan=int(span.get("rowspan") or 1),
        colspan=int(span.get("colspan") or 1),
    )


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #


def encode_artifact(page: PageResult) -> bytes:
    return gzip.compress(
        json.dumps(serialize_page(page), ensure_ascii=False).encode("utf-8"),
        compresslevel=6,
    )


def decode_artifact(data: bytes) -> PageResult:
    return deserialize_page(json.loads(gzip.decompress(data).decode("utf-8")))


__all__ = [
    "SCHEMA_VERSION",
    "serialize_page",
    "deserialize_page",
    "encode_artifact",
    "decode_artifact",
]
