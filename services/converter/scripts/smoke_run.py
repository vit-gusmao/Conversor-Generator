"""Developer smoke runner: converts fixture PDFs and prints the workbook plan.

Usage:
    python scripts/smoke_run.py [fixture ...]

Not part of the test suite (the suite asserts the same things with pytest), but
useful when iterating on heuristics.
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_ROOT))

from audit.events import AuditRecorder  # noqa: E402
from engine.pipeline import ConversionPipeline, PipelineOptions  # noqa: E402
from tests.fixtures.generate_pdfs import generate_all  # noqa: E402

OUTPUT = SERVICE_ROOT / "data" / "fixtures"


def main(argv: list[str]) -> int:
    paths = generate_all(OUTPUT)
    selected = argv[1:] or [
        "simple_field",
        "multi_fields",
        "inline_fields",
        "fields_below",
        "two_columns",
        "bordered_table",
        "borderless_table",
        "transposed_table",
        "free_text",
        "mixed_report",
    ]
    failures = 0
    for name in selected:
        path = paths.get(name)
        if path is None:
            print(f"!! fixture desconhecida: {name}")
            continue
        recorder = AuditRecorder(run_id=f"run_{name}")
        pipeline = ConversionPipeline(
            pdf_path=path,
            run_id=f"run_{name}",
            conversion_id=f"conv_{name}",
            recorder=recorder,
            options=PipelineOptions(ocr_enabled=False),
        )
        result = pipeline.run()
        print("=" * 78)
        print(f"{name}: status={result.status} quality={result.quality} error={result.error}")
        if result.error:
            failures += 1
            print(f"   {result.error.code}: {result.error.message}")
            continue
        for sheet in result.plan.sheets:
            print(f"  [{sheet.kind}] {sheet.name} rows={sheet.n_rows} headers={sheet.headers}")
            for row in sheet.rows[:5]:
                values = []
                for cell in row:
                    if cell is None:
                        values.append("")
                    else:
                        values.append(f"{cell.display!r}({cell.value_type})")
                print("     ", " | ".join(values))
        print(
            f"  cells={result.stats['cells']} tables={result.stats['tables']} "
            f"records={result.stats['records']} coverage={result.stats['coverage_percent']}%"
        )
        print("  checks:", [(c["check"], c["status"]) for c in result.checks])
        if result.status not in {"completed", "completed_with_warnings"}:
            failures += 1
    print("=" * 78)
    print(f"fixtures com problema: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
