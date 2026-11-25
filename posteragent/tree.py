"""Poster Tree: the intermediate representation.

The key invariant, taken from PosterForest: geometry is NEVER authored by a
model. Internal nodes carry a single scalar `ratio`; absolute boxes are a
derived cache recomputed from the root. Overlap is therefore structurally
impossible and full canvas coverage is automatic.

Grammar (docs/posterforest-notes.md section 2):
    SplitNode    -> exactly 2 children
    SemanticNode -> exactly 1 child, reserves a header band
    ContentLeaf  -> 0 children
"""
from __future__ import annotations

import copy
import itertools
import re
from dataclasses import dataclass, field
from typing import Iterator, Literal

from . import config

SplitType = Literal["vertical", "horizontal"]
ContentType = Literal["title", "text", "figure", "table"]

_ids = itertools.count(1)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Drop control characters. PDF text carries them (e.g. \\x02 before math
    glyphs); PPTX XML cannot, so python-pptx escapes them as _x0002_ and the
    exported deck would silently differ from the poster tree."""
    return _CONTROL.sub("", text or "")


def _nid(prefix: str) -> str:
    return f"{prefix}{next(_ids)}"


@dataclass
class Box:
    """Normalised region of the poster canvas, origin top-left."""

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    def px(self, size: config.PosterSize) -> tuple[int, int, int, int]:
        return (
            int(round(self.x * size.px_w)),
            int(round(self.y * size.px_h)),
            int(round((self.x + self.w) * size.px_w)),
            int(round((self.y + self.h) * size.px_h)),
        )

    def inches(self, size: config.PosterSize) -> tuple[float, float, float, float]:
        return (self.x * size.w_in, self.y * size.h_in, self.w * size.w_in, self.h * size.h_in)


@dataclass
class Measured:
    """Written by the renderer; read by the content agent."""

    volume_ratio: float = 0.0
    overflow: bool = False
    font_scale: float = 1.0


@dataclass
class Bullet:
    """python-pptx paragraph model, matching the PosterForest content agent
    output schema verbatim (prompts/content_agent.tex:64-73)."""

    alignment: str = "left"
    bullet: bool = True
    level: int = 0
    runs: list[dict[str, str]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.get("text", "") for r in self.runs)

    def to_json(self) -> dict:
        return {
            "alignment": self.alignment,
            "bullet": self.bullet,
            "level": self.level,
            "runs": [{"text": r.get("text", "")} for r in self.runs],
        }

    @staticmethod
    def from_json(d: dict) -> "Bullet":
        runs = d.get("runs") or []
        if isinstance(runs, str):
            runs = [{"text": runs}]
        runs_out = []
        for r in runs:
            if isinstance(r, str):
                runs_out.append({"text": clean(r)})
            elif isinstance(r, dict):
                runs_out.append({"text": clean(str(r.get("text", "")))})
        if not runs_out:
            runs_out = [{"text": clean(str(d.get("text", "")))}]
        lvl = d.get("level", 0)
        return Bullet(
            alignment=str(d.get("alignment", "left")),
            bullet=bool(d.get("bullet", True)),
            level=int(lvl) if isinstance(lvl, (int, float)) else 0,
            runs=runs_out,
        )

    @staticmethod
    def plain(text: str, level: int = 0) -> "Bullet":
        return Bullet(level=level, runs=[{"text": clean(text)}])


@dataclass
class ContentLeaf:
    kind: str = "content"
    id: str = field(default_factory=lambda: _nid("c"))
    source_id: str = ""  # back-pointer into the raw doc tree
    content_type: ContentType = "text"
    bullets: list[Bullet] = field(default_factory=list)
    asset_id: str | None = None
    asset_path: str | None = None
    caption: str = ""
    title: str = ""  # used by content_type == "title"
    subtitle: str = ""
    box: Box = field(default_factory=Box)
    measured: Measured = field(default_factory=Measured)

    @property
    def children(self) -> list:
        return []

    @property
    def text(self) -> str:
        if self.content_type == "title":
            return f"{self.title}\n{self.subtitle}"
        if self.content_type in ("figure", "table"):
            return self.caption
        return "\n".join(b.text for b in self.bullets)


@dataclass
class SemanticNode:
    kind: str = "semantic"
    id: str = field(default_factory=lambda: _nid("s"))
    type: str = "section"
    title: str = ""
    children: list = field(default_factory=list)  # exactly 1
    header_in: float = 1.15  # absolute inches, so headers stay uniform
    box: Box = field(default_factory=Box)

    @property
    def child(self):
        return self.children[0]


@dataclass
class SplitNode:
    kind: str = "split"
    id: str = field(default_factory=lambda: _nid("p"))
    split_type: SplitType = "horizontal"
    ratio: float = 0.5  # fraction of parent box given to children[0]
    children: list = field(default_factory=list)  # exactly 2
    box: Box = field(default_factory=Box)
    locked: bool = False  # structural split (e.g. title band): never clamped,
    # never offered to the vision layout agent
    # "title" = the title band (never moves); "column" = column widths (moved
    # only by the measured allocator, which sees the whole poster); "inner" =
    # everything else.
    role: str = "inner"


Node = SplitNode | SemanticNode | ContentLeaf


# --------------------------------------------------------------------------
# traversal
# --------------------------------------------------------------------------
def walk(node: Node) -> Iterator[Node]:
    """Breadth-first, matching PosterForest's refinement order."""
    queue = [node]
    while queue:
        n = queue.pop(0)
        yield n
        queue.extend(n.children)


def leaves(node: Node) -> list[ContentLeaf]:
    return [n for n in walk(node) if isinstance(n, ContentLeaf)]


def splits(node: Node) -> list[SplitNode]:
    return [n for n in walk(node) if isinstance(n, SplitNode)]


def semantics(node: Node) -> list[SemanticNode]:
    return [n for n in walk(node) if isinstance(n, SemanticNode)]


def find(root: Node, node_id: str) -> Node | None:
    for n in walk(root):
        if n.id == node_id:
            return n
    return None


def section_of(root: Node, node_id: str) -> SemanticNode | None:
    """Nearest enclosing semantic node, i.e. the 'section' a leaf belongs to."""
    path: list[Node] = []

    def dfs(n: Node) -> bool:
        path.append(n)
        if n.id == node_id:
            return True
        for c in n.children:
            if dfs(c):
                return True
        path.pop()
        return False

    if not dfs(root):
        return None
    for n in reversed(path):
        if isinstance(n, SemanticNode):
            return n
    return None


def clone(root: Node) -> Node:
    return copy.deepcopy(root)


def path_to(root: Node, node_id: str) -> list[Node]:
    """Root-to-node path, inclusive; [] when absent."""
    path: list[Node] = []

    def dfs(n: Node) -> bool:
        path.append(n)
        if n.id == node_id:
            return True
        for c in n.children:
            if dfs(c):
                return True
        path.pop()
        return False

    return path if dfs(root) else []


# --------------------------------------------------------------------------
# geometry: the derived cache
# --------------------------------------------------------------------------
def resolve_geometry(root: Node, size: config.PosterSize, box: Box | None = None) -> None:
    """Recompute every `box` from the root. Call after ANY ratio change."""
    b = box or Box(0.0, 0.0, 1.0, 1.0)
    root.box = b

    if isinstance(root, SplitNode):
        a, c = root.children
        # Locked splits are structural (the title band) and keep their ratio
        # exactly; only agent-adjustable splits get the sanity clamp.
        r = root.ratio if root.locked else min(config.RATIO_MAX, max(config.RATIO_MIN, root.ratio))
        if root.split_type == "vertical":  # vertical cut line -> left | right
            resolve_geometry(a, size, Box(b.x, b.y, b.w * r, b.h))
            resolve_geometry(c, size, Box(b.x + b.w * r, b.y, b.w * (1 - r), b.h))
        else:  # horizontal cut line -> top / bottom
            resolve_geometry(a, size, Box(b.x, b.y, b.w, b.h * r))
            resolve_geometry(c, size, Box(b.x, b.y + b.h * r, b.w, b.h * (1 - r)))
    elif isinstance(root, SemanticNode):
        # header_in is absolute so every section header is the same height
        frac = min(0.6, root.header_in / max(0.01, b.h * size.h_in))
        resolve_geometry(root.child, size, Box(b.x, b.y + b.h * frac, b.w, b.h * (1 - frac)))


def header_box(node: SemanticNode, size: config.PosterSize) -> Box:
    b = node.box
    frac = min(0.6, node.header_in / max(0.01, b.h * size.h_in))
    return Box(b.x, b.y, b.w, b.h * frac)


def apply_delta(node: SplitNode, direction: str, delta: float) -> tuple[float, float]:
    """Layout-agent output -> ratio change, hard-clamped.

    The paper gives no arithmetic for this (docs/posterforest-notes.md s2).
    Returns (old_ratio, new_ratio).
    """
    old = node.ratio
    d = (max(0.0, min(5.0, float(delta))) / 5.0) * config.RATIO_STEP_MAX
    grow_first = direction.upper() in ("LEFT", "TOP")
    new = old + (d if grow_first else -d)
    node.ratio = min(config.RATIO_MAX, max(config.RATIO_MIN, new))
    return old, node.ratio


def valid_directions(split_type: SplitType) -> tuple[str, str]:
    return ("LEFT", "RIGHT") if split_type == "vertical" else ("TOP", "BOTTOM")


# --------------------------------------------------------------------------
# text fingerprint: used by the dialogue preservation checks
# --------------------------------------------------------------------------
def text_map(root: Node) -> dict[str, str]:
    """leaf id -> its full text. Compared before/after an edit to detect
    unintended changes outside the requested scope."""
    return {leaf.id: leaf.text for leaf in leaves(root)}


def all_text(root: Node) -> str:
    """Every string the reader sees. The single definition of "the poster's
    text", so the dialogue preservation checks cannot drift apart."""
    parts = []
    for n in walk(root):
        if isinstance(n, SemanticNode) and n.title:
            parts.append(n.title)
        elif isinstance(n, ContentLeaf):
            parts.append(n.text)
    return "\n".join(parts)


def dump(node: Node) -> dict:
    """Lossless serialisation (unlike to_json, which is a prompt summary)."""
    if isinstance(node, SplitNode):
        return {"kind": "split", "id": node.id, "split_type": node.split_type,
                "ratio": node.ratio, "locked": node.locked, "role": node.role,
                "children": [dump(c) for c in node.children]}
    if isinstance(node, SemanticNode):
        return {"kind": "semantic", "id": node.id, "type": node.type, "title": node.title,
                "header_in": node.header_in, "children": [dump(c) for c in node.children]}
    leaf: ContentLeaf = node
    return {"kind": "content", "id": leaf.id, "source_id": leaf.source_id,
            "content_type": leaf.content_type, "bullets": [b.to_json() for b in leaf.bullets],
            "asset_id": leaf.asset_id, "asset_path": leaf.asset_path, "caption": leaf.caption,
            "title": leaf.title, "subtitle": leaf.subtitle}


def load(d: dict) -> Node:
    """Inverse of dump(). Boxes and measurements are derived, so the caller
    must resolve_geometry() and render.measure() afterwards."""
    kind = d["kind"]
    if kind == "split":
        return SplitNode(id=d["id"], split_type=d["split_type"], ratio=float(d["ratio"]),
                         locked=bool(d.get("locked")), role=d.get("role", "inner"),
                         children=[load(c) for c in d["children"]])
    if kind == "semantic":
        return SemanticNode(id=d["id"], type=d.get("type", "section"), title=d.get("title", ""),
                            header_in=float(d.get("header_in", 1.15)),
                            children=[load(c) for c in d["children"]])
    return ContentLeaf(id=d["id"], source_id=d.get("source_id", ""),
                       content_type=d.get("content_type", "text"),
                       bullets=[Bullet.from_json(b) for b in d.get("bullets", [])],
                       asset_id=d.get("asset_id"), asset_path=d.get("asset_path"),
                       caption=d.get("caption", ""), title=d.get("title", ""),
                       subtitle=d.get("subtitle", ""))


def to_json(node: Node) -> dict:
    """Compact serialisation for prompts and for the UI's debug pane."""
    if isinstance(node, SplitNode):
        return {
            "kind": "split",
            "id": node.id,
            "split_type": node.split_type,
            "ratio": round(node.ratio, 3),
            "children": [to_json(c) for c in node.children],
        }
    if isinstance(node, SemanticNode):
        return {
            "kind": "semantic",
            "id": node.id,
            "type": node.type,
            "title": node.title,
            "children": [to_json(c) for c in node.children],
        }
    leaf: ContentLeaf = node
    d: dict = {
        "kind": "content",
        "id": leaf.id,
        "content_type": leaf.content_type,
        "volume_ratio": round(leaf.measured.volume_ratio, 1),
    }
    if leaf.content_type == "title":
        d["title"] = leaf.title
    elif leaf.content_type in ("figure", "table"):
        d["asset"] = f"{leaf.content_type} {leaf.asset_id}"
        d["caption"] = leaf.caption[:120]
    else:
        d["bullets"] = [b.text for b in leaf.bullets]
    return d
