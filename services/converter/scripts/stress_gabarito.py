"""Stress runner scored against the corpus ground truth (GABARITO).

The corpus ships a ``GABARITO.jsonl`` next to every pack: for each PDF it
declares the logical content that *must* come out (fields and table rows) and
the expected handling for negative cases (encrypted, corrupted, empty,
invisible text, untrustworthy text layer).

This runner converts every PDF through the real pipeline and measures, per
document:

* **expectation** — did the converter do the right thing for this case class?
* **field accuracy** — expected label/value pairs found in the right column
  (and, separately, found in *any* column: extraction right, structure wrong);
* **table accuracy** — expected rows/cells reproduced;
* **invention** — data claimed for a document that has no visible data.

The state file makes runs resumable; the report is rewritten atomically after
every document, so a long stress pass can be stopped and continued.

Usage (from services/converter):

    .venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs
    .venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs --ocr --budget 1800
    .venv/Scripts/python scripts/stress_gabarito.py --root ../../stress/pdfs --per-category 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import traceback
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit.events import AuditRecorder  # noqa: E402
from engine.pipeline import ConversionPipeline, PipelineOptions  # noqa: E402

EXTRACTED_KINDS = {"records", "table"}
_MONEY_SYMBOLS = "R$US€£"

#: Events worth printing while a document is being converted: they are the
#: per-page milestones, so a defect on page 1 is visible without waiting for a
#: 200-page scan to finish.
_PAGE_EVENTS = {
    "PAGE_ANALYZED",
    "OCR_STARTED",
    "OCR_COMPLETED",
    "OCR_ORIENTATION_CORRECTED",
    "OCR_PREPROCESS_FAILED",
    "TABLE_SELECTED",
    "PAGE_FAILED",
    "TEXT_LAYER_ASSESSED",
    "INVISIBLE_TEXT_LAYER_DETECTED",
}


class LiveRecorder(AuditRecorder):
    """Audit recorder that also prints per-page milestones as they happen."""

    def __init__(self, run_id: str, live: bool = True) -> None:
        super().__init__(run_id=run_id)
        self.live = live

    def emit(self, event_type: str, stage: str, message: str = "", **kwargs):
        event = super().emit(event_type, stage, message, **kwargs)
        if self.live and event_type in _PAGE_EVENTS:
            page = getattr(event, "page", None)
            marker = f"p{page}" if page else "  "
            print(f"      · {marker} {event_type}: {message[:120]}", flush=True)
        return event


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    last_error: Exception | None = None
    for _ in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:  # Windows: antivirus/indexer holds the file
            last_error = exc
            time.sleep(0.1)
    if last_error is not None:
        raise last_error


# --------------------------------------------------------------------------- #
# Ground truth index
# --------------------------------------------------------------------------- #


def _path_key(path: Path) -> str:
    return str(path).replace("\\", "/").lower()


def load_gabarito(root: Path) -> dict[str, dict]:
    """Map normalised absolute PDF path -> gabarito record."""
    index: dict[str, dict] = {}
    for gabarito in root.rglob("GABARITO.jsonl"):
        pack = gabarito.parent
        with gabarito.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                relative = record.get("arquivo")
                if not relative:
                    continue
                index[_path_key(pack / relative)] = record
    return index


# --------------------------------------------------------------------------- #
# Value comparison
# --------------------------------------------------------------------------- #


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(without_marks.casefold().split())


def _canon_header(text: str) -> str:
    return _fold(text).replace(":", "").strip()


_NUMBER_RE = re.compile(r"[-+]?\d[\d.,\s]*")


def _number(text: str) -> Decimal | None:
    """Parse a value the way the corpus writes it (dot or comma decimals)."""
    if text is None:
        return None
    cleaned = (text or "").strip()
    for symbol in _MONEY_SYMBOLS:
        cleaned = cleaned.replace(symbol, "")
    cleaned = cleaned.replace("\u00a0", " ").strip()
    if not cleaned:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()").strip()
    if not _NUMBER_RE.fullmatch(cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -value if negative else value


def values_equal(expected, actual) -> bool:
    expected = "" if expected is None else str(expected)
    actual = "" if actual is None else str(actual)
    if _fold(expected) == _fold(actual):
        return True
    left, right = _number(expected), _number(actual)
    if left is not None and right is not None:
        return abs(left - right) < Decimal("0.005")
    # Identifiers are sometimes exported without their punctuation
    # (MAT000001 == MAT-000001); compare the alphanumeric skeleton.
    left_skeleton = re.sub(r"[^0-9a-zA-Z]+", "", expected).casefold()
    right_skeleton = re.sub(r"[^0-9a-zA-Z]+", "", actual).casefold()
    return bool(left_skeleton) and left_skeleton == right_skeleton


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _sheet_values(plan) -> dict[str, list[str]]:
    """canonical header -> all displays written under that header."""
    values: dict[str, list[str]] = {}
    for sheet in plan.sheets:
        if sheet.kind not in EXTRACTED_KINDS:
            continue
        headers = [_canon_header(header) for header in sheet.headers]
        for row in sheet.rows:
            for index, cell in enumerate(row):
                if cell is None or index >= len(headers):
                    continue
                display = cell.display if cell.display is not None else str(cell.value)
                values.setdefault(headers[index], []).append(display)
    return values


def _all_values(plan) -> list[str]:
    out: list[str] = []
    for sheet in plan.sheets:
        if sheet.kind not in EXTRACTED_KINDS:
            continue
        for row in sheet.rows:
            for cell in row:
                if cell is None:
                    continue
                out.append(cell.display if cell.display is not None else str(cell.value))
    return out


def score_fields(expected: dict, plan) -> dict:
    by_header = _sheet_values(plan)
    everything = _all_values(plan)
    total = len(expected)
    in_column = 0
    anywhere = 0
    missing: list[str] = []
    misplaced: list[str] = []
    for label, value in expected.items():
        candidates = by_header.get(_canon_header(label), [])
        if any(values_equal(value, candidate) for candidate in candidates):
            in_column += 1
            anywhere += 1
            continue
        if any(values_equal(value, candidate) for candidate in everything):
            anywhere += 1
            misplaced.append(label)
        else:
            missing.append(label)
    return {
        "expected": total,
        "in_column": in_column,
        "anywhere": anywhere,
        "missing": missing,
        "misplaced": misplaced,
        "ratio": round(in_column / total, 4) if total else None,
        "extraction_ratio": round(anywhere / total, 4) if total else None,
    }


def filter_rows_by_source(expected_rows: list[dict], text: str) -> tuple[list[dict], int]:
    """Drop declared rows that this document does not print.

    The corpus shares one items template per family; a document with an
    observation row in place of item 02 still declares three items.  Scoring the
    missing item as a converter failure would be a fake defect.
    """
    if not expected_rows:
        return [], 0
    folded = _fold(text)
    if len(folded) < 40:
        return list(expected_rows), 0
    kept: list[dict] = []
    dropped = 0
    for row in expected_rows:
        marker = str(
            row.get("Descricao") or row.get("Descrição") or next(iter(row.values()), "")
        )
        if marker.strip() and _fold(marker) in folded:
            kept.append(row)
        else:
            dropped += 1
    return kept, dropped


def source_table_present(text: str, expected_rows: list[dict]) -> bool | None:
    """Is the expected table actually printed in the document?

    The corpus declares the same invoice layout for every document of a family,
    but several families (plain text, forms, ...) do not print the items at all.
    Penalising the converter for content that is not in the file would be a
    fake defect, so the text layer is checked first:

    * ``False`` — the document has a text layer and the items are not in it
      (the expectation is generic, table scoring is skipped);
    * ``True`` — the items are in the text layer, or the page has no text layer
      at all (a scan: the table can only be read by OCR, so it must be scored).
    """
    if not expected_rows:
        return None
    markers = [
        str(row.get("Descricao") or row.get("Descrição") or next(iter(row.values()), ""))
        for row in expected_rows
    ]
    markers = [marker for marker in markers if marker.strip()]
    if not markers:
        return None
    if len(text.strip()) < 40:
        # No usable text layer (scan or empty page): the table lives in the
        # image, so it is a real target for OCR.
        return True
    folded = _fold(text)
    hits = sum(1 for marker in markers if _fold(marker) in folded)
    return hits / len(markers) >= 0.6


def _skeleton(text: str) -> str:
    """Letters/digits only: tolerant to punctuation and spacing differences."""
    return re.sub(r"[^0-9a-z]+", "", _fold(text))


def source_text(path: Path, max_pages: int = 3) -> str:
    """Everything the document declares as content: text layer + form values.

    Fillable PDFs keep their data in the AcroForm widgets; the text layer only
    has the labels, so the widget values must count as "present in the file".
    """
    text = ""
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages[:max_pages])
    except Exception:  # noqa: BLE001 - unreadable PDF: no evidence available
        text = ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        fields = reader.get_fields() or {}
        values = [
            str(spec.get("/V"))
            for spec in fields.values()
            if spec is not None and spec.get("/V") is not None
        ]
        if values:
            text += "\n" + "\n".join(values)
    except Exception:  # noqa: BLE001 - no readable form: nothing to add
        pass
    return text


def _numbers_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for match in re.findall(r"\d[\d.,]*", text or ""):
        value = _number(match)
        if value is not None:
            out.add(value)
    return out


def fields_present_in_source(expected: dict, text: str) -> tuple[dict, list[str]]:
    """Split the declared fields into "printed in this document" and "generic".

    The corpus reuses one invoice template per family, so a document may declare
    eight fields while printing only four — and occasionally declares a value
    whose digits do not match the PDF at all.  A value that is nowhere in the
    document cannot be extracted by any converter; counting it as a miss would
    invent a defect.  Scans have no text layer at all: then every declared value
    is a real target for OCR.
    """
    if not expected:
        return {}, []
    folded = _fold(text)
    if len(folded) < 20:
        return dict(expected), []
    present: dict = {}
    absent: list[str] = []
    for label, value in expected.items():
        needle = _fold(str(value))
        if needle and needle in folded:
            present[label] = value
        else:
            absent.append(label)
    return present, absent


def _best_table_sheet(expected_rows: list[dict], plan):
    expected_headers = {_canon_header(key) for row in expected_rows for key in row}
    best = None
    best_overlap = 0
    for sheet in plan.sheets:
        if sheet.kind not in EXTRACTED_KINDS or not sheet.rows:
            continue
        headers = [_canon_header(header) for header in sheet.headers]
        overlap = len(expected_headers & set(headers))
        if overlap > best_overlap:
            best, best_overlap = (sheet, headers), overlap
    return best, best_overlap, expected_headers


def score_table(expected_rows: list[dict], plan, *, present_in_source: bool | None = True) -> dict:
    empty = {"rows_expected": len(expected_rows), "rows_matched": 0,
             "cells_expected": sum(len(row) for row in expected_rows), "cells_matched": 0,
             "row_ratio": None, "cell_ratio": None, "sheet": None,
             "not_in_source": False, "scored": False, "columns_missing": []}
    if not expected_rows:
        return empty
    if present_in_source is False:
        # The expectation is generic: this document never prints those items.
        return {**empty, "not_in_source": True}
    best, overlap, expected_headers = _best_table_sheet(expected_rows, plan)
    if best is None or overlap == 0:
        return {
            **empty,
            "row_ratio": 0.0,
            "cell_ratio": 0.0,
            "scored": True,
            "columns_missing": sorted(expected_headers),
        }
    sheet, headers = best
    index_of = {}
    for position, header in enumerate(headers):
        index_of.setdefault(header, position)

    rows_matched = 0
    cells_matched = 0
    used: set[int] = set()
    for expected in expected_rows:
        available = {key: index_of.get(_canon_header(key)) for key in expected}
        best_row = None
        best_score = -1
        for row_index, row in enumerate(sheet.rows):
            if row_index in used:
                continue
            score = 0
            usable = 0
            for key, value in expected.items():
                column = available.get(key)
                if column is None:
                    continue
                usable += 1
                cell = row[column] if column < len(row) else None
                if cell is not None and values_equal(value, cell.display if cell.display is not None else cell.value):
                    score += 1
            if usable and score == usable and score > best_score:
                best_row, best_score = row_index, score
        if best_row is not None:
            used.add(best_row)
            rows_matched += 1
            cells_matched += best_score
    cells_expected = sum(len(row) for row in expected_rows)
    present = sum(1 for key in expected_headers if _canon_header(key) in index_of)
    return {
        "rows_expected": len(expected_rows),
        "rows_matched": rows_matched,
        "cells_expected": cells_expected,
        "cells_matched": cells_matched,
        "row_ratio": round(rows_matched / len(expected_rows), 4),
        "cell_ratio": round(cells_matched / cells_expected, 4) if cells_expected else None,
        "sheet": sheet.name,
        "scored": True,
        "not_in_source": False,
        "columns_present": present,
        "columns_expected": len(expected_headers),
        "columns_missing": sorted(
            {key for key in expected_headers if _canon_header(key) not in index_of}
        ),
    }


NEGATIVE_EXPECTATIONS = {
    "pdf_sem_dados",
    "solicitar_senha_para_extrair",
    "tratamento_de_pdf_corrompido",
    "texto_nao_visivel",
}


def score_expectation(expected: str, result, fields: dict, table: dict, issue_codes=()) -> dict:
    """Decide whether the converter handled this case class correctly."""
    error_code = result.error.code if result.error is not None else None
    status = result.status
    values = _all_values(result.plan)
    claimed = bool(values)
    codes = set(issue_codes or ())

    if expected == "pdf_sem_dados":
        ok = error_code == "NO_CONTENT" and not claimed
        return {"ok": ok, "reason": "documento vazio deve falhar com NO_CONTENT",
                "error_code": error_code, "claimed_data": claimed}
    if expected == "solicitar_senha_para_extrair":
        ok = error_code == "ENCRYPTED_PDF"
        return {"ok": ok, "reason": "PDF protegido deve pedir a senha (ENCRYPTED_PDF)",
                "error_code": error_code, "claimed_data": claimed}
    if expected == "tratamento_de_pdf_corrompido":
        ok = error_code == "INVALID_FILE" or status == "failed"
        return {"ok": ok, "reason": "PDF corrompido deve ser tratado sem exceção",
                "error_code": error_code, "claimed_data": claimed}

    # Content cases: the logical content must come out, and the table only
    # counts when this document actually prints it.
    field_ratio = fields.get("ratio")
    table_ratio = table.get("row_ratio") if table.get("scored") else None
    content_ok = bool(field_ratio) and field_ratio >= 0.99
    if table_ratio is not None:
        content_ok = content_ok and table_ratio >= 0.9
    if expected == "camada_de_texto_nao_confiavel":
        ok = content_ok
        reason = "camada oculta errada: os valores visuais devem prevalecer (OCR)"
    elif expected == "texto_nao_visivel":
        # The hidden layer is the only copy of the content: exporting it is
        # right, but it must be flagged as not visually verifiable.
        ok = content_ok and "INVISIBLE_TEXT_LAYER" in codes
        reason = "texto invisível deve ser exportado e sinalizado como não verificável"
    else:
        ok = content_ok
        reason = "campos visíveis devem ser extraídos"
    return {"ok": ok, "reason": reason, "error_code": error_code, "claimed_data": claimed,
            "field_ratio": field_ratio, "table_row_ratio": table_ratio}


# --------------------------------------------------------------------------- #
# Corpus ordering
# --------------------------------------------------------------------------- #


def collect_pdfs(root: Path, order: str) -> list[Path]:
    pdfs = [path for path in root.rglob("*.pdf") if path.is_file()]
    if order == "name":
        pdfs.sort(key=lambda path: str(path))
    elif order == "desc":
        pdfs.sort(key=lambda path: path.stat().st_size, reverse=True)
    else:
        # Default: smallest first. A heavy 35 MiB / 200-page scan is exercised
        # only after every light document has already been converted and scored.
        pdfs.sort(key=lambda path: path.stat().st_size)
    return pdfs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT.parents[1] / "stress" / "pdfs"))
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument(
        "--live",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="imprime cada marco de página durante a conversão (padrão: ligado)",
    )
    parser.add_argument(
        "--only-ocr",
        action="store_true",
        help="processa apenas documentos que o gabarito marca como OCR obrigatório",
    )
    parser.add_argument("--budget", type=float, default=0, help="seconds; 0 = no budget")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--per-category", type=int, default=0, help="cap documents per category")
    parser.add_argument("--categories", default="", help="comma separated category filter")
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--order", choices=["asc", "desc", "name"], default="asc")
    parser.add_argument("--formats", default="xlsx,txt")
    parser.add_argument("--state", default=str(ROOT / "data" / "stress_gabarito.json"))
    parser.add_argument("--report", default=str(ROOT / "data" / "stress_gabarito_report.json"))
    parser.add_argument("--rerun", action="store_true", help="ignore previous state entries")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"root not found: {root}", flush=True)
        return 2

    gabarito = load_gabarito(root)
    print(f"gabarito: {len(gabarito)} documento(s) com verdade de referência", flush=True)

    mode = "ocr" if args.ocr else "no-ocr"
    state_path = Path(args.state)
    report_path = Path(args.report)
    if state_path.exists():
        previous = _load(state_path, {})
        if previous.get("mode") not in {None, mode}:
            suffix = ".ocr" if args.ocr else ".noocr"
            state_path = state_path.with_suffix(state_path.suffix + suffix)
            report_path = report_path.with_suffix(report_path.suffix + suffix)
    state = _load(state_path, {"seen": {}, "started_at": _now(), "mode": mode})
    state["mode"] = mode
    seen: dict[str, dict] = state.setdefault("seen", {})
    if args.rerun:
        seen.clear()

    wanted = {category.strip() for category in args.categories.split(",") if category.strip()}

    pdfs = collect_pdfs(root, args.order)
    if wanted:
        pdfs = [path for path in pdfs if any(category in path.name or category in str(path.parent) for category in wanted)]
    if args.per_category:
        counts: dict[str, int] = {}
        limited: list[Path] = []
        for path in pdfs:
            category = path.parent.name
            if counts.get(category, 0) >= args.per_category:
                continue
            counts[category] = counts.get(category, 0) + 1
            limited.append(path)
        pdfs = limited
    if args.only_ocr:
        pdfs = [
            path
            for path in pdfs
            if (gabarito.get(_path_key(path)) or {}).get("ocr_necessario")
        ]
    if args.limit:
        pdfs = pdfs[: args.limit]
    formats = [item.strip() for item in args.formats.split(",") if item.strip()]
    print(
        f"mode={mode} order={args.order} files={len(pdfs)} already={len(seen)} "
        f"formats={formats} per_category={args.per_category or '-'}",
        flush=True,
    )

    started = time.perf_counter()
    processed = 0
    crashed = 0
    for index, path in enumerate(pdfs, start=1):
        if args.budget and time.perf_counter() - started > args.budget:
            print("budget reached", flush=True)
            break
        key = str(path.relative_to(root))
        if key in seen:
            continue
        truth = gabarito.get(_path_key(path))
        record: dict = {
            "file": key,
            "size_bytes": path.stat().st_size,
            "at": _now(),
            "ocr": args.ocr,
            "gabarito_id": (truth or {}).get("id"),
            "category": (truth or {}).get("categoria") or path.parent.name,
            "group": (truth or {}).get("grupo"),
            "expectativa": (truth or {}).get("expectativa"),
        }
        try:
            digest = _sha256(path)
            record["sha256"] = digest
            run_id = f"stress_{digest[:12]}"
            recorder = LiveRecorder(run_id=run_id, live=args.live)
            result = ConversionPipeline(
                pdf_path=path,
                run_id=run_id,
                conversion_id=f"conv_{digest[:12]}",
                recorder=recorder,
                options=PipelineOptions(
                    ocr_enabled=args.ocr,
                    max_pages=args.max_pages,
                    target_formats=formats,
                    source_name=path.name,
                ),
            ).run()
            text = source_text(path)
            expected_rows, absent_rows = filter_rows_by_source(
                (truth or {}).get("tabela_esperada") or [], text
            )
            table = score_table(
                expected_rows,
                result.plan,
                present_in_source=source_table_present(
                    text, (truth or {}).get("tabela_esperada") or []
                ),
            )
            table["rows_absent_from_source"] = absent_rows
            expected_fields, absent_fields = fields_present_in_source(
                (truth or {}).get("campos_esperados") or {}, text
            )
            fields = score_fields(expected_fields, result.plan)
            fields["absent_from_source"] = absent_fields
            issue_codes = sorted({issue.code for issue in recorder.issues})
            expectation = (
                score_expectation(record["expectativa"], result, fields, table, issue_codes)
                if truth and record["expectativa"]
                else {"ok": None, "reason": "sem gabarito"}
            )
            record.update(
                {
                    "status": result.status,
                    "quality": result.quality,
                    "publication": result.publication_status,
                    "error": None
                    if result.error is None
                    else {"code": result.error.code, "message": result.error.message[:300]},
                    "expectation_ok": expectation.get("ok"),
                    "expectation_reason": expectation.get("reason"),
                    "claimed_data": expectation.get("claimed_data"),
                    "fields": fields,
                    "table": table,
                    "pages": len(result.ir.pages),
                    "pages_failed": sum(1 for page in result.ir.pages if page.status == "failed"),
                    "pages_partial": sum(1 for page in result.ir.pages if page.status == "partial"),
                    "ocr_pages": sum(1 for page in result.ir.pages if page.ocr_chars),
                    "tables": len(result.ir.tables),
                    "sheets": len(result.plan.sheets),
                    "sheet_kinds": sorted({sheet.kind for sheet in result.plan.sheets}),
                    "cells": result.stats.get("cells", 0),
                    "issue_codes": issue_codes,
                    "warnings": sum(1 for issue in recorder.issues if issue.severity == "warning"),
                    "errors": sum(1 for issue in recorder.issues if issue.severity in {"error", "critical"}),
                    "duration_ms": result.duration_ms,
                }
            )
        except Exception as exc:  # noqa: BLE001
            crashed += 1
            record.update(
                {
                    "status": "crashed",
                    "error": {"code": "EXCEPTION", "message": str(exc)[:300]},
                    "expectation_ok": False,
                    "traceback": traceback.format_exc(limit=6),
                }
            )
        seen[key] = record
        processed += 1
        _write(state_path, state)
        if processed % 5 == 0 or record["status"] not in {"completed"} or not record.get("expectation_ok", True):
            fields_ratio = (record.get("fields") or {}).get("ratio")
            print(
                f"[{index}/{len(pdfs)}] {record['status']:<22} "
                f"exp={'ok' if record.get('expectation_ok') else 'FAIL':<4} "
                f"f={fields_ratio if fields_ratio is not None else '-':<6} "
                f"t={(record.get('table') or {}).get('row_ratio') if (record.get('table') or {}).get('row_ratio') is not None else '-':<6} "
                f"{record.get('duration_ms') or 0:>7}ms {key}",
                flush=True,
            )

    report = build_report(seen, mode, len(pdfs), processed, crashed)
    _write(report_path, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if crashed == 0 else 1


def _aggregate(documents: list[dict]) -> dict:
    total = len(documents)
    with_expectation = [doc for doc in documents if doc.get("expectation_ok") is not None]
    passed = [doc for doc in with_expectation if doc.get("expectation_ok")]
    field_ratios = [doc["fields"]["ratio"] for doc in documents
                    if (doc.get("fields") or {}).get("ratio") is not None]
    extraction_ratios = [doc["fields"]["extraction_ratio"] for doc in documents
                         if (doc.get("fields") or {}).get("extraction_ratio") is not None]
    row_ratios = [doc["table"]["row_ratio"] for doc in documents
                  if (doc.get("table") or {}).get("scored")
                  and (doc.get("table") or {}).get("row_ratio") is not None]
    cell_ratios = [doc["table"]["cell_ratio"] for doc in documents
                   if (doc.get("table") or {}).get("scored")
                   and (doc.get("table") or {}).get("cell_ratio") is not None]
    statuses: dict[str, int] = {}
    issues: dict[str, int] = {}
    for doc in documents:
        statuses[str(doc.get("status"))] = statuses.get(str(doc.get("status")), 0) + 1
        for code in doc.get("issue_codes") or []:
            issues[code] = issues.get(code, 0) + 1

    def mean(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "documents": total,
        "expectation_pass": len(passed),
        "expectation_total": len(with_expectation),
        "expectation_rate": round(len(passed) / len(with_expectation), 4) if with_expectation else None,
        "field_ratio_mean": mean(field_ratios),
        "field_extraction_mean": mean(extraction_ratios),
        "table_row_ratio_mean": mean(row_ratios),
        "table_cell_ratio_mean": mean(cell_ratios),
        "by_status": dict(sorted(statuses.items(), key=lambda pair: pair[1], reverse=True)),
        "top_issue_codes": dict(sorted(issues.items(), key=lambda pair: pair[1], reverse=True)[:12]),
        "duration_ms_total": sum(int(doc.get("duration_ms") or 0) for doc in documents),
    }


def build_report(seen: dict[str, dict], mode: str, files_total: int, processed: int, crashed: int) -> dict:
    documents = list(seen.values())
    by_category: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = {}
    for doc in documents:
        grouped.setdefault(str(doc.get("category") or "-"), []).append(doc)
    for category, items in sorted(grouped.items()):
        by_category[category] = _aggregate(items)

    failures = [
        {
            "file": doc.get("file"),
            "category": doc.get("category"),
            "expectativa": doc.get("expectativa"),
            "status": doc.get("status"),
            "fields": (doc.get("fields") or {}).get("ratio"),
            "field_extraction": (doc.get("fields") or {}).get("extraction_ratio"),
            "table_rows": (doc.get("table") or {}).get("row_ratio"),
            "missing": (doc.get("fields") or {}).get("missing"),
            "misplaced": (doc.get("fields") or {}).get("misplaced"),
            "absent_from_source": (doc.get("fields") or {}).get("absent_from_source"),
            "columns_missing": (doc.get("table") or {}).get("columns_missing"),
            "error": doc.get("error"),
        }
        for doc in documents
        if doc.get("expectation_ok") is False
    ]
    failures.sort(key=lambda item: (str(item["category"]), str(item["file"])))
    return {
        "summary": {
            "mode": mode,
            "files_total": files_total,
            "processed_this_run": processed,
            "state_entries": len(documents),
            "crashed": crashed,
            "updated_at": _now(),
            **_aggregate(documents),
        },
        "by_category": by_category,
        "failures": failures,
        "documents": documents,
    }


if __name__ == "__main__":
    raise SystemExit(main())
