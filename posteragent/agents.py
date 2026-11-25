"""The agents. Prompts adapted from PosterForest's published ones.

Deliberate deviations from the paper, each noted at its call site:
  * the summarizing step consumes the Raw Doc Tree and keeps `assets`,
    instead of re-parsing the markdown and flattening to depth 2;
  * the feedback agent is replaced by a reviewer that names the affected
    panel, the unmet claim or readability requirement and the proposed
    change, and judges every frozen claim (claims.py) on the rendered image;
  * the content agent works under hard guards: required condition phrases
    and numbers survive, or the edit is rejected; it can ask for space;
  * `global_feedback` is threaded into the content and layout prompts, which
    as published generate that text and then discard it.
See docs/posterforest-notes.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from PIL import Image

from . import claims as claims_mod
from . import config, llm
from .claims import Claim
from .layout_init import Outline, OutlineSection
from .parse import ParsedPaper
from .tree import Bullet, ContentLeaf, Node, SplitNode, leaves, valid_directions

_MD_CAP = 42000  # chars of paper markdown sent to the parser agent
# Per-pass caps on the content agent's length change, so the volume-ratio
# loop converges across T_max rounds instead of overshooting and oscillating.
_MAX_EXPAND_PCT = 140.0
_MAX_CONDENSE_PCT = 55.0


# ==========================================================================
# Parser agent -> Raw Doc Tree
# ==========================================================================
PARSER_SYS = (
    "You are the Parser Agent for scientific posters. You convert a parsed "
    "paper into a hierarchical document tree. You reply with JSON only."
)

PARSER_PROMPT = """\
## TASK
Build a hierarchical tree of this paper.

## RULES
1. Preserve Content: copy the paper's own wording into `content`, do not
   summarise -- that happens in a later stage. Cap each node's `content` at
   about 900 characters; if a section is longer, keep its most substantive
   passages and drop filler.
2. Caption-Guided Strategy: use the figure/table captions to decide where
   sections begin. Group text around the figures and tables whose captions
   relate to it. Map each passage to the section most relevant to the nearby
   figures or tables.
3. Logical flow: order sections as the paper presents them
   (introduction -> method -> results).
4. Assets are REFERENCES, not nodes. Put figure/table ids in `assets`.
   Use the exact ids from the inventory below. Every id should appear in
   exactly one node.
5. Depth: up to 3 levels (section / subsection / subsubsection). Use `[]`
   for `children` at a leaf.
6. Skip references, acknowledgments and appendices.

## OUTPUT (JSON only)
{{"tree": {{"id": "root", "title": "<paper title>", "type": "title",
  "content": "<authors and abstract>",
  "assets": {{"figures": [], "tables": [], "references": []}},
  "children": [
    {{"id": "1", "title": "Introduction", "type": "section",
     "content": "<full text of this section>",
     "assets": {{"figures": ["1"], "tables": [], "references": []}},
     "children": []}}
  ]}}}}

## ASSET INVENTORY
{inventory}

## PAPER
{markdown}
"""


def parser_agent(paper: ParsedPaper, trace: llm.Trace) -> dict:
    md = paper.markdown
    if len(md) > _MD_CAP:
        md = md[:_MD_CAP] + "\n\n[truncated]"
    out = llm.call(
        "parser",
        PARSER_PROMPT.format(inventory=paper.inventory_text(), markdown=md),
        system=PARSER_SYS,
        max_tokens=9000,
        trace=trace,
        context={"markdown": md, "paper": paper},
    )
    # Models drop the wrapper often enough that insisting on it is not worth a
    # retry; accept a bare root node too.
    tree = None
    if isinstance(out, dict):
        tree = out.get("tree")
        if not isinstance(tree, dict) and ("children" in out or "title" in out):
            tree = out
    if not isinstance(tree, dict):
        raise ValueError("parser agent did not return a document tree")
    return tree


def _flatten_raw(tree: dict) -> list[dict]:
    """Depth-first list of nodes with their asset bindings."""
    out: list[dict] = []

    def rec(n: dict, depth: int) -> None:
        if not isinstance(n, dict):
            return
        assets = n.get("assets") or {}
        out.append(
            {
                "id": str(n.get("id", "")),
                "title": str(n.get("title", "")),
                "type": str(n.get("type", "section")),
                "depth": depth,
                "content": str(n.get("content", "")),
                "figures": [str(x) for x in (assets.get("figures") or [])],
                "tables": [str(x) for x in (assets.get("tables") or [])],
            }
        )
        for c in n.get("children") or []:
            rec(c, depth + 1)

    rec(tree, 0)
    return out


def audit_assets(raw_nodes: list[dict], paper: ParsedPaper) -> list[str]:
    """PosterForest's headline failure mode is assets going missing or being
    mismatched, with no recovery downstream. Make it loud instead of silent."""
    problems: list[str] = []
    for kind, pool in (("figure", paper.figures_info), ("table", paper.tables_info)):
        key = "figures" if kind == "figure" else "tables"
        seen: dict[str, list[str]] = {}
        for n in raw_nodes:
            for a in n[key]:
                seen.setdefault(a, []).append(n["id"])
        for a, owners in seen.items():
            if a not in pool:
                problems.append(f"{kind} {a} referenced by node(s) {owners} but never extracted")
            elif len(owners) > 1:
                problems.append(f"{kind} {a} claimed by multiple nodes {owners}")
        for a in pool:
            if a not in seen:
                problems.append(f"{kind} {a} extracted but no node claims it")
    return problems


# ==========================================================================
# Summarizing agent -> poster outline
# ==========================================================================
SELECT_SYS = (
    "You are the Summarizing Agent for scientific posters. You prune, merge "
    "and summarise a document tree into poster sections. You reply with JSON only."
)

SELECT_PROMPT = """\
## TASK
Turn this document tree into a poster outline of {lo}-{hi} sections.

## RULES
1. A poster is not a short paper. A reader must get the research question,
   the method, and the key findings without reading the paper.
2. Prune: drop nodes a poster does not need. Merge: combine nodes that belong
   on one panel. Summarise: rewrite the kept content as short bullets.
3. Bullets: 2-5 per section, each ONE sentence under 22 words. No markdown,
   no bold, no citation brackets.
   Use ONLY facts present in the tree's `content`. Every number you write must
   appear verbatim there. Do NOT invent percentages, improvements, dataset
   sizes or baseline names -- a qualitative bullet is always better than a
   fabricated statistic.
4. Keep each figure/table id from the tree attached to the section whose
   content explains it. Use an id at most once. Omit ids that do not earn
   poster space.
5. `importance` in 0.5-2.0: how much poster area this section deserves.
   A key results table earns more than a teaser.
6. `source_ids` lists the tree node ids you drew from, so later revisions can
   go back to the paper.
7. Total poster text should be roughly 600-850 words.
8. REQUIRED CLAIMS below must all be communicated. Assign each claim id to
   exactly one section via `claims`, state the claim in that section's
   bullets, and include EVERY one of its `conditions` phrases word for word in
   that section's bullets. If a claim names an asset, attach that asset to the
   same section. These requirements are checked automatically.

## REQUIRED CLAIMS
{claims}

## OUTPUT (JSON only)
{{"sections": [
  {{"id": "1", "title": "<short panel heading, <= 4 words>",
    "bullets": ["...", "..."],
    "figures": ["2"], "tables": [],
    "importance": 1.2, "source_ids": ["1", "1.1"], "claims": ["f1"]}}
]}}

## DOCUMENT TREE
{tree}
"""


def select_outline(
    raw_nodes: list[dict],
    paper: ParsedPaper,
    trace: llm.Trace,
    n_lo: int = 5,
    n_hi: int = 7,
    claims: list[Claim] | None = None,
) -> Outline:
    """Node-wise selection over the Raw Doc Tree.

    The paper's own Summarizing prompt consumes the markdown again, flattens
    to two levels and drops `assets` -- which disables the hierarchy its
    ablation says is essential. We consume the tree and keep the bindings.
    """
    compact = [
        {
            "id": n["id"],
            "title": n["title"],
            "type": n["type"],
            "figures": n["figures"],
            "tables": n["tables"],
            "content": n["content"][:1400],
        }
        for n in raw_nodes
    ]
    out = llm.call(
        "summarize",
        SELECT_PROMPT.format(
            lo=n_lo, hi=n_hi, tree=json.dumps(compact, ensure_ascii=False),
            claims=claims_mod.prompt_block(claims or []),
        ),
        system=SELECT_SYS,
        max_tokens=5000,
        trace=trace,
        context={"nodes": compact, "claims": claims or [], "lo": n_lo, "hi": n_hi},
    )
    raw_secs = (out or {}).get("sections") or []
    by_id = {n["id"]: n for n in raw_nodes}

    used: set[tuple[str, str]] = set()
    sections: list[OutlineSection] = []
    claim_home: dict[str, str] = {}  # claim id -> outline section id
    for i, s in enumerate(raw_secs):
        if not isinstance(s, dict):
            continue
        bullets = [str(b).strip() for b in (s.get("bullets") or []) if str(b).strip()]
        assets = []
        for kind, key in (("figure", "figures"), ("table", "tables")):
            for aid in s.get(key) or []:
                aid = str(aid)
                if (kind, aid) in used:
                    continue
                a = paper.asset(kind, aid)
                if a:
                    assets.append(a)
                    used.add((kind, aid))
        src = [str(x) for x in (s.get("source_ids") or [])]
        source_text = "\n\n".join(
            by_id[x]["content"] for x in src if x in by_id
        )[:6000]
        try:
            imp = float(s.get("importance", 1.0))
        except (TypeError, ValueError):
            imp = 1.0
        # Section ids must be unique: they key claims and source text.
        sid = str(s.get("id") or i + 1)
        if any(x.id == sid for x in sections):
            sid = f"{sid}#{i + 1}"
        for cid in s.get("claims") or []:
            claim_home.setdefault(str(cid), sid)
        sections.append(
            OutlineSection(
                id=sid,
                title=str(s.get("title") or f"Section {i + 1}")[:48],
                bullets=bullets,
                assets=assets,
                importance=imp,
                source_ids=src,
                source_text=source_text,
            )
        )

    if not sections:
        raise ValueError("summarizing agent returned no sections")
    outline = Outline(title=paper.title, authors=paper.authors, sections=sections)
    _bind_claims(outline, claims or [], claim_home, paper, trace)
    return outline


def _bind_claims(outline: Outline, claims: list[Claim], home: dict[str, str],
                 paper: ParsedPaper, trace: llm.Trace) -> None:
    """Attach each claim to one section and make sure its asset is there.

    Binding is fixed from here on. A claim the summarizer forgot goes to the
    section already sharing most of its vocabulary; its missing conditions
    then surface as review issues rather than being silently dropped.
    """
    by_id = {s.id: s for s in outline.sections}
    for c in claims:
        sec = by_id.get(home.get(c.id, ""))
        if sec is None:
            words = set(claims_mod.norm(c.quote).split())
            sec = max(
                outline.sections,
                key=lambda s: len(words & set(claims_mod.norm(
                    s.title + " " + " ".join(s.bullets) + " " + s.source_text).split())),
            )
            trace.log(f"claim {c.id}: summarizer did not place it; bound to '{sec.title}'")
        c.section_id = sec.id
        # The claim's own passage is always available to the content agent.
        if c.quote not in sec.source_text:
            sec.source_text = (c.quote + "\n\n" + sec.source_text)[:6000]
        if c.asset:
            kind, _, aid = c.asset.partition(":")
            asset = paper.asset(kind, aid)
            owner = next((s for s in outline.sections
                          if any(a.kind == kind and a.asset_id == aid for a in s.assets)), None)
            if owner is None and asset:
                sec.assets.append(asset)
                trace.log(f"claim {c.id}: attached missing {kind} {aid} to '{sec.title}'")
            elif owner is not None and owner is not sec and asset:
                owner.assets = [a for a in owner.assets
                                if not (a.kind == kind and a.asset_id == aid)]
                sec.assets.append(asset)
                trace.log(f"claim {c.id}: moved {kind} {aid} from '{owner.title}' to '{sec.title}'")


# ==========================================================================
# Content agent: expand / condense, routed on the measured volume ratio
# ==========================================================================
CONTENT_SYS = (
    "You are the Content Agent for scientific posters. You adjust the text of "
    "one poster panel to fit its space. You reply with a JSON array only."
)

_SCHEMA_BLOCK = """\
## OUTPUT SCHEMA (mandatory)
Return ONLY this JSON object:
  {"bullets": [ {"alignment": "left", "bullet": true, "level": 0,
                 "runs": [{"text": "..."}]} ],
   "needs_space": false, "space_reason": ""}
Every bullet object must have exactly those four keys. Do NOT output plain
strings in place of bullet objects, nested arrays, or any other shape.
Every string must be double-quoted. No prose, no code fences."""

EXPAND_PROMPT = """\
## GOAL
`current_volume_ratio` is the panel's current fill percentage: {ratio}%.
Target is 100%, never above. Expand so the result is about {delta}% longer
than `previous_response`.

## HOW
- Add gradually, most important points first. Do not dump every detail.
- Split an existing bullet into smaller clearer ones; add concise sub-bullets
  (level 1) with details from `current_content`.
- Include key concepts, mechanisms, assumptions, limitations and results.
- Use ONLY facts that appear in `current_content`. Never invent a number,
  percentage, dataset size or baseline name. If `current_content` has no
  numbers, write qualitative bullets instead of inventing figures.
- Avoid repeating `sibling_contents` unless you add a new perspective.
- Keep every REQUIRED phrase below word for word.
- Stop once you estimate you have reached the target. If still under 95%,
  leave the rest for a later pass.

## REQUIRED PHRASES (must appear word for word)
{required}
{feedback}
{schema}

## previous_response (the current bullets, same shape as your output)
{previous}

## current_content (source passages for this panel)
{content}

## sibling_contents (other panels -- do not duplicate these)
{siblings}
"""

CONDENSE_PROMPT = """\
## GOAL
`current_volume_ratio` is the panel's current fill percentage: {ratio}%.
It overflows. Condense so the result is about {delta}% shorter than
`previous_response`.

## HOW
- Merge bullets into fewer ones; keep every sentence short and crisp.
- Remove overlap with `sibling_contents`: drop background or definitions that
  are already covered there.
- Structural compression: keep only the most essential contributions, numbers
  and distinctions. Fold sub-bullets into their parent unless critical.
- Never introduce a fact or number that is not already in `previous_response`.
- First remove repetition and optional background. NEVER remove or reword the
  REQUIRED phrases below; the poster fails review without them.
- If the required content cannot fit even after that, still return your
  shortest complete version in `bullets` (never an empty list), and set
  `needs_space` to true with a one-line `space_reason`: the layout agent will
  look for room elsewhere.
- Stop once you estimate you have reached the target. If still above 105%,
  leave deeper cuts for a later pass.

## REQUIRED PHRASES (must appear word for word)
{required}
{feedback}
{schema}

## previous_response (the current bullets, same shape as your output)
{previous}

## sibling_contents (other panels -- drop anything duplicated there)
{siblings}
"""

REPAIR_PROMPT = """\
## GOAL
This poster panel fails a content requirement. Fix exactly these problems and
change as little else as possible.

## PROBLEMS
{problems}

## RULES
- Every REQUIRED phrase below must appear word for word.
- Use ONLY facts from `current_content` (the paper's own passages). Delete
  any number the problems list names as unsupported.
- Keep the panel about the same length: drop repetition to make room. The
  panel is {ratio}% full; if it cannot fit, still return the full revised
  `bullets` and set `needs_space` to true with a one-line `space_reason`.

## REQUIRED PHRASES (must appear word for word)
{required}
{feedback}
{schema}

## previous_response (the current bullets, same shape as your output)
{previous}

## current_content (source passages for this panel)
{content}

## sibling_contents (other panels -- do not duplicate these)
{siblings}
"""


_BULLET_KEYS = ("bullets", "result", "output", "panel", "content", "items", "text", "texts", "points")


def _bulletish(v) -> bool:
    return isinstance(v, (str, dict)) and bool(
        v.strip() if isinstance(v, str) else (v.get("runs") or v.get("text")))


def _as_bullet_list(out, _depth: int = 0) -> list:
    """Find the bullet list in whatever shape the model returned.

    Accepted: a bare list; {"bullets": [...]} or one of a few synonyms; the
    same nested one level down ({"panel": {"bullets": [...]}}); a single
    string, split into one bullet per line. Items may be bullet objects,
    {"text": ...} or plain strings. Anything else is [] and the caller
    records the raw output.
    """
    if isinstance(out, list):
        if out and all(isinstance(x, list) for x in out):  # [[...]] wrapper
            out = [y for x in out for y in x]
        return [x for x in out if _bulletish(x)]
    if isinstance(out, str):
        lines = [ln.strip().lstrip("-•*– ").strip() for ln in out.splitlines()]
        return [ln for ln in lines if ln]
    if isinstance(out, dict) and _depth < 2:
        for key in _BULLET_KEYS:
            if key in out:
                got = _as_bullet_list(out[key], _depth + 1)
                if got:
                    return got
        for v in out.values():  # unknown wrapper key
            if isinstance(v, (dict, list)):
                got = _as_bullet_list(v, _depth + 1)
                if got:
                    return got
    return []


def _to_bullets(items: list) -> list[Bullet]:
    bullets = [Bullet.from_json(b if isinstance(b, dict) else {"text": str(b)}) for b in items]
    return [b for b in bullets if b.text.strip()]


def _snippet(out) -> str:
    try:
        text = json.dumps(out, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(out)
    return text[:400]


_UNUSABLE = """

## YOUR PREVIOUS REPLY COULD NOT BE USED
It did not contain a non-empty `bullets` array. It began:
{snippet}
Reply again with ONLY the JSON object from OUTPUT SCHEMA. `bullets` must hold
the complete revised panel text (never empty), even when you set
`needs_space` to true.
"""


@dataclass
class ContentEdit:
    leaf_id: str
    mode: str  # "expand" | "condense" | "repair"
    before_ratio: float
    bullets: list[Bullet]
    needs_space: bool = False
    space_reason: str = ""
    rejected: str = ""  # why a proposed edit was discarded by the guards


def _siblings_text(root: Node, leaf: ContentLeaf, limit: int = 1200) -> str:
    parts = []
    for other in leaves(root):
        if other.id == leaf.id or other.content_type != "text":
            continue
        txt = " / ".join(b.text for b in other.bullets)
        if txt:
            parts.append(txt)
    return "\n".join(parts)[:limit] or "(none)"


def content_agent(
    root: Node,
    leaf: ContentLeaf,
    source_text: str,
    trace: llm.Trace,
    global_feedback: str = "",
    *,
    required: list[str] | None = None,
    problems: list[str] | None = None,
    protected: list[str] | None = None,
    known_numbers: set[str] | None = None,
    allow_expand: bool = True,
    word_limit: int | None = None,
) -> ContentEdit | None:
    """Expand, condense or repair one panel. None when nothing needs doing.

    `required` are the frozen condition phrases this panel must carry. An
    edit that loses one that was present, or introduces a number the paper
    does not contain, is rejected and the previous text kept: the agent
    cannot make a draft pass by weakening its content requirements.

    The 90-100% no-op band is the real cost saver: after the first pass most
    panels fall inside it. The floor is 90 rather than PosterForest's 80
    because they name poor content density as a limitation.
    """
    required = required or []
    problems = problems or []
    protected = protected or []
    ratio = leaf.measured.volume_ratio
    n_words = len(leaf.text.split())
    over_limit = word_limit is not None and n_words > word_limit
    if problems:
        mode = "repair"
    elif ratio > config.VOLUME_TARGET_HI or over_limit:
        mode = "condense"
    elif ratio < config.VOLUME_TARGET_LO and allow_expand:
        mode = "expand"
    else:
        return None
    if mode == "expand" and word_limit is not None:
        return None  # a user-imposed limit outranks filling the panel

    # The paper phrases the target as "(100 - current_volume_ratio)% longer",
    # which is wrong: going from 27% fill to 100% needs ~270% more text, not
    # 73%. The length change is multiplicative in 100/ratio. Capped per pass so
    # it converges instead of oscillating -- the prompt's "leave the rest for a
    # later pass" only works if the asked-for step is reachable.
    r = max(1.0, min(ratio, 400.0))
    if mode == "expand":
        delta = int(round(min(_MAX_EXPAND_PCT, (100.0 / r - 1.0) * 100.0)))
    elif mode == "condense":
        delta = int(round(min(_MAX_CONDENSE_PCT, (1.0 - 100.0 / r) * 100.0)))
    else:
        delta = 0
    if over_limit:
        delta = max(delta, int(round(100.0 * (1 - word_limit / n_words))))
    if mode != "repair" and delta < 5:
        return None

    fb = (
        f"\n## GLOBAL FEEDBACK on the whole poster (address it if it applies here)\n{global_feedback}"
        if global_feedback
        else ""
    )
    if protected:
        fb += ("\n## PROTECTED TEXT (must stay character for character)\n"
               + "\n".join(f'- "{p}"' for p in protected))
    if word_limit is not None:
        fb += f"\n## LENGTH LIMIT\nAt most {word_limit} words in total."
    previous = json.dumps([b.to_json() for b in leaf.bullets], ensure_ascii=False)
    tmpl = {"expand": EXPAND_PROMPT, "condense": CONDENSE_PROMPT, "repair": REPAIR_PROMPT}[mode]
    kwargs = dict(
        ratio=ratio,
        delta=delta,
        required="\n".join(f'- "{q}"' for q in required) or "(none)",
        feedback=fb,
        schema=_SCHEMA_BLOCK,
        previous=previous,
        siblings=_siblings_text(root, leaf),
    )
    if mode in ("expand", "repair"):
        # `current_content` is withheld from condense on purpose: the paper
        # enforces "source text is for expansion only" structurally.
        kwargs["content"] = (source_text or "(no source text available)")[:6000]
    if mode == "repair":
        kwargs["problems"] = "\n".join(f"- {p}" for p in problems)

    prompt = tmpl.format(**kwargs)
    ctx = {"mode": mode, "leaf": leaf, "ratio": ratio, "delta": delta,
           "required": required, "problems": problems, "protected": protected,
           "source": source_text, "word_limit": word_limit}
    needs_space, reason, bullets = False, "", []
    for attempt in range(2):
        out = llm.call(f"content:{mode}", prompt, system=CONTENT_SYS, max_tokens=3000,
                       trace=trace, context={**ctx, "attempt": attempt})
        if isinstance(out, dict):
            needs_space = needs_space or bool(out.get("needs_space"))
            reason = reason or str(out.get("space_reason", ""))[:200]
        bullets = _to_bullets(_as_bullet_list(out))
        if bullets:
            break
        # Keep the evidence: what the model sent, not just that it failed.
        trace.log(f"content:{mode} on {leaf.id}: unusable output (attempt {attempt + 1}): {_snippet(out)}")
        prompt = tmpl.format(**kwargs) + _UNUSABLE.format(snippet=_snippet(out))
    edit = ContentEdit(leaf.id, mode, ratio, bullets, needs_space, reason)
    if not bullets:
        trace.log(f"content:{mode} on {leaf.id}: still unusable after a corrective retry; keeping previous")
        if needs_space:  # the signal alone is still useful to the layout agent
            edit.bullets = list(leaf.bullets)
            edit.rejected = "no usable text"
            return edit
        return None

    before, after = leaf.text, "\n".join(b.text for b in bullets)
    lost = [q for q in required
            if claims_mod.contains(before, q) and not claims_mod.contains(after, q)]
    altered = [p for p in protected if p in before and p not in after]
    invented = []
    if known_numbers is not None:
        had = set(claims_mod.numbers(before))
        invented = [n for n in claims_mod.numbers(after) if n not in known_numbers and n not in had]
    if lost or altered or invented:
        edit.bullets = list(leaf.bullets)
        edit.rejected = "; ".join(
            ([f"lost required {lost!r}"] if lost else [])
            + ([f"changed protected text {altered[0][:50]!r}"] if altered else [])
            + ([f"introduced unsupported number(s) {invented!r}"] if invented else []))
        # Condensing that only works by dropping required content is exactly
        # the case where the panel needs room, not shorter text.
        edit.needs_space = edit.needs_space or (mode == "condense" and bool(lost))
        trace.log(f"content:{mode} on {leaf.id} rejected: {edit.rejected}")
    return edit


# ==========================================================================
# Layout agent: one bounded delta on one split, from an annotated crop
# ==========================================================================
LAYOUT_SYS = (
    "You are the Layout Agent for scientific posters. You judge figure "
    "legibility balance from an image. You reply with JSON only."
)

LAYOUT_PROMPT = """\
## TASK
Decide which panel to enlarge and by how much, based on figure legibility
balance within the red analysis box. Inside the red box, green boxes are the
two panels and blue boxes are figures.

## CONTEXT
- Split Type: {split_type}
- Allowed directions: {d0} or {d1}

## HOW TO JUDGE
- Charts and tables: judge by PERCEIVED TEXT SIZE (axis ticks, labels,
  legends, numbers).
- Diagrams, schematics and workflows: even with little text, judge by
  structural complexity (component count, branching, arrow and box density,
  nested sub-panels, tiny icons). Treat a complex diagram as if it contained
  implicit small text.
- A panel is "harder to read" if it has smaller text OR higher visual complexity.
- Do NOT optimise by raw figure size or panel area.
- Enlarge the harder-to-read panel. Do NOT enlarge a panel that is already
  comfortable to read.
- Shape Sanity: avoid creating an extremely tall and narrow panel.
- If the two panels are already balanced, or enlarging would distort shape,
  return delta 0.
{text_note}
{feedback}

## DELTA SCALE
0 = no adjustment, 1-2 = very subtle, 3-4 = moderate, 5 = maximum safe enlargement.

## OUTPUT (JSON only)
{{"direction": "{d0}" or "{d1}", "delta": <0-5>,
  "reason": "<legibility comparison and shape consideration, one sentence>"}}
"""


@dataclass
class LayoutEdit:
    split_id: str
    direction: str
    delta: float
    reason: str
    old_ratio: float = 0.0
    new_ratio: float = 0.0


def layout_agent(
    node: SplitNode,
    overlay: Image.Image,
    trace: llm.Trace,
    global_feedback: str = "",
) -> LayoutEdit | None:
    d0, d1 = valid_directions(node.split_type)
    has_fig = any(l.content_type in ("figure", "table") for l in leaves(node))
    # The published prompt judges figure legibility only, so its behaviour on
    # a figure-free split is undefined. Extend the criterion explicitly.
    text_note = (
        ""
        if has_fig
        else "- There are no figures here. Judge instead by text density: "
        "enlarge the panel whose text is more cramped or is being truncated."
    )
    fb = f"\n## GLOBAL FEEDBACK on the whole poster\n{global_feedback}" if global_feedback else ""

    out = llm.call(
        "layout",
        LAYOUT_PROMPT.format(
            split_type=node.split_type, d0=d0, d1=d1, text_note=text_note, feedback=fb
        ),
        system=LAYOUT_SYS,
        images=[overlay],
        max_tokens=600,
        trace=trace,
        context={"node": node, "directions": (d0, d1), "has_figure": has_fig},
    )
    if not isinstance(out, dict):
        return None
    direction = str(out.get("direction", "")).strip().upper()
    if direction not in (d0, d1):
        trace.log(f"layout on {node.id}: bad direction {direction!r}, ignored")
        return None
    try:
        delta = float(out.get("delta", 0))
    except (TypeError, ValueError):
        return None
    if delta <= 0:
        return None
    return LayoutEdit(node.id, direction, delta, str(out.get("reason", ""))[:300])


# ==========================================================================
# Review agent: inspects the rendered poster against the frozen claims
# ==========================================================================
REVIEW_SYS = (
    "You review a rendered scientific poster against its source passages. "
    "You are strict about scientific meaning. You reply with JSON only."
)

REVIEW_PROMPT = """\
## TASK
Inspect the rendered poster image alongside the panel inventory and the
claims it must communicate. Report concrete, fixable problems.

## CLAIMS (frozen; quote is the paper's own passage)
{claims}

## PANELS (id, section, type, current text or caption)
{panels}

## AUTOMATIC CHECKS ALREADY FOUND
{known}

## LOOK FOR
- unreadable elements: figure labels or text too small to read, crowding,
  truncated text;
- missing explanations: a claim not actually communicated, or communicated
  without the conditions needed to interpret it;
- mismatches: a figure beside text about something else, or text that
  misstates its figure or source passage.
Do not repeat the automatic checks. Do not ask for stylistic changes.

## OUTPUT (JSON only)
{{"issues": [
   {{"element": "<panel id>", "requirement": "<which claim id or 'readability'>",
     "problem": "<what is wrong>",
     "change": "repair" | "condense" | "expand" | "more_space"}}],
  "verdicts": {{"<claim id>": true | false}},
  "equivalents": {{"<claim id>": {{"<condition phrase>": "<exact visible wording that means the same>"}}}}}}
`verdicts` must cover every claim: true only if the poster states the claim
faithfully to its quote. Use `equivalents` only for a condition that is
present in different words; numbers must be identical.
"""

_CHANGES = ("repair", "condense", "expand", "more_space")


@dataclass
class VisualReview:
    issues: list[dict]
    verdicts: dict[str, bool]
    equivalents: dict[str, dict]
    error: str = ""


def review_agent(preview: Image.Image, panels: list[dict], claims: list[Claim],
                 known: list[str], trace: llm.Trace) -> VisualReview:
    """The only truly global, multimodal checkpoint in the pipeline."""
    panel_lines = "\n".join(
        f"- {p['id']} [{p['section']}] {p['type']}: {p['text'][:420]}" for p in panels)
    out = llm.call(
        "review",
        REVIEW_PROMPT.format(
            claims=claims_mod.prompt_block(claims),
            panels=panel_lines,
            known="\n".join(f"- {k}" for k in known) or "(none)",
        ),
        system=REVIEW_SYS, images=[preview], max_tokens=2000, trace=trace,
        context={"panels": panels, "claims": claims, "known": known},
    )
    if not isinstance(out, dict):
        raise ValueError("review agent did not return an object")
    ids = {p["id"] for p in panels}
    issues = []
    for it in out.get("issues") or []:
        if not isinstance(it, dict) or str(it.get("element")) not in ids:
            continue
        change = str(it.get("change", "")).strip().lower()
        issues.append({"element": str(it["element"]),
                       "requirement": str(it.get("requirement", "readability"))[:60],
                       "problem": str(it.get("problem", ""))[:300],
                       "change": change if change in _CHANGES else "repair"})
    verdicts = {}
    raw_v = out.get("verdicts") if isinstance(out.get("verdicts"), dict) else {}
    for c in claims:
        v = raw_v.get(c.id)
        if isinstance(v, bool):
            verdicts[c.id] = v
    raw_e = out.get("equivalents") if isinstance(out.get("equivalents"), dict) else {}
    equivalents = {k: v for k, v in raw_e.items() if isinstance(v, dict)}
    return VisualReview(issues, verdicts, equivalents)
