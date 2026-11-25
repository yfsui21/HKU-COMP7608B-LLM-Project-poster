"""Scripted offline backend for tests and CI. Not a model.

It answers each agent from the structured `context` the agent passes to
llm.call(), using plain heuristics (sentence cues, number windows, dropping
the last bullet). The point is to exercise every code path -- verification,
binding, guards, space requests, allocation, schedules, dialogue checks,
rollback, artifacts -- deterministically and for free. Its posters say nothing
about model quality and must never be reported as results.

    from posteragent import llm, offline
    llm.set_backend(offline.backend)
"""
from __future__ import annotations

import re
from typing import Any, Sequence

from . import claims as claims_mod
from .tree import Bullet

name = "offline-stub"

_SENT = re.compile(r"(?<=[.?!])\s+(?=[A-Z])")
_FINDING_CUES = ("reduc", "improv", "outperform", "reached", "increas", "rose", "higher", "lower",
                 "achiev", "cut ")
_METHOD_CUES = ("we propose", "we compare", "we use", "reviewed", "we train", "we study",
                "we introduce", "our method", "annotators")
_QUESTION_CUES = ("research question", "whether", "we ask", "asks whether", "question is")


def _sentences(text: str) -> list[str]:
    flat = re.sub(r"-\n(\w)", r"\1", text)
    flat = " ".join(flat.split())
    return [s.strip() for s in _SENT.split(flat) if len(s.split()) >= 6]


def _windows(sentence: str, k: int = 3) -> list[str]:
    """Verbatim phrases of up to k words starting at each number."""
    words = sentence.split()
    out = []
    for i, w in enumerate(words):
        if claims_mod.NUMBER_RE.search(w):
            phrase = " ".join(words[i:i + k]).rstrip(".,;:")
            if phrase and phrase not in out:
                out.append(phrase)
    return out[:3]


def _claims(ctx: dict) -> dict:
    pages: list[str] = ctx["pages"]
    found: dict[str, list[tuple[int, str]]] = {"question": [], "method": [], "finding": []}
    for pno, text in enumerate(pages, 1):
        for s in _sentences(text):
            low = s.lower()
            if any(c in low for c in _QUESTION_CUES):
                found["question"].append((pno, s))
            elif any(c in low for c in _METHOD_CUES):
                found["method"].append((pno, s))
            elif any(c in low for c in _FINDING_CUES) and claims_mod.numbers(s):
                found["finding"].append((pno, s))
    inventory = ctx.get("inventory", "")
    assets = re.findall(r"- (figure|table) id=(\S+)", inventory)
    out = []
    for role, cid, limit in (("question", "q", 1), ("method", "m", 1), ("finding", "f", 2)):
        for n, (pno, s) in enumerate(found[role][:limit], 1):
            asset = None
            if role == "finding" and assets:
                kind, aid = assets[(n - 1) % len(assets)]
                asset = f"{kind}:{aid}"
            conds = _windows(s) or [" ".join(s.split()[:4]).rstrip(".,;:")]
            out.append({"id": cid if limit == 1 else f"{cid}{n}", "role": role,
                        "statement": s[:120], "quote": s, "page": pno,
                        "conditions": conds, "asset": asset})
    return {"claims": out}


def _parser(ctx: dict) -> dict:
    md: str = ctx["markdown"]
    blocks = re.split(r"^## ", md, flags=re.M)
    head, children = blocks[0], []
    for i, b in enumerate(blocks[1:], 1):
        title, _, body = b.partition("\n")
        figs = re.findall(r"\[FIGURE (\w+)\]", body)
        tabs = re.findall(r"\[TABLE (\w+)\]", body)
        body = re.sub(r"\[(FIGURE|TABLE) \w+\][^\n]*", "", body)
        if not body.strip() and not figs and not tabs:
            continue
        children.append({"id": str(i), "title": title.strip(), "type": "section",
                         "content": " ".join(body.split())[:900],
                         "assets": {"figures": figs, "tables": tabs, "references": []},
                         "children": []})
    title = ctx["paper"].title if ctx.get("paper") else head.strip("# \n")
    return {"tree": {"id": "root", "title": title, "type": "title", "content": "",
                     "assets": {"figures": [], "tables": [], "references": []},
                     "children": children}}


def _summarize(ctx: dict) -> dict:
    nodes = [n for n in ctx["nodes"] if n["id"] != "root" and n["content"].strip()]
    nodes = [n for n in nodes if not n["title"].lower().startswith(("abstract", "a. example"))] or nodes
    nodes = nodes[: ctx["hi"]]
    secs = []
    for n in nodes:
        bullets = [" ".join(s.split()[:22]) for s in _sentences(n["content"])[:3]]
        secs.append({"id": n["id"], "title": re.sub(r"^\d+\s+", "", n["title"])[:30],
                     "bullets": bullets or [n["content"][:120]], "figures": n["figures"],
                     "tables": n["tables"], "importance": 1.0, "source_ids": [n["id"]],
                     "claims": []})
    for c in ctx["claims"]:
        home = next((s for s, n in zip(secs, nodes) if claims_mod.contains(n["content"], c.quote)),
                    secs[0] if secs else None)
        if home is None:
            continue
        home["claims"].append(c.id)
        text = " ".join(home["bullets"])
        if any(not claims_mod.contains(text, q) for q in c.conditions):
            home["bullets"].insert(0, c.quote)
    return {"sections": secs}


def _bullets(texts: list[str]) -> list[dict]:
    return [Bullet.plain(t).to_json() for t in texts if t.strip()]


def _content(ctx: dict) -> dict:
    leaf = ctx["leaf"]
    mode = ctx["mode"]
    required: list[str] = ctx["required"]
    protected: list[str] = ctx["protected"]
    texts = [b.text for b in leaf.bullets]
    keep = lambda t: any(claims_mod.contains(t, q) for q in required) or any(p in t for p in protected)
    if mode == "condense":
        words = sum(len(t.split()) for t in texts)
        goal = words * (1 - ctx["delta"] / 100.0)
        limit = ctx.get("word_limit")
        if limit is not None:
            goal = min(goal, limit)
        for i in range(len(texts) - 1, -1, -1):
            if sum(len(t.split()) for t in texts) <= goal:
                break
            if not keep(texts[i]):
                texts.pop(i)
        if sum(len(t.split()) for t in texts) > goal:
            # Trim unprotected bullets to their first clause before giving up.
            texts = [t if keep(t) else " ".join(t.split()[:8]) for t in texts]
        needs = sum(len(t.split()) for t in texts) > goal and ctx["ratio"] > 100
        return {"bullets": _bullets(texts), "needs_space": needs,
                "space_reason": "required phrases do not fit" if needs else ""}
    if mode == "expand":
        have = " ".join(texts)
        extra = [s for s in _sentences(ctx["source"]) if not claims_mod.contains(have, s)]
        add = max(1, int(len(texts) * ctx["delta"] / 100.0))
        return {"bullets": _bullets(texts + [" ".join(s.split()[:22]) for s in extra[:add]])}
    # repair: drop bullets with unsupported numbers, add the source sentence
    # carrying each missing condition.
    bad = set()
    for p in ctx["problems"]:
        m = re.search(r"number\(s\) not in the paper: (.*)", p)
        if m:
            bad |= {x.strip() for x in m.group(1).split(",")}
    texts = [t for t in texts if not (set(claims_mod.numbers(t)) & bad)]
    for q in required:
        if not claims_mod.contains(" ".join(texts), q):
            src = next((s for s in _sentences(ctx["source"]) if claims_mod.contains(s, q)), q)
            texts.insert(0, src)
    return {"bullets": _bullets(texts)}


def _intent(ctx: dict) -> dict:
    """Understands the scripted study requests, nothing more."""
    req = ctx["request"].lower()
    sections = ctx["sections"]  # [{id, title, leaves:[{id,type,asset}]}]
    history = ctx["history"]

    def find_section(text: str):
        for s in sections:
            if s["title"].lower() in text:
                return s
        for key, words in (("method", ("method", "approach")), ("result", ("result", "finding")),
                           ("question", ("question", "introduction", "motivation"))):
            if any(w in text for w in words):
                for s in sections:
                    if any(w in s["title"].lower() for w in words) or key in s["title"].lower():
                        return s
        return None

    out: dict[str, Any] = {"target_section_id": None, "target_leaf_id": None, "op": "unsupported",
                           "word_limit": None, "scale": None, "protect_section_ids": [],
                           "new_protected": [], "conflict": False, "conflict_reason": "",
                           "infeasible": False, "infeasible_reason": "", "restated": ""}
    m = re.search(r"(enlarge|shrink)[^%]*?(\d+)\s*%", req)
    if m:
        figs = [(s, l) for s in sections for l in s["leaves"] if l["type"] in ("figure", "table")]
        fm = re.search(r"(figure|table)\s+(\w+)", req)
        pick = next(((s, l) for s, l in figs if fm and l["type"] == fm.group(1)
                     and l["asset"] == fm.group(2)), figs[0] if figs else (None, None))
        if pick[0]:
            out.update(target_section_id=pick[0]["id"], target_leaf_id=pick[1]["id"],
                       op=f"{m.group(1)}_asset",
                       scale=1 + int(m.group(2)) / 100 * (1 if m.group(1) == "enlarge" else -1))
    m = re.search(r"shorten (.*?) to (\d+) words", req)
    if m:
        what = m.group(1)
        sec = None
        if any(w in what for w in ("protected", "you kept", "kept unchanged", "locked")):
            prev = [h for h in history if h.get("protected_sections")]
            if prev:
                sid = prev[-1]["protected_sections"][0]
                sec = next((s for s in sections if s["id"] == sid), None)
        sec = sec or find_section(what)
        if sec:
            out.update(target_section_id=sec["id"], op="shorten_text", word_limit=int(m.group(2)))
    m = re.search(r"(?:keep|protect) (?:the )?(.*?)(?: text)?(?: exactly| unchanged|$)", req)
    if "keep" in req or "protect" in req:
        tail = req.split("keep", 1)[-1] if "keep" in req else req.split("protect", 1)[-1]
        sec = find_section(tail)
        if sec:
            out["protect_section_ids"] = [sec["id"]]
            if out["op"] == "unsupported":
                out.update(op="protect", target_section_id=sec["id"])
    return out


def _edit(ctx: dict) -> dict:
    texts = [b.text for b in ctx["leaf"].bullets]
    limit = ctx.get("word_limit")
    if limit:
        out, n = [], 0
        for t in texts:
            words = t.split()
            if n + len(words) <= limit:
                out.append(t)
                n += len(words)
            elif limit - n >= 4:
                out.append(" ".join(words[: limit - n]))
                n = limit
        texts = out or [" ".join(texts[0].split()[:limit])]
    return {"bullets": _bullets(texts)}


def backend(agent: str, prompt: str, images: Sequence, context: dict) -> Any:
    kind = agent.split(":")[0]
    if kind == "claims":
        return _claims(context)
    if kind == "parser":
        return _parser(context)
    if kind == "summarize":
        return _summarize(context)
    if kind == "content":
        return _content(context)
    if kind == "review":
        return {"issues": [], "verdicts": {c.id: True for c in context["claims"]}, "equivalents": {}}
    if agent == "layout":
        return {"direction": context["directions"][0], "delta": 0, "reason": "offline: no change"}
    if agent == "dialogue:intent":
        return _intent(context)
    if agent == "dialogue:edit":
        return _edit(context)
    raise ValueError(f"offline backend has no script for agent {agent!r}")


backend.name = name  # type: ignore[attr-defined]
