# PaperCanvas

Group 56 · HKU COMP7608B
**Joint Semantic and Spatial Reasoning for Scientific Poster Generation**
Baodi Han, Shitu Ma, Yifan Sui, Jinkai Tao

PaperCanvas turns a text-based paper PDF into an editable single-slide PowerPoint poster and a PNG preview, then revises it through dialogue. Its central question, from the proposal, is whether letting content and layout decisions respond to each other on the rendered poster preserves scientific meaning and improves layout compared with doing one stage before the other.

```sh
pip install -r requirements.txt
python verify_offline.py                                    # mechanics check, no model calls
python papercanvas.py --offline generate samples/synthetic_paper.pdf --out runs/try
python papercanvas.py app                                   # UI on http://127.0.0.1:7860
```

`--offline` swaps the model for a scripted backend (`posteragent/offline.py`). It exercises every code path for free, and its posters say nothing about quality. Live use needs credentials (below).

## How it works

| Stage | Module | Model |
|---|---|---|
| PDF → page text, markdown, caption-anchored figure/table crops | `parse.py` | no |
| **Claim agent**: question, method, 1–3 findings, each with a verbatim quote, page, interpretation conditions and supporting asset, verified against the PDF and then **frozen** | `claims.py` | text |
| Parser agent → document tree; summarizing agent → sections, with every claim bound to one section | `agents.py` | text |
| Initial allocation from content statistics → binary split tree | `layout_init.py` | no |
| Render PNG + PPTX, reopen the deck, review | `render.py`, `review.py` | vision |
| Content passes and layout passes in one of three schedules | `pipeline.py`, `allocate.py` | text + vision |
| Dialogue under the joint or the fixed-layout condition | `dialogue.py` | text |

**Geometry is never written by a model.** The poster is a binary split tree: every internal node holds one ratio, and boxes are derived from the root. Panels cannot overlap or leave the canvas by construction, and the review still checks for both.

**Frozen requirements.** Each claim's quote must appear on its cited page, and each condition (dataset, baseline, metric, sample size) must appear in the paper. Anything unverifiable is dropped after one corrective retry and recorded as a failure. During revision, a content edit is rejected (previous text kept) if it drops a required phrase that was present, changes protected text, or introduces a number the paper does not contain. The agent therefore cannot pass review by weakening its own requirements.

**Review after every pass.** Each issue names the element, the unmet requirement and the proposed change. Blocking issues are:

- measurable geometry: overflow at the minimum font size (0.72 × nominal), overlap, out of canvas, a missing figure;
- claim checks: a missing condition, an unsupported number, a claim the vision reviewer judges unfaithful to its quote;
- user constraints: protected text, word limits, requested figure sizes;
- a deck that does not match the tree.

A draft with none of these is *resolved*. Reviewer remarks on readability steer the next pass, but they are not counted as failures.

**The content–layout link.** When a content pass has condensed a panel and its required text still overflows at the minimum font size, or the content agent sets `needs_space`, the panel files a space request. The layout pass's allocator walks every split above the panel, innermost first and up to the column widths. For each candidate ratio step it re-measures the whole poster, keeps the cheapest step that helps, and records the donor panels and any newly crowded panels. The next content pass receives the new crowding as ordinary overflow issues. A vision layout agent (PosterForest-style annotated crops) then makes bounded legibility adjustments within columns. Any of its moves that makes text elsewhere overflow is undone.

### Schedules

| | passes | difference |
|---|---|---|
| joint | content, layout, content, layout | later content responds to the new allocation |
| content-first | content, content, layout, layout | never returns to content |
| layout-first | layout, layout, content, content | boxes frozen after the layout passes; space requests go unserved |

All three start from the same initial draft and initial review. Each gets two content passes and two layout passes, the same per-pass call caps (8 content calls, 6 layout vision calls) and a review after every pass.

### Dialogue

Each request applies to the previous accepted poster. The session carries protected spans (with the turn that declared them), word limits, requested figure widths and the edit history. Intent parsing resolves back-references such as "the text you kept unchanged". The following are reported and not applied:

- a request that conflicts with an earlier constraint;
- a word limit too small to hold the section's required phrases.

Each request gets at most two attempts. An edit that fails review twice is rolled back with the reason. Protections declared in that turn stay in force.

- **joint**: the requested change, then space can come from anywhere and crowded panels anywhere can be condensed.
- **layout-first**: a figure can grow only at the expense of the panel it shares a split with, and only the target's own section may be reworded.

## Commands

```sh
python papercanvas.py generate paper.pdf --out runs/p1 [--size 48 36] [--condition joint|content-first|layout-first]
python papercanvas.py edit runs/p1 "Enlarge Figure 2 by 20% and keep the results text exactly as it is"
python papercanvas.py edit runs/p1 "Shorten the method section to 40 words"
python papercanvas.py app
```

`edit` returns exit code 2 when a request is not applied. A session directory holds:

- `session.json`: tree, frozen claims, constraints, rounds, dialogue history;
- `trace.json`: every model call with tokens and time, plus pipeline events;
- one folder per pass or turn with `preview.png`, `poster.pptx` and `review.json`;
- the final `poster.pptx` / `poster.png`.

### Credentials and models

Copy `.env.local.example` to `.env.local` (gitignored) and fill in the endpoint. Models whose names start with `claude-` go over the Anthropic Messages API (streamed); all other models go over OpenAI-compatible Chat Completions. The models are set with `POSTER_TEXT_MODEL` / `POSTER_VISION_MODEL` (default `claude-opus-5`). Set `POSTER_INPUT_USD_PER_M` and `POSTER_OUTPUT_USD_PER_M` to get cost estimates. Without them, cost is reported as null, never as zero.

## Evaluation protocol (proposal §5)

1. Put the 100 Paper2Poster source PDFs in one directory and keep the reference posters elsewhere. Run `python evaluate.py split PAPERS plan.json --seed 56`. This samples 5 development and 15 evaluation papers and preselects 5 dialogue papers.
2. Calibrate on the development papers with `python evaluate.py run --papers PAPERS --plan plan.json --split development --out runs/dev`. In `plan.json`, adjust:
   - `size_in` (default 48 × 36 in);
   - `thresholds`: minimum font scale, font sizes, fill band, underfill level, split clamps, per-pass call caps, dialogue attempts;
   - each dialogue script and its feasible/conflict labels.

   Then run `python evaluate.py freeze plan.json`. This validates the thresholds and records digests of the plan and the prompts, plus the model IDs. After freezing, `run` on the evaluation split refuses to start if the size, thresholds, split, scripts, prompts or models have changed. Edits to other code are recorded as a warning. Prepare the two evaluators' per-paper checklists before anyone sees an output, and keep them out of the system.
3. Run `python evaluate.py run --papers PAPERS --plan plan.json --out runs/study`. Each paper gets one shared draft and is run at the standard size and at 75% of its area with typography unchanged, under all three schedules. The dialogue papers then run the script under joint and layout-first, forked from the joint poster at the standard size. Failures stay in the denominators. This issues many paid requests; `trace.json` and `results.json` record calls, tokens, time and cost per case.
4. Give each evaluator `runs/study/blinded/` (anonymised previews, `rater-N.csv`, `dialogue-rater-N.csv`), the source papers and their checklist. Keep `blinding-key.json` private. Then run `python evaluate.py summarize runs/study`. It reports:
   - completion and resolution per condition and size;
   - blocking issues by kind;
   - joint-minus-baseline paired differences, automatic and human;
   - rater agreement;
   - feasible-request completion and conflict detection, separately.

   Automatic measures use the system's own requirements and human ratings use the independent checklist, so they are reported separately.

`python evaluate.py run --offline --out runs/smoke` runs the whole protocol on the synthetic paper with the scripted backend.

## Status and limitations

- **Done:** the system, the three schedules, dialogue under both conditions, and the evaluation runner and summary. `verify_offline.py` (also run in CI) checks the mechanics.
- **Outstanding:** the 15-paper study, the human ratings and agreement. No comparative claim follows from anything in this repository yet.
- **Topology is fixed at initialisation.** Passes re-weight splits but cannot regroup sections, so `layout_init.py` matters most.
- **Figure extraction is caption-anchored.** It works on the bundled PosterForest paper (6/6 figures, 2/2 tables) but can merge neighbours on crowded pages. Extraction warnings and the asset audit are recorded, and the UI shows every crop.
- **The preview is drawn with PIL, not by rendering the PPTX.** The deck is reopened and its text compared with the tree. PowerPoint substitutes Arial for the DejaVu measurement font; Arial is narrower, so fitted text still fits.
- **Scanned PDFs without a text layer are rejected.**

`docs/posterforest-notes.md` records how the design maps onto PosterForest (Choi et al., ACL 2026) and where it deviates. This implementation does not use PosterForest's code.

## Attribution

`samples/synthetic_paper.pdf` is invented test content, generated by `scripts/make_synthetic_paper.py`. `scripts/fetch_sample.sh` downloads the PosterForest paper (arXiv 2508.21720) for local testing; it is not redistributed. Parts of this code were written with AI assistance (Claude Code). Runtime prompts are in `posteragent/agents.py`, `claims.py` and `dialogue.py`.
