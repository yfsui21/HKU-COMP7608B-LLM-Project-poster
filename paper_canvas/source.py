"""PDF extraction, source verification and page crops."""
import io
import re
from itertools import groupby
from pathlib import Path
import pdfplumber
import pypdfium2 as pdfium

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "paired_review.pdf"

def reading_order(page):
    """Separate substantial text columns only when a continuous gutter is visible."""
    words=[w for w in page.extract_words(x_tolerance=1) if .2*page.height<w["top"]<.94*page.height]
    xs=range(round(.23*page.width),round(.76*page.width),3)
    gaps=[list(group) for clear,group in groupby(xs,key=lambda x:not any(w["x0"]<x<w["x1"] for w in words)) if clear]
    for gap in sorted(gaps,key=len,reverse=True):
        split=gap[len(gap)//2]
        if len(gap)>=4 and min(sum(w["x1"]<split for w in words),sum(w["x0"]>split for w in words))>=45:
            cols=[page.crop((0,0,split,page.height)),page.crop((split,0,page.width,page.height))]
            return "\n".join(col.extract_text(x_tolerance=1) or "" for col in cols)
    return page.extract_text(x_tolerance=1) or ""

def read_paper(path):
    with pdfplumber.open(path) as pdf:
        pages = []
        for i, page in enumerate(pdf.pages, 1):
            text = re.sub(r"(?<=\w)-\n(?=[a-z])","",reading_order(page))
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
        anchors=[s["quote"]]
        if s.get("additional_quote"):
            if not on_page(s["page"],s["additional_quote"]):
                raise ValueError("Additional source passage missing for "+s["label"])
            anchors.append(s["additional_quote"])
        if not s["required"] or any(not any(t.lower() in q.lower() for q in anchors) or t.lower() not in s["text"].lower() for t in s["required"]):
            raise ValueError("Required context missing from claim or evidence")
    ev = plan["evidence"]
    if not on_page(ev["page"],ev["caption"]):
        raise ValueError("Missing verifiable figure/table caption")
