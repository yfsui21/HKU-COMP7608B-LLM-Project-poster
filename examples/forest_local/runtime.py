"""GPT inference, PDF input and slide rasterization for the pinned upstream pipeline."""
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from pathlib import Path
from types import SimpleNamespace

import pdfplumber
import pypdfium2 as pdfium
from json_repair import repair_json
from PIL import Image

from paper_canvas.extract import figure_options
from paper_canvas.source import page_png, reading_order


def parse_pdf_input(args, directory):
    """Collect page-addressable text and captioned crops from the actual input PDF."""
    pdf = Path(args.paper_path)
    dest = Path(directory)
    pages = []
    with pdfplumber.open(pdf) as doc:
        for index, page in enumerate(doc.pages, 1):
            text = reading_order(page)
            boundary = re.search(r"(?im)^\s*(?:\d+[.]?\s+)?(?:References|Bibliography)\s*$", text)
            pages.append({"page": index, "text": text[:boundary.start()] if boundary else text})
            if boundary:
                break
    if not any(p["text"].strip() for p in pages):
        raise ValueError("This adapter requires a text-based PDF.")
    figures, tables = {}, {}
    for entry in figure_options(pdf):
        match = re.match(r"^(fig(?:ure)?\.?|table)\s*(\d+)\s*[.:]\s*", entry["caption"], re.I)
        if not match or entry["page"] > pages[-1]["page"]:
            continue
        kind = "table" if match[1].lower() == "table" else "figure"
        number = match[2]
        index = tables if kind == "table" else figures
        if number in index:
            continue
        crop = dest / f"{kind}-{number}.png"
        data = page_png(pdf, entry["page"], entry["box"])
        crop.write_bytes(data)
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
        index[number] = dict(**entry, figure_path=crop.resolve().as_posix(), width=w, height=h,
                             figure_size=w*h, figure_aspect=w/h, original_index=int(number),
                             caption_number=number, is_compound=False, subfigure_count=1)
        if kind == "table":
            index[number]["table_path"] = index[number]["figure_path"]
    for key, value in [("pages", pages), ("images", figures), ("tables", tables)]:
        (dest / f"{args.poster_name}_{key}.json").write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    # The upstream citation matcher looks for Markdown section boundaries.
    heading = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z][^.!?]{1,95}|Abstract|Conclusions?)$")
    markdown = "\n\n".join("\n".join("## " + line if heading.fullmatch(line.strip()) else line
                                     for line in page["text"].splitlines()) for page in pages)
    return markdown, figures, tables


def fit_document(text, budget=55000):
    """Allocate a character budget across source sections, keeping their starts and ends."""
    if len(text) <= budget:
        return text
    sections = re.split(r"\n(?=## )", text)
    quota = max(1, (budget - len(sections) * 8) // len(sections))
    def excerpt(section):
        if len(section) <= quota:
            return section
        lead = int(quota * .7)
        return section[:lead].rsplit(" ", 1)[0] + "\n[…]\n" + section[-(quota-lead):].split(" ", 1)[-1]
    return "\n".join(map(excerpt, sections))


def restore_title_section(sections, body):
    """Recover the mandatory title row from model metadata verified against page one."""
    meta = sections.get("meta", {})
    title = meta.get("poster_title", "").strip()
    match = re.search(re.escape(title), body[:3000], re.I) if title else None
    if not match:
        return False
    meta["poster_title"] = match[0]
    sections["sections"].insert(0, {"title": "Poster Title & Author",
                                    "content": " · ".join(str(meta.get(k, "")) for k in
                                                          ("poster_title", "authors", "affiliations"))})
    return True


def get_agent_config(name):
    return {"model_type": os.environ["FOREST_MODEL"], "token_limit": 24576}


def make_model(config):
    return config["model_type"]


def agent_step_with_timeout(agent, msg):
    return agent.step(msg)


class BaseMessage:
    @staticmethod
    def make_user_message(role_name, content, image_list=()):
        return SimpleNamespace(content=content, image_list=image_list)


class ChatAgent:
    """Small subset of the upstream agent interface; GPT supplies the textual and visual decisions."""
    def __init__(self, system_message, model, **kwargs):
        self.system = system_message
        self.model = model
        self.reset()

    def reset(self):
        self.history = []

    def step(self, message):
        text = message if isinstance(message, str) else message.content
        parts = [{"type": "text", "text": text}]
        for source in getattr(message, "image_list", ()):
            image = source.convert("RGB")
            image.thumbnail((1344, 1344))
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=90)
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + data}})
        user = {"role": "user", "content": parts if len(parts) > 1 else text}
        messages = [{"role": "system", "content": self.system}, *self.history, user]
        payload = dict(model=self.model, messages=messages)
        body = json.dumps(payload, ensure_ascii=False).encode()
        key = hashlib.sha256(body).hexdigest()
        cache = Path("local-calls")
        cache.mkdir(exist_ok=True)
        path = cache / (key + ".json")
        # Exact request caching allows a failed platform integration to resume without
        # re-inferring prior successful calls. A fresh output directory has no cache.
        reused = path.exists()
        if reused:
            result = json.loads(path.read_text(encoding="utf-8"))
        else:
            from gpt_backend import complete
            result = complete(payload, key)
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        answer = result["choices"][0]["message"]["content"]
        self.history.extend([user, {"role": "assistant", "content": answer}])
        record = dict(request=key, model=self.model, images=len(parts)-1, cached=reused,
                      usage=result["usage"], time=time.time())
        with Path("inference.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        return SimpleNamespace(msgs=[SimpleNamespace(content=answer)], info={"usage": result["usage"]})


def get_json_from_response(text):
    fenced = re.search(r"\x60\x60\x60(?:json)?\s*(.*?)\x60\x60\x60", text, re.S | re.I)
    obj = fenced[1] if fenced else text
    try:
        return json.loads(obj)
    except json.JSONDecodeError:
        return repair_json(obj, return_objects=True)


def ppt_to_images_with_title(file, output_dir, warning=False, dpi=72, output_type="png", title="", stem=None):
    deck = Path(file).resolve()
    exe = os.environ.get("FOREST_SOFFICE") or shutil.which("soffice")
    if not exe:
        raise RuntimeError("Set FOREST_SOFFICE to the LibreOffice executable.")
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
        profile = (Path(temporary) / "office-profile").as_uri()
        subprocess.run([exe, "-env:UserInstallation=" + profile, "--headless", "--convert-to",
                        "pdf", "--outdir", temporary, str(deck)], capture_output=True, check=True, timeout=120)
        with pdfium.PdfDocument(str(Path(temporary) / (deck.stem + ".pdf"))) as doc:
            if len(doc) != 1:
                raise ValueError("Expected a single-slide poster")
            image = doc[0].render(scale=dpi/72).to_pil().convert("RGB")
            name = stem or ("poster_" + title if title else "poster")
            image.save(Path(output_dir) / (name + "." + output_type))


def ppt_to_images(file, output_dir, **kwargs):
    return ppt_to_images_with_title(file, output_dir, stem="poster", **kwargs)
