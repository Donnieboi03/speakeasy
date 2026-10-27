"""Layout detector fallbacks, events and block invariants (docs/contracts/phase4-layout.md sections 4.7, 5, 6).
Run under the doc venv (pypdfium2 + onnxruntime + numpy) with a verified model copy:
    ~/.speakeasy/tts-venv/bin/python tests/test_layoutdet.py   (model: SPEAKEASY_LAYOUT_MODEL, else ~/.speakeasy/models, read-only)
The no-onnxruntime and reportlab checks also need an interpreter with pypdfium2, Pillow and reportlab but without onnxruntime
(NO_ORT_PYTHON); they skip when it is unset. Every parse runs in a temp SPEAKEASY_HOME, never ~/.speakeasy."""
import json, os, shutil, subprocess, sys, tempfile
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SRC = os.path.join(REPO, "src")
sys.path.insert(0, SRC)
import layoutdet

MODEL = os.environ.get("SPEAKEASY_LAYOUT_MODEL") or os.path.expanduser("~/.speakeasy/models/pp_doclayout_s.onnx")
NO_ORT_PYTHON = os.environ.get("NO_ORT_PYTHON", "")
FIXTURES = os.path.join(REPO, "tests", "audit", "bench", "fixtures", "public")
DOCS = sorted(d for d in os.listdir(FIXTURES) if os.path.isdir(os.path.join(FIXTURES, d)))
TMP = tempfile.mkdtemp(prefix="se_layout_test_")

def parse(doc, python=sys.executable, **env):
    home = tempfile.mkdtemp(dir=TMP); out = os.path.join(home, "out.json")
    e = {k: v for k, v in os.environ.items() if not k.startswith("SPEAKEASY_")}
    e.update(SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_TRACKER=os.path.join(home, "no-tracker"), **env)
    p = subprocess.run([python, os.path.join(SRC, "docmap.py"), "parse-pdf", os.path.join(FIXTURES, doc, "doc.pdf"), out],
                       env=e, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return json.load(open(out)), p.stderr

def blocks(data):
    return [pg.get("blocks") for pg in data["pages"]]

def without_ms(data):
    d = json.loads(json.dumps(data))
    for pg in d["pages"]: pg.get("layout", {}).pop("ms", None)
    for ev in d["layout_events"]: ev.pop("ms", None)
    return d

def assert_fallback(data, stderr, reason):
    assert f"layout: detector unavailable ({reason})" in stderr, stderr
    ev = [e for e in data["layout_events"] if e["event"] == "layout_detector_unavailable"]
    assert len(ev) == 1 and ev[0]["reason"] == reason and ev[0]["page"] is None, ev
    assert data["layout"]["status"] == "unavailable" and data["layout"]["reason"] == reason
    assert all(pg["layout"] == {"src": "heuristic", "reason": reason} for pg in data["pages"] if pg["figs_done"])
    assert not any(b["src"] in ("detector", "regex+detector") for pg in data["pages"] for b in pg["blocks"])

def check_invariants(data):
    for pg in data["pages"]:
        if not pg["figs_done"]: continue
        W, H, bl = pg["w"], pg["h"], pg["blocks"]
        seen = set()
        for b in bl:
            assert b["kind"] in ("paragraph", "caption", "figure", "table") and "src" in b, b
            x0, y0, x1, y1 = b["bbox"]; assert 0 <= x0 < x1 <= W + 0.1 and 0 <= y0 < y1 <= H + 0.1, b
            if b["kind"] == "paragraph":
                assert b["src"] in ("detector", "lines")
                ls = [pg["lines"][i] for i in b["lines"]]
                assert b["bbox"] == [min(l[0] for l in ls), min(l[1] for l in ls), max(l[2] for l in ls), max(l[3] for l in ls)]
            if b["kind"] == "caption": assert b["src"] in ("regex", "regex+detector")
            for i in b.get("lines", []): assert i not in seen, ("shared line", i); seen.add(i)
        for f in pg["figures"]:
            assert sum(1 for b in bl if b["kind"] == f["kind"] and b.get("label") == f["label"]) == 1
            a, c = f["bbox"], f["caption_bbox"]
            vertical = min(a[3], c[3]) - max(a[1], c[1])
            horizontal = min(a[2], c[2]) - max(a[0], c[0])
            assert vertical <= 2 or horizontal <= 0, ("body overlaps its caption", f["label"], a, c)
            if pg["columns"] == 2 and (c[2] <= W / 2 + 10 or c[0] >= W / 2 - 10) and f["bbox_src"] == "detector":
                assert a[2] <= W / 2 + 10 if c[2] <= W / 2 + 10 else a[0] >= W / 2 - 10, ("body leaves its caption's column", f["label"])

def test_detector_on_every_page_and_events():
    for doc in DOCS:
        data, stderr = parse(doc, SPEAKEASY_LAYOUT_MODEL=MODEL)
        assert "layout:" not in stderr, stderr
        assert data["layout"] == {"status": "ok", "detector_version": layoutdet.DETECTOR_VERSION}
        pages = [pg for pg in data["pages"] if pg["figs_done"]]
        assert all(pg["layout"]["src"] == "detector" for pg in pages)
        ev = data["layout_events"]
        assert [e["page"] for e in ev] == [pg["n"] for pg in pages] and all(e["event"] == "layout_page" for e in ev)
        for e, pg in zip(ev, pages):
            assert e["blocks_by_kind"] == pg["layout"]["blocks_by_kind"] == {k: sum(b["kind"] == k for b in pg["blocks"]) for k in layoutdet.KINDS}
        check_invariants(data)

def test_invariants_with_detector_off():
    for doc in DOCS:
        data, stderr = parse(doc, SPEAKEASY_LAYOUT="off")
        assert_fallback(data, stderr, "disabled"); check_invariants(data)

def test_deterministic():
    a, _ = parse("arx-2406.06619", SPEAKEASY_LAYOUT_MODEL=MODEL)
    b, _ = parse("arx-2406.06619", SPEAKEASY_LAYOUT_MODEL=MODEL)
    assert without_ms(a) == without_ms(b)

def test_fallback_reasons_match_layout_off():
    doc = "syn-fig5-01"
    off, _ = parse(doc, SPEAKEASY_LAYOUT="off")
    bad = os.path.join(TMP, "bad.onnx"); raw = bytearray(open(MODEL, "rb").read()); raw[1000] ^= 1; open(bad, "wb").write(raw)
    for reason, kw in (("model-missing", {"SPEAKEASY_LAYOUT_MODEL": os.path.join(TMP, "nope.onnx")}),
                       ("model-mismatch", {"SPEAKEASY_LAYOUT_MODEL": bad})):
        data, stderr = parse(doc, **kw); assert_fallback(data, stderr, reason); assert blocks(data) == blocks(off), reason
    if os.path.exists(NO_ORT_PYTHON):
        data, stderr = parse(doc, python=NO_ORT_PYTHON, SPEAKEASY_LAYOUT_MODEL=MODEL)
        assert_fallback(data, stderr, "no-onnxruntime"); assert blocks(data) == blocks(off)
    else:
        print(f"skip no-onnxruntime: {NO_ORT_PYTHON} missing")

def test_model_lookup_order():
    home = tempfile.mkdtemp(dir=TMP); os.makedirs(os.path.join(home, "models"))
    shutil.copyfile(MODEL, os.path.join(home, "models", layoutdet.MODEL_FILE))
    old = dict(os.environ)
    try:
        os.environ.pop("SPEAKEASY_LAYOUT_MODEL", None); os.environ["SPEAKEASY_HOME"] = home
        assert layoutdet.model_path() == os.path.join(home, "models", layoutdet.MODEL_FILE)
        os.environ["SPEAKEASY_LAYOUT_MODEL"] = os.path.join(TMP, "missing.onnx")
        assert layoutdet.model_path() is None                     # a set override never falls through
    finally:
        os.environ.clear(); os.environ.update(old)

def _in_process_parse(doc, **patch):
    """parse_pdf in this process with layoutdet attributes patched (mocked session, patched hash)."""
    import docmap
    saved = {k: getattr(layoutdet, k) for k in patch}
    for k, v in patch.items(): setattr(layoutdet, k, v)
    old = dict(os.environ); os.environ["SPEAKEASY_HOME"] = tempfile.mkdtemp(dir=TMP)
    try:
        return docmap.parse_pdf(os.path.join(FIXTURES, doc, "doc.pdf"))
    finally:
        for k, v in saved.items(): setattr(layoutdet, k, v)
        os.environ.clear(); os.environ.update(old)

def test_load_failed():
    junk = os.path.join(TMP, "junk.onnx"); open(junk, "wb").write(b"not an onnx model")
    off, _ = parse("syn-fig5-01", SPEAKEASY_LAYOUT="off")
    os.environ["SPEAKEASY_LAYOUT_MODEL"] = junk
    try:
        data = _in_process_parse("syn-fig5-01", MODEL_SIZE=len(b"not an onnx model"), MODEL_SHA256=layoutdet.file_sha256(junk))
    finally:
        os.environ["SPEAKEASY_LAYOUT_MODEL"] = MODEL
    ev = data["layout_events"]
    assert len(ev) == 1 and ev[0]["reason"] == "load-failed" and ev[0]["error"], ev
    assert all(pg["layout"]["reason"] == "load-failed" for pg in data["pages"] if pg["figs_done"])
    assert blocks(json.loads(json.dumps(data))) == blocks(off)

def test_inference_failed_page_falls_back_alone():
    real_detect = layoutdet.Detector._detect; calls = []
    def flaky(self, page, W, H):
        calls.append(1)
        if len(calls) == 2: raise RuntimeError("mock inference failure")
        return real_detect(self, page, W, H)
    os.environ["SPEAKEASY_LAYOUT_MODEL"] = MODEL
    layoutdet.Detector._detect = flaky
    try:
        data = _in_process_parse("syn-fig5-01")
    finally:
        layoutdet.Detector._detect = real_detect
    off, _ = parse("syn-fig5-01", SPEAKEASY_LAYOUT="off")
    srcs = [pg["layout"]["src"] for pg in data["pages"]]
    assert srcs[1] == "heuristic" and data["pages"][1]["layout"]["reason"] == "inference-failed"
    assert all(s == "detector" for i, s in enumerate(srcs) if i != 1)
    failed = [e for e in data["layout_events"] if e["event"] == "layout_detector_unavailable"]
    assert failed == [{"event": "layout_detector_unavailable", "reason": "inference-failed", "error": "RuntimeError",
                       "detector_version": layoutdet.DETECTOR_VERSION, "page": 2}]
    assert data["layout"]["status"] == "ok"
    assert json.loads(json.dumps(data["pages"][1]["blocks"])) == off["pages"][1]["blocks"]

def test_sel_parse_events_and_prev_pages():
    """--sel parses detect the selection's page and neighbours; a later partial parse adds only its own events and
    keeps the earlier pages' layout; a cache hit runs nothing."""
    import docmap
    home = tempfile.mkdtemp(dir=TMP); pdf = os.path.join(home, "doc.pdf")
    shutil.copyfile(os.path.join(FIXTURES, "syn-report-01", "doc.pdf"), pdf)
    old = dict(os.environ)
    os.environ.update(SPEAKEASY_HOME=home, SPEAKEASY_DOC_PYTHON=sys.executable, SPEAKEASY_LAYOUT_MODEL=MODEL, SPEAKEASY_TRACKER=os.path.join(home, "none"))
    try:
        full, _ = parse("syn-report-01", SPEAKEASY_LAYOUT_MODEL=MODEL)
        n = len(full["pages"]); assert n >= 3
        text_of = lambda k: "\n".join(l[4] for l in full["pages"][k - 1]["lines"][:6])
        first, state = docmap.load(pdf, sel_text=text_of(1)); assert state == "miss"
        assert [e["page"] for e in first["layout_events"]] == [1, 2]
        again, state = docmap.load(pdf, sel_text=text_of(n)); assert state == "partial-miss"
        assert [e["page"] for e in again["layout_events"]] == [k for k in (n - 1, n) if k > 2]
        assert [pg["layout"] for pg in again["pages"][:2]] == [pg["layout"] for pg in first["pages"][:2]]
        _, state = docmap.load(pdf, sel_text=text_of(1)); assert state == "hit"
    finally:
        os.environ.clear(); os.environ.update(old)

def test_layout_off_in_config_file():
    home = tempfile.mkdtemp(dir=TMP); open(os.path.join(home, "config"), "w").write("SPEAKEASY_LAYOUT=off\n")
    out = os.path.join(home, "out.json")
    e = {k: v for k, v in os.environ.items() if not k.startswith("SPEAKEASY_")}
    e.update(SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_LAYOUT_MODEL=MODEL)
    p = subprocess.run([sys.executable, os.path.join(SRC, "docmap.py"), "parse-pdf", os.path.join(FIXTURES, "syn-fig5-01", "doc.pdf"), out],
                       env=e, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert_fallback(json.load(open(out)), p.stderr, "disabled")

def test_pdfkit_path_matches_page_and_logs_event():
    """SPEAKEASY_PDF_ENGINE=pdfkit: a stub tracker prints --pdf-lines output (figs_done pages without figures)."""
    import docmap, pdfpage
    home = tempfile.mkdtemp(dir=TMP); pdf = os.path.join(home, "doc.pdf")
    shutil.copyfile(os.path.join(FIXTURES, "syn-report-01", "doc.pdf"), pdf)
    pages = []
    for i, page in enumerate(pdfpage.open_pdf(pdf)):
        pages.append({"n": i + 1, "w": page.rect.width, "h": page.rect.height, "lines": docmap._text_lines(page)[0], "figures": [], "figs_done": True})
    dump = os.path.join(home, "pdfkit.json"); json.dump({"kind": "pdf", "pages": pages, "n_pages": len(pages)}, open(dump, "w"))
    stub = os.path.join(home, "speakeasy-track"); open(stub, "w").write(f"#!/bin/sh\ncat '{dump}'\n"); os.chmod(stub, 0o755)
    old = dict(os.environ)
    os.environ.update(SPEAKEASY_HOME=home, SPEAKEASY_PDF_ENGINE="pdfkit", SPEAKEASY_TRACKER=stub)
    try:
        doc, state = docmap.load(pdf, sel_text="\n".join(l[4] for l in pages[0]["lines"][3:8]))
        assert state == "miss-pdfkit"
        assert doc["layout_events"] == [{"event": "layout_detector_unavailable", "reason": "pdfkit-engine", "error": None, "detector_version": None, "page": None}]
        pg, how = docmap.match_page(doc, "\n".join(l[4] for l in pages[0]["lines"][3:8])); assert pg["n"] == 1, how
        assert docmap.parse_summary(pg)["layout"] == {"src": "heuristic", "reason": "pdfkit-engine"}
    finally:
        os.environ.clear(); os.environ.update(old)

def test_table_beside_other_column_prose_stays_in_its_column():
    """Two-column page: ruled table in the left column above a framed figure, prose beside it in the right column."""
    if not os.path.exists(NO_ORT_PYTHON): print("skip gutter test: no reportlab interpreter"); return
    spec = json.load(open(os.path.join(REPO, "tests", "audit", "bench", "specs", "syn-fig5-01.json")))
    table = dict(next(it for it in spec["flow"] if "table" in it), span="column", grid=True, w_frac=1.0)
    figure = dict(next(it for it in spec["flow"] if it.get("figure") == "F5"), span="column", frame=True, h=150)
    prose = [it for it in spec["flow"] if "para" in it][:3]
    spec.update(doc_id="gutter", cases=[], flow=[table, figure, {"break": "column"}] + prose)
    d = tempfile.mkdtemp(dir=TMP); json.dump(spec, open(os.path.join(d, "spec.json"), "w"))
    subprocess.run([NO_ORT_PYTHON, os.path.join(REPO, "tests", "audit", "bench", "gen", "synth.py"), os.path.join(d, "spec.json"), d], check=True, capture_output=True)
    import docmap
    os.environ["SPEAKEASY_LAYOUT_MODEL"] = MODEL
    pg = _in_process_parse_path(os.path.join(d, "doc.pdf"))["pages"][0]
    W = pg["w"]; right = [i for i, l in enumerate(pg["lines"]) if l[0] >= W / 2 - 10 and l[2] - l[0] > 0.25 * W]
    for f in pg["figures"]:
        assert f["bbox"][2] <= W / 2 + 10, (f["label"], f["bbox"])
    in_para = {i for b in pg["blocks"] if b["kind"] == "paragraph" for i in b["lines"]}
    assert set(right) <= in_para, sorted(set(right) - in_para)

def test_fig5_table_stays_left_of_prose_on_both_paths():
    """boxacc's Fig. 5 page: one-column by the column rule, Table 1's caption ends past the midline, prose beside it."""
    if not os.path.exists(NO_ORT_PYTHON): print("skip fig5 test: no reportlab interpreter"); return
    d = tempfile.mkdtemp(dir=TMP)
    subprocess.run([NO_ORT_PYTHON, os.path.join(REPO, "tests", "audit", "boxacc", "make_fig5.py"), d], check=True, capture_output=True)
    for env in ({"SPEAKEASY_LAYOUT_MODEL": MODEL}, {"SPEAKEASY_LAYOUT": "off"}):
        old = dict(os.environ); os.environ.update(env)
        try: pg = _in_process_parse_path(os.path.join(d, "fig5.pdf"))["pages"][0]
        finally: os.environ.clear(); os.environ.update(old)
        prose = [i for i, l in enumerate(pg["lines"]) if l[4].startswith("Right column sentence")]
        table = next(f for f in pg["figures"] if f["label"] == "Table 1")
        assert table["bbox"][2] < min(pg["lines"][i][0] for i in prose), (env, table["bbox"])
        in_para = {i for b in pg["blocks"] if b["kind"] == "paragraph" for i in b["lines"]}
        assert set(prose) <= in_para, (env, sorted(set(prose) - in_para))

def _in_process_parse_path(path):
    import docmap
    old = dict(os.environ); os.environ["SPEAKEASY_HOME"] = tempfile.mkdtemp(dir=TMP)
    try: return docmap.parse_pdf(path)
    finally: os.environ.clear(); os.environ.update(old)

def test_no_text_in_layout_fields():
    data, _ = parse("arx-2406.06619", SPEAKEASY_LAYOUT_MODEL=MODEL)
    words = {w for pg in data["pages"] for l in pg["lines"] for w in l[4].split() if len(w) >= 6 and w.isalpha()}
    blob = json.dumps([data["layout"], data["layout_events"], [pg.get("layout") for pg in data["pages"]]])
    assert not any(w in blob for w in words if w not in ("detector", "heuristic", "status", "unavailable", "blocks", "caption", "figure", "paragraph"))

if __name__ == "__main__":
    assert layoutdet.model_verified(MODEL), f"{MODEL} is not the verified model"
    try:
        for k, f in list(globals().items()):
            if k.startswith("test_"): f(); print("ok", k)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("ok")
