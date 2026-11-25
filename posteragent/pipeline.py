"""Generation: parse -> claims -> outline -> initial layout -> four passes.

    joint          content, layout, content, layout
    content-first  content, content, layout, layout   (never back to content)
    layout-first   layout, layout, content, content   (boxes frozen after layout)

All three start from the same initial draft, get two content and two layout
passes, the same per-pass call caps, and a render + review after every pass.
The only difference is whether later decisions of one kind can respond to the
other -- which is the proposal's question.

How the two kinds of pass talk to each other:
  content pass -> SpaceRequest when required text does not fit at the minimum
                  font size (measured), or when the content agent says so;
  layout pass  -> allocate.py finds the donor, moves the split, and reports
                  newly crowded panels, which the next content pass receives
                  as ordinary overflow issues.
"""
from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from . import agents, allocate as alloc_mod, claims as claims_mod, config, llm, render
from .allocate import SpaceRequest, allocate
from .claims import Claim
from .layout_init import Outline, OutlineSection, build_initial_tree
from .parse import AssetInfo, ParsedPaper, parse_pdf
from .review import Review, review
from .tree import (
    ContentLeaf,
    Node,
    SemanticNode,
    SplitNode,
    apply_delta,
    clone,
    dump,
    find,
    leaves,
    load as load_tree,
    resolve_geometry,
    section_of,
    splits,
)

Progress = Callable[[float, str], None]

ORDERS: dict[str, tuple[str, ...]] = {
    "joint": ("content", "layout", "content", "layout"),
    "content-first": ("content", "content", "layout", "layout"),
    "layout-first": ("layout", "layout", "content", "content"),
}


def _noop(_frac: float, _msg: str) -> None:
    pass


@dataclass
class ProtectedSpan:
    text: str
    turn: int


@dataclass
class Draft:
    """Everything the schedules share: one per paper."""

    paper: ParsedPaper
    raw_nodes: list[dict]
    claims: list[Claim]
    claim_failures: list[str]
    outline: Outline
    asset_problems: list[str]
    trace: llm.Trace
    seconds: float = 0.0


@dataclass
class Session:
    """One poster under one schedule: what the UI, dialogue and study use."""

    workdir: Path
    size: config.PosterSize
    paper: ParsedPaper
    outline: Outline
    raw_nodes: list[dict]
    root: Node
    preview: Image.Image | None
    trace: llm.Trace
    claims: list[Claim] = field(default_factory=list)
    claim_failures: list[str] = field(default_factory=list)
    asset_problems: list[str] = field(default_factory=list)
    condition: str = "joint"
    layout_frozen: bool = False
    protected: list[ProtectedSpan] = field(default_factory=list)
    word_limits: dict[str, int] = field(default_factory=dict)
    min_width: dict[str, float] = field(default_factory=dict)
    requests: list[SpaceRequest] = field(default_factory=list)  # pending, content -> layout
    last_allocations: list[dict] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    artifact: str = ""  # review folder name holding the current deck
    last_review: Review | None = None

    # --- helpers -----------------------------------------------------------
    def source_text_for(self, leaf: ContentLeaf) -> str:
        for s in self.outline.sections:
            if s.id == leaf.source_id:
                return s.source_text
        return ""

    def section_title_for(self, leaf: ContentLeaf) -> str:
        sec = section_of(self.root, leaf.id)
        return sec.title if isinstance(sec, SemanticNode) else ""

    def required_for(self, leaf: ContentLeaf) -> list[str]:
        out: list[str] = []
        for c in self.claims:
            if c.section_id == leaf.source_id:
                out += [q for q in c.conditions if q not in out]
        return out

    @property
    def known_numbers(self) -> set[str]:
        return set(claims_mod.numbers("\n".join(self.paper.pages)))

    def remeasure(self) -> None:
        resolve_geometry(self.root, self.size)
        render.measure(self.root, self.size)

    def rerender(self) -> Image.Image:
        self.remeasure()
        self.preview = render.render_png(self.root, self.size)
        return self.preview

    def export(self, stem: str = "poster") -> tuple[Path, Path]:
        png = self.workdir / f"{stem}.png"
        if self.preview is None:
            self.rerender()
        self.preview.save(png)
        pptx = render.render_pptx(self.root, self.size, self.workdir / f"{stem}.pptx")
        return png, pptx

    def record(self, stage: str, rv: Review, notes: list[str]) -> None:
        self.last_review = rv
        self.artifact = rv.folder.name
        self.rounds.append({"stage": stage, "artifact": rv.folder.name, "notes": list(notes),
                            "issues": len(rv.issues), "blocking": len(rv.blocking),
                            "resolved": rv.resolved,
                            "kinds": sorted({i["kind"] for i in rv.blocking})})
        self.trace.log(f"[{self.condition}] {stage}: {rv.summary()}"
                       + (f" | {'; '.join(notes)}" if notes else ""))


# --------------------------------------------------------------------------
# preparation
# --------------------------------------------------------------------------
def prepare(pdf_path: str | Path, workdir: str | Path, *,
            n_sections: tuple[int, int] = (5, 7), trace: llm.Trace | None = None,
            progress: Progress = _noop) -> Draft:
    """Parse the paper, freeze the claims, choose the outline. Raises on
    failure; the evaluation runner records the reason."""
    trace = trace or llm.Trace()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    progress(0.05, "Parsing PDF (text, figures, tables)...")
    paper = parse_pdf(pdf_path, workdir)
    if not any(t.strip() for t in paper.pages):
        raise ValueError("the PDF has no extractable text (scanned pages are not supported)")
    trace.log(f"parsed {paper.n_pages}p: {len(paper.figures_info)} figures, "
              f"{len(paper.tables_info)} tables, {len(paper.warnings)} extraction warnings")
    for w in paper.warnings:
        trace.log(f"  extraction: {w}")

    progress(0.15, "Claim agent: what must the poster communicate?")
    claims, failures = claims_mod.extract(paper, trace)
    trace.log(f"claims: {len(claims)} verified and frozen, {len(failures)} recorded problem(s)")
    if not claims:
        raise ValueError("no claim could be verified against the paper")

    progress(0.30, "Parser agent: building the document tree...")
    raw_tree = agents.parser_agent(paper, trace)
    raw_nodes = agents._flatten_raw(raw_tree)
    problems = agents.audit_assets(raw_nodes, paper)
    for p in problems:
        trace.log(f"  asset audit: {p}")

    progress(0.42, "Summarizing agent: selecting poster content...")
    outline = agents.select_outline(raw_nodes, paper, trace, *n_sections, claims=claims)
    trace.log(f"outline: {len(outline.sections)} sections, "
              f"{sum(len(s.bullets) for s in outline.sections)} bullets, "
              f"{sum(len(s.assets) for s in outline.sections)} assets placed")
    return Draft(paper, raw_nodes, claims, failures, outline, problems, trace,
                 round(time.time() - t0, 2))


def initial_session(draft: Draft, size: config.PosterSize, workdir: str | Path,
                    *, use_reviewer: bool = True) -> Session:
    """Layout init + first review. Every schedule at this size forks from it."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    trace = llm.Trace()
    root = build_initial_tree(draft.outline, size)
    sess = Session(workdir=workdir, size=size, paper=draft.paper,
                   outline=copy.deepcopy(draft.outline), raw_nodes=draft.raw_nodes, root=root,
                   preview=None, trace=trace, claims=copy.deepcopy(draft.claims),
                   claim_failures=list(draft.claim_failures),
                   asset_problems=list(draft.asset_problems))
    rv = review(sess, "00-initial", use_reviewer=use_reviewer)
    sess.record("initial", rv, ["deterministic layout from content statistics"])
    return sess


def fork(base: Session, workdir: str | Path, condition: str) -> Session:
    """Independent copy for another schedule: same draft, same initial review."""
    import shutil

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    keep = base.last_review
    sess = Session(
        workdir=workdir, size=base.size, paper=base.paper, outline=copy.deepcopy(base.outline),
        raw_nodes=base.raw_nodes, root=clone(base.root),
        preview=base.preview.copy() if base.preview is not None else None,
        trace=llm.Trace(), claims=copy.deepcopy(base.claims),
        claim_failures=list(base.claim_failures), asset_problems=list(base.asset_problems),
        condition=condition, layout_frozen=base.layout_frozen,
        protected=copy.deepcopy(base.protected), word_limits=dict(base.word_limits),
        min_width=dict(base.min_width), requests=[], rounds=copy.deepcopy(base.rounds),
        artifact=base.artifact,
    )
    if keep is not None:
        src, dst = Path(keep.folder), workdir / Path(keep.folder).name
        if src.resolve() != dst.resolve():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        sess.last_review = copy.copy(keep)
        sess.last_review.folder = dst
    return sess


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------
def content_pass(sess: Session, rv: Review, *, only: set[str] | None = None,
                 may_request: bool = True) -> list[str]:
    """One content pass: repair claims, condense overflow, fill sparse panels.

    `only` restricts editing to some panels (dialogue). Calls are capped and
    run concurrently: each edit depends only on its own panel and a snapshot
    of its siblings.
    """
    text = {l.id: l for l in leaves(sess.root) if l.content_type == "text"}
    problems: dict[str, list[str]] = {}
    hints: dict[str, list[str]] = {}
    for i in rv.issues:
        lid = i["element"]
        if lid not in text:
            continue
        if i["kind"] in ("missing_condition", "unsupported_number", "claim_rejected", "claim_unplaced"):
            problems.setdefault(lid, []).append(f"{i['requirement']}: {i['detail']}")
        elif i["source"] == "reviewer":
            if i["change"] == "repair":
                problems.setdefault(lid, []).append(f"{i['requirement']}: {i['detail']}")
            else:
                hints.setdefault(lid, []).append(i["detail"])

    def priority(l: ContentLeaf) -> tuple:
        over_limit = l.id in sess.word_limits and len(l.text.split()) > sess.word_limits[l.id]
        return (0 if l.id in problems else 1 if (l.measured.overflow or over_limit) else 2,
                -abs(l.measured.volume_ratio - 95))

    def wants(l: ContentLeaf) -> bool:
        r = l.measured.volume_ratio
        limit = sess.word_limits.get(l.id)
        return (l.id in problems or l.measured.overflow
                or (limit is not None and len(l.text.split()) > limit)
                or not (config.VOLUME_TARGET_LO <= r <= config.VOLUME_TARGET_HI))

    targets = sorted((l for l in text.values() if wants(l) and (only is None or l.id in only)),
                     key=priority)[: config.MAX_CONTENT_CALLS]
    if not targets:
        return []
    known = sess.known_numbers

    def work(leaf: ContentLeaf):
        spans = [p.text for p in sess.protected if p.text in leaf.text]
        try:
            return agents.content_agent(
                sess.root, leaf, sess.source_text_for(leaf), sess.trace,
                "; ".join(hints.get(leaf.id, [])),
                required=sess.required_for(leaf), problems=problems.get(leaf.id),
                protected=spans, known_numbers=known,
                word_limit=sess.word_limits.get(leaf.id),
            )
        except RuntimeError as e:
            sess.trace.log(f"content agent failed on {leaf.id}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as ex:
        edits = list(ex.map(work, targets))

    notes: list[str] = []
    asked: dict[str, str] = {}
    condensed: set[str] = set()
    for leaf, edit in zip(targets, edits):
        if edit is None:
            continue
        target = find(sess.root, leaf.id)
        if not isinstance(target, ContentLeaf):
            continue
        target.bullets = edit.bullets
        name = sess.section_title_for(leaf) or leaf.id
        if edit.rejected:
            notes.append(f"content:{edit.mode} on '{name}' kept the previous text ({edit.rejected})")
        else:
            notes.append(f"content:{edit.mode} on '{name}' (was {edit.before_ratio:.0f}% full)")
        if edit.mode in ("condense", "repair"):
            condensed.add(leaf.id)
        if edit.needs_space:
            asked[leaf.id] = edit.space_reason or "required content does not fit"
    sess.remeasure()

    # The measured half of the space request: condensed and still too big at
    # the minimum font size means shorter text alone cannot solve it.
    for lid in condensed:
        leaf = find(sess.root, lid)
        if isinstance(leaf, ContentLeaf) and leaf.measured.overflow and lid not in asked:
            asked[lid] = f"still {leaf.measured.volume_ratio:.0f}% full after condensing"
    if asked and may_request:
        if sess.layout_frozen:
            notes.append(f"{len(asked)} space request(s) not served: layout is frozen in this condition")
        else:
            for lid, reason in asked.items():
                if not any(r.leaf_id == lid and r.target_width_in is None for r in sess.requests):
                    sess.requests.append(SpaceRequest(lid, reason, source="content"))
                notes.append(f"space requested by '{sess.section_title_for(text[lid]) or lid}': {reason}")
    return notes


def _layout_candidates(root: Node) -> list[SplitNode]:
    """Splits worth a vision call, largest region first."""
    out = []
    for n in splits(root):
        if n.locked:
            continue
        has_asset = any(l.content_type in ("figure", "table") for l in leaves(n))
        crowded = any(l.measured.overflow for l in leaves(n))
        if has_asset or crowded:
            out.append(n)
    out.sort(key=lambda n: -(n.box.w * n.box.h))
    return out[: config.MAX_LAYOUT_CALLS]


def _legibility_moves(sess: Session, feedback: str) -> list[str]:
    """PosterForest-style layout agent on annotated crops, one bounded delta
    per split. A move that makes any panel overflow more is undone: the agent
    sees one crop at a time, the measurement sees the whole poster."""
    notes: list[str] = []
    base = render.render_png(sess.root, sess.size)
    for node in _layout_candidates(sess.root):
        target = find(sess.root, node.id)
        if not isinstance(target, SplitNode):
            continue
        overlay = render.render_overlay(sess.root, target, sess.size, base)
        try:
            edit = agents.layout_agent(target, overlay, sess.trace, feedback)
        except RuntimeError as e:
            sess.trace.log(f"layout agent failed on {node.id}: {e}")
            continue
        if edit is None:
            continue
        crowd, excess = alloc_mod.crowded(sess.root), alloc_mod._excess(sess.root)
        widths = {lid: render.asset_width_in(find(sess.root, lid), sess.size) for lid in sess.min_width}
        old, new = apply_delta(target, edit.direction, edit.delta)
        sess.remeasure()
        worse = (alloc_mod.crowded(sess.root) - crowd) or alloc_mod._excess(sess.root) > excess + 1e-6
        shrunk = [lid for lid, w in widths.items()
                  if w >= sess.min_width[lid] - 0.01
                  > render.asset_width_in(find(sess.root, lid), sess.size)]
        if worse or shrunk:
            target.ratio = old
            sess.remeasure()
            notes.append(f"layout: {node.id} {edit.direction} undone ("
                         + ("would overflow text elsewhere" if worse else "would undo a user's figure size") + ")")
            continue
        if abs(new - old) < 1e-6:
            continue
        notes.append(f"layout: {node.id} {edit.direction} ratio {old:.2f}->{new:.2f} ({edit.reason[:80]})")
        base = render.render_png(sess.root, sess.size)
    return notes


def collect_requests(sess: Session, rv: Review) -> list[SpaceRequest]:
    """Pending requests from the content side plus what the review measured."""
    reqs = list(sess.requests)
    have = {(r.leaf_id, r.target_width_in is not None) for r in reqs}
    for i in rv.issues:
        lid = i["element"]
        leaf = find(sess.root, lid) if lid != "poster" else None
        if not isinstance(leaf, ContentLeaf):
            continue
        is_asset = leaf.content_type in ("figure", "table")
        target = None
        if i["kind"] == "figure_too_small":
            target = sess.min_width.get(lid)
        elif i["kind"] == "missing_asset" or (i["source"] == "reviewer" and i["change"] == "more_space"):
            if is_asset:
                target = max(0.5, render.asset_width_in(leaf, sess.size) * 1.15)
        elif i["kind"] != "overflow":
            continue
        if is_asset and target is None:
            continue
        key = (lid, target is not None)
        if key not in have and (target is not None or leaf.content_type == "text"):
            have.add(key)
            reqs.append(SpaceRequest(lid, i["detail"], target_width_in=target, source=i["source"]))
    return reqs


def serve_requests(sess: Session, reqs: list[SpaceRequest], *, scope: str = "all",
                   guard_widths: bool = True, content_follows: bool = False) -> list[str]:
    """Run the allocator for each request; keep earlier figure sizes intact.

    When a content pass follows, a hard figure-size requirement (a user's
    request, a missing or too-small figure -- not a reviewer suggestion) may
    crowd other panels: that content pass condenses them, and the final
    review still rejects any overflow left."""
    notes: list[str] = []
    for req in reqs:
        before = clone(sess.root)
        widths = {lid: w for lid, w in sess.min_width.items() if lid != req.leaf_id}
        got = {lid: render.asset_width_in(find(sess.root, lid), sess.size) for lid in widths}
        crowd_ok = content_follows and req.target_width_in is not None and req.source != "reviewer"
        a = allocate(sess.root, sess.size, req, scope=scope, allow_crowding=crowd_ok)
        if guard_widths and any(got[lid] >= w - 0.01 > render.asset_width_in(find(sess.root, lid), sess.size)
                                for lid, w in widths.items()):
            sess.root = before
            sess.remeasure()
            a.moves, a.donors, a.new_crowding = [], [], []
            a.satisfied = False
            a.note = "would undo a figure size requested earlier"
        sess.last_allocations.append(a.to_json() | {"reason": req.reason, "source": req.source})
        if a.moves:
            notes.append(f"space for {a.receiver}: from {', '.join(a.donors) or '?'}"
                         + ("" if a.satisfied else " (partial)")
                         + (f"; newly crowded: {', '.join(a.new_crowding)}" if a.new_crowding else ""))
        elif a.note != "already satisfied":
            notes.append(f"space for {a.receiver}: none ({a.note})")
    return notes


def layout_pass(sess: Session, rv: Review, *, content_follows: bool = False) -> list[str]:
    sess.last_allocations = []
    if sess.layout_frozen:
        return ["layout frozen"]
    reqs = collect_requests(sess, rv)
    sess.requests = []
    notes = serve_requests(sess, reqs, content_follows=content_follows)
    feedback = "; ".join(i["detail"] for i in rv.issues if i["source"] == "reviewer")[:600]
    notes += _legibility_moves(sess, feedback)
    sess.remeasure()
    return notes


# --------------------------------------------------------------------------
# schedules
# --------------------------------------------------------------------------
def run_schedule(sess: Session, condition: str, *, use_reviewer: bool = True,
                 budget_s: float | None = None, progress: Progress = _noop) -> Session:
    """Four passes, a render + review after each. Never raises on quality."""
    if condition not in ORDERS:
        raise ValueError(f"unknown condition {condition!r}; choose from {', '.join(ORDERS)}")
    sess.condition = condition
    t0 = time.time()
    rv = sess.last_review
    if rv is None:
        rv = review(sess, "00-initial", use_reviewer=use_reviewer)
        sess.record("initial", rv, [])
    order = ORDERS[condition]
    for i, role in enumerate(order, 1):
        if budget_s is not None and time.time() - t0 > budget_s:
            sess.trace.log(f"budget of {budget_s:.0f}s reached; stopped before pass {i} ({role})")
            break
        if condition == "layout-first" and i == 3:
            # Section boundaries and figure boxes are final from here on.
            sess.layout_frozen = True
            sess.requests = []
        progress(0.55 + 0.4 * i / len(order), f"[{condition}] pass {i}/{len(order)}: {role}")
        sess.last_allocations = []
        notes = (content_pass(sess, rv) if role == "content"
                 else layout_pass(sess, rv, content_follows="content" in order[i:]))
        rv = review(sess, f"{i:02d}-{role}", use_reviewer=use_reviewer)
        sess.record(f"{i:02d}-{role}", rv, notes)
    save(sess)
    return sess


def generate(pdf_path: str | Path, workdir: str | Path, size: config.PosterSize, *,
             condition: str = "joint", n_sections: tuple[int, int] = (5, 7),
             use_reviewer: bool = True, budget_s: float | None = config.TOTAL_BUDGET_S,
             progress: Progress = _noop) -> Session:
    """PDF in, revised poster out (CLI and UI entry point)."""
    workdir = Path(workdir)
    draft = prepare(pdf_path, workdir, n_sections=n_sections, progress=progress)
    progress(0.52, "Layout init and first review...")
    sess = initial_session(draft, size, workdir, use_reviewer=use_reviewer)
    sess.trace.calls[:0] = draft.trace.calls
    sess.trace.events[:0] = draft.trace.events
    run_schedule(sess, condition, use_reviewer=use_reviewer, budget_s=budget_s, progress=progress)
    sess.export()
    progress(1.0, "Done")
    return sess


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def save(sess: Session, history: list | None = None) -> Path:
    """session.json + trace.json in the session's workdir."""
    data = {
        "size": [sess.size.w_in, sess.size.h_in, sess.size.scale],
        "condition": sess.condition, "layout_frozen": sess.layout_frozen,
        "artifact": sess.artifact,
        "claims": [c.to_json() for c in sess.claims], "claim_failures": sess.claim_failures,
        "asset_problems": sess.asset_problems,
        "outline": {"title": sess.outline.title, "authors": sess.outline.authors,
                    "sections": [asdict(s) for s in sess.outline.sections]},
        "paper": {k: v for k, v in asdict(sess.paper).items() if k != "markdown"},
        "root": dump(sess.root),
        "protected": [asdict(p) for p in sess.protected],
        "word_limits": sess.word_limits, "min_width": sess.min_width,
        "rounds": sess.rounds, "history": history or [],
    }
    path = sess.workdir / "session.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    (sess.workdir / "trace.json").write_text(json.dumps(
        {"model": llm.backend_name(), "usage": sess.trace.usage(),
         "calls": [asdict(c) for c in sess.trace.calls], "events": sess.trace.events},
        indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def load_session(workdir: str | Path) -> Session:
    workdir = Path(workdir)
    d = json.loads((workdir / "session.json").read_text(encoding="utf-8"))
    pp = dict(d["paper"])
    for k in ("figures_info", "tables_info"):
        pp[k] = {n: AssetInfo(**a) for n, a in pp[k].items()}
    paper = ParsedPaper(markdown="", **pp)
    outline = Outline(d["outline"]["title"], d["outline"]["authors"], [
        OutlineSection(**{**s, "assets": [AssetInfo(**a) for a in s["assets"]]})
        for s in d["outline"]["sections"]])
    w, h, scale = d["size"]
    sess = Session(
        workdir=workdir, size=config.PosterSize(w, h, scale), paper=paper, outline=outline,
        raw_nodes=[], root=load_tree(d["root"]), preview=None, trace=llm.Trace(),
        claims=[Claim.from_json(c) for c in d["claims"]], claim_failures=d["claim_failures"],
        asset_problems=d["asset_problems"], condition=d["condition"],
        layout_frozen=d["layout_frozen"],
        protected=[ProtectedSpan(**p) for p in d["protected"]],
        word_limits={k: int(v) for k, v in d["word_limits"].items()},
        min_width={k: float(v) for k, v in d["min_width"].items()},
        rounds=d["rounds"], artifact=d["artifact"],
    )
    sess.rerender()
    return sess
