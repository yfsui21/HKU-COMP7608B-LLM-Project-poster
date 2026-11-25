"""Build the small synthetic paper used by the offline tests.

The content is invented and labelled as such. It exists so CI can exercise
the parser's caption-anchored figure/table extraction, the claim checks and
the whole revision loop without a network connection or a 49 MB PDF.

    python scripts/make_synthetic_paper.py            # -> samples/synthetic_paper.pdf
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

OUT = Path(__file__).resolve().parents[1] / "samples" / "synthetic_paper.pdf"

W, H = letter
LM, RM = 60, W - 60


def para(c: Canvas, y: float, text: str, size: float = 10.5, lead: float = 14) -> float:
    """Greedy word wrap between the margins; returns the next baseline."""
    c.setFont("Times-Roman", size)
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if c.stringWidth(trial, "Times-Roman", size) > RM - LM:
            c.drawString(LM, y, line)
            y -= lead
            line = word
        else:
            line = trial
    if line:
        c.drawString(LM, y, line)
        y -= lead
    return y - 6


def heading(c: Canvas, y: float, text: str) -> float:
    c.setFont("Times-Bold", 13)
    c.drawString(LM, y, text)
    return y - 20


def bar_chart(c: Canvas, x: float, y: float) -> None:
    """Vector bar chart with axis labels, drawn bottom-left at (x, y)."""
    data = [("Single review", 12), ("Checklist", 9), ("Paired review", 5)]
    c.setStrokeColorRGB(0.2, 0.2, 0.2)
    c.line(x, y, x + 300, y)
    c.line(x, y, x, y + 130)
    for i, (label, v) in enumerate(data):
        bx = x + 30 + i * 90
        c.setFillColorRGB(0.12, 0.42 + 0.12 * i, 0.55)
        c.rect(bx, y, 50, v * 9, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 8)
        c.drawString(bx, y - 11, label)
        c.drawString(bx + 18, y + v * 9 + 4, str(v))
    c.setFont("Helvetica", 8)
    c.drawString(x - 6, y + 134, "errors")


def line_chart(c: Canvas, x: float, y: float) -> None:
    pts_a = [(0, 30), (1, 52), (2, 68), (3, 77), (4, 81)]
    pts_b = [(0, 30), (1, 40), (2, 47), (3, 51), (4, 53)]
    c.setStrokeColorRGB(0.2, 0.2, 0.2)
    c.line(x, y, x + 300, y)
    c.line(x, y, x, y + 120)
    for pts, rgb in ((pts_a, (0.8, 0.3, 0.1)), (pts_b, (0.1, 0.3, 0.8))):
        c.setStrokeColorRGB(*rgb)
        c.setLineWidth(1.6)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            c.line(x + 20 + x0 * 65, y + y0 * 1.3, x + 20 + x1 * 65, y + y1 * 1.3)
    c.setLineWidth(1)
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica", 8)
    for i in range(5):
        c.drawString(x + 17 + i * 65, y - 11, f"{(i + 1) * 20}")
    c.drawString(x + 120, y - 22, "records reviewed per annotator")
    c.drawString(x + 230, y + 110, "paired")
    c.drawString(x + 230, y + 70, "single")


def table(c: Canvas, x: float, y: float) -> None:
    rows = [("Protocol", "Errors", "Minutes", "Agreement"),
            ("Single review", "12", "31", "0.71"),
            ("Checklist", "9", "38", "0.78"),
            ("Paired review", "5", "54", "0.89")]
    c.setStrokeColorRGB(0, 0, 0)
    c.line(x, y, x + 330, y)
    c.line(x, y - 18, x + 330, y - 18)
    c.line(x, y - 18 * len(rows), x + 330, y - 18 * len(rows))
    for r, row in enumerate(rows):
        c.setFont("Helvetica-Bold" if r == 0 else "Helvetica", 9)
        for k, cell in enumerate(row):
            c.drawString(x + 6 + k * 82, y - 13 - r * 18, cell)


def build(out: Path = OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    c = Canvas(str(out), pagesize=letter, invariant=1)
    c.setTitle("Paired Review Reduces Labeling Errors in Field Observations")

    c.setFont("Times-Bold", 20)
    c.drawCentredString(W / 2, H - 70, "Paired Review Reduces Labeling Errors")
    c.drawCentredString(W / 2, H - 94, "in Field Observations")
    c.setFont("Times-Roman", 11)
    c.drawCentredString(W / 2, H - 116, "A. Example, B. Example (synthetic test document)")

    y = H - 150
    y = heading(c, y, "Abstract")
    y = para(c, y, "This synthetic note asks whether a second, independent review reduces labeling "
             "errors in field observation records. On 40 synthetic records, paired review reduced "
             "incorrect labels from 12 to 5 compared with a single review, at the cost of 23 "
             "additional minutes per batch.")
    y = heading(c, y, "1 Introduction")
    y = para(c, y, "Field observation datasets are labeled by a single annotator in most projects. "
             "Labeling errors propagate into every downstream analysis, yet the benefit of a "
             "second review is rarely measured. Our research question is whether a paired "
             "review reduces labeling errors relative to a single review and to a checklist.")
    y = heading(c, y, "2 Method")
    y = para(c, y, "Two annotators reviewed 40 synthetic field records independently. A paired review "
             "reconciled disagreements before final scoring. The checklist condition used one "
             "annotator with a 12-item checklist. Errors count incorrect labels against a "
             "reference key, with one label per record.")
    bar_chart(c, LM + 40, y - 170)
    c.setFont("Times-Roman", 10)
    c.drawString(LM, y - 200, "Figure 1: Incorrect labels on 40 synthetic records for each review protocol.")
    c.showPage()

    y = H - 70
    y = heading(c, y, "3 Results")
    y = para(c, y, "Paired review reduced errors from 12 to 5 on the 40 synthetic records, while the "
             "checklist reached 9 errors. Agreement between annotators rose to 0.89 with paired "
             "review. The improvement held as the number of records per annotator grew from 20 "
             "to 100, where paired accuracy reached 81% against 53% for a single review.")
    table(c, LM + 40, y - 6)
    c.setFont("Times-Roman", 10)
    c.drawString(LM, y - 92, "Table 1: Errors, review time and agreement for the three protocols.")
    line_chart(c, LM + 40, y - 270)
    c.drawString(LM, y - 312, "Figure 2: Accuracy (%) as the number of records per annotator grows.")
    y = y - 342
    y = heading(c, y, "4 Conclusion")
    y = para(c, y, "A paired review is a cheap way to cut labeling errors when records are few. "
             "Its cost is review time: paired review took 54 minutes against 31 for a single review.")
    y = heading(c, y, "Limitations")
    para(c, y, "All records are synthetic and the study uses two annotators, so the numbers "
         "illustrate the workflow rather than establish an effect size.")
    c.save()
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
