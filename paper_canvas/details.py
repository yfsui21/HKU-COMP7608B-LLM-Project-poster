"""Optional poster material with explicit anchors in the input paper."""
import re

NUMBER = r"\d[\d,]*(?:\.\d+)?|two|three|four|five|six|seven|eight|nine|ten"
UNIT = r"(?:synthetic |field |Medscape |case |clinical ){0,3}(?:queries|records|challenges|participants|patients|passages|tokens|annotators|images|datasets)\b"

def enrich(plan,pages):
    method=plan["sections"][1]
    measures=[]
    seen=set()
    for m in re.finditer(r"\b("+NUMBER+r")\s+("+UNIT+r")",method["text"],re.I):
        unit=m[2].lower()
        if unit not in seen and m[0].casefold() in " ".join(filter(None,[method["quote"],method.get("additional_quote")])).casefold():
            measures.append(dict(value=m[1],label=m[2],page=method["page"]))
            seen.add(unit)
    # Prefer population size and budget over a count mentioned in between them.
    plan["measures"]=measures if len(measures)<=2 else [measures[0],measures[-1]]
    notes=[]
    for heading,pattern in [("Evaluation",r"Evaluation(?: protocol)?"),("Limitations",r"(?:Study )?Limitations")]:
        for page in pages:
            match=re.search(r"(?:^|\n)(?:\d+(?:\.\d+)*\s+)?"+pattern+r"\s*\n",page["text"],re.I)
            if not match:
                continue
            tail=" ".join(page["text"][match.end():].split())
            sentences=re.split(r"(?<=[.!?])\s+(?=[A-Z])",tail)
            for sentence in sentences[:3]:
                if not (5<=len(sentence.split())<=30 and sentence.endswith((".","?","!"))):
                    continue
                if heading=="Limitations" and re.search(r"(several|some) limitations",sentence,re.I):
                    continue
                if sentence not in {s["text"] for s in plan["sections"]}:
                    notes.append(dict(label=heading,text=sentence,page=page["page"]))
                    break
            if notes and notes[-1]["label"]==heading:
                break
    plan["notes"]=notes
    for page in pages:
        lines=page["text"].splitlines()
        definition=next((line.strip() for line in lines if re.search(r"\b(?:counts queries|measure incorrect|is defined as)\b",line,re.I)
                         and 6<=len(line.split())<=22 and line.endswith(".")),None)
        if definition:
            plan["definition"]=dict(text=definition,page=page["page"])
            break
    if any("synthetic" in line.lower() and "demonstration" in line.lower() for line in pages[0]["text"].splitlines()):
        plan["source_label"]="Synthetic study"
    elif plan.get("credit"):
        plan["source_label"]="PLOS ONE   /   2024" if "PLOS ONE" in plan["credit"] and "(2024)" in plan["credit"] else "Research article"
    else:
        plan["source_label"]="Research article"

def secondary_evidence(pool,options,primary):
    for page,role,sentence in pool:
        if role!="result" or not re.search(r"\d",sentence) or len(sentence.split())>32:
            continue
        ref=re.search(r"\bfig(?:ure)?\.?\s*(\d+)\b",sentence,re.I)
        if not ref:
            continue
        for candidate in options:
            if candidate!=primary and re.match(r"fig(?:ure)?\.?\s*"+ref[1]+r"\b",candidate["caption"],re.I):
                return dict(**candidate,claim=sentence,claim_page=page)
    return None
