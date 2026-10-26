#!/usr/bin/env python3
"""Privacy audit of a capture: privacy_audit.py [--image PATH] [--log runs.jsonl] [--screen WxH] [--thumb OUT]
                                             [--focus-log focus.jsonl] [--tracker-log tracker.jsonl] [--live focus.json]
Defaults: ~/.speakeasy/last_capture.png and last log line. Reports captured rect vs screen, highlight bbox, thumbnail to eyeball.
Also audits labels and the Phase 2 log fields for document text (`label_audit`; `PRIVACY_LEAK` when anything is found):
without SPEAKEASY_LOG_TEXT=1 every focus-log / live-file / tracker-draw label must be null or match LABEL_RE, and the
Phase 2 fields (sent, score, runner_up, doc.target_src, doc.split_from, first_focus_ms, doc_ready_ms, doc_parse,
ground_miss_reasons) and the Phase 4 layout fields (doc_parse.layout, layout_events) hold numbers, enums, labels and the
detector version hash only; Phase 5 part_pick focus records and runs part_picks hold ids, numbers, enums and page geometry
only, whatever SPEAKEASY_LOG_TEXT is, and no speakeasy_cap_pick_* crop may be left in TMPDIR. Offending values are reported
by position, never printed."""
import json, os, re, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
import highlight
home = os.path.expanduser(os.environ.get("SPEAKEASY_HOME", "~/.speakeasy"))
ap = argparse.ArgumentParser(); ap.add_argument("--image", default=os.path.join(home, "last_capture.png"))
ap.add_argument("--log", default=os.path.join(home, "logs", "runs.jsonl")); ap.add_argument("--screen"); ap.add_argument("--thumb", default="/tmp/speakeasy_capture_thumb.png")
ap.add_argument("--focus-log"); ap.add_argument("--tracker-log"); ap.add_argument("--live")
a = ap.parse_args(); out = {"image": a.image}
LABEL_RE = re.compile(r"^(Figure|Table) \d+[a-h]?( row \d+)?( \(grounded\))?$")
SRC_ENUM = {"graphics", "whitespace", "caption-only", "detector", "detector-substitute", "text-rows", "graphics+text-rows"}
TARGET_SRC = re.compile(r"^(lines|panel-labels|panel-guessed|table-rows|label-object|label-pad|vlm|callout|callout-marker|pick|figure:(graphics|whitespace|caption-only|detector|detector-substitute)|table:(graphics|text-rows|graphics\+text-rows|caption-only|detector))$")
COUNT_KEYS = {"columns", "figs_done", "blocks", "bbox_src", "labels_src", "panels", "split", "paragraph", "caption", "figure", "table", "pdf", "ocr", "none", "labels", "guessed",
              "error", "no-box", "too-large", "timeout", "superseded", "no-fit",
              "parts", "text", "callout", "callout_attached", "gutter", "low-confidence", "off-menu", "no-logprobs", "unavailable", "out-of-scope"} | SRC_ENUM
PP_KEYS = {"state", "sent", "sentence_id", "fig", "panel", "scope", "candidates", "cand_rects", "chosen_index", "p_top", "p_margin", "letter_mass",
           "palette", "called", "outcome", "applied", "drop", "ms", "queue_ms", "model", "provider", "fallback_from", "confidence", "ts", "pid"}
PP_ENUMS = {"scope": {"panel", "figure"}, "palette": {"red", "magenta", "blue", None}, "drop": {"superseded", "no-fit", None}, "provider": {"local", "openrouter"},
            "outcome": {"accepted", "low-confidence", "off-menu", "no-logprobs", "unavailable", "error", "timeout", "out-of-scope"},
            "fallback_from": {None, "unavailable", "no-logprobs"}, "confidence": {None, "logprob", "letter-only"}}
PICK_SKIPPED = {"too-few", "whole-figure", "disabled", "doc-not-ready", "degenerate-figure", "generic-phrase"}
AREA_PDF = {"ok", "doc-timeout", "doc-unresolved", "doc-failed", "no-page", "markdown", "no-fit", "empty", "error"}
ENUM_RUN = {"area_pdf": AREA_PDF, "area_region_src": {"area-fit", "capture-fit", "none"}, "figure_mode_src": {None, "caption", "vision", "area-overlap", "area-labels"}}
INT_RUN = ("area_dropped", "stand_in_blocked", "no_target_chunks", "figures_degenerate")
FIRST_DRAW_SRC = {"page-fit", "capture+offset", "held"}
def _part_pick_ok(r):
    """phase5-parts.md 6.1: exactly the contract keys; ids, numbers, enums, a Figure/Table label and page rects only."""
    if set(r) != PP_KEYS: return False
    for k, v in r.items():
        if k in PP_ENUMS:
            if v not in PP_ENUMS[k]: return False
        elif k == "fig":
            if not (v is None or (isinstance(v, str) and re.match(r"^(Figure|Table) \d+$", v))): return False
        elif k == "sentence_id":
            if not (v is None or (isinstance(v, str) and re.match(r"^s\d+$", v))): return False
        elif k == "panel":
            if not (v is None or (isinstance(v, str) and re.match(r"^[a-z]$", v))): return False
        elif k == "model":
            if not (isinstance(v, str) and re.match(r"^[A-Za-z0-9._:/-]{1,80}$", v)): return False
        elif k == "cand_rects":
            if not (isinstance(v, list) and all(isinstance(x, list) and len(x) == 4 and all(isinstance(y, (int, float)) and not isinstance(y, bool) for y in x) for x in v)): return False
        elif k != "state" and not (v is None or isinstance(v, (bool, int, float))): return False
    return True
def _part_picks_ok(v):
    if not isinstance(v, dict): return False
    for k, x in v.items():
        if k == "breaker":
            if isinstance(x, dict):
                if not (set(x) <= {"local", "openrouter"} and all(y in (None, "unavailable", "no-logprobs") for y in x.values())): return False
            elif x not in (None, "unavailable", "no-logprobs"): return False
        elif k in ("outcomes", "skipped", "ms", "by_provider", "fallback"):
            keys = PP_ENUMS["outcome"] | PICK_SKIPPED | {"p50", "max", "local", "openrouter", "unavailable", "no-logprobs"}
            if not (isinstance(x, dict) and all(kk in keys and (xx is None or isinstance(xx, (int, float))) for kk, xx in x.items())): return False
        elif not (x is None or isinstance(x, (bool, int, float))): return False
    return True
def _jl(p):
    rows = []
    if p and os.path.exists(p):
        for i, l in enumerate(open(p, errors="ignore")):
            try: rows.append((i + 1, json.loads(l)))
            except Exception: pass
    return rows
LAYOUT_REASONS = {"disabled", "no-onnxruntime", "model-missing", "model-mismatch", "load-failed", "inference-failed", "pdfkit-engine"}
DETECTOR_VERSION_RE = re.compile(r"^pp-doclayout-s/[0-9a-f]{12}$")
ERROR_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
def _int(v): return isinstance(v, int) and not isinstance(v, bool)
def _counts_only(v):
    if isinstance(v, dict): return all(k in COUNT_KEYS and _counts_only(x) for k, x in v.items())
    return v is None or isinstance(v, (bool, int, float))
def _kind_counts(v):
    return isinstance(v, dict) and set(v) <= {"paragraph", "caption", "figure", "table"} and all(_int(x) for x in v.values())
def _page_layout_ok(v):
    """doc_parse.layout (Phase 4): {src, ms, blocks_by_kind} on a detector page, {src, reason} on a fallback page."""
    if not isinstance(v, dict) or v.get("src") not in ("detector", "heuristic"): return False
    if not set(v) <= {"src", "ms", "blocks_by_kind", "reason"}: return False
    return ("ms" not in v or _int(v["ms"])) and ("blocks_by_kind" not in v or _kind_counts(v["blocks_by_kind"])) and \
        ("reason" not in v or v["reason"] in LAYOUT_REASONS)
def _layout_event_ok(e):
    """layout_events entries (Phase 4): layout_page / layout_detector_unavailable, numbers, enums and hashes only."""
    if not isinstance(e, dict): return False
    dv = e.get("detector_version")
    if dv is not None and not (isinstance(dv, str) and DETECTOR_VERSION_RE.match(dv)): return False
    if e.get("event") == "layout_page":
        return set(e) == {"event", "page", "ms", "detector_version", "blocks_by_kind"} and _int(e["page"]) and _int(e["ms"]) and _kind_counts(e["blocks_by_kind"])
    if e.get("event") == "layout_detector_unavailable":
        return set(e) == {"event", "reason", "error", "detector_version", "page"} and e["reason"] in LAYOUT_REASONS and \
            (e["error"] is None or (isinstance(e["error"], str) and ERROR_CLASS_RE.match(e["error"]))) and (e["page"] is None or _int(e["page"]))
    return False
def _doc_parse_ok(v):
    if not isinstance(v, dict): return False
    rest = {k: x for k, x in v.items() if k != "layout"}
    return _counts_only(rest) and ("layout" not in v or _page_layout_ok(v["layout"]))
def label_audit(runs_log):
    logs = os.path.dirname(runs_log); fl = a.focus_log or os.path.join(logs, "focus.jsonl"); tl = a.tracker_log or os.path.join(logs, "tracker.jsonl")
    live = a.live or os.path.join(os.path.dirname(logs), "focus.json")
    cfg = os.path.join(os.path.dirname(logs), "config")
    cfg_on = os.path.exists(cfg) and any(re.match(r"\s*SPEAKEASY_LOG_TEXT=1\s*$", l) for l in open(cfg, errors="ignore"))
    F, T, R = _jl(fl), _jl(tl), _jl(runs_log)
    text_mode = os.environ.get("SPEAKEASY_LOG_TEXT") == "1" or cfg_on or any("text" in r for _, r in F if r.get("state") in ("map", "show"))
    bad, checked = [], 0
    def lab(where, n, v):
        nonlocal checked; checked += 1
        if v is not None and not (isinstance(v, str) and LABEL_RE.match(v)): bad.append({"where": where, "line": n, "label_len": len(str(v))})
    for n, r in F:
        d = r.get("doc") if isinstance(r.get("doc"), dict) else None
        if r.get("state") == "show" and d is not None and "label" in d and "text" not in r: lab("focus.jsonl", n, d["label"])
        if r.get("state") == "part_pick" and not _part_pick_ok(r): bad.append({"where": "focus.jsonl", "line": n, "field": "part_pick"})
        if r.get("state") == "map":
            for k in ("sent", "score", "runner_up"):
                if k in r and not (r[k] is None or (isinstance(r[k], (int, float)) and not isinstance(r[k], bool))): bad.append({"where": "focus.jsonl", "line": n, "field": k})
        if d is not None:
            if "target_src" in d and not (isinstance(d["target_src"], str) and TARGET_SRC.match(d["target_src"])): bad.append({"where": "focus.jsonl", "line": n, "field": "doc.target_src"})
            if "split_from" in d and not (isinstance(d["split_from"], str) and LABEL_RE.match(d["split_from"])): bad.append({"where": "focus.jsonl", "line": n, "field": "doc.split_from"})
    if not text_mode:
        for n, r in T:
            if r.get("event") == "draw" and "label" in r: lab("tracker.jsonl", n, r["label"])
            for k in ("meta", "doc"):
                if isinstance(r.get(k), dict) and "label" in r[k]: lab("tracker.jsonl", n, r[k]["label"])
        if os.path.exists(live):
            try:
                lv = json.load(open(live)); d = lv.get("doc") if isinstance(lv.get("doc"), dict) else {}
                if "label" in d: lab("focus.json", 1, d["label"])
            except Exception: pass
    for n, r in R:
        for k in ("first_focus_ms", "doc_ready_ms"):
            if k in r and not (r[k] is None or (isinstance(r[k], int) and not isinstance(r[k], bool))): bad.append({"where": "runs.jsonl", "line": n, "field": k})
        if "doc_parse" in r and not _doc_parse_ok(r["doc_parse"]): bad.append({"where": "runs.jsonl", "line": n, "field": "doc_parse"})
        if "ground_miss_reasons" in r and not (isinstance(r["ground_miss_reasons"], dict) and _counts_only(r["ground_miss_reasons"])):
            bad.append({"where": "runs.jsonl", "line": n, "field": "ground_miss_reasons"})
        if "layout_events" in r and not (isinstance(r["layout_events"], list) and all(_layout_event_ok(e) for e in r["layout_events"])):
            bad.append({"where": "runs.jsonl", "line": n, "field": "layout_events"})
        if "part_picks" in r and not _part_picks_ok(r["part_picks"]): bad.append({"where": "runs.jsonl", "line": n, "field": "part_picks"})
        for k, enum in ENUM_RUN.items():
            if k in r and r[k] not in enum: bad.append({"where": "runs.jsonl", "line": n, "field": k})
        if "area_pdf_error" in r and not (r["area_pdf_error"] is None or (isinstance(r["area_pdf_error"], str) and ERROR_CLASS_RE.match(r["area_pdf_error"]))):
            bad.append({"where": "runs.jsonl", "line": n, "field": "area_pdf_error"})
        if "area_page_rect" in r and not (r["area_page_rect"] is None or (isinstance(r["area_page_rect"], list) and len(r["area_page_rect"]) == 4 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r["area_page_rect"]))):
            bad.append({"where": "runs.jsonl", "line": n, "field": "area_page_rect"})
        for k in INT_RUN:
            if k in r and not (r[k] is None or _int(r[k])): bad.append({"where": "runs.jsonl", "line": n, "field": k})
    for n, r in T:
        if r.get("event") == "draw" and "first_draw_src" in r and r["first_draw_src"] not in FIRST_DRAW_SRC: bad.append({"where": "tracker.jsonl", "line": n, "field": "first_draw_src"})
    if os.path.exists(live):
        try:
            if json.load(open(live)).get("state") == "part_pick": bad.append({"where": "focus.json", "line": 1, "field": "part_pick"})
        except Exception: pass
    return {"text_mode": text_mode, "labels_checked": checked, "violations": len(bad), "first": bad[:5], "verdict": "ok" if not bad else "document text in labels or phase-2 fields"}
out["label_audit"] = label_audit(a.log)
if out["label_audit"]["violations"]: out["PRIVACY_LEAK"] = out["label_audit"]["verdict"]
_ls = [l for l in open(a.log) if l.strip()] if os.path.exists(a.log) else []
_last = json.loads(_ls[-1]) if _ls else {}
if os.path.exists(a.image) and _last and not _last.get("image"):
    out["STALE_CAPTURE"] = f"{a.image} exists but the last run used no image; it is a leftover from an earlier run"
import glob as _glob, tempfile as _tempfile
for stray in ["/tmp/speakeasy_sel.png"] + sorted(set(_glob.glob(os.path.join(_tempfile.gettempdir(), "speakeasy_cap_pick_*")) + _glob.glob("/tmp/speakeasy_cap_pick_*"))):
    if os.path.exists(stray): out.setdefault("stray_files", []).append(stray)
if not os.path.exists(a.image): out["error"] = "no capture image kept"; print(json.dumps(out, indent=2)); sys.exit(1)
im = Image.open(a.image); out["capture_px"] = f"{im.size[0]}x{im.size[1]}"
last = None
if os.path.exists(a.log):
    ls = [l for l in open(a.log) if l.strip()]
    if ls: last = json.loads(ls[-1])
if last: out.update({k: last.get(k) for k in ("ts", "command", "capture_method", "error", "capture_rect", "capture_crop", "capture_original_px", "capture_cropped_px", "image_px", "image_sent_px")})
if a.screen:
    sw, sh = map(int, a.screen.split("x")); out["capture_frac_of_screen"] = round(im.size[0] * im.size[1] / (sw * sh), 3)
    out["verdict"] = "FULL SCREEN captured, check this" if out["capture_frac_of_screen"] > 0.9 else ("large capture" if out["capture_frac_of_screen"] > 0.5 else "ok")
im.convert("RGB").copy().thumbnail((640, 640)); t = im.convert("RGB"); t.thumbnail((640, 640)); t.save(a.thumb)
out["thumbnail"] = a.thumb; out["reminder"] = f"Open {a.thumb} to confirm only the intended region was captured."
out["highlight_in_capture"] = highlight.detect(a.image)
print(json.dumps(out, indent=2))
