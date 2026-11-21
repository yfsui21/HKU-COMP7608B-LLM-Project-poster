"""Compose a single scientific poster in points and emit editable PPTX and PNG."""
import io
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Pt
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
import pypdfium2 as pdfium
from .conference_layout import compose

IDS=("question","method","result")
INK, NAVY, ACCENT, QUIET = "202937","202E49","A5462E","485568"
CREAM, PALE, WHITE = "FAF7EE","E8ECEF","FFFFFF"

def wrap(text,width,font,size):
    lines=[]
    for paragraph in text.splitlines():
        line=""
        for word in paragraph.split():
            candidate=(line+" "+word).strip()
            if line and stringWidth(candidate,font,size)>width:
                lines.append(line)
                line=word
            else:
                line=candidate
        lines.append(line)
    return lines or [""]

def scene(s,asset):
    w,h=s["size"]
    if "forest" in s:
        from .forest import scene as forest_scene
        return checked_scene(s,*forest_scene(s,wrap))
    if s.get("conference"):
        items,issues=compose(s,asset,wrap)
        return checked_scene(s,items,issues)
    margin,gutter=.035*w,.026*w
    inside=w-2*margin
    items,issues=[],[]
    def rect(key,x,y,width,height,color,background=False):
        items.append(dict(id=key,kind="rect",x=x,y=y,w=width,h=height,color=color,background=background))
    def block(key,text,x,y,width,size,color=INK,font="Helvetica"):
        lines=wrap(text,width,font,size)
        height=len(lines)*size*1.18
        items.append(dict(id=key,kind="text",x=x,y=y,w=width,h=height,lines=lines,size=size,color=color,font=font))
        if any(stringWidth(t,font,size)>width+.1 for t in lines):
            issues.append(key+" has a word wider than its box")
        return y+height
    def label(key,text,x,y,width,color=ACCENT):
        return block(key,text.upper(),x,y,width,10.5,color,"Helvetica-Bold")

    # Masthead: bibliographic context and study quantities taken from the method.
    measures=s.get("measures",[])
    space=min(260,w*.30) if measures else 0
    title_width=inside-space-(gutter if space else 0)
    label("publication",s.get("source_label","Research article"),margin,14,title_width)
    title_end=block("title",s["title"],margin,30,title_width,33,NAVY,"Times-Bold")
    measure_end=0
    for i,m in enumerate(measures):
        cw=space/len(measures)
        x=w-margin-space+i*cw
        rect("measure-rule-"+str(i),x,20,1,42,"BDB8A8")
        end=block("value-"+str(i),m["value"],x+9,15,cw-11,25,ACCENT,"Times-Bold")
        measure_end=max(measure_end,block("unit-"+str(i),m["label"],x+9,end+1,cw-11,10,QUIET))
    top=max(title_end,measure_end)+17
    rect("masthead",0,0,w,top,CREAM,True)
    rect("header-rule",0,top,w,1.3,NAVY)

    # Size the lower contextual strip from its actual text, before allocating figure space.
    notes=s.get("notes",[])[:2]
    footer_text=s.get("credit","")
    footer_text=(footer_text+"    |    " if footer_text else "")+"Source figure: p. "+str(s["evidence"]["page"])
    footer_h=len(wrap(footer_text,inside,"Helvetica",9))*10.62
    footer_y=h-footer_h-6
    bottom=footer_y-10
    if notes:
        cw=(inside-(len(notes)-1)*gutter)/len(notes)
        text_h=max(len(wrap(n["text"],cw,"Helvetica",13))*13*1.18 for n in notes)
        band_top=bottom-(text_h+34)
        rect("context-band",0,band_top,w,bottom-band_top,"ECEBE1",True)
        for i,n in enumerate(notes):
            x=margin+i*(cw+gutter)
            label("note-head-"+str(i),n["label"]+"   /   p. "+str(n["page"]),x,band_top+5,cw)
            block("note-"+str(i),n["text"],x,band_top+20,cw,13)
        bottom=band_top-9
    block("source-note",footer_text,margin,footer_y,inside,9,QUIET)

    # Left column contains the scientific question, protocol and optional second exhibit.
    left=(inside-gutter)*s["left_fraction"]
    rx=margin+left+gutter
    right=w-margin-rx
    y=top+14
    for i in (0,1):
        part=s["sections"][i]
        end=label(IDS[i]+"-heading",part["label"]+"   /   p. "+str(part["page"]),margin,y,left)
        y=block(IDS[i],part["text"],margin,end+5,left,18 if i==0 else 15.5,
                NAVY if i==0 else INK,"Times-Bold" if i==0 else "Helvetica")
        if i==0:
            rect("method-rule",margin,y+8,left,.7,"C3C9CB")
            y+=17
    secondary=s.get("secondary")
    definition=s.get("definition")
    if definition and not secondary:
        available=bottom-y
        text_height=len(wrap(definition["text"],left-22,"Helvetica",14))*14*1.18
        if available>=text_height+61:
            start=y+17
            rect("definition-fill",margin,start,left,text_height+33,PALE,True)
            rect("definition-accent",margin,start,3,text_height+33,ACCENT)
            end=label("definition-title","Outcome measure   /   p. "+str(definition["page"]),margin+11,start+5,left-22)
            y=block("definition",definition["text"],margin+11,end+4,left-22,14)
    if secondary:
        y=label("support-heading","Supporting result   /   figure on p. "+str(secondary["page"]),margin,y+14,left)+5
        y=block("support-claim",secondary["claim"],margin,y,left,14)
        max_h=bottom-y-8
        fw=min(left,max(35,max_h)*secondary["aspect"])
        fh=fw/secondary["aspect"]
        items.append(dict(id="support",kind="image",x=margin+(left-fw)/2,y=y+5,w=fw,h=fh))
        y+=5+fh
    if y>bottom+.1:
        issues.append("left column exceeds body area")

    # Main finding and its evidence occupy one visual region; the source crop is never stretched.
    px=14
    width=right-2*px
    start=top+9
    rect("finding-panel",rx,start,right,max(1,bottom-start),NAVY,True)
    result=s["sections"][2]
    end=label("result-heading",result["label"]+"   /   p. "+str(result["page"]),rx+px,start+12,width,"F9BA92")
    end=block("result",result["text"],rx+px,end+5,width,19,WHITE,"Times-Bold")
    caption=s["evidence"]["caption"]
    caption_height=len(wrap(caption,width,"Helvetica",12))*12*1.18
    chart_y=end+9
    available=bottom-chart_y-caption_height-21
    with Image.open(io.BytesIO(asset)) as im:
        aspect=im.width/im.height
    natural=min(width,max(16,available)*min(1,s["figure_fraction"]/.40)*aspect)
    fw=natural*s["figure_scale"]
    fh=fw/aspect
    plate_h=max(fh+6,min(max(1,available),width/aspect+7))
    rect("figure-mount",rx+px,chart_y,width,plate_h,WHITE,True)
    items.append(dict(id="figure",kind="image",x=rx+px+(width-fw)/2,y=chart_y+(plate_h-fh)/2,w=fw,h=fh))
    caption_end=block("caption",caption,rx+px,chart_y+plate_h+5,width,12,"E4EAF1")
    if caption_end>bottom-1:
        issues.append("caption exceeds result panel")
    if fw>width+.1:
        issues.append("figure wider than allocated result column")

    return checked_scene(s,items,issues)

def checked_scene(s,items,issues):
    w,h=s["size"]
    fixed=s.get("fixed_boxes",{})
    for item in items:
        if item["id"] in fixed:
            box=fixed[item["id"]]
            item["x"],item["y"]=box["x"],box["y"]
            if item["kind"]=="text" and item["h"]>box["h"]+.1:
                issues.append(item["id"]+" overflows fixed text box")
    for i,a in enumerate(items):
        if min(a["x"],a["y"])<-.1 or a["x"]+a["w"]>w+.1 or a["y"]+a["h"]>h+.1:
            issues.append(a["id"]+" outside canvas")
        if a.get("background"):
            continue
        for b in items[:i]:
            if b.get("background"):
                continue
            dx=min(a["x"]+a["w"],b["x"]+b["w"])-max(a["x"],b["x"])
            dy=min(a["y"]+a["h"],b["y"]+b["h"])-max(a["y"],b["y"])
            if min(dx,dy)>.1:
                issues.append(a["id"]+" overlaps "+b["id"])
    return items,issues

def render(s,asset,folder,figure_proofs=False):
    folder.mkdir(parents=True,exist_ok=True)
    items,issues=scene(s,asset)
    w,h=s["size"]
    deck=Presentation()
    deck.slide_width,deck.slide_height=Pt(w),Pt(h)
    slide=deck.slides.add_slide(deck.slide_layouts[6])
    buf=io.BytesIO()
    pdf=Canvas(buf,pagesize=(w,h))
    for a in sorted(items,key=lambda e:not e.get("background",False)):
        x,y,bw,bh=(a[k] for k in ("x","y","w","h"))
        coords=list(map(Pt,(x,y,bw,bh)))
        if a["kind"]=="image":
            data=asset if a["id"]=="figure" else (folder.parent/(a["id"]+".png")).read_bytes()
            shape=slide.shapes.add_picture(io.BytesIO(data),*coords)
            pdf.drawImage(ImageReader(io.BytesIO(data)),x,h-y-bh,bw,bh)
        elif a["kind"]=="table":
            shape=slide.shapes.add_table(len(a["rows"]),len(a["widths"]),*coords)
            table=shape.table
            for j,width in enumerate(a["widths"]):
                table.columns[j].width=Pt(width)
            yy=y
            for i,row in enumerate(a["rows"]):
                xx=x
                rh=a["heights"][i]
                table.rows[i].height=Pt(rh)
                fill="233D65" if i==0 else ("EAF1F1" if i%2 else "F7F9FA")
                color="FFFFFF" if i==0 else "1C2B3B"
                font="Helvetica-Bold" if i==0 else "Helvetica"
                for j,value in enumerate(row):
                    cw=a["widths"][j]
                    cell=table.cell(i,j)
                    cell.fill.solid()
                    cell.fill.fore_color.rgb=RGBColor.from_string(fill)
                    cell.margin_left=cell.margin_right=Pt(3)
                    cell.margin_top=cell.margin_bottom=Pt(1)
                    cell.text=value
                    p=cell.text_frame.paragraphs[0]
                    p.font.name="Arial"
                    p.font.size=Pt(11.3)
                    p.font.bold=i==0
                    p.font.color.rgb=RGBColor.from_string(color)
                    pdf.setFillColor("#"+fill)
                    pdf.rect(xx,h-yy-rh,cw,rh,stroke=0,fill=1)
                    pdf.setFont(font,11.3)
                    pdf.setFillColor("#"+color)
                    for k,line in enumerate(wrap(value,cw-7,font,11.3)):
                        pdf.drawString(xx+3,h-yy-13-k*13.4,line)
                    xx+=cw
                yy+=rh
        elif a["kind"]=="rect":
            shape=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,*coords)
            shape.fill.solid()
            shape.fill.fore_color.rgb=RGBColor.from_string(a["color"])
            shape.line.fill.background()
            shape.shadow.inherit=False
            pdf.setFillColor("#"+a["color"])
            pdf.rect(x,h-y-bh,bw,bh,stroke=0,fill=1)
        else:
            shape=slide.shapes.add_textbox(*coords)
            frame=shape.text_frame
            frame.margin_left=frame.margin_right=frame.margin_top=frame.margin_bottom=0
            frame.word_wrap=False
            pdf.setFillColor("#"+a["color"])
            pdf.setFont(a["font"],a["size"])
            for i,line in enumerate(a["lines"]):
                p=frame.paragraphs[0] if i==0 else frame.add_paragraph()
                p.text=line
                p.font.name="Times New Roman" if a["font"].startswith("Times") else "Arial"
                p.font.bold=a["font"].endswith("-Bold")
                p.font.size=Pt(a["size"])
                p.font.color.rgb=RGBColor.from_string(a["color"])
                p.line_spacing=Pt(a["size"]*1.18)
                p.space_after=p.space_before=Pt(0)
                pdf.drawString(x,h-y-a["size"]-i*a["size"]*1.18,line)
        shape.name=a["id"]
    deck.save(folder/"poster.pptx")
    Presentation(folder/"poster.pptx")
    pdf.save()
    with pdfium.PdfDocument(buf.getvalue()) as doc:
        doc[0].render(scale=min(1.4,1800/max(w,h))).to_pil().save(folder/"preview.png")
        if figure_proofs:
            for a in items:
                if a["kind"]=="image":
                    x,y,bw,bh=(a[k] for k in ("x","y","w","h"))
                    if min(x,y,w-x-bw,h-y-bh)>=0:
                        crop=(x,h-y-bh,w-x-bw,y)
                        doc[0].render(scale=1,crop=crop).to_pil().save(folder/(a["id"]+"-proof.png"))
    return items,issues
