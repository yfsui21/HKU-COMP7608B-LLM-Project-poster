"""Original synthetic paper with page-separated methods and vector evidence."""
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas

c = Canvas(str(Path(__file__).with_name("retrieval_study.pdf")), pagesize=(680,820), invariant=1)
c.setTitle("Retrieval under a fixed token budget")
def text(y, words, size=13, bold=False):
    c.setFillColor("#1E3040")
    c.setFont("Helvetica-Bold" if bold else "Helvetica",size)
    c.drawString(44,y,words)

text(751,"Retrieval under a fixed token budget",29,True)
text(718,"Synthetic experiment for the PaperCanvas demonstration",14)
text(641,"Research question",20,True)
text(609,"Can hybrid retrieval improve evidence recall under a fixed token budget?")
text(580,"We study the quality of evidence selected before answer generation.")
text(503,"Method",20,True)
text(470,"We compare lexical, dense and hybrid retrieval on 200 synthetic queries.")
text(444,"All systems retrieve five passages with a context limit of 2048 tokens.")
text(367,"Evaluation",20,True)
text(334,"Each query has one annotated evidence passage in a fixed corpus.")
text(308,"Recall at five counts queries whose evidence appears among five passages.")
c.showPage()
text(752,"Results",24,True)
text(710,"At five passages, hybrid retrieval reached 84% recall versus 71% for dense retrieval.")
text(683,"Lexical retrieval reached 62% recall on the same synthetic query set.")
text(590,"Evidence recall",19,True)
for y,label,value,color in [(521,"Lexical",62,"#849AAA"),(456,"Dense",71,"#537B83"),(391,"Hybrid",84,"#117365")]:
    text(y,label,16)
    c.setFillColor(color)
    c.rect(139,y-3,4.3*value,27,fill=1,stroke=0)
    c.setFont("Helvetica-Bold",16)
    c.drawString(148+4.3*value,y,str(value)+"%")
text(327,"Figure 1. Evidence recall at five passages for three retrieval methods.",13)
text(230,"Limitations",18,True)
text(199,"All values are illustrative. This dataset does not measure real-world retrieval.")
c.save()
