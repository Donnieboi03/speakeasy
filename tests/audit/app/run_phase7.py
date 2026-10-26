#!/usr/bin/env python3
"""Phase-7 holdout acceptance suite (black box) for docs/contracts/phase7-usable-app.md and docs/design.md.

Drives only public interfaces: the CLI (src/speakeasy: speak / selection / stats / logs --scrub), the logs and live files
under a temp $SPEAKEASY_HOME, the app built from app/ into the temp root (its --snapshot / --self-check modes and one passive
live launch with a temp home), the tracker-free backend with a mock LLM (tests/audit/stream/mock_llm.py), a mock Kokoro
(tests/audit/tts/mock_tts.py) and stubbed afplay / say / launchctl / open, and the Phase 2 bench. Never reads src/ or
app/Sources to design checks (section X only runs the contract's own regex lints over them), never writes under the real
~/.speakeasy (real logs are only copied read-only), never touches the user's running Speakeasy.app.

Usage: python3 run_phase7.py [--repo PATH] [--only A,L,R,K,S,P,U,C,X,G] [--app PATH/Speakeasy.app] [--skip-bench]
                             [--ui-input] [--keep] [--real-logs DIR] [--doc-python PY] [--layout-model PATH]
Run it under a Python with Pillow + numpy; --doc-python defaults to $SPEAKEASY_DOC_PYTHON or this interpreter, --layout-model to $SPEAKEASY_LAYOUT_MODEL.
Output: one line per check `PASS|FAIL|SKIP <name> <detail>`, `INFO ...` lines, then `SUMMARY ...`. Exit 0 only if no FAIL."""
import argparse, datetime, glob, hashlib, json, os, re, resource, shutil, signal, socket, statistics, subprocess, sys, tempfile
import threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import p7swift

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", ".."))); ap.add_argument("--only", default="")
ap.add_argument("--app", help="prebuilt Speakeasy.app (default: build app/ into the temp root)")
ap.add_argument("--skip-bench", action="store_true"); ap.add_argument("--ui-input", action="store_true",
                help="also inject mouse / key events into the live session (clicks the test copy's bar)")
ap.add_argument("--keep", action="store_true"); ap.add_argument("--real-logs", default=os.path.expanduser("~/.speakeasy/logs"))
ap.add_argument("--doc-python", default=os.environ.get("SPEAKEASY_DOC_PYTHON") or sys.executable, help="python with pypdfium2 (+ onnxruntime, numpy for the bench)")
ap.add_argument("--layout-model", default=os.environ.get("SPEAKEASY_LAYOUT_MODEL", ""), help="verified pp_doclayout_s.onnx (section G skips without it)")
A = ap.parse_args()
REPO = os.path.abspath(A.repo); SRC = os.path.join(REPO, "src"); CLI = os.path.join(SRC, "speakeasy")
AUD = os.path.join(REPO, "tests", "audit"); BENCH = os.path.join(AUD, "bench"); PUB = os.path.join(BENCH, "fixtures", "public")
ONLY = {x.strip().upper() for x in A.only.split(",") if x.strip()}
REAL_HOME = os.path.realpath(os.path.expanduser("~/.speakeasy"))
TMPROOT = os.path.realpath(tempfile.mkdtemp(prefix="se_p7_")); T0 = time.time()
sys.path.insert(0, BENCH)

R = []
def _s(d): return " ".join(str(d).split())[:600]
def add(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"; R.append((st, name))
    print(f"{st} {name} {_s(detail) if (not ok or os.environ.get('P7_VERBOSE')) else ''}".rstrip(), flush=True); return ok
def skip(name, why): R.append(("SKIP", name)); print(f"SKIP {name} {_s(why)}", flush=True)
def info(msg): print(f"INFO {_s(msg)}", flush=True)
def section(tag):
    def deco(fn):
        def run():
            if ONLY and tag not in ONLY: return
            t = time.time()
            try: fn()
            except Exception as e: add(f"{tag}_section_crashed", False, f"{type(e).__name__}: {e} :: {traceback.format_exc()[-900:]}")
            info(f"section {tag} {time.time() - t:.1f}s")
        run.__name__ = fn.__name__; return run
    return deco
def isint(v): return isinstance(v, int) and not isinstance(v, bool)
def isnum(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
def near(a, b, tol=0.051): return isnum(a) and isnum(b) and abs(a - b) <= tol
def jl(p):
    out = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x:
                try: out.append(json.loads(x))
                except Exception: out.append({"_raw": x})
    return out
def wjl(p, recs, raw_extra=()):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        for r in recs: f.write(json.dumps(r) + "\n")
        for x in raw_extra: f.write(x + "\n")
def h12(s): return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]
def iso(t): return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))
def pts(v):
    """Parse a log timestamp (ISO string or epoch number) to epoch seconds, else None."""
    if isnum(v): return float(v) / (1000.0 if v > 1e11 else 1.0)
    if isinstance(v, str):
        for f in (lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00")), lambda s: datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")):
            try:
                d = f(v)
                return d.timestamp() if d.tzinfo else time.mktime(d.timetuple())
            except Exception: pass
    return None
def walk_keys(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items(): yield k, path + "." + str(k); yield from walk_keys(v, path + "." + str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o): yield from walk_keys(v, f"{path}[{i}]")
def walk_strings(o):
    if isinstance(o, str): yield o
    elif isinstance(o, dict):
        for k, v in o.items(): yield from walk_strings(v)
    elif isinstance(o, list):
        for v in o: yield from walk_strings(v)
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def pid_alive(pid):
    try: os.kill(pid, 0); return True
    except Exception: return False

# ---------------------------------------------------------------- isolation guards
def real_home_state():
    out = {}
    for sub in ("", "logs"):
        d = os.path.join(REAL_HOME, sub)
        if os.path.isdir(d):
            for n in os.listdir(d):
                p = os.path.join(d, n)
                try: out[os.path.join(sub, n)] = os.stat(p).st_mtime_ns
                except OSError: pass
    return out
REAL0 = real_home_state()

_n = [0]
def mkhome(tag="h"):
    _n[0] += 1; d = os.path.join(TMPROOT, "homes", f"{_n[0]:04d}-{tag}"); h = os.path.join(d, "home"); os.makedirs(h); os.makedirs(os.path.join(d, "tmp"))
    rh = os.path.realpath(h); assert rh.startswith(TMPROOT + os.sep) and not rh.startswith(REAL_HOME)
    return h

# stubs: afplay / say that log start (with pid / pgid) and "play" in 50 ms steps so SIGSTOP really pauses them; launchctl / open no-ops
STUBS = os.path.join(TMPROOT, "stubs"); os.makedirs(STUBS)
_PLAY = r'''#!/bin/bash
TOOL=__TOOL__
f="${@: -1}"
now(){ perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
log(){ printf '{"tool":"%s","event":"%s","pid":%d,"pgid":%d,"t":%s}\n' "$TOOL" "$1" $$ "$(ps -o pgid= -p $$ | tr -d ' ')" "$(now)" >> "${P7_STUB_LOG:-/dev/null}"; }
if [ "$TOOL" = say ] && [ -n "$STUB_SPOKEN" ]; then printf '%s\n' "$*" >> "$STUB_SPOKEN"; fi
log start
trap 'log killed; kill $c 2>/dev/null; exit 143' TERM INT
perl -MTime::HiRes=sleep -e 'my $n = int(($ENV{STUB_PLAY_SEC} || 0.5) / 0.05 + 0.5); for (1..$n) { sleep 0.05 }' & c=$!
wait $c
log end
'''
for tool in ("afplay", "say"):
    p = os.path.join(STUBS, tool); open(p, "w").write(_PLAY.replace("__TOOL__", tool)); os.chmod(p, 0o755)
for tool in ("launchctl", "open"):
    p = os.path.join(STUBS, tool); open(p, "w").write(f'#!/bin/bash\necho "{tool} $*" >> "${{P7_STUB_LOG:-/dev/null}}.cmds"\nexit 0\n'); os.chmod(p, 0o755)
NOSEL = os.path.join(AUD, "nosel", "stubs")

def penv(home, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "STUB_", "MOCK_", "P7_")) and k not in ("PYTHONPATH",)}
    e.update(HOME=home, SPEAKEASY_HOME=home, CFFIXED_USER_HOME=home, TMPDIR=os.path.join(os.path.dirname(home), "tmp"), PYTHONDONTWRITEBYTECODE="1",
             PATH=":".join([STUBS, NOSEL, os.environ.get("PATH", "/usr/bin:/bin")]))
    for k, v in kw.items():
        if v is None: e.pop(k, None)
        else: e[k] = str(v)
    return e
def cli(args, home, inp=None, timeout=120, **kw):
    t = time.time()
    p = subprocess.run([CLI] + args, input=inp, capture_output=True, text=True, env=penv(home, **kw), timeout=timeout)
    return p.returncode, p.stdout, p.stderr, (time.time() - t) * 1000
def stats_json(home, window=None, **kw):
    rc, out, err, ms = cli(["stats", "--json"] + (["--window", str(window)] if window else []), home, **kw)
    try: d = json.loads(out)
    except Exception: d = None
    return rc, d, err, ms

# ---------------------------------------------------------------- mocks
class LLM:
    def __init__(self):
        import run_bench as RB
        d = os.path.join(TMPROOT, "mockllm"); os.makedirs(d, exist_ok=True); self.m = RB.Mock(d); self.port = self.m.port; self.reply = self.m.reply
    def set(self, text): open(self.reply, "w", encoding="utf-8").write(text)
    def stop(self): self.m.stop()
class TTS:
    def __init__(self):
        self.port = free_port(); self.log = os.path.join(TMPROOT, f"mocktts{self.port}.jsonl")
        e = {k: v for k, v in os.environ.items() if not k.startswith("MOCK_")}; e.update(MOCK_PORT=str(self.port), MOCK_LOG=self.log, MOCK_DELAY="0.05")
        self.p = subprocess.Popen([sys.executable, os.path.join(AUD, "tts", "mock_tts.py")], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f"http://127.0.0.1:{self.port}/v1/audio/speech"
        for _ in range(100):
            try: socket.create_connection(("127.0.0.1", self.port), 0.2).close(); break
            except Exception: time.sleep(0.05)
    def stop(self):
        if self.p.poll() is None: self.p.terminate()
class _E500(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self): self.send_response(500); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n); self.send_response(500); self.end_headers(); self.wfile.write(b"boom")
_MOCKS = {}
def llm():
    if "llm" not in _MOCKS: _MOCKS["llm"] = LLM()
    return _MOCKS["llm"]
def tts():
    if "tts" not in _MOCKS: _MOCKS["tts"] = TTS()
    return _MOCKS["tts"]
def tts500():
    if "e500" not in _MOCKS:
        port = free_port(); srv = ThreadingHTTPServer(("127.0.0.1", port), _E500); threading.Thread(target=srv.serve_forever, daemon=True).start()
        _MOCKS["e500"] = (srv, f"http://127.0.0.1:{port}/v1/audio/speech")
    return _MOCKS["e500"][1]
def dead_url():
    return f"http://127.0.0.1:{free_port()}/v1/audio/speech"

SENTS = ["The quindle sensor measures zorblax drift across the northern valley every morning.",
         "Each reading travels to a base station that stores it for later review by the team.",
         "When the drift grows too large the station raises a warning for the operator on duty.",
         "The operator then compares the warning with the previous week of stored readings.",
         "Most warnings turn out to be caused by fog settling over the quindle sensor housing.",
         "A small heater now keeps the housing dry so the false warnings have nearly stopped."]
def back_env(home, tts_mode="kokoro", play=0.3, tts_url=None, **kw):
    m = llm()
    e = dict(SPEAKEASY_LLM_URL=f"http://127.0.0.1:{m.port}/v1", SPEAKEASY_MODEL="mock-text", SPEAKEASY_LLM="local", SPEAKEASY_VISION="never",
             SPEAKEASY_GROUND="off", SPEAKEASY_TTS=tts_mode, SPEAKEASY_DOC_READ="local", SPEAKEASY_DOC_PYTHON=A.doc_python,
             SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker", "speakeasy-track"), SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1",
             SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_KEEP_CAPTURE="0", SPEAKEASY_PREWARM="0", STUB_PLAY_SEC=str(play),
             P7_STUB_LOG=os.path.join(os.path.dirname(home), "stub.jsonl"))
    if tts_mode == "kokoro": e["SPEAKEASY_TTS_URL"] = tts_url or tts().url
    if os.path.isfile(A.layout_model): e["SPEAKEASY_LAYOUT_MODEL"] = A.layout_model
    e.update(kw); return e
def start_speak(home, text, reply, args=(), **kw):
    llm().set(reply)
    e = penv(home, **back_env(home, **kw))
    p = subprocess.Popen([CLI, "speak", "--app", "menubar"] + list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e)
    p.stdin.write(text); p.stdin.close(); p.stdin = None; return p
def stub_events(home):
    return jl(os.path.join(os.path.dirname(home), "stub.jsonl"))
def starts(home, tool="afplay"): return [r for r in stub_events(home) if r.get("event") == "start" and r.get("tool") == tool]
def wait_for(fn, timeout, step=0.05):
    t = time.time()
    while time.time() - t < timeout:
        v = fn()
        if v: return v
        time.sleep(step)
    return None
def speak_pid(home):
    try: return int(open(os.path.join(home, "speak.pid")).read().split()[0])
    except Exception: return None
def last_run(home):
    L = [r for r in jl(os.path.join(home, "logs", "runs.jsonl")) if r.get("command") in ("speak", "selection")]
    return L[-1] if L else None
def finish(p, timeout=90):
    try: out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired: p.kill(); out, err = p.communicate()
    return p.returncode, err

# ---------------------------------------------------------------- PDF fixtures (title metadata through an incremental update)
def pdf_with_title(src, dst, title):
    b = open(src, "rb").read()
    sx = int(re.findall(rb"startxref\s+(\d+)", b)[-1]); tr = re.findall(rb"trailer\s*<<(.*?)>>", b, re.S)[-1]
    root = re.search(rb"/Root\s+(\d+\s+\d+\s+R)", tr).group(1); size = int(re.search(rb"/Size\s+(\d+)", tr).group(1))
    t = title.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
    obj_off = len(b) + 1
    body = b"\n" + f"{size} 0 obj\n".encode() + b"<< /Title (" + t + b") /Producer (p7) >>\nendobj\n"
    xref_off = len(b) + len(body)
    x = f"xref\n0 1\n0000000000 65535 f \n{size} 1\n{obj_off:010d} 00000 n \n".encode()
    x += b"trailer\n<< /Size " + str(size + 1).encode() + b" /Root " + root + b" /Info " + f"{size} 0 R".encode() + b" /Prev " + str(sx).encode() + b" >>\nstartxref\n" + str(xref_off).encode() + b"\n%%EOF\n"
    open(dst, "wb").write(b + body + x)
    try:
        out = subprocess.run([A.doc_python, "-c", "import sys,pypdfium2 as P; print(P.PdfDocument(sys.argv[1]).get_metadata_dict().get('Title',''))", dst], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception: out = None
    return out == title

def doc_case(doc="syn-fig5-01", pick=None):
    import run_bench as RB
    cs = json.load(open(os.path.join(PUB, doc, "cases.json"))); g = json.load(open(os.path.join(PUB, doc, "gt.json")))
    c = cs[0] if pick is None else next((x for x in cs if pick(x)), cs[0])
    return RB, c, {p["n"]: p["w"] for p in g["pages"]}[c["page"]]
def speak_doc(home, pdf_path, case_doc="syn-fig5-01", pick=None, run_id="p7doc", wait=True, **kw):
    RB, c, pw = doc_case(case_doc, pick); d = os.path.dirname(home)
    hint = os.path.join(d, f"hint_{time.time_ns()}.json"); lbp = os.path.join(d, f"lines_{time.time_ns()}.json")
    json.dump({"app": "Preview", "bundle": "com.apple.Preview", "title": os.path.basename(pdf_path), "ax_document": "file://" + pdf_path}, open(hint, "w"))
    cap, lb = RB.geometry_for(c, pw); json.dump(lb, open(lbp, "w"))
    p = start_speak(home, "\n".join(it["text"] for it in c["selection"]), " ".join(s["text"] for s in c["script"]),
                    args=["--capture-method", "bounds", f"--capture-rect={cap}", "--line-bounds", lbp, "--doc-hint", hint, "--run-id", run_id], **kw)
    if not wait: return p, c
    rc, err = finish(p, 180); return rc, err, c

# ================================================================ A. stats schema 2 on fixture logs
NOW = time.time(); D = 86400.0
_mid = time.mktime(datetime.date.today().timetuple()); _since = NOW - _mid
T_TODAY_A = min(50.0, _since * 0.5); T_TODAY_B = min(20.0, _since * 0.25)
DOC_A, DOC_B, DOC_C = "aaaa11112222", "bbbb33334444", "cccc55556666"
LIB_NAME_A = "Quokka Ledger Study"; LIB_PATH_A = "/Users/fixture/zq/quokka-ledger-9931.pdf"
PRIV = ["zebrafish ventricle regenerates", "marmoset cortex", "Wombat Burrow Thermodynamics", "otterbadger"]
def _run(rid, off, cmd="speak", app="menubar", tfa=None, audio=0.0, words=0, doc=None, page=None, new=None, prov=None, prov_cfg=None,
         tts_req="kokoro", tts_used="kokoro", layout=None, fm=None, sm=None, dry=False, **extra):
    r = {"ts": iso(NOW - off), "command": cmd, "app": app, "dry_run": dry, "run_id": rid, "backend": "llm", "model": "mock", "error": None,
         "input_chars": 120, "output_chars": 110, "total_ms": 4000, "llm_ms": 50, "image": False, "image_px": None, "capture_method": "area",
         "capture_ms": 100, "capture_rect": "0,0,100,100", "copy_ms": None, "peak_rss_mb": 50, "words_spoken": words, "audio_s": audio,
         "tts_first_audio_ms": tfa, "speak_ms": int(audio * 1000), "timings": {"tts_first_audio_ms": tfa, "speak_ms": int(audio * 1000)},
         "tts": tts_used, "tts_requested": tts_req, "kind": "figure", "words_in": 40, "notes": None, "llm_error": None}
    if prov is not None: r["llm_provider"] = prov
    if prov_cfg is not None: r["llm_provider_configured"] = prov_cfg
    if doc: r.update(doc_path_hash=doc, page=page, doc_source="ax-document")
    if layout: r["doc_parse"] = {"columns": 1, "figs_done": True, "blocks": {"paragraph": 3, "caption": 1, "figure": 1, "table": 0}, "layout": layout}
    if fm is not None: r["focus_methods"] = fm
    if sm is not None: r["sentence_map"] = {"sentences": sm, "mapped": sm, "method": "page", "ms": 2}
    if new: r.update(new)
    r.update(extra); return r
def NEWF(end, sn, ss, figs, tfb=None): return {"end": end, "sent_n": sn, "sent_spoken": ss, "paused_ms": 0, "skips": 0, "fig_ids": figs, "tts_fallback": tfb}
def fixture_runs():
    L = [
        _run("r1", T_TODAY_A, tfa=2000, audio=30, words=90, doc=DOC_A, page=3, new=NEWF("finished", 5, 5, ["p3:fig1"]), prov="openrouter", prov_cfg="openrouter", fm={"align": 4, "hold": 1}, sm=5),
        _run("r2", T_TODAY_B, cmd="selection", tfa=2000, audio=10, words=30, doc=DOC_A, page=3, new=NEWF("stopped", 6, 2, ["p3:fig1"]), prov="local", prov_cfg="openrouter", fm={"doc-object": 3, "none": 2}, sm=6),
        _run("r3", 2 * D, tfa=2000, audio=20, words=60, doc=DOC_A, page=3, new=NEWF("finished", 4, 4, ["p3:tab1", "p3:fig1"]), prov="openrouter", prov_cfg="openrouter", fm={"align": 4}, sm=4,
             input="The " + PRIV[0] + " quickly.", output="It says the " + PRIV[0] + ".", error="llm: could not parse the sentence about the " + PRIV[1] + " here", notes="Title: " + PRIV[2],
             stream_note="saw " + PRIV[3]),
        _run("r4", 2 * D + 60, tfa=None, audio=0, words=0, doc=DOC_A, page=3, new=NEWF("error", None, 0, []), prov="local", prov_cfg="openrouter"),
        _run("r5", 3 * D + 100, tfa=6000, audio=20, words=60, doc=DOC_B, page=3, new=NEWF("finished", 3, 3, ["p3:fig1"], "unavailable"), tts_used="say",
             layout={"src": "heuristic", "reason": "model-missing"}, fm={"doc-object": 3}, sm=3),
        _run("r6", 3 * D, tfa=6000, audio=15, words=45, doc=DOC_B, page=2, new=NEWF("finished", 2, 2, ["p2:tab1"]), prov="local", layout={"src": "heuristic", "reason": "disabled"}, fm={"align": 2}, sm=2),
        _run("r7", 5 * D + 50, tfa=12000, audio=5, words=15, new=NEWF("stopped", 3, 1, []), fm={"align": 1, "hold": 2}, sm=3),
        _run("r8", 5 * D, tfa=12000, audio=15, words=45, fm={"align": 3}, sm=3),
        _run("r9", 4 * D, tfa=6000, audio=25, words=75, doc=DOC_C, page=4, fm={"hold": 5}, sm=5),
        _run("r10", 10 * D, tfa=10000, audio=12, words=36, doc=DOC_A, page=7, new=NEWF("finished", 2, 2, ["p7:fig3"], "unavailable"), tts_used="say"),
        _run("r11", 20 * D, tfa=3000, audio=10, words=30, doc=DOC_B, page=9, new=NEWF("finished", 2, 2, [])),
        _run("r12", 40 * D, tfa=4000, audio=10, words=30, doc=DOC_A, page=8, new=NEWF("finished", 2, 2, ["p8:fig1"])),
        _run("x_dry", 30, tfa=1000, audio=5, words=15, dry=True),
        _run("x_rw", 40, cmd="rewrite", tfa=None),
        {"ts": iso(NOW - 35), "command": "scrub", "files": 1, "records_changed": {"runs": 1, "focus": 0}, "fields": {}, "other_logs_with_text": 0, "ms": 5},
        {"ts": iso(NOW - 45), "command": "stats", "app": "cli", "dry_run": False},
    ]
    return L
TPID = {"r1": 9101, "r3": 9103, "r6": 9106, "r7": 9107, "r10": 9110}
def fixture_tracker():
    L = []
    def cue(rid, off, seq, landed, hidden=False):
        return {"ts": NOW - off, "event": "cue_result", "tracker_pid": TPID[rid], "run_id": rid, "seq": seq, "cue_kind": "text_band",
                "first_draw_ms": 40, "corrections": 0 if landed else 1, "max_shift_px": 0 if landed else 30, "landed": landed, "hidden": hidden}
    for rid, off, res in (("r1", T_TODAY_A, [(True, False), (True, False), (True, False), (False, False)]),
                          ("r3", 2 * D, [(True, False), (True, False), (False, True), (False, False)]),
                          ("r7", 5 * D + 50, [(True, False), (True, False)]), ("r10", 10 * D, [(True, False), (True, False)]),
                          ("r6", 3 * D, [])):
        L.append({"ts": NOW - off - 1, "event": "start", "tracker_pid": TPID[rid], "pid": 1, "window_id": 1, "accessibility": True, "screen_recording": rid != "r6", "fps": "3", "run_id": rid})
        for i, (ld, hd) in enumerate(res): L.append(cue(rid, off - 0.5 - 0.01 * i, i + 1, ld, hd))
        if rid == "r1": L.append({"ts": NOW - off - 0.2, "event": "draw-failed", "why": "panel not on screen", "after_ms": 300, "seq": 4, "tracker_pid": TPID[rid]})
        if rid == "r3": L.append({"ts": NOW - off - 0.3, "event": "correction", "seq": 4, "dx": 20, "dy": 14, "from": "event", "to": "ocr", "after_ms": 120, "tracker_pid": TPID[rid]})
        L.append({"ts": NOW - off - 0.05, "event": "exit", "tracker_pid": TPID[rid], "run_id": rid, "reason": "no_screen_recording" if rid == "r6" else "speech-finished",
                  "draws": len(res), "corrections": 0, "cues": {"shows": len(res), "landed": sum(a for a, _ in res), "hidden": sum(b for _, b in res)}})
    return L
def fixture_app(ts_fmt):
    f = (lambda t: iso(t)) if ts_fmt == "iso" else (lambda t: round(t, 3))
    return [{"ts": f(NOW - T_TODAY_A + 3), "event": "user_flag", "run_id": "r1", "seq": 2, "cue_kind": "text_band"},
            {"ts": f(NOW - 3 * D + 5), "event": "user_flag", "run_id": "r6", "seq": 1, "cue_kind": "table_row"},
            {"ts": f(NOW - 10 * D), "event": "user_flag", "run_id": "r10", "seq": 1, "cue_kind": "text_band"},
            {"ts": f(NOW - 60), "event": "bar_state", "from": "idle", "to": "hover"}]
def make_stats_home(app_ts="iso", library=True, tracker=True, extra_config=""):
    h = mkhome("stats"); lg = os.path.join(h, "logs")
    wjl(os.path.join(lg, "runs.jsonl"), fixture_runs(), raw_extra=['{"ts": "2026-10-26T07:00:00-0700", "command": "speak", broken'])
    if tracker: wjl(os.path.join(lg, "tracker.jsonl"), fixture_tracker())
    wjl(os.path.join(lg, "app.jsonl"), fixture_app(app_ts))
    wjl(os.path.join(lg, "focus.jsonl"), [{"ts": NOW - 50, "state": "show", "seq": 1, "rect": [1, 2, 3, 4], "text": "The " + PRIV[0], "pid": 5}])
    if library:
        lib = {DOC_A: {"name": LIB_NAME_A, "path": LIB_PATH_A, "first_ts": iso(NOW - 12 * D), "last_ts": iso(NOW - 60)}}
        open(os.path.join(h, "library.json"), "w").write(json.dumps(lib)); os.chmod(os.path.join(h, "library.json"), 0o600)
    t = tts()
    open(os.path.join(h, "config"), "w").write(f"SPEAKEASY_TTS=kokoro\nSPEAKEASY_TTS_URL={t.url}\nSPEAKEASY_LLM_URL=http://127.0.0.1:{llm().port}/v1\n" + extra_config)
    return h
def oracle(window):
    runs = fixture_runs(); today = datetime.date.today()
    def inwin(r):
        t = pts(r["ts"])
        return datetime.date.fromtimestamp(t) == today if window == 1 else NOW - t < window * D
    reads = [r for r in runs if r.get("command") in ("speak", "selection") and r.get("dry_run") is False and r.get("app") in ("menubar", "cli")]
    W = [r for r in reads if inwin(r)]
    tod = [r for r in reads if datetime.date.fromtimestamp(pts(r["ts"])) == today]
    fa = [r["tts_first_audio_ms"] / 1000 for r in W if r.get("tts_first_audio_ms") is not None]
    byp = {}
    for r in W:
        if r.get("tts_first_audio_ms") is not None: byp.setdefault(r.get("llm_provider") or "local", []).append(r["tts_first_audio_ms"] / 1000)
    trend = {}
    for r in reads:
        t = pts(r["ts"])
        if NOW - t < 14 * D and r.get("tts_first_audio_ms") is not None: trend.setdefault(datetime.date.fromtimestamp(t).isoformat(), []).append(r["tts_first_audio_ms"] / 1000)
    items = {(r["doc_path_hash"], f) for r in W if r.get("doc_path_hash") for f in (r.get("fig_ids") or [])}
    comp = {"finished": 0, "stopped": 0, "error": 0}
    for r in W:
        if r.get("end") in comp: comp[r["end"]] += 1
    rids = {r["run_id"] for r in W}
    cues = [c for c in fixture_tracker() if c["event"] == "cue_result" and (NOW - c["ts"] < window * D if window != 1 else datetime.date.fromtimestamp(c["ts"]) == today)]
    flags = [a for a in fixture_app("num") if a["event"] == "user_flag" and (NOW - a["ts"] < window * D if window != 1 else datetime.date.fromtimestamp(a["ts"]) == today)]
    def per_doc(rs):
        docs = {}
        for r in sorted(rs, key=lambda r: pts(r["ts"])):
            if not r.get("doc_path_hash"): continue
            d = docs.setdefault(r["doc_path_hash"], {"pages": set(), "items": set(), "reads": 0})
            d["pages"].add(r["page"]); d["items"].update(r.get("fig_ids") or []); d["reads"] += 1; d["last_ts"] = pts(r["ts"])
            fi = r.get("fig_ids") or []
            d["resume"] = {"page": r["page"], "fig_id": fi[-1] if fi else None, "sentence": r.get("sent_spoken"), "of": r.get("sent_n")}
        return docs
    docs = per_doc(W); docs_all = per_doc(reads)
    names = {DOC_A: LIB_NAME_A}
    prob = {}
    def P(pid, r_or_t):
        t = pts(r_or_t["ts"]) if isinstance(r_or_t, dict) else r_or_t
        p = prob.setdefault(pid, {"count": 0, "last": 0}); p["count"] += 1; p["last"] = max(p["last"], t)
    for r in W:
        if r.get("tts_requested") == "kokoro" and r.get("tts") == "say": P("kokoro-down", r)
        if r.get("llm_provider_configured") == "openrouter" and r.get("llm_provider") == "local": P("cloud-fallback", r)
        lay = ((r.get("doc_parse") or {}).get("layout") or {})
        if lay.get("src") == "heuristic" and lay.get("reason") in ("model-missing", "model-mismatch", "no-onnxruntime", "load-failed"): P("layout-missing", r)
        if (r.get("tts_first_audio_ms") or 0) > 10000: P("slow-start", r)
    hf = {}
    pid2run = {e["tracker_pid"]: e["run_id"] for e in fixture_tracker() if e["event"] == "start"}
    for e in fixture_tracker():
        if (e["event"] == "draw-failed" or (e["event"] == "exit" and e.get("reason") == "no_screen_recording")) and pid2run.get(e["tracker_pid"]) in rids:
            hf[pid2run[e["tracker_pid"]]] = max(hf.get(pid2run[e["tracker_pid"]], 0), e["ts"])
    for rid, t in hf.items(): P("highlight-failed", t)
    return dict(reads={"today": len(tod), "window": len(W)}, listened={"today": round(sum(r["audio_s"] for r in tod) / 60, 1), "window": round(sum(r["audio_s"] for r in W) / 60, 1)},
                fa=fa, byp=byp, trend={k: statistics.median(v) for k, v in trend.items()}, figures=sum(1 for _, f in items if ":fig" in f), tables=sum(1 for _, f in items if ":tab" in f),
                comp=comp, shows=len(cues), landed=sum(c["landed"] for c in cues), hidden=sum(c["hidden"] for c in cues), flagged=len(flags),
                no_cue=sum((r.get("focus_methods") or {}).get("hold", 0) + (r.get("focus_methods") or {}).get("none", 0) for r in W),
                sentences=sum((r.get("sentence_map") or {}).get("sentences", 0) for r in W),
                wpm=sum(r["words_spoken"] for r in W) / sum(r["audio_s"] for r in W) * 60, docs=docs, docs_all=docs_all, names=names, problems=prob)
SCHEMA1 = ["words_in", "words_listened", "audio_minutes", "figures_explained", "tables_read", "lists_read", "median_first_audio_ms",
           "time_saved_min_estimate", "time_saved_basis", "streak_days", "longest_streak_days", "today_words", "days"]
def forbidden_keys(d):
    bad = []
    for k, path in walk_keys(d):
        kl = str(k).lower()
        if kl.startswith(("time_saved", "streak", "longest")) or kl in ("lists_read", "words_in", "days", "heatmap", "today_words", "words_listened"): bad.append(path)
    return bad
def find(d, key):
    """First value under `key` anywhere in d (top-level preferred)."""
    if isinstance(d, dict) and key in d: return d[key]
    for k, path in walk_keys(d):
        if k == key:
            cur = d
            for part in re.findall(r"\.([^.\[]+)|\[(\d+)\]", path):
                cur = cur[part[0]] if part[0] else cur[int(part[1])]
            return cur
    return None

@section("A")
def stats_v2():
    h = make_stats_home(); rc, d, err, ms = stats_json(h, 7)
    add("A_stats_json_exit0_parses", rc == 0 and isinstance(d, dict), (rc, err[-300:]))
    if not isinstance(d, dict): return
    info(f"A stats --json --window 7 in {ms:.0f} ms; top keys {sorted(d)}")
    O = oracle(7)
    add("A_schema_2_window_7", d.get("schema") == 2 and d.get("window_days") == 7, (d.get("schema"), d.get("window_days")))
    add("A_schema1_keys_removed", not [k for k in SCHEMA1 if k in d], [k for k in SCHEMA1 if k in d])
    add("A_deleted_metrics_absent_anywhere", not forbidden_keys(d), forbidden_keys(d)[:8])
    add("A_reads_today_window", d.get("reads") == O["reads"], (d.get("reads"), O["reads"]))
    lm = d.get("listened_min") or {}
    add("A_listened_min_one_decimal", near(lm.get("today"), O["listened"]["today"]) and near(lm.get("window"), O["listened"]["window"]), (lm, O["listened"]))
    fa = d.get("first_audio_s") or {}
    add("A_first_audio_median_p90_n", near(fa.get("median"), statistics.median(O["fa"])) and near(fa.get("p90"), 12.0) and fa.get("n") == len(O["fa"]),
        (fa.get("median"), fa.get("p90"), fa.get("n"), statistics.median(O["fa"]), len(O["fa"])))
    bp = fa.get("by_provider") or {}
    want = {k: {"median": statistics.median(v), "n": len(v)} for k, v in O["byp"].items()}
    okp = set(bp) == set(want) and all(near((bp.get(k) or {}).get("median"), w["median"]) and (bp.get(k) or {}).get("n") == w["n"] for k, w in want.items())
    add("A_first_audio_by_provider_local_default", okp, (bp, want))
    tr = fa.get("trend")
    trd = {x.get("date"): x.get("median") for x in tr if isinstance(x, dict)} if isinstance(tr, list) else None
    oldest = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    ok_tr = isinstance(trd, dict) and all(near(trd.get(k), v) for k, v in O["trend"].items()) and all(k > oldest for k in trd if k) \
        and all(v is None for k, v in trd.items() if k not in O["trend"]) and len(tr) <= 14
    add("A_first_audio_trend_14_daily_medians", ok_tr, (tr, O["trend"]))
    add("A_items_distinct_figures_tables", d.get("items") == {"figures": O["figures"], "tables": O["tables"]}, (d.get("items"), O["figures"], O["tables"]))
    comp = d.get("completion") or {}
    add("A_completion_counts", all(comp.get(k) == v for k, v in O["comp"].items()), (comp, O["comp"]))
    hl = d.get("highlight") or {}
    add("A_highlight_shows_landed_hidden", (hl.get("shows"), hl.get("landed"), hl.get("hidden")) == (O["shows"], O["landed"], O["hidden"]), (hl, O["shows"], O["landed"], O["hidden"]))
    add("A_highlight_no_cue_sentences", (hl.get("no_cue_sentences"), hl.get("sentences")) == (O["no_cue"], O["sentences"]), (hl.get("no_cue_sentences"), hl.get("sentences"), O["no_cue"], O["sentences"]))
    rate = hl.get("on_target_rate")
    if rate is not None:
        want_r = O["landed"] / (O["shows"] - O["hidden"])
        add("A_highlight_on_target_rate_excludes_hidden", near(rate, want_r, 0.006) or near(rate, want_r * 100, 0.6), (rate, want_r))
    # user_flag timestamps: the app's format is not fixed by the contract; accept either ISO or epoch, but one must count
    flagged = {"iso": hl.get("flagged")}
    h2 = make_stats_home(app_ts="num"); rc2, d2, _, _ = stats_json(h2, 7); flagged["num"] = ((d2 or {}).get("highlight") or {}).get("flagged")
    add("A_highlight_flagged_user_flag_events_in_window", O["flagged"] in flagged.values() and all(v in (O["flagged"], 0, None) for v in flagged.values()), (flagged, O["flagged"]))
    add("A_speech_wpm", near(d.get("speech_wpm"), O["wpm"], 1.0), (d.get("speech_wpm"), O["wpm"]))
    docs = d.get("documents")
    add("A_documents_list", isinstance(docs, list) and {x.get("doc") for x in docs if isinstance(x, dict)} == set(O["docs"]), (docs if not isinstance(docs, list) else [x.get("doc") for x in docs], sorted(O["docs"])))
    for x in docs if isinstance(docs, list) else []:
        w = O["docs"].get(x.get("doc"))
        if not w: continue
        tag = {DOC_A: "A", DOC_B: "B", DOC_C: "C"}[x["doc"]]
        wa = O["docs_all"][x["doc"]]
        same = lambda w_: sorted(x.get("pages") or []) == sorted(w_["pages"]) and sorted(x.get("items") or []) == sorted(w_["items"]) and x.get("reads") == w_["reads"]
        add(f"A_doc{tag}_pages_items_reads", same(w) or same(wa), (x.get("pages"), x.get("items"), x.get("reads"), "window:", sorted(w["pages"]), sorted(w["items"]), w["reads"]))
        if not same(w) and same(wa): info(f"A documents[] count all-time history, not the window (doc {tag})")
        add(f"A_doc{tag}_last_ts", pts(x.get("last_ts")) is not None and abs(pts(x.get("last_ts")) - w["last_ts"]) <= 1.5, (x.get("last_ts"), iso(w["last_ts"])))
        add(f"A_doc{tag}_resume_point", x.get("resume") == w["resume"], (x.get("resume"), w["resume"]))
        add(f"A_doc{tag}_name_from_library_only", x.get("name") == O["names"].get(x["doc"]), (x.get("name"), O["names"].get(x["doc"])))
    probs = d.get("problems")
    got = {p.get("id"): p for p in probs if isinstance(p, dict)} if isinstance(probs, list) else {}
    want = O["problems"]
    extras = set(got) - set(want)
    add("A_problems_ids", set(want) <= set(got) and extras <= {"model-down", "permission"}, (sorted(got), sorted(want)))
    if extras: info(f"A problems beyond the planted ones (live checks?): {sorted(extras)} {[got[e] for e in extras]}")
    for pid, w in want.items():
        g = got.get(pid) or {}
        add(f"A_problem_{pid}_count_last_ts", g.get("count") == w["count"] and pts(g.get("last_ts")) is not None and abs(pts(g.get("last_ts")) - w["last"]) <= 1.5,
            (g, w["count"], iso(w["last"])))
    exp_open = {"cloud-fallback": True, "highlight-failed": True, "layout-missing": False, "slow-start": False, "kokoro-down": False}
    add("A_problems_open_flag_24h", all((got.get(k) or {}).get("open") is v for k, v in exp_open.items() if k in got), {k: (got.get(k) or {}).get("open") for k in exp_open})
    sk = find(d, "skipped_lines")
    add("A_malformed_line_skipped_and_counted", sk == 1, sk)
    # output strings: enums, hashes, ISO times, fig_ids, library names only; never the library path or any planted text
    blob = json.dumps(d)
    bad = [s for s in walk_strings(d) if s != LIB_NAME_A and not re.fullmatch(r"[a-z0-9_.:+\-]{1,40}|[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.]+([+\-][0-9:]{4,5}|Z)?)?", s)]
    add("A_output_strings_whitelisted", not bad, bad[:6])
    leaks = [w for w in PRIV + [LIB_PATH_A, "quokka-ledger-9931", "/Users/"] if w.lower() in blob.lower()]
    add("A_privacy_no_text_path_in_json", not leaks, leaks)
    rc, out, err, _ = cli(["stats"] + ["--window", "7"], h)
    lines = [x for x in out.splitlines() if x.strip()]
    tl = [w for w in PRIV + [LIB_PATH_A, "quokka-ledger-9931"] if w.lower() in (out + err).lower()]
    add("A_stats_text_five_lines", rc == 0 and len(lines) == 5, (rc, len(lines), [re.sub(r"[A-Za-z]{4,}", "w", x)[:80] for x in lines[:8]]))
    add("A_privacy_no_text_in_stats_text", not tl, tl)
    add("A_stats_text_no_deleted_metrics_no_em_dash", not re.search(r"streak|time saved|words in|—", out, re.I), [x for x in lines if re.search(r"streak|time saved|words in|—", x, re.I)][:3])
    add("A_stats_text_first_line_reads_and_minutes", bool(lines) and re.search(rf"\b{O['reads']['window']}\b", lines[0]) is not None and "min" in lines[0], lines[:1])
    # other windows
    for win in (30, 1):
        rcw, dw, errw, _ = stats_json(h, win); Ow = oracle(win)
        if not isinstance(dw, dict): add(f"A_window_{win}_parses", False, (rcw, errw[-200:])); continue
        gp = {p.get("id"): p.get("count") for p in dw.get("problems") or [] if isinstance(p, dict)}
        ok = dw.get("window_days") == win and dw.get("reads") == Ow["reads"] and (dw.get("first_audio_s") or {}).get("n") == len(Ow["fa"]) \
            and all(gp.get(k) == v["count"] for k, v in Ow["problems"].items()) and dw.get("items") == {"figures": Ow["figures"], "tables": Ow["tables"]}
        add(f"A_window_{win}_reads_items_problems", ok, (dw.get("window_days"), dw.get("reads"), Ow["reads"], (dw.get("first_audio_s") or {}).get("n"), len(Ow["fa"]), gp,
                                                       {k: v["count"] for k, v in Ow["problems"].items()}, dw.get("items"), Ow["figures"], Ow["tables"]))
    rc0, d0, _, _ = stats_json(h)
    add("A_default_window_is_7", isinstance(d0, dict) and d0.get("window_days") == 7 and d0.get("reads") == O["reads"], (d0 or {}).get("window_days"))
    rcx, _, errx, _ = cli(["stats", "--json", "--window", "5"], h)
    add("A_window_only_1_7_30", rcx != 0, (rcx, errx[-120:]))
    # failure cases
    he = mkhome("empty"); rc, de, err, _ = stats_json(he)
    ok = rc == 0 and isinstance(de, dict) and de.get("schema") == 2 and (de.get("reads") or {}).get("window") == 0 and (de.get("reads") or {}).get("today") == 0 \
        and de.get("documents") == [] and [p for p in de.get("problems") or [] if p.get("id") not in ("model-down", "permission", "kokoro-down")] == []
    add("A_missing_runs_log_zeros_empty_lists", ok, (rc, de, err[-200:]))
    hn = make_stats_home(tracker=False); rc, dn, err, _ = stats_json(hn)
    add("A_tracker_absent_highlight_null", rc == 0 and isinstance(dn, dict) and "highlight" in dn and dn.get("highlight") is None, (rc, (dn or {}).get("highlight")))
    hu = make_stats_home(); open(os.path.join(hu, "library.json"), "w").write("{not json")
    rc, du, err, _ = stats_json(hu)
    add("A_library_unreadable_names_null_no_error", rc == 0 and isinstance(du, dict) and all(x.get("name") is None for x in du.get("documents") or []) and len(du.get("documents") or []) == 3, (rc, err[-200:]))
    hu2 = make_stats_home(); os.chmod(os.path.join(hu2, "library.json"), 0)
    rc, du2, err, _ = stats_json(hu2); os.chmod(os.path.join(hu2, "library.json"), 0o600)
    add("A_library_no_permission_names_null_no_error", rc == 0 and isinstance(du2, dict) and all(x.get("name") is None for x in du2.get("documents") or []), (rc, err[-200:]))
    for how in ("env", "config"):
        ho = make_stats_home(extra_config="SPEAKEASY_DOC_NAMES=off\n" if how == "config" else "")
        rc, do, err, _ = stats_json(ho, **({"SPEAKEASY_DOC_NAMES": "off"} if how == "env" else {}))
        add(f"A_names_off_{how}_every_name_null", rc == 0 and isinstance(do, dict) and do.get("documents") and all(x.get("name") is None for x in do["documents"]), (rc, [(x.get("doc"), x.get("name")) for x in (do or {}).get("documents") or []]))
    # A7.7 speed on a copy of the user's real logs
    if os.path.isdir(A.real_logs):
        hr = mkhome("realcopy"); os.makedirs(os.path.join(hr, "logs"))
        for f in glob.glob(os.path.join(A.real_logs, "*.jsonl*")): shutil.copy2(f, os.path.join(hr, "logs"))
        n_runs = len(jl(os.path.join(hr, "logs", "runs.jsonl"))); n_trk = sum(1 for _ in open(os.path.join(hr, "logs", "tracker.jsonl"))) if os.path.exists(os.path.join(hr, "logs", "tracker.jsonl")) else 0
        mss = []
        for _ in range(3):
            rc, dr, err, ms = stats_json(hr); mss.append(ms)
        add("A_real_logs_copy_parses", rc == 0 and isinstance(dr, dict) and dr.get("schema") == 2, (rc, err[-200:]))
        add("A_real_logs_copy_under_500ms", statistics.median(mss) < 500, f"median {statistics.median(mss):.0f} ms of {[round(m) for m in mss]} ({n_runs} runs, {n_trk} tracker lines)")
        info(f"A real copy: reads {(dr or {}).get('reads')} first_audio {((dr or {}).get('first_audio_s') or {}).get('median')} / p90 {((dr or {}).get('first_audio_s') or {}).get('p90')} by_provider {((dr or {}).get('first_audio_s') or {}).get('by_provider')} problems {[(p.get('id'), p.get('count')) for p in (dr or {}).get('problems') or []]}")
        rt = [r for r in jl(os.path.join(hr, "logs", "runs.jsonl")) if isinstance(r, dict)]
        texts = [r.get(k) for r in rt for k in ("error", "llm_error", "tts_error", "notes", "stream_note", "doc_note") if isinstance(r.get(k), str) and len(r.get(k)) > 12]
        hits = [t[:20] for t in texts if t in json.dumps(dr)]
        add("A_real_copy_no_error_or_note_strings_in_output", not hits, len(hits))
        # A7.5 cue_result invariants on recorded tracker output (only once real runs of the new tracker exist)
        T = [r for r in jl(os.path.join(hr, "logs", "tracker.jsonl")) if isinstance(r, dict)]
        cr = [r for r in T if r.get("event") == "cue_result"]
        if not cr: skip("A_tracker_cue_result_invariants", "no cue_result records in the recorded tracker log yet")
        else:
            bad = []
            for tp in {r.get("tracker_pid") for r in cr}:
                mine = [r for r in cr if r.get("tracker_pid") == tp]; ex = [r for r in T if r.get("event") == "exit" and r.get("tracker_pid") == tp]
                seqs = {r.get("seq") for r in mine}
                if ex and (ex[-1].get("cues") or {}) != {"shows": len(mine), "landed": sum(bool(r.get("landed")) for r in mine), "hidden": sum(bool(r.get("hidden")) for r in mine)}: bad.append(("exit.cues", tp))
                bad += [("correction.seq", tp) for r in T if r.get("event") == "correction" and r.get("tracker_pid") == tp and r.get("seq") not in seqs]
                bad += [("fields", tp) for r in mine if not {"ts", "tracker_pid", "run_id", "seq", "cue_kind", "first_draw_ms", "corrections", "max_shift_px", "landed", "hidden"} <= set(r)]
            add("A_tracker_cue_result_invariants", not bad, bad[:5])
    else: skip("A_real_logs_copy_under_500ms", f"no real logs at {A.real_logs}")

# ================================================================ L. document library written by the backend
@section("L")
def library():
    src = os.path.join(PUB, "syn-fig5-01", "doc.pdf")
    h = mkhome("lib"); d = os.path.dirname(h)
    titled = os.path.join(d, "zqxname7731.pdf"); title = "Quokka Ledger Study 4471"
    if not pdf_with_title(src, titled, title): skip("L_library_title", "could not plant PDF title metadata"); return
    rc, err, c = speak_doc(h, titled, run_id="lib1")
    r = last_run(h)
    add("L_doc_read_ok", rc == 0 and r is not None and bool(r.get("doc_path_hash")), (rc, err[-300:], (r or {}).get("doc_path_hash")))
    if not r or not r.get("doc_path_hash"): return
    lp = os.path.join(h, "library.json")
    try: lib = json.load(open(lp))
    except Exception as e: lib = None; add("L_library_json_written", False, f"{type(e).__name__}: {e}"); return
    add("L_library_json_written", isinstance(lib, dict) and len(lib) == 1, list(lib) if isinstance(lib, dict) else lib)
    ent = lib.get(r["doc_path_hash"]) if isinstance(lib, dict) else None
    add("L_library_key_is_run_doc_path_hash", isinstance(ent, dict), (list(lib), r["doc_path_hash"]))
    add("L_library_mode_0600", (os.stat(lp).st_mode & 0o777) == 0o600, oct(os.stat(lp).st_mode & 0o777))
    add("L_library_outside_logs", not os.path.exists(os.path.join(h, "logs", "library.json")), "")
    if isinstance(ent, dict):
        add("L_entry_fields", set(ent) >= {"name", "path", "first_ts", "last_ts"}, sorted(ent))
        add("L_name_is_pdf_title_metadata", ent.get("name") == title, ent.get("name"))
        add("L_path_is_resolved_path", isinstance(ent.get("path"), str) and os.path.realpath(os.path.expanduser(ent["path"])) == os.path.realpath(titled), ent.get("path"))
    leftovers = [n for n in os.listdir(h) if n.startswith("library") and n != "library.json"]
    add("L_atomic_write_no_temp_left", not leftovers, leftovers)
    def grep_logs(home, words):
        hits = []
        for f in glob.glob(os.path.join(home, "logs", "**", "*"), recursive=True):
            if os.path.isfile(f):
                b = open(f, "rb").read().decode("utf-8", "ignore").lower()
                hits += [(os.path.basename(f), w) for w in words if w.lower() in b]
        return hits
    words = [title, "Quokka Ledger", "zqxname7731", titled]
    add("L_name_and_path_never_in_logs", not grep_logs(h, words), grep_logs(h, words)[:5])
    rc, ds, err, _ = stats_json(h)
    dd = next((x for x in (ds or {}).get("documents") or [] if x.get("doc") == r["doc_path_hash"]), None)
    add("L_stats_shows_name_never_path", isinstance(dd, dict) and dd.get("name") == title and titled not in json.dumps(ds) and "zqxname7731" not in json.dumps(ds), (dd, err[-200:]))
    # second read: same entry, last_ts moves, first_ts stays
    ent1 = dict(ent or {}); time.sleep(1.2)
    rc, err, _ = speak_doc(h, titled, run_id="lib2")
    try: lib2 = json.load(open(lp))
    except Exception: lib2 = {}
    e2 = lib2.get(r["doc_path_hash"]) or {}
    add("L_second_read_updates_last_ts_only", len(lib2) == 1 and e2.get("first_ts") == ent1.get("first_ts") and pts(e2.get("last_ts")) is not None and pts(ent1.get("last_ts")) is not None and pts(e2["last_ts"]) >= pts(ent1["last_ts"]) and e2.get("last_ts") != ent1.get("last_ts"),
        (ent1.get("first_ts"), ent1.get("last_ts"), e2.get("first_ts"), e2.get("last_ts")))
    # name rules: empty title and long title fall back to the file name
    for tag, t in (("empty_title", ""), ("long_title", "A" * 60 + " " + "B" * 70)):
        hh = mkhome("lib" + tag); fn = os.path.join(os.path.dirname(hh), f"zqx{tag}5512.pdf")
        if not pdf_with_title(src, fn, t): skip(f"L_name_{tag}_is_file_name", "title not planted"); continue
        speak_doc(hh, fn, run_id="l" + tag)
        try: lb = json.load(open(os.path.join(hh, "library.json")))
        except Exception: lb = {}
        names = [v.get("name") for v in lb.values()] if isinstance(lb, dict) else []
        add(f"L_name_{tag}_is_file_name", names in ([os.path.basename(fn)], [os.path.splitext(os.path.basename(fn))[0]]), names)
    # names off: no new entries; the file goes away; stats names null
    for how in ("env", "config"):
        ho = mkhome("liboff"); shutil.copyfile(lp, os.path.join(ho, "library.json")); os.chmod(os.path.join(ho, "library.json"), 0o600)
        before = open(os.path.join(ho, "library.json")).read()
        kw = {"SPEAKEASY_DOC_NAMES": "off"} if how == "env" else {}
        if how == "config": open(os.path.join(ho, "config"), "w").write("SPEAKEASY_DOC_NAMES=off\n")
        other = os.path.join(os.path.dirname(ho), "zqxoff8812.pdf"); shutil.copyfile(src, other)
        speak_doc(ho, other, run_id="loff", **kw)
        exists = os.path.exists(os.path.join(ho, "library.json"))
        now_lib = open(os.path.join(ho, "library.json")).read() if exists else None
        add(f"L_names_off_{how}_no_new_entry", now_lib in (None, before) and "zqxoff8812" not in (now_lib or ""), (exists, (now_lib or "")[:80]))
        add(f"L_names_off_{how}_file_deleted", not exists, "library.json still present after a read with names off")
        rc, do, _, _ = stats_json(ho, **kw)
        add(f"L_names_off_{how}_stats_names_null", isinstance(do, dict) and all(x.get("name") is None for x in do.get("documents") or []), [(x.get("doc"), x.get("name")) for x in (do or {}).get("documents") or []])

# ================================================================ R. new runs fields (A3) and live state
def read_text(home, n=6, play=0.3, **kw):
    p = start_speak(home, " ".join(SENTS[:n]), " ".join(SENTS[:n]), play=play, **kw); return p
@section("R")
def runs_fields():
    h = mkhome("fin"); p = read_text(h, n=4, play=0.25)
    states = []
    def poll():
        sp = os.path.join(h, "state.json")
        try: states.append(json.load(open(sp)))
        except Exception: pass
        return p.poll() is not None
    wait_for(poll, 90, 0.05); rc, err = finish(p); r = last_run(h) or {}
    ns = len(starts(h))
    add("R_finished_read_ok", rc == 0 and bool(r), (rc, err[-300:]))
    add("R_end_finished", r.get("end") == "finished", r.get("end"))
    add("R_finished_sent_spoken_eq_sent_n", isint(r.get("sent_n")) and r.get("sent_spoken") == r.get("sent_n") and r.get("sent_n") >= 1, (r.get("sent_n"), r.get("sent_spoken"), ns))
    add("R_sent_spoken_matches_players_started", r.get("sent_spoken") == ns, (r.get("sent_spoken"), ns))
    add("R_paused_ms_skips_zero_ints", r.get("paused_ms") == 0 and r.get("skips") == 0 and isint(r.get("paused_ms")) and isint(r.get("skips")), (r.get("paused_ms"), r.get("skips")))
    add("R_fig_ids_list_empty_without_doc", r.get("fig_ids") == [], r.get("fig_ids"))
    add("R_tts_fallback_null_when_kokoro_ok", "tts_fallback" in r and r.get("tts_fallback") is None and r.get("tts") == "kokoro", (r.get("tts"), r.get("tts_fallback")))
    sp = [s for s in states if s.get("state") == "speaking"]
    want = ["run_id", "sent_i", "sent_n", "paused", "started_ms"]
    miss = sorted({k for k in want if not any(k in s for s in sp)})
    add("R_state_json_live_fields_while_speaking", bool(sp) and not miss, (len(states), len(sp), miss))
    nocue = sorted({k for k in ("seq", "cue_kind", "label") if not any(k in s for s in sp)})
    if nocue: info(f"R text read without cues: state.json lacks {nocue} (checked on the PDF read below)")
    if sp:
        add("R_state_json_paused_false_while_playing", all(s.get("paused") is False for s in sp if "paused" in s), [s.get("paused") for s in sp][:5])
        si = [s.get("sent_i") for s in sp if isint(s.get("sent_i"))]
        add("R_state_json_sent_i_advances", len(set(si)) >= 2 and si == sorted(si), si[:20])
        add("R_state_json_run_id_matches_log", any(s.get("run_id") == r.get("run_id") for s in sp) if r.get("run_id") else True, (r.get("run_id"), sp[0].get("run_id")))
        sm = [s.get("started_ms") for s in sp if isnum(s.get("started_ms"))]
        add("R_state_json_started_ms_stable", len(set(sm)) == 1, sorted(set(sm))[:4])
    # stopped after the second sentence
    h = mkhome("stop"); p = read_text(h, n=6, play=1.5)
    got = wait_for(lambda: len(starts(h)) >= 2, 60)
    time.sleep(0.4); pid = speak_pid(h)
    if got and pid: os.kill(pid, signal.SIGTERM)
    rc, err = finish(p); r = last_run(h) or {}
    add("R_stopped_read_end_stopped", got and r.get("end") == "stopped", (bool(got), pid, r.get("end"), rc))
    add("R_stopped_read_sent_spoken_2", r.get("sent_spoken") == 2, (r.get("sent_spoken"), len(starts(h))))
    add("R_stopped_read_sent_n_planned", isint(r.get("sent_n")) and r.get("sent_n") >= 3, r.get("sent_n"))
    # Kokoro down / erroring: say is used and tts_fallback names the class
    for tag, url, okv in (("unavailable", dead_url(), ("unavailable",)), ("http500", tts500(), ("error", "unavailable"))):
        h = mkhome("kfb" + tag); p = read_text(h, n=2, play=0.1, tts_url=url); rc, err = finish(p); r = last_run(h) or {}
        add(f"R_kokoro_{tag}_tts_fallback", r.get("tts_requested") == "kokoro" and r.get("tts") == "say" and r.get("tts_fallback") in okv, (r.get("tts_requested"), r.get("tts"), r.get("tts_fallback")))
    cmds = [x for f in glob.glob(os.path.join(TMPROOT, "homes", "*", "stub.jsonl.cmds")) for x in open(f)]
    if cmds: info(f"R backend called stubbed launchctl/open: {cmds[:3]}")
    # selection path: fields present too
    h = mkhome("sel"); llm().set(SENTS[0])
    e = penv(h, **back_env(h, play=0.1))
    sp_ = subprocess.run([CLI, "selection", "--capture-method", "none", "--app", "menubar"], input=SENTS[0], capture_output=True, text=True, env=e, timeout=90)
    r = last_run(h) or {}
    add("R_selection_run_has_new_fields", all(k in r for k in ("end", "sent_n", "sent_spoken", "paused_ms", "skips", "fig_ids", "tts_fallback")), (sp_.returncode, sorted(k for k in ("end", "sent_n", "sent_spoken", "paused_ms", "skips", "fig_ids", "tts_fallback") if k not in r)))
    # figure read on a PDF: fig_ids format p<page>:fig<n> / tab<n>, consistent with the cues drawn
    for doc in ("syn-fig5-01", "syn-panels-01"):
        h = mkhome("fig"); pdf = os.path.join(os.path.dirname(h), f"{doc}.pdf"); shutil.copyfile(os.path.join(PUB, doc, "doc.pdf"), pdf)
        pick = lambda c: any(re.search(r"\b(Figure|Fig\.|panel)\b", s["text"]) for s in c["script"])
        p, c = speak_doc(h, pdf, case_doc=doc, pick=pick, run_id="fig-" + doc, play=0.4, wait=False); sts = []
        def poll2():
            try: sts.append(json.load(open(os.path.join(h, "state.json"))))
            except Exception: pass
            return p.poll() is not None
        wait_for(poll2, 120, 0.05); finish(p)
        r = last_run(h) or {}; fi = r.get("fig_ids"); ck = r.get("cue_kinds") or {}
        spk = [s_ for s_ in sts if s_.get("state") == "speaking"]
        cued = [s_ for s_ in spk if isint(s_.get("seq")) and isinstance(s_.get("cue_kind"), str)]
        add(f"R_state_json_seq_cue_kind_label_{doc}", bool(cued) and all("label" in s_ for s_ in spk) and all(s_.get("label") is None or (isinstance(s_["label"], str) and len(s_["label"]) <= 40) for s_ in spk),
            (len(spk), len(cued), sorted({k for s_ in spk for k in s_})))
        if doc == "syn-fig5-01": info(f"R state.json labels seen {sorted({str(s_.get('label')) for s_ in spk})[:5]} cue kinds {sorted({str(s_.get('cue_kind')) for s_ in spk})}")
        boxed = sum(ck.get(k, 0) for k in ("figure_box", "part_oval", "table_row", "panel_box"))
        okfmt = isinstance(fi, list) and all(isinstance(x, str) and re.fullmatch(rf"p{r.get('page')}:(fig|tab)[1-9]\d*", x) for x in fi) and len(fi) == len(set(fi))
        add(f"R_fig_ids_format_{doc}", okfmt, (fi, r.get("page")))
        add(f"R_fig_ids_present_iff_figure_cue_{doc}", isinstance(fi, list) and (bool(fi) == (boxed > 0)), (fi, ck))

# ================================================================ K. control channel (B6): SIGUSR2 pause, SIGUSR1 next, SIGTERM stop
def ps_stat(pid):
    try: return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except Exception: return ""
@section("K")
def controls():
    h = mkhome("ctl"); p = read_text(h, n=6, play=1.2)
    if not wait_for(lambda: len(starts(h)) >= 1 and speak_pid(h), 60): add("K_read_started", False, "no player start"); finish(p); return
    time.sleep(0.3); pid = speak_pid(h); s1 = starts(h)[-1]
    t_pause = time.time(); os.kill(pid, signal.SIGUSR2)
    st = wait_for(lambda: (json.load(open(os.path.join(h, "state.json"))) if os.path.exists(os.path.join(h, "state.json")) else {}).get("paused") is True, 2.0)
    add("K_pause_sets_state_paused", bool(st), "state.json paused never true")
    time.sleep(0.3); stt = ps_stat(s1["pid"])
    add("K_pause_stops_player_process", stt.startswith("T"), (s1["pid"], stt))
    n_before = len(starts(h)); time.sleep(1.8)
    add("K_no_new_sentence_while_paused", len(starts(h)) == n_before, (n_before, len(starts(h))))
    os.kill(pid, signal.SIGUSR2); t_res = time.time()
    st = wait_for(lambda: (json.load(open(os.path.join(h, "state.json"))) if os.path.exists(os.path.join(h, "state.json")) else {}).get("paused") is False, 2.0)
    add("K_resume_clears_state_paused", bool(st), "")
    time.sleep(0.2)
    add("K_resume_continues_player", not ps_stat(s1["pid"]).startswith("T"), ps_stat(s1["pid"]))
    cur = starts(h)[-1]; n0 = len(starts(h)); os.kill(pid, signal.SIGUSR1)
    nxt = wait_for(lambda: len(starts(h)) > n0, 3.0)
    killed = any(e.get("event") == "killed" and e.get("pid") == cur["pid"] for e in stub_events(h))
    add("K_next_kills_current_and_plays_next", bool(nxt) and killed, (bool(nxt), killed))
    time.sleep(0.4); os.kill(pid, signal.SIGTERM); t_end = time.time()
    rc, err = finish(p); r = last_run(h) or {}
    pm = r.get("paused_ms")
    add("K_paused_ms_measured", isint(pm) and 1500 <= pm <= (t_res - t_pause) * 1000 + 400, (pm, round((t_res - t_pause) * 1000)))
    add("K_skips_1", r.get("skips") == 1, r.get("skips"))
    add("K_end_stopped_after_controls", r.get("end") == "stopped", r.get("end"))
    wall = t_end - s1["t"]
    add("K_audio_s_excludes_pause", isnum(r.get("audio_s")) and r["audio_s"] <= wall - 1.2, (r.get("audio_s"), round(wall, 2)))
    # resume after the player died acts as Next
    h = mkhome("ctl2"); p = read_text(h, n=4, play=1.0)
    if wait_for(lambda: len(starts(h)) >= 1 and speak_pid(h), 60):
        time.sleep(0.3); pid = speak_pid(h); cur = starts(h)[-1]; os.kill(pid, signal.SIGUSR2); time.sleep(0.5)
        try: os.killpg(cur["pgid"], signal.SIGKILL)
        except Exception:
            try: os.kill(cur["pid"], signal.SIGKILL)
            except Exception: pass
        n0 = len(starts(h)); time.sleep(0.3); os.kill(pid, signal.SIGUSR2)
        nxt = wait_for(lambda: len(starts(h)) > n0, 4.0)
        rc, err = finish(p); r = last_run(h) or {}
        add("K_resume_after_player_died_plays_next", bool(nxt) and rc == 0 and r.get("end") in ("finished", "stopped"), (bool(nxt), rc, r.get("end"), err[-200:]))
    else: add("K_resume_after_player_died_plays_next", False, "read did not start"); finish(p)
    # signals with no read running: nothing breaks (stale pid file)
    h = mkhome("ctl3"); open(os.path.join(h, "speak.pid"), "w").write("999999\n")
    p = read_text(h, n=2, play=0.1); rc, err = finish(p)
    add("K_stale_speak_pid_ignored", rc == 0 and (last_run(h) or {}).get("end") == "finished", (rc, (last_run(h) or {}).get("end")))

# ================================================================ S. speakeasy logs --scrub (A5.4)
SUP_OLD = "llm: superlative removed: superlative 'highest' on 0.93 contradicts BLEU"
SUP_NEW = "llm: superlative removed: superlative 'highest' contradicts its column"
def scrub_fixture(h):
    lg = os.path.join(h, "logs"); os.makedirs(lg, exist_ok=True)
    F = [
        {"ts": 1792872946.1, "state": "show", "seq": 1, "rect": [10.0, 20.0, 300.0, 18.0], "sentence_id": "s1", "text": "Alpha beta gamma delta.", "src": ["line one zebra", "line two yak"],
         "ref_src": ["ref line kangaroo"], "ref_lines": [3, 4], "doc": {"label": "Figure 4", "kind": "figure", "page": 2}, "color": "#3B82F6", "opacity": 0.22, "border": True, "pid": 11},
        {"ts": 1792872946.2, "state": "map", "sent": 0, "method": "align", "lines": [1, 2], "sentence_id": "s2", "text": "Quietly the marmot sings",
         "doc": {"label": "Figure 4 shows the encoder pipeline", "page": 2, "kind": "figure"}, "pid": 11},
        {"ts": 1792872946.3, "state": "show", "seq": 2, "rect": [1.0, 2.0, 3.0, 4.0], "sentence_id": "s3", "text_len": 12, "text_hash": h12("clean record"), "pid": 11},
        {"ts": 1792872946.4, "state": "show", "seq": 3, "rect": [1.0, 2.0, 3.0, 4.0], "sentence_id": "s4", "text": "Already hashed sentence", "text_hash": "deadbeef0000", "text_len": 999, "pid": 11},
        {"ts": 1792872946.5, "state": "sentence_map", "sentences": 4, "mapped": 4, "method": "page", "ms": 1, "pid": 11},
        {"ts": 1792872946.6, "state": "map", "sent": 1, "method": "doc-object", "sentence_id": "s5", "doc": {"label": "Table 2 (a)", "page": 3}, "pid": 11},
        {"ts": 1792872946.7, "state": "show", "seq": 4, "rect": [5.0, 6.0, 7.0, 8.0], "sentence_id": "s6", "src": ["only source line pelican"], "pid": 11},
        {"ts": 1792872946.8, "state": "show", "seq": 5, "rect": [5.0, 6.0, 7.0, 8.0], "sentence_id": "s7", "doc": {"label": "Figure 3 panel b", "page": 1}, "pid": 11},
        {"ts": 1792872946.9, "state": "map", "sent": 2, "sentence_id": "s8", "text": "", "method": "hold", "pid": 11},
        {"ts": 1792872947.0, "state": "clear", "pid": 11},
    ]
    F1 = [{"ts": 1792800000.0, "state": "show", "seq": 9, "rect": [1.0, 1.0, 1.0, 1.0], "sentence_id": "r1", "text": "Rotated record wombat", "pid": 7}]
    F3 = [{"ts": 1792700000.0, "state": "show", "seq": 1, "rect": [1.0, 1.0, 1.0, 1.0], "text_len": 3, "text_hash": h12("abc"), "pid": 3}]
    Rn = [
        {"ts": "2026-10-24T10:00:00-0700", "command": "speak", "run_id": "a", "input": "Secret input otter", "output": "Secret output badger", "error": None, "words_spoken": 4, "audio_s": 1.0, "dry_run": False, "app": "cli"},
        {"ts": "2026-10-24T10:01:00-0700", "command": "speak", "run_id": "b", "error": SUP_OLD, "llm_error": SUP_OLD, "notes": None, "words_spoken": 4, "audio_s": 1.0, "dry_run": False, "app": "cli"},
        {"ts": "2026-10-24T10:02:00-0700", "command": "speak", "run_id": "c", "notes": "superlative 'lowest' on 12 contradicts Latency", "error": None, "dry_run": False, "app": "cli"},
        {"ts": "2026-10-24T10:03:00-0700", "command": "speak", "run_id": "d", "error": "tts: kokoro unavailable", "dry_run": False, "app": "cli"},
        {"ts": "2026-10-24T10:04:00-0700", "command": "speak", "run_id": "e", "llm_error": SUP_NEW, "dry_run": False, "app": "cli"},
        {"ts": "2026-10-24T10:05:00-0700", "command": "scrub", "files": 0, "records_changed": {"runs": 0, "focus": 0}, "fields": {}, "other_logs_with_text": 0, "ms": 1},
    ]
    R2 = [{"ts": "2026-10-20T10:00:00-0700", "command": "selection", "run_id": "z", "input": "Old rotated input heron", "dry_run": False, "app": "cli"}]
    T = [{"ts": 1792872946.0, "event": "start", "tracker_pid": 5}, {"ts": 1792872946.1, "event": "draw", "seq": 1, "text": "leaked tracker text", "tracker_pid": 5}]
    Ap = [{"ts": "2026-10-24T10:00:00-0700", "event": "bar_state", "from": "idle", "to": "hover"}]
    wjl(os.path.join(lg, "focus.jsonl"), F); wjl(os.path.join(lg, "focus.jsonl.1"), F1); wjl(os.path.join(lg, "focus.jsonl.3"), F3)
    wjl(os.path.join(lg, "runs.jsonl"), Rn); wjl(os.path.join(lg, "runs.jsonl.2"), R2); wjl(os.path.join(lg, "tracker.jsonl"), T); wjl(os.path.join(lg, "app.jsonl"), Ap)
    open(os.path.join(lg, "tts-server.log"), "w").write("not a jsonl log; text: Secret input otter\n")
    open(os.path.join(h, "library.json"), "w").write(json.dumps({"ffff00001111": {"name": "Lib Name", "path": "/x/y.pdf", "first_ts": "a", "last_ts": "b"}}))
    open(os.path.join(h, "last_capture.png"), "wb").write(b"\x89PNG fake")
    return dict(F=F, F1=F1, F3=F3, R=Rn, R2=R2,
                exp_records={"focus": 7, "runs": 4},
                exp_fields={"text": 5, "src": 2, "ref_src": 1, "doc.label": 2, "input": 2, "output": 1, "error": 1, "llm_error": 1, "notes": 1})
def snap_dir(d):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "**", "*"), recursive=True)):
        if os.path.isfile(f): out[os.path.relpath(f, d)] = (open(f, "rb").read(), os.stat(f).st_mtime_ns)
    return out
def scrub(h, args=("--yes",), inp=None, **kw): return cli(["logs", "--scrub"] + list(args), h, inp=inp, timeout=120, **kw)
FORBID_RUN_KEYS = ("input", "output")
def has_forbidden(rec, kind):
    if kind == "focus":
        lab = (rec.get("doc") or {}).get("label") if isinstance(rec.get("doc"), dict) else None
        return any(k in rec for k in ("text", "src", "ref_src")) or (isinstance(lab, str) and len(lab.split()) > 3)
    return any(k in rec for k in FORBID_RUN_KEYS) or any(isinstance(rec.get(k), str) and re.search(r"superlative '[^']*' on .*contradicts", rec[k]) for k in ("error", "llm_error", "notes"))
def scrub_event(h):
    ev = [r for r in jl(os.path.join(h, "logs", "runs.jsonl")) if r.get("command") == "scrub"]
    return ev[-1] if ev else None
@section("S")
def scrub_tests():
    h = mkhome("scrub"); fx = scrub_fixture(h); lg = os.path.join(h, "logs")
    rcs, st0, _, _ = stats_json(h)
    before = snap_dir(h)
    # prompt, declined
    rc, out, err, _ = scrub(h, args=(), inp="n\n")
    m = re.search(r"Remove text from (\d+) records in (\d+) files\? \[y/N\]", out + err)
    add("S_prompt_counts", bool(m) and int(m.group(1)) == 11 and int(m.group(2)) == 4, (out + err)[-200:])
    after_no = snap_dir(h)
    changed = [k for k in before if k != "logs/runs.jsonl" and after_no.get(k) != before[k]]
    rb, ra = before["logs/runs.jsonl"][0], after_no.get("logs/runs.jsonl", (b"",))[0]
    add("S_declined_changes_nothing", not changed and ra.startswith(rb) and ra.count(b"\n") - rb.count(b"\n") <= 1 and set(after_no) == set(before), (changed, ra.count(b"\n") - rb.count(b"\n")))
    # refuses while a read is running
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "speakeasy.py", "speak"]); open(os.path.join(h, "speak.pid"), "w").write(f"{live.pid}\n")
    s_lock = snap_dir(lg)
    rc, out, err, _ = scrub(h)
    live.kill(); live.wait(); os.remove(os.path.join(h, "speak.pid"))
    add("S_refuses_during_read_exit2", rc == 2 and "A read is running. Run the scrub after it ends." in out + err, (rc, (out + err)[-160:]))
    add("S_refuses_during_read_nothing_changed", snap_dir(lg) == s_lock, "")
    # injected write failures: read-only logs folder, then a file-size limit that breaks the temp write half way
    s_ro = snap_dir(lg); os.chmod(lg, 0o555)
    try: rc, out, err, _ = scrub(h)
    finally: os.chmod(lg, 0o755)
    add("S_readonly_dir_fails_cleanly", rc != 0 and snap_dir(lg) == s_ro, (rc, (out + err)[-160:]))
    hb = mkhome("scrubfsz"); scrub_fixture(hb); lgb = os.path.join(hb, "logs")
    big = jl(os.path.join(lgb, "focus.jsonl")) * 60; wjl(os.path.join(lgb, "focus.jsonl"), big)
    sz = os.path.getsize(os.path.join(lgb, "focus.jsonl")); s_b = snap_dir(lgb)
    def lim():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN); resource.setrlimit(resource.RLIMIT_FSIZE, (sz // 3, sz // 3))
    p = subprocess.run([CLI, "logs", "--scrub", "--yes"], capture_output=True, text=True, env=penv(hb), preexec_fn=lim, timeout=120)
    s_a = snap_dir(lgb)
    add("S_write_failure_original_byte_identical", s_a.get("focus.jsonl", (None,))[0] == s_b["focus.jsonl"][0], (p.returncode, (p.stdout + p.stderr)[-200:]))
    add("S_write_failure_no_temp_left", set(s_a) == set(s_b), sorted(set(s_a) ^ set(s_b)))
    add("S_write_failure_nonzero_exit", p.returncode != 0, p.returncode)
    # the real scrub
    s_pre = snap_dir(h); names_pre = set(os.listdir(lg)); home_pre = set(os.listdir(h))
    rc, out, err, _ = scrub(h)
    add("S_scrub_yes_exit0", rc == 0, (rc, (out + err)[-300:]))
    add("S_no_new_files_logs_or_home", set(os.listdir(lg)) == names_pre and set(os.listdir(h)) == home_pre, (sorted(set(os.listdir(lg)) ^ names_pre), sorted(set(os.listdir(h)) ^ home_pre)))
    bak = [f for f in glob.glob(os.path.join(TMPROOT, "homes", "**", "*"), recursive=True) if re.search(r"\.(bak|orig|tmp)$|~$", f) and "scrub" in f]
    add("S_no_backup_anywhere", not bak, bak[:4])
    ev = scrub_event(h) or {}
    rc_ = ev.get("records_changed") or {}; fl = ev.get("fields") or {}
    add("S_event_records_changed_exact", rc_ == fx["exp_records"], (rc_, fx["exp_records"]))
    add("S_event_fields_exact", {k: fl.get(k) for k in fx["exp_fields"]} == fx["exp_fields"], (fl, fx["exp_fields"]))
    add("S_event_other_logs_with_text_1", ev.get("other_logs_with_text") == 1, ev.get("other_logs_with_text"))
    add("S_event_files_count", ev.get("files") in (4, 5, 7), ev.get("files"))
    req = {"ts", "command", "files", "records_changed", "fields", "other_logs_with_text", "ms"}
    strs = [(k, v) for k, v in ev.items() if isinstance(v, str) and k not in ("ts", "command")]
    add("S_event_count_only_shape", req <= set(ev) and not strs and isint(ev.get("ms")) and all(isint(v) for v in fl.values()) and all(isint(v) for v in rc_.values()), (sorted(ev), strs))
    if set(ev) - req: info(f"S scrub event extra keys {sorted(set(ev) - req)}")
    add("S_prints_counts_only", not re.search(r"otter|badger|zebra|marmot|wombat|heron|kangaroo|pelican|encoder pipeline|BLEU", out + err, re.I), (out + err)[-200:])
    F = jl(os.path.join(lg, "focus.jsonl")); F1 = jl(os.path.join(lg, "focus.jsonl.1")); Rn = jl(os.path.join(lg, "runs.jsonl")); R2 = jl(os.path.join(lg, "runs.jsonl.2"))
    left = [(n, i) for n, L, k in (("focus", F, "focus"), ("focus.1", F1, "focus"), ("runs", Rn, "runs"), ("runs.2", R2, "runs")) for i, r in enumerate(L) if has_forbidden(r, k)]
    add("S_zero_forbidden_after", not left, left[:6])
    add("S_record_count_order_unchanged", len(F) == len(fx["F"]) and [r.get("ts") for r in F] == [r["ts"] for r in fx["F"]] and len(F1) == 1
        and [r.get("ts") for r in Rn[:len(fx["R"])]] == [r["ts"] for r in fx["R"]] and len(Rn) == len(fx["R"]) + 1 and len(R2) == 1, (len(F), len(F1), len(Rn), len(R2)))
    keep = ("ts", "state", "seq", "rect", "sentence_id")
    bad = [i for i, (a, b) in enumerate(zip(fx["F"], F)) if any(a.get(k) != b.get(k) for k in keep)]
    add("S_focus_keeps_ts_state_seq_rect_sentence_id", not bad, bad)
    o0, o1 = fx["F"][0], F[0]
    add("S_text_to_len_hash", o1.get("text_len") == len(o0["text"]) and o1.get("text_hash") == h12(o0["text"]), (o1.get("text_len"), o1.get("text_hash"), h12(o0["text"])))
    add("S_empty_text_removed", "text" not in F[8] and F[8].get("text_len") in (0, None) and F[8].get("text_hash") in (h12(""), None), {k: F[8].get(k) for k in ("text_len", "text_hash", "text")})
    add("S_rotated_text_to_len_hash", F1[0].get("text_len") == len(fx["F1"][0]["text"]) and F1[0].get("text_hash") == h12(fx["F1"][0]["text"]), F1[0])
    add("S_existing_hash_not_overwritten", F[3].get("text_hash") == "deadbeef0000" and F[3].get("text_len") == 999 and "text" not in F[3], F[3])
    add("S_src_to_n_hash", "src" not in o1 and isint(o1.get("src_n")) and isinstance(o1.get("src_hash"), str) and re.fullmatch(r"[0-9a-f]{12}", o1.get("src_hash") or "") is not None
        and isint(F[6].get("src_n")) and re.fullmatch(r"[0-9a-f]{12}", F[6].get("src_hash") or "") is not None, ({k: o1.get(k) for k in ("src_n", "src_hash")}, {k: F[6].get(k) for k in ("src_n", "src_hash")}))
    if o1.get("src_n") != len(o0["src"]): info(f"S src_n {o1.get('src_n')} for a {len(o0['src'])}-line src (writer semantics; not gated)")
    add("S_ref_src_removed_ref_lines_kept", "ref_src" not in o1 and o1.get("ref_lines") == [3, 4], {k: o1.get(k) for k in ("ref_src", "ref_lines")})
    add("S_doc_label_rules", (o1.get("doc") or {}).get("label") == "Figure 4" and (F[1].get("doc") or {}).get("label") is None and (F[5].get("doc") or {}).get("label") == "Table 2 (a)"
        and (F[7].get("doc") or {}).get("label") is None and (F[1].get("doc") or {}).get("page") == 2, [(F[i].get("doc") or {}) for i in (0, 1, 5, 7)])
    if "label" in (F[1].get("doc") or {}): info("S long doc.label kept as null rather than removed")
    nontext = [i for i, (a, b) in enumerate(zip(fx["F"], F)) if any(a.get(k) != b.get(k) for k in a if k not in ("text", "src", "ref_src", "doc", "text_len", "text_hash", "src_n", "src_hash"))
               or any(k not in a and k not in ("text_len", "text_hash", "src_n", "src_hash") for k in b)]
    add("S_focus_other_fields_preserved", not nontext, nontext)
    r0, r1, r2, r3, r4 = Rn[0], Rn[1], Rn[2], Rn[3], Rn[4]
    add("S_runs_input_output_removed", "input" not in r0 and "output" not in r0 and r0.get("words_spoken") == 4 and "input" not in R2[0], (sorted(r0), sorted(R2[0])))
    add("S_runs_superlative_note_text_free", r1.get("error") == SUP_NEW and r1.get("llm_error") == SUP_NEW and r2.get("notes") == "superlative 'lowest' contradicts its column", (r1.get("error"), r1.get("llm_error"), r2.get("notes")))
    add("S_runs_clean_records_untouched", r3 == fx["R"][3] and r4 == fx["R"][4] and Rn[5] == fx["R"][5], "")
    others = [i for i, (a, b) in enumerate(zip(fx["R"], Rn)) if any(a.get(k) != b.get(k) for k in a if k not in ("input", "output", "error", "llm_error", "notes")) or set(b) - set(a)]
    add("S_runs_other_fields_preserved", not others, others)
    s_post = snap_dir(h)
    for k in ("logs/tracker.jsonl", "logs/app.jsonl", "logs/focus.jsonl.3", "logs/tts-server.log", "library.json", "last_capture.png"):
        add(f"S_untouched_{k.replace('/', '_').replace('.', '_')}", s_post.get(k) == s_pre.get(k), "")
    rcs2, st1, _, _ = stats_json(h)
    add("S_stats_identical_before_after", st0 is not None and st0 == st1, (json.dumps(st0)[:200], json.dumps(st1)[:200]))
    # idempotent second run
    time.sleep(1.1); s1 = snap_dir(lg)
    rc, out, err, _ = scrub(h); s2 = snap_dir(lg)
    ev2 = scrub_event(h) or {}
    add("S_second_run_zero_changed", rc == 0 and ev2.get("records_changed") == {"runs": 0, "focus": 0} and ev2 is not ev, (rc, ev2.get("records_changed")))
    same = [k for k in s1 if k != "runs.jsonl" and s2.get(k) != s1[k]]
    rb, ra = s1["runs.jsonl"][0], s2["runs.jsonl"][0]
    add("S_second_run_files_bytes_mtime_unchanged", not same and set(s1) == set(s2), same)
    add("S_second_run_only_appends_one_event", ra.startswith(rb) and ra[len(rb):].count(b"\n") == 1 and b'"scrub"' in ra[len(rb):], ra[len(rb):][:200])
    # SPEAKEASY_LOG_TEXT=1: scrubs anyway, warns
    hl = mkhome("scrublt"); scrub_fixture(hl)
    rc, out, err, _ = scrub(hl, SPEAKEASY_LOG_TEXT="1")
    left = [i for i, r in enumerate(jl(os.path.join(hl, "logs", "focus.jsonl"))) if has_forbidden(r, "focus")]
    add("S_log_text_set_scrubs_and_warns", rc == 0 and not left and "SPEAKEASY_LOG_TEXT=1 is set: new records will hold text again." in out + err, (rc, left, (out + err)[-200:]))
    # writer parity: a LOG_TEXT=1 log scrubbed equals the LOG_TEXT-unset writer's form (same input, same mock)
    pair = {}
    for lt in ("1", None):
        hp = mkhome("pair"); pdf = os.path.join(os.path.dirname(hp), "pair.pdf"); shutil.copyfile(os.path.join(PUB, "syn-fig5-01", "doc.pdf"), pdf)
        speak_doc(hp, pdf, run_id="pair", play=0.05, SPEAKEASY_LOG_TEXT=lt); pair[lt] = hp
    rc, out, err, _ = scrub(pair["1"])
    A_, B_ = jl(os.path.join(pair["1"], "logs", "focus.jsonl")), jl(os.path.join(pair[None], "logs", "focus.jsonl"))
    vol = {"ts", "pid", "expires", "ms", "after_ms", "t"}
    diffs = []
    if len(A_) != len(B_): diffs.append(("count", len(A_), len(B_)))
    for i, (a, b) in enumerate(zip(A_, B_)):
        ka, kb = set(a) - vol, set(b) - vol
        if ka != kb: diffs.append((i, a.get("state"), sorted(ka ^ kb)))
        for k in ("text_len", "text_hash", "src_n", "src_hash"):
            if a.get(k) != b.get(k): diffs.append((i, k, a.get(k), b.get(k)))
    add("S_scrubbed_equals_unset_writer_form", rc == 0 and not diffs, diffs[:5])
    ra_ = [r for r in jl(os.path.join(pair["1"], "logs", "runs.jsonl")) if r.get("command") != "scrub"]
    add("S_scrubbed_runs_no_input_output", rc == 0 and ra_ and all("input" not in r and "output" not in r for r in ra_), [sorted(set(r) & {"input", "output"}) for r in ra_])
    # A7.9 on a copy of the user's real logs (never the real files)
    if not os.path.isdir(A.real_logs): skip("S_real_copy", "no real logs"); return
    hr = mkhome("scrubreal"); lr = os.path.join(hr, "logs"); os.makedirs(lr)
    for f in glob.glob(os.path.join(A.real_logs, "*.jsonl*")): shutil.copy2(f, lr)
    want = {"focus": 0, "runs": 0}; recs0 = {}
    for f in sorted(glob.glob(os.path.join(lr, "*.jsonl*"))):
        b = os.path.basename(f); kind = "focus" if b.startswith("focus.jsonl") else "runs" if b.startswith("runs.jsonl") else None
        if not kind or not re.fullmatch(r"(focus|runs)\.jsonl(\.[1-5])?", b): continue
        L = jl(f); recs0[b] = L; want[kind] += sum(1 for r in L if has_forbidden(r, kind))
    st_a = stats_json(hr)[1]
    rc, out, err, _ = scrub(hr)
    ev = scrub_event(hr) or {}
    add("S_real_copy_records_changed", rc == 0 and ev.get("records_changed") == want, (rc, ev.get("records_changed"), want, "(contract: focus 1265, runs 7 on 2026-10-26)"))
    left = [(b, i) for b in recs0 for i, r in enumerate(jl(os.path.join(lr, b))) if has_forbidden(r, "focus" if b.startswith("focus") else "runs")]
    add("S_real_copy_zero_forbidden", not left, left[:5])
    cnt = {b: (len(recs0[b]), len(jl(os.path.join(lr, b))) - (1 if b == "runs.jsonl" else 0)) for b in recs0}
    add("S_real_copy_record_counts_unchanged", all(a == c for a, c in cnt.values()), cnt)
    badhash = 0; tot = 0
    for b in recs0:
        if not b.startswith("focus"): continue
        for a, z in zip(recs0[b], jl(os.path.join(lr, b))):
            if isinstance(a.get("text"), str) and "text_hash" not in a:
                tot += 1; badhash += not (z.get("text_len") == len(a["text"]) and z.get("text_hash") == h12(a["text"]))
    add("S_real_copy_text_len_hash", tot > 0 and badhash == 0, (badhash, tot))
    st_b = stats_json(hr)[1]
    add("S_real_copy_stats_identical", st_a is not None and st_a == st_b, "")

# ================================================================ P. privacy of the new fields (SPEAKEASY_LOG_TEXT unset) + cue tokens
TOKENS = {"ink": "14233B", "navy": "1C3150", "navy-line": "2C4468", "slate": "566577", "paper": "F3F6F8", "paper-2": "E8EEF2", "line": "CBD5DE",
          "ice": "A8D8EC", "glacier": "1F6E99", "ember": "E0892B", "brick": "B23A2B", "bar-text": "E3ECF2",
          "d-paper": "101B2C", "d-paper-2": "182841", "d-slate": "9AAABB", "d-line": "2A3D58"}
@section("P")
def privacy():
    h = mkhome("priv"); p = read_text(h, n=4, play=0.1); finish(p)
    pdfh = mkhome("privdoc"); pdf = os.path.join(os.path.dirname(pdfh), "zqxprivdoc.pdf"); shutil.copyfile(os.path.join(PUB, "syn-fig5-01", "doc.pdf"), pdf)
    rc, err, c = speak_doc(pdfh, pdf, run_id="privdoc", play=0.05)
    words = {w for s in SENTS for w in re.findall(r"[a-z]{7,}", s.lower())} | {"quindle", "zorblax"}
    dwords = {w for st in c["selection"] + c["script"] for w in re.findall(r"[a-z]{8,}", st["text"].lower())} - {"distribution", "component", "components"}
    hits = []
    for home, ws in ((h, words), (pdfh, dwords)):
        for f in glob.glob(os.path.join(home, "logs", "*")):
            vals = " ".join(v.lower() for rec in jl(f) for v in walk_strings(rec))
            for w in ws:
                if w in vals:
                    keys = sorted({k for rec in jl(f) if isinstance(rec, dict) for k, v in rec.items() if w in " ".join(walk_strings(v)).lower()})
                    hits.append((os.path.basename(f), w, keys[:4]))
    add("P_no_text_in_logs_log_text_unset", not hits, hits[:6])
    for home in (h, pdfh):
        r = last_run(home) or {}
        ok = r.get("end") in ("finished", "stopped", "error") and isint(r.get("sent_spoken")) and (r.get("sent_n") is None or isint(r.get("sent_n"))) and isint(r.get("paused_ms")) \
            and isint(r.get("skips")) and isinstance(r.get("fig_ids"), list) and all(re.fullmatch(r"p\d+:(fig|tab)\d+", x) for x in r.get("fig_ids") or []) \
            and r.get("tts_fallback") in (None, "unavailable", "error") and ("capture_bar_hidden" not in r or isinstance(r["capture_bar_hidden"], bool))
        add(f"P_new_runs_fields_types_{'doc' if home == pdfh else 'text'}", ok, {k: r.get(k) for k in ("end", "sent_n", "sent_spoken", "paused_ms", "skips", "fig_ids", "tts_fallback")})
        ap_ = os.path.join(home, "logs", "app.jsonl")
        if os.path.exists(ap_): info(f"P backend wrote app.jsonl in a CLI run ({len(jl(ap_))} records)")
    st = os.path.join(pdfh, "state.json")
    if os.path.exists(st):
        s = json.load(open(st)); bad = [v for v in walk_strings(s) if any(w in v.lower() for w in dwords | words) or "/" in v]
        add("P_state_json_no_text_or_path", not bad, bad[:3])
    shows = [r for r in jl(os.path.join(pdfh, "logs", "focus.jsonl")) + jl(os.path.join(h, "logs", "focus.jsonl")) if r.get("state") == "show" and r.get("color")]
    cols = sorted({str(r.get("color")).upper().lstrip("#") for r in shows})
    add("P_focus_cue_colors_are_tokens", bool(cols) and all(c_ in TOKENS.values() for c_ in cols) and "3B82F6" not in cols, cols)
    bands = [r for r in shows if r.get("cue_kind") in ("text_band", None) and str(r.get("color")).upper().lstrip("#") == "A8D8EC"]
    if bands: add("P_band_ice_38pct_no_outline", all(near(r.get("opacity"), 0.38, 0.011) and not r.get("border") for r in bands), sorted({(r.get("opacity"), r.get("border")) for r in bands}))
    else: info(f"P no ice band show records (cue kinds {sorted({r.get('cue_kind') for r in shows})}, colors {cols})")

# ================================================================ app build + helpers (U, C)
APP = {}
def app_bundle():
    if "path" in APP: return APP["path"]
    APP["path"] = None
    if A.app:
        APP["path"] = A.app if os.path.isdir(A.app) else None; APP["why"] = "--app not found"; return APP["path"]
    bd = os.path.join(TMPROOT, "appbuild"); os.makedirs(os.path.join(bd, "src"))
    shutil.copy(os.path.join(SRC, "tracker.swift"), os.path.join(bd, "src"))
    subprocess.run(["rsync", "-a", "--exclude", ".build", os.path.join(REPO, "app") + "/", os.path.join(bd, "app") + "/"], check=True)
    t = time.time()
    p = subprocess.run(["bash", "build.sh", os.path.join(bd, "out")], cwd=os.path.join(bd, "app"), capture_output=True, text=True, timeout=1800,
                       env={k: v for k, v in os.environ.items() if not k.startswith("SPEAKEASY_") and k != "BUNDLE_PYTHON"})
    app = os.path.join(bd, "out", "Speakeasy.app")
    info(f"app build rc {p.returncode} in {time.time() - t:.0f}s")
    if p.returncode or not os.path.isdir(app): APP["why"] = (p.stdout + p.stderr)[-400:]; return None
    APP["path"] = app; return app
def app_bin(): return os.path.join(app_bundle(), "Contents", "MacOS", "Speakeasy")
def bin_has(s):
    try: return s.encode() in open(app_bin(), "rb").read()
    except Exception: return False
SW = {}
def swift_tool(name):
    if name in SW: return SW[name]
    d = os.path.join(TMPROOT, "swift"); os.makedirs(d, exist_ok=True); srcf = os.path.join(d, name + ".swift"); exe = os.path.join(d, name)
    open(srcf, "w").write(getattr(p7swift, name.upper()))
    p = subprocess.run(["swiftc", "-O", srcf, "-o", exe], capture_output=True, text=True, timeout=600)
    SW[name] = exe if p.returncode == 0 else None
    if p.returncode: info(f"swift helper {name} failed: {p.stderr[-300:]}")
    return SW[name]
def winlist():
    exe = swift_tool("winlist")
    if not exe: return None
    try: return json.loads(subprocess.run([exe], capture_output=True, text=True, timeout=20).stdout)
    except Exception: return None
def ocr(paths):
    exe = swift_tool("ocr")
    if not exe or not paths: return {}
    try: return json.loads(subprocess.run([exe] + list(paths), capture_output=True, text=True, timeout=300).stdout)
    except Exception: return {}
def front():
    try: return subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception: return None

# ================================================================ C. design system (snapshots, fonts)
def hex2rgb(h): return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
TOK_RGB = {k: hex2rgb(v) for k, v in TOKENS.items()}
def load_png(p):
    import numpy as np
    from PIL import Image
    im = Image.open(p).convert("RGBA"); return np.asarray(im).astype(int)
def analyze(arr, window=True):
    import numpy as np
    a = arr[..., 3]; rgb = arr[..., :3]
    op = a == 255; out = {"h": arr.shape[0], "w": arr.shape[1], "opaque": int(op.sum())}
    if not op.any(): out["empty"] = True; return out
    ys, xs = np.where(a > 0); out["bbox_any"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
    yo, xo = np.where(op); out["bbox_op"] = [int(xo.min()), int(yo.min()), int(xo.max()), int(yo.max())]
    px = rgb[op]
    out["white"] = int(((px == 255).all(1)).sum()); out["black"] = int(((px == 0).all(1)).sum())
    wm = op & (rgb == 255).all(-1)
    if wm.any():
        wy, wx = np.where(wm); out["white_bbox"] = [int(wx.min()), int(wy.min()), int(wx.max()), int(wy.max())]
    cols, cnt = np.unique(px, axis=0, return_counts=True)
    out["mode"] = tuple(int(v) for v in cols[cnt.argmax()])
    T = np.array(list(TOK_RGB.values()), float)
    segs = [(T[i], T[j]) for i in range(len(T)) for j in range(i + 1, len(T))]
    def dist(c):
        c = np.asarray(c, float); d = np.abs(T - c).max(1).min()
        if d <= 3: return d
        best = d
        for p0, p1 in segs:
            v = p1 - p0; t = np.clip(np.dot(c - p0, v) / max(np.dot(v, v), 1e-9), 0, 1); best = min(best, np.abs(p0 + t * v - c).max())
        return best
    off = 0; worst = []
    for c, n in zip(cols, cnt):
        dd = dist(c)
        if dd > 6: off += int(n); worst.append((int(n), "#%02X%02X%02X" % tuple(int(v) for v in c)))
    out["off_token_frac"] = off / max(1, len(px)); out["off_token_top"] = sorted(worst, reverse=True)[:5]
    # partially transparent pixels outside the opaque bbox grown by 2 px = shadow / glow
    x0, y0, x1, y1 = out["bbox_op"]; m = (a > 0).copy(); m[max(0, y0 - 2):y1 + 3, max(0, x0 - 2):x1 + 3] = False
    out["outside_alpha"] = int(m.sum())
    return out
def has_color(arr, hx, tol=3, min_px=4):
    import numpy as np
    c = np.array(hex2rgb(hx)); m = (arr[..., 3] == 255) & (np.abs(arr[..., :3] - c).max(-1) <= tol); return int(m.sum()) >= min_px, int(m.sum())
def corner_radius_pt(arr, scale=2):
    a = arr[..., 3]; import numpy as np
    yo, xo = np.where(a >= 128)
    if not len(xo): return None, None
    x0, y0, x1, y1 = xo.min(), yo.min(), xo.max(), yo.max()
    S = int(min(40, (x1 - x0 + 1) // 2, (y1 - y0 + 1) // 2))
    sq = a[y0:y0 + S, x0:x0 + S].astype(float)
    n = float((1 - sq / 255.0).sum())  # transparent area outside the quarter circle = (1 - pi/4) r^2
    return (n / 0.2146) ** 0.5 / scale, int(a[y0, x0])
def tick_frames(meta):
    out = []
    for k, path in walk_keys(meta):
        if "tick" in str(k).lower():
            v = find(meta, k)
            if isinstance(v, list) and len(v) == 4 and all(isnum(x) for x in v): out.append(v)
            elif isinstance(v, dict):
                g = lambda *ks: next((v[x] for x in ks if isnum(v.get(x))), None)
                vals = [g("x", "minX"), g("y", "minY"), g("w", "width"), g("h", "height")]
                if all(isnum(x) for x in vals): out.append(vals)
    return out
def tick_ok(arr, rect, colors):
    import numpy as np
    H, W = arr.shape[:2]; best = 0.0
    for sc in (2, 1):
        for flip in (False, True):
            x, y, w, h = [v * sc for v in rect]
            if flip: y = H - y - h
            x0, x1 = int(round(x)), int(round(x + w)); y0, y1 = int(round(y)), int(round(y + h))
            if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0 or x1 > W or y1 > H: continue
            col = arr[y0:y1, x0 + (x1 - x0) // 2]
            op = col[:, 3] >= 250
            if op.mean() < 0.5: continue  # rounded corners clip the tick ends; judge the drawn part
            ok = np.zeros(len(col), bool)
            for hx in colors: ok |= (np.abs(col[:, :3] - np.array(hex2rgb(hx))).max(-1) <= 3) & op
            best = max(best, ok.sum() / op.sum())
    return best
TICK_EDGE = {}
BAR_STATES = ["idle", "hover", "armed", "preparing", "reading", "paused", "error", "hidden"]
SURF = {"bar": (["bar"], [(s, [s]) for s in BAR_STATES]),
        "log": (["reading-log", "readinglog", "reading_log", "log", "stats"], [("data", ["data"]), ("empty", ["empty"]), ("error", ["error"])]),
        "perm": (["permissions", "onboarding"], [("none", ["none", "none-granted", "none_granted"]), ("one", ["one", "one-granted", "one_granted"]), ("all", ["all", "all-granted", "all_granted"])]),
        "settings": (["settings"], [("main", ["default", "main", "data", "idle"])]),
        "cloud": (["cloud"], [("off", ["off"]), ("key", ["on-key", "on-with-key", "on_with_key", "key", "on"]), ("nokey", ["on-no-key", "on-without-key", "on_without_key", "nokey", "no-key"])])}
REQ_TEXT = {("bar", "hover"): ["read area", "read selection"], ("bar", "armed"): ["drag over what to read", "esc cancels"],
            ("bar", "preparing"): ["preparing", "stop"], ("bar", "reading"): [r"re:\d+\s*o\S\s*\d+", "pause", "next", "stop"], ("bar", "paused"): ["resume", "next", "stop"],
            ("log", "empty"): ["nothing read yet"], ("log", "error"): ["could not read the log", "open logs folder"],
            ("log", "data"): ["reading log", "first audio", "highlight", "problems", LIB_NAME_A.lower(), "document 4eb5ec", "name hidden", "resume", "wpm"],
            ("perm", "none"): ["screen recording", "accessibility", "open screen recording settings", "everything stays on this mac"],
            ("settings", "main"): ["settings", "hotkeys", "show document names", "screen sharing apps can see the bar"],
            ("cloud", "off"): ["openrouter"], ("cloud", "key"): ["openrouter", "replace", "remove"], ("cloud", "nokey"): ["openrouter", "save"]}
BANNED = [r"—", r"\bok\b", r"\bgrant", r"learn more", r"\bcontinue\b", r"seamless", r"effortless", r"\bmagic", r"supercharge", r"\bunlock", r"\bsmart\b",
          r"\bai\b", r"!", r"streak", r"time saved", r"words in\b", r"powered by"]
LOG_FIXTURE = {"schema": 2, "window_days": 7, "reads": {"today": 9, "window": 27}, "listened_min": {"today": 14.0, "window": 19.9},
               "first_audio_s": {"median": 3.9, "p90": 16.5, "n": 27, "by_provider": {"local": {"median": 6.1, "n": 13}, "openrouter": {"median": 2.3, "n": 14}},
                                 "trend": [{"date": (datetime.date.today() - datetime.timedelta(days=i)).isoformat(), "median": 4.0 + i * 0.1} for i in range(14)]},
               "items": {"figures": 4, "tables": 1}, "completion": {"finished": 20, "stopped": 6, "error": 1},
               "highlight": {"shows": 120, "landed": 104, "hidden": 9, "flagged": 0, "no_cue_sentences": 11, "sentences": 160}, "speech_wpm": 175,
               "documents": [{"doc": "9e1d2c3b4a59", "name": LIB_NAME_A, "pages": [3, 4, 5, 6, 7], "items": ["p5:fig1"], "reads": 20, "last_ts": iso(NOW - 3600),
                              "resume": {"page": 5, "fig_id": "p5:fig1", "sentence": 8, "of": 12}},
                             {"doc": "4eb5eccca1cd", "name": None, "pages": [2], "items": [], "reads": 7, "last_ts": iso(NOW - 2 * D), "resume": {"page": 2, "fig_id": None, "sentence": 3, "of": 9}}],
               "problems": [{"id": "kokoro-down", "count": 1, "last_ts": iso(NOW - 2 * D), "open": False}]}
def snapshot(spec, out, appearance, fixture=None, home=None):
    home = home or mkhome("snap")
    args = [app_bin(), "--snapshot", spec, "--out", out, "--appearance", appearance] + (["--fixture", fixture] if fixture else [])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=20, env=penv(home)); return p.returncode, (p.stdout + p.stderr)[-300:]
    except subprocess.TimeoutExpired: return -9, "timeout (killed)"
@section("C")
def design():
    app = app_bundle()
    if not app: add("C_app_builds", False, APP.get("why")); return
    add("C_app_builds", True)
    res = os.path.join(app, "Contents", "Resources")
    fonts = sorted(glob.glob(os.path.join(res, "Fonts", "*.[ot]tf")))
    fam = {f: sum(1 for x in fonts if re.search(f, os.path.basename(x), re.I)) for f in ("SourceSerif4", "IBMPlexSans(?!.*Mono)", "IBMPlexMono")}
    add("C_six_bundled_fonts_3_faces_2_weights", len(fonts) == 6 and all(v == 2 for v in fam.values()), ([os.path.basename(x) for x in fonts], fam))
    notice = open(os.path.join(res, "NOTICE"), errors="ignore").read() if os.path.exists(os.path.join(res, "NOTICE")) else ""
    add("C_notice_two_ofl_texts_reserved_names", len(re.findall(r"SIL OPEN FONT LICENSE", notice, re.I)) >= 2 and re.search(r"Reserved Font Name", notice, re.I) is not None
        and re.search(r"Source Serif", notice) is not None and re.search(r"Plex", notice) is not None, len(re.findall(r"SIL OPEN FONT LICENSE", notice, re.I)))
    if not (bin_has("self-check") or bin_has("selfCheck") or bin_has("SelfCheck")): add("C_self_check_fonts", False, "binary has no --self-check mode")
    else:
        try:
            p = subprocess.run([app_bin(), "--self-check", "fonts"], capture_output=True, text=True, timeout=30, env=penv(mkhome("selfcheck")))
            o = p.stdout + p.stderr
            add("C_self_check_fonts", p.returncode == 0 and re.search(r"SourceSerif4", o) and re.search(r"IBMPlexSans", o) and re.search(r"IBMPlexMono", o), (p.returncode, o[-200:]))
        except subprocess.TimeoutExpired: add("C_self_check_fonts", False, "timeout")
    if not bin_has("napshot"): add("C_snapshot_mode_present", False, "binary has no --snapshot mode (C4)"); return
    try: import numpy, PIL  # noqa
    except Exception: skip("C_snapshot_pixels", "needs numpy + Pillow (run under the doc venv)"); return
    sd = os.path.join(TMPROOT, "snaps"); os.makedirs(sd)
    fx = os.path.join(sd, "stats_fixture.json"); json.dump(LOG_FIXTURE, open(fx, "w"))
    help_out = ""
    for a_ in (["--snapshot", "help"], ["--snapshot", "list"]):
        try: hp = subprocess.run([app_bin()] + a_, capture_output=True, text=True, timeout=20, env=penv(mkhome("snaphelp"))); help_out += hp.stdout + hp.stderr
        except Exception: pass
    listed = set(re.findall(r"\b([a-z][a-z_-]*):([a-z][a-z_-]*)\b", help_out))
    if listed: info(f"C snapshot specs listed by the app: {sorted(':'.join(x) for x in listed)}")
    got = {}; timeouts = [0]; known = {}
    for key, (snames, states) in SURF.items():
        for st, cands in states:
            for appr in ("light", "dark"):
                done = None; tried = []
                specs = [known[(key, st)]] if (key, st) in known else sorted([f"{s}:{c}" for s in snames for c in cands], key=lambda sp: tuple(sp.split(":")) not in listed)
                for spec in specs:
                    if timeouts[0] >= 2: tried.append((spec, "not tried: earlier snapshots hung")); break
                    png = os.path.join(sd, f"{key}_{st}_{appr}_{spec.replace(':', '_')}.png")
                    rc, out = snapshot(spec, png, appr, fixture=fx if (key, st) == ("log", "data") else None); tried.append((spec, rc, out[-80:]))
                    if rc == -9: timeouts[0] += 1; break
                    if rc == 0 and os.path.exists(png): done = png; known[(key, st)] = spec; break
                    if listed and tuple(spec.split(":")) in listed: break
                got[(key, st, appr)] = done
                add(f"C_snapshot_{key}_{st}_{appr}", bool(done), tried[:3])
    ok = [k for k, v in got.items() if v]
    add("C_snapshot_mode_present", bool(ok), f"{len(ok)}/{len(got)} snapshots")
    texts = ocr([v for v in got.values() if v])
    for (key, st, appr), png in got.items():
        if not png: continue
        tag = f"{key}_{st}_{appr}"
        arr = load_png(png); an = analyze(arr)
        meta = {}
        try: meta = json.load(open(png + ".json"))
        except Exception: add(f"C_sidecar_json_{tag}", False, "no <png>.json with frames")
        if key == "bar" and st == "hidden":
            add(f"C_bar_hidden_draws_nothing_{appr}", an.get("empty") or an["opaque"] == 0, an.get("opaque")); continue
        if an.get("empty"): add(f"C_not_empty_{tag}", False, "fully transparent"); continue
        add(f"C_no_pure_white_black_{tag}", an["white"] == 0 and an["black"] == 0, (an["white"], an["black"], "white px bbox (2x)", an.get("white_bbox")))
        add(f"C_no_shadow_alpha_outside_{tag}", an["outside_alpha"] == 0, an["outside_alpha"])
        add(f"C_token_colours_only_{tag}", an["off_token_frac"] <= 0.01, (round(an["off_token_frac"], 4), an["off_token_top"]))
        if key == "bar":
            add(f"C_bar_surface_navy_{tag}", max(abs(a - b) for a, b in zip(an["mode"], TOK_RGB["navy"])) <= 3, "#%02X%02X%02X" % an["mode"])
            r_pt, a00 = corner_radius_pt(arr)
            add(f"C_bar_radius_le_4_corner_transparent_{tag}", r_pt is not None and 0 < r_pt <= 4.6 and a00 <= 40, (r_pt, a00))
            tcol = ["B23A2B"] if st == "error" else ["A8D8EC"]
            frames = tick_frames(meta)
            if frames: frac = max(tick_ok(arr, f, tcol) for f in frames)
            else:
                x0, y0, x1, y1 = an["bbox_op"]; frac = tick_ok(arr, [x0 / 2, y0 / 2, 3, (y1 - y0 + 1) / 2], tcol)
            add(f"C_bar_margin_tick_{tag}", frac >= 0.9, (round(frac, 3), frames[:2]))
            fr = (meta.get("frames") or {}) if isinstance(meta, dict) else {}
            if isinstance(fr.get("tick"), list) and isinstance(fr.get("dominant"), list):
                TICK_EDGE[tag] = fr["tick"][0] - fr["dominant"][0]
            if st == "idle":
                x0, y0, x1, y1 = an["bbox_op"]
                add(f"C_bar_idle_56x8_{appr}", abs((x1 - x0 + 1) / 2 - 56) <= 2 and abs((y1 - y0 + 1) / 2 - 8) <= 1.5, ((x1 - x0 + 1) / 2, (y1 - y0 + 1) / 2))
            if st == "reading": add(f"C_bar_reading_ember_current_tick_{appr}", has_color(arr, "E0892B")[0], has_color(arr, "E0892B")[1])
            if st == "error": add(f"C_bar_error_brick_{appr}", has_color(arr, "B23A2B", min_px=20)[0], has_color(arr, "B23A2B")[1])
        else:
            want = TOK_RGB["paper"] if appr == "light" else TOK_RGB["d-paper"]
            add(f"C_window_background_paper_{tag}", max(abs(a - b) for a, b in zip(an["mode"], want)) <= 3, "#%02X%02X%02X" % an["mode"])
            tc = ["1F6E99", "A8D8EC", "B23A2B"] if appr == "light" else ["A8D8EC", "B23A2B"]
            frames = tick_frames(meta)
            frac = max([tick_ok(arr, f, tc) for f in frames] or [0])
            if (key, st) == ("perm", "all") and not frames: info(f"C {tag}: no tick frame (nothing missing to mark)")
            else: add(f"C_window_margin_tick_{tag}", frac >= 0.9, (round(frac, 3), frames[:2], sorted(meta)[:8] if isinstance(meta, dict) else None))
        t = " ".join(texts.get(png) or []); tl = " ".join(t.lower().split())
        if REQ_TEXT.get((key, st)):
            miss = [w for w in REQ_TEXT[(key, st)] if not re.search(w[3:] if w.startswith("re:") else rf"(?<![a-z]){re.escape(w)}(?![a-z])", tl)]
            add(f"C_copy_required_labels_{tag}", not miss, (miss, tl[:300]))
        bans = [b for b in BANNED if re.search(b, tl)]
        add(f"C_copy_rules_{tag}", not bans, (bans, tl[:200]))
    off = {k: v for k, v in TICK_EDGE.items() if abs(v) > 1}
    if TICK_EDGE: add("C_bar_tick_is_left_edge_all_states", not off, ("design 4: the tick is the bar's left edge; inset pt by snapshot", off))
    for st in BAR_STATES:
        a, b = got.get(("bar", st, "light")), got.get(("bar", st, "dark"))
        if a and b:
            import numpy as np
            x, y = load_png(a), load_png(b)
            add(f"C_bar_same_in_both_appearances_{st}", x.shape == y.shape and np.abs(x - y).max() <= 3, "differs")

# ================================================================ U. the bar, live (own copy, temp home; passive unless --ui-input)
ALLOWED_EVENTS = {"bar_state", "bar_move", "bar_hide", "area", "control", "user_flag", "problem", "font_fallback", "focus_kept"}
BAR_ENUM = set(BAR_STATES)
ALLOWED_TRANS = {("idle", "hover"), ("hover", "idle"), ("idle", "armed"), ("hover", "armed"), ("armed", "preparing"), ("armed", "idle"), ("idle", "preparing"),
                 ("hover", "preparing"), ("preparing", "reading"), ("preparing", "error"), ("preparing", "idle"), ("reading", "idle"), ("reading", "paused"),
                 ("reading", "error"), ("paused", "reading"), ("paused", "idle"), ("error", "idle"), ("error", "hover"), ("hidden", "idle"), ("hidden", "preparing"),
                 ("hidden", "armed"), ("hidden", "reading"), ("error", "preparing"), ("error", "armed"), ("reading", "preparing"), ("paused", "error"), ("idle", "reading"), ("hover", "reading")}
def app_events(home): return jl(os.path.join(home, "logs", "app.jsonl"))
def my_windows(wl, pid): return [w for w in (wl or {}).get("windows", []) if w.get("pid") == pid]
def bar_window(wl, pid):
    """The bar: a small panel of ours that is not a status-item window in the menu bar."""
    if not wl: return None
    ph = wl["primary_h"]; out = []
    for w in my_windows(wl, pid):
        x, y, bw_, bh = w["bounds"]
        if bw_ > 420 or bh > 60 or w["layer"] <= 0: continue
        s = screen_of(wl, x + bw_ / 2, y + bh / 2); top = ph - (s["frame"][1] + s["frame"][3])
        if y - top > 60: out.append(w)
    return out[0] if out else None
def screen_of(wl, x, y_cg):
    ph = wl["primary_h"]
    for s in wl["screens"]:
        fx, fy, fw, fh = s["frame"]; top = ph - (fy + fh)
        if fx <= x < fx + fw and top <= y_cg < top + fh: return s
    return wl["screens"][0]
@section("U")
def bar_live():
    app = app_bundle()
    if not app: add("U_app_builds", False, APP.get("why")); return
    if not swift_tool("winlist"): skip("U_bar_live", "swift winlist helper did not build"); return
    real_before = real_home_state(); f0 = front(); wl0 = winlist()
    fs = False
    if wl0 and wl0.get("front"):
        fp = wl0["front"]["pid"]
        for w in wl0["windows"]:
            if w["pid"] == fp and w["layer"] == 0 and any(abs(w["bounds"][2] - s["frame"][2]) < 1 and abs(w["bounds"][3] - s["frame"][3]) < 1 for s in wl0["screens"]): fs = True
    deadtts = dead_url(); h, pa = launch_app(deadtts)
    try:
        time.sleep(4.0)
        add("U_app_running", pa.poll() is None, pa.poll())
        add("U_launch_does_not_take_focus", f0 == front(), (f0, front()))
        ev = app_events(h)
        add("U_app_jsonl_in_temp_home", bool(ev), "no logs/app.jsonl under SPEAKEASY_HOME after launch")
        add("U_app_jsonl_shape", all(isinstance(e, dict) and "ts" in e and e.get("event") in ALLOWED_EVENTS for e in ev), [e.get("event") for e in ev][:10])
        add("U_no_font_fallback_after_launch", not [e for e in ev if e.get("event") == "font_fallback"], [e for e in ev if e.get("event") == "font_fallback"])
        kd = wait_for(lambda: next((e for e in app_events(h) if e.get("event") == "problem" and e.get("id") == "kokoro-down" and e.get("live") is True), None), 12)
        add("U_live_check_at_launch_kokoro_down", bool(kd), [e for e in app_events(h) if e.get("event") == "problem"][:4])
        bad_p = [e for e in app_events(h) if e.get("event") == "problem" and (e.get("id") not in PROBLEM_IDS or not isinstance(e.get("live"), bool))]
        add("U_problem_events_shape", not bad_p, bad_p[:3])
        wl = winlist(); bw = bar_window(wl, pa.pid)
        if fs: add("U_idle_bar_hidden_in_fullscreen", bw is None, bw); info("U frontmost window is full-screen: idle placement checks skipped")
        else: add("U_idle_bar_window_present", bw is not None, [(w["layer"], w["bounds"]) for w in my_windows(wl, pa.pid)])
        if bw: placement_checks(wl, bw, pa.pid, h)
        bar_id = bw["id"] if bw else None
        # a CLI read in the same home (state.json speaking): the contract enters `reading` from state.json; the app may only follow its own reads
        n_ev = len(app_events(h))
        p = start_speak(h, " ".join(SENTS), " ".join(SENTS), tts_mode="say", play=2.5)
        rd = wait_for(lambda: any(e.get("event") == "bar_state" and e.get("to") == "reading" for e in app_events(h)[n_ev:]), 12)
        if rd:
            add("U_cli_read_bar_reading", True)
            reading_checks(h, pa, f0, bar_id, "cli")
        else: info("U the bar does not follow a CLI-started read (state.json speaking in its home); reading / collision checks need --ui-input (read from the bar)")
        sp = speak_pid(h)
        if sp: os.kill(sp, signal.SIGTERM)
        finish(p, 60)
        if rd:
            idle = wait_for(lambda: (lambda L: L and L[-1].get("to") == "idle")([e for e in app_events(h) if e.get("event") == "bar_state"]), 6)
            add("U_bar_back_to_idle_after_read", bool(idle), [(e.get("from"), e.get("to")) for e in app_events(h) if e.get("event") == "bar_state"][-4:])
        session_checks(h, f0)
    finally: stop_app(pa)
    if fs: skip("U_occupant_branches", "frontmost window is full-screen: the idle bar is hidden")
    elif swift_tool("panel"): occupant_branches(f0)
    else: skip("U_occupant_branches", "swift panel helper did not build")
    if A.ui_input: ui_input_checks(f0)
    else: skip("U_ui_input_checks", "hover / click / area-from-bar / collision / hotkey / menu checks inject events into the live session; rerun with --ui-input")
    newf = [k for k in real_home_state() if k not in real_before and re.search(r"app\.jsonl|cue\.json|library\.json", k)]
    add("U_own_copy_never_wrote_real_home", not newf, newf)
PROBLEM_IDS = ("kokoro-down", "model-down", "cloud-fallback", "layout-missing", "highlight-failed", "permission", "slow-start")
def launch_app(tts_url):
    h = mkhome("app"); os.makedirs(os.path.join(h, "logs"), exist_ok=True)
    open(os.path.join(h, "config"), "w").write(f"SPEAKEASY_TTS=kokoro\nSPEAKEASY_LLM_URL=http://127.0.0.1:{llm().port}/v1\nSPEAKEASY_TTS_URL={tts_url}\nSPEAKEASY_TRACK=off\n")
    llm().set(" ".join(SENTS))
    pa = subprocess.Popen([app_bin()], env=penv(h, **back_env(h, tts_mode="kokoro", tts_url=tts_url, play=2.0)), stdout=subprocess.DEVNULL,
                          stderr=open(os.path.join(TMPROOT, f"app_stderr_{_n[0]}.txt"), "w"))
    return h, pa
def stop_app(pa):
    if pa.poll() is None:
        pa.terminate()
        try: pa.wait(5)
        except subprocess.TimeoutExpired: pa.kill(); pa.wait()
def pointer_screen(wl):
    mx, my = wl["mouse"]
    for sc in wl["screens"]:
        fx, fy, fw, fh = sc["frame"]
        if fx <= mx < fx + fw and fy <= my < fy + fh: return sc
    return wl["screens"][0]
def _isect(a, b): return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
def home_geometry(wl, s):
    """(vx, vw, v_top_cg, v_bot_cg, home zone) in CG points: the zone the idle bar (72 x 24 hit area, 8 pt above the visible bottom) needs."""
    ph = wl["primary_h"]; vx, vy, vw, vh = s["visible"]; v_bot = ph - vy; hx = vx + vw / 2
    return vx, vw, ph - (vy + vh), v_bot, [hx - 44, v_bot - 8 - 24 - 8, 88, 40]
def occupants(wl, s, exclude_pids, zone):
    """Other processes' floating, visible windows over a zone (layer > 0 excludes documents, Dock and desktop)."""
    v_top = wl["primary_h"] - (s["visible"][1] + s["visible"][3])
    return [w for w in wl["windows"] if w["pid"] not in exclude_pids and w["layer"] > 0 and w.get("alpha", 1) > 0
            and w["bounds"][1] >= v_top - 1 and _isect(w["bounds"], zone)]
def bar_shaped(w, v_bot): return w["bounds"][3] <= 80 and abs(v_bot - (w["bounds"][1] + w["bounds"][3])) <= 40
def expected_placement(wl, pid):
    """Amended B1: free -> centre; bar-shaped occupants only -> stacked 8 pt above the highest, same centre; any larger occupant ->
    bottom left, else (left also taken) bottom right."""
    s = pointer_screen(wl); vx, vw, v_top, v_bot, zone = home_geometry(wl, s)
    occ = occupants(wl, s, {pid}, zone)
    if not occ: return {"kind": "free", "occ": []}
    if all(bar_shaped(w, v_bot) for w in occ):
        return {"kind": "stacked", "occ": occ, "top": min(w["bounds"][1] for w in occ)}
    left_zone = [vx, v_bot - 8 - 24 - 8, 120, 40]
    return {"kind": "right" if occupants(wl, s, {pid}, left_zone) else "left", "occ": occ}
def check_placement(wl, bw, pid, h, tag):
    """Asserts the bar's frame against the amended B1 rule for the occupants present now; returns the expectation."""
    x, y, w_, h_ = bw["bounds"]; s = pointer_screen(wl); vx, vw, v_top, v_bot, zone = home_geometry(wl, s); hx = vx + vw / 2
    exp = expected_placement(wl, pid); gap = v_bot - (y + h_); cxb = x + w_ / 2
    moves = [e for e in app_events(h) if e.get("event") == "bar_move"]
    manual = [e for e in moves if e.get("reason") == "drag"]
    occ_desc = [(w["layer"], [round(v) for v in w["bounds"]]) for w in exp["occ"]][:4]
    if exp["kind"] == "free": ok = abs(cxb - hx) <= 2 and 0 <= gap <= 16
    elif exp["kind"] == "stacked": ok = abs(cxb - hx) <= 2 and abs((y + h_) - (exp["top"] - 8)) <= 2
    elif exp["kind"] == "left": ok = x <= vx + 24 and 0 <= gap <= 16
    else: ok = x + w_ >= vx + vw - 24 and 0 <= gap <= 16
    if not ok and manual: info(f"U {tag} placement differs from the occupancy rule but a drag was logged (remembered manual position wins): {manual[-1]}")
    add(f"U_{tag}_bar_placement_{exp['kind']}", ok or bool(manual), (exp["kind"], [round(v) for v in bw["bounds"]], "visible", s["visible"], "gap", round(gap, 1), "occupants", occ_desc))
    add(f"U_{tag}_bar_never_above_88pt", gap <= 88 or bool(manual), ("bottom gap", round(gap, 1)))
    occm = [e for e in moves if e.get("reason") == "occupied"]
    if exp["kind"] == "free":
        add(f"U_{tag}_no_occupied_move_when_free", not occm, occm[:2])
    else:
        e = occm[-1] if occm else {}
        want_pos = "centre" if exp["kind"] == "stacked" else exp["kind"]
        okm = bool(occm) and e.get("pos") == want_pos and e.get("stacked") is (exp["kind"] == "stacked") and "occupants" in e \
            and (isint(e["occupants"]) and e["occupants"] >= 1 or isinstance(e["occupants"], list) and len(e["occupants"]) >= 1)
        add(f"U_{tag}_bar_move_occupied_event", okm or bool(manual), (e, want_pos, exp["kind"]))
        if isinstance(e.get("occupants"), list):
            strs = [v for v in walk_strings(e["occupants"]) if not re.fullmatch(r"[a-z0-9_\-]{1,24}", v)]
            add(f"U_{tag}_occupied_event_no_app_names", not strs, strs[:3])
    return exp
PANELS = []
def own_panel(w, h, where="centre", bottom=0.0):
    exe = swift_tool("panel")
    if not exe: return None
    p = subprocess.Popen([exe, str(w), str(h), where, str(bottom)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    PANELS.append(p)
    line = p.stdout.readline()
    if not line.startswith("ready"): p.kill(); return None
    time.sleep(0.4); return p
def kill_panels():
    while PANELS:
        p = PANELS.pop()
        if p.poll() is None: p.kill(); p.wait()
def occupant_branches(f0):
    """Amended B1 with own test windows: a 60 pt bar-shaped panel (stack above) and 600 pt panels (left, then right)."""
    wl = winlist(); s = pointer_screen(wl); vx, vw, v_top, v_bot, zone = home_geometry(wl, s)
    foreign = occupants(wl, s, set(), zone)
    cases = [("own_barshaped", [(240, 60, "centre", 4)], "stacked"), ("own_large", [(512, 600, "centre", 0)], "left"),
             ("own_large_left_taken", [(512, 600, "centre", 0), (240, 600, "left", 0)], "right")]
    for tag, specs, want in cases:
        if want == "stacked" and any(not bar_shaped(w, v_bot) for w in foreign):
            skip(f"U_{tag}_bar_placement_stacked", f"a large foreign window already occupies the home spot ({[(w['layer'], [round(v) for v in w['bounds']]) for w in foreign][:2]}): the bar-shaped branch cannot be isolated"); continue
        try:
            if not all(own_panel(*sp) for sp in specs): skip(f"U_{tag}", "test panel did not start"); continue
            f0p = front()  # baseline after the planted panels are up: the check is about Speakeasy's launch, not the harness helper
            h, pa = launch_app(dead_url())
            try:
                time.sleep(4.0); wl = winlist(); bw = bar_window(wl, pa.pid)
                add(f"U_{tag}_launch_keeps_focus", front() == f0p, (f0p, front()))
                if not bw: add(f"U_{tag}_bar_window_present", False, [(w["layer"], w["bounds"]) for w in my_windows(wl, pa.pid)]); continue
                exp = check_placement(wl, bw, pa.pid, h, tag)
                add(f"U_{tag}_rule_branch_is_{want}", exp["kind"] == want, (exp["kind"], "expected from the planted panels", want))
            finally: stop_app(pa)
        finally: kill_panels()
def placement_checks(wl, bw, pid=None, h=None):
    x, y, w_, h_ = bw["bounds"]; s = screen_of(wl, x + w_ / 2, y + h_ / 2); ph = wl["primary_h"]
    vx, vy, vw, vh = s["visible"]; v_top_cg = ph - (vy + vh); v_bot_cg = ph - vy
    add("U_bar_level_statusbar", bw["layer"] in (25, 26), bw["layer"])
    add("U_bar_sharing_none", bw["sharing"] == 0, bw["sharing"])
    add("U_bar_inside_visible_frame", x >= vx - 0.5 and x + w_ <= vx + vw + 0.5 and y >= v_top_cg - 0.5 and y + h_ <= v_bot_cg + 0.5, (bw["bounds"], s["visible"]))
    exp = check_placement(wl, bw, pid, h, "launch")
    add("U_bar_bottom_centre", exp["kind"] != "free" or (abs((x + w_ / 2) - (vx + vw / 2)) <= 2 and 0 <= v_bot_cg - (y + h_) <= 16),
        ("home spot free: bottom centre expected", bw["bounds"], s["visible"]) if exp["kind"] == "free" else ("occupied home spot judged by U_launch_bar_placement_*", exp["kind"]))
    mx, my = wl["mouse"]; ms = None
    for sc in wl["screens"]:
        fx, fy, fw, fh = sc["frame"]
        if fx <= mx < fx + fw and fy <= my < fy + fh: ms = sc
    add("U_idle_bar_on_pointer_display", ms is None or ms["frame"] == s["frame"], (ms or {}).get("frame"))
    add("U_idle_size", w_ <= 80 and h_ <= 28, bw["bounds"])
def reading_checks(h, pa, f0, bar_id, tag):
    """Bar in `reading`: size, created once, no activation, then the cue-collision rule with a planted cue.json."""
    time.sleep(0.4); wl = winlist(); bw2 = bar_window(wl, pa.pid)
    add(f"U_{tag}_reading_bar_340x36", bw2 is not None and 300 <= bw2["bounds"][2] <= 380 and 30 <= bw2["bounds"][3] <= 44, (bw2 or {}).get("bounds"))
    add(f"U_{tag}_panel_created_once", bw2 is not None and (bar_id is None or bw2["id"] == bar_id), (bar_id, (bw2 or {}).get("id")))
    add(f"U_{tag}_reading_keeps_focus", front() == f0, front())
    if not bw2: return
    x, y, w_, h_ = bw2["bounds"]; s = screen_of(wl, x + w_ / 2, y + h_ / 2); fx, fy, fw, fh = s["frame"]
    vx, _, vw, _ = s["visible"]; scx = vx + vw / 2; bcx = x + w_ / 2
    ph = wl["primary_h"]; top = ph - (fy + fh)  # full display height: the same rect in either y convention
    # the cue covers the bar's ACTUAL frame (rule 8 may have moved it off centre, e.g. under a Wispr Flow window); a centred bar gets an off-centre cue so the far corner is unambiguous
    cue = [bcx - 100, top, 400, fh] if abs(bcx - scx) <= 1 else [x, top, w_, fh]
    cmid = cue[0] + cue[2] / 2; far = "left" if cmid - vx > (vx + vw) - cmid else "right"
    # return leg: a fresh cue clear of the home spot (rule 1 "stops intersecting"); deleting cue.json is "stale", where the bar stays put
    cue2 = [vx + vw - 120, top, 100, fh] if bcx <= scx else [vx + 20, top, 100, fh]
    try: stj = json.load(open(os.path.join(h, "state.json")))
    except Exception: stj = {}
    stop = threading.Event(); cur = [cue]
    def writer():
        while not stop.is_set():
            tmp = os.path.join(h, ".cue.json.tmp")
            with open(tmp, "w") as fh_: json.dump({"run_id": stj.get("run_id"), "seq": stj.get("seq", 1), "rect": cur[0]}, fh_)
            os.replace(tmp, os.path.join(h, "cue.json")); time.sleep(0.2)
    n1 = len(app_events(h)); th = threading.Thread(target=writer, daemon=True); th.start(); tc = time.time()
    mv = wait_for(lambda: next((e for e in app_events(h)[n1:] if e.get("event") == "bar_move" and e.get("reason") == "collision"), None), 3.0)
    lat = time.time() - tc
    add(f"U_{tag}_collision_bar_move_logged", bool(mv), ([e for e in app_events(h)[n1:]][:5], bw2["bounds"], cue))
    if mv:
        add(f"U_{tag}_collision_slides_to_far_corner", mv.get("pos") == far, (mv, far))
        time.sleep(0.5); b3 = bar_window(winlist(), pa.pid); bb = (b3 or {}).get("bounds")
        disj = bb is not None and (bb[0] + bb[2] + 24 <= cue[0] + 0.5 if far == "left" else bb[0] >= cue[0] + cue[2] + 24 - 0.5)
        add(f"U_{tag}_collision_bar_disjoint_from_cue", disj, (bb, cue, far))
        info(f"U collision latency {lat * 1000:.0f} ms (incl. log polling); home {[round(v) for v in bw2['bounds']]}, far {far}")
        n2 = len(app_events(h)); cur[0] = cue2
        back = wait_for(lambda: next((e for e in app_events(h)[n2:] if e.get("event") == "bar_move" and e.get("reason") == "collision" and e.get("pos") != far), None), 5.0)
        add(f"U_{tag}_collision_returns_logged", bool(back), [e for e in app_events(h)[n2:]][:5])
        time.sleep(0.5); b4 = bar_window(winlist(), pa.pid); hb = (b4 or {}).get("bounds")
        add(f"U_{tag}_collision_returns_to_home_frame", hb is not None and all(abs(a - b) <= 1 for a, b in zip(hb, bw2["bounds"])), (hb, bw2["bounds"]))
    stop.set(); th.join()
    try: os.remove(os.path.join(h, "cue.json"))
    except OSError: pass
def session_checks(h, f0):
    bs = [e for e in app_events(h) if e.get("event") == "bar_state"]
    badt = [(e.get("from"), e.get("to")) for e in bs if (e.get("from"), e.get("to")) not in ALLOWED_TRANS or e.get("to") not in BAR_ENUM]
    chain = [i for i in range(1, len(bs)) if bs[i].get("from") != bs[i - 1].get("to")]
    add("U_bar_state_transitions_valid", not badt, badt[:6])
    add("U_bar_state_chain_consistent", not chain, [(bs[i - 1].get("to"), bs[i].get("from")) for i in chain][:5])
    strs = [v for e in app_events(h) for k, v in e.items() if isinstance(v, str) and k not in ("ts", "run_id") and not re.fullmatch(r"[a-z0-9_\-]{1,24}", v)]
    add("U_app_jsonl_no_text_titles_paths", not strs, strs[:5])
    add("U_focus_unchanged_whole_session", front() == f0, (f0, front()))
def ui_input_checks(f0):
    post = swift_tool("post")
    if not post or subprocess.run([post, "trusted"], capture_output=True, text=True).stdout.strip() != "1":
        skip("U_ui_input_checks", "event poster is not trusted for Accessibility"); return
    h, pa = launch_app(tts().url)
    try:
        time.sleep(4.0); wl = winlist(); bw = bar_window(wl, pa.pid)
        if not bw: skip("U_ui_input_checks", "no idle bar"); return
        x, y, w_, h_ = bw["bounds"]; cx, cy = x + w_ / 2, y + h_ / 2; bar_id = bw["id"]
        top = next((w for w in wl["windows"] if w.get("alpha", 1) > 0 and w["bounds"][0] <= cx < w["bounds"][0] + w["bounds"][2] and w["bounds"][1] <= cy < w["bounds"][1] + w["bounds"][3]), None)
        if not top or top["pid"] != pa.pid: skip("U_ui_input_checks", "another window covers the test bar"); return
        mx, my = wl["mouse"]; ph = wl["primary_h"]; n0 = len(app_events(h))
        subprocess.run([post, "move", str(cx), str(cy)]); time.sleep(0.6)
        add("U_hover_after_dwell", any(e.get("event") == "bar_state" and e.get("to") == "hover" for e in app_events(h)[n0:]), [e for e in app_events(h)[n0:]][:4])
        b2 = bar_window(winlist(), pa.pid)
        add("U_hover_size_300x36", b2 is not None and 260 <= b2["bounds"][2] <= 340 and 30 <= b2["bounds"][3] <= 44, (b2 or {}).get("bounds"))
        subprocess.run([post, "move", str(cx), str(cy - 300)]); time.sleep(0.8)
        add("U_hover_leaves_to_idle", any(e.get("event") == "bar_state" and e.get("from") == "hover" and e.get("to") == "idle" for e in app_events(h)[n0:]), "")
        n1 = len(app_events(h)); subprocess.run([post, "click", str(cx), str(cy)]); time.sleep(0.6)
        add("U_click_idle_bar_arms", any(e.get("event") == "bar_state" and e.get("to") == "armed" for e in app_events(h)[n1:]), [e for e in app_events(h)[n1:]][:4])
        add("U_click_bar_keeps_focus", front() == f0, (f0, front()))
        subprocess.run([post, "key", "53"]); time.sleep(0.6)
        ar = [e for e in app_events(h)[n1:] if e.get("event") == "area"]
        add("U_esc_cancels_armed_area_event", any(e.get("via") == "bar" and e.get("result") == "cancel-esc" for e in ar), ar)
        add("U_armed_cancel_keeps_focus", front() == f0, (f0, front()))
        n2 = len(app_events(h)); subprocess.run([post, "key", "35", "ctrl,alt"]); time.sleep(0.6)
        add("U_pause_hotkey_not_registered_at_idle", not [e for e in app_events(h)[n2:] if e.get("event") == "control"], [e for e in app_events(h)[n2:]][:3])
        # Read area from the bar (B9.4): click, drag, release; the read runs on the mock LLM in the temp home
        b4 = bar_window(winlist(), pa.pid) or bw; x, y, w_, h_ = b4["bounds"]; cx, cy = x + w_ / 2, y + h_ / 2
        n3 = len(app_events(h)); subprocess.run([post, "click", str(cx), str(cy)]); time.sleep(0.6)
        if not any(e.get("event") == "bar_state" and e.get("to") == "armed" for e in app_events(h)[n3:]):
            skip("U_area_from_bar", "second click did not arm; no drag sent (it would land in the user's window)"); return
        subprocess.run([post, "drag", str(cx - 160), str(cy - 380), str(cx + 160), str(cy - 220)])
        rd = wait_for(lambda: any(e.get("event") == "bar_state" and e.get("to") == "reading" for e in app_events(h)[n3:]), 20)
        ar = [e for e in app_events(h)[n3:] if e.get("event") == "area"]
        add("U_area_from_bar_event", any(e.get("via") == "bar" and e.get("result") == "read" for e in ar), ar)
        add("U_area_from_bar_keeps_focus", front() == f0, (f0, front()))
        if not rd: add("U_area_read_reaches_reading", False, [(e.get("from"), e.get("to")) for e in app_events(h)[n3:] if e.get("event") == "bar_state"]); return
        reading_checks(h, pa, f0, bar_id, "area")
        # temporary hotkeys while reading reach the run (B9.8, Stop by SIGTERM so ⌃⌥S never reaches the user's own Speakeasy)
        n4 = len(app_events(h))
        subprocess.run([post, "key", "35", "ctrl,alt"]); time.sleep(2.2); subprocess.run([post, "key", "35", "ctrl,alt"]); time.sleep(0.5)
        subprocess.run([post, "key", "45", "ctrl,alt"]); time.sleep(0.8)
        try: stj = json.load(open(os.path.join(h, "state.json")))
        except Exception: stj = {}
        subprocess.run([post, "key", "13", "ctrl,alt"]); time.sleep(0.8)
        sp = speak_pid(h)
        if sp: os.kill(sp, signal.SIGTERM)
        wait_for(lambda: not speak_pid(h) or not pid_alive(speak_pid(h)), 20)
        time.sleep(1.0); r = last_run(h) or {}; ev = app_events(h)[n4:]
        add("U_area_run_logged_capture_method_area", r.get("capture_method") == "area", r.get("capture_method"))
        acts = [(e.get("action"), e.get("via")) for e in ev if e.get("event") == "control"]
        add("U_hotkey_control_events", all(x_ in acts for x_ in (("pause", "hotkey"), ("resume", "hotkey"), ("next", "hotkey"), ("flag", "hotkey"))), acts)
        uf = [e for e in ev if e.get("event") == "user_flag"]
        add("U_wrong_spot_user_flag_from_state", len(uf) == 1 and uf[0].get("run_id") == stj.get("run_id") and isint(uf[0].get("seq")) and "cue_kind" in uf[0], (uf, {k: stj.get(k) for k in ("run_id", "seq", "cue_kind")}))
        add("U_hotkeys_reach_backend", isint(r.get("paused_ms")) and r["paused_ms"] >= 1500 and r.get("skips") == 1 and r.get("end") == "stopped", (r.get("paused_ms"), r.get("skips"), r.get("end")))
        add("U_hotkeys_keep_focus", front() == f0, (f0, front()))
        try: titles = json.loads(subprocess.run([post, "menu", str(pa.pid)], capture_output=True, text=True, timeout=10).stdout or "[]")
        except Exception: titles = []
        rc, ds, _, _ = stats_json(h); want = None
        if isinstance(ds, dict): want = f"Today: {ds['reads']['today']} read{'s' if ds['reads']['today'] != 1 else ''}, {round(ds['listened_min']['today'])} min"
        add("U_menu_line_today_reads_min", bool(titles) and re.fullmatch(r"Today: \d+ reads?, \d+ min", titles[0] or "") is not None and (want is None or titles[0] == want), (titles[:3], want))
        add("U_menu_reading_log_item", any(t == "Reading log…" for t in titles) and not any(t.startswith("Stats") for t in titles), titles)
        session_checks(h, f0)
        subprocess.run([post, "move", str(mx), str(ph - my)])
    finally: stop_app(pa)

# ================================================================ X. contract lints (C5.1 / C5.2) and file layout
@section("X")
def lints():
    srcd = os.path.join(REPO, "app", "Sources")
    files = [f for f in glob.glob(os.path.join(srcd, "**", "*.swift"), recursive=True)]
    names = {os.path.basename(f) for f in files}
    add("X_tokens_bar_snapshot_files_exist_pill_deleted", {"Tokens.swift", "Bar.swift", "Snapshot.swift"} <= names and "Pill.swift" not in names, sorted(names))
    add("X_fonts_resources_dir", len(glob.glob(os.path.join(REPO, "app", "Resources", "Fonts", "*.[ot]tf"))) == 6, len(glob.glob(os.path.join(REPO, "app", "Resources", "Fonts", "*"))))
    pats = [r"#[0-9A-Fa-f]{6}\b", r"NSColor\.system", r"Color\.(green|blue|orange|purple|pink|red)\b", r"hasShadow\s*=\s*true", r"\.shadow\(", r"NSVisualEffectView",
            r"\bGradient\b", r"\.blur\(", r"\bCapsule\(", r"0x3B82F6|3B82F6"]
    hits = []
    for f in files:
        if os.path.basename(f) == "Tokens.swift": continue
        for i, line in enumerate(open(f, errors="ignore"), 1):
            s = line.split("//")[0]
            for p in pats:
                if re.search(p, s): hits.append(f"{os.path.basename(f)}:{i}:{p}")
            for m in re.finditer(r"(?:cornerRadius|radius)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", s):
                if float(m.group(1)) > 4: hits.append(f"{os.path.basename(f)}:{i}:radius {m.group(1)}")
    add("X_token_lint_app_sources", not hits, hits[:10])
    th = []
    for i, line in enumerate(open(os.path.join(SRC, "tracker.swift"), errors="ignore"), 1):
        s = line.split("//")[0]
        for p in (r"NSColor\.system", r"3B82F6", r"hasShadow\s*=\s*true", r"\.shadow\(", r"NSVisualEffectView"):
            if re.search(p, s): th.append(f"tracker.swift:{i}:{p}")
    add("X_token_lint_tracker", not th, th[:10])
    ch = []
    lit = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
    for f in files:
        for i, line in enumerate(open(f, errors="ignore"), 1):
            for m in lit.finditer(line):
                s = m.group(1)
                if "—" in s: ch.append(f"{os.path.basename(f)}:{i}:em-dash")
                if s in ("OK", "Grant…", "Grant...", "Continue", "Learn more"): ch.append(f"{os.path.basename(f)}:{i}:{s}")
                if re.search(r"\b(seamless|effortless|supercharge|powered by AI)\b", s, re.I): ch.append(f"{os.path.basename(f)}:{i}:{s[:30]}")
                if re.fullmatch(r"[A-Z][^!]{3,}[a-z.]!", s): ch.append(f"{os.path.basename(f)}:{i}:exclamation")
    add("X_copy_lint_app_strings", not ch, ch[:10])
    if os.path.exists(os.path.join(SRC, "focus.py")):
        fp = open(os.path.join(SRC, "focus.py"), errors="ignore").read()
        add("X_focus_py_old_blue_retired", "3B82F6" not in fp.upper(), "")

# ================================================================ G. bench guard (A7.10)
@section("G")
def bench_guard():
    if A.skip_bench: skip("G_bench_public_equals_baseline", "--skip-bench"); return
    if not os.path.isfile(A.layout_model): skip("G_bench_public_equals_baseline", f"no layout model at {A.layout_model}"); return
    out = os.path.join(TMPROOT, "bench.json"); t = time.time()
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "MOCK_", "STUB_"))}
    p = subprocess.run([A.doc_python, os.path.join(BENCH, "run_bench.py"), "--set", "public", "--layout-model", A.layout_model, "--doc-python", A.doc_python, "--out", out],
                       capture_output=True, text=True, timeout=5400, env=e)
    info(f"G bench rc {p.returncode} in {time.time() - t:.0f}s")
    if p.returncode or not os.path.exists(out): add("G_bench_runs", False, (p.returncode, p.stderr[-400:])); return
    b = json.load(open(out)); base = json.load(open(os.path.join(BENCH, "baseline.json")))["metrics"]
    m = ((b.get("sets") or {}).get("public") or {})
    got = {"boxes.score": (m.get("boxes") or {}).get("score"), "boxes.precision_50": (m.get("boxes") or {}).get("precision_50"), "lines.score": (m.get("lines") or {}).get("score"),
           "parts.score": (m.get("parts") or {}).get("score"), "parts.precision": (m.get("parts") or {}).get("precision")}
    want = {"boxes.score": base["boxes"]["score"], "boxes.precision_50": base["boxes"]["precision_50"], "lines.score": base["lines"]["score"],
            "parts.score": base["parts"]["score"], "parts.precision": base["parts"]["precision"]}
    add("G_bench_public_equals_baseline", got == want, (got, want))
    sp = m.get("speed") or {}; info(f"G speed {sp} (baseline {base.get('speed')})")
    g = subprocess.run([A.doc_python, os.path.join(BENCH, "bench_gate.py"), out, "--public-only", "--parts-min", "70"], capture_output=True, text=True)
    add("G_bench_gate_pass", g.returncode == 0, (g.stdout + g.stderr)[-300:])

# ---------------------------------------------------------------- main
try:
    for fn in (stats_v2, library, runs_fields, controls, scrub_tests, privacy, design, bar_live, lints, bench_guard): fn()
finally:
    for k, m in _MOCKS.items():
        try: m.stop() if hasattr(m, "stop") else m[0].shutdown()
        except Exception: pass
    leaked = [k for k, v in real_home_state().items() if k not in REAL0 and re.search(r"app\.jsonl|cue\.json|library\.json|speak\.pid", k)]
    if leaked: add("Z_real_home_untouched", False, leaked)
    if not A.keep: shutil.rmtree(TMPROOT, ignore_errors=True)
    else: info(f"kept {TMPROOT}")
n = {s: sum(1 for x, _ in R if x == s) for s in ("PASS", "FAIL", "SKIP")}
print(f"SUMMARY checks={len(R)} pass={n['PASS']} fail={n['FAIL']} skip={n['SKIP']} secs={time.time() - T0:.0f}", flush=True)
sys.exit(1 if n["FAIL"] else 0)
