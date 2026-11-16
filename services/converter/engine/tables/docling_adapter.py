"""Optional Docling / TableFormer adapter.

Docling is deliberately optional: it is a heavy dependency with its own model
downloads.  When it is installed the adapter contributes *structure only* —
cells and spans — while the characters always come from the native/OCR text
layer.  This separation prevents a generative model from inventing cell
content.
"""

from __future__ import annotations

import importlib.util

from models.document import BBox

from .lattice import RawCell, RawTable


def available() -> bool:
    return importlib.util.find_spec("docling") is not None


def detect_with_docling(
    pdf_path: str,
    page_number: int,
    *,
    tokens_in_box=None,
) -> list[RawTable]:
    """Return tables predicted by Docling for one page, in page coordinates."""
    if not available():
        return []
    try:
        from docling.datamodel.base_models import InputFormat  # type: ignore[import-not-found]
        from docling.datamodel.pipeline_options import PdfPipelineOptions  # type: ignore[import-not-found]
        from docling.document_converter import DocumentConverter, PdfFormatOption  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return []

    try:
        options = PdfPipelineOptions()
        options.do_table_structure = True
        options.do_ocr = False  # OCR is handled by our own stack
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        document = converter.convert(pdf_path).document
    except Exception:  # noqa: BLE001
        return []

    tables: list[RawTable] = []
    try:
        for table in document.tables:
            data = table.data
            n_rows = int(getattr(data, "num_rows", 0) or 0)
            n_cols = int(getattr(data, "num_cols", 0) or 0)
            if n_rows < 2 or n_cols < 2:
                continue
            grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
            cells: list[RawCell] = []
            for cell in data.table_cells:
                row = int(cell.start_row_offset_idx)
                col = int(cell.start_col_offset_idx)
                rowspan = int(getattr(cell, "row_span", 1) or 1)
                colspan = int(getattr(cell, "col_span", 1) or 1)
                text = getattr(cell, "text", "") or ""
                if 0 <= row < n_rows and 0 <= col < n_cols:
                    grid[row][col] = text.strip()
                    cells.append(
                        RawCell(
                            row=row,
                            col=col,
                            text=text.strip(),
                            bbox=BBox.empty(),
                            rowspan=rowspan,
                            colspan=colspan,
                        )
                    )
            # Docling returns provenance in PDF coordinates (bottom-left origin
            # is not guaranteed); when it is missing we fall back to the text
            # bounding box below.
            bbox = BBox.empty()
            try:
                prov = table.prov[0]
                bbox = BBox(prov.bbox.l, prov.bbox.t, prov.bbox.r, prov.bbox.b)
            except Exception:  # noqa: BLE001
                if tokens_in_box is not None:
                    bbox = tokens_in_box
            tables.append(
                RawTable(
                    page_number=page_number,
                    bbox=bbox,
                    rows=grid,
                    cells=cells,
                    strategy="docling",
                    engine="docling-tableformer",
                    confidence=0.8,
                )
            )
    except Exception:  # noqa: BLE001
        return tables
    return tables


__all__ = ["available", "detect_with_docling"]
