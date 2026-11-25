"""Reproduce the disclosed crop and author-style repairs for the DIO showcase."""
import sys,shutil,json,io,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from PIL import Image
from paper_canvas.native_tree import digest, render_preview
from paper_canvas.extract import figure_options
from paper_canvas.source import page_png
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('source_session',type=Path)
p.add_argument('destination',type=Path)
p.add_argument('--soffice',required=True)
p.add_argument('--record',type=Path,required=True)
args=p.parse_args()
src=args.source_session;dst=args.destination
shutil.copytree(src,dst)
p=dst/'original/poster.pptx'; deck=Presentation(p);slide=deck.slides[0]
shapes={s.shape_id:s for s in slide.shapes}
s=shapes[62]
ev=next(e for e in figure_options(dst/'source.pdf') if e['caption'].startswith('Table 3:'))
asset=page_png(dst/'source.pdf',ev['page'],ev['box'],detail=True)
_,rid=slide.part.get_or_add_image_part(io.BytesIO(asset));s._pic.blipFill.blip.rEmbed=rid
with Image.open(io.BytesIO(asset)) as img: ratio=img.width/img.height
x,y,w,h=s.left,s.top,s.width,s.height
nw=min(w,h*ratio);nh=nw/ratio
s.left=int(x+(w-nw)/2);s.top=int(y+(h-nh)/2);s.width=int(nw);s.height=int(nh)
s=shapes[31]
for para in s.text_frame.paragraphs:
    para.alignment=PP_ALIGN.CENTER
    for run in para.runs:
        run.font.color.rgb=RGBColor(255,255,255)
        if 'Shanghai Jiao Tong' in run.text:
            run.text='\n'+run.text.lstrip()
deck.save(p)
state=json.loads((dst/'state.json').read_text());state['original_sha256']=digest(p)
state['original_coverage']=render_preview(p,args.soffice,dst/'original')
(dst/'state.json').write_text(json.dumps(state,indent=2))
args.record.parent.mkdir(parents=True,exist_ok=True)
args.record.write_text(json.dumps(dict(raw_sha256=digest(src/'original/poster.pptx'),prepared_sha256=digest(p),operations=['Replace misidentified Table 3 crop with source page 7 ablation table, preserve aspect ratio and center in original box.','Normalize author and affiliation text to white and center; separate affiliations with line break.'],assisted_preparation=True,excluded_from_batch=True),indent=2))
print(dst)
