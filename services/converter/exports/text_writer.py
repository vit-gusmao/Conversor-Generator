"""TXT exporter.

Writes the textual IR as UTF-8 (no BOM by default) with LF line endings.  Page
separators are explicit and recorded in the manifest, so a consumer can tell an
inserted separator from a character that existed in the document.

Offsets are counted in Unicode code points of the final file, never in bytes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from exports.base import MIME_TYPES, ExportError, ExportOptions, ExportResult, validate_options
from exports.text_plan import TextArtifactPlan

_UNSAFE = re.compile(r"[^\w.\- ()]+", re.UNICODE)


@dataclass(slots=True)
class AssembledText:
    text: str
    segments: list[dict[str, Any]] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def assemble_text(
    plan: TextArtifactPlan,
    *,
    newline: str = "lf",
    page_separator: str = "form_feed",
) -> AssembledText:
    """Join the plan into one string and compute code-point offsets."""
    nl = "\r\n" if newline == "crlf" else "\n"

    def convert(value: str) -> str:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        return value.replace("\n", nl) if nl != "\n" else value

    parts: list[str] = []
    cursor = 0
    segments_out: list[dict[str, Any]] = []
    pages_out: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = list(plan.warnings)

    for index, page in enumerate(plan.pages):
        if page_separator == "marker":
            prefix = (convert("\n\n") if index else "") + convert(
                f"--- Página {page.page_number} ---\n"
            )
        elif page_separator == "form_feed":
            prefix = "\f" if index else ""
        else:
            prefix = convert("\n\n") if index else ""
        parts.append(prefix)
        cursor += len(prefix)
        page_start = cursor
        first = True
        for segment in page.segments:
            if not first:
                joiner = convert("\n")
                parts.append(joiner)
                cursor += len(joiner)
            text = convert(segment.text)
            start = cursor
            parts.append(text)
            cursor += len(text)
            segments_out.append(
                {
                    "page_number": segment.page_number,
                    "kind": segment.kind,
                    "element_id": segment.element_id,
                    "table_id": segment.table_id,
                    "offset_start": start,
                    "offset_end": cursor,
                }
            )
            first = False
        pages_out.append(
            {
                "page_number": page.page_number,
                "status": page.status,
                "offset_start": page_start,
                "offset_end": cursor,
                "char_count": cursor - page_start,
                "has_text": page.has_text,
                "warnings": page.warnings,
            }
        )

    return AssembledText(text="".join(parts), segments=segments_out, pages=pages_out, warnings=warnings)


def export_text(
    plan: TextArtifactPlan,
    options: ExportOptions | None = None,
    *,
    filename_base: str | None = None,
) -> ExportResult:
    options = validate_options(options or ExportOptions(format="txt"))
    if options.format != "txt":
        raise ExportError("UNSUPPORTED_FORMAT", "Este exportador produz somente TXT.")

    assembled = assemble_text(
        plan, newline=options.newline, page_separator=options.page_separator
    )
    encoding = "utf-8-sig" if options.encoding.lower() in {"utf-8-sig", "utf8-sig"} else "utf-8"
    data = assembled.text.encode(encoding)
    base = _safe_base(filename_base or plan.source_name or "documento")
    manifest = {
        "format": "txt",
        "encoding": encoding,
        "newline": options.newline,
        "page_separator": options.page_separator,
        "page_count": len(plan.pages),
        "segment_count": len(assembled.segments),
        "characters": len(assembled.text),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source_sha256": plan.source_sha256,
        "offsets_unit": "unicode_code_point",
        "pages": assembled.pages,
        "segments": assembled.segments,
    }
    return ExportResult(
        format="txt",
        filename=f"{base}.txt",
        data=data,
        mime_type=MIME_TYPES["txt"],
        warnings=assembled.warnings,
        manifest=manifest,
        options=options.to_dict(),
    )


def _safe_base(name: str) -> str:
    base = (name or "documento").replace("\\", "/").split("/")[-1]
    base, _, _ext = base.rpartition(".") if "." in base else (base, "", "")
    base = _UNSAFE.sub("_", base).strip(" .") or "documento"
    return base[:120]


__all__ = ["AssembledText", "assemble_text", "export_text"]
