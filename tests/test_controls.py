"""Pause / next / stop from the app (phase7-usable-app.md B6) and the run fields they leave (A3: end, sent_n, sent_spoken,
paused_ms, skips), plus the live state.json fields. A stub `say` (sleeps, makes no sound) stands in for speech; the LLM
URL is dead so the deterministic reader speaks. Temp SPEAKEASY_HOME only."""
import json, os, signal, subprocess, sys, tempfile, time
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
TEXT = "\n".join(f"Sentence number {w} talks about the quiet harbour town at dusk." for w in ("one", "two", "three", "four", "five"))

def start(home, app="menubar"):
    bin_dir = os.path.join(home, "stubbin"); os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, "say"), "w") as f: f.write("#!/bin/sh\nsleep 0.6\n")
    os.chmod(os.path.join(bin_dir, "say"), 0o755)
    env = dict(os.environ, SPEAKEASY_HOME=home, SPEAKEASY_LLM_URL="http://127.0.0.1:9/v1", SPEAKEASY_TTS="say", SPEAKEASY_PREWARM="0",
               SPEAKEASY_DOC_READ="off", SPEAKEASY_TRACK="off", PATH=bin_dir + os.pathsep + os.environ["PATH"])
    env.pop("SPEAKEASY_LOG_TEXT", None)
    args = [sys.executable, os.path.join(SRC, "speakeasy.py"), "speak"] + (["--app", app] if app else []) + ["--capture-method", "none", "--capture-rect", "0,0,800,600"]
    p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, text=True)
    p.stdin.write(TEXT); p.stdin.close()
    return p

def live(home):
    try:
        with open(os.path.join(home, "state.json")) as f: return json.load(f)
    except (OSError, ValueError): return {}

def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False

def last_run(home):
    with open(os.path.join(home, "logs", "runs.jsonl")) as f: return json.loads(f.readlines()[-1])

def test_pause_next_stop():
    home = tempfile.mkdtemp(); p = start(home)
    assert wait_for(lambda: live(home).get("state") == "speaking" and live(home).get("sent_i", 0) >= 1), live(home)
    s = live(home)
    assert s["run_id"] and isinstance(s["started_ms"], int) and s["paused"] is False and "label" in s and s["cloud"] is False, s
    p.send_signal(signal.SIGUSR2)
    assert wait_for(lambda: live(home).get("paused") is True, 5)
    held = live(home)["sent_i"]; time.sleep(1.6)
    assert live(home)["sent_i"] == held                                        # nothing new started while paused
    p.send_signal(signal.SIGUSR2)
    assert wait_for(lambda: live(home).get("paused") is False, 5)
    time.sleep(0.1); p.send_signal(signal.SIGUSR1)                              # skip the sentence playing now
    assert wait_for(lambda: live(home).get("sent_i", 0) >= held + 1, 5)
    p.send_signal(signal.SIGTERM)
    assert p.wait(20) == 0
    r = last_run(home)
    assert r["end"] == "stopped" and r["paused_ms"] >= 1500 and r["skips"] == 1, {k: r.get(k) for k in ("end", "paused_ms", "skips")}
    assert r["sent_n"] == 5 and 2 <= r["sent_spoken"] < 5 and r["tts_fallback"] is None, (r["sent_n"], r["sent_spoken"])
    assert r["audio_s"] * 1000 <= r["timings"]["speak_ms"] - 1500 + 100         # audio_s excludes the pause
    assert live(home).get("state") == "idle" and set(live(home)) <= {"state", "run_id", "t"}, live(home)

def test_finished_read_counts_every_sentence():
    home = tempfile.mkdtemp(); p = start(home)
    assert p.wait(60) == 0
    r = last_run(home)
    assert r["end"] == "finished" and r["sent_spoken"] == r["sent_n"] == 5 and r["skips"] == 0 and r["paused_ms"] == 0, r
    assert r["fig_ids"] == [] and "llm_error_class" in r

def test_resume_after_the_player_died_acts_as_next():
    home = tempfile.mkdtemp(); p = start(home)
    assert wait_for(lambda: live(home).get("state") == "speaking" and live(home).get("sent_i", 0) >= 1), live(home)
    p.send_signal(signal.SIGUSR2)
    assert wait_for(lambda: live(home).get("paused") is True, 5)
    kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
    assert kids, "no player process"
    for k in kids: os.killpg(int(k), signal.SIGKILL)                           # the player's process group dies while paused
    time.sleep(0.3); p.send_signal(signal.SIGUSR2)
    assert p.wait(60) == 0
    r = last_run(home)
    assert r["end"] == "finished" and r["skips"] == 1 and r["sent_spoken"] == r["sent_n"] == 5 and not r.get("tts_error"), \
        {k: r.get(k) for k in ("end", "skips", "sent_spoken", "sent_n", "tts_error")}

def test_cli_reads_write_live_state():
    home = tempfile.mkdtemp()
    env = dict(os.environ, SPEAKEASY_HOME=home, SPEAKEASY_LLM_URL="http://127.0.0.1:9/v1", SPEAKEASY_DOC_READ="off")
    subprocess.run([sys.executable, os.path.join(SRC, "speakeasy.py"), "speak", "--dry-run"], input="One short line.", text=True,
                   capture_output=True, env=env, timeout=60)
    assert live(home).get("state") in (None, "idle")                            # a dry run never shows as a read
    p = start(home, app=None)                                                   # a CLI read: the bar follows it too
    assert wait_for(lambda: live(home).get("state") == "speaking" and live(home).get("sent_i", 0) >= 1), live(home)
    assert p.wait(60) == 0 and live(home).get("state") == "idle"

def test_signals_before_speech_do_not_kill_the_read():
    home = tempfile.mkdtemp(); p = start(home)
    assert wait_for(lambda: os.path.exists(os.path.join(home, "state.json")), 10)
    p.send_signal(signal.SIGUSR1)                                               # "next" while preparing: ignored
    assert p.wait(60) == 0 and last_run(home)["end"] == "finished"
