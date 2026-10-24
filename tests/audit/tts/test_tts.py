#!/usr/bin/env python3
"""Black-box TTS / config / dry_run / stop-cleanup tests using a mock TTS server and afplay/say stubs."""
import glob, json, os, signal, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
from schema import validate, flatten
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../src/speakeasy")); BAD = "http://127.0.0.1:9/v1"
PORT = 18880; BASE_URL = f"http://127.0.0.1:{PORT}"; TTS_URL = BASE_URL + "/v1/audio/speech"; R = []
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:260]})
def jl(p): return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []
LONG = " ".join(f"Paragraph {i} explains that stage {i} of the pipeline filters the signal and passes it on to the next stage for further processing." for i in range(1, 9))

def env(**kw):
    h = tempfile.mkdtemp(prefix="se_tts_"); tmp = os.path.join(h, "tmp"); os.makedirs(tmp)
    e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, TMPDIR=tmp, SPEAKEASY_LLM_URL=BAD, PATH=os.path.join(HERE, "stubs") + ":" + os.environ["PATH"],
             STUB_LOG=os.path.join(h, "stub.jsonl"), STUB_PLAY_SEC="0.6")
    for k in ("SPEAKEASY_TTS", "SPEAKEASY_TTS_URL", "SPEAKEASY_LOG_TEXT", "SPEAKEASY_MODEL"): e.pop(k, None)
    e.update({k: str(v) for k, v in kw.items()}); return h, e
def run(args, inp, e, timeout=120):
    t = time.time(); p = subprocess.run([BIN] + args, input=inp, capture_output=True, text=True, env=e, timeout=timeout); return p.returncode, p.stdout, p.stderr, time.time() - t
def last(h): L = jl(os.path.join(h, "logs", "runs.jsonl")); return flatten(L[-1]) if L else {}

mlog = tempfile.mktemp(suffix=".jsonl")
srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_tts.py")], env=dict(os.environ, MOCK_PORT=str(PORT), MOCK_LOG=mlog, MOCK_DELAY="0.3"))
time.sleep(1)
try:
    # A. dry_run boolean on speak/selection
    h, e = env(); run(["speak", "--dry-run"], "Hello there.", e); l = last(h)
    add("speak_dry_run_field_true", l.get("dry_run") is True, l.get("dry_run"))
    h, e = env(); run(["selection", "--capture-method", "none", "--dry-run"], "", e); l = last(h)
    add("selection_dry_run_field_true", l.get("dry_run") is True, l.get("dry_run"))
    # B. empty input -> backend none
    for cmd in (["rewrite"], ["speak", "--dry-run"], ["selection", "--capture-method", "none", "--dry-run"]):
        h, e = env(); rc, out, err, _ = run(cmd, "", e); l = last(h)
        add(f"empty_{cmd[0]}_backend_none", l.get("backend") == "none" and l.get("llm_ms") is None and l.get("model") is None and rc == 0,
            f"rc={rc} backend={l.get('backend')} llm_ms={l.get('llm_ms')} model={l.get('model')}")
    # C. kokoro happy path (real speak, stubbed afplay)
    h, e = env(SPEAKEASY_TTS="kokoro", SPEAKEASY_TTS_URL=TTS_URL)
    open(mlog, "w").close(); before = set(os.listdir(e["TMPDIR"]))
    rc, out, err, dt = run(["speak"], LONG, e); l = last(h); reqs = jl(mlog); plays = [s for s in jl(e["STUB_LOG"]) if s["tool"] == "afplay"]
    says = [s for s in jl(e["STUB_LOG"]) if s["tool"] == "say"]
    add("kokoro_exit0", rc == 0, f"rc={rc} {err[:150]}")
    add("kokoro_posted_to_audio_speech", reqs and all(r["path"].rstrip("/").endswith("/audio/speech") for r in reqs), [r["path"] for r in reqs][:3])
    add("kokoro_request_openai_shape", reqs and all(isinstance(r["req"].get("input"), str) and r["req"]["input"] and "model" in r["req"] for r in reqs), str(reqs[0]["req"] if reqs else None)[:200])
    add("kokoro_afplay_used", len(plays) >= 1, f"{len(plays)} plays")
    add("kokoro_no_say_used", not says, says[:1])
    add("kokoro_clips_existed_when_played", plays and all(p["existed"] and p["bytes"] > 44 for p in plays), [(p["existed"], p["bytes"]) for p in plays][:4])
    add("kokoro_multiple_clips_for_long_text", len(reqs) >= 2, f"{len(reqs)} requests")
    if len(reqs) >= 2 and len(plays) >= 2:
        add("kokoro_prefetch_next_during_playback", reqs[1]["t_start"] < plays[0]["end"], f"req2_start={reqs[1]['t_start']:.2f} play1_end={plays[0]['end']:.2f}")
        add("kokoro_plays_in_order_no_overlap", all(plays[i]["end"] <= plays[i+1]["start"] + 0.05 for i in range(len(plays)-1)), "")
    spoken = " ".join(r["req"].get("input", "") for r in reqs)
    add("kokoro_all_text_sent", "Paragraph 8" in spoken or "stage 8" in spoken, spoken[-120:])
    add("log_tts_kokoro", l.get("tts") == "kokoro" and l.get("tts_requested") == "kokoro", f"{l.get('tts')}/{l.get('tts_requested')}")
    add("log_dry_run_false", l.get("dry_run") is False, l.get("dry_run"))
    for k in ("tts_voice", "tts_synth_ms", "tts_first_audio_ms", "speak_ms"):
        add(f"log_{k}_present", l.get(k) not in (None, ""), l.get(k))
    add("kokoro_ok_tts_error_null", l.get("tts_error") is None, l.get("tts_error"))
    add("kokoro_ok_llm_error_set_when_llm_down", bool(l.get("llm_error")), l.get("llm_error"))
    add("error_is_stage_prefixed", isinstance(l.get("error"), str) and l["error"].lower().lstrip().startswith(("llm", "tts", "capture", "vision")), l.get("error"))
    rss = l.get("tts_server_rss_mb")
    add("tts_server_rss_mb_numeric_or_null_onbox", rss is None or (isinstance(rss, (int, float)) and 1 < rss < 20000), rss)
    add("log_tts_server_rss_mb_key", "tts_server_rss_mb" in l, l.get("tts_server_rss_mb"))
    if isinstance(l.get("tts_first_audio_ms"), (int, float)) and isinstance(l.get("speak_ms"), (int, float)):
        add("first_audio_before_speak_end", l["tts_first_audio_ms"] <= l["speak_ms"] + (l.get("total_ms") or 0), "")
        add("first_audio_under_2s_with_300ms_server", l["tts_first_audio_ms"] < 2000, l["tts_first_audio_ms"])
    add("schema_valid_kokoro_run", not validate(l), validate(l))
    leftover = set(os.listdir(e["TMPDIR"])) - before
    add("no_temp_wavs_after_normal_finish", not [f for f in leftover if f.endswith(".wav")] and not glob.glob(os.path.join(h, "**", "*.wav"), recursive=True), leftover)
    # voice passthrough
    open(mlog, "w").close(); h, e = env(SPEAKEASY_TTS="kokoro", SPEAKEASY_TTS_URL=TTS_URL)
    run(["speak", "--voice", "af_heart"], "Short one.", e); reqs = jl(mlog); l = last(h)
    add("kokoro_voice_passthrough", reqs and reqs[0]["req"].get("voice") == "af_heart" and l.get("tts_voice") == "af_heart", f"{reqs[0]['req'].get('voice') if reqs else None} / {l.get('tts_voice')}")
    # D. server down -> say fallback
    h, e = env(SPEAKEASY_TTS="kokoro", SPEAKEASY_TTS_URL="http://127.0.0.1:9")
    rc, out, err, dt = run(["speak"], "The server is down so say should speak this.", e); l = last(h); says = [s for s in jl(e["STUB_LOG"]) if s["tool"] == "say"]
    add("down_exit0", rc == 0, f"rc={rc}"); add("down_falls_back_to_say", len(says) >= 1, f"{len(says)} say calls")
    add("down_log_tts_say_requested_kokoro", l.get("tts") == "say" and l.get("tts_requested") == "kokoro", f"{l.get('tts')}/{l.get('tts_requested')}")
    add("down_log_error_set", bool(l.get("error")), l.get("error"))
    add("down_tts_error_set", bool(l.get("tts_error")), l.get("tts_error"))
    add("down_error_mentions_tts_stage", "tts" in str(l.get("error")).lower(), l.get("error")); add("down_stderr_warning", err.strip(), err[:120])
    add("down_fast_fallback_under_5s", dt < 5, f"{dt:.1f}s")
    # server returns 404 (wrong path) -> also fallback
    h, e = env(SPEAKEASY_TTS="kokoro", SPEAKEASY_TTS_URL=BASE_URL + "/wrongprefix")
    rc, out, err, dt = run(["speak"], "Wrong path test.", e); l = last(h)
    add("http_error_falls_back_to_say", rc == 0 and l.get("tts") == "say" and l.get("error"), f"rc={rc} tts={l.get('tts')} err={l.get('error')}")
    # base URL (like SPEAKEASY_LLM_URL style) should also work
    for bu in (BASE_URL, BASE_URL + "/v1"):
        open(mlog, "w").close(); h, e = env(SPEAKEASY_TTS="kokoro", SPEAKEASY_TTS_URL=bu); run(["speak"], "Base URL test.", e); l = last(h)
        add(f"accepts_base_url_{bu.split(str(PORT))[1] or 'root'}", l.get("tts") == "kokoro", f"tts={l.get('tts')} paths={[r['path'] for r in jl(mlog)]}")
    # E. stop mid-playback leaves no afplay / wavs
    h, e = env(SPEAKEASY_TTS="kokoro", SPEAKEASY_TTS_URL=TTS_URL, STUB_PLAY_SEC="5")
    p = subprocess.Popen([BIN, "speak"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=e)
    p.stdin.write(LONG); p.stdin.close()
    t0 = time.time()
    while time.time() - t0 < 25 and not subprocess.run(["pgrep", "-f", os.path.join(HERE, "stubs", "afplay")], capture_output=True).stdout: time.sleep(0.2)
    playing = bool(subprocess.run(["pgrep", "-f", os.path.join(HERE, "stubs", "afplay")], capture_output=True).stdout)
    add("stop_test_playback_started", playing, "")
    rc, _, err, _ = run(["stop"], "", e); time.sleep(1.5)
    add("stop_exit0", rc == 0, f"rc={rc} {err[:80]}")
    left = subprocess.run(["pgrep", "-af", os.path.join(HERE, "stubs", "afplay")], capture_output=True, text=True).stdout
    add("stop_no_afplay_left", not left.strip(), left[:150])
    try: p.wait(5); add("stop_speak_process_exits", True, f"rc={p.returncode}")
    except subprocess.TimeoutExpired: add("stop_speak_process_exits", False, "still running"); p.kill()
    wavs = [f for f in os.listdir(e["TMPDIR"]) if f.endswith(".wav")] + glob.glob(os.path.join(h, "**", "*.wav"), recursive=True)
    add("stop_no_temp_wavs_left", not wavs, wavs[:5])
    reqs_after = len(jl(mlog)); time.sleep(2); add("stop_no_more_synth_after_stop", len(jl(mlog)) == reqs_after, "")
    # stop while say is speaking
    h, e = env(STUB_PLAY_SEC="5"); p = subprocess.Popen([BIN, "speak"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=e)
    p.stdin.write("Say engine stop test sentence."); p.stdin.close(); time.sleep(2.5); run(["stop"], "", e); time.sleep(1)
    add("stop_kills_say", not subprocess.run(["pgrep", "-f", os.path.join(HERE, "stubs", "say")], capture_output=True).stdout, "")
    try: p.wait(5)
    except subprocess.TimeoutExpired: p.kill()
    # F. config file defaults + env precedence (format discovered black-box)
    found = None
    for fmt, body in (("KEY=VALUE", "SPEAKEASY_TTS=kokoro\nSPEAKEASY_TTS_URL={u}\n"), ("export KEY=VALUE", "export SPEAKEASY_TTS=kokoro\nexport SPEAKEASY_TTS_URL={u}\n"),
                      ("json", '{{"SPEAKEASY_TTS": "kokoro", "SPEAKEASY_TTS_URL": "{u}"}}'), ("lowercase key=value", "tts=kokoro\ntts_url={u}\n"),
                      ("toml", 'tts = "kokoro"\ntts_url = "{u}"\n')):
        h, e = env(); open(os.path.join(h, "config"), "w").write(body.format(u=TTS_URL)); open(mlog, "w").close()
        run(["speak"], "Config test.", e); l = last(h)
        if l.get("tts_requested") == "kokoro" and jl(mlog): found = (fmt, body); break
    add("config_file_sets_defaults", found, found[0] if found else "no tried format (KEY=VALUE, export, json, key=value, toml) took effect")
    if found:
        h, e = env(SPEAKEASY_TTS="say"); open(os.path.join(h, "config"), "w").write(found[1].format(u=TTS_URL)); open(mlog, "w").close()
        run(["speak"], "Env wins test.", e); l = last(h)
        add("env_overrides_config", l.get("tts_requested") == "say" and not jl(mlog), f"tts_requested={l.get('tts_requested')} mock_calls={len(jl(mlog))}")
        h, e = env(); open(os.path.join(h, "config"), "w").write(found[1].format(u=TTS_URL) + "\n# comment line\n\n"); run(["speak", "--dry-run"], "x y z.", e)
        add("config_tolerates_comments_blank", last(h).get("command") == "speak", "")
    h, e = env(); open(os.path.join(h, "config"), "w").write("this is ::: not valid {{{"); rc, out, err, _ = run(["rewrite"], "Bad config test.", e)
    add("bad_config_does_not_crash", rc == 0 and out.strip(), f"rc={rc} {err[:100]}")
finally:
    srv.terminate()
res = {"checks": R, "summary": {"pass": sum(c["pass"] for c in R), "fail": sum(not c["pass"] for c in R)}}
os.makedirs(os.path.join(os.path.dirname(HERE), "reports"), exist_ok=True)
json.dump(res, open(os.path.join(os.path.dirname(HERE), "reports", "tts.json"), "w"), indent=2)
md = ["# TTS / config tests", f"summary: {res['summary']}", "", "| check | result | detail |", "|---|---|---|"] + [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:140].replace('|','¦')} |" for c in R]
open(os.path.join(os.path.dirname(HERE), "reports", "tts.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
