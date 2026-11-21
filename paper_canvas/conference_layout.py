"""Measure nested conference panels, then distribute a column's spare height to its figures."""
import io
from PIL import Image
from reportlab.pdfbase.pdfmetrics import stringWidth

BLUE, INK, TEAL, MUTED = "233D65","1C2B3B","126C67","42586D"

def compose(s,asset,wrap):
    tree=s["conference"]
    w,h=s["size"]
    m,gap=22,15
    inner=w-2*m
    items,issues=[],[]
    def box(key,x,y,bw,bh,fill,back=False):
        items.append(dict(id=key,kind="rect",x=x,y=y,w=bw,h=bh,color=fill,background=back))
    def text(key,value,x,y,bw,size,color=INK,bold=False):
        font="Helvetica-Bold" if bold else "Helvetica"
        lines=wrap(value,bw,font,size)
        bh=len(lines)*size*1.18
        items.append(dict(id=key,kind="text",x=x,y=y,w=bw,h=bh,lines=lines,size=size,font=font,color=color))
        if any(stringWidth(line,font,size)>bw+.1 for line in lines):
            issues.append(key+" has a word wider than its box")
        return y+bh
    size=max(23,min(29,inner/stringWidth(s["title"],"Helvetica-Bold",1)))
    title_end=text("title",s["title"],m,13,inner,size,"FFFFFF",True)
    authors=tree.get("authors","")
    y=title_end+5
    if authors:
        y=text("authors",authors,m,y,inner,13,"FFFFFF",True)+3
    y=text("publication",tree["affiliation"],m,y,inner,10,"CEDCEB")+13
    box("masthead",0,0,w,y,BLUE,True)
    box("masthead-edge",0,y,w,3,"43A999")
    top=y+15
    footer=tree.get("credit","")
    foot_y=h-18
    if footer:
        text("source-note",footer,m,foot_y,inner,8.5,MUTED)
    bottom=foot_y-9
    endings=[(title,tree.get(k,""),tree.get(k+"_page")) for title,k in [("Interpretation","takeaway"),("Study limits","limit")] if tree.get(k)]
    if endings:
        cw=(inner-gap*(len(endings)-1))/len(endings)
        height=max(len(wrap(v,cw-20,"Helvetica",12))*14.16 for _,v,_ in endings)+32
        bottom-=height
        box("end-band",m,bottom,inner,height,"EAF1F1",True)
        for j,(title,value,page) in enumerate(endings):
            x=m+j*(cw+gap)+9
            yy=text("end-title-"+str(j),title.upper()+"   /   P. "+str(page),x,bottom+5,cw-20,10,TEAL,True)+2
            text("end-"+str(j),value,x,yy,cw-20,12)
        bottom-=9

    with Image.open(io.BytesIO(asset)) as im:
        aspects={"figure":im.width/im.height}
    if s.get("secondary"):
        aspects["support"]=s["secondary"]["aspect"]
    aspects.update({"exhibit-"+str(i):e["aspect"] for i,e in enumerate(s.get("exhibits",[]))})
    total=inner-2*gap
    middle=total*(.39+.43-s["left_fraction"])

    def panel(p,key,x,y,bw,allow_image,draw):
        # The same routine measures and emits content, so wrapping drives each row height.
        def tx(k,value,size,color=INK,bold=False,indent=0):
            nonlocal y
            if draw:
                y=text(k,value,x+indent,y,bw-indent,size,color,bold)
            else:
                font="Helvetica-Bold" if bold else "Helvetica"
                y+=len(wrap(value,bw-indent,font,size))*size*1.18
        tx(key+"-heading",p["title"],15,BLUE,True)
        y+=4
        if "section" in p:
            idx=p["section"]
            tx(("question","method","result")[idx],s["sections"][idx]["text"],13)
            y+=4
        if "stat" in p:
            tx(key+"-stat",p["stat"],28,TEAL,True)
        if "text" in p:
            tx(key+"-body",p["text"],12.5)
            y+=4
        for j,(name,body) in enumerate(p.get("steps",[])):
            if draw:
                box(key+"-rail-"+str(j),x+1,y+1,2,12,"47A597")
            tx(key+"-step-"+str(j),name,12.5,TEAL,True,10)
            tx(key+"-detail-"+str(j),body,11.8,INK,False,10)
            y+=6
        if "table" in p:
            # Content and row/column sizes also feed the native PowerPoint table.
            values=p["table"]
            fractions=[.60,.18,.22]
            cell_widths=[bw*f for f in fractions]
            sizes=[]
            for r,row in enumerate(values):
                font="Helvetica-Bold" if r==0 else "Helvetica"
                sizes.append(7+max(len(wrap(v,cw-7,font,11.3)) for v,cw in zip(row,cell_widths))*13.4)
            if draw:
                items.append(dict(id=key+"-table",kind="table",x=x,y=y,w=bw,h=sum(sizes),rows=values,widths=cell_widths,heights=sizes))
            y+=sum(sizes)+4
        if "bars" in p:
            for j,(name,n) in enumerate(p["bars"]):
                row_h=20
                if draw:
                    lane=bw-110
                    text(key+"-category-"+str(j),name,x,y,66,11.3)
                    box(key+"-track-"+str(j),x+69,y+3,lane,8,"E1EAEB",True)
                    box(key+"-bar-"+str(j),x+69,y+3,lane*n/p["denominator"],8,TEAL)
                    text(key+"-count-"+str(j),str(n),x+bw-34,y,34,11.3,INK,True)
                y+=row_h
        if "asset" in p and draw:
            name=p["asset"]
            scale=s["figure_scale"] if name=="figure" else 1
            natural=min(bw,allow_image*aspects[name])
            fw=natural*scale
            fh=fw/aspects[name]
            items.append(dict(id=name,kind="image",x=x+(bw-fw)/2,y=y+(allow_image-fh)/2,w=fw,h=fh))
            if fw>bw+.1 or fh>allow_image+.1:
                issues.append(name+" exceeds its conference panel")
            y+=allow_image
        return y+4

    # Keep the middle column under the editor's control. Measure the two outside
    # subtrees at candidate widths before committing space to either sibling.
    def column_cost(group, bw):
        panels=group["panels"]
        available=bottom-(top+32)-9*(len(panels)-1)
        fixed=sum(panel(p,"",0,0,bw,0,False) for p in panels)
        natural=sum(bw/aspects[p["asset"]] for p in panels if "asset" in p)
        return max(0,fixed-available), fixed+natural

    side=total-middle
    candidates=[]
    groups=tree["columns"]
    for fraction in (.40,.425,.45,.475,.50,.525,.55,.575,.60):
        left=side*fraction
        a=column_cost(groups[0],left)
        b=column_cost(groups[2],side-left)
        cost=(max(a[0],b[0]),max(a[1],b[1]),abs(fraction-.5))
        candidates.append((cost,left))
    left=min(candidates)[1]
    widths=[left,middle,side-left]

    x=m
    for col,(group,bw) in enumerate(zip(tree["columns"],widths)):
        prefix="column-"+str(col)
        box(prefix+"-bar",x,top,bw,25,BLUE if col==1 else "DCEAE8",True)
        text(prefix+"-title",group["title"],x+8,top+4,bw-16,14,"FFFFFF" if col==1 else TEAL,True)
        start=top+32
        panels=group["panels"]
        heights=[panel(p,"",0,0,bw,0,False) for p in panels]
        free=bottom-start-sum(heights)-9*(len(panels)-1)
        pics=[i for i,p in enumerate(panels) if "asset" in p]
        if free<0:
            issues.append(group["title"]+" text exceeds column height")
        slots={}
        if pics:
            weights=[min(bw/aspects[panels[i]["asset"]],250)*(s["figure_fraction"]/.4 if panels[i]["asset"]=="figure" else 1) for i in pics]
            slots={i:max(1,free*wt/sum(weights)) for i,wt in zip(pics,weights)}
            if min(slots.values())<45:
                issues.append(group["title"]+" insufficient space for readable figures")
        spacing=9 if pics or len(panels)<2 else 9+min(16,max(0,free)/(len(panels)-1))
        y=start
        for i,p in enumerate(panels):
            if i:
                box(prefix+"-rule-"+str(i),x,y-spacing*.55,bw,.7,"BFCFCE")
            y=panel(p,prefix+"-"+str(i),x,y,bw,slots.get(i,0),True)+spacing
        x+=bw+gap
    return items,issues
