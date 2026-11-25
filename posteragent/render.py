"""Rendering: PIL preview, annotated overlays, PPTX export.

The renderer owns three things the agents depend on (see
docs/posterforest-notes.md section 4):

1. text measurement -> `current_volume_ratio`, the signal the content agent is
   blind without;
2. the annotated crop the layout agent reasons over (red = split region,
   green = its two panels, blue = figures);
3. the PPTX artifact.

LibreOffice is not available here, so the preview is drawn directly with PIL
rather than round-tripping pptx -> pdf -> png. That also means text metrics
used for measurement are exactly the metrics used for drawing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config
from .tree import (
    Box,
    Bullet,
    ContentLeaf,
    Node,
    SemanticNode,
    SplitNode,
    header_box,
    leaves,
    walk,
)

_PAD_IN = 0.18  # gap between panels
_INNER_IN = 0.26  # panel padding
_LINE_SP = 1.30
_PARA_SP = 0.42
_BULLET_INDENT_IN = 0.34

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(path: str, px: int) -> ImageFont.FreeTypeFont:
    px = max(6, int(px))
    key = (path, px)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(path, px)
    return _font_cache[key]


def _pt_px(pt: float, size: config.PosterSize) -> int:
    """Points at the poster's own scale -> preview pixels."""
    return max(6, int(round(pt * size.scale * config.PREVIEW_PX_PER_IN / 72.0)))


def _text_w(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> float:
    return d.textlength(s, font=f)


def _wrap(
    d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: float
) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if _text_w(d, trial, f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------
_scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))


def _layout_bullets(
    bullets: list[Bullet],
    avail_w: float,
    size: config.PosterSize,
    scale: float = 1.0,
) -> tuple[float, list[tuple[str, int, ImageFont.FreeTypeFont]]]:
    """Wrap bullets and return (total height in px, drawable lines)."""
    body_px = _pt_px(config.PT_BODY * scale, size)
    f = _font(config.FONT_REGULAR, body_px)
    indent_px = _BULLET_INDENT_IN * config.PREVIEW_PX_PER_IN * size.scale
    out: list[tuple[str, int, ImageFont.FreeTypeFont]] = []
    h = 0.0
    for i, b in enumerate(bullets):
        txt = b.text.strip()
        if not txt:
            continue
        lvl = max(0, min(2, b.level))
        marker = ("• ", "– ", "· ")[lvl] if b.bullet else ""
        left = indent_px * lvl
        wrapped = _wrap(_scratch, marker + txt, f, max(10.0, avail_w - left))
        if i:
            h += body_px * _PARA_SP
        for j, ln in enumerate(wrapped):
            out.append((ln if j == 0 else "  " + ln, lvl, f))
            h += body_px * _LINE_SP
    return h, out


def text_area(leaf: ContentLeaf, size: config.PosterSize) -> tuple[float, float]:
    """Preview pixels available to a text panel's lines -- the exact area
    _draw_text_leaf and render_pptx use, so a measured fit is a real fit."""
    x0, y0, x1, y1 = leaf.box.px(size)
    k = config.PREVIEW_PX_PER_IN * size.scale
    m = 2 * (_PAD_IN + _INNER_IN) * k
    return (x1 - x0) - m, (y1 - y0) - m


def measure(root: Node, size: config.PosterSize) -> None:
    """Fill `measured` on every content leaf.

    `volume_ratio` is measured at the NOMINAL font size, never at the
    auto-shrunk one: if shrinking fed back into the ratio it would always read
    ~100% and the content agent would never be asked to condense.
    """
    for leaf in leaves(root):
        avail_w, avail_h = text_area(leaf, size)
        if avail_w <= 4 or avail_h <= 4:
            leaf.measured.volume_ratio = 999.0
            leaf.measured.overflow = True
            leaf.measured.font_scale = config.MIN_FONT_SCALE
            continue

        if leaf.content_type != "text":
            # Figures always fill their box (fit-contain), so they are never
            # under- or over-full in the content agent's sense.
            leaf.measured.volume_ratio = 100.0
            leaf.measured.overflow = False
            leaf.measured.font_scale = 1.0
            continue

        used, _ = _layout_bullets(leaf.bullets, avail_w, size, 1.0)
        leaf.measured.volume_ratio = round(100.0 * used / avail_h, 1)

        scale = 1.0
        while scale > config.MIN_FONT_SCALE:
            used_s, _ = _layout_bullets(leaf.bullets, avail_w, size, scale)
            if used_s <= avail_h:
                break
            # Never below the frozen minimum, whatever step it lands on.
            scale = max(config.MIN_FONT_SCALE, round(scale - 0.04, 2))
        used_f, _ = _layout_bullets(leaf.bullets, avail_w, size, scale)
        leaf.measured.font_scale = scale
        leaf.measured.overflow = used_f > avail_h


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------
def _rounded(d: ImageDraw.ImageDraw, box, fill, outline=None, r=10) -> None:
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=2)


def title_scale(leaf: ContentLeaf, size: config.PosterSize) -> tuple[float, float]:
    """(font scale, total height px) for the title band; shared with PPTX."""
    x0, y0, x1, y1 = leaf.box.px(size)
    pad = int(_PAD_IN * config.PREVIEW_PX_PER_IN * size.scale)
    inner = x1 - x0 - 4 * pad
    for scale in (1.0, 0.86, 0.74, 0.64, 0.55):
        f = _font(config.FONT_BOLD, _pt_px(config.PT_TITLE * scale, size))
        lines = _wrap(_scratch, leaf.title, f, inner)
        fa = _font(config.FONT_REGULAR, _pt_px(config.PT_AUTHORS * scale, size))
        alines = _wrap(_scratch, leaf.subtitle, fa, inner)[:2] if leaf.subtitle else []
        th = len(lines) * f.size * 1.18 + (len(alines) * fa.size * 1.22 if alines else 0)
        if th <= (y1 - y0) - 4 * pad or scale == 0.55:
            return scale, th
    raise AssertionError("unreachable")


def header_scale(node: SemanticNode, size: config.PosterSize) -> float:
    hb = header_box(node, size)
    x0, _, x1, _ = hb.px(size)
    pad = int(_PAD_IN * config.PREVIEW_PX_PER_IN * size.scale)
    avail = (x1 - x0) - 4 * pad
    for scale in (1.0, 0.86, 0.72, 0.6):
        f = _font(config.FONT_BOLD, _pt_px(config.PT_SECTION * scale, size))
        if _text_w(_scratch, node.title, f) <= avail:
            return scale
    return 0.6


def _draw_title(d: ImageDraw.ImageDraw, leaf: ContentLeaf, size: config.PosterSize) -> None:
    x0, y0, x1, y1 = leaf.box.px(size)
    pad = int(_PAD_IN * config.PREVIEW_PX_PER_IN * size.scale)
    _rounded(d, (x0 + pad, y0 + pad, x1 - pad, y1 - pad), config.COL_TITLE_BG, r=14)

    inner = x1 - x0 - 2 * pad - 2 * pad
    scale, th = title_scale(leaf, size)
    f = _font(config.FONT_BOLD, _pt_px(config.PT_TITLE * scale, size))
    lines = _wrap(d, leaf.title, f, inner)
    fa = _font(config.FONT_REGULAR, _pt_px(config.PT_AUTHORS * scale, size))
    alines = _wrap(d, leaf.subtitle, fa, inner)[:2] if leaf.subtitle else []

    cy = y0 + ((y1 - y0) - th) / 2
    for ln in lines:
        w = _text_w(d, ln, f)
        d.text(((x0 + x1 - w) / 2, cy), ln, font=f, fill=config.COL_TITLE_FG)
        cy += f.size * 1.18
    for ln in alines:
        w = _text_w(d, ln, fa)
        d.text(((x0 + x1 - w) / 2, cy), ln, font=fa, fill=(214, 225, 238))
        cy += fa.size * 1.22


def _fit_caption(
    d: ImageDraw.ImageDraw, text: str, size: config.PosterSize, max_w: float, max_h: float
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """Wrap a caption into at most max_h, shrinking then eliding.

    Mid-sentence clipping was the defect the feedback agent flagged on every
    round, so prefer a smaller font, then an explicit word-boundary ellipsis.
    """
    for scale in (1.0, 0.9, 0.8, 0.7):
        f = _font(config.FONT_REGULAR, _pt_px(config.PT_CAPTION * scale, size))
        lh = f.size * 1.25
        n_max = max(1, int(max_h // lh))
        lines = _wrap(d, text, f, max_w)
        if len(lines) <= n_max:
            return lines, f
        if scale == 0.7:
            kept = lines[:n_max]
            # Drop the trailing partial word, then mark the elision.
            last = kept[-1].rstrip()
            while last and _text_w(d, last + " …", f) > max_w:
                last = last[: last.rfind(" ")] if " " in last else last[:-1]
            kept[-1] = last + " …"
            return kept, f
    raise AssertionError("unreachable")


@dataclass
class AssetLayout:
    """Where an asset and its caption go, in preview pixels."""

    panel: tuple[int, int, int, int]
    pic: tuple[float, float, float, float]  # x, y, w, h; zero size if no room
    caption_lines: list[str]
    caption_px: int
    caption_y: float
    truncated: bool

    def pic_inches(self) -> tuple[float, float, float, float]:
        return tuple(v / config.PREVIEW_PX_PER_IN for v in self.pic)


def asset_layout(leaf: ContentLeaf, size: config.PosterSize) -> AssetLayout:
    x0, y0, x1, y1 = leaf.box.px(size)
    k = config.PREVIEW_PX_PER_IN * size.scale
    pad, inner = int(_PAD_IN * k), int(_INNER_IN * k * 0.5)
    bx = (x0 + pad, y0 + pad, x1 - pad, y1 - pad)
    avail_w = bx[2] - bx[0] - 2 * inner
    avail_h = bx[3] - bx[1] - 2 * inner
    lines: list[str] = []
    cf = _font(config.FONT_REGULAR, _pt_px(config.PT_CAPTION, size))
    if leaf.caption and avail_w > 10:
        # A caption may not take more than a third of the panel.
        lines, cf = _fit_caption(_scratch, leaf.caption, size, avail_w, max(1.0, avail_h / 3))
    cap_h = len(lines) * cf.size * 1.25
    pic = (float(bx[0] + inner), float(bx[1] + inner), 0.0, 0.0)
    ah = avail_h - cap_h
    if avail_w > 4 and ah > 4 and leaf.asset_path and Path(leaf.asset_path).exists():
        with Image.open(leaf.asset_path) as im:
            w0, h0 = im.size
        r = min(avail_w / w0, ah / h0)
        nw, nh = max(1.0, w0 * r), max(1.0, h0 * r)
        pic = (bx[0] + inner + (avail_w - nw) / 2, bx[1] + inner + (ah - nh) / 2, nw, nh)
    truncated = bool(lines) and lines[-1].endswith(" …")
    return AssetLayout(bx, pic, lines, cf.size, bx[3] - inner - cap_h, truncated)


def asset_width_in(leaf: ContentLeaf, size: config.PosterSize) -> float:
    """Displayed width of a figure/table, in inches."""
    return asset_layout(leaf, size).pic_inches()[2]


def _draw_asset(d: ImageDraw.ImageDraw, img: Image.Image, leaf: ContentLeaf,
                size: config.PosterSize) -> None:
    lay = asset_layout(leaf, size)
    bx = lay.panel
    _rounded(d, bx, config.COL_PANEL_BG, config.COL_BORDER)
    x, y, w, h = lay.pic
    if w >= 1 and h >= 1:
        thumb = img.resize((max(1, int(w)), max(1, int(h))), Image.LANCZOS)
        d._image.paste(thumb, (int(x), int(y)))
    cf = _font(config.FONT_REGULAR, lay.caption_px)
    inner = int(_INNER_IN * config.PREVIEW_PX_PER_IN * size.scale * 0.5)
    cy = lay.caption_y
    for ln in lay.caption_lines:
        d.text((bx[0] + inner, cy), ln, font=cf, fill=config.COL_CAPTION)
        cy += cf.size * 1.25


def _draw_text_leaf(d: ImageDraw.ImageDraw, leaf: ContentLeaf, size: config.PosterSize) -> None:
    x0, y0, x1, y1 = leaf.box.px(size)
    pad = int(_PAD_IN * config.PREVIEW_PX_PER_IN * size.scale)
    inner = int(_INNER_IN * config.PREVIEW_PX_PER_IN * size.scale)
    _rounded(d, (x0 + pad, y0 + pad, x1 - pad, y1 - pad),
             config.COL_PANEL_BG, config.COL_BORDER)

    avail_w = (x1 - x0) - 2 * pad - 2 * inner
    if avail_w <= 4:
        return
    _, lines = _layout_bullets(leaf.bullets, avail_w, size, leaf.measured.font_scale)
    cy = y0 + pad + inner
    limit = y1 - pad - inner
    indent_px = _BULLET_INDENT_IN * config.PREVIEW_PX_PER_IN * size.scale
    for txt, lvl, f in lines:
        if cy + f.size > limit:
            d.text((x0 + pad + inner, min(cy, limit - f.size)), "…",
                   font=f, fill=config.COL_OVERFLOW)
            break
        d.text((x0 + pad + inner + indent_px * lvl, cy), txt, font=f, fill=config.COL_TEXT)
        cy += f.size * _LINE_SP


def _draw_header(d: ImageDraw.ImageDraw, node: SemanticNode, size: config.PosterSize) -> None:
    hb = header_box(node, size)
    x0, y0, x1, y1 = hb.px(size)
    pad = int(_PAD_IN * config.PREVIEW_PX_PER_IN * size.scale)
    bx = (x0 + pad, y0 + pad, x1 - pad, y1 - int(pad * 0.3))
    _rounded(d, bx, config.COL_HEADER_BG, r=8)
    f = _font(config.FONT_BOLD, _pt_px(config.PT_SECTION * header_scale(node, size), size))
    ty = bx[1] + ((bx[3] - bx[1]) - f.size * 1.1) / 2
    d.text((bx[0] + pad, ty), node.title, font=f, fill=config.COL_HEADER_FG)


def render_png(root: Node, size: config.PosterSize) -> Image.Image:
    """Full poster preview. Call measure() first."""
    img = Image.new("RGB", (size.px_w, size.px_h), config.COL_BG)
    d = ImageDraw.Draw(img)
    d._image = img  # _draw_asset pastes through this

    for n in walk(root):
        if isinstance(n, SemanticNode):
            _draw_header(d, n, size)
        elif isinstance(n, ContentLeaf):
            if n.content_type == "title":
                _draw_title(d, n, size)
            elif n.asset_path and Path(n.asset_path).exists():
                try:
                    with Image.open(n.asset_path) as a:
                        _draw_asset(d, a.convert("RGB"), n, size)
                except OSError:
                    _draw_text_leaf(d, n, size)
            else:
                _draw_text_leaf(d, n, size)
    return img


def render_overlay(root: Node, node: SplitNode, size: config.PosterSize,
                   base: Image.Image | None = None) -> Image.Image:
    """The annotated crop the layout agent is shown.

    red = this split's region, green = its two child panels,
    blue = figures inside. Vocabulary from prompts/layout_agent.tex:25-26.
    """
    img = (base or render_png(root, size)).copy()
    d = ImageDraw.Draw(img)
    lw = max(3, int(size.px_w / 420))

    for leaf in leaves(node):
        if leaf.content_type in ("figure", "table"):
            d.rectangle(leaf.box.px(size), outline=(40, 90, 230), width=lw)
    for ch in node.children:
        d.rectangle(ch.box.px(size), outline=(30, 150, 60), width=lw + 1)
    d.rectangle(node.box.px(size), outline=(220, 30, 30), width=lw + 3)

    x0, y0, x1, y1 = node.box.px(size)
    m = int(size.px_w * 0.035)
    return img.crop(
        (max(0, x0 - m), max(0, y0 - m), min(size.px_w, x1 + m), min(size.px_h, y1 + m))
    )


# --------------------------------------------------------------------------
# PPTX export
# --------------------------------------------------------------------------
def render_pptx(root: Node, size: config.PosterSize, out_path: str | Path) -> Path:
    """Editable artifact, from the already-resolved boxes."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width = Inches(size.w_in)
    prs.slide_height = Inches(size.h_in)
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank

    def rgb(t):
        return RGBColor(*t)

    def add_box(b: Box, fill=None, line=None):
        from pptx.enum.shapes import MSO_SHAPE

        x, y, w, h = b.inches(size)
        shp = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h)
        )
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(fill)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = rgb(line)
            shp.line.width = Pt(1.5)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        return shp

    def inset(b: Box, pad_in: float) -> Box:
        x, y, w, h = b.inches(size)
        p = pad_in
        return Box(
            (x + p) / size.w_in,
            (y + p) / size.h_in,
            max(0.1, w - 2 * p) / size.w_in,
            max(0.1, h - 2 * p) / size.h_in,
        )

    def plain(tf, name="Arial"):
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0

    def fnt(p, pt, color, bold=False):
        p.font.size = Pt(max(1, round(pt, 1)))
        p.font.bold = bold
        # Arial is narrower than the DejaVu metrics used for measuring, so a
        # panel that measured as fitting also fits in PowerPoint.
        p.font.name = "Arial"
        p.font.color.rgb = rgb(color)

    ppi = config.PREVIEW_PX_PER_IN
    pad = _PAD_IN * size.scale
    for n in walk(root):
        if isinstance(n, SemanticNode):
            hb = inset(header_box(n, size), pad)
            shp = add_box(hb, fill=config.COL_HEADER_BG)
            shp.name = f"header-{n.id}"
            tf = shp.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.text = n.title
            fnt(p, config.PT_SECTION * size.scale * header_scale(n, size),
                config.COL_HEADER_FG, bold=True)
            continue
        if not isinstance(n, ContentLeaf):
            continue

        if n.content_type == "title":
            shp = add_box(inset(n.box, pad), fill=config.COL_TITLE_BG)
            shp.name = n.id
            scale, _ = title_scale(n, size)
            tf = shp.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.text = n.title
            p.alignment = PP_ALIGN.CENTER
            fnt(p, config.PT_TITLE * size.scale * scale, config.COL_TITLE_FG, bold=True)
            if n.subtitle:
                p2 = tf.add_paragraph()
                p2.text = n.subtitle
                p2.alignment = PP_ALIGN.CENTER
                fnt(p2, config.PT_AUTHORS * size.scale * scale, (214, 225, 238))
            continue

        panel = add_box(inset(n.box, pad), fill=config.COL_PANEL_BG, line=config.COL_BORDER)
        panel.name = f"panel-{n.id}"

        if n.content_type in ("figure", "table"):
            lay = asset_layout(n, size)
            x, y, w, h = lay.pic_inches()
            if w > 0.02 and h > 0.02:
                pic = slide.shapes.add_picture(str(n.asset_path), Inches(x), Inches(y),
                                               Inches(w), Inches(h))
                pic.name = n.id
            if lay.caption_lines:
                inner = _INNER_IN * size.scale * 0.5
                cap_pt = lay.caption_px * 72.0 / ppi
                cap_h = len(lay.caption_lines) * cap_pt * 1.25 / 72.0
                bx = [v / ppi for v in lay.panel]
                tb = slide.shapes.add_textbox(
                    Inches(bx[0] + inner), Inches(lay.caption_y / ppi),
                    Inches(max(0.1, bx[2] - bx[0] - 2 * inner)), Inches(max(0.1, cap_h)))
                tb.name = f"caption-{n.id}"
                plain(tb.text_frame)
                p = tb.text_frame.paragraphs[0]
                p.text = " ".join(lay.caption_lines)
                fnt(p, cap_pt, config.COL_CAPTION)
            continue

        x, y, w, h = inset(n.box, pad + _INNER_IN * size.scale).inches(size)
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tb.name = n.id
        tf = tb.text_frame
        plain(tf)
        fs = config.PT_BODY * size.scale * n.measured.font_scale
        for i, b in enumerate(n.bullets):
            lvl = max(0, min(2, b.level))
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = (("• ", "– ", "· ")[lvl] if b.bullet else "") + b.text
            p.level = lvl
            fnt(p, fs, config.COL_TEXT)
            if i:
                p.space_before = Pt(round(fs * _PARA_SP, 1))

    out = Path(out_path)
    prs.save(str(out))
    return out


# --------------------------------------------------------------------------
# geometric checks
# --------------------------------------------------------------------------
def geometry_issues(root: Node, size: config.PosterSize) -> list[dict]:
    """Overflow, overlap, out-of-canvas and truncated captions.

    Overlap and out-of-canvas cannot occur by construction (tree.py); they are
    still checked so a geometry bug can never pass silently.
    """
    out: list[dict] = []
    eps = 1e-6
    ls = leaves(root)
    for l in ls:
        b = l.box
        if b.x < -eps or b.y < -eps or b.x + b.w > 1 + eps or b.y + b.h > 1 + eps or b.w <= 0 or b.h <= 0:
            out.append({"kind": "out_of_canvas", "element": l.id, "detail": "panel leaves the canvas"})
        if l.content_type == "text" and l.measured.overflow:
            out.append({"kind": "overflow", "element": l.id,
                        "detail": f"text needs {l.measured.volume_ratio:.0f}% of its panel and still "
                                  f"does not fit at the minimum font size"})
        if l.content_type in ("figure", "table"):
            lay = asset_layout(l, size)
            if lay.truncated:
                out.append({"kind": "caption_truncated", "element": l.id,
                            "detail": "caption is cut off"})
            if lay.pic[2] < 1 or lay.pic[3] < 1:
                out.append({"kind": "missing_asset", "element": l.id,
                            "detail": f"{l.content_type} {l.asset_id} has no room or no image"})
    for i in range(len(ls)):
        for j in range(i + 1, len(ls)):
            a, c = ls[i].box, ls[j].box
            ox = min(a.x + a.w, c.x + c.w) - max(a.x, c.x)
            oy = min(a.y + a.h, c.y + c.h) - max(a.y, c.y)
            if ox > 1e-5 and oy > 1e-5:
                out.append({"kind": "overlap", "element": ls[i].id, "detail": f"overlaps {ls[j].id}"})
    return out


def check_pptx(path: str | Path, root: Node) -> list[str]:
    """Reopen the exported deck and compare it with the tree: the artifact the
    user receives, not the in-memory state, is what must be correct."""
    from pptx import Presentation

    problems: list[str] = []
    prs = Presentation(str(path))
    if len(prs.slides) != 1:
        return [f"expected 1 slide, found {len(prs.slides)}"]
    shapes = {s.name: s for s in prs.slides[0].shapes}
    for l in leaves(root):
        if l.content_type == "text":
            shp = shapes.get(l.id)
            got = "\n".join(p.text for p in shp.text_frame.paragraphs) if shp is not None else ""
            want = [b.text for b in l.bullets if b.text.strip()]
            if any(w not in got for w in want):
                problems.append(f"{l.id}: text box does not match the poster tree")
        elif l.content_type in ("figure", "table") and l.id not in shapes:
            problems.append(f"{l.id}: {l.content_type} {l.asset_id} is not in the deck")
    return problems
