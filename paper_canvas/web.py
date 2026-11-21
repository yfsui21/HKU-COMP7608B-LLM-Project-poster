"""Loopback-only interactive poster studio; sessions and reviews persist on disk."""
import base64
import json
import re
import secrets
import shutil
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .agent import generate, edit, read, write, copy_evidence
from .source import EXAMPLE, page_png
from .model import Model
from . import forest_example

STATIC = Path(__file__).parent
SAMPLES = {"field": EXAMPLE, "retrieval": EXAMPLE.with_name("retrieval_study.pdf")}
OPEN_PAPER = Path("runs/open-papers/hadi-2024.pdf")
MIME = {"preview.png":"image/png","poster.pptx":"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
VERSION = re.compile(r"(?:0[0-4]-(?:initial|layout|content)|edit-[0-9a-f]{10})")
def events(folder):
    return [json.loads(line) for line in (folder/"trace.jsonl").read_text(encoding="utf8").splitlines()]

def additional_sources(state):
    out=list(state.get("notes",[]))
    if state.get("definition"):
        out.append(dict(label="Outcome measure",**state["definition"]))
    if fig:=state.get("secondary"):
        out.extend([dict(label="Supporting figure",**fig),
                    dict(label="Supporting result",page=fig["claim_page"],text=fig["claim"])])
    for e in state.get("exhibits",[]):
        out.append(dict(label=e["caption"].split(". ")[0],**e))
    tree=state.get("conference",{})
    for group in tree.get("columns",[]):
        for panel in group["panels"]:
            if "text" in panel or "steps" in panel:
                body=panel.get("text") or " ".join(text for _,text in panel["steps"])
                out.append(dict(label=panel["title"],page=panel["page"],text=body))
    return out

def snapshot(folder):
    state=read(folder/"state.json")
    stream=events(folder)
    review=read(folder/state["artifact"]/"review.json")
    versions=["00-initial"]
    for item in stream:
        name=item.get("artifact")
        if name and name not in versions:
            versions.append(name)
    return dict(session=folder.name,artifact=state["artifact"],title=state["title"],history=state["history"],
                size=state["size"],protected=list(state["protected"]),sections=state["sections"],
                evidence=state["evidence"],additional=additional_sources(state),trace=stream,elements=review["elements"],issues=review["issues"],
                versions=versions,editorial=state.get("conference",{}).get("editorial"),
                planner=state.get("planner","vision" if state["model"]!="offline-fixture" else "fixture"))

def comparison(root,folder,request,live):
    # Fork both branches from the accepted state including every prior constraint.
    original=read(folder/"state.json")
    rows=[]
    for condition in ("joint","layout-first"):
        branch=root/uuid.uuid4().hex
        branch.mkdir()
        state=dict(original,condition=condition)
        state.pop("fixed_boxes",None)
        write(branch/"state.json",state)
        copy_evidence(folder,branch)
        shutil.copytree(folder/state["artifact"],branch/state["artifact"])
        current=edit(branch,request,Model(live))
        attempts=[e for e in events(branch) if e["event"]=="attempt"]
        version=attempts[-1]["artifact"] if attempts else state["artifact"]
        review=read(branch/version/"review.json")
        fig=next(e for e in review["elements"] if e["id"]=="figure")
        rows.append(dict(condition=condition,session=branch.name,version=version,
                         decision=current["history"][-1],attempts=attempts,width=fig["w"],issues=review["issues"]))
    return rows

def serve(port,live):
    root=Path("runs/web")
    root.mkdir(parents=True,exist_ok=True)
    token=secrets.token_urlsafe(32)
    def session(sid):
        if not isinstance(sid,str) or not re.fullmatch(r"[0-9a-f]{32}",sid) or not (root/sid/"state.json").is_file():
            raise ValueError("找不到这次工作会话。")
        return root/sid
    class Handler(BaseHTTPRequestHandler):
        def reply(self,code,mime,body):
            self.send_response(code)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers()
            self.wfile.write(body)

        def data(self,value,code=200):
            self.reply(code,"application/json; charset=utf-8",json.dumps(value,ensure_ascii=False).encode())

        def do_GET(self):
            if self.headers.get("Host")!=f"127.0.0.1:{port}":
                return self.reply(403,"text/plain",b"Local address required")
            path=urlparse(self.path).path
            if path=="/":
                page=(STATIC/"demo.html").read_text(encoding="utf8")
                option='<option value="published">开放论文 · Hadi 等（PLOS ONE，20 页）</option>' if OPEN_PAPER.exists() else ""
                if forest_example.PATH.exists():
                    option+='<option value="forest">同论文对照 · PosterForest（ACL 2026，校对提纲）</option>'
                return self.reply(200,"text/html; charset=utf-8",page.replace("{{TOKEN}}",token).replace("{{LIVE}}",str(live).lower()).replace("{{OPEN_PAPER}}",option).encode())
            if path in ("/studio.css","/studio.js"):
                mime="text/css" if path.endswith("css") else "application/javascript"
                return self.reply(200,mime+"; charset=utf-8",(STATIC/path[1:]).read_bytes())
            parts=path.strip("/").split("/")
            try:
                if parts[0]=="artifact" and len(parts) in (3,4) and parts[-1] in MIME:
                    folder=session(parts[1])
                    version=parts[2] if len(parts)==4 else read(folder/"state.json")["artifact"]
                    if VERSION.fullmatch(version):
                        return self.reply(200,MIME[parts[-1]],(folder/version/parts[-1]).read_bytes())
                if parts[0]=="source" and len(parts)==3 and parts[2].isdigit():
                    folder=session(parts[1])
                    s=read(folder/"state.json")
                    page=int(parts[2])
                    if page in {p["page"] for p in [*s["sections"],s["evidence"],*additional_sources(s)]}:
                        pdf=folder/"source.pdf"
                        if not pdf.is_file():
                            legacy=root/(folder.name+".pdf")
                            pdf=legacy if legacy.exists() else EXAMPLE
                        return self.reply(200,"image/png",page_png(pdf,page))
            except (ValueError,FileNotFoundError):
                pass
            self.reply(404,"text/plain",b"Unknown resource")

        def do_POST(self):
            if self.headers.get("X-Canvas-Token")!=token:
                return self.reply(403,"text/plain",b"Session token required")
            try:
                length=int(self.headers.get("Content-Length",0))
                if not 1<=length<=24_000_000:
                    raise ValueError("上传文件上限为 16 MB。")
                data=json.loads(self.rfile.read(length))
                if self.path=="/generate":
                    sid=uuid.uuid4().hex
                    folder=root/sid
                    sample=data.get("sample","field")
                    samples={**SAMPLES,"published":OPEN_PAPER,"forest":forest_example.PATH}
                    pdf=samples[sample]
                    if data.get("pdf"):
                        raw=base64.b64decode(data["pdf"],validate=True)
                        if not raw.startswith(b"%PDF-") or len(raw)>16_000_000:
                            raise ValueError("请选择 16 MB 以内的 PDF。")
                        pdf=root/(sid+".pdf")
                        pdf.write_bytes(raw)
                    try:
                        generate(pdf,folder,[float(data.get("width",1008)),float(data.get("height",540))],"joint",Model(live))
                        shutil.copyfile(pdf,folder/"source.pdf")
                    finally:
                        if data.get("pdf"):
                            pdf.unlink(missing_ok=True)
                else:
                    folder=session(data["session"])
                    if self.path=="/edit":
                        edit(folder,data["request"],Model(live))
                    elif self.path=="/compare":
                        return self.data(comparison(root,folder,data["request"],live))
                    elif self.path!="/resume":
                        raise ValueError("Unknown action")
                self.data(snapshot(folder))
            except Exception as exc:
                self.data({"error":str(exc)},400)
    print(f"PaperCanvas at http://127.0.0.1:{port}",flush=True)
    HTTPServer(("127.0.0.1",port),Handler).serve_forever()
