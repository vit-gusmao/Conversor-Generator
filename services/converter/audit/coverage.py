"""Textual coverage accounting.

The metric answers one question only: *did every text block the engine
recovered receive a documented destination?*  It deliberately does not measure
recognition accuracy — coverage of 100% is compatible with OCR errors, which is
why `audit/manifest.py` reports precision separately (only against golden
datasets).
"""

from __future__ import annotations

from models.document import (
    DEST_DROPPED,
    DEST_FIELD,
    DEST_HEADER_FOOTER,
    DEST_METADATA,
    DEST_TABLE,
    DEST_TEXT,
    DEST_UNCLASSIFIED,
    CoverageRow,
    PageResult,
)

_ASSIGNED = {DEST_FIELD, DEST_TABLE, DEST_METADATA}


def block_token_count(block) -> int:
    count = 0
    for line in block.lines:
        count += max(1, len(line.words))
    return max(1, count)


def build_coverage(pages: list[PageResult]) -> list[CoverageRow]:
    rows: list[CoverageRow] = []
    for page in pages:
        row = CoverageRow(page_number=page.page_number)
        for block in page.blocks:
            tokens = block_token_count(block)
            row.text_blocks += 1
            row.tokens += tokens
            dest = block.destination
            if dest in _ASSIGNED:
                row.assigned += tokens
            elif dest == DEST_TEXT:
                row.preserved += tokens
            elif dest == DEST_HEADER_FOOTER:
                row.header_footer += tokens
            elif dest == DEST_UNCLASSIFIED:
                # Explicitly preserved in the unstructured sheet: has a
                # destination, so it counts as covered.
                row.unclassified += tokens
            else:
                # No destination at all is a defect: it is recorded as dropped
                # so it can never inflate the coverage metric.
                row.dropped += tokens
        page.coverage = row
        rows.append(row)
    return rows


def aggregate_coverage(rows: list[CoverageRow]) -> dict[str, float]:
    tokens = sum(r.tokens for r in rows)
    assigned = sum(r.assigned for r in rows)
    preserved = sum(r.preserved for r in rows)
    unclassified = sum(r.unclassified for r in rows)
    header_footer = sum(r.header_footer for r in rows)
    dropped = sum(r.dropped for r in rows)
    blocks = sum(r.text_blocks for r in rows)
    # `dropped` (including blocks that never got a destination) is excluded on
    # purpose: 100% coverage must mean every recovered block was delivered
    # somewhere, never that the text vanished.
    covered = assigned + preserved + unclassified + header_footer
    coverage = round(100.0 * covered / tokens, 2) if tokens else 100.0
    assignment = round(100.0 * assigned / tokens, 2) if tokens else 100.0
    return {
        "tokens": float(tokens),
        "assigned": float(assigned),
        "preserved": float(preserved),
        "unclassified": float(unclassified),
        "header_footer": float(header_footer),
        "dropped": float(dropped),
        "blocks": float(blocks),
        "coverage_percent": coverage,
        "assignment_percent": assignment,
    }


__all__ = ["build_coverage", "aggregate_coverage", "block_token_count"]
