#!/usr/bin/env python3
"""Phase-5 acceptance suite (black box, promoted from the holdout) for docs/contracts/phase5-parts.md (figure parts: hierarchy,
callouts, picker). Drives only `docmap.py parse-pdf`, the CLI (src/speakeasy), the benchmark harness (tests/audit/bench) with the
mock LLM (tests/audit/stream/mock_llm.py), and the logs / live focus.json under temp $SPEAKEASY_HOME. Never reads app code and
never touches ~/.speakeasy. "Unchanged" behaviour is checked differentially against the B5 commit (git archive of
--baseline-ref) and the public B5 bench results in b5/. The only real model call is the optional proxy-record check (P4) against
the local Ollama on loopback. Public data only unless --holdout DIR (with DIR's B5 hidden files in --b5-dir) is given.

Usage: python3 run_phase5.py --model PATH/pp_doclayout_s.onnx [--det-python PY] [--noort-python PY] [--repo PATH]
                             [--baseline-ref 91166c4] [--only S,C,F,Q,A,D,W,T,U,H,G,N,P,Z] [--skip-bench] [--keep] [--holdout DIR]
  --det-python: pypdfium2 + onnxruntime + numpy (default $SPEAKEASY_DOC_PYTHON or this interpreter); --noort-python: reportlab,
  for the fixture PDFs (default this interpreter).
Output: one line per check `PASS|FAIL|SKIP <name> <detail>`, `INFO ...` lines, then `SUMMARY ...`. Exit 0 only if no FAIL."""
import argparse, base64, collections, glob, hashlib, json, math, os, re, shutil, signal, socket, statistics, subprocess, sys, tempfile, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", ".."))); ap.add_argument("--baseline-ref", default="91166c4")
ap.add_argument("--only", default=""); ap.add_argument("--skip-bench", action="store_true"); ap.add_argument("--keep", action="store_true")
ap.add_argument("--det-python", default=os.environ.get("SPEAKEASY_DOC_PYTHON") or sys.executable, help="python with pypdfium2, onnxruntime, numpy")
ap.add_argument("--noort-python", default=sys.executable, help="python with reportlab (fixture generation)")
ap.add_argument("--model", default=os.environ.get("SPEAKEASY_LAYOUT_MODEL", ""), help="verified pp_doclayout_s.onnx")
ap.add_argument("--b5-dir", default=os.path.join(HERE, "b5"))
ap.add_argument("--holdout", default=None, help="hidden bench dir; without it the suite runs on public data only")
ap.add_argument("--ollama", default="http://127.0.0.1:11434")
A = ap.parse_args()
REPO = os.path.abspath(A.repo); SRC = os.path.join(REPO, "src"); CLI = os.path.join(SRC, "speakeasy"); BENCH = os.path.join(REPO, "tests", "audit", "bench")
MOCKPY = os.path.join(REPO, "tests", "audit", "stream", "mock_llm.py")
STUBS = [os.path.join(REPO, "tests", "audit", "nosel", "stubs"), os.path.join(REPO, "tests", "audit", "tts", "stubs")]
ONLY = {x.strip().upper() for x in A.only.split(",") if x.strip()}
DETPY, NOORT, MODEL, B5 = A.det_python, A.noort_python, A.model, A.b5_dir
REAL_HOME = os.path.realpath(os.path.expanduser("~/.speakeasy"))
TMPROOT = os.path.realpath(tempfile.mkdtemp(prefix="se_p5_")); T0 = time.time()
VMODEL = "qwen3-vl:4b-instruct"
PP_KEYS = {"state", "sent", "sentence_id", "fig", "panel", "scope", "candidates", "cand_rects", "chosen_index", "p_top", "p_margin", "letter_mass",
           "palette", "called", "outcome", "applied", "drop", "ms", "queue_ms", "model", "provider", "fallback_from", "confidence", "ts", "pid"}
OUTCOMES = {"accepted", "low-confidence", "off-menu", "no-logprobs", "unavailable", "error", "timeout", "out-of-scope"}
MISS = ("error", "low-confidence", "off-menu", "no-logprobs", "unavailable", "out-of-scope")          # 6.2 as amended (A2.6, A2.9)
SKIPPED = {"too-few", "whole-figure", "disabled", "doc-not-ready", "degenerate-figure", "generic-phrase"}
def breaker_of(pk, who="local"):
    b = (pk or {}).get("breaker"); return b.get(who) if isinstance(b, dict) else (b if who == "local" else None)
PART_KEYS = {"id", "kind", "n", "t", "label", "rect", "panel", "attached", "src"}
PANEL_KEYS = {"label", "bbox", "src", "guessed"}
sys.path.insert(0, BENCH)
import score as S
from run_bench import geometry_for
from geom import page_to_screen, xywh, K

R = []
def _s(d): return " ".join(str(d).split())[:600]
def add(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"; R.append((st, name)); print(f"{st} {name} {_s(detail) if not ok or os.environ.get('P5_VERBOSE') else ''}".rstrip(), flush=True)
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
def isrect(r): return isinstance(r, list) and len(r) == 4 and all(isnum(v) for v in r) and r[0] <= r[2] and r[1] <= r[3]
def jl(p):
    out = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x:
                try: out.append(json.loads(x))
                except Exception: out.append({"_raw": x})
    return out
def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1])); i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0
def ctr(r): return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
def inside(pt, r, s=0.0): return r[0] - s <= pt[0] <= r[2] + s and r[1] - s <= pt[1] <= r[3] + s
def within(a, b, s=0.6): return a[0] >= b[0] - s and a[1] >= b[1] - s and a[2] <= b[2] + s and a[3] <= b[3] + s
def area(r): return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
def strip_ms(d):
    if isinstance(d, dict): return {k: strip_ms(v) for k, v in d.items() if k != "ms"}
    if isinstance(d, list): return [strip_ms(x) for x in d]
    return d
def norm(s): return re.sub(r"\s+", " ", s or "").strip()

# ---------------------------------------------------------------- setup
if not os.path.isfile(MODEL): print(f"FATAL layout model missing at {MODEL!r} (pass --model or set SPEAKEASY_LAYOUT_MODEL)"); sys.exit(2)
NO_HOLDOUT = os.path.join(TMPROOT, "no-holdout")   # run_bench's hidden-set lookup points here unless --holdout is given
BASE = os.path.join(TMPROOT, "base"); os.makedirs(BASE)
subprocess.run(f"git -C '{REPO}' archive {A.baseline_ref} | tar -x -C '{BASE}'", shell=True, check=True)
for f in glob.glob(os.path.join(BENCH, "*.py")): shutil.copy(f, os.path.join(BASE, "tests", "audit", "bench"))
shutil.copy(MOCKPY, os.path.join(BASE, "tests", "audit", "stream"))  # B5 code measured with the current (verifier) harness
BASE_SRC = os.path.join(BASE, "src"); BASE_CLI = os.path.join(BASE_SRC, "speakeasy")
FXD = os.path.join(TMPROOT, "fx"); os.makedirs(FXD)
subprocess.run([NOORT, os.path.join(HERE, "make_p5docs.py"), FXD], check=True, capture_output=True)
FX = {k: os.path.join(FXD, k + ".pdf") for k in ("callouts", "panels", "single", "area", "hairline")}
GT = json.load(open(os.path.join(FXD, "p5_gt.json")))
PUB = os.path.join(BENCH, "fixtures", "public"); PUBLIC = sorted(glob.glob(os.path.join(PUB, "*", "doc.pdf")))
STUBD = os.path.join(TMPROOT, "stubs"); os.makedirs(STUBD)
open(os.path.join(STUBD, "security"), "w").write("#!/bin/sh\necho 'security: stubbed (no keychain in tests)' >&2\nexit 44\n"); os.chmod(os.path.join(STUBD, "security"), 0o755)
_real_snapshot = {}
for sub in ("models", "cache/doc", "logs/runs.jsonl", "logs/focus.jsonl", "focus.json"):
    p = os.path.join(REAL_HOME, sub)
    _real_snapshot[sub] = (sorted(os.listdir(p)) if os.path.isdir(p) else (os.path.getsize(p), os.path.getmtime(p))) if os.path.exists(p) else None

_n = [0]
def mkhome(tag="h"):
    _n[0] += 1; d = os.path.join(TMPROOT, "homes", f"{_n[0]:04d}-{tag}"); h = os.path.join(d, "home"); os.makedirs(h); os.makedirs(os.path.join(d, "tmp"))
    rh = os.path.realpath(h); assert rh.startswith(TMPROOT + os.sep) and not rh.startswith(REAL_HOME)
    return h
def penv(home, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "STUB_", "MOCK_")) and k.lower() not in ("pythonpath", "no_proxy", "https_proxy", "http_proxy", "all_proxy")}
    e.update(HOME=home, SPEAKEASY_HOME=home, TMPDIR=os.path.join(os.path.dirname(home), "tmp"), PYTHONDONTWRITEBYTECODE="1")
    for k, v in kw.items():
        if v is None: e.pop(k, None)
        else: e[k] = str(v)
    return e

def parse(pdf, src=None, model=MODEL, home=None, py=None):
    py = py or DETPY; src = src or SRC; home = home or mkhome("parse"); d0 = os.path.dirname(home)
    cp = os.path.join(d0, f"in_{time.time_ns()}.pdf"); shutil.copyfile(pdf, cp); out = os.path.join(d0, f"out_{time.time_ns()}.json")
    kw = {"SPEAKEASY_LAYOUT_MODEL": model} if model else {}
    t = time.time()
    try: p = subprocess.run([py, os.path.join(src, "docmap.py"), "parse-pdf", cp, out], capture_output=True, text=True, env=penv(home, **kw), timeout=300)
    except subprocess.TimeoutExpired: return {"rc": "timeout", "d": None, "err": "", "ms": None}
    ms = round((time.time() - t) * 1000)
    try: d = json.load(open(out))
    except Exception: d = None
    return {"rc": p.returncode, "d": d, "err": p.stderr, "ms": ms, "home": home}
def pages(d): return [p for p in ((d or {}).get("pages") or []) if isinstance(p, dict)]
def figs(d): return [(p.get("n"), f) for p in pages(d) for f in (p.get("figures") or []) if isinstance(f, dict)]
def fig(d, page, label):
    return next((f for n, f in figs(d) if n == page and f.get("label") == label), None)

# ---------------------------------------------------------------- mock + CLI driver
class Mock:
    def __init__(self, **env):
        d = tempfile.mkdtemp(prefix="mock_", dir=TMPROOT); self.log = os.path.join(d, "mock.jsonl"); self.reply = os.path.join(d, "reply.txt")
        pf = os.path.join(d, "port"); open(self.reply, "w").close(); open(self.log, "w").close()
        e = {k: v for k, v in os.environ.items() if not k.startswith(("MOCK_", "SPEAKEASY_"))}
        e.update(MOCK_PORT="0", MOCK_PORT_FILE=pf, MOCK_LOG=self.log, MOCK_REPLY_FILE=self.reply, MOCK_FIRST_DELAY="0.3", MOCK_TOKEN_DELAY="0.002")
        e.update({k: str(v) for k, v in env.items()})
        self.p = subprocess.Popen([sys.executable, MOCKPY], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time(); self.port = None
        while time.time() - t0 < 5 and self.port is None:
            if os.path.exists(pf):
                try: self.port = int(open(pf).read())
                except ValueError: pass
            time.sleep(0.03)
        if self.port is None: raise RuntimeError("mock did not start")
    def recs(self): return jl(self.log)
    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try: self.p.wait(5)
            except subprocess.TimeoutExpired: self.p.kill()

def api_posts(recs): return [r for r in recs if r.get("method") == "POST" and str(r.get("path", "")).split("?")[0].rstrip("/").endswith("/api/chat")]
def pick_posts(recs): return [r for r in api_posts(recs) if r.get("n_images")]
def pick_replies(recs): return [r for r in recs if r.get("event") == "pick_reply"]

PICK_ENV = dict(SPEAKEASY_GROUND="local", SPEAKEASY_PICK="on", SPEAKEASY_VISION_MODEL=VMODEL)

def area_geometry(pdf, page, area):
    """Bench geometry G0 for a Read-area run: the area rect on screen and OCR-like lines (page lines whose centre is in the area)."""
    pg = next(p for p in pages(P(pdf)["d"]) if p.get("n") == page)
    s_ = 1.25 * K; g = {"zoom": 1.25, "backing": 2, "ox": (1512 - pg["w"] * s_) / 2, "oy": 300 - area[1] * s_}
    ls = sorted([l for l in pg["lines"] if inside(ctr(l[:4]), area)], key=lambda l: (round((l[1] + l[3]) / 2 / 3), l[0]))   # OCR reading order: rows, then x
    cap = ",".join(f"{v:.1f}" for v in xywh(page_to_screen(area, g)))
    out, loc = [], 0                                   # the app's OCR line-bounds format (app/Sources/Speakeasy/Reader.swift)
    for l in ls:
        out.append({"loc": loc, "abs": loc, "len": len(l[4]), "rect": [round(v, 1) for v in xywh(page_to_screen(l[:4], g))]}); loc += len(l[4]) + 1
    return cap, out, "\n".join(l[4] for l in ls)

def speak(pdf, page, cap_prefix, script, cli=None, mock_env=None, env=None, stub_play=0.05, port=None, poll_focus=False, warm=True, tag="cli", area=None, hint_doc=None):
    """One CLI run on `pdf` (selection: the caption line starting with cap_prefix) speaking `script` (list of sentences) through
    the mock. Returns a dict with rc, rec (runs record), focus, mock (records of the measured run), spoken, shows by sentence, etc."""
    cli = cli or CLI; home = mkhome(tag); d0 = os.path.dirname(home); tmpd = os.path.join(d0, "tmp")
    if area is None:
        sel, pw = caption_sel(pdf, page, cap_prefix)
        case = {"selection": sel}; cap, lb = geometry_for(case, pw); stdin = "\n".join(it["text"] for it in sel)
    else: cap, lb, stdin = area_geometry(pdf, page, area)
    cp = os.path.join(d0, "doc.pdf")
    if not os.path.exists(cp): shutil.copyfile(pdf, cp)
    if hint_doc is not None: cp = hint_doc
    hint = os.path.join(d0, "hint.json"); lbp = os.path.join(d0, "lines.json")
    json.dump({"app": "Preview", "bundle": "com.apple.Preview", "title": os.path.basename(cp), "ax_document": "file://" + cp}, open(hint, "w")); json.dump(lb, open(lbp, "w"))
    mock = Mock(**(mock_env or {})); open(mock.reply, "w").write(" ".join(script))
    spoken = os.path.join(d0, "spoken.txt"); stublog = os.path.join(d0, "stub.jsonl")
    base = dict(SPEAKEASY_LLM_URL=f"http://127.0.0.1:{port or mock.port}/v1", SPEAKEASY_MODEL="mock-text", SPEAKEASY_LLM="local", SPEAKEASY_VISION="never",
                SPEAKEASY_GROUND="off", SPEAKEASY_TTS="say", SPEAKEASY_DOC_READ="local", SPEAKEASY_DOC_PYTHON=DETPY, SPEAKEASY_LAYOUT_MODEL=MODEL,
                SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker", "speakeasy-track"), SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1",
                SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_TEXT_HIGHLIGHT="on", SPEAKEASY_FIGURE_CUES="on", SPEAKEASY_KEEP_CAPTURE="0", SPEAKEASY_LOG_TEXT="1",
                STUB_PLAY_SEC=str(stub_play), STUB_SPOKEN=spoken, STUB_LOG=stublog, PATH=":".join([STUBD] + STUBS + [os.environ.get("PATH", "/usr/bin:/bin")]))
    base.update(env or {})
    e = penv(home, **base)
    cmd = [cli, "speak", "--capture-method", "bounds" if area is None else "area", f"--capture-rect={cap}", "--line-bounds", lbp, "--doc-hint", hint, "--run-id", f"p5-{_n[0]}"]
    if area is not None: cmd += ["--area-text", "ocr"]
    def run():
        t = time.time()
        try: p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=e, timeout=120); rc, err = p.returncode, p.stderr
        except subprocess.TimeoutExpired: rc, err = "timeout", ""
        return rc, err, time.time() - t
    out = {"home": home, "tmp": tmpd, "pdf": pdf}
    try:
        if warm:
            rc, err, _ = run(); out["warm_rc"] = rc; out["warm_focus"] = jl(os.path.join(home, "logs", "focus.jsonl"))
        for attempt in range(3):
            shutil.rmtree(os.path.join(home, "logs"), ignore_errors=True)
            if os.path.exists(spoken): os.remove(spoken)
            m0 = len(mock.recs()); seen = []; stop = threading.Event(); out["t_start"] = time.time()
            def poll():
                fj = os.path.join(home, "focus.json")
                while not stop.is_set():
                    try: seen.append(json.load(open(fj)).get("state"))
                    except Exception: pass
                    time.sleep(0.02)
            th = threading.Thread(target=poll, daemon=True)
            if poll_focus: th.start()
            rc, err, wall = run(); stop.set()
            recs = jl(os.path.join(home, "logs", "runs.jsonl")); rec = recs[0] if len(recs) == 1 else {}
            dr, fs = rec.get("doc_ready_ms"), rec.get("first_sentence_ms")
            if not warm or dr is None or fs is None or dr <= fs or attempt == 2: break
        time.sleep(0.3)
        focus = jl(os.path.join(home, "logs", "focus.jsonl"))
        out.update(rc=rc, err=err, wall=wall, rec=rec, nrecs=len(recs), focus=focus, mock=mock.recs()[m0:], mock_all=mock.recs(), focus_states=seen,
                   spoken=open(spoken).read() if os.path.exists(spoken) else "", leftovers=leftovers(tmpd))
    finally:
        mock.stop()
    maps = [r for r in focus if r.get("state") == "map"]; shows = [r for r in focus if r.get("state") == "show"]
    ids = [(f"s{i}", t) for i, t in enumerate(script)]
    try: owner = S.attribute(ids, maps)
    except Exception as ex: owner = {}; out["attr_error"] = str(ex)
    cshow = S.chunk_shows(shows); by = collections.defaultdict(list)
    for k, sid in sorted(owner.items()): by[int(sid[1:])].append(cshow.get(k))
    out.update(owner=owner, cshow=cshow, by_sent=by, maps=maps, shows=shows, picks=[r for r in focus if r.get("state") == "part_pick"])
    rq, ft, ff = rec.get("llm_request_at_ms"), rec.get("llm_first_token_ms"), rec.get("first_focus_ms")
    out["fh"] = ff - (rq + ft) if None not in (ff, rq, ft) else None
    out["chunk_of_pick"] = {r.get("sent"): owner.get(r.get("sent")) for r in out["picks"]}
    return out

def leftovers(tmpd):
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(tmpd, "**", "speakeasy_cap_pick_*"), recursive=True))

def ovals(o, i): return [s for s in o["by_sent"].get(i, []) if s and s.get("cue_kind") == "part_oval"]
def tsrc(s): return ((s or {}).get("doc") or {}).get("target_src")
def target(s): return ((s or {}).get("doc") or {}).get("target")

def common_run_checks(name, o, script, fh_ref=None, area_run=False):
    add(f"{name}_exit0", o.get("rc") == 0 and o.get("nrecs") == 1, (o.get("rc"), o.get("nrecs"), (o.get("err") or "")[-300:]))
    sp = norm(o.get("spoken"))
    add(f"{name}_every_sentence_spoken", all(norm(t) in sp for t in script), [t for t in script if norm(t) not in sp][:3])
    if not area_run:   # A2.3: in area runs a chunk with no target holds or draws nothing
        add(f"{name}_every_chunk_shown", len(o["maps"]) > 0 and {r.get("sent") for r in o["maps"]} <= set(o["cshow"]), (len(o["maps"]), len(o["cshow"])))
    fh = o.get("fh")
    if not area_run or (o["shows"] and 0 in {r.get("sent") for r in o["shows"]}): add(f"{name}_first_highlight_lt_300", fh is not None and fh < 300, fh)
    if fh_ref is not None and fh is not None: add(f"{name}_first_highlight_within_noise", abs(fh - fh_ref) <= 150, (fh, fh_ref))
    add(f"{name}_no_temp_pick_png", not o["leftovers"], o["leftovers"][:3])
    pp_record_checks(name, o)

def pp_record_checks(name, o):
    bad = []
    for r in o["picks"]:
        if set(r) != PP_KEYS: bad.append(("keys", sorted(set(r) ^ PP_KEYS))); continue
        if r["outcome"] not in OUTCOMES: bad.append(("outcome", r["outcome"]))
        if not isint(r["sent"]) or not isint(r["candidates"]) or not isinstance(r["called"], bool) or not isinstance(r["applied"], bool): bad.append(("types", r))
        if r["scope"] not in ("panel", "figure"): bad.append(("scope", r["scope"]))
        if r["scope"] == "panel" and not (isinstance(r["panel"], str) and re.fullmatch(r"[a-z]", r["panel"])): bad.append(("panel", r["panel"]))
        if not (r["fig"] is None or (isinstance(r["fig"], str) and re.fullmatch(r"(Figure|Table) \d+", r["fig"]))): bad.append(("fig", r["fig"]))
        if not (r["sentence_id"] is None or (isinstance(r["sentence_id"], str) and re.fullmatch(r"s\d+", r["sentence_id"]))): bad.append(("sid", r["sentence_id"]))
        if r["palette"] not in ("red", "magenta", "blue", None): bad.append(("palette", r["palette"]))
        if r["drop"] not in ("superseded", "no-fit", None): bad.append(("drop", r["drop"]))
        if r["provider"] not in ("local", "openrouter"): bad.append(("provider", r["provider"]))
        if r["fallback_from"] not in (None, "unavailable", "no-logprobs"): bad.append(("fallback_from", r["fallback_from"]))
        if r["confidence"] not in (None, "logprob", "letter-only"): bad.append(("confidence", r["confidence"]))
        if r["provider"] == "local" and (r["fallback_from"] is not None or r["confidence"] == "letter-only"): bad.append(("local_fallback_or_letter_only", r["fallback_from"], r["confidence"]))
        if not (isinstance(r["cand_rects"], list) and len(r["cand_rects"]) == r["candidates"] and all(isrect(x) and all(abs(round(v, 1) - v) < 1e-9 for v in x) for x in r["cand_rects"])): bad.append(("cand_rects", r["candidates"]))
        if not 2 <= (r["candidates"] or 0) <= 16: bad.append(("candidates_range", r["candidates"]))
        if r["outcome"] in ("accepted", "low-confidence"):
            if not (isint(r["chosen_index"]) and 0 <= r["chosen_index"] < r["candidates"]): bad.append(("chosen_index", r["chosen_index"]))
        elif r["chosen_index"] is not None: bad.append(("chosen_index_not_null", r["outcome"], r["chosen_index"]))
        for k in ("p_top", "p_margin", "letter_mass"):
            v = r[k]
            if v is not None and not (isnum(v) and 0 <= v <= 1.0001 and abs(round(v, 2) - v) < 1e-9): bad.append((k, v))
        if r["outcome"] in ("no-logprobs", "unavailable") and any(r[k] is not None for k in ("p_top", "p_margin", "letter_mass")): bad.append(("p_null", r["outcome"]))
        if r["applied"] and r["outcome"] != "accepted": bad.append(("applied_not_accepted", r["outcome"]))
        for k, v in r.items():
            if isinstance(v, str) and k not in ("state", "scope", "outcome", "palette", "drop", "model", "provider", "fig", "sentence_id", "panel", "fallback_from", "confidence"): bad.append(("str", k))
            if isinstance(v, str) and k != "model" and (len(v) > 24 or (k not in ("fig",) and " " in v)): bad.append(("long_str", k, v[:30]))
            if k == "model" and not (isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", v)): bad.append(("model", v))
    add(f"{name}_part_pick_records_schema_no_text", not bad, bad[:4])
    texts = [json.dumps([v for v in r.values() if isinstance(v, str)]) for r in o["picks"]]
    leak = [w for w in ("turbidity", "spare", "pipe", "Intake", "Sand", "filter", "Clear", "water", "colour", "letter", ".png", "base64") if any(w.lower() in t.lower() for t in texts)]
    add(f"{name}_part_pick_no_phrase_label_prompt_with_LOG_TEXT_1", not leak, leak)
    add(f"{name}_part_pick_one_record_per_pick", len({(r.get("sent"), r.get("ts")) for r in o["picks"]}) == len(o["picks"]) and
        len(o["picks"]) == len({r.get("sent") for r in o["picks"]}), [(r.get("sent"), r.get("outcome")) for r in o["picks"]])
    if o.get("focus_states") is not None and o.get("focus_states"):
        add(f"{name}_part_pick_never_in_focus_json", "part_pick" not in o["focus_states"], collections.Counter(o["focus_states"]))
    rec = o.get("rec") or {}; pk = rec.get("part_picks")
    if o["picks"] or pk:
        ok = isinstance(pk, dict) and {"requests", "called", "accepted", "applied", "outcomes", "skipped", "ms", "threshold", "breaker"} <= set(pk)
        b = (pk or {}).get("breaker")
        bok = b is None or b in ("unavailable", "no-logprobs") or (isinstance(b, dict) and set(b) <= {"local", "openrouter"} and all(x in (None, "unavailable", "no-logprobs") for x in b.values()))
        add(f"{name}_runs_part_picks_schema", ok and set((pk.get("skipped") or {})) <= SKIPPED and isinstance(pk.get("ms"), dict) and bok
            and set(pk["ms"]) >= {"p50", "max"} and isnum(pk.get("threshold")) and 0.4 <= pk["threshold"] <= 0.95
            and set(pk.get("by_provider") or {}) <= {"local", "openrouter"} and set(pk.get("fallback") or {}) <= {"unavailable", "no-logprobs"}, pk)
        if ok and isinstance(pk.get("by_provider"), dict):
            bp = collections.Counter(r.get("provider") for r in o["picks"])
            add(f"{name}_runs_by_provider_matches_records", {k: v for k, v in pk["by_provider"].items() if v} == dict(bp), (pk["by_provider"], dict(bp)))
        if ok:
            oc = collections.Counter(r.get("outcome") for r in o["picks"])
            add(f"{name}_runs_part_picks_match_records", pk["requests"] == len(o["picks"]) and pk["called"] == sum(1 for r in o["picks"] if r.get("called"))
                and pk["accepted"] == oc.get("accepted", 0) and pk["applied"] == sum(1 for r in o["picks"] if r.get("applied"))
                and {k: v for k, v in (pk.get("outcomes") or {}).items() if v} == dict(oc), (pk, dict(oc)))
            gm = rec.get("ground_miss_reasons") or {}; fm = rec.get("focus_methods") or {}
            add(f"{name}_ground_invariant_sum", sum(gm.values()) + fm.get("ground-pick", 0) == len(o["picks"]), (gm, fm, len(o["picks"])))
            add(f"{name}_retired_bbox_keys_absent", "ground-vlm" not in fm and "ground_box_convention" not in rec and not ({"no-box", "too-large"} & set(gm)), (fm, sorted(gm)))
            miss = sum(gm.get(k, 0) for k in MISS)
            add(f"{name}_ground_miss_equals_reasons", fm.get("ground-miss", 0) == miss, (fm.get("ground-miss"), gm))
            add(f"{name}_ground_pick_counts_applied", fm.get("ground-pick", 0) == pk["applied"], (fm.get("ground-pick"), pk["applied"]))
            answered = [r for r in o["picks"] if r.get("outcome") in ("accepted", "low-confidence", "off-menu", "out-of-scope")]
            if answered: add(f"{name}_ground_provider_is_last_answering_provider", rec.get("ground_provider") == answered[-1].get("provider") or rec.get("ground_provider") in {r.get("provider") for r in answered}, (rec.get("ground_provider"), [r.get("provider") for r in answered]))
            else: add(f"{name}_ground_provider_absent_without_answer", rec.get("ground_provider") in (None,), rec.get("ground_provider"))
    inv = invariant_counts(o)
    add(f"{name}_inv_callout_target_lt_40pct_of_panel", inv["callout_oversize"] == 0, inv)
    add(f"{name}_inv_no_hairline_figure_box", inv["hairline_draw"] == 0, inv)
    called = sum(1 for r in o["picks"] if r.get("called"))
    add(f"{name}_mock_requests_match_called_records", len(pick_posts(o["mock"])) == called, (len(pick_posts(o["mock"])), called))

def invariant_counts(o, area_rect=None):
    """A2.11 / A4.1 hard invariants on one run's shows (each must be 0). area: page-space area rect (area runs)."""
    out = {"callout_oversize": 0, "hairline_draw": 0, "stand_in": 0, "area_leak": 0}
    pdf = o.get("pdf"); d = P(pdf)["d"] if pdf else None
    pls = {pg.get("n"): pg.get("lines") or [] for pg in pages(d)}
    for sh in o["shows"]:
        dd = sh.get("doc") or {}; t = dd.get("target")
        if not isrect(t): continue
        pf = [f for n, f in figs(d) if n == dd.get("page")] if d else []
        if sh.get("cue_kind") == "figure_box" and (t[2] - t[0] < 10 or t[3] - t[1] < 10): out["hairline_draw"] += 1
        if dd.get("target_src") in ("callout", "callout-marker"):
            f = next((f for f in pf if inside(ctr(t), f["bbox"])), None)
            if f:
                pb = next((x["bbox"] for x in f.get("panels") or [] if isrect(x.get("bbox")) and inside(ctr(t), x["bbox"])), f["bbox"])
                if area(t) >= 0.4 * area(pb): out["callout_oversize"] += 1
    if area_rect is not None:
        boxes = [f["bbox"] for n, f in figs(d)] + [x["bbox"] for n, f in figs(d) for x in f.get("panels") or [] if isrect(x.get("bbox"))]
        for sh in o["shows"]:
            dd = sh.get("doc") or {}; t = dd.get("target")
            if not isrect(t): continue
            if iou(t, area_rect) >= 0.9 and not (sh.get("cue_kind") == "figure_box" and any(iou(t, b) >= 0.98 for b in boxes)): out["stand_in"] += 1
            if sh.get("cue_kind") == "text_band":
                rs = [r for r in (dd.get("lines") or []) if isrect(r)] or [t]
                for l in pls.get(dd.get("page"), []):
                    c = ctr(l[:4])
                    if not inside(c, area_rect) and any(inside(c, r) for r in rs): out["area_leak"] += 1; break
    return out

# ---------------------------------------------------------------- shared parses
PARSED = {}
def P(pdf, src=None):
    key = (pdf, src or SRC)
    if key not in PARSED: PARSED[key] = parse(pdf, src=src)
    return PARSED[key]
def caption_sel(pdf, page, prefix, d=None):
    d = d or P(pdf)["d"] or P(pdf, BASE_SRC)["d"]
    pg = next(p for p in pages(d) if p.get("n") == page)
    ln = next(l for l in pg["lines"] if str(l[4]).startswith(prefix))
    return [{"text": ln[4], "rect": [ln[0], ln[1], ln[2], ln[3]]}], pg["w"]


# ---------------------------------------------------------------- S: parse-time hierarchy
def part_schema_issues(d):
    bad = []
    for n, f in figs(d):
        parts = f.get("parts"); panels = f.get("panels") or []; fb = f.get("bbox")
        if not isinstance(parts, list): bad.append((n, f.get("label"), "no parts list")); continue
        letters = {p.get("label") for p in panels if isinstance(p, dict)}
        for i, p in enumerate(parts):
            if not isinstance(p, dict) or set(p) != PART_KEYS: bad.append((n, f.get("label"), i, "keys", sorted(set(p or {}) ^ PART_KEYS))); continue
            if p["id"] != f"p{i + 1}": bad.append((n, f.get("label"), i, "id", p["id"]))
            if p["kind"] not in ("text", "callout"): bad.append((n, i, "kind", p["kind"]))
            if p["kind"] == "callout" and not (isint(p["n"]) and 1 <= p["n"] <= 99): bad.append((n, i, "n", p["n"]))
            if p["kind"] == "text" and p["n"] is not None: bad.append((n, i, "text n", p["n"]))
            if not isinstance(p["t"], str): bad.append((n, i, "t"))
            if not (isrect(p["label"]) and isrect(p["rect"])): bad.append((n, i, "rects")); continue
            if not isinstance(p["attached"], bool) or p["src"] not in ("pdf", "ocr"): bad.append((n, i, "attached/src", p["attached"], p["src"]))
            if p["panel"] is not None and p["panel"] not in letters: bad.append((n, i, "panel", p["panel"], sorted(x for x in letters if x)))
            if isrect(fb) and not within(p["rect"], fb): bad.append((n, f.get("label"), i, "rect outside figure", p["rect"], fb))
            pb = next((x.get("bbox") for x in panels if isinstance(x, dict) and x.get("label") == p["panel"]), None)
            if p["panel"] and isrect(pb) and not within(p["rect"], pb): bad.append((n, f.get("label"), i, "rect crosses panel", p["panel"], p["rect"], pb))
            if p["kind"] == "text" and not re.search(r"[A-Za-z]{2,}", p["t"]): bad.append((n, i, "text part without 2 letters", p["t"]))
        tl = sorted(norm(l[4]) for l in (f.get("labels") or []) if isinstance(l, list) and len(l) >= 5)
        tp = sorted(norm(p.get("t")) for p in parts if isinstance(p, dict) and p.get("kind") == "text")
        if f.get("kind") != "table" and tl != tp: bad.append((n, f.get("label"), "text parts != labels", len(tl), len(tp)))
        for x in panels:
            if not isinstance(x, dict) or set(x) != PANEL_KEYS: bad.append((n, f.get("label"), "panel keys", sorted(set(x or {}) ^ PANEL_KEYS))); continue
            if x["src"] not in ("label", "gutter", "caption") or x["guessed"] is not (x["src"] == "caption"): bad.append((n, f.get("label"), "panel src/guessed", x["src"], x["guessed"]))
            if isrect(fb) and not within(x["bbox"], fb): bad.append((n, f.get("label"), "panel outside figure", x["bbox"], fb))
        for o in f.get("objects") or []:
            pass
    return bad

def callouts_of(f, num=None): return [p for p in (f.get("parts") or []) if isinstance(p, dict) and p.get("kind") == "callout" and (num is None or p.get("n") == num)]

@section("S")
def s_parse():
    cur = {k: P(v) for k, v in FX.items()}; base = {k: P(v, BASE_SRC) for k, v in FX.items()}
    for k, r in cur.items(): add(f"S_parse_ok_{k}", r["rc"] == 0 and r["d"] is not None, (r["rc"], (r["err"] or "")[-300:]))
    pub_cur = {os.path.basename(os.path.dirname(p)): P(p) for p in PUBLIC}; pub_base = {os.path.basename(os.path.dirname(p)): P(p, BASE_SRC) for p in PUBLIC}
    allbad = []
    for k, r in list(cur.items()) + list(pub_cur.items()): allbad += [(k,) + tuple(b) for b in part_schema_issues(r["d"])]
    add("S_parts_panels_schema_all_figures", not allbad, allbad[:5])
    # labels / objects unchanged vs B5; drawn-label panels unchanged
    lab, obj, pan = [], [], []
    for k in list(FX) + sorted(pub_cur):
        c, b = (cur.get(k) or pub_cur.get(k))["d"], (base.get(k) or pub_base.get(k))["d"]
        bf = {(n, f.get("label")): f for n, f in figs(b)}
        for n, f in figs(c):
            g = bf.get((n, f.get("label")))
            if g is None: continue
            if (f.get("labels") or []) != (g.get("labels") or []): lab.append((k, n, f.get("label")))
            if [list(o[:5]) for o in f.get("objects") or []] != [list(o[:5]) for o in g.get("objects") or []]: obj.append((k, n, f.get("label")))
            if g.get("panels") and not any(x.get("guessed") for x in g["panels"]):
                fb = f.get("bbox") or [-1e9, -1e9, 1e9, 1e9]
                clip = lambda r: [max(r[0], fb[0]), max(r[1], fb[1]), min(r[2], fb[2]), min(r[3], fb[3])]
                cp_ = [(x.get("label"), x.get("bbox")) for x in f.get("panels") or []]; bp_ = [(x.get("label"), clip(x.get("bbox"))) for x in g["panels"]]
                same = len(cp_) == len(bp_) and all(a[0] == b[0] and isrect(a[1]) and max(abs(u - v) for u, v in zip(a[1], b[1])) <= 0.15 for a, b in zip(cp_, bp_))
                if not same and [a[0] for a in cp_] == [b[0] for b in bp_] and all(x.get("src") == "label" for x in f.get("panels") or []):
                    info(f"S {k} p{n} {f.get('label')}: drawn-label panels re-boxed (A3.4 labels-below): {cp_} (B5 {bp_})"); same = True
                if not same or any(x.get("src") != "label" for x in f.get("panels") or []):
                    pan.append((k, n, f.get("label"), cp_, bp_))
    add("S_labels_unchanged_vs_B5", not lab, lab[:5]); add("S_objects_unchanged_vs_B5", not obj, obj[:5]); add("S_label_panels_unchanged_vs_B5", not pan, pan[:5])
    # callouts.pdf page 1
    g1 = GT["callouts"]["p1"]; f1 = fig(cur["callouts"]["d"], 1, "Figure 1")
    if not f1: add("S_callouts_fig1_found", False, [x[1].get("label") for x in figs(cur["callouts"]["d"])]); return
    co = callouts_of(f1)
    add("S_callout_numbers_exactly_1_to_5", sorted(p["n"] for p in co) == [1, 2, 3, 4, 5], sorted(p.get("n") for p in co))
    def one(n): c = callouts_of(f1, n); return c[0] if len(c) == 1 else None
    for n, key, why in ((1, "O1", "enclosed_no_leader_nearest_object"), (2, "O2", "circled_glyph_nearest_object"), (3, "O3", "leader_far_end_object_not_marker_neighbour"),
                        (4, "O5", "two_segment_leader_smallest_object_at_far_end")):
        c = one(n)
        add(f"S_callout_{n}_{why}", bool(c) and c["attached"] is True and iou(c["rect"], g1[key]) >= 0.8, (c and (c["rect"], c["attached"]), g1[key]))
    c3, c4 = one(3), one(4)
    add("S_callout_3_not_on_nearby_object_O4", bool(c3) and iou(c3["rect"], g1["O4"]) < 0.3, c3 and c3["rect"])
    add("S_callout_4_not_on_enclosing_object_O6", bool(c4) and iou(c4["rect"], g1["O6"]) < 0.5, c4 and c4["rect"])
    c5 = one(5)
    add("S_callout_5_enclosed_unattached_padded_marker", bool(c5) and c5["attached"] is False and inside(ctr(g1["M5"]), c5["rect"]) and area(c5["rect"]) <= 44 * 44,
        c5 and (c5["rect"], c5["attached"]))
    add("S_callout_2_circled_glyph_text", bool(one(2)) and one(2)["t"] in ("\u2461", "2"), one(2) and one(2)["t"])
    ticks = g1["ticks"]
    tk = [p for p in f1.get("parts") or [] if any(iou(p["label"], t) > 0.2 or inside(ctr(p["label"]), t, 1.0) for t in ticks)]
    add("S_axis_ticks_never_parts", not tk, [(p["id"], p["kind"], p["t"]) for p in tk])
    add("S_callouts_not_in_labels", not any(re.fullmatch(r"\d+|[\u2460-\u24ff\u2776-\u2793]", norm(l[4])) for l in f1.get("labels") or []), f1.get("labels"))
    add("S_text_parts_are_the_labels", sorted(p["t"] for p in f1["parts"] if p["kind"] == "text") == sorted(["Intake", "Sand filter", "Clear well"]),
        [p["t"] for p in f1["parts"] if p["kind"] == "text"])
    ids_order = [ctr(p["label"]) for p in f1["parts"]]
    add("S_part_ids_reading_order", all((ids_order[i][1] <= ids_order[i + 1][1] + 12) for i in range(len(ids_order) - 1)), ids_order)
    # callouts.pdf page 2 (drawn panels)
    g2 = GT["callouts"]["p2"]; f2 = fig(cur["callouts"]["d"], 2, "Figure 2")
    if f2:
        add("S_fig2_label_panels", [(x.get("label"), x.get("src")) for x in f2.get("panels") or []] == [("a", "label"), ("b", "label")], f2.get("panels"))
        c1 = sorted(callouts_of(f2, 1), key=lambda p: p["rect"][0]); c3b = callouts_of(f2, 3)
        add("S_fig2_callout_1_in_both_panels", len(c1) == 2 and [p["panel"] for p in c1] == ["a", "b"], [(p["panel"], p["rect"]) for p in c1])
        add("S_fig2_callout_targets", len(c1) == 2 and iou(c1[0]["rect"], g2["Pa"]) >= 0.8 and iou(c1[1]["rect"], g2["Pb1"]) >= 0.8 and len(c3b) == 1
            and c3b[0]["panel"] == "b" and iou(c3b[0]["rect"], g2["Pb2"]) >= 0.8, ([p["rect"] for p in c1], [p["rect"] for p in c3b]))
        add("S_fig2_callout_target_not_panel_frame", all(iou(p["rect"], g2["Fa"]) < 0.5 and iou(p["rect"], g2["Fb"]) < 0.5 for p in callouts_of(f2)), [p["rect"] for p in callouts_of(f2)])
        add("S_fig2_callout_rect_clipped_to_panel", all(within(p["rect"], next(x["bbox"] for x in f2["panels"] if x["label"] == p["panel"])) for p in callouts_of(f2) if p["panel"]), "")
    else: add("S_fig2_found", False, "")
    # panels.pdf
    gp = GT["panels"]; d = cur["panels"]["d"]
    exp = {1: ("cols2", "gutter"), 2: ("grid4", "gutter"), 3: ("cols3_cap2", "caption"), 4: ("narrow", "caption"), 5: ("rows2", "gutter"), 6: ("arrow", "caption")}
    for pg, (key, src) in exp.items():
        f = fig(d, pg, f"Figure {pg}"); ps = (f or {}).get("panels") or []
        srcs = sorted({x.get("src") for x in ps})
        add(f"S_panels_{key}_src_{src}", srcs == [src], [(x.get("label"), x.get("src"), x.get("guessed"), x.get("bbox")) for x in ps])
        if src == "gutter":
            cells = gp[key]["cells"]; letters = [x.get("label") for x in ps]
            want = [chr(97 + i) for i in range(len(cells))]
            mapped = [next((x["label"] for x in ps if isrect(x.get("bbox")) and inside(ctr(c), x["bbox"])), None) for c in cells]
            add(f"S_panels_{key}_letters_reading_order", letters == want and mapped == want, (letters, mapped))
            add(f"S_panels_{key}_not_guessed", all(x.get("guessed") is False for x in ps), [x.get("guessed") for x in ps])
            add(f"S_panels_{key}_cells_disjoint", all(iou(a["bbox"], b["bbox"]) == 0 for i, a in enumerate(ps) for b in ps[i + 1:]), [x.get("bbox") for x in ps])
        else:
            add(f"S_panels_{key}_caption_guess_guessed_true", len(ps) == 2 and all(x.get("guessed") is True for x in ps), [x.get("guessed") for x in ps])
    # single.pdf
    fs = fig(cur["single"]["d"], 1, "Figure 1")
    add("S_single_figure_parsed", bool(fs) and isinstance(fs.get("parts"), list), fs and fs.get("parts"))
    # determinism
    for k in ("callouts", "panels"):
        r2 = parse(FX[k]); add(f"S_parse_deterministic_{k}", strip_ms(r2["d"]) == strip_ms(cur[k]["d"]), "differs")
    # parse time per figure page vs B5 (p95 over public docs + fixtures)
    deltas = []
    for pdf in PUBLIC + list(FX.values()):
        tc = min(parse(pdf)["ms"] for _ in range(2)); tb = min(parse(pdf, src=BASE_SRC)["ms"] for _ in range(2))
        nf = max(1, len({n for n, f in figs(P(pdf)["d"])}))
        deltas.append((tc - tb) / nf)
    deltas.sort(); p95 = deltas[max(0, math.ceil(0.95 * len(deltas)) - 1)]
    info(f"S parse delta ms per figure page vs B5: p50 {deltas[len(deltas) // 2]:.0f} p95 {p95:.0f} (wall clock, includes process start)")
    add("S_extra_parse_time_per_figure_page_p95_le_30ms", p95 <= 30, f"p95 {p95:.0f} ms")
    # PARSE_VERSION bump invalidates a B5 cache entry
    script = ["Figure 1 shows the treatment line.", "Callout 3 is the turbidity probe after the sand filter."]
    o5 = speak(FX["callouts"], 1, "Figure 1:", script, cli=BASE_CLI, warm=False, tag="pv-b5")
    home = o5["home"]; files0 = {p: open(p, "rb").read() for p in glob.glob(os.path.join(home, "cache", "doc", "*"))}
    add("S_B5_cli_filled_cache", bool(files0) and o5.get("rc") == 0, (o5.get("rc"), list(files0)[:2]))
    o = speak_in_home(home, FX["callouts"], 1, "Figure 1:", script)
    rec = o.get("rec") or {}
    newer = [p for p in glob.glob(os.path.join(home, "cache", "doc", "*")) if p not in files0 or open(p, "rb").read() != files0.get(p)]
    has_parts = any('"parts"' in open(p, errors="ignore").read() for p in newer)
    add("S_parse_version_bump_invalidates_B5_cache", rec.get("doc_cache") != "hit" and has_parts, (rec.get("doc_cache"), len(newer), has_parts))
    add("S_parse_version_rerun_hits_new_cache", (speak_in_home(home, FX["callouts"], 1, "Figure 1:", script).get("rec") or {}).get("doc_cache") == "hit", "")

def speak_in_home(home, pdf, page, prefix, script, **kw):
    """A measured run in an existing home (no warm-up), used for cache checks."""
    global mkhome
    orig = mkhome
    def same(tag="h"): return home
    mkhome = same
    try: return speak(pdf, page, prefix, script, warm=False, **kw)
    finally: mkhome = orig


# ---------------------------------------------------------------- C: deterministic path (gated env)
C1 = ["Figure 1 shows the treatment line.", "Callout 3 is the turbidity probe after the sand filter.", "Marker two is the flow meter.",
      "Callout (1) is the inlet valve.", "Callout 4 is the pressure gauge.", "Callout 5 is the spare port.", "Step 3 rinses the filter.",
      "About 3 % of the flow is recycled.", "The \u2462 sensor reads the turbidity."]
C2 = ["Figure 2 has two stages.", "In panel b, callout 1 is the second valve.", "Callout 3 is the drain.", "Panel a is on the left.",
      "Callout 3 is the drain again.", "Callout 1 is the first valve."]
C3 = ["Figure 2 has two stages.", "Callout 1 is a valve."]
FH_REF = {}

@section("C")
def c_deterministic():
    g1 = GT["callouts"]["p1"]; g2 = GT["callouts"]["p2"]
    o = speak(FX["callouts"], 1, "Figure 1:", C1, env={"SPEAKEASY_PICK": "on"}, tag="c1"); FH_REF["c1"] = o.get("fh")
    common_run_checks("C1", o, C1)
    add("C1_attribution", "attr_error" not in o, o.get("attr_error"))
    def oval_on(i, key, gtd):
        ov = ovals(o, i)
        return bool(ov) and all(tsrc(s) == "callout" and inside(ctr(target(s)), gtd[key], 0.5) for s in ov), [(tsrc(s), target(s)) for s in ov]
    for i, key, nm in ((1, "O3", "callout_3_leader_target"), (2, "O2", "marker_two_word_number"), (3, "O1", "callout_paren_1"), (4, "O5", "callout_4_two_segment_leader"),
                       (8, "O3", "circled_digit_alone")):
        ok, det = oval_on(i, key, g1); add(f"C1_{nm}_oval_target_src_callout", ok, det)
    add("C1_callout_ref_skips_word_match_sand_filter", not any(inside(ctr(target(s)), g1["O2"]) for s in ovals(o, 1)), [target(s) for s in ovals(o, 1)])
    ov5 = ovals(o, 5); m5 = g1["M5"]; side = m5[3] - m5[1]; pad = 1.25 * side      # padded by the marker/enclosure height (stroke included)
    add("C1_unattached_callout_oval_on_padded_marker_A4.1", bool(ov5) and all(tsrc(s) == "callout-marker" and inside(ctr(m5), target(s)) and within(target(s), [m5[0] - pad, m5[1] - pad, m5[2] + pad, m5[3] + pad]) for s in ov5),
        [(tsrc(s), target(s)) for s in ov5])
    add("C1_step_N_not_a_callout_ref", not any(tsrc(s) == "callout" for s in ovals(o, 6)), [tsrc(s) for s in ovals(o, 6)])
    add("C1_number_with_percent_not_a_callout_ref", not any(tsrc(s) == "callout" for s in ovals(o, 7)), [tsrc(s) for s in ovals(o, 7)])
    add("C1_no_panel_sized_oval", not any(iou(target(s), fig(P(FX["callouts"])["d"], 1, "Figure 1")["bbox"]) > 0.5 for s in o["shows"] if s.get("cue_kind") == "part_oval"), "")
    add("C1_gated_no_api_chat", not api_posts(o["mock_all"]), len(api_posts(o["mock_all"])))
    add("C1_gated_no_part_pick_record", not o["picks"] and not any(r.get("state") == "part_pick" for r in o.get("warm_focus") or []), len(o["picks"]))
    pk = (o.get("rec") or {}).get("part_picks")
    add("C1_gated_part_picks_disabled_only", pk is None or (pk.get("requests") == 0 and pk.get("called") == 0 and (pk.get("skipped") or {}).get("disabled", 0) >= 1), pk)
    dp = (o.get("rec") or {}).get("doc_parse") or {}; f1 = fig(P(FX["callouts"])["d"], 1, "Figure 1") or {}
    pg_parts = [p for n, f in figs(P(FX["callouts"])["d"]) if n == 1 for p in f.get("parts") or []]
    want = {"text": sum(p["kind"] == "text" for p in pg_parts), "callout": sum(p["kind"] == "callout" for p in pg_parts),
            "callout_attached": sum(p["kind"] == "callout" and p["attached"] for p in pg_parts)}
    add("C1_doc_parse_parts_counts", dp.get("parts") == want, (dp.get("parts"), want))
    add("C1_doc_parse_panels_gutter_key", isinstance((dp.get("panels") or {}).get("gutter"), int), dp.get("panels"))
    o2 = speak(FX["callouts"], 2, "Figure 2:", C2, tag="c2"); common_run_checks("C2", o2, C2)
    def on(o, i, key): ov = ovals(o, i); return bool(ov) and all(tsrc(s) == "callout" and inside(ctr(target(s)), g2[key], 0.5) for s in ov), [(tsrc(s), target(s)) for s in ov]
    for i, key, nm in ((1, "Pb1", "named_panel_b_callout_1"), (2, "Pb2", "unique_callout_3_whole_figure"), (4, "Pb2", "held_panel_a_does_not_restrict_callout_3"),
                       (5, "Pa", "held_panel_a_prefers_its_callout_1")):
        ok, det = on(o2, i, key); add(f"C2_{nm}", ok, det)
    add("C2_panel_sentence_no_part_oval", not ovals(o2, 3), [target(s) for s in ovals(o2, 3)])
    o3 = speak(FX["callouts"], 2, "Figure 2:", C3, tag="c3"); common_run_checks("C3", o3, C3)
    add("C3_ambiguous_callout_no_deterministic_oval", not ovals(o3, 1) and bool(o3["by_sent"].get(1)), [(tsrc(s), target(s)) for s in ovals(o3, 1)])


# ---------------------------------------------------------------- F: picker fallback matrix (pick env, verifier mock)
F_SCRIPT = ["Figure 1 shows the treatment line.", "The red valve controls the inflow.", "The blue pipe carries clean water to the tank.", "Callout 3 is the probe."]
def lp(p): return math.log(p)
def fixed(entries): return {"MOCK_PICK": "fixed", "MOCK_PICK_TOP_LOGPROBS": json.dumps([{"token": t, "logprob": lp(p)} for t, p in entries])}

def runF(name, mock_env, script=F_SCRIPT, stub_play=0.05, env=None, **kw):
    e = dict(PICK_ENV); e.update(env or {})
    o = speak(FX["callouts"], 1, "Figure 1:", script, mock_env=mock_env, env=e, stub_play=stub_play, tag=name, **kw)
    common_run_checks(name, o, script, FH_REF.get("c1"))
    return o

# part_pick records are logged at settle time, so log line order is not pick order: compare in submission order (sent, ts).
def by_submit(rs): return sorted(rs, key=lambda r: (r.get("sent") if isint(r.get("sent")) else 10 ** 9, r.get("ts") or 0))
def outcomes(o): return [r.get("outcome") for r in by_submit(o["picks"])]

@section("F")
def f_fallbacks():
    if "c1" not in FH_REF:
        FH_REF["c1"] = speak(FX["callouts"], 1, "Figure 1:", C1, tag="c1ref").get("fh")
    o = runF("F_none", {})
    add("F_none_picks_submitted", len(o["picks"]) >= 1, outcomes(o))
    add("F_none_outcome_no_logprobs", bool(o["picks"]) and set(outcomes(o)) == {"no-logprobs"}, outcomes(o))
    add("F_none_breaker_no_logprobs_one_request", (breaker_of(o["rec"].get("part_picks")) == "no-logprobs") and len(pick_posts(o["mock"])) == 1
        and sum(1 for r in o["picks"] if r.get("called")) == 1, ((o["rec"].get("part_picks") or {}).get("breaker"), len(pick_posts(o["mock"]))))
    add("F_none_cue_stays_figure_box", all(s and s.get("cue_kind") == "figure_box" for s in o["by_sent"].get(1, [])), [(s or {}).get("cue_kind") for s in o["by_sent"].get(1, [])])
    rq = pick_replies(o["mock"])
    if rq:
        r0 = rq[0]
        add("F_request_body_logprobs_top20_temp0_num_predict1", r0.get("logprobs_req") is True and r0.get("top_logprobs_req") == 20 and r0.get("stream") is False
            and (r0.get("options") or {}).get("temperature") == 0 and (r0.get("options") or {}).get("num_predict") == 1, r0)
        post = [r for r in api_posts(o["mock"])]
        add("F_request_model_and_one_png", bool(post) and post[0].get("model") == VMODEL and post[0].get("n_images") == 1 and len(post[0].get("png_dims") or []) == 1,
            [{k: p.get(k) for k in ("model", "n_images", "png_dims", "keep_alive")} for p in post[:1]])
        dims = [int(x) for x in re.findall(r"\d+", (post[0].get("png_dims") or ["0x0"])[0])] if post else [0, 0]
        add("F_crop_long_side_le_1024", bool(dims) and max(dims) <= 1024, dims)
    for nm, me, want in (("F_http503", {"MOCK_PICK": "status:503"}, {"unavailable"}), ("F_http404", {"MOCK_PICK": "status:404"}, {"unavailable"}),
                         ("F_reset", {"MOCK_PICK": "reset"}, {"unavailable", "error"})):
        o = runF(nm, me)
        add(f"{nm}_outcome", bool(o["picks"]) and set(outcomes(o)) <= want, outcomes(o))
        pk = o["rec"].get("part_picks") or {}
        if nm != "F_reset":
            add(f"{nm}_breaker_unavailable_later_not_called", breaker_of(pk) == "unavailable" and sum(1 for r in o["picks"] if r.get("called")) == 1
                and len(pick_posts(o["mock"])) == 1, (pk.get("breaker"), [(r.get("called"), r.get("outcome")) for r in o["picks"]]))
            add(f"{nm}_ground_errors_class_counted", ((o["rec"].get("ground_errors") or {}).get("count") or 0) >= 1, o["rec"].get("ground_errors"))
    o = runF("F_malformed", {"MOCK_PICK": "malformed"})
    add("F_malformed_outcome_error", bool(o["picks"]) and "error" in outcomes(o) and set(outcomes(o)) <= {"error"}, outcomes(o))
    add("F_malformed_ground_errors", ((o["rec"].get("ground_errors") or {}).get("count") or 0) >= 1, o["rec"].get("ground_errors"))
    long = F_SCRIPT + ["The plant runs every day.", "Operators check it each morning.", "Water quality is logged hourly.", "The report goes out weekly."]
    o = runF("F_delay16", dict(fixed([("A", 0.97), ("B", 0.02)]), MOCK_PICK_DELAY="16"), script=long, stub_play=2.5)
    add("F_delay16_outcome_timeout", bool(o["picks"]) and outcomes(o)[0] == "timeout", outcomes(o))
    add("F_delay16_late_reply_never_drawn", not any(tsrc(s) == "pick" for s in o["shows"]), [tsrc(s) for s in o["shows"] if tsrc(s)])
    add("F_delay16_run_not_delayed", o["wall"] < 2.5 * len(long) + 8, round(o["wall"], 1))
    add("F_delay16_ground_miss_timeout", ((o["rec"].get("ground_miss_reasons") or {}).get("timeout") or 0) >= 1, o["rec"].get("ground_miss_reasons"))
    o = runF("F_accepted", fixed([("A", 0.97), ("B", 0.02)]), stub_play=1.5, env={"SPEAKEASY_KEEP_CAPTURE": "1"}, poll_focus=True)
    acc = [r for r in o["picks"] if r.get("outcome") == "accepted"]
    add("F_accepted_outcome", bool(acc) and acc[0].get("chosen_index") == 0 and isnum(acc[0].get("p_top")) and acc[0]["p_top"] >= 0.97,
        [(r.get("outcome"), r.get("chosen_index"), r.get("p_top")) for r in o["picks"]])
    ap = [r for r in acc if r.get("applied")]
    add("F_accepted_applied_while_current", bool(ap), [(r.get("applied"), r.get("drop")) for r in acc])
    for r in ap[:1]:
        sh = o["cshow"].get(r["sent"]); ch = r["cand_rects"][r["chosen_index"]]
        add("F_accepted_show_part_oval_on_chosen_rect", bool(sh) and sh.get("cue_kind") == "part_oval" and tsrc(sh) == "pick" and target(sh)
            and max(abs(a - b) for a, b in zip(target(sh), ch)) <= 0.15, (sh and (sh.get("cue_kind"), tsrc(sh), target(sh)), ch))
        add("F_accepted_label_grounded_suffix", bool(sh) and str((sh.get("doc") or {}).get("label", "")).endswith(" (grounded)"), sh and (sh.get("doc") or {}).get("label"))
        add("F_accepted_rect_inside_scope", all(within(x, fig(P(FX["callouts"])["d"], 1, "Figure 1")["bbox"], 4.5) for x in r["cand_rects"]), r["cand_rects"][:3])
    add("F_accepted_focus_methods_ground_pick", (o["rec"].get("focus_methods") or {}).get("ground-pick", 0) == len(ap), o["rec"].get("focus_methods"))
    add("F_keep_capture_never_keeps_pick_png", not o["leftovers"] and not glob.glob(os.path.join(o["home"], "**", "*pick*.png"), recursive=True), o["leftovers"])
    o = runF("F_offmenu", fixed([("The", 0.6), ("A", 0.3)]))
    add("F_offmenu_outcome", bool(o["picks"]) and set(outcomes(o)) == {"off-menu"} and all(r.get("chosen_index") is None for r in o["picks"]), [(r.get("outcome"), r.get("letter_mass")) for r in o["picks"]])
    add("F_offmenu_letter_mass_0.3", bool(o["picks"]) and by_submit(o["picks"])[0].get("letter_mass") == 0.3, [r.get("letter_mass") for r in o["picks"]])
    o = runF("F_lowercase", fixed([("a", 0.95), ("B", 0.04)]))
    add("F_lowercase_never_counts_off_menu", bool(o["picks"]) and set(outcomes(o)) == {"off-menu"} and by_submit(o["picks"])[0].get("letter_mass") == 0.04, [(r.get("outcome"), r.get("letter_mass")) for r in o["picks"]])
    o = runF("F_merge", fixed([("B", 0.3), (" B", 0.3), ("A", 0.35)]))
    r = by_submit(o["picks"])[0] if o["picks"] else {}
    add("F_merge_space_letter_argmax_B", r.get("chosen_index") == 1 and r.get("p_top") == 0.63 and r.get("p_margin") == 0.26 and r.get("letter_mass") == 0.95
        and r.get("outcome") in ("accepted", "low-confidence"), {k: r.get(k) for k in ("outcome", "chosen_index", "p_top", "p_margin", "letter_mass")})
    thr = (o["rec"].get("part_picks") or {}).get("threshold")
    if isnum(thr): add("F_merge_outcome_matches_threshold", r.get("outcome") == ("accepted" if 0.6 / 0.95 >= thr else "low-confidence"), (r.get("outcome"), thr))
    info(f"F PICK_MIN_P (runs part_picks.threshold) = {thr}")
    o = runF("F_low", fixed([("A", 0.3), ("B", 0.3), ("C", 0.3)]))
    lc = by_submit(o["picks"])[0] if o["picks"] else {}; sh = o["cshow"].get(lc.get("sent"))
    add("F_low_confidence_keeps_cue", lc.get("outcome") == "low-confidence" and isint(lc.get("chosen_index")) and lc.get("p_top") == 0.33 and bool(sh)
        and sh.get("cue_kind") == "figure_box" and tsrc(sh) != "pick", (lc.get("outcome"), lc.get("p_top"), sh and sh.get("cue_kind")))
    o = runF("F_mass_boundary", fixed([("A", 0.5), ("zz", 0.5)]))
    add("F_letter_mass_exactly_0.5_not_off_menu", bool(o["picks"]) and by_submit(o["picks"])[0].get("outcome") == "accepted" and by_submit(o["picks"])[0].get("letter_mass") == 0.5,
        [(r.get("outcome"), r.get("letter_mass")) for r in o["picks"]])
    o = runF("F_pick_off", fixed([("A", 0.97)]), env={"SPEAKEASY_PICK": "off"})
    add("F_pick_off_no_request", not api_posts(o["mock_all"]) and not any(r.get("called") for r in o["picks"]), len(api_posts(o["mock_all"])))
    o = runF("F_ground_off", fixed([("A", 0.97)]), env={"SPEAKEASY_GROUND": "off"})
    add("F_ground_off_no_request_no_record", not api_posts(o["mock_all"]) and not o["picks"], len(api_posts(o["mock_all"])))
    pk = o["rec"].get("part_picks")
    add("F_ground_off_skipped_disabled", pk is None or (pk.get("skipped") or {}).get("disabled", 0) >= 1, pk)
    script = ["Figure 1 shows the storage tank.", "The blue pump moves the water."]
    e = dict(PICK_ENV); o = speak(FX["single"], 1, "Figure 1:", script, mock_env=fixed([("A", 0.97)]), env=e, tag="F_toofew")
    common_run_checks("F_toofew", o, script)
    pk = o["rec"].get("part_picks") or {}
    add("F_fewer_than_2_candidates_skipped_too_few", not api_posts(o["mock_all"]) and not any(r.get("called") for r in o["picks"]) and (pk.get("skipped") or {}).get("too-few", 0) >= 1, pk)
    script = ["Figure 1 shows the treatment line.", "It is shown here.", "The red valve controls the inflow."]
    o = runF("F_generic", fixed([("A", 0.97)]), script=script)
    gen_sent = [k for k, sid in o["owner"].items() if sid == "s1"]
    add("F_generic_phrase_not_sent", not any(r.get("sent") in gen_sent and r.get("called") for r in o["picks"]), [(r.get("sent"), r.get("outcome")) for r in o["picks"]])
    sk = ((o["rec"].get("part_picks") or {}).get("skipped") or {})
    add("F_generic_phrase_skipped_counted", sk.get("generic-phrase", 0) + sk.get("whole-figure", 0) >= 1, o["rec"].get("part_picks"))
    # OpenRouter (A2.9): the local picker always runs first; OpenRouter only after a local unavailable / no-logprobs, with a key.
    # Network is blocked by a dead HTTPS proxy and the keychain by a stub, so nothing can leave the machine.
    orenv = dict(SPEAKEASY_GROUND="openrouter", SPEAKEASY_PICK="on", SPEAKEASY_OR_KEY="sk-or-test-not-a-key", HTTPS_PROXY="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9",
                 SPEAKEASY_OR_TIMEOUT="2", SPEAKEASY_VISION_MODEL=VMODEL)
    oc = speak(FX["callouts"], 1, "Figure 1:", F_SCRIPT, mock_env=fixed([("A", 0.97), ("B", 0.02)]), env=orenv, tag="F_or_local")
    common_run_checks("F_or_local", oc, F_SCRIPT)
    pk = oc["rec"].get("part_picks") or {}
    add("F_or_local_first_picks_go_to_local", bool(pick_posts(oc["mock"])) and all(r.get("provider") == "local" and r.get("fallback_from") is None for r in oc["picks"]),
        [(r.get("provider"), r.get("outcome")) for r in oc["picks"]])
    add("F_or_local_answers_never_fall_back", not (pk.get("by_provider") or {}).get("openrouter") and not any((pk.get("fallback") or {}).values()), pk)
    add("F_or_retired_bbox_keys_absent", "ground_box_convention" not in oc["rec"] and "ground-vlm" not in (oc["rec"].get("focus_methods") or {}), oc["rec"].get("focus_methods"))
    oc = speak(FX["callouts"], 1, "Figure 1:", F_SCRIPT, mock_env={}, env=orenv, tag="F_or_fallback")
    common_run_checks("F_or_fallback", oc, F_SCRIPT)
    pk = oc["rec"].get("part_picks") or {}; orp = [r for r in by_submit(oc["picks"]) if r.get("provider") == "openrouter"]
    add("F_or_no_logprobs_falls_back_once_then_openrouter", len(pick_posts(oc["mock"])) == 1 and bool(orp) and orp[0].get("fallback_from") == "no-logprobs"
        and all(r.get("fallback_from") is None for r in orp[1:]), [(r.get("provider"), r.get("fallback_from"), r.get("outcome")) for r in by_submit(oc["picks"])])
    add("F_or_fallback_counted", (pk.get("fallback") or {}).get("no-logprobs", 0) >= 1 and (pk.get("by_provider") or {}).get("openrouter", 0) == len(orp), pk)
    add("F_or_openrouter_down_unavailable_breaker", all(r.get("outcome") == "unavailable" for r in orp) and breaker_of(pk, "openrouter") == "unavailable" and breaker_of(pk) == "no-logprobs",
        (pk.get("breaker"), [r.get("outcome") for r in orp]))
    add("F_or_openrouter_failure_keeps_cue", not any(tsrc(sh) == "pick" for sh in oc["shows"]), "")
    oa = speak(FX["callouts"], 1, "Figure 1:", F_SCRIPT, mock_env={}, env=dict(orenv, SPEAKEASY_GROUND="auto"), tag="F_auto_down")
    add("F_auto_local_no_logprobs_never_openrouter", not any(r.get("provider") == "openrouter" for r in oa["picks"]) and len(pick_posts(oa["mock"])) <= 1,
        [(r.get("provider"), r.get("outcome")) for r in oa["picks"]])
    # no doc venv: no request
    o = runF("F_no_doc_venv", fixed([("A", 0.97)]), env={"SPEAKEASY_DOC_PYTHON": os.path.join(TMPROOT, "no-venv", "bin", "python")}, warm=False)
    add("F_no_doc_venv_no_request", not api_posts(o["mock_all"]), len(api_posts(o["mock_all"])))


# ---------------------------------------------------------------- Q: FIFO, one in flight, 15 s deadline
Q_SCRIPT = ["Figure 1 shows the treatment line.", "The red valve controls the inflow.", "The blue pipe carries clean water to the tank.",
            "The red cable powers the small motor.", "The green hatch opens for cleaning.", "The plant runs every day.", "Operators check it each morning."]

@section("Q")
def q_queue():
    e = dict(PICK_ENV)
    o = speak(FX["callouts"], 1, "Figure 1:", Q_SCRIPT, mock_env=dict(fixed([("A", 0.97)]), MOCK_PICK_DELAY="1.2"), env=e, stub_play=1.5, tag="Q_fifo")
    common_run_checks("Q_fifo", o, Q_SCRIPT)
    rq = sorted([r for r in pick_replies(o["mock_all"]) if r["t0"] >= o["t_start"]], key=lambda r: r["t0"])
    add("Q_several_picks", len(rq) >= 3, len(rq))
    overl = [(round(a["t1"] - b["t0"], 2)) for a, b in zip(rq, rq[1:]) if b["t0"] < a["t1"] - 0.05]
    add("Q_one_pick_in_flight", not overl, overl)
    called = sorted([r for r in o["picks"] if r.get("called")], key=lambda r: r["sent"])
    add("Q_requests_equal_called_records", len(rq) == len(called), (len(rq), len(called)))
    qm = [r.get("queue_ms") for r in called]
    add("Q_fifo_queue_grows_in_submit_order", len(qm) >= 3 and all(isint(x) for x in qm) and all(qm[i] <= qm[i + 1] + 300 for i in range(len(qm) - 1)) and qm[-1] >= 800, qm)
    add("Q_no_pick_cancelled_by_later_chunk", all(r.get("outcome") == "accepted" for r in called), [r.get("outcome") for r in called])
    o = speak(FX["callouts"], 1, "Figure 1:", Q_SCRIPT, mock_env=dict(fixed([("A", 0.97)]), MOCK_PICK_DELAY="6"), env=e, stub_play=3.2, tag="Q_deadline")
    common_run_checks("Q_deadline", o, Q_SCRIPT)
    rq = sorted([r for r in pick_posts(o["mock"])], key=lambda r: r["t"])
    for r in rq: r["t0"] = r["t"]
    nrec = len(o["picks"])
    add("Q_deadline_records_for_every_pick", nrec >= 4, [(r.get("sent"), r.get("outcome"), r.get("called"), r.get("queue_ms")) for r in o["picks"]])
    add("Q_deadline_at_most_3_sent", len(rq) <= 3, len(rq))
    add("Q_deadline_no_request_after_15s", bool(rq) and rq[-1]["t0"] - rq[0]["t0"] <= 15.5, [round(r["t0"] - rq[0]["t0"], 1) for r in rq])
    add("Q_deadline_queued_past_deadline_never_sent", any(r.get("outcome") == "timeout" and r.get("called") is False for r in o["picks"]),
        [(r.get("outcome"), r.get("called")) for r in o["picks"]])
    add("Q_deadline_answer_after_15s_is_timeout", sum(1 for r in o["picks"] if r.get("outcome") == "accepted") <= 2, outcomes(o))


# ---------------------------------------------------------------- H: harness self-tests (verifier-owned bench)
def bench(args, py=None, cwd=None, env=None, timeout=3600):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "MOCK_"))}; e.update(env or {})
    e["SPEAKEASY_HOLDOUT"] = A.holdout or NO_HOLDOUT
    t = time.time()
    p = subprocess.run([py or DETPY, os.path.join(cwd or BENCH, "run_bench.py")] + args, capture_output=True, text=True, env=e, timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.time() - t

def tree_src_sha():
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(SRC, "*.py"))): h.update(open(p, "rb").read())
    return h.hexdigest()

def wrapper(kind):
    w = os.path.join(TMPROOT, f"wrap_{kind}.sh")
    if kind == "api":
        body = ('#!/bin/sh\nbase="${SPEAKEASY_LLM_URL%/v1}"\ncurl -s -m 3 -X POST "$base/api/chat" -H "Content-Type: application/json" '
                '-d \'{"model":"x","messages":[{"role":"user","content":"x","images":["AAAA"]}]}\' >/dev/null 2>&1\nexec "CLI" "$@"\n').replace("CLI", CLI)
    else:
        body = ('#!/bin/sh\n"CLI" "$@"; rc=$?\nmkdir -p "$SPEAKEASY_HOME/logs"; echo \'{"state": "part_pick", "sent": 0, "outcome": "no-logprobs"}\' >> "$SPEAKEASY_HOME/logs/focus.jsonl"\nexit $rc\n').replace("CLI", CLI)
    open(w, "w").write(body); os.chmod(w, 0o755); return w

@section("H")
def h_harness():
    p = subprocess.run([sys.executable, os.path.join(BENCH, "test_score.py")], capture_output=True, text=True)
    add("H_test_score_passes", p.returncode == 0, p.stdout[-400:])
    for kind in ("api", "record"):
        rc, out, err, _ = bench(["--set", "public", "--cases", "syn-fig5-01/c01", "--layout-model", MODEL, "--out", os.path.join(TMPROOT, f"h_{kind}.json")],
                                env={"SPEAKEASY_BIN": wrapper(kind)})
        add(f"H_gated_harness_detects_picker_{kind}", rc == 3 and "picker ran in gated mode (case syn-fig5-01/c01)" in err, (rc, err[-300:]))
    rc, out, err, _ = bench(["--set", "public", "--cases", "syn-fig5-01/c01", "--pick", "replay", "--pick-cassette", os.path.join(TMPROOT, "nope.json"), "--out", os.path.join(TMPROOT, "h_nocas.json")])
    add("H_replay_without_cassette_exit2", rc == 2 and "pick cassette not found" in err, (rc, err[-200:]))
    rc, out, err, _ = bench(["--set", "public", "--cases", "syn-fig5-01/c01", "--pick", "proxy", "--pick-upstream", "http://example.com:11434", "--out", os.path.join(TMPROOT, "h_up.json")])
    add("H_proxy_non_loopback_upstream_exit2", rc == 2 and "loopback" in err, (rc, err[-200:]))
    rc, out, err, _ = bench(["--set", "public", "--pick", "replay", "--record-baseline", "--out", os.path.join(TMPROOT, "h_rb.json")])
    add("H_pick_mode_never_records_baseline", rc == 2, (rc, err[-200:]))
    empty = os.path.join(TMPROOT, "empty_cassette.json"); json.dump({"header": {"src_sha": tree_src_sha()}, "entries": {}}, open(empty, "w"))
    wrong = os.path.join(TMPROOT, "wrong_sha_cassette.json"); json.dump({"header": {"src_sha": "0" * 64}, "entries": {}}, open(wrong, "w"))
    rc, out, err, _ = bench(["--set", "public", "--cases", "syn-fig5-01/c01", "--pick", "replay", "--pick-cassette", wrong, "--out", os.path.join(TMPROOT, "h_wrong.json")])
    add("H_replay_refuses_cassette_with_other_src_sha", rc == 2 and "cassette not current (src changed after recording)" in err, (rc, err[-200:]))
    out_p = os.path.join(TMPROOT, "h_stale.json")
    rc, out, err, _ = bench(["--set", "public", "--cases", "arx-2406.06619/c05", "--pick", "replay", "--pick-cassette", empty, "--layout-model", MODEL, "--out", out_p])
    try: pk = json.load(open(out_p))["sets"]["public"]["pick"]
    except Exception: pk = None
    if pk and pk.get("called"):
        add("H_stale_cassette_exit6_and_marked", rc == 6 and pk.get("stale") is True and pk.get("replay_miss", 0) > 0 and "pick cassette stale: re-record with tools/pick_eval.py --record" in err, (rc, pk and pk.get("replay_miss")))
    else: skip("H_stale_cassette_exit6_and_marked", f"no pick was sent on arx-2406.06619/c05 (rc {rc}, pick {pk and pk.get('requests')})")
    gp = subprocess.run([sys.executable, os.path.join(BENCH, "bench_gate.py"), out_p], capture_output=True, text=True)
    add("H_gate_refuses_pick_bench", gp.returncode == 2, gp.stdout[-200:])


# ---------------------------------------------------------------- G / N: gated benchmark
def load(p):
    try: return json.load(open(p))
    except Exception: return None
KEPT = {}
def kept_root(err):
    m = re.findall(r"temp root kept at (\S+)", err); return m[0] if m else None
def case_dirs(err, root):
    """{case_id: measured focus.jsonl} for a kept public bench root, from the harness's own log lines."""
    idx = {}
    for m in re.finditer(r"bench: public case\s+(\d+) (\S+) lines", err): idx[int(m.group(1))] = m.group(2)
    out = {}
    for d in sorted(glob.glob(os.path.join(root, "runs", "*-case"))):
        r = jl(os.path.join(d, "home", "logs", "runs.jsonl"))
        if r and str(r[0].get("run_id", "")).startswith("bench-"):
            i = int(r[0]["run_id"].split("-")[1])
            if i in idx: out[idx[i]] = jl(os.path.join(d, "home", "logs", "focus.jsonl"))
    return out
def sentence_targets(focus_by_case):
    """{(case, sentence_id): [target_src of every part_oval show]} for public parts sentences."""
    out = {}
    for d in sorted(glob.glob(os.path.join(PUB, "*", "cases.json"))):
        for case in json.load(open(d)):
            f = focus_by_case.get(case["case_id"])
            if f is None: continue
            maps = [r for r in f if r.get("state") == "map"]; cs = S.chunk_shows([r for r in f if r.get("state") == "show"])
            try: owner = S.attribute([(s["sentence_id"], s["text"]) for s in case["script"]], maps)
            except Exception: continue
            for k, sid in owner.items():
                sh = cs.get(k)
                if sh and sh.get("cue_kind") == "part_oval": out.setdefault(f"{case['case_id']}|{sid}", []).append(tsrc(sh))
    return out

def b5_public_targets():
    p = os.path.join(B5, "public_targets.json")
    if os.path.exists(p): return json.load(open(p))
    rc, out, err, _ = bench(["--set", "public", "--layout-model", MODEL, "--keep-temp", "--out", os.path.join(TMPROOT, "b5_keep.json")], cwd=os.path.join(BASE, "tests", "audit", "bench"))
    root = kept_root(err)
    t = sentence_targets(case_dirs(err, root)) if root else {}
    if rc == 0 and t: json.dump(t, open(p, "w"), indent=1, sort_keys=True)
    for r in set(re.findall(r"temp root kept at (\S+)", err)): shutil.rmtree(r, ignore_errors=True)
    return t

@section("G")
def g_bench():
    if A.skip_bench: skip("G_bench", "--skip-bench"); return
    out = os.path.join(TMPROOT, "bench_all.json"); lc = os.path.join(TMPROOT, "hidden_last_cases.json")
    sets = ("public", "hidden") if A.holdout else ("public",)
    hargs = ["--set", "all", "--last-cases-out", lc, "--holdout", A.holdout] if A.holdout else ["--set", "public"]
    rc, so, err, secs = bench(hargs + ["--layout-model", MODEL, "--check-determinism", "--keep-temp", "--out", out])
    info(f"G bench --set {'all' if A.holdout else 'public'} --check-determinism exit {rc} in {secs:.0f}s")
    for l in so.splitlines(): info("G " + l)
    add("G_bench_exit0", rc == 0, (rc, err[-400:]))
    add("G_no_picker_in_gated_mode", "picker ran in gated mode" not in err, re.findall(r"picker ran in gated mode \([^)]*\)", err)[:2])
    add("G_check_determinism_ok", rc != 4 and ("determinism ok" in err or rc != 0), re.findall(r"determinism: differs at \S+", err)[:5])
    b = load(out)
    if not b: add("G_bench_json", False, "missing"); return
    gargs = ["--hidden-baseline", os.path.join(B5, "hidden_baseline.json")] if A.holdout else ["--hidden-baseline", os.path.join(NO_HOLDOUT, "baseline.json"), "--public-only"]
    gp = subprocess.run([sys.executable, os.path.join(BENCH, "bench_gate.py"), out, "--baseline", os.path.join(B5, "public_baseline.json"),
                         "--parts-min", "70"] + gargs, capture_output=True, text=True)
    for l in gp.stdout.splitlines():
        if not re.match(r"^(public|hidden) (boxes|lines|parts)\.by_", l): info("G gate: " + l)
    add("G_gate_pass_vs_B5_with_parts_floor_70", gp.returncode == 0, gp.stdout.splitlines()[-1:] if gp.stdout else gp.stderr[-200:])
    for s, need in (("public", 46), ("hidden", 36))[:len(sets)]:
        pt = (b["sets"].get(s) or {}).get("parts") or {}
        add(f"G_{s}_parts_ge_70", isnum(pt.get("score")) and pt["score"] >= 70.0 and pt.get("hits", 0) >= need, (pt.get("score"), pt.get("hits"), pt.get("n")))
        sp = ((b["sets"].get(s) or {}).get("speed") or {}).get("first_highlight_ms") or {}
        add(f"G_{s}_first_highlight_p50_p95_lt_300", all(isnum(sp.get(q)) and sp[q] < 300 for q in ("p50", "p95")), sp)
        iv = (b["sets"].get(s) or {}).get("invariants") or {}
        add(f"G_{s}_hard_invariants_0", bool(iv) and all(iv.get(k) == 0 for k in ("stand_in", "hairline_draw", "callout_oversize")), iv)
        wt = ((b["sets"].get(s) or {}).get("boxes") or {}).get("wrong_type")
        add(f"G_{s}_boxes_wrong_type_reported_per_kind", isinstance(wt, dict) and set(wt) == set(S.SCORED_KINDS), wt)
        info(f"G {s} parts {pt.get('score')} ({pt.get('hits')}/{pt.get('n')}) precision {pt.get('precision')} by_style {json.dumps({k: (v['hits'], v['n']) for k, v in (pt.get('by_style') or {}).items()})} miss {json.dumps(pt.get('miss_reasons'))}")
    # public: no lost hit, no new stray ovals (vs B5 run with the same harness)
    b5 = load(os.path.join(B5, "bench_all_b5.json" if A.holdout else "bench_public_b5.json"))
    cur_cases = {c["case_id"]: c for c in b["sets"]["public"].get("cases") or []}
    base_cases = {c["case_id"]: c for c in (b5 or {}).get("sets", {}).get("public", {}).get("cases") or []}
    lost, strays = [], []
    for cid, bc in base_cases.items():
        cc = cur_cases.get(cid)
        if cc is None: lost.append((cid, "case missing")); continue
        bm = {m["sentence_id"] for m in bc["misses"] if m["category"] == "parts"}; cm = {m["sentence_id"]: m["reason"] for m in cc["misses"] if m["category"] == "parts"}
        lost += [(cid, sid, cm[sid]) for sid in cm if sid not in bm]
        bs = bc["parts"]["ovals"] - bc["parts"].get("good", 0); cs = cc["parts"]["ovals"] - cc["parts"].get("good", 0)
        if cs > bs: strays.append((cid, bs, cs))
    add("G_public_no_lost_parts_hit_vs_B5", bool(base_cases) and not lost, lost[:6])
    add("G_public_no_new_stray_ovals_vs_B5", bool(base_cases) and not strays, strays[:6])
    lostl = []
    for cid, bc in base_cases.items():
        cc = cur_cases.get(cid) or {"misses": []}
        bm = {m["sentence_id"] for m in bc["misses"] if m["category"] == "lines"}
        lostl += [(cid, m["sentence_id"]) for m in cc["misses"] if m["category"] == "lines" and m["sentence_id"] not in bm]
    add("G_public_no_lost_lines_hit_vs_B5", not lostl, lostl[:5])
    root = kept_root(err)
    if root:
        cur_t = sentence_targets(case_dirs(err, root)); b5t = b5_public_targets()
        parts_ids = {json.load(open(d))["doc_id"]: {s["id"] for s in json.load(open(d))["sentences"] if s["score"] == "parts"} for d in glob.glob(os.path.join(PUB, "*", "gt.json"))}
        b5hit = [k for k in sorted(b5t) if k.split("|")[1] in parts_ids.get(k.split("/")[0], set())
                 and not any(m["sentence_id"] == k.split("|")[1] and m["category"] == "parts" for m in base_cases.get(k.split("|")[0], {}).get("misses", []))]
        diff = [(k, b5t[k], cur_t.get(k)) for k in b5hit if sorted(set(cur_t.get(k) or [])) != sorted(set(b5t[k]))]
        add("G_public_B5_hits_keep_target_src", bool(b5hit) and not diff, diff[:5])
        info(f"G B5 public parts hits compared for target_src: {len(b5hit)}")
    else: skip("G_public_B5_hits_keep_target_src", "no kept temp root")
    for r in set(re.findall(r"temp root kept at (\S+)", err)): shutil.rmtree(r, ignore_errors=True)
    KEPT["bench"] = b
    if not A.holdout: return
    # hidden aggregates
    hb = load(os.path.join(B5, "bench_all_b5.json"))["sets"]["hidden"]["parts"]; hc = b["sets"].get("hidden", {}).get("parts") or {}
    add("G_hidden_parts_hits_not_lower", hc.get("hits", -1) >= hb["hits"], (hb["hits"], hc.get("hits")))
    fell = [(k, hb["by_style"][k]["hits"], (hc.get("by_style") or {}).get(k, {}).get("hits")) for k in hb["by_style"] if (hc.get("by_style") or {}).get(k, {}).get("hits", -1) < hb["by_style"][k]["hits"]]
    add("G_hidden_by_style_hits_not_lower", not fell, fell)
    hl = load(lc); h5 = load(os.path.join(B5, "hidden_cases_b5.json"))
    if hl and h5:
        b5m = {(c["case_id"], m["sentence_id"]) for c in h5["cases"] for m in c["misses"] if m["category"] == "parts"}
        nl = sum(1 for c in hl["cases"] for m in c["misses"] if m["category"] == "parts" and (c["case_id"], m["sentence_id"]) not in b5m)
        st = sum(1 for c in hl["cases"] for c5 in h5["cases"] if c5["case_id"] == c["case_id"] and c["parts"]["ovals"] - c["parts"].get("good", 0) > c5["parts"]["ovals"] - c5["parts"].get("good", 0))
        add("G_hidden_no_lost_parts_hit_vs_B5", nl == 0, f"{nl} lost (count only)"); add("G_hidden_no_new_stray_ovals_vs_B5", st == 0, f"{st} cases (count only)")
    KEPT["bench"] = b

@section("N")
def n_nomodel():
    if A.skip_bench: skip("N_bench", "--skip-bench"); return
    out = os.path.join(TMPROOT, "bench_nomodel.json")
    rc, so, err, secs = bench(["--set", "public", "--out", out])
    for l in so.splitlines(): info("N " + l)
    b = load(out); pt = ((b or {}).get("sets", {}).get("public") or {}).get("parts") or {}
    add("N_no_model_bench_exit0", rc == 0, (rc, err[-300:]))
    add("N_no_model_public_parts_ge_63.08", isnum(pt.get("score")) and pt["score"] >= 63.08, (pt.get("score"), pt.get("hits"), pt.get("n")))
    b5 = load(os.path.join(B5, "bench_public_nomodel_b5.json"))
    if b5: info(f"N B5 no-model public parts {b5['sets']['public']['parts']['score']} -> {pt.get('score')}")


# ---------------------------------------------------------------- P: pick replay
def ollama_ok():
    import urllib.request
    try:
        tags = json.loads(urllib.request.urlopen(A.ollama.rstrip("/") + "/api/tags", timeout=3).read())
        return any(m.get("name") == VMODEL for m in tags.get("models", []))
    except Exception: return False

def cassette_clean(path):
    d = load(path)
    if not isinstance(d, dict): return False, "unreadable"
    hdr = d.get("header") or {}; ent = d.get("entries") or {}
    need = {"model", "model_digest", "ollama_version", "pypdfium2", "pillow", "commit", "recorded_at"}
    bad = [k for k in ent if not re.fullmatch(r"[0-9a-f]{64}", k)]
    for v in ent.values():
        if set(v) != {"top_logprobs"}: bad.append(sorted(v)); continue
        toks = [e.get("token") for e in v["top_logprobs"]]
        if any(not (isinstance(t, str) and (re.fullmatch(r"\s*[A-P]\s*", t) or t == "<other>")) for t in toks) or toks.count("<other>") != 1: bad.append(toks[:4])
    return (need <= set(hdr) and not bad and bool(ent)), {"missing_header": sorted(need - set(hdr)), "bad": bad[:3], "entries": len(ent)}

_DEFAULT_ON = {}
def picker_default_on():
    """Whether a pick is sent with SPEAKEASY_PICK unset (probe once per suite run)."""
    if "v" not in _DEFAULT_ON:
        o = speak(FX["callouts"], 1, "Figure 1:", F_SCRIPT, mock_env=fixed([("A", 0.97)]), env={"SPEAKEASY_GROUND": "local", "SPEAKEASY_VISION_MODEL": VMODEL}, tag="P_default")
        _DEFAULT_ON["v"] = bool(pick_posts(o["mock"])) or any(r.get("called") for r in o["picks"]); _DEFAULT_ON["o"] = o
    return _DEFAULT_ON["v"]

@section("P")
def p_pick():
    if A.skip_bench: skip("P_bench", "--skip-bench"); return
    pub = os.path.join(BENCH, "pick_cassette.json"); hid = os.path.join(A.holdout or NO_HOLDOUT, "pick_cassette.json"); tree = tree_src_sha()
    if not os.path.exists(pub): add("P_public_cassette_committed", False, "tests/audit/bench/pick_cassette.json missing (A4.2)"); return
    ok, det = cassette_clean(pub); add("P_public_cassette_hashes_and_letter_logprobs_only", ok, det)
    shas = {k: ((load(c) or {}).get("header") or {}).get("src_sha") for k, c in (("public", pub), ("hidden", hid)) if os.path.exists(c)}
    info(f"P tree src_sha {tree[:12]}; cassette src_sha " + json.dumps({k: (v or "none")[:12] for k, v in shas.items()}))
    if any(v != tree for v in shas.values()):
        skip("P_replay_determinism_3_passes", "cassette not current (src changed after recording): hand-back (A4.2)"); return
    add("P_both_cassettes_src_sha_match_tree" if A.holdout else "P_public_cassette_src_sha_matches_tree",
        set(shas) == ({"public", "hidden"} if A.holdout else {"public"}) and all(v == tree for v in shas.values()), shas)
    blocks = []
    for i in range(3):
        out = os.path.join(TMPROOT, f"bench_pick_public_{i}.json")
        rc, so, err, secs = bench(["--set", "public", "--pick", "replay", "--layout-model", MODEL, "--out", out])
        pk = (((load(out) or {}).get("sets") or {}).get("public") or {}).get("pick") or {}
        info(f"P replay pass {i + 1}: exit {rc} in {secs:.0f}s, requests {pk.get('requests')} called {pk.get('called')} accepted {pk.get('accepted')} "
             f"replay_miss {pk.get('replay_miss')} (warm-up {pk.get('replay_miss_warmup')}) by_outcome {json.dumps(pk.get('by_outcome'))}")
        add(f"P_public_replay_pass{i + 1}_exit0_miss_0", rc == 0 and pk.get("replay_miss") == 0, (rc, pk.get("replay_miss"), err[-200:]))
        blocks.append({k: v for k, v in pk.items() if k not in ("ms", "replay_miss_warmup")})
    diff = [S.diff_paths(blocks[0], x) for x in blocks[1:]]
    add("P_public_replay_3_passes_identical_pick_blocks", all(not d for d in diff), diff)
    pk = blocks[0]
    info(f"P public pick block {json.dumps(pk)}")
    gb = KEPT.get("bench") or load(os.path.join(TMPROOT, "bench_all.json"))
    gpp = ((gb or {}).get("sets", {}).get("public") or {}).get("parts", {}).get("precision")
    pwp = (pk.get("parts_with_pick") or {}).get("precision")
    if not isnum(gpp): skip("P_parts_with_pick_precision_ge_gated_minus_1", "no gated bench in this run")
    elif picker_default_on():
        add("P_parts_with_pick_precision_ge_gated_minus_1", isnum(pwp) and pwp >= gpp - 1, (pwp, gpp))
    else:   # A6.3: report-only while the picker is default-off (Amendment 5)
        info(f"P parts_with_pick.precision {pwp} vs gated parts.precision {gpp} (guard {gpp - 1:.2f}): report-only, picker default-off (A6.3)"
             f"{'' if isnum(pwp) and pwp >= gpp - 1 else ' - below the guard'}")
    gp = subprocess.run([sys.executable, os.path.join(BENCH, "bench_gate.py"), os.path.join(TMPROOT, "bench_pick_public_0.json")], capture_output=True, text=True)
    add("P_gate_refuses_pick_bench", gp.returncode == 2, gp.stdout[-200:])
    hprec = None
    if os.path.exists(hid):
        hb = []
        for i in range(2):
            out = os.path.join(TMPROOT, f"bench_pick_hidden_{i}.json")
            rc, so, err, secs = bench(["--set", "hidden", "--pick", "replay", "--layout-model", MODEL, "--out", out, "--last-cases-out", os.path.join(TMPROOT, "hpick_cases.json"), "--holdout", A.holdout])
            pk = (((load(out) or {}).get("sets") or {}).get("hidden") or {}).get("pick") or {}
            add(f"P_hidden_replay_pass{i + 1}_exit0_miss_0", rc == 0 and pk.get("replay_miss") == 0, (rc, pk.get("replay_miss")))
            hb.append({k: v for k, v in pk.items() if k not in ("ms", "replay_miss_warmup")})
        add("P_hidden_replay_2_passes_identical", hb[0] == hb[1], S.diff_paths(hb[0], hb[1])[:5])
        pk = hb[0]; hprec = pk.get("precision")
        info(f"P hidden pick (aggregates) requests {pk.get('requests')} accepted {pk.get('accepted')} precision {hprec} recall {pk.get('recall')} candidate_recall {pk.get('candidate_recall')} "
             f"parts_with_pick {json.dumps(pk.get('parts_with_pick'))} by_outcome {json.dumps(pk.get('by_outcome'))}")
    elif A.holdout: skip("P_hidden_replay", "<holdout>/pick_cassette.json not recorded")
    # default-off is final for Phase 5 (Amendment 1 item 4, A5.1): with SPEAKEASY_PICK unset no pick is sent
    picker_default_on(); o = _DEFAULT_ON["o"]
    add("P_picker_default_off_A5.1", not pick_posts(o["mock"]) and not any(r.get("called") for r in o["picks"]), (len(pick_posts(o["mock"])), ((o["rec"].get("part_picks") or {}).get("skipped"))))
    # own record + replay determinism against the real local model (loopback only)
    if not ollama_ok(): skip("P_own_record_replay_deterministic", f"{VMODEL} not available at {A.ollama}"); return
    cas = os.path.join(TMPROOT, "own_cassette.json"); sub = "syn-panels-01/*"
    rc, so, err, _ = bench(["--set", "public", "--cases", sub, "--pick", "proxy", "--pick-upstream", A.ollama, "--pick-cassette", cas, "--layout-model", MODEL,
                            "--out", os.path.join(TMPROOT, "own_proxy.json")])
    pkp = ((load(os.path.join(TMPROOT, "own_proxy.json")) or {}).get("sets", {}).get("public") or {}).get("pick") or {}
    if not pkp.get("called"): skip("P_own_record_replay_deterministic", f"no pick called on {sub} (rc {rc}, requests {pkp.get('requests')})"); return
    info("P own cassette written: " + str("cassette written" in err))
    ok, det = cassette_clean(cas) if os.path.exists(cas) else (False, "proxy wrote no cassette (partial run?)")
    add("P_own_cassette_letter_logprobs_only", ok, det)
    rc, so, err, _ = bench(["--set", "public", "--cases", sub, "--pick", "replay", "--check-determinism", "--pick-cassette", cas, "--layout-model", MODEL,
                            "--out", os.path.join(TMPROOT, "own_replay.json")])
    pkr = ((load(os.path.join(TMPROOT, "own_replay.json")) or {}).get("sets", {}).get("public") or {}).get("pick") or {}
    add("P_own_record_replay_deterministic", rc == 0 and pkr.get("replay_miss") == 0, (rc, pkr.get("replay_miss"), err[-300:]))
    same = {k: pkp.get(k) for k in ("requests", "by_outcome", "accepted", "precision")} == {k: pkr.get(k) for k in ("requests", "by_outcome", "accepted", "precision")}
    add("P_proxy_and_replay_same_decisions", same, ({k: pkp.get(k) for k in ("requests", "by_outcome", "precision")}, {k: pkr.get(k) for k in ("requests", "by_outcome", "precision")}))


# ---------------------------------------------------------------- A: Read-area runs (A2.1-A2.3, A2.8, A2.10, A2.12)
AREA_CASES = [   # (name, page, area key, script, verbatim sentence indexes, fit expected)
    ("A_para_above_and_fig", 1, "para_above_and_fig", ["The plant was rebuilt over one summer.", "Its new line pushes raw water through four basins in a row.",
      "The drawing shows the four basins.", "Water flows from left to right."], [1], True),
    ("A_prose_fig_caption", 2, "prose_fig_caption", ["The third plant uses ozone and carbon.", "The ozone reactor follows the raw water tank.",
      "Each stage has a probe under it.", "Its control room watches every stage from one large screen."], [3], True),
    ("A_prose_fig_para_footer", 2, "prose_fig_para", ["River water is cleaned in four steps.", "The carbon filter comes after the reactor.",
      "Staff swap the carbon beds once a year in spring.", "The last step adds chlorine."], [2], True),
    ("A_fig_caption_para_labels_first", 1, "fig_caption_para", ["The drawing traces the water from the first basin to the last.", "Water enters at the inlet basin on the left.",
      "Then it is stirred in the mixer.", "The mixer runs at a slower speed than in the first plant.", "Clean water leaves through the outlet."], [3], False),
    ("A_labels_mostly", 2, "labels_mostly", ["The carbon filter sits between the reactor and the dosing stage.", "The raw water tank is first.", "Every stage has its own probe."], [], False),
]

def scr_rect(sh):
    r = sh.get("rect"); return [r[0], r[1], r[0] + r[2], r[1] + r[3]] if isinstance(r, list) and len(r) == 4 else None

@section("A")
def a_area():
    for name, pg, key, script, verbatim, fit in AREA_CASES:
        ar = (GT["area"]["areas"] if pg == 1 else GT["area"]["p2"]["areas"])[key]
        o = speak(FX["area"], pg, None, script, area=ar, env=dict(PICK_ENV), mock_env=fixed([("A", 0.4), ("B", 0.3)]), stub_play=0.3, tag=name)
        common_run_checks(name, o, script, area_run=True)
        rec = o.get("rec") or {}
        if not fit:
            info(f"A {name}: area_pdf {rec.get('area_pdf')} (the OCR lines start with figure labels; the area fit anchors on leading prose lines, as at B5); checking the A2.1 rule for its outcome")
            if rec.get("area_pdf") == "ok": add(f"{name}_figure_mode_from_area_A2.2", rec.get("figure_mode_src") in ("area-overlap", "area-labels"), rec.get("figure_mode_src"))
            add(f"{name}_no_fit_region_capture_fit_or_none", rec.get("area_region_src") in ("capture-fit", "none") and ("area_page_rect" not in rec or rec.get("area_region_src") == "capture-fit"), (rec.get("area_region_src"), rec.get("area_page_rect")))
            inv = invariant_counts(o, area_rect=ar); add(f"{name}_inv_area_leak_0", inv["area_leak"] == 0, inv); add(f"{name}_inv_stand_in_0", inv["stand_in"] == 0, inv)
            continue
        add(f"{name}_capture_method_area_pdf_ok", rec.get("capture_method") == "area" and rec.get("area_pdf") == "ok", (rec.get("capture_method"), rec.get("area_pdf"), rec.get("area_pdf_error")))
        apr = rec.get("area_page_rect")
        add(f"{name}_area_page_rect_is_the_area_A2.1", rec.get("area_region_src") == "area-fit" and isrect(apr) and max(abs(a - b) for a, b in zip(apr, ar)) <= 4.0
            and all(abs(round(v, 1) - v) < 1e-9 for v in apr), (rec.get("area_region_src"), apr, ar))
        add(f"{name}_figure_mode_from_area_A2.2", rec.get("doc_figure_mode") is True and rec.get("figure_mode_src") in ("area-overlap", "area-labels"), (rec.get("doc_figure_mode"), rec.get("figure_mode_src")))
        inv = invariant_counts(o, area_rect=ar)
        add(f"{name}_inv_area_leak_0", inv["area_leak"] == 0, inv); add(f"{name}_inv_stand_in_0", inv["stand_in"] == 0, inv)
        capr = [float(x) for x in area_geometry(FX["area"], pg, ar)[0].split(",")]; capr = [capr[0], capr[1], capr[0] + capr[2], capr[1] + capr[3]]
        add(f"{name}_no_show_is_the_capture_rect", not any(scr_rect(sh) and iou(scr_rect(sh), capr) >= 0.9 for sh in o["shows"]), "")
        para = [i for i in range(len(script)) if i not in verbatim]
        bands = [(i, (s_ or {}).get("cue_kind")) for i in para for s_ in o["by_sent"].get(i, []) if s_ and s_.get("cue_kind") == "text_band"]
        add(f"{name}_paraphrased_chunks_no_text_band", not bands, bands[:4])
        for i in verbatim:
            add(f"{name}_A6.2_verbatim_prose_chunk_gets_band_s{i}", bool(o["by_sent"].get(i)) and all(s_ and s_.get("cue_kind") == "text_band" for s_ in o["by_sent"][i]),
                [(s_ or {}).get("cue_kind") for s_ in o["by_sent"].get(i, [])])
        for k in ("stand_in_blocked", "no_target_chunks", "area_dropped"):
            if k in rec: add(f"{name}_{k}_int", isint(rec[k]) and rec[k] >= 0, rec[k])
        info(f"A {name}: figure_mode_src {rec.get('figure_mode_src')} area_page_rect {apr} stand_in_blocked {rec.get('stand_in_blocked')} no_target_chunks {rec.get('no_target_chunks')} "
             f"cues {[(i, [(s_ or {}).get('cue_kind') for s_ in o['by_sent'].get(i, [])]) for i in range(len(script))]}")
    # A2.8: area_pdf says why no page matched (no text in the value)
    ar = GT["area"]["areas"]["para_above_and_fig"]; script = ["The drawing traces the water from the first basin to the last.", "Clean water leaves through the outlet."]
    slow = os.path.join(TMPROOT, "slow_py.sh"); open(slow, "w").write(f'#!/bin/sh\nsleep 9\nexec "{DETPY}" "$@"\n'); os.chmod(slow, 0o755)
    for nm, want, kw in (("doc_unresolved", "doc-unresolved", {"hint_doc": os.path.join(TMPROOT, "missing", "nowhere.pdf")}),
                         ("markdown", "markdown", {"hint_doc": os.path.join(FXD, "notes.md")}),
                         ("no_page", "no-page", {"hint_doc": FX["single"]}),
                         ("doc_timeout", "doc-timeout", {"env": {"SPEAKEASY_DOC_PYTHON": slow}, "warm": False})):
        o = speak(FX["area"], 1, None, script, area=ar, tag="A8_" + nm, **kw)
        common_run_checks(f"A8_{nm}", o, script, area_run=True)
        rec = o.get("rec") or {}
        add(f"A8_{nm}_area_pdf_{want}", rec.get("area_pdf") == want, (rec.get("area_pdf"), rec.get("area_pdf_error")))
        add(f"A8_{nm}_area_pdf_error_class_only", "area_pdf_error" not in rec or rec["area_pdf_error"] is None or bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", str(rec["area_pdf_error"]))), rec.get("area_pdf_error"))
        add(f"A8_{nm}_no_retired_area_values", rec.get("area_pdf") not in ("no-doc",) and not str(rec.get("area_pdf", "")).startswith("error:"), rec.get("area_pdf"))
        capr = [float(x) for x in area_geometry(FX["area"], 1, ar)[0].split(",")]; capr = [capr[0], capr[1], capr[0] + capr[2], capr[1] + capr[3]]
        add(f"A8_{nm}_no_stand_in_capture_rect", not any(scr_rect(sh) and iou(scr_rect(sh), capr) >= 0.9 for sh in o["shows"]), [scr_rect(sh) for sh in o["shows"]][:2])


# ---------------------------------------------------------------- D: degenerate (hairline) figure boxes (A2.4)
@section("D")
def d_hairline():
    script = ["Figure 1 lists the three shifts of the plant.", "The night shift also runs the cleaning cycle."]
    for nm, model in (("D_with_model", MODEL), ("D_no_model", os.path.join(TMPROOT, "no-model", "pp_doclayout_s.onnx"))):
        d = parse(FX["hairline"], model=model)["d"]; f = fig(d, 1, "Figure 1") or {}
        bb = f.get("bbox") or [0, 0, 0, 0]; thin = (bb[2] - bb[0] < 10 or bb[3] - bb[1] < 10)
        add(f"{nm}_parse_marks_hairline", (f.get("degenerate") is True and thin) or (f.get("degenerate") is False and f.get("bbox_src") == "detector-substitute" and not thin),
            (f.get("bbox"), f.get("bbox_src"), f.get("degenerate")))
        o = speak(FX["hairline"], 1, "Figure 1:", script, env=dict(PICK_ENV, SPEAKEASY_LAYOUT_MODEL=model), mock_env=fixed([("A", 0.99)]), tag=nm)
        common_run_checks(nm, o, script)
        rec = o.get("rec") or {}; pk = rec.get("part_picks") or {}
        add(f"{nm}_no_hairline_figure_box", not any(sh.get("cue_kind") == "figure_box" and isrect(target(sh)) and (target(sh)[2] - target(sh)[0] < 10 or target(sh)[3] - target(sh)[1] < 10) for sh in o["shows"]),
            [target(sh) for sh in o["shows"] if sh.get("cue_kind") == "figure_box"][:3])
        if f.get("degenerate"):
            add(f"{nm}_figures_degenerate_counted", isint(rec.get("figures_degenerate")) and rec["figures_degenerate"] >= 1, rec.get("figures_degenerate"))
            add(f"{nm}_no_pick_on_degenerate_figure", not pick_posts(o["mock"]) and not any(r.get("called") for r in o["picks"]), (len(pick_posts(o["mock"])), pk.get("skipped")))
            add(f"{nm}_chunks_fall_back_to_text_cues", not any(sh.get("cue_kind") in ("figure_box", "part_oval") for sh in o["shows"]), [sh.get("cue_kind") for sh in o["shows"]])


# ---------------------------------------------------------------- W: whole-figure sentences (A2.5)
@section("W")
def w_whole():
    # A6.1 / A6.1b: whole-figure only when no content word remains after removing the figure reference, the generic figure words
    # (with inflections), stopwords and non-callout numbers; any remaining concrete word names an object (label, else callout,
    # else picker-eligible). Labels of Figure 1: Intake, Sand filter, Clear well.
    whole = ["Figure 1 shows the overall setup.", "This diagram compares the two approaches.", "Fig. 1 summarises the results."]
    obj = {"pick": "The red valve controls the inflow.", "label": "The Intake receives the river water.", "callout": "Callout 3 is the probe."}
    script = [whole[0], obj["pick"], whole[1], obj["label"], whole[2], obj["callout"]]
    W_IDX, PICK_IDX, LABEL_IDX, CALLOUT_IDX = (0, 2, 4), 1, 3, 5
    o = speak(FX["callouts"], 1, "Figure 1:", script, env=dict(PICK_ENV), mock_env=fixed([("A", 0.99)]), stub_play=1.2, tag="W_whole")
    common_run_checks("W_whole", o, script)
    fb = (fig(P(FX["callouts"])["d"], 1, "Figure 1") or {}).get("bbox")
    def ks(i): return [k for k, sid in o["owner"].items() if sid == f"s{i}"]
    for i in W_IDX:
        sh = o["by_sent"].get(i, [])
        tag = "_A6.1_overall_setup" if i == 0 else ""
        add(f"W_s{i}_whole_figure_keeps_figure_box{tag}", bool(sh) and all(x and x.get("cue_kind") == "figure_box" and isrect(target(x)) and iou(target(x), fb) >= 0.9 for x in sh),
            [((x or {}).get("cue_kind"), target(x or {})) for x in sh])
        add(f"W_s{i}_whole_figure_no_pick{tag}", not any(r.get("sent") in ks(i) for r in o["picks"]), [(r.get("sent"), r.get("outcome")) for r in o["picks"]])
        add(f"W_s{i}_whole_figure_no_oval_ever{tag}", not any(x.get("sent") in ks(i) and x.get("cue_kind") == "part_oval" for x in o["shows"]), "")
    sk = ((o["rec"].get("part_picks") or {}).get("skipped") or {})
    add("W_skipped_whole_figure_counted_3", sk.get("whole-figure", 0) >= 3, sk)
    add("W_object_sentence_still_picks_A6.1b", any(r.get("sent") in ks(PICK_IDX) and r.get("called") for r in o["picks"]), [(r.get("sent"), r.get("outcome")) for r in o["picks"]])
    add("W_label_sentence_label_oval_no_pick", any(tsrc(x) in ("label-object", "label-pad") for x in o["by_sent"].get(LABEL_IDX, []) if x)
        and not any(r.get("sent") in ks(LABEL_IDX) and r.get("called") for r in o["picks"]), [(tsrc(x or {}), (x or {}).get("cue_kind")) for x in o["by_sent"].get(LABEL_IDX, [])])
    add("W_callout_sentence_callout_oval_no_pick", bool(o["by_sent"].get(CALLOUT_IDX)) and all(x and tsrc(x) == "callout" for x in o["by_sent"][CALLOUT_IDX])
        and not any(r.get("sent") in ks(CALLOUT_IDX) for r in o["picks"]), [(tsrc(x or {}), (x or {}).get("cue_kind")) for x in o["by_sent"].get(CALLOUT_IDX, [])])
    # the same whole-figure sentence stays whole-figure with the picker off (no request, figure box)
    o2 = speak(FX["callouts"], 1, "Figure 1:", [whole[0], obj["pick"]], env=dict(PICK_ENV, SPEAKEASY_PICK="off"), mock_env=fixed([("A", 0.99)]), tag="W_pick_off")
    add("W_picker_off_no_request_overall_setup_figure_box", not pick_posts(o2["mock"]) and all(x and x.get("cue_kind") == "figure_box" for x in o2["by_sent"].get(0, [])) and bool(o2["by_sent"].get(0)),
        ([(x or {}).get("cue_kind") for x in o2["by_sent"].get(0, [])], len(pick_posts(o2["mock"]))))


# ---------------------------------------------------------------- T: tracker first draw after a scroll (A2.7, A2.11 item 4)
@section("T")
def t_tracker():
    pkg = os.path.join(REPO, "tracker")
    if not shutil.which("swift") or not os.path.exists(os.path.join(pkg, "Package.swift")): skip("T_swift_first_draw_tests", "swift or tracker package missing"); return
    t = time.time()
    p = subprocess.run(["swift", "test", "--filter", "FirstDrawTests"], cwd=pkg, capture_output=True, text=True, timeout=1500)
    out = p.stdout + p.stderr
    mt = re.findall(r"Executed (\d+) tests?, with (\d+) failures", out); mx = max(mt, key=lambda x: int(x[0])) if mt else None
    info(f"T swift test --filter FirstDrawTests exit {p.returncode} in {time.time() - t:.0f}s: {mx}")
    add("T_first_draw_tests_pass", p.returncode == 0 and mx is not None and int(mx[0]) >= 1 and mx[1] == "0", (p.returncode, mx, out[-300:]))
    names = re.findall(r"Test Case '-\[\S+ (test\w+)\]' passed", out) + re.findall(r"✔ Test (\w+)\(\) passed", out)
    add("T_first_draw_test_covers_scroll_refit", any(re.search(r"(?i)scroll", n) for n in names), names[:8])
    src = open(os.path.join(pkg, "Tests", "TrackerTests", "FirstDrawTests.swift"), errors="ignore").read() if os.path.exists(os.path.join(pkg, "Tests", "TrackerTests", "FirstDrawTests.swift")) else ""
    add("T_first_draw_test_asserts_page_fit_within_2pt", "page-fit" in src and re.search(r"300", src) is not None, "FirstDrawTests.swift has no page-fit / 300 pt scroll assertion")


# ---------------------------------------------------------------- U: implementer unit tests (new files, run as-is)
U_RUNNER = r"""
import importlib, inspect, sys, traceback, unittest
sys.path[:0] = [sys.argv[1], sys.argv[2]]
m = importlib.import_module(sys.argv[3]); n = f = 0
for name, fn in sorted(vars(m).items()):
    if name.startswith("test_") and inspect.isfunction(fn) and not inspect.signature(fn).parameters:
        n += 1
        try: fn()
        except Exception: f += 1; print("FAILED", name, traceback.format_exc()[-300:])
suite = unittest.defaultTestLoader.loadTestsFromModule(m); r = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w")).run(suite)
n += r.testsRun; f += len(r.failures) + len(r.errors)
print(f"RESULT {n} {f}")
"""
@section("U")
def u_unit():
    files = sorted(glob.glob(os.path.join(REPO, "tests", "test_*.py")))
    p5 = [x for x in files if os.path.basename(x) in ("test_partpick.py", "test_part_hierarchy.py", "test_callout_targets.py", "test_panel_layout.py", "test_pick_fallback.py")]
    for x in p5:
        nm = os.path.basename(x)[:-3]
        p = subprocess.run([DETPY, "-B", "-c", U_RUNNER, os.path.join(REPO, "tests"), SRC, nm], capture_output=True, text=True, timeout=600, cwd=REPO, env=penv(mkhome("u")))
        m = re.findall(r"RESULT (\d+) (\d+)", p.stdout)
        info(f"U {nm}: {m[-1] if m else p.stderr[-200:]}")
        add(f"U_{nm}_passes", bool(m) and int(m[-1][0]) >= 1 and m[-1][1] == "0", (m, p.stdout[-300:], p.stderr[-300:]))


# ---------------------------------------------------------------- Z: isolation
@section("Z")
def z_isolation():
    now = {}
    for sub in _real_snapshot:
        p = os.path.join(REAL_HOME, sub)
        now[sub] = (sorted(os.listdir(p)) if os.path.isdir(p) else (os.path.getsize(p), os.path.getmtime(p))) if os.path.exists(p) else None
    add("Z_real_home_unchanged", now == _real_snapshot, {k: (now[k], _real_snapshot[k]) for k in now if now[k] != _real_snapshot[k]})
    left = glob.glob(os.path.join(tempfile.gettempdir(), "speakeasy_cap_pick_*")) + glob.glob("/tmp/speakeasy_cap_pick_*")
    add("Z_no_pick_png_in_system_tmp", not left, left[:3])


def main():
    for k in ("S", "C", "F", "Q", "A", "D", "W", "T", "U", "H", "G", "N", "P", "Z"):
        {"S": s_parse, "C": c_deterministic, "F": f_fallbacks, "Q": q_queue, "A": a_area, "D": d_hairline, "W": w_whole, "T": t_tracker, "U": u_unit,
         "H": h_harness, "G": g_bench, "N": n_nomodel, "P": p_pick, "Z": z_isolation}[k]()
    c = collections.Counter(s for s, _ in R)
    print(f"SUMMARY checks={len(R)} pass={c['PASS']} fail={c['FAIL']} skip={c['SKIP']} secs={time.time() - T0:.0f}", flush=True)
    if not A.keep: shutil.rmtree(TMPROOT, ignore_errors=True)
    else: info(f"temp root kept at {TMPROOT}")
    sys.exit(1 if c["FAIL"] else 0)


if __name__ == "__main__":
    main()
