"""Claim requirements: what the poster must communicate, frozen before revision.

The proposal (s4.2): "For each selected claim, it will record the source
passage, supporting figure, and conditions needed to interpret it ... They
will remain fixed during revision so that the agent cannot make a draft pass
by weakening its content requirements. Their wording may change, but their
meaning must be preserved. Missing or incorrectly extracted elements will be
recorded as failures."

So:
  * a claim agent proposes the research question, the main method and the
    key findings, each with a verbatim quote, its page, interpretation
    conditions and a supporting figure/table;
  * every quote is checked against the raw text of its cited page, every
    condition against the paper, every asset against the parsed inventory.
    Anything that does not verify is dropped and recorded as a failure,
    after one corrective retry;
  * the surviving claims are frozen. Reviews check the rendered poster
    against them on every pass.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from . import llm
from .parse import ParsedPaper

_MD_CAP = 42000
ROLES = ("question", "method", "finding")


@dataclass
class Claim:
    id: str
    role: str  # question | method | finding
    statement: str  # one-line paraphrase, for reviewers
    quote: str  # verbatim from the paper
    page: int  # 1-based, verified
    conditions: list[str] = field(default_factory=list)  # verbatim phrases, frozen
    asset: str | None = None  # "figure:2" / "table:1"
    section_id: str = ""  # outline section that carries it (set by summarizer)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Claim":
        return Claim(**{k: d[k] for k in Claim.__dataclass_fields__ if k in d})


# --------------------------------------------------------------------------
# normalisation shared by every text comparison
# --------------------------------------------------------------------------
_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*%?")


def norm(text: str) -> str:
    """Lowercase, de-hyphenate line breaks, drop punctuation, collapse space.

    Lenient on typography (quotes, dashes, line breaks, ligatures) and strict
    on words and digits, which is what "verbatim" has to mean for PDF text.
    """
    t = text or ""
    for a, b in _LIGATURES.items():
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)  # hyphenated line break
    t = t.lower()
    t = re.sub(r"[^a-z0-9%.]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", t)  # keep decimal points only
    return " ".join(t.split())


def contains(haystack: str, needle: str) -> bool:
    n = norm(needle)
    return bool(n) and f" {n} " in f" {norm(haystack)} "


def numbers(text: str) -> list[str]:
    return [m.rstrip("%").replace(",", "") for m in NUMBER_RE.findall(text or "")]


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
CLAIM_SYS = (
    "You identify what a scientific poster must communicate. You copy text "
    "verbatim from the paper and reply with JSON only."
)

CLAIM_PROMPT = """\
## TASK
Identify the claims a poster of this paper must communicate:
- exactly one "question": the research question or problem;
- exactly one "method": the main method or study design;
- one to three "finding": the key results.

For each claim give:
- `quote`: one or two consecutive sentences copied VERBATIM from the paper
  that state the claim. Copy, do not paraphrase. It will be string-matched.
- `page`: the 1-based page the quote is on (pages are marked below).
- `conditions`: 1-4 SHORT phrases (2-6 words) copied verbatim from the
  paper that a reader needs to interpret the claim correctly -- the dataset,
  the comparison or baseline, the metric and unit, the sample size. Prefer
  phrases inside the quote. For a finding, include its numbers. These will be
  enforced: the poster fails review if any of them is missing.
- `asset`: the figure or table that best supports the claim, as
  "figure:<id>" or "table:<id>" using ids from the inventory, or null.
- `statement`: a one-line paraphrase for reviewers.
{feedback}
## OUTPUT (JSON only)
{{"claims": [
  {{"id": "q", "role": "question", "statement": "...", "quote": "...", "page": 1,
    "conditions": ["..."], "asset": null}},
  {{"id": "m", "role": "method", ...}},
  {{"id": "f1", "role": "finding", ...}}
]}}

## ASSET INVENTORY
{inventory}

## PAPER (page markers are <<page N>>)
{paper}
"""


def _paged_text(paper: ParsedPaper, cap: int = _MD_CAP) -> str:
    parts = [f"<<page {i + 1}>>\n{t.strip()}" for i, t in enumerate(paper.pages)]
    text = "\n\n".join(parts)
    return text if len(text) <= cap else text[:cap] + "\n\n[truncated]"


def _asset_key(paper: ParsedPaper, value) -> str | None:
    if not value or not isinstance(value, str) or ":" not in value:
        return None
    kind, _, aid = value.partition(":")
    kind = kind.strip().lower()
    if kind in ("fig", "figure"):
        kind = "figure"
    return f"{kind}:{aid.strip()}" if paper.asset(kind, aid.strip()) else None


def verify(raw: list, paper: ParsedPaper) -> tuple[list[Claim], list[str]]:
    """Keep only what checks out against the PDF; describe everything else."""
    claims: list[Claim] = []
    problems: list[str] = []
    full = "\n".join(paper.pages)
    seen: set[str] = set()
    for i, c in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(c, dict):
            continue
        role = str(c.get("role", "")).strip().lower()
        cid = str(c.get("id") or f"c{i + 1}").strip()
        label = f"claim {cid}"
        if role not in ROLES:
            problems.append(f"{label}: unknown role {role!r}")
            continue
        if cid in seen:
            problems.append(f"{label}: duplicate id")
            continue
        quote = str(c.get("quote", "")).strip()
        try:
            page = int(c.get("page", 0))
        except (TypeError, ValueError):
            page = 0
        if len(norm(quote).split()) < 4:
            problems.append(f"{label}: quote missing or too short")
            continue
        if not (1 <= page <= len(paper.pages)) or not contains(paper.pages[page - 1], quote):
            found = next((k + 1 for k, t in enumerate(paper.pages) if contains(t, quote)), 0)
            if not found:
                problems.append(f"{label}: quote not found in the paper: {quote[:90]!r}")
                continue
            problems.append(f"{label}: quote is on page {found}, not page {page}; corrected")
            page = found
        conditions = []
        for cond in c.get("conditions") or []:
            cond = str(cond).strip()
            if not cond:
                continue
            if contains(full, cond):
                conditions.append(cond)
            else:
                problems.append(f"{label}: condition not found in the paper: {cond!r}")
        asset = c.get("asset")
        key = _asset_key(paper, asset)
        if asset and not key:
            problems.append(f"{label}: asset {asset!r} is not in the extracted inventory")
        seen.add(cid)
        claims.append(Claim(id=cid, role=role, statement=str(c.get("statement", ""))[:240],
                            quote=quote, page=page, conditions=conditions, asset=key))
    for role in ROLES:
        if not any(c.role == role for c in claims):
            problems.append(f"no verified {role} claim")
    return claims, problems


def extract(paper: ParsedPaper, trace: llm.Trace, retries: int = 1) -> tuple[list[Claim], list[str]]:
    """Claim agent plus verification, with one corrective retry.

    Returns (frozen claims, recorded failures). The failures are part of the
    result, not swallowed: the proposal counts them.
    """
    feedback = ""
    best: tuple[list[Claim], list[str]] = ([], ["claim agent returned nothing"])
    for attempt in range(retries + 1):
        out = llm.call(
            "claims",
            CLAIM_PROMPT.format(feedback=feedback, inventory=paper.inventory_text(),
                                paper=_paged_text(paper)),
            system=CLAIM_SYS, max_tokens=3000, trace=trace,
            context={"pages": paper.pages, "inventory": paper.inventory_text(),
                     "feedback": feedback, "title": paper.title},
        )
        raw = (out or {}).get("claims") if isinstance(out, dict) else out
        claims, problems = verify(raw, paper)
        if len(claims) > len(best[0]) or (len(claims) == len(best[0]) and len(problems) < len(best[1])):
            best = (claims, problems)
        hard = [p for p in problems if not p.endswith("; corrected")]
        if not hard:
            break
        feedback = ("\n## YOUR PREVIOUS ANSWER FAILED VERIFICATION\n"
                    + "\n".join(f"- {p}" for p in hard)
                    + "\nCopy quotes and conditions character for character from the page text.\n")
        trace.log(f"claims attempt {attempt + 1}: {len(hard)} verification problem(s), "
                  + ("retrying" if attempt < retries else "keeping verified claims only"))
    claims, problems = best
    for p in problems:
        trace.log(f"  claim check: {p}")
    return claims, problems


def prompt_block(claims: list[Claim]) -> str:
    """How claims are shown to the summarizing and content agents."""
    return json.dumps([{"id": c.id, "role": c.role, "statement": c.statement, "quote": c.quote,
                        "conditions": c.conditions, "asset": c.asset} for c in claims],
                      ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# checks on a rendered poster
# --------------------------------------------------------------------------
def missing_conditions(claim: Claim, text: str, equivalents: dict | None = None) -> list[str]:
    """Conditions absent from `text`. A reviewer-supplied equivalent counts
    only if it is visibly present AND carries exactly the same numbers --
    "wording may change, but meaning must be preserved"."""
    out = []
    for cond in claim.conditions:
        if contains(text, cond):
            continue
        alt = (equivalents or {}).get(cond)
        if isinstance(alt, str) and contains(text, alt) and numbers(alt) == numbers(cond):
            continue
        out.append(cond)
    return out


def unsupported_numbers(poster_text: str, paper: ParsedPaper) -> list[str]:
    """Numbers on the poster that do not occur anywhere in the paper."""
    known = set(numbers("\n".join(paper.pages)))
    bad = []
    for n in numbers(poster_text):
        if n not in known and n not in bad:
            bad.append(n)
    return bad
