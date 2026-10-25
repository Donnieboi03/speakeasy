#!/usr/bin/env python3
"""Headless box-accuracy analyzer for the latest (or a given) speakeasy run. Reads logs only, plus optionally the PDF for ground truth.

For every drawn box (tracker.jsonl draw records, screen points x,y,w,h) it finds the doc target the box was drawn for (focus.jsonl show
events: doc.target in page points x0,y0,x1,y1, top-left origin), infers the page->screen transform the tracker used, and measures each box
edge against the expected region:
  * ground truth = the PDF's own coordinates for that label (caption + graphics above it for figures, caption + rows for tables), with --pdf;
  * or --expect x,y,w,h (a screen rect you measured), or the run's capture region in Read-area mode (box must stay inside it).
A box passes when every visible edge is within --tol points (default 8) of the expected region.
Usage:
  python3 tests/audit/box_accuracy.py [--logs ~/.speakeasy/logs] [--run RUN_ID|latest] [--pdf FILE] [--expect x,y,w,h] [--tol 8] [--json OUT]
  (use a python with PyMuPDF for --pdf, e.g. ~/.speakeasy/tts-venv/bin/python)
Prints no document text or paths; labels, numbers and hashes only."""
import argparse, datetime, json, os, re, statistics, sys
def jl(p):
    o = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x.startswith("{"):
                try: o.append(json.loads(x))
                except Exception: pass
    return o
def rts(r):
    t = r.get("ts")
    if isinstance(t, (int, float)): return float(t)
    try: return datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except Exception: return None
def x1y1(r): return [r[0], r[1], r[0] + r[2], r[1] + r[3]]
# ---------- ground truth from the PDF
def pdf_truth(pdf, page, label):
    try: import pymupdf
    except ImportError:
        try: import fitz as pymupdf
        except ImportError: return None, "no PyMuPDF in this python"
    p = pymupdf.open(pdf)[page - 1]
    if isinstance(label, list):  # text band: the PDF line(s) overlapping the logged target
        t = pymupdf.Rect(label); ls = []
        for b in p.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                r = pymupdf.Rect(l["bbox"])
                if r.intersects(t) and (r & t).height > 0.5 * r.height: ls.append(r)
        if not ls: return None, "no pdf line under the target"
        u = ls[0]
        for r in ls[1:]: u |= r
        return [u.x0, u.y0, u.x1, u.y1], "pdf-lines"
    m = re.match(r"(fig(?:ure)?|table)\.?\s*(\d+)\s*([a-z])?", label or "", re.I)
    if not m: return None, f"label not parseable: {label}"
    kind, num, panel = ("table" if m.group(1).lower().startswith("t") else "figure"), m.group(2), m.group(3)
    blocks = [b for b in p.get_text("blocks") if b[6] == 0]
    pat = re.compile(rf"^\s*(table)\s*{num}\b" if kind == "table" else rf"^\s*(fig\.?|figure)\s*{num}\b", re.I)
    cap = next((b for b in blocks if pat.match(b[4])), None)
    if not cap: return None, f"caption for {kind} {num} not found"
    gfx = [g["rect"] for g in p.get_drawings() if g["rect"].width * g["rect"].height > 50] + [pymupdf.Rect(i["bbox"]) for i in p.get_image_info()]
    if kind == "figure":
        gfx = [g for g in gfx if g.height > 4 and g.width > 4]
        above = [g for g in gfx if g.y1 <= cap[1] + 4 and g.y1 >= cap[1] - 600]
        below = [g for g in gfx if g.y0 >= cap[3] - 4 and g.y0 <= cap[3] + 600]
        cand = above or below
        if not cand: return None, "no graphics next to the caption"
        # start from the graphic nearest the caption (largest bottom edge above it / smallest top edge below it)
        start = max(cand, key=lambda g: (round(g.y1, 1), g.width * g.height)) if above else min(cand, key=lambda g: (round(g.y0, 1), -g.width * g.height))
        u = pymupdf.Rect(start); grew = True
        while grew:
            grew = False
            for g in cand:
                if g in u: continue
                if g.y1 >= u.y0 - 8 and g.y0 <= u.y1 + 8 and g.x1 >= u.x0 and g.x0 <= u.x1:
                    if not u.contains(g): u |= g; grew = True
        r = [u.x0, u.y0, u.x1, u.y1]
        if panel:  # panel = framed sub-rect whose top-left holds "(a)"/"a)"
            pl = [b for b in blocks if re.match(rf"^\(?{panel}\)\s*$", b[4].strip()) and u.x0 - 5 <= b[0] <= u.x1 and u.y0 - 5 <= b[1] <= u.y1]
            if pl:
                fr = [g for g in gfx if g.x0 <= pl[0][0] + 2 and g.y0 <= pl[0][1] + 2 and g.x1 >= pl[0][2] and g.y1 >= pl[0][3] and g.width > 0.5 * u.width]
                if fr: f = min(fr, key=lambda g: g.width * g.height); r = [f.x0, f.y0, f.x1, f.y1]
        return r, "pdf"
    # table: caption top down to the last block before a gap > 20 pt or a figure graphic
    y = cap[3]; x0, x1 = cap[0], cap[2]
    col = sorted([b for b in blocks if b[1] >= cap[3] - 1 and b[0] < cap[0] + (cap[2] - cap[0]) and b[0] >= cap[0] - 10], key=lambda b: b[1])
    for b in col:
        if b[1] - y > 20 or any(g.y0 <= b[1] <= g.y1 and g.height > 80 for g in gfx): break
        y = max(y, b[3]); x1 = max(x1, b[2])
    rules = [g for g in gfx if g.height < 3 and cap[1] - 5 <= g.y0 <= y + 12 and g.x0 >= cap[0] - 10]
    if rules: y = max(y, max(g.y1 for g in rules)); x1 = max(x1, max(g.x1 for g in rules))
    return [x0, cap[1], x1, y], "pdf"
# ---------- absolute anchor: find the rendered PDF page inside the run's capture image (template match, FFT)
def anchor(pdf, page, img_path, cap, s0):
    try:
        import numpy as np, pymupdf
        from PIL import Image
    except ImportError as e: return None, f"anchor needs numpy+PIL+PyMuPDF ({e})"
    im = Image.open(img_path).convert("L"); bk = im.width / cap[2]; f = 4
    A = np.asarray(im.resize((im.width // f, im.height // f)), dtype=np.float32)
    def edges2(a): gy, gx = np.gradient(a); return np.hypot(gx, gy)
    EA = edges2(A); EA -= EA.mean(); best = None
    pg = pymupdf.open(pdf)[page - 1]
    for sc in ([s0 * (1 + d / 200) for d in range(-8, 9)] if s0 else [z / 100 * 1.594 for z in range(80, 201, 5)]):
        k = sc * bk / f; pm = pg.get_pixmap(matrix=pymupdf.Matrix(k, k), colorspace=pymupdf.csGRAY)
        P = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width).astype(np.float32); EP = edges2(P); EP -= EP.mean()
        H, W = A.shape[0] + EP.shape[0], A.shape[1] + EP.shape[1]
        c = np.fft.irfft2(np.fft.rfft2(EA, (H, W)) * np.conj(np.fft.rfft2(EP, (H, W))), (H, W))
        i = np.unravel_index(np.argmax(c), c.shape); dy, dx = i[0] if i[0] < H - EP.shape[0] + 1 else i[0] - H, i[1] if i[1] < W - EP.shape[1] + 1 else i[1] - W
        score = c[i] / (np.linalg.norm(EA) * np.linalg.norm(EP) + 1e-6)
        if not best or score > best[0]: best = (score, sc, dx, dy)
    score, sc, dx, dy = best
    return (float(sc), float(cap[0] + dx * f / bk), float(cap[1] + dy * f / bk), round(float(score), 3)), "ok"
# ---------- transform inference
def fit(target, rect, win, s_known=None):
    """target page x0,y0,x1,y1 ; rect screen x,y,w,h. An edge sitting exactly on the window edge counts as clamped (unknown).
    Scale comes from an unclamped full fit (s_known) when this box is clamped. Returns (s, tx, ty, note)."""
    tw, th = target[2] - target[0], target[3] - target[1]; x0, y0, x1, y1 = x1y1(rect)
    top_cut = bool(win) and abs(y0 - win[1]) <= 2; bot_cut = bool(win) and abs(y1 - (win[1] + win[3])) <= 2
    left_cut = bool(win) and abs(x0 - win[0]) <= 2; right_cut = bool(win) and abs(x1 - (win[0] + win[2])) <= 2
    if s_known and (top_cut or bot_cut or left_cut or right_cut): s = s_known
    elif not (left_cut or right_cut) and tw > 0: s = (x1 - x0) / tw
    elif not (top_cut or bot_cut) and th > 0: s = (y1 - y0) / th
    else: return None
    tx = (x0 - s * target[0]) if not left_cut else (x1 - s * target[2])
    ty = (y0 - s * target[1]) if not top_cut else ((y1 - s * target[3]) if not bot_cut else None)
    if ty is None: return None
    aspect = None if (top_cut or bot_cut or left_cut or right_cut) or th <= 0 else round(((y1 - y0) / th) / s, 3)
    return s, tx, ty, {"top_cut": bool(top_cut), "bottom_cut": bool(bot_cut), "aspect": aspect}
def edges(rect, exp, win, cut):
    x0, y0, x1, y1 = x1y1(rect); e0, f0, e1, f1 = exp
    if win:  # the expected region is also limited to the visible window
        f0c, f1c = max(f0, win[1]), min(f1, win[1] + win[3]); e0c, e1c = max(e0, win[0]), min(e1, win[0] + win[2])
    else: e0c, f0c, e1c, f1c = e0, f0, e1, f1
    d = {"left": x0 - e0c, "top": y0 - f0c, "right": x1 - e1c, "bottom": y1 - f1c}
    return {k: round(v, 1) for k, v in d.items()}
def key(d): return d.get("label") or tuple(round(v) for v in d["target"])
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--logs", default=os.path.expanduser(os.environ.get("SPEAKEASY_LOGS", "~/.speakeasy/logs")))
    ap.add_argument("--run", default="latest"); ap.add_argument("--pdf"); ap.add_argument("--expect"); ap.add_argument("--tol", type=float, default=8.0); ap.add_argument("--json")
    ap.add_argument("--capture", default=os.path.expanduser("~/.speakeasy/last_capture.png"), help="capture image of the run, for the absolute page->screen anchor")
    ap.add_argument("--truth-label", help="measure every doc box against this PDF object instead of the label speakeasy logged (e.g. 'Fig. 5' when it was mislabelled)")
    ap.add_argument("--with-doc", action="store_true", help="pick the latest run that has doc boxes")
    a = ap.parse_args()
    R = jl(os.path.join(a.logs, "runs.jsonl")); F = jl(os.path.join(a.logs, "focus.jsonl")); T = jl(os.path.join(a.logs, "tracker.jsonl"))
    if not R: print("no runs.jsonl"); sys.exit(2)
    cands = [r for r in R if a.run in ("latest",) or r.get("run_id") == a.run]
    if a.with_doc: cands = [r for r in cands if r.get("doc_source") and (r.get("focus_events") or 0) > 0]
    if not cands: print("run not found"); sys.exit(2)
    run = cands[-1]; t0 = rts(run) or 0; rid = run.get("run_id")
    nxt = [rts(r) for r in R if (rts(r) or 0) > t0 + 1]; t1 = min(nxt) if nxt else t0 + 3600
    shows = [f for f in F if f.get("state") == "show" and t0 - 5 <= f.get("ts", 0) < t1]
    own = {t.get("tracker_pid") for t in T if rid and t.get("run_id") == rid}
    trk = [t for t in T if t.get("tracker_pid") in own] if own else [t for t in T if t0 - 5 <= t.get("ts", 0) < t1]
    sess = sorted({t.get("tracker_pid") for t in trk if t.get("tracker_pid")})
    draws = [t for t in trk if t.get("event") == "draw" and isinstance(t.get("text_rect") or t.get("rect"), list)]
    win = next((t.get("win") for t in trk if isinstance(t.get("win"), list)), None)
    cap = [float(v) for v in run["capture_rect"].split(",")] if isinstance(run.get("capture_rect"), str) else None
    mode = "read-area" if run.get("capture_method") == "area" else ("selection" if run.get("command") == "selection" else run.get("capture_method"))
    docs = [s["doc"] for s in shows if isinstance(s.get("doc"), dict) and s["doc"].get("target")]
    hdr = {"run_ts": run.get("ts"), "run_id": rid, "command": run.get("command"), "mode": mode, "capture_rect": cap, "page": run.get("page"), "doc_source": run.get("doc_source"),
           "doc_transform": run.get("doc_transform"), "area_clamped": run.get("area_clamped"), "focus_methods": run.get("focus_methods"), "tracker_sessions": sess, "draws": len(draws), "window": win,
           "labels": sorted({f"{d.get('kind')}:{d.get('label')}" for d in docs})}
    print("== run", json.dumps(hdr, default=str))
    truth = {}
    if a.pdf:
        for d in docs:
            k = key(d)
            if k not in truth: truth[k] = pdf_truth(a.pdf, d.get("page") or run.get("page") or 1, a.truth_label or d.get("label") or d["target"])
        for k, (r, why) in truth.items():
            lab = k; tgt = next(d["target"] for d in docs if key(d) == k)
            off = [round(t - g, 1) for t, g in zip(tgt, r)] if r else None
            print(f"   page-space  {lab!s:14}{(' as ' + a.truth_label) if a.truth_label else ''} logged target {[round(v,1) for v in tgt]}  pdf truth {[round(v,1) for v in r] if r else why}  delta(x0,y0,x1,y1) {off}")
    ANC = None
    if a.pdf and cap and os.path.exists(a.capture) and abs(os.path.getmtime(a.capture) - t0) < 120:
        s0 = None
        for dr in draws:
            dd = next((x["doc"] for x in shows if isinstance(x.get("doc"), dict) and x["doc"].get("target")), None)
            if dd: f0 = fit(dd["target"], dr.get("text_rect") or dr["rect"], win); s0 = f0 and f0[0]; break
        ANC, why = anchor(a.pdf, run.get("page") or 1, a.capture, cap, s0)
        print("   anchor (page found in the capture image):", {"scale": round(ANC[0], 4), "tx": round(ANC[1], 1), "ty": round(ANC[2], 1), "score": ANC[3]} if ANC else why)
    else: print("   anchor: none (no --pdf, or the capture image is not from this run); offsets use a transform inferred from each box")
    OBJS = []
    if ANC:
        try:
            import pymupdf
            pg_ = pymupdf.open(a.pdf)[(run.get("page") or 1) - 1]
            OBJS = [([g["rect"].x0, g["rect"].y0, g["rect"].x1, g["rect"].y1], "graphic") for g in pg_.get_drawings() if g["rect"].width > 8 and g["rect"].height > 8]
            OBJS += [(list(l["bbox"]), "text-line") for b in pg_.get_text("dict")["blocks"] for l in b.get("lines", [])]
        except Exception: OBJS = []
    first_scroll = min([t["ts"] for t in trk if t.get("event") in ("scroll", "hscroll-confirmed", "correction")] or [1e18])
    exp_manual = x1y1([float(v) for v in a.expect.split(",")]) if a.expect else None
    rows = []; T_last = None; s_full = {}
    # pair each draw with the doc target that was showing at that time
    for dr in draws:
        rect = dr.get("text_rect") or dr["rect"]
        sh = [s for s in shows if s.get("ts", 0) <= dr["ts"] + 0.5]; doc = next((s.get("doc") for s in reversed(sh) if isinstance(s.get("doc"), dict) and s["doc"].get("target")), None)
        if doc and dr.get("cue") == "figure" and doc.get("kind") == "table": hdr.setdefault("warnings", set()).add(f"figure cue drawn for a table target ({doc.get('label')})")
        row = {"seq": dr.get("seq"), "t": round(dr["ts"] - t0, 2), "src": dr.get("src"), "cue": dr.get("cue"), "rect": rect, "label": doc and doc.get("label")}
        exp = None; how = None
        if doc:
            f = fit(doc["target"], rect, win, s_full.get(doc.get("label")))
            if f and not any((f[3].get(k) for k in ("top_cut", "bottom_cut"))) and f[3].get("aspect") and abs(f[3]["aspect"] - 1) < 0.05: s_full.setdefault(doc.get("label"), f[0])
            if f: T_last = f
            elif T_last: f = T_last
            if f:
                s, tx, ty, note = f; row.update(scale=round(s, 3), cut=note)
                gt = truth.get(key(doc), (None, None))[0] if a.pdf else None
                if gt and ANC and dr["ts"] < first_scroll: s, tx, ty = ANC[:3]; how = "pdf+capture (absolute)"; row["scale"] = round(s, 3)
                elif gt: how = "pdf, transform from this box (relative)"
                if gt: exp = [tx + s * gt[0], ty + s * gt[1], tx + s * gt[2], ty + s * gt[3]]
        if exp is None and not doc and ANC and dr["ts"] < first_scroll and OBJS:
            # no logged target: compare with the nearest real PDF object (graphic or text line) under the box, via the absolute anchor
            sa, txa, tya = ANC[:3]; x0, y0, x1, y1 = x1y1(rect); pg = [(x0 - txa) / sa, (y0 - tya) / sa, (x1 - txa) / sa, (y1 - tya) / sa]
            def iou(a_, b_):
                ix = max(0, min(a_[2], b_[2]) - max(a_[0], b_[0])); iy = max(0, min(a_[3], b_[3]) - max(a_[1], b_[1])); i = ix * iy
                return i / ((a_[2] - a_[0]) * (a_[3] - a_[1]) + (b_[2] - b_[0]) * (b_[3] - b_[1]) - i + 1e-9)
            best = max(OBJS, key=lambda o_: iou(pg, o_[0]))
            if iou(pg, best[0]) > 0.05:
                g_ = best[0]; exp = [txa + sa * g_[0], tya + sa * g_[1], txa + sa * g_[2], tya + sa * g_[3]]; how = f"nearest pdf {best[1]} (iou {iou(pg, g_):.2f})"; row["scale"] = round(sa, 3)
        if exp_manual: exp, how = exp_manual, "manual"
        if exp is None and cap and mode == "read-area":
            exp, how = None, "inside-capture"
            x0, y0, x1, y1 = x1y1(rect); c = x1y1(cap)
            out = {"left": round(min(0, x0 - c[0]), 1), "top": round(min(0, y0 - c[1]), 1), "right": round(max(0, x1 - c[2]), 1), "bottom": round(max(0, y1 - c[3]), 1)}
            row.update(how=how, offsets=out, max_abs=max(abs(v) for v in out.values()))
        if exp is not None:
            o = edges(rect, exp, win, row.get("cut"))
            # an edge is excused only when the expected edge itself is off the visible window (the box is then rightly clamped)
            off_top = win and exp[1] < win[1]; off_bot = win and exp[3] > win[1] + win[3]
            vis = {k: v for k, v in o.items() if not (k == "top" and off_top) and not (k == "bottom" and off_bot)}
            row.update(how=how, expected=[round(v, 1) for v in [exp[0], exp[1], exp[2] - exp[0], exp[3] - exp[1]]], offsets=o, max_abs=max(abs(v) for v in vis.values()) if vis else None)
        if "max_abs" in row and row["max_abs"] is not None: row["pass"] = row["max_abs"] <= a.tol
        rows.append(row)
    for r in rows:
        print(f"   draw seq={r['seq']} t+{r['t']}s src={r['src']} cue={r['cue']} label={r['label']} rect={[round(v) for v in r['rect']]}"
              + (f" expected={[round(v) for v in r['expected']]}" if r.get("expected") else "") + (f" offsets={r['offsets']} max={r['max_abs']} {'PASS' if r.get('pass') else 'FAIL'} ({r['how']})" if "offsets" in r else " (no ground truth)")
              + (f" scale={r.get('scale')}" if r.get("scale") else "") + (f" cut={r['cut']}" if r.get("cut") else ""))
    if hdr.get("warnings"): print("== warnings", sorted(hdr["warnings"]))
    m = [r for r in rows if "pass" in r]; vals = [abs(v) for r in m for v in r["offsets"].values()]
    summ = {"boxes": len(rows), "measured": len(m), "pass": sum(r["pass"] for r in m), "fail": sum(not r["pass"] for r in m), "tol_pt": a.tol,
            "edge_abs_mean": round(statistics.mean(vals), 1) if vals else None, "edge_abs_median": round(statistics.median(vals), 1) if vals else None, "edge_abs_max": max(vals) if vals else None,
            "worst_edge": max(((k, v, r["seq"]) for r in m for k, v in r["offsets"].items()), key=lambda x: abs(x[1]), default=None)}
    if not draws: summ["note"] = "no tracker draw records for this run (old tracker build or tracker not started)"
    print("== summary", json.dumps(summ)); print("RESULT:", "PASS" if m and not summ["fail"] else ("FAIL" if m else "NO-DATA"))
    if a.json: hdr["warnings"] = sorted(hdr.get("warnings", [])); json.dump({"run": hdr, "rows": rows, "summary": summ}, open(a.json, "w"), indent=2, default=float)
    sys.exit(0 if m and not summ["fail"] else (1 if m else 3))
if __name__ == "__main__": main()
