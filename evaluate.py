"""Proposal protocol: freeze a split, compare schedules, run dialogue scripts.

    python evaluate.py split PAPERS_DIR plan.json [--seed 56]
    python evaluate.py run --papers PAPERS_DIR --plan plan.json --split development --out runs/dev
      ... calibrate size_in, thresholds and dialogue scripts in plan.json ...
    python evaluate.py freeze plan.json
    python evaluate.py run --papers PAPERS_DIR --plan plan.json --out runs/study
    python evaluate.py summarize runs/study

`freeze` seals the canvas size, the readability thresholds and per-pass
budgets, the model ids and a digest of the prompts. `run` on the evaluation
split refuses a plan that is not frozen or whose prompts or models changed
since; the development split can be run at any time.

`run` without --plan uses the bundled synthetic paper (a smoke test, not a
study). --offline swaps the model for the scripted backend (posteragent/
offline.py): it exercises the protocol end to end for free and its numbers
mean nothing.

For each evaluation paper: one shared draft (parse, frozen claims, outline);
at each canvas size (standard, and 75% of its area with typography fixed) one
shared initial layout and first review; then joint, content-first and
layout-first, each with two content and two layout passes and a review after
every pass. Dialogue papers fork the accepted joint poster at the standard
size and run the same three-turn script under joint and layout-first.
Failures are recorded and stay in the denominators.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import shutil
import subprocess
import time
import traceback
from collections import Counter
from pathlib import Path

from posteragent import config, dialogue, llm, pipeline
from posteragent.assessment import POSTER, TURN, read, summarize, write

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "samples" / "synthetic_paper.pdf"

# Generic three-turn script: enlarge a figure while protecting text, shorten
# another section, then refer back to the protected text with a conflicting
# request. Review targets and labels per paper on development data before
# freezing the plan.
SCRIPT = [
    ["Enlarge the main results figure by 30% and keep the key findings text exactly as it is", "feasible"],
    ["Shorten the method section to 40 words", "feasible"],
    ["Now shorten the text you kept unchanged to 10 words", "conflict"],
]


def freeze(args) -> None:
    files = sorted(p.relative_to(args.papers).as_posix() for p in args.papers.rglob("*.pdf"))
    if len(files) != 100:
        raise SystemExit("Supply exactly 100 paper PDFs, one per Paper2Poster pair. "
                         "Keep the reference posters in a separate directory.")
    rng = random.Random(args.seed)
    selected = rng.sample(files, 20)
    write(args.plan, dict(seed=args.seed, development=selected[:5], evaluation=selected[5:],
                          dialogues={p: SCRIPT for p in rng.sample(selected[5:], 5)},
                          size_in=list(args.size), areas=[1, 0.75],
                          thresholds=config.thresholds(), frozen=False))
    print(f"wrote {args.plan}. Run the development split, adjust size_in, thresholds and the "
          f"dialogue scripts and labels, then: python evaluate.py freeze {args.plan}")


PROMPT_FILES = ("agents.py", "claims.py", "dialogue.py")
CODE_FILES = tuple(sorted(p.name for p in (ROOT / "posteragent").glob("*.py") if p.name != "offline.py"))


FROZEN_FIELDS = ("seed", "development", "evaluation", "dialogues", "size_in", "areas", "thresholds")


def _plan_digest(plan: dict) -> str:
    """Digest of everything calibrated on the development split."""
    body = json.dumps({k: plan.get(k) for k in FROZEN_FIELDS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _models() -> dict:
    return {"text_model": config.TEXT_MODEL, "vision_model": config.VISION_MODEL}


def freeze_plan(args) -> None:
    plan = read(args.plan)
    if plan.get("frozen"):
        raise SystemExit(f"{args.plan} is already frozen ({plan['freeze']['at']}).")
    config.apply_thresholds(plan["thresholds"])  # validates
    for name, turns in plan.get("dialogues", {}).items():
        if not all(isinstance(t, list) and len(t) == 2 and t[1] in ("feasible", "conflict") for t in turns):
            raise SystemExit(f"dialogue script for {name}: each turn must be [request, feasible|conflict]")
    plan["frozen"] = True
    plan["freeze"] = dict(at=time.strftime("%Y-%m-%d %H:%M:%S"), git_commit=_git(), **_models(),
                          prompt_digest=_digest(*PROMPT_FILES), code_digest=_digest(*CODE_FILES),
                          plan_digest=_plan_digest(plan))
    write(args.plan, plan)
    print(f"froze {args.plan}: size {plan['size_in']} in, {len(plan['thresholds'])} thresholds, "
          f"models {plan['freeze']['text_model']} / {plan['freeze']['vision_model']}")


def _check_frozen(plan: dict, offline: bool) -> list[str]:
    """Hard errors for the evaluation split; returns soft warnings."""
    f = plan.get("freeze") or {}
    if not plan.get("frozen") or not f:
        raise SystemExit("The plan is not frozen. Calibrate on the development split, then run "
                         "`python evaluate.py freeze PLAN` (or pass --allow-unfrozen for a dry run).")
    errors = []
    if f.get("plan_digest") != _plan_digest(plan):
        changed = "size, thresholds, split or dialogue scripts"
        errors.append(f"plan.json ({changed}) was edited after freezing")
    if f["prompt_digest"] != _digest(*PROMPT_FILES):
        errors.append("prompts changed since the plan was frozen")
    if not offline and _models() != {k: f[k] for k in ("text_model", "vision_model")}:
        errors.append(f"models {_models()} differ from the frozen "
                      f"{ {k: f[k] for k in ('text_model', 'vision_model')} }")
    if errors:
        raise SystemExit("Frozen plan violated: " + "; ".join(errors) + ". Use --allow-unfrozen for a dry run.")
    return [] if f["code_digest"] == _digest(*CODE_FILES) else [
        "posteragent code changed since freezing (prompts unchanged); recorded, not blocking"]


def _digest(*names: str) -> str:
    h = hashlib.sha256()
    for n in names:
        h.update((ROOT / "posteragent" / n).read_bytes())
    return h.hexdigest()


def _git() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _completed(folder: Path) -> tuple[bool, str]:
    """System completion: the PPTX reopens with one slide and the preview exists."""
    from pptx import Presentation

    try:
        prs = Presentation(str(folder / "poster.pptx"))
        if len(prs.slides) != 1:
            return False, "deck does not have exactly one slide"
        if not (folder / "preview.png").is_file():
            return False, "preview missing"
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"deck does not open: {e}"


def _case_row(sess: pipeline.Session, out: Path, seconds: float) -> dict:
    rv = sess.last_review
    folder = Path(rv.folder)
    ok, why = _completed(folder)
    claims = rv.claims
    return dict(
        completed=ok, reason=why or None, resolved=rv.resolved, blocking=len(rv.blocking),
        blocking_kinds=dict(Counter(i["kind"] for i in rv.blocking)),
        reviewer_issues=sum(i["source"] == "reviewer" for i in rv.issues),
        claims_total=len(claims), claims_passed=sum(bool(c.get("passed")) for c in claims),
        rounds=[{k: r[k] for k in ("stage", "blocking", "kinds", "notes")} for r in sess.rounds],
        artifact=folder.relative_to(out).as_posix(), usage=sess.trace.usage(),
        seconds=round(seconds, 1),
    )


def run(args) -> None:
    if args.offline:
        from posteragent import offline
        llm.set_backend(offline.backend)
    warnings: list[str] = []
    if args.plan:
        plan = read(args.plan)
        if args.split == "evaluation" and not args.allow_unfrozen:
            warnings = _check_frozen(plan, args.offline)
        if "thresholds" not in plan:
            raise SystemExit("plan.json has no thresholds; re-create it with `evaluate.py split`.")
        try:
            config.apply_thresholds(plan["thresholds"])
        except ValueError as e:
            raise SystemExit(str(e))
        names = plan[args.split]
        papers = [args.papers / name for name in names]
        missing = [str(p) for p in papers if not p.is_file()]
        if missing:
            raise SystemExit(f"missing paper PDFs: {missing[:3]}")
        plan = {**plan, "evaluation": names}
    else:
        plan = dict(evaluation=[SAMPLE.name], dialogues={SAMPLE.name: [
            ["Enlarge Figure 1 by 30% and keep the results text exactly as it is", "feasible"],
            ["Shorten the method section to 12 words", "feasible"],
            ["Now shorten the text you kept unchanged to 5 words", "conflict"]]},
            size_in=[48, 36], areas=[1, 0.75])
        papers = [SAMPLE]
    args.out.mkdir(parents=True, exist_ok=False)
    write(args.out / "protocol.json", plan)
    for w in warnings:
        print(f"warning: {w}")
    write(args.out / "settings.json", dict(
        backend=llm.backend_name(), **_models(), split=args.split,
        reviewer=not args.no_reviewer, git_commit=_git(), plan_frozen=bool(plan.get("frozen")),
        freeze=plan.get("freeze"), warnings=warnings,
        prompt_digest=_digest(*PROMPT_FILES), code_digest=_digest(*CODE_FILES),
        thresholds_applied=config.thresholds(), started=time.strftime("%Y-%m-%d %H:%M:%S")))

    standard = config.PosterSize(*plan.get("size_in", [48, 36]))
    use_reviewer = not args.no_reviewer
    cases, dialogues, preparation, blind, turn_material = [], [], [], [], []
    for index, paper in enumerate(papers):
        name = plan["evaluation"][index]
        pdir = args.out / f"paper-{index:02d}"
        script = plan.get("dialogues", {}).get(name)
        print(f"[{index + 1}/{len(papers)}] {name}", flush=True)
        t0 = time.time()
        try:
            draft = pipeline.prepare(paper, pdir / "draft")
            preparation.append(dict(paper=name, ok=True, claims=len(draft.claims),
                                    claim_failures=draft.claim_failures,
                                    asset_problems=draft.asset_problems,
                                    usage=draft.trace.usage(), seconds=round(time.time() - t0, 1)))
            (pdir / "draft" / "trace.json").write_text(json.dumps(
                {"usage": draft.trace.usage(), "events": draft.trace.events}, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - the denominator keeps failures
            reason = f"preparation failed: {type(e).__name__}: {e}"
            preparation.append(dict(paper=name, ok=False, reason=reason, trace=traceback.format_exc()[-2000:]))
            for area in plan["areas"]:
                for cond in pipeline.ORDERS:
                    cases.append(dict(paper=name, area=area, condition=cond, completed=False, reason=reason))
            for cond in dialogue.CONDITIONS:
                for turn, (req, label) in enumerate(script or [], 1):
                    dialogues.append(dict(paper=name, condition=cond, turn=turn, request=req, label=label,
                                          status="unavailable", reason=reason))
            continue

        joint_standard = None
        for area in plan["areas"]:
            size = standard if area == 1 else standard.shrunk(area)
            adir = pdir / f"area-{area}"
            try:
                base = pipeline.initial_session(draft, size, adir / "initial", use_reviewer=use_reviewer)
                pipeline.save(base)
            except Exception as e:  # noqa: BLE001
                reason = f"initial layout failed: {type(e).__name__}: {e}"
                for cond in pipeline.ORDERS:
                    cases.append(dict(paper=name, area=area, condition=cond, completed=False, reason=reason))
                continue
            for cond in pipeline.ORDERS:
                row = dict(paper=name, area=area, condition=cond,
                           size_in=[round(size.w_in, 2), round(size.h_in, 2)])
                t1 = time.time()
                try:
                    sess = pipeline.fork(base, adir / cond, cond)
                    pipeline.run_schedule(sess, cond, use_reviewer=use_reviewer)
                    row.update(_case_row(sess, args.out, time.time() - t1))
                    if row["completed"]:
                        blind.append(row)
                    if cond == "joint" and area == 1:
                        joint_standard = sess
                except Exception as e:  # noqa: BLE001
                    row.update(completed=False, reason=f"{type(e).__name__}: {e}",
                               trace=traceback.format_exc()[-2000:], seconds=round(time.time() - t1, 1))
                cases.append(row)
                print(f"    area {area} {cond}: " + ("ok" if row["completed"] else "FAILED")
                      + (f", {row['blocking']} blocking" if row.get("completed") else f" ({row.get('reason')})"),
                      flush=True)

        if not script:
            continue
        for cond in dialogue.CONDITIONS:
            if joint_standard is None:
                for turn, (req, label) in enumerate(script, 1):
                    dialogues.append(dict(paper=name, condition=cond, turn=turn, request=req, label=label,
                                          status="unavailable", reason="starting poster unavailable"))
                continue
            ddir = args.out / "dialogues" / f"{index:02d}-{cond}"
            sess = pipeline.fork(joint_standard, ddir, cond)
            state = dialogue.start(sess, cond)
            for turn, (req, label) in enumerate(script, 1):
                locks = [p.text for p in sess.protected]
                start = len(sess.trace.calls)
                try:
                    rec = dialogue.apply_request(state, req)
                    row = dict(paper=name, case=f"{index:02d}-{cond}", label=label,
                               usage=sess.trace.usage(start), **rec.to_json())
                except Exception as e:  # noqa: BLE001
                    row = dict(paper=name, case=f"{index:02d}-{cond}", condition=cond, turn=turn,
                               request=req, label=label, status="error", reason=f"{type(e).__name__}: {e}")
                dialogues.append(row)
                if row.get("before") and row.get("artifact"):
                    turn_material.append((row, ddir, dict(
                        requests=[h.request for h in state.history],
                        previous_protection=locks, protected=[p.text for p in sess.protected],
                        decision=row.get("message"))))
            write(ddir / "preservation.json", dialogue.preservation_report(state))

    write(args.out / "results.json", dict(attempted=len(cases), completed=sum(c["completed"] for c in cases),
                                          preparation=preparation, cases=cases, dialogues=dialogues))
    _blind(args.out, blind, turn_material)
    summarize(args.out)
    print(args.out / "summary.json")


def _blind(out: Path, blind: list[dict], turn_material: list) -> None:
    """Anonymised previews and empty rater sheets. Scores must come from
    two independent evaluators using checklists the system never sees."""
    random.Random(702).shuffle(blind)
    key = {}
    folder = out / "blinded"
    folder.mkdir()
    for i, row in enumerate(blind, 1):
        ident = f"poster-{i:03d}"
        shutil.copy2(out / row["artifact"] / "preview.png", folder / f"{ident}.png")
        key[ident] = {k: row[k] for k in ("paper", "area", "condition", "artifact")}
    write(out / "blinding-key.json", key)
    for judge in (1, 2):
        order = list(key)
        random.Random(702 + judge).shuffle(order)
        with (folder / f"rater-{judge}.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["poster", "paper", *POSTER, "notes"])
            w.writerows([i, key[i]["paper"]] for i in order)
    random.Random(705).shuffle(turn_material)
    turn_key = {}
    for i, (row, ddir, instructions) in enumerate(turn_material, 1):
        ident = f"turn-{i:03d}"
        turn_key[ident] = {k: row.get(k) for k in ("paper", "case", "condition", "turn", "label",
                                                   "status", "before", "artifact")}
        for stage in ("before", "artifact"):
            src = ddir / row[stage] / "preview.png"
            if src.is_file():
                shutil.copy2(src, folder / f"{ident}-{'before' if stage == 'before' else 'after'}.png")
        write(folder / f"{ident}.json", instructions)
    write(out / "dialogue-key.json", turn_key)
    for judge in (1, 2):
        order = list(turn_key)
        random.Random(705 + judge).shuffle(order)
        with (folder / f"dialogue-rater-{judge}.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["poster", "paper", *TURN, "notes"])
            w.writerows([i, turn_key[i]["paper"]] for i in order)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("split", help="sample 5 development + 15 evaluation papers")
    sp.add_argument("papers", type=Path)
    sp.add_argument("plan", type=Path)
    sp.add_argument("--seed", type=int, default=56)
    sp.add_argument("--size", type=float, nargs=2, default=[48, 36], metavar=("W_IN", "H_IN"))
    rp = sub.add_parser("run", help="run the frozen comparison")
    rp.add_argument("--papers", type=Path, default=Path("."))
    rp.add_argument("--plan", type=Path)
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--offline", action="store_true", help="scripted backend; tests the protocol only")
    rp.add_argument("--no-reviewer", action="store_true", help="skip the vision reviewer (ablation)")
    rp.add_argument("--split", choices=["development", "evaluation"], default="evaluation",
                    help="which papers of the plan to run (default: evaluation)")
    rp.add_argument("--allow-unfrozen", action="store_true",
                    help="dry run of the evaluation split without a valid freeze")
    fz = sub.add_parser("freeze", help="seal size, thresholds, models and prompts in the plan")
    fz.add_argument("plan", type=Path)
    sm = sub.add_parser("summarize", help="recompute summary.json after raters fill the CSVs")
    sm.add_argument("run", type=Path)
    args = parser.parse_args()
    if args.cmd == "split":
        freeze(args)
    elif args.cmd == "freeze":
        freeze_plan(args)
    elif args.cmd == "run":
        run(args)
    else:
        summarize(args.run)
        print(args.run / "summary.json")


if __name__ == "__main__":
    main()
