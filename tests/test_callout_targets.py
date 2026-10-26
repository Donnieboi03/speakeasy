"""Callout target eligibility (phase5-parts.md A4.1): a target never contains the marker (frames, backgrounds, a photo the
number is printed on) and never covers >= 40 % of the marker's panel; with nothing eligible the oval is the padded marker."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import docmap

class FakePath:
    def __init__(self, pts, filled=False, closes=False, kinds=None):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.r = [min(xs), min(ys), max(xs), max(ys)]; self.filled = filled; self.n = len(pts)
        self._pts = [(x, y, "move" if i == 0 else (kinds or {}).get(i, "line"), closes and i == len(pts) - 1) for i, (x, y) in enumerate(pts)]
    def points(self): return self._pts

def box(x0, y0, x1, y1, filled=True):
    return FakePath([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], filled=filled, closes=True)

def circle(cx, cy, r):
    return FakePath([(cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r), (cx + r, cy)], kinds={i: "bezier" for i in range(1, 5)})

FIG = {"bbox": [40, 40, 560, 300], "panels": [{"label": "a", "bbox": [40, 40, 300, 300]}, {"label": "b", "bbox": [300, 40, 560, 300]}], "objects": []}
FRAME = box(305, 45, 555, 295, filled=False)                     # panel b's frame: it holds every marker inside it
MARKER = [400.0, 150.0, 403.0, 155.0]

def parts(markers, paths, images=()):
    return docmap._callout_parts(FIG, markers, paths, list(images))

def test_marker_in_frame_takes_the_nearby_shape():
    shape = box(413, 140, 450, 170)                               # 10 pt to the right of the marker
    p = parts([(MARKER, "3", 3, "pdf")], [FRAME, circle(401.5, 152.5, 6), shape])
    assert p[0]["attached"] and p[0]["rect"] == [413, 140, 450, 170], p

def test_marker_in_frame_alone_is_the_padded_marker():
    p = parts([(MARKER, "3", 3, "pdf")], [FRAME, circle(401.5, 152.5, 6)])
    enc = [395.5, 146.5, 407.5, 158.5]; h = enc[3] - enc[1]
    assert not p[0]["attached"] and p[0]["rect"] == [enc[0] - h, enc[1] - h, enc[2] + h, enc[3] + h], p

def test_number_printed_on_a_photo_is_the_padded_marker():
    photo = [320.0, 60.0, 540.0, 280.0]                           # a raster photo under the circled number
    p = parts([([400.0, 150.0, 406.0, 156.0], "\u2462", 3, "pdf")], [FRAME], [photo])
    assert not p[0]["attached"] and p[0]["rect"] == [394.0, 144.0, 412.0, 162.0], p

def test_leader_into_frame_and_shape_takes_the_shape():
    shape = box(330, 200, 370, 230)
    leader = FakePath([(402.0, 157.0), (350.0, 215.0)])           # ends inside the shape, which is inside the frame
    p = parts([(MARKER, "4", 4, "pdf")], [FRAME, leader, shape])
    assert p[0]["attached"] and p[0]["rect"] == [330, 200, 370, 230], p

def test_marker_partly_over_a_shape_takes_the_shape():
    shape = box(380, 152, 401, 180)                               # overlaps the marker's lower-left corner
    p = parts([(MARKER, "5", 5, "pdf")], [FRAME, circle(401.5, 152.5, 6), shape])
    assert p[0]["attached"] and p[0]["rect"] == [380, 152, 401, 180], p

def test_shape_of_40_percent_of_the_panel_is_never_a_target():
    big = box(320, 160, 540, 290)                                 # 28600 pt2 of panel b's 67600 (42 %), 5 pt below the marker
    p = parts([(MARKER, "6", 6, "pdf")], [FRAME, circle(401.5, 152.5, 6), big])
    assert not p[0]["attached"], p

def test_another_markers_enclosure_is_not_a_target():
    p = parts([(MARKER, "1", 1, "pdf"), ([412.0, 150.0, 415.0, 155.0], "2", 2, "pdf")],
              [FRAME, circle(401.5, 152.5, 6), circle(413.5, 152.5, 6)])
    assert [q["attached"] for q in p] == [False, False], p

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
