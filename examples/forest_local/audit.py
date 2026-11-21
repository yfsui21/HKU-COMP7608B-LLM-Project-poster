"""Verify a completed upstream run, then package the poster and its provenance."""
import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def hierarchy(node):
    return {"title": node.get("section_name") or node.get("title") or node.get("node_type"),
            "children": [hierarchy(c) for c in node.get("children", [])]}


def finish(work, run, pdf, slug, trace_start):
    def artifact(stage, suffix):
        folder, = run.glob(f"{stage:02d}_*")
        return folder / (slug + "_" + suffix + ".json")

    raw, refined, tree = (load(artifact(i, name))["tree_data"] for i, name in
                          [(1, "raw_tree"), (2, "refined_tree"), (3, "poster_panels")])
    allocation = load(artifact(4, "tree_split_results"))
    initial_text = load(artifact(5, "bullet_contents"))
    arrangements = load(artifact(6, "final_arrangements"))
    activity = load(run / "tmp" / "step6_activity_log.json")
    final = run / "08_finalize_output"
    deck = Presentation(final / "poster_final.pptx")
    if len(deck.slides) != 1 or activity["running"]:
        raise ValueError("Incomplete run or more than one slide")
    sources = {}
    for kind, suffix in [("figure", "images"), ("table", "tables")]:
        for number, entry in load(artifact(1, suffix)).items():
            img = Path(entry["figure_path"])
            sig = hashlib.sha256(img.read_bytes()).hexdigest()
            sources[sig] = dict(asset=f"{kind} {number}", page=entry["page"],
                                crop=entry["box"], caption=entry["caption"], sha256=sig)
    figures, text_boxes, outside = [], [], []
    for shape in deck.slides[0].shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            sig = hashlib.sha256(shape.image.blob).hexdigest()
            figures.append(sources[sig])
        elif shape.has_text_frame and shape.text.strip():
            text_boxes.append(shape.text)
        if min(shape.left, shape.top) < -12700 or shape.left+shape.width > deck.slide_width+12700 or shape.top+shape.height > deck.slide_height+12700:
            outside.append(shape.name)
    if not figures or not text_boxes or len(figures) != len(arrangements["figure_arrangement"]):
        raise ValueError("Exported objects do not match the layout")
    if outside:
        raise ValueError(f"Slide contains out-of-bounds shapes: {outside}")
    with Image.open(final / "poster_final.jpg") as image:
        image.verify()
    with pdfium.PdfDocument(str(pdf)) as doc:
        pages = len(doc)
        first_page = doc[0].get_textpage().get_text_bounded()
    def normalized(value):
        return re.sub(r"[^\w]", "", value).casefold()
    if not normalized(text_boxes[0]) or normalized(text_boxes[0]) not in normalized(first_page):
        raise ValueError("Slide title could not be verified on the PDF's first page")
    requests = [json.loads(line) for line in (work / "inference.jsonl").read_text(encoding="utf-8").splitlines()[trace_start:]]
    if not any(r["images"] for r in requests):
        raise ValueError("No image-bearing model response recorded")
    report = dict(**load(work / "input.json"), pdf_pages=pages, paper=pdf.name,
                  canvas_inches=[deck.slide_width.inches, deck.slide_height.inches], slides=len(deck.slides),
                  title_verified_on_pdf_page=1, editable_text_boxes=len(text_boxes), out_of_bounds_shapes=outside,
                  model_requests={"text": sum(not r["images"] for r in requests),
                                  "vision": sum(bool(r["images"]) for r in requests),
                                  "reused_exact_requests": sum(r["cached"] for r in requests)},
                  stages={"1_document_tree": hierarchy(raw), "2_refined_tree": hierarchy(refined),
                          "3_poster_tree": hierarchy(tree), "4_panel_count": len(allocation["panel_arrangement"]),
                          "5_text_arrangements": len(initial_text),
                          "6_feedback_events": dict(Counter(e["type"] for e in activity["events"]))},
                  figures=figures, slide_text=text_boxes)
    result = work / "result"
    result.mkdir(exist_ok=True)
    for ext in ("pptx", "jpg"):
        shutil.copy2(final / ("poster_final." + ext), result / ("poster." + ext))
    (result / "audit.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Verified poster:", result)
