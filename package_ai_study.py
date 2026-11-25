"""Prepare reviewable study evidence for Git. Never packages credentials, PDFs, logs or node_modules."""
import argparse,json,shutil
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,default=Path('runs/proposal-live-study'));args=p.parse_args()
    dest=Path('evaluation/proposal-ai-study');dest.mkdir(parents=True,exist_ok=True)
    data=Path('runs/proposal-data')
    for name in ('download-status.json','plan-draft.json','plan-development.json','plan-frozen.json'):
        if (data/name).exists():shutil.copy2(data/name,dest/name)
    single=Path('runs/human-review/ai-simulated-review');sd=dest/'tsne-pilot';sd.mkdir(exist_ok=True)
    for name in ('evaluation-provenance.json','human-review-confirmation.json','ratings-data.json','paper-checklist.txt','单论文评价报告_AI模拟.txt','评价方式变更说明.txt'):
        if (single/name).exists():shutil.copy2(single/name,sd/name)
    for name in ('rater-1.csv','rater-2.csv','dialogue-rater-1.csv','dialogue-rater-2.csv'):
        shutil.copy2(single/'study/blinded'/name,sd/name)
    shutil.copy2(single/'study/summary.json',sd/'summary.json')
    workbook=single/'outputs/tsne-ai-review/tSNE_AI模拟评分.xlsx'
    if workbook.exists():shutil.copy2(workbook,sd/workbook.name)
    preflight=Path('runs/proposal-offline-preflight')
    if preflight.exists():
        pd=dest/'offline-preflight';pd.mkdir(exist_ok=True)
        for name in ('results.json','summary.json','settings.json','protocol.json'):
            if (preflight/name).exists():shutil.copy2(preflight/name,pd/name)
    if (args.study/'results.json').exists():
        live_results=json.loads((args.study/'results.json').read_text(encoding='utf8'))
        for case in live_results['cases']:
            if not case.get('artifact'):continue
            source=Path(case['artifact'])
            target=dest/'live/artifacts'/Path(case['paper']).stem/str(case['area'])/case['condition']
            target.mkdir(parents=True,exist_ok=True)
            for name in ('poster.pptx','preview.png','review.json'):
                if (source/name).exists():shutil.copy2(source/name,target/name)
            case['packaged_artifact']=target.relative_to(dest).as_posix()
        (dest/'live-results.json').write_text(json.dumps(live_results,ensure_ascii=False,indent=2),encoding='utf8')
        if (args.study/'exports').exists():
            shutil.copytree(args.study/'exports',dest/'exports',dirs_exist_ok=True)
        for paper in (args.study/'per-paper').glob('*'):
            for name in ('protocol.json','results.json','dialogues-progress.json'):
                if (paper/name).exists():
                    target=dest/'live'/paper.name;target.mkdir(parents=True,exist_ok=True);shutil.copy2(paper/name,target/name)
            for item in paper.rglob('checklist.json'):
                target=dest/'live'/paper.name/'checklist.json';target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(item,target)
            for item in paper.rglob('ai-role-*.json'):
                rel=item.relative_to(paper);target=dest/'live'/paper.name/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(item,target)
    for development in ('proposal-development','proposal-development-v2'):
        source=Path('runs')/development/'results.json'
        if source.exists():shutil.copy2(source,dest/(development+'-results.json'))
    (dest/'README.md').write_text('''# PaperCanvas AI-assisted study evidence

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
''',encoding='utf8')
    print(dest)

if __name__=='__main__':main()
