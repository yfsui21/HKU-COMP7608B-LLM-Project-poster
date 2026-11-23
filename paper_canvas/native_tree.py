"""Edit a PosterForest export in its own panel tree and PowerPoint coordinate system."""
import hashlib
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from difflib import SequenceMatcher
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from .agent import _acquire_lock, read, write


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def box(shape):
    return [shape.left, shape.top, shape.width, shape.height]


def project(rect, old, new):
    """Map a shape from the original parent rectangle into its revised rectangle."""
    x, y, w, h = rect
    sx, sy = new[2] / old[2], new[3] / old[3]
    return [new[0] + (x-old[0])*sx, new[1] + (y-old[1])*sy, w*sx, h*sy]


def compact(node, panels, available):
    kind = node["type"]
    if kind in ("group", "leaf"):
        data = node["panel_data"]
        source_id = str(data["panel_id"])
        name = data.get("section_name") or data.get("panel_name")
        candidates = [key for key in available if panels[key]["source_id"]==source_id and panels[key]["name"]==name]
        if not candidates:
            raise ValueError(f"Missing panel {source_id} ({name}) in the final slide")
        pid = candidates[0]
        available.remove(pid)
        out = dict(type=kind, panel=pid, name=panels[pid]["name"], box=panels[pid]["box"])
        if kind == "group":
            out["child"] = compact(node["child_tree"], panels, available)
        return out
    if not kind.startswith("internal") or node["split_type"] not in ("horizontal", "vertical"):
        raise ValueError("Unknown layout node: " + kind)
    first, second = [compact(c, panels, available) for c in node["children"]]
    a, b = first["box"], second["box"]
    x, y = min(a[0], b[0]), min(a[1], b[1])
    bounds = [x, y, max(a[0]+a[2], b[0]+b[2])-x, max(a[1]+a[3], b[1]+b[3])-y]
    axis = int(node["split_type"] == "horizontal")
    if a[axis]+a[axis+2] > b[axis]+1500:
        raise ValueError("Final panel positions disagree with source split " + kind)
    # Step 6 may change Step 4's ratio. Read the accepted geometry including inter-panel gaps.
    ratio = a[axis+2] / (a[axis+2]+b[axis+2])
    return dict(type="split", id=kind, direction=node["split_type"], box=bounds,
                original_ratio=ratio, ratio=ratio, children=[first, second])


def branches(node):
    if node["type"] == "group":
        yield from branches(node["child"])
    elif node["type"] == "split":
        yield node
        for child in node["children"]:
            yield from branches(child)


def title(node):
    return node["name"] if "name" in node else " / ".join(title(c) for c in node["children"])


def choices(tree):
    return [dict(id=n["id"], direction=n["direction"], ratio=n["ratio"],
                 first=title(n["children"][0]), second=title(n["children"][1])) for n in branches(tree)]


def panel_boxes(node, target, output):
    if node["type"] != "split":
        output[node["panel"]] = target
        if node["type"] == "group":
            child = node["child"]
            panel_boxes(child, project(child["box"], node["box"], target), output)
        return
    first, second = node["children"]
    a, b = [project(child["box"], node["box"], target) for child in (first, second)]
    axis = int(node["direction"] == "horizontal")
    extent = axis + 2
    delta = (a[extent]+b[extent]) * (node["ratio"]-node["original_ratio"])
    a[extent] += delta
    b[axis] += delta
    b[extent] -= delta
    panel_boxes(first, a, output)
    panel_boxes(second, b, output)


def start(work, dest):
    run = work
    original = run/"08_finalize_output/poster_final.pptx"
    paper_hash = None
    if not original.is_file():
        result = work / "result"
        original = result / "poster.pptx"
        signature = digest(original)
        matches = [p for p in work.glob("outputs/*/08_finalize_output/poster_final.pptx") if digest(p)==signature]
        if not matches:
            raise ValueError("No upstream run matches the generated PowerPoint")
        run = matches[-1].parents[1]
        paper_hash = read(result/"audit.json")["pdf_sha256"]
    layout_file, = run.glob("04*/*_tree_split_results.json")
    final_file, = run.glob("06*/*_final_arrangements.json")
    layout, final = read(layout_file), read(final_file)
    ppt = Presentation(original)
    slide, = ppt.slides
    panels, mapping = {}, {}
    shapes = iter(slide.shapes)
    for key, field, prefix in (("panel_arrangement", "section_name", "var_"),
                               ("text_arrangement", "textbox_name", ""), ("figure_arrangement", "figure_name", "")):
        for item in final[key]:
            shape = next(shapes, None)
            name = item.get(field) or item["panel_name"]
            expected = prefix + re.sub(r"[^0-9a-zA-Z_]+", "_", name)
            if shape is None or shape.name != expected:
                raise ValueError("Upstream shape binding failed for " + name)
            source_id = str(item["panel_id"])
            pid = source_id
            if key == "panel_arrangement":
                if pid in panels:
                    pid = f"{source_id}@{shape.shape_id}"
                panels[pid] = dict(name=name, source_id=source_id, box=box(shape))
            else:
                # Some upstream decks reuse an ID for a content leaf and a subsection.
                candidates = [k for k,p in panels.items() if p["source_id"]==source_id]
                if len(candidates) != 1:
                    cx,cy = shape.left+shape.width/2, shape.top+shape.height/2
                    candidates = [k for k in candidates for x,y,w,h in [panels[k]["box"]]
                                  if x<=cx<=x+w and y<=cy<=y+h]
                if len(candidates)!=1:
                    raise ValueError(f"Ambiguous panel binding for {name} ({source_id})")
                pid = candidates[0]
            mapping[str(shape.shape_id)] = pid
    if next(shapes, None) is not None:
        raise ValueError("Unbound shapes in the source deck")
    tree = compact(layout["content_panel_tree"], panels, list(panels))
    owned = {}
    panel_boxes(tree, tree["box"], owned)
    if set(panels) - set(owned) != {str(layout["title_panel"]["panel_id"])}:
        raise ValueError("Tree does not cover the body panels")
    state = dict(format="posterforest-tree-v1", tree=tree, panels=panels, owners=mapping,
                 original_sha256=digest(original), pdf_sha256=paper_hash,
                 artifact="original", history=[])
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        session = Path(tmp)/"session"
        initial = session/"original"
        initial.mkdir(parents=True)
        shutil.copyfile(original, initial/"poster.pptx")
        with Image.open(run/"08_finalize_output/poster_final.jpg") as image:
            image.save(initial/"preview.png")
        write(session/"state.json", state)
        if dest.exists():
            raise ValueError("Use a new tree session directory")
        session.rename(dest)
    return state


def render_preview(pptx, soffice, dest=None):
    """Measure how much of each textbox's text is visible inside its actual PDF bounds."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        subprocess.run([str(soffice), "-env:UserInstallation="+(folder/"office").as_uri(),
                        "--headless", "--convert-to", "pdf", "--outdir", str(folder), str(pptx.resolve())],
                       check=True, capture_output=True, timeout=120)
        with pdfium.PdfDocument(str(folder/(pptx.stem+".pdf"))) as pdf:
            if len(pdf) != 1:
                raise ValueError("Expected one rendered page")
            page = pdf[0]
            if dest:
                page.render(scale=1800/max(page.get_size())).to_pil().save(dest/"preview.png")
            deck = Presentation(pptx)
            width, height = page.get_size()
            sx, sy = width/deck.slide_width, height/deck.slide_height
            textpage = page.get_textpage()
            coverage = {}
            for shape in deck.slides[0].shapes:
                if not shape.has_text_frame or not shape.text.strip():
                    continue
                x,y,w,h = box(shape)
                visible = textpage.get_text_bounded(x*sx, height-(y+h)*sy, (x+w)*sx, height-y*sy)
                expected = "".join(shape.text.split())
                matches = SequenceMatcher(None, expected, "".join(visible.split()), autojunk=False)
                coverage[str(shape.shape_id)] = sum(block.size for block in matches.get_matching_blocks())
            return coverage


def resize(session, node_id, ratio, soffice, expected_version=None, request=None):
    if not math.isfinite(ratio) or not 0 < ratio < 1:
        raise ValueError("Split share must be strictly between 0 and 1")
    lock = session/".editing"
    _acquire_lock(lock)
    try:
        state = read(session/"state.json")
        if expected_version and state["artifact"] != expected_version:
            raise ValueError("The layout changed. Reload the current version.")
        tree = state["tree"]
        match = next((n for n in branches(tree) if n["id"] == node_id), None)
        if match is None:
            raise ValueError("Unknown split node")
        old_ratio = match["ratio"]
        match["ratio"] = ratio
        geometry = {}
        panel_boxes(tree, tree["box"], geometry)
        # At PDF print resolution, a sub-point panel no longer has a usable content area.
        for pid,rect in geometry.items():
            previous = state["panels"][pid]["box"]
            if any(rect[i] < min(12700, previous[i])-.5 for i in (2,3)):
                raise ValueError("Edit would collapse panel "+state["panels"][pid]["name"])
        original = session/"original/poster.pptx"
        if digest(original) != state["original_sha256"]:
            raise ValueError("Original PowerPoint changed")
        deck = Presentation(original)
        for shape in deck.slides[0].shapes:
            pid = state["owners"][str(shape.shape_id)]
            if pid not in geometry:
                continue
            source = state["panels"][pid]["box"]
            new = project(box(shape), source, geometry[pid])
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                k = min(new[2]/shape.width, new[3]/shape.height)
                w, h = shape.width*k, shape.height*k
                new = [new[0]+(new[2]-w)/2, new[1]+(new[3]-h)/2, w, h]
            x,y,w,h = map(round,new)
            if min(w,h)<=0 or min(x,y)<-2 or x+w>deck.slide_width+2 or y+h>deck.slide_height+2:
                raise ValueError("Edit would place an element outside the canvas")
            if shape.has_text_frame and shape.text.strip() and (w < shape.width or h < shape.height):
                sizes = [r.font.size for p in shape.text_frame.paragraphs for r in p.runs if r.font.size]
                if sizes and min(w,h)<max(sizes):
                    raise ValueError("Edit leaves less than one character or line of space for "+shape.name)
            shape.left,shape.top,shape.width,shape.height = x,y,w,h
        version = "edit-"+uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory(dir=session) as tmp:
            candidate = Path(tmp)/"candidate"
            candidate.mkdir()
            deck.save(candidate/"poster.pptx")
            coverage = render_preview(candidate/"poster.pptx", soffice, candidate)
            baseline = state.get("original_coverage")
            if baseline is None:
                baseline = render_preview(original, soffice)
                state["original_coverage"] = baseline
            lost = [key for key,count in baseline.items() if coverage[key] < count]
            if lost:
                raise ValueError("Rendered text loses coverage inside textboxes: "+", ".join(lost))
            write(candidate/"review.json", dict(source_coverage=baseline, candidate_coverage=coverage))
            candidate.rename(session/version)
        state["history"].append(dict(node=node_id, before=old_ratio, after=ratio, artifact=version, request=request))
        state["artifact"] = version
        write(session/"state.json",state)
        return state
    finally:
        lock.unlink()
