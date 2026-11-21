"""Regression checks for imported scientific claims, rollback and independent scoring."""
import argparse
import copy
import csv
import shutil
import tempfile
from pathlib import Path

from pptx import Presentation

from paper_canvas.agent import read, write
from paper_canvas.assessment import POSTER, TURN, summarize
from paper_canvas.forest import check


def main(root):
    results=read(root/"results.json")
    assert len(results["cases"])==6 and results["completed"]==6
    for case in results["cases"]:
        dest=root/case["artifact"]
        state=read(dest.parent/"state.json")
        report=read(dest/"review.json")
        rows=check(state,report["elements"],dest/"poster.pptx",semantic=True)
        assert len(rows)==7 and all(r["passed"] for r in rows)
        assert all(r.get("semantic_passed",True) for r in report["claims"])
    joint=[r for r in results["dialogues"] if r["condition"]=="joint"]
    assert [r["accepted"] for r in joint]==[True,True,False]
    assert joint[-1]["conflict"] and joint[-1]["after"]==joint[-1]["before"]
    session=root/"dialogues"/joint[0]["case"]
    before=session/joint[0]["before"]
    after=session/joint[-1]["after"]
    a,b=[{s.name:s for s in Presentation(p/"poster.pptx").slides[0].shapes} for p in (before,after)]
    assert abs(b["figure"].width/a["figure"].width-1.08)<.0001 and b["result"].text==a["result"].text
    assert len(b["question"].text.split())<=9 and b["question"].text!=a["question"].text
    state=read(session/"state.json")
    report=read(after/"review.json")
    with tempfile.TemporaryDirectory() as tmp:
        tmp=Path(tmp)
        altered=Presentation(after/"poster.pptx")
        result=next(s for s in altered.slides[0].shapes if s.name=="result")
        result.text=result.text.replace("5.75%","15.75%")
        altered.save(tmp/"wrong.pptx")
        verdict=next(r for r in check(state,report["elements"],tmp/"wrong.pptx",semantic=True) if r["claim"]=="result")
        assert not verdict["passed"] and "15.75%" in verdict["unsupported_numbers"] and "5.75%" in verdict["missing"]
        swapped=copy.deepcopy(state)
        name=swapped["forest"]["claims"]["main_caption"]["assets"][0]
        swapped["forest"]["assets"][name]["element"]="exhibit-method"
        assert not next(r for r in check(swapped,report["elements"],after/"poster.pptx",semantic=True) if r["claim"]=="main_caption")["passed"]
        # Fabricated scores exist only in this temporary test of the aggregation formulas.
        (tmp/"blinded").mkdir()
        for name in ("results.json","blinding-key.json","dialogue-key.json"):
            shutil.copy2(root/name,tmp/name)
        bk=read(tmp/"blinding-key.json")
        for prefix,fields,key in (("rater",POSTER,bk),("dialogue-rater",TURN,read(tmp/"dialogue-key.json"))):
            for judge in (1,2):
                with (tmp/"blinded"/f"{prefix}-{judge}.csv").open("w",newline="") as stream:
                    w=csv.DictWriter(stream,fieldnames=["poster",*fields])
                    w.writeheader()
                    for ident,row in key.items():
                        value={k:0 for k in fields}
                        if fields==POSTER:
                            value.update(checklist_total=4,checklist_met=4 if row["condition"]=="joint" else 2,
                                         label_legibility_1_5=judge+2,reading_order_1_5=4)
                        w.writerow(dict(poster=ident,**value))
        summary=summarize(tmp)
        assert summary["poster_ratings"]["jointly_scored"]==6 and len(summary["paired_differences"])==4
        assert all(p["joint_minus_baseline"]["coverage"]==.5 for p in summary["paired_differences"])
        assert summary["poster_ratings"]["agreement"]["label_legibility_1_5"]["mean_absolute_difference"]==1
        assert summary["dialogue_ratings"]["jointly_scored"]==6
    print("PASS: six exports, seven claims each, protected dialogue, numeric and image substitutions, paired scoring.")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run",type=Path)
    main(parser.parse_args().run)
