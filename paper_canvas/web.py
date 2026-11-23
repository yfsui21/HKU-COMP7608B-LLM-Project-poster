"""Loopback-only interactive poster studio; sessions and reviews persist on disk."""
import base64
import json
import re
import secrets
import shutil
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent import generate, edit, read, write, copy_evidence
from .source import EXAMPLE, page_png
from .model import Model
from . import forest_example, native_tree

STATIC = Path(__file__).parent
REPO = STATIC.parent
SAMPLES = {"field": EXAMPLE, "retrieval": EXAMPLE.with_name("retrieval_study.pdf")}
OPEN_PAPER = REPO / "runs/open-papers/hadi-2024.pdf"
MIME = {"preview.png":"image/png","poster.pptx":"application/vnd.openxmlformats-officedocument.presentationml.presentation"}
VERSION = re.compile(r"(?:0[0-4]-(?:initial|layout|content)|edit-[0-9a-f]{10})")
REPLAY = REPO / "runs/posterforest-replay"
REPLAY_FILES = {"poster.pptx": MIME["poster.pptx"], "report.json": "application/json",
                "before.jpg": "image/jpeg", "reference.jpg": "image/jpeg"}


def replay_page(query):
    params = parse_qs(query)
    orientation = "portrait" if params.get("orientation") == ["portrait"] else "landscape"
    stage = "before" if params.get("stage") == ["before"] else "reference"
    folder = REPLAY / orientation
    prefix = "/replay/" + orientation + "/"
    def link(o, s, label):
        current = ' aria-current="page"' if (o, s) == (orientation, stage) else ""
        return f'<a class="secondary"{current} href="/posterforest?orientation={o}&amp;stage={s}">{label}</a>'

    stages = {"before": "反馈前", "reference": "终稿"}
    image = ""
    audit = '<p>在项目目录运行 <code>python examples/replay_posterforest.py --fetch</code> 后刷新，即可查看本机复现结果。</p>'
    if (folder / "report.json").is_file() and (folder / "poster.pptx").is_file():
        report = read(folder / "report.json")
        name = stage + ".jpg"
        if (folder / name).is_file():
            image = f'<a href="{prefix}{name}"><img src="{prefix}{name}" alt="作者{stages[stage]}海报"></a>'
        rows = [("分区", report["panels"]), ("文字框", report["textboxes"]), ("图表", report["pictures"])]
        audit = '<dl class="stats">' + "".join(f"<div><dt>{k}</dt><dd>{int(v)}</dd></div>" for k,v in rows) + "</dl>"
        count = int(report["compared_parts"])
        status = f"画布尺寸一致，{count} 个文件部件逐字节匹配。" if report["matched"] else "比对发现差异，请查看校验报告。"
        audit += f'<p>{status}</p><a class="secondary" href="{prefix}poster.pptx" download>下载本机生成的 PPTX</a><p><a href="{prefix}report.json">查看校验报告</a></p>'
    values = dict(DIRECTIONS="".join(link(o,stage,t) for o,t in [("landscape","横版"),("portrait","竖版")]),
                  STAGES="".join(link(orientation,s,t) for s,t in stages.items()), CAPTION="作者"+stages[stage],
                  AUDIT=audit, IMAGE=image, EDIT='<a class="secondary" href="/?example=forest">用 PaperCanvas 生成同篇论文 →</a>' if forest_example.PATH.exists() else "")
    page = (STATIC / "posterforest.html").read_text(encoding="utf-8")
    for key, value in values.items():
        page = page.replace("{{" + key + "}}", value)
    return page.encode()
def events(folder):
    return [json.loads(line) for line in (folder/"trace.jsonl").read_text(encoding="utf8").splitlines()]

def additional_sources(state):
    out=list(state.get("notes",[]))
    if "forest" in state:
        for claim_id,claim in state["forest"]["claims"].items():
            out.extend(dict(label=claim_id,page=q["page"],text=q["quote"]) for q in claim["quotes"])
        out.extend(dict(label=name,**asset) for name,asset in state["forest"]["assets"].items())
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
                versions=versions,claims=review.get("claims",[]),editorial=state.get("conference",{}).get("editorial"),
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

def serve(port,live,forest_result=None,forest_pdf=None,tree_session=None,soffice=None):
    if tree_session:
        if read(tree_session/"state.json").get("format")!="posterforest-tree-v1":
            raise ValueError("Select a native tree session")
        if not soffice:
            raise ValueError("Provide --soffice to render split edits")
    root=REPO/"runs/web"
    root.mkdir(parents=True,exist_ok=True)
    token=secrets.token_urlsafe(32)
    forest_result=forest_result or REPO/"examples/tsne"
    forest_pdf=forest_pdf or REPO/"runs/open-papers/tsne-jmlr.pdf"
    imported=(forest_result/"claims.json").is_file() and forest_pdf.is_file()
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
            if tree_session and path in ("/","/tree"):
                return self.reply(200,"text/html; charset=utf-8",
                                  (STATIC/"native_tree.html").read_text(encoding="utf8").replace("{{TOKEN}}",token).encode())
            if tree_session and path=="/tree/state":
                state=read(tree_session/"state.json")
                return self.data(dict(artifact=state["artifact"],branches=native_tree.choices(state["tree"])))
            if tree_session and path.startswith("/tree/file/"):
                parts=path.split("/")
                if len(parts)==5 and (parts[3]=="original" or VERSION.fullmatch(parts[3])) and parts[4] in MIME:
                    file=tree_session/parts[3]/parts[4]
                    if file.is_file():
                        return self.reply(200,MIME[parts[4]],file.read_bytes())
                return self.reply(404,"text/plain",b"Unknown tree artifact")
            if path=="/posterforest":
                return self.reply(200,"text/html; charset=utf-8",replay_page(urlparse(self.path).query))
            if path=="/":
                page=(STATIC/"demo.html").read_text(encoding="utf8")
                option='<option value="published">开放论文 · Hadi 等（PLOS ONE，20 页）</option>' if OPEN_PAPER.exists() else ""
                if forest_example.PATH.exists():
                    option+='<option value="forest">同论文对照 · PosterForest（ACL 2026，校对提纲）</option>'
                if imported:
                    option+='<option value="generated">从已生成的 PosterForest 海报继续修订</option>'
                return self.reply(200,"text/html; charset=utf-8",page.replace("{{TOKEN}}",token).replace("{{LIVE}}",str(live).lower()).replace("{{OPEN_PAPER}}",option).encode())
            if path in ("/studio.css","/studio.js"):
                mime="text/css" if path.endswith("css") else "application/javascript"
                return self.reply(200,mime+"; charset=utf-8",(STATIC/path[1:]).read_bytes())
            parts=path.strip("/").split("/")
            try:
                if len(parts)==3 and parts[0]=="replay" and parts[1] in ("portrait","landscape") and parts[2] in REPLAY_FILES:
                    return self.reply(200,REPLAY_FILES[parts[2]],(REPLAY/parts[1]/parts[2]).read_bytes())
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
                if tree_session and self.path=="/tree/resize":
                    result=native_tree.resize(tree_session,data["node"],float(data["ratio"]),soffice,data["artifact"])
                    return self.data(dict(artifact=result["artifact"],branches=native_tree.choices(result["tree"])))
                if self.path=="/generate":
                    sid=uuid.uuid4().hex
                    folder=root/sid
                    sample=data.get("sample","field")
                    samples={**SAMPLES,"published":OPEN_PAPER,"forest":forest_example.PATH}
                    if imported:
                        samples["generated"]=forest_pdf
                    pdf=samples[sample]
                    if data.get("pdf"):
                        raw=base64.b64decode(data["pdf"],validate=True)
                        if not raw.startswith(b"%PDF-") or len(raw)>16_000_000:
                            raise ValueError("请选择 16 MB 以内的 PDF。")
                        pdf=root/(sid+".pdf")
                        pdf.write_bytes(raw)
                    try:
                        generate(pdf,folder,[float(data.get("width",1008)),float(data.get("height",540))],"joint",Model(live),
                                 forest_result=forest_result if sample=="generated" else None)
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
