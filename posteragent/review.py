"""Render + review: the checkpoint after every editing pass.

Each review renders the draft (PNG preview and editable PPTX), reopens the
PPTX, and returns a list of issues. Every issue names the affected element,
the requirement it fails and the proposed change, as the proposal asks:

    {"kind", "element", "section", "requirement", "detail", "change",
     "source": geometry | claim | constraint | reviewer, "blocking": bool}

Blocking issues are the measurable failures -- overflow at the minimum font
size, overlap, out-of-canvas, a frozen claim condition missing, a number the
paper does not contain, a claim the reviewer judges misstated, a figure
missing, a protected span changed, a user limit not met. A draft with none of
these is "resolved". Reviewer remarks about readability are non-blocking:
they steer the next pass but a model's taste does not decide success.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import agents, claims as claims_mod, config, render
from .tree import ContentLeaf, SemanticNode, all_text, leaves, resolve_geometry, section_of

BLOCKING = {"overflow", "out_of_canvas", "overlap", "missing_asset", "missing_condition",
            "unsupported_number", "claim_rejected", "protected_changed", "word_limit",
            "figure_too_small", "pptx_mismatch", "claim_unplaced"}


@dataclass
class Review:
    label: str
    issues: list[dict]
    claims: list[dict]
    panels: list[dict]
    folder: Path
    reviewer_error: str = ""
    allocations: list[dict] = field(default_factory=list)

    @property
    def blocking(self) -> list[dict]:
        return [i for i in self.issues if i["blocking"]]

    @property
    def resolved(self) -> bool:
        return not self.blocking

    def summary(self) -> str:
        b = len(self.blocking)
        return f"{len(self.issues)} issue(s), {b} blocking" if self.issues else "no issues"


def _issue(kind: str, element: str, section: str, requirement: str, detail: str,
           change: str, source: str) -> dict:
    return {"kind": kind, "element": element, "section": section, "requirement": requirement,
            "detail": detail, "change": change, "source": source, "blocking": kind in BLOCKING}


def _section_title(root, leaf_id: str) -> str:
    sec = section_of(root, leaf_id)
    return sec.title if isinstance(sec, SemanticNode) else ""


def panels(sess) -> list[dict]:
    out = []
    for l in leaves(sess.root):
        if l.content_type == "title":
            continue
        text = l.text if l.content_type == "text" else f"[{l.content_type} {l.asset_id}] {l.caption}"
        out.append({"id": l.id, "section": _section_title(sess.root, l.id), "type": l.content_type,
                    "text": text, "fill": l.measured.volume_ratio,
                    "box_in": [round(v, 2) for v in l.box.inches(sess.size)],
                    "font_scale": l.measured.font_scale,
                    "width_in": round(render.asset_width_in(l, sess.size), 2)
                    if l.content_type in ("figure", "table") else None})
    return out


def claim_text(sess, claim) -> tuple[str, list[ContentLeaf]]:
    """Everything a reader sees for this claim's section: heading, text, captions."""
    owned = [l for l in leaves(sess.root) if l.source_id == claim.section_id]
    parts = []
    if owned:
        sec = section_of(sess.root, owned[0].id)
        if isinstance(sec, SemanticNode):
            parts.append(sec.title)
    parts += [l.text for l in owned]
    return "\n".join(parts), owned


def review(sess, label: str, *, use_reviewer: bool = True) -> Review:
    """Render the session's current tree into workdir/label and check it."""
    folder = Path(sess.workdir) / label
    folder.mkdir(parents=True, exist_ok=True)
    resolve_geometry(sess.root, sess.size)
    render.measure(sess.root, sess.size)
    sess.preview = render.render_png(sess.root, sess.size)
    sess.preview.save(folder / "preview.png")
    pptx = render.render_pptx(sess.root, sess.size, folder / "poster.pptx")

    issues: list[dict] = []
    root = sess.root
    by_id = {l.id: l for l in leaves(root)}

    for g in render.geometry_issues(root, sess.size):
        leaf = by_id.get(g["element"])
        change = {"overflow": "condense", "missing_asset": "more_space",
                  "caption_truncated": "more_space"}.get(g["kind"], "more_space")
        issues.append(_issue(g["kind"], g["element"], _section_title(root, g["element"]),
                             "readability", g["detail"], change, "geometry"))
        if leaf is not None and g["kind"] == "overflow":
            leaf.measured.overflow = True
    for l in leaves(root):
        if l.content_type == "text" and not l.measured.overflow and l.measured.volume_ratio < config.UNDERFILL_PCT:
            issues.append(_issue("underfilled", l.id, _section_title(root, l.id), "density",
                                 f"text fills only {l.measured.volume_ratio:.0f}% of its panel",
                                 "expand", "geometry"))
    for p in render.check_pptx(pptx, root):
        issues.append(_issue("pptx_mismatch", p.split(":")[0], "", "artifact", p, "repair", "geometry"))

    # --- user constraints (dialogue) -------------------------------------
    text_all = all_text(root)
    for span in sess.protected:
        if span.text not in text_all:
            issues.append(_issue("protected_changed", "poster", "", "protected text",
                                 f"protected text from turn {span.turn} changed: {span.text[:80]!r}",
                                 "repair", "constraint"))
    for leaf_id, limit in sess.word_limits.items():
        leaf = by_id.get(leaf_id)
        if leaf is not None and len(leaf.text.split()) > limit:
            issues.append(_issue("word_limit", leaf_id, _section_title(root, leaf_id), "word limit",
                                 f"{len(leaf.text.split())} words, limit {limit}", "condense",
                                 "constraint"))
    for leaf_id, width in sess.min_width.items():
        leaf = by_id.get(leaf_id)
        if leaf is not None:
            got = render.asset_width_in(leaf, sess.size)
            if got < width - 0.01:
                issues.append(_issue("figure_too_small", leaf_id, _section_title(root, leaf_id),
                                     "requested size", f"displayed {got:.2f} in, requested >= {width:.2f} in",
                                     "more_space", "constraint"))

    # --- numbers the paper does not contain ------------------------------
    for l in leaves(root):
        if l.content_type != "text":
            continue
        bad = claims_mod.unsupported_numbers(l.text, sess.paper)
        if bad:
            issues.append(_issue("unsupported_number", l.id, _section_title(root, l.id), "faithfulness",
                                 f"number(s) not in the paper: {', '.join(bad)}", "repair", "claim"))

    # --- frozen claims, deterministic part --------------------------------
    def claim_rows(equivalents: dict) -> list[dict]:
        rows = []
        for c in sess.claims:
            text, owned = claim_text(sess, c)
            missing = claims_mod.missing_conditions(c, text, equivalents.get(c.id))
            asset_ok = True
            if c.asset:
                kind, _, aid = c.asset.partition(":")
                asset_ok = any(l.content_type == kind and l.asset_id == aid
                               and render.asset_width_in(l, sess.size) > 0 for l in owned)
            rows.append({"claim": c.id, "role": c.role, "statement": c.statement,
                         "source": {"page": c.page, "quote": c.quote},
                         "conditions": c.conditions, "missing": missing, "asset": c.asset,
                         "asset_on_poster": asset_ok,
                         "elements": [l.id for l in owned], "text": text})
        return rows

    rows = claim_rows({})
    known = [f"{i['kind']} on {i['element']}: {i['detail']}" for i in issues]
    known += [f"claim {r['claim']} missing condition {m!r}" for r in rows for m in r["missing"]]

    # --- vision-language review -------------------------------------------
    vr = None
    error = ""
    if use_reviewer:
        try:
            vr = agents.review_agent(sess.preview, panels(sess), sess.claims, known, sess.trace)
        except (RuntimeError, ValueError) as e:
            error = str(e)
            sess.trace.log(f"review {label}: reviewer unavailable ({e}); geometric and claim checks only")
    if vr is not None:
        rows = claim_rows(vr.equivalents)
        for it in vr.issues:
            issues.append({**_issue("reviewer", it["element"], _section_title(root, it["element"]),
                                    it["requirement"], it["problem"], it["change"], "reviewer"),
                           "blocking": False})
    for r in rows:
        r["reviewer_verdict"] = None if vr is None else vr.verdicts.get(r["claim"])
        text_leaves = [e for e in r["elements"] if by_id.get(e) is not None
                       and by_id[e].content_type == "text"]
        target = text_leaves[0] if text_leaves else (r["elements"][0] if r["elements"] else "poster")
        sec = _section_title(root, target) if target != "poster" else ""
        if not r["elements"]:
            issues.append(_issue("claim_unplaced", "poster", "", f"claim {r['claim']}",
                                 "no panel carries this claim", "repair", "claim"))
        for m in r["missing"]:
            issues.append(_issue("missing_condition", target, sec, f"claim {r['claim']}",
                                 f"required condition missing: {m!r}", "repair", "claim"))
        if not r["asset_on_poster"]:
            issues.append(_issue("missing_asset", target, sec, f"claim {r['claim']}",
                                 f"supporting {r['asset']} is not shown with this claim",
                                 "more_space", "claim"))
        if r["reviewer_verdict"] is False:
            issues.append(_issue("claim_rejected", target, sec, f"claim {r['claim']}",
                                 "reviewer: the poster does not state this claim faithfully "
                                 "to its source passage", "repair", "claim"))
        r["passed"] = (not r["missing"] and r["asset_on_poster"] and bool(r["elements"])
                       and r["reviewer_verdict"] is not False)

    result = Review(label, issues, rows, panels(sess), folder, error,
                    allocations=list(getattr(sess, "last_allocations", [])))
    (folder / "review.json").write_text(json.dumps({
        "label": label, "condition": sess.condition, "resolved": result.resolved,
        "summary": result.summary(), "issues": issues, "claims": rows,
        "panels": result.panels, "allocations": result.allocations,
        "reviewer_error": error, "size_in": [sess.size.w_in, sess.size.h_in],
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    return result
