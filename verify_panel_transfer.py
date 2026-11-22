"""Check cross-panel revision on the bundled t-SNE display at a constrained size."""
import argparse
import tempfile
from pathlib import Path

from pptx import Presentation

from paper_canvas.agent import edit, generate, read
from paper_canvas.model import Model


def shapes(path):
    slide, = Presentation(path).slides
    return {shape.name: shape for shape in slide.shapes}


def verify(pdf, session):
    api = Model()
    result = Path(__file__).resolve().parent / "examples/tsne"
    state = generate(pdf, session, [2250, 1940], "joint", api, forest_result=result)
    final = session / state["artifact"]
    report = read(final / "review.json")
    transfer = report["panel_allocation"]
    assert (transfer["donor"], transfer["receiver"]) == ("method", "main")
    assert "main_caption" in transfer["affected_claims"]["main"]
    assert "method" in transfer["affected_claims"]["method"]
    assert transfer["geometry_before"] and not transfer["geometry_after"]
    assert not report["issues"] and all(c["passed"] for c in report["claims"])
    before = shapes(session / "00-initial/poster.pptx")
    after = shapes(final / "poster.pptx")
    assert after["method"].width < before["method"].width
    assert after["main_caption"].width > before["main_caption"].width
    assert after["secondary_caption"].left == before["secondary_caption"].left
    for key in ("figure", "exhibit-method", "exhibit-secondary"):
        assert before[key].image.blob == after[key].image.blob
        assert abs(before[key].width / before[key].height - after[key].width / after[key].height) < .0001
    locked = edit(session, "protect result", api)
    assert locked["history"][-1]["accepted"]
    conflict = edit(session, "shorten protected section to 2 words", api)
    assert conflict["history"][-1]["conflict"] and conflict["artifact"] == locked["artifact"]
    assert conflict["forest"]["split_ratios"] == state["forest"]["split_ratios"]
    print(f"PASS: {transfer['transferred_pt']:.2f} pt from method to main; {len(report['claims'])} claims; conflict rollback.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, help="Keep the generated session in a new directory")
    args = parser.parse_args()
    if args.out:
        verify(args.pdf, args.out)
    else:
        with tempfile.TemporaryDirectory() as temp:
            verify(args.pdf, Path(temp) / "poster")
