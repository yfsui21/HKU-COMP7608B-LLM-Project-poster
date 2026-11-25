"""PDF -> markdown + figure/table inventory, using PyMuPDF.

PosterForest uses Docling; it is not installed here, so we extract with
PyMuPDF and recover figures by *caption anchoring*: locate "Figure N:" /
"Table N:" text blocks, then clip the page region adjacent to the caption and
rasterise it. This works for vector figures as well as embedded rasters,
which matters because arXiv PDFs are mostly vector.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

from .tree import clean

CAPTION_RE = re.compile(
    # The separator is mandatory: PyMuPDF often drops the space after it
    # ("Figure 1:Limitations"), and requiring it is what keeps in-text
    # references like "as shown in Figure 2 we ..." from being read as captions.
    r"^\s*(?P<kind>Figure|Fig\.?|Table|Algorithm)\s*(?P<num>\d+)\s*"
    r"(?P<sep>[:.—–)])\s*(?P<rest>\S.{19,})",
    re.IGNORECASE | re.S,
)

_CLIP_DPI = 170
_MIN_INK = 0.006  # below this the clip is blank
_MIN_CLIP_IN = 0.5  # ignore slivers
_MAX_CLIP_FRAC = 0.88  # a figure clip may not exceed this fraction of page height
_GAP_PT = 26.0  # vertical gap that still counts as the same float (subfigures)
_GAP_TABLE = 400.0  # tables: span the whole caption-to-caption band
_MAX_TEXT_GROW = 20.0  # pt a clip may grow on the side away from its caption
_CHAIN_GAP = 14.0  # pt between consecutive label blocks on the caption side
_LABEL_WORDS = 14  # longer blocks are prose, never absorbed as labels


@dataclass
class AssetInfo:
    asset_id: str  # "1", "2", ... unique within its kind
    kind: str  # "figure" | "table"
    caption_number: str
    caption: str
    path: str
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{'Figure' if self.kind == 'figure' else 'Table'} {self.caption_number}"

    @property
    def aspect(self) -> float:
        return self.width / max(1, self.height)


@dataclass
class ParsedPaper:
    title: str
    authors: str
    markdown: str
    figures_info: dict[str, AssetInfo] = field(default_factory=dict)
    tables_info: dict[str, AssetInfo] = field(default_factory=dict)
    n_pages: int = 0
    warnings: list[str] = field(default_factory=list)
    # Raw text of each page (index 0 = page 1). Claim quotes and the numbers
    # on the poster are verified against this, not against the markdown,
    # which has captions and headings rewritten.
    pages: list[str] = field(default_factory=list)

    def asset(self, kind: str, asset_id: str) -> AssetInfo | None:
        pool = self.figures_info if kind == "figure" else self.tables_info
        return pool.get(str(asset_id))

    def inventory_text(self) -> str:
        lines = []
        for a in self.figures_info.values():
            lines.append(f"- figure id={a.asset_id} ({a.label}, aspect {a.aspect:.2f}): {a.caption[:260]}")
        for a in self.tables_info.values():
            lines.append(f"- table id={a.asset_id} ({a.label}, aspect {a.aspect:.2f}): {a.caption[:260]}")
        return "\n".join(lines) if lines else "(none extracted)"


# --------------------------------------------------------------------------
# text structure
# --------------------------------------------------------------------------
@dataclass
class _Line:
    page: int
    blk: int  # index of the containing text block, so captions can be skipped whole
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    bold: bool

    @property
    def rotated(self) -> bool:
        """Vertical text — the arXiv stamp in the left margin, typically the
        largest glyphs on page 1 and never part of the title."""
        w, h = self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]
        return h > w * 1.5


def _lines(doc: fitz.Document) -> list[_Line]:
    out: list[_Line] = []
    for pno, page in enumerate(doc):
        d = page.get_text("dict")
        for bi, blk in enumerate(d.get("blocks", [])):
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                spans = ln.get("spans", [])
                if not any(s.get("text", "").strip() for s in spans):
                    continue
                # Join ALL spans, including whitespace-only ones: in several
                # fonts every inter-word space is its own span, so filtering
                # them out silently welds words together.
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue
                sized = [s for s in spans if s.get("text", "").strip()]
                size = max(float(s.get("size", 0)) for s in sized)
                bold = any("bold" in str(s.get("font", "")).lower() for s in sized)
                out.append(_Line(pno, bi, tuple(ln["bbox"]), text, round(size, 1), bold))
    return out


def _body_size(lines: list[_Line]) -> float:
    counter: Counter[float] = Counter()
    for ln in lines:
        counter[ln.size] += len(ln.text)
    return counter.most_common(1)[0][0] if counter else 10.0


_HEAD_NUM = re.compile(r"^\s*(\d+(\.\d+)*)\s*[.)]?\s+\S")
_KNOWN_HEADS = {
    "abstract", "introduction", "related work", "background", "method",
    "methods", "methodology", "approach", "experiments", "experimental setup",
    "results", "discussion", "conclusion", "conclusions", "limitations",
    "references", "acknowledgments", "acknowledgements", "appendix",
}


def _is_heading(ln: _Line, body: float) -> bool:
    t = ln.text.strip()
    if len(t) > 90 or len(t) < 3:
        return False
    if t.lower().rstrip(" .:").lstrip("0123456789. ") in _KNOWN_HEADS:
        return True
    big = ln.size >= body * 1.09
    if big and (ln.bold or _HEAD_NUM.match(t) or t.isupper() or t.istitle()):
        return True
    if ln.bold and _HEAD_NUM.match(t) and ln.size >= body * 0.99:
        return True
    return False


_ARXIV_STAMP = re.compile(r"^\s*arXiv:\s*\d", re.IGNORECASE)
# PyMuPDF drops the space after punctuation in some fonts; restore it only
# after punctuation, never inside a word (so "PosterForest" stays intact).
_TIGHT_PUNCT = re.compile(r"([:;,.!?])(?=[A-Z(])")
# "hierarchi- cal" -> "hierarchical". A real compound hyphen is never followed
# by whitespace, so requiring the space makes this safe.
_SOFT_HYPHEN = re.compile(r"([a-z])-\s+([a-z])")


def _detighten(s: str) -> str:
    return _TIGHT_PUNCT.sub(r"\1 ", s)


def _dehyphenate(s: str) -> str:
    return _SOFT_HYPHEN.sub(r"\1\2", s)


def _title_and_authors(lines: list[_Line], body: float) -> tuple[str, str]:
    first = [
        ln
        for ln in lines
        if ln.page == 0 and not ln.rotated and not _ARXIV_STAMP.match(ln.text)
    ]
    if not first:
        return "Untitled", ""
    top = sorted(first, key=lambda l: -l.size)[0].size
    title_parts, idx = [], -1
    for i, ln in enumerate(first):
        if abs(ln.size - top) < 0.6 and len(ln.text) > 3:
            title_parts.append(ln.text)
            idx = i
        elif title_parts:
            break
    title = _detighten(" ".join(title_parts).strip() or first[0].text)

    authors: list[str] = []
    for ln in first[idx + 1 : idx + 9]:
        t = ln.text.strip()
        if t.lower().startswith("abstract") or len(authors) >= 4:
            break
        if ln.size > body * 0.92 and len(t) > 2 and not CAPTION_RE.match(t):
            authors.append(_detighten(t))
    return title, " / ".join(authors[:3])


# --------------------------------------------------------------------------
# caption-anchored asset extraction
# --------------------------------------------------------------------------
def _page_columns(blocks: list[tuple], page: fitz.Page) -> list[tuple[float, float]] | None:
    """Two-column geometry for this page alone, or None if it cannot be split."""
    r = page.rect
    mid = r.x0 + r.width / 2
    left = [b for b in blocks if b[2] <= mid + 4]
    right = [b for b in blocks if b[0] >= mid - 4]
    if len(left) >= 4 and len(right) >= 4:
        return [
            (min(b[0] for b in left), max(b[2] for b in left)),
            (min(b[0] for b in right), max(b[2] for b in right)),
        ]
    return None


def _doc_columns(doc: fitz.Document) -> list[tuple[float, float]] | None:
    """Consensus column geometry for the whole document.

    Per-page detection degrades exactly where it matters: a page dominated by
    a full-width float has too few narrow text blocks to split, so the float's
    clip would span both columns and drag in the neighbouring column's text.
    Column geometry is a property of the paper, not of one page, so take the
    median over the pages that *can* be split.
    """
    found: list[list[tuple[float, float]]] = []
    for page in doc:
        blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
        if not blocks:
            continue
        c = _page_columns(blocks, page)
        if c:
            found.append(c)
    if len(found) < max(2, 0.25 * doc.page_count):
        return None
    return [
        (statistics.median(c[i][0] for c in found), statistics.median(c[i][1] for c in found))
        for i in (0, 1)
    ]


def _columns(
    blocks: list[tuple], page: fitz.Page, consensus: list[tuple[float, float]] | None
) -> list[tuple[float, float]]:
    own = _page_columns(blocks, page)
    if own:
        return own
    if consensus:
        return consensus
    if not blocks:
        r = page.rect
        return [(r.x0 + 36, r.x1 - 36)]
    return [(min(b[0] for b in blocks), max(b[2] for b in blocks))]


def _ink(img: Image.Image) -> float:
    a = np.asarray(img.convert("L"), dtype=np.uint8)
    if a.size == 0:
        return 0.0
    return float((a < 245).mean())


def _clip(page: fitz.Page, rect: fitz.Rect) -> Image.Image | None:
    if rect.width <= 2 or rect.height <= 2:
        return None
    zoom = _CLIP_DPI / 72.0
    pm = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    return Image.frombytes("RGB", (pm.width, pm.height), pm.samples)


def _graphic_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Bounding boxes of vector paths and embedded images.

    Text blocks are useless as float boundaries: a vector figure's own axis
    labels are reported as text, so "nearest text block above the caption"
    lands *inside* the figure. Drawings are the reliable anchor, and they also
    pick up booktabs rules, which makes tables work by the same path.
    """
    out: list[fitz.Rect] = []
    pr = page.rect
    try:
        for d in page.get_drawings():
            r = fitz.Rect(d["rect"])
            # Keep thin rects: booktabs rules are ~0.4pt tall and are the only
            # graphic a LaTeX table produces. Filtering on height loses tables.
            if max(r.width, r.height) > 4 and r.width <= pr.width and r.height <= pr.height:
                out.append(_nonempty(r))
    except Exception:  # noqa: BLE001 - malformed content streams
        pass
    try:
        for img in page.get_images(full=True):
            for r in page.get_image_rects(img[0]):
                rr = fitz.Rect(r)
                if rr.width > 2 and rr.height > 2:
                    out.append(rr)
    except Exception:  # noqa: BLE001
        pass
    return out


def _nonempty(r: fitz.Rect) -> fitz.Rect:
    """Give zero-extent rects a sliver of thickness.

    PyMuPDF treats a rect with zero width or height as *empty*, and union
    with an empty rect returns empty. A booktabs rule is exactly zero-height,
    so unioning a table's rules silently yields nothing.
    """
    if r.y1 - r.y0 < 0.5:
        r.y1 = r.y0 + 0.5
    if r.x1 - r.x0 < 0.5:
        r.x1 = r.x0 + 0.5
    return r


def _cluster(
    rects: list[fitz.Rect], upward: bool, limit: float, gap: float, anchor: float
) -> fitz.Rect | None:
    """Union the rects that lie in the band between the caption and `limit`.

    The band filter has to come first. Sorting by distance from the caption
    puts a *neighbouring* float's graphics at the head of the list, so testing
    the limit inside the growth loop breaks on the very first candidate and
    collapses the cluster to a single rule.

    The union is explicit min/max rather than `|=` so it does not depend on
    PyMuPDF's empty-rect semantics (see _nonempty).
    """
    if upward:
        band = [r for r in rects if r.y0 >= limit - 1 and r.y1 <= anchor + 2]
    else:
        band = [r for r in rects if r.y1 <= limit + 1 and r.y0 >= anchor - 2]
    if not band:
        return None
    band.sort(key=(lambda r: -r.y1) if upward else (lambda r: r.y0))
    x0, y0, x1, y1 = band[0].x0, band[0].y0, band[0].x1, band[0].y1
    for r in band[1:]:
        if upward:
            if r.y1 < y0 - gap:
                break
        elif r.y0 > y1 + gap:
            break
        x0, y0 = min(x0, r.x0), min(y0, r.y0)
        x1, y1 = max(x1, r.x1), max(y1, r.y1)
    return fitz.Rect(x0, y0, x1, y1)


def _extract_assets(doc: fitz.Document, outdir: Path) -> tuple[dict, dict, list[str], dict]:
    """Figures, tables, warnings and the clipped region of each asset per
    page (so the text inside a float stays out of the body markdown)."""
    regions: dict[int, list[fitz.Rect]] = {}
    figures: dict[str, AssetInfo] = {}
    tables: dict[str, AssetInfo] = {}
    warnings: list[str] = []

    consensus = _doc_columns(doc)

    for pno, page in enumerate(doc):
        blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
        if not blocks:
            continue
        cols = _columns(blocks, page, consensus)
        pr = page.rect
        graphics = _graphic_rects(page)
        caption_blocks = [b for b in blocks if CAPTION_RE.match(b[4].strip().replace("\n", " "))]

        for b in caption_blocks:
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4].strip()
            m = CAPTION_RE.match(text.replace("\n", " "))
            if not m or m.group("kind").lower().startswith("algorithm"):
                continue
            kind = "table" if m.group("kind").lower().startswith("table") else "figure"
            num = m.group("num")
            pool = tables if kind == "table" else figures
            if num in pool:  # first occurrence wins; later ones are cross-refs
                continue
            caption = clean(" ".join(text.split()))

            # A caption wider than one column belongs to a full-width float.
            spans_all = len(cols) > 1 and (x1 - x0) > 1.35 * (cols[0][1] - cols[0][0])
            if spans_all:
                cx0, cx1 = min(c[0] for c in cols), max(c[1] for c in cols)
            else:
                cx0, cx1 = min(cols, key=lambda c: abs((c[0] + c[1]) / 2 - (x0 + x1) / 2))

            def in_col(r: fitz.Rect) -> bool:
                cx = (r.x0 + r.x1) / 2
                return cx0 - 8 <= cx <= cx1 + 8

            near = [r for r in graphics if in_col(r)]
            # Do not reach past a neighbouring float's caption -- but only
            # captions that share this column. A table in the left column must
            # not cap the band of a figure in the right column.
            others = [
                c
                for c in caption_blocks
                if c is not b and not (c[2] < cx0 - 6 or c[0] > cx1 + 6)
            ]
            up_limit = max((c[3] for c in others if c[3] <= y0 + 2), default=pr.y0)
            dn_limit = min((c[1] for c in others if c[1] >= y1 - 2), default=pr.y1)

            cand: list[tuple[float, float, float, Image.Image]] = []
            # A LaTeX table's only graphics are its booktabs rules, and the
            # gap between toprule and bottomrule is the whole table body, so
            # the figure gap tolerance would split it apart. The neighbouring
            # caption limits are the real guard here.
            gap = _GAP_TABLE if kind == "table" else _GAP_PT
            for upward in (True, False):
                side = (
                    [r for r in near if r.y1 <= y0 + 2]
                    if upward
                    else [r for r in near if r.y0 >= y1 - 2]
                )
                u = _cluster(
                    side,
                    upward,
                    up_limit if upward else dn_limit,
                    gap,
                    y0 if upward else y1,
                )
                if u is None:
                    continue
                # Absorb the float's own text: axis titles, tick and category
                # labels, legends, table rows. Three rules, so that labels
                # are kept but a neighbouring paragraph is never annexed:
                #  * inside: any block centred within the drawn graphics;
                #  * near side (towards the caption): short blocks chained
                #    outward from the graphics, each within _CHAIN_GAP of the
                #    last, never past the caption's edge;
                #  * far side: short blocks within _MAX_TEXT_GROW.
                g = u  # graphics only; the candidate's distance uses this
                gy0, gy1 = u.y0, u.y1
                labels = []
                for tb in blocks:
                    tr = fitz.Rect(tb[:4])
                    if tb is b or not in_col(tr) or CAPTION_RE.match(tb[4].strip().replace("\n", " ")):
                        continue
                    cy = (tr.y0 + tr.y1) / 2
                    if u.y0 - 6 <= cy <= u.y1 + 6:
                        gy0, gy1 = min(gy0, tr.y0), max(gy1, tr.y1)
                    elif len(tb[4].split()) <= _LABEL_WORDS:
                        labels.append(tr)
                if upward:  # caption below the float
                    edge = gy1
                    for tr in sorted((r for r in labels if r.y0 >= u.y1 - 2 and r.y1 <= y0 + 1),
                                     key=lambda r: r.y0):
                        if tr.y0 > edge + _CHAIN_GAP:
                            break
                        edge = max(edge, tr.y1)
                    gy1 = min(max(gy1, edge), y0 - 0.5)
                    far = [r for r in labels if r.y1 <= u.y0 + 2 and r.y0 >= u.y0 - _MAX_TEXT_GROW]
                    gy0 = min([gy0] + [r.y0 for r in far])
                    gy0 = max(gy0, u.y0 - _MAX_TEXT_GROW)
                else:  # caption above the float
                    edge = gy0
                    for tr in sorted((r for r in labels if r.y1 <= u.y0 + 2 and r.y0 >= y1 - 1),
                                     key=lambda r: -r.y1):
                        if tr.y1 < edge - _CHAIN_GAP:
                            break
                        edge = min(edge, tr.y0)
                    gy0 = max(min(gy0, edge), y1 + 0.5)
                    far = [r for r in labels if r.y0 >= u.y1 - 2 and r.y1 <= u.y1 + _MAX_TEXT_GROW]
                    gy1 = max([gy1] + [r.y1 for r in far])
                    gy1 = min(gy1, u.y1 + _MAX_TEXT_GROW)
                u = fitz.Rect(
                    max(cx0 - 4, pr.x0),
                    max(gy0, up_limit, pr.y0 + 4),
                    min(cx1 + 4, pr.x1),
                    min(gy1, dn_limit, pr.y1 - 4),
                )
                if u.y1 - u.y0 < _MIN_CLIP_IN * 72 or (u.y1 - u.y0) > pr.height * _MAX_CLIP_FRAC:
                    continue
                im = _clip(page, u)
                if im is None:
                    continue
                # Score by how much of the region is actually *drawn*, not by
                # ink. Ink favours dense body text, which is exactly how a
                # float on a crowded page steals its neighbour's region.
                area = max(1.0, (u.x1 - u.x0) * (u.y1 - u.y0))
                drawn = sum(
                    max(0.0, min(r.x1, u.x1) - max(r.x0, u.x0))
                    * max(0.0, min(r.y1, u.y1) - max(r.y0, u.y0))
                    for r in near
                )
                # Distance from the caption to the nearest graphic on this side
                # (graphics only: absorbed labels would make every side "near").
                gap_pt = (y0 - g.y1) if upward else (g.y0 - y1)
                cand.append((drawn / area, _ink(im), max(0.0, gap_pt), im, fitz.Rect(u)))

            if not cand:
                warnings.append(f"{kind} {num} (p{pno + 1}): no clippable region near caption")
                continue
            # A caption belongs to the float it touches. Density alone picks
            # the wrong side when a sparse table (a few rules) sits on one side
            # of its caption and a dense chart on the other, so a clearly
            # nearer candidate wins; density breaks ties.
            nearest = min(c[2] for c in cand)
            score, ink, _, im, rect = max(cand, key=lambda c: (c[2] <= nearest + 18.0, c[0]))
            if ink < _MIN_INK:
                warnings.append(f"{kind} {num} (p{pno + 1}): region looks blank (ink {ink:.3f})")
                continue

            path = outdir / f"{kind}_{num}.png"
            im.save(path)
            regions.setdefault(pno, []).append(rect)
            pool[num] = AssetInfo(
                asset_id=num,
                kind=kind,
                caption_number=num,
                caption=caption,
                path=str(path),
                width=im.width,
                height=im.height,
            )
    return figures, tables, warnings, regions


# --------------------------------------------------------------------------
def parse_pdf(pdf_path: str | Path, workdir: str | Path) -> ParsedPaper:
    """Extract markdown + assets. Caption markers are inlined into the
    markdown so the parser agent can do caption-guided sectioning."""
    pdf_path = Path(pdf_path)
    outdir = Path(workdir) / "assets"
    outdir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    try:
        lines = _lines(doc)
        body = _body_size(lines)
        title, authors = _title_and_authors(lines, body)
        figures, tables, warnings, regions = _extract_assets(doc, outdir)

        def in_float(ln: _Line) -> bool:
            x = (ln.bbox[0] + ln.bbox[2]) / 2
            y = (ln.bbox[1] + ln.bbox[3]) / 2
            return any(r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1 for r in regions.get(ln.page, ()))

        # Build markdown, replacing caption blocks with explicit asset markers.
        # Captions are skipped by *block*, not by line: a caption wraps over
        # several lines and the tail would otherwise land in the body text.
        caption_blocks: dict[tuple[int, int], tuple[str, str]] = {}
        for ln in lines:
            m = CAPTION_RE.match(ln.text)
            if m and (ln.page, ln.blk) not in caption_blocks:
                kind = "TABLE" if m.group("kind").lower().startswith("table") else "FIGURE"
                if not m.group("kind").lower().startswith("algorithm"):
                    caption_blocks[(ln.page, ln.blk)] = (kind, m.group("num"))

        md: list[str] = [f"# {title}", ""]
        if authors:
            md += [f"*{authors}*", ""]
        seen_caption: set[tuple[str, str]] = set()
        buf: list[str] = []

        def flush() -> None:
            if buf:
                md.append(_dehyphenate(_detighten(" ".join(buf).strip())))
                md.append("")
                buf.clear()

        for ln in lines:
            t = ln.text.strip()
            if not t or t == title or ln.rotated or _ARXIV_STAMP.match(t) or in_float(ln):
                continue
            key_blk = (ln.page, ln.blk)
            if key_blk in caption_blocks:
                kind, num = caption_blocks[key_blk]
                if (kind, num) not in seen_caption:
                    seen_caption.add((kind, num))
                    flush()
                    pool = tables if kind == "TABLE" else figures
                    # Prefer the extractor's full block-level caption text.
                    full = pool[num].caption if num in pool else " ".join(t.split())
                    md.append(f"[{kind} {num}] {_dehyphenate(_detighten(full))}")
                    md.append("")
                continue
            if _is_heading(ln, body):
                flush()
                md.append(f"## {t}")
                md.append("")
                if t.lower().startswith("reference"):
                    break
                continue
            buf.append(t)
        flush()

        for key, pool in (("figure", figures), ("table", tables)):
            for num in sorted(pool):
                if (key.upper(), num) not in seen_caption:
                    warnings.append(f"{key} {num}: extracted but no caption marker in markdown")

        return ParsedPaper(
            title=clean(title),
            authors=clean(authors),
            markdown=clean("\n".join(md)),
            figures_info=figures,
            tables_info=tables,
            n_pages=doc.page_count,
            warnings=warnings,
            pages=[clean(page.get_text()) for page in doc],
        )
    finally:
        doc.close()
