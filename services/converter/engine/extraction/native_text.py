"""Stage 2/3 — native text extraction with geometry.

This is *not* `page.extract_text()`.  Every word keeps its bounding box, font,
size, direction and provenance, because reconstructing columns later depends
entirely on geometry.

Coordinate system: PDF points, origin at the **top-left** of the visually
rotated page (what a human sees).  `/Rotate` is normalised here so downstream
stages never need to care.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from config import settings
from models.document import ORIGIN_NATIVE, BBox, Token, median

_REPLACEMENT = "\ufffd"


@dataclass(slots=True)
class NativePage:
    page_number: int
    width: float
    height: float
    rotation: int
    tokens: list[Token] = field(default_factory=list)
    rotated_tokens: list[Token] = field(default_factory=list)
    char_count: int = 0
    visible_char_count: int = 0
    quality: dict[str, float] = field(default_factory=dict)
    has_text_layer: bool = False
    is_blank: bool = True
    image_count: int = 0
    image_coverage: float = 0.0
    vector_count: int = 0
    has_borders: bool = False
    engine: str = "pdfplumber"

    @property
    def all_tokens(self) -> list[Token]:
        return self.tokens + self.rotated_tokens

    @property
    def has_drawings(self) -> bool:
        return self.image_count > 0 or self.vector_count > 0

    def to_metrics(self) -> dict[str, float]:
        return {
            "native_chars": float(self.char_count),
            "native_words": float(len(self.tokens)),
            "rotated_words": float(len(self.rotated_tokens)),
            "images": float(self.image_count),
            "image_coverage": round(self.image_coverage, 4),
            "vectors": float(self.vector_count),
            "quality_score": round(self.quality.get("score", 0.0), 4),
        }


def _rotate_bbox(box: BBox, rotation: int, page_w: float, page_h: float) -> BBox:
    """Map a top-left bbox from unrotated page space into visual space."""
    r = rotation % 360
    if r == 0:
        return box
    if r == 90:
        return BBox(
            x0=page_h - box.y1,
            y0=box.x0,
            x1=page_h - box.y0,
            y1=box.x1,
        )
    if r == 180:
        return BBox(
            x0=page_w - box.x1,
            y0=page_h - box.y1,
            x1=page_w - box.x0,
            y1=page_h - box.y0,
        )
    if r == 270:
        return BBox(
            x0=box.y0,
            y0=page_w - box.x1,
            x1=box.y1,
            y1=page_w - box.x0,
        )
    return box


def _visual_size(width: float, height: float, rotation: int) -> tuple[float, float]:
    r = rotation % 360
    return (height, width) if r in (90, 270) else (width, height)


def _is_white(color) -> bool:
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return float(color) >= 0.985
    if isinstance(color, (list, tuple)):
        channels = [float(c) for c in color if isinstance(c, (int, float))]
        if not channels:
            # all([]) is True: a colour with no numeric channel used to be
            # classified as invisible text and lowered the page score.
            return False
        return all(channel >= 0.985 for channel in channels)
    return False


def _extract_rotated_words(pdf_page, rotation: int, raw_w: float, raw_h: float) -> list[dict]:
    """Regroup characters in visual space for pages carrying ``/Rotate``.

    ``extract_words`` groups characters in the unrotated coordinate space, which
    reverses word order (and text order) on rotated pages.  Here every character
    box is mapped to visual coordinates first, then characters are clustered
    into lines and split into words using explicit spaces or geometric gaps.
    """
    try:
        chars = list(pdf_page.chars)
    except Exception:  # noqa: BLE001
        return []

    items: list[dict] = []
    for char in chars:
        text = char.get("text") or ""
        if text == "":
            continue
        try:
            box = BBox(
                float(char["x0"]), float(char["top"]), float(char["x1"]), float(char["bottom"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        items.append(
            {
                "text": text,
                "box": _rotate_bbox(box, rotation, raw_w, raw_h),
                "font": char.get("fontname"),
                "size": char.get("size"),
            }
        )
    if not items:
        return []

    heights = [item["box"].height for item in items if item["box"].height > 0.5]
    tol = max(1.4, (median(heights) or 8.0) * 0.55)
    items.sort(key=lambda item: (item["box"].y_center, item["box"].x0))

    lines: list[list[dict]] = []
    centers: list[float] = []
    for item in items:
        y = item["box"].y_center
        placed = False
        for index, center in enumerate(centers):
            if abs(center - y) <= tol:
                lines[index].append(item)
                centers[index] = sum(x["box"].y_center for x in lines[index]) / len(lines[index])
                placed = True
                break
        if not placed:
            lines.append([item])
            centers.append(y)

    widths = [item["box"].width for item in items if item["box"].width > 0.3]
    median_width = median(widths) or 5.0
    words: list[dict] = []

    for line in lines:
        line.sort(key=lambda item: item["box"].x0)
        current: list[dict] = []
        previous: dict | None = None
        for item in line:
            is_space = item["text"].isspace()
            gap = 0.0 if previous is None else item["box"].x0 - previous["box"].x1
            if is_space or (previous is not None and gap > median_width * 1.1):
                if current:
                    words.append(_join_char_run(current))
                    current = []
                previous = item
                continue
            current.append(item)
            previous = item
        if current:
            words.append(_join_char_run(current))

    return words


def _join_char_run(run: list[dict]) -> dict:
    texts = [item["text"] for item in run]
    fonts = [item["font"] for item in run if item["font"]]
    sizes = [float(item["size"]) for item in run if item["size"]]
    boxes = [item["box"] for item in run]
    union = BBox.union_all(boxes)
    return {
        "text": "".join(texts),
        "x0": union.x0,
        "top": union.y0,
        "x1": union.x1,
        "bottom": union.y1,
        "fontname": max(set(fonts), key=fonts.count) if fonts else None,
        "size": median(sizes) if sizes else None,
        "upright": True,
    }


def extract_native(pdf_page, page_number: int) -> NativePage:
    rotation = int(getattr(pdf_page, "rotation", 0) or 0) % 360
    raw_w = float(getattr(pdf_page, "width", 0.0) or 0.0)
    raw_h = float(getattr(pdf_page, "height", 0.0) or 0.0)
    width, height = _visual_size(raw_w, raw_h, rotation)

    page = NativePage(
        page_number=page_number,
        width=round(width, 2),
        height=round(height, 2),
        rotation=rotation,
        vector_count=len(getattr(pdf_page, "lines", []) or [])
        + len(getattr(pdf_page, "rects", []) or [])
        + len(getattr(pdf_page, "curves", []) or []),
        has_borders=bool(
            len(getattr(pdf_page, "lines", []) or []) + len(getattr(pdf_page, "rects", []) or []) >= 4
        ),
    )

    # -- images ------------------------------------------------------------
    images = getattr(pdf_page, "images", []) or []
    page.image_count = len(images)
    if images and width > 0 and height > 0:
        coverage = 0.0
        for image in images:
            try:
                box = BBox(float(image["x0"]), float(image["top"]), float(image["x1"]), float(image["bottom"]))
                box = _rotate_bbox(box, rotation, raw_w, raw_h)
                coverage = max(coverage, box.overlap_ratio(BBox(0, 0, width, height)))
            except Exception:  # noqa: BLE001
                continue
        page.image_coverage = min(1.0, coverage)

    # -- words -------------------------------------------------------------
    words: list[dict] = []
    already_visual = rotation != 0
    if already_visual:
        # `extract_words` groups characters in the *unrotated* coordinate
        # space, which reverses words on a rotated page.  We regroup the
        # characters ourselves in visual space instead.
        words = _extract_rotated_words(pdf_page, rotation, raw_w, raw_h)
    else:
        try:
            words = pdf_page.extract_words(
                keep_blank_chars=False,
                use_text_flow=False,
                x_tolerance=1.6,
                y_tolerance=2.4,
                extra_attrs=["fontname", "size", "upright", "non_stroking_color"],
            )
        except Exception:  # noqa: BLE001 - attribute set differs across versions
            try:
                words = pdf_page.extract_words(
                    keep_blank_chars=False,
                    x_tolerance=1.6,
                    y_tolerance=2.4,
                    extra_attrs=["fontname", "size"],
                )
            except Exception:  # noqa: BLE001
                words = []

    char_count = 0
    visible_chars = 0
    invisible = 0
    replacement = 0
    control = 0
    tiny = 0
    fonts: list[str] = []
    sizes: list[float] = []

    index = 0
    for word in words:
        text = (word.get("text") or "").strip()
        if not text:
            continue
        try:
            box = BBox(
                float(word["x0"]), float(word["top"]), float(word["x1"]), float(word["bottom"])
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not already_visual:
            box = _rotate_bbox(box, rotation, raw_w, raw_h)
        upright = bool(word.get("upright", True))
        size = float(word["size"]) if word.get("size") else None
        font = str(word["fontname"]) if word.get("fontname") else None

        char_count += len(text)
        replacement += text.count(_REPLACEMENT)
        control += sum(1 for ch in text if unicodedata.category(ch) in {"Cc", "Co", "Cn"} and ch not in "\t\n\r")
        if _is_white(word.get("non_stroking_color")):
            invisible += len(text)
        else:
            visible_chars += len(text)
        if size is not None and size < 4.0:
            tiny += len(text)
        if font:
            fonts.append(font)
        if size:
            sizes.append(size)

        index += 1
        token = Token(
            element_id=f"txt_{page_number:03d}_{index:05d}",
            text=text,
            bbox=box,
            page_number=page_number,
            origin=ORIGIN_NATIVE,
            font=font,
            size=round(size, 2) if size else None,
            direction="ltr" if upright else "rotated",
            confidence=1.0,
            index=index,
            engine="pdfplumber",
        )
        if upright:
            page.tokens.append(token)
        else:
            page.rotated_tokens.append(token)

    page.char_count = char_count
    page.visible_char_count = visible_chars
    page.quality = _assess_quality(
        page,
        replacement=replacement,
        control=control,
        invisible=invisible,
        tiny=tiny,
        fonts=fonts,
        sizes=sizes,
    )
    page.has_text_layer = char_count >= 20 and visible_chars >= 10
    page.is_blank = char_count < 5 and page.image_count == 0 and page.vector_count < 5
    return page


def _assess_quality(
    page: NativePage,
    *,
    replacement: int,
    control: int,
    invisible: int,
    tiny: int,
    fonts: list[str],
    sizes: list[float],
) -> dict[str, float]:
    """Heuristic health score of the text layer, 0 (useless) .. 1 (clean)."""
    total = max(1, page.char_count)
    score = 1.0

    replacement_ratio = replacement / total
    control_ratio = control / total
    invisible_ratio = invisible / total
    tiny_ratio = tiny / total

    score -= min(0.6, replacement_ratio * 4.0)
    score -= min(0.4, control_ratio * 4.0)
    score -= min(0.5, invisible_ratio * 2.0)
    score -= min(0.3, tiny_ratio * 2.0)

    long_words = sum(1 for t in page.tokens if len(t.text) > 45)
    glued_ratio = long_words / max(1, len(page.tokens))
    score -= min(0.25, glued_ratio * 1.5)

    # duplicate / overlapping words: a classic symptom of broken ToUnicode maps
    seen: dict[tuple[int, int], str] = {}
    duplicates = 0
    for token in page.tokens:
        key = (int(token.bbox.x0 / 2), int(token.bbox.y0 / 2))
        if key in seen:
            # Same position: whether the text matches or not, the layer is
            # duplicated. Ignoring the identical case hid the very symptom it
            # was meant to detect.
            duplicates += 1
        else:
            seen[key] = token.text
    duplicate_ratio = duplicates / max(1, len(page.tokens))
    score -= min(0.3, duplicate_ratio)

    unique_fonts = len(set(fonts))
    median_size = median(sizes) if sizes else 0.0

    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 4),
        "replacement_ratio": round(replacement_ratio, 4),
        "control_ratio": round(control_ratio, 4),
        "invisible_ratio": round(invisible_ratio, 4),
        "tiny_ratio": round(tiny_ratio, 4),
        "duplicate_ratio": round(duplicate_ratio, 4),
        "glued_ratio": round(glued_ratio, 4),
        "fonts": float(unique_fonts),
        "median_font_size": round(median_size, 2),
    }


def classify_page(native: NativePage) -> str:
    """native | scan | hybrid | image | empty.

    Classification is deliberately *per page*: a single PDF often mixes a
    native cover page with scanned attachments.
    """
    if native.is_blank:
        return "empty"
    if native.has_text_layer and native.image_coverage >= 0.55 and native.char_count < 80:
        return "hybrid"
    if native.has_text_layer:
        return "native"
    if native.tokens:
        # Few characters but real tokens: the text is exported, so calling the
        # page "empty" contradicted the workbook.
        return "native"
    if native.image_count > 0 or native.vector_count > 40:
        return "scan"
    return "empty"


def needs_ocr(native: NativePage) -> bool:
    """Whether OCR should be attempted for this page.

    The global switch is applied by the caller (`PipelineOptions.ocr_enabled`),
    so the same function can be unit-tested in both modes.
    """
    if native.is_blank:
        return False
    if not native.has_text_layer:
        return native.image_count > 0 or native.vector_count > 10
    return native.quality.get("score", 1.0) < 0.55


__all__ = ["NativePage", "extract_native", "classify_page", "needs_ocr"]
