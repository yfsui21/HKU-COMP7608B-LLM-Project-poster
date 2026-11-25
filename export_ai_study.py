"""Export actual batch cases, missing values and AI-review provenance to CSV and per-paper reports."""
import argparse,csv,json,statistics
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,default=Path('runs/proposal-live-study'));args=p.parse_args()
    root=args.study;results=json.loads((root/'results.json').read_text(encoding='utf8'))
    manifest=json.loads(Path('runs/proposal-data/download-status.json').read_text(encoding='utf8'))
    titles={r['file']:r['title'] for r in manifest}
    cases=results['cases'];out=root/'exports';out.mkdir(exist_ok=True)
    headers=['paper','title','area','condition','status','role','checklist_met','checklist_total','coverage','wrong_numbers','figure_mismatches','overflow','overlaps','outside','label_legibility_1_5','reading_order_1_5','notes','artifact']
    rows=[];decisions=[];groups=[]
    for case in cases:
        scores=case.get('ratings',[])
        for role in (1,2):
            score=next((r for r in scores if r['role']==role),None)
            row=dict(paper=case['paper'],title=titles.get(case['paper'],case['paper']),area=case['area'],condition=case['condition'],status=case['status'],role=f'AI-role-{role}',artifact=case.get('artifact',''))
            if score:
                for field in headers[6:16]:
                    row[field]=(score['checklist_met']/score['checklist_total']) if field=='coverage' else score[field]
                row['notes']='AI review; no independent human rating. '+score['notes']
                for d in score['decisions']:decisions.append(dict(paper=case['paper'],area=case['area'],condition=case['condition'],role=role,**d))
            else:row['notes']='NOT_SCORED: '+case.get('error','No valid AI rating')
            if case.get('generation_error'):row['notes']+=' REJECTED_CANDIDATE: '+case['generation_error']
            rows.append(row)
    with (out/'ratings.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=headers);w.writeheader();w.writerows(rows)
    for area in (1,.75):
        for condition in ('joint','content-first','layout-first'):
            records=[r for r in rows if r['area']==area and r['condition']==condition and 'coverage' in r and r['status']=='ai_scored']
            groups.append(dict(area=area,condition=condition,scored_role_rows=len(records),
                coverage=statistics.mean(r['coverage'] for r in records) if records else None,
                label_legibility=statistics.mean(r['label_legibility_1_5'] for r in records) if records else None,
                reading_order=statistics.mean(r['reading_order_1_5'] for r in records) if records else None))
    candidate_groups=[]
    for area in (1,.75):
        for condition in ('joint','content-first','layout-first'):
            records=[r for r in rows if r['area']==area and r['condition']==condition and 'coverage' in r and r['status'] in ('ai_scored','failed_candidate_ai_scored')]
            candidate_groups.append(dict(area=area,condition=condition,scored_role_rows=len(records),
                coverage=statistics.mean(r['coverage'] for r in records) if records else None,
                label_legibility=statistics.mean(r['label_legibility_1_5'] for r in records) if records else None,
                reading_order=statistics.mean(r['reading_order_1_5'] for r in records) if records else None))
    paired=[]
    for paper in {c['paper'] for c in cases}:
        for area in (1,.75):
            available={c['condition']:c for c in cases if c['paper']==paper and c['area']==area and c['status']=='ai_scored'}
            if 'joint' not in available:continue
            for baseline in ('content-first','layout-first'):
                if baseline not in available:continue
                def average(c,metric):
                    return statistics.mean((r['checklist_met']/r['checklist_total']) if metric=='coverage' else r[metric] for r in c['ratings'])
                paired.append(dict(paper=paper,area=area,baseline=baseline,joint_minus_baseline={m:average(available['joint'],m)-average(available[baseline],m) for m in ('coverage','label_legibility_1_5','reading_order_1_5')}))
    summary=dict(paper_count=results['paper_count'],expected_cases=results['expected_cases'],attempted=len(cases),
        generated=sum('artifact' in c for c in cases),accepted_outputs=sum(c.get('accepted',False) for c in cases),
        ai_scored_cases=sum(c['status']=='ai_scored' for c in cases),failed_candidate_ai_scored=sum(c['status']=='failed_candidate_ai_scored' for c in cases),
        human_review_status='New batch not yet reviewed by the user',independent_human_raters=0,groups=groups,
        all_exported_candidate_diagnostic_groups=candidate_groups,paired_differences=paired)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    (out/'workbook-data.json').write_text(json.dumps(dict(summary=summary,headers=headers,rows=rows,decisions=decisions),ensure_ascii=False,indent=2),encoding='utf8')
    for name in sorted({c['paper'] for c in cases}):
        subset=[c for c in cases if c['paper']==name]
        lines=[titles.get(name,name),f'Source file: {name}',
            'Evaluation: two AI role-conditioned perspectives from the same backend, not two independent human raters.',
            'The AI-authored nine-item source checklist and model replies accompany each case.',
            'This new paper has not yet received documented human review. Missing scores are not zero.', '']
        for c in subset:
            lines.append(f"Area {c['area']}, strategy {c['condition']}: {c['status']}")
            if c.get('error'):lines.append('Failure: '+c['error'])
            if c.get('generation_error'):lines.append('Rejected candidate, diagnostic scoring only: '+c['generation_error'])
            for s in c.get('ratings',[]):
                lines.append(f"AI role {s['role']}: {s['checklist_met']}/{s['checklist_total']}, labels {s['label_legibility_1_5']}/5, order {s['reading_order_1_5']}/5")
                lines.append(s['notes'])
                lines.extend(f"  {d['id']}: {'met' if d['met'] else 'not met'}; {d['reason']}" for d in s['decisions'])
            lines.append('')
        (out/(Path(name).stem+'-review.txt')).write_text('\n'.join(lines),encoding='utf-8-sig')
    (out/'study-report.txt').write_text(f"PaperCanvas {summary['paper_count']}-paper AI-assisted study\n"+json.dumps(summary,ensure_ascii=False,indent=2)+'\n\nScores refer only to available successful cases. All failures remain in the completion denominator. This is not a two-human-evaluator study.\n',encoding='utf-8-sig')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
