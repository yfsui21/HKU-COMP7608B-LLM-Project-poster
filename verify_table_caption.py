"""Regression: a caption above a table must not select the preceding table."""
from pathlib import Path
from tempfile import TemporaryDirectory
from reportlab.pdfgen import canvas
from paper_canvas.extract import figure_options

with TemporaryDirectory() as tmp:
    path = Path(tmp) / 'tables.pdf'
    c = canvas.Canvas(str(path), pagesize=(600,800))
    for label, top in [('Table 1: First table.',700),('Table 2: Ablation results.',450)]:
        c.drawString(100,top,label)
        for offset in (12,32,65):
            c.line(100,top-offset,500,top-offset)
        c.drawString(110,top-27,'Method      Score')
        c.drawString(110,top-52,'Full model   58.63')
    c.save()
    found = figure_options(path)
    assert len(found)==2, found
    assert found[0]['box'][1] < .2, found
    assert .43 < found[1]['box'][1] < .47, found
    assert .50 < found[1]['box'][3] < .54, found
    # A caption without nearby rules must not borrow the previous graphic.
    path2=Path(tmp)/'unresolved.pdf'
    c=canvas.Canvas(str(path2),pagesize=(600,800))
    c.line(100,700,500,700);c.line(100,650,500,650)
    c.drawString(100,600,'Table 2: Missing table.')
    c.save()
    assert figure_options(path2)==[]
print('PASS: above-caption table selection and unresolved-table safety')
