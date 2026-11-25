"""Multi-turn dialogue revision under the proposal's two conditions.

    joint         the requested change, then the same content-layout feedback
                  as generation: space can come from anywhere on the poster,
                  and newly crowded panels anywhere can be condensed;
    layout-first  the requested change only: a figure can grow only at the
                  expense of the panel it shares a split with, and only the
                  target's own section may be re-worded. Everything else is
                  frozen.

Both use the same intent parsing, protection checks, attempt limit and
review. "Both may apply the requested size change; only the joint condition
may redistribute space elsewhere" (proposal s5.1).

Carried across turns (in the Session, so reviews enforce them): protected
spans with the turn that declared them, word limits, requested figure widths;
plus the edit history and open conflicts here. A request that conflicts with
an earlier constraint is reported and not applied. An edit that fails review
twice is rolled back with the reason.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import agents, config, llm, render
from .allocate import SpaceRequest
from .pipeline import ProtectedSpan, Session, content_pass, collect_requests, save, serve_requests
from .review import review
from .tree import (
    Bullet,
    ContentLeaf,
    SemanticNode,
    SplitNode,
    clone,
    find,
    leaves,
    path_to,
    semantics,
    text_map,
)

CONDITIONS = ("joint", "layout-first")


@dataclass
class TurnRecord:
    turn: int
    request: str
    condition: str
    status: str  # applied | rejected | reported
    message: str
    op: str = ""
    target: str = ""  # leaf id
    target_section: str = ""  # section title, for later references
    protected_sections: list[str] = field(default_factory=list)  # section ids
    conflict: bool = False
    notes: list[str] = field(default_factory=list)
    attempts: int = 0
    artifact: str = ""  # accepted deck after this turn
    before: str = ""  # accepted deck before this turn
    issues: list[str] = field(default_factory=list)  # blocking kinds at the end

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class DialogueState:
    session: Session
    history: list[TurnRecord] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def turn(self) -> int:
        return len(self.history)

    @property
    def condition(self) -> str:
        return self.session.condition


def start(sess: Session, condition: str) -> DialogueState:
    if condition not in CONDITIONS:
        raise ValueError(f"dialogue condition must be one of {CONDITIONS}")
    sess.condition = condition
    sess.layout_frozen = condition == "layout-first"
    return DialogueState(sess)


# --------------------------------------------------------------------------
# intent
# --------------------------------------------------------------------------
INTENT_SYS = (
    "You interpret a user's poster revision request against the current poster "
    "structure and the conversation so far. You reply with JSON only."
)

INTENT_PROMPT = """\
## TASK
Resolve this revision request to a concrete target and operation.

## REQUEST (turn {turn})
{request}

## CURRENT POSTER (sections and their panels)
{structure}

## CONSTRAINTS DECLARED IN EARLIER TURNS (still in force)
{constraints}

## EARLIER TURNS
{history}

## RULES
1. Resolve the target from the request itself, or from an earlier turn when
   the request refers back ("that figure", "it", "the text you kept",
   "the protected section"). Use the ids shown above.
2. `op` is one of: "enlarge_asset", "shrink_asset", "shorten_text",
   "expand_text", "rewrite_text", "protect" (only protecting, no edit),
   "unsupported".
3. `scale`: for asset ops, the size factor (1.2 = 20% larger). `word_limit`:
   for shorten_text, the requested maximum. Only when the request names one.
4. `protect_section_ids`: sections whose text the user asks to keep
   unchanged IN THIS TURN. `new_protected`: other exact strings to keep,
   quoted verbatim from the poster.
5. Do not judge feasibility or conflicts; the system checks those.

## OUTPUT (JSON only)
{{"target_section_id": "<section id>", "target_leaf_id": "<panel id or null>",
  "op": "...", "word_limit": null, "scale": null,
  "protect_section_ids": [], "new_protected": [],
  "restated": "<one-line restatement of what you will do>"}}
"""

EDIT_SYS = "You edit the text of one poster panel. You reply with JSON only."

EDIT_PROMPT = """\
## TASK
{instruction}

## HARD CONSTRAINTS
- These exact strings must appear character for character if they are in the
  panel now. Never reword, split or delete them:
{protected}
- These phrases are required to interpret the paper's claims; keep each one
  word for word:
{required}
- Use only facts from the current bullets or the source passages. Never
  introduce a number that is not in them.
{limit}
{feedback}
## OUTPUT SCHEMA (mandatory)
Return ONLY this JSON object:
  {{"bullets": [ {{"alignment": "left", "bullet": true, "level": 0,
                 "runs": [{{"text": "..."}}]}} ]}}

## CURRENT BULLETS
{previous}

## SOURCE PASSAGES FROM THE PAPER
{source}
"""


def _structure(sess: Session) -> tuple[str, list[dict]]:
    lines, sections = [], []
    for sec in semantics(sess.root):
        entry = {"id": sec.id, "title": sec.title, "leaves": []}
        lines.append(f'section id={sec.id} title="{sec.title}"')
        for leaf in leaves(sec):
            if leaf.content_type == "text":
                lines.append(f"  panel id={leaf.id} type=text words={len(leaf.text.split())} "
                             f"fill={leaf.measured.volume_ratio:.0f}%")
                lines += [f'    - "{b.text[:150]}"' for b in leaf.bullets]
                entry["leaves"].append({"id": leaf.id, "type": "text", "asset": None})
            elif leaf.content_type in ("figure", "table"):
                w = render.asset_width_in(leaf, sess.size)
                lines.append(f"  panel id={leaf.id} type={leaf.content_type} asset={leaf.asset_id} "
                             f'width={w:.1f}in caption="{leaf.caption[:80]}"')
                entry["leaves"].append({"id": leaf.id, "type": leaf.content_type,
                                        "asset": leaf.asset_id})
        sections.append(entry)
    return "\n".join(lines), sections


def _constraints(sess: Session) -> str:
    out = [f'- turn {p.turn}: keep unchanged "{p.text[:110]}"' for p in sess.protected]
    out += [f"- panel {k}: at most {v} words" for k, v in sess.word_limits.items()]
    out += [f"- panel {k}: displayed width at least {v:.2f} in" for k, v in sess.min_width.items()]
    return "\n".join(out) or "(none)"


def parse_intent(state: DialogueState, request: str) -> dict:
    sess = state.session
    structure, sections = _structure(sess)
    history = [{"turn": h.turn, "request": h.request, "status": h.status, "op": h.op,
                "target": h.target, "target_section": h.target_section,
                "protected_sections": h.protected_sections} for h in state.history]
    out = llm.call(
        "dialogue:intent",
        INTENT_PROMPT.format(turn=state.turn + 1, request=request, structure=structure,
                             constraints=_constraints(sess),
                             history=json.dumps(history, ensure_ascii=False) if history else "(none)"),
        system=INTENT_SYS, max_tokens=1000, trace=sess.trace,
        context={"request": request, "sections": sections, "history": history},
    )
    return out if isinstance(out, dict) else {}


def _edit_text(sess: Session, leaf: ContentLeaf, instruction: str, word_limit: int | None,
               feedback: list[str]) -> list[Bullet] | None:
    protected = [p.text for p in sess.protected if p.text in leaf.text]
    required = sess.required_for(leaf)
    out = llm.call(
        "dialogue:edit",
        EDIT_PROMPT.format(
            instruction=instruction,
            protected="\n".join(f'  - "{p}"' for p in protected) or "  (none)",
            required="\n".join(f'  - "{q}"' for q in required) or "  (none)",
            limit=f"- The whole panel must be at most {word_limit} words." if word_limit else "",
            feedback=("## THE PREVIOUS ATTEMPT FAILED REVIEW\n" + "\n".join(f"- {f}" for f in feedback)
                      + "\n") if feedback else "",
            previous=json.dumps([b.to_json() for b in leaf.bullets], ensure_ascii=False),
            source=(sess.source_text_for(leaf) or "(unavailable)")[:4000],
        ),
        system=EDIT_SYS, max_tokens=2500, trace=sess.trace,
        context={"leaf": leaf, "word_limit": word_limit, "protected": protected,
                 "required": required, "instruction": instruction},
    )
    bullets = agents._to_bullets(agents._as_bullet_list(out))
    if not bullets:
        sess.trace.log(f"dialogue:edit on {leaf.id}: unusable output: {agents._snippet(out)}")
    return bullets or None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _section_of(sess: Session, sec_id: str) -> SemanticNode | None:
    n = find(sess.root, sec_id) if sec_id else None
    return n if isinstance(n, SemanticNode) else None


def _section_containing(sess: Session, leaf_id: str) -> SemanticNode | None:
    for n in reversed(path_to(sess.root, leaf_id)):
        if isinstance(n, SemanticNode):
            return n
    return None


def _shrink(sess: Session, leaf: ContentLeaf, scale: float) -> str | None:
    """Give part of the asset's innermost split to its neighbour."""
    path = path_to(sess.root, leaf.id)
    for i in range(len(path) - 2, -1, -1):
        n = path[i]
        if isinstance(n, SplitNode) and not n.locked:
            side = 0 if n.children[0] is path[i + 1] else 1
            target = render.asset_width_in(leaf, sess.size) * scale
            old = n.ratio
            for step in (0.03, 0.06, 0.1, 0.15, 0.2):
                n.ratio = max(config.RATIO_MIN, min(config.RATIO_MAX, old + (-step if side == 0 else step)))
                sess.remeasure()
                if render.asset_width_in(leaf, sess.size) <= target + 0.01:
                    return f"split {n.id}: ratio {old:.2f}->{n.ratio:.2f}"
            n.ratio = old
            sess.remeasure()
            return None
    return None


def _reject(state: DialogueState, rec: TurnRecord, status: str, message: str) -> TurnRecord:
    rec.status, rec.message = status, message
    rec.artifact = rec.before
    state.history.append(rec)
    save(state.session, [h.to_json() for h in state.history])
    llm_trace(state, rec)
    return rec


def llm_trace(state: DialogueState, rec: TurnRecord) -> None:
    state.session.trace.log(f"[dialogue/{rec.condition}] turn {rec.turn} {rec.status}: "
                            f"{rec.request[:80]} -> {rec.message[:160]}")


# --------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------
def apply_request(state: DialogueState, request: str, extra_protected: str = "") -> TurnRecord:
    """One dialogue turn, applied to the previous accepted poster."""
    sess = state.session
    turn = state.turn + 1
    rec = TurnRecord(turn, request, state.condition, "rejected", "", before=sess.artifact)

    try:
        intent = parse_intent(state, request)
    except RuntimeError as e:
        return _reject(state, rec, "rejected", f"Could not interpret the request: {e}")
    op = str(intent.get("op") or "unsupported")
    rec.op = op
    restated = str(intent.get("restated", ""))[:200]

    # --- resolve the target ---------------------------------------------
    sec = _section_of(sess, str(intent.get("target_section_id") or ""))
    leaf_id = intent.get("target_leaf_id")
    leaf = find(sess.root, str(leaf_id)) if leaf_id else None
    if isinstance(leaf, ContentLeaf) and sec is None:
        sec = _section_containing(sess, leaf.id)
    want_asset = op in ("enlarge_asset", "shrink_asset")
    if not isinstance(leaf, ContentLeaf) or (leaf.content_type in ("figure", "table")) != want_asset:
        pool = leaves(sec) if sec is not None else []
        leaf = next((l for l in pool if (l.content_type in ("figure", "table")) == want_asset
                     and l.content_type != "title"), None)
    if leaf is not None:
        rec.target = leaf.id
        sec = sec or _section_containing(sess, leaf.id)
    rec.target_section = sec.title if sec is not None else ""

    # --- protections declared this turn (in force for this turn's edit) ---
    new_spans: list[str] = []
    for sid in intent.get("protect_section_ids") or []:
        s = _section_of(sess, str(sid))
        if s is None:
            continue
        rec.protected_sections.append(s.id)
        new_spans += [l.text for l in leaves(s) if l.content_type == "text" and l.text.strip()]
    text_now = "\n".join(l.text for l in leaves(sess.root))
    for span in list(intent.get("new_protected") or []) + [x.strip() for x in extra_protected.splitlines()]:
        if isinstance(span, str) and span.strip():
            if span.strip() in text_now:
                new_spans.append(span.strip())
            else:
                rec.notes.append(f"ignored protection for text not on the poster: {span[:60]!r}")
    if op == "protect" and leaf is not None and not new_spans and leaf.content_type == "text":
        new_spans.append(leaf.text)
    old_spans = [p.text for p in sess.protected]
    for span in new_spans:
        if span not in old_spans:
            sess.protected.append(ProtectedSpan(span, turn))
            old_spans.append(span)
    if new_spans:
        rec.notes.append(f"protected {len(new_spans)} span(s)")

    if op == "unsupported":
        return _reject(state, rec, "reported", "Not applied: this system resizes a figure or table, "
                       "shortens, expands or rewords a section, and protects text.")
    if op == "protect":
        if not new_spans:
            return _reject(state, rec, "reported", "Not applied: could not tell which text to protect.")
        rec.status, rec.message, rec.artifact = "applied", "Protection recorded.", sess.artifact
        state.history.append(rec)
        save(sess, [h.to_json() for h in state.history])
        llm_trace(state, rec)
        return rec
    if leaf is None:
        return _reject(state, rec, "reported", "Not applied: could not resolve which "
                       + ("figure or table" if want_asset else "section") + " you mean.")

    # --- conflicts with earlier constraints: report, never apply -----------
    spans_in = [p for p in sess.protected if p.text in leaf.text]
    limit = scale = None
    try:
        if intent.get("word_limit") is not None:
            limit = int(intent["word_limit"])
        if intent.get("scale") is not None:
            scale = float(intent["scale"])
    except (TypeError, ValueError):
        pass
    conflict = ""
    if op == "shorten_text":
        if not limit or limit < 1:
            return _reject(state, rec, "reported", "Not applied: give a word limit, e.g. 'to 30 words'.")
        kept = sum(len(p.text.split()) for p in spans_in)
        if kept > limit:
            p = min(spans_in, key=lambda p: p.turn)
            conflict = (f"shortening to {limit} words would change text protected in turn {p.turn} "
                        f"({kept} protected words)")
        required = sess.required_for(leaf)
        need = sum(len(q.split()) for q in required)
        if not conflict and need > limit:
            return _reject(state, rec, "reported",
                           f"Not applied: {limit} words cannot hold the {len(required)} phrase(s) this "
                           f"section needs to state its claim correctly ({need} words).")
    elif op in ("expand_text", "rewrite_text"):
        if spans_in and any(p.text == leaf.text for p in spans_in):
            p = spans_in[0]
            conflict = f"this section's text was protected in turn {p.turn}"
        elif op == "expand_text" and leaf.id in sess.word_limits:
            conflict = f"an earlier turn limited this section to {sess.word_limits[leaf.id]} words"
    elif op == "shrink_asset" and leaf.id in sess.min_width:
        conflict = f"an earlier turn asked for this {leaf.content_type} to be at least " \
                   f"{sess.min_width[leaf.id]:.1f} in wide"
    if want_asset and not (scale and scale > 0 and abs(scale - 1) > 0.01):
        scale = 1.2 if op == "enlarge_asset" else 0.85
        rec.notes.append(f"no size given; using {scale:g}x")
    if want_asset and scale and scale > 3:
        return _reject(state, rec, "reported", "Not applied: more than 3x is not a supported change.")
    if conflict:
        rec.conflict = True
        state.conflicts.append(f"turn {turn}: {conflict}")
        return _reject(state, rec, "reported", f"Not applied: {conflict}. Remove or change that "
                       "constraint first.")

    # --- apply, verify, retry once, else roll back ----------------------
    snapshot = (clone(sess.root), dict(sess.word_limits), dict(sess.min_width))
    before_text = text_map(sess.root)
    fixed = state.condition == "layout-first"
    allowed = {l.id for l in leaves(sec)} if sec is not None else {leaf.id}
    if op == "shorten_text":
        sess.word_limits[leaf.id] = limit
    if op == "enlarge_asset":
        sess.min_width[leaf.id] = render.asset_width_in(leaf, sess.size) * scale
    feedback: list[str] = []
    accepted = None
    for attempt in range(1, config.MAX_EDIT_ATTEMPTS + 1):
        rec.attempts = attempt
        sess.root = clone(snapshot[0])
        sess.remeasure()
        target = find(sess.root, leaf.id)
        notes: list[str] = []
        if op in ("shorten_text", "expand_text", "rewrite_text"):
            instr = {"shorten_text": f"Shorten this panel to at most {limit} words, keeping its meaning.",
                     "expand_text": "Add detail to this panel from the source passages.",
                     "rewrite_text": "Reword this panel more clearly without changing its meaning."}[op]
            new = _edit_text(sess, target, f"{instr} The user asked: {request}",
                             sess.word_limits.get(leaf.id), feedback)
            if new is None:
                feedback = ["the editor returned nothing usable"]
                continue
            target.bullets = new
            sess.remeasure()
            notes.append(f"rewrote '{rec.target_section}' ({len(target.text.split())} words)")
        elif op == "enlarge_asset":
            # A content pass follows in both conditions (restricted to the
            # target section under layout-first), so the donor may be crowded.
            notes += serve_requests(sess, [SpaceRequest(leaf.id, request, sess.min_width[leaf.id],
                                                        "dialogue")],
                                    scope="innermost" if fixed else "all", content_follows=True)
        elif op == "shrink_asset":
            note = _shrink(sess, target, scale)
            if note is None:
                return _restore_and_reject(state, rec, snapshot, "reported",
                                           "Not applied: that figure is already as small as its panel allows.")
            notes.append(note)

        # The same content-layout feedback as generation, within the condition.
        quick = review(sess, f"turn-{turn:02d}-a{attempt}-draft", use_reviewer=False)
        if quick.blocking:
            only = None if not fixed else {l.id for l in leaves(sec)} if sec is not None else {leaf.id}
            sess.requests = []
            notes += content_pass(sess, quick, only=only, may_request=not fixed)
            if not fixed:
                rv2 = review(sess, f"turn-{turn:02d}-a{attempt}-draft", use_reviewer=False)
                notes += serve_requests(sess, collect_requests(sess, rv2))
                sess.requests = []
        rv = review(sess, f"turn-{turn:02d}-a{attempt}")
        issues = [f"{i['kind']} on {i['element']}: {i['detail']}" for i in rv.blocking]
        after = text_map(sess.root)
        strayed = [lid for lid, t in after.items() if before_text.get(lid, t) != t and lid not in allowed]
        if fixed and strayed:
            issues.append(f"fixed-layout condition: text changed outside the target section ({len(strayed)} panel(s))")
        elif strayed:
            notes.append(f"also re-worded {len(strayed)} other panel(s) to make room")
        if not issues:
            accepted = (rv, notes)
            break
        feedback = issues
        rec.notes.append(f"attempt {attempt} failed review: " + "; ".join(issues)[:300])
    if accepted is None:
        return _restore_and_reject(state, rec, snapshot, "rejected",
                                   "Not applied; previous version kept. Unresolved after "
                                   f"{config.MAX_EDIT_ATTEMPTS} attempts: " + "; ".join(feedback)[:400])
    rv, notes = accepted
    sess.record(f"turn-{turn:02d}", rv, notes)
    rec.status, rec.message = "applied", restated or "Applied and checked."
    rec.notes += notes
    rec.artifact = sess.artifact
    state.history.append(rec)
    save(sess, [h.to_json() for h in state.history])
    llm_trace(state, rec)
    return rec


def _restore_and_reject(state: DialogueState, rec: TurnRecord, snapshot, status: str,
                        message: str) -> TurnRecord:
    sess = state.session
    sess.root, sess.word_limits, sess.min_width = clone(snapshot[0]), dict(snapshot[1]), dict(snapshot[2])
    sess.rerender()
    if sess.last_review is not None:
        rec.issues = sorted({i["kind"] for i in sess.last_review.blocking})
    return _reject(state, rec, status, message)


def preservation_report(state: DialogueState) -> dict:
    """Request completion, protection and conflict measures for one script."""
    sess = state.session
    text = "\n".join(l.text for l in leaves(sess.root))
    intact = [p for p in sess.protected if p.text in text]
    return {
        "condition": state.condition,
        "turns": len(state.history),
        "applied": sum(h.status == "applied" for h in state.history),
        "reported_not_applied": sum(h.status == "reported" for h in state.history),
        "rejected_by_checks": sum(h.status == "rejected" for h in state.history),
        "conflicts_reported": sum(h.conflict for h in state.history),
        "protected_spans": len(sess.protected),
        "protected_intact": len(intact),
        "word_limits_met": all(len(find(sess.root, k).text.split()) <= v
                               for k, v in sess.word_limits.items() if find(sess.root, k) is not None),
        "figure_sizes_met": all(render.asset_width_in(find(sess.root, k), sess.size) >= v - 0.01
                                for k, v in sess.min_width.items() if find(sess.root, k) is not None),
    }


def resume(workdir) -> DialogueState:
    """Reopen a saved session with its dialogue history (CLI edits)."""
    from .pipeline import load_session

    sess = load_session(workdir)
    data = json.loads((sess.workdir / "session.json").read_text(encoding="utf-8"))
    state = DialogueState(sess, [TurnRecord(**h) for h in data.get("history", [])])
    if sess.condition not in CONDITIONS:
        sess.condition = "joint"
    sess.layout_frozen = sess.condition == "layout-first"
    state.conflicts = [f"turn {h.turn}: {h.message}" for h in state.history if h.conflict]
    return state
