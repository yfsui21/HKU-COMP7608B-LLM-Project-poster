"""PaperCanvas command line.

    python papercanvas.py generate paper.pdf --out runs/p1 [--size 48 36] [--condition joint]
    python papercanvas.py edit runs/p1 "enlarge figure 2 by 20% and keep the results text unchanged"
    python papercanvas.py app                     # Gradio UI on http://127.0.0.1:7860

Add --offline to any command to use the scripted backend instead of a model
(for trying the workflow without credentials; its output is not a result).
Credentials: copy .env.local.example to .env.local.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="scripted backend, no model calls")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="paper PDF -> revised poster (PPTX + PNG)")
    g.add_argument("pdf", type=Path)
    g.add_argument("--out", type=Path, required=True, help="new session directory")
    g.add_argument("--size", type=float, nargs=2, default=[48, 36], metavar=("W_IN", "H_IN"))
    g.add_argument("--condition", default="joint", choices=["joint", "content-first", "layout-first"])
    g.add_argument("--sections", type=int, nargs=2, default=[5, 7], metavar=("MIN", "MAX"))
    g.add_argument("--no-reviewer", action="store_true", help="skip the vision reviewer")
    g.add_argument("--budget", type=float, default=None, help="wall-clock cap in seconds")
    e = sub.add_parser("edit", help="one dialogue turn on a saved session")
    e.add_argument("session", type=Path)
    e.add_argument("request")
    e.add_argument("--condition", choices=["joint", "layout-first"],
                   help="dialogue condition (default: the session's, or joint)")
    e.add_argument("--protect", default="", help="exact text to keep unchanged (one per line)")
    a = sub.add_parser("app", help="launch the Gradio UI")
    a.add_argument("--port", type=int, default=7860)
    a.add_argument("--host", default=None, help="default 127.0.0.1; 0.0.0.0 exposes it to the network")
    args = parser.parse_args()

    from posteragent import config, llm
    if args.offline:
        from posteragent import offline
        llm.set_backend(offline.backend)

    if args.cmd == "generate":
        from posteragent import pipeline
        if args.out.exists() and any(args.out.iterdir()):
            parser.error(f"{args.out} is not empty; use a new directory for each generation")
        sess = pipeline.generate(args.pdf, args.out, config.PosterSize(*args.size),
                                 condition=args.condition, n_sections=tuple(args.sections),
                                 use_reviewer=not args.no_reviewer, budget_s=args.budget,
                                 progress=lambda f, m: print(f"[{f:4.0%}] {m}", flush=True))
        for r in sess.rounds:
            print(f"  {r['stage']:<11} blocking={r['blocking']} {', '.join(r['kinds'])}")
        rv = sess.last_review
        print(f"{'resolved' if rv.resolved else 'UNRESOLVED'}: {rv.summary()}")
        print(f"poster: {args.out / 'poster.pptx'}  preview: {args.out / 'poster.png'}")
        print(f"usage: {json.dumps(sess.trace.usage())}")
        return 0
    if args.cmd == "edit":
        from posteragent import dialogue
        state = dialogue.resume(args.session)
        if args.condition and args.condition != state.condition:
            if state.history:
                parser.error(f"this session's dialogue already uses {state.condition}")
            state = dialogue.start(state.session, args.condition)
        rec = dialogue.apply_request(state, args.request, args.protect)
        state.session.export()
        print(f"turn {rec.turn} [{rec.condition}] {rec.status}: {rec.message}")
        for n in rec.notes:
            print(f"  - {n}")
        return 0 if rec.status == "applied" else 2
    if args.cmd == "app":
        import app
        app.launch(args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
