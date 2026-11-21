"""Regression checks for both recovery and the joint-vs-fixed allocation comparison."""
import json
import tempfile
from pathlib import Path
from pptx import Presentation
from paper_canvas.agent import generate,edit,width
from paper_canvas.model import Model
from paper_canvas.source import EXAMPLE
from paper_canvas.render import scene

def main():
    api=Model()
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for mode in ("joint","layout-first"):
            dest=root/mode
            original=generate(EXAMPLE,dest,[1008,540],mode,api)
            asset=(dest/"evidence.png").read_bytes()
            updated=edit(dest,"enlarge figure by 35% and protect result",api)
            accepted=updated["history"][-1]["accepted"]
            assert accepted==(mode=="joint"), updated["history"]
            if accepted:
                assert width(updated,asset)>=1.35*width(original,asset)-.1
                assert not scene(updated,asset)[1]
                assert updated["protected"]["result"]==original["sections"][2]["text"]
                shortened=edit(dest,"shorten question to 6 words",api)
                assert shortened["history"][-1]["accepted"] and len(shortened["sections"][0]["text"].split())<=6
                rejected=edit(dest,"shorten protected section to 2 words",api)
                assert not rejected["history"][-1]["accepted"] and rejected["artifact"]==shortened["artifact"]
                unclear=edit(dest,"paint it warmer",api)
                assert not unclear["history"][-1]["accepted"] and unclear["artifact"]==shortened["artifact"]
                assert len(Presentation(dest/shortened["artifact"]/"poster.pptx").slides)==1
            else:
                assert original["artifact"]==updated["artifact"]
        broken=root/"damaged.pdf"
        broken.write_bytes(b"invalid pdf")
        try:
            generate(broken,root/"failure",[1008,540],"joint",api)
        except Exception:
            assert not (root/"failure/state.json").exists()
            assert "generation_failed" in (root/"failure/trace.jsonl").read_text()
        else:
            raise AssertionError("Invalid source accepted")
    print("PASS: generation, allocation, edit history, protection, bounded rollback, invalid PDF.")
if __name__=="__main__":
    main()
