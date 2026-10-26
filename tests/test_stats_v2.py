"""Reading log metrics, schema 2 (phase7-usable-app.md A2, A4-A7): definitions, the field whitelist, problems, the
document library join, and speed on a large tracker log. Pure stdlib; temp SPEAKEASY_HOME only."""
import datetime, json, os, subprocess, sys, tempfile, time
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)
import stats

NOW = datetime.datetime(2026, 10, 26, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-7)))
SECRET = "Quantum zebra manuscripts"

def ts(days=0, hours=0):
    return (NOW - datetime.timedelta(days=days, hours=hours)).strftime("%Y-%m-%dT%H:%M:%S%z")

def read(**kw):
    r = {"ts": ts(), "command": "speak", "app": "menubar", "dry_run": False, "audio_s": 60, "words_spoken": 175}
    r.update(kw); return r

def fixture():
    return [
        read(tts_first_audio_ms=2300, llm_provider="openrouter", llm_provider_configured="openrouter", fig_ids=["p5:fig1"], end="finished",
             doc_path_hash="aaaaaaaaaaaa", page=5, sent_spoken=12, sent_n=12, text=SECRET, input=SECRET, output=SECRET, error="llm: " + SECRET, notes=SECRET),
        read(ts=ts(hours=1), tts_first_audio_ms=4000, fig_ids=["p5:fig1"], end="stopped", doc_path_hash="aaaaaaaaaaaa", page=5, sent_spoken=2, sent_n=9),
        read(ts=ts(days=1, hours=1), tts_first_audio_ms=12000, fig_ids=["p5:fig1", "p6:tab1"], end="finished", doc_path_hash="aaaaaaaaaaaa", page=6,
             tts_requested="kokoro", tts="say"),
        read(ts=ts(days=2), tts_first_audio_ms=6000, llm_provider="local", llm_provider_configured="openrouter", doc_path_hash="bbbbbbbbbbbb", page=2,
             backend="fallback", llm_error_class="URLError", doc_parse={"layout": {"src": "heuristic", "reason": "model-missing"}}),
        read(ts=ts(days=3), command="selection", app="cli", tts_first_audio_ms=5000),                       # old record: no new fields
        read(ts=ts(days=20), tts_first_audio_ms=1000),                                                       # outside the 7-day window
        read(dry_run=True), {"ts": ts(), "command": "scrub", "files": 2}, {"ts": ts(), "command": "rewrite"},
    ]

def test_definitions():
    s = stats.compute(fixture(), tracker=[], app=[], library={}, window=7, now=NOW)
    assert s["schema"] == 2 and s["window_days"] == 7
    assert s["reads"] == {"today": 2, "window": 5}, s["reads"]
    assert s["listened_min"] == {"today": 2.0, "window": 5.0}
    fa = s["first_audio_s"]
    assert fa["median"] == 5.0 and fa["p90"] == 12.0 and fa["n"] == 5, fa
    assert fa["by_provider"] == {"local": {"median": 5.5, "n": 4}, "openrouter": {"median": 2.3, "n": 1}}, fa["by_provider"]
    assert s["items"] == {"figures": 1, "tables": 1}, s["items"]                  # the same figure read three times counts once
    assert s["completion"] == {"finished": 2, "stopped": 1, "error": 0}
    assert s["speech_wpm"] == 175
    a = next(d for d in s["documents"] if d["doc"] == "aaaaaaaaaaaa")
    assert a["pages"] == [5, 6] and a["items"] == ["p5:fig1", "p6:tab1"] and a["reads"] == 3, a
    assert a["resume"] == {"page": 5, "fig_id": "p5:fig1", "sentence": 12, "of": 12}, a["resume"]
    assert s["documents"][0]["doc"] == "aaaaaaaaaaaa"                              # newest first
    assert s["since"] == {"items": "2026-10-25", "completion": "2026-10-25"}, s["since"]

def test_removed_keys_and_no_text():
    s = stats.compute(fixture(), tracker=None, now=NOW)
    out = json.dumps(s)
    for k in ("time_saved", "streak", "longest", "lists_read", "words_in", '"days"'):
        assert k not in out, k
    assert SECRET not in out and SECRET not in "\n".join(stats.text_lines(s))
    assert s["highlight"] is None                                                  # no tracker log: no highlight data

def test_problems():
    trk = [{"ts": NOW.timestamp() - 60, "event": "draw-failed", "tracker_pid": 9}, {"ts": NOW.timestamp() - 50, "event": "draw-failed", "tracker_pid": 9},
           {"ts": NOW.timestamp() - 40, "event": "exit", "reason": "no_screen_recording", "tracker_pid": 10}]
    s = stats.compute(fixture(), tracker=trk, now=NOW)
    got = {p["id"]: (p["count"], p["open"]) for p in s["problems"]}
    assert got == {"kokoro-down": (1, False), "model-down": (1, False), "cloud-fallback": (1, False), "layout-missing": (1, False),
                   "highlight-failed": (2, True), "slow-start": (1, False)}, got
    assert [p["id"] for p in s["problems"]] == [p for p in stats.PROBLEM_IDS if p in got]

def test_highlight_counts():
    t = NOW.timestamp()
    trk = [{"ts": t, "event": "cue_result", "landed": True, "hidden": False}, {"ts": t, "event": "cue_result", "landed": False, "hidden": True},
           {"ts": t - 40 * 86400, "event": "cue_result", "landed": True, "hidden": False}]
    app = [{"ts": t, "event": "user_flag"}, {"ts": t, "event": "bar_state"}]
    runs = [read(focus_methods={"band": 4, "hold": 1, "none": 2}, sentence_map={"sentences": 9})]
    s = stats.compute(runs, tracker=trk, app=app, now=NOW)
    assert s["highlight"] == {"shows": 2, "landed": 1, "hidden": 1, "flagged": 1, "no_cue_sentences": 3, "sentences": 9}, s["highlight"]

def run_cli(home, *args, env=None):
    e = dict(os.environ, SPEAKEASY_HOME=home, SPEAKEASY_LLM_URL="http://127.0.0.1:9/v1", **(env or {}))
    return subprocess.run([sys.executable, os.path.join(SRC, "speakeasy.py"), *args], capture_output=True, text=True, env=e, timeout=60)

def write_home(runs, tracker=None, library=None):
    h = tempfile.mkdtemp(); os.makedirs(os.path.join(h, "logs"))
    with open(os.path.join(h, "logs", "runs.jsonl"), "w") as f: f.write("".join(json.dumps(r) + "\n" for r in runs) + "{not json\n")
    if tracker is not None:
        with open(os.path.join(h, "logs", "tracker.jsonl"), "w") as f: f.write("".join(json.dumps(r) + "\n" for r in tracker))
    if library is not None:
        with open(os.path.join(h, "library.json"), "w") as f: json.dump(library, f)
    return h

def test_library_join_never_prints_paths():
    now = datetime.datetime.now().astimezone()
    runs = [dict(read(), ts=now.strftime("%Y-%m-%dT%H:%M:%S%z"), doc_path_hash="aaaaaaaaaaaa", page=3)]
    h = write_home(runs, library={"aaaaaaaaaaaa": {"name": "On Figures", "path": "/Users/someone/papers/secret-path.pdf"}})
    p = run_cli(h, "stats", "--json")
    assert p.returncode == 0, p.stderr
    s = json.loads(p.stdout)
    assert s["documents"][0]["name"] == "On Figures" and "secret-path" not in p.stdout and s["skipped_lines"] == 1
    off = json.loads(run_cli(h, "stats", "--json", env={"SPEAKEASY_DOC_NAMES": "off"}).stdout)
    assert off["documents"][0]["name"] is None
    txt = run_cli(h, "stats")
    assert txt.returncode == 0 and len(txt.stdout.strip().splitlines()) == 5, txt.stdout

def test_empty_home_and_window_flag():
    h = tempfile.mkdtemp()
    p = run_cli(h, "stats", "--json", "--window", "30")
    s = json.loads(p.stdout)
    assert p.returncode == 0 and s["window_days"] == 30 and s["reads"] == {"today": 0, "window": 0} and s["documents"] == [] and s["highlight"] is None

def test_speed_on_large_logs():
    now = datetime.datetime.now().astimezone()
    runs = [dict(read(), ts=(now - datetime.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S%z"), tts_first_audio_ms=3000 + i) for i in range(120)]
    t = time.time()
    trk = [{"ts": t, "event": "frame", "seq": i, "scan": True, "visible": True, "rect": [1, 2, 3, 4]} for i in range(30000)]
    trk += [{"ts": t, "event": "cue_result", "landed": True, "hidden": False} for _ in range(500)]
    h = write_home(runs, tracker=trk)
    t0 = time.time(); s = stats.load(h, 7); ms = (time.time() - t0) * 1000
    assert s["highlight"]["shows"] == 500 and s["reads"]["window"] == 120
    assert ms < 500, ms
