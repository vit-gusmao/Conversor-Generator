"""Rasterisation for OCR.

The rendered bitmap is strictly temporary: it exists in memory (or in a temp
file during a single worker invocation) and is never attached to the workbook.
`pypdfium2` renders the page as *displayed*, i.e. with `/Rotate` applied, which
keeps OCR boxes in the same visual coordinate space used by the native
extractor.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from config import settings
from models.document import BBox


@dataclass(slots=True)
class RasterPage:
    page_number: int
    width_pt: float
    height_pt: float
    scale: float
    image: object  # PIL.Image.Image
    dpi: int
    #: clockwise rotation already applied to `image` (0/90/180/270)
    rotation: int = 0

    def to_points(self, pixel_bbox: tuple[float, float, float, float]) -> BBox:
        x0, y0, x1, y1 = pixel_bbox
        if self.rotation:
            x0, y0, x1, y1 = _unrotate_bbox(
                (x0, y0, x1, y1), self.rotation, self.width_px, self.height_px
            )
        return BBox(
            x0=x0 / self.scale,
            y0=y0 / self.scale,
            x1=x1 / self.scale,
            y1=y1 / self.scale,
        )

    @property
    def width_px(self) -> int:
        return int(getattr(self.image, "width", 0))

    @property
    def height_px(self) -> int:
        return int(getattr(self.image, "height", 0))

    def rotated(self, angle: int) -> "RasterPage":
        """Return a copy whose image was rotated clockwise by *angle* degrees.

        Coordinates produced on the rotated image are mapped back to the page by
        `to_points`, so token geometry stays in the original visual space — the
        reason a plain ``image.rotate()`` used to place every word outside the
        page.
        """
        angle = angle % 360
        if angle == 0:
            return self
        rotated_image = self.image.rotate(-angle, expand=True)
        width_pt, height_pt = self.width_pt, self.height_pt
        if angle in (90, 270):
            width_pt, height_pt = height_pt, width_pt
        return RasterPage(
            page_number=self.page_number,
            width_pt=width_pt,
            height_pt=height_pt,
            scale=self.scale,
            image=rotated_image,
            dpi=self.dpi,
            rotation=angle,
        )


def _unrotate_bbox(
    box: tuple[float, float, float, float], angle: int, width: int, height: int
) -> tuple[float, float, float, float]:
    """Map a box from the rotated image back to the original pixel space.

    ``width``/``height`` are the dimensions of the *rotated* image.
    """
    x0, y0, x1, y1 = box
    corners = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    mapped: list[tuple[float, float]] = []
    for x, y in corners:
        if angle == 90:  # clockwise: new_x = H-1-y, new_y = x
            mapped.append((y, width - 1 - x))
        elif angle == 180:
            mapped.append((width - 1 - x, height - 1 - y))
        elif angle == 270:  # counter-clockwise 90
            mapped.append((height - 1 - y, x))
        else:
            mapped.append((x, y))
    xs = [point[0] for point in mapped]
    ys = [point[1] for point in mapped]
    return min(xs), min(ys), max(xs), max(ys)


def _open_document(source: bytes | str | Path):
    import pypdfium2 as pdfium

    if isinstance(source, (str, Path)):
        return pdfium.PdfDocument(str(source))
    return pdfium.PdfDocument(io.BytesIO(source))


def render_page(
    source: bytes | str | Path,
    page_index: int,
    *,
    dpi: int | None = None,
    max_pixels: int | None = None,
) -> RasterPage:
    """Render a single page (0-based index) to a PIL image."""
    dpi = dpi or settings.converter_render_dpi
    max_pixels = max_pixels or settings.converter_ocr_max_pixels
    pdf = _open_document(source)
    try:
        page = pdf[page_index]
        width_pt, height_pt = page.get_size()
        scale = dpi / 72.0
        if width_pt * height_pt * scale * scale > max_pixels:
            # Downscale instead of refusing: OCR on a slightly smaller raster is
            # better than no text at all, and the audit records the effective DPI.
            scale = (max_pixels / max(1.0, width_pt * height_pt)) ** 0.5
            dpi = max(72, int(scale * 72))
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil().convert("RGB")
        return RasterPage(
            page_number=page_index + 1,
            width_pt=width_pt,
            height_pt=height_pt,
            scale=scale,
            image=image,
            dpi=dpi,
        )
    finally:
        pdf.close()


def render_region(raster: RasterPage, bbox: BBox, padding: float = 3.0):
    """Crop a region (points) from an already rendered page."""
    x0 = max(0, int((bbox.x0 - padding) * raster.scale))
    y0 = max(0, int((bbox.y0 - padding) * raster.scale))
    x1 = int((bbox.x1 + padding) * raster.scale)
    y1 = int((bbox.y1 + padding) * raster.scale)
    x1 = min(raster.image.width, max(x0 + 1, x1))
    y1 = min(raster.image.height, max(y0 + 1, y1))
    return raster.image.crop((x0, y0, x1, y1)), (x0, y0)


def image_stats(image) -> dict[str, float]:
    """Cheap statistics used to decide whether OCR is worth attempting."""
    import numpy as np

    arr = np.asarray(image.convert("L"), dtype=np.uint8)
    if arr.size == 0:
        return {"mean": 0.0, "std": 0.0, "dark_ratio": 0.0, "ink_pixels": 0.0}
    dark = float((arr < 128).mean())
    return {
        "mean": round(float(arr.mean()), 2),
        "std": round(float(arr.std()), 2),
        "dark_ratio": round(dark, 4),
        "ink_pixels": float(dark * arr.size),
    }


__all__ = ["RasterPage", "render_page", "render_region", "image_stats"]
