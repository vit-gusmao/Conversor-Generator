"""Stage 6b — grouping fields into records.

`Nome: João` and `Cidade: Belo Horizonte` describe the same entity; `Nome: João`
and `Nome: Maria` describe two entities.  The distinction is made here using
repetition, vertical rhythm, section titles and explicit record headers — never
by guessing.

A record becomes **one row**; its fields become **columns**.
"""

from __future__ import annotations

from dataclasses import dataclass

from models.document import Field, Record, TextBlock, median


@dataclass(slots=True)
class GroupingConfig:
    max_vertical_gap_ratio: float = 3.6
    repeat_label_break: bool = True
    min_fields_for_repeat_break: int = 2


def group_records(
    fields: list[Field],
    *,
    page_number: int,
    blocks: list[TextBlock] | None = None,
    config: GroupingConfig | None = None,
) -> list[Record]:
    config = config or GroupingConfig()
    if not fields:
        return []

    blocks = blocks or []
    title_breaks = _title_breaks(fields, blocks)

    ordered = sorted(fields, key=lambda f: (round(f.label_bbox.y0, 1), f.label_bbox.x0))
    heights = [f.bbox.height for f in ordered if f.bbox.height > 0]
    median_height = median(heights) or 12.0

    records: list[Record] = []
    current: list[Field] = []

    def flush() -> None:
        nonlocal current
        if current:
            record = Record(
                record_id=f"rec_{page_number:03d}_{len(records) + 1:04d}",
                page_number=page_number,
                fields=list(current),
                section=current[0].section,
                order=len(records),
            )
            records.append(record.finalize())
            current = []

    for field in ordered:
        if not current:
            current.append(field)
            continue

        previous = current[-1]
        gap = field.bbox.y0 - previous.bbox.y1
        # Two different situations look alike:
        #  * the record restarted (Nome, Cargo, Nome, Cargo) -> a new record;
        #  * a label repeats inside one record (Telefone twice) -> same record,
        #    with the second value kept in its own column.
        # Only the first is a restart signal, so the test is "the label repeats
        # the label that opened this record", not "any label repeats".
        restart = config.repeat_label_break and _same_label(current[0].label, field.label)
        too_far = gap > median_height * config.max_vertical_gap_ratio
        section_changed = bool(field.section) and field.section != current[0].section
        crossed_title = any(
            title_box.y0 >= previous.bbox.y1 - 1.0 and title_box.y1 <= field.bbox.y0 + 1.0
            for title_box in title_breaks
        )

        if (
            (restart and len(current) >= config.min_fields_for_repeat_break)
            or too_far
            or section_changed
            or crossed_title
        ):
            flush()
        current.append(field)

    flush()
    return records


def _same_label(a: str, b: str) -> bool:
    return _normalise_label(a) == _normalise_label(b)


def _normalise_label(label: str) -> str:
    return " ".join(label.lower().replace(":", "").split())


def _title_breaks(fields: list[Field], blocks: list[TextBlock]):
    if not fields or not blocks:
        return []
    return [block.bbox for block in blocks if block.kind == "title"]


def record_key(field: Field) -> tuple[int, float, float]:
    return (field.page_number, round(field.label_bbox.y0, 1), field.label_bbox.x0)


__all__ = ["group_records", "GroupingConfig", "record_key"]
