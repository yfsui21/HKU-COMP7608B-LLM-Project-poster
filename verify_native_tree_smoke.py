"""Import and pure-geometry smoke checks for the native PosterForest editor."""
from paper_canvas.native_tree import branches, choices, panel_boxes, project, title


def main():
    # A compact vertical split with two leaves, mirroring native_tree.compact() output.
    tree = {
        "type": "split",
        "id": "internal-0",
        "direction": "vertical",
        "box": [0, 0, 1000, 500],
        "original_ratio": 0.5,
        "ratio": 0.5,
        "children": [
            {"type": "leaf", "panel": "p1", "name": "Method", "box": [0, 0, 500, 500]},
            {"type": "leaf", "panel": "p2", "name": "Result", "box": [500, 0, 500, 500]},
        ],
    }
    assert [n["id"] for n in branches(tree)] == ["internal-0"]
    assert title(tree["children"][0]) == "Method"
    assert {c["id"] for c in choices(tree)} == {"internal-0"}
    out = {}
    panel_boxes(tree, tree["box"], out)
    assert set(out) == {"p1", "p2"}
    assert out["p1"][2] == 500 and out["p2"][2] == 500
    # A ratio change moves the boundary without changing the total extent.
    tree["ratio"] = 0.6
    moved = {}
    panel_boxes(tree, tree["box"], moved)
    assert moved["p1"][2] == 600 and moved["p2"][0] == 600 and moved["p2"][2] == 400
    assert moved["p1"][2] + moved["p2"][2] == 1000
    # project maps a child rectangle back through its parent frame.
    assert project([10, 10, 100, 100], [0, 0, 100, 100], [0, 0, 200, 100]) == [20, 10, 200, 100]
    print("PASS: native tree import and pure geometry.")


if __name__ == "__main__":
    main()
