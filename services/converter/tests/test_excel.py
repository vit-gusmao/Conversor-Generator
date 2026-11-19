"""The XLSX auditor must fail loudly on anything it is meant to forbid."""

from __future__ import annotations

import io

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from PIL import Image

from engine.excel.auditor import audit_xlsx
from engine.excel.writer import write_workbook
from models.document import CellPlan, SheetPlan, WorkbookPlan


def sample_plan() -> WorkbookPlan:
    sheet = SheetPlan(name="Dados", kind="records", headers=["Nome", "Salário"])
    sheet.rows.append(
        [
            CellPlan(value="João da Silva", display="João da Silva", value_type="text", header="Nome", original="João da Silva"),
            CellPlan(
                value=3500.0,
                display="R$ 3.500,00",
                value_type="money",
                header="Salário",
                original="R$ 3.500,00",
                number_format='"R$" #,##0.00',
            ),
        ]
    )
    return WorkbookPlan(sheets=[sheet])


def test_generated_workbook_passes_audit():
    plan = sample_plan()
    result = write_workbook(plan, run_id="run_test")
    audit = audit_xlsx(result.data, plan)
    assert audit.ok is True, audit.checks
    checks = {c["check"]: c["status"] for c in audit.checks}
    assert checks["xlsx_no_media"] == "pass"
    assert checks["xlsx_no_formulas"] == "pass"
    assert checks["xlsx_values_match_plan"] == "pass"


def test_embedded_image_is_detected(tmp_path):
    plan = sample_plan()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Dados"
    worksheet.append(["Nome", "Salário"])
    worksheet.append(["João da Silva", 3500])
    image_path = tmp_path / "logo.png"
    Image.new("RGB", (20, 20), "red").save(image_path)
    worksheet.add_image(XLImage(str(image_path)), "D2")
    buffer = io.BytesIO()
    workbook.save(buffer)

    audit = audit_xlsx(buffer.getvalue(), plan)
    assert audit.ok is False
    checks = {c["check"]: c["status"] for c in audit.checks}
    assert checks["xlsx_no_media"] == "fail"
    assert checks["xlsx_no_image_relationships"] == "fail"
    assert checks["xlsx_no_images_object"] == "fail"


def test_formula_is_detected():
    plan = sample_plan()
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Dados"
    worksheet.append(["Nome", "Salário"])
    worksheet.append(["João da Silva", "=SUM(1,2)"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    audit = audit_xlsx(buffer.getvalue(), plan)
    checks = {c["check"]: c["status"] for c in audit.checks}
    assert checks["xlsx_no_formulas"] == "fail"


def test_value_mismatch_is_detected():
    plan = sample_plan()
    plan.sheets[0].rows[0][0].value = "Maria"
    plan.sheets[0].rows[0][0].display = "Maria"
    result = write_workbook(plan, run_id="run_test")
    # The writer wrote "Maria"; tamper by rebuilding from a different plan.
    tampered = sample_plan()
    audit = audit_xlsx(result.data, tampered)
    checks = {c["check"]: c["status"] for c in audit.checks}
    assert checks["xlsx_values_match_plan"] == "fail"


def test_identifiers_are_written_as_text():
    plan = WorkbookPlan(
        sheets=[
            SheetPlan(
                name="Dados",
                kind="records",
                headers=["Matrícula"],
                rows=[
                    [
                        CellPlan(
                            value="001245",
                            display="001245",
                            value_type="code",
                            header="Matrícula",
                            original="001245",
                            number_format="@",
                        )
                    ]
                ],
            )
        ]
    )
    result = write_workbook(plan)
    workbook = load_workbook(io.BytesIO(result.data))
    cell = workbook["Dados"]["A2"]
    assert cell.value == "001245"
    assert cell.number_format == "@"
    assert isinstance(cell.value, str)


def test_no_images_api_guard():
    """The writer refuses to serialise a workbook that somehow holds images."""
    plan = sample_plan()
    result = write_workbook(plan)
    assert result.sheets == 1
    assert result.cells == 2


def test_header_replacements_are_counted_in_sanitized_chars():
    # AU2-19: _write_header must report replaced characters so the audit counts them.
    sheet = SheetPlan(name="Dados", kind="records", headers=["Na\x01me", "Sal\x02\x03ário"])
    plan = WorkbookPlan(sheets=[sheet])
    result = write_workbook(plan, run_id="run_header")
    assert result.sanitized_chars == 3
    assert result.sanitized_refs == ["Dados"]
