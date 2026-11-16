"""Resumable stress runner over the real PDF corpus.

Runs the full conversion pipeline (inspection -> extraction -> structure ->
XLSX -> TXT/CSV -> audit/publication) for every PDF found under a root, with
content-hash deduplication and a resumable state file.

Usage (from services/converter):

    .venv/Scripts/python scripts/stress_corpus.py --no-ocr --budget 1800
    .venv/Scripts/python scripts/stress_corpus.py --ocr --budget 7200

The state file makes a second invocation continue where the previous stopped.
Reports are written atomically after every document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit.events import AuditRecorder  # noqa: E402
from engine.pipeline import ConversionPipeline, PipelineOptions  # noqa: E402


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
    # Windows can transiently lock the target (antivirus/indexer): retry instead
    # of aborting a long stress run.
    last_error: Exception | None = None
    for _ in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:  # WinError 5
            last_error = exc
            time.sleep(0.1)
    if last_error is not None:
        raise last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT.parents[1] / "study" / "pdfs"))
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--budget", type=float, default=0, help="seconds; 0 = no budget")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument(
        "--order",
        choices=["asc", "desc", "name"],
        default="asc",
        help="asc = menores primeiro (padrão); pesados ficam para o final",
    )
    parser.add_argument("--state", default=str(ROOT / "data" / "stress_state.json"))
    parser.add_argument("--report", default=str(ROOT / "data" / "stress_report.json"))
    parser.add_argument("--text-out", default=str(ROOT / "data" / "stress_text"))
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"root not found: {root}", flush=True)
        return 2

    state_path = Path(args.state)
    report_path = Path(args.report)
    state = _load(state_path, {"seen": {}, "started_at": _now(), "mode": "ocr" if args.ocr else "no-ocr"})
    if state.get("mode") != ("ocr" if args.ocr else "no-ocr"):
        # A different pass has its own state file next to the default one.
        suffix = ".ocr" if args.ocr else ".noocr"
        state_path = state_path.with_suffix(state_path.suffix + suffix)
        report_path = report_path.with_suffix(report_path.suffix + suffix)
        state = _load(state_path, {"seen": {}, "started_at": _now(), "mode": "ocr" if args.ocr else "no-ocr"})

    seen: dict[str, dict] = state.setdefault("seen", {})
    pdfs = [path for path in root.rglob("*.pdf") if path.is_file()]
    if args.order == "name":
        pdfs.sort(key=lambda path: str(path))
    elif args.order == "desc":
        pdfs.sort(key=lambda path: path.stat().st_size, reverse=True)
    else:
        # Default: smallest first. A heavy 200-page scan only starts after every
        # light document has already been exercised.
        pdfs.sort(key=lambda path: path.stat().st_size)
    if args.limit:
        pdfs = pdfs[: args.limit]
    print(
        f"mode={'ocr' if args.ocr else 'no-ocr'} order={args.order} "
        f"files={len(pdfs)} already={len(seen)}",
        flush=True,
    )

    started = time.perf_counter()
    processed = 0
    failed_now = 0
    duplicates = 0
    for index, path in enumerate(pdfs, start=1):
        if args.budget and time.perf_counter() - started > args.budget:
            print("budget reached", flush=True)
            break
        key = str(path.relative_to(root))
        if key in seen:
            continue
        try:
            digest = _sha256(path)
        except Exception as exc:  # noqa: BLE001
            seen[key] = {"file": key, "status": "unreadable", "error": str(exc), "at": _now()}
            _write(state_path, state)
            continue
        duplicate = next(
            (item for item in seen.values() if item.get("sha256") == digest and item.get("status") not in {"unreadable"}),
            None,
        )
        if duplicate is not None:
            duplicates += 1
            seen[key] = {
                "file": key,
                "sha256": digest,
                "status": "duplicate",
                "duplicate_of": duplicate.get("file"),
                "at": _now(),
            }
            _write(state_path, state)
            continue

        run_id = f"stress_{digest[:12]}"
        record: dict = {
            "file": key,
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "at": _now(),
            "ocr": args.ocr,
        }
        try:
            recorder = AuditRecorder(run_id=run_id)
            result = ConversionPipeline(
                pdf_path=path,
                run_id=run_id,
                conversion_id=f"conv_{digest[:12]}",
                recorder=recorder,
                options=PipelineOptions(
                    ocr_enabled=args.ocr,
                    max_pages=args.max_pages,
                    target_formats=["xlsx", "txt", "csv"],
                ),
            ).run()
            record.update(
                {
                    "status": result.status,
                    "quality": result.quality,
                    "publication": result.publication_status,
                    "pages": len(result.ir.pages),
                    "pages_failed": sum(1 for page in result.ir.pages if page.status == "failed"),
                    "pages_partial": sum(1 for page in result.ir.pages if page.status == "partial"),
                    "ocr_regions": result.ir.ocr_regions,
                    "ocr_pages": sum(1 for page in result.ir.pages if page.ocr_chars),
                    "tokens": sum(len(page.tokens) for page in result.ir.pages),
                    "tables": len(result.ir.tables),
                    "sheets": len(result.plan.sheets),
                    "cells": result.stats.get("cells", 0),
                    "exports": [item.format for item in result.exports],
                    "export_bytes": {item.format: item.size_bytes for item in result.exports},
                    "issues": len(recorder.issues),
                    "warnings": sum(1 for issue in recorder.issues if issue.severity == "warning"),
                    "errors": sum(1 for issue in recorder.issues if issue.severity in {"error", "critical"}),
                    "issue_codes": sorted({issue.code for issue in recorder.issues}),
                    "duration_ms": result.duration_ms,
                    "error": None
                    if result.error is None
                    else {"code": result.error.code, "message": result.error.message},
                }
            )
            if result.text_plan is not None:
                text = "\n".join(
                    segment.text
                    for page in result.text_plan.pages
                    for segment in page.segments
                )
                target = Path(args.text_out) / (digest[:16] + ".txt")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", errors="replace")
                record["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
                record["text_chars"] = len(text)
        except Exception as exc:  # noqa: BLE001
            failed_now += 1
            record.update(
                {
                    "status": "crashed",
                    "error": {"code": "EXCEPTION", "message": str(exc)},
                    "traceback": traceback.format_exc(limit=6),
                }
            )
        seen[key] = record
        processed += 1
        _write(state_path, state)
        if processed % 10 == 0 or record["status"] not in {"completed"}:
            print(
                f"[{index}/{len(pdfs)}] {record['status']:<22} "
                f"{record['quality'] if 'quality' in record else '-':<9} "
                f"{record['duration_ms'] if 'duration_ms' in record else 0:>7}ms "
                f"{record.get('size_bytes', 0) // 1024:>6}KiB {key}",
                flush=True,
            )

    values = list(seen.values())
    summary = {
        "mode": "ocr" if args.ocr else "no-ocr",
        "files_total": len(pdfs),
        "processed_this_run": processed,
        "state_entries": len(values),
        "duplicates": duplicates,
        "by_status": _count(values, "status"),
        "by_quality": _count(values, "quality"),
        "by_publication": _count(values, "publication"),
        "errors_by_code": _error_codes(values),
        "duration_ms_total": sum(int(item.get("duration_ms") or 0) for item in values),
        "slowest": sorted(
            (item for item in values if item.get("duration_ms")),
            key=lambda item: int(item["duration_ms"]),
            reverse=True,
        )[:10],
        "updated_at": _now(),
    }
    _write(report_path, {"summary": summary, "documents": values})
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if not any(item.get("status") == "crashed" for item in values) else 1


def _count(values: list[dict], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in values:
        key = str(item.get(field) or "-")
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items(), key=lambda pair: pair[1], reverse=True))


def _error_codes(values: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in values:
        error = item.get("error")
        if isinstance(error, dict) and error.get("code"):
            code = str(error["code"])
            result[code] = result.get(code, 0) + 1
    return dict(sorted(result.items(), key=lambda pair: pair[1], reverse=True))


if __name__ == "__main__":
    raise SystemExit(main())
