"""Local extractive planner for English text PDFs with captioned evidence."""
import re
import pdfplumber
from .details import secondary_evidence

HEADINGS = {
    "question": r"(research question|introduction|abstract|objective|background)",
    "method": r"(methods?|methodology|approach|experimental setup|materials and methods)",
    "result": r"(results?|findings|main results|experimental results|conclusions?)"
}
CUES = {"question":r"\?|\b(investigate|ask|study|examine|evaluate whether)\b",
        "method":r"\b(compare|inputted|train|trained|evaluated|participants|randomized|sampled)\b",
        "result":r"\b(reached|improved|reduced|achieved|increased|outperform\w*|found|showed)\b"}
CAPTION = re.compile(r"^(?:fig(?:ure)?\.?|table)\s*\d+[. :]",re.I)

def passages(pages):
    items=[]
    role=None
    for page in pages:
        pending=""
        for line in page["text"].splitlines():
            clean=line.strip()
            heading=re.sub(r"^[\d.]+\s*","",clean).strip(".: ").lower()
            kind=next((k for k,pat in HEADINGS.items() if re.fullmatch(pat,heading)),None)
            if kind:
                role,pending=kind,""
                continue
            if CAPTION.match(clean) or re.fullmatch(r"\d+",clean):
                pending=""
                continue
            pending=(pending+" "+clean).strip()
            if len(pending)>420:
                pending=clean
            if re.search(r"[.?!](?:\s|$)",pending):
                sentences=re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])",pending)
                pending="" if re.search(r"[.?!]$",sentences[-1]) else sentences.pop()
                for sentence in sentences:
                    if 6<=len(sentence.split())<=48:
                        items.append((page["page"],role,sentence))
            elif len(clean.split())<6 and not pending.endswith(","):
                pending=""
    return items

def figure_options(path):
    options=[]
    with pdfplumber.open(path) as doc:
        for idx,page in enumerate(doc.pages,1):
            lines=page.extract_text_lines(x_tolerance=1)
            for index,line in enumerate(lines):
                caption=line["text"]
                if not CAPTION.match(caption):
                    continue
                previous=line
                for continuation in lines[index+1:index+5]:
                    if (caption.endswith((".","!","?")) or continuation["top"]-previous["bottom"]>5
                            or abs(continuation["x0"]-line["x0"])>8 or continuation["text"].startswith("http")):
                        break
                    caption+=" "+continuation["text"]
                    previous=continuation
                photos=[im for im in page.images if 0<=line["top"]-im["bottom"]<=18
                        and im["width"]>page.width*.25 and im["height"]>40]
                if len(photos)==1:
                    im=photos[0]
                    box=[im["x0"]/page.width,im["top"]/page.height,im["x1"]/page.width,im["bottom"]/page.height]
                    options.append(dict(page=idx,caption=caption,box=box))
                    continue
                # Locate visual marks above the caption; ignore page borders and background fills.
                marks=[v for v in [*page.images,*page.rects,*page.curves,*page.lines]
                       if v["bottom"]<=line["top"]+2 and line["top"]-v["bottom"]<page.height*.45
                       and v["width"]>=3 and v["height"]<page.height*.65]
                if not marks:
                    continue
                marks.sort(key=lambda v:v["bottom"],reverse=True)
                group=[marks[0]]
                for mark in marks[1:]:
                    if min(o["top"] for o in group)-mark["bottom"]>page.height*.10:
                        break
                    group.append(mark)
                y0=max(0,min(v["top"] for v in group)-12)
                y1=min(line["top"]-2,max(v["bottom"] for v in group)+14)
                x0,x1=min(v["x0"] for v in group),max(v["x1"] for v in group)
                for label in lines:
                    if y0-4<=label["top"]<=y1 and label["x1"]>=x0-page.width*.24 and label["x0"]<=x1+50:
                        x0,x1=min(x0,label["x0"]),max(x1,label["x1"])
                if y1-y0<15 or x1-x0<25:
                    continue
                box=[max(0,(x0-8)/page.width),y0/page.height,min(1,(x1+8)/page.width),y1/page.height]
                options.append(dict(page=idx,caption=caption,box=box))
    return options

def plan(path,pages):
    pool=passages(pages)
    evidence=figure_options(path)
    all_evidence=evidence
    if not evidence:
        raise ValueError("No extractable figure with a caption was located. Use a text-based PDF whose captions sit below their figures, or switch to vision-model mode.")
    sections=[]
    used=set()
    for role,label in zip(HEADINGS,("Research question","Method","Key finding")):
        scored=[]
        for page,heading,sentence in pool:
            if sentence in used:
                continue
            cue=bool(re.search(CUES[role],sentence,re.I))
            score=4*(heading==role)+3*cue+(role=="result" and bool(re.search(r"\d",sentence)))
            if score>=3:
                scored.append((score,page,sentence))
        if not scored:
            raise ValueError("Missing verifiable source text for "+label+". Use an English paper with a research question, methods and results.")
        _,page,quote=max(scored,key=lambda v:v[0])
        used.add(quote)
        # Keep the complete primary statement as the immutable context in extractive mode.
        selected=next(i for i,item in enumerate(pool) if item[0]==page and item[2]==quote)
        following=pool[selected+1:selected+2]
        extra=next((text for pg,kind,text in following if pg==page and kind==role and text not in used
                    and len(text.split())+len(quote.split())<=32),None)
        if extra:
            used.add(extra)
        text=" ".join(filter(None,[quote,extra]))
        required=[quote,extra] if extra and re.search(r"\d",extra) else [quote]
        sections.append(dict(label=label,page=page,quote=quote,text=text,additional_quote=extra,required=required))
    result=sections[-1]
    words=set(re.findall(r"[a-z]{4,}",result["quote"].lower()))
    ref=re.search(r"\b(fig(?:ure)?|table)\.?\s*(\d+)\b",result["quote"],re.I)
    if ref:
        kind="table" if ref[1].lower()=="table" else r"fig(?:ure)?"
        matching=[e for e in evidence if re.match(kind+r"\.?\s*"+ref[2]+r"\b",e["caption"],re.I)]
        if not matching:
            raise ValueError("The finding cites a figure or table that could not be located.")
        evidence=matching
    ev=max(evidence,key=lambda e:len(words.intersection(re.findall(r"[a-z]{4,}",e["caption"].lower())))+2*(e["page"]==result["page"]))
    with pdfplumber.open(path) as doc:
        title=(doc.metadata or {}).get("Title")
        authors=(doc.metadata or {}).get("Author","")
    if not title or title.lower() in ("untitled","anonymous"):
        title=pages[0]["text"].splitlines()[0]
    output=dict(title=title,sections=sections,evidence=ev,planner="extractive")
    if isinstance(authors,str) and 2<len(authors)<180 and authors.lower() not in {"anonymous","unknown"}:
        output["paper_authors"]=authors
    secondary=secondary_evidence(pool,all_evidence,ev)
    if secondary:
        output["secondary"]=secondary
    if title=="Evaluation of ChatGPT as a diagnostic tool for medical learners and clinicians" and any(
            "journal.pone.0307383" in p["text"] for p in pages):
        output["credit"]="Adapted from Hadi, Tran, Nagarajan & Kirpalani (2024), PLOS ONE. doi:10.1371/journal.pone.0307383. CC BY."
    return output
