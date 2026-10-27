#!/usr/bin/env python3

"""Box-accuracy harness (black box). Ground truth comes from the fixture PDF's own coordinates (make_fig5.py).
Part A - speakeasy CLI: page-space doc targets for Read-area and selection runs over a zoom x backing x display x scroll matrix
         (figure cue on Fig. 5 under a ruled Table 1, the table itself, caption text band, prose text band), labels, and that the
         emitted screen rects stay inside the capture region in points (no Retina doubling, negative/secondary display origins).
Part B - box_accuracy.py: synthetic tracker draws generated with a known page->screen transform for every geometry; the analyzer must
         recover the transform from the capture image and report ~0 pt for correct boxes and the injected offset for wrong ones.
Env: DOC_VENV (python with PyMuPDF+Pillow+numpy, default the venv running this script), SPEAKEASY_BIN, BOXACC_QUICK=1 for a reduced matrix;
the caller's SPEAKEASY_LAYOUT_MODEL / SPEAKEASY_FIGCLS_MODEL / SPEAKEASY_LAYOUT2_MODEL reach the CLI (the shipped detector path), else
the parse uses its heuristics. Run this script with a python that has reportlab (fixture generation)."""
import itertools, json, os, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); AUD = os.path.dirname(HERE); sys.path.insert(0, HERE)
from geom import page_to_screen, xywh, render_capture, K
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(HERE, "../../../src/speakeasy")); VENV = os.environ.get("DOC_VENV", sys.prefix)
PY = os.path.join(VENV, "bin/python"); TOL = 8.0; R = []; TMP = tempfile.gettempdir()
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:300]})
FX = tempfile.mkdtemp(prefix="se_boxfx_"); subprocess.run([sys.executable, os.path.join(HERE, "make_fig5.py"), FX], check=True, capture_output=True)
PDF = os.path.join(FX, "fig5.pdf"); GT = json.load(open(os.path.join(FX, "fig5_gt.json")))["boxes"]
LINES = json.loads(subprocess.run([PY, "-c", f"import pymupdf,json;p=pymupdf.open({PDF!r})[0];print(json.dumps([[*l['bbox'],''.join(s['text'] for s in l['spans'])] for b in p.get_text('dict')['blocks'] for l in b.get('lines',[])]))"], capture_output=True, text=True, check=True).stdout)
def line(prefix): return next(l for l in LINES if l[4].startswith(prefix))
QUICK = os.environ.get("BOXACC_QUICK") == "1"
# geometry: zoom, backing (1 = non-Retina, 2 = Retina), display origin (main with 33/37 pt menu bar, secondary left/right/above), scroll (pt the page is scrolled)
ZOOMS = [1.0, 1.3] if QUICK else [1.0, 1.1, 1.25, 1.3, 1.5]
DISPLAYS = {"main_menubar33": (0, 33), "main_notch37": (0, 37), "left_secondary": (-1920, 0), "right_above": (1512, -1080)}
def geometries():
    for z, b in itertools.product(ZOOMS, [1, 2]): yield dict(zoom=z, backing=b, display="main_menubar33", scroll=0)
    for d, sc in itertools.product(DISPLAYS, [0, 180] if not QUICK else [180]): yield dict(zoom=1.3, backing=2, display=d, scroll=sc)
def place(g):
    dx, dy = DISPLAYS[g["display"]]; s = g["zoom"] * K
    # page top-left: centred in a 1512-wide window under the toolbar (+70), scrolled so Fig. 5 sits mid-window, minus g["scroll"]
    ox = dx + (1512 - 595.28 * s) / 2; oy = dy + 70 + 300 - 466.5 * s - g["scroll"] * 0.0 + (g["scroll"] and -g["scroll"])
    return dict(g, ox=ox, oy=oy)
def env(h, clip):
    e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, SPEAKEASY_LLM_URL=f"http://127.0.0.1:{PORT}/v1", SPEAKEASY_MODEL="mock", SPEAKEASY_VISION="never",
             PATH=os.path.join(AUD, "nosel", "stubs") + ":" + os.path.join(AUD, "tts", "stubs") + ":" + os.environ["PATH"], STUB_CLIP=clip,
             STUB_LOG=os.path.join(h, "stub.jsonl"), STUB_SPOKEN=os.path.join(h, "spoken.txt"), STUB_PLAY_SEC="0.05")
    e.pop("SPEAKEASY_DOC_READ", None); return e
def cli(g, mode, region_pg, clip, line_rects=None):
    """region_pg: page-space x0,y0,x1,y1 the user dragged (area) / selected (selection). Returns (log, shows, cap)."""
    h = tempfile.mkdtemp(prefix="se_box_"); os.symlink(VENV, os.path.join(h, "tts-venv"))
    json.dump({"app": "Cursor", "bundle": "com.todesktop.230313mzl4w4u92", "title": "fig5.pdf", "ax_document": "file://" + PDF}, open(os.path.join(h, "hint.json"), "w"))
    sc = page_to_screen(region_pg, g); cap = [round(v, 1) for v in xywh(sc)]
    img = os.path.join(TMP, f"speakeasy_cap_box{os.getpid()}.png"); render_capture(PDF, g, cap, img)
    args = ["--image", img, "--capture-rect=" + ",".join(map(str, cap)), "--doc-hint", os.path.join(h, "hint.json"), "--run-id", f"t{int(time.time()*1000)}"]
    if mode == "area":
        # Read area sends its OCR lines as line bounds (phase5-parts.md A2.1: the area's page rect comes from their fit to the
        # page; without them an area run has no region and only screen-only cues): the page lines inside the drag, mapped to screen
        inside = [l for l in LINES if region_pg[0] <= (l[0] + l[2]) / 2 <= region_pg[2] and region_pg[1] <= (l[1] + l[3]) / 2 <= region_pg[3]]
        lb, loc = [], 0
        for l in inside: lb.append({"loc": loc, "len": len(l[4]), "rect": [round(v, 1) for v in xywh(page_to_screen(l[:4], g))]}); loc += len(l[4]) + 1
        clip = "\n".join(l[4] for l in inside) or clip
        lbp = os.path.join(h, "area_lines.json"); json.dump(lb, open(lbp, "w"))
        cmd = [BIN, "speak", "--capture-method", "area", "--area-text", "ocr", "--line-bounds", lbp] + args; inp = clip
    else:
        cmd = [BIN, "selection", "--capture-method", "bounds"] + args; inp = None
        if line_rects: lb = os.path.join(h, "lines.json"); json.dump(line_rects, open(lb, "w")); cmd += ["--line-bounds", lb]
    for _ in range(2):  # second run sees a warm doc cache (first-time parses are not waited for)
        shutil.copy(img, img + ".run.png"); cmd[cmd.index("--image") + 1] = img + ".run.png"
        pr = subprocess.run(cmd, input=inp, capture_output=True, text=True, env=env(h, clip), timeout=200)
    if not os.path.exists(os.path.join(h, "logs", "runs.jsonl")): return {"_err": (pr.returncode, pr.stderr[-300:], cap)}, [], cap, h
    L = [json.loads(x) for x in open(os.path.join(h, "logs", "runs.jsonl"))]; F = [json.loads(x) for x in open(os.path.join(h, "logs", "focus.jsonl"))]
    pid = F[-1].get("pid") if F else None; shows = [f for f in F if f.get("state") == "show" and f.get("pid") == pid]
    for f in (img, img + ".run.png"):
        if os.path.exists(f): os.remove(f)
    return L[-1], shows, cap, h
def worst(a, b): return max(abs(x - y) for x, y in zip(a, b))
mock = subprocess.Popen([sys.executable, os.path.join(AUD, "stream", "mock_llm.py")], env=dict(os.environ, MOCK_PORT="18931", MOCK_TOKEN_DELAY="0.002", MOCK_TEXT="Figure 5 shows the training and validation pipeline. In panel a, recordings are filtered and segmented. Panel b validates the models."),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
PORT = 18931; time.sleep(0.8)
try:
    if not PORT: raise SystemExit("mock LLM did not start")
    # ---------------- Part A: CLI
    CASES = {
        "area_fig5": ("area", [30, 455, 570, 735], "Fig. 5 | Drowsiness Detector Training and Validation Diagram. a Ear ExG recordings are re-referenced and filtered.", "fig5"),
        "area_table1_over_fig5": ("area", [30, 262, 310, 392], "Table 1 | Relevant system, WANDmini, and NMIC specifications Maximum Recording Channels 64 Battery Life 44 h", "table1"),
        "sel_fig5_caption": ("sel", None, "Fig. 5 | Drowsiness Detector Training and Validation Diagram.", "fig5"),
        "sel_prose_band": ("sel", None, "Prose line 3 about ear EEG electrodes and the drowsiness protocol used here.", "prose_line_3"),
    }
    for g0 in geometries():
        g = place(g0); tag = f"z{g['zoom']}_b{g['backing']}_{g['display']}_s{g['scroll']}"
        for cn, (mode, region, clip, gtk) in CASES.items():
            if QUICK and cn.startswith("sel") and g["display"] != "main_menubar33": continue
            lr = None
            if mode == "sel":
                ls = [l for l in LINES if clip.startswith(l[4][:20]) or l[4].startswith(clip[:20])][:1]
                region = [ls[0][0], ls[0][1], ls[0][2], ls[0][3]]; lr = [[round(v, 1) for v in xywh(page_to_screen(l[:4], g))] for l in ls]
            log, shows, cap, h = cli(g, mode, region, clip, lr)
            if "_err" in log: add(f"A_{cn}_{tag}_cli_ran", False, log["_err"]); continue
            docs = [s["doc"] for s in shows if isinstance(s.get("doc"), dict) and s["doc"].get("target")]
            want = GT[gtk]; s = g["zoom"] * K
            t = next((d for d in docs if (gtk.startswith("fig") and d.get("kind") in ("figure",)) or (gtk == "table1" and d.get("kind") == "table") or (gtk.startswith("prose") and d.get("kind") in ("line", "lines", "text"))), None)
            add(f"A_{cn}_{tag}_doc_target_present", t, [(d.get("kind"), d.get("label")) for d in docs][:4])
            if t:
                off_pt = worst(t["target"], want)
                add(f"A_{cn}_{tag}_page_target_within_{TOL:g}pt_on_screen", off_pt * s <= TOL, f"page delta {off_pt:.1f} pt x scale {s:.2f} = {off_pt*s:.1f} screen pt; logged {[round(v,1) for v in t['target']]} truth {want}")
                if gtk == "fig5": add(f"A_{cn}_{tag}_label_is_figure_5", str(t.get("label", "")).replace("Fig.", "Figure").startswith("Figure 5"), t.get("label"))
                if gtk == "table1": add(f"A_{cn}_{tag}_table_stops_above_figure", t["target"][3] <= GT["fig5"][1] + 2, t["target"])
            c = [cap[0], cap[1], cap[0] + cap[2], cap[1] + cap[3]]
            outside = [s_["rect"] for s_ in shows if isinstance(s_.get("rect"), list) and not (s_["rect"][0] >= c[0] - 1 and s_["rect"][1] >= c[1] - 1 and s_["rect"][0] + s_["rect"][2] <= c[2] + 1 and s_["rect"][1] + s_["rect"][3] <= c[3] + 1)]
            if mode == "area": add(f"A_{cn}_{tag}_rects_inside_capture_points", not outside, {"capture": cap, "outside": outside[:2]})
            if lr:
                rr = [s_["rect"] for s_ in shows if isinstance(s_.get("rect"), list)]
                d = min((worst(r, lr[0]) for r in rr), default=None)
                add(f"A_{cn}_{tag}_text_band_rect_matches_line_bounds", d is not None and d <= TOL, f"best {d} pt; line {lr[0]} rects {rr[:2]}")
            shutil.rmtree(h, ignore_errors=True)
    # negative display origin passed the way a shell script would ("--capture-rect" "-1920,...")
    g = place(dict(zoom=1.3, backing=2, display="left_secondary", scroll=0)); cap = [round(v, 1) for v in xywh(page_to_screen([30, 455, 570, 735], g))]
    h = tempfile.mkdtemp(prefix="se_neg_"); img = os.path.join(TMP, f"speakeasy_cap_neg{os.getpid()}.png"); render_capture(PDF, g, cap, img)
    pr = subprocess.run([BIN, "speak", "--capture-method", "area", "--area-text", "ocr", "--image", img, "--capture-rect", ",".join(map(str, cap))], input="Fig. 5 | x", capture_output=True, text=True, env=env(h, ""), timeout=120)
    add("A_negative_origin_capture_rect_as_separate_arg_accepted", pr.returncode == 0, (pr.returncode, pr.stderr.strip().splitlines()[-1:] if pr.stderr else "", cap))
    if os.path.exists(img): os.remove(img)
    # ---------------- Part B: analyzer on synthetic draws with a known transform
    sys.path.insert(0, AUD)
    for g0 in geometries():
        g = place(g0); tag = f"z{g['zoom']}_b{g['backing']}_{g['display']}_s{g['scroll']}"
        for inj, (dx, dy, grow) in {"exact": (0, 0, 0), "shift_y_120": (0, -120, 0), "table_swallow": (0, 0, 1)}.items():
            d = tempfile.mkdtemp(prefix="se_boxan_"); os.makedirs(os.path.join(d, "logs")); L = os.path.join(d, "logs")
            region = [30, 455, 570, 735]; cap = [round(v, 1) for v in xywh(page_to_screen(region, g))]; img = os.path.join(d, "cap.png"); render_capture(PDF, g, cap, img)
            tgt = GT["fig5"] if not grow else [GT["fig5"][0], GT["table1"][1] + 24, GT["fig5"][2], GT["fig5"][3]]
            scr = page_to_screen(tgt, g); dr = [scr[0] + dx, scr[1] + dy, scr[2] - scr[0], scr[3] - scr[1]]
            t0 = time.time(); rid = "r" + tag + inj; win = [DISPLAYS[g["display"]][0], DISPLAYS[g["display"]][1], 3000, 3000]
            open(os.path.join(L, "runs.jsonl"), "w").write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)), "run_id": rid, "command": "speak", "capture_method": "area", "capture_rect": ",".join(map(str, cap)), "page": 1, "doc_source": "ax-document", "focus_events": 1}) + "\n")
            open(os.path.join(L, "focus.jsonl"), "w").write(json.dumps({"state": "show", "ts": t0 + .2, "pid": 1, "rect": cap, "doc": {"kind": "figure", "label": "Figure 5", "page": 1, "target": tgt}}) + "\n")
            open(os.path.join(L, "tracker.jsonl"), "w").write(json.dumps({"event": "start", "ts": t0 + .1, "tracker_pid": 7, "run_id": rid}) + "\n" + json.dumps({"event": "frame", "ts": t0 + .15, "tracker_pid": 7, "win": win}) + "\n" +
                                                             json.dumps({"event": "draw", "ts": t0 + .3, "tracker_pid": 7, "run_id": rid, "rect": dr, "text_rect": dr, "cue": "figure", "src": "event", "seq": 1}) + "\n")
            os.utime(img, (t0, t0))
            p = subprocess.run([PY, os.path.join(AUD, "box_accuracy.py"), "--logs", L, "--pdf", PDF, "--capture", img, "--json", os.path.join(d, "o.json")], capture_output=True, text=True, timeout=120)
            o = json.load(open(os.path.join(d, "o.json"))) if os.path.exists(os.path.join(d, "o.json")) else {}
            rows = o.get("rows", []); m = rows[0].get("max_abs") if rows else None; how = rows[0].get("how") if rows else None
            if inj == "exact": add(f"B_{tag}_exact_box_passes", p.returncode == 0 and m is not None and m <= 3, (m, how, p.stdout[-200:]))  # analyzer anchor precision ~±3 pt (1/4-scale FFT match)
            if inj == "shift_y_120": add(f"B_{tag}_shift120_measured", p.returncode == 1 and m is not None and abs(m - 120) <= 3, (m, how))
            if inj == "table_swallow": add(f"B_{tag}_table_swallow_top_edge_flagged", p.returncode == 1 and rows and abs(rows[0]["offsets"]["top"] + (GT["fig5"][1] - tgt[1]) * g["zoom"] * K) <= 3, (rows[0].get("offsets") if rows else None, how))
            add(f"B_{tag}_{inj}_absolute_anchor_used", how and "absolute" in how, how)
            shutil.rmtree(d, ignore_errors=True)
finally:
    mock.terminate(); shutil.rmtree(FX, ignore_errors=True)
res = {"summary": {"pass": sum(c["pass"] for c in R), "fail": sum(not c["pass"] for c in R)}, "checks": R}
os.makedirs(os.path.join(AUD, "reports"), exist_ok=True); json.dump(res, open(os.path.join(AUD, "reports", "box_accuracy.json"), "w"), indent=2)
md = ["# box accuracy harness", f"summary: {res['summary']}", "", "| check | result | detail |", "|---|---|---|"] + [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:200].replace('|','¦')} |" for c in R]
open(os.path.join(AUD, "reports", "box_accuracy.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
