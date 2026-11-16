"""Command line utility: convert a PDF locally without the HTTP layer."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from config import settings
from logging_setup import configure_logging
from models.db import init_db
from repositories.conversion_repository import ConversionRepository
from storage import get_storage
from worker.tasks import execute_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pdf2x", description="Converte um PDF em XLSX")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--no-ocr", action="store_true")
    args = parser.parse_args(argv)

    configure_logging(settings.log_level)
    init_db()
    if not args.pdf.exists():
        print(f"arquivo não encontrado: {args.pdf}", file=sys.stderr)
        return 2

    run_id = f"run_{uuid.uuid4().hex}"
    conversion_id = f"conv_{uuid.uuid4().hex}"
    storage = get_storage()
    key = f"documents/{run_id}.pdf"
    storage.put_bytes(key, args.pdf.read_bytes(), "application/pdf")

    execute_run(
        run_id=run_id,
        conversion_id=conversion_id,
        storage_key=key,
        filename=args.pdf.name,
        options={"ocr": not args.no_ocr},
    )

    repo = ConversionRepository()
    data = repo.xlsx_bytes(run_id)
    if data is None:
        print("conversão falhou; consulte a auditoria", file=sys.stderr)
        return 1
    target = args.output or args.pdf.with_suffix(".xlsx")
    target.write_bytes(data)
    print(f"ok: {target} ({len(data)} bytes) — run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
