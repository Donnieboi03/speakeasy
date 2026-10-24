#!/usr/bin/env python3
"""Black-box: direction-aware superlative correction (via mock LLM answers), notes field, legacy /tmp file cleanup."""
import json, os, re, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); AUD = os.path.dirname(HERE); sys.path.insert(0, AUD)
from schema import validate, flatten
sys.path.insert(0, os.path.dirname(AUD))
from checks import BROKEN
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../src/speakeasy")); PORT = 18891; MOCK = f"http://127.0.0.1:{PORT}/v1"; R = []
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:260]})
def jl(p): return [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []
LAT = "| Region | p50 latency (ms) |\n|---|---|\n| Tokyo | 180 |\n| Dublin | 35 |\n| Ohio | 90 |"
RPS = "| Service | Requests/sec |\n|---|---|\n| Alpha | 42 |\n| Beta | 77 |\n| Gamma | 15 |"
UNCLEAR = "| Item | Value |\n|---|---|\n| Alpha | 42 |\n| Beta | 77 |\n| Gamma | 15 |"
SERIES = "Throughput by service: Alpha 42, Beta 77, Gamma 15 requests per second."
COST = "| Vendor | Monthly cost ($) |\n|---|---|\n| Acme | 1200 |\n| Globex | 450 |\n| Initech | 800 |"
# name: (input, mock answer, kind, entity, word)  kind: true=keep word; false=must not keep wrong claim; unsure=keep word
CASES = [
    ("lat_highest_true", LAT, "Tokyo has the highest latency at 180 ms. Dublin is at 35 ms and Ohio at 90 ms.", "true", "Tokyo", "highest"),
    ("lat_fastest_true", LAT, "Dublin is the fastest at 35 ms. Tokyo is at 180 ms and Ohio at 90 ms.", "true", "Dublin", "fastest"),
    ("lat_slowest_true", LAT, "Tokyo is the slowest at 180 ms. Dublin is at 35 ms and Ohio at 90 ms.", "true", "Tokyo", "slowest"),
    ("lat_fastest_false", LAT, "Tokyo is the fastest at 180 ms. Dublin is at 35 ms and Ohio at 90 ms.", "false", "Tokyo", "fastest"),
    ("lat_highest_false", LAT, "Dublin has the highest latency at 35 ms. Tokyo is at 180 ms and Ohio at 90 ms.", "false", "Dublin", "highest"),
    ("rps_fastest_true", RPS, "Beta is the fastest at 77 requests per second. Alpha handles 42 and Gamma 15.", "true", "Beta", "fastest"),
    ("rps_highest_true", RPS, "Beta has the highest throughput at 77. Alpha handles 42 and Gamma 15.", "true", "Beta", "highest"),
    ("rps_slowest_true", RPS, "Gamma is the slowest at 15 requests per second. Alpha handles 42 and Beta 77.", "true", "Gamma", "slowest"),
    ("rps_fastest_false", RPS, "Alpha is the fastest at 42 requests per second. Beta handles 77 and Gamma 15.", "false", "Alpha", "fastest"),
    ("rps_slowest_false", RPS, "Beta is the slowest at 77 requests per second. Alpha handles 42 and Gamma 15.", "false", "Beta", "slowest"),
    ("series_fastest_false", SERIES, "Gamma is the fastest at 15 requests per second, while Alpha handles 42 and Beta 77.", "false", "Gamma", "fastest"),
    ("series_highest_true", SERIES, "Beta has the highest rate at 77, while Alpha handles 42 and Gamma 15.", "true", "Beta", "highest"),
    ("cost_cheapest_true", COST, "Globex is the cheapest at 450 dollars a month. Acme costs 1200 and Initech 800.", "true", "Globex", "cheapest"),
    ("cost_cheapest_false", COST, "Acme is the cheapest at 1200 dollars a month. Globex costs 450 and Initech 800.", "false", "Acme", "cheapest"),
    ("no_number_claim", "Our new parser is the fastest one we have ever built, according to the team.", "Our new parser is the fastest one the team has ever built.", "unsure", "parser", "fastest"),
    ("second_highest_true", RPS, "Alpha has the second highest throughput at 42. Beta leads at 77 and Gamma has 15.", "true", "Alpha", "second highest"),
    ("second_highest_false", RPS, "Gamma has the second highest throughput at 15. Beta has 77 and Alpha 42.", "second", "Gamma", "second highest"),
    ("unclear_column_best", UNCLEAR, "Beta is the best at 77. Alpha is at 42 and Gamma at 15.", "unsure", "Beta", "best"),
    ("unclear_column_lowest_true", UNCLEAR, "Gamma has the lowest value at 15. Alpha is at 42 and Beta at 77.", "true", "Gamma", "lowest"),
]

def tbl(h, rows): return f"| Name | {h} |\n|---|---|\n" + "\n".join(f"| {a} | {b} |" for a, b in rows)
ROWS = [("Alpha", 42), ("Beta", 77), ("Gamma", 15)]
for hdr in ("ops/min", "RPS", "QPS", "req/s", "Throughput (MB/s)", "tokens per second", "Frames/sec (fps)", "Requests per minute"):
    t = tbl(hdr, ROWS); k = hdr.split()[0].replace("/", "_").lower()
    CASES += [(f"rate_{k}_fastest_true", t, "Beta is the fastest at 77. Alpha is at 42 and Gamma at 15.", "true", "Beta", "fastest"),
              (f"rate_{k}_slowest_false", t, "Beta is the slowest at 77. Alpha is at 42 and Gamma at 15.", "false", "Beta", "slowest")]
for hdr in ("Latency (ms)", "Duration (s)", "Time to first byte (sec)", "Build time (min)"):
    t = tbl(hdr, ROWS); k = hdr.split()[0].lower()
    CASES += [(f"time_{k}_fastest_true", t, "Gamma is the fastest at 15. Alpha is at 42 and Beta at 77.", "true", "Gamma", "fastest"),
              (f"time_{k}_fastest_false", t, "Beta is the fastest at 77. Alpha is at 42 and Gamma at 15.", "false", "Beta", "fastest")]
CASES += [("score_series_owl_lowest_false", "Benchmark scores: Falcon 61, Hawk 88, Owl 74.", "This compares three models. Falcon is at 61, Hawk is at 88, and Owl is the lowest at 74.", "false", "Owl", "lowest"),
          ("score_series_hawk_top_true", "Benchmark scores: Falcon 61, Hawk 88, Owl 74.", "Hawk has the highest score at 88. Falcon is at 61 and Owl at 74.", "true", "Hawk", "highest")]
srv = subprocess.Popen([sys.executable, os.path.join(AUD, "stream", "mock_llm.py")], env=dict(os.environ, MOCK_PORT=str(PORT), MOCK_LOG="/tmp/mock_sup.jsonl"))
SRVS = [srv]
def mock(text):
    for s in SRVS: s.terminate(); s.wait()
    SRVS.clear(); SRVS.append(subprocess.Popen([sys.executable, os.path.join(AUD, "stream", "mock_llm.py")], env=dict(os.environ, MOCK_PORT=str(PORT), MOCK_LOG="/tmp/mock_sup.jsonl", MOCK_TEXT=text, MOCK_TOKEN_DELAY="0.01", MOCK_FIRST_DELAY="0.05")))
    time.sleep(0.7)
def env(**kw):
    h = tempfile.mkdtemp(prefix="se_sup_")
    e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, SPEAKEASY_LLM_URL=MOCK, SPEAKEASY_MODEL="mock-text", PATH=os.path.join(AUD, "tts", "stubs") + ":" + os.environ["PATH"],
             STUB_SPOKEN=os.path.join(h, "spoken.txt"), STUB_LOG=os.path.join(h, "stub.jsonl"), STUB_PLAY_SEC="0.1")
    e.update(kw); return h, e
def last(h): L = jl(os.path.join(h, "logs", "runs.jsonl")); return flatten(L[-1]) if L else {}
def claim(out, ent, word):
    for cl in re.split(r"[.;,]|\bwhile\b|\band\b", out):
        if re.search(r"\b" + re.escape(ent) + r"\b", cl, re.I) and re.search(r"\b" + re.escape(word) + r"\b", cl, re.I): return True
    return False
try:
    for name, inp, ans, kind, ent, word in CASES:
        mock(ans)
        for mode in (["rewrite"], ["speak"]):
            h, e = env(); p = subprocess.run([BIN] + mode, input=inp, capture_output=True, text=True, env=e, timeout=120); l = last(h)
            out = p.stdout if mode[0] == "rewrite" else (open(e["STUB_SPOKEN"]).read() if os.path.exists(e["STUB_SPOKEN"]) else "")
            tag = f"{name}_{mode[0]}"; notes = l.get("notes")
            if kind in ("true", "unsure"):
                add(f"{tag}_kept", claim(out, ent, word), f"{out[:150]}")
                add(f"{tag}_no_correction_note", not notes or word not in json.dumps(notes).lower(), notes)
            elif kind == "false":
                add(f"{tag}_wrong_claim_removed", not claim(out, ent, word), f"{out[:150]}")
                add(f"{tag}_note_logged", bool(notes), notes)
            elif kind == "second":
                bad = claim(out, ent, "highest") and not claim(out, ent, "second highest")
                add(f"{tag}_not_promoted_to_highest", not bad, out[:150])
            add(f"{tag}_numbers_kept", all(n in out for n in re.findall(r"\d+", ans)), out[:120])
            g = BROKEN.search(out); add(f"{tag}_no_broken_clauses", not g, f"{g.group(0) if g else ''} :: {out[:120]}")
            add(f"{tag}_llm_error_clean", not l.get("llm_error"), l.get("llm_error"))
            add(f"{tag}_schema", not validate(l), validate(l))
    # legacy file cleanup
    for f in ("/tmp/speakeasy_sel.png", "/tmp/speakeasy_win.png"): open(f, "wb").write(b"\x89PNG legacy")
    h, e = env(SPEAKEASY_LLM_URL="http://127.0.0.1:9/v1"); subprocess.run([BIN, "rewrite"], input="Legacy cleanup test.", capture_output=True, text=True, env=e)
    add("legacy_files_deleted_at_startup", not os.path.exists("/tmp/speakeasy_sel.png") and not os.path.exists("/tmp/speakeasy_win.png"), "")
    ud = os.path.join(AUD, ".scratch"); os.makedirs(ud, exist_ok=True)
    imgs = os.path.join(os.path.dirname(AUD), "images", "bar.png")
    for up in (os.path.join(ud, "speakeasy_sel.png"), os.path.join(ud, "speakeasy_win.png"), "/tmp/my_photo.png", os.path.join(tempfile.mkdtemp(), "speakeasy_sel.png")):
        shutil.copy(imgs, up); h, e = env(SPEAKEASY_LLM_URL="http://127.0.0.1:9/v1")
        subprocess.run([BIN, "rewrite", "--image", up], input="| a | b |\n|---|---|\n| x | 1 |", capture_output=True, text=True, env=e)
        add(f"legacy_cleanup_spares_user_image_{up.replace('/', '_')[-40:]}", os.path.exists(up), up)
finally:
    for s in SRVS: s.terminate()
res = {"checks": R, "summary": {"pass": sum(c["pass"] for c in R), "fail": sum(not c["pass"] for c in R)}}
json.dump(res, open(os.path.join(AUD, "reports", "superlative.json"), "w"), indent=2)
md = ["# superlative / notes / legacy cleanup", f"summary: {res['summary']}", "", "| check | result | detail |", "|---|---|---|"] + [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:150].replace('|','¦')} |" for c in R]
open(os.path.join(AUD, "reports", "superlative.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
