"""Gradio app: paper PDF -> revised poster -> multi-turn dialogue revision.

Run:  python app.py   or   python papercanvas.py app   (then open the printed URL)
"""
from __future__ import annotations

import json
import os
import tempfile
import traceback
from pathlib import Path

import gradio as gr

from posteragent import config, dialogue, llm, offline, pipeline
from posteragent.tree import leaves, splits, to_json

SAMPLE = Path(__file__).resolve().parent / "samples" / "synthetic_paper.pdf"
# Every browser run gets its own folder here; the server may serve files from
# this directory only.
WEB_RUNS = Path(__file__).resolve().parent / "runs" / "web"


def _asset_gallery(sess) -> list[tuple[str, str]]:
    out = []
    for a in list(sess.paper.figures_info.values()) + list(sess.paper.tables_info.values()):
        placed = any(
            l.asset_id == a.asset_id and l.content_type == a.kind
            for l in leaves(sess.root)
        )
        out.append((a.path, f"{a.label} {'(on poster)' if placed else '(not used)'}"))
    return out


def _trace_md(sess) -> str:
    lines = [f"**{sess.trace.summary()}**", ""]
    if sess.asset_problems:
        lines.append("### Asset audit")
        lines += [f"- {p}" for p in sess.asset_problems]
        lines.append("")
    if sess.paper.warnings:
        lines.append("### Extraction warnings")
        lines += [f"- {w}" for w in sess.paper.warnings]
        lines.append("")
    lines.append(f"### Passes ({sess.condition})")
    for r in sess.rounds:
        lines.append(f"**{r['stage']}** — {r['blocking']} blocking issue(s) {', '.join(r['kinds'])}")
        for n in r["notes"]:
            lines.append(f"  - {n}")
    lines.append("")
    lines.append("### Model calls")
    for c in sess.trace.calls:
        flag = "" if c.ok else f" **FAILED** {c.note}"
        lines.append(
            f"- `{c.agent}` {c.model} — {c.prompt_tokens}+{c.completion_tokens} tok, "
            f"{c.seconds:.1f}s{flag}"
        )
    return "\n".join(lines)


def _claims_md(sess) -> str:
    rv = sess.last_review
    rows = {r["claim"]: r for r in (rv.claims if rv else [])}
    lines = ["Frozen before revision; checked on every pass. "
             "✅ = all conditions present and the reviewer agrees.", ""]
    for c in sess.claims:
        r = rows.get(c.id, {})
        ok = "✅" if r.get("passed") else "❌"
        lines.append(f"{ok} **{c.id} · {c.role}** (p.{c.page}{', ' + c.asset if c.asset else ''}) — {c.statement}")
        lines.append(f"> {c.quote}")
        lines.append("conditions: " + ", ".join(f"`{q}`" + (" (missing)" if q in r.get("missing", []) else "")
                                              for q in c.conditions))
        lines.append("")
    if sess.claim_failures:
        lines.append("### Recorded extraction problems")
        lines += [f"- {p}" for p in sess.claim_failures]
    if rv and rv.issues:
        lines.append("### Issues in the current draft")
        lines += [f"- {'**blocking** ' if i['blocking'] else ''}`{i['kind']}` on {i['section'] or i['element']}: "
                  f"{i['detail']}" for i in rv.issues]
    return "\n".join(lines)


def _outline_md(sess) -> str:
    lines = []
    for s in sess.outline.sections:
        assets = ", ".join(a.label for a in s.assets) or "—"
        lines.append(f"**{s.title}**  ·  importance {s.importance}  ·  assets: {assets}")
        for b in s.bullets:
            lines.append(f"- {b}")
        lines.append("")
    return "\n".join(lines)


def _fill_md(sess) -> str:
    rows = ["| panel | section | fill | font scale | overflow |", "|---|---|---|---|---|"]
    for l in leaves(sess.root):
        if l.content_type != "text":
            continue
        rows.append(
            f"| `{l.id}` | {sess.section_title_for(l)} | {l.measured.volume_ratio:.0f}% "
            f"| {l.measured.font_scale:.2f} | {'yes' if l.measured.overflow else 'no'} |"
        )
    return "\n".join(rows)


def _tree_md(sess) -> str:
    """The Poster Tree itself. Geometry is never in here -- boxes are a derived
    cache -- so what you see is the whole mutable state the agents touch."""
    n_split = len(splits(sess.root))
    n_free = len([n for n in splits(sess.root) if not n.locked])
    head = (
        f"{n_split} splits: {n_free} adjustable by the vision layout agent; column widths "
        f"move only through the measured allocator; the title band never moves.\n\n"
        "```json\n" + json.dumps(to_json(sess.root), indent=1, ensure_ascii=False) + "\n```"
    )
    return head


def _history_md(state) -> str:
    if state is None or not state.history:
        return "_No revision turns yet._"
    rep = dialogue.preservation_report(state)
    lines = [
        f"**{rep['turns']} turns** — {rep['applied']} applied, "
        f"{rep['reported_not_applied']} reported (not applied), "
        f"{rep['rejected_by_checks']} rolled back after review",
        "",
        f"**Protected spans intact:** {rep['protected_intact']}/{rep['protected_spans']}  ·  "
        f"**conflicts reported:** {rep['conflicts_reported']}  ·  "
        f"word limits met: {rep['word_limits_met']}  ·  figure sizes met: {rep['figure_sizes_met']}",
        "",
    ]
    for h in state.history:
        icon = {"applied": "✅", "reported": "⚠️", "rejected": "❌"}.get(h.status, "·")
        lines.append(f"{icon} **Turn {h.turn}** [{h.condition}] — _{h.request}_")
        lines.append(f"   → {h.message}")
        for n in h.notes:
            lines.append(f"   - {n}")
        lines.append("")
    lines.append("### Constraints in force")
    lines.append(dialogue._constraints(state.session))
    return "\n".join(lines)


# --------------------------------------------------------------------------
def do_generate(pdf_file, size_key, n_lo, n_hi, use_review, condition,
                budget_min, text_model, vision_model, use_offline, progress=gr.Progress()):
    llm.set_backend(offline.backend if use_offline else None)
    if text_model.strip():
        config.TEXT_MODEL = text_model.strip()
    if vision_model.strip():
        config.VISION_MODEL = vision_model.strip()

    path = pdf_file if isinstance(pdf_file, str) else getattr(pdf_file, "name", None)
    if not path:
        if SAMPLE.exists():
            path = str(SAMPLE)
        else:
            raise gr.Error("Upload a paper PDF first.")

    size = config.PosterSize(*config.POSTER_SIZES[size_key])
    WEB_RUNS.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="poster_", dir=WEB_RUNS))

    def prog(frac, msg):
        progress(min(0.99, frac), desc=msg)

    try:
        sess = pipeline.generate(
            path, workdir, size,
            condition=condition,
            n_sections=(int(n_lo), int(n_hi)),
            use_reviewer=bool(use_review),
            budget_s=float(budget_min) * 60.0,
            progress=prog,
        )
    except Exception as e:  # noqa: BLE001 - surface the real cause in the UI
        raise gr.Error(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-1200:]}") from e

    png, pptx = sess.export()
    state = dialogue.start(sess, "layout-first" if condition == "layout-first" else "joint")
    rounds = [(str(workdir / r["artifact"] / "preview.png"), f"{r['stage']} · {r['blocking']} blocking")
              for r in sess.rounds]

    return (
        state,
        sess.preview,
        [str(png), str(pptx)],
        _outline_md(sess),
        _claims_md(sess),
        _fill_md(sess),
        _trace_md(sess),
        _asset_gallery(sess),
        rounds,
        _history_md(state),
        _tree_md(sess),
        gr.update(interactive=True),
    )


def do_revise(state, request, protected, cond, progress=gr.Progress()):
    if state is None:
        raise gr.Error("Generate a poster first.")
    if not (request or "").strip():
        raise gr.Error("Type a revision request.")
    if state.turn >= config.MAX_DIALOGUE_TURNS:
        gr.Warning(
            f"Past the {config.MAX_DIALOGUE_TURNS}-turn cap used in the evaluation "
            "protocol; continuing anyway."
        )

    if cond != state.condition:
        if state.history:
            gr.Warning(f"This dialogue already uses {state.condition}; keeping it.")
        else:
            state = dialogue.start(state.session, cond)
    progress(0.2, desc="Resolving the request against the poster...")
    try:
        rec = dialogue.apply_request(state, request.strip(), protected or "")
    except Exception as e:  # noqa: BLE001
        raise gr.Error(f"{type(e).__name__}: {e}") from e

    progress(0.9, desc="Re-rendering...")
    sess = state.session
    png, pptx = sess.export(stem=f"poster_turn{rec.turn}")
    msg = {"applied": "✅", "reported": "⚠️", "rejected": "❌"}.get(rec.status, "")
    return (
        state,
        sess.preview,
        [str(png), str(pptx)],
        _history_md(state),
        _fill_md(sess),
        _trace_md(sess),
        f"{msg} **turn {rec.turn}** — {rec.message}",
        _tree_md(sess),
        _claims_md(sess),
        "",
    )


def do_reset(state):
    if state is None:
        return state, "_No revision turns yet._"
    state.session.protected.clear()
    state.session.word_limits.clear()
    state.session.min_width.clear()
    state.history.clear()
    state.conflicts.clear()
    return state, _history_md(state)


# --------------------------------------------------------------------------
CSS = """
.poster img {background: #fff;}
footer {display: none !important;}
"""

with gr.Blocks(title="PaperCanvas") as demo:
    gr.Markdown(
        "# PaperCanvas — joint semantic and spatial reasoning for posters\n"
        "Paper PDF → frozen claim requirements → poster (PPTX + preview) revised by "
        "content and layout passes → multi-turn dialogue. Geometry lives in a binary "
        "split tree, so panels cannot overlap; a panel that cannot fit its required "
        "content asks for space and the allocator reports where it came from."
    )

    st = gr.State(None)

    with gr.Row():
        # ---------------- left: controls ----------------
        with gr.Column(scale=3):
            gr.Markdown("### 1 · Generate")
            pdf_in = gr.File(label="Paper PDF", file_types=[".pdf"], type="filepath")
            if SAMPLE.exists():
                gr.Markdown(f"_Leave empty to use the bundled sample: `{SAMPLE}`_")
            size_in = gr.Dropdown(
                list(config.POSTER_SIZES), value=config.DEFAULT_SIZE, label="Poster size"
            )
            with gr.Row():
                lo_in = gr.Number(5, label="Min sections", precision=0, minimum=3, maximum=10)
                hi_in = gr.Number(7, label="Max sections", precision=0, minimum=3, maximum=12)
            review_in = gr.Checkbox(
                True,
                label="Vision reviewer",
                info="Off = geometric and claim checks only.",
            )
            cond_in = gr.Radio(
                list(pipeline.ORDERS), value="joint", label="Schedule",
                info="joint alternates content and layout passes; the others finish one "
                     "kind before the other (layout-first freezes boxes after its layout passes).",
            )
            offline_in = gr.Checkbox(
                False, label="Offline scripted backend",
                info="No model calls; exercises the workflow only. Output is not a result.",
            )
            budget_in = gr.Slider(
                2, 40, value=config.TOTAL_BUDGET_S / 60, step=1,
                label="Time budget (minutes)",
                info="Checked between passes. On expiry the run stops "
                     "refining and returns the current poster."
            )
            with gr.Accordion("Models", open=False):
                tm_in = gr.Textbox(config.TEXT_MODEL, label="Text model")
                vm_in = gr.Textbox(config.VISION_MODEL, label="Vision model")
            gen_btn = gr.Button("Generate poster", variant="primary")

            gr.Markdown("---\n### 2 · Revise by dialogue")
            req_in = gr.Textbox(
                label="Request",
                placeholder="Enlarge Figure 2 by 20% and keep the results text unchanged.\nShorten the method section to 40 words.",
                lines=2,
            )
            prot_in = gr.Textbox(
                label="Protected text (one span per line)",
                placeholder="Paste text that must stay character-for-character identical.",
                lines=3,
                info="Accumulates across turns; an edit that alters it is rolled back.",
            )
            scope_in = gr.Radio(
                list(dialogue.CONDITIONS), value="joint", label="Dialogue condition",
                info="joint may take space from anywhere and re-word crowded panels; "
                     "layout-first may only change the target and the split beside it.",
            )
            rev_btn = gr.Button("Apply request", variant="primary", interactive=False)
            reset_btn = gr.Button("Clear dialogue state", size="sm")
            status_out = gr.Markdown()

        # ---------------- right: outputs ----------------
        with gr.Column(scale=7):
            poster_out = gr.Image(
                label="Poster preview", type="pil", elem_classes="poster", height=620,
            )
            files_out = gr.Files(label="Download (PNG + editable PPTX)")
            with gr.Tabs():
                with gr.Tab("Revision history"):
                    hist_out = gr.Markdown("_No revision turns yet._")
                with gr.Tab("Claims and issues"):
                    claims_out = gr.Markdown()
                with gr.Tab("Selected content"):
                    outline_out = gr.Markdown()
                with gr.Tab("Fill measurement"):
                    gr.Markdown(
                        "`volume_ratio` drives the content agent's expand/condense "
                        f"routing. Target band {config.VOLUME_TARGET_LO:.0f}–"
                        f"{config.VOLUME_TARGET_HI:.0f}%; measured at the nominal "
                        "font size, not the auto-shrunk one."
                    )
                    fill_out = gr.Markdown()
                with gr.Tab("Passes"):
                    rounds_out = gr.Gallery(label="Poster after each pass", columns=3, height=360)
                with gr.Tab("Extracted assets"):
                    assets_out = gr.Gallery(label="Figures and tables", columns=4, height=360)
                with gr.Tab("Poster Tree"):
                    tree_out = gr.Markdown()
                with gr.Tab("Agent trace"):
                    trace_out = gr.Markdown()

    gen_btn.click(
        do_generate,
        [pdf_in, size_in, lo_in, hi_in, review_in, cond_in, budget_in, tm_in, vm_in, offline_in],
        [st, poster_out, files_out, outline_out, claims_out, fill_out, trace_out,
         assets_out, rounds_out, hist_out, tree_out, rev_btn],
    )
    rev_btn.click(
        do_revise,
        [st, req_in, prot_in, scope_in],
        [st, poster_out, files_out, hist_out, fill_out, trace_out, status_out,
         tree_out, claims_out, req_in],
    )
    reset_btn.click(do_reset, [st], [st, hist_out])


def launch(host: str | None = None, port: int | None = None) -> None:
    """Serve the UI. Binds to 127.0.0.1 by default: the app spends the API
    key of whoever runs it, so exposing it on a network is an explicit choice
    (POSTER_HOST=0.0.0.0 or --host)."""
    demo.queue(max_size=8).launch(
        server_name=host or os.environ.get("POSTER_HOST", "127.0.0.1"),
        server_port=port or int(os.environ.get("POSTER_PORT", "7860")),
        share=False,
        show_error=True,
        css=CSS,
        theme=gr.themes.Soft(),
        allowed_paths=[str(WEB_RUNS)],
    )


if __name__ == "__main__":
    launch()
