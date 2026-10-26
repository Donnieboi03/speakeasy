#!/usr/bin/env python3
"""Audit speakeasy-track output: tracker.jsonl (track.json / overlay.json / logs/hs.log must not exist). Schema-agnostic: reports keys seen, rate, gaps, exit reason, cpu/rss if logged.
Usage: audit_tracker.py HOME_DIR [--fps 3]"""
import json, os, sys, argparse, statistics
from collections import Counter
REQ_ANY = {"ts"}; WANT = ["ts", "state", "rect", "visible", "reason", "fps", "cpu", "rss_mb", "found", "confidence", "scale", "event", "exit"]
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("home"); ap.add_argument("--fps", type=float, default=3); a = ap.parse_args()
    lp = os.path.join(a.home, "logs", "tracker.jsonl"); out = {"log": lp, "problems": []}
    lines, bad = [], 0
    if os.path.exists(lp):
        for l in open(lp):
            if not l.strip(): continue
            try: lines.append(json.loads(l))
            except Exception: bad += 1
    else: out["problems"].append("no tracker.jsonl")
    out.update(lines=len(lines), bad_json=bad, keys=dict(Counter(k for l in lines for k in l)))
    ts = [l["ts"] for l in lines if isinstance(l.get("ts"), (int, float))]
    if ts:
        if ts != sorted(ts): out["problems"].append("ts not monotonic")
        if len(ts) > 2:
            d = [b - a_ for a_, b in zip(ts, ts[1:])]; out["median_interval_s"] = round(statistics.median(d), 3); out["max_gap_s"] = round(max(d), 3)
    for k in ("cpu", "cpu_pct", "rss_mb", "mem_mb"):
        v = [l[k] for l in lines if isinstance(l.get(k), (int, float))]
        if v: out[f"{k}_max"] = max(v); out[f"{k}_median"] = statistics.median(v)
    out["method_mix"] = dict(Counter(str(l.get("method")) for l in lines if "method" in l))
    for i, l in enumerate(lines):
        if l.get("method") == "doc" and not l.get("rect") and l.get("visible") is not False: out["problems"].append(f"#{i} doc frame without rect")
        if any(isinstance(v, str) and ("/Users/" in v or v.endswith((".pdf", ".md"))) for v in l.values()): out["problems"].append(f"#{i} raw path in tracker log")
    out["missing_wanted_keys"] = [k for k in WANT if k not in out["keys"]]
    out["last"] = lines[-1] if lines else None
    for f in ("track.json", "overlay.json", os.path.join("logs", "hs.log")):
        if os.path.exists(os.path.join(a.home, f)): out["problems"].append(f"{f} written (removed file)")
    for i, l in enumerate(lines):
        if l.get("event") in ("start", "exit") and l.get("overlay", "swift-panel") != "swift-panel": out["problems"].append(f"#{i} overlay {l.get('overlay')} (always swift-panel)")
    print(json.dumps(out, indent=2, default=str))
if __name__ == "__main__": main()
