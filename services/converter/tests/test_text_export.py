"""TXT export: Unicode, page separators, offsets and failed pages."""

from __future__ import annotations

from exports.base import ExportOptions
from exports.text_plan import build_text_plan
from exports.text_writer import export_text
from models.document import BBox, DocumentIR, PageResult, TableStructure, TextBlock


def _ir() -> DocumentIR:
    page1 = PageResult(page_number=1, width=600, height=800)
    page1.blocks = [
        TextBlock(
            element_id="b1",
            page_number=1,
            bbox=BBox(10, 10, 400, 30),
            text="João da Silva — Ação 😀",
            reading_order=0,
        ),
        TextBlock(
            element_id="b2",
            page_number=1,
            bbox=BBox(10, 40, 400, 60),
            text="Segunda linha",
            reading_order=1,
        ),
    ]
    page2 = PageResult(page_number=2, width=600, height=800)
    page2.blocks = [
        TextBlock(
            element_id="b3",
            page_number=2,
            bbox=BBox(10, 10, 400, 30),
            text="Página dois",
            reading_order=0,
        )
    ]
    page3 = PageResult(page_number=3, width=0, height=0, status="failed", classification="empty")
    return DocumentIR(file_name="relatorio.pdf", sha256="abc", page_count=3, pages=[page1, page2, page3])


def test_txt_preserves_unicode_and_form_feed():
    result = export_text(build_text_plan(_ir()), ExportOptions(format="txt"))
    text = result.data.decode("utf-8")
    assert "João da Silva — Ação 😀" in text
    assert "\f" in text
    assert "Página dois" in text
    assert result.manifest["page_count"] == 3
    assert result.manifest["encoding"] == "utf-8"
    failed = [page for page in result.manifest["pages"] if page["status"] == "failed"]
    assert failed and failed[0]["char_count"] == 0
    assert any(item["code"] == "PAGE_NOT_REPRESENTED" for item in result.warnings)


def test_txt_marker_and_crlf_offsets_are_code_points():
    plan = build_text_plan(_ir())
    result = export_text(
        plan, ExportOptions(format="txt", newline="crlf", page_separator="marker")
    )
    text = result.data.decode("utf-8")
    assert "--- Página 1 ---" in text
    assert "\r\n" in text
    assert "\n" not in text.replace("\r\n", "")
    segments = result.manifest["segments"]
    assert segments
    for segment in segments:
        piece = text[segment["offset_start"] : segment["offset_end"]]
        assert piece and not piece.startswith("\r\n")


def test_txt_table_text_is_tab_separated_without_duplication():
    page = PageResult(page_number=1, width=600, height=800)
    page.tables = [
        TableStructure(
            table_id="t1",
            page_number=1,
            bbox=BBox(10, 10, 400, 100),
            strategy="lattice",
            headers=["Nome", "Valor"],
            rows=[["Ana", "10,00"]],
        )
    ]
    # A block inside the table must not be repeated in the TXT.
    page.blocks = [
        TextBlock(
            element_id="inside",
            page_number=1,
            bbox=BBox(12, 12, 200, 30),
            text="Nome",
            reading_order=0,
        )
    ]
    ir = DocumentIR(file_name="t.pdf", sha256="x", page_count=1, pages=[page])
    text = export_text(build_text_plan(ir), ExportOptions(format="txt")).data.decode("utf-8")
    assert "Nome\tValor" in text
    assert "Ana\t10,00" in text
    assert text.count("Nome") == 1


def test_txt_does_not_invent_content_for_blank_pages():
    page = PageResult(page_number=1, width=600, height=800, classification="blank")
    ir = DocumentIR(file_name="b.pdf", sha256="x", page_count=1, pages=[page])
    result = export_text(build_text_plan(ir), ExportOptions(format="txt"))
    assert result.data.decode("utf-8").strip() == ""
    assert result.warnings == []
