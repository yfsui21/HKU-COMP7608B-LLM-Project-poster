"""Build the original synthetic fixture used by offline demos."""
from pathlib import Path
from reportlab.pdfgen.canvas import Canvas

c = Canvas(str(Path(__file__).with_name("paired_review.pdf")), pagesize=(612,792), invariant=1)
def line(y, text, size=12, bold=False):
    c.setFont("Helvetica-Bold" if bold else "Helvetica",size)
    c.drawString(50,y,text)

line(735,"Paired review for field observations",25,True)
line(708,"Synthetic methods note for PaperCanvas demonstrations")
line(649,"Research question",16,True)
line(623,"Can a second review reduce labeling errors in field observations?")
line(572,"Method",16,True)
line(546,"Two annotators reviewed 40 synthetic field records independently.")
line(525,"A paired review reconciled disagreements before final scoring.")
line(468,"Results",16,True)
line(442,"On 40 synthetic records, paired review reduced errors from 12 to 5.")
line(422,"Counts measure incorrect labels, with one label per record.")
for y,label,value in [(325,"Single review",12),(267,"Paired review",5)]:
    line(y,label)
    c.setFillColorRGB(.08,.44,.41)
    c.rect(163,y-3,value*23,22,fill=1,stroke=0)
    c.setFillColorRGB(.08,.13,.18)
    c.drawString(170+value*23,y,str(value))
line(211,"Figure 1. Incorrect labels across 40 synthetic field records.")
line(142,"Limitations",16,True)
line(117,"This synthetic example illustrates the workflow.")
c.save()