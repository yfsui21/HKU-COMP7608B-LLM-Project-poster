"""Measured space allocation: who gets room, who gives it, what it costs.

The proposal (s4.2): "If the explanation still cannot fit at the minimum font
size, it will request more space. The layout agent will identify where that
space can come from and report any new crowding. The next content pass will
then revise the affected sections using this feedback."

A request names a receiving panel (a text panel that overflows, or a figure
that must reach a displayed width). The allocator walks every split above the
receiver -- innermost first, up to and including the column widths -- tries a
few ratio steps, re-measures the WHOLE poster for each, and keeps the cheapest
step that helps. Nothing here calls a model: the same request on the same
poster always gives the same answer, and no schedule gets a smarter allocator
than another.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config, render
from .tree import (
    Node,
    SplitNode,
    leaves,
    path_to,
    resolve_geometry,
    section_of,
)


@dataclass
class SpaceRequest:
    leaf_id: str
    reason: str
    target_width_in: float | None = None  # figures: displayed width to reach
    source: str = "content"  # content | geometry | review | dialogue

    def to_json(self) -> dict:
        return {"leaf": self.leaf_id, "reason": self.reason,
                "target_width_in": self.target_width_in, "source": self.source}


@dataclass
class Allocation:
    receiver: str
    satisfied: bool
    moves: list[dict] = field(default_factory=list)
    donors: list[str] = field(default_factory=list)
    new_crowding: list[str] = field(default_factory=list)
    note: str = ""

    def to_json(self) -> dict:
        return {"receiver": self.receiver, "satisfied": self.satisfied, "moves": self.moves,
                "donors": self.donors, "new_crowding": self.new_crowding, "note": self.note}


def label(root: Node, leaf_id: str) -> str:
    sec = section_of(root, leaf_id)
    leaf = next((l for l in leaves(root) if l.id == leaf_id), None)
    what = "text" if leaf is None or leaf.content_type == "text" else f"{leaf.content_type} {leaf.asset_id}"
    return f"{sec.title if sec is not None else '?'} / {what} ({leaf_id})"


def _remeasure(root: Node, size: config.PosterSize) -> None:
    resolve_geometry(root, size)
    render.measure(root, size)


def _bounds(node: SplitNode) -> tuple[float, float]:
    if node.role == "column":
        return config.COLUMN_RATIO_MIN, config.COLUMN_RATIO_MAX
    return config.RATIO_MIN, config.RATIO_MAX


def _state(root: Node) -> dict[str, float]:
    return {l.id: (l.measured.volume_ratio if l.content_type == "text" else 0.0) for l in leaves(root)}


def crowded(root: Node) -> set[str]:
    return {l.id for l in leaves(root) if l.content_type == "text" and l.measured.overflow}


def _excess(root: Node) -> float:
    """Total fill beyond 100%, the quantity an allocation must not increase."""
    return sum(max(0.0, l.measured.volume_ratio - 100.0)
               for l in leaves(root) if l.content_type == "text")


def _satisfied(root: Node, size: config.PosterSize, req: SpaceRequest) -> bool:
    leaf = next((l for l in leaves(root) if l.id == req.leaf_id), None)
    if leaf is None:
        return False
    if req.target_width_in is not None:
        return render.asset_width_in(leaf, size) >= req.target_width_in - 0.01
    return not leaf.measured.overflow


def _benefit(root: Node, size: config.PosterSize, req: SpaceRequest) -> float:
    leaf = next((l for l in leaves(root) if l.id == req.leaf_id), None)
    if leaf is None:
        return 0.0
    if req.target_width_in is not None:
        return render.asset_width_in(leaf, size)
    return -leaf.measured.volume_ratio


def _candidates(root: Node, leaf_id: str, scope: str) -> list[tuple[SplitNode, int]]:
    """Splits above the leaf, innermost first, with the leaf's side.

    scope "all"      -> every split but the title band, incl. column widths;
    scope "innermost"-> only the split that directly separates the leaf from
                        its neighbour (the fixed-layout dialogue condition).
    """
    path = path_to(root, leaf_id)
    out: list[tuple[SplitNode, int]] = []
    for i in range(len(path) - 1):
        n = path[i]
        if isinstance(n, SplitNode) and n.role != "title":
            out.append((n, 0 if n.children[0] is path[i + 1] else 1))
    out.reverse()
    return out[:1] if scope == "innermost" else out


def allocate(root: Node, size: config.PosterSize, req: SpaceRequest, *,
             scope: str = "all", max_moves: int = 6, allow_crowding: bool = False) -> Allocation:
    """Give `req` room, mutating `root`.

    By default a move that crowds another panel is only kept if it satisfies
    the request, so overflow is never just shifted around. With
    `allow_crowding` (a content pass will follow and can condense the donor)
    any move that brings the request closer is allowed -- needed when the
    request takes several coordinated moves, e.g. a figure that must grow in
    width and height. Either way the new crowding is reported."""
    _remeasure(root, size)
    alloc = Allocation(receiver=label(root, req.leaf_id), satisfied=_satisfied(root, size, req))
    if alloc.satisfied:
        alloc.note = "already satisfied"
        return alloc

    before_crowded = crowded(root)
    for _ in range(max_moves):
        base_benefit = _benefit(root, size, req)
        base_excess = _excess(root)
        base_crowded = crowded(root)
        options: list[tuple[tuple, SplitNode, float, float]] = []
        for depth, (node, side) in enumerate(_candidates(root, req.leaf_id, scope)):
            old = node.ratio
            lo, hi = _bounds(node)
            for step in config.ALLOC_STEPS:
                new = min(hi, max(lo, old + (step if side == 0 else -step)))
                if abs(new - old) < 1e-6:
                    continue
                node.ratio = new
                _remeasure(root, size)
                gain = _benefit(root, size, req) - base_benefit
                ok = _satisfied(root, size, req)
                worse = _excess(root) - base_excess
                newly = len(crowded(root) - base_crowded)
                node.ratio = old
                if gain <= 1e-6:
                    continue
                if not ok and not allow_crowding and (newly or worse > 1e-6):
                    continue  # no net help: moves room around without fixing it
                # Prefer: satisfies; crowds nobody new; least total overflow;
                # nearest split (keeps the change local); smallest step.
                options.append(((not ok, newly, round(max(0.0, worse), 1), depth, abs(new - old)),
                                node, old, new))
        if not options:
            break
        options.sort(key=lambda o: o[0])
        _, node, old, new = options[0]
        node.ratio = new
        _remeasure(root, size)
        side = next(s for n, s in _candidates(root, req.leaf_id, scope) if n is node)
        donor = node.children[1 - side]
        alloc.moves.append({"split": node.id, "role": node.role, "split_type": node.split_type,
                            "ratio_before": round(old, 4), "ratio_after": round(new, 4)})
        for l in leaves(donor):
            name = label(root, l.id)
            if name not in alloc.donors:
                alloc.donors.append(name)
        if _satisfied(root, size, req):
            break

    _remeasure(root, size)
    alloc.satisfied = _satisfied(root, size, req)
    alloc.new_crowding = [label(root, i) for i in sorted(crowded(root) - before_crowded)]
    if not alloc.moves:
        alloc.note = "no split above this panel can give it room without making the poster worse"
    elif not alloc.satisfied:
        alloc.note = "partial: the request is not fully met"
    return alloc
