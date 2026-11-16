"""Deterministic PDF fixtures for the test suite.

The PDFs are generated (not committed binaries) so every test run exercises the
engine against documents whose exact text and geometry are known.

Run manually:  python tests/fixtures/generate_pdfs.py <output-dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

WIDTH, HEIGHT = A4


def _canvas(path: Path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(path.stem)
    c.setAuthor("pdf2x fixtures")
    return c


def _line(c, x: float, y: float, text: str, size: float = 11, bold: bool = False) -> None:
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    c.drawString(x, HEIGHT - y, text)


def simple_field(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 100, "Nome: João da Silva")
    c.showPage()
    c.save()


def multi_fields(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 100, "Nome: João da Silva")
    _line(c, 72, 120, "Idade: 25 anos")
    _line(c, 72, 140, "Cidade: Belo Horizonte")
    _line(c, 72, 160, "Profissão: Analista de Dados")
    c.showPage()
    c.save()


def inline_fields(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 100, "Nome: João da Silva Matrícula: 001245 Cargo: Analista de Dados")
    _line(c, 72, 120, "Cidade: Belo Horizonte")
    c.showPage()
    c.save()


def fields_below(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 100, "Nome:")
    _line(c, 72, 116, "João da Silva")
    _line(c, 72, 140, "Cidade:")
    _line(c, 72, 156, "Belo Horizonte")
    c.showPage()
    c.save()


def multiline_value(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 100, "Nome: João da Silva")
    _line(c, 72, 120, "Endereço: Avenida Principal,")
    _line(c, 72, 136, "número 250, bloco B,")
    _line(c, 72, 152, "apartamento 301")
    _line(c, 72, 176, "Cidade: Belo Horizonte")
    c.showPage()
    c.save()


def two_columns(path: Path) -> None:
    c = _canvas(path)
    rows = [
        ("Nome", "João da Silva"),
        ("Data de nascimento", "15/05/2000"),
        ("Cidade", "Belo Horizonte"),
        ("Cargo", "Analista"),
    ]
    for index, (label, value) in enumerate(rows):
        y = 100 + index * 20
        _line(c, 72, y, label)
        _line(c, 260, y, value)
    c.showPage()
    c.save()


def bordered_table(path: Path) -> None:
    c = _canvas(path)
    x0, top = 72, 100
    widths = [120, 80, 90]
    rows = [
        ["Nome", "Cargo", "Salário"],
        ["João da Silva", "Analista", "R$ 3.500,00"],
        ["Maria Oliveira", "Assistente", "R$ 2.800,00"],
        ["Pedro Santos", "Supervisor", "R$ 4.200,00"],
    ]
    row_height = 22
    x = x0
    for w in widths:
        c.line(x, HEIGHT - top, x, HEIGHT - (top + row_height * len(rows)))
        x += w
    c.line(x, HEIGHT - top, x, HEIGHT - (top + row_height * len(rows)))

    for r_index in range(len(rows) + 1):
        y = top + r_index * row_height
        c.line(x0, HEIGHT - y, x0 + sum(widths), HEIGHT - y)

    for r_index, row in enumerate(rows):
        x = x0
        for c_index, value in enumerate(row):
            _line(c, x + 4, top + r_index * row_height + 14, value, bold=(r_index == 0))
            x += widths[c_index]
    c.showPage()
    c.save()


def borderless_table(path: Path) -> None:
    c = _canvas(path)
    rows = [
        ("PRODUTO", "QTD", "PREÇO"),
        ("Teclado", "2", "150,00"),
        ("Mouse", "3", "80,00"),
        ("Monitor", "1", "1200,00"),
        ("Cabo HDMI", "5", "35,50"),
    ]
    for index, (product, qty, price) in enumerate(rows):
        y = 100 + index * 20
        _line(c, 72, y, product, bold=(index == 0))
        c.setFont("Helvetica-Bold" if index == 0 else "Helvetica", 11)
        c.drawRightString(300, HEIGHT - y, qty)
        c.drawRightString(400, HEIGHT - y, price)
    c.showPage()
    c.save()


def transposed_table(path: Path) -> None:
    c = _canvas(path)
    rows = [
        ("Campo", "Valor"),
        ("Nome", "João da Silva"),
        ("Idade", "25"),
        ("Cidade", "Belo Horizonte"),
        ("Cargo", "Analista"),
    ]
    x0, top = 72, 100
    row_height = 22
    c.rect(x0, HEIGHT - (top + row_height * len(rows)), 300, row_height * len(rows))
    for index in range(len(rows) + 1):
        y = top + index * row_height
        c.line(x0, HEIGHT - y, x0 + 300, HEIGHT - y)
    c.line(x0 + 120, HEIGHT - top, x0 + 120, HEIGHT - (top + row_height * len(rows)))
    for index, (label, value) in enumerate(rows):
        _line(c, x0 + 4, top + index * row_height + 14, label, bold=(index == 0))
        _line(c, x0 + 124, top + index * row_height + 14, value, bold=(index == 0))
    c.showPage()
    c.save()


def multipage_table(path: Path, pages: int = 3) -> None:
    c = _canvas(path)
    data = [
        ("Nome", "Salário"),
        ("João", "3500"),
        ("Maria", "2800"),
        ("Pedro", "4200"),
        ("Ana", "3100"),
        ("Carla", "2900"),
        ("Bruno", "3300"),
    ]
    per_page = 3
    index = 0
    for page in range(pages):
        x0, top = 72, 100
        widths = [180, 120]
        chunk = data[index : index + per_page]
        index += per_page
        if not chunk:
            break
        row_height = 22
        for r in range(len(chunk) + 1):
            y = top + r * row_height
            c.line(x0, HEIGHT - y, x0 + sum(widths), HEIGHT - y)
        c.line(x0, HEIGHT - top, x0, HEIGHT - (top + row_height * len(chunk)))
        c.line(x0 + widths[0], HEIGHT - top, x0 + widths[0], HEIGHT - (top + row_height * len(chunk)))
        c.line(x0 + sum(widths), HEIGHT - top, x0 + sum(widths), HEIGHT - (top + row_height * len(chunk)))
        for r, row in enumerate(chunk):
            _line(c, x0 + 4, top + r * row_height + 14, row[0], bold=(r == 0))
            _line(c, x0 + widths[0] + 4, top + r * row_height + 14, row[1], bold=(r == 0))
        c.showPage()
    c.save()


def free_text(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 100, "Relatório de Atividades", size=16, bold=True)
    _line(c, 72, 130, "Durante o mês de outubro foram realizadas diversas", size=11)
    _line(c, 72, 146, "atividades de análise de dados.", size=11)
    _line(c, 72, 172, "Os resultados foram enviados ao setor responsável", size=11)
    _line(c, 72, 188, "para conferência.", size=11)
    c.showPage()
    c.save()


def mixed_report(path: Path) -> None:
    c = _canvas(path)
    _line(c, 72, 70, "RELATÓRIO DE FUNCIONÁRIO", size=16, bold=True)
    _line(c, 72, 100, "Dados cadastrais", size=13, bold=True)
    _line(c, 72, 120, "Nome: João da Silva")
    _line(c, 72, 140, "Matrícula: 001245")
    _line(c, 72, 160, "Cargo: Analista de Dados")
    _line(c, 72, 180, "Cidade: Belo Horizonte")
    _line(c, 72, 215, "Pagamentos", size=13, bold=True)

    x0, top = 72, 235
    widths = [110, 110, 90]
    rows = [
        ["Mês", "Salário", "Bônus"],
        ["Janeiro", "R$ 3.500,00", "R$ 300,00"],
        ["Fevereiro", "R$ 3.500,00", "R$ 400,00"],
    ]
    row_height = 22
    for r in range(len(rows) + 1):
        y = top + r * row_height
        c.line(x0, HEIGHT - y, x0 + sum(widths), HEIGHT - y)
    xx = x0
    for w in widths:
        c.line(xx, HEIGHT - top, xx, HEIGHT - (top + row_height * len(rows)))
        xx += w
    c.line(xx, HEIGHT - top, xx, HEIGHT - (top + row_height * len(rows)))
    for r, row in enumerate(rows):
        xx = x0
        for c_index, value in enumerate(row):
            _line(c, xx + 4, top + r * row_height + 14, value, bold=(r == 0))
            xx += widths[c_index]

    _line(c, 72, 340, "Observações", size=13, bold=True)
    _line(c, 72, 360, "O funcionário recebeu adicional de produtividade")
    _line(c, 72, 376, "conforme as normas internas.")
    c.showPage()
    c.save()


def rotated_page(path: Path) -> None:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject

    temporary = path.with_suffix(".base.pdf")
    c = _canvas(temporary)
    # Realistic case: the content is stored rotated and /Rotate 90 makes it
    # readable.  Text is drawn along the rotated axis, exactly like a landscape
    # scan in a portrait container.
    c.saveState()
    c.translate(120, 120)
    c.rotate(90)
    c.setFont("Helvetica", 11)
    c.drawString(0, 0, "Nome: João da Silva")
    c.drawString(0, -24, "Cidade: Belo Horizonte")
    c.restoreState()
    c.showPage()
    c.save()

    reader = PdfReader(str(temporary))
    writer = PdfWriter()
    page = reader.pages[0]
    page[NameObject("/Rotate")] = NumberObject(90)
    writer.add_page(page)
    with open(path, "wb") as handle:
        writer.write(handle)
    temporary.unlink(missing_ok=True)


def scanned_page(path: Path) -> None:
    """An image-only page: no text layer, exactly like a paper scan."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader

    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    draw.text((120, 150), "Nome: João da Silva", fill="black", font=font)
    draw.text((120, 220), "Cidade: Belo Horizonte", fill="black", font=font)
    image_path = path.with_suffix(".png")
    image.save(image_path)

    c = _canvas(path)
    c.drawImage(ImageReader(str(image_path)), 0, 0, width=WIDTH, height=HEIGHT)
    c.showPage()
    c.save()
    image_path.unlink(missing_ok=True)


def hybrid_page(path: Path) -> None:
    """Native text plus a scanned region on the same page."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 30)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    draw.text((40, 60), "Imagem digitalizada", fill="black", font=font)
    draw.text((40, 140), "Nome: Maria Oliveira", fill="black", font=font)
    image_path = path.with_suffix(".png")
    image.save(image_path)

    from reportlab.lib.utils import ImageReader

    c = _canvas(path)
    _line(c, 72, 80, "RELATÓRIO HÍBRIDO", size=14, bold=True)
    _line(c, 72, 110, "Código: 4707")
    c.drawImage(ImageReader(str(image_path)), 72, HEIGHT - 400, width=400, height=186)
    c.showPage()
    c.save()
    image_path.unlink(missing_ok=True)


def mixed_native_scan(path: Path) -> None:
    """Page 1 has a real text layer; page 2 is an image only (no OCR layer)."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader

    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    draw.text((120, 200), "Contrato 2026", fill="black", font=font)
    draw.text((120, 280), "Nome: Maria Oliveira", fill="black", font=font)
    image_path = path.with_suffix(".png")
    image.save(image_path)

    c = _canvas(path)
    _line(c, 72, 100, "Nome: João da Silva")
    _line(c, 72, 130, "Cidade: Belo Horizonte")
    c.showPage()
    c.drawImage(ImageReader(str(image_path)), 0, 0, width=WIDTH, height=HEIGHT)
    c.showPage()
    c.save()
    image_path.unlink(missing_ok=True)


def native_plus_blank(path: Path) -> None:
    """Page 1 has text; page 2 is legitimately empty (not a lost page)."""
    c = _canvas(path)
    _line(c, 72, 100, "Nome: João da Silva")
    c.showPage()
    c.showPage()
    c.save()


def encrypted_pdf(path: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    temporary = path.with_suffix(".base.pdf")
    c = _canvas(temporary)
    _line(c, 72, 100, "Nome: João da Silva")
    c.showPage()
    c.save()
    reader = PdfReader(str(temporary))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("segredo")
    with open(path, "wb") as handle:
        writer.write(handle)
    temporary.unlink(missing_ok=True)


def not_a_pdf(path: Path) -> None:
    path.write_bytes(b"This is not a PDF at all.")


BUILDERS = {
    "simple_field": simple_field,
    "multi_fields": multi_fields,
    "inline_fields": inline_fields,
    "fields_below": fields_below,
    "multiline_value": multiline_value,
    "two_columns": two_columns,
    "bordered_table": bordered_table,
    "borderless_table": borderless_table,
    "transposed_table": transposed_table,
    "multipage_table": multipage_table,
    "free_text": free_text,
    "mixed_report": mixed_report,
    "rotated_page": rotated_page,
    "scanned_page": scanned_page,
    "hybrid_page": hybrid_page,
    "mixed_native_scan": mixed_native_scan,
    "native_plus_blank": native_plus_blank,
    "encrypted_pdf": encrypted_pdf,
    "not_a_pdf": not_a_pdf,
}


def generate_all(output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: dict[str, Path] = {}
    for name, builder in BUILDERS.items():
        suffix = ".txt" if name == "not_a_pdf" else ".pdf"
        path = output_dir / f"{name}{suffix}"
        builder(path)
        generated[name] = path
    return generated


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "generated"
    created = generate_all(output)
    for name, path in created.items():
        print(f"{name:<20} {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
