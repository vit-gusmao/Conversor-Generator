"""Stage 6 — the universal field/value engine.

Four complementary passes, in order of confidence:

1. `label: value` on the same line (with several pairs per line supported).
2. `label:` on one line and the value on the next line(s), including
   multi-line values.
3. two geometric columns with no separator at all (label column / value column).
4. interactive AcroForm fields (handled by the caller and merged in).

Nothing is discarded: a block that yields no field is returned untouched so the
resolver can still send it to the free-text sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from audit.events import AuditRecorder
from engine.fields.label_detector import (
    Segment,
    is_label_like,
    is_value_like,
    label_only,
    split_label_value,
    split_two_columns,
)
from models.document import BBox, Field, Line, TextBlock, Token, median


@dataclass(slots=True)
class FieldExtraction:
    fields: list[Field] = field(default_factory=list)
    consumed_block_ids: set[str] = field(default_factory=set)
    consumed_lines: set[tuple[str, int]] = field(default_factory=set)
    unresolved_labels: list[str] = field(default_factory=list)

    @property
    def has_fields(self) -> bool:
        return bool(self.fields)


def extract_fields(
    blocks: list[TextBlock],
    *,
    page_number: int,
    recorder: AuditRecorder | None = None,
) -> FieldExtraction:
    result = FieldExtraction()
    sections = _sections(blocks)

    for block in blocks:
        if not block.lines:
            continue
        produced = _process_block(block, page_number, result, recorder, sections)
        if produced and block.element_id not in result.consumed_block_ids:
            if all(
                (block.element_id, line.index) in result.consumed_lines
                for line in block.lines
                if line.text.strip()
            ):
                result.consumed_block_ids.add(block.element_id)

    _cross_block_label_rows(blocks, result, page_number, sections, recorder)
    _merge_multiline_values(result.fields)
    return result


def _cross_block_label_rows(
    blocks: list[TextBlock],
    result: FieldExtraction,
    page_number: int,
    sections: dict[str, str | None],
    recorder: AuditRecorder | None,
) -> None:
    """`Nome: Data:` in one block, the values in the block right below.

    The layout engine splits a label line and its value line into separate
    blocks when the vertical gap is large enough, so the in-block pass never
    sees them together.  The pairing is geometric and strict: single-line label
    block, single-line value block directly below, and at least two value
    segments so that one label is never invented from a stray line.
    """
    ordered = sorted(blocks, key=lambda block: (block.reading_order, block.bbox.y0))
    for position, block in enumerate(ordered):
        if block.element_id in result.consumed_block_ids:
            continue
        lines = [line for line in block.lines if line.text.strip()]
        if len(lines) != 1:
            continue
        segments = _label_row(lines[0])
        if segments is None:
            continue
        for candidate in ordered[position + 1 : position + 3]:
            if candidate.element_id in result.consumed_block_ids:
                continue
            value_lines = [line for line in candidate.lines if line.text.strip()]
            if len(value_lines) != 1:
                continue
            gap = candidate.bbox.y0 - block.bbox.y1
            if gap < -2 or gap > max(26.0, block.bbox.height * 2.6):
                continue
            value_line = value_lines[0]
            if split_label_value(value_line) or label_only(value_line) is not None:
                continue
            pairs = _pair_label_row_with_values(segments, value_line)
            if not pairs:
                continue
            section = sections.get(block.element_id)
            created = 0
            for segment, value_words in pairs:
                value_text = " ".join(word.text for word in value_words).strip()
                if not value_text:
                    continue
                result.fields.append(
                    Field(
                        label=segment.label,
                        value=value_text,
                        page_number=page_number,
                        label_bbox=segment.label_bbox,
                        value_bbox=BBox.union_all(word.bbox for word in value_words),
                        element_ids=[word.element_id for word in segment.label_words + value_words],
                        separator="below",
                        confidence=0.74,
                        reason_codes=["label_row", "values_below", "cross_block"],
                        section=section,
                    )
                )
                created += 1
            if created:
                result.consumed_block_ids.add(block.element_id)
                result.consumed_block_ids.add(candidate.element_id)
                if recorder:
                    recorder.decide(
                        stage="field_resolution",
                        subject=f"{block.text.strip()[:60]!r} / linha de valores",
                        accepted=True,
                        algorithm="field-resolver-v1",
                        reason_codes=["label_row", "values_below", "cross_block"],
                        page=page_number,
                        payload={"fields": created, "gap": round(gap, 2)},
                    )
            break


def _sections(blocks: list[TextBlock]) -> dict[str, str | None]:
    """Nearest preceding title text for each block (document order).

    Ordering must include the page: ``reading_order`` restarts at 0 on every
    page, so sorting by it alone lets a title from page 2 become the section of
    page 1 blocks.
    """
    mapping: dict[str, str | None] = {}
    current: str | None = None
    for block in sorted(blocks, key=lambda b: (b.page_number, b.reading_order)):
        mapping[block.element_id] = current
        if block.kind == "title" or _looks_like_section_title(block):
            if _looks_like_table_header(block.text):
                # "Item Descrição Qtd Preco Total" names columns, not a section:
                # accepting it renamed the fields below the table.
                continue
            if ":" in block.text or "=" in block.text:
                # A label/value line is data.  The layout can call it a title
                # (it is short and starts with a capital), and then it renamed
                # the section of the following fields — splitting one form into
                # two sheets.
                continue
            if _digit_tokens(block.text) >= 2:
                # The layout can call a value row a title ("Maria Carolina Demo
                # 29 01 2025").  Data must never rename the section: it split a
                # single record into several sheets.
                continue
            current = block.text.strip().split("\n")[0][:120]
    return mapping


def _looks_like_table_header(text: str) -> bool:
    from engine.tables.text_pattern import _QUANTITY_LABELS, _close_to_label

    words = (text or "").split()
    if len(words) < 3:
        return False
    return sum(1 for word in words if _close_to_label(word, _QUANTITY_LABELS)) >= 3


def _digit_tokens(text: str) -> int:
    return sum(1 for word in (text or "").split() if any(ch.isdigit() for ch in word))


_DATA_IN_TITLE = re.compile(
    r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"  # dates
    r"|\d{1,3}(?:\.\d{3})*,\d{2}"  # pt-BR money / decimals
    r"|R\$\s*\d"  # currency
)


def _looks_like_section_title(block: TextBlock) -> bool:
    text = block.text.strip()
    if not text or len(text) > 80:
        return False
    # A label/value line (`Nome: João`) must never become a section header: it
    # would rename the following fields' section and split one record into two.
    if ":" in text or "=" in text:
        return False
    lines = [line for line in block.lines if line.text.strip()]
    if len(lines) != 1:
        return False
    words = text.split()
    # Data is not a heading: a date, an amount or two or more numeric tokens
    # means this is a value row ("Maria Carolina Demo 29/01/2025",
    # "03 Deslocamento 12 266.85").  Making it a section split one record into
    # several sheets.
    if _DATA_IN_TITLE.search(text):
        return False
    if sum(1 for word in words if any(ch.isdigit() for ch in word)) >= 2:
        return False
    return len(words) <= 6 and (text.isupper() or text.istitle())


def section_map(blocks: list[TextBlock]) -> dict[str, str | None]:
    """Nearest preceding section title for each block (public wrapper)."""
    return _sections(blocks)


def _process_block(
    block: TextBlock,
    page_number: int,
    result: FieldExtraction,
    recorder: AuditRecorder | None,
    sections: dict[str, str | None],
) -> bool:
    lines = [line for line in block.lines if line.text.strip()]
    if not lines:
        return False
    section = sections.get(block.element_id)
    produced = False

    # -- pass 1: same line, explicit separator -----------------------------
    for line_pos, line in enumerate(lines):
        segments = split_label_value(line)
        for segment in segments:
            field = _segment_to_field(segment, page_number, line.index, section)
            if field is None:
                if recorder:
                    recorder.decide(
                        stage="field_resolution",
                        subject=f"{segment.label!r} / {segment.value[:40]!r}",
                        accepted=False,
                        algorithm="field-resolver-v1",
                        reason_codes=["label_rejected_by_shape"],
                        page=page_number,
                    )
                continue
            result.fields.append(field)
            result.consumed_lines.add((block.element_id, line.index))
            _extend_multiline(field, lines, line_pos, result, block.element_id)
            produced = True
            if recorder:
                recorder.decide(
                    stage="field_resolution",
                    subject=field.label,
                    accepted=True,
                    algorithm="field-resolver-v1",
                    reason_codes=field.reason_codes or segment.reason_codes,
                    page=page_number,
                    payload={
                        "label": field.label,
                        "value": field.value,
                        "separator": field.separator,
                        "confidence": round(field.confidence, 3),
                        "label_bbox": field.label_bbox.to_list(),
                        "value_bbox": field.value_bbox.to_list(),
                    },
                )

    # -- pass 2: label alone, value below ----------------------------------
    for index, line in enumerate(lines):
        if (block.element_id, line.index) in result.consumed_lines:
            continue
        pending = label_only(line)
        if pending is None:
            continue
        value_words, consumed, multiline = _collect_value_below(lines, index, pending)
        if not value_words:
            result.unresolved_labels.append(pending.label)
            continue
        value_text = " ".join(w.text for w in value_words).strip()
        if not value_text:
            continue
        field = Field(
            label=pending.label,
            value=value_text,
            page_number=page_number,
            label_bbox=pending.label_bbox,
            value_bbox=BBox.union_all(w.bbox for w in value_words),
            element_ids=[w.element_id for w in pending.label_words + value_words],
            separator="below",
            confidence=0.82 if not multiline else 0.86,
            reason_codes=["value_below", "vertical_proximity"]
            + (["multiline_value"] if multiline else []),
            multiline=multiline,
            line_index=line.index,
            section=section,
        )
        result.fields.append(field)
        result.consumed_lines.add((block.element_id, line.index))
        for consumed_line in consumed:
            result.consumed_lines.add((block.element_id, consumed_line))
        produced = True
        if recorder:
            recorder.decide(
                stage="field_resolution",
                subject=field.label,
                accepted=True,
                algorithm="field-resolver-v1",
                reason_codes=field.reason_codes,
                page=page_number,
                payload={"label": field.label, "value": field.value, "separator": "below"},
            )

    # -- pass 2b: a row of labels, the values on the row below --------------
    # Two-column forms print "Nome: Data:" on one line and the two values on
    # the next.  Pass 2 only understands a single label, so those forms produced
    # no field at all.
    for index, line in enumerate(lines):
        if (block.element_id, line.index) in result.consumed_lines:
            continue
        pending_row = _label_row(line)
        if pending_row is None or index + 1 >= len(lines):
            continue
        value_line = lines[index + 1]
        if (block.element_id, value_line.index) in result.consumed_lines:
            continue
        if split_label_value(value_line) or label_only(value_line) is not None:
            continue
        pairs = _pair_label_row_with_values(pending_row, value_line)
        if not pairs:
            continue
        for segment, value_words in pairs:
            value_text = " ".join(w.text for w in value_words).strip()
            if not value_text:
                continue
            result.fields.append(
                Field(
                    label=segment.label,
                    value=value_text,
                    page_number=page_number,
                    label_bbox=segment.label_bbox,
                    value_bbox=BBox.union_all(w.bbox for w in value_words),
                    element_ids=[w.element_id for w in segment.label_words + value_words],
                    separator="below",
                    confidence=0.78,
                    reason_codes=["label_row", "values_below", "column_alignment"],
                    line_index=line.index,
                    section=section,
                )
            )
            produced = True
        result.consumed_lines.add((block.element_id, line.index))
        result.consumed_lines.add((block.element_id, value_line.index))

    # -- pass 3: two geometric columns, no separator -----------------------
    # Runs even when pass 1/2 produced fields: a block can mix "Cargo: X" with
    # plain two-column rows, and those rows used to be dropped.
    if True:
        candidates = _two_column_candidates(lines, result, block.element_id)
        if candidates:
            for line, left, right, gap in candidates:
                label_text = " ".join(w.text for w in left).strip()
                value_text = " ".join(w.text for w in right).strip()
                field = Field(
                    label=label_text,
                    value=value_text,
                    page_number=page_number,
                    label_bbox=BBox.union_all(w.bbox for w in left),
                    value_bbox=BBox.union_all(w.bbox for w in right),
                    element_ids=[w.element_id for w in left + right],
                    separator="geometry",
                    confidence=0.7,
                    reason_codes=["two_columns", "consistent_alignment", "no_separator"],
                    line_index=line.index,
                    section=section,
                )
                result.fields.append(field)
                result.consumed_lines.add((block.element_id, line.index))
                produced = True
                if recorder:
                    recorder.decide(
                        stage="field_resolution",
                        subject=field.label,
                        accepted=True,
                        algorithm="field-resolver-v1",
                        reason_codes=field.reason_codes,
                        page=page_number,
                        payload={"label": field.label, "value": field.value, "gap": round(gap, 2)},
                    )

    return produced


def _label_row(line: Line) -> list[Segment] | None:
    """``Nome: Data:`` — two or more labels sharing one line, no values.

    Every word after the last label must end with a separator; a line that mixes
    labels and free text is not a label row.
    """
    words = [word for word in line.words if word.text.strip()]
    if len(words) < 2:
        return None
    groups: list[list[Token]] = []
    current: list[Token] = []
    for word in words:
        current.append(word)
        if word.text.strip().endswith((":", "=")):
            groups.append(current)
            current = []
    if current or len(groups) < 2:
        return None
    segments: list[Segment] = []
    for group in groups:
        label_words = [word for word in group if word.text.strip() not in {":", "="}]
        if label_words:
            last = label_words[-1]
            text = last.text.rstrip(":=").strip()
            if text and text != last.text:
                label_words = label_words[:-1] + [
                    Token(
                        element_id=last.element_id,
                        text=text,
                        bbox=last.bbox,
                        page_number=last.page_number,
                        origin=last.origin,
                        font=last.font,
                        size=last.size,
                        direction=last.direction,
                        confidence=last.confidence,
                        index=last.index,
                        engine=last.engine,
                    )
                ]
        label = " ".join(word.text for word in label_words).strip()
        if not label or not is_label_like(label):
            return None
        segments.append(
            Segment(
                label=label,
                value="",
                label_words=label_words,
                value_words=[],
                separator="colon",
                reason_codes=["label_row", "values_below"],
                confidence=0.68,
            )
        )
    return segments


def _pair_label_row_with_values(
    segments: list[Segment], value_line: Line
) -> list[tuple[Segment, list[Token]]]:
    """Match each label of the row with the value segment under it.

    Values are split at wide gaps and assigned to the label whose left edge is
    closest: forms align the value with its label horizontally.
    """
    words = [word for word in value_line.words if word.text.strip()]
    if not words:
        return []
    sizes = [word.size for word in words if word.size]
    height = median(sizes) or 10.0
    gap_threshold = max(12.0, height * 1.8)
    chunks: list[list[Token]] = [[words[0]]]
    for previous, word in zip(words, words[1:]):
        if word.bbox.x0 - previous.bbox.x1 >= gap_threshold:
            chunks.append([word])
        else:
            chunks[-1].append(word)
    if len(chunks) < 2:
        return []
    pairs: list[tuple[Segment, list[Token]]] = []
    for chunk in chunks:
        left = BBox.union_all(word.bbox for word in chunk).x0
        segment = min(segments, key=lambda item: abs(item.label_bbox.x0 - left))
        pairs.append((segment, chunk))
    return pairs


def _extend_multiline(
    field: Field,
    lines: list[Line],
    line_pos: int,
    result: FieldExtraction,
    block_id: str,
    max_extra: int = 4,
) -> None:
    """Append continuation lines to a value that starts on the label's line.

    ``Endereço: Avenida Principal,`` followed by ``número 250, bloco B,`` and
    ``apartamento 301`` is one cell, not three rows.
    """
    if not field.value or not _needs_continuation(field.value):
        return
    reference_x = field.label_bbox.x0
    previous_bottom = field.value_bbox.y1
    height = max(6.0, field.label_bbox.height)
    parts = [field.value]
    consumed: list[Line] = []

    for offset in range(1, max_extra + 1):
        position = line_pos + offset
        if position >= len(lines):
            break
        candidate = lines[position]
        if (block_id, candidate.index) in result.consumed_lines:
            break
        gap = candidate.bbox.y0 - previous_bottom
        if gap > height * 2.6:
            break
        if candidate.bbox.x0 < reference_x - 24:
            break
        words = [w for w in candidate.words if w.text.strip()]
        if not words:
            continue
        if split_label_value(candidate) or label_only(candidate) is not None:
            break
        text = " ".join(w.text for w in words).strip()
        parts.append(text)
        consumed.append(candidate)
        previous_bottom = candidate.bbox.y1
        if not _needs_continuation(text):
            break

    if not consumed:
        return
    field.value = " ".join(parts).strip()
    field.multiline = True
    if "multiline_value" not in field.reason_codes:
        field.reason_codes.append("multiline_value")
    field.element_ids.extend(w.element_id for line in consumed for w in line.words)
    field.value_bbox = BBox.union_all([field.value_bbox] + [line.bbox for line in consumed])
    for line in consumed:
        result.consumed_lines.add((block_id, line.index))


def _segment_to_field(
    segment: Segment, page_number: int, line_index: int, section: str | None
) -> Field | None:
    if not segment.label or not segment.value:
        return None
    if not is_label_like(segment.label):
        return None
    if segment.value.strip() == "":
        return None
    return Field(
        label=segment.label.strip(" :="),
        value=segment.value.strip(),
        page_number=page_number,
        label_bbox=segment.label_bbox,
        value_bbox=segment.value_bbox,
        element_ids=[w.element_id for w in segment.label_words + segment.value_words],
        separator=segment.separator,
        confidence=segment.confidence,
        reason_codes=list(segment.reason_codes),
        line_index=line_index,
        section=section,
    )


def _collect_value_below(
    lines: list[Line], index: int, pending: Segment
) -> tuple[list, set[int], bool]:
    """Collect the value of a `Label:` line, merging continuation lines."""
    label_box = pending.label_bbox
    collected: list = []
    consumed: set[int] = set()
    multiline = False
    reference_x = label_box.x0
    previous_bottom = label_box.y1
    height = max(6.0, label_box.height)

    for offset in range(1, min(6, len(lines) - index)):
        candidate = lines[index + offset]
        gap = candidate.bbox.y0 - previous_bottom
        if gap > height * 2.6:
            break
        if candidate.bbox.x0 < reference_x - 24:
            break
        words = [w for w in candidate.words if w.text.strip()]
        if not words:
            continue
        if split_label_value(candidate) or label_only(candidate) is not None:
            break
        text = " ".join(w.text for w in words)
        if not text.strip():
            continue
        if offset == 1:
            collected = list(words)
            consumed.add(candidate.index)
            multiline = len(words) > 0 and _needs_continuation(text)
            previous_bottom = candidate.bbox.y1
            continue
        if not multiline:
            break
        # continuation: same indentation family and no new field semantics
        collected.extend(words)
        consumed.add(candidate.index)
        previous_bottom = candidate.bbox.y1
        multiline = _needs_continuation(text)

    return collected, consumed, multiline and len(collected) > 0


def _needs_continuation(text: str) -> bool:
    """A value continues on the next line only with positive evidence."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[-1] in ",;:-–—/":
        return True
    # An unterminated sentence is a weak signal; it only counts when the line
    # is long *enough to have wrapped* and does not end like a finished value.
    words = stripped.split()
    return len(words) >= 8 and stripped[-1] not in ".!?;)"


def _two_column_candidates(lines: list[Line], result: FieldExtraction, block_id: str):
    """Find lines forming a stable label column + value column."""
    if len(lines) < 2:
        return []
    within_word_gaps: list[float] = []
    for line in lines:
        words = [w for w in line.words if w.text.strip()]
        if len(words) < 3:
            # With two words there is no *intra*-phrase gap to learn from: their
            # only gap is the column separator itself.
            continue
        within_word_gaps.extend([b.bbox.x0 - a.bbox.x1 for a, b in zip(words, words[1:])])
    median_gap = median([g for g in within_word_gaps if g >= 0]) or 4.0
    threshold = max(16.0, median_gap * 3.2)

    splits: list[tuple[Line, list, list, float]] = []
    for line in lines:
        if (block_id, line.index) in result.consumed_lines:
            continue
        split = split_two_columns(line, threshold)
        if not split:
            continue
        left, right, gap = split
        left_text = " ".join(w.text for w in left).strip()
        right_text = " ".join(w.text for w in right).strip()
        if not is_label_like(left_text):
            continue
        if not right_text or right_text.endswith(":"):
            continue
        if len(left_text.split()) > 6:
            continue
        splits.append((line, left, right, gap))

    if len(splits) < 2:
        return []

    # Alignment must be consistent: left edges and right edges repeat.
    left_edges = [BBox.union_all(w.bbox for w in item[1]).x0 for item in splits]
    right_edges = [BBox.union_all(w.bbox for w in item[2]).x0 for item in splits]
    if _spread(left_edges) > 14.0 or _spread(right_edges) > 18.0:
        return []
    return splits


def _spread(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return max(values) - min(values)


def _merge_multiline_values(fields: list[Field]) -> None:
    """Join values that were split by a line break inside the same field."""
    for field in fields:
        if not field.value:
            continue
        field.value = " ".join(field.value.split())
        if field.value.endswith(",") and field.multiline:
            field.value = field.value.rstrip(",;:")


__all__ = ["extract_fields", "FieldExtraction", "section_map"]
