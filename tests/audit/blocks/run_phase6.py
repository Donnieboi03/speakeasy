#!/usr/bin/env python3
"""Phase-6 acceptance suite (black box, promoted from the holdout) for docs/contracts/phase6-block-classifier.md (block classifier).
Public data only unless --holdout DIR (the hidden bench dir) is given; checks that need hidden data then SKIP. Defaults are
file-relative (--repo) and this interpreter (--det-python / --noort-python, or SPEAKEASY_DOC_PYTHON / SPEAKEASY_NOORT_PYTHON); models
default to SPEAKEASY_LAYOUT_MODEL / SPEAKEASY_FIGCLS_MODEL / SPEAKEASY_LAYOUT2_MODEL, else install.sh's ~/.speakeasy/models copies
(read only). The no-onnxruntime checks SKIP when --noort-python can import onnxruntime.
Drives only `docmap.py parse-pdf`, the CLI (src/speakeasy), `speakeasy logs --scrub`, the verifier's bench harness pieces
(tests/audit/bench: scorer, geometry, gap fixtures, OCR stub) and mock LLM (tests/audit/stream/mock_llm.py), and reads logs, the
parse output and the temp $SPEAKEASY_HOME. Never reads app code (one static rule: no hex colour literal in src/blockcls.py, 9.4) and
never touches ~/.speakeasy (its doc venv is used read-only as an interpreter). "Unchanged" behaviour is checked differentially
against --baseline-ref (post-Phase-7 main, the B5' commit ac83185 by default), extracted with git archive. Amendment 1 rulings are
applied: the OCR OUT.json schema and exit codes (A1.1), the kind job's parse-recorded state and final sidecar (A1.2), and the exact
kill-switch identity (A1.3). Amendment 2: T_B frozen 0.50 and the T_C grid (A2.1, A2.2), the header tie on OCR-row tables (A2.3), the
accepted additions' limits (duplicate / container skips, lexicon-caption anchor, area-no-vlm; A2.4, A2.5), the sidecar `pages`
coverage (A2.5) and the OCR helper resolution rule (A2.6). Amendments 3 and 4 (section Y): the gap-page router (never on the existing
families), heron's add-only candidates, the kill switches (SPEAKEASY_LAYOUT2=off kills heron and the structural fallback; BLOCKCLS=off
removes every layout2 key), each heron fallback reason (blocks equal a LAYOUT2=off run except no-text-structural blocks on R1 pages),
the budgets and the text-free layout2 fields and events. Default parses and runs use heron (the gate configuration, A4.4).

Stage C is exercised with the classifier absent (contract 10: every candidate then routes to stage C, route low-p), so the mock's
answers decide the outcome deterministically. No real model call is made; nothing can leave the machine (a recording HTTPS proxy
catches any non-loopback attempt).

Usage: python3 run_phase6.py [--repo PATH] [--baseline-ref REF] [--only S,R,K,L,U,C,D,V,N,O,T,Y,A,P,H,B,Z] [--keep] [--with-bench]
         [--det-python PY] [--noort-python PY] [--layout-model PATH] [--figcls-model PATH] [--layout2-model PATH] [--holdout DIR]
Output: `PASS|FAIL|SKIP <name> <detail>` per check, `INFO ...`, then `SUMMARY ...`. Exit 0 only if no FAIL.
Sections: S schema + text-free parse output, fig_ids; R caption regex first, unchanged; K kill switch; L lexicon (positives, negatives,
labels, aliases, in-box match); U uncaptioned candidates (gated: VLM off); C classifier missing / mismatch / no onnxruntime; D determinism
(parse, crops); V stage C outcomes and fallbacks (accept, p_A + p_B, text, off-menu, low-confidence, no-logprobs, unavailable, timeout,
malformed, request shape, one at a time, budget, cache hit, GROUND=off, VLM=off); N network (loopback only; OpenRouter only under the
three 6.4 conditions); O OCR helper missing, image tables without rows; A Read-area capture (doc-unresolved vs area_pdf ok); P privacy
(LOG_TEXT unset and 1, scrub); H static install / licence / no hex; B bench acceptance 14.1-14.3 (only with --with-bench: gated bench on
both families with bench_gate, wrong_type vs the B5' bench, gap gate, replay thresholds); Z ~/.speakeasy unchanged."""
import argparse, collections, glob, hashlib, json, math, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
MODELS = os.path.expanduser("~/.speakeasy/models")
ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", ".."))); ap.add_argument("--baseline-ref", default="ac83185")
ap.add_argument("--only", default=""); ap.add_argument("--keep", action="store_true"); ap.add_argument("--with-bench", action="store_true")
ap.add_argument("--det-python", default=os.environ.get("SPEAKEASY_DOC_PYTHON") or sys.executable, help="python with pypdfium2, Pillow, onnxruntime, numpy")
ap.add_argument("--noort-python", default=os.environ.get("SPEAKEASY_NOORT_PYTHON") or sys.executable,
                help="python with reportlab and Pillow (fixture generation) and, for the no-onnxruntime checks, without onnxruntime")
ap.add_argument("--layout-model", default=os.environ.get("SPEAKEASY_LAYOUT_MODEL") or os.path.join(MODELS, "pp_doclayout_s.onnx"))
ap.add_argument("--figcls-model", default=os.environ.get("SPEAKEASY_FIGCLS_MODEL") or os.path.join(MODELS, "docling_figure_classifier_v2_5.onnx"))
ap.add_argument("--layout2-model", default=os.environ.get("SPEAKEASY_LAYOUT2_MODEL") or os.path.join(MODELS, "docling_layout_heron.onnx"))
ap.add_argument("--holdout", default=None, help="hidden bench dir; without it the suite runs on public data only")
A = ap.parse_args()
HOLD = os.path.abspath(os.path.expanduser(A.holdout)) if A.holdout else None
REPO = os.path.abspath(A.repo); SRC = os.path.join(REPO, "src"); CLI = os.path.join(SRC, "speakeasy"); BENCH = os.path.join(REPO, "tests", "audit", "bench")
MOCKPY = os.path.join(REPO, "tests", "audit", "stream", "mock_llm.py")
STUBS = [os.path.join(REPO, "tests", "audit", "nosel", "stubs"), os.path.join(REPO, "tests", "audit", "tts", "stubs")]
ONLY = {x.strip().upper() for x in A.only.split(",") if x.strip()}
DETPY, NOORT, LMODEL, FMODEL, L2MODEL = A.det_python, A.noort_python, A.layout_model, A.figcls_model, A.layout2_model
HERON_SHA = "59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a"; HERON_VERSION = "docling-heron/59c81a3a2923"   # A3.1 pin
FIGCLS_SHA = "27ffc48c27ae4e12c99b6f6de0dd730005245e47b70dd0c1339e62cbac3ec4c0"; FIGCLS_VERSION = "docling-figcls-v2.5/27ffc48c27ae"
REAL_HOME = os.path.realpath(os.path.expanduser("~/.speakeasy"))
TMPROOT = os.path.realpath(tempfile.mkdtemp(prefix="se_p6_")); T0 = time.time()
VMODEL = "qwen3-vl:4b-instruct"
KIND_KEYS = {"event", "page", "rect", "anchor", "det_cls", "det_score", "cls_label", "cls_p", "route", "stage", "kind", "p_top", "p_margin", "outcome", "ms",
             "model", "provider", "confidence"}
OUTCOMES = {"accepted", "rejected", "text", "low-confidence", "off-menu", "no-logprobs", "unavailable", "timeout", "error", "pending", "skipped"}
CLS_LABELS = {"table", "photograph", "engineering_drawing", "line_chart", "bar_chart", "flow_chart", "geographical_map", "pie_chart", "scatter_plot",
              "chemistry_structure", "topographical_map", "box_plot", "logo", "icon", "signature", "stamp", "qr_code", "bar_code", "other",
              "screenshot_from_computer", "screenshot_from_manual", "page_thumbnail", "full_page_image", "music", "calendar", "crossword_puzzle"}
PAGE_KIND_KEYS = {"candidates", "accepted", "routed", "rejected", "skipped", "ms", "no_text"}
REASONS = {None, "disabled", "no-onnxruntime", "model-missing", "model-mismatch", "load-failed", "inference-failed"}
FIG_RE = re.compile(r"^p\d+:(fig|tab)\d+$")
GRID = [round(0.5 + 0.05 * i, 2) for i in range(10)]
GRID_TC = [round(0.4 + 0.05 * i, 2) for i in range(12)]          # A2.2: T_C grid 0.40 ... 0.95 (provisional 0.95 until A2.2a)
T_B_FROZEN = 0.50                                                 # A2.1
SKIP_REASONS = {"over-cap", "budget", "duplicate", "container", "area-no-vlm"}   # A2.4
ANCHORS = ("uncaptioned", "ambiguous-caption", "lexicon-caption", "no-text", "no-text-structural", "area")   # A3.4 adds no-text-structural
DET_CLS = ("image", "chart", "table", "picture", "ink", None)   # A3.4 adds ink (structural); A5.2 pin update: heron's native 'picture' (figure family)
L2_REASONS = {"disabled", "no-onnxruntime", "model-missing", "model-mismatch", "load-failed", "inference-failed"}
L2_PAGE_KEYS = {"routed", "by", "src", "ms", "boxes", "added", "structural"}
L2_PAGE_EVENT_KEYS = {"event", "page", "by", "ms", "added", "structural", "detector_version"}
L2_UNAV_EVENT_KEYS = {"event", "reason", "error", "detector_version", "page"}
sys.path.insert(0, BENCH); sys.dont_write_bytecode = True
import score as S
import caption_lexicon as LEX
from run_bench import geometry_for, wait_kind_job, parse_cache_entries, sidecars_changed

R = []
def _s(d): return " ".join(str(d).split())[:700]
def add(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"; R.append((st, name)); print(f"{st} {name} {_s(detail) if not ok or os.environ.get('P6_VERBOSE') else ''}".rstrip(), flush=True)
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
def isnum(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
def isint(v): return isinstance(v, int) and not isinstance(v, bool)
def isrect(r): return isinstance(r, list) and len(r) == 4 and all(isnum(v) for v in r) and r[0] <= r[2] and r[1] <= r[3]
def jl(p):
    out = []
    for f in sorted(glob.glob(p + "*")) if p.endswith(".jsonl") else ([p] if os.path.exists(p) else []):
        for x in open(f, errors="ignore"):
            x = x.strip()
            if x:
                try: out.append(json.loads(x))
                except Exception: out.append({"_raw": x})
    return out
def strip_timing(d):
    """A1.3 item 3: drop keys named ms or ending in _ms (and nothing else)."""
    if isinstance(d, dict): return {k: strip_timing(v) for k, v in d.items() if k != "ms" and not k.endswith("_ms")}
    if isinstance(d, list): return [strip_timing(x) for x in d]
    return d
def strip_volatile(d):
    if isinstance(d, dict): return {k: strip_volatile(v) for k, v in d.items() if k not in ("ms", "ms_page")}
    if isinstance(d, list): return [strip_volatile(x) for x in d]
    return d
def diff(a, b, path="$"):
    if type(a) != type(b): return [path]
    if isinstance(a, dict): return [x for k in sorted(set(a) | set(b)) for x in ([f"{path}.{k}"] if k not in a or k not in b else diff(a[k], b[k], f"{path}.{k}"))]
    if isinstance(a, list): return [path] if len(a) != len(b) else [x for i, (p, q) in enumerate(zip(a, b)) for x in diff(p, q, f"{path}[{i}]")]
    return [] if a == b else [path]

# ---------------------------------------------------------------- setup
for f, what in ((LMODEL, "layout model"), (FMODEL, "classifier model"), (L2MODEL, "second-detector (heron) model")):
    if not os.path.isfile(f): print(f"FATAL {what} missing at {f}"); sys.exit(2)
if hashlib.sha256(open(FMODEL, "rb").read()).hexdigest() != FIGCLS_SHA: print(f"FATAL classifier model at {FMODEL} is not the pinned file"); sys.exit(2)
if hashlib.sha256(open(L2MODEL, "rb").read()).hexdigest() != HERON_SHA: print(f"FATAL heron model at {L2MODEL} is not the pinned file"); sys.exit(2)
BASE_SRC = None
if A.baseline_ref:
    BASE = os.path.join(TMPROOT, "base"); os.makedirs(BASE)
    subprocess.run(f"git -C '{REPO}' archive {A.baseline_ref} | tar -x -C '{BASE}'", shell=True, check=True)
    BASE_SRC = os.path.join(BASE, "src")
FXD = os.path.join(TMPROOT, "fx"); os.makedirs(FXD)
subprocess.run([NOORT, os.path.join(HERE, "make_p6docs.py"), FXD, REPO], check=True, capture_output=True)
P6DOC = {k: os.path.join(v, "doc.pdf") for k, v in json.load(open(os.path.join(FXD, "p6_docs.json"))).items()}
P6GT = {k: json.load(open(os.path.join(os.path.dirname(v), "gt.json"))) for k, v in P6DOC.items()}
P6CASES = {k: json.load(open(os.path.join(os.path.dirname(v), "cases.json"))) for k, v in P6DOC.items()}
PUBGAP = os.path.join(BENCH, "fixtures", "public-gap"); HIDGAP = os.path.join(HOLD, "gap", "fixtures") if HOLD else os.path.join(TMPROOT, "no-holdout")
HIDDEN_DEFAULT = sorted(glob.glob(os.path.join(HOLD, "fixtures", "*", "doc.pdf"))) if HOLD else []
NOORT_HAS_ORT = subprocess.run([NOORT, "-c", "import onnxruntime"], capture_output=True).returncode == 0
NOORT_SKIP = f"--noort-python {NOORT} imports onnxruntime (pass an interpreter without it, e.g. SPEAKEASY_NOORT_PYTHON)"
SIDECAR_KEYS = ["version", "parse_key", "state", "routed", "answered", "outcomes", "breaker", "error", "started_ts", "done_ts", "done_ms", "entries"]
DONE_KEYS = {"event", "parse_key", "state", "routed", "answered", "done_ms", "breaker"}
P6_OFF_FORBIDDEN = ("kinds", "blockcls_events", "captioned", "printed_label", "aliases", "caption_src", "kind_src", "rows_src", "ocr_rows")
GAPDOCS = {os.path.basename(d): d for d in sorted(glob.glob(os.path.join(PUBGAP, "*"))) + sorted(glob.glob(os.path.join(HIDGAP, "*"))) if os.path.isdir(d)}
PUBLIC_DEFAULT = sorted(glob.glob(os.path.join(BENCH, "fixtures", "public", "*", "doc.pdf")))
STUBD = os.path.join(TMPROOT, "stubs"); os.makedirs(STUBD)
open(os.path.join(STUBD, "security"), "w").write("#!/bin/sh\necho 'security: stubbed (no keychain in tests)' >&2\nexit 44\n"); os.chmod(os.path.join(STUBD, "security"), 0o755)
_real_snapshot = {}
for sub in ("models", "cache/doc", "logs/runs.jsonl", "logs/focus.jsonl", "logs/blockcls.jsonl", "focus.json"):
    p = os.path.join(REAL_HOME, sub)
    _real_snapshot[sub] = (sorted(os.listdir(p)) if os.path.isdir(p) else (os.path.getsize(p), os.path.getmtime(p))) if os.path.exists(p) else None

_n = [0]
def mkhome(tag="h"):
    _n[0] += 1; d = os.path.join(TMPROOT, "homes", f"{_n[0]:04d}-{tag}"); h = os.path.join(d, "home"); os.makedirs(h); os.makedirs(os.path.join(d, "tmp"))
    rh = os.path.realpath(h); assert rh.startswith(TMPROOT + os.sep) and not rh.startswith(REAL_HOME)
    return h
def penv(home, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "STUB_", "MOCK_", "OCR_STUB_")) and k.lower() not in ("pythonpath", "no_proxy", "https_proxy", "http_proxy", "all_proxy")}
    e.update(HOME=home, SPEAKEASY_HOME=home, TMPDIR=os.path.join(os.path.dirname(home), "tmp"), PYTHONDONTWRITEBYTECODE="1")
    for k, v in kw.items():
        if v is None: e.pop(k, None)
        else: e[k] = str(v)
    return e

_parse_cache = {}
def parse(pdf, src=None, py=None, home=None, **kw):
    """docmap.py parse-pdf in a fresh temp home. kw: env overrides (None removes). Default: layout + classifier model, VLM off, no OCR."""
    py = py or DETPY; src = src or SRC; home = home or mkhome("parse"); d0 = os.path.dirname(home)
    env = dict(SPEAKEASY_LAYOUT_MODEL=LMODEL, SPEAKEASY_FIGCLS_MODEL=FMODEL, SPEAKEASY_LAYOUT2_MODEL=L2MODEL, SPEAKEASY_BLOCKCLS_VLM="off",
               SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker", "speakeasy-track"))
    env.update(kw)
    cp = os.path.join(d0, f"in_{time.time_ns()}.pdf"); shutil.copyfile(pdf, cp); out = os.path.join(d0, f"out_{time.time_ns()}.json")
    t = time.time()
    try: p = subprocess.run([py, os.path.join(src, "docmap.py"), "parse-pdf", cp, out], capture_output=True, text=True, env=penv(home, **env), timeout=300)
    except subprocess.TimeoutExpired: return {"rc": "timeout", "d": None, "err": "", "ms": None, "home": home}
    try: d = json.load(open(out))
    except Exception: d = None
    return {"rc": p.returncode, "d": d, "err": p.stderr[-600:], "ms": round((time.time() - t) * 1000), "home": home}
def P(pdf, **kw):
    key = (pdf, tuple(sorted(kw.items())))
    if key not in _parse_cache: _parse_cache[key] = parse(pdf, **kw)
    return _parse_cache[key]
def pages(d): return [p for p in ((d or {}).get("pages") or []) if isinstance(p, dict)]
def figs(d): return [(p.get("n"), f) for p in pages(d) for f in (p.get("figures") or []) if isinstance(f, dict)]
def blocks(d, page=None): return [(p.get("n"), b) for p in pages(d) for b in (p.get("blocks") or []) if isinstance(b, dict) and (page is None or p.get("n") == page)]
def events(d):
    """block_kind records of a parse (layout2_page / layout2_unavailable records live in the same list; read them with l2_events)."""
    return [e for e in ((d or {}).get("blockcls_events") or []) if isinstance(e, dict) and e.get("event", "block_kind") == "block_kind"]
def gt_figs(gt): return [(p["n"], b) for p in gt["pages"] for b in p["blocks"] if b["kind"] in ("figure", "table")]
def gt_caps(gt): return {(p["n"], b.get("label")): b for p in gt["pages"] for b in p["blocks"] if b["kind"] == "caption"}
def text_words(gt_dir):
    """Content words of a fixture (lines of its cases and spec) used to look for leaked document text."""
    ws = set()
    for c in json.load(open(os.path.join(gt_dir, "cases.json"))):
        for it in c.get("selection", []) + c.get("script", []): ws |= {w.strip(".,:;") for w in it["text"].split() if len(w.strip(".,:;")) >= 6}
    return ws

# ---------------------------------------------------------------- mock, proxy trap, CLI driver
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
    def kinds(self): return [r for r in self.recs() if r.get("event") == "kind_reply"]
    def picks(self): return [r for r in self.recs() if r.get("event") == "pick_reply"]
    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try: self.p.wait(5)
            except subprocess.TimeoutExpired: self.p.kill()

class Trap:
    """HTTP(S) proxy that records the first request line of every connection and closes it (nothing is forwarded)."""
    def __init__(self):
        self.lines = []; self.s = socket.socket(); self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); self.s.bind(("127.0.0.1", 0)); self.s.listen(16)
        self.port = self.s.getsockname()[1]; threading.Thread(target=self._loop, daemon=True).start()
    def _loop(self):
        while True:
            try: c, _ = self.s.accept()
            except OSError: return
            try: c.settimeout(2); self.lines.append(c.recv(4096).split(b"\r\n", 1)[0].decode(errors="replace"))
            except Exception: pass
            finally: c.close()
    def env(self):
        u = f"http://127.0.0.1:{self.port}"
        return dict(HTTPS_PROXY=u, https_proxy=u, HTTP_PROXY=u, http_proxy=u, ALL_PROXY=u, NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
TRAP = Trap()

def fixed(pairs):
    return {"MOCK_KIND": "fixed", "MOCK_KIND_TOP_LOGPROBS": json.dumps([{"token": t, "logprob": math.log(p)} for t, p in pairs])}

KIND_ENV = dict(SPEAKEASY_GROUND="local", SPEAKEASY_BLOCKCLS_VLM="local", SPEAKEASY_PICK="off", SPEAKEASY_VISION_MODEL=VMODEL)
NO_CLS = dict(SPEAKEASY_FIGCLS_MODEL=os.path.join(TMPROOT, "no-model", "docling_figure_classifier_v2_5.onnx"))

def speak(name, pdf, case, home=None, mock_env=None, env=None, cli=None, area=None, hint=None, stub_play=0.05, src_py=None):
    """One CLI run (bounds selection of a fixture case, or a Read-area capture when area=(png, case)) through a fresh mock.
    Returns dict(rc, rec, focus, kinds (mock kind_reply records of this run), mock, spoken, home, tmp, leftovers)."""
    cli = cli or CLI; home = home or mkhome(name); d0 = os.path.dirname(home); tmpd = os.path.join(d0, "tmp")
    hintp = os.path.join(d0, f"hint_{time.time_ns()}.json"); lbp = os.path.join(d0, f"lines_{time.time_ns()}.json")
    cp = os.path.join(d0, "doc.pdf")
    if pdf and not os.path.exists(cp): shutil.copyfile(pdf, cp)
    if area is None:
        pw = next(p["w"] for p in json.load(open(os.path.join(os.path.dirname(pdf), "gt.json")))["pages"] if p["n"] == case["page"])
        cap, lb = geometry_for(case, pw); stdin = "\n".join(it["text"] for it in case["selection"])
        cmd = [cli, "speak", "--capture-method", "bounds", f"--capture-rect={cap}", "--line-bounds", lbp, "--doc-hint", hintp, "--run-id", f"p6-{_n[0]}"]
        json.dump(hint or {"app": "Preview", "bundle": "com.apple.Preview", "title": "doc.pdf", "ax_document": "file://" + cp}, open(hintp, "w"))
    else:
        png, acase = area; capp = os.path.join(d0, "capture.png"); shutil.copyfile(png, capp); lb = acase["line_bounds"]
        stdin = "\n".join(it["text"] for it in acase["selection"]); rect = ",".join(f"{v:.1f}" for v in acase["capture_rect"])
        cmd = [cli, "speak", "--image", capp, "--capture-method", "area", f"--capture-rect={rect}", "--line-bounds", lbp, "--doc-hint", hintp,
               "--area-text", "ocr", "--run-id", f"p6-{_n[0]}"]
        json.dump(hint or {"app": "Safari", "bundle": "com.apple.Safari", "title": "capture"}, open(hintp, "w"))
    json.dump(lb, open(lbp, "w"))
    script = [it["text"] for it in (case or area[1])["script"]]
    mock = Mock(**(mock_env or {})); open(mock.reply, "w").write(" ".join(script))
    spoken = os.path.join(d0, "spoken.txt")
    if os.path.exists(spoken): os.remove(spoken)
    base = dict(SPEAKEASY_LLM_URL=f"http://127.0.0.1:{mock.port}/v1", SPEAKEASY_MODEL="mock-text", SPEAKEASY_LLM="local", SPEAKEASY_VISION="never",
                SPEAKEASY_GROUND="off", SPEAKEASY_TTS="say", SPEAKEASY_DOC_READ="local", SPEAKEASY_DOC_PYTHON=src_py or DETPY, SPEAKEASY_LAYOUT_MODEL=LMODEL,
                SPEAKEASY_FIGCLS_MODEL=FMODEL, SPEAKEASY_LAYOUT2_MODEL=L2MODEL, SPEAKEASY_BLOCKCLS_VLM="off", SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker", "speakeasy-track"),
                SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1", SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_TEXT_HIGHLIGHT="on", SPEAKEASY_FIGURE_CUES="on",
                SPEAKEASY_KEEP_CAPTURE="0", SPEAKEASY_LOG_TEXT="1", STUB_PLAY_SEC=str(stub_play), STUB_SPOKEN=spoken,
                PATH=":".join([STUBD] + STUBS + [os.environ.get("PATH", "/usr/bin:/bin")]))
    base.update(TRAP.env()); base.update(env or {})
    for f in glob.glob(os.path.join(home, "logs", "runs.jsonl*")) + glob.glob(os.path.join(home, "logs", "focus.jsonl*")): os.remove(f)
    t = time.time()
    try: p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=penv(home, **base), timeout=150); rc, err = p.returncode, p.stderr
    except subprocess.TimeoutExpired: rc, err = "timeout", ""
    time.sleep(0.3); out = {"name": name, "home": home, "tmp": tmpd, "rc": rc, "err": err[-500:], "wall": time.time() - t, "mock_obj": mock, "script": script}
    recs = jl(os.path.join(home, "logs", "runs.jsonl")); out["rec"] = recs[-1] if recs else {}; out["nrecs"] = len(recs)
    out["focus"] = jl(os.path.join(home, "logs", "focus.jsonl")); out["spoken"] = open(spoken).read() if os.path.exists(spoken) else ""
    out["leftovers"] = leftovers(tmpd)
    return out
def finish(o, wait=True):
    """Wait for the run's kind job (sidecar stable), then stop the mock and collect its kind requests and the blockcls log."""
    if wait:
        o["job"] = wait_kind_job(o["home"], float(os.environ.get("P6_KIND_WAIT", "75")))
        o["job_done"] = o["job"]["status"] in ("done", "none")
    o["kinds"] = o["mock_obj"].kinds(); o["mock"] = o["mock_obj"].recs(); o["mock_obj"].stop()
    o["bk"] = [r for r in jl(os.path.join(o["home"], "logs", "blockcls.jsonl")) if r.get("event") == "block_kind"]
    o["done_recs"] = [r for r in jl(os.path.join(o["home"], "logs", "blockcls.jsonl")) if r.get("event") == "kind_job_done"]
    o["leftovers"] = leftovers(o["tmp"])
    return o
def leftovers(tmpd):
    """Crop PNGs (6.2) and the Read-area app-lines OCR file (A1.1) must never survive a run."""
    return sorted(os.path.basename(x) for pat in ("speakeasy_cap_kind_*", "speakeasy_cap_ocr_*") for x in glob.glob(os.path.join(tmpd, "**", pat), recursive=True))
def kind_jobs(home):
    """{parse key: blockcls.kind_job} of a home's parse cache entries (A1.2)."""
    return {k: bc.get("kind_job") for k, bc in parse_cache_entries(home)}
def sidecar_checks(name, o, want_states=("done", "partial"), breaker=None):
    """A1.2: the queued parse, the final sidecar (exact top-level keys, written once) and the kind_job_done record (no text)."""
    j = (o.get("job") or {}).get("jobs") or []
    add(f"{name}_parse_kind_job_queued_with_routed", bool(j) and all(x.get("kind_job") == "queued" and isinstance(x.get("routed"), int) and x["routed"] >= 1 for x in j),
        [(x.get("kind_job"), x.get("routed")) for x in j])
    if not j: return
    sc = None
    try: sc = json.load(open(j[0]["path"], encoding="utf-8"))
    except Exception as e: add(f"{name}_sidecar_readable", False, f"{type(e).__name__}: {e}"); return
    pg = sc.get("pages")
    add(f"{name}_sidecar_pages_cover_routed_pages", isinstance(pg, list) and pg == sorted(set(pg)) and all(isint(x) for x in pg) and set(j[0].get("routed_pages") or []) <= set(pg),
        (pg, j[0].get("routed_pages")))
    add(f"{name}_sidecar_schema", set(sc) == set(SIDECAR_KEYS) | {"pages"} and sc.get("version") == 1 and sc.get("parse_key") == j[0]["key"] and re.fullmatch(r"[0-9a-f]{20}", str(sc.get("parse_key")))
        and isinstance(sc.get("done_ms"), int) and sc.get("breaker") in (None, "unavailable", "no-logprobs") and isinstance(sc.get("entries"), list)
        and isinstance(sc.get("outcomes"), dict) and sc.get("routed") == j[0]["routed"], {k: sc.get(k) for k in SIDECAR_KEYS if k != "entries"})
    add(f"{name}_sidecar_state_{'_'.join(want_states)}", sc.get("state") in want_states, (sc.get("state"), sc.get("error")))
    if breaker is not None: add(f"{name}_sidecar_breaker_{breaker}", sc.get("breaker") == breaker, sc.get("breaker"))
    if sc.get("state") == "done": add(f"{name}_done_means_no_skipped", not (sc.get("outcomes") or {}).get("skipped"), sc.get("outcomes"))
    dr = o.get("done_recs") or []
    add(f"{name}_kind_job_done_record", len(dr) == 1 and set(dr[0]) <= DONE_KEYS | {"ts", "pid"} and DONE_KEYS <= set(dr[0]) and dr[0].get("state") == sc.get("state")
        and dr[0].get("parse_key") == sc.get("parse_key") and not any_text_values(dr[0]), dr[:2])
    add(f"{name}_no_tmp_sidecar_left", not [f for f in os.listdir(os.path.dirname(j[0]["path"])) if ".kinds." in f and not f.endswith((".kinds.json", ".kinds.lock"))],
        sorted(os.listdir(os.path.dirname(j[0]["path"]))))
def fake_ocr_source(argl, code, table=None):
    """A fake `speakeasy-track` (A1.1 output, A2.6 invocation). table: {bbox, rows, tokens} in page pt; when the PNG's aspect matches the
    table body plus the 4 pt margin, it answers one OCR line per row, mapped to PNG pixels; otherwise it answers no lines."""
    return f"""#!/usr/bin/env python3
import json, os, struct, sys
argv = sys.argv[1:]; png = len(argv) == 3 and open(argv[1], "rb").read(8) == b"\\x89PNG\\r\\n\\x1a\\n"
open({argl!r}, "a").write(json.dumps({{"argv": argv, "png": png}}) + "\\n")
if len(argv) != 3 or argv[0] != "ocr": sys.exit(2)
if not png: sys.exit(3)
if {code}: sys.exit({code})
w, h = struct.unpack(">II", open(argv[1], "rb").read(24)[16:24]); lines = []; T = {table!r}
if T:
    bx0, by0, bx1, by1 = T["bbox"]; aw, ah = bx1 - bx0 + 8, by1 - by0 + 8
    if abs((w / h) - (aw / ah)) / (aw / ah) < 0.04:
        z = w / aw
        for r, t in zip(T["rows"], T["tokens"]):
            rc = [round(max(0, (r[0] - bx0 + 4) * z), 1), round(max(0, (r[1] - by0 + 4) * z), 1), round(min(w, (r[2] - bx0 + 4) * z), 1), round(min(h, (r[3] - by0 + 4) * z), 1)]
            lines.append({{"rect": rc, "text": t, "conf": 0.99}})
out = {{"version": 1, "engine": "vision-text", "image_size": [w, h], "unit": "px", "origin": "top-left", "lines": lines, "tables": [], "ms": 1}}
json.dump(out, open(argv[2] + ".t", "w")); os.replace(argv[2] + ".t", argv[2])
"""
def norm(s): return re.sub(r"\s+", " ", s or "").strip()
def run_ok(name, o):
    sp = norm(o.get("spoken"))
    add(f"{name}_exit0_one_record", o.get("rc") == 0 and o.get("nrecs", 0) >= 1, (o.get("rc"), o.get("nrecs"), o.get("err")))
    add(f"{name}_every_sentence_spoken", all(norm(t) in sp for t in o["script"]), [t for t in o["script"] if norm(t) not in sp][:2])
    add(f"{name}_no_temp_crop_png", not o.get("leftovers"), o.get("leftovers", [])[:3])

def kind_record_checks(name, recs, words=(), need=True):
    bad = [] if recs or not need else [("no block_kind records", 0)]
    # The logger stamps a timestamp on every log record (and possibly the writer pid, then on all of them): ts, and pid only when every
    # record carries it, are allowed beyond 9.2's keys (+ fig_id, kind_src); any other extra key still fails.
    extra_ok = {"fig_id", "kind_src", "ts"} | ({"pid"} if recs and all("pid" in r for r in recs) else set())
    for r in recs:
        ks = set(r)
        if not (KIND_KEYS <= ks <= KIND_KEYS | extra_ok): bad.append(("keys", sorted(ks ^ KIND_KEYS))); continue
        if "ts" in r and not isnum(r["ts"]): bad.append(("ts", r["ts"]))
        if "pid" in r and not isint(r["pid"]): bad.append(("pid", r["pid"]))
        if "kind_src" in r and r["kind_src"] not in ("caption", "lexicon", "classifier", "vlm", None): bad.append(("kind_src", r["kind_src"]))
        if r["anchor"] == "lexicon-caption" and r.get("kind_src", "lexicon") != "lexicon": bad.append(("lexicon_caption_kind_src", r.get("kind_src")))
        if r["outcome"] not in OUTCOMES: bad.append(("outcome", r["outcome"]))
        if r["anchor"] not in ANCHORS: bad.append(("anchor", r["anchor"]))
        if r["det_cls"] not in DET_CLS: bad.append(("det_cls", r["det_cls"]))
        if (r["anchor"] == "no-text-structural") != (r["det_cls"] == "ink") or (r["det_cls"] == "ink" and r["det_score"] is not None): bad.append(("structural", r["anchor"], r["det_cls"], r["det_score"]))
        if r["route"] not in ("textish", "disagree", "low-p", None): bad.append(("route", r["route"]))
        if r["stage"] not in ("classifier", "vlm"): bad.append(("stage", r["stage"]))
        if r["kind"] not in ("figure", "table", "text", "none", None): bad.append(("kind", r["kind"]))
        if r["cls_label"] is not None and r["cls_label"] not in CLS_LABELS: bad.append(("cls_label", r["cls_label"]))
        if r["provider"] not in ("local", "openrouter", None) or r["confidence"] not in ("logprob", "letter-only", None): bad.append(("provider/confidence", r["provider"], r["confidence"]))
        if not (isinstance(r["model"], str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", r["model"])): bad.append(("model", r["model"]))
        if not isrect(r["rect"]) or (r["anchor"] != "area" and not all(abs(round(v, 1) - v) < 1e-9 for v in r["rect"])): bad.append(("rect", r["rect"]))
        if not (isint(r["page"]) or (r["anchor"] == "area" and r["page"] is None)): bad.append(("page", r["page"]))
        for k in ("p_top", "p_margin", "cls_p", "det_score"):
            v = r[k]
            if v is not None and not (isnum(v) and 0 <= v <= 1.0001 and abs(round(v, 2) - v) < 1e-9): bad.append((k, v))
        if r["outcome"] in ("no-logprobs", "unavailable") and (r["p_top"] is not None or r["p_margin"] is not None): bad.append(("p_null", r["outcome"]))
        if "fig_id" in r and not (r["fig_id"] is None or FIG_RE.match(str(r["fig_id"]))): bad.append(("fig_id", r["fig_id"]))
        if not isnum(r["ms"]): bad.append(("ms", r["ms"]))
    add(f"{name}_block_kind_records_schema", not bad, bad[:4])
    blob = free_text_blob(recs)          # enum-valued fields (anchor, outcome, ...) are pinned above, not scanned for words
    leak = sorted(w for w in words if w.lower() in blob)
    add(f"{name}_block_kind_records_no_document_text", not leak, leak[:5])

# Keys whose values are closed enums (validated against their sets by the schema checks) or the model name: an enum value such as
# "ambiguous-caption" is not document text, so the document-word scans read every other string value. Dict keys under "outcomes" /
# "skipped" / "accepted" are enum names too and are checked against their enums instead (enum_dict_keys_ok).
ENUM_KEYS = {"event", "anchor", "det_cls", "cls_label", "route", "stage", "kind", "outcome", "model", "provider", "confidence", "kind_src", "fig_id",
             "state", "breaker", "kind_job", "status", "reason", "src", "by", "detector_version"}   # layout2 enums (A3.4), validated by l2_schema_bad
def free_text_blob(obj):
    """Lower-cased string values of obj outside ENUM_KEYS (dict keys excluded), for the document-word scans."""
    out = []
    def walk(x, k=None):
        if isinstance(x, dict):
            for kk, v in x.items(): walk(v, kk)
        elif isinstance(x, list):
            for v in x: walk(v, k)
        elif isinstance(x, str) and k not in ENUM_KEYS: out.append(x.lower())
    walk(obj); return "\n".join(out)
def enum_dict_keys_ok(obj):
    """Keys of every outcomes / skipped / accepted dict in obj are enum names ([] when all are)."""
    bad = []
    allowed = {"outcomes": OUTCOMES, "skipped": SKIP_REASONS, "accepted": {"figure", "table"}}
    def walk(x):
        if isinstance(x, dict):
            for kk, v in x.items():
                if kk in allowed and isinstance(v, dict): bad.extend((kk, z) for z in v if z not in allowed[kk])
                walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(obj); return bad
def any_text_values(obj, allow=()):
    """Strings in obj (recursively) that are not short enums, hashes, model names or fig_ids."""
    out = []
    def walk(x, k=None):
        if isinstance(x, dict):
            for kk, v in x.items(): walk(v, kk)
        elif isinstance(x, list):
            for v in x: walk(v, k)
        elif isinstance(x, str):
            if k in allow: return
            if " " in x or len(x) > 40: out.append((k, x[:40]))
    walk(obj); return out

# ---------------------------------------------------------------- S: schema and text-free parse output, fig_ids
@section("S")
def sec_schema():
    for name in ("uncap", "lexpos"):
        r = P(P6DOC[name]); d = r["d"] or {}
        add(f"S_{name}_parse_exit0", r["rc"] == 0 and bool(d), (r["rc"], r["err"]))
        bc = d.get("blockcls")
        add(f"S_{name}_blockcls_block", isinstance(bc, dict) and set(bc) == {"status", "classifier_version", "reason", "T_B", "T_C", "kind_job", "routed"} and bc.get("status") == "ok"
            and bc.get("kind_job") == "none" and isint(bc.get("routed"))
            and bc.get("classifier_version") == FIGCLS_VERSION and bc.get("reason") in REASONS, bc)
        if isinstance(bc, dict):
            add(f"S_{name}_T_B_frozen_0_50_and_T_C_on_the_A2_2_grid", bc.get("T_B") == T_B_FROZEN and bc.get("T_C") in GRID_TC, (bc.get("T_B"), bc.get("T_C")))
        bad = []
        for p in pages(d):
            if not p.get("figs_done"): continue
            k = p.get("kinds")
            if not (isinstance(k, dict) and set(k) == PAGE_KIND_KEYS and isinstance(k.get("accepted"), dict) and set(k["accepted"]) == {"figure", "table"}
                    and isinstance(k.get("skipped"), dict) and isinstance(k.get("no_text"), bool) and all(isint(k.get(x)) for x in ("candidates", "routed", "rejected"))):
                bad.append((p.get("n"), k))
            elif not set(k["skipped"]) <= SKIP_REASONS: bad.append(("skip_reasons", p.get("n"), sorted(k["skipped"])))
            elif k["candidates"] > 8 or (k["routed"] + k["rejected"] + sum(k["accepted"].values())) > k["candidates"]: bad.append(("counts", p.get("n"), k))
        add(f"S_{name}_pages_kinds_schema_and_cap8", not bad and any(p.get("kinds") for p in pages(d)), bad[:3])
        kind_record_checks(f"S_{name}", events(d), text_words(os.path.dirname(P6DOC[name])))
        add(f"S_{name}_events_text_free", not any_text_values(events(d)), any_text_values(events(d))[:3])
        fb = []
        for n, f in figs(d):
            need = {"captioned", "printed_label", "aliases", "caption_src", "kind_src"}
            if not need <= set(f): fb.append(("keys", n, f.get("label"), sorted(need - set(f)))); continue
            if f["kind_src"] not in ("caption", "lexicon", "classifier", "vlm") or f["caption_src"] not in ("regex", "lexicon", "lexicon+in-box", None): fb.append(("enum", f.get("label"), f["kind_src"], f["caption_src"]))
            if f["captioned"] is False:
                if not re.fullmatch(r"(Figure|Table) U\d+-\d+", str(f.get("label"))) or f.get("caption_bbox"): fb.append(("uncaptioned_label", f.get("label"), f.get("caption_bbox")))
            elif not (isinstance(f.get("aliases"), list) and f.get("label") in f["aliases"] and f.get("printed_label") in f["aliases"]): fb.append(("aliases", f.get("label"), f.get("aliases")))
            if str(f.get("label", "")).startswith("Table") and f.get("rows_src") not in ("ocr-table", "ocr-lines", "none", None): fb.append(("rows_src", f.get("rows_src")))
        add(f"S_{name}_figures_new_keys", not fb and bool(figs(d)), fb[:4])
        add(f"S_{name}_figure_blocks_have_kind_src", all(b.get("kind_src") in ("caption", "lexicon", "classifier", "vlm") for _, b in blocks(d) if b.get("kind") in ("figure", "table")),
            [(n, b.get("kind"), b.get("kind_src")) for n, b in blocks(d) if b.get("kind") in ("figure", "table") and b.get("kind_src") not in ("caption", "lexicon", "classifier", "vlm")][:3])
        ids = collections.defaultdict(list)
        for n, f in figs(d):
            if "fig_id" in f: ids[(n, "tab" if str(f.get("label", "")).startswith("Table") else "fig")].append(f["fig_id"])
        if ids:
            okf = all(FIG_RE.match(str(x)) for v in ids.values() for x in v) and all(v == [f"p{n}:{k}{i + 1}" for i in range(len(v))] for (n, k), v in ids.items())
            add(f"S_{name}_fig_ids_reading_order_per_page", okf, dict(list(ids.items())[:4]))
    o = finish(speak("S_run", P6DOC["uncap"], P6CASES["uncap"][0]), wait=False)
    rec = o["rec"]; run_ok("S_run", o)
    add("S_run_doc_parse_kinds_matches_page", isinstance((rec.get("doc_parse") or {}).get("kinds"), dict) and set(rec["doc_parse"]["kinds"]) == PAGE_KIND_KEYS, (rec.get("doc_parse") or {}).get("kinds"))
    add("S_run_fig_ids_format", all(FIG_RE.match(str(x)) for x in rec.get("fig_ids") or []), rec.get("fig_ids"))
    if "blockcls_events" in rec: kind_record_checks("S_run_runs", rec["blockcls_events"], text_words(os.path.dirname(P6DOC["uncap"])))

# ---------------------------------------------------------------- R: caption regex first, unchanged
@section("R")
def sec_regex_first():
    if not BASE_SRC: skip("R_all", "no --baseline-ref (post-Phase-7 main)"); return
    docs = PUBLIC_DEFAULT + [P6DOC["uncap"], P6DOC["lexpos"]] + [os.path.join(GAPDOCS[k], "doc.pdf") for k in sorted(GAPDOCS) if k.startswith("gap")]
    bad = []; n = 0
    for pdf in docs:
        new = P(pdf)["d"]; old = parse(pdf, src=BASE_SRC, SPEAKEASY_FIGCLS_MODEL=None)["d"]
        if not new or not old: bad.append(("parse", os.path.basename(os.path.dirname(pdf)))); continue
        nf = {(p, f.get("label")): f for p, f in figs(new)}
        for p, f in figs(old):
            n += 1; g = nf.get((p, f.get("label")))
            if g is None: bad.append(("lost", os.path.basename(os.path.dirname(pdf)), p, f.get("label"))); continue
            for k in ("bbox", "caption_bbox", "panels", "parts"):
                if k in f and strip_volatile(f.get(k)) != strip_volatile(g.get(k)): bad.append(("changed", os.path.basename(os.path.dirname(pdf)), p, f.get("label"), k))
            if g.get("caption_src") != "regex" or g.get("kind_src") != "caption": bad.append(("src", f.get("label"), g.get("caption_src"), g.get("kind_src")))
    add(f"R_standard_captioned_figures_unchanged_vs_baseline_{n}", not bad and n > 0, bad[:6])

# ---------------------------------------------------------------- K: kill switch
@section("K")
def sec_killswitch():
    """A1.3: with SPEAKEASY_BLOCKCLS=off the parse equals the ac83185 parse except the one exact blockcls key; only ms / *_ms differ."""
    if not BASE_SRC: skip("K_identity", "no --baseline-ref"); return
    on_bc = (P(P6DOC["uncap"])["d"] or {}).get("blockcls") or {}
    def check(pdf):
        off = parse(pdf, SPEAKEASY_BLOCKCLS="off")["d"]; base = parse(pdf, src=BASE_SRC, SPEAKEASY_FIGCLS_MODEL=None)["d"]
        if not off or not base: return ["parse failed"]
        bc = off.get("blockcls"); E = []
        want = {"status": "unavailable", "classifier_version": None, "reason": "disabled", "T_B": on_bc.get("T_B"), "T_C": on_bc.get("T_C"), "kind_job": "none", "routed": 0}
        if bc != want or list(bc or {}) != list(want): E.append(("blockcls", bc))
        rest = {k: v for k, v in off.items() if k != "blockcls"}
        blob = json.dumps(rest)
        E += [("forbidden_key", k) for k in P6_OFF_FORBIDDEN if f'"{k}":' in blob]
        E += diff(strip_timing(base), strip_timing(rest))[:4]
        return E
    pub = PUBLIC_DEFAULT + [os.path.join(PUBGAP, k, "doc.pdf") for k in sorted(os.listdir(PUBGAP)) if os.path.isdir(os.path.join(PUBGAP, k))]
    bad = [(os.path.basename(os.path.dirname(p)), e[:3]) for p in pub for e in [check(p)] if e]
    add(f"K_off_equals_ac83185_on_every_public_fixture_{len(pub)}", not bad and on_bc.get("T_B") == T_B_FROZEN, bad[:4])
    p6 = [P6DOC["lexpos"], P6DOC["uncap"], P6DOC["lexneg"]]
    bad = [(os.path.basename(os.path.dirname(p)), e[:3]) for p in p6 for e in [check(p)] if e]
    add("K_off_equals_ac83185_on_targeted_docs", not bad, bad[:3])
    hid = HIDDEN_DEFAULT + [os.path.join(HIDGAP, k, "doc.pdf") for k in sorted(os.listdir(HIDGAP)) if os.path.isdir(os.path.join(HIDGAP, k))] if os.path.isdir(HIDGAP) else HIDDEN_DEFAULT
    if not HOLD: skip("K_off_equals_ac83185_on_hidden_fixtures", "no --holdout")
    else:
        nbad = sum(1 for p in hid if check(p))
        add(f"K_off_equals_ac83185_on_hidden_fixtures", nbad == 0 and bool(hid), f"{nbad} of {len(hid)} differ")
    o = finish(speak("K_off_run", P6DOC["uncap"], P6CASES["uncap"][0], mock_env=fixed([("C", 0.97)]), env=dict(KIND_ENV, SPEAKEASY_BLOCKCLS="off", **NO_CLS)), wait=False)
    run_ok("K_off_run", o); add("K_off_no_kind_request", not o["kinds"], len(o["kinds"]))
    rec = o["rec"]
    add("K_off_runs_log_has_no_phase6_keys", not ({"blockcls_events", "kind_job", "area_kinds"} & set(rec)) and "kinds" not in (rec.get("doc_parse") or {}),
        sorted({"blockcls_events", "kind_job", "area_kinds"} & set(rec)))
    add("K_off_parse_cache_kind_job_none", set(kind_jobs(o["home"]).values()) <= {"none"}, kind_jobs(o["home"]))
    sc = [k for k in GAPDOCS if "scan" in k and k.startswith("gap")]
    if sc:
        c = next(c for c in json.load(open(os.path.join(GAPDOCS[sc[0]], "cases.json"))) if c.get("area"))
        o = finish(speak("K_off_area", None, None, area=(os.path.join(GAPDOCS[sc[0]], c["capture"]), c), env={"SPEAKEASY_BLOCKCLS": "off"}), wait=False)
        add("K_off_area_capture_classify_does_not_start", "area_kinds" not in o["rec"] and not o["leftovers"], (o["rec"].get("area_kinds"), o["leftovers"]))


# ---------------------------------------------------------------- L: lexicon
@section("L")
def sec_lexicon():
    base = parse(P6DOC["lexpos"], src=BASE_SRC, SPEAKEASY_FIGCLS_MODEL=None)["d"] if BASE_SRC else None
    std = {(p, f.get("label")) for p, f in figs(base)} if base else set()
    for variant, kw in (("cls", {}), ("nocls", dict(py=NOORT)), ("detector_off", dict(SPEAKEASY_LAYOUT_MODEL=os.path.join(TMPROOT, "nolayout.onnx")))):
        if variant == "nocls" and NOORT_HAS_ORT: skip("L_nocls_positives", NOORT_SKIP); continue
        d = P(P6DOC["lexpos"], **kw)["d"]; gt = P6GT["lexpos"]; caps = gt_caps(gt); bad = []; seen = 0
        for p, g in gt_figs(gt):
            cap = caps.get((p, g.get("label"))); m = LEX.match(g["label"] + ":")
            want_kind = g["kind"]
            hit = next((f for n, f in figs(d) if n == p and S._is_rect(f.get("caption_bbox")) and cap and S.iou(f["caption_bbox"], cap["bbox"]) >= 0.5), None)
            if hit is None: bad.append(("not_captioned", g["label"])); continue
            seen += 1; lab = str(hit.get("label")); pl = LEX.printed_label(g["label"] + ":")
            if not lab.startswith("Figure " if want_kind == "figure" else "Table "): bad.append(("kind", g["label"], lab))
            if pl and hit.get("printed_label") != pl: bad.append(("printed_label", g["label"], hit.get("printed_label"), pl))
            num = (m[2] if m else None); exp_src = "regex" if (p, lab) in std else ("lexicon", "lexicon+in-box")
            if num and exp_src != "regex" and not lab.endswith(" " + num): bad.append(("number_as_printed", g["label"], lab))   # standard captions keep today's label
            if not (isinstance(hit.get("aliases"), list) and lab in hit["aliases"] and hit.get("printed_label") in hit["aliases"]): bad.append(("aliases", g["label"], hit.get("aliases")))
            if (exp_src == "regex" and hit.get("caption_src") != "regex") or (exp_src != "regex" and BASE_SRC and hit.get("caption_src") not in exp_src): bad.append(("caption_src", g["label"], hit.get("caption_src")))
        add(f"L_{variant}_positives_captioned_with_label_printed_label_aliases_{seen}", not bad, bad[:6])
    for variant, kw in (("cls", {}), ("nocls", dict(py=NOORT))):
        if variant == "nocls" and NOORT_HAS_ORT: skip("L_nocls_negatives", NOORT_SKIP); continue
        d = P(P6DOC["lexneg"], **kw)["d"]; gt = P6GT["lexneg"]; dec = gt["pages"][0]["prose_decoys"]
        acc = S.new_kind_acc(); S.prose_decoys_page(dec, [{"kind": b.get("kind"), "bbox": b.get("bbox")} for _, b in blocks(d, 1)], acc)
        capfig = [f.get("label") for _, f in figs(d) if S._is_rect(f.get("caption_bbox")) and any(S.iou(f["caption_bbox"], r) > 0.3 for r in dec)]
        add(f"L_{variant}_negatives_stay_prose", acc["prose_decoys"]["kept"] == len(dec) and not capfig, (acc["prose_decoys"], capfig))
    bad = []; n_lc = 0
    for pdf in [P6DOC["lexpos"]] + [os.path.join(GAPDOCS[x], "doc.pdf") for x in sorted(GAPDOCS)]:
        dd = P(pdf)["d"] or {}
        for e in events(dd):
            if e.get("anchor") != "lexicon-caption": continue
            n_lc += 1
            f = next((f for n, f in figs(dd) if n == e.get("page") and isrect(f.get("bbox")) and S.iou(f["bbox"], e["rect"]) >= 0.5), None)
            if f is None or f.get("caption_src") not in ("lexicon", "lexicon+in-box") or f.get("kind_src") != "lexicon": bad.append((e.get("page"), (f or {}).get("caption_src"), (f or {}).get("kind_src")))
            if e.get("outcome") in ("pending",) or e.get("route"): bad.append(("routed_lexicon_caption", e.get("route"), e.get("outcome")))
    add(f"L_lexicon_caption_anchor_pairs_lexicon_captions_only_{n_lc}", not bad, bad[:4])
    d = P(P6DOC["lexpos"], SPEAKEASY_BLOCKCLS="off")["d"]
    add("L_kill_switch_turns_lexicon_off", not any(f.get("caption_src") in ("lexicon", "lexicon+in-box") for _, f in figs(d)),
        [(f.get("label"), f.get("caption_src")) for _, f in figs(d) if f.get("caption_src") in ("lexicon", "lexicon+in-box")])
    # polluted text layer (4.1): public gap report doc page 2, hidden report doc page 2
    for doc in ("gap-report-01", "hgap-report-01"):
        if doc not in GAPDOCS: continue
        gt = json.load(open(os.path.join(GAPDOCS[doc], "gt.json"))); d = P(os.path.join(GAPDOCS[doc], "doc.pdf"))["d"]; caps = gt_caps(gt)
        for p, g in gt_figs(gt):
            if not g.get("polluted"): continue
            cap = caps.get((p, g.get("label"))); hit = next((f for n, f in figs(d) if n == p and S._is_rect(f.get("caption_bbox")) and S.iou(f["caption_bbox"], cap["bbox"]) >= 0.5), None)
            add(f"L_polluted_{'public' if doc.startswith('gap') else 'hidden'}_in_box_match", bool(hit) and hit.get("caption_src") == "lexicon+in-box" and hit["caption_bbox"][0] >= cap["bbox"][0] - 1.0,
                (hit or {}).get("caption_src") if doc.startswith("gap") else bool(hit))

# ---------------------------------------------------------------- U: uncaptioned candidates in gated mode
@section("U")
def sec_uncaptioned():
    d = P(P6DOC["uncap"])["d"]; gt = P6GT["uncap"]; ev = events(d)
    unc = [(p, f) for p, f in figs(d) if f.get("captioned") is False]
    add("U_uncaptioned_bodies_become_targets_or_route", bool(unc) or any(e.get("route") for e in ev), ([f.get("label") for _, f in unc], [(e.get("route"), e.get("outcome")) for e in ev]))
    logo = next(b for b in gt["pages"][0]["blocks"] if b.get("decoy") == "logo"); timg = next(b for b in gt["pages"][0]["blocks"] if b.get("decoy") == "text-image")
    on = [(f.get("label")) for _, f in figs(d) if S._is_rect(f.get("bbox")) and (S.iou(f["bbox"], logo["bbox"]) >= 0.3 or S.iou(f["bbox"], timg["bbox"]) >= 0.3)]
    add("U_no_block_on_logo_or_text_image_decoy", not on, on)
    routed = [e for e in ev if e.get("stage") == "classifier" and e.get("route")]
    stuck = [e for e in routed if any(S.iou(b["bbox"], e["rect"]) >= 0.8 for _, b in blocks(d, e.get("page")) if b.get("kind") in ("figure", "table") and b.get("kind_src") != "caption")]
    add("U_routed_candidate_has_no_block_with_vlm_off", not stuck, [(e.get("rect"), e.get("route")) for e in stuck][:3])
    acc = [e for e in ev if e.get("stage") == "classifier" and e.get("outcome") == "accepted"]
    add("U_classifier_accepts_have_kind_src_classifier_blocks", all(any(b.get("kind_src") == "classifier" and S.iou(b["bbox"], e["rect"]) >= 0.5 for _, b in blocks(d, e.get("page"))) for e in acc),
        [e.get("rect") for e in acc][:3])
    add("U_stage_b_speed_budget", all((e.get("ms") or 0) <= 400 for e in ev if e.get("stage") == "classifier"), [e.get("ms") for e in ev][:8])
    bad = []; seen = collections.Counter()
    docs = [P6DOC["uncap"], P6DOC["lexpos"]] + [os.path.join(GAPDOCS[x], "doc.pdf") for x in sorted(GAPDOCS)]
    for pdf in docs:
        dd = P(pdf)["d"] or {}; evs = events(dd)
        for p in pages(dd):
            sk = (p.get("kinds") or {}).get("skipped") or {}; seen.update(sk)
            rs = [e["rect"] for e in evs if e.get("page") == p.get("n") and isrect(e.get("rect")) and e.get("anchor") != "area"]
            dup = sum(1 for i in range(len(rs)) for j in range(i + 1, len(rs)) if S.iou(rs[i], rs[j]) >= 0.5)
            inner = lambda big: sum(1 for x in rs if x is not big and S.area(x) > 0 and (max(0, min(x[2], big[2]) - max(x[0], big[0])) * max(0, min(x[3], big[3]) - max(x[1], big[1]))) >= 0.9 * S.area(x))
            cont = sum(1 for big in rs if inner(big) >= 2)
            tag = "public" if "/public-gap/" in pdf or pdf in (P6DOC["uncap"], P6DOC["lexpos"]) else "hidden"
            if sk.get("duplicate", 0) > dup: bad.append((tag, "duplicate", p.get("n"), sk.get("duplicate"), dup))
            if sk.get("container", 0) > cont: bad.append((tag, "container", p.get("n"), sk.get("container"), cont))
    add("U_duplicate_and_container_skips_match_candidate_geometry", not bad, bad[:4])
    add("U_skip_reasons_in_A2_4_enum", set(seen) <= SKIP_REASONS, dict(seen))
    info(f"U skipped reasons across parses {dict(seen)}")
    k = [p.get("kinds") for p in pages(d) if p.get("kinds")]
    info(f"U stage B ms per page {[x.get('ms') for x in k]} candidates {[x.get('candidates') for x in k]}")

# ---------------------------------------------------------------- C: classifier missing / mismatch / no onnxruntime
@section("C")
def sec_classifier_missing():
    bad_model = os.path.join(TMPROOT, "bad_figcls.onnx"); b = bytearray(open(FMODEL, "rb").read()); b[1000] ^= 0xFF; open(bad_model, "wb").write(bytes(b))
    for nm, kw, want in (("missing", NO_CLS, "model-missing"), ("mismatch", dict(SPEAKEASY_FIGCLS_MODEL=bad_model), "model-mismatch"), ("no_onnxruntime", dict(py=NOORT), "no-onnxruntime")):
        if nm == "no_onnxruntime" and NOORT_HAS_ORT: skip(f"C_{nm}", NOORT_SKIP); continue
        r = P(P6DOC["uncap"], **kw); d = r["d"] or {}; bc = d.get("blockcls") or {}; ev = events(d)
        add(f"C_{nm}_parse_exit0", r["rc"] == 0 and bool(d), (r["rc"], r["err"]))
        if nm != "no_onnxruntime": add(f"C_{nm}_status_unavailable_reason", bc.get("status") == "unavailable" and bc.get("reason") == want, bc)
        else: add(f"C_{nm}_status_unavailable", bc.get("status") == "unavailable" and bc.get("reason") in REASONS - {None, "disabled"}, bc)
        # Without onnxruntime the detector is unavailable too, so section 3 yields no candidates (section 10): nothing can route
        if nm == "no_onnxruntime" and not ev and not any((p.get("layout") or {}).get("src") == "detector" for p in pages(d)):
            skip(f"C_{nm}_every_candidate_routes_low_p", "without onnxruntime no page is detected, so there are no candidates to route (section 10)")
        else: add(f"C_{nm}_every_candidate_routes_low_p", bool(ev) and all(e.get("route") == "low-p" for e in ev if e.get("stage") == "classifier"), [(e.get("route"), e.get("outcome")) for e in ev][:5])
        add(f"C_{nm}_no_classifier_blocks_with_vlm_off", not any(f.get("kind_src") in ("classifier", "vlm") for _, f in figs(d)), [f.get("label") for _, f in figs(d) if f.get("kind_src") in ("classifier", "vlm")])
    d = P(P6DOC["lexpos"], **NO_CLS)["d"]
    add("C_missing_lexicon_still_runs", sum(1 for _, f in figs(d) if f.get("caption_src") in ("lexicon", "lexicon+in-box")) >= 6, [(f.get("label"), f.get("caption_src")) for _, f in figs(d)][:8])

# ---------------------------------------------------------------- D: determinism
@section("D")
def sec_determinism():
    for name in ("uncap", "lexpos"):
        a = parse(P6DOC[name])["d"]; b = parse(P6DOC[name])["d"]
        add(f"D_{name}_two_parses_identical_except_ms", bool(a) and diff(strip_volatile(a), strip_volatile(b)) == [], diff(strip_volatile(a), strip_volatile(b))[:4])
    shas = []
    for i in range(2):
        o = finish(speak(f"D_crops{i}", P6DOC["uncap"], P6CASES["uncap"][0], mock_env=fixed([("C", 0.97)]), env=dict(KIND_ENV, **NO_CLS)))
        shas.append(sorted(r.get("png_sha") for r in o["kinds"]))
    add("D_crop_bytes_identical_across_jobs", shas[0] == shas[1] and bool(shas[0]), (len(shas[0]), len(shas[1])))

# ---------------------------------------------------------------- V: stage C outcomes and fallbacks
def vrun(name, mock_env, env_extra=None, doc="uncap", wait=True):
    o = finish(speak(name, P6DOC[doc], P6CASES[doc][0], mock_env=mock_env, env=dict(KIND_ENV, **NO_CLS, **(env_extra or {}))), wait=wait)
    run_ok(name, o); return o
def outcomes(o): return collections.Counter(r.get("outcome") for r in o["bk"] if r.get("stage") == "vlm")
def second_run_blocks(o, doc="uncap"):
    o2 = finish(speak(o["name"] + "_reload", P6DOC[doc], P6CASES[doc][1 if len(P6CASES[doc]) > 1 else 0], home=o["home"], mock_env={"MOCK_KIND": "status:500"},
                      env=dict(KIND_ENV, **NO_CLS)), wait=False)
    dp = o2["rec"].get("doc_parse") or {}
    return o2, (dp.get("blocks") or {})

@section("V")
def sec_stage_c():
    gated = finish(speak("V_gated", P6DOC["uncap"], P6CASES["uncap"][1], mock_env={}, env=dict(NO_CLS)), wait=False)
    gb = (gated["rec"].get("doc_parse") or {}).get("blocks") or {}
    T_C = (P(P6DOC["uncap"], **NO_CLS)["d"] or {}).get("blockcls", {}).get("T_C") or 0.8
    info(f"V T_C {T_C}; gated blocks {gb}")
    o = vrun("V_accept_table", fixed([("C", 0.97), ("A", 0.02)]))
    oc = outcomes(o); add("V_accept_outcome_accepted", oc.get("accepted", 0) >= 1 and set(oc) <= {"accepted", "skipped"}, dict(oc))
    add("V_accept_job_final_within_75s", (o.get("job") or {}).get("status") == "done", (o.get("job") or {}).get("status"))
    sidecar_checks("V_accept", o, ("done",))
    kj = o["rec"].get("kind_job")
    add("V_accept_runs_kind_job_schema", isinstance(kj, dict) and {"started", "routed", "called", "accepted", "outcomes", "breaker", "ms"} <= set(kj) and kj.get("started") is True, kj)
    reqs = [r for r in o["mock"] if r.get("method") == "POST" and "/api/chat" in str(r.get("path")) and r.get("n_images")]
    shape = [(r.get("options"), r.get("stream"), r.get("keep_alive"), r.get("model"), r.get("png_dims")) for r in reqs]
    kr = o["kinds"]
    add("V_request_shape_6_2", bool(kr) and all(r.get("logprobs_req") is True and r.get("top_logprobs_req") == 20 and (r.get("options") or {}).get("temperature") == 0
                                                   and (r.get("options") or {}).get("num_predict") == 1 and r.get("stream") is False and r.get("keep_alive") not in (None, "")
                                                   and r.get("model") == VMODEL for r in kr), [(r.get("options"), r.get("stream"), r.get("keep_alive"), r.get("model")) for r in kr][:3])
    dims = [max(int(x) for x in dm.split("x")) for r in reqs for dm in (r.get("png_dims") or [])]
    add("V_crop_long_side_448_to_1024", bool(dims) and all(448 <= v <= 1024 for v in dims) and len(set(dims)) == 1, dims[:6])
    iv = sorted((r["t0"], r["t1"]) for r in kr)
    add("V_one_request_at_a_time", bool(iv) and all(iv[i][1] <= iv[i + 1][0] + 0.005 for i in range(len(iv) - 1)), iv[:4])
    pg = collections.Counter(r.get("page") for r in o["bk"] if r.get("stage") == "vlm" and r.get("outcome") != "skipped")
    add("V_budget_4_per_page_16_per_job", bool(pg) and all(v <= 4 for v in pg.values()) and sum(pg.values()) <= 16 and len(kr) <= 16, dict(pg))
    kind_record_checks("V_accept", o["bk"], text_words(os.path.dirname(P6DOC["uncap"])))
    o2, nb = second_run_blocks(o)
    add("V_accepted_answers_become_blocks_on_reload", (nb.get("table", 0) + nb.get("figure", 0)) > (gb.get("table", 0) + gb.get("figure", 0)), (gb, nb))
    add("V_cache_hit_runs_nothing", bool(kr) and not o2["kinds"], (len(kr), len(o2["kinds"])))
    add("V_sidecar_written_once", bool(o.get("job", {}).get("jobs")) and sidecars_changed(o["job"]) == [], sidecars_changed(o.get("job") or {}))
    o = vrun("V_pA_plus_pB_is_figure", fixed([("A", 0.5), ("B", 0.47), ("C", 0.02)]))
    fr = [r for r in o["bk"] if r.get("stage") == "vlm" and r.get("outcome") == "accepted"]
    add("V_pA_plus_pB_accepted_as_figure", bool(fr) and all(r.get("kind") == "figure" and abs((r.get("p_top") or 0) - 0.97) <= 0.011 for r in fr), [(r.get("kind"), r.get("p_top")) for r in fr][:3])
    o = vrun("V_letter_merge", fixed([("A", 0.5), (" A", 0.47), ("C", 0.02)]))
    fr = [r for r in o["bk"] if r.get("stage") == "vlm" and r.get("outcome") == "accepted"]
    add("V_A_and_space_A_merge", bool(fr) and all(r.get("kind") == "figure" and (r.get("p_top") or 0) >= 0.96 for r in fr), [(r.get("kind"), r.get("p_top")) for r in fr][:3])
    o = vrun("V_text", fixed([("D", 0.97), ("C", 0.02)])); oc = outcomes(o); sidecar_checks("V_text", o)
    add("V_text_outcome_text", oc.get("text", 0) >= 1 and "accepted" not in oc, dict(oc))
    _, nb = second_run_blocks(o); add("V_text_creates_no_block", bool(o["kinds"]) and (nb.get("table", 0) + nb.get("figure", 0)) == (gb.get("table", 0) + gb.get("figure", 0)), (gb, nb))
    o = vrun("V_none_letter_E", fixed([("E", 0.97), ("C", 0.02)])); oc = outcomes(o)
    add("V_E_no_block_outcome", not ({"accepted", "text"} & set(oc)) and bool(oc), dict(oc))
    o = vrun("V_off_menu", fixed([("The", 0.6), ("C", 0.3)])); oc = outcomes(o)
    add("V_letter_mass_below_half_off_menu", oc.get("off-menu", 0) >= 1 and set(oc) <= {"off-menu", "skipped"}, dict(oc))
    rest = (1 - (T_C - 0.04)) / 3
    o = vrun("V_low_confidence", fixed([("C", T_C - 0.04), ("A", rest), ("D", rest), ("E", rest)])); oc = outcomes(o)
    add("V_below_T_C_low_confidence", oc.get("low-confidence", 0) >= 1 and set(oc) <= {"low-confidence", "skipped"}, dict(oc))
    for nm, me, want in (("no_logprobs", {"MOCK_KIND": "none"}, "no-logprobs"), ("http503", {"MOCK_KIND": "status:503"}, "unavailable"),
                         ("model_not_pulled_404", {"MOCK_KIND": "status:404"}, "unavailable"), ("reset", {"MOCK_KIND": "reset"}, "unavailable")):
        o = vrun(f"V_{nm}", me); oc = outcomes(o); kj = o["rec"].get("kind_job") or {}
        sidecar_checks(f"V_{nm}", o, breaker=want if nm != "reset" else None)
        okw = oc.get(want, 0) >= 1 if nm != "reset" else (oc.get("unavailable", 0) + oc.get("error", 0)) >= 1
        add(f"V_{nm}_outcome_{want}", okw and "accepted" not in oc, dict(oc))
        add(f"V_{nm}_breaker_one_request", len(o["kinds"]) <= 1 and (nm == "reset" or kj.get("breaker") == want), (len(o["kinds"]), kj.get("breaker")))
        _, nb = second_run_blocks(o); add(f"V_{nm}_no_block", bool(o["kinds"]) and (nb.get("table", 0) + nb.get("figure", 0)) == (gb.get("table", 0) + gb.get("figure", 0)), (gb, nb))
    o = vrun("V_malformed", {"MOCK_KIND": "malformed"}); oc = outcomes(o)
    add("V_malformed_outcome_error", oc.get("error", 0) >= 1 and "accepted" not in oc, dict(oc)); sidecar_checks("V_malformed", o)
    o = vrun("V_delay9", dict(fixed([("C", 0.97)]), MOCK_KIND_DELAY="9")); oc = outcomes(o)
    add("V_reply_after_8s_timeout", oc.get("timeout", 0) >= 1 and "accepted" not in oc, dict(oc)); sidecar_checks("V_delay9", o)
    _, nb = second_run_blocks(o); add("V_late_reply_ignored_no_block", bool(o["kinds"]) and (nb.get("table", 0) + nb.get("figure", 0)) == (gb.get("table", 0) + gb.get("figure", 0)), (gb, nb))
    for nm, ex in (("ground_off", {"SPEAKEASY_GROUND": "off"}), ("vlm_off", {"SPEAKEASY_BLOCKCLS_VLM": "off"})):
        o = vrun(f"V_{nm}", fixed([("C", 0.97)]), ex, wait=False)
        kjs = kind_jobs(o["home"])
        add(f"V_{nm}_no_request_kind_job_none_no_sidecar", not o["kinds"] and bool(kjs) and set(kjs.values()) == {"none"}
            and not glob.glob(os.path.join(o["home"], "cache", "doc", "*.kinds.json")), (len(o["kinds"]), kjs))

# ---------------------------------------------------------------- N: network
@section("N")
def sec_network():
    n0 = len(TRAP.lines)
    o = vrun("N_local", fixed([("C", 0.97)]))
    add("N_local_nothing_leaves_loopback", bool(o["kinds"]) and len(TRAP.lines) == n0, (len(o["kinds"]), TRAP.lines[n0:][:3]))
    orenv = dict(SPEAKEASY_GROUND="openrouter", SPEAKEASY_OR_KEY="sk-or-test-not-a-key", SPEAKEASY_OR_TIMEOUT="2")
    n0 = len(TRAP.lines); o = vrun("N_vlm_local_with_cloud_opt_in", {"MOCK_KIND": "none"}, dict(orenv, SPEAKEASY_BLOCKCLS_VLM="local"))
    add("N_blockcls_vlm_local_never_openrouter", not any("openrouter" in l for l in TRAP.lines[n0:]) and not any(r.get("provider") == "openrouter" for r in o["bk"]), TRAP.lines[n0:][:3])
    n0 = len(TRAP.lines); o = vrun("N_auto_breaker_cloud", {"MOCK_KIND": "none"}, dict(orenv, SPEAKEASY_BLOCKCLS_VLM="auto"))
    hit = [l for l in TRAP.lines[n0:] if "openrouter" in l.lower()]
    add("N_auto_opt_in_breaker_tries_openrouter_only_then", bool(hit) or any(r.get("provider") == "openrouter" for r in o["bk"]), (TRAP.lines[n0:][:3], outcomes(o)))
    n0 = len(TRAP.lines); o = vrun("N_auto_no_opt_in", {"MOCK_KIND": "none"}, dict(SPEAKEASY_BLOCKCLS_VLM="auto", SPEAKEASY_OR_KEY="sk-or-test-not-a-key"))
    add("N_auto_without_cloud_grounding_stays_local", len(TRAP.lines) == n0, TRAP.lines[n0:][:3])

# ---------------------------------------------------------------- O: OCR helper missing, image tables
@section("O")
def sec_ocr():
    for doc in [k for k in GAPDOCS if "scan" in k]:
        d = P(os.path.join(GAPDOCS[doc], "doc.pdf"))["d"] or {}; tag = "public" if doc.startswith("gap") else "hidden"
        nt = [p for p in pages(d) if (p.get("kinds") or {}).get("no_text")]
        add(f"O_{tag}_scan_pages_flagged_no_text", len(nt) >= 2, [(p.get("n"), (p.get("kinds") or {}).get("no_text")) for p in pages(d)])
        blob = json.dumps(d)
        add(f"O_{tag}_ocr_unavailable_reported", '"ocr": "unavailable"' in blob, "no ocr: unavailable value in the parse output")
        add(f"O_{tag}_scan_bodies_stay_uncaptioned", sum(1 for n, f in figs(d) if n in {p.get("n") for p in nt}) >= 3 and not any(f.get("captioned") for n, f in figs(d) if n in {p.get("n") for p in nt}), [(n, f.get("label")) for n, f in figs(d) if f.get("captioned")][:4])
        add(f"O_{tag}_no_paragraph_blocks_on_scans", not any(b.get("kind") == "paragraph" for n, b in blocks(d) if n in {p.get("n") for p in nt}), "")
    # A2.6 helper resolution: SPEAKEASY_OCR_BIN if set (not executable -> unavailable, no fall-through), else SPEAKEASY_TRACKER, else
    # $SPEAKEASY_HOME/bin/speakeasy-track; always `<path> ocr IN.png OUT.json` (A1.1 format and codes); nothing left in TMPDIR
    if "gap-scan-01" in GAPDOCS:
        pdf = os.path.join(GAPDOCS["gap-scan-01"], "doc.pdf"); fdir = os.path.join(TMPROOT, "fake-ocr"); os.makedirs(fdir, exist_ok=True)
        def helper(nm, code=0, path=None):
            argl = os.path.join(fdir, f"{nm}.jsonl"); hp = path or os.path.join(fdir, f"{nm}.py"); os.makedirs(os.path.dirname(hp), exist_ok=True)
            open(hp, "w").write(fake_ocr_source(argl, code)); os.chmod(hp, 0o755); return hp, argl
        nonexec = os.path.join(fdir, "nonexec.py"); open(nonexec, "w").write(fake_ocr_source(os.path.join(fdir, "nonexec.jsonl"), 0)); os.chmod(nonexec, 0o644)
        cases = []
        hp, al = helper("bin_ok"); cases.append(("ocr_bin_ok", dict(SPEAKEASY_OCR_BIN=hp), al, [], False))
        hp, al = helper("bin_vision_error", 4); cases.append(("ocr_bin_exit4", dict(SPEAKEASY_OCR_BIN=hp), al, [], True))
        tp, tl = helper("tracker_a"); cases.append(("ocr_bin_not_executable_no_fallthrough", dict(SPEAKEASY_OCR_BIN=nonexec, SPEAKEASY_TRACKER=tp), None, [tl], True))
        tp, tl = helper("tracker_b"); cases.append(("ocr_bin_missing_no_fallthrough", dict(SPEAKEASY_OCR_BIN=os.path.join(fdir, "absent"), SPEAKEASY_TRACKER=tp), None, [tl], True))
        tp, tl = helper("tracker_c"); cases.append(("tracker_when_ocr_bin_unset", dict(SPEAKEASY_OCR_BIN=None, SPEAKEASY_TRACKER=tp), tl, [], False))
        hh = mkhome("ocr_home_bin"); hp, hl = helper("home_bin", path=os.path.join(hh, "bin", "speakeasy-track"))
        cases.append(("home_bin_when_both_unset", dict(SPEAKEASY_OCR_BIN=None, SPEAKEASY_TRACKER=None, home=hh), hl, [], False))
        cases.append(("gated_nothing_resolves", dict(SPEAKEASY_OCR_BIN=None), None, [], True))
        for nm, kw, called, not_called, unavailable in cases:
            r = parse(pdf, **kw); d = r["d"] or {}; blob = json.dumps(d); calls = jl(called) if called else []
            tmpd = os.path.join(os.path.dirname(r["home"]), "tmp"); left = [f for f in os.listdir(tmpd) if not f.startswith(".")] if os.path.isdir(tmpd) else []
            add(f"O_{nm}_parse_exit0", r["rc"] == 0 and bool(d), (r["rc"], r["err"]))
            if called: add(f"O_{nm}_called_as_path_ocr_in_out", bool(calls) and all(len(c["argv"]) == 3 and c["argv"][0] == "ocr" and c["png"] for c in calls), calls[:2])
            for nl in not_called: add(f"O_{nm}_other_helper_not_called", not jl(nl), len(jl(nl)))
            add(f"O_{nm}_{'ocr_unavailable' if unavailable else 'ocr_not_unavailable'}", ('"ocr": "unavailable"' in blob) == unavailable, (r["rc"], len(calls)))
            add(f"O_{nm}_nothing_left_in_tmpdir", not left, left[:4])
    for doc in [k for k in GAPDOCS if "imgtable" in k]:
        gt = json.load(open(os.path.join(GAPDOCS[doc], "gt.json"))); d = P(os.path.join(GAPDOCS[doc], "doc.pdf"))["d"] or {}; tag = "public" if doc.startswith("gap") else "hidden"
        hits = []
        for p, g in gt_figs(gt):
            if g.get("gap") != "image-table": continue
            f = next((f for n, f in figs(d) if n == p and S._is_rect(f.get("bbox")) and S.iou(f["bbox"], g["bbox"]) >= 0.5), None)
            hits.append(bool(f) and str(f.get("label", "")).startswith("Table") and f.get("rows_src") == "none" and not f.get("ocr_rows"))
        add(f"O_{tag}_image_tables_without_ocr_keep_box_rows_src_none", bool(hits) and all(hits), hits)

# ---------------------------------------------------------------- T: header tie on OCR-row tables (A2.3)
@section("T")
def sec_header_tie():
    gt = P6GT["ocrtie"]; tb = next(b for b in gt["pages"][0]["blocks"] if b["kind"] == "table")
    rows_tok = [" ".join(r) for r in [["Depot", "Crates", "Pallets"], ["North", "42", "12"], ["Quay", "37", "9"], ["Inland", "55", "16"]]]
    argl = os.path.join(TMPROOT, "tie_ocr.jsonl"); hp = os.path.join(TMPROOT, "tie_ocr.py")
    open(hp, "w").write(fake_ocr_source(argl, 0, {"bbox": tb["bbox"], "rows": tb["rows"], "tokens": rows_tok})); os.chmod(hp, 0o755)
    d = parse(P6DOC["ocrtie"], SPEAKEASY_OCR_BIN=hp)["d"] or {}
    f = next((f for n, f in figs(d) if isrect(f.get("bbox")) and S.iou(f["bbox"], tb["bbox"]) >= 0.5), None)
    add("T_image_table_rows_from_ocr_lines", bool(f) and f.get("rows_src") == "ocr-lines" and len(f.get("ocr_rows") or []) >= 4, (f or {}).get("rows_src") if f else "no table")
    case = next(c for c in P6CASES["ocrtie"] if c["case_id"].endswith("/r01")); sents = {x["id"]: x for x in gt["sentences"]}
    # Like run_bench: a warm-up run fills the parse cache (the document job with its OCR rows), then the measured run must have the
    # document ready before the first sentence (doc_ready_ms <= first_sentence_ms), retried up to 2 times; otherwise rows cannot map.
    home = mkhome("T_tie"); finish(speak("T_tie_warm", P6DOC["ocrtie"], case, home=home, env={"SPEAKEASY_OCR_BIN": hp}), wait=False)
    for attempt in range(3):
        o = finish(speak("T_tie", P6DOC["ocrtie"], case, home=home, env={"SPEAKEASY_OCR_BIN": hp}), wait=False)
        dr, fs = o["rec"].get("doc_ready_ms"), o["rec"].get("first_sentence_ms")
        if dr is not None and fs is not None and dr <= fs: break
    run_ok("T_tie", o)
    add("T_tie_doc_ready_before_first_sentence", dr is not None and fs is not None and dr <= fs, (dr, fs, attempt))
    info(f"T measured run: attempt {attempt} doc_ready_ms {dr} first_sentence_ms {fs} doc_parse.blocks {(o['rec'].get('doc_parse') or {}).get('blocks')} "
         f"figure_mode {o['rec'].get('doc_figure_mode')} shows {[(r.get('sent'), r.get('cue_kind'), (r.get('doc') or {}).get('kind'), (r.get('doc') or {}).get('target_src'), (r.get('doc') or {}).get('target')) for r in o['focus'] if r.get('state') == 'show']} "
         f"maps {[(r.get('sent'), r.get('method'), r.get('score'), r.get('runner_up')) for r in o['focus'] if r.get('state') == 'map']}")
    maps = [r for r in o["focus"] if r.get("state") == "map"]; shows = [r for r in o["focus"] if r.get("state") == "show"]
    try: owner = S.attribute([(x["sentence_id"], x["text"]) for x in case["script"]], maps)
    except Exception as e: add("T_tie_attribution", False, str(e)); return
    cs = S.chunk_shows(shows); res = []
    for x in case["script"]:
        sh = [cs.get(k) for k, sid in sorted(owner.items()) if sid == x["sentence_id"]]
        res.append(S.score_row_sentence(sents[x["sentence_id"]], sh))
    # diagnostics only (INFO, not checks): does a single data-row token, or a strict data-row win, get a row band on this table?
    # single_token_tie: the old 1-vs-1 fixture tie (1/3 of each row, below the 0.4 row-match threshold); per the A2.3 ruling the
    # whole-table cue is correct there, so it is reported, not asserted (the asserted tie is now 2 vs 2, make_p6docs.py r01)
    for tag, txts in (("single_token_tie", ["Depot, Crates and Pallets head the columns.", "Crates at Quay."]),
                      ("single_data_token", ["Depot, Crates and Pallets head the columns.", "Quay is next."]),
                      ("strict_data_win", ["Depot, Crates and Pallets head the columns.", "Quay holds 37 crates and 9 pallets."])):
        pc = dict(case, script=[{"sentence_id": f"d{i}", "text": t} for i, t in enumerate(txts)])
        po = finish(speak(f"T_probe_{tag}", P6DOC["ocrtie"], pc, home=home, env={"SPEAKEASY_OCR_BIN": hp}), wait=False)
        info(f"T probe {tag}: maps {[(r.get('sent'), r.get('method')) for r in po['focus'] if r.get('state') == 'map']} "
             f"shows {[(r.get('sent'), r.get('cue_kind'), (r.get('doc') or {}).get('target')) for r in po['focus'] if r.get('state') == 'show']}")
    add("T_header_wins_only_when_strictly_higher", res[0][0], res[0])
    add("T_tie_between_header_and_data_row_goes_to_the_data_row", res[1][0], res[1])


# ---------------------------------------------------------------- A: Read-area capture
@section("A")
def sec_area():
    for doc in [k for k in GAPDOCS if "scan" in k]:
        cases = [c for c in json.load(open(os.path.join(GAPDOCS[doc], "cases.json"))) if c.get("area")]; tag = "public" if doc.startswith("gap") else "hidden"
        gt = json.load(open(os.path.join(GAPDOCS[doc], "gt.json"))); areas = {a["id"]: a for a in gt["areas"]}
        for c in cases[:2]:
            o = finish(speak(f"A_{tag}_{c['area']}", None, None, area=(os.path.join(GAPDOCS[doc], c["capture"]), c)), wait=False)
            rec = o["rec"]; ak = rec.get("area_kinds"); a = areas[c["area"]]
            run_ok(f"A_{tag}_{c['area']}", o)
            add(f"A_{tag}_{c['area']}_area_pdf_not_ok", rec.get("area_pdf") not in (None, "ok"), rec.get("area_pdf"))
            add(f"A_{tag}_{c['area']}_area_kinds_schema", isinstance(ak, dict) and {"candidates", "figure", "table", "outcomes", "ms"} <= set(ak)
                and all(isrect(r) and r[2] <= a["w"] + 1 and r[3] <= a["h"] + 1 for k in ("figure", "table") for r in ak.get(k) or []), ak)
            add(f"A_{tag}_{c['area']}_no_fig_id", "fig_id" not in json.dumps(ak or {}) and not rec.get("fig_ids"), rec.get("fig_ids"))
            add(f"A_{tag}_{c['area']}_area_kinds_text_free", not any_text_values(ak or {}), any_text_values(ak or {})[:3])
    sc = [k for k in GAPDOCS if "scan" in k and k.startswith("gap")]
    if sc:   # A2.5: stage C does not run for area candidates; routed ones are skipped (area-no-vlm) and get no cue
        c = next(c for c in json.load(open(os.path.join(GAPDOCS[sc[0]], "cases.json"))) if c.get("area"))
        o = finish(speak("A_area_no_vlm", None, None, area=(os.path.join(GAPDOCS[sc[0]], c["capture"]), c), mock_env=fixed([("C", 0.97)]), env=dict(KIND_ENV, **NO_CLS)), wait=False)
        ak = o["rec"].get("area_kinds") or {}; oc = ak.get("outcomes") or {}
        add("A_area_routed_candidates_send_no_kind_request", not o["kinds"], len(o["kinds"]))
        add("A_area_routed_candidates_skipped_without_cue", isinstance(ak, dict) and oc.get("skipped", 0) >= 1 and not oc.get("accepted") and not (ak.get("figure") or ak.get("table")), ak)
    doc = "gap-report-01"
    if doc in GAPDOCS:
        pdf = os.path.join(GAPDOCS[doc], "doc.pdf"); d = P(pdf)["d"]; pg = next(p for p in pages(d) if p.get("n") == 1); area = [50, 50, 545, 345]
        # an area over the page's top text (Phase 5 bench geometry, the app's OCR line-bounds format): area_pdf ok, capture result discarded
        from geom import page_to_screen, xywh, K
        ls = sorted([l for l in pg.get("lines") or [] if isinstance(l, list) and len(l) >= 5 and area[0] <= (l[0] + l[2]) / 2 <= area[2] and area[1] <= (l[1] + l[3]) / 2 <= area[3]],
                    key=lambda l: (round((l[1] + l[3]) / 2 / 3), l[0]))
        s_ = 1.25 * K; g = {"zoom": 1.25, "backing": 2, "ox": (1512 - pg["w"] * s_) / 2, "oy": 300 - area[1] * s_}
        lb = []; loc = 0
        for l in ls: lb.append({"loc": loc, "abs": loc, "len": len(l[4]), "rect": [round(v, 1) for v in xywh(page_to_screen(l[:4], g))]}); loc += len(l[4]) + 1
        case = json.load(open(os.path.join(GAPDOCS[doc], "cases.json")))[0]
        acase = {"line_bounds": lb, "selection": [{"text": l[4]} for l in ls], "capture_rect": [round(v, 1) for v in xywh(page_to_screen(area, g))], "script": case["script"]}
        png = os.path.join(GAPDOCS["gap-scan-01"], "area-a1.png") if "gap-scan-01" in GAPDOCS else None
        home = mkhome("A_pdf_ok"); cp = os.path.join(os.path.dirname(home), "doc.pdf"); shutil.copyfile(pdf, cp)
        o = finish(speak("A_area_pdf_ok", pdf, None, home=home, area=(png, acase), hint={"app": "Preview", "bundle": "com.apple.Preview", "title": "doc.pdf", "ax_document": "file://" + cp}), wait=False)
        rec = o["rec"]
        if rec.get("area_pdf") == "ok": add("A_area_pdf_ok_capture_result_discarded", not rec.get("area_kinds"), rec.get("area_kinds"))
        else: skip("A_area_pdf_ok_capture_result_discarded", f"area_pdf {rec.get('area_pdf')} (the area did not resolve to the page)")

_route_info = {}
def stage_c_route_info():
    """(default_off, hidden_routed). default_off: with SPEAKEASY_BLOCKCLS_VLM unset and grounding local, a parse whose candidates all
    route (classifier absent) records kind_job "none" (None when nothing routes). hidden_routed: routed candidates over the hidden gap
    set in the gated parse (blockcls.routed). A hidden kind cassette only exists when the hidden set sends stage C requests."""
    if not _route_info:
        d = parse(P6DOC["uncap"], SPEAKEASY_BLOCKCLS_VLM=None, SPEAKEASY_GROUND="local", **NO_CLS)["d"] or {}
        bc = d.get("blockcls") or {}
        _route_info["default_off"] = (bc.get("kind_job") == "none") if isint(bc.get("routed")) and bc["routed"] > 0 else None
        n = 0
        for k in sorted(GAPDOCS):
            if k.startswith("hgap"):
                dd = P(os.path.join(GAPDOCS[k], "doc.pdf"))["d"] or {}; r = (dd.get("blockcls") or {}).get("routed")
                n += r if isint(r) else sum((p.get("kinds") or {}).get("routed") or 0 for p in pages(dd))
        _route_info["hidden_routed"] = n
    return _route_info["default_off"], _route_info["hidden_routed"]
def hidden_kind_cassette_moot():
    """Skip reason for the hidden kind-cassette checks, or None: stage C default-off and no routed hidden candidate means the hidden
    bench sends no stage C request, so no hidden kind cassette can be recorded."""
    off, n = stage_c_route_info()
    return f"stage C is default-off and the hidden set routes {n} candidates (no stage C request, so no hidden kind cassette)" if off and n == 0 else None


# ---------------------------------------------------------------- P: privacy and scrub
@section("P")
def sec_privacy():
    words = text_words(os.path.dirname(P6DOC["uncap"]))
    for lt in ("1", None):
        o = vrun(f"P_logtext_{lt or 'unset'}", fixed([("C", 0.97)]), {"SPEAKEASY_LOG_TEXT": lt})
        rec = o["rec"]; pieces = {k: rec.get(k) for k in ("blockcls_events", "kind_job", "area_kinds")}; pieces["doc_parse.kinds"] = (rec.get("doc_parse") or {}).get("kinds")
        blob = free_text_blob(pieces) + "\n" + free_text_blob(o["bk"])   # enum values are not document text; their dict keys are pinned below
        leak = sorted(w for w in words if w.lower() in blob); ek = enum_dict_keys_ok(pieces)
        add(f"P_logtext_{lt or 'unset'}_no_document_text_in_phase6_fields", isinstance(rec.get("kind_job"), dict) and bool(o["bk"]) and not leak and not ek and not any_text_values(pieces),
            (isinstance(rec.get("kind_job"), dict), len(o["bk"]), leak[:5], ek[:3], any_text_values(pieces)[:3]))
        add(f"P_logtext_{lt or 'unset'}_no_crop_survives", not o["leftovers"] and not glob.glob(os.path.join(o["home"], "**", "speakeasy_cap_kind_*"), recursive=True), o["leftovers"])
    home = o["home"]
    env = penv(home, SPEAKEASY_LOG_TEXT="1", PATH=":".join([STUBD] + STUBS + [os.environ.get("PATH", "/usr/bin:/bin")]))
    r1 = subprocess.run([CLI, "logs", "--scrub", "--yes"], capture_output=True, text=True, env=env, timeout=60)
    r2 = subprocess.run([CLI, "logs", "--scrub", "--yes"], capture_output=True, text=True, env=env, timeout=60)
    ev = [r for r in jl(os.path.join(home, "logs", "runs.jsonl")) if r.get("command") == "scrub"]
    add("P_scrub_runs_twice_exit0", r1.returncode == 0 and r2.returncode == 0 and len(ev) >= 2, (r1.returncode, r2.returncode, r1.stderr[-200:]))
    if len(ev) >= 2:
        add("P_scrub_second_run_changes_nothing", sum((ev[-1].get("records_changed") or {}).values()) == 0, ev[-1].get("records_changed"))
        add("P_scrub_blockcls_scan_reports_zero", ev[-1].get("other_logs_with_text") == 0, ev[-1])
    for cas in [os.path.join(BENCH, "kind_cassette.json")] + ([os.path.join(HOLD, "gap", "kind_cassette.json")] if HOLD else []):
        if not os.path.exists(cas):
            moot = None if cas.startswith(BENCH) else hidden_kind_cassette_moot()
            skip(f"P_kind_cassette_{'public' if cas.startswith(BENCH) else 'hidden'}_no_text", moot or "not recorded yet"); continue
        c = json.load(open(cas)); strs = any_text_values(c.get("entries"), allow=("token",))
        add(f"P_kind_cassette_{'public' if cas.startswith(BENCH) else 'hidden'}_no_text", not strs and all(len(k) == 64 for k in c.get("entries", {})), strs[:3])

# ---------------------------------------------------------------- H: static install / licence / no hex
@section("H")
def sec_static():
    bc = os.path.join(SRC, "blockcls.py")
    add("H_blockcls_module_exists", os.path.isfile(bc), bc)
    if os.path.isfile(bc):
        src = open(bc).read()
        add("H_no_hex_colour_literal_in_blockcls", not re.search(r"#[0-9A-Fa-f]{6}\b|0x[0-9A-Fa-f]{6}\b", src), re.findall(r"#[0-9A-Fa-f]{6}\b|0x[0-9A-Fa-f]{6}\b", src)[:3])
    # The installers fetch through `blockcls.py fetch`, which holds the pins (contract section 2): the pinned revision URL, the sha and
    # the size live in blockcls.py (never `main`), and each script must invoke that fetch.
    pin = "f859dfbff5c9916cd996942d4b0db7fa25808220"
    bsrc = open(bc).read() if os.path.isfile(bc) else ""
    url = f"docling-project/DocumentFigureClassifier-v2.5/resolve/{pin}/model.onnx"
    joined = re.sub(r"""["']\s*\n\s*["']""", "", bsrc)        # adjacent string literals split across lines
    pins = {"revision": f"https://huggingface.co/docling-project/DocumentFigureClassifier-v2.5/resolve/{pin}/model.onnx" in joined,
            "sha256": FIGCLS_SHA in bsrc, "size": "16940439" in bsrc.replace("_", ""), "never_main": "resolve/main" not in bsrc}
    add("H_blockcls_fetch_pins_revision_sha_size", all(pins.values()), pins)
    add("H_blockcls_has_fetch_entry", bool(re.search(r"""["']fetch["']""", bsrc)), "no fetch subcommand in blockcls.py")
    for f in ("src/install.sh", "app/bundle_python.sh"):
        t = open(os.path.join(REPO, f)).read() if os.path.exists(os.path.join(REPO, f)) else ""
        add(f"H_{os.path.basename(f)}_invokes_blockcls_fetch", bool(re.search(r"""blockcls\.py["']?\s+fetch\s""", t)), f)
    for f in ("app/NOTICE", "README.md"):
        t = open(os.path.join(REPO, f)).read() if os.path.exists(os.path.join(REPO, f)) else ""
        add(f"H_{os.path.basename(f)}_licence_line", "DocumentFigureClassifier" in t and "Docling" in t and "MIT" in t, f)

# ---------------------------------------------------------------- B: bench acceptance 14.1-14.3 (opt-in: --with-bench, long)
@section("B")
def sec_bench():
    if not A.with_bench: skip("B_bench", "pass --with-bench (runs the gated bench on both families and the replay bench)"); return
    rb = [DETPY, os.path.join(BENCH, "run_bench.py")]; gate = [DETPY, os.path.join(BENCH, "bench_gate.py")]; out = os.path.join(TMPROOT, "bench"); os.makedirs(out, exist_ok=True)
    def run(args, tag):
        t = time.time(); p = subprocess.run(rb + args, capture_output=True, text=True, timeout=7200, cwd=REPO); info(f"B {tag} exit {p.returncode} in {time.time() - t:.0f}s")
        return p
    # 14.1 existing families vs B5'
    sets = ["--set", "all", "--holdout", HOLD] if HOLD else ["--set", "public"]
    p = run(sets + ["--layout-model", LMODEL, "--figcls-model", FMODEL, "--layout2-model", L2MODEL, "--check-determinism", "--out", os.path.join(out, "all.json"),
             "--last-cases-out", os.path.join(out, "hidden_cases.json")], "existing families")
    add("B_14_1_bench_exit0_determinism", p.returncode == 0, p.stderr[-300:])
    g = subprocess.run(gate + [os.path.join(out, "all.json")], capture_output=True, text=True)
    add("B_14_1_gate_pass_vs_B5prime", g.returncode == 0 and "GATE PASS" in g.stdout, [l for l in g.stdout.splitlines() if "FAIL" in l][:6])
    b5p = os.path.join(os.path.dirname(HOLD), "phase6", "b5prime", "b5prime_bench.json") if HOLD else ""
    if os.path.exists(b5p) and os.path.exists(os.path.join(out, "all.json")):
        old = json.load(open(b5p)); new = json.load(open(os.path.join(out, "all.json")))
        rise = [(s, k, old["sets"][s]["boxes"]["wrong_type"][k]["n"], new["sets"][s]["boxes"]["wrong_type"][k]["n"]) for s in new["sets"] for k in ("figure", "table")
                if new["sets"][s]["boxes"]["wrong_type"][k]["n"] > old["sets"][s]["boxes"]["wrong_type"][k]["n"]]
        add("B_14_1_wrong_type_figure_table_not_rising", not rise, rise)
    else: skip("B_14_1_wrong_type_figure_table_not_rising", "no B5' bench at <holdout>/phase6/b5prime/b5prime_bench.json")
    # 14.2 gap family, gated
    if os.path.exists(os.path.join(out, "all.json")):
        al = json.load(open(os.path.join(out, "all.json")))
        # A5.1 pin update: routed == 0 on the existing families is reported (INFO), no longer asserted; the layout2 block must exist
        add("B_A3_6_existing_families_report_layout2", all("layout2" in b for b in al["sets"].values()))
        routed_by_set = {s_: (b.get("layout2") or {}).get("routed") for s_, b in al["sets"].items()}
        info(f"B existing-family layout2 routed {routed_by_set}")
    p = run(["--family", "gap"] + sets + ["--layout-model", LMODEL, "--figcls-model", FMODEL, "--layout2-model", L2MODEL, "--check-determinism", "--out", os.path.join(out, "gap.json"),
             "--last-cases-out", os.path.join(out, "gap_hidden_cases.json")], "gap gated")
    add("B_14_2_gap_bench_exit0_determinism", p.returncode == 0, p.stderr[-300:])
    g = subprocess.run(gate + [os.path.join(out, "gap.json")], capture_output=True, text=True)
    gap = json.load(open(os.path.join(out, "gap.json"))) if os.path.exists(os.path.join(out, "gap.json")) else {"sets": {}}
    # A3.6 item 2: a hidden-gap miss of the 80 target ships at the recorded ceiling (hidden kind.score >= 73.09, precision >= 90);
    # every other gate line must still PASS.
    gate_fails = [l for l in g.stdout.splitlines() if l.endswith("FAIL")]
    hk = (gap["sets"].get("hidden-gap") or {}).get("kind") or {}
    at_ceiling = bool(gate_fails) and all(l.startswith(("hidden-gap kind.score", "hidden-gap kind.by_gap")) for l in gate_fails) \
        and (hk.get("score") or 0) >= 73.09 and (hk.get("precision") or 0) >= 90
    add("B_14_2_gap_gate_pass_or_A3_6_ceiling", g.returncode == 0 and "GATE PASS" in g.stdout or at_ceiling,
        [l for l in g.stdout.splitlines() if "FAIL" in l or "kind.score" in l][:8])
    if at_ceiling: info(f"B 14.2 hidden gap at the A3.6 ceiling: kind.score {hk.get('score')} precision {hk.get('precision')}")
    l2 = (gap["sets"].get("public-gap") or {}).get("layout2") or {}
    add("B_A3_4_public_heron_ms_p95_le_600", l2.get("ms", {}).get("p95") is not None and l2["ms"]["p95"] <= 600, l2.get("ms"))
    add("B_A3_4_public_heron_load_le_300", l2.get("load_ms", {}).get("max") is not None and l2["load_ms"]["max"] <= 300, l2.get("load_ms"))
    p = run(["--family", "gap", "--set", "public", "--layout-model", LMODEL, "--figcls-model", FMODEL, "--out", os.path.join(out, "gap_heron_missing.json")], "gap public model-missing")
    if os.path.exists(os.path.join(out, "gap_heron_missing.json")):
        mm = json.load(open(os.path.join(out, "gap_heron_missing.json")))["sets"].get("public-gap") or {}
        info(f"B A4.4 fallback figure (heron model-missing): public kind {(mm.get('kind') or {}).get('score')} precision {(mm.get('kind') or {}).get('precision')}, "
             f"layout2 reasons {(mm.get('layout2') or {}).get('reasons')}")
    # 14.3 replay. The hidden kind cassette is moot when stage C is default-off and the hidden set routes nothing (hidden_kind_cassette_moot):
    # then the hidden set replays OCR only and its kind_vlm checks SKIP with that reason.
    moot = hidden_kind_cassette_moot()
    need = [os.path.join(BENCH, "kind_cassette.json"), os.path.join(BENCH, "ocr_cassette.json")]
    if HOLD: need += [os.path.join(HOLD, "gap", "ocr_cassette.json")] + ([] if moot else [os.path.join(HOLD, "gap", "kind_cassette.json")])
    missing = [x for x in need if not os.path.exists(x)]
    if missing: skip("B_14_3_replay", f"cassettes not recorded: {[os.path.basename(os.path.dirname(x)) + '/' + os.path.basename(x) for x in missing]}"); return
    def replay(side, kind):
        o = os.path.join(out, f"replay_{side}.json")
        p = run(["--family", "gap", "--set", side] + (["--holdout", HOLD] if HOLD else []) + ["--layout-model", LMODEL, "--figcls-model", FMODEL, "--layout2-model", L2MODEL, "--ocr", "replay", "--out", o,
                 "--last-cases-out", os.path.join(out, f"replay_{side}_cases.json")] + (["--kind", "replay"] if kind else []), f"gap replay {side}")
        add(f"B_14_3_{side}_replay_exit0_not_stale", p.returncode == 0, p.stderr[-300:])
        return (json.load(open(o))["sets"].get(f"{side}-gap") or {}) if os.path.exists(o) else {}
    pub = replay("public", True); hid = replay("hidden", not moot) if HOLD else None
    kv, k = pub.get("kind_vlm") or {}, (gap["sets"].get("public-gap") or {}).get("kind") or {}
    add("B_14_3_public_kind_vlm_score_ge_kind_minus_1", kv.get("score") is not None and k.get("score") is not None and kv["score"] >= k["score"] - 1, (kv.get("score"), k.get("score")))
    add("B_14_3_public_kind_vlm_precision_ge_95", (kv.get("precision") or 0) >= 95, kv.get("precision"))
    add("B_14_3_public_replay_miss_0", kv.get("replay_miss") == 0, kv.get("replay_miss"))
    if hid is None: skip("B_14_3_hidden", "no --holdout")
    elif moot: skip("B_14_3_hidden_kind_vlm_precision", moot)
    else:
        hp = (hid.get("kind_vlm") or {}).get("precision")
        info(f"B 14.3 hidden kind_vlm.precision {hp}: stage C ships default-{'on' if (hp or 0) >= 90 else 'off'}")
    for side, blk in (("public", pub), ("hidden", hid)) if HOLD else (("public", pub),):
        add(f"B_14_3_{side}_image_table_rows_ge_80", ((blk.get("image_table_rows") or {}).get("score") or 0) >= 80, (blk.get("image_table_rows") or {}).get("score"))


# ---------------------------------------------------------------- Y: second detector and structural fallback (Amendments 3, 4)
FAKE_ORT_HERON = r'''
import importlib.util, json, os, sys
_LOG = os.environ.get("FAKE_ORT_LOG")
def _log(ev):
    if _LOG:
        with open(_LOG, "a") as f: f.write(json.dumps(dict(ev, pid=os.getpid())) + "\n")
_real = os.environ["FAKE_ORT_REAL"]
_spec = importlib.util.spec_from_file_location("onnxruntime", os.path.join(_real, "__init__.py"), submodule_search_locations=[_real])
_m = importlib.util.module_from_spec(_spec); sys.modules["onnxruntime"] = _m; _spec.loader.exec_module(_m)
_Real = _m.InferenceSession
class InferenceSession(_Real):
    """Fails only heron's session (inputs images + orig_target_sizes): at load, or on its N-th run."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self._fake_n = 0
        self._heron = {i.name for i in self.get_inputs()} >= {"images", "orig_target_sizes"}
        _log({"ev": "session", "heron": self._heron})
        if self._heron and os.environ.get("FAKE_ORT_MODE") == "load-fail": raise RuntimeError("fake load failure")
    def run(self, *a, **k):
        if self._heron:
            self._fake_n += 1; _log({"ev": "run", "n": self._fake_n})
            if str(self._fake_n) in os.environ.get("FAKE_ORT_FAIL_RUNS", "").split(","): raise RuntimeError("fake inference failure")
        return super().run(*a, **k)
_m.InferenceSession = InferenceSession
'''
_fake_ort = {}
def fake_ort_env(**kw):
    """PYTHONPATH shadowing onnxruntime with FAKE_ORT_HERON (the real package is loaded underneath)."""
    if not _fake_ort:
        d = os.path.join(TMPROOT, "fake_ort"); os.makedirs(os.path.join(d, "onnxruntime"), exist_ok=True)
        open(os.path.join(d, "onnxruntime", "__init__.py"), "w").write(FAKE_ORT_HERON)
        real = subprocess.run([DETPY, "-c", "import onnxruntime, os; print(os.path.dirname(onnxruntime.__file__))"], capture_output=True, text=True).stdout.strip()
        _fake_ort.update(dir=d, real=real)
    log = os.path.join(TMPROOT, f"fake_ort_{time.time_ns()}.jsonl")
    return dict(PYTHONPATH=_fake_ort["dir"], FAKE_ORT_REAL=_fake_ort["real"], FAKE_ORT_LOG=log, **kw), log
def l2_pages(d): return {p.get("n"): p.get("layout2") for p in pages(d) if isinstance(p.get("layout2"), dict)}
def l2_events(d, name): return [e for e in ((d or {}).get("blockcls_events") or []) if isinstance(e, dict) and e.get("event") == name]
def structural_rects(d):
    return [(e.get("page"), e["rect"]) for e in events(d) if e.get("event", "block_kind") == "block_kind" and e.get("anchor") == "no-text-structural" and isrect(e.get("rect"))]
def blocks_by_page(d): return {p.get("n"): sorted((b.get("kind"), tuple(b.get("bbox") or [])) for b in p.get("blocks") or []) for p in pages(d)}
def lost_blocks(base, d):
    """Blocks of `base` missing from `d`, except orphan-line paragraphs (src "lines") whose centre lies inside a figure/table block that
    `d` added: section 5 / Phase 4 4.6 keep a candidate's lines out of paragraphs, so a new body absorbs them by rule."""
    bb = {p.get("n"): p.get("blocks") or [] for p in pages(base)}; db = {p.get("n"): p.get("blocks") or [] for p in pages(d)}
    key = lambda b: (b.get("kind"), tuple(b.get("bbox") or []))
    out = []
    for n, bs in bb.items():
        have = {key(b) for b in db.get(n, [])}; base_keys = {key(b) for b in bs}
        added = [b["bbox"] for b in db.get(n, []) if b.get("kind") in ("figure", "table") and key(b) not in base_keys and isrect(b.get("bbox"))]
        for b in bs:
            if key(b) in have: continue
            if b.get("src") == "lines" and b.get("kind") == "paragraph" and isrect(b.get("bbox")) and any(S.inside(S.centre(b["bbox"]), a) for a in added): continue
            out.append((n, key(b)))
    return out
def figs_sig(d): return sorted((n, f.get("label"), tuple(f.get("bbox") or []), tuple(f.get("caption_bbox") or [])) for n, f in figs(d) if f.get("captioned") is not False)
def l2_schema_bad(d):
    """A3.4 shapes of top-level layout2, pages[].layout2 and the two events; [] when they hold."""
    bad = []
    top = d.get("layout2")
    # top-level layout2 is not specified by A3.4; "idle" (no routed page, heron never loaded) is accepted only with routed 0 and no reason
    if not (isinstance(top, dict) and top.get("status") in ("ok", "unavailable", "idle") and top.get("reason") in L2_REASONS | {None}
            and (top.get("status") != "idle" or (top.get("routed") == 0 and top.get("reason") is None))): bad.append(("top", top))
    for n, l2 in l2_pages(d).items():
        ks = set(l2)
        if not (L2_PAGE_KEYS <= ks <= L2_PAGE_KEYS | {"reason"}) or l2.get("src") not in ("heron", "skipped", "unavailable", None) or not set(l2.get("by") or []) <= {"R1", "R2", "R3"} \
                or bool(l2.get("routed")) != bool(l2.get("by")) or set(l2.get("boxes") or {}) != {"figure", "table"} or not isint(l2.get("ms")) or l2.get("reason") not in L2_REASONS | {None, "over-cap"}:
            bad.append(("page", n, l2)); continue
        if not l2["routed"] and (l2["src"] is not None or l2["ms"] != 0 or l2["added"] or l2["structural"]): bad.append(("unrouted_cost", n, l2))
    for e in l2_events(d, "layout2_page"):
        if set(e) - {"ts", "pid"} != L2_PAGE_EVENT_KEYS or not set(e.get("by") or []) <= {"R1", "R2", "R3"} or e.get("detector_version") not in (HERON_VERSION, None): bad.append(("layout2_page", e))
    for e in l2_events(d, "layout2_unavailable"):
        if set(e) - {"ts", "pid"} != L2_UNAV_EVENT_KEYS or e.get("reason") not in L2_REASONS or not (e.get("error") is None or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", str(e["error"]))):
            bad.append(("layout2_unavailable", e))
    routed = sorted(n for n, l2 in l2_pages(d).items() if l2["routed"] and l2["src"] != "skipped")
    if sorted(e.get("page") for e in l2_events(d, "layout2_page")) != routed: bad.append(("one_layout2_page_per_routed_page", routed, [e.get("page") for e in l2_events(d, "layout2_page")]))
    return bad
def unav_lines(err): return [l for l in (err or "").splitlines() if l.startswith("layout2: detector unavailable")]

@section("Y")
def sec_layout2():
    pubgap = [os.path.join(PUBGAP, k, "doc.pdf") for k in sorted(os.listdir(PUBGAP)) if os.path.isdir(os.path.join(PUBGAP, k))]
    # router on the existing families: A5.1 pin update (Amendment 5 supersedes A3.6 item 3's routed == 0): firing is reported as a
    # count (INFO), not asserted, while the main-bench scores stay exactly equal to B5'; hidden as a count only. Still asserted: every
    # main-family page carries pages[].layout2, and heron costs nothing on a doc with no routed page.
    fired = [(os.path.basename(os.path.dirname(p)), n) for p in PUBLIC_DEFAULT for n, l2 in l2_pages(P(p)["d"]).items() if l2.get("routed")]
    fired_docs = {t for t, _ in fired}
    noload = [os.path.basename(os.path.dirname(p)) for p in PUBLIC_DEFAULT if os.path.basename(os.path.dirname(p)) not in fired_docs
              and (((P(p)["d"] or {}).get("layout2") or {}).get("load_ms") is not None or l2_events(P(p)["d"], "layout2_page"))]
    add(f"Y_main_family_pages_carry_layout2_{len(PUBLIC_DEFAULT)}_docs", all(l2_pages(P(p)["d"]) for p in PUBLIC_DEFAULT))
    info(f"Y router fired on {len(fired)} public main-family pages ({len(PUBLIC_DEFAULT)} docs) {fired[:5]}")
    add("Y_no_heron_cost_on_unrouted_main_family_docs", not noload, noload[:5])
    hf = sum(1 for p in HIDDEN_DEFAULT for l2 in l2_pages(P(p)["d"]).values() if l2.get("routed"))
    if HOLD: info(f"Y router fired on {hf} hidden main-family pages ({len(HIDDEN_DEFAULT)} docs)")
    # gap pages: schema, router fires, budgets, add-only, no text
    routed_any = 0; bad_schema = []; ms = []; loads = []; addonly = []; leak = []; words = set()
    for pdf in pubgap:
        d = P(pdf)["d"] or {}; off = P(pdf, SPEAKEASY_LAYOUT2="off")["d"] or {}; tag = os.path.basename(os.path.dirname(pdf))
        bad_schema += [(tag,) + tuple(x) for x in l2_schema_bad(d)]
        for n, l2 in l2_pages(d).items():
            routed_any += bool(l2.get("routed"))
            if l2.get("src") == "heron": ms.append(l2["ms"])
        if ((d.get("layout2") or {}).get("load_ms")) is not None: loads.append(d["layout2"]["load_ms"])
        lost = lost_blocks(off, d)
        if lost or figs_sig(off) != figs_sig(d): addonly.append((tag, "pp_s_or_caption_block_changed", lost[:2]))
        orect = [(e.get("page"), e["rect"]) for e in events(off) if e.get("event", "block_kind") == "block_kind" and isrect(e.get("rect"))]
        bodies = [(n, b["bbox"]) for n, b in blocks(off) if b.get("kind") == "paragraph" and isrect(b.get("bbox"))] + \
                 [(n, f["bbox"]) for n, f in figs(off) if f.get("captioned") is not False and isrect(f.get("bbox"))]      # A3.2: captioned bodies and paragraphs
        for e in events(d):
            if e.get("event", "block_kind") != "block_kind" or not isrect(e.get("rect")) or (e.get("page"), e["rect"]) in orect: continue
            pg = e.get("page"); r = e["rect"]; thr = 0.3 if e.get("anchor") == "no-text-structural" else 0.5
            if any(n == pg and S.iou(r, o) >= thr for n, o in orect): addonly.append((tag, "added_overlaps_pp_s_candidate", pg))
            if any(n == pg and (S.area(r) > 0) and (max(0, min(r[2], b[2]) - max(r[0], b[0])) * max(0, min(r[3], b[3]) - max(r[1], b[1]))) > 0.3 * S.area(r) for n, b in bodies if e.get("anchor") != "no-text-structural"):
                addonly.append((tag, "added_over_body_or_paragraph", pg))
        words |= text_words(os.path.dirname(pdf))
        blob = free_text_blob({"layout2": d.get("layout2"), "pages": [l2 for l2 in l2_pages(d).values()], "ev": l2_events(d, "layout2_page") + l2_events(d, "layout2_unavailable")})
        leak += [w for w in words if w.lower() in blob] + [x for x in any_text_values([d.get("layout2"), list(l2_pages(d).values())]) if x]
    add("Y_layout2_fields_and_events_schema", not bad_schema, bad_schema[:4])
    seen_cls = collections.Counter(e.get("det_cls") for p in pubgap for e in events(P(p)["d"]))
    info(f"Y det_cls values on public gap block_kind records {dict(seen_cls)} (9.2 / A3.4 / A5.2 enum: image, chart, table, picture, ink)")
    add("Y_router_fires_on_public_gap_pages", routed_any >= 1, routed_any)
    add("Y_heron_candidates_add_only", not addonly, addonly[:4])
    add("Y_no_text_in_layout2_fields_or_events", not leak, leak[:4])
    add("Y_budget_heron_ms_p95_le_600_per_routed_page", bool(ms) and S.nearest_rank(ms, 0.95) <= 600, (S.nearest_rank(ms, 0.5), S.nearest_rank(ms, 0.95), len(ms)))
    add("Y_budget_heron_load_le_300_ms", bool(loads) and max(loads) <= 300, loads)
    info(f"Y heron ms per routed page p50 {S.nearest_rank(ms, 0.5)} p95 {S.nearest_rank(ms, 0.95)} n {len(ms)}; load_ms {loads}")
    # kill switches (A3.4, A4.1): LAYOUT2=off kills heron and the structural fallback; BLOCKCLS=off removes every layout2 key
    for pdf in pubgap:
        tag = os.path.basename(os.path.dirname(pdf)); off = P(pdf, SPEAKEASY_LAYOUT2="off")["d"] or {}
        routes = any(l2.get("routed") for l2 in l2_pages(P(pdf)["d"]).values())   # a doc with no gap page never needs heron: reason may stay null
        okoff = ((off.get("layout2") or {}).get("reason") in (("disabled",) if routes else ("disabled", None)) and not any(l2.get("src") == "heron" or l2.get("structural") for l2 in l2_pages(off).values())
                 and not structural_rects(off) and not any(e.get("detector_version") for e in l2_events(off, "layout2_page")))
        if not okoff: add(f"Y_layout2_off_kills_heron_and_structural_{tag}", False, (off.get("layout2"), len(structural_rects(off)))); break
    else: add("Y_layout2_off_kills_heron_and_structural", True)
    ko = [os.path.basename(os.path.dirname(p)) for p in pubgap if '"layout2' in json.dumps(P(p, SPEAKEASY_BLOCKCLS="off")["d"] or {})]
    add("Y_blockcls_off_has_no_layout2_keys_or_events", not ko, ko[:4])
    # each heron unavailable reason (A3.6 item 6 as restated by A4.1): reason event, src unavailable, stderr line; blocks = LAYOUT2=off
    # except blocks of no-text-structural records on R1 pages; the structural fallback still runs on R1 pages
    flip = os.path.join(TMPROOT, "heron_flip.onnx"); shutil.copyfile(L2MODEL, flip)
    with open(flip, "r+b") as f: f.seek(4096); b0 = f.read(1); f.seek(4096); f.write(bytes([b0[0] ^ 0xFF]))
    for doc in ("gap-scan-01", "gap-slides-01"):
        pdf = os.path.join(PUBGAP, doc, "doc.pdf"); off = P(pdf, SPEAKEASY_LAYOUT2="off")["d"] or {}
        cases = [("model_missing_override", dict(SPEAKEASY_LAYOUT2_MODEL=os.path.join(TMPROOT, "nope", "docling_layout_heron.onnx")), "model-missing", None),
                 ("model_mismatch_byte_flip", dict(SPEAKEASY_LAYOUT2_MODEL=flip), "model-mismatch", None)]
        fe, flog = fake_ort_env(FAKE_ORT_MODE="load-fail"); cases.append(("load_failed", fe, "load-failed", flog))
        for nm, kw, want, flog in cases:
            r = parse(pdf, **kw); d = r["d"] or {}; tag = f"Y_{doc}_{nm}"
            add(f"{tag}_exit0", r["rc"] == 0 and bool(d), (r["rc"], r["err"]))
            if not d: continue
            if flog is not None and not any(x.get("heron") for x in jl(flog)): skip(f"{tag}_reason", "fake onnxruntime never saw heron's session"); continue
            ev = l2_events(d, "layout2_unavailable"); rp = {n: l2 for n, l2 in l2_pages(d).items() if l2.get("routed")}
            add(f"{tag}_one_reason_event", len(ev) == 1 and ev[0].get("reason") == want and ev[0].get("page") is None and (want != "load-failed" or ev[0].get("error") == "RuntimeError"), ev)
            add(f"{tag}_routed_pages_src_unavailable", bool(rp) and all(l2.get("src") == "unavailable" for l2 in rp.values()), {n: l2.get("src") for n, l2 in rp.items()})
            add(f"{tag}_stderr_line", unav_lines(r["err"]) == [f"layout2: detector unavailable ({want})"], unav_lines(r["err"]))
            add(f"{tag}_top_layout2_unavailable", (d.get("layout2") or {}).get("status") == "unavailable" and (d.get("layout2") or {}).get("reason") == want, d.get("layout2"))
            r1 = {n for n, l2 in rp.items() if "R1" in (l2.get("by") or [])}; st = structural_rects(d)
            add(f"{tag}_structural_only_on_R1_pages", all(n in r1 for n, _ in st), [n for n, _ in st if n not in r1])
            ob, db = blocks_by_page(off), blocks_by_page(d)
            extra = [(n, b) for n, bs in db.items() for b in bs if b not in ob.get(n, [])]
            unexplained = [(n, b) for n, b in extra if not (b[0] in ("figure", "table") and any(sn == n and S.iou(list(b[1]), sr) >= 0.5 for sn, sr in st))]
            missing = lost_blocks(off, d)
            add(f"{tag}_blocks_equal_layout2_off_except_structural", not unexplained and not missing, (unexplained[:2], missing[:2]))
        fe, flog = fake_ort_env(FAKE_ORT_FAIL_RUNS="1"); full = P(pdf)["d"] or {}; r = parse(pdf, **fe); d = r["d"] or {}; tag = f"Y_{doc}_inference_failed"
        runs = [x for x in jl(flog) if x.get("ev") == "run"]
        if not runs: skip(tag, "heron's session.run never called through the fake"); continue
        ev = l2_events(d, "layout2_unavailable"); fp = ev[0].get("page") if len(ev) == 1 else None
        add(f"{tag}_one_event_with_page_and_error", len(ev) == 1 and ev[0].get("reason") == "inference-failed" and isint(fp) and ev[0].get("error") == "RuntimeError", ev)
        add(f"{tag}_page_src_unavailable_reason", (l2_pages(d).get(fp) or {}).get("src") == "unavailable" and (l2_pages(d).get(fp) or {}).get("reason") == "inference-failed", l2_pages(d).get(fp))
        others = [n for n, l2 in l2_pages(d).items() if l2.get("routed") and n != fp]
        add(f"{tag}_other_routed_pages_keep_heron", all((l2_pages(d).get(n) or {}).get("src") == "heron" for n in others), {n: (l2_pages(d).get(n) or {}).get("src") for n in others})
        fb, db = blocks_by_page(full), blocks_by_page(d)
        add(f"{tag}_other_pages_equal_full_run", all(db.get(n) == fb.get(n) for n in db if n != fp), [n for n in db if n != fp and db.get(n) != fb.get(n)])
    # no onnxruntime at all: heron is unavailable with that reason (the structural fallback needs no model)
    r = parse(os.path.join(PUBGAP, "gap-scan-01", "doc.pdf"), py=NOORT) if not NOORT_HAS_ORT else {"d": None}; d = r["d"] or {}
    if NOORT_HAS_ORT: skip("Y_no_onnxruntime_reason", NOORT_SKIP)
    elif not any(l2.get("routed") for l2 in l2_pages(d).values()) and not (d.get("layout2") or {}).get("reason"):
        skip("Y_no_onnxruntime_reason", "without onnxruntime no page is detected, so the router routes nothing and heron is never needed")
    else: add("Y_no_onnxruntime_reason", (d.get("layout2") or {}).get("reason") == "no-onnxruntime" and any(e.get("reason") == "no-onnxruntime" for e in l2_events(d, "layout2_unavailable")),
        (d.get("layout2"), l2_events(d, "layout2_unavailable")))
    # structural fallback budget (A3.4: <= 50 ms per R1 page) in the model-missing parse, where layout2.ms is the router + fallback only
    sm = [l2["ms"] for p in pubgap for l2 in l2_pages(parse(p, SPEAKEASY_LAYOUT2_MODEL=os.path.join(TMPROOT, "nope2", "h.onnx"))["d"]).values() if l2.get("routed") and "R1" in (l2.get("by") or [])]
    add("Y_budget_structural_fallback_le_50ms_p95_per_R1_page", bool(sm) and S.nearest_rank(sm, 0.95) <= 50, (S.nearest_rank(sm, 0.95), len(sm)))


# ---------------------------------------------------------------- Z: ~/.speakeasy unchanged
@section("Z")
def sec_real_home():
    now = {}
    for sub in _real_snapshot:
        p = os.path.join(REAL_HOME, sub)
        now[sub] = (sorted(os.listdir(p)) if os.path.isdir(p) else (os.path.getsize(p), os.path.getmtime(p))) if os.path.exists(p) else None
    add("Z_real_home_untouched", now == _real_snapshot, [k for k in now if now[k] != _real_snapshot[k]])

for fn in (sec_schema, sec_regex_first, sec_killswitch, sec_lexicon, sec_uncaptioned, sec_classifier_missing, sec_determinism, sec_stage_c, sec_network, sec_ocr, sec_header_tie, sec_layout2,
           sec_area, sec_privacy, sec_static, sec_bench, sec_real_home):
    fn()
c = collections.Counter(s for s, _ in R)
print(f"SUMMARY pass {c['PASS']} fail {c['FAIL']} skip {c['SKIP']} in {time.time() - T0:.0f}s (repo {REPO}, baseline {A.baseline_ref or 'none'})", flush=True)
if not A.keep: shutil.rmtree(TMPROOT, ignore_errors=True)
else: print(f"INFO kept {TMPROOT}")
sys.exit(1 if c["FAIL"] else 0)
