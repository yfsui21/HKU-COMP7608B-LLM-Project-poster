"""Offline regression checks: no network, no credentials, no model.

    python verify_offline.py

Uses the scripted backend (posteragent/offline.py) on the bundled synthetic
paper. It checks mechanics, not quality: claim verification, binding and
guards, space requests and allocation, the three schedules, dialogue
protection/conflict/rollback, persistence, and the evaluation protocol.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from posteragent import claims as claims_mod  # noqa: E402
from posteragent import config, dialogue, llm, offline, pipeline, render  # noqa: E402
from posteragent.allocate import SpaceRequest, allocate  # noqa: E402
from posteragent.parse import parse_pdf  # noqa: E402
from posteragent.tree import ContentLeaf, Bullet, dump, find, leaves, load  # noqa: E402

PDF = ROOT / "samples" / "synthetic_paper.pdf"
failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def main() -> int:
    llm.set_backend(offline.backend)
    tmp = Path(tempfile.mkdtemp(prefix="pc-verify-"))
    try:
        print("parse")
        paper = parse_pdf(PDF, tmp / "parse")
        check(set(paper.figures_info) == {"1", "2"} and set(paper.tables_info) == {"1"},
              "two figures and one table extracted")
        t = paper.tables_info["1"]
        check(t.width / t.height > 4, "table crop is the table, not the neighbouring chart")
        check(len(paper.pages) == 2, "raw page text kept for verification")
        from PIL import Image as _Im
        with _Im.open(paper.figures_info["1"].path) as im:
            # Graphics alone are 130pt tall at 170 dpi (~307 px); the category
            # labels below and the axis title above add ~60 px.
            check(im.height > 340, f"figure crop keeps its axis and category labels ({im.height}px tall)")
        md = paper.markdown
        check("records reviewed per annotator" not in md and "Single review 12" not in md
              and "Minutes Agreement" not in md, "figure labels and table cells stay out of the body text")
        check("Paired review reduced errors from 12 to 5" in md, "body text next to floats is kept")

        print("claims")
        good = {"id": "f", "role": "finding", "statement": "s", "page": 2,
                "quote": "Paired review reduced errors from 12 to 5 on the 40 synthetic records",
                "conditions": ["40 synthetic records", "invented phrase"], "asset": "figure:9"}
        got, probs = claims_mod.verify([good, {**good, "id": "x", "quote": "This sentence is not in the paper."}], paper)
        check(len(got) == 1 and got[0].conditions == ["40 synthetic records"], "unverifiable condition dropped")
        check(got[0].asset is None and any("figure:9" in p for p in probs), "unknown asset recorded as failure")
        check(any("not found" in p for p in probs), "invented quote rejected")
        wrong_page, probs = claims_mod.verify([{**good, "page": 1}], paper)
        check(wrong_page[0].page == 2 and any("corrected" in p for p in probs), "wrong page corrected and recorded")
        check(claims_mod.unsupported_numbers("errors fell from 12 to 7", paper) == ["7"],
              "number absent from the paper flagged")

        print("guards")
        from posteragent import agents
        leaf = ContentLeaf(bullets=[Bullet.plain("On 40 synthetic records, errors fell from 12 to 5.")])
        leaf.measured.volume_ratio = 180
        saved = llm._backend
        llm.set_backend(lambda *a: {"bullets": [{"runs": [{"text": "Errors fell a lot, by 99 points."}]}]})
        tr = llm.Trace()
        e = agents.content_agent(leaf, leaf, "", tr, required=["40 synthetic records"],
                                 known_numbers=set(claims_mod.numbers("\n".join(paper.pages))))
        llm.set_backend(saved)
        check(e is not None and e.rejected and e.bullets[0].text == leaf.bullets[0].text,
              "edit that drops a required phrase / invents a number is rejected")
        check(e.needs_space, "a condense that only works by dropping required text asks for space")

        shapes = {"nested": {"panel": {"bullets": ["a b", {"runs": [{"text": "c d"}]}]}},
                  "string": "- first point\n- second point", "strings": ["x y", "z w"],
                  "empty": {"bullets": [], "needs_space": True}, "junk": {"verdict": "ok"}}
        got = {k: len(agents._as_bullet_list(v)) for k, v in shapes.items()}
        check(got == {"nested": 2, "string": 2, "strings": 2, "empty": 0, "junk": 0},
              f"bullet coercion accepts nested/string forms, rejects empty/junk {got}")
        replies = iter([{"needs_space": True, "bullets": []},
                        {"bullets": [{"runs": [{"text": "On 40 synthetic records, errors fell from 12 to 5."}]}]}])
        prompts = []
        llm.set_backend(lambda agent, prompt, images, ctx: (prompts.append(prompt), next(replies))[1])
        tr = llm.Trace()
        leaf.measured.volume_ratio = 180
        e = agents.content_agent(leaf, leaf, "", tr, required=["40 synthetic records"],
                                 known_numbers=set(claims_mod.numbers("\n".join(paper.pages))))
        llm.set_backend(saved)
        check(len(tr.calls) == 2 and "COULD NOT BE USED" in prompts[1] and e is not None
              and not e.rejected and e.needs_space, "unusable reply gets one corrective retry, keeps needs_space")
        check(any('unusable output (attempt 1): {"needs_space": true' in ev for ev in tr.events),
              "the unusable reply itself is recorded in the trace")
        llm.set_backend(lambda *a: {"verdict": "ok"})
        tr = llm.Trace()
        e = agents.content_agent(leaf, leaf, "", tr, required=[], known_numbers=None)
        llm.set_backend(saved)
        check(e is None and len(tr.calls) == 2, "two unusable replies: previous text kept, no third call")

        print("generation (three schedules, two sizes)")
        draft = pipeline.prepare(PDF, tmp / "draft")
        check(len(draft.claims) >= 3 and all(c.section_id for c in draft.claims), "claims frozen and bound")
        standard = config.PosterSize(24, 18, 1.0)  # cramped on purpose: forces space requests
        bases = {}
        for area in (1, 0.75):
            size = standard if area == 1 else standard.shrunk(area)
            if area != 1:
                check(abs(size.w_in * size.h_in - 0.75 * 24 * 18) < 1e-6 and size.scale == standard.scale,
                      "smaller canvas: 75% area, same typography")
            base = pipeline.initial_session(draft, size, tmp / f"a{area}" / "base")
            bases[area] = base
            for cond, order in pipeline.ORDERS.items():
                s = pipeline.fork(base, tmp / f"a{area}" / cond, cond)
                pipeline.run_schedule(s, cond)
                stages = [r["stage"].split("-", 1)[-1] for r in s.rounds[1:]]
                check(stages == list(order), f"area {area} {cond}: passes {stages}")
                check((s.workdir / s.artifact / "poster.pptx").is_file()
                      and (s.workdir / s.artifact / "preview.png").is_file(), f"area {area} {cond}: artifacts")
                if cond == "layout-first":
                    check(s.layout_frozen, "layout-first freezes boxes for its content passes")
        initial_block = len(bases[1].last_review.blocking)
        check(initial_block > 0, f"cramped canvas starts with blocking issues ({initial_block})")

        print("allocation")
        s = pipeline.fork(bases[1], tmp / "alloc", "joint")
        over = next((l for l in leaves(s.root) if l.content_type == "text" and l.measured.overflow), None)
        if over is not None:
            a = allocate(s.root, s.size, SpaceRequest(over.id, "test"))
            check(bool(a.moves) and bool(a.donors), "overflowing panel gets room from a named donor")
        fig = next(l for l in leaves(s.root) if l.content_type == "figure")
        w0 = render.asset_width_in(fig, s.size)
        a = allocate(s.root, s.size, SpaceRequest(fig.id, "test", target_width_in=w0 * 1.2))
        check(render.asset_width_in(find(s.root, fig.id), s.size) > w0, "figure request widens the figure")
        b = allocate(s.root, s.size, SpaceRequest(fig.id, "test", target_width_in=w0 * 5), scope="innermost")
        check(all(m["role"] != "column" for m in b.moves), "innermost scope never moves column widths")

        print("persistence")
        d = dump(s.root)
        check(dump(load(d)) == d, "tree dump/load round-trips")
        pipeline.save(s)
        s2 = pipeline.load_session(s.workdir)
        check([l.text for l in leaves(s2.root)] == [l.text for l in leaves(s.root)], "session reloads")

        print("dialogue")
        joint = pipeline.fork(bases[1], tmp / "dj", "joint")
        pipeline.run_schedule(joint, "joint")
        results = {}
        for cond in dialogue.CONDITIONS:
            sess = pipeline.fork(joint, tmp / f"d-{cond}", cond)
            st = dialogue.start(sess, cond)
            r1 = dialogue.apply_request(st, "Enlarge Figure 1 by 30% and keep the results text exactly as it is")
            r2 = dialogue.apply_request(st, "Shorten the method section to 12 words")
            r3 = dialogue.apply_request(st, "Now shorten the text you kept unchanged to 5 words")
            results[cond] = (r1, r2, r3, dialogue.preservation_report(st))
            check(r2.status == "applied" and len(find(sess.root, r2.target).text.split()) <= 12,
                  f"{cond}: word limit applied")
            check(r3.conflict and r3.status == "reported", f"{cond}: back-reference resolved and conflict reported")
            rep = results[cond][3]
            check(rep["protected_intact"] == rep["protected_spans"] >= 1, f"{cond}: protected text intact")
        r1j, r1f = results["joint"][0], results["layout-first"][0]
        check(r1j.status == "applied", "joint: figure enlargement applied by redistributing space")
        check(r1f.status in ("applied", "rejected"), "layout-first: enlargement applied or rolled back")
        if r1f.status == "rejected":
            check(r1f.artifact == r1f.before, "layout-first: rollback keeps the previous deck")
        st = dialogue.resume(tmp / "d-joint")
        check(len(st.history) == 3 and st.history[0].protected_sections, "dialogue history resumes from disk")

        print("evaluation protocol")
        papers = tmp / "papers"
        papers.mkdir()
        for i in range(100):
            shutil.copy2(PDF, papers / f"p{i:03d}.pdf")
        plan = tmp / "plan.json"
        subprocess.run([sys.executable, "evaluate.py", "split", str(papers), str(plan)], cwd=ROOT,
                       check=True, capture_output=True)
        p = json.loads(plan.read_text())
        check(len(p["development"]) == 5 and len(p["evaluation"]) == 15 and len(p["dialogues"]) == 5
              and not set(p["development"]) & set(p["evaluation"]), "split: 5 dev / 15 eval / 5 dialogue")
        out = tmp / "study"
        r = subprocess.run([sys.executable, "evaluate.py", "run", "--offline", "--out", str(out)],
                           cwd=ROOT, capture_output=True, text=True)
        check(r.returncode == 0, "offline protocol run completes" + ("" if r.returncode == 0 else f": {r.stderr[-400:]}"))
        if r.returncode == 0:
            summ = json.loads((out / "summary.json").read_text())
            check(summ["completion"]["n"] == 6, "6 schedule cases (3 conditions x 2 sizes)")
            check(set(summ["dialogue"]) == {"joint", "layout-first"}, "dialogue under both conditions")
            check((out / "blinded" / "rater-1.csv").is_file() and (out / "blinding-key.json").is_file(),
                  "blinded previews and rater sheets")

        print("frozen thresholds")
        check(p.get("thresholds") == config.thresholds() and not p["frozen"],
              "split writes every threshold, unfrozen")

        def ev(*a: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, "evaluate.py", *a], cwd=ROOT, capture_output=True, text=True)

        # Shrink the plan to one paper per split so the runs stay fast.
        small = {**p, "development": p["development"][:1], "evaluation": p["evaluation"][:1],
                 "dialogues": {p["evaluation"][0]: next(iter(p["dialogues"].values()))}}
        small["thresholds"] = {**small["thresholds"], "min_font_scale": 0.9, "underfill_pct": 50.0}
        plan.write_text(json.dumps(small))
        r = ev("run", "--offline", "--papers", str(papers), "--plan", str(plan), "--out", str(tmp / "e0"))
        check(r.returncode != 0 and "not frozen" in r.stdout + r.stderr, "evaluation split refused before freezing")
        r = ev("run", "--offline", "--papers", str(papers), "--plan", str(plan), "--split", "development",
               "--out", str(tmp / "dev"))
        check(r.returncode == 0, "development split runs before freezing")
        if r.returncode == 0:
            st = json.loads((tmp / "dev" / "settings.json").read_text())
            check(st["thresholds_applied"]["min_font_scale"] == 0.9 and st["split"] == "development",
                  "plan thresholds applied and recorded")
            res = json.loads((tmp / "dev" / "results.json").read_text())
            check([c["paper"] for c in res["cases"]][:1] == small["development"], "development papers used")

        bad = {**small, "thresholds": {**small["thresholds"], "volume_target_lo": 120.0}}
        plan.write_text(json.dumps(bad))
        check(ev("freeze", str(plan)).returncode != 0, "incoherent thresholds refused at freeze")
        missing = {**small, "thresholds": {k: v for k, v in small["thresholds"].items() if k != "pt_body"}}
        plan.write_text(json.dumps(missing))
        check(ev("freeze", str(plan)).returncode != 0, "missing threshold refused at freeze")

        plan.write_text(json.dumps(small))
        r = ev("freeze", str(plan))
        frozen = json.loads(plan.read_text())
        check(r.returncode == 0 and frozen["frozen"] and frozen["freeze"]["plan_digest"]
              and frozen["freeze"]["prompt_digest"], "freeze seals plan, prompts and models")
        check(ev("freeze", str(plan)).returncode != 0, "freezing twice is refused")
        r = ev("run", "--offline", "--papers", str(papers), "--plan", str(plan), "--out", str(tmp / "e1"))
        check(r.returncode == 0, "frozen evaluation split runs" + ("" if r.returncode == 0 else f": {r.stderr[-300:]}"))
        if r.returncode == 0:
            st = json.loads((tmp / "e1" / "settings.json").read_text())
            check(st["thresholds_applied"] == small["thresholds"] and st["plan_frozen"],
                  "evaluation run used exactly the frozen thresholds")

        tampered = {**frozen, "thresholds": {**frozen["thresholds"], "min_font_scale": 0.6}}
        plan.write_text(json.dumps(tampered))
        r = ev("run", "--offline", "--papers", str(papers), "--plan", str(plan), "--out", str(tmp / "e2"))
        check(r.returncode != 0 and "edited after freezing" in r.stdout + r.stderr,
              "thresholds edited after freezing are refused")
        tampered = {**frozen, "size_in": [40, 30]}
        plan.write_text(json.dumps(tampered))
        r = ev("run", "--offline", "--papers", str(papers), "--plan", str(plan), "--out", str(tmp / "e3"))
        check(r.returncode != 0, "canvas size edited after freezing is refused")
        check(not (tmp / "e2").exists() and not (tmp / "e3").exists(), "a refused run writes no output")

        # Thresholds really steer the system: a stricter minimum font size turns
        # a panel that shrinks to fit into a measured overflow.
        saved_t = config.thresholds()
        try:
            s = pipeline.fork(bases[1], tmp / "thr", "joint")
            tight = next(l for l in leaves(s.root) if l.content_type == "text")
            # Grow the panel until it is a little too full at the nominal size
            # (115-185%; 0.72x fits up to ~190%): it should then fit by shrinking to 0.72x, not at 1.0x.
            filler = Bullet.plain("Paired review reconciled disagreements before final scoring of each record.")
            tight.bullets = [filler]
            s.remeasure()
            while find(s.root, tight.id).measured.volume_ratio < 115:
                tight.bullets.append(filler)
                s.remeasure()
            ratio = find(s.root, tight.id).measured.volume_ratio
            loose = config.apply_thresholds({**saved_t, "min_font_scale": 0.72})
            s.remeasure()
            m = find(s.root, tight.id).measured
            fits_loose = not m.overflow and m.font_scale >= 0.72
            config.apply_thresholds({**loose, "min_font_scale": 1.0})
            s.remeasure()
            over_strict = find(s.root, tight.id).measured.overflow
            config.apply_thresholds({**saved_t, "min_font_scale": 0.3})
            s.remeasure()
            check(find(s.root, tight.id).measured.font_scale >= 0.3, "font never shrinks below the frozen minimum")
            check(115 <= ratio <= 185 and fits_loose and over_strict,
                  f"min_font_scale changes the overflow verdict ({ratio:.0f}% full: fits at 0.72x, overflows at 1.0x)")
        finally:
            config.apply_thresholds(saved_t)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
