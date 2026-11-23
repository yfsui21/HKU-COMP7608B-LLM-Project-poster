"""Exercise an actual PosterForest branch against the source slide and exported geometry."""
import argparse
import tempfile
from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from paper_canvas.native_tree import apply_revision, box, branches, panel_boxes, read, resize, start


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
        # Exercise the shared transaction with the actual slide's text and node IDs.
        titles = [s for s in first.shapes if s.has_text_frame and s.text.strip()]
        heading = titles[0]
        index = next(i for i,p in enumerate(heading.text_frame.paragraphs) if p.text.strip())
        shortened = heading.text.split()[0]
        change = dict(shape=str(heading.shape_id), paragraph=index, text=shortened)
        lock_id = str(titles[1].shape_id)
        plan = dict(texts=[change], splits={branch["id"]:branch["ratio"]}, protect=[lock_id])
        def reject(candidate, _):
            raise ValueError("Test reviewer rejected this candidate")
        try:
            apply_revision(session, plan, soffice, result["artifact"], "test", reject)
        except ValueError as exc:
            assert str(exc) == "Test reviewer rejected this candidate"
            assert read(session/"state.json") == result
        else:
            raise AssertionError("A rejected joint revision was saved")
        joint = apply_revision(session, plan, soffice, result["artifact"], "test joint revision")
        resumed = resize(session, branch["id"], ratio, soffice, joint["artifact"])
        saved = Presentation(session/resumed["artifact"]/"poster.pptx")
        assert next(s for s in saved.slides[0].shapes if s.shape_id==heading.shape_id).text_frame.paragraphs[index].text == shortened
        try:
            apply_revision(session, {"texts":[dict(change,shape=lock_id)]}, soffice, resumed["artifact"], "test conflict")
        except ValueError as exc:
            assert "protected" in str(exc) and read(session/"state.json")==resumed
        else:
            raise AssertionError("Protected text edit was saved")
        print(f"PASS: {len(nodes)} splits, {len(first.shapes)} shapes, {changed} moved or resized, rollback verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--soffice", required=True)
    parser.add_argument("--index", type=int, default=2)
    parser.add_argument("--delta", type=float, default=.01)
    args = parser.parse_args()
    verify(args.work, args.soffice, args.index, args.delta)
