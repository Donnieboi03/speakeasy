"""Phase 6 block classifier units (docs/contracts/phase6-block-classifier.md 14.7): routing, stage C letters, the caption
lexicon, the in-box match, candidate filters and caps, crops, U-labels, fig_ids, cue style, sidecar keys, the OCR schema and the
sidecar merge. Stdlib checks run anywhere; the render checks need the doc venv (pypdfium2 + Pillow):
    ~/.speakeasy/tts-venv/bin/python tests/test_blockcls.py
Everything runs in a temp SPEAKEASY_HOME, never ~/.speakeasy."""
import hashlib, json, math, os, re, sys, tempfile
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(REPO, "src")
HOME = tempfile.mkdtemp(prefix="se_blockcls_test_")
os.environ["SPEAKEASY_HOME"] = HOME
sys.path.insert(0, SRC)
import blockcls, docmap
import speakeasy_state as state

def reply(probs):
    """An /api/chat reply whose top_logprobs hold these token probabilities."""
    return {"logprobs": [{"top_logprobs": [{"token": t, "logprob": math.log(p)} for t, p in probs.items()]}]}

# ---------------- routing B1-B5 ----------------
def test_routing_table():
    T = blockcls.T_B
    assert blockcls.route("figure", "logo", 0.99) == ("rejected", None)                        # B1 decoration
    assert blockcls.route("figure", "music", 0.99) == ("pending", "textish")                   # B2
    assert blockcls.route("figure", "table", 0.99) == ("pending", "disagree")                  # B3
    assert blockcls.route("table", "table", T - 0.01) == ("pending", "low-p")                  # B4
    assert blockcls.route("table", "table", T) == ("accepted", None)                           # B5: p_top == T accepts
    assert blockcls.route("figure", "bar_chart", 0.99) == ("accepted", None)
    assert blockcls.route("figure", "logo", 0.99, available=False) == ("pending", "low-p")     # classifier unavailable
    assert set(blockcls.FAMILY) == set(blockcls.LABELS) and len(blockcls.LABELS) == 26

# ---------------- stage C letters ----------------
def test_letter_merge_and_figure_sum():
    d = blockcls.kind_decide(reply({"A": 0.40, " A": 0.20, "B": 0.38, "C": 0.02}))
    assert d["kind"] == "figure" and d["p_top"] == 0.98 and d["outcome"] == "accepted", d        # p(figure) = p_A + p_B, "A" and " A" merged
    d = blockcls.kind_decide(reply({"a": 0.3, "C": 0.7}))
    assert d["kind"] == "table" and d["p_top"] == 1.0, d                                          # lowercase letters are not letters

def test_letter_mass_boundary():
    assert blockcls.kind_decide(reply({"C": 0.49, "Z": 0.51}))["outcome"] == "off-menu"
    d = blockcls.kind_decide(reply({"C": 0.5, "Z": 0.5}))
    assert d["outcome"] == "accepted" and d["kind"] == "table" and d["letter_mass"] == 0.5, d
    assert blockcls.kind_decide({"message": {"content": "C"}})["outcome"] == "no-logprobs"

def test_stage_c_threshold_and_text():
    T = blockcls.T_C
    assert blockcls.kind_decide(reply({"C": T, "D": 1 - T}))["outcome"] == "accepted"            # p_top == T accepts
    assert blockcls.kind_decide(reply({"C": T - 0.02, "D": 1.02 - T}))["outcome"] == "low-confidence"
    assert blockcls.kind_decide(reply({"D": 0.99, "A": 0.01}))["outcome"] == "text"
    assert blockcls.kind_decide(reply({"E": 0.99, "A": 0.01}))["outcome"] == "low-confidence"   # none: no block

# ---------------- lexicon ----------------
def test_lexicon_positives():
    want = {"Exhibit 3: Revenue": ("ambiguous", "Exhibit 3"), "Abb. 2: Aufbau": ("figure", "Abbildung 2"), "Tabelle 1. Werte": ("table", "Tabelle 1"),
            "TABLE IV": ("table", "TABLE IV"), "Fig. S2. Drift": ("figure", "Figure S2"), "Figura 3 - Mapa": ("figure", "Figura 3"),
            "Chart 1: Sales": ("figure", "Chart 1"), "Figure 3.2: Detail": ("figure", "Figure 3.2"), "Tabla 3 | Datos": ("table", "Tabla 3"),
            "Tableau 2 : Résultats": ("table", "Tableau 2"), "Gráfico 4. Ventas": ("figure", "Gráfico 4"), "Table A.1: Appendix": ("table", "Table A.1")}
    for text, (kind, printed) in want.items():
        m = docmap.lexicon_match(text)
        assert m and m["kind"] == kind and m["printed_label"] == printed, (text, m)
    info = docmap.caption_info("TABLE IV", "table")
    assert info["label"] == "Table IV" and info["aliases"] == ["TABLE IV", "Table IV"] and info["src"] == "lexicon", info
    assert docmap.caption_info("Fig. 3. Response")["src"] == "regex" and docmap.caption_info("Fig. 3. Response")["label"] == "Figure 3"

def test_lexicon_negatives():
    for text in ("Table 2 shows the totals", "Chart 1 of the plan", "TABLE OF CONTENTS", "Figure skating", "Exhibit A", "Exhibit A.",
                 "Tables 2: x", "table iv", "Map of the city"):
        assert docmap.lexicon_match(text) is None, text

def test_roman_numerals():
    roman = "I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX XXI XXII XXIII XXIV XXV XXVI XXVII XXVIII XXIX XXX " \
            "XXXI XXXII XXXIII XXXIV XXXV XXXVI XXXVII XXXVIII XXXIX XL".split()
    for k, r in enumerate(roman, 1):
        m = docmap.lexicon_match(f"TABLE {r}. Results")
        assert m and m["n"] == k, (r, m)
        assert docmap.lexicon_match(f"Fig. {r}") and docmap.lexicon_match(f"fig {r}") is None, r     # only after a capitalised noun
    for bad in ("XLI", "IIII", "VX", "IL"):
        assert docmap.lexicon_match(f"TABLE {bad}") is None, bad

class FakeLay:
    def __init__(self, boxes, weak=()): self._boxes, self._weak = boxes, list(weak)
    def caption_boxes(self, weak=False): return self._boxes + (self._weak if weak else [])

class FakePage:
    """Characters 5 pt wide from x0 on one line (y 272-279)."""
    def __init__(self, text, x0): self.chars = [(c, [x0 + 5 * k, 272.0, x0 + 5 * k + 4, 279.0]) for k, c in enumerate(text)]
    def chars_in(self, r): return [(c, b) for c, b in self.chars if r[0] <= (b[0] + b[2]) / 2 <= r[2] and r[1] <= (b[1] + b[3]) / 2 <= r[3]]

def test_in_box_polluted_line():
    line = [49.1, 271.9, 562.9, 279.1, "ygy Fig. 3. A sample question"]
    assert docmap.caption_info(line[4]) is None
    page = FakePage(line[4], 50.0)                                                      # "F" starts at x 70
    hit = docmap._in_box_caption(page, line, FakeLay([[66.0, 270.0, 562.0, 290.0]]))    # the box starts after the junk
    assert hit and hit[0]["label"] == "Figure 3" and hit[0]["src"] == "lexicon+in-box" and hit[1][0] == [70.0, 272.0, 194.0, 279.0], hit
    hit = docmap._in_box_caption(page, line, FakeLay([[40.0, 270.0, 562.0, 290.0]]))    # junk inside the box: leading tokens stripped
    assert hit and hit[0]["label"] == "Figure 3" and hit[1][1] == "Fig. 3. A sample question" and hit[1][0][0] == 70.0, hit
    many = FakePage("a b c d Fig. 3. Text", 50.0)                                       # more than IN_BOX_JUNK_TOKENS junk tokens: no match
    assert docmap._in_box_caption(many, [49, 271.9, 562.9, 279.1, "a b c d Fig. 3. Text"], FakeLay([[40.0, 270.0, 562.0, 290.0]])) is None
    assert docmap._in_box_caption(page, line, FakeLay([[0, 400, 100, 420]])) is None
    hit = docmap._in_box_caption(page, line, FakeLay([], weak=[[40.0, 270.0, 120.0, 290.0], [40.0, 268.0, 570.0, 292.0]]))   # sub-threshold caption boxes
    assert hit and hit[1][0] == [70.0, 272.0, 194.0, 279.0], hit                          # the box holding most of the line wins

def test_lexicon_hidden_style_nouns():
    for text, kind, label in (("Anlage 1: Lagerbestand.", "ambiguous", "Anlage 1"), ("ANLAGE 2: Lieferungen.", "ambiguous", "ANLAGE 2"),
                              ("Table A.1. Stock.", "table", "Table A.1"), ("TABLE A.2: Stock.", "table", "TABLE A.2"), ("Table S1: Extra.", "table", "Table S1"),
                              ("TABLE IV", "table", "TABLE IV"), ("Exhibit 4 - Staffing.", "ambiguous", "Exhibit 4"), ("Exhibit 5", "ambiguous", "Exhibit 5")):
        m = docmap.lexicon_match(text)
        assert m and m["kind"] == kind and m["printed_label"] == label, (text, m)
    assert docmap.caption_info("Anlage 1: x", "table")["label"] == "Table 1"

class FakeBoxLay:
    """adjacent_body over raw boxes (layoutdet.PageLayout's method on a minimal object)."""
    def __init__(self, boxes):
        import layoutdet
        self.lay = layoutdet.PageLayout.__new__(layoutdet.PageLayout)
        self.lay.boxes, self.lay.lines, self.lay.W, self.lay.H, self.lay.columns, self.lay.unpaired = boxes, [], 600.0, 800.0, 1, set()

def test_adjacent_body_overlapping_caption():
    box = {"cls": "table", "role": "table", "score": 0.5, "bbox": [55, 150, 290, 230]}
    lay = FakeBoxLay([box]).lay
    assert lay.adjacent_body([60, 156, 180, 164], 8)[0] is box                          # a box over the caption pairs (as in bodies())

def test_split_shared_box():
    cap_l, cap_r = [60, 150, 180, 158], [320, 150, 445, 158]
    lines = [cap_l + ["Exhibit 4: Staffing."], cap_r + ["Table A.1: Fuel."], [60, 170, 100, 178, "North"], [200, 170, 240, 178, "12"],
             [324, 170, 360, 178, "R1"], [480, 170, 520, 178, "310"], [60, 190, 100, 198, "Quay"], [324, 190, 360, 198, "R2"]]
    info = lambda src: {"src": src}
    found = [([0], info("lexicon"), "table"), ([1], info("lexicon"), "table")]
    lrect = lambda i: lines[i][:4]
    out = docmap._split_shared(found, [[55, 166, 540, 222], None], lrect, lines, [])
    assert out[0][2] < 320 and out[1][0] > 240 and out[1][2] == 540 and out[0][0] == 55, out
    std = [([0], info("regex"), "table"), ([1], info("lexicon"), "table")]
    assert docmap._split_shared(std, [[55, 166, 540, 222], None], lrect, lines, [])[0] == [55, 166, 540, 222]    # standard bodies unchanged

# ---------------- candidates ----------------
def test_candidate_filters():
    W, H = 600.0, 800.0
    b = lambda r, s=0.5, c="image": {"cls": c, "score": s, "bbox": r}
    bodies = [b([100, 100, 300, 300]),                         # kept
              b([100, 400, 120, 600]),                         # 20 pt wide: too small
              b([10, 10, 60, 50]),                             # 2000 pt^2 < 1 % of the page
              b([0, 0, 590, 790]),                             # >= 90 % of the page
              b([320, 100, 560, 300]),                         # covers a captioned body
              b([100, 620, 400, 760]),                         # covers a paragraph block
              b([105, 105, 300, 300], 0.4, "chart")]           # the same object again (IoU >= 0.5)
    out, dropped = blockcls.filter_candidates(bodies, [[330, 110, 550, 290]], [[100, 640, 400, 700]], W, H)
    assert [c["bbox"] for c in out] == [[100, 100, 300, 300]] and [(d[0]["bbox"], d[1]) for d in dropped] == [([105, 105, 300, 300], "duplicate")], (out, dropped)
    tie = [b([110, 100, 300, 300], 0.5), b([100, 100, 300, 300], 0.5, "chart")]       # equal scores: the earlier in reading order stays
    assert [c["bbox"] for c in blockcls.filter_candidates(tie, [], [], W, H)[0]] == [[100, 100, 300, 300]]
    group = [b([50, 50, 550, 300], 0.9), b([60, 60, 290, 290]), b([310, 60, 540, 290])]
    out, dropped = blockcls.filter_candidates(group, [], [], W, H)
    assert [c["bbox"] for c in out] == [[60, 60, 290, 290], [310, 60, 540, 290]] and [d[1] for d in dropped] == ["container"], (out, dropped)
    loose = [b([50, 50, 550, 300], 0.9), b([60, 60, 290, 290]), b([300, 60, 580, 290])]   # the second inner box is < 90 % inside
    assert len(blockcls.filter_candidates(loose, [], [], W, H)[0]) == 3

def test_skips_logged():
    s = StubSession(); s.vlm = "off"
    c = lambda r: {"cls": "image", "score": 0.5, "bbox": r}
    acc, kinds = s.classify_candidates(1, None, [c([0, 0, 50, 50])], 600, 800, "uncaptioned", [(c([0, 0, 60, 60]), "duplicate"), (c([0, 0, 90, 90]), "container")])
    assert kinds["skipped"] == {"duplicate": 1, "container": 1} and [e.get("skip") for e in s.events] == ["duplicate", "container", None], s.events
    class Routed(blockcls.Session):
        def stage_b(self, page, rect, W, H): return ("other", 0.9, 0.5, 5)
    s = Routed(); s.vlm = "local"
    acc, kinds = s.classify_candidates(1, None, [c([0, 0, 50, 50])], 600, 800, "area")
    assert not acc and kinds["skipped"] == {"area-no-vlm": 1} and s.events[0]["outcome"] == "skipped" and s.events[0]["skip"] == "area-no-vlm"

class StubSession(blockcls.Session):
    def stage_b(self, page, rect, W, H): return ("bar_chart", 0.99, 0.98, 10)

def test_candidate_caps():
    s = StubSession(); s.vlm = "off"
    cands = [{"cls": "image", "score": 0.5, "bbox": [0, k * 10, 50, k * 10 + 9]} for k in range(10)]
    acc, kinds = s.classify_candidates(1, None, cands, 600, 800, "uncaptioned")
    assert len(acc) == blockcls.PAGE_CAP and kinds["skipped"] == {"over-cap": 2} and kinds["candidates"] == 8, kinds
    s.n_candidates = blockcls.DOC_CAP - 1
    acc, kinds = s.classify_candidates(2, None, cands[:3], 600, 800, "uncaptioned")
    assert len(acc) == 1 and kinds["skipped"] == {"over-cap": 2}, kinds
    ev = s.events[0]
    assert set(ev) >= {"event", "page", "rect", "anchor", "det_cls", "det_score", "cls_label", "cls_p", "route", "stage", "kind", "p_top",
                       "p_margin", "outcome", "ms", "model", "provider", "confidence"}
    assert not any(isinstance(v, str) and len(v) > 40 for e in s.events for v in e.values())   # enums only, no text

def test_stage_c_default_off():
    for k in ("SPEAKEASY_BLOCKCLS_VLM", "SPEAKEASY_GROUND"): os.environ.pop(k, None)
    assert blockcls.VLM_DEFAULT == "off" and blockcls.vlm_mode() == "off"
    os.environ["SPEAKEASY_BLOCKCLS_VLM"] = "local"
    try:
        assert blockcls.vlm_mode() == "local"
        os.environ["SPEAKEASY_GROUND"] = "off"; assert blockcls.vlm_mode() == "off"
    finally: os.environ.pop("SPEAKEASY_BLOCKCLS_VLM"); os.environ.pop("SPEAKEASY_GROUND", None)

def test_ocr_helper_lookup():
    exe = os.path.join(HOME, "ocr-exe"); trk = os.path.join(HOME, "trk")
    for p in (exe, trk):
        with open(p, "w") as f: f.write("#!/bin/sh\nexit 0\n")
        os.chmod(p, 0o755)
    saved = {k: os.environ.pop(k, None) for k in ("SPEAKEASY_OCR_BIN", "SPEAKEASY_TRACKER")}
    try:
        assert blockcls.ocr_helper() is None                                       # $SPEAKEASY_HOME/bin/speakeasy-track absent
        os.environ["SPEAKEASY_TRACKER"] = trk; assert blockcls.ocr_helper() == trk
        os.environ["SPEAKEASY_OCR_BIN"] = exe; assert blockcls.ocr_helper() == exe
        os.environ["SPEAKEASY_OCR_BIN"] = os.path.join(HOME, "missing"); assert blockcls.ocr_helper() is None     # no fall-through
        os.environ["SPEAKEASY_OCR_BIN"] = ""; assert blockcls.ocr_helper() is None
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None: os.environ[k] = v

def test_disabled_record_and_routed():
    os.environ["SPEAKEASY_BLOCKCLS"] = "off"
    try:
        s = blockcls.Session()
        assert not s.on and s.top() == {"status": "unavailable", "classifier_version": None, "reason": "disabled", "T_B": blockcls.T_B,
                                        "T_C": blockcls.T_C, "kind_job": "none", "routed": 0}
    finally: del os.environ["SPEAKEASY_BLOCKCLS"]
    os.environ["SPEAKEASY_FIGCLS_MODEL"] = os.path.join(HOME, "missing.onnx"); os.environ["SPEAKEASY_GROUND"] = "local"
    try:
        s = blockcls.Session(); s.vlm = "local"
        acc, kinds = s.classify_candidates(1, None, [{"cls": "table", "score": 0.6, "bbox": [0, 0, 200, 200]}], 600, 800, "uncaptioned")
        top = s.top()
        assert not acc and kinds["routed"] == 1 and s.events[0]["route"] == "low-p" and s.events[0]["outcome"] == "pending"
        assert top["status"] == "unavailable" and top["reason"] == "model-missing" and top["kind_job"] == "queued" and top["routed"] == 1, top
    finally: del os.environ["SPEAKEASY_FIGCLS_MODEL"]; del os.environ["SPEAKEASY_GROUND"]

# ---------------- sidecar ----------------
def test_sidecar_key():
    k = blockcls.entry_key("ab" * 32, 3, [1.04, 2, 3, 4], "qwen3-vl:4b-instruct", "sha256:1", "p" * 64)
    assert k == blockcls.entry_key("ab" * 32, 3, [1.0, 2, 3, 4], "qwen3-vl:4b-instruct", "sha256:1", "p" * 64)     # rect at 0.1 pt
    assert k != blockcls.entry_key("ab" * 32, 3, [1.0, 2, 3, 4], "qwen3-vl:4b-instruct", "sha256:2", "p" * 64)     # model digest
    assert k != blockcls.entry_key("ab" * 32, 3, [1.0, 2, 3, 4], "qwen3-vl:4b-instruct", "sha256:1", "q" * 64)     # prompt hash
    from prompts import KIND_PROMPT
    assert blockcls.prompt_sha() == hashlib.sha256(KIND_PROMPT.encode()).hexdigest()
    assert all(f"{l}:" in KIND_PROMPT for l in "ABCDE") and "letter only" in KIND_PROMPT and "{" not in KIND_PROMPT

def test_job_start_rules():
    cache = os.path.join(HOME, "abc.json")
    queued = {"blockcls": {"kind_job": "queued"}}
    assert blockcls.job_should_start(cache, queued) and not blockcls.job_should_start(cache, {"blockcls": {"kind_job": "none"}})
    with open(blockcls.lock_path(cache), "w") as f: f.write(str(os.getpid()))           # a live lock: nothing starts
    assert not blockcls.job_should_start(cache, queued)
    with open(blockcls.lock_path(cache), "w") as f: f.write("999999")                   # a dead lock: the job starts
    assert blockcls.job_should_start(cache, queued)
    os.remove(blockcls.lock_path(cache))
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump({"state": "failed", "breaker": "unavailable", "done_ts": 0}, f)
    assert blockcls.job_should_start(cache, queued)                                      # failed + unavailable, 10 min later
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump({"state": "partial", "breaker": None, "done_ts": 0}, f)
    on3 = dict(queued, blockcls_events=[{"stage": "classifier", "outcome": "pending", "page": 3}])
    assert blockcls.job_should_start(cache, on3)                                         # covers no page yet
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump({"state": "done", "pages": [1, 3], "done_ts": 0}, f)
    assert not blockcls.job_should_start(cache, on3)
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump({"state": "done", "pages": [1], "done_ts": 0}, f)
    assert blockcls.job_should_start(cache, on3)                                         # page 3 parsed later: not covered
    os.remove(blockcls.sidecar_path(cache))

def test_two_step_parse_and_union():
    """A2.5: a job covers only the pages no earlier job covered, keeps the earlier entries and writes one union sidecar."""
    try:
        import pdfpage  # noqa: F401
    except ImportError:
        print("skip test_two_step_parse_and_union: no pypdfium2"); return
    s = blockcls.Session(); s.on, s.vlm = True, "local"
    s.events = [{"page": 3, "outcome": "pending", "route": "low-p", "stage": "classifier"}]
    assert s.top([1, 2])["kind_job"] == "queued" and s.top([1, 2, 3])["kind_job"] == "none"
    pdf = os.path.join(REPO, "tests", "audit", "bench", "fixtures", "public-gap", "gap-slides-01", "doc.pdf")
    cache = os.path.join(HOME, "two.json")
    ev = lambda p, r: {"event": "block_kind", "page": p, "rect": r, "anchor": "uncaptioned", "det_cls": "image", "det_score": 0.5, "cls_label": "other",
                       "cls_p": 0.4, "route": "textish", "stage": "classifier", "outcome": "pending"}
    with open(cache, "w") as f: json.dump({"blockcls": {"kind_job": "queued"}, "blockcls_events": [ev(1, [60, 80, 330, 320]), ev(3, [370, 250, 690, 337])]}, f)
    old = {"version": 1, "state": "done", "pages": [1], "entries": [{"page": 1, "rect": [60, 80, 330, 320], "outcome": "accepted", "kind": "figure"}],
           "done_ts": 1.0}
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump(old, f)
    sent = []
    def transport(url, body, headers, timeout):
        sent.append(url)
        return reply({"D": 0.99, "A": 0.01})
    job = blockcls.Job(pdf, cache); job.mode = "local"; job.transport = transport
    job.run(); job.finish()
    side = blockcls.read_sidecar(cache)
    assert len(sent) == 1 and side["pages"] == [1, 3] and side["state"] == "done", side
    assert [(e["page"], e["outcome"]) for e in side["entries"]] == [(1, "accepted"), (3, "text")] and side["done_ts"] > 1.0
    done = [json.loads(l) for l in open(os.path.join(HOME, "logs", "blockcls.jsonl")) if "kind_job_done" in l]
    assert done[-1]["routed"] == 1 and done[-1]["state"] == "done", done
    os.remove(blockcls.sidecar_path(cache))

def test_kind_by_layout_without_verdict():
    cap = [56, 100, 200, 108]
    cells = [[60, 120, 90, 127, "Ost"], [150, 120, 170, 127, "61"], [60, 134, 95, 141, "Mitte"], [150, 134, 170, 141, "47"]]
    prose = [[56, 120, 290, 128, "The halls are counted every month by the hall leads."]]
    assert docmap._kind_by_layout(cap, 8, [], cells, 595) == "table"                     # cell rows below
    above = [[l[0], l[1] - 60, l[2], l[3] - 60, l[4]] for l in cells]
    assert docmap._kind_by_layout([56, 90, 200, 98], 8, [], above, 595) == "table"      # under its table
    assert docmap._kind_by_layout(cap, 8, [[60, 40, 200, 96]], prose, 595) == "figure"  # graphics above, prose below
    assert docmap._kind_by_layout(cap, 8, [[60, 112, 200, 180]], prose, 595) == "table"  # graphics below only
    assert docmap._kind_by_layout(cap, 8, [], prose, 595) == "figure"
    assert len(docmap._cell_rows(cells, cap, 8, 595, False)) == 2

def test_body_family_unavailable_is_recorded():
    os.environ["SPEAKEASY_FIGCLS_MODEL"] = os.path.join(HOME, "missing.onnx")
    try:
        s = blockcls.Session()
        assert s.body_family(1, None, [0, 0, 100, 100], 600, 800, "table", 0.6) is None
        assert s.events[-1]["outcome"] == "unavailable" and s.events[-1]["route"] == "low-p" and s.top()["kind_job"] == "none"
    finally: del os.environ["SPEAKEASY_FIGCLS_MODEL"]

def test_header_tie_ocr_rows():
    hdr = {"rect": [0, 0, 100, 10], "toks": {"halle", "kisten", "paletten"}, "n": 1}
    ost = {"rect": [0, 12, 100, 22], "toks": {"ost", "61", "18"}, "n": 1}
    mitte = {"rect": [0, 24, 100, 34], "toks": {"mitte", "47", "13"}, "n": 1}
    rows = [hdr, ost, mitte]
    s = "Halle Ost has 61 Kisten and 18 Paletten."
    assert docmap.match_row(rows, s) == 0                                                 # Phase 2 rule (PDF-text tables): unchanged
    assert docmap.match_row_ocr(rows, s) == 1                                             # OCR rows: a tie goes to the data row
    assert docmap.match_row_ocr(rows, "Columns: Halle, Kisten, Paletten.") == 0           # strictly higher: the header stays
    assert docmap.match_row_ocr(rows, "Halle Mitte has 47 Kisten and 13 Paletten.", 2) == 2  # forward-only start unchanged
    assert docmap.match_row_ocr(rows, "Nothing here.") is None

def test_merge_sidecar():
    cache = os.path.join(HOME, "merge.json")
    page = {"n": 1, "w": 600.0, "h": 800.0, "columns": 1, "figs_done": True, "figures": [],
            "lines": [[100, 500, 300, 510, "Running text one"], [100, 512, 300, 522, "Running text two"]], "blocks": []}
    from llm_client import vision_model
    side = {"version": 1, "state": "done", "entries": [
        {"page": 1, "rect": [100, 100, 300, 300], "outcome": "accepted", "kind": "table", "p_top": 0.99, "p_margin": 0.98, "model": vision_model(),
         "prompt_sha": blockcls.prompt_sha(), "provider": "local", "confidence": "logprob"},
        {"page": 1, "rect": [90, 490, 310, 530], "outcome": "text", "kind": "text", "p_top": 0.99, "p_margin": 0.98, "model": vision_model(),
         "prompt_sha": blockcls.prompt_sha(), "provider": "local", "confidence": "logprob"},
        {"page": 1, "rect": [400, 100, 500, 300], "outcome": "accepted", "kind": "figure", "p_top": 0.99, "p_margin": 0.98, "model": vision_model(),
         "prompt_sha": "0" * 64, "provider": "local", "confidence": "logprob"}]}                 # another prompt: ignored
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump(side, f)
    doc = docmap.merge_kinds({"kind": "pdf", "pages": [json.loads(json.dumps(page))]}, cache)
    pg = doc["pages"][0]
    assert [f["label"] for f in pg["figures"]] == ["Table U1-1"] and pg["figures"][0]["kind_src"] == "vlm" and pg["figures"][0]["captioned"] is False
    assert [b["kind"] for b in pg["blocks"]] == ["table", "paragraph"] and pg["blocks"][1]["lines"] == [0, 1] and pg["blocks"][1]["src"] == "lines"
    assert len(doc["blockcls_events"]) == 3 and all(e["stage"] == "vlm" for e in doc["blockcls_events"])
    side["state"] = "failed"
    with open(blockcls.sidecar_path(cache), "w") as f: json.dump(side, f)
    assert docmap.merge_kinds({"kind": "pdf", "pages": [json.loads(json.dumps(page))]}, cache)["pages"][0]["figures"] == []
    os.remove(blockcls.sidecar_path(cache))

# ---------------- U-labels, fig_ids, cues ----------------
def figs_page():
    cap = {"label": "Figure 1", "kind": "figure", "bbox": [50, 100, 300, 300], "caption_bbox": [50, 305, 300, 315], "captioned": True,
           "aliases": ["Figure 1"], "printed_label": "Figure 1", "caption": "Figure 1: The pump.", "panels": []}
    lex = {"label": "Table 3", "kind": "table", "bbox": [50, 420, 300, 500], "caption_bbox": [50, 405, 300, 415], "captioned": True,
           "aliases": ["Tabla 3", "Table 3"], "printed_label": "Tabla 3", "caption_src": "lexicon", "caption": "Tabla 3: Datos.", "panels": []}
    u1 = docmap._added_fig("figure", "Figure U2-1", 2, [320, 100, 560, 300], "classifier", [])
    u2 = docmap._added_fig("table", "Table U2-1", 2, [320, 420, 560, 500], "classifier", [])
    blocks = [{"kind": "figure", "bbox": cap["bbox"]}, {"kind": "figure", "bbox": u1["bbox"]}, {"kind": "table", "bbox": u2["bbox"]},
              {"kind": "table", "bbox": lex["bbox"]}]
    return {"n": 2, "figures": docmap._reading_sort([lex, u2, cap, u1], blocks)}

def test_u_labels_never_referenced():
    page = figs_page(); figs = page["figures"]
    ctx = docmap.figure_context(figs)
    assert not any("U2" in c for c in ctx) and any(c.startswith("Tabla 3") for c in ctx), ctx
    for s in ("Figure U2-1 shows the trend.", "Table U2 lists it.", "See figure U2-1."):
        assert docmap.find_ref(s, figs)[0] is None, s
    assert docmap.find_ref("Tabla 3 lists the values.", figs)[0]["label"] == "Table 3"
    assert docmap.find_ref("In Figure 1b the pump runs.", figs) == (figs[0], "b")

def test_fig_ids_reading_order():
    import focus
    page = figs_page()
    ids = [focus._fig_id(page, f) for f in page["figures"]]
    assert [f["label"] for f in page["figures"]] == ["Figure 1", "Figure U2-1", "Table U2-1", "Table 3"]
    assert ids == ["p2:fig1", "p2:fig2", "p2:tab1", "p2:tab2"], ids

class FakeArea:
    def __init__(self):
        self.result = {"area_kinds": {"figure": [[40, 60, 440, 460]], "table": [[500, 60, 900, 300]], "candidates": 2, "outcomes": {}, "ms": 1}}
        self.origin, self.scale = (100.0, 200.0), 2.0
        self.lines = [{"rect": [510, 70, 890, 90], "text": "Region Units Share"}, {"rect": [510, 110, 890, 130], "text": "North 120 40"},
                      {"rect": [510, 150, 890, 170], "text": "South 80 27"}]
    def ready(self): return True

def test_area_capture_cues_style_no_fig_id():
    import focus
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    state.DOC = None; state.AREA_KINDS = FakeArea()
    try:
        rec = {"area_pdf": "doc-unresolved"}
        f = focus.Focus("North 120 40", [100.0, 200.0, 500.0, 350.0], None, True, True, rec)
        f.area_clip = [100.0, 200.0, 500.0, 350.0]
        r, method = f._area_capture_cue(focus.FocusRect([0, 0, 1, 1]), "North sold 120 units, a 40 percent share.")
        assert method == "doc-table_row" and list(r) == [350.0, 254.5, 200.0, 11.0] and r.header == [350.0, 234.5, 200.0, 11.0], (list(r), r.header)
        r2, m2 = f._area_capture_cue(focus.FocusRect([0, 0, 1, 1]), "The chart rises over time.")
        assert m2 == "doc-figure" and list(r2) == [120.0, 230.0, 200.0, 200.0] and rec["figure_mode_src"] == "area-capture"
        r.sent = r2.sent = 0
        f.show(r, "x"); f.show(r2, "y")
        shows = [json.loads(l) for l in open(os.path.join(HOME, "logs", "focus.jsonl")) if '"show"' in l]
        assert [s["cue_kind"] for s in shows[-2:]] == ["table_row", "figure_box"], shows
        for s in shows[-2:]:
            assert s["color"] == focus.CUE_STYLE["color"] and s["opacity"] == focus.CUE_STYLE["opacity"] and "fig_id" not in s, s
        assert "_fig_ids" not in rec
    finally: state.AREA_KINDS = None

def test_no_hex_in_phase6_code():
    for name in ("blockcls.py",):
        src = open(os.path.join(SRC, name), encoding="utf-8").read()
        assert not re.search(r"#[0-9A-Fa-f]{6}\b", src), name

# ---------------- OCR schema and rows ----------------
def test_ocr_schema_and_rows():
    good = {"version": 1, "engine": "vision-text", "image_size": [300, 100], "unit": "px", "origin": "top-left",
            "lines": [{"rect": [10, 10, 290, 25], "text": "Region Units", "conf": 0.98}, {"rect": [10, 40, 290, 55], "text": "North 120", "conf": 0.97}],
            "tables": [], "ms": 12}
    assert blockcls.ocr_valid(good)
    for bad in ({**good, "version": 2}, {**good, "lines": [{"rect": [10, 10, 400, 25], "text": "x", "conf": 1}]}, {**good, "tables": None}, []):
        assert not blockcls.ocr_valid(bad)
    ocr = {"lines": [[100, 200, 190, 205, "Region Units"], [100, 210, 190, 215, "North 120"]], "tables": []}
    rows, src = blockcls.ocr_rows(ocr, [95, 195, 200, 220])
    assert src == "ocr-lines" and len(rows) == 2 and rows[1]["tokens"] == sorted(rows[1]["tokens"]) and "north" in rows[1]["tokens"], rows
    assert blockcls.ocr_rows(None, [0, 0, 1, 1]) == ([], "none")
    tab = {"lines": [], "tables": [{"rows": [{"rect": [100, 200, 150, 205], "cells": ["A", "B"]}, {"rect": [100, 210, 150, 215], "cells": ["North", "120"]}]}]}
    rows, src = blockcls.ocr_rows(tab, [95, 195, 200, 220])
    assert src == "ocr-table" and rows[0]["rect"] == [95, 200, 200, 205], rows

def test_run_ocr_failures():
    helper = os.path.join(HOME, "fake-ocr")
    with open(helper, "w") as f: f.write("#!/bin/sh\nexit 4\n")
    os.chmod(helper, 0o755)
    png = os.path.join(HOME, "in.png"); open(png, "wb").close()
    assert blockcls.run_ocr(helper, png) is None
    assert not any(n.startswith("speakeasy_cap_ocr_") for n in os.listdir(tempfile.gettempdir()) if str(os.getpid()) in n)

# ---------------- renders (doc venv) ----------------
def test_byte_identical_crops():
    try:
        import pdfpage
    except ImportError:
        print("skip test_byte_identical_crops: no pypdfium2"); return
    pdf = os.path.join(REPO, "tests", "audit", "bench", "fixtures", "public-gap", "gap-report-01", "doc.pdf")
    page = pdfpage.open_pdf(pdf)[0]
    shas = []
    for k in range(2):
        out = os.path.join(tempfile.gettempdir(), f"speakeasy_test_kind_{os.getpid()}_{k}.png")
        try:
            blockcls.kind_png(page, [122.3, 139.2, 471.8, 286.8], page.rect.width, page.rect.height, out)
            shas.append(hashlib.sha256(open(out, "rb").read()).hexdigest())
            from PIL import Image
            assert max(Image.open(out).size) == blockcls.KIND_CROP_PX
        finally: os.remove(out)
    assert shas[0] == shas[1], shas

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"): f()
    print("ok")
