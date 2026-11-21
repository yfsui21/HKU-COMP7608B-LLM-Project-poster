"""Run the local demo or operate a saved poster from a terminal."""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from paper_canvas.agent import ORDERS, edit, generate, write
from paper_canvas.forest import bind, canvas
from paper_canvas.model import Model
from paper_canvas.source import EXAMPLE

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="cmd",required=True)
    gen=sub.add_parser("generate")
    gen.add_argument("pdf",type=Path,nargs="?",default=EXAMPLE)
    gen.add_argument("--size",nargs=2,type=float,metavar=("WIDTH_PT","HEIGHT_PT"))
    gen.add_argument("--condition",choices=list(ORDERS),default="joint")
    gen.add_argument("--forest-result",type=Path,help="Resume the generated display deck with its verified claim bindings")
    binding=sub.add_parser("bind")
    binding.add_argument("pdf",type=Path)
    binding.add_argument("result",type=Path,help="Generated directory containing display.pptx and its evidence")
    pipeline=sub.add_parser("pipeline",help="Generate, bind source claims, and open a revision session from one PDF")
    pipeline.add_argument("pdf",type=Path)
    pipeline.add_argument("--source",required=True,type=Path,help="Pinned PosterForest checkout")
    pipeline.add_argument("--soffice",required=True,type=Path)
    pipeline.add_argument("--work",required=True,type=Path,help="Upstream working directory")
    pipeline.add_argument("--out",required=True,type=Path,help="New PaperCanvas revision session")
    pipeline.add_argument("--backend",choices=["session","codex"],default="codex")
    pipeline.add_argument("--model",help="PosterForest GPT model identifier")
    gen.add_argument("--out",type=Path,required=True)
    change=sub.add_parser("edit")
    change.add_argument("session",type=Path)
    change.add_argument("request")
    ui=sub.add_parser("demo")
    ui.add_argument("--port",type=int,default=8765)
    ui.add_argument("--forest-result",type=Path)
    ui.add_argument("--paper",type=Path)
    for parser in (gen,change,ui):
        parser.add_argument("--live",action="store_true")
    args=p.parse_args()
    if args.cmd=="pipeline":
        root=Path(__file__).resolve().parent
        if args.out.exists():
            p.error("--out must be a new session directory")
        if args.backend=="session":
            os.environ.setdefault("PAPERCANVAS_GPT_QUEUE",str(args.work.resolve()/"revision-requests"))
        else:
            os.environ.pop("PAPERCANVAS_GPT_QUEUE",None)
            os.environ["PAPERCANVAS_CODEX_MODEL"]=args.model or "codex-default"
        model=Model(True)
        subprocess.run([sys.executable,str(root/"examples/run_posterforest.py"),str(args.pdf),
                        "--source",str(args.source),"--soffice",str(args.soffice),"--out",str(args.work),
                        "--backend",args.backend,"--model",args.model or ("GPT-current-session" if args.backend=="session" else "codex-default")],check=True)
        subprocess.run([sys.executable,str(root/"examples/forest_local/display.py"),str(args.work),"--soffice",str(args.soffice),"--pdf",str(args.pdf)],check=True)
        result=args.work/"result"
        write(result/"claims.json",bind(args.pdf,result,model))
        state=generate(args.pdf,args.out,canvas(result),"joint",model,forest_result=result)
        print((args.out/state["artifact"]/"poster.pptx").resolve())
        return
    model=Model(args.cmd=="bind" or args.live)
    if args.cmd=="bind":
        dest=args.result/"claims.json"
        write(dest,bind(args.pdf,args.result,model))
        print(dest)
        return
    if args.cmd=="demo":
        from paper_canvas.web import serve
        serve(args.port,model.live,args.forest_result,args.paper)
        return
    if args.cmd=="generate":
        folder=args.out
        size=args.size or (canvas(args.forest_result) if args.forest_result else [1008,540])
        state=generate(args.pdf,folder,size,args.condition,model,forest_result=args.forest_result)
    else:
        folder=args.session
        state=edit(folder,args.request,model)
        print(state["history"][-1]["reason"])
    print((folder/state["artifact"]/"poster.pptx").resolve())
    if args.cmd=="edit" and not state["history"][-1]["accepted"]:
        raise SystemExit(2)

if __name__=="__main__":
    try:
        main()
    except (ValueError,OSError,KeyError) as exc:
        raise SystemExit(str(exc))
