"""Proposal protocol: freeze split, compare schedules, run paired dialogue scripts."""
import argparse
import csv
import hashlib
import json
import math
import random
import shutil
from pathlib import Path

from paper_canvas.agent import ORDERS, edit, generate, read, write, copy_evidence
from paper_canvas.model import Model
from paper_canvas.source import EXAMPLE
from paper_canvas.assessment import POSTER, TURN, summarize

SCRIPT = [("enlarge figure by 35% and protect result","feasible"),
          ("shorten question to 6 words","feasible"),
          ("shorten protected section to 2 words","conflict")]

def freeze(args):
    files=sorted(p.relative_to(args.papers).as_posix() for p in args.papers.rglob("*.pdf"))
    if len(files)!=100:
        raise ValueError("Supply exactly 100 paper PDFs, with one per Paper2Poster pair. Keep reference posters separate.")
    rng=random.Random(args.seed)
    selected=rng.sample(files,20)
    write(args.plan,dict(seed=args.seed,development=selected[:5],evaluation=selected[5:],
                         dialogues={p:SCRIPT for p in rng.sample(selected[5:],5)},size=[1008,540]))
    print("Review feasibility labels and dimensions on development data, then freeze the plan before testing.")

def run(args):
    args.out.mkdir(parents=True,exist_ok=False)
    plan=read(args.plan) if args.plan else dict(evaluation=[EXAMPLE.name],dialogues={EXAMPLE.name:SCRIPT},size=[1008,540])
    api=Model(bool(args.plan) and not args.offline)
    if args.forest_result and len(plan["evaluation"])!=1:
        raise ValueError("--forest-result selects one paper. Use --forest-root for a multi-paper study.")
    write(args.out/"protocol.json",plan)
    metadata=dict(model=api.name,temperature=0 if api.live and not(api.queue or api.codex) else None,
                  max_completion_tokens_per_call=1800 if api.live and not(api.queue or api.codex) else None,
                  prompt_digest=hashlib.sha256(Path("paper_canvas/model.py").read_bytes()).hexdigest())
    write(args.out/"settings.json",metadata)
    results,dialogues,blind=[],[],[]
    turn_material=[]
    for index,name in enumerate(plan["evaluation"]):
        paper=(args.papers/name) if args.plan else EXAMPLE
        for area in (1,.75):
            size=[v*math.sqrt(area) for v in plan["size"]]
            base=args.out/f"paper-{index:02}-area-{area}"
            initial=base/"joint"
            for condition in ORDERS:
                dest=base/condition
                row=dict(paper=name,area=area,condition=condition,completed=False,session=dest.relative_to(args.out).as_posix())
                try:
                    if condition!="joint" and not (initial/"initial.json").exists():
                        raise ValueError("Shared parsing/initialization failed")
                    result=args.forest_result or (args.forest_root/paper.stem if args.forest_root else None)
                    s=generate(paper,dest,size,condition,api,None if condition=="joint" else initial,forest_result=result)
                    row.update(completed=True,artifact=(dest/s["artifact"]).relative_to(args.out).as_posix())
                    checks=read(dest/s["artifact"]/"review.json")
                    row["claims_checked"]=len(checks.get("claims",[]))
                    row["issues"]=checks["issues"]
                    blind.append(row)
                except Exception as exc:
                    row["reason"]=str(exc)
                results.append(row)
            if area==1 and name in plan["dialogues"]:
                for condition in ("joint","layout-first"):
                    key=f"{index:02}-{condition}"
                    if not (initial/"state.json").exists():
                        for turn,(request,label) in enumerate(plan["dialogues"][name],1):
                            dialogues.append(dict(case=key,paper=name,condition=condition,turn=turn,request=request,label=label,
                                                  completed=False,reason="Starting poster unavailable"))
                        continue
                    dest=args.out/"dialogues"/key
                    dest.mkdir(parents=True)
                    s=read(initial/"state.json")
                    s["condition"]=condition
                    write(dest/"state.json",s)
                    copy_evidence(initial,dest)
                    shutil.copytree(initial/s["artifact"],dest/s["artifact"])
                    for turn,(request,label) in enumerate(plan["dialogues"][name],1):
                        before=s["artifact"]
                        locks=dict(s["protected"])
                        s=edit(dest,request,api)
                        row=dict(case=key,paper=name,condition=condition,turn=turn,label=label,before=before,after=s["artifact"],**s["history"][-1])
                        dialogues.append(row)
                        turn_material.append((row,dest,dict(requests=[r["request"] for r in s["history"]],
                            previous_protection=locks,protected=s["protected"],decision=row["reason"])))
    write(args.out/"results.json",dict(attempted=len(results),completed=sum(r["completed"] for r in results),
                                       cases=results,dialogues=dialogues))
    # Scores and source checklists must be supplied independently by human evaluators.
    random.Random(702).shuffle(blind)
    key={}
    blinded=args.out/"blinded"
    blinded.mkdir()
    for i,row in enumerate(blind,1):
        identifier=f"poster-{i:03}"
        shutil.copy2(args.out/row["artifact"]/"preview.png",blinded/(identifier+".png"))
        key[identifier]=row
    write(args.out/"blinding-key.json",key)
    for judge in (1,2):
        order=list(key)
        random.Random(702+judge).shuffle(order)
        with (blinded/f"rater-{judge}.csv").open("w",encoding="utf8",newline="") as f:
            writer=csv.writer(f)
            writer.writerow(["poster","paper",*POSTER,"notes"])
            for identifier in order:
                writer.writerow([identifier,key[identifier]["paper"]])
    random.Random(705).shuffle(turn_material)
    turn_key={}
    for i,(row,dest,instructions) in enumerate(turn_material,1):
        ident=f"turn-{i:03}"
        turn_key[ident]=row
        for stage in ("before","after"):
            shutil.copy2(dest/row[stage]/"preview.png",blinded/f"{ident}-{stage}.png")
        write(blinded/f"{ident}.json",instructions)
    write(args.out/"dialogue-key.json",turn_key)
    for judge in (1,2):
        order=list(turn_key)
        random.Random(705+judge).shuffle(order)
        with (blinded/f"dialogue-rater-{judge}.csv").open("w",encoding="utf8",newline="") as f:
            writer=csv.writer(f)
            writer.writerow(["poster","paper",*TURN,"notes"])
            writer.writerows([i,turn_key[i]["paper"]] for i in order)
    summarize(args.out)
    print(args.out/"summary.json")

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="cmd",required=True)
    split=sub.add_parser("split")
    split.add_argument("papers",type=Path)
    split.add_argument("plan",type=Path)
    split.add_argument("--seed",type=int,default=56)
    runp=sub.add_parser("run")
    runp.add_argument("--papers",type=Path,default=Path("."))
    runp.add_argument("--plan",type=Path)
    runp.add_argument("--offline",action="store_true",help="Replay frozen content and run geometric checks locally")
    sources=runp.add_mutually_exclusive_group()
    sources.add_argument("--forest-result",type=Path)
    sources.add_argument("--forest-root",type=Path,help="Generated result directories indexed by paper stem")
    runp.add_argument("--out",type=Path,required=True)
    summary=sub.add_parser("summarize")
    summary.add_argument("run",type=Path)
    args=parser.parse_args()
    if args.cmd=="summarize":
        summarize(args.run)
        print(args.run/"summary.json")
    else:
        (freeze if args.cmd=="split" else run)(args)
