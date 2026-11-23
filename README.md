# PaperCanvas

Group 56 · HKU COMP7608B  
**Joint Semantic and Spatial Reasoning for Scientific Poster Generation**  
Baodi Han, Shitu Ma, Yifan Sui, Jinkai Tao

PaperCanvas helps a researcher turn a text-based paper PDF into an editable, single-slide PowerPoint poster and revise it in conversation. This demo focuses on three claims (question, method, finding) with a primary figure or table and, when the source and canvas support it, a second cited figure. It records the source passage, page, required conditions, review observations and resulting layout.

**Standalone t-SNE demo.** Starting from the 27-page JMLR paper, a signed-in Codex CLI generated a one-page poster, alternated content and layout revisions, and processed three dialogue turns. Open the [final editable PowerPoint](examples/tsne/independent-cli/poster.pptx), [LibreOffice preview](examples/tsne/independent-cli/preview.png), [acceptance record](examples/tsne/independent-cli/acceptance.json) or [claim-by-claim evidence and verdicts](examples/tsne/independent-cli/review.json).

### Edit the paper's native PosterForest tree

By default `pipeline` creates the claim-bound display session in `--out` and the native tree session in
`OUT/tree`. Use `--layout tree` to open the upstream poster directly in `--out`, or `--layout display`
for the three-column workflow. The native editor reads the upstream split IDs and directions and binds
the final slide's shapes to their panels. Opening a session preserves the original PPTX byte for byte.
The editor recovers the accepted split shares from the final panel positions, including changes made
after the initial layout stage.

To open a completed local run without repeating inference:

```sh
python papercanvas.py tree-import runs/tsne-generation --out runs/tsne-tree
python papercanvas.py demo --tree-session runs/tsne-tree --soffice /path/to/soffice
```

Open the address printed by the server. Select a branch by its two child sections, set the share for the
left or upper child, and apply the edit. The preview and download link follow the accepted version.
For the CLI, `tree-list SESSION` lists branch IDs and their shares. Use
`python papercanvas.py edit SESSION "split NODE_ID 55%" --soffice /path/to/soffice`.
With a configured vision model, `edit --live` also accepts a natural-language layout request and supplies the
current preview and branch names to the model. Existing image bytes, text and font settings are preserved.
LibreOffice renders each edited deck; the editor compares visible text within the textboxes with the
source rendering and rejects new character loss. Native layout edits and the display session's
claim-bound text revisions currently maintain separate version histories.

`tree-import` also accepts an upstream output directory containing stages 4, 6 and 8.
`python verify_native_tree.py WORK --soffice /path/to/soffice` checks the modified subtree, unaffected
shapes, image aspect ratios and rollback. The regression was exercised on the local t-SNE run and the
[upstream ACL paper example](https://github.com/kaist-cvml/poster-forest/tree/dd4b060f3e5e8ed511693bdc9e24a1d1e57165f9/outputs/20260612_180322_vllm_qwen3_vllm_qwen3_vl_2026_ACL).

### Generate, revise and verify from a PDF

Install the two requirements files, sign in with `codex login`, and prepare the pinned PosterForest checkout and LibreOffice executable described below. Start with **new work and session paths**. The acceptance case uses the original PDF from [Visualizing Data using t-SNE](https://www.jmlr.org/papers/v9/vandermaaten08a.html).

```sh
curl -L https://www.jmlr.org/papers/volume9/vandermaaten08a/vandermaaten08a.pdf -o runs/tsne-jmlr.pdf
python papercanvas.py pipeline runs/tsne-jmlr.pdf --backend codex --source runs/posterforest-source --soffice /path/to/soffice --work runs/tsne-generation --out runs/tsne-session-cli
python verify_independent.py runs/tsne-jmlr.pdf runs/tsne-generation runs/tsne-session-cli --run-dialogue --report runs/tsne-acceptance.json
```

The first command after the download runs extraction, PosterForest generation, figure selection, display composition, claim binding and four alternating revisions. The verifier sends the three edits to the CLI, reopens the accepted PPTX, measures its figure width, compares protected text, and checks all seven evidence groups. Run it again without `--run-dialogue` to audit a completed case locally. The third request deliberately conflicts with the first turn’s protection. Rejecting it and retaining the second turn’s accepted slide passes the test.

Each CLI inference uses an ephemeral session. For other live edits, set `PAPERCANVAS_CODEX_MODEL=codex-default` (or your selected model) and run `python papercanvas.py edit SESSION "request" --live` with the dialogue queue unset. A separate `pipeline --backend session` mode lets an active chat answer requests via a file queue. The [earlier evaluation](evaluation/tsne-proposal/summary.json) used that transport for its two sizes and three schedules. The standalone case above tests one paper and one joint schedule.

The recorded standalone run made 35 text and 29 vision requests during native PosterForest generation, with zero cached responses at that stage. During debugging, display composition and revision resumed from this run's own intermediate files. The final revision and dialogue trace records 16 model calls. Seven versions were checked against seven claim groups each. The accepted main figure grew from 747.60 to 807.41 pt, the edited question contains nine words, and the protected result is identical in the three exported decks compared by the verifier. [Native generation audit](examples/tsne/independent-cli/audit.json) · [Revision and dialogue trace](examples/tsne/independent-cli/trace.jsonl)

### Replay the earlier generated example

The imported three-column display supports width transfers between adjacent body panels.
PaperCanvas uses PosterForest's vertical binary-split method on the editable display columns:
the first split separates the method panel, and the second divides the two results panels.
It measures candidate transfers on the rendered scene, preserves image proportions, and records
the donor, recipient, transferred width, affected claims, and geometry checks in `review.json`.
For a reproducible offline geometry test using the bundled display and the original PDF, run
`python verify_panel_transfer.py runs/tsne-jmlr.pdf --out runs/tsne-transfer`. On its 2250 × 1940 pt
canvas, a 112.5 pt transfer from method to main fits the main caption above the interpretation band.
The test reopens the PPTX and checks both panels, all three images, and dialogue rollback.

To reproduce the connected t-SNE demo locally using the existing generated slide and its bound evidence, download the same source PDF and run:

```sh
python -c "from pathlib import Path; Path('runs/open-papers').mkdir(parents=True,exist_ok=True)"
curl -L https://www.jmlr.org/papers/volume9/vandermaaten08a/vandermaaten08a.pdf -o runs/open-papers/tsne-jmlr.pdf
python papercanvas.py generate runs/open-papers/tsne-jmlr.pdf --forest-result examples/tsne --out runs/tsne-session
python papercanvas.py edit runs/tsne-session "enlarge figure by 8% and protect result"
python papercanvas.py edit runs/tsne-session "shorten question to 9 words"
python papercanvas.py edit runs/tsne-session "shorten protected section to 2 words"
python papercanvas.py demo
```

The third edit returns exit code 2 and keeps the second edit. Open [the generated-poster workspace](http://127.0.0.1:8765/?example=generated) to inspect the sources, edit history and accepted PowerPoint. Use `demo --forest-result DIR --paper PDF` to open another generated poster. To attach bindings to an existing generated directory, use `python papercanvas.py bind paper.pdf DIR`. This calls the configured GPT model, retries invalid bindings up to twice, verifies every quoted passage on its cited page and freezes the scientific conditions before revision. The importer supports the display generator’s editable three-figure deck schema.

Each review reads the text and embedded pictures back from the exported slide. It checks scientific conditions, newly introduced numbers, source crop identities, overlaps and canvas bounds. In live mode GPT also compares each claim with its passages and rendered preview, and can identify a faithful paraphrase by quoting its visible wording. Local replay uses the bound original and compact texts. The source PDF, original requirements, previous accepted version and versioned preview stay available during dialogue.

### t-SNE evaluation and scoring

The study resumed the PDF-verified poster generated in the preceding run. At each canvas size all three schedules started with identical content and positions, fixed fonts, two content edits, two layout edits and five reviews. The smaller canvas used 75% of the area. Both dialogue conditions started from the accepted joint draft. In the joint condition the figure grew from 662.23 to 715.21 pt, the question was shortened to nine words, and a conflicting edit preserved the accepted slide. The fixed-layout condition could not complete the enlargement without overlap. Its failed first turn rolled back the requested lock too, leaving “protected section” unresolved in the last turn.

All six candidates exported successfully. The active-session study recorded 73 logical requests and 12 distinct review images. For 47 requests the session supplied a previous decision on equivalent material, including repeated renders with identical pixels and claim evidence; five no-issue requests retained the approved content or allocation. Token counts and billing were unavailable. These figures cover revision and evaluation after import. This is a single-paper case study with no independent human ratings.

```sh
python evaluate.py run --papers runs/open-papers --plan examples/tsne/protocol.json --forest-result examples/tsne --offline --out runs/tsne-evaluation
python verify_proposal.py runs/tsne-evaluation
python evaluate.py summarize runs/tsne-evaluation
```

Omit `--offline` for model-based review of every rendered pass. For a multi-paper split, `--forest-root DIR` selects the generated bundle in `DIR/<paper-stem>/` for each input. The [study archive](evaluation/tsne-proposal/study.zip) includes evaluated outputs, traces, model decisions, blinded previews and empty rater forms. After extracting it, `summarize` and `verify_proposal.py` also accept the extracted directory. Give raters only the blinded folder, source papers and independent, prewritten paper checklists. Raters enter integer counts, checklist coverage and 1–5 ratings; dialogue sheets record request, protection, reference and conflict outcomes plus factual and layout errors. Re-run `summarize` to obtain double-rated denominators, per-schedule means, joint-minus-baseline differences within each paper and size, exact agreement and mean absolute differences. Blank ratings are excluded. Generation failures remain in the completion denominator.

![Poster generated by the offline example](examples/preview.png)

[Editable demo poster](examples/poster.pptx) · [Synthetic input paper](examples/retrieval_study.pdf) · [Observed demo results](evaluation/offline-results.json)

## Run the demo

Use Python 3.12. From the repository root, install into a virtual environment and start the local app:

```sh
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python papercanvas.py demo
```

Open **http://127.0.0.1:8765**. The Chinese-language studio starts with a generated poster from a two-page synthetic retrieval study. No API key is needed for local mode.

1. Open **论文证据**. Select a claim to read its source passage, or select the figure to see the exact crop on the source page.
2. Open **策略对照** and click **运行对照**. Both branches attempt the same 35% enlargement while preserving the result text. Each starts from your accepted poster. Inspect the final candidates, measured figure widths and issues from each attempt. The accepted session stays intact.
3. Under **海报编辑**, apply the enlargement. Try locking a paragraph and then shortening that paragraph to two words. **修订记录** lets you inspect the initial draft, each editing pass, rejected candidates and the retained version. Refreshing the same tab restores its session.
4. **导入论文** accepts an English text-based PDF (up to 16 MB). Set the page dimensions or select either synthetic example. Download the accepted poster with **↓ PPTX**.

### A published paper

Run `python examples/fetch_open_paper.py` once to cache the original open-access PDF locally. Refresh the studio, select **导入论文 → 开放论文 · Hadi 等**, and generate. This uses the same extractive planner as uploaded PDFs. The source is Hadi, Tran, Nagarajan and Kirpalani (2024), [Evaluation of ChatGPT as a diagnostic tool for medical learners and clinicians](https://doi.org/10.1371/journal.pone.0307383), PLOS ONE, CC BY. The publisher download is hash-checked. Generated posters credit the authors and identify the adaptation. This input is for testing document handling.

### Local extraction and its limits

The field-observation example uses a recorded semantic plan. Other local PDFs use section headings and sentence cues to select source text. The parser looks for a column gutter, matches explicit figure references, and locates image/vector marks above captions. Required source phrases survive editing. The local layout pass measures alternative column and figure allocations and chooses the smallest change that resolves the observed geometry problems. The local reviewer checks measurable geometry and text invariants. It does not independently judge scientific correctness. Inspect the evidence tab for misselected claims, reading-order errors or incorrect crops, especially on multi-column pages. Scans, complex tables and figures without detectable visual marks may require a different input or vision provider.

## Use a vision-language model

Configure a vision-capable Chat Completions model that supports image input, JSON object output, temperature and max_tokens. Model access may incur provider charges. In PowerShell, set the following variables in the terminal that launches the app:

```powershell
$env:PAPERCANVAS_MODEL = 'your-vision-model-id'
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'API key')).Password
python papercanvas.py demo --live
```

The live app accepts a PDF upload and natural-language requests to enlarge a figure, shorten a section or protect text. One title, three text panels and one evidence crop are supported. It requests clarification for other actions. The optional OPENAI_BASE_URL must be a trusted HTTPS API base ending in /v1. The service receives paper text, selected page images, poster previews and edit instructions. The key is read from the environment, sent in the Authorization header and excluded from state, trace and exports. Supply provider rates in PAPERCANVAS_INPUT_USD_PER_M and PAPERCANVAS_OUTPUT_USD_PER_M to estimate cost per million tokens. Unconfigured rates appear as null. Offline cost is zero.

The live API path requires your service credentials. It has not been validated against a paid provider in this release. Scanned PDFs without text, unverifiable quotes, missing figures, invalid model output and unresolved checks can cause a recorded failure. Font metrics use the Latin Helvetica/Arial family. Validate other scripts and complex typography before using them.

## CLI and artifacts

```sh
python papercanvas.py generate examples/paired_review.pdf --size 1008 540 --out runs/talk
python papercanvas.py edit runs/talk "enlarge figure by 35% and protect result"
python papercanvas.py edit runs/talk "shorten question to 6 words"
python papercanvas.py generate your-paper.pdf --live --out runs/paper
python papercanvas.py edit runs/paper "shorten the method to 20 words" --live
python verify_demo.py
python evaluate.py run --out runs/offline-evaluation
```

Use a fresh output directory for each generation. Sessions keep initial.json, evidence.png, trace.jsonl and versioned PPTX/PNG/review files. state.json points to the accepted version. Failed edits keep that pointer and append the reason to history. Browser runs are stored under runs/web. Each process serves one local user. An interrupted editor can leave a .editing lock; after verifying that the process stopped, remove that lock to resume. The app does not require a notebook or PowerPoint installation.

## Poster composition

### Reproduce the PosterForest rendering baseline

The [pinned upstream revision](https://github.com/kaist-cvml/poster-forest/tree/dd4b060f3e5e8ed511693bdc9e24a1d1e57165f9) includes two runs on the same ACL paper. Each starts with a different canvas and has its own final arrangement. With the project requirements installed, run from the repository root:

```sh
python examples/replay_posterforest.py --fetch
python papercanvas.py demo
```

Open [the local comparison](http://127.0.0.1:8765/posterforest). Switch between the authors' before-feedback and final images for either canvas. The download contains the locally regenerated single-slide PowerPoint. To repeat offline, run `python examples/replay_posterforest.py` after the first download. The input manifest pins each file by its Git blob ID.

The replay reads the authors' step 6 layout, text and crops, applies the step 7 unit and style settings, and calls their unmodified PowerPoint code generator. It opens the reference deck after generation to compare slide size, slide XML, masters, layouts, theme, relationships and embedded media. Each canvas matched on all 34 compared parts, with 25 panels, 25 text boxes and seven pictures. See the [recorded audit](evaluation/posterforest-replay.json). Steps 1–6 use published checkpoints. The replay makes zero model calls and does not measure end-to-end generation quality or reproduce the human study.

### Generate a new poster with GPT and PosterForest

The earlier active-session run started from the [27-page JMLR paper](https://www.jmlr.org/papers/v9/vandermaaten08a.html). The active GPT session handled 36 text and 29 image-bearing requests, including 28 panel reviews and one whole-poster critique. The fresh run reused zero responses. The generated 48 × 36 inch slide contains 33 editable text boxes and six embedded PDF crops. The audit records the generated document and poster trees, source pages, crop hashes and feedback counts. [Original PPTX](examples/tsne/poster.pptx) · [Preview](examples/tsne/poster.jpg) · [Six-stage audit](examples/tsne/audit.json)

The original layout repeats several bullets, truncates a long heading, and places MNIST figures beside text about other experiments. An optional display pass takes one additional GPT text response, pairs each selected image with its source caption and checks the quoted evidence against the PDF pages. It keeps the original output available for comparison. The display poster uses this project's three-column template. [Display PPTX](examples/tsne/display.pptx) · [Preview](examples/tsne/display.jpg) · [Figure and claim evidence](examples/tsne/display-evidence.json)

Use Python 3.12, Git, LibreOffice, the project requirements, and `examples/forest_local/requirements.txt`. For unattended GPT generation, install the Codex CLI and sign in with `codex login`. From the repository root:

```sh
python -m pip install -r requirements.txt -r examples/forest_local/requirements.txt
git clone --filter=blob:none --sparse https://github.com/kaist-cvml/poster-forest.git runs/posterforest-source
git -C runs/posterforest-source sparse-checkout set PosterForest utils
git -C runs/posterforest-source checkout dd4b060f3e5e8ed511693bdc9e24a1d1e57165f9
curl -L https://www.jmlr.org/papers/volume9/vandermaaten08a/vandermaaten08a.pdf -o runs/tsne-jmlr.pdf
python examples/run_posterforest.py runs/tsne-jmlr.pdf --source runs/posterforest-source --out runs/new-paper --soffice /path/to/soffice
python examples/forest_local/display.py runs/new-paper --soffice /path/to/soffice --pdf runs/tsne-jmlr.pdf
```

The default backend sends each upstream prompt and any rendered images through `codex exec` in an ephemeral read-only session. Supply `--model` to select a GPT model available to your login. The login stays with Codex. Each completed run saves a single-slide PowerPoint, its LibreOffice preview and a verification report under `result/`.

The `session` backend also allows the GPT model in an active chat to handle the same requests directly. It exposes one pending request at a time in the run directory. The model reads its prompt and images, then supplies its JSON answer. The pipeline advances automatically; this mode needs an active model session to service the queue. Token counts are unavailable in this mode. Each new output directory starts with an empty response cache.

**Implementation.** The launcher reads the authors' programs and prompts from the pinned Git revision. For text PDFs with numbered captions, pdfplumber/PDFium supply the page text and crops used by the first-stage parsing prompt. Long documents contribute excerpts across sections. The authors' tree refinement, panel allocation, binary layout, bullet generation, and node-level modification planner then operate on the new results. The review model receives images rendered from the candidate PowerPoint. The portable wrapper also handles Windows file paths and can recover a missing title row from source-matched model metadata. Full intermediate outputs stay under `outputs/`. The audit verifies the slide count, title on the PDF's first page, figure bytes against source crops, canvas bounds, and presence of visual feedback.

### Generate with PaperCanvas

Open the [PaperCanvas output](examples/forest/poster.pptx) or [view its preview](examples/forest/preview.png). PaperCanvas measures the wrapped text and figures in the outer section groups to select their widths, and fits the main figure to its available panel. The scientific content of this example uses the reviewed outline described below.

For a same-paper visual comparison, run `python examples/fetch_open_paper.py --paper forest`, then choose the ACL 2026 PosterForest example in the import dialog. Compare its single slide with the landscape preview in the [authors' repository](https://github.com/kaist-cvml/poster-forest#preview). The example uses a reviewed outline, checked figure crops and values from the paper's Table 2. PaperCanvas measures and renders the panels locally; the example does not invoke PosterForest or reproduce its evaluation. External PDFs are cached under runs/open-papers.

Landscape posters at least 880 × 620 pt with sufficient distinct evidence can use three section groups with nested text, images and tables. The Hadi and PosterForest examples have reviewed editorial selections. Other inputs use extractive figure captions and the available source metadata. The ordinary compact layout remains available for shorter papers.

The one-slide PPTX includes individually editable text boxes and figure crops. The masthead uses quantities verified in the method passage. The findings panel pairs the main claim with its cited figure. When the paper provides them, the poster includes outcome definitions, evaluation details and limitations with page references. On canvases at least 640 pt tall, the extractive planner can add a second result that explicitly cites another detected figure. The evidence index links these additions to the paper.

## Implementation and checks

- **Source and content** — paper_canvas/source.py extracts per-page text, tables and captions. The live planner chooses claims and a supporting caption, then localizes the figure/table on its source-page image. Source page/quote matching and required-phrase checks reject unverifiable inputs.
- **Joint revisions** — paper_canvas/agent.py alternates two content passes and two layout passes. The live reviewer sees the rendered poster and the source evidence after every pass. The editor can shorten prose, change column widths and assign more height to evidence. Required scientific conditions remain fixed. The protected-text guard compares exact strings.
- **Rendering** — paper_canvas/render.py measures lines, preserves figure aspect ratio, and detects overflow, overlaps and out-of-canvas elements. PPTX text blocks and pictures are independently editable. The PNG is rendered from the same measured scene via PDFium. PowerPoint can substitute fonts and produce slightly different glyph positions.
- **Dialogue** — a request uses the preceding accepted version, prior size/word constraints and edit history. Each request has at most two attempts. A fixed-layout baseline can resize the requested figure but cannot move the other boxes. The model prompts and API settings are in paper_canvas/model.py.
- **Evidence** — see evaluation/offline-dialogue.jsonl for the two-attempt recovery and protected-text rejection. verify_demo.py also covers malformed input, an ambiguous request and rollback. The synthetic example can be regenerated with python examples/make_paper.py.

## Proposal evaluation protocol

1. Obtain the Paper2Poster 100 paper–poster pairs and keep the 100 source PDFs in a separate directory. Reference posters are for human context only. Use `python evaluate.py split path/to/papers plan.json --seed 56`. This selects 5 development and 15 evaluation papers, and preselects 5 dialogue cases.
2. On development data, set standard canvas dimensions and readability thresholds. Review the three-turn scripts and their feasibility labels for the five selected papers before freezing plan.json, prompts, model ID and settings. The bundled script is calibrated only for the synthetic paper. Prepare an independent per-paper claim/context checklist before viewing outputs. Keep it outside model inputs.
3. With credentials configured, run `python evaluate.py run --papers path/to/papers --plan plan.json --out runs/study`. This can issue hundreds of paid requests. It compares content-first, layout-first and joint from the same extracted draft and evidence at each size. Each schedule receives two content and two layout passes, the same reviewer schedule and per-call completion cap. Both dimensions scale by √0.75 for the smaller canvas; typography stays fixed. Dialogue conditions fork the same accepted poster. Inspect actual token usage, runtime, failures and estimated cost in traces. The runner preserves the denominator when parsing or generation fails.
4. Give each of two independent evaluators the source papers, frozen checklists, anonymized previews and their randomized rater CSV. Keep the blinding key private until both have scored. Record checklist coverage, wrong numerical claims, mismatched evidence, geometric errors, figure-label legibility and reading order (1 = major difficulty, 3 = edits needed, 5 = clear). Also check dialogue completion, reference resolution, protected text and new errors. Report paired differences by paper/size, agreement, denominators, feasible-request completion and conflict detection separately.

The checked-in evaluation is a synthetic smoke run across 2 sizes × 3 strategies. The 15-paper study, human ratings and agreement measurements are outstanding. No comparative scientific claim follows from the fixture.

## Attribution and AI assistance

The two bundled sample papers and their charts are synthetic and bundled for redistributable testing. Runtime dependencies and versions are listed in requirements.txt. The planned dataset and comparator protocol come from the Group 56 proposal and [Paper2Poster](https://github.com/Paper2Poster/Paper2Poster). Codex generated the demo code, sample, prompts, checks and documentation from the project proposal and the request to build and publish a demo. Runtime agent prompts are in paper_canvas/model.py. This demo is not a final course submission or a record of individual student contributions.
