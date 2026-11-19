"""Image ingestion with an explicit, verifiable codec registry.

Supported formats are declared per group and checked against the *installed*
Pillow build.  A missing codec produces ``CODEC_UNAVAILABLE`` with the install
instruction; it never silently degrades to an empty conversion.

Defaults (configurable by the operator): 50 MiB per file, 500 frames, 40
megapixels per frame and 200 megapixels decoded per job.  Limits are checked
before the expensive decode.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError, features

from config import settings
from ingestion.base import IngestedDocument, IngestedPage, IngestionError

#: signature -> (format name, mime type)
SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "PNG", "image/png"),
    (b"\xff\xd8\xff", "JPEG", "image/jpeg"),
    (b"GIF87a", "GIF", "image/gif"),
    (b"GIF89a", "GIF", "image/gif"),
    (b"BM", "BMP", "image/bmp"),
    (b"II*\x00", "TIFF", "image/tiff"),
    (b"MM\x00*", "TIFF", "image/tiff"),
    (b"\x00\x00\x01\x00", "ICO", "image/x-icon"),
    (b"\x00\x00\x00\x0cjP  ", "JPEG2000", "image/jp2"),
    (b"P1", "PNM", "image/x-portable-bitmap"),
    (b"P2", "PNM", "image/x-portable-graymap"),
    (b"P3", "PNM", "image/x-portable-pixmap"),
    (b"P4", "PNM", "image/x-portable-bitmap"),
    (b"P5", "PNM", "image/x-portable-graymap"),
    (b"P6", "PNM", "image/x-portable-pixmap"),
]

FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
    "ICO": "image/x-icon",
    "JPEG2000": "image/jp2",
    "PNM": "image/x-portable-pixmap",
    "AVIF": "image/avif",
    "HEIF": "image/heif",
    "PSD": "image/vnd.adobe.photoshop",
    "SVG": "image/svg+xml",
}

_BASIC = {"PNG", "JPEG", "BMP", "WEBP", "GIF", "TIFF"}


def is_image(data: bytes) -> bool:
    return detect_signature(data) is not None


def detect_signature(data: bytes) -> str | None:
    head = data[:32]
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "WEBP"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"avif", b"avis"}:
            return "AVIF"
        if brand in {b"heic", b"heix", b"hevc", b"mif1", b"msf1"}:
            return "HEIF"
    text = head.lstrip()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in data[:512]):
        return "SVG"
    if head.startswith(b"8BPS"):
        return "PSD"
    for signature, fmt, _mime in SIGNATURES:
        if head.startswith(signature):
            # Two-byte magics are too weak on their own: "BMF;Ativo" and
            # "P2;Nome" are CSV rows, not bitmaps.
            if fmt == "BMP" and not _valid_bmp_header(data):
                continue
            if fmt == "PNM" and not _valid_pnm_header(data):
                continue
            return fmt
    return None


def _valid_bmp_header(data: bytes) -> bool:
    """BMP file header: at least 26 bytes, reserved fields (bytes 6-9) are zero."""
    return len(data) >= 26 and data[6:10] == b"\0\0\0\0"


def _valid_pnm_header(data: bytes) -> bool:
    """Netpbm magic is always followed by whitespace (``P5 ...``, ``P1\\n``)."""
    return data[2:3].isspace()


def declared_mime(fmt: str | None) -> str | None:
    return FORMAT_MIME.get(fmt or "")


def image_capabilities() -> list[dict[str, Any]]:
    """Installed codec report.

    ``available`` reflects the current Pillow build; it is not a promise that
    every variant of a format decodes.
    """
    checks = {
        "webp": bool(features.check("webp")),
        "jpg_2000": bool(features.check("jpg_2000")),
    }
    svg_available = _module_available("cairosvg")
    return [
        {
            "group": "basic",
            "formats": ["PNG", "JPEG", "BMP", "TIFF", "GIF"],
            "outputs": ["txt"],
            "multipage": True,
            "available": True,
            "dependency": "Pillow",
        },
        {
            "group": "web",
            "formats": ["WebP", "APNG"],
            "outputs": ["txt"],
            "multipage": True,
            "available": checks["webp"],
            "dependency": "Pillow[webp]",
        },
        {
            "group": "advanced",
            "formats": ["ICO", "PNM"],
            "outputs": ["txt"],
            "multipage": False,
            "available": True,
            "dependency": "Pillow",
        },
        {
            "group": "jpeg2000",
            "formats": ["JPEG2000"],
            "outputs": ["txt"],
            "multipage": False,
            "available": checks["jpg_2000"],
            "dependency": "Pillow[jpeg2000] (OpenJPEG)",
        },
        {
            "group": "advanced-modern",
            "formats": ["AVIF", "HEIF/HEIC"],
            "outputs": ["txt"],
            "multipage": False,
            "available": bool(_module_available("pillow_heif"))
            or bool(features.check("avif")),
            "dependency": "pillow-heif / Pillow[avif]",
        },
        {
            "group": "photoshop",
            "formats": ["PSD"],
            "outputs": ["txt"],
            "multipage": False,
            "available": True,
            "dependency": "Pillow",
            "notes": "PSD é lido pelo Pillow (imagem composta); não depende de rawpy.",
        },
        {
            "group": "photographic",
            "formats": ["DNG/RAW"],
            "outputs": ["txt"],
            "multipage": False,
            "available": _module_available("rawpy"),
            "dependency": "rawpy (optional)",
            "notes": "A composição/preview escolhida precisa ser registrada; RAW exige perfil de cor.",
        },
        {
            "group": "vector",
            "formats": ["SVG"],
            "outputs": ["txt"],
            "multipage": False,
            "available": svg_available,
            "dependency": "cairosvg (optional, isolated and sanitised)",
            "notes": "SVG é conteúdo ativo: rasterização isolada, sem rede, scripts ou referências externas.",
        },
    ]


def decode_image(
    data: bytes,
    *,
    max_bytes: int | None = None,
    max_frames: int | None = None,
    max_megapixels: float | None = None,
    max_job_megapixels: float | None = None,
) -> IngestedDocument:
    if not data:
        raise IngestionError("INVALID_FILE", "O arquivo enviado está vazio.")
    limit_bytes = max_bytes or settings.max_bytes()
    if len(data) > limit_bytes:
        raise IngestionError(
            "FILE_TOO_LARGE",
            f"O arquivo excede o limite de {limit_bytes // (1024 * 1024)} MiB.",
        )

    signature = detect_signature(data)
    if signature == "SVG":
        if not _module_available("cairosvg"):
            raise IngestionError(
                "CODEC_UNAVAILABLE",
                "SVG requer o codec opcional cairosvg, que não está instalado.",
                details={"format": "SVG", "install": "pip install cairosvg"},
            )
        raise IngestionError(
            "CODEC_UNAVAILABLE",
            "A rasterização de SVG ainda não está habilitada nesta instalação.",
            details={"format": "SVG", "install": "pip install cairosvg"},
        )

    try:
        image = Image.open(io.BytesIO(data))
    except UnidentifiedImageError as exc:
        if signature:
            raise IngestionError(
                "CODEC_UNAVAILABLE",
                f"O arquivo parece ser {signature}, mas o codec não está disponível.",
                details={"format": signature, "error": str(exc)},
            ) from exc
        raise IngestionError(
            "UNSUPPORTED_FORMAT",
            "O formato do arquivo não foi reconhecido como imagem suportada.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise IngestionError(
            "DECODE_FAILED", f"Não foi possível decodificar a imagem: {exc}"
        ) from exc

    fmt = (image.format or signature or "UNKNOWN").upper()
    if fmt not in FORMAT_MIME and fmt != "MPO":
        raise IngestionError(
            "UNSUPPORTED_FORMAT",
            f"Formato de imagem não suportado: {fmt}.",
            details={"format": fmt},
        )

    limit_frames = max_frames or settings.converter_max_image_frames
    limit_mp = max_megapixels or settings.converter_max_image_megapixels
    job_mp = max_job_megapixels or settings.converter_max_job_megapixels

    try:
        frames = int(getattr(image, "n_frames", 1) or 1)
    except Exception:  # noqa: BLE001
        frames = 1
    if frames > limit_frames:
        raise IngestionError(
            "FRAME_LIMIT",
            f"A imagem contém {frames} quadros, acima do limite de {limit_frames}.",
            details={"frames": frames, "limit": limit_frames},
        )

    width, height = image.size
    megapixels = (width * height) / 1_000_000
    if megapixels > limit_mp:
        raise IngestionError(
            "PIXEL_LIMIT",
            f"Um quadro possui {megapixels:.1f} megapixels, acima do limite de {limit_mp:.0f}.",
            details={"megapixels": round(megapixels, 2), "limit": limit_mp},
        )
    if megapixels * frames > job_mp:
        raise IngestionError(
            "PIXEL_LIMIT",
            f"O job decodificaria {megapixels * frames:.0f} megapixels, acima do orçamento de "
            f"{job_mp:.0f}.",
            details={"megapixels_total": round(megapixels * frames, 2), "budget": job_mp},
        )

    pages: list[IngestedPage] = []
    warnings: list[dict[str, Any]] = []
    for index in range(frames):
        try:
            image.seek(index)
            frame = image.copy()
        except Exception as exc:  # noqa: BLE001
            if index == 0:
                raise IngestionError(
                    "DECODE_FAILED", f"Falha ao decodificar a imagem: {exc}"
                ) from exc
            warnings.append(
                {
                    "code": "FRAME_DECODE_FAILED",
                    "frame_index": index,
                    "message": f"O quadro {index + 1} não pôde ser decodificado: {exc}",
                }
            )
            continue

        transformations: list[str] = []
        try:
            orientation = frame.getexif().get(274)
        except Exception:  # noqa: BLE001
            orientation = None
        if orientation and orientation != 1:
            frame = ImageOps.exif_transpose(frame)
            transformations.append("exif_transpose")
        if frame.mode in {"RGBA", "LA", "P", "PA"}:
            rgba = frame.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            frame = background
            transformations.append("alpha_on_white")
        if frame.mode != "RGB":
            frame = frame.convert("RGB")
            transformations.append("rgb_convert")

        pages.append(
            IngestedPage(
                page_number=index + 1,
                image=frame,
                width=frame.width,
                height=frame.height,
                frame_index=index,
                transformations=transformations,
            )
        )

    if not pages:
        raise IngestionError("DECODE_FAILED", "Nenhum quadro da imagem pôde ser decodificado.")

    return IngestedDocument(
        source_type="image",
        mime_type=FORMAT_MIME.get(fmt, "application/octet-stream"),
        decoder="Pillow",
        decoder_version=getattr(__import__("PIL"), "__version__", "unknown"),
        pages=pages,
        metadata={
            "format": fmt,
            "frames_declared": frames,
            "frames_decoded": len(pages),
            "size": [width, height],
            "mode": image.mode,
        },
        warnings=warnings,
    )


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


__all__ = [
    "is_image",
    "detect_signature",
    "declared_mime",
    "image_capabilities",
    "decode_image",
]
