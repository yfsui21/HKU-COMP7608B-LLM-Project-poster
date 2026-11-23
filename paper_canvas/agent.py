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
from pptx import Presentation
from pathlib import Path
from . import conference, extract, forest, forest_example, model, source
from .details import enrich
from PIL import Image
from .render import IDS, render, scene

ORDERS = {"joint":["content","layout"]*2, "content-first":["content"]*2+["layout"]*2,
          "layout-first":["layout"]*2+["content"]*2}

LOCK_STALE_SECONDS = 600


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
    ev["sha256"]=forest.digest(asset)
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
    for filename in ("evidence.png","support.png","source.pdf"):
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
        if "forest" not in s and any(t.casefold() not in p["text"].casefold() for t in p["required"]):
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
    proofs=bool(getattr(api,"codex",None) and not getattr(api,"queue",None) and "forest" in s)
    boxes,issues=render(s,asset,folder,figure_proofs=proofs)
    issues.extend(constraints(s,asset))
    claims=[]
    if "forest" in s:
        claims=forest.check(s,boxes,folder/"poster.pptx",semantic=api.live)
    else:
        slide=Presentation(folder/"poster.pptx").slides[0]
        shapes={shape.name:shape for shape in slide.shapes}
        for key,p in zip(IDS,s["sections"]):
            text=forest.normalized(shapes[key].text) if key in shapes else ""
            missing=[t for t in p["required"] if forest.normalized(t) not in text]
            ev=s["evidence"]
            mismatch=key=="result" and forest.digest(shapes["figure"].image.blob)!=ev.get("sha256",forest.digest(asset))
            quotes=[{"page":p["page"],"quote":q} for q in (p["quote"],p.get("additional_quote")) if q]
            claims.append(dict(claim=key,elements=[key],text=text,source=quotes,conditions=p["required"],
                        assets=[ev["caption"]] if key=="result" else [],missing=missing,
                        mismatched=["figure"] if mismatch else [],unsupported_numbers=[],passed=not (missing or mismatch)))
    if api.live:
        preview=(folder/"preview.png").read_bytes()
        payload=dict(poster=context(s),claims=claims)
        if proofs:
            paths=sorted(folder.glob("*-proof.png"))
            payload["image_order"]=["whole poster",*[p.name for p in paths]]
            preview=[preview,*[p.read_bytes() for p in paths]]
        verdict=api.ask(model.CLAIM_REVIEW if claims else model.REVIEW,payload,preview)
        observed=verdict["issues"]
        if not isinstance(observed,list):
            raise ValueError("Reviewer issues must be a list")
        issues.extend(observed)
        if claims:
            votes=verdict["verdicts"]
            if set(votes)!={c["claim"] for c in claims} or any(type(v) is not bool for v in votes.values()):
                raise ValueError("Reviewer omitted or malformed a claim verdict")
            for c in claims:
                c["semantic_passed"]=votes[c["claim"]]
                aliases=verdict.get("equivalents",{}).get(c["claim"],{})
                accepted={term:phrase for term,phrase in aliases.items() if term in c["missing"]
                          and isinstance(phrase,str) and forest.normalized(phrase) and forest.normalized(phrase) in c["text"]
                          and re.findall(r"\d+(?:[.,]\d+)*",term)==re.findall(r"\d+(?:[.,]\d+)*",phrase)}
                c["equivalents"]=accepted
                c["missing"]=[v for v in c["missing"] if v not in accepted]
                c["passed"]=c["semantic_passed"] and not (c["missing"] or c["mismatched"] or c["unsupported_numbers"])
    for claim in claims:
        if not claim["passed"]:
            issues.append(dict(target=claim["claim"],problem="Unmet scientific requirement",
                               change=claim["missing"]+claim["mismatched"]+claim["unsupported_numbers"] or ["Restore source support"]))
    report=dict(issues=issues,elements=boxes,claims=claims,suggestions=verdict.get("suggestions",[]) if api.live else [])
    if "allocation" in s.get("forest",{}):
        report["panel_allocation"]=s["forest"]["allocation"]
    write(folder/"review.json",report)
    return issues

def content_pass(s,issues,api):
    if api.live:
        prompt=model.CONTENT
        if "forest" in s:
            allowed={f for k,fields in forest.groups(s["forest"].get("template")).items() if k not in s["protected"] for f in fields if f not in forest.TARGET}
            prompt=model.FOREST_CONTENT+" Optional fields may ONLY use these keys: "+", ".join(sorted(allowed))+". Never edit shape IDs or source labels."
        response=api.ask(prompt,dict(poster=context(s),issues=issues))
        texts=response["texts"]
        if len(texts)!=3 or not all(isinstance(t,str) and t.strip() for t in texts):
            raise ValueError("Editor must supply three text blocks")
        if "forest" in s and response.get("fields"):
            for key,value in response["fields"].items():
                if key not in allowed or not isinstance(value,str) or not value.strip():
                    raise ValueError("Unsupported or protected poster element: "+key)
                s["forest"].setdefault("edits",{})[key]=value
    elif "forest" in s:
        texts=[p["text"] for p in s["sections"]]
        for i,key in enumerate(IDS):
            short=s["forest"]["claims"][key].get("short")
            affected=any(key in (problem if isinstance(problem,str) else str(problem.get("target",""))) for problem in issues)
            if short and (affected or len(texts[i].split())>s["limits"].get(key,math.inf)) and len(short.split())<len(texts[i].split()):
                texts[i]=short
    else:
        texts=[p["text"] for p in s["sections"]]
        # Recorded alternatives for this one synthetic paper.
        choices=["Does review reduce labeling errors?", "Two annotators reviewed 40 synthetic field records.",
                 "On 40 synthetic records, paired review cut errors from 12 to 5."]
        for i,key in enumerate(IDS):
            if len(texts[i].split())>s["limits"].get(key,math.inf) or (issues and s.get("planner")=="extractive"):
                texts[i]=s["sections"][i]["quote"] if s.get("planner")=="extractive" else choices[i]
    for key,p,text in zip(IDS,s["sections"],texts):
        if key not in s["protected"] and (("forest" in s and api.live) or all(t.lower() in text.lower() for t in p["required"])):
            p["text"]=text

def fit_requested_figure(s,asset):
    if "figure" in s["limits"]:
        s["figure_scale"]=1
        s["figure_scale"]=max(.10,s["limits"]["figure"]/width(s,asset))

def allocate_panels(s, issues, asset, proposal):
    """Try adjacent width transfers; measure the entire candidate before adopting one."""
    f = s["forest"]
    geometry = forest.panel_geometry(f)
    if not geometry or "fixed_boxes" in s:
        return
    edges = geometry[2]
    items, problems = scene(s, asset)
    initial = set(problems)
    if not initial and not (issues and proposal):
        return
    panels = forest.PANELS
    preferred = (proposal.get("donor"), proposal.get("receiver")) if isinstance(proposal, dict) else ()
    total = edges[-1]
    best_score = (len(initial), 1, 0.)
    best = None
    for boundary in (1, 2):
        for fraction in (.015, .03, .05, .08, .12):
            for direction in (-1, 1):
                trial = edges.copy()
                trial[boundary] += total*fraction*direction
                donor, receiver = panels[boundary-1:boundary+1]
                if direction > 0:
                    donor, receiver = receiver, donor
                if min(b-a for a,b in zip(trial,trial[1:])) < total*.16:
                    continue
                ratios = [trial[1]/total, (trial[2]-trial[1])/(total-trial[1])]
                candidate = dict(s, forest=dict(f, split_ratios=ratios))
                remaining = set(scene(candidate,asset)[1])
                preferred_pair = preferred == (donor, receiver)
                score = (len(remaining), int(not preferred_pair), fraction)
                if remaining <= initial and score < best_score:
                    best_score = score
                    best = (ratios, dict(donor=donor, receiver=receiver, transferred_pt=fraction*s["size"][0],
                                         reason=issues, geometry_before=sorted(initial), geometry_after=sorted(remaining)))
    if best:
        f["split_ratios"], f["allocation"] = best
        allocation = f["allocation"]
        panel_by_id = {item["id"]:item.get("panel") for item in items}
        claims = {}
        for claim, fields in forest.groups(f.get("template")).items():
            elements = [forest.TARGET.get(field,field) for field in fields]
            elements.extend(f["assets"][asset_id]["element"] for asset_id in f["claims"][claim]["assets"])
            for panel in (allocation["donor"], allocation["receiver"]):
                if any(panel_by_id.get(element)==panel for element in elements):
                    claims.setdefault(panel,[]).append(claim)
        allocation["affected_claims"] = claims


def layout_pass(s,issues,api,asset=None):
    if "forest" in s:
        proposal = None
        if api.live:
            decision=api.ask(model.FOREST_LAYOUT,dict(poster=context(s),issues=issues))
            s["forest"]["reflow"]=max(0,min(1,float(decision["reflow"])))
            proposal = decision.get("transfer")
        elif issues:
            s["forest"]["reflow"]=1.
        if issues and asset is not None:
            allocate_panels(s,issues,asset,proposal)
        return
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
    if api.live and all(rates) and all(c["usage"].get("reported",True) and "prompt_tokens" in c["usage"] and "completion_tokens" in c["usage"] for c in calls):
        cost=sum(c["usage"]["prompt_tokens"]*float(rates[0])+c["usage"]["completion_tokens"]*float(rates[1]) for c in calls)/1e6
    trace(folder,"usage",model=api.name,calls=calls,estimated_usd=cost,elapsed_s=round(time.monotonic()-start,3))

def generate(pdf,folder,size,condition,api,initial=None,forest_result=None):
    folder.mkdir(parents=True,exist_ok=False)
    start,offset=time.monotonic(),len(api.calls)
    try:
        if initial:
            s=copy.deepcopy(read(initial/"initial.json"))
            copy_evidence(initial,folder)
        elif forest_result:
            s=forest.prepare(pdf,forest_result,folder,size,api)
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
    t=request.lower().strip().rstrip(".")
    if "protected section" in t and len(s["protected"])==1:
        t=t.replace("protected section",next(iter(s["protected"])))
    if "it" in t.split() and s["history"]:
        previous=next((v.get("target") for v in reversed(s["history"]) if v.get("accepted")),None)
        if previous:
            t=re.sub(r"\bit\b",previous,t)
    if api.live:
        action=api.ask(model.INTENT,dict(request=request,resolved_request=t,history=s["history"],
                                         protected=s["protected"],limits=s["limits"]))
        if action["operation"]!="clarify":
            return action
    # A supported explicit command remains unambiguous even when the model flags a conflict.
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

def _acquire_lock(guard):
    """Take the single-writer guard, recovering a lock left by an interrupted edit."""
    try:
        with guard.open("x", encoding="utf8") as stream:
            stream.write(f"{os.getpid()} {time.time()}\n")
        return
    except FileExistsError:
        pass
    try:
        age = time.time() - guard.stat().st_mtime
    except OSError:
        age = 0
    if age > LOCK_STALE_SECONDS:
        guard.unlink(missing_ok=True)
        with guard.open("x", encoding="utf8") as stream:
            stream.write(f"{os.getpid()} {time.time()}\n")
        return
    raise ValueError("Another edit is in progress (.editing lock exists). "
                     "If no other process is running, remove the lock file and retry.")


def edit(folder,request,api):
    # Exclusive single-writer guard. state.json is the only committed pointer.
    guard=folder/".editing"
    _acquire_lock(guard)
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
                    record["conflict"]=key in s["protected"] and limit>=1
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
        guard.unlink(missing_ok=True)
        cost_record(api,folder,start,offset)
