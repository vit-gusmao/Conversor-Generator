"""Synthetic multi-page PDFs for the large-document gates.

Each page carries a unique marker so a test can prove that *every* page of the
original document has an outcome, not just that the run "finished".
"""

from __future__ import annotations

from pathlib import Path


def generate_marked_pdf(path: Path, pages: int, *, prefix: str = "PAGINA") -> Path:
    """Writes a native (text layer) PDF with one unique marker per page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    for page_number in range(1, pages + 1):
        document.setFont("Helvetica-Bold", 16)
        document.drawString(48, height - 64, f"Relatorio sintetico - pagina {page_number}")
        document.setFont("Helvetica", 11)
        document.drawString(48, height - 96, f"{prefix}-{page_number:06d}")
        document.drawString(48, height - 116, "Nome: Documento Sintetico")
        document.drawString(48, height - 132, f"Valor: {page_number}.50")
        document.drawString(48, height - 148, f"Data: 01/01/2026")
        document.showPage()
    document.save()
    return path


def page_marker(page_number: int, *, prefix: str = "PAGINA") -> str:
    return f"{prefix}-{page_number:06d}"


__all__ = ["generate_marked_pdf", "page_marker"]
