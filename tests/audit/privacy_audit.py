#!/usr/bin/env python3
"""Privacy audit of a capture: privacy_audit.py [--image PATH] [--log runs.jsonl] [--screen WxH] [--thumb OUT]
                                             [--focus-log focus.jsonl] [--tracker-log tracker.jsonl] [--live focus.json]
Defaults: ~/.speakeasy/last_capture.png and last log line. Reports captured rect vs screen, highlight bbox, thumbnail to eyeball.
Also audits labels and the Phase 2 log fields for document text (`label_audit`; `PRIVACY_LEAK` when anything is found):
without SPEAKEASY_LOG_TEXT=1 every focus-log / live-file / tracker-draw label must be null or match LABEL_RE, and the
Phase 2 fields (sent, score, runner_up, doc.target_src, doc.split_from, first_focus_ms, doc_ready_ms, doc_parse,
ground_miss_reasons) and the Phase 4 layout fields (doc_parse.layout, layout_events) hold numbers, enums, labels and the
detector version hash only. Offending values are reported by position, never printed."""
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
SRC_ENUM = {"graphics", "whitespace", "caption-only", "detector", "text-rows", "graphics+text-rows"}
TARGET_SRC = re.compile(r"^(lines|panel-labels|panel-guessed|table-rows|label-object|label-pad|vlm|figure:(graphics|whitespace|caption-only|detector)|table:(graphics|text-rows|graphics\+text-rows|caption-only|detector))$")
COUNT_KEYS = {"columns", "figs_done", "blocks", "bbox_src", "labels_src", "panels", "split", "paragraph", "caption", "figure", "table", "pdf", "ocr", "none", "labels", "guessed",
              "error", "no-box", "too-large", "timeout", "superseded", "no-fit"} | SRC_ENUM
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
    return {"text_mode": text_mode, "labels_checked": checked, "violations": len(bad), "first": bad[:5], "verdict": "ok" if not bad else "document text in labels or phase-2 fields"}
out["label_audit"] = label_audit(a.log)
if out["label_audit"]["violations"]: out["PRIVACY_LEAK"] = out["label_audit"]["verdict"]
_ls = [l for l in open(a.log) if l.strip()] if os.path.exists(a.log) else []
_last = json.loads(_ls[-1]) if _ls else {}
if os.path.exists(a.image) and _last and not _last.get("image"):
    out["STALE_CAPTURE"] = f"{a.image} exists but the last run used no image; it is a leftover from an earlier run"
for stray in ("/tmp/speakeasy_sel.png",):
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
