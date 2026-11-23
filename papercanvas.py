"""Run the local demo or operate a saved poster from a terminal."""
import argparse
import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from paper_canvas.agent import ORDERS, edit, generate, write
from paper_canvas.forest import bind, canvas
from paper_canvas.model import Model
from paper_canvas.source import EXAMPLE
from paper_canvas import native_tree

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
    pipeline.add_argument("--layout",choices=["both","tree","display"],default="both",
                          help="By default create the claim-bound session and a native tree editor under OUT/tree")
    tree=sub.add_parser("tree-import",help="Open the accepted native layout of an existing PosterForest run")
    tree.add_argument("work",type=Path)
    tree.add_argument("--out",type=Path,required=True)
    tree.add_argument("--paper",type=Path,help="Attach the original PDF for source-grounded native text edits")
    show=sub.add_parser("tree-list",help="List adjustable splits with their child sections")
    show.add_argument("session",type=Path)
    pipeline.add_argument("--backend",choices=["session","codex"],default="codex")
    pipeline.add_argument("--model",help="PosterForest GPT model identifier")
    gen.add_argument("--out",type=Path,required=True)
    change=sub.add_parser("edit")
    change.add_argument("session",type=Path)
    change.add_argument("request")
    change.add_argument("--paper",type=Path,help="Original PDF for a native session imported without one")
    change.add_argument("--soffice",default=os.getenv("FOREST_SOFFICE") or shutil.which("soffice"))
    ui=sub.add_parser("demo")
    ui.add_argument("--port",type=int,default=8765)
    ui.add_argument("--forest-result",type=Path)
    ui.add_argument("--paper",type=Path)
    ui.add_argument("--tree-session",type=Path)
    ui.add_argument("--soffice",default=os.getenv("FOREST_SOFFICE") or shutil.which("soffice"))
    for parser in (gen,change,ui):
        parser.add_argument("--live",action="store_true")
    args=p.parse_args()
    if args.cmd=="tree-import":
        state=native_tree.start(args.work,args.out,args.paper)
        print((args.out/state["artifact"]/"poster.pptx").resolve())
        return
    if args.cmd=="tree-list":
        print(json.dumps(native_tree.choices(native_tree.read(args.session/"state.json")["tree"]),indent=2))
        return
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
        if args.layout=="tree":
            state=native_tree.start(args.work,args.out,args.pdf)
            print((args.out/state["artifact"]/"poster.pptx").resolve())
            return
        try:
            subprocess.run([sys.executable,str(root/"examples/forest_local/display.py"),str(args.work),"--soffice",str(args.soffice),"--pdf",str(args.pdf)],check=True)
            result=args.work/"result"
            write(result/"claims.json",bind(args.pdf,result,model))
            state=generate(args.pdf,args.out,canvas(result),"joint",model,forest_result=result)
            print((args.out/state["artifact"]/"poster.pptx").resolve())
        finally:
            # The upstream tree remains usable even if the optional display composition fails.
            if args.layout=="both":
                native=native_tree.start(args.work,args.out/"tree",args.pdf)
                print((args.out/"tree"/native["artifact"]/"poster.pptx").resolve())
        return
    model=Model(args.cmd=="bind" or args.live)
    if args.cmd=="edit":
        state=native_tree.read(args.session/"state.json")
        if state.get("format")=="posterforest-tree-v1":
            if not args.soffice:
                p.error("Provide --soffice or set FOREST_SOFFICE for native rendering")
            match=re.fullmatch(r"split (\S+) (\d+(?:\.\d+)?)%",args.request.strip())
            if match:
                node, ratio=match[1],float(match[2])/100
                result=native_tree.resize(args.session,node,ratio,args.soffice,state["artifact"],args.request)
            elif model.live:
                from paper_canvas.native_revision import revise
                result=revise(args.session,args.request,model,args.soffice,args.paper,state["artifact"])
            else:
                p.error('Use "split NODE 55%" or --live for a natural language layout request')
            print((args.session/result["artifact"]/"poster.pptx").resolve())
            return
    if args.cmd=="bind":
        dest=args.result/"claims.json"
        write(dest,bind(args.pdf,args.result,model))
        print(dest)
        return
    if args.cmd=="demo":
        from paper_canvas.web import serve
        serve(args.port,model.live,args.forest_result,args.paper,args.tree_session,args.soffice)
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
