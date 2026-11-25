"""O_Layout_Init: content statistics -> binary split tree. No LLM.

PosterForest demotes this from an agent to an algorithm ("the initial
allocation of regions is deterministically derived from content statistics",
03_method.tex:148) but gives no rules -- its "see supplementary" pointer is
commented out and the supplement has nothing. It is also the highest-leverage
component in the system, because the refinement loop can only adjust split
ratios: topology is frozen here. If sections are grouped badly, no amount of
iterating recovers.

Strategy: column-major balanced packing (preserving reading order), then a
right-leaning chain of splits per level. Overlap is structurally impossible.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .parse import AssetInfo
from .tree import Box, Bullet, ContentLeaf, SemanticNode, SplitNode, resolve_geometry


@dataclass
class OutlineSection:
    """One poster section as chosen by the content stage."""

    id: str
    title: str
    bullets: list[str] = field(default_factory=list)
    assets: list[AssetInfo] = field(default_factory=list)
    importance: float = 1.0  # PosterForest never models this (appendix:409)
    source_ids: list[str] = field(default_factory=list)
    source_text: str = ""  # paper passages, for the content agent's expand path


@dataclass
class Outline:
    title: str
    authors: str
    sections: list[OutlineSection]


def _text_mass(sec: OutlineSection) -> float:
    return sum(len(b) for b in sec.bullets) * config.CHAR_COST


def _asset_mass(a: AssetInfo) -> float:
    return config.FIG_COST if a.kind == "figure" else config.TAB_COST


def section_mass(sec: OutlineSection) -> float:
    base = _text_mass(sec) + sum(_asset_mass(a) for a in sec.assets)
    # Importance feeds the init weights; this is the cheap fix for the
    # "no modelling of figure importance" limitation (06_appendix.tex:409).
    return max(300.0, base) * max(0.5, min(2.0, sec.importance))


def _balanced_columns(
    sections: list[OutlineSection], n_cols: int
) -> list[list[OutlineSection]]:
    """Fill columns in reading order, closing a column once it has taken its
    share of the total mass. Keeps left-to-right reading order intact, which a
    pure bin-packer would scramble."""
    n_cols = max(1, min(n_cols, len(sections)))
    total = sum(section_mass(s) for s in sections)
    target = total / n_cols
    cols: list[list[OutlineSection]] = [[] for _ in range(n_cols)]
    ci, acc = 0, 0.0
    remaining = len(sections)
    for s in sections:
        m = section_mass(s)
        # Move on if this column is full AND every later column can still be
        # given at least one section.
        if (
            ci < n_cols - 1
            and acc > 0
            and acc + m / 2 > target
            and remaining > (n_cols - 1 - ci)
        ):
            ci += 1
            acc = 0.0
        cols[ci].append(s)
        acc += m
        remaining -= 1
    return [c for c in cols if c]


def _chain(items: list, masses: list[float], split_type: str, locked: bool = False,
           role: str = "inner"):
    """Right-leaning split chain so N regions come out of binary splits."""
    if len(items) == 1:
        return items[0]
    total = sum(masses)
    r = masses[0] / total if total else 1.0 / len(items)
    rest = _chain(items[1:], masses[1:], split_type, locked, role)
    return SplitNode(
        split_type=split_type,
        ratio=r if locked else min(config.RATIO_MAX, max(config.RATIO_MIN, r)),
        children=[items[0], rest],
        locked=locked,
        role=role,
    )


def _section_body(sec: OutlineSection) -> object:
    """Text block plus its assets, grouped so they resize together."""
    text_leaf = ContentLeaf(
        source_id=sec.id,
        content_type="text",
        bullets=[Bullet.plain(b) for b in sec.bullets],
    )
    if not sec.assets:
        return text_leaf

    asset_leaves = [
        ContentLeaf(
            source_id=sec.id,
            content_type=a.kind,
            asset_id=a.asset_id,
            asset_path=a.path,
            caption=a.caption,
        )
        for a in sec.assets
    ]
    # Wide assets stack under the text; tall ones sit beside it. This is the
    # cheapest guard against the "extremely tall-narrow panel" the layout
    # agent's Shape Sanity rule warns about.
    mean_aspect = sum(a.aspect for a in sec.assets) / len(sec.assets)
    asset_split = "horizontal" if mean_aspect >= 1.6 else "vertical"
    assets_node = _chain(
        asset_leaves, [_asset_mass(a) for a in sec.assets], asset_split
    )
    if not sec.bullets:
        return assets_node

    tm, am = _text_mass(sec), sum(_asset_mass(a) for a in sec.assets)
    if mean_aspect >= 1.6:
        return _chain([text_leaf, assets_node], [tm, am], "horizontal")
    return _chain([text_leaf, assets_node], [tm, am], "vertical")


def build_initial_tree(outline: Outline, size: config.PosterSize) -> SplitNode:
    """Poster = locked title band on top, then a column grid."""
    title_leaf = ContentLeaf(
        content_type="title", title=outline.title, subtitle=outline.authors
    )

    sections = outline.sections or [
        OutlineSection(id="s0", title="Content", bullets=["(no content selected)"])
    ]
    n_cols = config.N_COLUMNS if size.w_in >= size.h_in else 2
    cols = _balanced_columns(sections, n_cols)

    col_nodes, col_masses = [], []
    for col in cols:
        sem_nodes = [
            SemanticNode(type="section", title=s.title, children=[_section_body(s)])
            for s in col
        ]
        masses = [section_mass(s) for s in col]
        col_nodes.append(_chain(sem_nodes, masses, "horizontal"))
        col_masses.append(sum(masses))

    # The column chain is locked against the *vision* layout agent: it only
    # ever sees one split's crop, has no global view and no model of
    # importance, so left to itself it votes to enlarge whichever side holds a
    # dense figure -- repeatedly, until a whole column is starved. Column
    # widths are instead moved only by the measured allocator (allocate.py),
    # which re-measures every panel on the poster before accepting a change.
    body = _chain(col_nodes, col_masses, "vertical", locked=True, role="column")

    # Title band height is physical, not proportional, so it is a locked split.
    title_in = 0.10 * size.h_in if size.h_in >= 30 else 0.12 * size.h_in
    root = SplitNode(
        split_type="horizontal",
        ratio=title_in / size.h_in,
        children=[title_leaf, body],
        locked=True,
        role="title",
    )
    resolve_geometry(root, size, Box())
    return root
