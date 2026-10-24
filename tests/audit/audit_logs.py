#!/usr/bin/env python3
"""Audit a speakeasy runs.jsonl. Usage: audit_logs.py [PATH] [--json OUT] [--md OUT] [--max-capture-frac 0.5 --screen 3024x1964]"""
import json, os, sys, argparse, statistics
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema import validate, flatten

def pct(xs, p):
    xs = sorted(xs); return None if not xs else xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))]

def rect_area(r):
    if r is None: return None
    if isinstance(r, str): r = [float(v) for v in r.replace("x", ",").split(",")]
    if isinstance(r, dict): r = [r.get("x", 0), r.get("y", 0), r.get("w") or r.get("width"), r.get("h") or r.get("height")]
    return float(r[2]) * float(r[3]) if len(r) >= 4 else None

def audit(path, screen=None, max_frac=0.5, slow_ms=30000, llm_reachable=None):
    lines = []; bad_json = 0
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw: continue
        try: lines.append(flatten(json.loads(raw)))
        except Exception: bad_json += 1
    runs, anomalies = [], []
    stages = ["copy_ms", "capture_ms", "vision_ms", "llm_ms", "tts_synth_ms", "tts_first_audio_ms", "speak_ms", "total_ms"]
    for i, l in enumerate(lines):
        flags = validate(l)
        if (l.get("total_ms") or 0) > slow_ms: flags.append(f"slow:{l['total_ms']:.0f}ms")
        reach = llm_reachable if llm_reachable is not None else bool(l.get("ollama_models"))
        if l.get("backend") == "fallback" and reach and l.get("input_chars"): flags.append("fallback_while_llm_reachable")
        if l.get("tts_requested") and l.get("tts") and l["tts"] != l["tts_requested"]: flags.append(f"tts_fallback:{l['tts_requested']}->{l['tts']}")
        if isinstance(l.get("tts_first_audio_ms"), (int, float)) and l["tts_first_audio_ms"] > 3000: flags.append(f"slow_first_audio:{l['tts_first_audio_ms']:.0f}ms")
        if l.get("error"): flags.append(f"error:{str(l['error'])[:60]}")
        a = rect_area(l.get("capture_rect"))
        if a and screen:
            sw, sh = map(int, screen.split("x")); frac = a / (sw * sh)
            if frac > max_frac: flags.append(f"capture_large:{frac:.0%}_of_screen")
        if l.get("image_px") and l.get("capture_method") in ("highlight", "bounds") and screen and l["image_px"] == screen:
            flags.append("capture_is_full_screen")
        runs.append({"i": i, "ts": l.get("ts"), "command": l.get("command"), "backend": l.get("backend"),
                     "capture_method": l.get("capture_method"), "image_px": l.get("image_px"), "image_sent_px": l.get("image_sent_px"),
                     "peak_rss_mb": l.get("peak_rss_mb"), "stages": {s: l.get(s) for s in stages if l.get(s) is not None}, "flags": flags})
        if flags: anomalies.append({"i": i, "ts": l.get("ts"), "flags": flags})
    lat = {s: [l[s] for l in lines if isinstance(l.get(s), (int, float))] for s in stages}
    mem = [m for l in lines for m in (l.get("ollama_models") or []) if isinstance(m, dict)]
    rep = {"path": path, "runs": len(lines), "bad_json_lines": bad_json,
           "latency_ms": {s: {"n": len(v), "p50": pct(v, 50), "p90": pct(v, 90), "p99": pct(v, 99), "max": max(v) if v else None} for s, v in lat.items()},
           "backend_mix": dict(Counter(l.get("backend") for l in lines)), "command_mix": dict(Counter(l.get("command") for l in lines)),
           "capture_method_mix": dict(Counter(l.get("capture_method") for l in lines if "capture_method" in l)),
           "image_sizes": dict(Counter(f"{l.get('image_px')}->{l.get('image_sent_px')}" for l in lines if l.get("image"))),
           "tts_mix": dict(Counter(f"{l.get('tts_requested')}->{l.get('tts')}" for l in lines if l.get("tts"))), "tts_server_rss_mb_max": max([l["tts_server_rss_mb"] for l in lines if isinstance(l.get("tts_server_rss_mb"), (int, float))], default=None),
           "errors": [l.get("error") for l in lines if l.get("error")],
           "peak_rss_mb": {"max": max([l["peak_rss_mb"] for l in lines if isinstance(l.get("peak_rss_mb"), (int, float))], default=None)},
           "ollama_models_seen": sorted({json.dumps({k: m.get(k) for k in ("name", "model", "size", "size_vram", "size_mb", "size_vram_mb") if k in m}) for m in mem}),
           "anomalies": anomalies, "timeline": runs}
    return rep

def to_md(r):
    o = [f"# speakeasy log audit", f"- file: `{r['path']}`  runs: {r['runs']}  bad JSON lines: {r['bad_json_lines']}",
         f"- backend mix: {r['backend_mix']}  commands: {r['command_mix']}", f"- capture methods: {r['capture_method_mix']}",
         f"- image sizes (orig->sent): {r['image_sizes']}", f"- TTS (requested->used): {r['tts_mix']}  TTS server RSS max: {r['tts_server_rss_mb_max']} MB", f"- peak RSS max: {r['peak_rss_mb']['max']} MB",
         f"- Ollama models seen: {r['ollama_models_seen'] or 'none logged'}", "", "## Latency (ms)", "| stage | n | p50 | p90 | p99 | max |", "|---|---|---|---|---|---|"]
    o += [f"| {s} | {v['n']} | {v['p50']} | {v['p90']} | {v['p99']} | {v['max']} |" for s, v in r["latency_ms"].items()]
    o += ["", f"## Anomalies ({len(r['anomalies'])})"] + [f"- run {a['i']} @ {a['ts']}: {', '.join(a['flags'])}" for a in r["anomalies"][:200]]
    o += ["", "## Timeline", "| # | ts | cmd | backend | capture | img | stages |", "|---|---|---|---|---|---|---|"]
    o += [f"| {t['i']} | {t['ts']} | {t['command']} | {t['backend']} | {t['capture_method']} | {t['image_px']} | {' '.join(f'{k[:-3]}={v:.0f}' for k, v in t['stages'].items())} |" for t in r["timeline"][-100:]]
    return "\n".join(o) + "\n"

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("path", nargs="?", default=os.path.expanduser(os.environ.get("SPEAKEASY_HOME", "~/.speakeasy")) + "/logs/runs.jsonl")
    ap.add_argument("--json"); ap.add_argument("--md"); ap.add_argument("--screen"); ap.add_argument("--max-capture-frac", type=float, default=0.5)
    ap.add_argument("--slow-ms", type=float, default=30000)
    a = ap.parse_args(); r = audit(a.path, a.screen, a.max_capture_frac, a.slow_ms)
    if a.json: json.dump(r, open(a.json, "w"), indent=2)
    md = to_md(r)
    if a.md: open(a.md, "w").write(md)
    print(md if not a.md else md.split("## Latency")[0] + f"anomalies: {len(r['anomalies'])}  (full: {a.md})")
