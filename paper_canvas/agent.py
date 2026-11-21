"""Bounded poster generation, review and transactional dialogue edits."""
import copy
import json
import io
import shutil
import math
import os
import re
import time
import uuid
from pathlib import Path
from . import conference, extract, forest_example, model, source
from .details import enrich
from PIL import Image
from .render import IDS, render, scene

ORDERS = {"joint":["content","layout"]*2, "content-first":["content"]*2+["layout"]*2,
          "layout-first":["layout"]*2+["content"]*2}

def write(path, data):
    temp = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf8")
    temp.replace(path)

def read(path):
    return json.loads(path.read_text(encoding="utf8"))

def trace(folder, event, **data):
    with (folder/"trace.jsonl").open("a",encoding="utf8") as f:
        f.write(json.dumps(dict(event=event,**data),ensure_ascii=False)+"\n")

def section(s,key):
    return s["sections"][IDS.index(key)]

def context(s):
    return {k:v for k,v in s.items() if k!="history"} | {"recent_turns":s["history"][-3:]}

def prepare(pdf,folder,size,api):
    if not all(144 <= n <= 4032 for n in size):
        raise ValueError("Dimensions must be 144–4032 points per side")
    pages = source.read_paper(pdf)
    if api.live:
        response = api.ask(model.PLAN,pages)
        plan = {key:response[key] for key in ("title","sections","evidence")}
    elif pdf.read_bytes()==source.EXAMPLE.read_bytes():
        plan = source.fixture_plan(pdf)
    elif forest_example.matches(pdf) and size[0]>=880 and size[1]>=620 and size[0]>=size[1]:
        plan = forest_example.plan()
    else:
        plan = extract.plan(pdf,pages)
    enrich(plan,pages)
    if not plan.get("conference"):
        conference.prepare(plan,pdf,folder,pages,size)
    conference.save_exhibits(plan,pdf,folder)
    source.verify(plan,pages)
    secondary=plan.get("secondary")
    if secondary and (size[1]>=640 or plan.get("conference")):
        picture=source.page_png(pdf,secondary["page"],secondary["box"])
        with Image.open(io.BytesIO(picture)) as im:
            secondary["aspect"]=im.width/im.height
        (folder/"support.png").write_bytes(picture)
    else:
        plan.pop("secondary",None)
    ev = plan["evidence"]
    if api.live:
        ev["box"] = api.ask(model.CROP,ev,source.page_png(pdf,ev["page"]))["box"]
    asset = source.page_png(pdf,ev["page"],ev["box"])
    (folder/"evidence.png").write_bytes(asset)
    state=dict(**plan,size=size,left_fraction=.43,figure_fraction=.40,figure_scale=1.0 if plan.get("conference") else .80,
               history=[],protected={},limits={},model=api.name)
    if api.live:
        layout_pass(state,["Allocate initial space from the selected evidence and canvas size."],api)
    return state

def width(s,asset):
    items,_ = scene(s,asset)
    return next(a["w"] for a in items if a["id"]=="figure")

def copy_evidence(origin,destination):
    for filename in ("evidence.png","support.png"):
        if (origin/filename).is_file():
            shutil.copy2(origin/filename,destination/filename)
    for path in origin.glob("exhibit-*.png"):
        shutil.copy2(path,destination/path.name)

def freeze_boxes(s,asset):
    items,_=scene(s,asset)
    s["fixed_boxes"]={a["id"]:{k:a[k] for k in ("x","y","w","h")} for a in items}

def constraints(s,asset):
    issues=[]
    for key,p in zip(IDS,s["sections"]):
        if any(t.casefold() not in p["text"].casefold() for t in p["required"]):
            issues.append(key+" lost required source context")
        if key in s["protected"] and p["text"]!=s["protected"][key]:
            issues.append(key+" changed protected text")
        limit=s["limits"].get(key)
        if limit is not None and len(p["text"].split())>limit:
            issues.append(key+" exceeds word limit")
    if width(s,asset)+.1<s["limits"].get("figure",0):
        issues.append("figure below requested width")
    return issues

def review(s,asset,folder,api):
    boxes,issues=render(s,asset,folder)
    issues.extend(constraints(s,asset))
    if api.live:
        observed=api.ask(model.REVIEW,context(s),(folder/"preview.png").read_bytes())["issues"]
        if not isinstance(observed,list):
            raise ValueError("Reviewer issues must be a list")
        issues.extend(observed)
    write(folder/"review.json",dict(issues=issues,elements=boxes))
    return issues

def content_pass(s,issues,api):
    if api.live:
        texts=api.ask(model.CONTENT,dict(poster=context(s),issues=issues))["texts"]
        if len(texts)!=3 or not all(isinstance(t,str) and t.strip() for t in texts):
            raise ValueError("Editor must supply three text blocks")
    else:
        texts=[p["text"] for p in s["sections"]]
        # Recorded alternatives for this one synthetic paper.
        choices=["Does review reduce labeling errors?", "Two annotators reviewed 40 synthetic field records.",
                 "On 40 synthetic records, paired review cut errors from 12 to 5."]
        for i,key in enumerate(IDS):
            if len(texts[i].split())>s["limits"].get(key,math.inf) or (issues and s.get("planner")=="extractive"):
                texts[i]=s["sections"][i]["quote"] if s.get("planner")=="extractive" else choices[i]
    for key,p,text in zip(IDS,s["sections"],texts):
        if key not in s["protected"] and all(t.lower() in text.lower() for t in p["required"]):
            p["text"]=text

def fit_requested_figure(s,asset):
    if "figure" in s["limits"]:
        s["figure_scale"]=1
        s["figure_scale"]=max(.10,s["limits"]["figure"]/width(s,asset))

def layout_pass(s,issues,api,asset=None):
    if api.live:
        values=api.ask(model.LAYOUT,dict(poster=context(s),issues=issues))
        for key,low,high in [("left_fraction",.30,.55),("figure_fraction",.20,.60)]:
            s[key]=max(low,min(high,float(values[key])))
    elif issues and asset:
        # Choose the smallest reallocation that passes measured scene checks.
        choices=[]
        for left in (.30,.34,.38,.42,.46,.50,.55,s["left_fraction"]):
            for height in (.20,.30,.40,.50,.60,s["figure_fraction"]):
                candidate=dict(s,left_fraction=left,figure_fraction=height)
                fit_requested_figure(candidate,asset)
                _,geometry=scene(candidate,asset)
                score=(len(geometry)+len(constraints(candidate,asset)),
                       abs(left-s["left_fraction"])+abs(height-s["figure_fraction"]))
                choices.append((score,left,height,candidate["figure_scale"]))
        _,s["left_fraction"],s["figure_fraction"],s["figure_scale"]=min(choices)

def cost_record(api,folder,start,offset):
    calls=api.calls[offset:]
    rates=[os.getenv("PAPERCANVAS_"+k+"_USD_PER_M") for k in ("INPUT","OUTPUT")]
    cost=0 if not api.live else None
    if api.live and all(rates) and all("prompt_tokens" in c["usage"] and "completion_tokens" in c["usage"] for c in calls):
        cost=sum(c["usage"]["prompt_tokens"]*float(rates[0])+c["usage"]["completion_tokens"]*float(rates[1]) for c in calls)/1e6
    trace(folder,"usage",model=api.name,calls=calls,estimated_usd=cost,elapsed_s=round(time.monotonic()-start,3))

def generate(pdf,folder,size,condition,api,initial=None):
    folder.mkdir(parents=True,exist_ok=False)
    start,offset=time.monotonic(),len(api.calls)
    try:
        if initial:
            s=copy.deepcopy(read(initial/"initial.json"))
            copy_evidence(initial,folder)
        else:
            s=prepare(pdf,folder,size,api)
        s["condition"]=condition
        write(folder/"initial.json",s)
        asset=(folder/"evidence.png").read_bytes()
        issues=review(s,asset,folder/"00-initial",api)
        trace(folder,"initial",issues=issues)
        for i,role in enumerate(ORDERS[condition],1):
            if condition=="layout-first" and i==3:
                freeze_boxes(s,asset)
            if role=="content":
                content_pass(s,issues,api)
            else:
                layout_pass(s,issues,api,asset)
            dest=folder/f"{i:02}-{role}"
            issues=review(s,asset,dest,api)
            trace(folder,"pass",role=role,issues=issues,artifact=dest.name)
        if issues:
            raise ValueError("Review unresolved after two rounds: "+json.dumps(issues,ensure_ascii=False))
        s["artifact"]=dest.name
        write(folder/"state.json",s)
        return s
    except Exception as exc:
        trace(folder,"generation_failed",reason=str(exc))
        raise
    finally:
        cost_record(api,folder,start,offset)

def intent(request,s,api):
    if api.live:
        return api.ask(model.INTENT,dict(request=request,history=s["history"],protected=s["protected"],limits=s["limits"]))
    t=request.lower().strip().rstrip(".")
    if "protected section" in t and len(s["protected"])==1:
        t=t.replace("protected section",next(iter(s["protected"])))
    # The fixture accepts transparent commands and one history reference.
    if "it" in t.split() and s["history"]:
        previous=next((v.get("target") for v in reversed(s["history"]) if v.get("accepted")),None)
        if previous:
            t=re.sub(r"\bit\b",previous,t)
    match=re.fullmatch(r"enlarge figure by (\d+(?:\.\d+)?)%(?: and protect (question|method|result))?",t)
    if match:
        return dict(target="figure",operation="enlarge",value=1+float(match[1])/100,protect=[match[2]] if match[2] else [])
    match=re.fullmatch(r"shorten (question|method|result) to (\d+) words",t)
    if match:
        return dict(target=match[1],operation="shorten",value=int(match[2]),protect=[])
    match=re.fullmatch(r"protect (question|method|result)",t)
    if match:
        return dict(target=match[1],operation="protect",protect=[match[1]])
    return dict(operation="clarify")

def edit(folder,request,api):
    # Exclusive single-writer guard. state.json is the only committed pointer.
    guard=folder/".editing"
    with guard.open("x"):
        pass
    start,offset=time.monotonic(),len(api.calls)
    try:
        old=read(folder/"state.json")
        if old["model"]!=api.name:
            raise ValueError("Resume using the original model setting")
        s=copy.deepcopy(old)
        asset=(folder/"evidence.png").read_bytes()
        if s["condition"]=="layout-first" and "fixed_boxes" not in s:
            freeze_boxes(s,asset)
        record=dict(request=request,accepted=False)
        try:
            action=intent(request,s,api)
            op,key=action["operation"],action.get("target")
            record.update(target=key,operation=op)
            if op=="clarify":
                raise ValueError("Clarify the element, requested change and measurable target.")
            for protected in action.get("protect",[]):
                s["protected"][protected]=section(old,protected)["text"]
            if op=="protect" and key in IDS:
                s["protected"][key]=section(old,key)["text"]
            elif op=="shorten" and key in IDS:
                limit=int(action["value"])
                if limit<1 or key in s["protected"] and len(section(old,key)["text"].split())>limit:
                    raise ValueError("Word limit conflicts with protected text or is invalid.")
                s["limits"][key]=limit
            elif op=="enlarge" and key=="figure" and 1<float(action["value"])<=10:
                s["limits"]["figure"]=width(old,asset)*float(action["value"])
            elif op!="protect" or key not in IDS:
                raise ValueError("Unsupported edit. Specify figure width or a section word limit.")
            issues=[]
            for i in range(2):
                if op!="protect":
                    content_pass(s,issues,api)
                    if s["condition"]=="joint" and i:
                        layout_pass(s,issues,api,asset)
                    fit_requested_figure(s,asset)
                dest=folder/("edit-"+uuid.uuid4().hex[:10])
                issues=review(s,asset,dest,api)
                trace(folder,"attempt",request=request,attempt=i+1,issues=issues,artifact=dest.name)
                if not issues:
                    s["artifact"]=dest.name
                    record.update(accepted=True,reason="Applied and checked.")
                    break
            if not record["accepted"]:
                raise ValueError("Unresolved after two attempts: "+json.dumps(issues))
        except Exception as exc:
            s=old
            record["reason"]=str(exc)
        s["history"].append(record)
        write(folder/"state.json",s)
        trace(folder,"edit",**record)
        return s
    finally:
        guard.unlink()
        cost_record(api,folder,start,offset)
