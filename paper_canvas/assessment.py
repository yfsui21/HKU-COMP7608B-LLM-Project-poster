"""Summarize the frozen experiment without treating missing scores or costs as zero."""
import csv
from collections import defaultdict
from statistics import mean

from .agent import read, write

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


def usage(root):
    import json
    rows=[]
    for path in root.rglob("trace.jsonl"):
        for line in path.read_text(encoding="utf8").splitlines():
            e=json.loads(line)
            if e["event"]=="usage":
                rows.append(e)
    costs=[e["estimated_usd"] for e in rows if e["estimated_usd"] is not None]
    calls=[c for e in rows for c in e["calls"]]
    tokens=[c["usage"] for c in calls if c["usage"].get("reported",True) and
            all(k in c["usage"] for k in ("prompt_tokens","completion_tokens"))]
    return dict(operations=len(rows),calls=len(calls),elapsed_seconds=sum(r["elapsed_s"] for r in rows),
                tokens_reported_calls=len(tokens),
                tokens=sum(t["prompt_tokens"]+t["completion_tokens"] for t in tokens) if len(tokens)==len(calls) else None,
                cost_known_operations=len(costs),estimated_usd=sum(costs) if len(costs)==len(rows) and rows else None)


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
    resources=[]
    for row in results["cases"]:
        conditions[(row["condition"],row["area"])].append(row)
        path=row.get("session") or (str((root/row["artifact"]).parent.relative_to(root)) if row.get("artifact") else None)
        if path:
            resources.append(dict(paper=row["paper"],area=row["area"],condition=row["condition"],**usage(root/path)))
    groups=[]
    for (condition,area),rows in conditions.items():
        values=[scores[(r["paper"],area,condition)] for r in rows if (r["paper"],area,condition) in scores]
        groups.append(dict(condition=condition,area=area,completion=rates(rows,lambda r:r["completed"]),
                           double_rated=len(values),means={k:mean(v[k] for v in values) for k in values[0]} if values else {}))
    turns=results["dialogues"]
    by_schedule={}
    for condition in sorted({t.get("condition",t["case"].split("-",1)[1]) for t in turns}):
        rows=[t for t in turns if t.get("condition",t["case"].split("-",1)[1])==condition]
        by_schedule[condition]=dict(feasible=rates((r for r in rows if r.get("label")=="feasible"),lambda r:r.get("accepted")),
            conflicts=rates((r for r in rows if r.get("label")=="conflict"),lambda r:r.get("conflict") and not r.get("accepted")),
            unavailable=sum(r.get("completed") is False for r in rows))
    turn_key=read(root/"dialogue-key.json") if (root/"dialogue-key.json").exists() else {}
    turn_scores=ratings(root,turn_key,"dialogue-rater",TURN)
    human_dialogue=[]
    for condition in by_schedule:
        for label in ("feasible","conflict"):
            pairs=[v for ident,v in turn_scores["per_output"].items() if
                   turn_key[ident]["condition"]==condition and turn_key[ident]["label"]==label]
            human_dialogue.append(dict(condition=condition,label=label,scored=len(pairs),
                       means={k:mean(v[k] for v in pairs) for k in TURN} if pairs else {}))
    report=dict(completion=rates(results["cases"],lambda r:r["completed"]),groups=groups,
                failures=[r for r in results["cases"] if not r["completed"]],usage=usage(root),generation_usage=resources,
                dialogue=by_schedule,poster_ratings=posters,dialogue_ratings=turn_scores,
                dialogue_human_means=human_dialogue,paired_differences=differences)
    write(root/"summary.json",report)
    return report
