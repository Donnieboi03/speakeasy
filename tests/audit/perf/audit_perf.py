#!/usr/bin/env python3
"""Black-box performance/behaviour budgets for the AX-first tracker, native overlay, single VLM and menu-bar app.
Reads only logs: <logs>/tracker.jsonl, runs.jsonl, focus.jsonl, hs.log (legacy Hammerspoon), overlay.jsonl (if present).
Each check returns PASS / FAIL / NEEDS-BUILD (the field the spec relies on is not logged yet) / NO-DATA.
Usage: audit_perf.py LOGDIR [--json out.json] [--md out.md]

Log contract the new builds should emit (proposed; legacy fallbacks noted):
  tracker.jsonl frame : src in {ax,ocr,doc}            (legacy: method)
                        scroll_lag_ms (scroll event -> box moved), per frame that follows a scroll
                        hide_ms + trigger in {app-switch,tab-switch,window-switch,scrolled-off,content-changed} on hide frames
                        rect [x,y,w,h] and text_rect [x,y,w,h] (union of the spoken text's glyph boxes)
  tracker.jsonl exit  : scroll_lag_avg_ms, scroll_lag_max_ms, ax_hits, ocr_hits, rss_mb_peak
  runs.jsonl          : tts_first_audio_ms, model (single VLM, qwen3-vl), ollama_models[{name,size_mb}], tts_server_rss_mb, peak_rss_mb,
                        ram_total_mb (optional: whole-pipeline physical footprint), overlay in {swift-panel,hammerspoon}, app in {menubar,cli}
"""
import argparse, json, os, re, statistics, sys
B = dict(scroll_avg=30, scroll_max=60, hide_avg=50, hide_max=100, width_ratio_max=1.25, width_ratio_min=0.9, ttfa_p50=1500, ttfa_p95=3000, ram_max_mb=8192, ram_aim_mb=4096, ax_share_min=0.5)
def jl(p):
    out = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x.startswith("{"):
                try: out.append(json.loads(x))
                except Exception: pass
    return out
def q(v, p): v = sorted(v); return v[min(len(v) - 1, int(round(p * (len(v) - 1))))] if v else None
def audit(d):
    T, Rn, H = jl(os.path.join(d, "tracker.jsonl")), jl(os.path.join(d, "runs.jsonl")), (open(os.path.join(d, "hs.log"), errors="ignore").read() if os.path.exists(os.path.join(d, "hs.log")) else "")
    F = [l for l in T if l.get("event") == "frame"]; X = [l for l in T if l.get("event") == "exit"]; C = []
    def add(name, status, detail, needs=None): C.append({"check": name, "status": status, "detail": detail, **({"needs": needs} if needs else {})})
    # 1 scroll -> box lag
    lag = [l["scroll_lag_ms"] for l in F if isinstance(l.get("scroll_lag_ms"), (int, float))]; src = "tracker.jsonl"
    if not lag:
        hs = [(float(a), float(b)) for a, b in re.findall(r"lag scroll->box avg ([\d.]+) ms max ([\d.]+) ms", H)]
        ex = [(x["scroll_lag_avg_ms"], x["scroll_lag_max_ms"]) for x in X if isinstance(x.get("scroll_lag_avg_ms"), (int, float))]
        sess = ex or hs; src = "tracker exit" if ex else "hs.log (legacy)"
        if sess:
            avg, mx = statistics.mean(a for a, _ in sess), max(b for _, b in sess)
            add("scroll_lag_budget", "PASS" if avg < B["scroll_avg"] and mx < B["scroll_max"] else "FAIL", {"source": src, "sessions": len(sess), "avg_ms": round(avg, 1), "max_ms": round(mx, 1), "budget": f"avg<{B['scroll_avg']} max<{B['scroll_max']}"})
        else: add("scroll_lag_budget", "NO-DATA", "no scroll lag logged")
        add("scroll_lag_per_frame_in_tracker_jsonl", "NEEDS-BUILD", "frames carry no scroll_lag_ms", needs="AX/event-driven tracker")
    else:
        avg, mx = statistics.mean(lag), max(lag)
        add("scroll_lag_budget", "PASS" if avg < B["scroll_avg"] and mx < B["scroll_max"] else "FAIL", {"source": src, "n": len(lag), "avg_ms": round(avg, 1), "p95_ms": q(lag, .95), "max_ms": mx})
    fit = [abs(float(x)) for x in re.findall(r"fit residual (-?[\d.]+) pt", H)]
    if fit: add("scroll_fit_residual_under_20pt", "PASS" if max(fit) < 20 else "FAIL", {"n": len(fit), "max_pt": max(fit), "values": fit[:8]})
    # 2 hide latency on switch
    hides = [l for l in F if l.get("visible") is False and (l.get("trigger") or l.get("reason"))]
    hm = [l["hide_ms"] for l in hides if isinstance(l.get("hide_ms"), (int, float))]
    if hm: add("hide_latency_budget", "PASS" if statistics.mean(hm) < B["hide_avg"] and max(hm) < B["hide_max"] else "FAIL", {"n": len(hm), "avg_ms": round(statistics.mean(hm), 1), "max_ms": max(hm), "triggers": sorted({str(l.get("trigger")) for l in hides})})
    else:
        prox = [l["ms"] for l in hides if isinstance(l.get("ms"), (int, float))]
        add("hide_latency_budget", "NEEDS-BUILD", {"proxy_scan_ms_on_hide_frames": {"n": len(prox), "avg": round(statistics.mean(prox), 1) if prox else None, "max": max(prox) if prox else None}, "reasons": sorted({str(l.get('reason')) for l in hides})}, needs="native overlay: hide_ms + trigger (app/tab/window switch)")
    sw = [l for l in hides if str(l.get("trigger") or l.get("reason")) in ("app-switch", "tab-switch", "window-switch", "not-frontmost")]
    add("hide_on_app_or_tab_switch_observed", "PASS" if sw else "NEEDS-BUILD", {"switch_hides": len(sw)}, needs=None if sw else "trigger=app-switch/tab-switch on hide frames")
    # 3 box width vs text
    pairs = [(l["rect"][2], l["text_rect"][2]) for l in F if isinstance(l.get("rect"), list) and isinstance(l.get("text_rect"), list) and len(l["rect"]) >= 4 and len(l["text_rect"]) >= 4 and l["text_rect"][2] > 0]
    if pairs:
        r = [a / b for a, b in pairs]; bad = [x for x in r if not B["width_ratio_min"] <= x <= B["width_ratio_max"]]
        add("box_width_tight_to_text", "PASS" if len(bad) <= 0.05 * len(r) else "FAIL", {"n": len(r), "median_ratio": round(statistics.median(r), 2), "out_of_band": len(bad), "band": [B["width_ratio_min"], B["width_ratio_max"]]})
    else:
        w = [l["rect"][2] for l in F if isinstance(l.get("rect"), list) and len(l["rect"]) >= 4]
        add("box_width_tight_to_text", "NEEDS-BUILD", {"rect_w_median": statistics.median(w) if w else None}, needs="text_rect on frames")
    # 4 fallback source per event
    srcs = [l.get("src", l.get("method")) for l in F if l.get("visible") is not False]
    have_src = sum(1 for l in F if "src" in l)
    from collections import Counter
    mix = dict(Counter(map(str, srcs)))
    if have_src:
        vis = [l for l in F if l.get("visible") is not False]
        add("source_recorded_per_visible_frame", "PASS" if vis and all(l.get("src") in ("ax", "ocr", "doc") for l in vis) else "FAIL", {"mix": dict(Counter(str(l.get("src")) for l in vis))})
    else: add("source_recorded_per_visible_frame", "NEEDS-BUILD" if srcs else "NO-DATA", {"legacy_method_mix": mix}, needs="src in {ax,ocr,doc} on every frame")
    axh = sum(x.get("ax_hits") or 0 for x in X); ocr = sum(x.get("ocr_hits") or 0 for x in X)
    add("accessibility_first_share", "PASS" if axh + ocr and axh / (axh + ocr) >= B["ax_share_min"] else ("FAIL" if axh + ocr else "NO-DATA"), {"ax_hits": axh, "ocr_hits": ocr, "min_share": B["ax_share_min"]})
    # 5 time to first audio
    t = [r["tts_first_audio_ms"] for r in Rn if isinstance(r.get("tts_first_audio_ms"), (int, float)) and not r.get("dry_run")]
    if t: add("ttfa_budget", "PASS" if q(t, .5) <= B["ttfa_p50"] and q(t, .95) <= B["ttfa_p95"] else "FAIL", {"n": len(t), "p50": q(t, .5), "p95": q(t, .95), "max": max(t), "budget": f"p50<={B['ttfa_p50']} p95<={B['ttfa_p95']}",
           "by_command": {c: q([r["tts_first_audio_ms"] for r in Rn if r.get("command") == c and isinstance(r.get("tts_first_audio_ms"), (int, float))], .5) for c in ("speak", "selection")}})
    else: add("ttfa_budget", "NO-DATA", "no tts_first_audio_ms")
    # 6 RAM
    tots = []
    for r in Rn:
        if isinstance(r.get("ram_total_mb"), (int, float)): tots.append(("logged", r["ram_total_mb"])); continue
        m = sum(x.get("size_mb") or 0 for x in (r.get("ollama_models") or []) if isinstance(x, dict))
        if m: tots.append(("estimated", m + (r.get("tts_server_rss_mb") or 0) + (r.get("peak_rss_mb") or 0) + max([x.get("rss_mb_peak") or 0 for x in X] or [0])))
    if tots:
        mx = max(v for _, v in tots); kind = "logged" if any(k == "logged" for k, _ in tots) else "estimated"
        add("ram_total_under_8gb", "PASS" if mx < B["ram_max_mb"] else "FAIL", {"max_mb": round(mx), "kind": kind, "note": "estimate = ollama model sizes + tts RSS + cli + tracker; MLX Metal memory under-counted" if kind == "estimated" else ""})
        add("ram_total_near_4gb_aim", "PASS" if mx <= B["ram_aim_mb"] else "FAIL", {"max_mb": round(mx)})
        if kind == "estimated": add("ram_total_mb_logged", "NEEDS-BUILD", "runs carry no ram_total_mb (physical footprint incl. Metal)", needs="menu-bar app / single VLM")
    else: add("ram_total_under_8gb", "NO-DATA", "")
    # 7 single VLM / overlay / app
    models = {x.get("name") for r in Rn for x in (r.get("ollama_models") or []) if isinstance(x, dict)} | {r.get("model") for r in Rn[-10:]} | {r.get("vision_model") for r in Rn[-10:]}
    models.discard(None); recent = {x.get("name") for r in Rn[-5:] for x in (r.get("ollama_models") or []) if isinstance(x, dict)}
    add("single_local_vlm_qwen3_vl", "PASS" if recent and all("qwen3-vl" in m.lower() or "qwen3vl" in m.lower() for m in recent) else "NEEDS-BUILD", {"recent_loaded": sorted(recent), "seen": sorted(models)}, needs="Qwen3-VL-4B build")
    ov = {r.get("overlay") for r in Rn if r.get("overlay")}
    add("native_swift_overlay", "PASS" if ov == {"swift-panel"} else "NEEDS-BUILD", {"overlay": sorted(ov), "hs_log_present": bool(H)}, needs="Swift overlay panel (log overlay=swift-panel)")
    ap = {r.get("app") for r in Rn if r.get("app")}
    add("menubar_app", "PASS" if "menubar" in ap else "NEEDS-BUILD", sorted(ap), needs="Swift menu-bar app (log app=menubar)")
    # 8 privacy: no raw user paths
    leaks = [k for r in Rn for k, v in r.items() if isinstance(v, str) and re.search(r"/Users/[^/]+/(?!\.speakeasy/)|\.pdf$|\.md$", v)]
    home_paths = sorted({k for r in Rn for k, v in r.items() if isinstance(v, str) and "/Users/" in v})
    add("no_raw_document_paths_in_runs", "PASS" if not leaks else "FAIL", {"fields": sorted(set(leaks))})
    add("no_home_paths_in_runs", "PASS" if not home_paths else "FAIL", {"fields_with_/Users/": home_paths, "note": "username leaks via app-internal paths"})
    s = {k: sum(1 for c in C if c["status"] == k) for k in ("PASS", "FAIL", "NEEDS-BUILD", "NO-DATA")}
    return {"logdir": d, "budgets": B, "summary": s, "checks": C}
def md(res):
    o = [f"# perf/behaviour audit: {res['logdir']}", f"summary: {res['summary']}", "", "| check | status | detail | needs |", "|---|---|---|---|"]
    o += [f"| {c['check']} | {c['status']} | {json.dumps(c['detail'], default=str)[:200].replace('|','¦')} | {c.get('needs','')} |" for c in res["checks"]]
    return "\n".join(o) + "\n"
if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("logdir"); ap.add_argument("--json"); ap.add_argument("--md"); a = ap.parse_args()
    r = audit(a.logdir); m = md(r); print(m)
    if a.json: json.dump(r, open(a.json, "w"), indent=2, default=str)
    if a.md: open(a.md, "w").write(m)
    sys.exit(1 if r["summary"]["FAIL"] else 0)
