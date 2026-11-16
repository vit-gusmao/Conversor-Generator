"""Adaptive image preprocessing for OCR.

Every operation is optional and recorded in the audit.  Binarisation is *not*
applied blindly: several OCR models do better on the original grayscale or
colour image, so we keep the original and only transform when the heuristic
says the page is low contrast or skewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.ocr.rasterize import image_stats


@dataclass(slots=True)
class PreprocessResult:
    image: object
    operations: list[str] = field(default_factory=list)
    parameters: dict[str, float] = field(default_factory=dict)
    skipped_reason: str | None = None


def _cv2():
    try:
        import cv2  # type: ignore[import-not-found]

        return cv2
    except ModuleNotFoundError:
        return None


def _np():
    import numpy as np

    return np


def preprocess(image, *, aggressive: bool = False) -> PreprocessResult:
    """Return a possibly enhanced copy of `image` plus the operations applied."""
    cv2 = _cv2()
    stats = image_stats(image)
    operations: list[str] = []
    params: dict[str, float] = {"input_std": stats["std"], "input_dark_ratio": stats["dark_ratio"]}

    if cv2 is None:
        # Pillow-only fallback: autocontrast + grayscale.
        from PIL import ImageOps

        out = ImageOps.autocontrast(image.convert("L"), cutoff=1).convert("RGB")
        operations.append("pillow_autocontrast")
        return PreprocessResult(image=out, operations=operations, parameters=params)

    np = _np()
    arr = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    # 1) deskew — only when there is a clear dominant angle
    angle = _estimate_skew(gray, cv2, np)
    if angle is not None and 0.4 <= abs(angle) <= 12.0:
        rotated = _rotate(gray, angle, cv2)
        if rotated is not None:
            gray = rotated
            operations.append("deskew")
            params["skew_degrees"] = round(angle, 3)

    # 2) contrast normalisation for washed-out scans
    if stats["std"] < 55 or stats["dark_ratio"] < 0.005:
        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        operations.append("clahe_contrast")

    # 3) moderate denoise
    if stats["std"] > 28:
        gray = cv2.fastNlMeansDenoising(gray, None, h=7, templateWindowSize=7, searchWindowSize=21)
        operations.append("denoise")

    # 4) adaptive threshold — only when the page looks like a document scan
    if aggressive or stats["dark_ratio"] > 0.01:
        binary = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            max(3, int(8 + (1.0 - stats["dark_ratio"]) * 4)),
        )
        # If thresholding removed almost everything, keep the grayscale version.
        ink = float((binary < 128).mean())
        params["binary_ink_ratio"] = round(ink, 4)
        if ink > 0.0005:
            gray = binary
            operations.append("adaptive_threshold")

    out = cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    return PreprocessResult(image=out, operations=operations, parameters=params)


def _estimate_skew(gray, cv2, np) -> float | None:
    try:
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180, threshold=120, minLineLength=max(80, gray.shape[1] // 4), maxLineGap=12
        )
        if lines is None or len(lines) < 6:
            return None
        angles = []
        for line in lines[:200]:
            x1, y1, x2, y2 = line[0]
            if x2 == x1:
                continue
            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(angle) <= 15:
                angles.append(float(angle))
        if len(angles) < 4:
            return None
        return float(np.median(angles))
    except Exception:  # noqa: BLE001
        return None


def _rotate(gray, angle: float, cv2):
    try:
        height, width = gray.shape[:2]
        center = (width / 2.0, height / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        return cv2.warpAffine(
            gray,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    except Exception:  # noqa: BLE001
        return None


def detect_orientation_hint(image) -> int | None:
    """Best-effort 0/90/180/270 hint. Returns None when inconclusive."""
    cv2 = _cv2()
    if cv2 is None:
        return None
    try:
        np = _np()
        gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        # Project ink onto both axes: upright text has high variance on rows and
        # low variance on columns relative to rotated text.
        ink = (gray < 128).astype("float32")
        row_profile = ink.mean(axis=1)
        col_profile = ink.mean(axis=0)
        row_score = float(np.var(np.diff(row_profile)))
        col_score = float(np.var(np.diff(col_profile)))
        if row_score <= 0:
            return None
        ratio = col_score / row_score
        if ratio > 1.8:
            return 90
        return 0
    except Exception:  # noqa: BLE001
        return None


__all__ = ["PreprocessResult", "preprocess", "detect_orientation_hint"]
