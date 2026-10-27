#!/usr/bin/env python3
"""Phase-4 acceptance suite (black box, promoted from the holdout) for docs/contracts/phase4-layout.md (layout detector for page blocks).
Public data only unless --holdout DIR (the hidden bench dir) and --hidden-baseline are given. Defaults are file-relative (--repo,
b4/baseline-public.json: the public bench baseline at 890faa4) and this interpreter (--det-python / --noort-python, or
SPEAKEASY_DOC_PYTHON / SPEAKEASY_NOORT_PYTHON); the model defaults to SPEAKEASY_LAYOUT_MODEL, else install.sh's ~/.speakeasy/models
copy (read only). The fixture generators are shared with tests/audit/logging.
Drives only `docmap.py parse-pdf` / `dump`, the CLI (src/speakeasy), src/install.sh and app/bundle_python.sh (temp dirs, stubbed
network / uv / codesign), the benchmark harness (tests/audit/bench) and the logs under a temp $SPEAKEASY_HOME. Never reads app
code, never calls a real LLM, never touches ~/.speakeasy (the doc venv there is only used read-only as an interpreter).
"Unchanged" fallback behaviour is checked differentially against the baseline commit (git archive of --baseline-ref).

Usage: python3 run_phase4.py [--repo PATH] [--baseline-ref 890faa4] [--only L,D,H,R,Q,S,F,P,C,T,I,U,B,Z] [--skip-bench] [--keep]
         [--det-python PY] [--noort-python PY] [--model PATH] [--public-baseline FILE] [--holdout DIR --hidden-baseline FILE]
Output: one line per check `PASS|FAIL|SKIP <name> <detail>`, `INFO ...` lines (timings, scores), then `SUMMARY ...`.
Exit 0 only if no FAIL."""
import argparse, glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import p4stubs

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", ".."))); ap.add_argument("--baseline-ref", default="890faa4")
ap.add_argument("--only", default=""); ap.add_argument("--skip-bench", action="store_true"); ap.add_argument("--keep", action="store_true")
ap.add_argument("--det-python", default=os.environ.get("SPEAKEASY_DOC_PYTHON") or sys.executable, help="python with pypdfium2, Pillow, onnxruntime, numpy")
ap.add_argument("--noort-python", default=os.environ.get("SPEAKEASY_NOORT_PYTHON") or sys.executable,
                help="python with pypdfium2, Pillow, reportlab and pyflakes, without onnxruntime")
ap.add_argument("--model", default=os.environ.get("SPEAKEASY_LAYOUT_MODEL") or os.path.expanduser("~/.speakeasy/models/pp_doclayout_s.onnx"))
ap.add_argument("--figcls-model", default=os.environ.get("SPEAKEASY_FIGCLS_MODEL") or os.path.expanduser("~/.speakeasy/models/docling_figure_classifier_v2_5.onnx"))
ap.add_argument("--layout2-model", default=os.environ.get("SPEAKEASY_LAYOUT2_MODEL") or os.path.expanduser("~/.speakeasy/models/docling_layout_heron.onnx"))
ap.add_argument("--public-baseline", default=os.path.join(HERE, "b4", "baseline-public.json"), help="public bench baseline at 890faa4")
ap.add_argument("--holdout", default=None, help="hidden bench dir; without it the suite runs on public data only")
ap.add_argument("--hidden-baseline", default=None, help="hidden bench baseline at 890faa4 (with --holdout)")
A = ap.parse_args()
REPO = os.path.abspath(A.repo); SRC = os.path.join(REPO, "src"); CLI = os.path.join(SRC, "speakeasy"); BENCH = os.path.join(REPO, "tests", "audit", "bench")
ONLY = {x.strip().upper() for x in A.only.split(",") if x.strip()}
DETPY, NOORT = A.det_python, A.noort_python
REAL_HOME = os.path.realpath(os.path.expanduser("~/.speakeasy"))
TMPROOT = os.path.realpath(tempfile.mkdtemp(prefix="se_p4_")); T0 = time.time()

SHA = "33688dbee1c23e34b81777e97cb428eb40f24b242c02b5f623484959e830aec8"; SIZE = 4917852; DV = "pp-doclayout-s/33688dbee1c2"
REV = "7e851b9e5ad66613d7318a3213ab3336ebe0a514"; MODEL_NAME = "pp_doclayout_s.onnx"
REASONS = ["disabled", "no-onnxruntime", "model-missing", "model-mismatch", "load-failed", "pdfkit-engine", "inference-failed"]
KINDS = ("paragraph", "caption", "figure", "table")
FIG_SRC = {"graphics", "whitespace", "caption-only", "detector"}
TAB_SRC = {"graphics", "text-rows", "graphics+text-rows", "caption-only", "detector"}
CAP_SRC = {"regex", "regex+detector"}; PARA_SRC = {"detector", "lines"}
LABEL_RE = re.compile(r"^(Figure|Table) \d+$")
# Phase 6 (phase6-block-classifier.md 4.2, 9.1): figure/table blocks gain kind_src; labels keep "Figure|Table <number as printed>"
# (Table IV, Figure S2, Figure 3.2) or "U<page>-<k>" for uncaptioned items; lexicon captions add figures the baseline never had
P6_LABEL_RE = re.compile(r"^(Figure|Table) ((?:S|[A-Z]\.)?\d+(?:[.\-]\d+)?[a-h]?|[IVXL]+|U\d+-\d+)$")
KIND_SRC = {"caption", "lexicon", "classifier", "vlm"}
def p6_added(f):
    return str(f.get("caption_src", "")).startswith("lexicon") or f.get("captioned") is False or f.get("kind_src") in ("classifier", "vlm")
UNAV_RE = re.compile(r"^layout: detector unavailable \(([a-z-]+)\)$")

R = []
def _s(d): return " ".join(str(d).split())[:500]
def add(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"; R.append((st, name)); print(f"{st} {name} {_s(detail) if not ok or os.environ.get('P4_VERBOSE') else ''}".rstrip(), flush=True)
def skip(name, why): R.append(("SKIP", name)); print(f"SKIP {name} {_s(why)}", flush=True)
def info(msg): print(f"INFO {msg}", flush=True)
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
def r1(v): return isnum(v) and abs(round(v, 1) - v) < 1e-9
def sha256(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
def jl(p):
    out = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x:
                try: out.append(json.loads(x))
                except Exception: out.append({"_raw": x})
    return out

# ---------------------------------------------------------------- setup
if not os.path.isfile(A.model) or os.path.getsize(A.model) != SIZE or sha256(A.model) != SHA:
    print(f"FATAL verified model missing or wrong at {A.model}"); sys.exit(2)
for _m in (A.figcls_model, A.layout2_model):
    if not os.path.isfile(_m): print(f"FATAL Phase 6 model missing at {_m} (the install and bundle scenarios start with it in place)"); sys.exit(2)
MD = os.path.join(TMPROOT, "models"); os.makedirs(MD)
GOOD = os.path.join(MD, "good", MODEL_NAME); os.makedirs(os.path.dirname(GOOD)); shutil.copyfile(A.model, GOOD)
_raw = open(GOOD, "rb").read()
FLIP = os.path.join(MD, "flip.onnx"); _b = bytearray(_raw); _b[len(_b) // 2] ^= 0xFF; open(FLIP, "wb").write(bytes(_b))
TRUNC = os.path.join(MD, "trunc.onnx"); open(TRUNC, "wb").write(_raw[:-4096])
NOTONNX = os.path.join(MD, "notonnx.onnx"); open(NOTONNX, "wb").write(b"not an onnx model " * (SIZE // 18) + b"x" * (SIZE % 18))
assert os.path.getsize(FLIP) == SIZE and sha256(FLIP) != SHA and os.path.getsize(NOTONNX) == SIZE
BASE = os.path.join(TMPROOT, "base"); os.makedirs(BASE)
subprocess.run(f"git -C '{REPO}' archive {A.baseline_ref} | tar -x -C '{BASE}'", shell=True, check=True)
BASE_SRC = os.path.join(BASE, "src")
PUB = os.path.join(BENCH, "fixtures", "public")
PUBLIC = sorted(glob.glob(os.path.join(PUB, "*", "doc.pdf")))
FXD = os.path.join(TMPROOT, "fx"); os.makedirs(FXD)
for script in ("make_fig5.py", "make_docs.py", "make_p2docs.py"):
    subprocess.run([NOORT, os.path.join(os.path.dirname(HERE), "logging", script), FXD], check=True, capture_output=True)
LOGFX_ALL = sorted(glob.glob(os.path.join(FXD, "*.pdf")))
FIG5 = os.path.join(FXD, "fig5.pdf")
REPORT = os.path.join(PUB, "syn-report-01", "doc.pdf")          # 3 pages, figures + tables
MULTI = os.path.join(PUB, "arx-2406.06619", "doc.pdf")          # 5 pages
ORT_REAL = subprocess.run([DETPY, "-c", "import onnxruntime, os; print(os.path.dirname(onnxruntime.__file__))"], capture_output=True, text=True).stdout.strip()
FAKE = p4stubs.make_fake_ort(TMPROOT)
_real_snapshot = {}
for sub in ("models", "cache/doc", "logs/runs.jsonl"):
    p = os.path.join(REAL_HOME, sub)
    _real_snapshot[sub] = (sorted(os.listdir(p)) if os.path.isdir(p) else os.path.getsize(p)) if os.path.exists(p) else None

_n = [0]
def mkhome(tag="h"):
    _n[0] += 1; d = os.path.join(TMPROOT, "homes", f"{_n[0]:04d}-{tag}"); h = os.path.join(d, "home"); os.makedirs(h); os.makedirs(os.path.join(d, "tmp"))
    rh = os.path.realpath(h); assert rh.startswith(TMPROOT + os.sep) and not rh.startswith(REAL_HOME)
    return h
def penv(home, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "STUB_", "MOCK_", "FAKE_ORT_")) and k not in ("PYTHONPATH",)}
    e.update(HOME=home, SPEAKEASY_HOME=home, TMPDIR=os.path.join(os.path.dirname(home), "tmp"), PYTHONDONTWRITEBYTECODE="1")
    for k, v in kw.items():
        if v is None: e.pop(k, None)
        else: e[k] = str(v)
    return e
_UNSET = object()
def parse(pdf, py=None, model=_UNSET, sel=None, prev=None, src=None, home=None, sub="parse-pdf", fake=None, **envkw):
    """One parse-pdf invocation in a fresh temp home. model: path for SPEAKEASY_LAYOUT_MODEL (default the verified copy; None = unset).
    fake: dict of FAKE_ORT_* settings to shadow onnxruntime with the wrapper. Returns dict(rc, d, err, ms, home, fake_log)."""
    py = py or DETPY; src = src or SRC; home = home or mkhome("parse"); d0 = os.path.dirname(home)
    cp = os.path.join(d0, f"in_{time.time_ns()}.pdf"); shutil.copyfile(pdf, cp); out = os.path.join(d0, f"out_{time.time_ns()}.json")
    kw = dict(envkw)
    if model is _UNSET: model = GOOD
    kw.setdefault("SPEAKEASY_LAYOUT_MODEL", model)
    flog = None
    if fake is not None:
        flog = os.path.join(d0, f"fake_{time.time_ns()}.jsonl")
        kw.update(PYTHONPATH=FAKE, FAKE_ORT_REAL=ORT_REAL, FAKE_ORT_LOG=flog, **{k: v for k, v in fake.items()})
    args = [py, os.path.join(src, "docmap.py"), sub, cp] + ([out] if sub == "parse-pdf" else [])
    if sel is not None:
        sp = os.path.join(d0, f"sel_{time.time_ns()}.txt"); open(sp, "w").write(sel); args += ["--sel", sp]
    if prev is not None:
        pp = os.path.join(d0, f"prev_{time.time_ns()}.json"); json.dump(prev, open(pp, "w")); args += ["--prev", pp]
    t = time.time()
    try: p = subprocess.run(args, capture_output=True, text=True, env=penv(home, **kw), timeout=300)
    except subprocess.TimeoutExpired: return {"rc": "timeout", "d": None, "err": "", "ms": None, "home": home, "fake_log": flog}
    ms = round((time.time() - t) * 1000)
    try: d = json.load(open(out)) if sub == "parse-pdf" else json.loads(p.stdout)
    except Exception: d = None
    return {"rc": p.returncode, "d": d, "err": p.stderr, "out": p.stdout, "ms": ms, "home": home, "fake_log": jl(flog) if flog else None}

def pages(d): return [p for p in ((d or {}).get("pages") or []) if isinstance(p, dict)]
def fd_pages(d): return [p for p in pages(d) if p.get("figs_done") is True]
def unav_lines(err): return [l for l in (err or "").splitlines() if l.startswith("layout:")]
def events(d, kind=None):
    ev = (d or {}).get("layout_events")
    if not isinstance(ev, list): return None
    return [e for e in ev if isinstance(e, dict) and (kind is None or e.get("event") == kind)]
def page_blocks(d): return {p.get("n"): p.get("blocks") for p in fd_pages(d)}
def page_figs(d): return {p.get("n"): p.get("figures") for p in fd_pages(d)}
def counts(blocks):
    c = {k: 0 for k in KINDS}
    for b in blocks or []:
        if isinstance(b, dict) and b.get("kind") in c: c[b["kind"]] += 1
    return c
def strip_ms(d):
    """Deep copy without the timing fields (ms) for determinism comparisons."""
    if isinstance(d, dict): return {k: strip_ms(v) for k, v in d.items() if k != "ms"}
    if isinstance(d, list): return [strip_ms(x) for x in d]
    return d
def vov(a, b):
    """Vertical overlap of two rects that overlap horizontally (0 when they do not)."""
    if min(a[2], b[2]) - max(a[0], b[0]) <= 0: return 0.0
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1])); i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0

# ---------------------------------------------------------------- block validator (phase2-logging D + phase4 4.7)
def validate(d):
    out = []; P = pages(d); fd = fd_pages(d)
    out.append(("has_figs_done_pages", bool(fd), [p.get("figs_done") for p in P]))
    out.append(("blocks_list_every_figs_done_page", all(isinstance(p.get("blocks"), list) for p in fd), [(p.get("n"), type(p.get("blocks")).__name__) for p in fd]))
    status = ((d or {}).get("layout") or {}).get("status")
    figs_all = [f for p in P for f in (p.get("figures") or [])]
    bad = [(f.get("label"), f.get("kind"), f.get("bbox_src")) for f in figs_all if f.get("bbox_src") not in (TAB_SRC if f.get("kind") == "table" else FIG_SRC)]
    out.append(("figures_bbox_src_enum_by_kind", not bad, bad[:4]))
    inv = {k: [] for k in ("block_schema_keys", "block_src_enum_by_kind", "block_bbox_rounded_0.1pt", "block_lines_valid", "block_label_Figure_or_Table_N",
                           "one_block_per_figure", "block_src_equals_figure_bbox_src", "one_caption_per_figure", "no_extra_fig_table_blocks", "no_extra_caption_blocks",
                           "paragraph_bbox_union_of_lines", "no_shared_lines", "paragraph_lines_outside_fig_table", "caption_lines_only_in_caption_block",
                           "fig_table_bbox_inside_page", "fig_table_excludes_caption_le_2pt", "heuristic_page_no_detector_src", "unavailable_no_detector_src",
                           "reading_order_top_to_bottom_per_column")}
    for p in fd:
        n = p.get("n"); blocks = p.get("blocks") if isinstance(p.get("blocks"), list) else []; lines = p.get("lines") or []; figs = p.get("figures") or []
        W, H = p.get("w") or 1e9, p.get("h") or 1e9; lsrc = (p.get("layout") or {}).get("src")
        good = []
        for i, b in enumerate(blocks):
            if not isinstance(b, dict): inv["block_schema_keys"].append((n, i, "not-dict")); continue
            k = b.get("kind"); bb = b.get("bbox")
            allowed = {"kind", "bbox", "src"} | ({"lines"} if k in ("paragraph", "caption") else set()) | ({"label"} if k in ("caption", "figure", "table") else set()) \
                | ({"kind_src"} if k in ("figure", "table") and b.get("kind_src") in KIND_SRC else set())
            if k not in KINDS or set(b) != allowed: inv["block_schema_keys"].append((n, i, k, sorted(set(b) ^ allowed)))
            if not (isinstance(bb, list) and len(bb) == 4 and all(isnum(v) for v in bb) and bb[0] < bb[2] and bb[1] < bb[3]): inv["block_schema_keys"].append((n, i, k, "bbox", bb)); continue
            good.append(b)
            if not all(r1(v) for v in bb): inv["block_bbox_rounded_0.1pt"].append((n, i, bb))
            enum = {"figure": FIG_SRC, "table": TAB_SRC, "caption": CAP_SRC, "paragraph": PARA_SRC}.get(k, set())
            if b.get("src") not in enum: inv["block_src_enum_by_kind"].append((n, i, k, b.get("src")))
            if "lines" in b and not (isinstance(b["lines"], list) and b["lines"] and all(isint(x) and 0 <= x < len(lines) for x in b["lines"])): inv["block_lines_valid"].append((n, i, k))
            if "label" in b and not (isinstance(b["label"], str) and P6_LABEL_RE.match(b["label"])): inv["block_label_Figure_or_Table_N"].append((n, i, b.get("label")))
            if b.get("src") in ("detector", "regex+detector"):
                if lsrc == "heuristic": inv["heuristic_page_no_detector_src"].append((n, i, k, b.get("src")))
                if status == "unavailable": inv["unavailable_no_detector_src"].append((n, i, k, b.get("src")))
        if lsrc == "heuristic" or status == "unavailable":
            for f in figs:
                if f.get("bbox_src") == "detector": inv["heuristic_page_no_detector_src"].append((n, "figures[]", f.get("label")))
        ft = [b for b in good if b.get("kind") in ("figure", "table")]; caps = [b for b in good if b.get("kind") == "caption"]; paras = [b for b in good if b.get("kind") == "paragraph"]
        for f in figs:
            want = "table" if f.get("kind") == "table" else "figure"
            m = [b for b in ft if b.get("label") == f.get("label") and b.get("kind") == want and b.get("bbox") == f.get("bbox")]
            if len(m) != 1: inv["one_block_per_figure"].append((n, f.get("label"), f.get("bbox")))
            elif m[0].get("src") != f.get("bbox_src"): inv["block_src_equals_figure_bbox_src"].append((n, f.get("label"), m[0].get("src"), f.get("bbox_src")))
            c = [b for b in caps if b.get("bbox") == f.get("caption_bbox")]
            if len(c) != 1 or c[0].get("label") != f.get("label"): inv["one_caption_per_figure"].append((n, f.get("label"), f.get("caption_bbox")))
            fb, cb = f.get("bbox"), f.get("caption_bbox")
            if isinstance(fb, list) and len(fb) == 4:
                if fb[0] < -0.05 or fb[1] < -0.05 or fb[2] > W + 0.05 or fb[3] > H + 0.05: inv["fig_table_bbox_inside_page"].append((n, f.get("label"), fb, [W, H]))
                if isinstance(cb, list) and len(cb) == 4 and vov(fb, cb) > 2.0 + 1e-6: inv["fig_table_excludes_caption_le_2pt"].append((n, f.get("kind"), f.get("label"), fb, cb, round(vov(fb, cb), 1)))
            if isinstance(cb, list) and len(cb) == 4:
                inside = {i for i, l in enumerate(lines) if l[0] >= cb[0] - 0.6 and l[1] >= cb[1] - 0.6 and l[2] <= cb[2] + 0.6 and l[3] <= cb[3] + 0.6}
                inpara = sorted(inside & set().union(*[set(b.get("lines") or []) for b in paras] or [set()]))
                if inpara: inv["caption_lines_only_in_caption_block"].append((n, f.get("label"), inpara[:5]))
        if len(ft) != len(figs): inv["no_extra_fig_table_blocks"].append((n, len(ft), len(figs)))
        if len(caps) != len(figs): inv["no_extra_caption_blocks"].append((n, len(caps), len(figs)))
        for b in paras:
            ls = [lines[i] for i in b.get("lines") or [] if isint(i) and i < len(lines)]
            if ls:
                u = [min(l[0] for l in ls), min(l[1] for l in ls), max(l[2] for l in ls), max(l[3] for l in ls)]
                if max(abs(a - c) for a, c in zip(u, b["bbox"])) > 0.15: inv["paragraph_bbox_union_of_lines"].append((n, b["bbox"], [round(v, 1) for v in u]))
        seen = {}
        for bi, b in enumerate(good):
            for li in b.get("lines") or []:
                if li in seen: inv["no_shared_lines"].append((n, li, seen[li], bi))
                seen[li] = bi
        for b in paras:
            for li in b.get("lines") or []:
                if not (isint(li) and li < len(lines)): continue
                l = lines[li]
                for f in ft:
                    x0, y0, x1, y1 = f["bbox"]
                    if l[0] >= x0 - 2 and l[1] >= y0 - 2 and l[2] <= x1 + 2 and l[3] <= y1 + 2: inv["paragraph_lines_outside_fig_table"].append((n, li, f.get("label"))); break
        cols = p.get("columns")
        groups = {"all": good} if cols == 1 else {"left": [b for b in good if (b["bbox"][2] - b["bbox"][0]) < 0.6 * W and (b["bbox"][0] + b["bbox"][2]) / 2 < W / 2],
                                                   "right": [b for b in good if (b["bbox"][2] - b["bbox"][0]) < 0.6 * W and (b["bbox"][0] + b["bbox"][2]) / 2 >= W / 2]}
        for g, gb in groups.items():
            ys = [b["bbox"][1] for b in gb]
            if any(ys[i + 1] < ys[i] - 1.0 for i in range(len(ys) - 1)): inv["reading_order_top_to_bottom_per_column"].append((n, g, ys[:10]))
    out += [(k, not v, v[:3]) for k, v in inv.items()]
    return out

def layout_schema(d, expect_status=None, expect_reason=None):
    """Section 6.1 / 6.2 field shapes. Returns [(suffix, ok, detail)]."""
    out = []; L = (d or {}).get("layout")
    ok = isinstance(L, dict) and L.get("status") in ("ok", "unavailable")
    if ok and L["status"] == "ok": ok = set(L) == {"status", "detector_version"} and L.get("detector_version") == DV
    if ok and L["status"] == "unavailable": ok = set(L) == {"status", "detector_version", "reason"} and L.get("reason") in REASONS and (L.get("detector_version") is None or L.get("detector_version") == DV)
    out.append(("top_layout_schema", ok, L))
    if expect_status: out.append((f"top_layout_status_{expect_status}", isinstance(L, dict) and L.get("status") == expect_status and (expect_reason is None or L.get("reason") == expect_reason), L))
    bad = []
    for p in fd_pages(d):
        pl = p.get("layout")
        if not isinstance(pl, dict): bad.append((p.get("n"), "missing")); continue
        if pl.get("src") == "detector":
            if set(pl) != {"src", "ms", "detector_version", "blocks_by_kind"} or not (isint(pl.get("ms")) and pl["ms"] >= 0) or pl.get("detector_version") != DV: bad.append((p.get("n"), pl)); continue
            if pl.get("blocks_by_kind") != counts(p.get("blocks")): bad.append((p.get("n"), "blocks_by_kind", pl.get("blocks_by_kind"), counts(p.get("blocks"))))
        elif pl.get("src") == "heuristic":
            if set(pl) != {"src", "reason"} or pl.get("reason") not in REASONS: bad.append((p.get("n"), pl))
        else: bad.append((p.get("n"), pl))
    out.append(("pages_layout_schema_every_figs_done_page", not bad, bad[:3]))
    ev = events(d)
    out.append(("layout_events_list", ev is not None, (d or {}).get("layout_events")))
    bad = []
    for e in ev or []:
        if e.get("event") == "layout_page":
            if set(e) != {"event", "page", "ms", "detector_version", "blocks_by_kind"} or not isint(e.get("page")) or not isint(e.get("ms")) or e.get("detector_version") != DV or set(e.get("blocks_by_kind") or {}) != set(KINDS): bad.append(e)
        elif e.get("event") == "layout_detector_unavailable":
            if set(e) != {"event", "reason", "error", "detector_version", "page"} or e.get("reason") not in REASONS or not (e.get("error") is None or (isinstance(e["error"], str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", e["error"]))) \
               or not (e.get("page") is None or isint(e["page"])) or not (e.get("detector_version") is None or e.get("detector_version") == DV): bad.append(e)
        else: bad.append(e)
    out.append(("layout_events_schema", not bad, bad[:3]))
    pg = [e.get("page") for e in ev or [] if e.get("page") is not None]
    out.append(("layout_events_in_page_order", pg == sorted(pg), pg))
    lp = {e.get("page"): e for e in ev or [] if e.get("event") == "layout_page"}
    bad = [(p.get("n"), (p.get("layout") or {}).get("blocks_by_kind"), (lp.get(p.get("n")) or {}).get("blocks_by_kind")) for p in fd_pages(d)
           if (p.get("layout") or {}).get("src") == "detector" and (p.get("n") not in lp or lp[p.get("n")].get("blocks_by_kind") != p["layout"].get("blocks_by_kind") or lp[p.get("n")].get("ms") != p["layout"].get("ms"))]
    out.append(("layout_page_event_matches_page_layout", not bad, bad[:3]))
    return out

def clip_page(q):
    """The baseline page with figure/table boxes clipped to the page (4.7: every figure/table bbox lies inside the page)."""
    W, H = q.get("w"), q.get("h"); q = json.loads(json.dumps(q))
    def c(b): return [round(max(0.0, b[0]), 1), round(max(0.0, b[1]), 1), round(min(W, b[2]), 1), round(min(H, b[3]), 1)] if isinstance(b, list) and len(b) == 4 else b
    for b in q.get("blocks") or []:
        if b.get("kind") in ("figure", "table"): b["bbox"] = c(b["bbox"])
    for f in q.get("figures") or []: f["bbox"] = c(f.get("bbox"))
    return q
def report(tag, checks):
    for suf, ok, det in checks: add(f"{tag}_{suf}", ok, det)
def agg(tag, results):
    """Merge per-file validator results into one check per suffix: FAIL lists the files that failed."""
    by = {}
    for f, checks in results:
        for suf, ok, det in checks: by.setdefault(suf, []).append((f, ok, det))
    for suf, rs in by.items():
        bad = [(f, det) for f, ok, det in rs if not ok]
        add(f"{tag}_{suf}", not bad, bad[:3])
def short(p): return os.path.relpath(p, PUB) if p.startswith(PUB) else os.path.basename(p)

LOGFX = []   # logging fixtures that parse at the baseline (corrupt.pdf is an intentional parse failure)
ON, OFF, BASEP = {}, {}, {}   # pdf -> parse result: detector on, SPEAKEASY_LAYOUT=off, baseline tree
def on(pdf):
    if pdf not in ON: ON[pdf] = parse(pdf)
    return ON[pdf]
def off(pdf):
    if pdf not in OFF: OFF[pdf] = parse(pdf, SPEAKEASY_LAYOUT="off")
    return OFF[pdf]
def basep(pdf):
    if pdf not in BASEP: BASEP[pdf] = parse(pdf, src=BASE_SRC, model=None)
    return BASEP[pdf]

for _f in LOGFX_ALL:
    if basep(_f)["rc"] == 0: LOGFX.append(_f)
info(f"tmp {TMPROOT} repo {REPO} baseline {A.baseline_ref} public {len(PUBLIC)} logging-fixtures {len(LOGFX)} (of {len(LOGFX_ALL)})")

# ================================================================ L. licence, docs, module
@section("L")
def licence_docs():
    notice = open(os.path.join(REPO, "app", "NOTICE"), errors="ignore").read() if os.path.exists(os.path.join(REPO, "app", "NOTICE")) else ""
    for k in ("PP-DocLayout-S", "PaddlePaddle", "Apache", "stefanj0/PP-DocLayout-S-ONNX"):
        add(f"L_NOTICE_names_{re.sub(r'[^A-Za-z0-9]+', '_', k)}", k in notice, "")
    add("L_NOTICE_Apache_2.0", bool(re.search(r"Apache(-| License,? (Version )?)2\.0", notice)), "")
    add("L_NOTICE_onnxruntime_named", "onnxruntime" in notice.lower() or "ONNX Runtime" in notice, "informative: onnxruntime (MIT) attribution")
    readme = open(os.path.join(REPO, "README.md"), errors="ignore").read()
    add("L_README_licence_line_PP-DocLayout-S", "PP-DocLayout-S" in readme and "Apache" in readme, "")
    arch = open(os.path.join(REPO, "docs", "architecture.md"), errors="ignore").read()
    add("L_architecture_module_row_layoutdet", bool(re.search(r"^\|\s*`layoutdet\.py`\s*\|", arch, re.M)), "")
    schema = open(os.path.join(REPO, "tests", "audit", "SCHEMA.md"), errors="ignore").read()
    add("L_SCHEMA_layout_events", "layout_events" in schema, ""); add("L_SCHEMA_doc_parse_layout", "doc_parse.layout" in schema or re.search(r"doc_parse[^\n]*layout", schema) is not None, "")
    add("L_layoutdet_module_exists", os.path.isfile(os.path.join(SRC, "layoutdet.py")), "")
    nonp = next((p for p in ("python3", "/usr/bin/python3") if subprocess.run([p, "-c", "import numpy"], capture_output=True).returncode != 0), None)
    if nonp:
        p = subprocess.run([nonp, "-c", f"import sys; sys.path.insert(0, {SRC!r}); import layoutdet"], capture_output=True, text=True)
        add("L_layoutdet_imports_without_numpy_onnxruntime", p.returncode == 0, p.stderr[-300:])
    else: skip("L_layoutdet_imports_without_numpy_onnxruntime", "no interpreter without numpy")
    p = subprocess.run([NOORT, "-c", f"import sys; sys.path.insert(0, {SRC!r}); import layoutdet"], capture_output=True, text=True)
    add("L_layoutdet_imports_without_onnxruntime", p.returncode == 0, p.stderr[-300:])
    p = subprocess.run(["python3", "-I", "-c", "import importlib.util; print(importlib.util.find_spec('layoutdet'))"], capture_output=True, text=True, cwd=TMPROOT)
    add("L_layoutdet_shadows_no_installed_module", p.stdout.strip() == "None", p.stdout)
    for py in (NOORT, DETPY):
        p = subprocess.run([py, "-c", "import importlib.util; print(importlib.util.find_spec('layoutdet'))"], capture_output=True, text=True, cwd=TMPROOT)
        add(f"L_layoutdet_shadows_nothing_in_{'noort' if py == NOORT else 'docvenv'}", p.stdout.strip() == "None", p.stdout)
    p = subprocess.run([NOORT, "-m", "pyflakes"] + sorted(glob.glob(os.path.join(SRC, "*.py"))), capture_output=True, text=True)
    add("L_pyflakes_clean_src", p.returncode == 0 and not p.stdout.strip(), (p.stdout + p.stderr)[-400:])
    ids = [os.path.basename(os.path.dirname(p)) for p in PUBLIC]
    hman = os.path.join(A.holdout, "manifest.json") if A.holdout else ""
    if os.path.exists(hman): ids += [d["doc_id"] for d in json.load(open(hman))["docs"]]
    hits = []
    for f in glob.glob(os.path.join(SRC, "*.py")):
        t = open(f, errors="ignore").read()
        hits += [(os.path.basename(f), i) for i in ids if len(i) > 4 and re.search(r"['\"/]" + re.escape(i) + r"['\"./]", t)]
    add("L_no_fixture_doc_ids_in_src", not hits, hits[:5])
    add("L_install_sh_no_unpinned_main_url", "/resolve/main/" not in open(os.path.join(SRC, "install.sh")).read(), "")
    bp = open(os.path.join(REPO, "app", "bundle_python.sh")).read()
    add("L_bundle_python_no_unpinned_main_url", "/resolve/main/" not in bp, "")

# ================================================================ D. detector on: public + logging fixtures
@section("D")
def detector_on():
    res, sch, rc_bad, err_bad = [], [], [], []
    det_blocks = {k: 0 for k in KINDS}; det_cap = 0; fig_det = 0; para_lines = 0
    for pdf in PUBLIC + LOGFX:
        r = on(pdf)
        if r["rc"] != 0 or r["d"] is None: rc_bad.append((short(pdf), r["rc"], r["err"][-200:])); continue
        if unav_lines(r["err"]): err_bad.append((short(pdf), unav_lines(r["err"])))
        res.append((short(pdf), validate(r["d"]))); sch.append((short(pdf), layout_schema(r["d"], "ok")))
        for p in fd_pages(r["d"]):
            for b in p.get("blocks") or []:
                if b.get("src") == "detector": det_blocks[b.get("kind")] = det_blocks.get(b.get("kind"), 0) + 1
                if b.get("src") == "regex+detector": det_cap += 1
                if b.get("src") == "lines": para_lines += 1
            fig_det += sum(1 for f in p.get("figures") or [] if f.get("bbox_src") == "detector")
    add("D_parse_exit0_every_fixture", not rc_bad, rc_bad[:3]); add("D_no_unavailable_stderr_line_when_ok", not err_bad, err_bad[:3])
    agg("D_on", res); agg("D_on", sch)
    pub_ok = [pdf for pdf in PUBLIC if on(pdf)["d"]]
    allsrc = [(p.get("layout") or {}).get("src") for pdf in pub_ok for p in fd_pages(on(pdf)["d"])]
    add("D_every_public_page_src_detector", allsrc and all(s == "detector" for s in allsrc), {s: allsrc.count(s) for s in set(allsrc)})
    ev_bad = []
    for pdf in pub_ok:
        d = on(pdf)["d"]; lp = [e.get("page") for e in events(d, "layout_page") or []]; fdn = [p.get("n") for p in fd_pages(d)]
        if lp != fdn or events(d, "layout_detector_unavailable"): ev_bad.append((short(pdf), lp, fdn))
    add("D_one_layout_page_event_per_detected_page_no_unavailable", not ev_bad, ev_bad[:3])
    info(f"D detector blocks by kind {det_blocks} captions regex+detector {det_cap} paragraphs lines {para_lines} figures[] bbox_src detector {fig_det}")
    add("D_detector_paragraph_blocks_present", det_blocks.get("paragraph", 0) > 0, det_blocks)
    add("D_bbox_src_detector_present_figures", fig_det > 0, fig_det)
    add("D_detector_figure_and_table_blocks_present", det_blocks.get("figure", 0) > 0 and det_blocks.get("table", 0) > 0, det_blocks)
    # determinism: two parses identical except ms
    for pdf in (REPORT, MULTI):
        a, b = on(pdf), parse(pdf)
        add(f"D_deterministic_except_ms_{short(pdf).split('/')[0]}", a["d"] is not None and strip_ms(a["d"]) == strip_ms(b["d"]), "")
    # dump carries the same layout keys
    # dump is a display tool: its output is cut at a fixed length. Since Phase 5/6 grew figures[], page 1's layout of a
    # report no longer fits, so (like the logging suite) the dump uses the one-figure tiny.pdf, small enough to print whole
    r = parse(os.path.join(FXD, "tiny.pdf"), sub="dump")
    add("D_dump_shows_pages_layout", r["rc"] == 0 and '"layout": {' in r["out"] and '"detector_version": "' + DV + '"' in r["out"] and '"blocks_by_kind"' in r["out"], (r["rc"], r["err"][-200:]))

# ================================================================ H. detector off: fallback = baseline heuristics + 4.4
@section("H")
def detector_off():
    res, sch, diffs, rc_bad, ln_bad, ev_bad = [], [], [], [], [], []
    for pdf in PUBLIC + LOGFX:
        r = off(pdf); b = basep(pdf)
        if r["rc"] != 0 or r["d"] is None: rc_bad.append((short(pdf), r["rc"], r["err"][-200:])); continue
        res.append((short(pdf), validate(r["d"]))); sch.append((short(pdf), layout_schema(r["d"], "unavailable", "disabled")))
        ul = unav_lines(r["err"])
        if ul != ["layout: detector unavailable (disabled)"]: ln_bad.append((short(pdf), ul))
        ev = events(r["d"]) or []
        if not (len(ev) == 1 and ev[0].get("event") == "layout_detector_unavailable" and ev[0].get("reason") == "disabled" and ev[0].get("page") is None and ev[0].get("error") is None):
            ev_bad.append((short(pdf), ev))
        if b["d"] is None: diffs.append((short(pdf), "baseline parse failed", b["err"][-200:])); continue
        bp = {p.get("n"): p for p in fd_pages(b["d"])}
        for p in fd_pages(r["d"]):
            q = qraw = bp.get(p.get("n"))
            if q is not None: q = clip_page(q)
            if q is None: diffs.append((short(pdf), p.get("n"), "page not figs_done at baseline")); continue
            if (p.get("layout") or {}) != {"src": "heuristic", "reason": "disabled"}: diffs.append((short(pdf), p.get("n"), "layout", p.get("layout")))
            added = {f.get("label") for f in p.get("figures") or [] if p6_added(f)}
            nb = [{k: v for k, v in x.items() if k != "kind_src" and (k != "src" or x.get("kind") in ("figure", "table"))} for x in p.get("blocks") or [] if x.get("label") not in added]
            ob = q.get("blocks") or []; lines = p.get("lines") or []
            figs_new = {f.get("label"): f for f in p.get("figures") or [] if not p6_added(f)}; figs_old = {f.get("label"): f for f in q.get("figures") or []}
            figs_raw = {f.get("label"): f for f in qraw.get("figures") or []}
            def inside_page_fix(lab, xb):
                """4.7 (every figure box inside the page), on the fallback path since Phase 5's on-page drawing clusters: a baseline
                box that stuck out of the page may shrink to a box inside the baseline box clipped to the page."""
                g = (figs_raw.get(lab) or {}).get("bbox"); W_, H_ = qraw.get("w") or 1e9, qraw.get("h") or 1e9
                if not (isinstance(g, list) and isinstance(xb, list) and (g[0] < -2 or g[1] < -2 or g[2] > W_ + 2 or g[3] > H_ + 2)): return False
                c = [max(0.0, g[0]), max(0.0, g[1]), min(W_, g[2]), min(H_, g[3])]
                return xb[0] >= c[0] - 0.15 and xb[1] >= c[1] - 0.15 and xb[2] <= c[2] + 0.15 and xb[3] <= c[3] + 0.15
            # contract 5 (gate ruling): on a page with 2+ captions the fallback reserves graphics per caption and splits figures
            # and tables both ways, so a figure/table box may change there, but only inside the baseline's figure/table extent
            old_ft = [y["bbox"] for y in ob if y.get("kind") in ("figure", "table") and isinstance(y.get("bbox"), list)]
            extent = [min(b[0] for b in old_ft), min(b[1] for b in old_ft), max(b[2] for b in old_ft), max(b[3] for b in old_ft)] if old_ft else None
            multi = len(figs_old) >= 2
            def redistributed(xb):
                return multi and extent is not None and isinstance(xb, list) and xb[0] >= extent[0] - 2 and xb[1] >= extent[1] - 2 and xb[2] <= extent[2] + 2 and xb[3] <= extent[3] + 2
            # figures, tables and captions: same set (order may change: a table without its caption now sorts below it)
            key = lambda x: (x.get("kind"), x.get("label"))
            NF = {key(x): x for x in nb if x.get("kind") != "paragraph"}; OF = {key(x): x for x in ob if x.get("kind") != "paragraph"}
            if set(NF) != set(OF): diffs.append((short(pdf), p.get("n"), "fig/table/caption set", sorted(set(NF) ^ set(OF), key=str)))
            for k_ in set(NF) & set(OF):
                x, y = NF[k_], OF[k_]
                if x.get("kind") == "table":
                    xb, yb = x.get("bbox"), y.get("bbox")
                    ok = isinstance(xb, list) and isinstance(yb, list) and all(xb[i] >= yb[i] - 0.15 for i in (0, 1)) and all(xb[i] <= yb[i] + 0.15 for i in (2, 3))
                    cb = (figs_old.get(y.get("label")) or {}).get("caption_bbox")
                    if not ok and isinstance(cb, list) and isinstance(yb, list) and yb[1] >= cb[1] - 2 and yb[3] <= cb[3] + 2 and vov(xb, cb) <= 2:
                        ok = True; info(f"H {short(pdf)} p{p.get('n')} {y.get('label')}: baseline box was only the caption {yb}; caption-free box now {xb} ({x.get('src')})")
                    if not ok and redistributed(xb):
                        ok = True; info(f"H {short(pdf)} p{p.get('n')} {y.get('label')}: redistributed on a multi-caption page {yb} -> {xb}")
                    # 4.4 column rule: a body rebuilt inside its caption's column (inside the baseline box) may change its evidence (src)
                    same_but_src = {k: v for k, v in x.items() if k not in ("bbox", "src")} == {k: v for k, v in y.items() if k not in ("bbox", "src")}
                    if ok and same_but_src and x.get("src") != y.get("src"): info(f"H {short(pdf)} p{p.get('n')} {y.get('label')}: in-column body {yb} -> {xb}, src {y.get('src')} -> {x.get('src')}")
                    if not ok or not same_but_src or (x.get("src") != y.get("src") and xb == yb): diffs.append((short(pdf), p.get("n"), "table", x, y))
                elif x != y:
                    if x.get("kind") == "figure" and inside_page_fix(x.get("label"), x.get("bbox")) and {k: v for k, v in x.items() if k != "bbox"} == {k: v for k, v in y.items() if k != "bbox"}:
                        info(f"H {short(pdf)} p{p.get('n')} {y.get('label')}: baseline box outside the page {figs_raw[y.get('label')].get('bbox')} -> {x.get('bbox')} (4.7)")
                    elif x.get("kind") == "figure" and redistributed(x.get("bbox")) and {k: v for k, v in x.items() if k != "bbox"} == {k: v for k, v in y.items() if k != "bbox"}:
                        info(f"H {short(pdf)} p{p.get('n')} {y.get('label')}: redistributed on a multi-caption page {y.get('bbox')} -> {x.get('bbox')}")
                    else: diffs.append((short(pdf), p.get("n"), x.get("kind"), x, y))
            # paragraphs: unchanged, except lines released by smaller figure/table boxes (4.4 caption-free tables; contract 5
            # redistribution on multi-caption pages)
            def inside_any(li, boxes):
                l = lines[li]; return any(l[0] >= b[0] - 2 and l[1] >= b[1] - 2 and l[2] <= b[2] + 2 and l[3] <= b[3] + 2 for b in boxes)
            old_t = [y["bbox"] for y in ob if y.get("kind") == "table" or (multi and y.get("kind") == "figure")]
            NP = [tuple(x.get("lines") or []) for x in nb if x.get("kind") == "paragraph"]; OP = [tuple(y.get("lines") or []) for y in ob if y.get("kind") == "paragraph"]
            nl = set().union(*map(set, NP)) if NP else set(); ol = set().union(*map(set, OP)) if OP else set()
            released = {li for li in nl - ol if isint(li) and li < len(lines) and inside_any(li, old_t)}
            # Phase 6: lines inside a lexicon-added figure / table or its caption (e.g. "TABLE I") leave the paragraphs, and only those
            added_boxes = [f[k_] for f in p.get("figures") or [] if p6_added(f) for k_ in ("bbox", "caption_bbox") if isinstance(f.get(k_), list)]
            taken6 = {li for li in range(len(lines)) if inside_any(li, added_boxes)} | set().union(*[set(x.get("lines") or []) for x in p.get("blocks") or [] if x.get("label") in added] or [set()])
            if ol - nl - taken6 or nl - ol - released: diffs.append((short(pdf), p.get("n"), "paragraph lines", sorted(ol - nl - taken6)[:6], sorted(nl - ol - released)[:6]))
            touched6 = [set(t) for t in OP if set(t) & taken6]
            untouched_new = sorted(t for t in NP if not set(t) & released and not any(set(t) <= o for o in touched6))
            untouched_old = sorted(t for t in OP if not any(set(t) <= set(u) for u in NP if set(u) & released) and not set(t) & taken6)
            if untouched_new != untouched_old: diffs.append((short(pdf), p.get("n"), "paragraph grouping", len(untouched_new), len(untouched_old)))
            bad_par = [x for x in nb if x.get("kind") == "paragraph" and set(x) != {"kind", "bbox", "lines"}]
            if bad_par: diffs.append((short(pdf), p.get("n"), "paragraph keys", bad_par[:1]))
            if released: info(f"H {short(pdf)} p{p.get('n')}: {len(released)} lines released from baseline table boxes into paragraphs")
            for lab, f in figs_new.items():
                g = figs_old.get(lab)
                if g is None: diffs.append((short(pdf), p.get("n"), "figure missing at baseline", lab)); continue
                if f.get("kind") != "table" and f.get("bbox") != g.get("bbox") and not redistributed(f.get("bbox")) and not inside_page_fix(lab, f.get("bbox")):
                    diffs.append((short(pdf), p.get("n"), "figures[].bbox", lab, f.get("bbox"), g.get("bbox")))
                if f.get("caption_bbox") != g.get("caption_bbox"): diffs.append((short(pdf), p.get("n"), "caption_bbox", lab))
    add("H_parse_exit0_every_fixture", not rc_bad, rc_bad[:3])
    add("H_stderr_one_line_disabled", not ln_bad, ln_bad[:3]); add("H_one_unavailable_event_disabled_page_null_error_null", not ev_bad, ev_bad[:2])
    agg("H_off", res); agg("H_off", sch)
    add("H_off_equals_baseline_heuristics_plus_caption_exclusion", not diffs, diffs[:3])
    info(f"H fallback-vs-baseline differences: {len(diffs)}")
    for d_ in diffs: info(f"H difference {d_}")
    for x in diffs[:12]: info(f"H diff {_s(x)[:300]}")
    # 4.4: a baseline table whose box contained its caption now excludes it (fallback path)
    shrunk = 0
    for pdf in PUBLIC + LOGFX:
        r, b = off(pdf), basep(pdf)
        if not (r["d"] and b["d"]): continue
        bp = {(p.get("n"), f.get("label")): f for p in fd_pages(b["d"]) for f in p.get("figures") or []}
        for p in fd_pages(r["d"]):
            for f in p.get("figures") or []:
                g = bp.get((p.get("n"), f.get("label")))
                if f.get("kind") == "table" and g and vov(g["bbox"], g["caption_bbox"]) > 2 and vov(f["bbox"], f["caption_bbox"]) <= 2: shrunk += 1
    add("H_fallback_tables_drop_baseline_caption_overlap", shrunk > 0, f"{shrunk} tables")

# ================================================================ R. every fallback reason
def reason_checks(tag, r, reason, ref, error=None, page=None):
    """r: parse result under the failure; ref: SPEAKEASY_LAYOUT=off parse of the same file."""
    d = r["d"]
    add(f"R_{tag}_exit0", r["rc"] == 0 and d is not None, (r["rc"], r["err"][-300:]))
    if d is None: return
    ul = unav_lines(r["err"]); want = f"layout: detector unavailable ({reason})"
    add(f"R_{tag}_stderr_line", ul.count(want) == (len([e for e in events(d) or [] if e.get("reason") == reason]) or 1) and all(l == want for l in ul), ul)
    ev = [e for e in events(d) or [] if e.get("event") == "layout_detector_unavailable"]
    if page is None:
        add(f"R_{tag}_one_event_reason", len(ev) == 1 and ev[0].get("reason") == reason and ev[0].get("page") is None, ev)
        add(f"R_{tag}_event_error", bool(ev) and (ev[0].get("error") == error if error is not None else ev[0].get("error") is None or isinstance(ev[0].get("error"), str)), ev[:1])
        add(f"R_{tag}_no_layout_page_events", not events(d, "layout_page"), events(d, "layout_page"))
        add(f"R_{tag}_top_layout_unavailable", (d.get("layout") or {}).get("status") == "unavailable" and (d.get("layout") or {}).get("reason") == reason, d.get("layout"))
        add(f"R_{tag}_every_page_heuristic_reason", all(p.get("layout") == {"src": "heuristic", "reason": reason} for p in fd_pages(d)) and bool(fd_pages(d)), [p.get("layout") for p in fd_pages(d)][:3])
        add(f"R_{tag}_blocks_identical_to_off", ref["d"] is not None and page_blocks(d) == page_blocks(ref["d"]) and page_figs(d) == page_figs(ref["d"]), "")
    report(f"R_{tag}", [x for x in layout_schema(d) if x[0] in ("top_layout_schema", "pages_layout_schema_every_figs_done_page", "layout_events_schema")])
    bad = [x for x in validate(d) if not x[1]]
    add(f"R_{tag}_invariants", not bad, bad[:2])

@section("R")
def reasons():
    ref = off(REPORT)
    reason_checks("disabled_env", ref, "disabled", ref)
    reason_checks("disabled_beats_bad_model", parse(REPORT, model=FLIP, SPEAKEASY_LAYOUT="off"), "disabled", ref)
    reason_checks("no_onnxruntime", parse(REPORT, py=NOORT), "no-onnxruntime", ref)
    reason_checks("model_missing_override", parse(REPORT, model=os.path.join(TMPROOT, "nope", MODEL_NAME)), "model-missing", ref)
    reason_checks("model_mismatch_byte_flip", parse(REPORT, model=FLIP), "model-mismatch", ref)
    reason_checks("model_mismatch_truncated", parse(REPORT, model=TRUNC), "model-mismatch", ref)
    reason_checks("model_mismatch_not_onnx_same_size", parse(REPORT, model=NOTONNX), "model-mismatch", ref)
    r = parse(REPORT, fake={"FAKE_ORT_MODE": "load-fail"})
    if r["fake_log"] is not None and not any(e.get("ev") == "session" for e in r["fake_log"]):
        skip("R_load_failed", f"fake onnxruntime never used (parse imports it some other way): {r['err'][-200:]}")
    else: reason_checks("load_failed", r, "load-failed", ref, error="RuntimeError")
    # inference-failed on one page only (2nd session.run raises)
    full = on(REPORT); r = parse(REPORT, fake={"FAKE_ORT_FAIL_RUNS": "2"})
    runs = [e for e in r["fake_log"] or [] if e.get("ev") == "run"]
    if not runs: skip("R_inference_failed", f"fake onnxruntime session.run never called: {r['err'][-200:]}"); return
    d = r["d"]
    add("R_inference_failed_exit0", r["rc"] == 0 and d is not None, r["err"][-300:])
    if d is None: return
    ev = [e for e in events(d) or [] if e.get("event") == "layout_detector_unavailable"]
    fp = ev[0].get("page") if len(ev) == 1 else None
    add("R_inference_failed_one_event_with_page_and_error", len(ev) == 1 and ev[0].get("reason") == "inference-failed" and isint(fp) and ev[0].get("error") == "RuntimeError", ev)
    add("R_inference_failed_stderr_line", unav_lines(r["err"]) == ["layout: detector unavailable (inference-failed)"], unav_lines(r["err"]))
    P = {p.get("n"): p for p in fd_pages(d)}; F = {p.get("n"): p for p in fd_pages(full["d"])}; O = {p.get("n"): p for p in fd_pages(ref["d"])}
    add("R_inference_failed_page_heuristic_reason", fp in P and P[fp].get("layout") == {"src": "heuristic", "reason": "inference-failed"}, P.get(fp, {}).get("layout"))
    others = [n for n in P if n != fp]
    add("R_inference_failed_neighbours_keep_detector", bool(others) and all((P[n].get("layout") or {}).get("src") == "detector" for n in others), {n: (P[n].get("layout") or {}).get("src") for n in P})
    add("R_inference_failed_page_blocks_equal_off", fp in P and fp in O and P[fp].get("blocks") == O[fp].get("blocks") and P[fp].get("figures") == O[fp].get("figures"), "")
    add("R_inference_failed_other_pages_equal_full_detector", all(strip_ms(P[n].get("blocks")) == strip_ms(F.get(n, {}).get("blocks")) and P[n].get("figures") == F.get(n, {}).get("figures") for n in others), "")
    add("R_inference_failed_layout_page_events_for_others", sorted(e.get("page") for e in events(d, "layout_page") or []) == sorted(others), events(d, "layout_page"))
    bad = [x for x in validate(d) if not x[1]]; add("R_inference_failed_invariants", not bad, bad[:2])
    info(f"R inference-failed page {fp} (session.run calls {len(runs)})")

# ================================================================ Q. model lookup order and sha verification
def bundle_tree(model_src):
    root = os.path.join(TMPROOT, f"bundle_{time.time_ns()}", "Resources"); be = os.path.join(root, "backend"); os.makedirs(be)
    for f in glob.glob(os.path.join(SRC, "*.py")): shutil.copy(f, be)
    if model_src: os.makedirs(os.path.join(root, "models")); shutil.copyfile(model_src, os.path.join(root, "models", MODEL_NAME))
    return be
def home_with(model_src):
    h = mkhome("q")
    if model_src: os.makedirs(os.path.join(h, "models")); shutil.copyfile(model_src, os.path.join(h, "models", MODEL_NAME))
    return h
@section("Q")
def lookup():
    pdf = os.path.join(PUB, "syn-fig5-01", "doc.pdf")
    def st(r): return ((r["d"] or {}).get("layout") or {}).get("status"), ((r["d"] or {}).get("layout") or {}).get("reason")
    cases = [
        ("override_only", dict(model=GOOD), ("ok", None)),
        ("home_only", dict(model=None, home=home_with(GOOD)), ("ok", None)),
        ("bundle_only", dict(model=None, src=bundle_tree(GOOD)), ("ok", None)),
        ("nothing_anywhere", dict(model=None, src=bundle_tree(None)), ("unavailable", "model-missing")),
        ("override_beats_bad_home", dict(model=GOOD, home=home_with(FLIP)), ("ok", None)),
        ("bad_override_beats_good_home", dict(model=FLIP, home=home_with(GOOD)), ("unavailable", "model-mismatch")),
        ("missing_override_no_fallthrough", dict(model=os.path.join(TMPROOT, "absent.onnx"), home=home_with(GOOD), src=bundle_tree(GOOD)), ("unavailable", "model-missing")),
        ("bad_home_beats_good_bundle", dict(model=None, home=home_with(FLIP), src=bundle_tree(GOOD)), ("unavailable", "model-mismatch")),
        ("good_home_beats_bad_bundle", dict(model=None, home=home_with(GOOD), src=bundle_tree(FLIP)), ("ok", None)),
        ("bad_bundle_only", dict(model=None, src=bundle_tree(TRUNC)), ("unavailable", "model-mismatch")),
        ("explicit_on", dict(model=GOOD, SPEAKEASY_LAYOUT="on"), ("ok", None)),
    ]
    for name, kw, want in cases:
        r = parse(pdf, **kw); got = st(r)
        add(f"Q_{name}", r["rc"] == 0 and got == want, (got, want, r["err"][-200:]))
    # sha checked before loading: no session is created for a mismatching file (fake onnxruntime counts sessions)
    r = parse(pdf, model=FLIP, fake={}); g = parse(pdf, fake={})
    if not any(e.get("ev") == "session" for e in g["fake_log"] or []): skip("Q_sha_checked_before_session", "fake onnxruntime not used"); return
    add("Q_sha_checked_before_session_mismatch_no_session", st(r) == ("unavailable", "model-mismatch") and not any(e.get("ev") == "session" for e in r["fake_log"] or []), r["fake_log"])
    r = parse(pdf, model=NOTONNX, fake={})
    add("Q_same_size_not_onnx_never_loaded", st(r) == ("unavailable", "model-mismatch") and not r["fake_log"], r["fake_log"])
    r = parse(pdf, model=GOOD, fake={}, SPEAKEASY_LAYOUT="off")
    add("Q_disabled_never_loads", not r["fake_log"], r["fake_log"])

# ================================================================ S. sessions, --sel / --prev
def page_text(d, n, k=3):
    for p in pages(d):
        if p.get("n") == n: return "\n".join(str(l[4]) for l in (p.get("lines") or [])[:k] if len(l) > 4)
    return ""
@section("S")
def sessions():
    r = parse(REPORT, fake={})
    ss = [e for e in r["fake_log"] or [] if e.get("ev") == "session"]; rs = [e for e in r["fake_log"] or [] if e.get("ev") == "run"]
    if not ss: skip("S_sessions", f"fake onnxruntime not used: {r['err'][-200:]}"); return
    add("S_one_session_per_parse", len(ss) == 1 and len({e.get("pid") for e in ss}) == 1, ss)
    add("S_one_run_per_detected_page", len(rs) == len(fd_pages(r["d"])), (len(rs), len(fd_pages(r["d"]))))
    full = on(MULTI)["d"]
    if not full: add("S_multi_parse", False, on(MULTI)["err"][-200:]); return
    N = len(pages(full)); s1 = page_text(full, 1)
    a = parse(MULTI, sel=s1, fake={})
    fa = sorted(p.get("n") for p in fd_pages(a["d"]))
    add("S_sel_detects_selection_page_and_neighbours_only", a["rc"] == 0 and fa and set(fa) <= {1, 2} and 1 in fa, fa)
    add("S_sel_layout_page_events_equal_detected_pages", sorted(e.get("page") for e in events(a["d"], "layout_page") or []) == fa, events(a["d"]))
    add("S_sel_one_run_per_detected_page", len([e for e in a["fake_log"] or [] if e.get("ev") == "run"]) == len(fa), a["fake_log"])
    nd = [p for p in pages(a["d"]) if p.get("figs_done") is not True]
    add("S_text_only_pages_have_no_detector_layout", all((p.get("layout") or {}).get("src") != "detector" for p in nd), [(p.get("n"), p.get("layout")) for p in nd][:3])
    if N >= 4:
        s4 = page_text(full, 4); b = parse(MULTI, sel=s4, prev=a["d"], fake={})
        fb = sorted(p.get("n") for p in fd_pages(b["d"])); new = sorted(e.get("page") for e in events(b["d"], "layout_page") or [])
        A1 = {p.get("n"): p for p in fd_pages(a["d"])}; B1 = {p.get("n"): p for p in fd_pages(b["d"])}
        add("S_prev_pages_keep_original_layout", all(B1.get(n, {}).get("layout") == A1[n].get("layout") for n in A1), [(n, B1.get(n, {}).get("layout"), A1[n].get("layout")) for n in A1][:2])
        add("S_prev_events_only_this_invocation", new and not (set(new) & set(A1)) and set(new) <= {3, 4, 5} and set(new) <= set(fb), (new, sorted(A1), fb))
        add("S_prev_runs_only_new_pages", len([e for e in b["fake_log"] or [] if e.get("ev") == "run"]) == len(new), b["fake_log"])
        c = parse(MULTI, sel=s1, prev=b["d"], fake={})
        add("S_no_detected_page_loads_nothing", c["rc"] == 0 and not c["fake_log"] and events(c["d"]) == [], (c["fake_log"], events(c["d"])))
    else: skip("S_prev", f"{N} pages")

# ================================================================ F. Fig. 5: table box stays out of the neighbouring column
@section("F")
def fig5():
    gt = json.load(open(os.path.join(FXD, "fig5_gt.json")))["boxes"]
    for mode, r in (("detector", on(FIG5)), ("heuristic", off(FIG5))):
        d = r["d"]
        if not d: add(f"F_{mode}_parse", False, r["err"][-200:]); continue
        p = fd_pages(d)[0]; lines = p.get("lines") or []
        t = next((f for f in p.get("figures") or [] if f.get("label") == "Table 1"), None)
        if t is None: add(f"F_{mode}_table1_found", False, [f.get("label") for f in p.get("figures") or []]); continue
        tb, cb = t["bbox"], t["caption_bbox"]
        right = [i for i, l in enumerate(lines) if str(l[4]).startswith("Right column sentence")]
        inside = [i for i in right if tb[0] <= (lines[i][0] + lines[i][2]) / 2 <= tb[2] and tb[1] <= (lines[i][1] + lines[i][3]) / 2 <= tb[3]]
        paras = set().union(*[set(b.get("lines") or []) for b in p.get("blocks") or [] if b.get("kind") == "paragraph"] or [set()])
        exp = [gt["table1"][0], cb[3], gt["table1"][2], gt["table1"][3]]
        info(f"F {mode} Table 1 bbox {tb} src {t.get('bbox_src')} caption {cb} expected~{exp} iou {iou(tb, exp):.2f}; Figure 5 {next((f.get('bbox') for f in p.get('figures') or [] if f.get('label') == 'Figure 5'), None)} gt {gt['fig5']}")
        add(f"F_{mode}_table1_excludes_caption", vov(tb, cb) <= 2.0, (tb, cb))
        if mode == "detector":
            add("F_detector_table1_src_detector", t.get("bbox_src") == "detector", t.get("bbox_src"))
            add("F_detector_table1_not_into_right_column_x1_le_305", tb[2] <= 305.0, tb)
            add("F_detector_table1_contains_no_right_column_line", not inside, inside)
            add("F_detector_right_column_lines_are_paragraphs", bool(right) and all(i in paras for i in right), [i for i in right if i not in paras])
            add("F_detector_table1_iou_ge_0.75_vs_body", iou(tb, exp) >= 0.75, (tb, exp))
            f5 = next((f for f in p.get("figures") or [] if f.get("label") == "Figure 5"), None)
            add("F_detector_figure5_iou_ge_0.75", f5 is not None and iou(f5["bbox"], gt["fig5"]) >= 0.75, (f5 or {}).get("bbox"))
        else:
            info(f"F heuristic Table 1 right-column lines inside the box: {len(inside)} of {len(right)} (not gated)")
    # public Fig. 5 layout fixture, detector on: table boxes vs GT
    g = json.load(open(os.path.join(PUB, "syn-fig5-01", "gt.json"))); d = on(os.path.join(PUB, "syn-fig5-01", "doc.pdf"))["d"]
    if d:
        P = {p.get("n"): p for p in fd_pages(d)}; rows = []
        for gp in g["pages"]:
            for b in gp["blocks"]:
                if b["kind"] == "table":
                    pr = [x for x in (P.get(gp["n"]) or {}).get("blocks") or [] if x.get("kind") == "table" and x.get("label") == b.get("label")]
                    rows.append((gp["n"], b.get("label"), round(iou(pr[0]["bbox"], b["bbox"]), 2) if pr else None))
        info(f"F syn-fig5-01 table IoU vs GT (detector): {rows}")
        add("F_public_fig5_tables_iou_ge_0.75", rows and all(x[2] is not None and x[2] >= 0.75 for x in rows), rows)

# ================================================================ P. privacy of the new fields
ALLOWED_STR = set(REASONS) | {"ok", "unavailable", "detector", "heuristic", "layout_page", "layout_detector_unavailable", DV}
LAYOUT_KEYS = {"status", "detector_version", "reason", "src", "ms", "blocks_by_kind", "event", "page", "error"} | set(KINDS)
def text_free(v, path="$", errs=None):
    errs = [] if errs is None else errs
    if isinstance(v, dict):
        for k, x in v.items():
            if k not in LAYOUT_KEYS: errs.append(f"{path}.key[{len(k)}]")
            text_free(x, f"{path}.{k if k in LAYOUT_KEYS else '?'}", errs)
    elif isinstance(v, list):
        for i, x in enumerate(v): text_free(x, f"{path}[{i}]", errs)
    elif isinstance(v, str):
        if v not in ALLOWED_STR and not (path.endswith(".error") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,80}", v)): errs.append(f"{path}(len {len(v)})")
    elif not (v is None or isinstance(v, (bool, int, float))): errs.append(f"{path}:{type(v).__name__}")
    return errs
def doc_words(d):
    w = set()
    for p in pages(d):
        for l in p.get("lines") or []:
            for t in re.findall(r"[A-Za-z]{6,}", str(l[4]) if len(l) > 4 else ""): w.add(t.lower())
    return w - {x.lower() for x in ALLOWED_STR} - {"detector", "heuristic", "caption", "figure", "paragraph", "unavailable", "disabled", "mismatch", "missing", "failed", "inference", "engine", "pdfkit", "status", "version", "layout", "blocks", "reason", "events"}
VOCAB = set(re.findall(r"[a-z]{6,}", json.dumps(sorted(ALLOWED_STR | LAYOUT_KEYS)).lower()))
def leaked_words(words, obj):
    toks = set(re.findall(r"[a-z]{6,}", json.dumps(obj).lower()))
    return sorted((words & toks) - VOCAB)
def has_path(obj): return "/" in json.dumps(obj).replace(DV, "")
def layout_fields(d): return [(d or {}).get("layout"), (d or {}).get("layout_events"), [p.get("layout") for p in pages(d)]]
@section("P")
def privacy():
    for lt in (None, "1"):
        tag = "logtext1" if lt else "logtext_unset"; bad = []
        for pdf in PUBLIC + [FIG5]:
            for r in ((on(pdf) if not lt else parse(pdf, SPEAKEASY_LOG_TEXT=lt)), parse(pdf, model=FLIP, SPEAKEASY_LOG_TEXT=lt)):
                if not r["d"]: bad.append((short(pdf), "no parse")); continue
                lf = layout_fields(r["d"]); e = text_free(lf); hits = leaked_words(doc_words(r["d"]), lf)
                if e or hits or has_path(lf): bad.append((short(pdf), e[:3], len(hits)))
        add(f"P_parse_layout_fields_text_free_{tag}", not bad, bad[:3])

# ================================================================ C. CLI runs log (layout_events, doc_parse.layout, pdfkit-engine)
sys.path.insert(0, BENCH); sys.path.insert(0, os.path.join(REPO, "tests", "audit", "boxacc"))
def cli_case(doc="syn-fig5-01", idx=0):
    import run_bench as RB
    c = json.load(open(os.path.join(PUB, doc, "cases.json")))[idx]; g = json.load(open(os.path.join(PUB, doc, "gt.json")))
    return RB, c, {p["n"]: p["w"] for p in g["pages"]}[c["page"]]
def speak(RB, mock, case, pw, home, n=1, play="0.3", bin_=None, **kw):
    """Runs the bench-style speak CLI n times in one home; returns [(rc, runs record or None, stderr)]."""
    d = os.path.dirname(home); pdf = os.path.join(PUB, case["doc_id"], "doc.pdf"); cp = os.path.join(d, f"{case['doc_id']}.pdf"); shutil.copyfile(pdf, cp)
    hint = os.path.join(d, "hint.json"); lbp = os.path.join(d, "lines.json")
    json.dump({"app": "Preview", "bundle": "com.apple.Preview", "title": f"{case['doc_id']}.pdf", "ax_document": "file://" + cp}, open(hint, "w"))
    cap, lb = RB.geometry_for(case, pw); json.dump(lb, open(lbp, "w"))
    open(mock.reply, "w", encoding="utf-8").write(" ".join(s["text"] for s in case["script"]))
    e = penv(home, SPEAKEASY_LLM_URL=f"http://127.0.0.1:{mock.port}/v1", SPEAKEASY_MODEL="mock-text", SPEAKEASY_LLM="local", SPEAKEASY_VISION="never", SPEAKEASY_GROUND="off",
             SPEAKEASY_TTS="say", SPEAKEASY_DOC_READ="local", SPEAKEASY_DOC_PYTHON=DETPY, SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker", "speakeasy-track"),
             SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1", SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_TEXT_HIGHLIGHT="on", SPEAKEASY_FIGURE_CUES="on", SPEAKEASY_KEEP_CAPTURE="0",
             STUB_PLAY_SEC=play, SPEAKEASY_LAYOUT_MODEL=GOOD, PATH=":".join(RB.STUBS + [os.environ.get("PATH", "/usr/bin:/bin")]))
    for k, v in kw.items():
        if v is None: e.pop(k, None)
        else: e[k] = str(v)
    out = []
    for i in range(n):
        k0 = len(jl(os.path.join(home, "logs", "runs.jsonl")))
        p = subprocess.run([bin_ or CLI, "speak", "--capture-method", "bounds", f"--capture-rect={cap}", "--line-bounds", lbp, "--doc-hint", hint, "--run-id", f"p4-{i}"],
                           input="\n".join(it["text"] for it in case["selection"]), capture_output=True, text=True, env=e, timeout=180)
        recs = jl(os.path.join(home, "logs", "runs.jsonl"))[k0:]
        out.append((p.returncode, recs[-1] if recs else None, p.stderr))
    return out
def cache_entries(home):
    out = []
    for f in sorted(glob.glob(os.path.join(home, "cache", "doc", "*.json")), key=os.path.getmtime, reverse=True):
        try: out.append(json.load(open(f)))
        except Exception: pass
    return out
TRK = {}
def tracker_exe():
    """Builds the tracker package with the current src/tracker.swift once into the temp root (PDFKit text path needs it)."""
    if "exe" in TRK: return TRK["exe"]
    TRK["exe"] = None
    if not shutil.which("swift"): TRK["why"] = "no swift"; return None
    pkg = os.path.join(TMPROOT, "trk", "pkg"); shutil.copytree(os.path.join(REPO, "tracker"), pkg, symlinks=False, ignore=shutil.ignore_patterns(".build"))
    ts = os.path.join(pkg, "Sources", "speakeasy-track", "tracker.swift")
    if os.path.lexists(ts): os.remove(ts)
    shutil.copy(os.path.join(SRC, "tracker.swift"), ts)
    p = subprocess.run(["swift", "build"], cwd=pkg, capture_output=True, text=True, timeout=900)
    exe = os.path.join(pkg, ".build", "debug", "speakeasy-track")
    if p.returncode or not os.path.exists(exe): TRK["why"] = (p.stdout + p.stderr)[-200:]; return None
    TRK["exe"] = exe; return exe
@section("C")
def runs_log():
    RB, case, pw = cli_case()
    mroot = os.path.join(TMPROOT, "mock"); os.makedirs(mroot, exist_ok=True); mock = RB.Mock(mroot)
    try:
        for lt in (None, "1"):
            tag = "logtext1" if lt else "logtext_unset"
            h = mkhome("cli"); res = speak(RB, mock, case, pw, h, n=2, SPEAKEASY_LOG_TEXT=lt)
            (rc1, r1, e1), (rc2, r2, e2) = res
            add(f"C_{tag}_runs_ok", rc1 == 0 and rc2 == 0 and r1 is not None and r2 is not None, (rc1, rc2, e1[-200:], e2[-200:]))
            if not (r1 and r2): continue
            info(f"C {tag} run1 doc_ready_ms {r1.get('doc_ready_ms')} first_sentence_ms {r1.get('first_sentence_ms')} page {r1.get('page')} doc_parse keys {sorted((r1.get('doc_parse') or {}).keys())}")
            ev = r1.get("layout_events"); ce = cache_entries(h)
            add(f"C_{tag}_miss_run_has_layout_events", isinstance(ev, list) and any(e.get("event") == "layout_page" for e in ev), ev if not isinstance(ev, list) else [e.get("event") for e in ev])
            if ce and isinstance(ce[0].get("layout_events"), list) and isinstance(ev, list):
                add(f"C_{tag}_layout_events_copied_from_parse", ev == ce[0]["layout_events"], (len(ev), len(ce[0]["layout_events"])))
            add(f"C_{tag}_cache_hit_run_no_layout_events", "layout_events" not in r2, r2.get("layout_events"))
            dl = (r1.get("doc_parse") or {}).get("layout")
            pg = next((p for d_ in ce for p in fd_pages(d_) if p.get("n") == r1.get("page")), None)
            want = {k: v for k, v in ((pg or {}).get("layout") or {}).items() if k != "detector_version"} if pg else None
            add(f"C_{tag}_doc_parse_layout_is_page_layout_without_version", isinstance(dl, dict) and dl.get("src") == "detector" and "detector_version" not in dl and (want is None or dl == want), (dl, want))
            dl2 = (r2.get("doc_parse") or {}).get("layout")
            add(f"C_{tag}_doc_parse_layout_on_cache_hit_too", isinstance(dl2, dict) and dl2.get("src") == "detector", dl2)
            errs = text_free([ev, dl, dl2])
            words = {w for st in case["selection"] + case["script"] for w in re.findall(r"[a-z]{6,}", st["text"].lower())}
            hits = leaked_words(words, [ev, dl, dl2])
            add(f"C_{tag}_runs_layout_fields_text_free", not errs and not hits and not has_path([ev, dl, dl2]), (errs[:3], hits[:3]))
            pa = subprocess.run([NOORT, os.path.join(REPO, "tests", "audit", "privacy_audit.py"), "--log", os.path.join(h, "logs", "runs.jsonl"), "--image", os.path.join(TMPROOT, "noimg.png")],
                                capture_output=True, text=True, env=penv(h, SPEAKEASY_LOG_TEXT=lt))
            try: la = json.loads(pa.stdout); info(f"C {tag} privacy_audit.py label_audit {la.get('label_audit', {}).get('verdict')} violations {la.get('label_audit', {}).get('violations')} PRIVACY_LEAK {'PRIVACY_LEAK' in la} (tool predates layout keys; informational)")
            except Exception: info(f"C {tag} privacy_audit.py unreadable output rc {pa.returncode}")
            vers = [(k, v) for d_ in ce for k, v in d_.items() if re.search(r"version|^v$", k, re.I) and isint(v)]
            if vers: add(f"C_{tag}_cache_parse_version_6", any(v >= 6 for _, v in vers), vers)
        # detector off (env, then config file) and pdfkit paths
        for tag in ("env", "config"):
            h = mkhome("clioff")
            if tag == "config": open(os.path.join(h, "config"), "w").write("SPEAKEASY_LAYOUT=off\n")
            (rc, r, e), = speak(RB, mock, case, pw, h, **({"SPEAKEASY_LAYOUT": "off"} if tag == "env" else {}))
            ev = (r or {}).get("layout_events")
            add(f"C_off_{tag}_runs_layout_events_disabled", isinstance(ev, list) and [x.get("reason") for x in ev if x.get("event") == "layout_detector_unavailable"] == ["disabled"] and not any(x.get("event") == "layout_page" for x in ev), ev)
            add(f"C_off_{tag}_doc_parse_layout_heuristic_disabled", ((r or {}).get("doc_parse") or {}).get("layout") == {"src": "heuristic", "reason": "disabled"}, ((r or {}).get("doc_parse") or {}).get("layout"))
        trk = tracker_exe()
        if not trk: info(f"C no tracker build ({TRK.get('why')}): PDFKit runs use no tracker")
        for tag, kw in (("pdf_engine_pdfkit", dict(SPEAKEASY_PDF_ENGINE="pdfkit")), ("no_doc_python", dict(SPEAKEASY_DOC_PYTHON=os.path.join(TMPROOT, "no-python", "python3")))):
            if trk: kw["SPEAKEASY_TRACKER"] = trk
            h = mkhome("pk"); (rc, r, e), = speak(RB, mock, case, pw, h, **kw)
            ev = (r or {}).get("layout_events")
            if (r or {}).get("doc_cache") == "no-pdf-engine":
                skip(f"C_{tag}_runs_one_pdfkit_engine_event", f"no PDF engine at all in this harness (doc_cache no-pdf-engine, tracker {'built' if trk else 'missing'}): the PDFKit path did not run"); continue
            info(f"C {tag} rc {rc} " + _s({k: (r or {}).get(k) for k in ("page", "doc_map_method", "doc_cache", "doc_note", "doc_source", "doc_ms", "layout_events")}) + " keys " + _s(sorted(r or {})))
            ok = isinstance(ev, list) and len(ev) == 1 and ev[0].get("event") == "layout_detector_unavailable" and ev[0].get("reason") == "pdfkit-engine" and set(ev[0]) == {"event", "reason", "error", "detector_version", "page"} and ev[0].get("page") is None
            add(f"C_{tag}_runs_one_pdfkit_engine_event", rc == 0 and ok, (rc, ev, e[-200:]))
            hb = mkhome("pkb"); (rcb, rb, eb), = speak(RB, mock, case, pw, hb, bin_=os.path.join(BASE_SRC, "speakeasy"), **kw)
            info(f"C {tag} baseline {A.baseline_ref}: page {(rb or {}).get('page')} doc_map_method {(rb or {}).get('doc_map_method')} doc_cache {(rb or {}).get('doc_cache')} doc_note {(rb or {}).get('doc_note')}")
            add(f"C_{tag}_pdfkit_doc_job_no_exception", not re.search(r"[A-Za-z]+(Error|Exception)\b", str((r or {}).get("doc_note") or "")), (r or {}).get("doc_note"))
            add(f"C_{tag}_pdfkit_page_and_map_method_as_baseline", rb is not None and (r or {}).get("page") == rb.get("page") and (r or {}).get("doc_map_method") == rb.get("doc_map_method"),
                ((r or {}).get("page"), (r or {}).get("doc_map_method"), (rb or {}).get("page"), (rb or {}).get("doc_map_method")))
            add(f"C_{tag}_no_detector_layout_in_doc_parse", ((r or {}).get("doc_parse") or {}).get("layout", {}).get("src") != "detector", ((r or {}).get("doc_parse") or {}).get("layout"))
    finally: mock.stop()

# ================================================================ T. timing (reported)
@section("T")
def timing():
    allms, bad = [], []
    for pdf in PUBLIC:
        g = json.load(open(os.path.join(os.path.dirname(pdf), "gt.json"))); n = next(p["n"] for p in g["pages"] if p.get("blocks_scored"))
        full = on(pdf)["d"]; sel = page_text(full, n) if full else ""
        a = parse(pdf, sel=sel); b = parse(pdf, sel=sel, model=None)
        ms = [(p.get("layout") or {}).get("ms") for p in fd_pages(a["d"])] if a["d"] else []
        allms += [m for m in ms if isint(m)]
        info(f"T {short(pdf).split('/')[0]} sel page {n}: cold parse-pdf --sel wall {a['ms']} ms with model, {b['ms']} ms without; detector ms/page {ms}")
        if a["rc"] != 0 or b["rc"] != 0 or not ms or not all(isint(m) for m in ms): bad.append((short(pdf), a["rc"], b["rc"], ms))
        lp = sorted(e.get("page") for e in events(a["d"], "layout_page") or [])
        if lp != sorted(p.get("n") for p in fd_pages(a["d"])) or not set(lp) <= {n - 1, n, n + 1}: bad.append((short(pdf), "detected pages", lp, n))
    add("T_sel_parses_report_detector_ms_per_page", not bad, bad[:3])
    if allms:
        s = sorted(allms); info(f"T detector ms/page over --sel parses: p50 {s[max(0, -(-len(s) // 2) - 1)]} p95 {s[max(0, -(-len(s) * 95 // 100) - 1)]} max {s[-1]} n {len(s)}")

# ================================================================ I. install.sh
# Since Phase 6, install.sh and app/bundle_python.sh also fetch and verify the block classifier and heron (phase6-block-classifier.md;
# the bundle refuses to build without them). Every I / U scenario starts with verified copies of those two in the home's model
# cache (APFS clones of the read-only install copies), so these checks stay about the layout model and see no Phase 6 download.
P6_MODELS = [A.figcls_model, A.layout2_model]
def seed_p6_models(home):
    d = os.path.join(home, "models"); os.makedirs(d, exist_ok=True)
    for m in P6_MODELS:
        dst = os.path.join(d, os.path.basename(m))
        if subprocess.run(["cp", "-c", m, dst], capture_output=True).returncode != 0: shutil.copyfile(m, dst)
def run_install(tag, mode, pre=None, venv_py=None):
    h = mkhome("inst" + tag); log = os.path.join(os.path.dirname(h), "stub.jsonl"); open(log, "w").close(); seed_p6_models(h)
    st = p4stubs.make_install_stubs(TMPROOT, sys.executable, DETPY)
    p4stubs.make_fake_venv(h, venv_py or DETPY)
    mp = os.path.join(h, "models", MODEL_NAME)
    if pre: os.makedirs(os.path.dirname(mp), exist_ok=True); shutil.copyfile(pre, mp)
    before = (os.stat(mp).st_ino, os.stat(mp).st_mtime_ns, sha256(mp)) if os.path.exists(mp) else None
    e = penv(h, PATH=st + ":" + os.environ.get("PATH", "/usr/bin:/bin"), SPEAKEASY_SKIP_KOKORO="1", STUB_LOG=log, STUB_CURL_MODE=mode, STUB_MODEL_GOOD=GOOD, STUB_MODEL_CORRUPT=FLIP,
             STUB_MODEL_TRUNC=TRUNC, https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
             ALL_PROXY="http://127.0.0.1:9", no_proxy="", NO_PROXY="")
    p = subprocess.run(["bash", os.path.join(SRC, "install.sh")], capture_output=True, text=True, env=e, cwd=os.path.dirname(h), timeout=600)
    calls = jl(log); fetch = [c for c in calls if c.get("tool") in ("curl", "wget") and any("huggingface" in str(x) or "pp_doclayout" in str(x) for x in c.get("argv") or [])]
    parts = glob.glob(os.path.join(h, "models", "*.part")) + glob.glob(os.path.join(h, "models", ".*part*"))
    return {"rc": p.returncode, "out": p.stdout + p.stderr, "home": h, "mp": mp, "calls": calls, "fetch": fetch, "parts": parts, "before": before,
            "after": (os.stat(mp).st_ino, os.stat(mp).st_mtime_ns, sha256(mp)) if os.path.exists(mp) else None, "rawlog": open(log).read()}
ERR_RE = lambda got: re.compile(r"^ERROR: layout model checksum mismatch \(expected " + SHA + r", got " + got + r"\); PDF layout detection off$", re.M)
WARN_RE = re.compile(r"^WARNING: .*PDF layout detection off\s*$", re.M)
@section("I")
def install():
    r = run_install("fresh", "good")
    add("I_fresh_download_exit0", r["rc"] == 0, r["out"][-300:])
    add("I_fresh_download_installed_verified", r["after"] is not None and r["after"][2] == SHA and os.path.getsize(r["mp"]) == SIZE, r["after"])
    add("I_fresh_download_no_part_left", not r["parts"], r["parts"])
    if not r["fetch"]: skip("I_downloader_is_curl_or_wget", "no stubbed curl/wget fetch seen: the installer downloads some other way; download scenarios below cannot be stubbed"); 
    else: add("I_fetch_pinned_revision_url", all(any(REV in str(x) and "/resolve/" in str(x) for x in c["argv"]) for c in r["fetch"]), [c["argv"] for c in r["fetch"]])
    add("I_fresh_rest_of_install_finished", "Config defaults:" in r["out"] and "Open ~/Applications/Speakeasy.app" in r["out"], r["out"][-200:])
    add("I_no_launchctl_or_kokoro_touched", not any(c.get("tool") == "launchctl" for c in r["calls"]), "")
    # re-run in the same home: kept, no network
    h = r["home"]; mp = r["mp"]; b0 = (os.stat(mp).st_ino, os.stat(mp).st_mtime_ns) if os.path.exists(mp) else None
    log = os.path.join(os.path.dirname(h), "stub.jsonl"); open(log, "w").close()
    e = penv(h, PATH=p4stubs.make_install_stubs(TMPROOT, sys.executable, DETPY) + ":" + os.environ["PATH"], SPEAKEASY_SKIP_KOKORO="1", STUB_LOG=log, STUB_CURL_MODE="fail",
             https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9")
    p = subprocess.run(["bash", os.path.join(SRC, "install.sh")], capture_output=True, text=True, env=e, cwd=os.path.dirname(h), timeout=600)
    refetch = [c for c in jl(log) if c.get("tool") in ("curl", "wget") and any("huggingface" in str(x) for x in c.get("argv") or [])]
    add("I_rerun_safe_exit0_no_fetch_unchanged", p.returncode == 0 and not refetch and b0 is not None and (os.stat(mp).st_ino, os.stat(mp).st_mtime_ns) == b0, (p.returncode, refetch, (p.stdout + p.stderr)[-200:]))
    r = run_install("cached", "fail", pre=GOOD)
    add("I_cached_good_model_exit0", r["rc"] == 0, r["out"][-300:])
    add("I_cached_good_model_no_network", not r["fetch"], r["fetch"])
    add("I_cached_good_model_file_unchanged", r["before"] is not None and r["before"] == r["after"], (r["before"], r["after"]))
    add("I_cached_good_model_no_error_or_warning", not ERR_RE(r"[0-9a-f]{64}").search(r["out"]) and not WARN_RE.search(r["out"]), "")
    for tag, mode, src in (("corrupt", "corrupt", FLIP), ("truncated", "trunc", TRUNC)):
        r = run_install(tag, mode)
        if not r["fetch"]: skip(f"I_{tag}_download", "download not via curl/wget"); continue
        add(f"I_{tag}_download_exit_nonzero", r["rc"] != 0, r["rc"])
        add(f"I_{tag}_download_ERROR_line_exact", bool(ERR_RE(sha256(src)).search(r["out"])), [l for l in r["out"].splitlines() if "layout" in l.lower()][:3])
        add(f"I_{tag}_download_no_model_at_install_path", not os.path.exists(r["mp"]), r["after"])
        add(f"I_{tag}_download_part_deleted", not r["parts"], r["parts"])
        add(f"I_{tag}_download_rest_of_install_finished", "Config defaults:" in r["out"] and "Open ~/Applications/Speakeasy.app" in r["out"], r["out"][-200:])
    for tag, mode in (("offline", "fail"), ("http404", "http404")):
        r = run_install(tag, mode)
        add(f"I_{tag}_exit0", r["rc"] == 0, (r["rc"], r["out"][-300:]))
        add(f"I_{tag}_WARNING_line", bool(WARN_RE.search(r["out"])), [l for l in r["out"].splitlines() if "layout" in l.lower() or "WARNING" in l][:3])
        add(f"I_{tag}_no_model_no_part", not os.path.exists(r["mp"]) and not r["parts"], (r["after"], r["parts"]))
    r = run_install("badpre_good", "good", pre=FLIP)
    add("I_bad_cached_file_replaced_by_verified_download", r["rc"] == 0 and r["after"] is not None and r["after"][2] == SHA, (r["rc"], r["after"]))
    r = run_install("badpre_offline", "fail", pre=FLIP)
    add("I_bad_cached_file_never_stays_offline", r["after"] is None or r["after"][2] == SHA, r["after"])
    r = run_install("noort", "good", venv_py=NOORT)
    pip = [l for l in r["rawlog"].splitlines() if '"pip"' in l or '"uv"' in l]
    add("I_installs_onnxruntime_numpy_into_doc_venv", any("onnxruntime" in l for l in pip) and any("numpy" in l for l in pip), pip[:4])

# ================================================================ U. app/bundle_python.sh
def run_bundle(tag, mode, cache=None):
    h = mkhome("bun" + tag); log = os.path.join(os.path.dirname(h), "stub.jsonl"); open(log, "w").close(); seed_p6_models(h)
    st = p4stubs.make_install_stubs(TMPROOT, sys.executable, DETPY)
    app = os.path.join(os.path.dirname(h), "Speakeasy.app"); os.makedirs(os.path.join(app, "Contents", "MacOS")); os.makedirs(os.path.join(app, "Contents", "Resources"))
    cp = os.path.join(h, "models", MODEL_NAME)
    if cache: os.makedirs(os.path.dirname(cp), exist_ok=True); shutil.copyfile(cache, cp)
    e = penv(h, PATH=st + ":" + os.environ.get("PATH", "/usr/bin:/bin"), STUB_LOG=log, STUB_CURL_MODE=mode, STUB_MODEL_GOOD=GOOD, STUB_MODEL_CORRUPT=FLIP, STUB_MODEL_TRUNC=TRUNC,
             https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9")
    p = subprocess.run(["bash", os.path.join(REPO, "app", "bundle_python.sh"), app], capture_output=True, text=True, env=e, cwd=os.path.dirname(h), timeout=600)
    calls = jl(log); rm = os.path.join(app, "Contents", "Resources", "models", MODEL_NAME)
    return {"rc": p.returncode, "out": p.stdout + p.stderr, "calls": calls, "res_model": sha256(rm) if os.path.isfile(rm) else None, "cache": sha256(cp) if os.path.isfile(cp) else None,
            "fetch": [c for c in calls if c.get("tool") in ("curl", "wget") and any("huggingface" in str(x) or "pp_doclayout" in str(x) for x in c.get("argv") or [])],
            "codesign": [c for c in calls if c.get("tool") == "codesign"], "uvpip": [c for c in calls if c.get("tool") == "uv" and (c.get("argv") or [])[:1] == ["pip"]]}
@section("U")
def bundle():
    r = run_bundle("cached", "fail", cache=GOOD)
    add("U_cached_model_build_ok", r["rc"] == 0, r["out"][-300:])
    add("U_cached_model_no_network", not r["fetch"], r["fetch"])
    add("U_model_copied_into_Resources_models_verified", r["res_model"] == SHA, r["res_model"])
    add("U_model_in_place_before_codesign", bool(r["codesign"]) and all(c.get("model_sha") == SHA for c in r["codesign"]), r["codesign"])
    pk = " ".join(" ".join(c["argv"]) for c in r["uvpip"])
    add("U_installs_onnxruntime_and_numpy", "onnxruntime" in pk and "numpy" in pk, pk[:300])
    r = run_bundle("fresh", "good")
    if not r["fetch"]: skip("U_download", f"no stubbed curl/wget fetch (rc {r['rc']}): {r['out'][-200:]}")
    else:
        add("U_fresh_download_build_ok", r["rc"] == 0, r["out"][-300:])
        add("U_fresh_download_fills_home_cache", r["cache"] == SHA, r["cache"])
        add("U_fresh_download_Resources_model_verified", r["res_model"] == SHA, r["res_model"])
        add("U_fetch_pinned_revision_url", all(any(REV in str(x) for x in c["argv"]) for c in r["fetch"]), [c["argv"] for c in r["fetch"]])
    for tag, mode, cache in (("corrupt", "corrupt", None), ("truncated", "trunc", None), ("offline", "fail", None), ("http404", "http404", None), ("badcache_offline", "fail", FLIP)):
        r = run_bundle(tag, mode, cache)
        add(f"U_{tag}_fails_build", r["rc"] != 0, (r["rc"], r["out"][-200:]))
        add(f"U_{tag}_no_model_in_bundle", r["res_model"] is None, r["res_model"])
        add(f"U_{tag}_no_bad_file_in_cache", r["cache"] in (None, SHA), r["cache"])
        if tag in ("corrupt", "truncated"):
            add(f"U_{tag}_ERROR_line", bool(ERR_RE(sha256(FLIP if tag == "corrupt" else TRUNC)).search(r["out"])), [l for l in r["out"].splitlines() if "layout" in l.lower()][:3])
    r = run_bundle("badcache_good", "good", cache=FLIP)
    add("U_bad_cache_replaced_by_verified_download", r["rc"] == 0 and r["res_model"] == SHA and r["cache"] == SHA, (r["rc"], r["res_model"], r["cache"], r["out"][-200:]))

# ================================================================ B. benchmark: fallback gate, detector gate + coverage, determinism
def bench(tag, args, env_model=None, timeout=3600):
    out = os.path.join(TMPROOT, f"bench_{tag}.json"); logf = os.path.join(TMPROOT, f"bench_{tag}.log")
    e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "STUB_", "MOCK_", "FAKE_ORT_"))}
    if env_model: e["SPEAKEASY_LAYOUT_MODEL"] = env_model
    t = time.time()
    with open(logf, "w") as lf:
        p = subprocess.run([DETPY, os.path.join(BENCH, "run_bench.py"), "--out", out, "--last-cases-out", os.path.join(TMPROOT, f"last_cases_{tag}.json")] + args,
                           stdout=lf, stderr=subprocess.STDOUT, env=e, timeout=timeout)
    b = json.load(open(out)) if os.path.exists(out) else None
    info(f"B {tag} exit {p.returncode} in {time.time() - t:.0f}s (log {logf})")
    for l in open(logf).read().splitlines():
        if re.match(r"^(public|hidden): ", l) or "LAYOUT COVERAGE" in l or "determinism" in l: info(f"B {tag} {l}")
    return p.returncode, b, out
SETS = ["--set", "all", "--holdout", A.holdout] if A.holdout else ["--set", "public"]
RUN_SETS = {"public", "hidden"} if A.holdout else {"public"}
def gate(tag, out):
    hidden = ["--hidden-baseline", A.hidden_baseline] if A.holdout else ["--hidden-baseline", os.path.join(TMPROOT, "no-hidden-baseline.json"), "--public-only"]
    p = subprocess.run([DETPY, os.path.join(BENCH, "bench_gate.py"), out, "--baseline", A.public_baseline] + hidden, capture_output=True, text=True)
    for l in p.stdout.splitlines():
        if re.search(r"(FAIL|PASS|warning|GATE|layout)", l) and not l.startswith(("public boxes.by_doc", "hidden boxes.by_doc")): info(f"B {tag} gate: {l}")
    return p.returncode, p.stdout
@section("B")
def benchmark():
    if A.skip_bench: skip("B_bench", "--skip-bench"); return
    if A.holdout and not A.hidden_baseline: add("B_phase4_baselines_present", False, "--holdout needs --hidden-baseline"); return
    for f in [A.public_baseline] + ([A.hidden_baseline] if A.holdout else []):
        if not os.path.exists(f): add("B_phase4_baselines_present", False, f); return
    rc, b, out = bench("fallback", SETS)
    add("B_fallback_bench_complete_exit0", rc == 0 and b and b.get("complete") is True, rc)
    if b:
        lay = {s: v.get("layout") for s, v in b["sets"].items()}
        add("B_fallback_every_page_heuristic_model_missing", all(l and l["src"]["detector"] == 0 and l["missing"] == 0 and set(l["reasons"]) == {"model-missing"} for l in lay.values()), lay)
        add("B_fallback_versions_layout_model_null", (b.get("versions") or {}).get("layout_model") is None, b.get("versions"))
        g, txt = gate("fallback", out); add("B_fallback_passes_gate_vs_890faa4", g == 0, txt.splitlines()[-1:] if txt else g)
    rc, b, out = bench("detector", SETS + ["--layout-model", GOOD])
    add("B_detector_bench_exit0_coverage_ok", rc == 0 and b and b.get("complete") is True, (rc, (b or {}).get("errors")))
    if b:
        lay = {s: v.get("layout") for s, v in b["sets"].items()}
        add("B_detector_coverage_heuristic_0_every_set", set(lay) == RUN_SETS and all(l and l["src"]["heuristic"] == 0 and l["missing"] == 0 and l["src"]["detector"] == l["pages"] > 0 for l in lay.values()), lay)
        add("B_detector_versions_layout_model", (b.get("versions") or {}).get("layout_model") == DV, b.get("versions"))
        add("B_detector_ms_per_page_reported", all(l and isnum(l["ms"]["p50"]) and isnum(l["ms"]["p95"]) for l in lay.values()), {s: (l or {}).get("ms") for s, l in lay.items()})
        for s, v in b["sets"].items():
            add(f"B_detector_{s}_boxes_score_ge_90", isnum(v["boxes"]["score"]) and v["boxes"]["score"] >= 90, v["boxes"]["score"])
            sp = v["speed"]["first_highlight_ms"]; add(f"B_detector_{s}_first_highlight_p50_p95_lt_300", isnum(sp["p50"]) and isnum(sp["p95"]) and sp["p95"] < 300 and sp["p50"] < 300, sp)
            info(f"B detector {s} boxes by kind {json.dumps({k: x['score'] for k, x in v['boxes']['by_kind'].items()})} P50 {v['boxes']['precision_50']}")
        if "public" in b["sets"] and "by_doc" in b["sets"]["public"]["boxes"]:
            info("B detector public boxes by doc " + json.dumps({k: round(100 * x["hits"] / x["gt"], 1) if x["gt"] else None for k, x in b["sets"]["public"]["boxes"]["by_doc"].items()}))
        g, txt = gate("detector", out); add("B_detector_passes_gate_vs_890faa4", g == 0, txt.splitlines()[-1:] if txt else g)
    rc, b, out = bench("determinism", ["--set", "public", "--check-determinism", "--layout-model", GOOD])
    add("B_check_determinism_with_detector_exit0", rc == 0, rc)
    rc, b, out = bench("env_model", ["--set", "public", "--cases", "syn-fig5-01/c01"], env_model=GOOD)
    add("B_caller_SPEAKEASY_LAYOUT_MODEL_passed_through", b is not None and (b["sets"].get("public") or {}).get("layout", {}).get("src", {}).get("detector", 0) > 0, (rc, (b or {}).get("sets", {}).get("public", {}).get("layout")))
    p = subprocess.run([NOORT, os.path.join(BENCH, "run_bench.py"), "--set", "public", "--layout-model", GOOD, "--out", os.path.join(TMPROOT, "noort.json")], capture_output=True, text=True, timeout=120)
    add("B_layout_model_without_onnxruntime_refused_exit2", p.returncode == 2 and "onnxruntime" in p.stderr, (p.returncode, p.stderr[-200:]))

# ================================================================ Z. never touched ~/.speakeasy
@section("Z")
def untouched():
    now = {}
    for sub in _real_snapshot:
        p = os.path.join(REAL_HOME, sub)
        now[sub] = (sorted(os.listdir(p)) if os.path.isdir(p) else os.path.getsize(p)) if os.path.exists(p) else None
    add("Z_no_model_written_to_real_home", now["models"] == _real_snapshot["models"], (now["models"], _real_snapshot["models"]))
    rl = os.path.join(REAL_HOME, "logs", "runs.jsonl")
    leaked = [l for l in open(rl, errors="ignore").read().splitlines()[-200:] if TMPROOT in l or "se_p4_" in l] if os.path.exists(rl) else []
    add("Z_no_suite_runs_in_real_runs_log", not leaked, len(leaked))

for fn in (licence_docs, detector_on, detector_off, reasons, lookup, sessions, fig5, privacy, runs_log, timing, install, bundle, benchmark, untouched): fn()
n = {s: sum(1 for x in R if x[0] == s) for s in ("PASS", "FAIL", "SKIP")}
print(f"SUMMARY checks={len(R)} pass={n['PASS']} fail={n['FAIL']} skip={n['SKIP']} secs={time.time() - T0:.0f}", flush=True)
if not A.keep: shutil.rmtree(TMPROOT, ignore_errors=True)
else: print(f"kept {TMPROOT}")
sys.exit(1 if n["FAIL"] else 0)
