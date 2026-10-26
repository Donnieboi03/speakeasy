"""Parse-time part hierarchy (phase5-parts.md 2-3): callout references in speech, callout markers (enclosed, circled, leader)
and their targets, gutter panels, and the deterministic callout lookup."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import docmap

class FakePath:
    """A vector path as pdfpage.Path exposes it: page rect, paint mode, segment count and points."""
    def __init__(self, pts, filled=False, closes=False, kinds=None):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.r = [min(xs), min(ys), max(xs), max(ys)]; self.filled = filled; self.n = len(pts)
        self._pts = [(x, y, "move" if i == 0 else (kinds or {}).get(i, "line"), closes and i == len(pts) - 1) for i, (x, y) in enumerate(pts)]
    def points(self): return self._pts

def box(x0, y0, x1, y1, filled=True):
    return FakePath([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], filled=filled, closes=True)

def circle(cx, cy, r):
    p = FakePath([(cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r), (cx + r, cy)], kinds={i: "bezier" for i in range(1, 5)})
    return p

FIG = {"bbox": [40, 40, 400, 300], "panels": [], "objects": []}

def callouts(markers, paths, images=()):
    return docmap._callout_parts(FIG, markers, paths, list(images))

def test_callout_reference_vectors():
    for s, n in (("Callout 3 is the probe.", 3), ("Here, callout (3) is the probe.", 3), ("The marker three is red.", 3), ("\u2462 is the pump.", 3),
                 ("No. 4 is a valve.", 4), ("Item 12 is the gauge.", 12)):
        assert docmap.callout_ref(s) == n, s
    for s in ("Figure 3 shows the plant.", "In step 3 the water settles.", "The pipe is 3 mm wide.", "Label 5% of the samples.",
              "Fig. 3 again.", "Table 3 lists them.", "In panel 3 nothing.", "Number 2.5 is a ratio."):
        assert docmap.callout_ref(s) is None, s

def test_enclosed_number_is_a_callout_with_the_nearest_target():
    target = box(100, 100, 160, 140)
    parts = callouts([([170.0, 95.0, 173.0, 100.0], "1", 1, "pdf")], [circle(171.5, 97.5, 6), target])
    assert len(parts) == 1 and parts[0]["attached"] and parts[0]["rect"] == [100, 100, 160, 140] and parts[0]["n"] == 1, parts

def test_circled_glyph_is_a_callout():
    parts = callouts([([200.0, 200.0, 206.0, 206.0], "\u2461", 2, "pdf")], [])
    assert len(parts) == 1 and parts[0]["n"] == 2 and not parts[0]["attached"], parts
    assert parts[0]["rect"][0] < 200 and parts[0]["rect"][2] > 206          # the padded marker box

def test_leader_far_end_picks_the_smallest_object_there():
    marker = [300.0, 60.0, 303.0, 65.0]
    leader = FakePath([(302.0, 66.5), (250.0, 120.0)])                # starts at the digit, ends inside the small box
    big, small, near = box(200, 100, 290, 200), box(240, 110, 265, 130), box(305, 70, 340, 100)
    parts = callouts([(marker, "4", 4, "pdf")], [leader, big, small, near])
    assert len(parts) == 1 and parts[0]["attached"] and parts[0]["rect"] == [240, 110, 265, 130], parts

def test_axis_tick_is_not_a_callout():
    axis = FakePath([(60.0, 280.0), (60.0, 60.0)])                    # the y axis runs along the tick numbers
    tick = FakePath([(57.0, 150.0), (60.0, 150.0)])                   # a 3 pt tick mark beside "5"
    parts = callouts([([50.0, 147.0, 54.0, 153.0], "5", 5, "pdf"), ([50.0, 57.0, 56.0, 63.0], "10", 10, "pdf")], [axis, tick, box(70, 100, 120, 280)])
    assert parts == [], parts

def test_digit_drawn_as_its_own_outline_is_not_enclosed():
    glyph = FakePath([(100.0, 100.0), (103.0, 100.0), (103.0, 105.0), (100.0, 105.0), (100.0, 100.0)], filled=True, closes=True)
    assert callouts([([100.2, 99.8, 103.0, 105.0], "2", 2, "pdf")], [glyph, box(110, 100, 160, 140)]) == []

def test_gutter_panels_follow_the_caption_count():
    fb = [0, 0, 400, 200]
    items = [[10, 10, 180, 190], [220, 10, 390, 190]]                 # two blocks with a 40 pt clear band between them
    ps = docmap._gutter_panels(fb, "Figure 2: (a) the rig. (b) the result.", items)
    assert [p["label"] for p in ps] == ["a", "b"] and ps[0]["bbox"][2] == ps[1]["bbox"][0] == 200 and all(p["src"] == "gutter" for p in ps), ps
    assert docmap._gutter_panels(fb, "Figure 2: (a) one (b) two (c) three.", items) == []        # 2 cells, 3 panels named
    assert docmap._gutter_panels(fb, "Figure 2: the rig.", items) == []

def test_callout_lookup_prefers_the_held_panel():
    fig = {"panels": [{"label": "a", "bbox": [0, 0, 100, 100]}, {"label": "b", "bbox": [100, 0, 200, 100]}],
           "parts": [{"kind": "callout", "n": 1, "panel": "a", "attached": True, "rect": [10, 10, 40, 40]},
                     {"kind": "callout", "n": 1, "panel": "b", "attached": True, "rect": [110, 10, 140, 40]},
                     {"kind": "callout", "n": 2, "panel": "b", "attached": False, "rect": [150, 50, 170, 70]}]}
    assert docmap.callout_part(fig, 1, None, "b")["rect"] == [110, 10, 140, 40]
    assert docmap.callout_part(fig, 1, None, None) is None                                         # ambiguous: the picker decides
    assert docmap.callout_part(fig, 1, "a", "b")["rect"] == [10, 10, 40, 40]                       # the named panel wins
    assert docmap.callout_part(fig, 2, None, None)["rect"] == [150, 50, 170, 70]                   # unattached: its padded marker (A4.1)

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
