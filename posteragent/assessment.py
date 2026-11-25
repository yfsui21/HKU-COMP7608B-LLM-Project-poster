"""Summarize the frozen experiment without treating missing scores or costs as zero.

Human ratings come from the blinded rater CSVs written by `evaluate.py run`;
automatic measures (completion, resolution, blocking issues, claim checks,
calls, tokens, cost) come from results.json. They are reported separately:
the automatic checks share the system's own requirements, the raters use an
independent checklist the system never sees.
"""
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

POSTER = ("checklist_met","checklist_total","wrong_numbers","figure_mismatches","overflow","overlaps",
          "outside","label_legibility_1_5","reading_order_1_5")
TURN = ("request_met","protected_preserved","reference_resolved","new_factual_errors","new_layout_errors","conflict_detected")


def rates(rows, fn):
    rows = list(rows)
    successes = sum(bool(fn(r)) for r in rows)
    return dict(n=len(rows),successes=successes,rate=successes/len(rows) if rows else None)


def ratings(root, key, prefix, fields):
    judges = []
    for n in (1,2):
        path=root/"blinded"/f"{prefix}-{n}.csv"
        scores={}
        if path.exists():
            with path.open(encoding="utf-8-sig",newline="") as f:
                for row in csv.DictReader(f):
                    ident=row["poster"]
                    if ident not in key or ident in scores:
                        raise ValueError(f"Unknown or duplicate blind ID {ident} in {path}")
                    values=[row.get(col) for col in fields]
                    if not any(v not in ("",None) for v in values):
                        scores[ident]=None
                        continue
                    if not all(v is not None and v.strip().isdigit() for v in values):
                        raise ValueError(f"Incomplete or invalid scores for {ident} in {path}")
                    cells=dict(zip(fields,map(int,values)))
                    if any(not 1<=v<=5 for k,v in cells.items() if k.endswith("_1_5")):
                        raise ValueError(f"Readability rating outside 1–5 for {ident}")
                    if fields==TURN and any(cells[k]>1 for k in ("request_met","protected_preserved","reference_resolved","conflict_detected")):
                        raise ValueError(f"Expected binary dialogue scores for {ident}")
                    if fields==POSTER:
                        if not 0<=cells["checklist_met"]<=cells["checklist_total"] or not cells["checklist_total"]:
                            raise ValueError(f"Invalid checklist denominator for {ident}")
                        cells["coverage"]=cells["checklist_met"]/cells["checklist_total"]
                    scores[ident]=cells
        judges.append({k:v for k,v in scores.items() if v is not None})
    paired=sorted(judges[0].keys() & judges[1].keys())
    avg,agreement={},{}
    for ident in paired:
        a,b=(judge[ident] for judge in judges)
        if fields==POSTER and a["checklist_total"]!=b["checklist_total"]:
            raise ValueError("Independent raters must use the same frozen checklist: "+ident)
        avg[ident]={k:(a[k]+b[k])/2 for k in a}
    if paired:
        for k in avg[paired[0]]:
            diffs=[abs(judges[0][i][k]-judges[1][i][k]) for i in paired]
            agreement[k]=dict(pairs=len(diffs),exact_agreement=mean(d==0 for d in diffs),mean_absolute_difference=mean(diffs))
    return dict(expected=len(key),scored_per_rater=[len(s) for s in judges],jointly_scored=len(paired),
                agreement=agreement,per_output=avg)


def usage(rows):
    """Sum per-case usage. Tokens/cost are null unless every case reported them."""
    rows=[r for r in rows if r.get("usage")]
    us=[r["usage"] for r in rows]
    costs=[u.get("estimated_usd") for u in us]
    return dict(cases=len(us),calls=sum(u["calls"] for u in us),
                failed_calls=sum(u.get("failed_calls",0) for u in us),
                prompt_tokens=sum(u["prompt_tokens"] for u in us),
                completion_tokens=sum(u["completion_tokens"] for u in us),
                model_seconds=round(sum(u.get("model_seconds",0) for u in us),1),
                wall_seconds=round(sum(r.get("seconds",0) for r in rows),1),
                estimated_usd=round(sum(costs),4) if us and all(c is not None for c in costs) else None)


def automatic(rows):
    """Per (condition, area): completion, resolution and blocking issue counts."""
    done=[r for r in rows if r["completed"]]
    kinds=defaultdict(int)
    for r in done:
        for k,v in r.get("blocking_kinds",{}).items():
            kinds[k]+=v
    return dict(completion=rates(rows,lambda r:r["completed"]),
                resolved=rates(done,lambda r:r.get("resolved")),
                mean_blocking_issues=mean(r["blocking"] for r in done) if done else None,
                blocking_by_kind=dict(kinds),
                claims_passed=sum(r.get("claims_passed",0) for r in done),
                claims_total=sum(r.get("claims_total",0) for r in done),
                usage=usage(rows))


def summarize(root):
    results=read(root/"results.json")
    key=read(root/"blinding-key.json")
    posters=ratings(root,key,"rater",POSTER)
    conditions=defaultdict(list)
    scores={}
    for ident, row in key.items():
        if ident in posters["per_output"]:
            scores[(row["paper"],row["area"],row["condition"])]=posters["per_output"][ident]
    differences=[]
    for (paper,area,condition), joint in scores.items():
        if condition!="joint":
            continue
        for baseline in ("content-first","layout-first"):
            other=scores.get((paper,area,baseline))
            if other:
                differences.append(dict(paper=paper,area=area,baseline=baseline,
                    joint_minus_baseline={k:v-other[k] for k,v in joint.items()}))
    for row in results["cases"]:
        conditions[(row["condition"],row["area"])].append(row)
    groups=[]
    for (condition,area),rows in sorted(conditions.items()):
        values=[scores[(r["paper"],area,condition)] for r in rows if (r["paper"],area,condition) in scores]
        groups.append(dict(condition=condition,area=area,automatic=automatic(rows),
                           double_rated=len(values),means={k:mean(v[k] for v in values) for k in values[0]} if values else {}))
    auto_pairs=[]
    by=dict(((r["paper"],r["area"],r["condition"]),r) for r in results["cases"] if r["completed"])
    for (paper,area,condition),r in by.items():
        if condition!="joint":
            continue
        for baseline in ("content-first","layout-first"):
            o=by.get((paper,area,baseline))
            if o:
                auto_pairs.append(dict(paper=paper,area=area,baseline=baseline,
                    blocking_joint_minus_baseline=r["blocking"]-o["blocking"],
                    claims_passed_joint_minus_baseline=r.get("claims_passed",0)-o.get("claims_passed",0)))
    turns=results["dialogues"]
    by_schedule={}
    for condition in sorted({t["condition"] for t in turns}):
        rows=[t for t in turns if t["condition"]==condition]
        by_schedule[condition]=dict(feasible=rates((r for r in rows if r.get("label")=="feasible"),lambda r:r.get("status")=="applied"),
            conflicts=rates((r for r in rows if r.get("label")=="conflict"),lambda r:r.get("conflict") and r.get("status")!="applied"),
            unavailable=sum(r.get("status")=="unavailable" for r in rows))
    turn_key=read(root/"dialogue-key.json") if (root/"dialogue-key.json").exists() else {}
    turn_scores=ratings(root,turn_key,"dialogue-rater",TURN)
    human_dialogue=[]
    for condition in by_schedule:
        for label in ("feasible","conflict"):
            pairs=[v for ident,v in turn_scores["per_output"].items() if
                   turn_key[ident]["condition"]==condition and turn_key[ident]["label"]==label]
            human_dialogue.append(dict(condition=condition,label=label,scored=len(pairs),
                       means={k:mean(v[k] for v in pairs) for k in TURN} if pairs else {}))
    report=dict(note=("Automatic measures use the system's own requirements; human ratings use the "
                      "independent checklist. Report them separately."),
                completion=rates(results["cases"],lambda r:r["completed"]),groups=groups,
                failures=[{k:r.get(k) for k in ("paper","area","condition","reason")} for r in results["cases"] if not r["completed"]],
                preparation=results.get("preparation",[]),
                usage=usage(results["cases"]),automatic_paired_differences=auto_pairs,
                dialogue=by_schedule,poster_ratings=posters,dialogue_ratings=turn_scores,
                dialogue_human_means=human_dialogue,paired_differences=differences)
    write(root/"summary.json",report)
    return report
