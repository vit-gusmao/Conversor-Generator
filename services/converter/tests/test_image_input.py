"""Image ingestion: decoders, EXIF, transparency, frames and limits."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from ingestion.base import IngestionError
from ingestion.image_input import decode_image, detect_signature, image_capabilities


def _bytes(image: Image.Image, fmt: str = "PNG", **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP"])
def test_basic_decoders(fmt):
    data = _bytes(Image.new("RGB", (40, 24), (200, 30, 30)), fmt)
    document = decode_image(data)
    assert document.page_count == 1
    assert document.pages[0].image.mode == "RGB"
    assert document.pages[0].width == 40


def test_multipage_tiff_keeps_frame_order():
    frames = [Image.new("RGB", (30, 30), color) for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    data = _bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:])
    document = decode_image(data)
    assert document.page_count == 3
    assert [page.page_number for page in document.pages] == [1, 2, 3]
    assert document.pages[2].image.getpixel((0, 0)) == (0, 0, 255)


def test_frame_limit_is_explicit():
    frames = [Image.new("RGB", (30, 30), color) for color in ((1, 2, 3), (4, 5, 6))]
    data = _bytes(frames[0], "TIFF", save_all=True, append_images=frames[1:])
    with pytest.raises(IngestionError) as info:
        decode_image(data, max_frames=1)
    assert info.value.code == "FRAME_LIMIT"


def test_exif_orientation_is_applied_once():
    image = Image.new("RGB", (40, 20), (10, 20, 30))
    exif = Image.Exif()
    exif[274] = 6  # rotate 90°
    data = _bytes(image, "JPEG", exif=exif)
    document = decode_image(data)
    page = document.pages[0]
    assert (page.width, page.height) == (20, 40)
    assert "exif_transpose" in page.transformations


def test_transparency_is_composited_on_white():
    image = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    data = _bytes(image, "PNG")
    document = decode_image(data)
    assert document.pages[0].image.getpixel((0, 0)) == (255, 255, 255)
    assert "alpha_on_white" in document.pages[0].transformations


def test_pixel_budget_is_checked_before_decode():
    data = _bytes(Image.new("RGB", (100, 100), (0, 0, 0)), "PNG")
    with pytest.raises(IngestionError) as info:
        decode_image(data, max_megapixels=0.0001)
    assert info.value.code == "PIXEL_LIMIT"


def test_unknown_content_is_rejected():
    with pytest.raises(IngestionError) as info:
        decode_image(b"not an image at all")
    assert info.value.code == "UNSUPPORTED_FORMAT"


def test_signatures_and_capabilities_are_declared():
    assert detect_signature(_bytes(Image.new("RGB", (2, 2)), "PNG")) == "PNG"
    groups = image_capabilities()
    basic = next(item for item in groups if item["group"] == "basic")
    assert basic["available"] is True
    assert "txt" in basic["outputs"]
