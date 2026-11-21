"""Replay PosterForest's published ACL example with its pinned PowerPoint generator."""
import argparse
import hashlib
import json
import os
import runpy
import shutil
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

from pptx import Presentation

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(Path(__file__).with_name("posterforest-inputs.json").read_text())
CACHE = ROOT / "runs" / "upstream-cache" / MANIFEST["revision"]
OUTPUT = ROOT / "runs" / "posterforest-replay"
RUNS = {"landscape": "180322", "portrait": "181802"}


def verified(data, digest):
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest() == digest


def ensure_input(entry, fetch):
    name, digest = entry
    path = CACHE / name
    if path.is_file() and verified(path.read_bytes(), digest):
        return
    if not fetch:
        raise ValueError(f"Missing or invalid input {name}. Run with --fetch.")
    url = f"https://raw.githubusercontent.com/{MANIFEST['repository']}/{MANIFEST['revision']}/{name}"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read()
    if not verified(data, digest):
        raise ValueError(f"Git blob mismatch for {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        stream.write(data)
    os.replace(stream.name, path)


def prepare_inputs(fetch):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda entry: ensure_input(entry, fetch), MANIFEST["blobs"].items()))


def replay(orientation, run):
    relative = f"outputs/20260612_{run}_vllm_qwen3_vllm_qwen3_vl_2026_ACL"
    base = CACHE / relative
    layout = json.loads((base / "06_step_06_modification_planning/2026_ACL_final_arrangements.json").read_text(encoding="utf-8"))
    panels, texts, figures = (layout[key + "_arrangement"] for key in ("panel", "text", "figure"))
    for item in figures:
        name = item["figure_path"]
        if name not in MANIFEST["blobs"] or not name.startswith(relative + "/01_parse_raw_poster/"):
            raise ValueError(f"Unexpected image path {name}")
        item["figure_path"] = (CACHE / name).as_posix()

    # Match upstream main.py's step 7 wrapper around the unchanged generator.
    for i, item in enumerate(texts):
        if i < 2 or ("title" in item["textbox_name"].lower() and int(item["panel_id"]) < 10):
            for bullet in item["content_for_ppt"]:
                bullet["runs"][0].update(color=(255, 255, 255), fill_color=(63, 86, 147))
    for item in [*panels, *texts, *figures]:
        for key in ("x", "y", "width", "height"):
            item[key] /= 25
    theme = dict(panel_visible=True, textbox_visible=False, figure_visible=False,
                 panel_theme=dict(color=(255, 255, 255), thickness=5, line_style="solid"),
                 textbox_theme=None, figure_theme=None)
    dest = OUTPUT / orientation
    dest.mkdir(parents=True, exist_ok=True)
    width, height = (48, 36) if orientation == "landscape" else (36, 48)
    generate = runpy.run_path(str(CACHE / "PosterForest/step_07_pptx_generation.py"))["generate_poster_code"]
    helpers = runpy.run_path(str(CACHE / "utils/pptx_utils.py"))["utils_functions"]
    with tempfile.TemporaryDirectory(dir=dest) as tmp:
        scratch = Path(tmp)
        poster = scratch / "poster.pptx"
        code = generate(panels, texts, figures, "poster_presentation", "poster_slide", helpers,
                        width, height, None, poster.as_posix(), content=texts, theme=theme,
                        tmp_dir=scratch.as_posix())
        exec(compile(code, "<pinned-posterforest-step-7>", "exec"), {})
        # The author's finished deck is opened only after creating our own.
        report = audit(poster, base / "08_finalize_output/poster_final.pptx")
        report.update(revision=MANIFEST["revision"], orientation=orientation,
                      source_run=relative, panels=len(panels), textboxes=len(texts), pictures=len(figures))
        (dest / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(orientation, json.dumps(report))
        os.replace(poster, dest / "poster.pptx")
        for name, source in (("before.jpg", "debug_renders/step6_attempt_00_start.jpg"),
                             ("reference.jpg", "poster_final.jpg")):
            shutil.copyfile(base / "08_finalize_output" / source, dest / name)
    return report


def audit(generated, reference):
    """Compare the slide, dimensions, media relationships, theme, and actual picture bytes."""
    a, b = (Presentation(path) for path in (generated, reference))
    with ZipFile(generated) as left, ZipFile(reference) as right:
        def components(archive):
            return {name: archive.read(name) for name in archive.namelist()
                    if name.startswith(("ppt/slides/", "ppt/slideLayouts/", "ppt/slideMasters/",
                                        "ppt/media/", "ppt/theme/"))}
        local, author = components(left), components(right)
        different = sorted(name for name in set(local) | set(author) if local.get(name) != author.get(name))
    dims = (a.slide_width, a.slide_height) == (b.slide_width, b.slide_height)
    return dict(matched=not different and dims and len(a.slides) == len(b.slides) == 1,
                different_parts=different, compared_parts=len(set(local) | set(author)),
                same_canvas=dims, canvas_inches=[a.slide_width.inches, a.slide_height.inches],
                shapes=len(a.slides[0].shapes))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="download and verify missing inputs")
    parser.add_argument("--fetch-only", action="store_true", help="cache inputs without executing the generator")
    args = parser.parse_args()
    prepare_inputs(args.fetch or args.fetch_only)
    if args.fetch_only:
        raise SystemExit(0)
    results = [replay(orientation, run) for orientation, run in RUNS.items()]
    raise SystemExit(0 if all(row["matched"] for row in results) else 1)
