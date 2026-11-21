"""Run the local demo or operate a saved poster from a terminal."""
import argparse
from pathlib import Path
from paper_canvas.agent import ORDERS, edit, generate
from paper_canvas.model import Model
from paper_canvas.source import EXAMPLE

def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="cmd",required=True)
    gen=sub.add_parser("generate")
    gen.add_argument("pdf",type=Path,nargs="?",default=EXAMPLE)
    gen.add_argument("--size",nargs=2,type=float,default=[1008,540],metavar=("WIDTH_PT","HEIGHT_PT"))
    gen.add_argument("--condition",choices=list(ORDERS),default="joint")
    gen.add_argument("--out",type=Path,required=True)
    change=sub.add_parser("edit")
    change.add_argument("session",type=Path)
    change.add_argument("request")
    ui=sub.add_parser("demo")
    ui.add_argument("--port",type=int,default=8765)
    for parser in (gen,change,ui):
        parser.add_argument("--live",action="store_true")
    args=p.parse_args()
    model=Model(args.live)
    if args.cmd=="demo":
        from paper_canvas.web import serve
        serve(args.port,model.live)
        return
    if args.cmd=="generate":
        folder=args.out
        state=generate(args.pdf,folder,args.size,args.condition,model)
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