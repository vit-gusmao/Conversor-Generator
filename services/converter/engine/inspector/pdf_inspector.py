"""Stage 1 — document inspection.

The PDF is untrusted input.  Before any content is read we determine: is it
really a PDF, can the parser open it, is it encrypted, how many pages, does it
carry interactive forms, and does it contain active content (JavaScript,
launch actions, embedded files) that must never be executed.

Nothing in this module executes embedded code; active content is *detected and
recorded*, then ignored.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from config import settings

PDF_MAGIC = b"%PDF-"
_DANGEROUS_TOKENS = {
    "javascript": (b"/JavaScript", b"/JS"),
    "embedded_files": (b"/EmbeddedFiles", b"/EmbeddedFile"),
    "launch": (b"/Launch",),
    "open_action": (b"/OpenAction",),
    "rich_media": (b"/RichMedia",),
    "xfa": (b"/XFA",),
}


@dataclass(slots=True)
class FormField:
    name: str
    value: str | None
    field_type: str | None
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    label_guess: str | None = None


@dataclass(slots=True)
class PDFInspection:
    ok: bool
    is_pdf: bool
    pdf_version: str | None = None
    page_count: int = 0
    encrypted: bool = False
    needs_password: bool = False
    has_forms: bool = False
    form_fields: list[FormField] = field(default_factory=list)
    has_javascript: bool = False
    has_embedded_files: bool = False
    has_launch_actions: bool = False
    has_open_action: bool = False
    has_xfa: bool = False
    rotation_pages: list[int] = field(default_factory=list)
    page_sizes: list[tuple[float, float, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    active_content: dict[str, bool] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False

    def to_report(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "is_pdf": self.is_pdf,
            "pdf_version": self.pdf_version,
            "page_count": self.page_count,
            "encrypted": self.encrypted,
            "needs_password": self.needs_password,
            "has_forms": self.has_forms,
            "has_javascript": self.has_javascript,
            "has_embedded_files": self.has_embedded_files,
            "has_launch_actions": self.has_launch_actions,
            "has_open_action": self.has_open_action,
            "has_xfa": self.has_xfa,
            "active_content": dict(self.active_content),
            "truncated": self.truncated,
            "rotation_pages": self.rotation_pages,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def sniff_is_pdf(data: bytes) -> bool:
    """Magic-byte check with tolerance for a small leading junk prefix."""
    if not data:
        return False
    if data.startswith(PDF_MAGIC):
        return True
    head = data[:1024]
    idx = head.find(PDF_MAGIC)
    return 0 <= idx <= 512


def _decompressed_streams(data: bytes, *, limit: int = 64) -> bytes:
    """Inflate FlateDecode streams so active content inside /ObjStm is visible.

    A plain byte scan only sees the compressed bytes: JavaScript or a Launch
    action stored in an object stream would pass unnoticed.
    """
    import re as _re
    import zlib

    out: list[bytes] = []
    scanned = 0
    for match in _re.finditer(rb"stream\r?\n", data):
        if scanned >= limit:
            break
        scanned += 1
        start = match.end()
        end = data.find(b"endstream", start)
        if end <= start:
            continue
        chunk = data[start:end]
        try:
            inflated = zlib.decompress(chunk)
        except Exception:  # noqa: BLE001
            continue
        if len(inflated) <= 2_000_000:
            out.append(inflated)
    return b"".join(out)


def _pdf_version(data: bytes) -> str | None:
    match = re.match(rb"%PDF-(\d\.\d)", data[:16])
    return match.group(1).decode("ascii") if match else None


INSPECT_BYTES = 64 * 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024


def _apply_active_content(inspection: PDFInspection, scan: bytes) -> None:
    inspection.active_content = {
        key: any(token in scan for token in tokens) for key, tokens in _DANGEROUS_TOKENS.items()
    }
    inspection.has_javascript = inspection.active_content.get("javascript", False)
    inspection.has_embedded_files = inspection.active_content.get("embedded_files", False)
    inspection.has_launch_actions = inspection.active_content.get("launch", False)
    inspection.has_open_action = inspection.active_content.get("open_action", False)
    inspection.has_xfa = inspection.active_content.get("xfa", False)


def _scan_active_content(path) -> dict[str, bool]:
    """Chunked scan for active content in a large file.

    Uncompressed markers (the common case) are detected exactly. Compressed
    object streams are not inflated here: the file is never executed, and the
    result is a warning for the operator, so the bounded scan is preferred over
    loading a multi-hundred-megabyte document into memory.
    """
    found = {key: False for key in _DANGEROUS_TOKENS}
    carry = b""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_SCAN_CHUNK)
            if not chunk:
                break
            window = carry + chunk
            for key, tokens in _DANGEROUS_TOKENS.items():
                if found[key]:
                    continue
                if any(token in window for token in tokens):
                    found[key] = True
            carry = window[-64:]
    return found


def inspect_pdf(data: bytes) -> PDFInspection:
    inspection = PDFInspection(ok=False, is_pdf=sniff_is_pdf(data))
    if not inspection.is_pdf:
        inspection.errors.append("O arquivo não possui a assinatura de um PDF válido.")
        return inspection

    inspection.pdf_version = _pdf_version(data)
    inspection.truncated = b"%%EOF" not in data[-2048:]
    scan = data + _decompressed_streams(data)
    inspection.active_content = {
        key: any(token in scan for token in tokens) for key, tokens in _DANGEROUS_TOKENS.items()
    }
    inspection.has_javascript = inspection.active_content.get("javascript", False)
    inspection.has_embedded_files = inspection.active_content.get("embedded_files", False)
    inspection.has_launch_actions = inspection.active_content.get("launch", False)
    inspection.has_open_action = inspection.active_content.get("open_action", False)
    inspection.has_xfa = inspection.active_content.get("xfa", False)

    if len(data) > settings.max_bytes():
        inspection.errors.append(
            f"O arquivo excede o limite de {settings.max_bytes() // (1024 * 1024)} MiB."
        )

    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - dependency is declared
        inspection.errors.append("Biblioteca de leitura de PDF indisponível (pypdf).")
        return inspection

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as exc:  # noqa: BLE001 - any parser failure is a validation failure
        inspection.errors.append(f"O PDF não pôde ser aberto: {exc}")
        return inspection

    return _inspect_reader(inspection, reader)



def _inspect_reader(inspection: PDFInspection, reader) -> PDFInspection:
    """Everything that needs an open `PdfReader`.

    Shared by the in-memory and path-based entry points so both apply exactly
    the same encryption, corruption, form and page checks.
    """

    if reader.is_encrypted:
        inspection.encrypted = True
        try:
            # Many PDFs are encrypted with an empty user password (owner-only
            # restrictions).  Try that before asking the user for a password.
            opened = reader.decrypt("")
            if not opened:
                inspection.needs_password = True
                inspection.errors.append(
                    "O PDF está protegido por senha e não pode ser processado sem a senha."
                )
                return inspection
            inspection.warnings.append(
                "PDF criptografado com senha de usuário vazia; restrições de proprietário ignoradas para leitura."
            )
        except Exception:  # noqa: BLE001
            inspection.needs_password = True
            inspection.errors.append("O PDF está protegido por senha.")
            return inspection

    try:
        inspection.page_count = len(reader.pages)
    except Exception as exc:  # noqa: BLE001
        inspection.errors.append(f"Não foi possível contar as páginas: {exc}")
        return inspection

    if inspection.page_count == 0:
        inspection.errors.append("O PDF não contém páginas.")
        return inspection

    # There is no page ceiling: every page of the document is processed. The
    # page count is reported so callers can size their own budgets.
    limit = settings.max_pages()
    if limit is not None and inspection.page_count > limit:
        inspection.warnings.append(
            f"O documento possui {inspection.page_count} páginas; o limite legado de "
            f"{limit} páginas está configurado, mas não é aplicado ao fluxo incremental."
        )

    for index, page in enumerate(reader.pages, start=1):
        try:
            box = page.mediabox
            width = float(box.width)
            height = float(box.height)
            rotation = int(page.get("/Rotate", 0) or 0) % 360
        except Exception:  # noqa: BLE001
            width, height, rotation = 0.0, 0.0, 0
            inspection.warnings.append(f"Página {index}: dimensões ilegíveis.")
        inspection.page_sizes.append((round(width, 2), round(height, 2), rotation))
        if rotation:
            inspection.rotation_pages.append(index)

    metadata = {}
    try:
        raw_meta = reader.metadata or {}
        for key, value in raw_meta.items():
            clean_key = str(key).lstrip("/")
            try:
                metadata[clean_key] = str(value)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        inspection.warnings.append("Metadados do documento ilegíveis.")
    inspection.metadata = metadata

    # -- interactive forms --------------------------------------------------
    try:
        fields = reader.get_fields() or {}
        if fields:
            inspection.has_forms = True
            inspection.form_fields = _extract_form_fields(reader, fields)
    except Exception as exc:  # noqa: BLE001
        inspection.warnings.append(f"Formulários AcroForm não puderam ser lidos: {exc}")

    if inspection.has_javascript:
        inspection.warnings.append(
            "O documento contém JavaScript embutido; nenhum código será executado."
        )
    if inspection.has_embedded_files:
        inspection.warnings.append("O documento contém arquivos anexados; eles serão ignorados.")
    if inspection.has_launch_actions:
        inspection.warnings.append("O documento contém ações de execução; elas serão ignoradas.")
    if inspection.has_open_action:
        inspection.warnings.append(
            "O documento define uma ação automática de abertura; ela será ignorada."
        )
    if inspection.has_xfa:
        inspection.warnings.append(
            "O documento contém um formulário XFA; apenas os campos AcroForm são lidos."
        )

    inspection.ok = not inspection.errors
    return inspection

def inspect_pdf_path(path) -> PDFInspection:
    """Inspects a PDF on disk, without loading huge documents into memory.

    Small files (up to `INSPECT_BYTES`) go through the in-memory path so the
    full active-content scan, including decompressed streams, is preserved.
    Larger files stay on disk: pypdf reads lazily and the active-content scan
    is chunked.
    """
    from pathlib import Path

    target = Path(path)
    size = target.stat().st_size
    if size <= INSPECT_BYTES:
        with target.open("rb") as handle:
            return inspect_pdf(handle.read())

    with target.open("rb") as handle:
        head = handle.read(1024)
        handle.seek(max(0, size - 2048))
        tail = handle.read()

    inspection = PDFInspection(ok=False, is_pdf=sniff_is_pdf(head))
    if not inspection.is_pdf:
        inspection.errors.append("O arquivo não possui a assinatura de um PDF válido.")
        return inspection

    inspection.pdf_version = _pdf_version(head)
    inspection.truncated = b"%%EOF" not in tail
    _apply_active_content(inspection, b"")
    inspection.active_content = _scan_active_content(target)
    inspection.has_javascript = inspection.active_content.get("javascript", False)
    inspection.has_embedded_files = inspection.active_content.get("embedded_files", False)
    inspection.has_launch_actions = inspection.active_content.get("launch", False)
    inspection.has_open_action = inspection.active_content.get("open_action", False)
    inspection.has_xfa = inspection.active_content.get("xfa", False)

    if size > settings.max_bytes():
        inspection.errors.append(
            f"O arquivo excede o limite de {settings.max_bytes() // (1024 * 1024)} MiB."
        )

    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - dependency is declared
        inspection.errors.append("Biblioteca de leitura de PDF indisponível (pypdf).")
        return inspection

    try:
        reader = PdfReader(str(target), strict=False)
    except Exception as exc:  # noqa: BLE001
        inspection.errors.append(f"O PDF não pôde ser aberto: {exc}")
        return inspection

    return _inspect_reader(inspection, reader)


def _extract_form_fields(reader, fields: dict) -> list[FormField]:
    """Merge declared fields with widget annotations to recover labels/geometry."""
    widgets: dict[str, tuple[int, tuple[float, float, float, float]]] = {}
    for page_index, page in enumerate(reader.pages, start=1):
        try:
            height = float(page.mediabox.height)
            annots = page.get("/Annots") or []
            for annot in annots:
                try:
                    obj = annot.get_object()
                except Exception:  # noqa: BLE001
                    continue
                name = obj.get("/T")
                if not name:
                    parent = obj.get("/Parent")
                    if parent is not None:
                        try:
                            name = parent.get_object().get("/T")
                        except Exception:  # noqa: BLE001
                            name = None
                rect = obj.get("/Rect")
                if not name or not rect:
                    continue
                x0, y0, x1, y1 = (float(v) for v in rect)
                widgets[str(name)] = (
                    page_index,
                    (min(x0, x1), height - max(y0, y1), max(x0, x1), height - min(y0, y1)),
                )
        except Exception:  # noqa: BLE001
            continue

    result: list[FormField] = []
    for name, spec in fields.items():
        value = spec.get("/V")
        field_type = str(spec.get("/FT")) if spec.get("/FT") else None
        page_number, bbox = widgets.get(name, (None, None))
        result.append(
            FormField(
                name=str(name),
                value=str(value) if value is not None else None,
                field_type=field_type,
                page_number=page_number,
                bbox=bbox,
            )
        )
    return result


__all__ = ["PDFInspection", "FormField", "inspect_pdf", "sniff_is_pdf"]
