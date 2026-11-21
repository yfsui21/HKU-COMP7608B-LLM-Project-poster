"""PDF extraction, source verification and page crops."""
import io
import re
from pathlib import Path
import pdfplumber
import pypdfium2 as pdfium

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "paired_review.pdf"

def read_paper(path):
    with pdfplumber.open(path) as pdf:
        pages = []
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            tables = page.extract_tables()
            captions = [v for v in text.splitlines() if re.match(r"^(fig(?:ure)?\.?|table)\s*\d+",v,re.I)]
            pages.append(dict(page=i,text=text,tables=tables,captions=captions))
    if not any(p["text"].strip() for p in pages):
        raise ValueError("No extractable text. Supply a text-based PDF.")
    return pages

def page_png(path,page,box=None):
    with pdfium.PdfDocument(str(path)) as doc:
        image = doc[page-1].render(scale=2).to_pil().convert("RGB")
    if box is not None:
        x,y,r,b = box
        if not (0 <= x < r <= 1 and 0 <= y < b <= 1):
            raise ValueError("Invalid normalized evidence crop")
        image = image.crop((round(x*image.width),round(y*image.height),round(r*image.width),round(b*image.height)))
    result = io.BytesIO()
    image.save(result,format="PNG")
    return result.getvalue()

def fixture_plan(path):
    if path.read_bytes() != EXAMPLE.read_bytes():
        raise ValueError("Offline mode supports examples/paired_review.pdf only. Use --live for other papers.")
    quotes = [
        ("Research question","Can a second review reduce labeling errors in field observations?", ["labeling errors"]),
        ("Method","Two annotators reviewed 40 synthetic field records independently.", ["Two annotators","40 synthetic field records"]),
        ("Key finding","On 40 synthetic records, paired review reduced errors from 12 to 5.", ["40 synthetic records","paired review","12 to 5"]),
    ]
    return dict(title="Paired review for field observations",
                sections=[dict(label=k,quote=q,text=q,page=1,required=t) for k,q,t in quotes],
                evidence=dict(page=1,caption="Figure 1. Incorrect labels across 40 synthetic field records.",box=[.075,.55,.84,.70]))

def verify(plan,pages):
    if not plan["title"].strip() or len(plan["sections"]) != 3:
        raise ValueError("Expected a title and three sections")
    def on_page(page,fragment):
        return (type(page) is int and 1 <= page <= len(pages) and fragment.strip()
                and " ".join(fragment.split()) in " ".join(pages[page-1]["text"].split()))
    for s in plan["sections"]:
        if not on_page(s["page"],s["quote"]):
            raise ValueError("Source passage missing for "+s["label"])
        if not s["required"] or any(t.lower() not in s["quote"].lower() or t.lower() not in s["text"].lower() for t in s["required"]):
            raise ValueError("Required context missing from claim or evidence")
    ev = plan["evidence"]
    if not on_page(ev["page"],ev["caption"]):
        raise ValueError("Missing verifiable figure/table caption")
