"""Set-of-Mark picker (phase5-parts.md 4.2-4.5): letter decision from top_logprobs, candidate cap / dedupe, deterministic
marked crops and non-overlapping tags. The render tests need pypdfium2 + Pillow (run under the doc venv)."""
import hashlib, math, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import partpick

FIXTURE = os.path.join(os.path.dirname(__file__), "audit", "bench", "fixtures", "public", "syn-panels-01", "doc.pdf")

def reply(*pairs):
    return {"logprobs": [{"token": pairs[0][0], "logprob": math.log(pairs[0][1]), "top_logprobs": [{"token": t, "logprob": math.log(p)} for t, p in pairs]}]}

def test_letters_merge_and_lowercase_never_counts():
    d = partpick.decide(reply(("A", 0.5), (" A", 0.2), ("a", 0.2), ("B", 0.1)), 3, min_p=0.5)
    assert d["outcome"] == "accepted" and d["chosen_index"] == 0, d
    assert d["letter_mass"] == 0.8 and abs(d["p_top"] - 0.875) <= 0.006 and d["p_margin"] == 0.75, d

def test_letters_outside_the_menu_do_not_count():
    d = partpick.decide(reply(("C", 0.6), ("A", 0.3)), 2, min_p=0.5)               # 2 candidates: only A and B are valid
    assert d["outcome"] == "off-menu" and d["letter_mass"] == 0.3, d

def test_letter_mass_boundary():
    assert partpick.decide(reply(("A", 0.5), ("The", 0.5)), 2, min_p=0.5)["outcome"] == "accepted"
    assert partpick.decide(reply(("A", 0.49), ("The", 0.51)), 2, min_p=0.5)["outcome"] == "off-menu"

def test_threshold_is_inclusive():
    d = partpick.decide(reply(("A", 0.6), ("B", 0.4)), 2, min_p=0.6)
    assert d["p_top"] == 0.6 and d["outcome"] == "accepted", d
    assert partpick.decide(reply(("A", 0.6), ("B", 0.4)), 2, min_p=0.61)["outcome"] == "low-confidence"

def test_no_logprobs():
    for r in ({}, {"logprobs": []}, {"logprobs": [{"token": "A", "logprob": 0.0}]}, {"logprobs": [{"top_logprobs": []}]}):
        d = partpick.decide(r, 2)
        assert d["outcome"] == "no-logprobs" and d["chosen_index"] is None and d["p_top"] is None, (r, d)

def test_candidate_cap_dedupe_and_order():
    scope = [0, 0, 400, 400]
    parts = [{"kind": "text", "panel": None, "attached": True, "rect": [10, 10, 60, 50]},
             {"kind": "text", "panel": None, "attached": True, "rect": [11, 11, 61, 51]},       # IoU >= 0.7 with the first
             {"kind": "text", "panel": None, "attached": False, "rect": [300, 300, 340, 330]},  # padded label: not a candidate
             {"kind": "callout", "panel": None, "attached": False, "rect": [200, 10, 230, 40]}]
    objs = [[x, y, x + 30, y + 30, "drawing", None] for x in range(0, 400, 40) for y in range(100, 260, 40)]   # 40 objects of 900 pt2
    objs.append([0, 0, 400, 300, "drawing", None])                                                          # 75 % of the scope: too big
    objs.append([0, 360, 4, 400, "drawing", None])                                                          # a side < 8 pt
    fig = {"parts": parts, "objects": objs}
    c = partpick.candidates(fig, scope, None)
    assert len(c) == partpick.MAX_CANDIDATES and [10, 10, 60, 50] in c and [11, 11, 61, 51] not in c and [200, 10, 230, 40] not in c, c
    assert [0, 0, 400, 300] not in c and [0, 360, 4, 400] not in c
    assert c[0] == [10, 10, 60, 50]                                   # reading order: the top row first
    assert [200, 10, 230, 40] in partpick.candidates(fig, scope, None, callout=True)

def test_candidates_stay_in_their_panel():
    fig = {"parts": [{"kind": "text", "panel": "a", "attached": True, "rect": [10, 10, 60, 50]},
                     {"kind": "text", "panel": "b", "attached": True, "rect": [210, 10, 260, 50]}],
           "objects": [[220, 60, 280, 120, "drawing", "b"], [20, 60, 80, 120, "drawing", "a"]]}
    assert partpick.candidates(fig, [200, 0, 400, 200], "b") == [[210, 10, 260, 50], [220, 60, 280, 120]]

def test_tags_never_overlap():
    rects = [[0, 0, 300, 300], [20, 20, 120, 120], [40, 40, 60, 60], [150, 150, 170, 170], [152, 150, 172, 170], [280, 0, 300, 20]]
    tags = partpick.place_tags(rects, 24, 28, (320, 320))
    for i, a in enumerate(tags):
        assert 0 <= a[0] and 0 <= a[1] and a[2] <= 320 and a[3] <= 320, a
        for b in tags[i + 1:]:
            assert not partpick._overlaps(a, b), (a, b)
    big = tags[0]                                                     # the big rect's tag sits clear of its smaller candidates
    assert not any(partpick._overlaps(big, r) for r in rects[1:]), big

def test_png_is_byte_identical():
    cands = [[73.2, 183.5, 177.8, 227.1], [188.2, 183.5, 292.8, 227.1], [73.2, 259.5, 177.8, 301.1], [188.2, 259.5, 292.8, 301.1]]
    shas = []
    for _ in range(2):
        out = os.path.join(tempfile.gettempdir(), f"speakeasy_test_pick_{os.getpid()}.png")
        try:
            info = partpick.render(FIXTURE, 1, [66.4, 149.8, 312.2, 335.4], cands, out)
            shas.append(hashlib.sha256(open(out, "rb").read()).hexdigest())
        finally:
            os.remove(out)
    assert shas[0] == shas[1] and info["palette"] == "red" and max(info["px"]) <= 1024, (shas, info)

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
