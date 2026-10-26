"""Panel labels drawn below their panels (phase5-parts.md A3.4), hairline figures (A2.4) and whole-figure sentences (A2.5)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import docmap

FB = [0, 0, 400, 300]

def lab(l, x, y):
    return {"l": l, "r": [x, y, x + 20, y + 10]}

def test_two_by_one_labels_below():
    gfx = [[20, 20, 180, 250], [220, 20, 380, 250]]
    ps = docmap._panels_below(FB, [lab("a", 90, 255), lab("b", 290, 255)], gfx)
    assert [(p["label"], p["bbox"]) for p in ps] == [("a", [0, 0, 200, 265]), ("b", [200, 0, 400, 265])], ps
    assert all(p["src"] == "label" and p["guessed"] is False for p in ps)

def test_two_by_one_labels_above_keeps_todays_rule():
    gfx = [[20, 20, 180, 250], [220, 20, 380, 250]]
    labels = [lab("a", 90, 5), lab("b", 290, 5)]
    assert docmap._panels_below(FB, labels, gfx) == []
    assert [p["label"] for p in docmap._panels(FB, labels)] == ["a", "b"]

def test_two_by_two_labels_below():
    gfx = [[20, 10, 180, 120], [220, 10, 380, 120], [20, 150, 180, 260], [220, 150, 380, 260]]
    labels = [lab("a", 90, 125), lab("b", 290, 125), lab("c", 90, 265), lab("d", 290, 265)]
    ps = {p["label"]: p["bbox"] for p in docmap._panels_below(FB, labels, gfx)}
    assert ps == {"a": [0, 0, 200, 135], "b": [200, 0, 400, 135], "c": [0, 137, 200, 275], "d": [200, 137, 400, 275]}, ps

def test_two_by_two_labels_above():
    gfx = [[20, 20, 180, 120], [220, 20, 380, 120], [20, 150, 180, 260], [220, 150, 380, 260]]
    labels = [lab("a", 90, 5), lab("b", 290, 5), lab("c", 90, 135), lab("d", 290, 135)]
    assert docmap._panels_below(FB, labels, gfx) == []

def test_mixed_labels_use_todays_rule():
    gfx = [[20, 20, 180, 250], [220, 30, 380, 260]]
    assert docmap._panels_below(FB, [lab("a", 90, 255), lab("b", 290, 15)], gfx) == []

def test_hairline_figure_takes_the_detector_box_above_its_caption():
    cap = [100, 400, 300, 412]
    boxes = [{"role": "figure", "bbox": [90, 200, 310, 395]}, {"role": "figure", "bbox": [400, 200, 500, 395]},
             {"role": "paragraph", "bbox": [90, 380, 310, 398]}]
    assert docmap._hairline([100, 399, 300, 400]) and not docmap._hairline([90, 200, 310, 395])
    assert docmap._detector_substitute(boxes, cap) == [90, 200, 310, 395]
    assert docmap._detector_substitute([{"role": "figure", "bbox": [90, 100, 310, 300]}], cap) is None      # 100 pt gap

def test_whole_figure_sentence_names_no_object():
    for s in ("Figure 1 shows the overall setup.", "Fig. 2b shows the overall setup.", "The diagram illustrates the system architecture.",
              "Figure 3 compares the results.", "This figure shows the data.", "This diagram compares the two approaches.",
              "Fig. 1 summarises the results.", "Figure 4 summarized both processes.", "The plots illustrated several workflows."):
        assert not docmap.names_object(s), s
    for s in ("The red valve controls the inflow.", "The settler gives the slurry time", "Figure 2 shows the probe on the left.",
              "Figure 2 shows the sensor drawing."):                  # unlabelled objects count too: the picker may find them
        assert docmap.names_object(s), s

def test_same_label_on_several_graphics():
    # four boxes each labelled "LoRA", told apart only by a second label ("1st", "k-th", "new") inside the box
    boxes = [[20 + 60 * k, 100, 70 + 60 * k, 160] for k in range(3)]
    labels = [[30 + 60 * k, 140, 60 + 60 * k, 148, "LoRA"] for k in range(3)] + [[30, 120, 45, 128, "1st"], [90, 120, 105, 128, "k-th"], [150, 120, 165, 128, "new"]]
    fig = {"bbox": [0, 0, 400, 300], "panels": [], "labels": labels, "objects": [b + ["drawing"] for b in boxes], "caption": "Figure 1: LoRA modules."}
    assert docmap.ground_label("The new LoRA module is trained.", fig)[0] == [140, 100, 190, 160]
    assert docmap.ground_label("The 1st LoRA block reads Polish.", fig)[0] == [20, 100, 70, 160]
    assert docmap.ground_label("The k-th LoRA block starts it.", fig) is None          # "k-th" names nothing it can match

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
