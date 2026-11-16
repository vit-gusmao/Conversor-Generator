"""Cell-level provenance: the bridge between a workbook cell and the PDF.

For every written cell we keep: the source block/token, its page and bbox, the
engine that produced it, the normalisation transformations applied and the
validation flags.  This is what makes "Ver Auditoria" able to answer
*"where did this number come from?"*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.document import BBox, CellPlan, TextBlock, WorkbookPlan


@dataclass(slots=True)
class CellEvidenceData:
    sheet: str
    cell: str
    row: int
    column: int
    header: str
    value: str | None
    original_value: str | None
    value_type: str
    number_format: str | None
    element_id: str | None
    page_number: int | None
    bbox: BBox | None
    origin: str | None
    engine: str | None
    confidence: float | None
    transformations: list[dict[str, str]] = field(default_factory=list)
    record_id: str | None = None
    field_label: str | None = None
    table_id: str | None = None
    source_preserved: bool = True
    value_matches_source: bool = True

    def to_api(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "cell": self.cell,
            "row": self.row,
            "column": self.column,
            "header": self.header,
            "value": self.value,
            "original_value": self.original_value,
            "value_type": self.value_type,
            "number_format": self.number_format,
            "source": (
                {
                    "element_id": self.element_id,
                    "page_number": self.page_number,
                    "bbox": self.bbox.to_list() if self.bbox else [0, 0, 0, 0],
                    "origin": self.origin or "native",
                    "engine": self.engine or "",
                    "confidence": self.confidence,
                }
                if self.element_id
                else None
            ),
            "transformations": self.transformations,
            "record_id": self.record_id,
            "field_label": self.field_label,
            "table_id": self.table_id,
            "validation": {
                "source_preserved": self.source_preserved,
                "cell_written": True,
                "value_matches_source": self.value_matches_source,
            },
        }


def column_letter(index: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


def build_cell_evidence(
    plan: WorkbookPlan, blocks_by_id: dict[str, TextBlock]
) -> list[CellEvidenceData]:
    evidence: list[CellEvidenceData] = []
    for sheet in plan.sheets:
        for r_idx, row in enumerate(sheet.rows, start=2):
            for c_idx, cell in enumerate(row, start=1):
                if cell is None:
                    continue
                ref = f"{column_letter(c_idx)}{r_idx}"
                block = blocks_by_id.get(cell.element_id or "")
                evidence.append(
                    CellEvidenceData(
                        sheet=sheet.name,
                        cell=ref,
                        row=r_idx,
                        column=c_idx,
                        header=cell.header,
                        value=_stringify(cell.value),
                        original_value=cell.original,
                        value_type=cell.value_type,
                        number_format=cell.number_format,
                        element_id=cell.element_id,
                        page_number=cell.page_number,
                        bbox=cell.bbox,
                        origin=cell.origin,
                        engine=cell.engine,
                        confidence=cell.confidence,
                        transformations=cell.transformations,
                        record_id=cell.record_id,
                        field_label=cell.field_label,
                        table_id=cell.table_id,
                        source_preserved=bool(cell.original is not None or block is not None),
                        value_matches_source=_matches(cell, block),
                    )
                )
    return evidence


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _matches(cell: CellPlan, block: TextBlock | None) -> bool:
    """Loose check: the normalised value must be derivable from the source text."""
    if cell.original is None:
        return True
    if block is None:
        return False
    source = block.text.lower()
    original = cell.original.strip().lower()
    if not original:
        return True
    if original in source:
        return True
    # numeric normalisation: compare digits only
    digits_original = "".join(ch for ch in original if ch.isdigit())
    digits_source = "".join(ch for ch in source if ch.isdigit())
    return bool(digits_original) and digits_original in digits_source


__all__ = ["CellEvidenceData", "build_cell_evidence", "column_letter"]
