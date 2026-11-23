"""Exercise an actual PosterForest branch against the source slide and exported geometry."""
import argparse
import tempfile
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from paper_canvas.native_tree import box, branches, panel_boxes, read, resize, start


def verify(work, soffice, index, delta):
    with tempfile.TemporaryDirectory() as temp:
        session = Path(temp)/"session"
        state = start(work, session)
        nodes = list(branches(state["tree"]))
        branch = nodes[index]
        original = Presentation(session/"original/poster.pptx")
        expected = {}
        panel_boxes(branch, branch["box"], expected)
        ratio = branch["ratio"] + delta
        result = resize(session, branch["id"], ratio, soffice, "original")
        edited = Presentation(session/result["artifact"]/"poster.pptx")
        first, = original.slides
        second, = edited.slides
        assert len(first.shapes) == len(second.shapes)
        changed = 0
        for a,b in zip(first.shapes,second.shapes):
            assert a.shape_id == b.shape_id and a.name == b.name
            if state["owners"][str(a.shape_id)] not in expected:
                assert box(a) == box(b)
            elif box(a) != box(b):
                changed += 1
            if a.has_text_frame:
                assert a.text == b.text
            if a.shape_type == MSO_SHAPE_TYPE.PICTURE:
                assert a.image.blob == b.image.blob
                assert abs(a.width/a.height - b.width/b.height) < .0001
        assert changed
        for a,b in zip(nodes,branches(result["tree"])):
            assert a["id"] == b["id"]
            assert b["ratio"] == (ratio if a["id"]==branch["id"] else a["ratio"])
        for share, version in [(ratio,"original"), (.000001,result["artifact"])]:
            try:
                resize(session, branch["id"], share, soffice, version)
            except ValueError:
                assert read(session/"state.json") == result
            else:
                raise AssertionError("Invalid or stale edit committed")
        print(f"PASS: {len(nodes)} splits, {len(first.shapes)} shapes, {changed} moved or resized, rollback verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--soffice", required=True)
    parser.add_argument("--index", type=int, default=2)
    parser.add_argument("--delta", type=float, default=.01)
    args = parser.parse_args()
    verify(args.work, args.soffice, args.index, args.delta)
