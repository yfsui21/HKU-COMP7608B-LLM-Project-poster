"""Download official metadata and a reproducible 5/15 PDF split; never fabricate missing inputs."""
import argparse, concurrent.futures, hashlib, json, random, time, urllib.request
from pathlib import Path

API='https://datasets-server.huggingface.co/rows?dataset=Paper2Poster%2FPaper2Poster&config=default&split=train&offset=0&length=100'

def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'PaperCanvas-research/1.0'})
    with urllib.request.urlopen(req,timeout=60) as response:
        return response.read()

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=Path('runs/proposal-data'));args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    manifest=args.out/'official-metadata.json'
    if not manifest.exists():manifest.write_bytes(fetch(API))
    raw=json.loads(manifest.read_text(encoding='utf8'))
    rows=sorted(raw['rows'],key=lambda r:r['row_idx'])
    if len(rows)!=100:raise ValueError('Expected official 100-row population')
    selected=random.Random(56).sample(rows,20)
    papers=args.out/'papers';papers.mkdir(exist_ok=True)
    results=[]
    def download(item):
        index,record=item; row=record['row']; name=f"paper-{record['row_idx']:03}.pdf"; path=papers/name
        result=dict(file=name,title=row['title'],paper_url=row['paper_url'],reference_poster_url=row['image_url'],
                    official_row_index=record['row_idx'],split='development' if index<5 else 'evaluation')
        try:
            if not path.exists():
                content=fetch(row['paper_url'])
                if not content.lstrip().startswith(b'%PDF-'):raise ValueError('Response is not a PDF')
                path.write_bytes(content)
            content=path.read_bytes()
            if not content.lstrip().startswith(b'%PDF-'):raise ValueError('Invalid saved PDF')
            result.update(downloaded=True,sha256=hashlib.sha256(content).hexdigest(),bytes=len(content))
        except Exception as exc:result.update(downloaded=False,error=str(exc))
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for r in pool.map(download,enumerate(selected)):
            results.append(r);print(r['file'],r['split'],'OK' if r['downloaded'] else r['error'],flush=True)
            (args.out/'download-status.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    evaluation=[r['file'] for r in results if r['split']=='evaluation']
    scripts=[['enlarge figure by 35% and protect result','feasible'],['shorten question to 6 words','feasible'],['shorten protected section to 2 words','conflict']]
    plan=dict(seed=56,population='Official Paper2Poster train 100, ordered by row_idx; sample20 then 5/15',
              metadata_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),development=[r['file'] for r in results[:5]],
              evaluation=evaluation,dialogues={n:scripts for n in random.Random(57).sample(evaluation,5)},size=[1008,540],
              status='DRAFT: develop/calibrate prompts, dimensions and dialogue feasibility before freezing',
              scoring_method='AI role-based review with separately documented human review; not two independent human raters')
    (args.out/'plan-draft.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
    print(f"PDF inputs ready: {sum(r['downloaded'] for r in results)}/20. No generation or ratings implied.")

if __name__=='__main__':main()
