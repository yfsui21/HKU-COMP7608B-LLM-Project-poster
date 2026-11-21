"""Build an additional evidence-linked display poster from a completed PosterForest run."""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches, Pt

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime import ChatAgent, fit_document, get_json_from_response, ppt_to_images_with_title


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def make_plan(work, run):
    folder, = run.glob("01_*")
    audit = read(work / "result/audit.json")
    pages = read(next(folder.glob("*_pages.json")))
    assets = {}
    for kind, filename in [("figure", "images"), ("table", "tables")]:
        for num, info in read(next(folder.glob(f"*_{filename}.json"))).items():
            assets[f"{kind} {num}"] = info
    instructions = """Prepare a compact scientific poster based on the native poster and source pages.
Choose three distinct assets by their actual captions. The central result is large, the method and secondary-result figures are smaller.
Return exactly this JSON object: authors (names only), publication (venue and year as on PDF), question (one sentence under 120 characters),
method_title, result_title, secondary_title (each under 38 characters), method (under 56 words),
method_asset, main_asset, secondary_asset (three keys from the supplied assets),
method_caption, main_caption, secondary_caption (each under 30 words, specific to the corresponding figure),
metric (single prominent value under 14 characters), metric_label (under 75 characters), detail (under 44 words with comparator and conditions),
limitation (under 42 words), evidence (array of {page: integer, quote: literal excerpt} supporting quantitative result and limitation).
Every claim must come from this paper. Use plain language. Use short headings.
Source page numbers refer to the PDF page sequence. Omit ambiguous claims."""
    body = "\n".join(f"\n## PDF page {p['page']}\n{p['text']}" for p in pages)
    context = {"poster": audit["slide_text"], "assets": {k: v["caption"] for k, v in assets.items()},
               "source": fit_document(body, 75000)}
    agent = ChatAgent(instructions, audit["model"])
    plan = get_json_from_response(agent.step(json.dumps(context, ensure_ascii=False)).msgs[0].content)
    choices = [plan[k] for k in ("method_asset", "main_asset", "secondary_asset")]
    if len(set(choices)) != 3 or any(k not in assets for k in choices):
        raise ValueError("Expected three distinct source assets in the display plan")
    page_text = {p["page"]: " ".join(p["text"].split()) for p in pages}
    if len(plan["evidence"]) < 2 or any(not e["quote"].strip() or
        " ".join(e["quote"].split()) not in page_text[e["page"]] for e in plan["evidence"]):
        raise ValueError("Display evidence must match the PDF text verbatim")
    return audit, plan, assets


def render(dest, title, plan, assets):
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(48), Inches(36)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    ink, accent, muted, paper = "132C33", "006C66", "39585D", "FFFFFF"

    def rect(x, y, w, h, color):
        s = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
        s.fill.solid()
        s.fill.fore_color.rgb = RGBColor.from_string(color)
        s.line.fill.background()
        return s

    def label(x, y, w, h, value, size, color=ink, bold=False):
        s = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = s.text_frame
        tf.clear()
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        p = tf.paragraphs[0]
        p.text = value
        p.font.name, p.font.size, p.font.bold = "Arial", Pt(size), bold
        p.font.color.rgb = RGBColor.from_string(color)
        return s

    def asset(x, y, w, h, role):
        key = plan[role + "_asset"]
        meta = assets[key]
        path = Path(meta["figure_path"])
        with Image.open(path) as image:
            scale = min(w / image.width, h / image.height)
            iw, ih = image.width*scale, image.height*scale
        slide.shapes.add_picture(str(path), Inches(x + (w-iw)/2), Inches(y + (h-ih)/2), Inches(iw), Inches(ih))
        label(x, y+h+.28, w, .55, f"{key.upper()}   /   PDF p. {meta['page']}", 26, accent, True)
        label(x, y+h+1.02, w, 2.65, plan[role + "_caption"], 37, muted)

    rect(0, 0, 48, 7, ink)
    label(1.2, .52, 45.5, .75, plan["publication"], 33, "B1DCD4", True)
    label(1.13, 1.64, 45.7, 2.35, title, min(142, 1840/max(14,len(title)) + 48), paper, True)
    label(1.2, 4.65, 45.5, 1.16, plan["authors"], 48, paper)
    rect(0, 7, 48, .12, "F0B26D")
    label(1.18, 8.05, 45.6, 2.50, plan["question"], 65, ink, True)
    rect(1.2, 10.8, 45.6, .045, "94B8B3")
    rect(14.48, 11.75, .04, 19.65, "BCD1CF")
    rect(32.78, 11.75, .04, 19.65, "BCD1CF")
    for x,w,tag,key in [(1.2,12.5,"METHOD","method_title"),(15.24,16.8,"PRIMARY RESULT","result_title"),
                        (33.54,13.25,"SECONDARY RESULT","secondary_title")]:
        label(x, 11.72, w, .55, tag, 28, accent, True)
        label(x, 12.67, w, 2.1, plan[key], 51, ink, True)
    label(1.2, 15.3, 12.5, 6.0, plan["method"], 43)
    asset(1.2, 21.6, 12.5, 5.65, "method")
    asset(15.24, 14.6, 16.8, 13.35, "main")
    label(33.54, 14.85, 13, 1.8, plan["metric"], 108, accent, True)
    label(33.54, 16.85, 13.12, 1.55, plan["metric_label"], 37, ink, True)
    label(33.54, 18.6, 13.1, 3.25, plan["detail"], 37)
    asset(33.54, 22.2, 13.1, 5.35, "secondary")
    rect(0, 32.15, 48, 3.85, "E4F1ED")
    label(1.2, 33.12, 7.35, 1.9, "Interpretation", 53, accent, True)
    label(9.05, 33.03, 37.8, 2.35, plan["limitation"], 43)
    pptx = dest / "display.pptx"
    deck.save(pptx)
    reopened = Presentation(pptx)
    if len(reopened.slides) != 1 or any(s.left < 0 or s.top < 0 or s.left+s.width > deck.slide_width or
                                        s.top+s.height > deck.slide_height for s in reopened.slides[0].shapes):
        raise ValueError("Display poster exceeds canvas")
    ppt_to_images_with_title(pptx, dest, dpi=72, stem="display", output_type="jpg")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("--soffice", required=True, type=Path)
    args = p.parse_args()
    work = args.work.resolve(strict=True)
    os.environ["FOREST_SOFFICE"] = str(args.soffice.resolve(strict=True))
    os.chdir(work)
    identity = read(work / "input.json")
    os.environ["FOREST_BACKEND"] = identity["backend"]
    run = max((work / "outputs").glob("*/08_finalize_output/poster_final.pptx"), key=lambda f:f.stat().st_mtime).parents[1]
    audit, plan, assets = make_plan(work, run)
    dest = work / "result"
    render(dest, audit["stages"]["1_document_tree"]["title"], plan, assets)
    sources = {name: {"page": info["page"], "caption": info["caption"],
                      "sha256": hashlib.sha256(Path(info["figure_path"]).read_bytes()).hexdigest()}
               for name, info in assets.items() if name in [plan[k + "_asset"] for k in ("main", "method", "secondary")]}
    (dest / "display-evidence.json").write_text(json.dumps(dict(plan=plan, sources=sources), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
