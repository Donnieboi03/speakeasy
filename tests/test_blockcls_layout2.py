"""Phase 6 Amendment 3 units: the gap-page router (R1-R3, a portrait paper page never routed), heron's class mapping and
fallback reasons, and the structural fallback removing text evidence. Stdlib checks run anywhere; the render checks need the doc
venv (pypdfium2, Pillow, numpy):
    ~/.speakeasy/tts-venv/bin/python tests/test_blockcls_layout2.py
Everything runs in a temp SPEAKEASY_HOME, never ~/.speakeasy."""
import os, sys, tempfile
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HOME = tempfile.mkdtemp(prefix="se_layout2_test_")
os.environ["SPEAKEASY_HOME"] = HOME
sys.path.insert(0, os.path.join(REPO, "src"))
import blockcls

def paper_lines(n=48, W=595.3):
    return [[56, 60 + 14 * k, W - 56, 70 + 14 * k, f"Line {k} of running text on the page."] for k in range(n)]

def test_portrait_paper_never_routed():
    W, H = 595.3, 841.9
    assert blockcls.route_page(paper_lines(), W, H, [], []) == []
    fig = [56, 300, 539, 500]                                           # a figure PP-S explains
    assert blockcls.route_page(paper_lines(), W, H, [fig], [[50, 290, 545, 510]]) == []

def test_r1_r2_r3():
    W, H = 595.3, 841.9
    assert blockcls.route_page([], W, H, [], []) == ["R1"]                           # no text layer
    assert blockcls.route_page(paper_lines(2), W, H, [], []) == ["R1"]               # < 3 lines
    tiny = [[56, 60, 80, 66, "a"], [56, 80, 80, 86, "b"], [56, 100, 80, 106, "c"]]
    assert "R1" in blockcls.route_page(tiny, W, H, [], [])                           # lines cover < 2 % of the page
    slide = [[40, 30 + 20 * k, 400, 44 + 20 * k, "Point"] for k in range(10)]
    assert blockcls.route_page(slide, 720, 405, [], []) == ["R2"]                    # landscape, < 25 lines
    assert "R2" not in blockcls.route_page(paper_lines(25, 720), 720, 405, [], [])   # 25 lines: not slide-like
    loose = [[60, 200, 400, 400]]                                                    # 8 % of the page, nothing explains it
    assert blockcls.route_page(paper_lines(), W, H, loose, []) == ["R3"]
    assert blockcls.route_page(paper_lines(), W, H, loose, [[60, 200, 400, 270]]) == ["R3"]   # 35 % inside an explained box: loose
    assert blockcls.route_page(paper_lines(), W, H, loose, [[60, 200, 400, 301]]) == []       # >= 50 % inside: explained
    assert blockcls.route_page(paper_lines(), W, H, [[60, 200, 70, 400]] * 3, []) == []   # sides under 24 pt do not count
    assert abs(blockcls.union_area([[0, 0, 10, 10], [5, 5, 15, 15]]) - 175) < 1e-6

def test_heron_class_mapping():
    assert len(blockcls.HERON_LABELS) == 17 and blockcls.HERON_LABELS[6] == "picture" and blockcls.HERON_LABELS[8] == "table"
    assert blockcls.HERON_FAMILY == {"picture": "figure", "table": "table"}
    assert set(blockcls.HERON_TEXT) == {"text", "list_item", "section_header", "title", "caption", "footnote"}
    assert blockcls.DETECTOR_FAMILY["picture"] == "figure" and blockcls.DETECTOR2_VERSION == "docling-heron/59c81a3a2923"
    assert 0.3 <= blockcls.HERON_SCORE_MIN <= 0.6

def reasons(**env):
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        d = blockcls.Detector2(); d.ready(); return d.reason, d.events, d.status()
    finally:
        for k, v in saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

def test_fallback_reasons():
    r, ev, st = reasons(SPEAKEASY_LAYOUT2="off")
    assert r == "disabled" and ev[0]["event"] == "layout2_unavailable" and st["status"] == "unavailable"
    r, ev, st = reasons(SPEAKEASY_LAYOUT2_MODEL=os.path.join(HOME, "missing.onnx"))
    assert r == "model-missing" and st["detector_version"] is None
    bad = os.path.join(HOME, "bad.onnx")
    with open(bad, "wb") as f: f.write(b"not an onnx file")
    r, *_ = reasons(SPEAKEASY_LAYOUT2_MODEL=bad)
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        assert r == "no-onnxruntime"; return
    assert r == "model-mismatch"
    sz, sha = blockcls.LAYOUT2_SIZE, blockcls.LAYOUT2_SHA256                         # a non-ONNX file with the patched hash: load-failed
    import layoutdet
    blockcls.LAYOUT2_SIZE, blockcls.LAYOUT2_SHA256 = os.path.getsize(bad), layoutdet.file_sha256(bad)
    try:
        r, ev, st = reasons(SPEAKEASY_LAYOUT2_MODEL=bad)
        assert r == "load-failed" and st["detector_version"] == blockcls.DETECTOR2_VERSION and ev[0]["error"], (r, ev)
    finally: blockcls.LAYOUT2_SIZE, blockcls.LAYOUT2_SHA256 = sz, sha
    assert blockcls.Detector2().status()["status"] == "idle"                          # never checked: no cost, no event

class Page:
    """A white page with a black text-like strip and a dark 'figure' block (PIL rendering)."""
    def __init__(self, W=600, H=800):
        from PIL import Image, ImageDraw
        self.img = Image.new("RGB", (W, H), "white"); d = ImageDraw.Draw(self.img)
        d.rectangle([100, 100, 400, 112], fill="black")                              # a text line
        for k in range(0, 200, 10): d.line([120, 300 + k, 480, 300 + k], fill="black", width=2)   # hatched figure 360 x 200
        self.rect = type("R", (), {"width": float(W), "height": float(H)})()
    def image(self, clip, zoom): return self.img

def test_structural_removes_text_evidence():
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("skip test_structural_removes_text_evidence: no numpy / Pillow"); return
    p = Page()
    found = blockcls.structural_candidates(p, 600, 800, [[100, 100, 400, 112]], [])
    assert len(found) == 1 and found[0]["cls"] == "ink" and found[0]["score"] is None and found[0]["anchor"] == "no-text-structural", found
    r = found[0]["bbox"]
    assert r[0] <= 120 and r[2] >= 480 and r[1] <= 300 and r[3] >= 490 and r[1] > 150, r          # the figure, not the text line
    assert blockcls.structural_candidates(p, 600, 800, [[100, 100, 400, 112]], [r]) == []      # an existing candidate covers it
    assert all(isinstance(v, float) for v in r)
    strip = blockcls.structural_candidates(p, 600, 800, [], [])                               # the text line itself is too small
    assert len(strip) == 1, strip

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
