#!/usr/bin/env python3
"""MUSC-letterhead PDF renderer for payer appeal letters (ReportLab)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# Without this every render stamps a fresh /CreationDate and document /ID, so 67
# identical letters come out as 67 changed files — 5.8 MB of git churn that says
# nothing about the letters. Invariant makes the bytes a function of the content,
# which is what lets the deploy mirror tell a real change from a re-render.
rl_config.invariant = 1

HERE = Path(__file__).parent
LOGO = HERE / "assets" / "musc-logo-navy.png"  # white knockout logo re-filled MUSC navy

MUSC_TEAL = colors.HexColor("#00857D")
MUSC_NAVY = colors.HexColor("#0B2C4A")
GREY = colors.HexColor("#5A6570")

ORG = {
    "name": "Medical University of South Carolina",
    "dept": "MUSC Health — Revenue Cycle / Patient Financial Services",
    "address": "169 Ashley Avenue, MSC 332",
    "city": "Charleston, South Carolina 29425",
    "phone": "(843) 792-2300",
    "fax": "(843) 792-9111",
    "web": "muschealth.org",
}

_styles = getSampleStyleSheet()
BODY = ParagraphStyle(
    "musc_body", parent=_styles["Normal"], fontName="Times-Roman", fontSize=10.5,
    leading=14.5, alignment=TA_JUSTIFY, spaceAfter=9,
)
H = ParagraphStyle(
    "musc_h", parent=BODY, fontName="Times-Bold", fontSize=10.5, leading=14,
    textColor=MUSC_NAVY, spaceBefore=8, spaceAfter=4, alignment=0, keepWithNext=1,
)
SMALL = ParagraphStyle(
    "musc_small", parent=BODY, fontSize=8, leading=10.5, textColor=GREY, alignment=0,
)
META = ParagraphStyle("musc_meta", parent=BODY, fontSize=9, leading=12, alignment=0, spaceAfter=0)


def _header(canvas, doc):
    canvas.saveState()
    w, h = LETTER
    if LOGO.exists():
        lw = 1.75 * inch
        canvas.drawImage(
            str(LOGO), 0.85 * inch, h - 1.24 * inch, width=lw, height=lw * 784 / 1200,
            mask="auto", preserveAspectRatio=True, anchor="sw",
        )
    canvas.setFont("Helvetica", 7.6)
    canvas.setFillColor(GREY)
    right = w - 0.85 * inch
    lines = [ORG["dept"], ORG["address"], ORG["city"], f"P {ORG['phone']}  •  F {ORG['fax']}", ORG["web"]]
    y = h - 0.62 * inch
    for line in lines:
        canvas.drawRightString(right, y, line)
        y -= 9.5
    canvas.setStrokeColor(MUSC_TEAL)
    canvas.setLineWidth(2)
    canvas.line(0.85 * inch, h - 1.32 * inch, right, h - 1.32 * inch)

    # footer
    canvas.setStrokeColor(colors.HexColor("#D5DBE0"))
    canvas.setLineWidth(0.6)
    canvas.line(0.85 * inch, 0.78 * inch, right, 0.78 * inch)
    canvas.setFont("Helvetica", 6.8)
    canvas.setFillColor(GREY)
    canvas.drawString(
        0.85 * inch, 0.62 * inch,
        "CONFIDENTIAL — contains protected health information. Intended solely for the addressed payer.",
    )
    canvas.drawString(
        0.85 * inch, 0.53 * inch,
        "DEMONSTRATION DOCUMENT — generated from synthetic patient data. Not a real patient or claim.",
    )
    canvas.drawRightString(right, 0.62 * inch, f"Page {doc.page}")
    canvas.restoreState()


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(f"<b>{k}</b>", META), Paragraph(v or "—", META)] for k, v in rows]
    t = Table(data, colWidths=[1.55 * inch, 4.35 * inch], hAlign="LEFT")
    t.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#E4E8EB")),
        ])
    )
    return t


def render_letter(path: Path, letter: dict) -> Path:
    """Render one appeal letter.

    ``letter`` keys: payer_name, payer_address, subject, meta_rows (list of
    (label, value)), sections (list of (heading, text)), salutation, closing_name,
    closing_title, closing_dept, enclosures (list[str]), letter_date.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(path), pagesize=LETTER,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
        topMargin=1.55 * inch, bottomMargin=0.95 * inch,
        title=letter.get("subject", "MUSC Appeal Letter"),
        author="MUSC Health Revenue Cycle",
        subject="Insurance claim denial appeal",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="musc", frames=[frame], onPage=_header)])

    flow: list = []
    flow.append(Paragraph(letter.get("letter_date") or date.today().strftime("%B %d, %Y"), BODY))
    flow.append(Spacer(1, 6))
    addr = letter.get("payer_address") or []
    for line in [letter["payer_name"], *addr]:
        flow.append(Paragraph(line, META))
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(f"<b>RE: {letter['subject']}</b>", BODY))
    flow.append(Spacer(1, 4))
    flow.append(_kv_table(letter.get("meta_rows", [])))
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(letter.get("salutation", "Dear Appeals Review Committee:"), BODY))
    flow.append(Spacer(1, 4))

    for heading, text in letter.get("sections", []):
        paras = [Paragraph(p.strip(), BODY) for p in (text or "").split("\n") if p.strip()]
        if not paras:
            continue
        if heading:
            # H has keepWithNext, so the heading never orphans at a page bottom
            flow.append(Paragraph(heading.upper(), H))
        flow.extend(paras)

    flow.append(Spacer(1, 14))
    flow.append(Paragraph("Respectfully submitted,", BODY))
    flow.append(Spacer(1, 26))
    flow.append(Paragraph(f"<b>{letter.get('closing_name', 'MUSC Health Appeals Team')}</b>", META))
    flow.append(Paragraph(letter.get("closing_title", "Appeals Specialist, Revenue Cycle"), META))
    flow.append(Paragraph(letter.get("closing_dept", ORG["dept"]), META))
    flow.append(Paragraph(f"{ORG['phone']}  |  {ORG['web']}", META))

    if letter.get("enclosures"):
        flow.append(Spacer(1, 10))
        flow.append(Paragraph("<b>Enclosures</b>", META))
        for e in letter["enclosures"]:
            flow.append(Paragraph(f"• {e}", SMALL))

    doc.build(flow)
    return path


if __name__ == "__main__":  # smoke test
    render_letter(
        HERE / "letters" / "_sample.pdf",
        {
            "payer_name": "UnitedHealthcare",
            "payer_address": ["Provider Appeals Department", "P.O. Box 30432", "Salt Lake City, UT 84130"],
            "subject": "Formal Appeal of Claim Denial — Jane Q. Sample (MRN MUSC0000001)",
            "meta_rows": [("Patient", "Jane Q. Sample"), ("Claim ID", "MUSC-20260101-AAAA-1")],
            "sections": [("Summary", "This is a sample paragraph.\nSecond paragraph.")],
            "enclosures": ["Remittance advice", "Operative note"],
        },
    )
    print("wrote sample")
