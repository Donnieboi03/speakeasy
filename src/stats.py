"""Reading log metrics (`speakeasy stats --json`, schema 2; docs/contracts/phase7-usable-app.md A). Reads a whitelist of
keys from each log record: never `text`, `input`, `output`, `error`, `llm_error`, `notes` or any `*_note` string."""
import datetime, json, math, os, statistics

SCHEMA = 2
RUN_KEYS = ("ts", "command", "dry_run", "app", "audio_s", "words_spoken", "timings", "tts_first_audio_ms", "llm_provider",
            "llm_provider_configured", "fig_ids", "end", "doc_path_hash", "page", "sent_spoken", "sent_n", "focus_methods",
            "sentence_map", "tts", "tts_requested", "backend", "llm_error_class", "doc_parse", "run_id")
TRACKER_KEYS = ("ts", "event", "tracker_pid", "run_id", "seq", "landed", "hidden", "reason")
APP_KEYS = ("ts", "event", "run_id")
TRACKER_EVENTS = ('"cue_result"', '"draw-failed"', '"no_screen_recording"')       # substring prefilter: 30k lines stay fast
APP_EVENTS = ('"user_flag"',)
UNREACHABLE = {"URLError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError", "TimeoutError", "timeout",
               "RemoteDisconnected", "OSError", "HTTPError"}
LAYOUT_MISSING = {"model-missing", "model-mismatch", "no-onnxruntime", "load-failed"}
PROBLEM_IDS = ("kokoro-down", "model-down", "cloud-fallback", "layout-missing", "highlight-failed", "slow-start")
SLOW_START_MS = 10000

def words(s):
    return len((s or "").split())

def kind_of(rec):
    """figure | table | list | text for one run record (written into runs.jsonl as `kind`)."""
    vb = rec.get("verbatim_blocks") or []
    if rec.get("image") or rec.get("figures_found"): return "figure"
    if "table" in vb: return "table"
    if "list" in vb: return "list"
    return "text"

# ---------------- reading the logs (whitelisted keys only) ----------------
def read_jsonl(base, keys, want=None, keep=5):
    """Records of base and its rotated copies (oldest first), only `keys`; lines without any `want` substring are skipped
    unread. Returns (records, malformed line count, file found)."""
    out, bad, found = [], 0, False
    for path in [f"{base}.{k}" for k in range(keep, 0, -1)] + [base]:
        try: f = open(path, encoding="utf-8")
        except FileNotFoundError: continue
        found = True
        with f:
            for line in f:
                if not line.strip() or (want and not any(w in line for w in want)): continue
                try: rec = json.loads(line)
                except ValueError: bad += 1; continue
                if isinstance(rec, dict): out.append({k: rec[k] for k in keys if k in rec})
                else: bad += 1
    return out, bad, found

def doc_names_on():
    return os.environ.get("SPEAKEASY_DOC_NAMES", "on").strip().lower() not in ("off", "0", "false", "no")

def read_library(home):
    """{doc_path_hash: name} from library.json; {} when names are off or the file is missing or unreadable (A6)."""
    if not doc_names_on(): return {}
    try:
        with open(os.path.join(home, "library.json"), encoding="utf-8") as f: lib = json.load(f)
    except (OSError, ValueError): return {}
    return {h: e.get("name") for h, e in lib.items() if isinstance(e, dict)} if isinstance(lib, dict) else {}

def load(home, window=7, now=None):
    logs = os.path.join(home, "logs")
    runs, bad_runs, _ = read_jsonl(os.path.join(logs, "runs.jsonl"), RUN_KEYS)
    tracker, bad_trk, has_tracker = read_jsonl(os.path.join(logs, "tracker.jsonl"), TRACKER_KEYS, TRACKER_EVENTS)
    app, bad_app, _ = read_jsonl(os.path.join(logs, "app.jsonl"), APP_KEYS, APP_EVENTS)
    return compute(runs, tracker if has_tracker else None, app, read_library(home), window, now, bad_runs + bad_trk + bad_app)

# ---------------- metrics ----------------
def _run_time(r):
    try: return datetime.datetime.strptime(str(r.get("ts")), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError: return None

def _epoch_time(ts):
    return datetime.datetime.fromtimestamp(float(ts)).astimezone() if isinstance(ts, (int, float)) else None

def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S%z")

def _first_audio_ms(r):
    v = r.get("tts_first_audio_ms", (r.get("timings") or {}).get("tts_first_audio_ms"))
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

def _median_s(ms):
    return round(statistics.median(ms) / 1000, 1)

def _p90_s(ms):
    s = sorted(ms)
    return round(s[max(0, math.ceil(0.9 * len(s)) - 1)] / 1000, 1)

def _is_read(r):
    return r.get("command") in ("speak", "selection") and not r.get("dry_run") and (r.get("app") or "cli") in ("menubar", "cli")

def _problem_hits(r):
    """Problem ids one read shows (A2.1), from fields that hold enums and class names only."""
    hits = []
    if r.get("tts_requested") == "kokoro" and r.get("tts") == "say": hits.append("kokoro-down")
    if r.get("backend") == "fallback" and r.get("llm_error_class") in UNREACHABLE: hits.append("model-down")
    if r.get("llm_provider_configured") == "openrouter" and r.get("llm_provider") == "local": hits.append("cloud-fallback")
    lay = (r.get("doc_parse") or {}).get("layout") or {}
    if lay.get("src") == "heuristic" and lay.get("reason") in LAYOUT_MISSING: hits.append("layout-missing")
    fa = _first_audio_ms(r)
    if fa is not None and fa > SLOW_START_MS: hits.append("slow-start")
    return hits

def compute(runs, tracker=None, app=None, library=None, window=7, now=None, skipped=0):
    now = now or datetime.datetime.now().astimezone()
    today = now.date()
    first_day = today - datetime.timedelta(days=window - 1)
    library = library or {}
    reads = []
    for r in runs:
        t = _run_time(r)
        if t is not None and _is_read(r): reads.append((t, r))
    win = [(t, r) for t, r in reads if first_day <= t.date() <= today]
    day = [(t, r) for t, r in win if t.date() == today]
    listened = lambda rs: round(sum(float(r.get("audio_s") or 0) for _, r in rs) / 60, 1)

    fa = [(t, r, _first_audio_ms(r)) for t, r in win if _first_audio_ms(r) is not None]
    by_provider = {}
    for _, r, ms in fa: by_provider.setdefault(r.get("llm_provider") or "local", []).append(ms)
    trend_from = today - datetime.timedelta(days=13)
    per_day = {}
    for t, r in reads:
        ms = _first_audio_ms(r)
        if ms is not None and trend_from <= t.date() <= today: per_day.setdefault(t.date().isoformat(), []).append(ms)
    first_audio = {"median": _median_s([m for *_, m in fa]) if fa else None, "p90": _p90_s([m for *_, m in fa]) if fa else None, "n": len(fa),
                   "by_provider": {p: {"median": _median_s(v), "n": len(v)} for p, v in sorted(by_provider.items())},
                   "trend": [{"date": d, "median": _median_s(v)} for d, v in sorted(per_day.items())]}

    items = {(r.get("doc_path_hash"), f) for _, r in win for f in r.get("fig_ids") or []}
    completion = {"finished": 0, "stopped": 0, "error": 0}
    for _, r in win:
        if r.get("end") in completion: completion[r["end"]] += 1
    audio = sum(float(r.get("audio_s") or 0) for _, r in win)
    spoken = sum(int(r.get("words_spoken") or 0) for _, r in win)

    highlight = None
    if tracker is not None:
        results = [e for e in tracker if e.get("event") == "cue_result" and _in_window(e, first_day, today)]
        flags = [e for e in (app or []) if e.get("event") == "user_flag" and _in_window(e, first_day, today)]
        mapped = [r for _, r in win if isinstance(r.get("focus_methods"), dict)]
        highlight = {"shows": len(results), "landed": sum(bool(e.get("landed")) for e in results), "hidden": sum(bool(e.get("hidden")) for e in results),
                     "flagged": len(flags),
                     "no_cue_sentences": sum(int(r["focus_methods"].get("hold", 0)) + int(r["focus_methods"].get("none", 0)) for r in mapped),
                     "sentences": sum(int((r.get("sentence_map") or {}).get("sentences") or 0) for _, r in win)}

    docs = {}
    for t, r in sorted(win, key=lambda x: x[0]):
        h = r.get("doc_path_hash")
        if not h: continue
        d = docs.setdefault(h, {"doc": h, "name": library.get(h), "pages": set(), "items": [], "reads": 0, "last_ts": None, "resume": None})
        if isinstance(r.get("page"), int): d["pages"].add(r["page"])
        for f in r.get("fig_ids") or []:
            if f not in d["items"]: d["items"].append(f)
        d["reads"] += 1; d["last_ts"] = r.get("ts")
        ids = r.get("fig_ids") or []
        d["resume"] = {"page": r.get("page"), "fig_id": ids[-1] if ids else None, "sentence": r.get("sent_spoken"), "of": r.get("sent_n")}
    documents = sorted(({**d, "pages": sorted(d["pages"]), "items": sorted(d["items"])} for d in docs.values()),
                       key=lambda d: _run_time({"ts": d["last_ts"]}) or now, reverse=True)

    return {"schema": SCHEMA, "window_days": window,
            "reads": {"today": len(day), "window": len(win)},
            "listened_min": {"today": listened(day), "window": listened(win)},
            "first_audio_s": first_audio,
            "items": {"figures": sum(":fig" in f for _, f in items), "tables": sum(":tab" in f for _, f in items)},
            "completion": completion, "highlight": highlight,
            "speech_wpm": round(spoken / audio * 60) if audio > 0 else None,
            "documents": documents,
            "problems": problems(win, tracker or [], first_day, today, now),
            "since": _since(win, tracker),
            "skipped_lines": skipped}

def _in_window(e, first_day, today):
    t = _epoch_time(e.get("ts"))
    return t is not None and first_day <= t.date() <= today

def problems(win, tracker, first_day, today, now):
    """[{id, count, last_ts, open}] in A2.1 order; open = seen in the last 24 h (the app adds its live checks)."""
    seen = {}
    for t, r in win:
        for pid in _problem_hits(r): seen.setdefault(pid, []).append(t)
    failed_runs = {}
    for e in tracker:
        if (e.get("event") == "draw-failed" or (e.get("event") == "exit" and e.get("reason") == "no_screen_recording")) and _in_window(e, first_day, today):
            key = e.get("tracker_pid") or e.get("run_id") or e.get("ts")
            t = _epoch_time(e.get("ts"))
            failed_runs[key] = max(failed_runs.get(key, t), t)
    if failed_runs: seen["highlight-failed"] = list(failed_runs.values())
    out = []
    for pid in PROBLEM_IDS:
        if pid not in seen: continue
        last = max(seen[pid])
        out.append({"id": pid, "count": len(seen[pid]), "last_ts": _iso(last), "open": (now - last).total_seconds() <= 86400})
    return out

def _since(win, tracker):
    """First date a newer field appears, for metrics whose history is shorter than the window (A6)."""
    out = {}
    for metric, field in (("items", "fig_ids"), ("completion", "end")):
        have = [t for t, r in win if field in r]
        if have and len(have) < len(win): out[metric] = min(have).date().isoformat()
    if tracker:
        cue = [_epoch_time(e.get("ts")) for e in tracker if e.get("event") == "cue_result"]
        cue = [t for t in cue if t is not None]
        if cue and win and min(cue).date() > min(t for t, _ in win).date(): out["highlight"] = min(cue).date().isoformat()
    return out

# ---------------- text output (`speakeasy stats`) ----------------
PROBLEM_TEXT = {"kokoro-down": "Kokoro is not running", "model-down": "the local model did not answer", "cloud-fallback": "OpenRouter failed",
                "layout-missing": "the PDF layout model is missing", "highlight-failed": "the highlight could not be drawn",
                "slow-start": "reads took over 10 s to start"}

def _n(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")

def text_lines(s):
    """The five lines of `speakeasy stats`: reads and minutes, first audio, items and completion, highlight, open problems."""
    fa, c, h = s["first_audio_s"], s["completion"], s["highlight"]
    prov = ", ".join(f"{'OpenRouter' if p == 'openrouter' else p.capitalize()} {v['median']} s" for p, v in fa["by_provider"].items())
    lines = [f"{_n(s['reads']['window'], 'read')}, {s['listened_min']['window']} min listened in the last {_n(s['window_days'], 'day')} "
             f"(today: {_n(s['reads']['today'], 'read')}, {s['listened_min']['today']} min)",
             f"First audio median: {fa['median'] if fa['median'] is not None else '-'} s, slowest 1 in 10: {fa['p90'] if fa['p90'] is not None else '-'} s"
             + (f" ({prov})" if prov else ""),
             f"{_n(s['items']['figures'], 'figure')}, {_n(s['items']['tables'], 'table')}; {c['finished']} finished, {c['stopped']} stopped, {_n(c['error'], 'error')}"]
    if h is None: lines.append("Highlight: no highlight data yet")
    else:
        cued = h["shows"] - h["hidden"]
        rate = f"{round(100 * h['landed'] / cued)} % on target first time" if cued > 0 else "no shows yet"
        lines.append(f"Highlight: {rate}, {h['flagged']} flagged wrong spot, {h['no_cue_sentences']} of {h['sentences']} sentences with no cue")
    open_ = [p for p in s["problems"] if p["open"]]
    lines.append("Problems: " + ("; ".join(f"{PROBLEM_TEXT[p['id']]} ({p['count']})" for p in open_) if open_ else "none open"))
    return lines
