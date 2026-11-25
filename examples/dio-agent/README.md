# DIO-Agent: curated NeurIPS 2026 showcase

This example summarizes **Agentic Reverse Engineering: From I/O to Code with Discovery Agent**
(Dong et al., NeurIPS 2026), arXiv v2 dated 18 October 2026. Paper and conference URLs,
authors, and the PDF hash are in `source.json`. The paper's results belong to its authors.
This is an assisted demonstration, **not an additional sample in the 90-case evaluation**.

Open `preview.png`, `poster.pptx` (editable, 48 × 36 inches), or `poster.pdf`.
`raw.png` / `raw.pptx` preserve the uncorrected generation. `before.png` is the
prepared baseline for dialogue. `session/` retains all accepted versions and the
state pointing to the final version. `summary.json` contains measured changes.

## What actually happened

- Pinned PosterForest `dd4b060f3e5e8ed511693bdc9e24a1d1e57165f9` generated a
  native poster using the configured HTTPS service and `gpt-6.1-sol`: 55 calls,
  576.32 seconds. The provider model name is recorded; it is not an independently
  verified immutable model snapshot. Generation prompts and image hashes are archived
  in `generation-prompts.jsonl`; the full upstream working directory is local only.
- Inspection found a wrong Table 3 crop and mixed author/affiliation colors. An
  explicit preparation step replaced the crop and normalized the header before
  dialogue. These two repairs were scripted by the development assistant, not
  performed by the native dialogue editor. See `preparation.json` and
  `scripts/prepare_dio_showcase.py` (specific to this recorded slide's shape IDs).
- `turn-*.json` records **every** attempted request, its outcome, model calls and
  version pointers. The first attempt lacks raw response bodies; later successful
  proposals and reviews are retained. Failed model responses appear as errors.
  Rejected requests did not replace the accepted poster.
- Before request 3, the reviewer prompt was corrected to explicitly
  request JSON. Before request 8, the planner prompt was clarified to distinguish proposing
  an edit from executing the renderer. These are development iterations, not
  fixed-system success-rate measurements.
- The source prose and Figure 3 give different sampling-comparison averages.
  This discrepancy motivated removing that numeric prose comparison from the
  showcase while retaining the source chart and matched-budget description.

The accepted review is an AI review. A team member still needs to perform and
record the human check described in `HUMAN_REVIEW.md`. No aesthetic score or
independent human preference improvement is claimed.

## Run with your own credentials

Install the main and pinned upstream requirements as described in the root README.
Configure `OPENAI_API_KEY`, `OPENAI_BASE_URL` (HTTPS), and `PAPERCANVAS_MODEL` in
**your shell**, without adding credentials to tracked files. Set the latter to a
vision-capable model supported by your service. Paid inference is required.

```sh
curl --fail -L https://arxiv.org/pdf/2605.15334v2 -o runs/discovery-agent.pdf
python papercanvas.py pipeline runs/discovery-agent.pdf --backend api --model "$PAPERCANVAS_MODEL" --source runs/posterforest-source --soffice /path/to/soffice --work runs/dio-new-generation --out runs/dio-new-session --layout tree
```

New generation is variable; its shape IDs may differ. The preparation script is
for replaying the recorded export, not for arbitrary new posters. To continue the
recorded session, copy `session/` to a new directory and put the verified source
PDF at `source.pdf` within it. Then run:

```sh
python scripts/showcase_edit.py runs/dio-replay 'Shorten the introduction while preserving the result.' --soffice /path/to/soffice --record runs/dio-replay/next-turn.json
```

Inspect the returned acceptance status and preview. Rejected edits retain the
preceding version. Raw prompts include published paper text but no API credentials.
