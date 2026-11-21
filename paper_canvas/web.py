"""A local-only, single-user demo. Only the accepted poster is downloadable."""
import base64
import json
import secrets
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .agent import generate, edit, read
from .source import EXAMPLE
from .model import Model

def serve(port,live):
    root=Path("runs/web")
    root.mkdir(parents=True,exist_ok=True)
    token=secrets.token_urlsafe(32)
    sessions={}
    class Handler(BaseHTTPRequestHandler):
        def reply(self,code,mime,body):
            self.send_response(code)
            self.send_header("Content-Type",mime)
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host")!=f"127.0.0.1:{port}":
                return self.reply(403,"text/plain",b"Local address required")
            path=urlparse(self.path).path
            if path=="/":
                page=Path(__file__).with_name("demo.html").read_text(encoding="utf8")
                page=page.replace("{{TOKEN}}",token).replace("{{MODE}}","Live vision model" if live else "Offline sample")
                return self.reply(200,"text/html; charset=utf-8",page.encode())
            parts=path.split("/")
            if len(parts)==4 and parts[1]=="artifact" and parts[2] in sessions and parts[3] in ("poster.pptx","preview.png"):
                folder=sessions[parts[2]]
                file=folder/read(folder/"state.json")["artifact"]/parts[3]
                mime="image/png" if parts[3]=="preview.png" else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                return self.reply(200,mime,file.read_bytes())
            self.reply(404,"text/plain",b"Unknown artifact")

        def do_POST(self):
            if self.headers.get("X-Canvas-Token")!=token:
                return self.reply(403,"text/plain",b"Session token required")
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 1<=length<=24_000_000:
                    raise ValueError("PDF upload limit is 16 MB")
                data=json.loads(self.rfile.read(length))
                api=Model(live)
                if self.path=="/generate":
                    sid=uuid.uuid4().hex
                    pdf=EXAMPLE
                    if data.get("pdf"):
                        if not live:
                            raise ValueError("Restart with --live for other papers.")
                        pdf=root/(sid+".pdf")
                        pdf.write_bytes(base64.b64decode(data["pdf"],validate=True))
                    folder=root/sid
                    s=generate(pdf,folder,[float(data["width"]),float(data["height"])],"joint",api)
                    sessions[sid]=folder
                elif self.path=="/edit":
                    sid=data["session"]
                    folder=sessions[sid]
                    s=edit(folder,data["request"],api)
                else:
                    raise ValueError("Unknown action")
                response=dict(session=sid,artifact=s["artifact"],history=s["history"],title=s["title"],
                              protected=list(s["protected"]),trace=[json.loads(l) for l in (folder/"trace.jsonl").read_text(encoding="utf8").splitlines()])
                self.reply(200,"application/json",json.dumps(response).encode())
            except Exception as exc:
                self.reply(400,"application/json",json.dumps({"error":str(exc)}).encode())
    print(f"PaperCanvas at http://127.0.0.1:{port}   Ctrl+C to stop",flush=True)
    HTTPServer(("127.0.0.1",port),Handler).serve_forever()
