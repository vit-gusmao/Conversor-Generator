"""Label detection.

Everything here is heuristic and explainable: each segmentation returns the
reason codes that produced it, so the audit can show *why* `Nome` became a
header.  No field dictionary is used — any label the document contains can
become a column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models.document import BBox, Line, Token, median

CONNECTORS = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "e",
    "em",
    "por",
    "para",
    "com",
    "sem",
    "ao",
    "aos",
    "à",
    "às",
    "no",
    "na",
    "nos",
    "nas",
    "of",
    "the",
    "and",
}

_ABBREVIATIONS = {"nº", "no.", "n.", "art.", "av.", "r.", "dr.", "dra.", "sr.", "sra.", "cnpj", "cpf"}
_DIGIT = re.compile(r"\d")
_CURRENCY = re.compile(r"(r\$|us\$|\$|€|£)", re.IGNORECASE)
_DATE_LIKE = re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b")
_LABEL_FORBIDDEN_START = re.compile(r"^\s*(?:[•\-\u2013\u2014*]|\(?\d{1,2}[.)]\s)")

MAX_LABEL_WORDS = 8
MAX_LABEL_CHARS = 90


@dataclass(slots=True)
class Segment:
    """One label/value pair found inside a line."""

    label: str
    value: str
    label_words: list[Token]
    value_words: list[Token]
    separator: str
    reason_codes: list[str] = field(default_factory=list)
    confidence: float = 0.0
    alternatives: list[dict[str, str]] = field(default_factory=list)

    @property
    def label_bbox(self) -> BBox:
        return BBox.union_all(w.bbox for w in self.label_words)

    @property
    def value_bbox(self) -> BBox:
        return BBox.union_all(w.bbox for w in self.value_words)


def is_label_like(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_LABEL_CHARS:
        return False
    if _LABEL_FORBIDDEN_START.match(stripped):
        return False
    words = stripped.split()
    if not words or len(words) > MAX_LABEL_WORDS:
        return False
    if stripped.endswith((".", "!", "?")) and stripped.lower() not in _ABBREVIATIONS:
        # A full sentence is not a label.
        if len(words) > 3:
            return False
    letters = sum(ch.isalpha() for ch in stripped)
    if letters < max(2, len(stripped) * 0.45):
        return False
    if all(ch.isdigit() or ch in ".-/:, " for ch in stripped):
        return False
    if _CURRENCY.search(stripped) and len(words) == 1:
        return False
    return True


def is_value_like(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _DIGIT.search(stripped) or _CURRENCY.search(stripped) or _DATE_LIKE.search(stripped):
        return True
    return not is_label_like(stripped) or len(stripped.split()) >= 2


def _median_gap(words: list[Token]) -> float:
    gaps = [b.bbox.x0 - a.bbox.x1 for a, b in zip(words, words[1:])]
    gaps = [g for g in gaps if g >= -0.5]
    return median(gaps) if gaps else 4.0


def _separator_indexes(words: list[Token]) -> list[int]:
    indexes: list[int] = []
    for index, word in enumerate(words):
        text = word.text.strip()
        if not text:
            continue
        if index == 0 and text in {":", "="}:
            continue  # a line cannot start with a bare separator
        if text in {":", "="}:
            indexes.append(index)
        elif text.endswith(":") or text.endswith("="):
            # "10:30" is a value, not a separator
            if text.count(":") == 1 and text.endswith(":") and len(text) > 1:
                indexes.append(index)
            elif text.count("=") == 1 and text.endswith("=") and len(text) > 1:
                indexes.append(index)
    return indexes


def _is_bold(word: Token) -> bool:
    font = (word.font or "").lower()
    return "bold" in font or "black" in font or "semibold" in font or "demi" in font


def _font_break(before: Token, after: Token) -> bool:
    if before.font and after.font and before.font != after.font:
        if _is_bold(before) != _is_bold(after):
            return True
    if before.size and after.size and abs(before.size - after.size) > 1.6:
        return True
    return False


def split_label_value(line: Line) -> list[Segment]:
    """Segment a line into one or more `label: value` pairs."""
    words = [w for w in line.words if w.text.strip()]
    if len(words) < 2:
        return []
    separators = _separator_indexes(words)
    if not separators:
        return []

    median_gap = _median_gap(words)
    label_starts: list[int] = []
    lower = 0
    for position, sep_index in enumerate(separators):
        start = _label_start(words, lower, sep_index, position, median_gap)
        label_starts.append(start)
        lower = sep_index + 1

    segments: list[Segment] = []
    for position, sep_index in enumerate(separators):
        start = label_starts[position]
        label_words = _strip_separator(words[start : sep_index + 1])
        if not label_words:
            continue
        label_text = " ".join(w.text for w in label_words).strip(" :=")
        if not is_label_like(label_text):
            continue

        value_begin = sep_index + 1
        value_end = label_starts[position + 1] if position + 1 < len(separators) else len(words)
        if value_end <= value_begin:
            segments.append(
                Segment(
                    label=label_text,
                    value="",
                    label_words=label_words,
                    value_words=[],
                    separator="colon",
                    reason_codes=["empty_value", "label_only"],
                    confidence=0.6,
                )
            )
            continue

        value_words = words[value_begin:value_end]
        value_text = " ".join(w.text for w in value_words).strip()
        if not value_text:
            segments.append(
                Segment(
                    label=label_text,
                    value="",
                    label_words=label_words,
                    value_words=[],
                    separator="colon",
                    reason_codes=["empty_value", "label_only"],
                    confidence=0.6,
                )
            )
            continue

        reasons = [
            "same_line",
            "equals_separator" if words[sep_index].text.strip() == "=" else "colon_separator",
        ]
        if _font_break(label_words[-1], value_words[0]):
            reasons.append("font_style_transition")
        if label_words[0].bbox.x0 <= line.bbox.x0 + 2:
            reasons.append("label_at_line_start")
        segments.append(
            Segment(
                label=label_text,
                value=value_text,
                label_words=label_words,
                value_words=value_words,
                separator="equals" if words[sep_index].text.strip() == "=" else "colon",
                reason_codes=reasons,
                confidence=min(0.98, 0.72 + 0.06 * len(reasons)),
            )
        )

    return segments


def _label_start(
    words: list[Token],
    lower: int,
    sep_index: int,
    position: int,
    median_gap: float,
) -> int:
    """Decide where the label begins.

    * first pair on the line  -> the label may span several words, bounded by
      tight spacing and font continuity;
    * later pairs on a dense line -> labels are short: one word, optionally
      extended through a connector phrase ("Base de Apuração").

    The remaining ambiguity (a multi-word label that follows another pair with
    uniform spacing) is not resolvable without semantics; the chosen split is
    recorded as a decision in the audit together with its reason codes.
    """
    if position == 0:
        start = sep_index
        while start - 1 >= lower:
            gap = words[start].bbox.x0 - words[start - 1].bbox.x1
            if gap > max(median_gap * 2.4, median_gap + 6.0):
                break
            if _font_break(words[start - 1], words[start]):
                break
            if sep_index - (start - 1) > MAX_LABEL_WORDS:
                break
            start -= 1
        return start

    start = sep_index
    if start - 1 >= lower:
        candidate = words[start - 1].text.strip().lower().strip(":,=.")
        if candidate in CONNECTORS and not _font_break(words[start - 1], words[start]):
            start -= 1
            if (
                start - 1 >= lower
                and sep_index - (start - 1) <= 3
                and not _font_break(words[start - 1], words[start])
            ):
                start -= 1
    return start


def _strip_separator(words: list[Token]) -> list[Token]:
    cleaned: list[Token] = []
    for index, word in enumerate(words):
        text = word.text.strip()
        if text in {":", "="}:
            continue
        if index == len(words) - 1 and (text.endswith(":") or text.endswith("=")):
            trimmed = text.rstrip(":=").strip()
            if not trimmed:
                continue
            cleaned.append(
                Token(
                    element_id=word.element_id,
                    text=trimmed,
                    bbox=word.bbox,
                    page_number=word.page_number,
                    origin=word.origin,
                    font=word.font,
                    size=word.size,
                    direction=word.direction,
                    confidence=word.confidence,
                    index=word.index,
                    engine=word.engine,
                )
            )
            continue
        cleaned.append(word)
    return cleaned


def label_only(line: Line) -> Segment | None:
    """`Nome:` on its own line (value expected below)."""
    words = [w for w in line.words if w.text.strip()]
    if len(words) < 1:
        return None
    last = words[-1].text.strip()
    if not (last.endswith(":") or last.endswith("=")):
        return None
    if len(words) > MAX_LABEL_WORDS:
        return None
    label_words = _strip_separator(words)
    if not label_words:
        return None
    label_text = " ".join(w.text for w in label_words).strip()
    if not is_label_like(label_text):
        return None
    return Segment(
        label=label_text,
        value="",
        label_words=label_words,
        value_words=[],
        separator="colon",
        reason_codes=["label_only", "value_below"],
        confidence=0.65,
    )


def split_two_columns(line: Line, gap_threshold: float) -> tuple[list[Token], list[Token], float] | None:
    """Split a line at its largest internal gap, if that gap is wide enough."""
    words = [w for w in line.words if w.text.strip()]
    if len(words) < 2:
        return None
    best_index = -1
    best_gap = -1.0
    for index in range(len(words) - 1):
        gap = words[index + 1].bbox.x0 - words[index].bbox.x1
        if gap > best_gap:
            best_gap = gap
            best_index = index
    if best_index < 0 or best_gap < gap_threshold:
        return None
    if best_index == len(words) - 1:
        return None
    left = words[: best_index + 1]
    right = words[best_index + 1 :]
    return left, right, best_gap


__all__ = [
    "Segment",
    "is_label_like",
    "is_value_like",
    "split_label_value",
    "label_only",
    "split_two_columns",
    "CONNECTORS",
]
