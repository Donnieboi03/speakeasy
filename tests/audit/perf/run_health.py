#!/usr/bin/env python3
"""Per-run health check + run report from logs only (runs.jsonl, tracker.jsonl, focus.jsonl, hs.log).
A tracker session = all tracker.jsonl lines with one tracker_pid (exit records without tracker_pid are matched by time).
Usage: run_health.py LOGDIR [--last N] [--all] [--json out.json]     exit code 1 if the newest session is unhealthy.

Required tracker.jsonl schema (new builds):
  start : event=start, ts, tracker_pid, pid, window_id, accessibility, screen_recording, fps, run_id, build (git sha of the tracker binary)
  draw  : event=draw (or panel-shown), ts, tracker_pid, seq, rect[x,y,w,h], text_rect[x,y,w,h], src in {ax,ocr,doc,scroll-delta}, overlay=swift-panel|hammerspoon
  hide  : event=hide, ts, tracker_pid, trigger in {app-switch,tab-switch,window-switch,not-frontmost,scrolled-off,content-changed,speech-finished}, hide_ms
  scroll: event=scroll (or field on draw), ts, tracker_pid, scroll_lag_ms, src
  ax    : event=ax (AX notification handled), ts, tracker_pid, notif, ax_ms
  exit  : event=exit, ts, tracker_pid, run_id, reason (speech-finished|sigterm|window-closed|error), draws, hides, scroll_events, ax_events,
          ax_hits, ocr_hits, scroll_delta_hits, scroll_lag_avg_ms, scroll_lag_max_ms, hide_ms_avg, hide_ms_max, rss_mb_peak, cpu_pct
          (must also be written from the SIGTERM handler, before the process exits)
"""
import argparse, json, os, re, statistics, sys, datetime
from collections import Counter, OrderedDict
def jl(p):
    o = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x.startswith("{"):
                try: o.append(json.loads(x))
                except Exception: pass
    return o
def ts_of(r):
    t = r.get("ts")
    if isinstance(t, (int, float)): return float(t)
    try: return datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except Exception: return None
def hslines(p):
    o = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)", x.rstrip())
            if m: o.append((datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(), m.group(2)))
    return o
def sessions(d):
    T = jl(os.path.join(d, "tracker.jsonl")); S = OrderedDict(); orphans = []
    for l in T:
        p = l.get("tracker_pid")
        if p is None: orphans.append(l); continue
        S.setdefault(p, []).append(l)
    for o in orphans:  # attach pid-less exit to the session it falls in
        t = o.get("ts") or 0; best = None
        for p, v in S.items():
            if v[0].get("ts", 0) <= t + 1: best = p
        if best is not None: o = dict(o, _orphan=True); S[best].append(o)
    return S
def health(d, pid, L, runs, hs, nxt_start):
    st = L[0].get("ts"); en = max(x.get("ts", 0) for x in L); end_win = min(nxt_start or en + 30, en + 30)
    ev = Counter(x.get("event") for x in L); frames = [x for x in L if x.get("event") == "frame"]
    ex = [x for x in L if x.get("event") == "exit"]; e = ex[-1] if ex else {}
    draws = [x for x in L if x.get("event") in ("draw", "panel-shown")] or [x for x in frames if x.get("visible") is True]
    hides = [x for x in L if x.get("event") == "hide"] or [x for x in frames if x.get("visible") is False]
    srcs = Counter(str(x.get("src", x.get("method"))) for x in draws)
    lag = [x["scroll_lag_ms"] for x in L if isinstance(x.get("scroll_lag_ms"), (int, float))]
    hms = [x["hide_ms"] for x in L if isinstance(x.get("hide_ms"), (int, float))]
    hsw = [m for t, m in hs if st - 2 <= t <= end_win]
    hlag = [(float(a), float(b)) for m in hsw for a, b in re.findall(r"lag scroll->box avg ([\d.]+) ms max ([\d.]+) ms", m)]
    killed = [m for m in hsw if re.search(r"exited code=(15|9)\b", m)]
    run = None
    for r in runs:
        t = ts_of(r)
        if t is not None and st - 30 <= t <= st + 5: run = r
    scroll_n = e.get("scroll_events", ev.get("scroll", 0)); ax_n = e.get("ax_events", ev.get("ax", 0) + sum(1 for x in draws if x.get("src") == "ax"))
    flags = []
    if not ex: flags.append("no_exit_summary" + ("(killed: " + killed[-1] + ")" if killed else ""))
    if ex and e.get("_orphan"): flags.append("exit_without_tracker_pid")
    if not draws: flags.append("no_panel_shown_or_draw_records")
    if frames and all(x.get("reason") == "not-frontmost" for x in frames): flags.append("never_frontmost_box_never_shown")
    if not scroll_n and not lag: flags.append("zero_scroll_events")
    if not ax_n: flags.append("zero_ax_events")
    if not lag and "scroll_lag_avg_ms" not in e: flags.append("missing_lag_fields" + ("(hs.log only)" if hlag else ""))
    if draws and any("src" not in x for x in draws): flags.append("draw_without_src")
    if draws and any("text_rect" not in x for x in draws): flags.append("draw_without_text_rect")
    if hides and not hms: flags.append("hide_without_hide_ms")
    if "build" not in L[0]: flags.append("start_without_build_sha")
    if run is None: flags.append("no_matching_runs_jsonl_record")
    la = lag or [b for _, b in hlag]
    rep = OrderedDict(
        session=pid, start=datetime.datetime.fromtimestamp(st).strftime("%Y-%m-%d %H:%M:%S %Z").strip(), duration_s=round(en - st, 1),
        window_id=L[0].get("window_id"), events=dict(ev), box_shown=bool(draws), draws=len(draws), source_mix=dict(srcs),
        lag_ms={"avg": round(statistics.mean(lag), 1) if lag else (round(statistics.mean(a for a, _ in hlag), 1) if hlag else None), "max": max(la) if la else None, "from": "tracker" if lag else ("hs.log" if hlag else None)},
        hide={"n": len(hides), "hide_ms_avg": round(statistics.mean(hms), 1) if hms else None, "hide_ms_max": max(hms) if hms else None, "triggers": dict(Counter(str(x.get("trigger") or x.get("reason")) for x in hides))},
        exit_reason=e.get("reason") or (killed[-1] if killed else None),
        run={k: (run or {}).get(k) for k in ("ts", "command", "model", "tts_first_audio_ms", "focus_events", "focus_fallback", "doc_source", "verbatim_blocks", "error")} if run else None,
        healthy=not flags, flags=flags)
    return rep
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("logdir"); ap.add_argument("--last", type=int, default=1); ap.add_argument("--all", action="store_true"); ap.add_argument("--json"); a = ap.parse_args()
    S = sessions(a.logdir); runs = jl(os.path.join(a.logdir, "runs.jsonl")); hs = hslines(os.path.join(a.logdir, "hs.log"))
    keys = list(S); starts = [S[k][0].get("ts") for k in keys]
    reps = [health(a.logdir, k, S[k], runs, hs, starts[i + 1] if i + 1 < len(keys) else None) for i, k in enumerate(keys)]
    if not reps and runs:
        r = runs[-1]; reps = [{"session": None, "box_shown": False, "healthy": False, "flags": ["no_tracker_session"], "run": {k: r.get(k) for k in ("ts", "command", "tts_first_audio_ms", "focus_events")}}]
    show = reps if a.all else reps[-a.last:]
    for r in show:
        print(f"== tracker session {r['session']}  {r.get('start','')}  ({r.get('duration_s','?')} s)  {'HEALTHY' if r['healthy'] else 'UNHEALTHY'}")
        for k in ("box_shown", "draws", "source_mix", "lag_ms", "hide", "exit_reason", "run"): print(f"  {k}: {json.dumps(r.get(k), default=str)}")
        if r["flags"]: print(f"  flags: {', '.join(r['flags'])}")
    tot = Counter(f.split("(")[0] for r in reps for f in r["flags"])
    print(f"\nsessions: {len(reps)}  unhealthy: {sum(not r['healthy'] for r in reps)}  flag counts: {dict(tot)}")
    if a.json: json.dump(reps, open(a.json, "w"), indent=2, default=str)
    sys.exit(0 if reps and reps[-1]["healthy"] else 1)
if __name__ == "__main__": main()
