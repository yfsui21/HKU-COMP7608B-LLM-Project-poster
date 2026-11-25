"""Run separate per-paper inference processes with bounded concurrency and preserve all failures."""
import argparse, concurrent.futures, json, os, subprocess, sys, time
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--development',action='store_true');p.add_argument('--plan',default='runs/proposal-data/plan-draft.json')
    p.add_argument('--out',default='runs/proposal-live-study');p.add_argument('--workers',type=int,default=2);args=p.parse_args()
    root=Path(args.out);root.mkdir(parents=True,exist_ok=True)
    plan=json.loads(Path(args.plan).read_text(encoding='utf8'))
    names=plan['development'] if args.development else plan['evaluation']
    status={}
    env=dict(os.environ,PAPERCANVAS_CODEX_MODEL='codex-default',PYTHONIOENCODING='utf-8',
             PAPERCANVAS_INFERENCE_TIMEOUT=os.getenv('PAPERCANVAS_INFERENCE_TIMEOUT','180'))
    env.pop('PAPERCANVAS_GPT_QUEUE',None)
    def run(name):
        out=root/'per-paper'/Path(name).stem
        cmd=[sys.executable,'run_ai_study.py','--plan',args.plan,'--only',name,'--out',str(out)]
        if args.development:cmd.append('--development')
        with (root/(Path(name).stem+'.log')).open('a',encoding='utf8') as log:
            started=time.monotonic(); proc=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,env=env)
        return name,dict(exit_code=proc.returncode,elapsed_s=round(time.monotonic()-started,1),out=str(out))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in concurrent.futures.as_completed([pool.submit(run,n) for n in names]):
            name,result=future.result();status[name]=result
            (root/'batch-status.json').write_text(json.dumps(status,indent=2),encoding='utf8')
            print('PAPER_FINISHED',name,result,flush=True)
    cases=[];dialogues=[]
    for name,r in status.items():
        rp=Path(r['out'])/'results.json'
        if rp.exists():
            records=json.loads(rp.read_text(encoding='utf8'));cases.extend(records['cases']);dialogues.extend(records['dialogues'])
    report=dict(paper_count=len(names),completed_processes=len(status),expected_cases=len(names)*(1 if args.development else 6),
        attempted=len(cases),generated=sum('artifact' in r for r in cases),accepted_outputs=sum(r.get('accepted',False) for r in cases),
        ai_scored=sum(r['status']=='ai_scored' for r in cases),failed_candidate_ai_scored=sum(r['status']=='failed_candidate_ai_scored' for r in cases),
        evaluation_type='AI role-based review, not two independent human raters',human_review_status='not yet reviewed',
        cases=cases,dialogues=dialogues)
    (root/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print('BATCH_FINISHED',len(cases),'cases; generated',report['generated'],'AI-scored',report['ai_scored'],flush=True)

if __name__=='__main__':main()
