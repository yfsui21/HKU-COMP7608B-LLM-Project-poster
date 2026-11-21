"""Exercise and verify the standalone t-SNE PDF-to-poster acceptance case."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from pptx import Presentation

from paper_canvas.agent import edit, read, write
from paper_canvas.forest import GROUPS, check, normalized
from paper_canvas.model import Model


TURNS = (
    "enlarge figure by 8% and protect result",
    "shorten question to 9 words",
    "shorten protected section to 2 words",
)


def events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slide_shapes(path):
    deck = Presentation(path)
    require(len(deck.slides) == 1, "Expected a single editable slide")
    return {shape.name: shape for shape in deck.slides[0].shapes}


def verify_final_claims(state, review, pptx):
    """Reopen the PPTX and verify the visible evidence used by the recorded reviewer."""
    recorded = {claim["claim"]: claim for claim in review["claims"]}
    for actual in check(state, review["elements"], pptx, semantic=True):
        claim = recorded[actual["claim"]]
        for key in ("text", "elements", "conditions", "assets", "source"):
            require(claim[key] == actual[key], f"Different {key} for {actual['claim']}")
        require(not actual["mismatched"] and not actual["unsupported_numbers"], "Invalid figure or number")
        equivalents = claim.get("equivalents", {})
        require(set(actual["missing"]) == set(equivalents), "Unaccounted condition in exported PowerPoint")
        for term, phrase in equivalents.items():
            require(normalized(phrase) and normalized(phrase) in actual["text"], "Paraphrase missing from slide")
            require(re.findall(r"\d+(?:[.,]\d+)*", term) == re.findall(r"\d+(?:[.,]\d+)*", phrase),
                    "Numerical value changed during paraphrase")


def exercise(session, model_name):
    require(not read(session / "state.json")["history"], "Use a new, unedited session for --run-dialogue")
    os.environ.pop("PAPERCANVAS_GPT_QUEUE", None)
    os.environ["PAPERCANVAS_CODEX_MODEL"] = model_name
    for request, expected in zip(TURNS, (True, True, False)):
        decision = edit(session, request, Model(True))["history"][-1]
        require(decision["accepted"] == expected, f"{request}: {decision['reason']}")


def verify(pdf, work, session):
    audit = read(work / "result/audit.json")
    calls = events(work / "inference.jsonl")
    state = read(session / "state.json")
    trace = events(session / "trace.jsonl")
    require(audit["backend"] == "codex" and not (work / "pending.json").exists(), "Expected independent CLI")
    require(audit["pdf_sha256"] == state["forest"]["pdf_sha256"] == sha256(pdf), "Different source PDF")
    require(state["forest"]["deck_sha256"] == sha256(work / "result/display.pptx"),
            "Revision did not start from the newly generated deck")
    require(audit["slides"] == 1 and audit["title_verified_on_pdf_page"] and not audit["out_of_bounds_shapes"]
            and audit["stages"]["6_feedback_events"]["done"] and audit["model_requests"]["reused_exact_requests"] == 0,
            "Expected fresh PDF extraction, generation and completed visual feedback")
    require(any(c["images"] for c in calls) and all(c["usage"].get("reported") for c in calls),
            "Missing standalone CLI inference receipts")
    passes = [e for e in trace if e["event"] == "pass"]
    roles = [e["role"] for e in passes]
    require(roles == ["content", "layout"] * 2, "Incomplete joint revision")
    decisions = state["history"]
    require([r["request"] for r in decisions] == list(TURNS), "Unexpected dialogue sequence")
    require([r["accepted"] for r in decisions] == [True, True, False] and decisions[-1].get("conflict")
            and decisions[-1]["target"] == "result", "Expected a protected-result conflict on turn three")
    attempts = [e for e in trace if e["event"] == "attempt"]
    accepted = [e["artifact"] for e in attempts if not e["issues"]]
    require(len(accepted) == 2 and state["artifact"] == accepted[-1], "Conflict changed the accepted version")
    reviewed_versions = ["00-initial", *[e["artifact"] for e in passes + attempts]]
    for version in reviewed_versions:
        claims = read(session / version / "review.json")["claims"]
        require(len(claims) == len(GROUPS) and {c["claim"] for c in claims} == set(GROUPS)
                and all(isinstance(c.get("semantic_passed"), bool) for c in claims),
                "Missing per-claim visual verdict in " + version)

    before = slide_shapes(session / passes[-1]["artifact"] / "poster.pptx")
    after_first = slide_shapes(session / accepted[0] / "poster.pptx")
    final = session / state["artifact"]
    after = slide_shapes(final / "poster.pptx")
    expected_width = 1.08 * before["figure"].width
    require(abs(after_first["figure"].width - expected_width) <= 2 and after["figure"].width >= expected_width - 2,
            "Requested figure width was not applied and preserved")
    require(before["result"].text == after_first["result"].text == after["result"].text, "Protected slide text changed")
    # PowerPoint stores a paragraph break at each rendered line in this template.
    require(after["result"].text.replace("\n", " ") == state["protected"]["result"]
            and state["sections"][2]["text"] == state["protected"]["result"], "Incorrect protected source text")
    words = len(after["question"].text.split())
    require(words <= 9 and words < len(after_first["question"].text.split()), "Question was not shortened to budget")
    for artifact in [passes[-1]["artifact"], *accepted]:
        review = read(session / artifact / "review.json")
        require(not review["issues"] and {c["claim"] for c in review["claims"]} == set(GROUPS)
                and all(c.get("semantic_passed") and c["passed"] for c in review["claims"]),
                "Incomplete scientific review: " + artifact)
    verify_final_claims(state, review, final / "poster.pptx")
    revisions = [call for e in trace if e["event"] == "usage" for call in e["calls"]]
    require(revisions and all(c["usage"].get("reported") and c["model"] == audit["model"] for c in revisions),
            "Missing revision CLI usage")
    return dict(paper=audit["paper"], pdf_pages=audit["pdf_pages"], pdf_sha256=audit["pdf_sha256"],
                backend=audit["backend"], model=audit["model"], upstream_revision=audit["source"],
                posterforest_generation_requests=audit["model_requests"], revision_requests=len(revisions),
                generated_deck_sha256=state["forest"]["deck_sha256"], accepted_pptx_sha256=sha256(final / "poster.pptx"),
                passes=roles, dialogue=decisions, attempts=attempts, protected_result_unchanged=True,
                reviewed_versions=len(reviewed_versions), claim_groups_per_review=len(GROUPS),
                figure_width_pt={"before": before["figure"].width.pt, "after": after["figure"].width.pt},
                final_question=after["question"].text, question_words=words,
                final_claim_verdicts={c["claim"]: c["passed"] for c in review["claims"]},
                remaining_issues=review["issues"], artifact=state["artifact"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pdf", "work", "session"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--run-dialogue", action="store_true", help="Send the three test requests to a fresh CLI session")
    parser.add_argument("--report", type=Path, help="Write a machine-readable acceptance record")
    args = parser.parse_args()
    if args.run_dialogue:
        audit = read(args.work / "result/audit.json")
        require(audit["backend"] == "codex", "Use the codex backend for this acceptance case")
        exercise(args.session, audit["model"])
    result = verify(args.pdf, args.work, args.session)
    if args.report:
        write(args.report, result)
    print(json.dumps(result, indent=2))
