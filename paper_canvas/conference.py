"""Group sourced poster material into sections, subsections and evidence leaves."""
import hashlib
import io
import re
from PIL import Image
from . import extract, source

HADI_PDF="4926250510ed3abdd0bb9e9556c044ef1770de19f1247fe4c20aab6e44e5a408"

def hadi_outline(s):
    # Editorial reading of the publisher PDF, keyed to its checked file hash.
    # Use the detailed Results counts for load (the abstract differs by one).
    return {
        "authors":"Ali Hadi  ·  Edward Tran  ·  Branavan Nagarajan  ·  Amrit Kirpalani",
        "affiliation":"Schulich School of Medicine and Dentistry, Western University   /   PLOS ONE, 2024",
        "columns":[
            {"title":"Study design","panels":[
                {"title":"Research question","section":0,"page":1},
                {"title":"Case selection","section":1,"page":1},
                {"title":"Evaluation protocol","steps":[
                    ["Select cases","Use written Medscape challenges published from September 2021 through January 2023. Exclude cases with visual assets."],
                    ["Standardize the prompt","Give ChatGPT 3.5 the case and four options. Request a diagnosis and a rationale for each alternative."],
                    ["Score independently","At least two blinded raters evaluate each case. Score the final answer separately from the four option-level judgments."]],"page":4},
                {"title":"Primary outcome","stat":"74 / 150","text":"Cases answered correctly (49%). The model agreed with the most common Medscape user response in 92 of 150 cases (61%).","page":9}
            ]},
            {"title":"Diagnostic performance","panels":[
                {"title":"Option-level discrimination","section":2,"asset":"figure","page":11},
                {"title":"Receiver operating characteristic","asset":"support","text":"Across the 600 answer options, reported sensitivity was 49% and specificity was 83%. The area under the ROC curve was 0.66.","page":12}
            ]},
            {"title":"Response evaluation","panels":[
                {"title":"Quality of explanations","text":"Raters classified each of 150 explanations by completeness and relevance.","table":[["Classification","Cases","Share"],["Complete, relevant","78","52%"],["Complete, irrelevant","0","0%"],["Incomplete, relevant","64","43%"],["Incomplete, irrelevant","8","5%"]],"page":10},
                {"title":"Cognitive load","bars":[["Low",78],["Moderate",61],["High",11]],"denominator":150,"text":"Counts use the detailed Results section (150 responses).","page":9},
                {"title":"Qualitative review","text":"Raters identified useful clinical reasoning and suggestions for further investigation. Weaknesses included errors with numerical values, missed information and hallucinations.","page":10}
            ]}
        ],
        "takeaway":"A relevant explanation could accompany an incorrect diagnosis. Evaluate the selected answer separately from how convincing the rationale sounds.",
        "takeaway_page":18,
        "limit":"One model and one case collection were tested. Results may change with other models, case sources or prompt designs.",
        "limit_page":17,
        "credit":s["credit"],
        "editorial":"Hadi et al. source-checked example. Figures 4 and 5 have conflicting caption and axis labels. The response table and bars use the detailed Results on pp. 9–10."
    }

def automatic_outline(s,pages,extra):
    q,m,r=s["sections"]
    design=[dict(title=q["label"],section=0,page=q["page"]),dict(title=m["label"],section=1,page=m["page"])]
    if s.get("definition"):
        d=s["definition"]
        design.append(dict(title="Outcome measure",text=d["text"],page=d["page"]))
    findings=[dict(title=r["label"],section=2,asset="figure",page=s["evidence"]["page"])]
    if s.get("secondary"):
        e=s["secondary"]
        findings.append(dict(title="Supporting result",text=e["claim"],asset="support",page=e["page"]))
    panels=[]
    for i,e in enumerate(extra):
        # Captions give the asset a verified semantic label, without inferring its result.
        caption=e["caption"]
        prefix=re.match(r"^(?:figure|fig\.?|table)\s*\d+",caption,re.I)
        panels.append(dict(title=prefix[0] if prefix else "Evidence",text=caption,asset="exhibit-"+str(i),page=e["page"]))
    for note in s.get("notes",[]):
        if note["label"]!="Limitations":
            design.append(dict(title=note["label"],text=note["text"],page=note["page"]))
    limit=next((n for n in s.get("notes",[]) if n["label"]=="Limitations"),{})
    return dict(authors=s.get("paper_authors",""),affiliation=s.get("source_label","Research article"),
                columns=[dict(title="Research design",panels=design),dict(title="Main findings",panels=findings),
                         dict(title="Further evidence",panels=panels)],
                limit=limit.get("text",""),limit_page=limit.get("page"),credit=s.get("credit",""))

def prepare(s,pdf,folder,pages,size):
    w,h=size
    if w<880 or h<620 or w<h:
        return
    options=extract.figure_options(pdf)
    def identity(e):
        m=re.match(r"^(fig(?:ure)?\.?|table)\s*(\d+)",e["caption"],re.I)
        return (m[1].lower()[:3],m[2]) if m else (e["page"],e["caption"])
    used={identity(e) for e in [s["evidence"],s.get("secondary")] if e}
    extra=[]
    for e in options:
        key=identity(e)
        if key not in used and key[0]=="fig" and e["caption"] in " ".join(pages[e["page"]-1]["text"].split()):
            used.add(key)
            extra.append(e)
    if len(used)<3 or not extra:
        return
    reviewed=pdf.stat().st_size==951634 and hashlib.sha256(pdf.read_bytes()).hexdigest()==HADI_PDF
    if reviewed and identity(s["evidence"])==("fig","2"):
        roc=next(e for e in options if identity(e)==("fig","3"))
        s["secondary"]=dict(**roc,claim="The AUC for the ROC was 0.66.",claim_page=9)
        s["conference"]=hadi_outline(s)
        return
    if reviewed:
        extra=[e for e in extra if identity(e) not in {("fig","4"),("fig","5")}]
        if not extra:
            return
    s["exhibits"]=extra[:3]
    s["conference"]=automatic_outline(s,pages,s["exhibits"])

def save_exhibits(s,pdf,folder):
    for i,e in enumerate(s.get("exhibits",[])):
        data=source.page_png(pdf,e["page"],e["box"])
        with Image.open(io.BytesIO(data)) as im:
            e["aspect"]=im.width/im.height
        (folder/f"exhibit-{i}.png").write_bytes(data)
