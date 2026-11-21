"""One measured scene for editable PPT shapes and the PNG preview (points)."""
import io
from pathlib import Path
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Pt
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
import pypdfium2 as pdfium

INK, TEAL, MUTED = "142F36", "0B7169", "455E65"
IDS = ("question","method","result")

def wrap(text, width, font, size):
    lines = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        line = ""
        for word in words:
            candidate = (line+" "+word).strip()
            if line and stringWidth(candidate,font,size)>width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines or [""]

def scene(s, asset):
    w,h = s["size"]
    m,g = 0.047*w,0.037*w
    items,issues = [],[]
    def block(key,text,x,y,width,size,color=INK,bold=False):
        font = "Helvetica-Bold" if bold else "Helvetica"
        lines = wrap(text,width,font,size)
        height = len(lines)*size*1.25
        items.append(dict(id=key,kind="text",x=x,y=y,w=width,h=height,lines=lines,size=size,color=color,bold=bold))
        if any(stringWidth(t,font,size)>width+.1 for t in lines):
            issues.append(key+" has a word wider than its box")
        return y+height
    def rule(key,x,y,width,height,color):
        items.append(dict(id=key,kind="rect",x=x,y=y,w=width,h=height,color=color))
    rule("top",0,0,w,9,TEAL)
    title_end = block("title",s["title"],m,m,w-2*m,36,bold=True)
    top = title_end+37
    rule("divider",m,top-18,w-2*m,1,"AECBC7")
    inner = w-2*m-g
    left = inner*s["left_fraction"]
    right = inner-left
    rx = m+left+g
    def section(idx,x,y,width):
        part = s["sections"][idx]
        end = block(IDS[idx]+"-heading",part["label"],x,y,width,17,TEAL,True)
        return block(IDS[idx],part["text"],x,end+9,width,22)
    end = section(0,m,top,left)
    section(1,m,end+40,left)
    end = section(2,rx,top,right)
    img = Image.open(io.BytesIO(asset))
    available_h = (h-top-m)*s["figure_fraction"]
    natural_w = min(right,available_h*img.width/img.height)
    fw = natural_w*s["figure_scale"]
    fh = fw*img.height/img.width
    fy = end+22
    items.append(dict(id="figure",kind="image",x=rx,y=fy,w=fw,h=fh))
    if fw>right+.1:
        issues.append("figure wider than allocated result column")
    block("caption",s["evidence"]["caption"],rx,fy+fh+9,right,15,MUTED)
    fixed = s.get("fixed_boxes",{})
    for item in items:
        if item["id"] in fixed:
            box = fixed[item["id"]]
            item["x"],item["y"] = box["x"],box["y"]
            if item["kind"]=="text" and item["h"]>box["h"]+.1:
                issues.append(item["id"]+" overflows fixed text box")
    for i,a in enumerate(items):
        if a["x"]<0 or a["y"]<0 or a["x"]+a["w"]>w+.1 or a["y"]+a["h"]>h+.1:
            issues.append(a["id"]+" outside canvas")
        for b in items[:i]:
            dx=min(a["x"]+a["w"],b["x"]+b["w"])-max(a["x"],b["x"])
            dy=min(a["y"]+a["h"],b["y"]+b["h"])-max(a["y"],b["y"])
            if min(dx,dy)>.1:
                issues.append(a["id"]+" overlaps "+b["id"])
    return items,issues

def render(s, asset, folder):
    folder.mkdir(parents=True,exist_ok=True)
    items, issues = scene(s,asset)
    w,h = s["size"]
    deck = Presentation()
    deck.slide_width,deck.slide_height = Pt(w),Pt(h)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    buf = io.BytesIO()
    pdf = Canvas(buf,pagesize=(w,h))
    for a in items:
        x,y,bw,bh = (a[k] for k in ("x","y","w","h"))
        coords = list(map(Pt,(x,y,bw,bh)))
        if a["kind"]=="image":
            shape = slide.shapes.add_picture(io.BytesIO(asset),*coords)
            pdf.drawImage(ImageReader(io.BytesIO(asset)),x,h-y-bh,bw,bh)
        elif a["kind"]=="rect":
            shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,*coords)
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(a["color"])
            shape.line.fill.background()
            pdf.setFillColor("#"+a["color"])
            pdf.rect(x,h-y-bh,bw,bh,stroke=0,fill=1)
        else:
            shape = slide.shapes.add_textbox(*coords)
            frame=shape.text_frame
            frame.margin_left=frame.margin_right=frame.margin_top=frame.margin_bottom=0
            frame.word_wrap=False
            pdf.setFillColor("#"+a["color"])
            pdf.setFont("Helvetica-Bold" if a["bold"] else "Helvetica",a["size"])
            for i,line in enumerate(a["lines"]):
                p = frame.paragraphs[0] if i==0 else frame.add_paragraph()
                p.text=line
                p.font.name,p.font.size,p.font.bold="Arial",Pt(a["size"]),a["bold"]
                p.font.color.rgb=RGBColor.from_string(a["color"])
                p.line_spacing=Pt(a["size"]*1.25)
                p.space_after=p.space_before=Pt(0)
                pdf.drawString(x,h-y-a["size"]-i*a["size"]*1.25,line)
        shape.name=a["id"]
    deck.save(folder/"poster.pptx")
    Presentation(folder/"poster.pptx")
    pdf.save()
    with pdfium.PdfDocument(buf.getvalue()) as doc:
        doc[0].render(scale=1.4).to_pil().save(folder/"preview.png")
    return items,issues
