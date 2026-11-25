# PaperCanvas AI-assisted study evidence

These are AI role-conditioned reviews. The two roles are not two independent human evaluators.
The t-SNE pilot has a user-reported human-review confirmation; no such confirmation is inferred for new papers.

## Sources and split
Source population: https://huggingface.co/datasets/Paper2Poster/Paper2Poster (100 rows).
Seed 56 selects 20 rows in official row-index order, first 5 development and last 15 evaluation.
Paper URLs, hashes and download failures are recorded in download-status.json.
Reference posters and dataset QA are not supplied to the generator or reviewer.
Source PDFs are kept locally; follow the original paper links to obtain them.

## Status interpretation
offline-preflight contains rule-based generation checks, not the live-model experiment.
Only live-results.json, when present, reports the completed live batch.
Missing or failed candidates have no invented numeric ratings.
The source checklist is model-generated; independent human evaluation remains outstanding.
Current backend model-call/token limits and calibration limitations are recorded in each protocol.json.

## Reproduce
Use Python 3.12 and install requirements.txt in a virtual environment.
1. python prepare_proposal_data.py
2. Configure a logged-in Codex CLI and PAPERCANVAS_CODEX_MODEL=codex-default.
3. python batch_ai_study.py --development --plan runs/proposal-data/plan-development.json --out runs/proposal-development
4. Review development results and freeze the protocol before testing.
5. python batch_ai_study.py --plan runs/proposal-data/plan-frozen.json --out runs/proposal-live-study
6. python package_ai_study.py

The development and frozen plans must be supplied and versioned after calibration; the downloader creates a draft only.
Each generation case receives two content and two layout passes, with review after initialization and each pass.
Each AI role uses the same nine source-grounded criteria; failed checks are preserved.
The role-based AI review does not fulfil the original proposed two-human-evaluator methodology.
