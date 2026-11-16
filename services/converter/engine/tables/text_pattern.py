"""Table reconstruction from the text pattern (geometry-independent).

Some pages have perfectly readable text but no usable geometry: a blurry or
dark scan collapses the ink projection into one band, and coordinates stop
separating rows.  Rejecting the table there throws away content that the reader
clearly sees.

The pattern is stable across invoices and receipts:

    Item Descrição Qtd Preco Total
    01 Servico mensal 8 89.37 714.96

A header line with at least three known column labels followed by at least two
lines that end in numbers is a table, no matter where the pixels are.  Values
stay exactly as recognised; only the cell boundaries are inferred from the
text, and every row keeps its own bounding box as provenance.
"""

from __future__ import annotations

import re
import unicodedata

from models.document import BBox, Token, median

from .lattice import RawCell, RawTable

_QUANTITY_LABELS = {
    "item", "itens", "codigo", "cod", "descricao", "desc", "produto", "servico",
    "qtd", "qtde", "quantidade", "qnt", "qt", "un", "unidade",
    "preco", "preço", "valor", "vlr", "vl", "unitario", "unitário",
    "total", "subtotal", "desconto", "peso", "km",
}

_NUMBER = r"[-+]?(?:R\$|US\$|€|£)?\s?\d[\d.,]*"


def _key(text: str) -> str:
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", folded)


def _label_key(text: str) -> str:
    return _key(text)


def _close_to_label(word: str, labels: set[str]) -> bool:
    """Header label with at most one OCR typo ("Preca" for "Preco").

    Only used to *recognise* the header line: the header itself is kept exactly
    as read, so the workbook never shows a corrected word that the document does
    not contain.
    """
    key = _label_key(word)
    if not key:
        return False
    if key in labels:
        return True
    if len(key) < 4:
        return False
    for label in labels:
        if abs(len(label) - len(key)) > 1:
            continue
        # one substitution, insertion or deletion
        if len(label) == len(key):
            if sum(1 for a, b in zip(label, key) if a != b) == 1:
                return True
        else:
            shorter, longer = (label, key) if len(label) < len(key) else (key, label)
            index = 0
            skipped = 0
            for char in longer:
                if index < len(shorter) and char == shorter[index]:
                    index += 1
                else:
                    skipped += 1
                if skipped > 1:
                    break
            if index == len(shorter) and skipped <= 1:
                return True
    return False


def _rows(tokens: list[Token]) -> list[tuple[str, BBox, list[Token]]]:
    heights = [token.bbox.height for token in tokens if token.bbox.height > 0]
    tolerance = max(2.0, (median(heights) or 8.0) * 0.6)
    groups: list[list[Token]] = []
    centers: list[float] = []
    for token in sorted(tokens, key=lambda item: (item.bbox.y_center, item.bbox.x0)):
        center = token.bbox.y_center
        for index, value in enumerate(centers):
            if abs(value - center) <= tolerance:
                groups[index].append(token)
                centers[index] = sum(t.bbox.y_center for t in groups[index]) / len(groups[index])
                break
        else:
            groups.append([token])
            centers.append(center)
    result = []
    for group in groups:
        group.sort(key=lambda item: item.bbox.x0)
        text = " ".join(token.text for token in group).strip()
        if text:
            result.append((text, BBox.union_all(token.bbox for token in group), group))
    result.sort(key=lambda item: item[1].y0)
    return result


def _split_row(text: str) -> list[str] | None:
    """Split one item line into (identifier, description, numbers...)."""
    match = re.match(
        rf"^(?P<first>\S+)\s+(?P<middle>.+?)\s+(?P<numbers>{_NUMBER}(?:\s+{_NUMBER})*)$",
        text.strip(),
    )
    if not match:
        return None
    first = match.group("first")
    middle = match.group("middle").strip()
    numbers = [
        value.strip()
        for value in re.findall(_NUMBER, match.group("numbers"))
        if value.strip()
    ]
    if not middle or not numbers:
        return None
    return [first, middle, *numbers]


def detect_text_tables(tokens: list[Token], page_number: int) -> list[RawTable]:
    if len(tokens) < 8:
        return []
    rows = _rows(tokens)
    tables: list[RawTable] = []
    index = 0
    while index < len(rows):
        text, box, words = rows[index]
        headers = text.split()
        known = sum(1 for word in headers if _close_to_label(word, _QUANTITY_LABELS))
        numeric_headers = sum(
            1
            for word in headers
            if _close_to_label(word, _QUANTITY_LABELS)
            and _label_key(word)
            not in {"item", "itens", "codigo", "cod", "descricao", "desc", "produto", "servico"}
        )
        if len(headers) < 3 or known < 3 or numeric_headers < 1:
            index += 1
            continue

        body: list[list[str]] = []
        body_rows: list[list[Token]] = []
        body_boxes: list[BBox] = []
        cursor = index + 1
        while cursor < len(rows):
            candidate_text, candidate_box, candidate_words = rows[cursor]
            cells = _split_row(candidate_text)
            if cells is None:
                break
            body.append(cells)
            body_rows.append(candidate_words)
            body_boxes.append(candidate_box)
            cursor += 1
        if len(body) < 2:
            index += 1
            continue

        width = max(len(headers), max(len(cells) for cells in body))
        grid: list[list[str]] = []
        for cells in body:
            numbers = cells[2:]
            # Numbers are right-aligned against the header's numeric columns.
            row_values = [""] * width
            row_values[0] = cells[0]
            if width > 1:
                row_values[1] = cells[1]
            numeric_slots = [position for position in range(width - 1, -1, -1)]
            slot_index = 0
            for value in reversed(numbers):
                while slot_index < len(numeric_slots) and numeric_slots[slot_index] <= 1:
                    slot_index += 1
                if slot_index >= len(numeric_slots):
                    break
                row_values[numeric_slots[slot_index]] = value
                slot_index += 1
            grid.append(row_values)

        cells_out: list[RawCell] = []
        for row_index, (cells, row_box, words) in enumerate(zip(grid, body_boxes, body_rows)):
            for column_index, value in enumerate(cells):
                cells_out.append(RawCell(row_index, column_index, value, row_box))

        tables.append(
            RawTable(
                page_number=page_number,
                bbox=BBox.union_all([box, *body_boxes]),
                rows=[headers[:width], *grid],
                cells=cells_out,
                row_boxes=[box, *body_boxes],
                col_boxes=[],
                strategy="text_pattern",
                engine="text-pattern",
                confidence=0.7,
            )
        )
        index = cursor
    return tables


__all__ = ["detect_text_tables"]
