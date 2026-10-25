#!/usr/bin/env python3
"""Self-test of audit_perf.py on synthetic logs (new-build contract: good + bad) so the budgets are verified before the builds land."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from audit_perf import audit
def mk(frames, exits, runs, hs=""):
    d = tempfile.mkdtemp(prefix="se_perf_")
    with open(os.path.join(d, "tracker.jsonl"), "w") as f:
        for x in frames: f.write(json.dumps({"event": "frame", **x}) + "\n")
        for x in exits: f.write(json.dumps({"event": "exit", **x}) + "\n")
    with open(os.path.join(d, "runs.jsonl"), "w") as f:
        for x in runs: f.write(json.dumps(x) + "\n")
    open(os.path.join(d, "hs.log"), "w").write(hs); return d
good = mk([{"src": "ax", "visible": True, "rect": [0, 0, 110, 20], "text_rect": [0, 0, 100, 20], "scroll_lag_ms": v} for v in (12, 18, 25, 40)] +
          [{"src": "ocr", "visible": True, "rect": [0, 0, 100, 20], "text_rect": [0, 0, 98, 20], "scroll_lag_ms": 20}, {"visible": False, "trigger": "app-switch", "hide_ms": 8}, {"visible": False, "trigger": "tab-switch", "hide_ms": 30}],
          [{"ax_hits": 9, "ocr_hits": 1, "rss_mb_peak": 60}], [{"tts_first_audio_ms": t, "ollama_models": [{"name": "qwen3-vl:4b", "size_mb": 3300}], "tts_server_rss_mb": 300, "peak_rss_mb": 40, "overlay": "swift-panel", "app": "menubar", "capture_path": None} for t in (700, 900, 1100, 1300)])
bad = mk([{"method": "ocr", "visible": True, "rect": [0, 0, 400, 20], "text_rect": [0, 0, 100, 20], "scroll_lag_ms": v} for v in (80, 120)] + [{"visible": False, "trigger": "app-switch", "hide_ms": 250}],
         [{"ax_hits": 0, "ocr_hits": 10}], [{"tts_first_audio_ms": 6000, "ollama_models": [{"name": "qwen2.5:3b", "size_mb": 2000}, {"name": "big", "size_mb": 7000}], "capture_path": "/Users/x/.speakeasy/last_capture.png"}])
R = []
g = {c["check"]: c["status"] for c in audit(good)["checks"]}; b = {c["check"]: c["status"] for c in audit(bad)["checks"]}
for k in ("scroll_lag_budget", "hide_latency_budget", "hide_on_app_or_tab_switch_observed", "box_width_tight_to_text", "source_recorded_per_visible_frame", "accessibility_first_share", "ttfa_budget", "ram_total_under_8gb", "ram_total_near_4gb_aim", "single_local_vlm_qwen3_vl", "native_swift_overlay", "menubar_app", "no_home_paths_in_runs"):
    R.append((f"good_{k}_pass", g.get(k) == "PASS", g.get(k)))
for k in ("scroll_lag_budget", "hide_latency_budget", "box_width_tight_to_text", "source_recorded_per_visible_frame", "accessibility_first_share", "ttfa_budget", "ram_total_under_8gb", "no_home_paths_in_runs"):
    R.append((f"bad_{k}_not_pass", b.get(k) in ("FAIL", "NEEDS-BUILD"), b.get(k)))
for n, ok, d in R: print(f"| {n} | {'PASS' if ok else 'FAIL'} | {d} |")
print(f"summary: {{'pass': {sum(o for _, o, _ in R)}, 'fail': {sum(not o for _, o, _ in R)}}}")

import subprocess, time
def sess(lines):
    d = tempfile.mkdtemp(prefix="se_health_"); open(os.path.join(d, "tracker.jsonl"), "w").write("".join(json.dumps(x) + "\n" for x in lines))
    open(os.path.join(d, "runs.jsonl"), "w").write(json.dumps({"ts": lines[0]["ts"], "command": "selection", "tts_first_audio_ms": 900}) + "\n"); return d
t0 = time.time(); P = 4242
healthy = sess([{"event": "start", "ts": t0, "tracker_pid": P, "window_id": 1, "build": "abc1234"},
    {"event": "draw", "ts": t0 + .1, "tracker_pid": P, "rect": [0, 0, 110, 20], "text_rect": [0, 0, 100, 20], "src": "ax"},
    {"event": "scroll", "ts": t0 + .2, "tracker_pid": P, "scroll_lag_ms": 14, "src": "ax"}, {"event": "ax", "ts": t0 + .25, "tracker_pid": P, "notif": "AXValueChanged"},
    {"event": "hide", "ts": t0 + .3, "tracker_pid": P, "trigger": "app-switch", "hide_ms": 6},
    {"event": "exit", "ts": t0 + 1, "tracker_pid": P, "reason": "sigterm", "scroll_events": 1, "ax_events": 1, "scroll_lag_avg_ms": 14, "scroll_lag_max_ms": 14}])
startonly = sess([{"event": "start", "ts": t0, "tracker_pid": P}])
H = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_health.py")
for nm, d, want in (("healthy_session_exit0", healthy, 0), ("start_only_session_flagged", startonly, 1)):
    p = subprocess.run([sys.executable, H, d], capture_output=True, text=True); ok = p.returncode == want
    print(f"| {nm} | {'PASS' if ok else 'FAIL'} | rc={p.returncode} {p.stdout.splitlines()[-1] if p.stdout else ''} |")
