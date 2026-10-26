#!/usr/bin/env python3
"""Phase-2 holdout acceptance suite (black box) for docs/contracts/phase2-logging.md, sections A-E + global rules.
Drives only the CLI (src/speakeasy), `docmap.py parse-pdf` / `dump`, the tracker built from src/tracker.swift, and the logs
and cache files they write under a temp $SPEAKEASY_HOME. "Unchanged" behaviour is checked differentially against the
baseline commit (git archive of --baseline-ref, default 7004d6e). Never calls a real LLM (local mock on an ephemeral port),
never touches ~/.speakeasy.
Usage: run_phase2.py [--repo PATH] [--baseline-ref REF] [--only T,D,V,A,C,G,P,F,X,S,E] [--no-tracker]
Output: one line per check `PASS|FAIL|SKIP <name> <detail>`, then `SUMMARY ...`. Exit 0 only if no FAIL."""
import argparse, glob, json, math, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import p2lib as L
from p2lib import jl, Mock, mkhome, env_for, runs, focus_log, flat, sentinel_hits, TMPROOT, PY
os.environ.setdefault("DOC_VENV", os.path.dirname(os.path.dirname(PY)))
from geom import page_to_screen, xywh, K

ap = argparse.ArgumentParser(); ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", ".."))); ap.add_argument("--baseline-ref", default="7004d6e")
ap.add_argument("--only", default=""); ap.add_argument("--no-tracker", action="store_true"); ap.add_argument("--keep", action="store_true"); A = ap.parse_args()
REPO = os.path.abspath(A.repo); SRC = os.path.join(REPO, "src"); CUR = os.path.join(SRC, "speakeasy"); ONLY = {x.strip().upper() for x in A.only.split(",") if x.strip()}
BAD = "http://127.0.0.1:9/v1"; IMGS = os.path.join(REPO, "tests", "images")
R = []
def _s(d): return " ".join(str(d).split())[:400]
def add(name, ok, detail=""):
    st = "PASS" if ok else "FAIL"; R.append((st, name)); print(f"{st} {name} {_s(detail) if not ok or os.environ.get('P2_VERBOSE') else ''}".rstrip(), flush=True)
def skip(name, why): R.append(("SKIP", name)); print(f"SKIP {name} {_s(why)}", flush=True)
def section(tag):
    def deco(fn):
        def run():
            if ONLY and tag not in ONLY: return
            try: fn()
            except Exception as e: add(f"{tag}_section_crashed", False, f"{type(e).__name__}: {e} :: {traceback.format_exc()[-700:]}")
        run.__name__ = fn.__name__; return run
    return deco
def isint(v): return isinstance(v, int) and not isinstance(v, bool)
def isnum(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
def r2(v): return isnum(v) and abs(round(v, 2) - v) < 1e-9
def r1(v): return isnum(v) and abs(round(v, 1) - v) < 1e-9

# ---------------------------------------------------------------- contract vocabularies
FIG_SRC = {"graphics", "whitespace", "caption-only", "detector", "detector-substitute"}  # detector-substitute: phase5-parts.md A2.4
TAB_SRC = {"graphics", "text-rows", "graphics+text-rows", "caption-only", "detector"}
ALL_SRC = FIG_SRC | TAB_SRC
# Phase 5 (phase5-parts.md 6.3, A2.9, A4.1): callout / callout-marker / pick targets; the bbox_2d path (vlm target, no-box / too-large,
# ground-vlm, ground_box_convention) is retired and must never be written
TARGET_SRC_FIXED = {"lines", "panel-labels", "panel-guessed", "table-rows", "label-object", "label-pad", "callout", "callout-marker", "pick"}
REASONS = {"no-fit", "superseded", "timeout", "error", "low-confidence", "off-menu", "no-logprobs", "unavailable", "out-of-scope"}
MISS_REASONS = ("error", "low-confidence", "off-menu", "no-logprobs", "unavailable", "out-of-scope")   # counted in focus_methods ground-miss
RETIRED_RUN_KEYS = {"ground_box_convention"}; RETIRED_METHODS = {"ground-vlm"}
F_LABEL_RE = re.compile(r"^(Figure|Table) \d+[a-h]?( row \d+)?( \(grounded\))?$")
def f_label_ok(v): return v is None or (isinstance(v, str) and bool(F_LABEL_RE.match(v)))
def p2_redact(label):
    """Phase 2 F default-mode form of a text-mode label (part text after ': ' dropped; null when not expressible)."""
    if f_label_ok(label): return label
    m = re.match(r"^((?:Figure|Table) \d+[a-h]?(?: row \d+)?)(?::.*?)?( \(grounded\))?$", str(label), re.S)
    return (m.group(1) + (m.group(2) or "")) if m and F_LABEL_RE.match(m.group(1) + (m.group(2) or "")) else None
LABEL_RE = re.compile(r"^(Figure|Table) \d+$")
BLOCK_KINDS = {"paragraph", "caption", "figure", "table"}
NEW_RUN_KEYS = {"first_focus_ms", "doc_ready_ms", "doc_parse", "ground_miss_reasons"}
# Phase 3 (docs/contracts/phase3-sentence-ids.md F): exactly these new keys are allowed (present only on runs with a sentence map).
P3_RUN = {"sentence_map"}
P3_MAP = {"sentence_id", "sentence_ids"}
P3_SHOW = {"sentence_id", "match_score", "runner_up", "rects"}
ALIGN_METHODS = {"align", "align-fuzzy", "span", "hold", "block"}   # Phase 3 amendment 3: sentence-map assignment methods
P2_MATCHER = {"overlap", "progress", "doc-line"}
def p3_method(cur, base):
    """A Phase 2 matcher method replaced by a Phase 3 assignment method counts as unchanged."""
    return cur == base or (cur in ALIGN_METHODS and base in P2_MATCHER)
# Phase 4 (docs/contracts/phase4-layout.md 4.7, 6): every block has src; layout / layout_events keys (numbers and enums only)
CAP_SRC = {"regex", "regex+detector"}; PARA_SRC = {"detector", "lines"}
LAYOUT_REASONS = {"disabled", "no-onnxruntime", "model-missing", "model-mismatch", "load-failed", "pdfkit-engine", "inference-failed"}
DETECTOR_VERSION_RE = re.compile(r"^pp-doclayout-s/[0-9a-f]{12}$")
P4_RUN = {"layout_events"}
# Phase 5 (docs/contracts/phase5-parts.md 2, 6): runs part_picks; figures[].parts and panels[].src; doc_parse parts / panels.gutter counts
P5_RUN = {"part_picks", "figure_mode_src", "stand_in_blocked", "no_target_chunks", "figures_degenerate", "area_page_rect", "area_region_src", "area_dropped", "area_pdf_error"}
P5_COUNT_KEYS = {"parts", "text", "callout", "callout_attached", "gutter", "detector-substitute"}
PICK_SKIPPED = {"too-few", "whole-figure", "disabled", "doc-not-ready", "degenerate-figure", "generic-phrase"}
def part_picks_ok(v):
    """runs part_picks (phase5-parts.md 6.2 as amended by A2.9 / A3.3): counts and enums only."""
    if not isinstance(v, dict) or not {"requests", "called", "accepted", "applied", "outcomes", "skipped", "ms", "threshold", "breaker"} <= set(v): return False
    if not all(isint(v[k]) and v[k] >= 0 for k in ("requests", "called", "accepted", "applied")): return False
    if not (isinstance(v["outcomes"], dict) and set(v["outcomes"]) <= REASONS - {"no-fit", "superseded"} | {"accepted"} and all(isint(x) for x in v["outcomes"].values())): return False
    if not (isinstance(v["skipped"], dict) and set(v["skipped"]) <= PICK_SKIPPED and all(isint(x) for x in v["skipped"].values())): return False
    b = v["breaker"]
    if not (b is None or b in ("unavailable", "no-logprobs") or (isinstance(b, dict) and set(b) <= {"local", "openrouter"} and all(x in (None, "unavailable", "no-logprobs") for x in b.values()))): return False
    if "by_provider" in v and not (isinstance(v["by_provider"], dict) and set(v["by_provider"]) <= {"local", "openrouter"} and all(isint(x) for x in v["by_provider"].values())): return False
    if "fallback" in v and not (isinstance(v["fallback"], dict) and set(v["fallback"]) <= {"unavailable", "no-logprobs"} and all(isint(x) for x in v["fallback"].values())): return False
    return isnum(v["threshold"]) and isinstance(v["ms"], dict) and all(x is None or isint(x) for x in v["ms"].values())
def breaker_local(v):
    b = (v or {}).get("breaker"); return b.get("local") if isinstance(b, dict) else b
def p5_same(k, x, y):
    """Phase 5 additions inside an existing figure key: panels gain src (and gutter splits may replace caption guesses), objects may gain a panel."""
    xv, yv = x.get(k), y.get(k)
    if k == "objects" and isinstance(xv, list) and isinstance(yv, list):
        return len(xv) == len(yv) and all(list(a)[:5] == list(b)[:5] for a, b in zip(xv, yv))
    if k == "panels" and isinstance(xv, list) and isinstance(yv, list):
        if any(isinstance(p, dict) and p.get("src") == "gutter" for p in yv): return True
        if [a.get("label") for a in xv] == [b.get("label") for b in yv] and all(isinstance(b, dict) and b.get("src") == "label" for b in yv) and not any(a.get("guessed") for a in xv): return True   # A3.4 labels-below re-boxing
        fb = y.get("bbox") or [-1e9, -1e9, 1e9, 1e9]
        def clip(r): return [max(r[0], fb[0]), max(r[1], fb[1]), min(r[2], fb[2]), min(r[3], fb[3])]
        return len(xv) == len(yv) and all(a.get("label") == b.get("label") and bool(a.get("guessed")) == bool(b.get("guessed")) and
                                          max(abs(u - v) for u, v in zip(clip(a["bbox"]), b["bbox"])) <= 0.15 for a, b in zip(xv, yv))
    return False
EXC_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,80}$")
def layout_ok(v, version):
    """pages[].layout (version=True) or doc_parse.layout (version=False)."""
    if not isinstance(v, dict): return False
    if v.get("src") == "heuristic": return set(v) == {"src", "reason"} and v.get("reason") in LAYOUT_REASONS
    want = {"src", "ms", "blocks_by_kind"} | ({"detector_version"} if version else set())
    bk = v.get("blocks_by_kind")
    return (v.get("src") == "detector" and set(v) == want and isint(v.get("ms")) and v["ms"] >= 0 and isinstance(bk, dict) and set(bk) == BLOCK_KINDS
            and all(isint(x) and x >= 0 for x in bk.values()) and (not version or bool(DETECTOR_VERSION_RE.match(str(v.get("detector_version"))))))
def layout_event_ok(e):
    if not isinstance(e, dict): return False
    dv = e.get("detector_version")
    if e.get("event") == "layout_page":
        return set(e) == {"event", "page", "ms", "detector_version", "blocks_by_kind"} and isint(e.get("page")) and isint(e.get("ms")) and bool(DETECTOR_VERSION_RE.match(str(dv))) \
            and isinstance(e.get("blocks_by_kind"), dict) and set(e["blocks_by_kind"]) == BLOCK_KINDS and all(isint(x) for x in e["blocks_by_kind"].values())
    if e.get("event") == "layout_detector_unavailable":
        return set(e) == {"event", "reason", "error", "detector_version", "page"} and e.get("reason") in LAYOUT_REASONS and (e.get("error") is None or bool(EXC_RE.match(str(e["error"])))) \
            and (dv is None or bool(DETECTOR_VERSION_RE.match(str(dv)))) and (e.get("page") is None or isint(e.get("page")))
    return False
def target_src_ok(v):
    if not isinstance(v, str): return False
    if v in TARGET_SRC_FIXED: return True
    m = re.fullmatch(r"(figure|table):(.+)", v)
    return bool(m) and m.group(2) in (FIG_SRC if m.group(1) == "figure" else TAB_SRC)

# ---------------------------------------------------------------- setup: baseline tree + fixtures
BASE = os.path.join(TMPROOT, "base"); os.makedirs(BASE)
subprocess.run(f"git -C '{REPO}' archive {A.baseline_ref} | tar -x -C '{BASE}'", shell=True, check=True)
BIN_BASE = os.path.join(BASE, "src", "speakeasy")
FXD = os.path.join(TMPROOT, "fx"); os.makedirs(FXD)
for script in ("make_fig5.py", "make_docs.py", "make_p2docs.py"):
    subprocess.run([PY, os.path.join(HERE, script), FXD], check=True, capture_output=True)
FX = json.load(open(os.path.join(FXD, "fixtures.json")))
def fx(n): return os.path.join(FXD, n)
ZIP_TABLE = "| Name | ZIP |\n|---|---|\n| Ann | 94110 |\n"
SENT_TABLE = "| Zorblax | Quintessa |\n|---|---|\n| Mirevolt | 42 |\n| Plumbrine | 77 |"
SENT_ANSWER = "Mirevolt has 42 quintessa units. Plumbrine has 77 quintessa units and leads the zorblax table. Grelquist is not in the table."

def scrub_env(home=None):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("SPEAKEASY_", "STUB_", "MOCK_")): e.pop(k)
    h = home or mkhome("pp"); e.update(HOME=h, SPEAKEASY_HOME=h, PYTHONDONTWRITEBYTECODE="1"); return e
def parse(pdf, src=SRC, sub="parse-pdf"):
    out = os.path.join(TMPROOT, f"parse_{os.getpid()}_{time.time_ns()}.json"); cp = os.path.join(TMPROOT, f"in_{time.time_ns()}_" + os.path.basename(pdf)); shutil.copy(pdf, cp)
    args = [PY, os.path.join(src, "docmap.py"), sub, cp] + ([out] if sub == "parse-pdf" else [])
    p = subprocess.run(args, capture_output=True, text=True, env=scrub_env(), timeout=180)
    try: d = json.load(open(out)) if sub == "parse-pdf" else json.loads(p.stdout)
    except Exception: d = None
    return p.returncode, d, p.stderr[-300:]

# ---------------------------------------------------------------- D-invariant validator (parse output, cache entries)
def validate_parse(d, tag, expect_cols=None):
    """Returns list of (check_suffix, ok, detail)."""
    out = []
    pages = (d or {}).get("pages") or []
    fd = [p for p in pages if p.get("figs_done") is True]
    out.append(("has_figs_done_pages", bool(fd), [p.get("figs_done") for p in pages]))
    bad_cols = [(p.get("n"), p.get("columns")) for p in fd if not (isint(p.get("columns")) and p.get("columns") in (1, 2))]
    out.append(("columns_int_1_or_2_every_figs_done_page", not bad_cols, bad_cols))
    out.append(("blocks_list_every_figs_done_page", all(isinstance(p.get("blocks"), list) for p in fd), [(p.get("n"), type(p.get("blocks")).__name__) for p in fd]))
    figs_all = [f for p in pages for f in (p.get("figures") or [])]
    bad = [(f.get("label"), f.get("kind"), f.get("bbox_src")) for f in figs_all if (f.get("bbox_src") not in (TAB_SRC if f.get("kind") == "table" else FIG_SRC))]
    out.append(("figures_bbox_src_enum_by_kind", not bad, bad))
    schema_bad, lines_bad, label_bad, src_bad, round_bad, inv = [], [], [], [], [], {k: [] for k in ("one_block_per_figure", "one_caption_per_figure", "no_extra_fig_table_blocks",
        "no_extra_caption_blocks", "paragraph_bbox_union_of_lines", "no_shared_lines", "paragraph_lines_outside_fig_table", "caption_lines_only_in_caption_block",
        "caption_block_lines_inside_caption_bbox", "reading_order_top_to_bottom_per_column", "block_src_equals_figure_bbox_src")}
    for p in fd:
        n = p.get("n"); blocks = p.get("blocks") if isinstance(p.get("blocks"), list) else []; lines = p.get("lines") or []; figs = p.get("figures") or []
        W = p.get("w") or 1e9
        for i, b in enumerate(blocks):
            if not isinstance(b, dict): schema_bad.append((n, i, "not-dict")); continue
            k = b.get("kind"); bb = b.get("bbox")
            allowed = {"kind", "bbox", "src"} | ({"lines"} if k in ("paragraph", "caption") else set()) | ({"label"} if k in ("caption", "figure", "table") else set())
            if k not in BLOCK_KINDS: schema_bad.append((n, i, "kind", k))
            if set(b) != allowed: schema_bad.append((n, i, k, "keys", sorted(set(b) ^ allowed)))
            if not (isinstance(bb, list) and len(bb) == 4 and all(isnum(v) for v in bb) and bb[0] < bb[2] and bb[1] < bb[3]): schema_bad.append((n, i, k, "bbox", bb)); continue
            if not all(r1(v) for v in bb): round_bad.append((n, i, bb))
            if "lines" in b and not (isinstance(b["lines"], list) and b["lines"] and all(isint(x) and 0 <= x < len(lines) for x in b["lines"])): lines_bad.append((n, i, k, b["lines"]))
            if "label" in b and not (isinstance(b["label"], str) and LABEL_RE.match(b["label"])): label_bad.append((n, i, b.get("label")))
            if b.get("src") not in {"figure": FIG_SRC, "table": TAB_SRC, "caption": CAP_SRC, "paragraph": PARA_SRC}.get(k, set()): src_bad.append((n, i, k, b.get("src")))
            elif b.get("src") in ("detector", "regex+detector") and ((p.get("layout") or {}).get("src") != "detector" or ((d or {}).get("layout") or {}).get("status") == "unavailable"): src_bad.append((n, i, k, b.get("src"), "detector src on a heuristic page"))
        good = [b for b in blocks if isinstance(b, dict) and isinstance(b.get("bbox"), list) and len(b["bbox"]) == 4]
        ft = [b for b in good if b.get("kind") in ("figure", "table")]; caps = [b for b in good if b.get("kind") == "caption"]; paras = [b for b in good if b.get("kind") == "paragraph"]
        for f in figs:
            want_kind = "table" if f.get("kind") == "table" else "figure"
            m = [b for b in ft if b.get("label") == f.get("label") and b.get("kind") == want_kind and b.get("bbox") == f.get("bbox")]
            if len(m) != 1: inv["one_block_per_figure"].append((n, f.get("label"), f.get("bbox"), [(b.get("kind"), b.get("label"), b.get("bbox")) for b in ft if b.get("label") == f.get("label")]))
            elif m[0].get("src") != f.get("bbox_src"): inv["block_src_equals_figure_bbox_src"].append((n, f.get("label"), m[0].get("src"), f.get("bbox_src")))
            c = [b for b in caps if b.get("bbox") == f.get("caption_bbox")]
            if len(c) != 1 or c[0].get("label") != f.get("label"): inv["one_caption_per_figure"].append((n, f.get("label"), f.get("caption_bbox"), [(b.get("label"), b.get("bbox")) for b in caps]))
            for cb in c:
                cbx = f.get("caption_bbox")
                out_l = [li for li in cb.get("lines") or [] if isint(li) and li < len(lines) and not (lines[li][0] >= cbx[0] - 0.6 and lines[li][1] >= cbx[1] - 0.6 and lines[li][2] <= cbx[2] + 0.6 and lines[li][3] <= cbx[3] + 0.6)]
                if out_l: inv["caption_block_lines_inside_caption_bbox"].append((n, f.get("label"), out_l))
            # every page line fully inside the caption bbox is a caption line of exactly that caption block
            cbx = f.get("caption_bbox")
            if isinstance(cbx, list) and len(cbx) == 4:
                inside = {i for i, l in enumerate(lines) if l[0] >= cbx[0] - 0.6 and l[1] >= cbx[1] - 0.6 and l[2] <= cbx[2] + 0.6 and l[3] <= cbx[3] + 0.6}
                inpara = sorted(inside & set().union(*[set(b.get("lines") or []) for b in paras] or [set()]))
                if inpara: inv["caption_lines_only_in_caption_block"].append((n, f.get("label"), "in-paragraph", inpara))
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
        for gname, gb in groups.items():
            ys = [b["bbox"][1] for b in gb]
            if any(ys[i + 1] < ys[i] - 1.0 for i in range(len(ys) - 1)): inv["reading_order_top_to_bottom_per_column"].append((n, gname, ys[:12]))
        if expect_cols is not None and isinstance(expect_cols, dict) and n in expect_cols:
            out.append((f"p{n}_columns_is_{expect_cols[n]}", cols == expect_cols[n], cols))
    out += [("block_schema_exact_keys", not schema_bad, schema_bad[:4]), ("block_lines_valid_indexes", not lines_bad, lines_bad[:4]),
            ("block_label_Figure_or_Table_N", not label_bad, label_bad[:4]), ("block_src_enum", not src_bad, src_bad[:4]), ("block_bbox_rounded_0.1pt", not round_bad, round_bad[:3])]
    out += [(k, not v, v[:3]) for k, v in inv.items()]
    return out
BOX_KEYS = {"bbox", "labels", "labels_src", "objects", "panels", "split_from"}
def _within(a, b, t=0.15): return a[0] >= b[0] - t and a[1] >= b[1] - t and a[2] <= b[2] + t and a[3] <= b[3] + t
def _clip(b, W, H): return [max(0.0, b[0]), max(0.0, b[1]), min(W, b[2]), min(H, b[3])]
def _covered(box, olds, t=0.5):
    """box lies inside the union of olds (checked on a 9x9 grid of interior points)."""
    xs = [box[0] + (box[2] - box[0]) * (i + 0.5) / 9 for i in range(9)]; ys = [box[1] + (box[3] - box[1]) * (j + 0.5) / 9 for j in range(9)]
    return all(any(o[0] - t <= x <= o[2] + t and o[1] - t <= y <= o[3] + t for o in olds) for x in xs for y in ys)
def _vov(a, b): return 0.0 if min(a[2], b[2]) - max(a[0], b[0]) <= 0 else max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
def contract_box_change(x, y, bpage, n_caps):
    """Baseline figure entry x vs current y on the same page. None: same box (every key must match). True: a box change
    phase4-layout.md allows on the fallback path (4.4 caption exclusion, column rule, 4.5 two-way cut, section 5 graphics
    reserved per caption, 4.7 inside the page): the new box lies inside the baseline box clipped to the page, or on a page
    with two or more captions inside the extent the baseline boxes covered; a table excludes its caption (<= 2 pt); the
    caption is unchanged; panels stay inside the box; split_from is a label. False: any other change."""
    xb, yb = x.get("bbox"), y.get("bbox")
    if xb == yb: return None
    if not (isinstance(xb, list) and isinstance(yb, list) and len(yb) == 4 and yb[0] < yb[2] and yb[1] < yb[3]): return False
    W, H = bpage.get("w") or 1e9, bpage.get("h") or 1e9
    if x.get("caption_bbox") != y.get("caption_bbox") or x.get("kind") != y.get("kind") or x.get("label") != y.get("label"): return False
    olds = [_clip(f["bbox"], W, H) for f in bpage.get("figures") or [] if isinstance(f.get("bbox"), list)]
    if not (_within(yb, _clip(xb, W, H)) or (n_caps >= 2 and _covered(yb, olds))): return False
    cb = y.get("caption_bbox")
    if isinstance(cb, list) and _vov(yb, cb) > 2.0 + 1e-6: return False
    if any(isinstance(pp.get("bbox"), list) and not _within(pp["bbox"], yb, 2.0) for pp in y.get("panels") or []): return False
    if y.get("split_from") is not None and not LABEL_RE.match(str(y.get("split_from"))): return False
    return True
def report(tag, checks):
    for suf, ok, det in checks: add(f"{tag}_{suf}", ok, det)

# ---------------------------------------------------------------- CLI run harness
class Watch:
    def __init__(self, home, dt=0.01):
        self.home, self.dt = home, dt; self.focus = []; self._stop = False; self.t = threading.Thread(target=self._run, daemon=True)
    def _run(self):
        last = None; fp = os.path.join(self.home, "focus.json")
        while not self._stop:
            try:
                raw = open(fp).read()
                if raw != last: last = raw; self.focus.append(json.loads(raw))
            except Exception: pass
            time.sleep(self.dt)
    def __enter__(self): self.t.start(); return self
    def __exit__(self, *a): self._stop = True; self.t.join(5)

ALL = []  # every collected run: dict(name, cmd, rec, focus, live, t0, t1, home, logtext, rc, expect)
def cli(name, args, home, env, inp=None, bin_=CUR, collect=True, timeout=240, **expect):
    n_runs = len(runs(home)); n_f = len(focus_log(home)); t0 = time.time()
    with Watch(home) as w:
        p = subprocess.run([bin_] + args, input=inp, capture_output=True, text=True, env=env, timeout=timeout)
    t1 = time.time(); new = runs(home)[n_runs:]; F = focus_log(home)[n_f:]
    o = {"name": name, "cmd": args[0], "rc": p.returncode, "stderr": p.stderr, "stdout": p.stdout, "recs": new, "rec": flat(new[-1]) if new else None, "raw": new[-1] if new else None,
         "focus": F, "live": w.focus, "t0": t0, "t1": t1, "home": home, "logtext": env.get("SPEAKEASY_LOG_TEXT") == "1", "expect": expect, "bin": bin_}
    if collect and bin_ == CUR: ALL.append(o)
    return o
def cache_docs(home):
    out = []
    for f in sorted(glob.glob(os.path.join(home, "cache", "doc", "*.json")), key=os.path.getmtime, reverse=True):  # newest first: a superseded old-version entry may stay on disk
        try: out.append(json.load(open(f)))
        except Exception: pass
    return out
def cache_fig(home, page, label):
    """Figure/table entry for `label` on `page` in the (single-document) cache."""
    for d in cache_docs(home):
        for p in d.get("pages") or []:
            if p.get("n") == page:
                for f in p.get("figures") or []:
                    if f.get("label") == label: return f
    return None
def hint(home, path, title="doc.pdf"):
    p = os.path.join(os.path.dirname(home), "hint.json")
    json.dump({"app": "Preview", "bundle": "com.apple.Preview", "title": title, "ax_document": "file://" + path}, open(p, "w")); return p

PARSED = {}
PARSE_COLS = {}   # fixture basename -> {page: columns} from parse-pdf (the parse decision)
def parse_cols(pdf):
    k = os.path.basename(pdf)
    if k not in PARSE_COLS:
        rc, d, _ = parse(pdf); PARSE_COLS[k] = {pg.get("n"): pg.get("columns") for pg in (d or {}).get("pages") or [] if pg.get("figs_done")}
    return PARSE_COLS[k]
def parse_fig(pdf, page, label):
    rc, d, _ = parse(pdf)
    for pg in (d or {}).get("pages") or []:
        if pg.get("n") == page:
            for f in pg.get("figures") or []:
                if f.get("label") == label: return f
    return None
TRK = {}
def tracker_exe():
    """Builds the current tracker (src/tracker.swift) once into the temp root; returns its path or None."""
    if "exe" in TRK: return TRK["exe"]
    TRK["exe"] = None
    if not shutil.which("swift"): TRK["why"] = "no swift toolchain"; return None
    T = os.path.join(TMPROOT, "trk"); pkg = os.path.join(T, "pkg"); os.makedirs(T, exist_ok=True)
    shutil.copytree(os.path.join(REPO, "tracker"), pkg, symlinks=False, ignore=shutil.ignore_patterns(".build"))
    os.makedirs(os.path.join(T, "tests"), exist_ok=True)
    for f in glob.glob(os.path.join(REPO, "tests", "*.json")): shutil.copy(f, os.path.join(T, "tests"))
    ts = os.path.join(pkg, "Sources", "speakeasy-track", "tracker.swift")
    if os.path.lexists(ts): os.remove(ts)
    shutil.copy(os.path.join(SRC, "tracker.swift"), ts)
    p = subprocess.run(["swift", "build"], cwd=pkg, capture_output=True, text=True, timeout=900)
    exe = os.path.join(pkg, ".build", "debug", "speakeasy-track")
    if p.returncode or not os.path.exists(exe): TRK["why"] = "swift build failed: " + (p.stdout + p.stderr)[-200:]; return None
    TRK["exe"] = exe; TRK["pkg"] = pkg; return exe
def ocr_wrapper(mode):
    """A SPEAKEASY_TRACKER stand-in: runs the real tracker; for --ocr-lines it returns all lines (pass) or none (empty -> the capture fit can never land)."""
    exe = tracker_exe()
    if not exe: return None
    w = os.path.join(TMPROOT, f"ocrwrap_{mode}.py")
    open(w, "w").write(f"""#!{PY}
import json, subprocess, sys
p = subprocess.run([{exe!r}] + sys.argv[1:], capture_output=True, text=True)
if "--ocr-lines" in sys.argv and {mode!r} == "empty":
    try: json.loads(p.stdout); print("[]"); sys.exit(0)
    except Exception: pass
sys.stdout.write(p.stdout); sys.exit(p.returncode)
""")
    os.chmod(w, 0o755); return w
def lines_of(pdf):
    if pdf not in PARSED:
        rc, d, _ = parse(pdf, os.path.join(BASE, "src")); PARSED[pdf] = d
    return PARSED[pdf]
def bench_args(home, pdf, page, idx):
    """Bench-style geometry G0 (zoom 1.25, backing 2, display (0, 33), first line at screen y 300); returns (args, stdin)."""
    pg = lines_of(pdf)["pages"][page - 1]; sel = [pg["lines"][i] for i in idx]
    g = dict(zoom=1.25, backing=2); s = 1.25 * K; g["ox"] = (1512 - pg["w"] * s) / 2; g["oy"] = 33 + 300 - sel[0][1] * s
    rects = [xywh(page_to_screen(l[:4], g)) for l in sel]
    x0 = min(r[0] for r in rects) - 16; y0 = min(r[1] for r in rects) - 16; x1 = max(r[0] + r[2] for r in rects) + 16; y1 = max(r[1] + r[3] for r in rects) + 16
    lb = os.path.join(os.path.dirname(home), f"lb_{time.time_ns()}.json"); json.dump([[round(v, 1) for v in r] for r in rects], open(lb, "w"))
    cr = ",".join(str(round(v, 1)) for v in (x0, y0, x1 - x0, y1 - y0))
    return ["--capture-method", "bounds", "--capture-rect=" + cr, "--line-bounds", lb, "--doc-hint", hint(home, pdf, os.path.basename(pdf))], "\n".join(l[4] for l in sel)
def find_line(pdf, page, prefix):
    for i, l in enumerate(lines_of(pdf)["pages"][page - 1]["lines"]):
        if l[4].startswith(prefix): return i
    raise KeyError(prefix)
def bench_env(home, mock, **kw):
    base = dict(SPEAKEASY_VISION="never", SPEAKEASY_GROUND="off", SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker"), SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1",
                SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_TEXT_HIGHLIGHT="on", SPEAKEASY_FIGURE_CUES="on", SPEAKEASY_KEEP_CAPTURE="0", SPEAKEASY_DOC_READ="local")
    base.update(kw); return env_for(home, mock.url if mock else BAD, **base)
def bench_case(name, pdf, page, idx, script, cmd="speak", extra=(), n=2, bin_=CUR, mock_kw=None, **envkw):
    """Warm-up + measured runs in one home (logs kept). Returns the list of run dicts."""
    m = Mock(MOCK_TEXT=script, MOCK_FIRST_DELAY="0.3", MOCK_TOKEN_DELAY="0.002", **(mock_kw or {}))
    try:
        h = mkhome(name[:10]); args, inp = bench_args(h, pdf, page, idx); out = []
        for i in range(n):
            o = cli(f"{name}#{i}", [cmd] + args + list(extra), h, bench_env(h, m, STUB_CLIP=inp, **envkw), inp=inp, bin_=bin_)
            o["ground_posts"] = [r for r in m.reqs() if r.get("image") and not r.get("stream")]; m.reset(); out.append(o)
        return out
    finally: m.stop()
def sentfig_case(name, n=2, bin_=CUR, dry=False, mock_kw=None, **envkw):
    """Phase-1-style PDF figure-caption selection with a page capture (grounding path)."""
    ans = "In Figure 2, the tallest bar marks the zorblax peak. The leftmost bar is the smallest one."
    m = Mock(MOCK_TEXT=ans, MOCK_VISION=ans, **(mock_kw or {"MOCK_GROUND": '{"bbox_2d": [100, 200, 300, 600], "label": "bar"}'}))
    try:
        h = mkhome(name[:10]); hp = hint(h, fx(FX["sentfig"]), "sentfig.pdf – Page 1 of 1"); out = []
        kw = dict(SPEAKEASY_VISION="never", STUB_CLIP=FX["sentfig_caption"], SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker")); kw.update(envkw)
        e = env_for(h, m.url, **kw)
        for i in range(n):
            cap = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_p2_{os.getpid()}_{time.time_ns()}.png"); shutil.copy(fx(FX["sentfig_png"]), cap)
            o = cli(f"{name}#{i}", ["selection", "--image", cap, "--capture-method", "bounds", "--capture-rect", "200,100,816,1056", "--doc-hint", hp] + (["--dry-run"] if dry else []), h, e, bin_=bin_, timeout=240)
            o["ground_posts"] = [r for r in m.reqs() if r.get("image") and not r.get("stream")]; m.reset(); out.append(o)
            if os.path.exists(cap): os.remove(cap)
        return out
    finally: m.stop()
def fixture_of(name):
    n = name.split("#")[0]
    for pre, f in (("fig5", "fig5.pdf"), ("x_t1", "fig5.pdf"), ("x_cap", "fig5.pdf"), ("two", FX["twocol"]), ("xf_", FX["twocol"]), ("paper", "paper.pdf"), ("multi", FX["multipage"]),
                   ("g_", FX["sentfig"]), ("fit_", FX["sentfig"]), ("ver_", FX["sentfig"]), ("x_sf", FX["sentfig"]), ("p_", "fig5.pdf")):
        if n.startswith(pre): return f
    return None
def shows(o): return [x for x in o["focus"] if x.get("state") == "show"]
def maps(o): return [x for x in o["focus"] if x.get("state") == "map"]
def docshows(o): return [x for x in shows(o) if isinstance(x.get("doc"), dict)]

# ================================================================ D. parse output
@section("D")
def parse_output():
    # columns is the parse's own two-column decision (not ground truth): only pure single-column pages are pinned to 1
    docs = {"fig5": (fx("fig5.pdf"), None), "paper": (fx("paper.pdf"), None), "prose": (fx("prose.pdf"), {1: 1}), "twocol": (fx(FX["twocol"]), None),
            "multipage": (fx(FX["multipage"]), {1: 1, 2: 1}), "sentfig": (fx(FX["sentfig"]), None), "blank": (fx(FX["blank"]), None), "tiny": (fx(FX["tiny"]), {1: 1})}
    pub = sorted(glob.glob(os.path.join(REPO, "tests", "audit", "bench", "fixtures", "public", "*", "doc.pdf")))
    for p in pub: docs["pub_" + os.path.basename(os.path.dirname(p))] = (p, None)
    if not pub: skip("D_public_bench_fixtures_invariants", "tests/audit/bench/fixtures/public/*/doc.pdf not present yet")
    # Phase 4 adds pages[].layout, layout, layout_events (shape-checked here) and figure split_from (4.5 two-way cut)
    ALLOWED_NEW = {"page": {"columns", "blocks", "layout"}, "figure": {"bbox_src", "split_from", "parts", "degenerate"}, "top": {"layout", "layout_events"}, "panel": {"src", "guessed"}}
    for tag, (pdf, cols) in docs.items():
        rc, d, err = parse(pdf); add(f"D_{tag}_parse_pdf_exit0", rc == 0 and d is not None, err)
        if d is None: continue
        report(f"D_{tag}", validate_parse(d, tag, cols))
        # additive only: every baseline key keeps its value; only the contract's new keys appear
        brc, b, _ = parse(pdf, os.path.join(BASE, "src"))
        if b is None: skip(f"D_{tag}_additive_vs_baseline", "baseline parse failed"); continue
        diffs, extras, boxdiffs = [], set(), []
        for k in b:
            if k != "pages" and b[k] != d.get(k): diffs.append(("top", k))
        extras |= {("top", k) for k in set(d) - set(b) - ALLOWED_NEW["top"]}
        L_ = d.get("layout")
        if "layout" in d and not (isinstance(L_, dict) and L_.get("status") in ("ok", "unavailable") and (L_.get("status") == "ok" or L_.get("reason") in LAYOUT_REASONS)): extras.add(("top", "layout", "shape"))
        if "layout_events" in d and not (isinstance(d["layout_events"], list) and all(layout_event_ok(e) for e in d["layout_events"])): extras.add(("top", "layout_events", "shape"))
        for bp, cp in zip(b.get("pages") or [], d.get("pages") or []):
            for k in bp:
                if k != "figures" and bp[k] != cp.get(k): diffs.append((bp.get("n"), k))
            extras |= {("page", k) for k in set(cp) - set(bp) - ALLOWED_NEW["page"]}
            if "layout" in cp and not layout_ok(cp["layout"], True): extras.add(("page", "layout", "shape"))
            bf, cf = bp.get("figures") or [], cp.get("figures") or []
            if len(bf) != len(cf): diffs.append((bp.get("n"), "n_figures", len(bf), len(cf)))
            for x, y in zip(bf, cf):
                why = contract_box_change(x, y, bp, len(bf))
                for k in x:
                    if x[k] != y.get(k) and not (why and k in BOX_KEYS) and not p5_same(k, x, y): diffs.append((bp.get("n"), x.get("label"), k))
                if why is False: boxdiffs.append((bp.get("n"), x.get("label"), x.get("kind"), x.get("bbox"), y.get("bbox")))
                extras |= {("figure", k) for k in set(y) - set(x) - ALLOWED_NEW["figure"]}
        if len(b.get("pages") or []) != len(d.get("pages") or []): diffs.append(("n_pages",))
        add(f"D_{tag}_existing_keys_unchanged_vs_baseline", not diffs, diffs[:5])
        # figure/table boxes: as at the baseline, or changed only as phase4-layout.md 4.4 / 4.5 / section 5 allow
        add(f"D_{tag}_figures_boxes_unchanged_or_contract_change_vs_baseline_{A.baseline_ref}", not boxdiffs, boxdiffs[:2])
        cols = {pg.get("n"): pg.get("columns") for pg in d.get("pages") or [] if pg.get("figs_done")}
        PARSE_COLS.setdefault(os.path.basename(pdf), cols)
        add(f"D_{tag}_no_new_keys_beyond_contract", not extras, sorted(extras)[:6])
        # no text in the new keys
        new_vals = json.dumps([[p.get("columns"), p.get("blocks")] for p in d.get("pages") or []] + [f.get("bbox_src") for p in d.get("pages") or [] for f in p.get("figures") or []])
        add(f"D_{tag}_new_keys_no_sentinel_text", not sentinel_hits(new_vals), sentinel_hits(new_vals))
    # specific expectations on the synthetic layouts
    rc, d, _ = parse(fx("prose.pdf"))
    if d:
        p = d["pages"][0]; inpara = set().union(*[set(b.get("lines") or []) for b in p.get("blocks") or [] if b.get("kind") == "paragraph"] or [set()])
        add("D_prose_every_body_line_in_a_paragraph", inpara == set(range(len(p["lines"]))), sorted(set(range(len(p["lines"]))) - inpara))
    rc, d, _ = parse(fx("fig5.pdf"))
    if d:
        p = d["pages"][0]; inpara = set().union(*[set(b.get("lines") or []) for b in p.get("blocks") or [] if b.get("kind") == "paragraph"] or [set()])
        want = {i for i, l in enumerate(p["lines"]) if l[4].startswith("Prose line")}
        add("D_fig5_left_prose_lines_in_paragraphs", want and want <= inpara, sorted(want - inpara))
        part = {i for i, l in enumerate(p["lines"]) if l[4] in ("Recordings", "Post-Processing", "Feature Extraction", "Models", "a)", "b)")}
        add("D_fig5_figure_inner_labels_not_paragraphs", not (part & inpara), sorted(part & inpara))
        kinds = {b.get("kind") for b in p.get("blocks") or []}
        add("D_fig5_has_all_four_block_kinds", kinds >= BLOCK_KINDS, sorted(kinds))
    rc, d, _ = parse(fx(FX["blank"]))
    if d: add("D_blank_page_no_blocks", all((p.get("blocks") == []) for p in d["pages"] if p.get("figs_done")), [p.get("blocks") for p in d["pages"]])
    rc, d, _ = parse(fx(FX["multipage"]))
    if d and len(d["pages"]) >= 2:
        f6 = [f for f in d["pages"][1].get("figures") or [] if f.get("label") == "Figure 6"]
        add("D_multipage_raster_figure_bbox_src_graphics", f6 and f6[0].get("bbox_src") == "graphics", [f.get("bbox_src") for f in f6])
    # `docmap.py dump` gains the same keys (dump prints a size-capped dump: use a document small enough to print whole)
    for tag, pdf in (("tiny", fx(FX["tiny"])),):
        cp = os.path.join(TMPROOT, "dump_" + os.path.basename(pdf)); shutil.copy(pdf, cp)
        p = subprocess.run([PY, os.path.join(SRC, "docmap.py"), "dump", cp], capture_output=True, text=True, env=scrub_env(), timeout=120)
        txt = p.stdout
        add(f"D_dump_{tag}_exit0", p.returncode == 0, p.stderr[-200:])
        add(f"D_dump_{tag}_shows_columns_blocks_bbox_src", all(f'"{k}"' in txt for k in ("columns", "blocks", "bbox_src")), [k for k in ("columns", "blocks", "bbox_src") if f'"{k}"' not in txt])
        try: dd = json.loads(txt)
        except Exception: dd = None
        rc2, pp, _ = parse(pdf)
        if dd is not None and pp: add(f"D_dump_{tag}_new_keys_equal_parse_pdf", [(p_.get("columns"), p_.get("blocks"), [f.get("bbox_src") for f in p_.get("figures") or []]) for p_ in dd.get("pages") or []]
                                      == [(p_.get("columns"), p_.get("blocks"), [f.get("bbox_src") for f in p_.get("figures") or []]) for p_ in pp["pages"]], "")
        else: skip(f"D_dump_{tag}_new_keys_equal_parse_pdf", f"dump output not whole JSON ({len(txt)} chars)")

# ================================================================ V. PARSE_VERSION bump invalidates old cache entries
@section("V")
def parse_version():
    m = Mock(MOCK_TEXT="In Figure 2, the tallest bar marks the peak. The leftmost bar is the smallest one.")
    try:
        h = mkhome("ver"); hp = hint(h, fx(FX["sentfig"]), "sentfig.pdf"); e = env_for(h, m.url, SPEAKEASY_VISION="never", SPEAKEASY_GROUND="off", STUB_CLIP=FX["sentfig_caption"], SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker"))
        def go(bin_, tag):
            cap = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_p2v_{time.time_ns()}.png"); shutil.copy(fx(FX["sentfig_png"]), cap)
            o = cli(tag, ["selection", "--image", cap, "--capture-method", "bounds", "--capture-rect", "200,100,816,1056", "--doc-hint", hp], h, e, bin_=bin_)
            if os.path.exists(cap): os.remove(cap)
            return o
        for i in range(2): go(BIN_BASE, f"ver_base#{i}")
        time.sleep(1.0)
        files = sorted(glob.glob(os.path.join(h, "cache", "doc", "*.json")))
        add("V_baseline_wrote_cache", files, files)
        if not files: return
        old = [json.load(open(f)) for f in files]
        add("V_baseline_cache_has_no_blocks", not any("blocks" in p for d in old for p in d.get("pages") or []), "")
        TAMP = [1.0, 1.0, 2.0, 2.0]
        for f, d in zip(files, old):
            for p in d.get("pages") or []:
                for fg in p.get("figures") or []: fg["bbox"] = list(TAMP)
            json.dump(d, open(f, "w"))
        o1 = go(CUR, "ver_cur#0"); time.sleep(1.0); o2 = go(CUR, "ver_cur#1"); time.sleep(0.5); o3 = go(CUR, "ver_cur#2")
        r1_ = o1["rec"] or {}
        add("V_old_cache_entry_not_a_hit", r1_.get("doc_cache") != "hit", r1_.get("doc_cache"))
        used = [x["doc"].get("target") for o in (o1, o2, o3) for x in docshows(o) if x["doc"].get("target") == TAMP]
        add("V_stale_entry_never_used_for_targets", not used, used[:2])
        new = cache_docs(h)
        add("V_cache_rebuilt_with_new_keys", any(all("blocks" in p and "columns" in p for p in d.get("pages") or [] if p.get("figs_done")) and d.get("pages")
                                                and all("bbox_src" in fg for p in d["pages"] for fg in p.get("figures") or []) for d in new), [sorted(p) for d in new for p in d.get("pages") or []][:2])
        add("V_rebuilt_entry_is_hit_on_later_read", (o3["rec"] or {}).get("doc_cache") == "hit", [(o["rec"] or {}).get("doc_cache") for o in (o1, o2, o3)])
        tg = [x["doc"].get("target") for x in docshows(o3) if x["doc"].get("kind") == "figure"]
        add("V_rebuilt_entry_targets_real_figure", tg and all(t != TAMP for t in tg), tg[:2])
        for i, d in enumerate(new):
            if any("blocks" in p for p in d.get("pages") or []): report(f"V_cache_entry{i}", validate_parse(d, "cache"))
    finally: m.stop()

# ================================================================ scenario runs (A/B/C/G/P share them)
SC = {}
def scenarios():
    if SC: return SC
    f5 = fx("fig5.pdf"); cap = find_line(f5, 1, "Fig. 5"); t1 = find_line(f5, 1, "Table 1")
    SC["fig5_cap"] = bench_case("fig5_cap", f5, 1, [cap, cap + 1], "Figure 5 shows the drowsiness detector pipeline. In panel a, ear recordings are re-referenced and filtered. Panel b shows cross-validation. The Post-Processing step cleans the data.")
    SC["fig5_t1"] = bench_case("fig5_t1", f5, 1, [t1], "Table 1 lists the system specifications. The battery life is 44 hours. The ADC resolution is 15 bits.")
    SC["fig5_prose"] = bench_case("fig5_prose", f5, 1, [2, 3, 4], "Prose line 3 is about ear EEG electrodes and the drowsiness protocol. Prose line 4 continues the same protocol description. Prose line 5 repeats it once more for the record.")
    tc = fx(FX["twocol"])
    SC["two_prose"] = bench_case("two_prose", tc, 1, [find_line(tc, 1, "The zorblax electrodes"), find_line(tc, 1, "Quintessa readings")], "The zorblax electrodes were placed behind each ear before the drive. Quintessa readings were sampled at two hundred and fifty hertz.")
    SC["two_t2"] = bench_case("two_t2", tc, 1, [find_line(tc, 1, "Table 2 |")], "Table 2 lists the zorblax mirevolt settings. The quintessa rate is 250 hertz. The plumbrine gain is 24.")
    SC["two_fig3"] = bench_case("two_fig3", tc, 1, [find_line(tc, 1, "Figure 3 |"), find_line(tc, 1, "a Dunmarrow")], "Figure 3 shows the plumbrine vexhollow pipeline. In panel a, the Zorblax unit captures dunmarrow data. Panel b shows grelquist scoring. The Skelvane box scores frumplet events.")
    SC["two_t4"] = bench_case("two_t4", tc, 1, [find_line(tc, 1, "Table 4 |")], "Table 4 lists the frumplet outcomes. There were 41 zorblax hits. There were 7 quintessa misses.")
    pp = fx("paper.pdf")
    SC["paper_fig4"] = bench_case("paper_fig4", pp, 2, [find_line(pp, 2, "Figure 4:")], "In Figure 4a, alpha power differs by condition. In Figure 4b, the drowsiness score climbs after minute 30.")
    mp = fx(FX["multipage"])
    SC["multi_fig6"] = bench_case("multi_fig6", mp, 2, [find_line(mp, 2, "Figure 6:")], "Figure 6 shows grelquist counts per dunmarrow trial. The second bar is the tallest.")
    SC["fig5_dry"] = bench_case("fig5_dry", f5, 1, [t1], "Table 1 lists the system specifications. The battery life is 44 hours.", extra=["--dry-run"])
    SC["fig5_sel_cmd"] = bench_case("fig5_selcmd", f5, 1, [cap], "Figure 5 shows the pipeline. Panel b shows cross-validation.", cmd="selection")
    SC["fig5_focus_off"] = bench_case("fig5_focusoff", f5, 1, [t1], "Table 1 lists the system specifications. The battery life is 44 hours.", SPEAKEASY_FOCUS="off")
    SC["fig5_docread_off"] = bench_case("fig5_docoff", f5, 1, [t1], "Table 1 lists the system specifications.", SPEAKEASY_DOC_READ="off")
    SC["fig5_logtext"] = bench_case("fig5_logtext", f5, 1, [cap, cap + 1], "Figure 5 shows the zorblax pipeline. In panel a, quintessa recordings are filtered. Panel b validates mirevolt models.", SPEAKEASY_LOG_TEXT="1")
    # corrupt PDF behind a valid hint (doc job runs and fails), markdown doc, no doc at all
    m = Mock(MOCK_TEXT="The zorblax note is short. It has two sentences.")
    try:
        for nm, path in (("corrupt", fx(FX["corrupt"])), ("markdown", fx("notes.md"))):
            h = mkhome(nm); hp = hint(h, path, os.path.basename(path)); e = bench_env(h, m)
            SC[nm] = [cli(f"{nm}#{i}", ["speak", "--capture-method", "bounds", "--capture-rect=100,100,600,80", "--doc-hint", hp], h, e, inp="Ear-worn EEG sensors can track drowsiness during long drives.") for i in range(2)]
        h = mkhome("nodoc"); e = bench_env(h, m)
        SC["nodoc_speak"] = [cli("nodoc_speak", ["speak"], h, e, inp="The deployment finished on Tuesday after a long delay. Users reported faster page loads.")]
        SC["nodoc_dry"] = [cli("nodoc_dry", ["speak", "--dry-run"], h, e, inp=ZIP_TABLE)]
        SC["rewrite"] = [cli("rewrite", ["rewrite"], h, e, inp="Hello there, zorblax quintessa test for rewrite.")]
        SC["sel_none_dry"] = [cli("sel_none_dry", ["selection", "--capture-method", "none", "--dry-run"], h, dict(e, STUB_CLIP=SENT_TABLE))]
        h2 = mkhome("fallback"); e2 = bench_env(h2, None)
        SC["fallback"] = [cli("fallback", ["speak"], h2, e2, inp="The zorblax deployment finished on Tuesday after a long delay.")]
    finally: m.stop()
    # phase-1-style text selection over a table screenshot (overlap/progress line matching), text logging off/on
    m = Mock(MOCK_TEXT=SENT_ANSWER)
    try:
        for nm, lt in (("tbl_off", None), ("tbl_on", "1")):
            h = mkhome(nm); e = env_for(h, m.url, SPEAKEASY_LOG_TEXT=lt, SPEAKEASY_VERBATIM="off", SPEAKEASY_VISION="never", STUB_CLIP=SENT_TABLE, SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker"))
            cap = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_p2t_{time.time_ns()}.png"); shutil.copy(os.path.join(IMGS, "table.png"), cap)
            SC[nm] = [cli(nm, ["selection", "--image", cap, "--capture-method", "bounds", "--capture-rect", "100,200,700,300"], h, e)]
            if os.path.exists(cap): os.remove(cap)
    finally: m.stop()
    m = Mock(MOCK_TEXT="[L2] Mirevolt has 42 quintessa units. [L3] Plumbrine has 77 quintessa units.")
    try:
        h = mkhome("tags"); e = env_for(h, m.url, SPEAKEASY_VERBATIM="off", SPEAKEASY_VISION="never", SPEAKEASY_FOCUS_TAGS="on", STUB_CLIP=SENT_TABLE, SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker"))
        cap = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_p2g_{time.time_ns()}.png"); shutil.copy(os.path.join(IMGS, "table.png"), cap)
        SC["tags"] = [cli("tags", ["selection", "--image", cap, "--capture-method", "bounds", "--capture-rect", "100,200,700,300"], h, e)]
        if os.path.exists(cap): os.remove(cap)
    finally: m.stop()
    # grounding scenarios on the raster sentinel figure
    SC["g_box"] = sentfig_case("g_box", SPEAKEASY_GROUND="local")
    SC["g_nobox"] = sentfig_case("g_nobox", mock_kw={"MOCK_GROUND": "I cannot find that part in the image."}, SPEAKEASY_GROUND="local")
    SC["g_huge"] = sentfig_case("g_huge", mock_kw={"MOCK_GROUND": '{"bbox_2d": [0, 0, 1000, 1000]}'}, SPEAKEASY_GROUND="local")
    SC["g_err"] = sentfig_case("g_err", mock_kw={"MOCK_GROUND_STATUS": "500"}, SPEAKEASY_GROUND="local")
    SC["g_slow"] = sentfig_case("g_slow", mock_kw={"MOCK_GROUND": '{"bbox_2d": [100, 200, 300, 600]}', "MOCK_GROUND_DELAY": "25"}, SPEAKEASY_GROUND="local")
    SC["g_off"] = sentfig_case("g_off", SPEAKEASY_GROUND="off")
    SC["g_logtext"] = sentfig_case("g_logtext", mock_kw={"MOCK_GROUND": "no box here"}, SPEAKEASY_GROUND="local", SPEAKEASY_LOG_TEXT="1")
    SC["g_dry"] = sentfig_case("g_dry", dry=True, SPEAKEASY_GROUND="local")
    SC["g_late_nobox"] = sentfig_case("g_late_nobox", mock_kw={"MOCK_GROUND": "No box in this answer.", "MOCK_GROUND_DELAY": "1.5"}, SPEAKEASY_GROUND="local")
    # Phase 5 picker (phase5-parts.md 4-6, A2.9): letter picks on the vector Fig. 5 through /api/chat; phrases name no label
    f5 = fx("fig5.pdf"); pcap = find_line(f5, 1, "Fig. 5")
    PS = "Figure 5 shows the drowsiness detector pipeline. The orange cable links the two rows. The grey cover protects the small board."
    def top(e): return {"MOCK_PICK_TOP": json.dumps([{"token": t, "logprob": math.log(p)} for t, p in e])}
    pk = dict(SPEAKEASY_GROUND="local", SPEAKEASY_PICK="on", STUB_PLAY_SEC="1.0")
    for nm, mk, extra in (("p_acc", top([("A", 0.99)]), {}), ("p_offmenu", top([("The", 0.9), ("A", 0.05)]), {}), ("p_low", top([("A", 0.3), ("B", 0.3), ("C", 0.3)]), {}),
                          ("p_nolp", {}, {}), ("p_http", {"MOCK_GROUND_STATUS": "500"}, {}), ("p_malformed", {"MOCK_PICK_MALFORMED": "1"}, {}),
                          ("p_slow", dict(top([("A", 0.99)]), MOCK_GROUND_DELAY="20"), {}), ("p_off", top([("A", 0.99)]), {"SPEAKEASY_GROUND": "off"}),
                          ("p_logtext", top([("The", 0.9)]), {"SPEAKEASY_LOG_TEXT": "1"}), ("p_default", top([("A", 0.99)]), {"SPEAKEASY_PICK": None})):
        kw = dict(pk); kw.update(extra)
        SC[nm] = bench_case(nm, f5, 1, [pcap, pcap + 1], PS, mock_kw=mk, **{k: v for k, v in kw.items() if v is not None})
    # capture fit forced to fail (OCR returns no lines) vs landing, on a figure-part sentence with grounding
    for nm, mode in (("fit_miss", "empty"), ("fit_ok", "pass")):
        w = ocr_wrapper(mode)
        if w: SC[nm] = sentfig_case(nm, SPEAKEASY_GROUND="local", SPEAKEASY_VISION="always", SPEAKEASY_CAPTURE_FIT="on", SPEAKEASY_TRACKER=w)
    # label redaction (F): part ovals with sentinel part labels, text logging unset / not exactly 1 / exactly 1
    tc = fx(FX["twocol"]); f3 = [find_line(tc, 1, "Figure 3 |"), find_line(tc, 1, "a Dunmarrow")]
    F3 = "Figure 3 shows the plumbrine vexhollow pipeline. In panel a, the Zorblax unit captures dunmarrow data. Panel b shows grelquist scoring. The Skelvane box scores frumplet events."
    SC["two_fig3_true"] = bench_case("two_fig3_true", tc, 1, f3, F3, SPEAKEASY_LOG_TEXT="true")
    SC["two_fig3_on"] = bench_case("two_fig3_on", tc, 1, f3, F3, SPEAKEASY_LOG_TEXT="1")
    return SC

# ---------------------------------------------------------------- universal validators
def check_focus(o, F):
    """Per-run focus-log checks. Returns {check: [violations]}."""
    v = {k: [] for k in ("map_sent_int", "map_sent_0_to_k_minus_1_in_order", "map_score_key_present", "map_runner_up_key_present", "map_score_type", "map_score_rounded_2dp",
                         "map_runner_up_null_when_score_null", "map_runner_up_le_score", "map_overlap_score_ge_1", "map_progress_score_below_1_or_null", "map_tag_scores_null",
                         "show_sent_int", "show_sent_refers_to_map", "show_after_its_map", "show_text_hash_matches_map_of_same_sent", "clear_plain_has_no_sent", "clear_hold_has_sent",
                         "target_src_iff_target", "target_src_enum", "split_from_label_form", "split_from_only_fig_table_kinds", "late_update_keeps_earlier_sent",
                         "kind_table_target_src_table", "kind_table_row_target_src_table_rows", "kind_line_target_src_lines", "kind_panel_target_src_panel", "grounded_object_target_src_vlm",
                         "label_object_target_src_label", "figure_box_target_src_figure", "target_src_suffix_equals_cache_bbox_src", "split_from_iff_cache_split_from", "panel_guessed_matches_cache")}
    ms = [x for x in F if x.get("state") == "map"]; sents = [x.get("sent") for x in ms]
    for x in ms:
        if not isint(x.get("sent")): v["map_sent_int"].append(x.get("sent"))
        if "score" not in x: v["map_score_key_present"].append(x.get("method"))
        if "runner_up" not in x: v["map_runner_up_key_present"].append(x.get("method"))
        s, ru, meth = x.get("score"), x.get("runner_up"), str(x.get("method"))
        for val in (s, ru):
            if val is not None and not isnum(val): v["map_score_type"].append((meth, val))
            elif val is not None and not r2(val): v["map_score_rounded_2dp"].append((meth, val))
        if s is None and ru is not None: v["map_runner_up_null_when_score_null"].append((meth, ru))
        if isnum(s) and isnum(ru) and ru > s + 0.011: v["map_runner_up_le_score"].append((meth, s, ru))
        if meth == "overlap" and not (isnum(s) and s >= 1.0 - 1e-9): v["map_overlap_score_ge_1"].append(s)
        if meth == "progress" and not (s is None or (isnum(s) and s < 1.0)): v["map_progress_score_below_1_or_null"].append(s)
        if meth.startswith("tag") and (s is not None or ru is not None): v["map_tag_scores_null"].append((meth, s, ru))
    if ms and sents != list(range(len(ms))): v["map_sent_0_to_k_minus_1_in_order"].append(sents[:20])
    seen_map, seen_show = {}, set()
    by_sent = {x.get("sent"): x for x in ms}
    for i, x in enumerate(F):
        st = x.get("state")
        if st == "map": seen_map[x.get("sent")] = i
        if st == "show":
            s = x.get("sent")
            if not isint(s): v["show_sent_int"].append(s)
            elif ms and s not in by_sent: v["show_sent_refers_to_map"].append((s, sorted(by_sent)))
            elif ms and s not in seen_map: v["show_after_its_map"].append(s)
            mm = by_sent.get(s)
            if mm and isint(mm.get("text_len")) and mm.get("text_len") == x.get("text_len") and mm.get("text_hash") != x.get("text_hash"): v["show_text_hash_matches_map_of_same_sent"].append(s)
            if (x.get("late_fit") or ((x.get("doc") or {}).get("target_src") == "vlm")) and s not in seen_show: v["late_update_keeps_earlier_sent"].append((s, sorted(seen_show)))
            seen_show.add(s)
        if st == "clear" and x.get("hold") is True: seen_show.add(x.get("sent"))  # a held box is the chunk's earlier record
        if st == "clear":
            if x.get("hold") is True and not isint(x.get("sent")): v["clear_hold_has_sent"].append(x)
            if x.get("hold") is not True and "sent" in x: v["clear_plain_has_no_sent"].append(x.get("sent"))
    for x in F:
        if x.get("state") != "show" or not isinstance(x.get("doc"), dict): continue
        d = x["doc"]; ts = d.get("target_src"); kind = d.get("kind"); lab = str(d.get("label") or "")
        if ("target" in d and d.get("target") is not None) != ("target_src" in d): v["target_src_iff_target"].append((kind, "target" in d, ts))
        if "target_src" in d and not target_src_ok(ts): v["target_src_enum"].append(ts)
        if "split_from" in d:
            if not (isinstance(d["split_from"], str) and LABEL_RE.match(d["split_from"])): v["split_from_label_form"].append(d["split_from"])
            if kind not in ("figure", "table", "table_row"): v["split_from_only_fig_table_kinds"].append(kind)
        if ts is None: continue
        if kind == "table" and not str(ts).startswith("table:"): v["kind_table_target_src_table"].append(ts)
        if kind == "table_row" and ts != "table-rows": v["kind_table_row_target_src_table_rows"].append(ts)
        if kind in ("line", "lines") and ts != "lines": v["kind_line_target_src_lines"].append(ts)
        if kind == "panel" and ts not in ("panel-labels", "panel-guessed"): v["kind_panel_target_src_panel"].append(ts)
        if kind == "object" and "(grounded)" in lab and ts != "pick": v["grounded_object_target_src_vlm"].append(ts)   # pick: phase5-parts.md 6.3 (bbox_2d retired)
        if kind == "object" and "(grounded)" not in lab and ts not in ("label-object", "label-pad"): v["label_object_target_src_label"].append((lab[:0], ts))
        page = d.get("page"); base_label = re.match(r"^((?:Figure|Table) \d+)", lab); base_label = base_label.group(1) if base_label else None
        cf = cache_fig(o["home"], page, base_label) if base_label else None
        if kind == "figure" and cf and d.get("target") == cf.get("bbox") and ts != f"figure:{cf.get('bbox_src')}": v["figure_box_target_src_figure"].append((ts, cf.get("bbox_src")))
        mt = re.fullmatch(r"(figure|table):(.+)", str(ts))
        if mt and cf and mt.group(2) != cf.get("bbox_src"): v["target_src_suffix_equals_cache_bbox_src"].append((ts, cf.get("bbox_src")))
        if kind in ("figure", "table", "table_row") and cf is not None and d.get("split_from") != cf.get("split_from"): v["split_from_iff_cache_split_from"].append((kind, d.get("split_from"), cf.get("split_from")))
        if kind == "panel" and cf:
            pl = re.match(r"^(?:Figure|Table) \d+([a-z])", lab); pn = [pp for pp in cf.get("panels") or [] if pl and pp.get("label") == pl.group(1)]
            if pn and ts != ("panel-guessed" if pn[0].get("guessed") else "panel-labels"): v["panel_guessed_matches_cache"].append((lab[-2:], ts, pn[0].get("guessed")))
    return v

def check_run(o):
    """Per-run runs-record checks. Returns {check: [violations]} (empty list = pass, None = not applicable)."""
    r, raw = o["rec"], o["raw"]; v = {}
    if r is None: return {"runs_record_written": ["none"]}
    cmd = raw.get("command"); F = o["focus"]; sh = [x for x in F if x.get("state") == "show"]; has_hold = any(x.get("hold") for x in F)
    if cmd in ("speak", "selection"):
        v["first_focus_ms_present"] = [] if "first_focus_ms" in raw else ["absent"]
        ff = raw.get("first_focus_ms")
        v["first_focus_ms_int_or_null"] = [] if (ff is None or isint(ff)) else [ff]
        v["first_focus_ms_null_iff_no_show"] = [] if ((ff is None) == (not sh)) else [(ff, len(sh))]
        if isint(ff) and sh:
            tot = r.get("total_ms")
            v["first_focus_ms_within_run"] = [] if (ff >= 0 and (not isnum(tot) or ff <= tot + 20)) else [(ff, tot)]
            t0 = sh[0].get("ts", 0) - ff / 1000.0
            v["first_focus_ms_measured_from_run_start"] = [] if (o["t0"] - 0.15 <= t0 <= o["t0"] + 5.0) else [(round(t0 - o["t0"], 3), "s after launch")]
            fs, lr, lt = r.get("first_sentence_ms"), r.get("llm_request_at_ms"), r.get("llm_first_token_ms")
            if not has_hold and raw.get("backend") == "llm" and isnum(fs): v["first_focus_ms_not_before_first_sentence"] = [] if ff >= fs - 3 else [(ff, fs)]
            if not has_hold and raw.get("backend") == "llm" and isnum(lr) and isnum(lt): v["first_focus_ms_not_before_first_token"] = [] if ff >= lr + lt - 3 else [(ff, lr, lt)]
    elif cmd == "rewrite":
        v["rewrite_has_no_first_focus_ms"] = [] if "first_focus_ms" not in raw else [raw.get("first_focus_ms")]
    if cmd in ("speak", "selection", "rewrite"):
        v["doc_ready_ms_present"] = [] if "doc_ready_ms" in raw else ["absent"]
        dr = raw.get("doc_ready_ms")
        v["doc_ready_ms_int_or_null"] = [] if (dr is None or isint(dr)) else [dr]
        if isint(dr):
            v["doc_ready_ms_nonneg"] = [] if dr >= 0 else [dr]
            if isint(r.get("doc_ms")): v["doc_ready_ms_ge_doc_ms"] = [] if dr >= r["doc_ms"] - 3 else [(dr, r["doc_ms"])]
    else:
        v["other_commands_have_no_new_run_keys"] = sorted(NEW_RUN_KEYS & set(raw))
    # doc_parse
    page = raw.get("page"); dp = raw.get("doc_parse")
    v["doc_parse_iff_page"] = [] if (("doc_parse" in raw) == (page is not None)) or cmd not in ("speak", "selection", "rewrite") else [(page, "doc_parse" in raw)]
    if isinstance(dp, dict):
        errs = []
        if not (isint(dp.get("columns")) and dp.get("columns") in (1, 2)): errs.append(("columns", dp.get("columns")))
        if not isinstance(dp.get("figs_done"), bool): errs.append(("figs_done", dp.get("figs_done")))
        bl = dp.get("blocks")
        if dp.get("figs_done") is False:
            if set(dp) != {"columns", "figs_done", "blocks"} or not isinstance(bl, dict) or set(bl) != {"paragraph"}: errs.append(("figs_false_keys", sorted(dp), sorted(bl or {})))
        else:
            want = {"columns", "figs_done", "blocks", "bbox_src", "labels_src", "panels", "split", "layout", "parts"}   # layout: phase4-layout.md 6.3; parts: phase5-parts.md 2.5
            if set(dp) != want: errs.append(("keys", sorted(set(dp) ^ want)))
            if "layout" in dp and not layout_ok(dp["layout"], False): errs.append(("layout", dp.get("layout")))
            if not (isinstance(bl, dict) and set(bl) == BLOCK_KINDS and all(isint(x) and x >= 0 for x in bl.values())): errs.append(("blocks", bl))
            bs = dp.get("bbox_src")
            if not (isinstance(bs, dict) and set(bs) <= ALL_SRC and all(isint(x) and x >= 0 for x in bs.values())): errs.append(("bbox_src", bs))
            ls = dp.get("labels_src")
            if not (isinstance(ls, dict) and set(ls) == {"pdf", "ocr", "none"} and all(isint(x) and x >= 0 for x in ls.values())): errs.append(("labels_src", ls))
            pn = dp.get("panels")
            if not (isinstance(pn, dict) and set(pn) == {"labels", "guessed", "gutter"} and all(isint(x) and x >= 0 for x in pn.values())): errs.append(("panels", pn))
            pt = dp.get("parts")
            if not (isinstance(pt, dict) and set(pt) == {"text", "callout", "callout_attached"} and all(isint(x) and x >= 0 for x in pt.values()) and pt["callout_attached"] <= pt["callout"]): errs.append(("parts", pt))
            if not (isint(dp.get("split")) and dp["split"] >= 0): errs.append(("split", dp.get("split")))
            if not errs:
                nf = bl["figure"] + bl["table"]
                if sum(bs.values()) != nf: errs.append(("sum_bbox_src_ne_fig+table", bs, nf))
                if sum(ls.values()) != nf: errs.append(("sum_labels_src_ne_fig+table", ls, nf))
                if bl["caption"] != nf: errs.append(("captions_ne_fig+table", bl))
                if dp["split"] > nf: errs.append(("split_gt_figs", dp["split"], nf))
        v["doc_parse_schema"] = errs
        # cross-check with the cache entry of the matched page
        cp = [p for d in cache_docs(o["home"]) for p in d.get("pages") or [] if p.get("n") == page]
        if cp and not errs:
            p = cp[0]; figs = p.get("figures") or []; cb = p.get("blocks") or []; x = []
            if p.get("columns") != dp["columns"]: x.append(("columns", dp["columns"], p.get("columns")))
            if bool(p.get("figs_done")) != dp["figs_done"]: x.append(("figs_done", dp["figs_done"], p.get("figs_done")))
            cnt = {k: sum(1 for b in cb if b.get("kind") == k) for k in BLOCK_KINDS}
            for k in dp["blocks"]:
                if dp["blocks"][k] != cnt[k]: x.append(("blocks." + k, dp["blocks"][k], cnt[k]))
            if dp["figs_done"]:
                csrc = {}
                for f in figs: csrc[f.get("bbox_src")] = csrc.get(f.get("bbox_src"), 0) + 1
                if {k: c for k, c in dp["bbox_src"].items() if c} != csrc: x.append(("bbox_src", dp["bbox_src"], csrc))
                cl = {k: sum(1 for f in figs if f.get("labels_src") == k) for k in ("pdf", "ocr", "none")}
                if dp["labels_src"] != cl: x.append(("labels_src", dp["labels_src"], cl))
                g = sum(1 for f in figs for pp in f.get("panels") or [] if pp.get("guessed")); lb = sum(1 for f in figs for pp in f.get("panels") or [] if not pp.get("guessed"))
                gu = sum(1 for f in figs for pp in f.get("panels") or [] if pp.get("src") == "gutter"); lb -= gu
                if dp["panels"] != {"labels": lb, "guessed": g, "gutter": gu}: x.append(("panels", dp["panels"], {"labels": lb, "guessed": g, "gutter": gu}))
                cp_ = {"text": sum(1 for f in figs for q in f.get("parts") or [] if q.get("kind") == "text"), "callout": sum(1 for f in figs for q in f.get("parts") or [] if q.get("kind") == "callout"),
                       "callout_attached": sum(1 for f in figs for q in f.get("parts") or [] if q.get("kind") == "callout" and q.get("attached"))}
                if dp.get("parts") != cp_: x.append(("parts", dp.get("parts"), cp_))
                sp = sum(1 for f in figs if f.get("split_from"))
                if dp["split"] != sp: x.append(("split", dp["split"], sp))
                pl = {k: v for k, v in (p.get("layout") or {}).items() if k != "detector_version"}
                if dp.get("layout") != pl: x.append(("layout", dp.get("layout"), pl))
            v["doc_parse_matches_cache_page"] = x
    # ground_miss_reasons
    gm = raw.get("ground_miss_reasons"); fm = raw.get("focus_methods") or {}
    if "ground_miss_reasons" in raw:
        e = []
        if not (isinstance(gm, dict) and gm and set(gm) <= REASONS and all(isint(c) and c >= 0 for c in gm.values()) and sum(gm.values()) >= 1): e.append(("shape", gm))
        else:
            if sum(gm.get(r, 0) for r in MISS_REASONS) != fm.get("ground-miss", 0):
                e.append(("sum_ne_ground_miss", gm, fm.get("ground-miss", 0)))
            if gm.get("error", 0) and not raw.get("ground_errors"): e.append(("error_without_ground_errors", gm))
            if isinstance(raw.get("ground_errors"), dict) and gm.get("error", 0) > (raw["ground_errors"].get("count") or 0): e.append(("error_gt_ground_errors.count", gm.get("error"), raw["ground_errors"].get("count")))
            if "ground_posts" in o and sum(gm.values()) - gm.get("unavailable", 0) - gm.get("no-logprobs", 0) > len(o["ground_posts"]) + gm.get("timeout", 0): e.append(("more_reasons_than_ground_requests", gm, len(o["ground_posts"])))
        v["ground_miss_reasons_valid"] = e
    else:
        v["ground_miss_reasons_absent_means_no_ground_miss"] = [] if not fm.get("ground-miss") else [fm.get("ground-miss")]
        if raw.get("ground_errors"): v["ground_errors_imply_ground_miss_reasons"] = [raw.get("ground_errors")]
    # Phase 5: every pick is counted once, under a reason or as ground-pick (6.2 invariant); picks sent == called <= requests
    pk = raw.get("part_picks"); tot = (sum(gm.values()) if isinstance(gm, dict) else 0) + fm.get("ground-pick", 0)
    v["ground_reasons_plus_pick_eq_part_picks_requests"] = [] if tot == ((pk or {}).get("requests") or 0) else [(gm, fm.get("ground-pick", 0), (pk or {}).get("requests"))]
    if "part_picks" in raw: v["part_picks_shape"] = [] if part_picks_ok(pk) else [pk]
    if "ground_posts" in o and isinstance(pk, dict):
        v["pick_requests_seen_eq_called"] = [] if len(o["ground_posts"]) <= (pk.get("called") or 0) <= len(o["ground_posts"]) + (gm or {}).get("timeout", 0) else [(len(o["ground_posts"]), pk.get("called"))]
    v["no_retired_bbox_keys"] = sorted((RETIRED_RUN_KEYS & set(raw)) | (RETIRED_METHODS & set(fm)) | ({"no-box", "too-large"} & set(gm or {})))
    return v

def new_field_values(o):
    """Every value of a new field in this run (records + live file), for the privacy/type checks."""
    vals = []
    for x in o["focus"] + o["live"]:
        for k in ("sent", "score", "runner_up"):
            if k in x: vals.append(("focus." + k, x[k]))
        d = x.get("doc") if isinstance(x.get("doc"), dict) else {}
        for k in ("target_src", "split_from"):
            if k in d: vals.append(("doc." + k, d[k]))
    for rec in o["recs"]:
        for k in NEW_RUN_KEYS | P4_RUN | {"part_picks"}:
            if k in rec: vals.append(("runs." + k, rec[k]))
    return vals
def textfree(k, val):
    """True when a new-field value holds only numbers / enums / booleans / Figure-Table labels."""
    if k == "runs.layout_events": return isinstance(val, list) and all(layout_event_ok(e) for e in val)
    if isinstance(val, dict) and k == "runs.doc_parse" and "layout" in val:
        return layout_ok(val["layout"], False) and textfree(k, {kk: vv for kk, vv in val.items() if kk != "layout"})
    if k == "runs.part_picks": return part_picks_ok(val)
    if isinstance(val, dict): return all(isinstance(kk, str) and (kk in BLOCK_KINDS | ALL_SRC | REASONS | {"columns", "figs_done", "blocks", "bbox_src", "labels_src", "panels", "split", "pdf", "ocr", "none", "labels", "guessed"} | P5_COUNT_KEYS) and textfree(k, vv) for kk, vv in val.items())
    if isinstance(val, list): return all(textfree(k, x) for x in val)
    if isinstance(val, str): return (k == "doc.target_src" and target_src_ok(val)) or (k == "doc.split_from" and bool(LABEL_RE.match(val)))
    return val is None or isinstance(val, (bool, int, float))

@section("A")
def focus_checks():
    S = scenarios(); agg = {}
    for o in ALL:
        if not o["focus"]: continue
        for k, viol in check_focus(o, o["focus"]).items():
            agg.setdefault(k, {"n": 0, "bad": []}); agg[k]["n"] += 1
            if viol: agg[k]["bad"].append((o["name"], viol[:2]))
    for k, a in sorted(agg.items()): add(f"A_B_{k}", not a["bad"], a["bad"][:3])
    # coverage: the scenarios really exercised the interesting branches
    allF = [x for o in ALL for x in o["focus"]]
    meths = {x.get("method") for x in allF if x.get("state") == "map"}
    add("A_cov_overlap_or_progress_maps_seen", bool(meths & ({"overlap", "progress"} | ALIGN_METHODS)), sorted(map(str, meths)))  # phase 3 amendment 3: sentence-map runs log the align family
    add("A_cov_float_scores_seen", any(isnum(x.get("score")) for x in allF if x.get("state") == "map"), "")
    add("A_cov_runner_up_float_seen", any(isnum(x.get("runner_up")) for x in allF if x.get("state") == "map"), "")
    tags = [x for x in maps(S["tags"][0]) if str(x.get("method")).startswith("tag")]
    if tags: add("A_tag_method_scores_null", all(x.get("score") is None and x.get("runner_up") is None for x in tags), [(x.get("method"), x.get("score")) for x in tags][:3])
    else: skip("A_tag_method_scores_null", f"no tag-method map records with SPEAKEASY_FOCUS_TAGS=on (methods {sorted(map(str, {x.get('method') for x in maps(S['tags'][0])}))})")
    kinds = {(x["doc"].get("kind"), x["doc"].get("target_src")) for o in ALL for x in docshows(o)}
    add("A_cov_doc_kinds_seen", {k for k, _ in kinds} >= {"figure", "table", "table_row", "line", "panel", "object"}, sorted(map(str, kinds)))
    # ---- acceptance on the Fig. 5 fixture (measured runs)
    for nm in ("fig5_cap", "fig5_t1", "fig5_prose"):
        o = S[nm][-1]; ms = maps(o); sh = shows(o)
        add(f"A_acc_{nm}_map_and_show_have_sent", ms and sh and all(isint(x.get("sent")) for x in ms + sh), [(x.get("state"), x.get("sent")) for x in ms + sh][:6])
        add(f"A_acc_{nm}_map_sent_0_to_k_minus_1", [x.get("sent") for x in ms] == list(range(len(ms))) and ms, [x.get("sent") for x in ms])
        bad = [x["doc"].get("kind") for x in docshows(o) if x["doc"].get("target") is not None and "target_src" not in x["doc"]]
        add(f"A_acc_{nm}_every_target_show_has_target_src", docshows(o) and not bad, bad)
        r = o["raw"] or {}
        add(f"A_acc_{nm}_runs_first_focus_doc_ready_doc_parse", isint(r.get("first_focus_ms")) and isint(r.get("doc_ready_ms")) and isinstance(r.get("doc_parse"), dict), {k: r.get(k) for k in ("first_focus_ms", "doc_ready_ms")})
        pc = parse_cols(fx("fig5.pdf")).get(1)
        add(f"A_acc_{nm}_doc_parse_columns_equals_parse_decision", (r.get("doc_parse") or {}).get("columns") == pc and pc in (1, 2), ((r.get("doc_parse") or {}).get("columns"), pc))
    t1 = [x["doc"] for o in S["fig5_t1"] for x in docshows(o) if str(x["doc"].get("label", "")).startswith("Table 1") and x["doc"].get("kind") in ("table", "table_row")]
    add("A_acc_fig5_table1_shows_seen", bool(t1), len(t1))
    pf = parse_fig(fx("fig5.pdf"), 1, "Table 1") or {}
    add("A_acc_fig5_table1_split_from_iff_parse_entry", t1 and all(d.get("split_from") == pf.get("split_from") for d in t1) and all(("split_from" in d) == ("split_from" in pf) for d in t1), {"parse": pf.get("split_from"), "shows": [(d.get("kind"), d.get("split_from")) for d in t1][:4]})
    add("A_acc_fig5_table_target_src_table_prefix", all(str(d.get("target_src")).startswith("table:") for d in t1 if d.get("kind") == "table") and any(d.get("kind") == "table" for d in t1), [d.get("target_src") for d in t1][:4])
    tr = [d for d in t1 if d.get("kind") == "table_row"]
    if tr: add("A_acc_fig5_table_row_target_src_table_rows", all(d.get("target_src") == "table-rows" for d in tr), [d.get("target_src") for d in tr])
    else: skip("A_acc_fig5_table_row_target_src_table_rows", "no table_row show in the Table 1 runs")
    # twocol: Table 2 split from Figure 3 (parse says so) carries split_from; Table 4 (standalone) does not
    t2 = [x["doc"] for o in S["two_t2"] for x in docshows(o) if str(x["doc"].get("label", "")).startswith("Table 2")]
    cf = cache_fig(S["two_t2"][-1]["home"], 1, "Table 2")
    if t2 and cf is not None: add("A_twocol_table2_split_from_matches_parse", all(d.get("split_from") == cf.get("split_from") for d in t2) and cf.get("split_from") == "Figure 3", [(d.get("split_from"), cf.get("split_from")) for d in t2][:3])
    else: skip("A_twocol_table2_split_from_matches_parse", f"no Table 2 show ({len(t2)}) or no cache entry")
    t4 = [x["doc"] for o in S["two_t4"] for x in docshows(o) if str(x["doc"].get("label", "")).startswith("Table 4")]
    if t4: add("A_twocol_standalone_table4_has_no_split_from", all("split_from" not in d for d in t4), [d.get("split_from") for d in t4][:3])
    else: skip("A_twocol_standalone_table4_has_no_split_from", "no Table 4 show")
    pg = [x["doc"] for o in S["paper_fig4"] for x in docshows(o) if x["doc"].get("kind") == "panel"]
    cur_paper = parse(fx("paper.pdf"))[1] or {}
    pgd = [pp.get("guessed") for f in ((cur_paper.get("pages") or [{}, {}])[1].get("figures") or []) if f.get("label") == "Figure 4" for pp in f.get("panels") or []]
    psrc = pgd; want_ts = "panel-guessed" if any(pgd) else "panel-labels"     # Phase 5 2.1: a gutter split is not guessed; only caption guesses are
    if pg: add("A_paper_fig4_panels_target_src_matches_panel_src", all(d.get("target_src") == want_ts for d in pg), ([d.get("target_src") for d in pg], sorted(map(str, psrc))))
    else: skip("A_paper_fig4_guessed_panels_target_src_panel_guessed", "no panel show for Figure 4 (guessed panels)")
    pl = [x["doc"] for o in S["fig5_cap"] for x in docshows(o) if x["doc"].get("kind") == "panel"]
    if pl: add("A_fig5_drawn_panel_labels_target_src_panel_labels", all(d.get("target_src") == "panel-labels" for d in pl), [d.get("target_src") for d in pl])
    else: skip("A_fig5_drawn_panel_labels_target_src_panel_labels", "no panel show in the Fig. 5 caption runs")
    gr = [x["doc"] for o in S["p_acc"] for x in docshows(o) if "(grounded)" in str(x["doc"].get("label"))]
    if gr: add("A_grounded_pick_target_src_pick", all(d.get("target_src") == "pick" for d in gr), [d.get("target_src") for d in gr])
    else: skip("A_grounded_pick_target_src_pick", "no applied pick in the p_acc runs")
    add("A_no_vlm_target_src_anywhere", not any(x["doc"].get("target_src") == "vlm" for o in ALL for x in docshows(o)), "")
    # live focus file carries the same keys
    live = [x for o in ALL for x in o["live"] if x.get("state") == "show"]
    add("A_live_show_has_sent", live and all(isint(x.get("sent")) for x in live), [x.get("sent") for x in live][:6])
    ld = [x["doc"] for x in live if isinstance(x.get("doc"), dict)]
    add("A_live_doc_target_has_target_src", ld and all(("target_src" in d) == (d.get("target") is not None) for d in ld), [(d.get("kind"), d.get("target_src")) for d in ld if ("target_src" in d) != (d.get("target") is not None)][:3])
    add("A_live_target_src_enum", all(target_src_ok(d["target_src"]) for d in ld if "target_src" in d), [d.get("target_src") for d in ld if "target_src" in d and not target_src_ok(d["target_src"])][:3])
    lt1 = [x["doc"] for o in S["fig5_t1"] for x in o["live"] if isinstance(x.get("doc"), dict) and str(x["doc"].get("label", "")).startswith("Table 1") and x["doc"].get("kind") in ("table", "table_row")]
    if lt1: add("A_live_table1_split_from_iff_parse_entry", all(d.get("split_from") == pf.get("split_from") and ("split_from" in d) == ("split_from" in pf) for d in lt1), [d.get("split_from") for d in lt1][:3])
    else: skip("A_live_table1_split_from_iff_parse_entry", "no Table 1 snapshot caught in the live file")
    lsent = []
    for o in ALL:
        ms = {x.get("sent") for x in maps(o)}
        lsent += [(o["name"], x.get("sent")) for x in o["live"] if x.get("state") == "show" and ms and x.get("sent") not in ms]
    add("A_live_sent_matches_run_map_sents", not lsent, lsent[:4])
    lc = [x for o in ALL for x in o["live"] if x.get("state") == "clear" and x.get("hold") is not True and "sent" in x]
    add("A_live_plain_clear_has_no_sent", not lc, lc[:2])

@section("C")
def runs_checks():
    S = scenarios(); agg = {}
    for o in ALL:
        for k, viol in check_run(o).items():
            agg.setdefault(k, {"n": 0, "bad": []}); agg[k]["n"] += 1
            if viol: agg[k]["bad"].append((o["name"], viol[:2]))
    for k, a in sorted(agg.items()): add(f"C_{k}", not a["bad"], a["bad"][:3])
    def rec(nm, i=-1): return S[nm][i]["raw"] or {}
    # expectations per scenario
    for nm in ("nodoc_speak", "nodoc_dry", "rewrite", "sel_none_dry", "fallback"):
        add(f"C_{nm}_doc_ready_ms_null_without_doc_job", "doc_ready_ms" in rec(nm) and rec(nm).get("doc_ready_ms") is None, rec(nm).get("doc_ready_ms", "<absent>"))
        add(f"C_{nm}_no_doc_parse_without_page", "doc_parse" not in rec(nm), "")
    add("C_docread_off_doc_ready_ms_null", "doc_ready_ms" in rec("fig5_docread_off") and rec("fig5_docread_off").get("doc_ready_ms") is None, rec("fig5_docread_off").get("doc_ready_ms", "<absent>"))
    add("C_docread_off_no_doc_parse", "doc_parse" not in rec("fig5_docread_off") and rec("fig5_docread_off").get("page") is None, rec("fig5_docread_off").get("page"))
    for i in (0, 1):
        add(f"C_corrupt_pdf_run{i}_doc_job_ran_doc_ready_ms_int", isint(rec("corrupt", i).get("doc_ready_ms")), {k: rec("corrupt", i).get(k) for k in ("doc_ready_ms", "doc_source", "page", "doc_note")})
        add(f"C_corrupt_pdf_run{i}_no_doc_parse", "doc_parse" not in rec("corrupt", i) and rec("corrupt", i).get("page") is None, rec("corrupt", i).get("page"))
        add(f"C_corrupt_pdf_run{i}_exit0", S["corrupt"][i]["rc"] == 0, S["corrupt"][i]["stderr"][-150:])
    add("C_markdown_no_doc_parse", all("doc_parse" not in o["raw"] for o in S["markdown"] if o["raw"]), [o["raw"].get("page") for o in S["markdown"] if o["raw"]])
    add("C_markdown_doc_ready_ms_int", any(isint((o["raw"] or {}).get("doc_ready_ms")) for o in S["markdown"]), [(o["raw"] or {}).get("doc_ready_ms") for o in S["markdown"]])
    ff = rec("fig5_focus_off")
    if not shows(S["fig5_focus_off"][-1]): add("C_focus_off_first_focus_ms_null", "first_focus_ms" in ff and ff.get("first_focus_ms") is None, ff.get("first_focus_ms", "<absent>"))
    else: skip("C_focus_off_first_focus_ms_null", "SPEAKEASY_FOCUS=off still wrote show records")
    dr = rec("fig5_dry")
    add("C_dry_run_first_focus_ms_present", "first_focus_ms" in dr, "")
    add("C_dry_run_first_focus_consistent_with_focus_log", (dr.get("first_focus_ms") is None) == (not shows(S["fig5_dry"][-1])), (dr.get("first_focus_ms"), len(shows(S["fig5_dry"][-1]))))
    add("C_selection_command_has_first_focus_ms_and_doc_ready_ms", "first_focus_ms" in rec("fig5_sel_cmd") and "doc_ready_ms" in rec("fig5_sel_cmd"), rec("fig5_sel_cmd").get("command"))
    add("C_rewrite_doc_ready_ms_present_first_focus_absent", "doc_ready_ms" in rec("rewrite") and "first_focus_ms" not in rec("rewrite"), sorted(NEW_RUN_KEYS & set(rec("rewrite"))))
    # warm-cache measured run: doc ready before the first sentence (bench race check; up to 3 tries like the bench)
    ok = False; seen = []
    for nm in ("fig5_cap", "fig5_t1", "fig5_prose", "two_prose"):
        r = rec(nm); seen.append((nm, r.get("doc_ready_ms"), r.get("first_sentence_ms"), r.get("doc_cache")))
        if r.get("doc_cache") == "hit" and isint(r.get("doc_ready_ms")) and isnum(r.get("first_sentence_ms")) and r["doc_ready_ms"] <= r["first_sentence_ms"]: ok = True
    add("C_warm_cache_doc_ready_before_first_sentence", ok, seen)
    nm_cold = [(o["name"], (o["raw"] or {}).get("doc_cache"), (o["raw"] or {}).get("doc_ready_ms")) for o in ALL if (o["raw"] or {}).get("doc_cache") == "miss"]
    add("C_cold_parse_doc_ready_ms_int", nm_cold and all(isint(x[2]) for x in nm_cold), nm_cold[:4])
    # Fig. 6 raster page 2: doc_parse describes page 2 with a label-less raster figure
    dp = rec("multi_fig6").get("doc_parse") or {}
    if rec("multi_fig6").get("page") == 2: add("C_multipage_page2_doc_parse_labels_src_none", (dp.get("labels_src") or {}).get("none") == 1 and dp.get("columns") == 1, dp)
    else: skip("C_multipage_page2_doc_parse_labels_src_none", f"page matched {rec('multi_fig6').get('page')}")
    dp = rec("two_t2").get("doc_parse") or {}
    rc_, pd_, _ = parse(fx(FX["twocol"])); pg1 = (pd_ or {"pages": [{}]})["pages"][0]; figs1 = pg1.get("figures") or []
    want = {"split": sum(1 for f in figs1 if f.get("split_from")), "table": sum(1 for f in figs1 if f.get("kind") == "table"), "figure": sum(1 for f in figs1 if f.get("kind") != "table"), "columns": pg1.get("columns")}
    got = {"split": dp.get("split"), "table": (dp.get("blocks") or {}).get("table"), "figure": (dp.get("blocks") or {}).get("figure"), "columns": dp.get("columns")}
    add("C_twocol_doc_parse_reflects_parse_pdf", got == want and want["split"] >= 1, {"doc_parse": got, "parse": want})
    cols_bad, n_cols = [], 0
    for o in ALL:
        f = fixture_of(o["name"])
        if f and o["raw"] and isinstance(o["raw"].get("doc_parse"), dict):
            n_cols += 1; want_c = parse_cols(fx(f)).get(o["raw"].get("page"))
            if o["raw"]["doc_parse"].get("columns") != want_c: cols_bad.append((o["name"], o["raw"]["doc_parse"].get("columns"), want_c))
    add("C_doc_parse_columns_equal_parse_pdf_decision", n_cols >= 10 and not cols_bad, (n_cols, cols_bad[:4]))
    # other commands: stop / stats / last never carry the new run keys
    h = S["nodoc_speak"][0]["home"]
    for cmd in (["stop"], ["stats", "--json"], ["last", "--json"]):
        n0 = len(runs(h)); p = subprocess.run([CUR] + cmd, capture_output=True, text=True, env=env_for(h, BAD), timeout=60); new = runs(h)[n0:]
        add(f"C_{cmd[0]}_records_no_new_run_keys", not any(NEW_RUN_KEYS & set(x) for x in new), [sorted(NEW_RUN_KEYS & set(x)) for x in new])
        if cmd[0] == "last": add("C_last_json_runs", p.returncode == 0, p.stderr[-120:])

@section("G")
def grounding_checks():
    S = scenarios()  # noqa
    def m(nm): return S[nm][-1]
    def gm(o): return (o["raw"] or {}).get("ground_miss_reasons"), ((o["raw"] or {}).get("focus_methods") or {})
    # Phase 5 letter picker (the bbox_2d answers of Phase 2 are retired, phase5-parts.md A2.9 / A3.5)
    def picks(o): return [x for x in o["focus"] if x.get("state") == "part_pick"]
    def pp(o): return (o["raw"] or {}).get("part_picks") or {}
    o = m("p_offmenu"); g, fm = gm(o)
    add("G_cov_offmenu_made_pick_requests", len(o["ground_posts"]) >= 1, len(o["ground_posts"]))
    add("G_offmenu_reason_equals_ground_miss", isinstance(g, dict) and fm.get("ground-miss", 0) >= 1 and g.get("off-menu") == fm.get("ground-miss"), (g, fm))
    add("G_offmenu_no_other_miss_reasons", isinstance(g, dict) and not (set(g) & (set(MISS_REASONS) - {"off-menu"})), g)
    o = m("p_low"); g, fm = gm(o)
    add("G_low_confidence_reason_equals_ground_miss", isinstance(g, dict) and fm.get("ground-miss", 0) >= 1 and g.get("low-confidence") == fm.get("ground-miss"), (g, fm))
    o = m("p_nolp"); g, fm = gm(o)
    add("G_no_logprobs_reason_and_breaker", isinstance(g, dict) and g.get("no-logprobs", 0) >= 1 and breaker_local(pp(o)) == "no-logprobs", (g, pp(o).get("breaker")))
    add("G_no_logprobs_breaker_one_request", len(o["ground_posts"]) == 1, len(o["ground_posts"]))
    o = m("p_http"); g, fm = gm(o); ge = (o["raw"] or {}).get("ground_errors")
    add("G_http_500_unavailable_and_breaker", isinstance(g, dict) and g.get("unavailable", 0) >= 1 and breaker_local(pp(o)) == "unavailable" and len(o["ground_posts"]) == 1, (g, pp(o).get("breaker"), len(o["ground_posts"])))
    add("G_http_500_sum_invariant", isinstance(g, dict) and sum(g.get(r, 0) for r in MISS_REASONS) == fm.get("ground-miss", 0), (g, fm))
    o = m("p_malformed"); g, fm = gm(o); ge = (o["raw"] or {}).get("ground_errors")
    add("G_malformed_reason_error_counted_and_le_ground_errors_count", isinstance(g, dict) and isinstance(ge, dict) and 1 <= g.get("error", 0) <= ge.get("count", 0), (g, ge))
    o = m("p_acc"); g, fm = gm(o); P_ = picks(o)
    add("G_cov_accepted_pick", any(x.get("outcome") == "accepted" for x in P_), [x.get("outcome") for x in P_])
    add("G_accepted_ground_pick_equals_applied", fm.get("ground-pick", 0) == sum(1 for x in P_ if x.get("applied")), (fm, [x.get("applied") for x in P_]))
    if fm.get("ground-pick") and not fm.get("ground-miss"): add("G_accepted_answers_only_uncounted_reasons", g is None or set(g) <= {"timeout", "superseded", "no-fit"}, g)
    else: skip("G_accepted_answers_only_uncounted_reasons", f"p_acc focus_methods {fm}")
    add("G_accepted_ground_provider_local", (o["raw"] or {}).get("ground_provider") == "local", (o["raw"] or {}).get("ground_provider"))
    o = m("p_off"); add("G_ground_off_no_requests_no_reasons", not o["ground_posts"] and "ground_miss_reasons" not in (o["raw"] or {}) and not picks(o), (len(o["ground_posts"]), (o["raw"] or {}).get("ground_miss_reasons")))
    o = m("p_default"); add("G_picker_default_off_no_requests", not o["ground_posts"] and not picks(o), (len(o["ground_posts"]), pp(o).get("skipped")))
    o = m("p_slow"); g, fm = gm(o)
    if o["ground_posts"]:
        add("G_slow_answer_reported_timeout_or_superseded", isinstance(g, dict) and set(g) <= {"timeout", "superseded"} and sum(g.values()) >= 1, (g, fm))
        add("G_slow_answer_not_a_ground_miss", fm.get("ground-miss", 0) == 0, fm)
    else: skip("G_slow_answer_reported_timeout_or_superseded", f"posts={len(o['ground_posts'])} methods={fm}")
    for nm in ("p_acc", "p_offmenu", "p_low", "p_nolp", "p_http", "p_malformed", "p_slow", "p_logtext"):
        bad = [(x.get("provider"), x.get("fallback_from"), x.get("confidence")) for x in picks(m(nm)) if x.get("provider") != "local" or x.get("fallback_from") is not None
               or x.get("confidence") not in (("logprob",) if x.get("outcome") in ("accepted", "low-confidence") else ("logprob", None))]
        add(f"G_{nm}_part_pick_provider_local_fallback_null_confidence", not bad, bad[:3])
        bp = pp(m(nm)).get("by_provider")
        if bp is not None: add(f"G_{nm}_by_provider_local_only", isinstance(bp, dict) and not bp.get("openrouter") and bp.get("local", 0) == len(picks(m(nm))), (bp, len(picks(m(nm)))))
    for nm in ("g_box", "g_nobox", "g_huge", "g_err", "g_slow", "g_late_nobox"):
        o = m(nm); fm = (o["raw"] or {}).get("focus_methods") or {}
        add(f"G_{nm}_retired_bbox_path_not_written", not (RETIRED_RUN_KEYS & set(o["raw"] or {})) and not (RETIRED_METHODS & set(fm)), sorted((RETIRED_RUN_KEYS & set(o["raw"] or {})) | (RETIRED_METHODS & set(fm))))
    if "fit_miss" in S:
        o = m("fit_miss"); g, fm = gm(o)
        add("G_cov_fit_miss_path_taken", fm.get("fit-miss", 0) >= 1 and any(x.get("hold") for x in o["focus"]), (fm, sum(1 for x in o["focus"] if x.get("hold"))))
        if o["ground_posts"]:
            add("G_acc_capture_fit_failure_counted_no_fit", isinstance(g, dict) and g.get("no-fit", 0) >= 1, g)
            add("G_no_fit_requests_not_counted_as_ground_miss", isinstance(g, dict) and sum(g.get(r, 0) for r in MISS_REASONS) == fm.get("ground-miss", 0), (g, fm))
        else: skip("G_acc_capture_fit_failure_counted_no_fit", "no pick on the raster sentinel figure (picker default-off, A5.1)")
        o = m("fit_ok"); g, fm = gm(o)
        add("G_cov_fit_ok_hold_then_fit", any(x.get("hold") for x in o["focus"]) and not fm.get("fit-miss"), (fm, sum(1 for x in o["focus"] if x.get("hold"))))
        add("G_fit_landed_no_no_fit_reason", not (isinstance(g, dict) and g.get("no-fit")), g)
        hc = [x for x in o["focus"] + m("fit_miss")["focus"] if x.get("state") == "clear" and x.get("hold")]
        add("G_hold_clear_records_have_sent", hc and all(isint(x.get("sent")) for x in hc), [x.get("sent") for x in hc][:6])
    else:
        for c in ("G_acc_capture_fit_failure_counted_no_fit", "G_fit_landed_no_no_fit_reason"): skip(c, "tracker (OCR) not buildable: " + str(TRK.get("why")))
    o = m("p_logtext"); g, fm = gm(o)
    add("G_logtext_on_reasons_still_counts_only", isinstance(g, dict) and textfree("runs.ground_miss_reasons", g), g)
    add("G_logtext_on_part_picks_counts_only", textfree("runs.part_picks", (o["raw"] or {}).get("part_picks")), (o["raw"] or {}).get("part_picks"))
    for nm in ("p_offmenu", "p_low", "p_nolp", "p_http", "p_malformed", "g_nobox", "g_huge", "g_err"):
        sh = [x for x in shows(m(nm)) if "(grounded)" in str((x.get("doc") or {}).get("label"))]
        add(f"G_{nm}_no_oval_drawn", not sh, len(sh))

def leak_paths(home):
    """file:event:json-path of every string leaf holding a sentinel word in the home's runs/focus/tracker logs."""
    out = []
    def walk(v, path, acc):
        if isinstance(v, dict):
            for k, x in v.items(): walk(x, f"{path}.{k}", acc)
        elif isinstance(v, list):
            for x in v: walk(x, f"{path}[]", acc)
        elif isinstance(v, str) and sentinel_hits(v): acc.append(path)
    for f in ("runs.jsonl", "focus.jsonl", "tracker.jsonl"):
        for rec in jl(os.path.join(home, "logs", f)):
            acc = []; walk(rec, "", acc)
            out += [f"{f}:{rec.get('state') or rec.get('event') or rec.get('command')}:{p_}" for p_ in acc]
    return sorted(set(out))

@section("P")
def privacy_checks():
    S = scenarios()
    leaks, typebad, ok_runs = [], [], 0
    for o in ALL:
        for k, val in new_field_values(o):
            if sentinel_hits(json.dumps(val)) or not textfree(k, val): typebad.append((o["name"], k, str(val)[:60]))
        if o["logtext"]: continue
        logs = L.all_text_under(os.path.join(o["home"], "logs"))
        hits = {f: sentinel_hits(t) for f, t in logs.items() if f in ("runs.jsonl", "focus.jsonl", "tracker.jsonl") and sentinel_hits(t)}
        if hits: leaks.append((o["name"], leak_paths(o["home"]) or hits))
        else: ok_runs += 1
    add("P_new_fields_hold_no_text_any_mode", not typebad, typebad[:4])
    add("P_default_no_sentinel_in_runs_focus_tracker_logs", not leaks and ok_runs > 5, leaks[:3])
    sent_inputs = [o for o in ALL if o["name"].split("#")[0] in ("two_prose", "two_t2", "two_fig3", "two_t4", "tbl_off", "g_nobox", "g_err", "rewrite", "fallback")]
    add("P_cov_sentinel_scenarios_ran", len(sent_inputs) >= 9 and all(o["rc"] == 0 for o in sent_inputs), [(o["name"], o["rc"]) for o in sent_inputs if o["rc"]][:3])
    # map/show records keep the text-free shape by default
    bad = [(o["name"], x.get("state")) for o in ALL if not o["logtext"] for x in o["focus"] if x.get("state") in ("map", "show") and ("text" in x or "src" in x)]
    add("P_default_focus_records_no_text_or_src", not bad, bad[:3])
    # live file: never text, and its new keys are text-free
    bad = [(o["name"], sorted(x)) for o in ALL for x in o["live"] if "text" in x]
    add("P_live_file_never_text", not bad, bad[:2])
    # log text on: the new fields still carry no text (records may hold text in their own keys)
    lt = [o for o in ALL if o["logtext"]]
    add("P_logtext_runs_ran_with_text", lt and any("text" in x for o in lt for x in o["focus"]), len(lt))
    add("P_logtext_new_fields_text_free", lt and all(textfree(k, v) and not sentinel_hits(json.dumps(v)) for o in lt for k, v in new_field_values(o)), "")
    # doc_parse holds counts only
    dps = [(o["name"], o["raw"]["doc_parse"]) for o in ALL if o["raw"] and isinstance(o["raw"].get("doc_parse"), dict)]
    add("P_doc_parse_values_counts_and_bools_only", dps and all(textfree("runs.doc_parse", d) for _, d in dps), [d for _, d in dps if not textfree("runs.doc_parse", d)][:2])
    # privacy_audit.py still runs on a home with a kept capture
    o = S["g_nobox"][-1]; img = os.path.join(o["home"], "last_capture.png")
    img = img if os.path.exists(img) else fx(FX["sentfig_png"])
    p = subprocess.run([PY, os.path.join(REPO, "tests", "audit", "privacy_audit.py"), "--image", img, "--log", os.path.join(o["home"], "logs", "runs.jsonl"), "--thumb", os.path.join(TMPROOT, "thumb.png")], capture_output=True, text=True, env=env_for(o["home"]), timeout=120)
    try: pa = json.loads(p.stdout)
    except Exception: pa = {}
    add("P_privacy_audit_runs_on_phase2_logs", p.returncode == 0 and pa and pa.get("error") is None, (p.returncode, pa.get("error"), p.stderr[-150:]))
    # pre-existing vs new leaks: the same sentinel figure read through the baseline CLI
    tc = fx(FX["twocol"]); b = bench_case("xp_fig3", tc, 1, [find_line(tc, 1, "Figure 3 |"), find_line(tc, 1, "a Dunmarrow")], "Figure 3 shows the plumbrine vexhollow pipeline. In panel a, the Zorblax unit captures dunmarrow data. Panel b shows grelquist scoring. The Skelvane box scores frumplet events.", bin_=BIN_BASE)
    bl, cl = set(leak_paths(b[-1]["home"])), set(leak_paths(S["two_fig3"][-1]["home"]))
    add("P_no_sentinel_leak_path_beyond_baseline", not (cl - bl), {"new": sorted(cl - bl), "baseline_also": sorted(bl)})

# ================================================================ F. labels carry no document text by default
def labels(o): return [x["doc"].get("label") for x in shows(o) if isinstance(x.get("doc"), dict) and "label" in x["doc"]]
@section("F")
def label_privacy():
    S = scenarios()
    bf, bl, br, nf, nl = [], [], [], 0, 0
    for o in ALL:
        if o["logtext"]: continue
        for lab in labels(o):
            nf += 1
            if not f_label_ok(lab): bf.append((o["name"], len(str(lab)), sentinel_hits(str(lab))))
        for x in o["live"]:
            if x.get("state") == "show" and isinstance(x.get("doc"), dict) and "label" in x["doc"]:
                nl += 1
                if not f_label_ok(x["doc"]["label"]): bl.append((o["name"], len(str(x["doc"]["label"]))))
        def walk(v, path):
            if isinstance(v, dict):
                for k, y in v.items(): walk(y, f"{path}.{k}")
            elif isinstance(v, list):
                for y in v: walk(y, path + "[]")
            elif isinstance(v, str) and re.match(r"^(Fig\.?|Figure|Table)\s*\d", v) and not f_label_ok(v): br.append((o["name"], path))
        for rec in o["recs"]: walk(rec, "")
    add("F_default_focus_log_labels_match_pattern_or_null", nf > 20 and not bf, (nf, bf[:4]))
    add("F_default_live_file_labels_match_pattern_or_null", nl > 5 and not bl, (nl, bl[:4]))
    add("F_default_runs_label_fields_match_pattern", not br, br[:4])
    d3 = S["two_fig3"][-1]; t3 = S["two_fig3_true"][-1]; o3 = S["two_fig3_on"][-1]
    add("F_cov_part_ovals_in_default_run", any(x["doc"].get("kind") == "object" for x in docshows(d3)), [x["doc"].get("kind") for x in docshows(d3)])
    add("F_logtext_true_not_exactly_1_is_redacted", labels(t3) and all(f_label_ok(l) for l in labels(t3)) and not sentinel_hits(json.dumps(labels(t3))), [len(str(l)) for l in labels(t3)])
    add("F_logtext_true_labels_equal_default", labels(t3) == labels(d3), "")
    add("F_cov_logtext_1_labels_keep_part_text", any(isinstance(l, str) and ": " in l for l in labels(o3)), [str(l)[:12] for l in labels(o3)])
    add("F_default_labels_are_redacted_logtext_labels", labels(d3) == [p2_redact(l) for l in labels(o3)] and labels(d3), (labels(d3)[:4], [p2_redact(l) for l in labels(o3)][:4]))
    geo = lambda o: [(x["doc"].get("kind"), x["doc"].get("target"), x.get("shape"), x.get("cue_kind"), x.get("rect")) for x in docshows(o)]
    add("F_target_kind_shape_rect_unchanged_by_redaction", geo(d3) == geo(o3) and geo(d3), "")
    gl = [l for nm in ("p_acc", "g_box", "fit_ok") if nm in S for o in S[nm] for l in labels(o) if l and "grounded" in l]
    add("F_grounded_label_suffix_form", gl and all(f_label_ok(l) and l.endswith(" (grounded)") for l in gl), gl[:3])
    rl = [l for o in S["fig5_t1"] + S["two_t2"] for l in labels(o) if l and " row " in l]
    if rl: add("F_table_row_label_form", all(f_label_ok(l) for l in rl), rl[:3])
    else: skip("F_table_row_label_form", "no table-row label seen")
    # against the baseline CLI: LOG_TEXT=1 labels are as today; default labels are today's labels redacted
    tc = fx(FX["twocol"]); f3 = [find_line(tc, 1, "Figure 3 |"), find_line(tc, 1, "a Dunmarrow")]
    F3 = "Figure 3 shows the plumbrine vexhollow pipeline. In panel a, the Zorblax unit captures dunmarrow data. Panel b shows grelquist scoring. The Skelvane box scores frumplet events."
    bd = bench_case("xf_fig3", tc, 1, f3, F3, bin_=BIN_BASE)[-1]; bo = bench_case("xf_fig3_on", tc, 1, f3, F3, bin_=BIN_BASE, SPEAKEASY_LOG_TEXT="1")[-1]
    add("F_logtext_1_labels_equal_baseline", labels(o3) == labels(bo) and labels(bo), (labels(o3)[:3], labels(bo)[:3]))
    add("F_default_labels_equal_baseline_redacted", labels(d3) == [p2_redact(l) for l in labels(bd)] and labels(bd), (labels(d3)[:3], [p2_redact(l) for l in labels(bd)][:3]))
    add("F_cov_baseline_default_leaked_part_text", any(sentinel_hits(str(l)) for l in labels(bd)), "")
    # privacy_audit.py label check on real homes
    for nm, o, want_text in (("default", d3, False), ("logtext1", o3, True)):
        p = subprocess.run([PY, os.path.join(REPO, "tests", "audit", "privacy_audit.py"), "--image", fx(FX["sentfig_png"]), "--log", os.path.join(o["home"], "logs", "runs.jsonl"), "--thumb", os.path.join(TMPROOT, "thumb.png")],
                           capture_output=True, text=True, env=env_for(o["home"], SPEAKEASY_LOG_TEXT="1" if want_text else None), timeout=120)
        try: pa = json.loads(p.stdout)
        except Exception: pa = {}
        la = pa.get("label_audit") or {}
        add(f"F_privacy_audit_{nm}_label_audit_clean", la.get("violations") == 0 and "PRIVACY_LEAK" not in pa and la.get("text_mode") == want_text and (want_text or la.get("labels_checked", 0) > 0), (p.returncode, la, p.stderr[-150:]))

# ================================================================ X. additive only vs baseline (same inputs, baseline CLI vs current)
@section("X")
def additive():
    f5 = fx("fig5.pdf"); t1 = find_line(f5, 1, "Table 1"); cap = find_line(f5, 1, "Fig. 5")
    pairs = {
        "fig5_t1": lambda b: bench_case("x_t1", f5, 1, [t1], "Table 1 lists the system specifications. The battery life is 44 hours. The ADC resolution is 15 bits.", bin_=b),
        "fig5_cap": lambda b: bench_case("x_cap", f5, 1, [cap, cap + 1], "Figure 5 shows the drowsiness detector pipeline. In panel a, ear recordings are re-referenced and filtered. Panel b shows cross-validation.", bin_=b),
        "sentfig": lambda b: sentfig_case("x_sf", bin_=b, SPEAKEASY_GROUND="local"),
    }
    ALLOW = {"runs": NEW_RUN_KEYS | P3_RUN | P4_RUN | P5_RUN, "map": {"sent", "score", "runner_up"} | P3_MAP, "show": {"sent"} | P3_SHOW, "clear": {"sent"}, "doc": {"target_src", "split_from"}}
    for nm, fn in pairs.items():
        b, c = fn(BIN_BASE), fn(CUR)
        def keys(runs_, st):
            if st == "runs": return set().union(*[set(o["raw"] or {}) for o in runs_])
            if st == "doc": return set().union(*[set(x["doc"]) for o in runs_ for x in docshows(o)] or [set()])
            return set().union(*[set(x) for o in runs_ for x in o["focus"] if x.get("state") == st] or [set()])
        for st in ("runs", "map", "show", "clear", "doc"):
            bk, ck = keys(b, st), keys(c, st); ckm = keys(c[-1:], st); bkm = keys(b[-1:], st)
            missing = sorted(bkm - ck); extra = sorted(ckm - bk - ALLOW[st])
            if st == "runs": missing = sorted(set(missing) - RETIRED_RUN_KEYS - {"ground_ms", "ground_provider"})   # bbox_2d path retired (A2.9); ground_provider only after an answered pick
            add(f"X_{nm}_{st}_keeps_every_baseline_key", not missing, missing)
            add(f"X_{nm}_{st}_adds_only_contract_keys", not extra, extra)
        # types of shared keys unchanged (runs)
        tb = {k: type(v).__name__ for o in b for k, v in (o["raw"] or {}).items() if v is not None}; tc = {k: type(v).__name__ for o in c for k, v in (o["raw"] or {}).items() if v is not None}
        tdiff = {k: (tb[k], tc[k]) for k in tb if k in tc and tb[k] != tc[k] and {tb[k], tc[k]} != {"int", "float"}}
        add(f"X_{nm}_runs_shared_key_types_unchanged", not tdiff, tdiff)
        # same targets for the same inputs (warm measured run)
        tg = lambda o, red: [(x["doc"].get("kind"), red(x["doc"].get("label")), x["doc"].get("target")) for x in docshows(o) if "(grounded)" not in str(x["doc"].get("label"))]
        tb_, tc_ = tg(b[-1], p2_redact), tg(c[-1], lambda l: l)
        same = tb_ == tc_
        if not same and nm == "fig5_t1" and tb_:
            # phase4-layout.md 4.4: the table target loses its caption and stops at the gutter; reading is otherwise unchanged
            # (same kinds, labels and order: table, then one table_row band per row). Row bands keep their y extent.
            t1 = parse_fig(f5, 1, "Table 1") or {}
            same = [(k, l) for k, l, _ in tb_] == [(k, l) for k, l, _ in tc_] and all(
                (k == "table" and t == t1.get("bbox")) or (k == "table_row" and isinstance(t, list) and isinstance(bt, list) and abs(t[1] - bt[1]) <= 0.15 and abs(t[3] - bt[3]) <= 0.15
                 and isinstance(t1.get("bbox"), list) and t[0] >= t1["bbox"][0] - 2 and t[2] <= t1["bbox"][2] + 2) or (k not in ("table", "table_row") and t == bt)
                for (k, l, bt), (_, _, t) in zip(tb_, tc_))
        add(f"X_{nm}_same_doc_targets_as_baseline_labels_redacted", same and bool(tb_), (tb_[:2], tc_[:2]))
        meth = lambda o: [x.get("method") for x in maps(o)]
        add(f"X_{nm}_same_map_methods_as_baseline", len(meth(b[-1])) == len(meth(c[-1])) and all(p3_method(y, x) for x, y in zip(meth(b[-1]), meth(c[-1]))), (meth(b[-1]), meth(c[-1])))
        stdout = lambda o: o["stdout"]
        add(f"X_{nm}_same_stdout_rc_as_baseline", (stdout(b[-1]), b[-1]["rc"]) == (stdout(c[-1]), c[-1]["rc"]), (b[-1]["rc"], c[-1]["rc"]))

# ================================================================ S. SCHEMA.md documents the new runs keys
@section("S")
def schema_doc():
    sc = open(os.path.join(REPO, "tests", "audit", "SCHEMA.md")).read()
    opt = sc.split("Optional keys", 1)[1] if "Optional keys" in sc else ""
    rows = {}
    for l in opt.splitlines():
        c = [x.strip() for x in l.strip().strip("|").split("|")]
        if len(c) >= 4: rows[c[0].strip("` ")] = c
    for k, ty in (("first_focus_ms", "int"), ("doc_ready_ms", "int"), ("doc_parse", "dict"), ("ground_miss_reasons", "dict")):
        add(f"S_schema_optional_table_has_{k}", k in rows, sorted(rows)[:10])
        if k in rows: add(f"S_schema_{k}_type_column", ty in rows[k][1] or (ty == "dict" and "object" in rows[k][1]), rows[k][1])
    gr = rows.get("ground_miss_reasons", [""] * 4)
    add("S_schema_ground_miss_reasons_lists_reasons", all(x in " ".join(gr) for x in ("error", "timeout", "superseded", "low-confidence", "off-menu", "no-logprobs", "unavailable", "out-of-scope")), gr[-1][:160] if gr else "")
    add("S_schema_has_part_picks_row", "part_picks" in rows, "")

# ================================================================ E. tracker draw records (headless: invalid window id, real panel)
@section("E")
def tracker():
    if A.no_tracker: skip("E_tracker", "--no-tracker"); return
    exe = tracker_exe()
    if not exe: skip("E_tracker", TRK.get("why")); return
    T = os.path.dirname(TRK["pkg"]); pkg = TRK["pkg"]
    p = subprocess.run(["swift", "test"], cwd=pkg, capture_output=True, text=True, timeout=900)
    mt = re.findall(r"Executed (\d+) tests?, with (\d+) failures", p.stdout + p.stderr); mx = max(mt, key=lambda x: int(x[0])) if mt else None
    add("E_tracker_xctests_10_pass", mx and int(mx[0]) >= 10 and mx[1] == "0" and p.returncode == 0, (p.returncode, mx if mx else (p.stdout + p.stderr)[-300:]))
    pf = os.path.join(T, "pf.swift"); open(pf, "w").write('import CoreGraphics; import ApplicationServices\nprint("\\(CGPreflightScreenCaptureAccess())")\n')
    subprocess.run(["swiftc", "-O", pf, "-o", os.path.join(T, "pf")], capture_output=True, timeout=300)
    pre = subprocess.run([os.path.join(T, "pf")], capture_output=True, text=True).stdout.strip() if os.path.exists(os.path.join(T, "pf")) else "false"
    if pre != "true": skip("E_tracker_draws", "Screen Recording not granted to this process"); return
    S, OX, OY = 1.5, 100.0, 120.0; TGT = [40.7, 77.4, 352.4, 85.8]
    scr = [OX + TGT[0] * S, OY + TGT[1] * S, (TGT[2] - TGT[0]) * S, (TGT[3] - TGT[1]) * S]
    def scenario(tag, show):
        h = os.path.join(T, f"home_{tag}"); os.makedirs(os.path.join(h, "logs"))
        parent = subprocess.Popen(["sleep", "4"]); open(os.path.join(h, "speak.pid"), "w").write(str(parent.pid))
        show = dict(show, pid=parent.pid, ts=time.time()); json.dump(show, open(os.path.join(h, "focus.json"), "w"))
        tp = subprocess.Popen([exe, "--window-id", "999999", "--pid", str(parent.pid), "--home", h, "--fps", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try: tp.wait(20)
        except subprocess.TimeoutExpired: tp.send_signal(signal.SIGTERM); tp.wait(10)
        parent.wait(); return [x for x in jl(os.path.join(h, "logs", "tracker.jsonl")) if x.get("event") == "draw"]
    base = {"state": "show", "seq": 1, "rect": [round(v, 1) for v in scr], "color": "#3B82F6", "opacity": 0.22, "src": ["Wexmoor zorblax line"], "range": None, "nlines": 1,
            "cue": "text", "cue_kind": "text_band", "draw": True, "sent": 0}
    withtf = dict(base, doc={"kind": "line", "label": None, "page": 1, "page_size": [595.3, 790.9], "target": TGT, "target_src": "lines", "anchors": [[40.7, 53.4, 352.4, 61.8, "Prose line 1"]]}, doc_transform=[S, OX, OY])
    D1 = scenario("tf", withtf); D2 = scenario("plain", base)
    if not D1 and not D2: skip("E_tracker_draws", "tracker wrote no draw records headless"); return
    for tag, D in (("doc_transform", D1), ("no_doc", D2)):
        if not D: skip(f"E_{tag}_draws", "no draw records"); continue
        add(f"E_{tag}_every_draw_has_page_rect_and_page_fit", all("page_rect" in x and "page_fit" in x for x in D), [sorted(x) for x in D][:1])
        add(f"E_{tag}_page_fit_enum", all(x.get("page_fit") in (None, "anchors", "event-transform") for x in D), [x.get("page_fit") for x in D][:4])
        add(f"E_{tag}_page_rect_null_iff_page_fit_null", all((x.get("page_rect") is None) == (x.get("page_fit") is None) for x in D), [(x.get("page_rect"), x.get("page_fit")) for x in D][:3])
        add(f"E_{tag}_page_rect_shape_rounded", all(x.get("page_rect") is None or (isinstance(x["page_rect"], list) and len(x["page_rect"]) == 4 and all(r1(v) for v in x["page_rect"])) for x in D), [x.get("page_rect") for x in D][:3])
        add(f"E_{tag}_existing_draw_keys_kept", all("rect" in x and "text_rect" in x for x in D), "")
        fd = [x.get("first_draw_src") for x in D if "first_draw_src" in x]
        add(f"E_{tag}_first_draw_src_on_first_draw_enum", bool(fd) and "first_draw_src" in D[0] and all(v in ("page-fit", "capture+offset", "held") for v in fd), [(x.get("seq"), x.get("first_draw_src")) for x in D][:4])
    if D1 and "first_draw_src" in D1[0]: add("E_doc_transform_first_draw_src_page_fit", D1[0].get("first_draw_src") == "page-fit", D1[0].get("first_draw_src"))
    if D2: add("E_no_doc_no_fit_page_rect_null", all(x.get("page_rect") is None and x.get("page_fit") is None for x in D2), [(x.get("page_rect"), x.get("page_fit")) for x in D2][:3])
    ev = [x for x in D1 if x.get("page_fit") == "event-transform"]
    if ev:
        bad = []
        for x in ev:
            tr = x.get("text_rect")
            if isinstance(tr, list) and len(tr) == 4:
                want = [(tr[0] - OX) / S, (tr[1] - OY) / S, (tr[0] + tr[2] - OX) / S, (tr[1] + tr[3] - OY) / S]
                if max(abs(a - b) for a, b in zip(want, x["page_rect"])) > 0.6: bad.append((x["page_rect"], [round(v, 1) for v in want]))
        add("E_event_transform_page_rect_is_text_rect_mapped_back", not bad, bad[:2])
        add("E_event_transform_page_rect_near_target", all(max(abs(a - b) for a, b in zip(x["page_rect"], TGT)) <= 3.0 for x in ev), [x["page_rect"] for x in ev][:2])
    elif D1: add("E_doc_transform_event_gives_event_transform_fit", any(x.get("page_fit") for x in D1), [(x.get("page_fit"), x.get("page_rect")) for x in D1][:3])
    # F: a default-mode live snapshot of a part oval (real CLI output) -> the draw label keeps the redacted form
    S_ = scenarios(); snap = next((x for o in S_["two_fig3"] for x in o["live"] if x.get("state") == "show" and (x.get("doc") or {}).get("kind") == "object"), None)
    if snap:
        snap = dict(snap); snap.pop("ts", None); snap.pop("pid", None); snap["doc_transform"] = snap.get("doc_transform") or [S, OX, OY]
        D3 = scenario("part", snap); labs = [x.get("label") for x in D3 if "label" in x]
        add("E_part_oval_draws_written", bool(D3), len(D3))
        add("E_default_draw_labels_match_F_pattern", all(f_label_ok(l) for l in labs) and not any(sentinel_hits(json.dumps(x)) for x in D3), (len(labs), [len(str(l)) for l in labs][:4]))
        if not labs: skip("E_cov_draw_label_present", "draw records carry no label key")
        else: add("E_cov_draw_label_present", True)
    else: skip("E_default_draw_labels_match_F_pattern", "no default-mode part-oval live snapshot captured")
    add("E_tracker_log_no_sentinel_text", not any(sentinel_hits(json.dumps(x)) for x in D1 + D2), "")

# ================================================================ T. self-test: the validators flag known-bad inputs
@section("T")
def selftest():
    good = {"pages": [{"n": 1, "w": 600, "h": 800, "figs_done": True, "columns": 1, "lines": [[10, 10, 100, 20, "a"], [10, 30, 100, 40, "b"], [10, 300, 90, 310, "c"]],
                       "figures": [{"label": "Figure 1", "kind": "figure", "bbox": [10.0, 100.0, 200.0, 280.0], "caption_bbox": [10.0, 300.0, 90.0, 310.0], "bbox_src": "graphics", "panels": []}],
                       "blocks": [{"kind": "paragraph", "bbox": [10.0, 10.0, 100.0, 40.0], "lines": [0, 1], "src": "lines"}, {"kind": "figure", "bbox": [10.0, 100.0, 200.0, 280.0], "label": "Figure 1", "src": "graphics"},
                                  {"kind": "caption", "bbox": [10.0, 300.0, 90.0, 310.0], "lines": [2], "label": "Figure 1", "src": "regex"}]}]}
    fails = lambda d: {s for s, ok, _ in validate_parse(d, "t") if not ok}
    add("T_validate_parse_accepts_good", not fails(good), fails(good))
    def mut(fn):
        d = json.loads(json.dumps(good)); fn(d["pages"][0]); return fails(d)
    add("T_flags_shared_line", "no_shared_lines" in mut(lambda p: p["blocks"][2]["lines"].append(1)))
    add("T_flags_paragraph_bbox_not_union", "paragraph_bbox_union_of_lines" in mut(lambda p: p["blocks"][0].__setitem__("bbox", [10.0, 10.0, 150.0, 40.0])))
    add("T_flags_figure_bbox_mismatch", "one_block_per_figure" in mut(lambda p: p["blocks"][1].__setitem__("bbox", [10.0, 100.0, 201.0, 280.0])))
    add("T_flags_text_key_in_block", "block_schema_exact_keys" in mut(lambda p: p["blocks"][0].__setitem__("text", "zorblax")))
    add("T_flags_bad_bbox_src", "figures_bbox_src_enum_by_kind" in mut(lambda p: p["figures"][0].__setitem__("bbox_src", "magic")))
    add("T_flags_caption_line_in_paragraph", "caption_lines_only_in_caption_block" in mut(lambda p: (p["blocks"][2]["lines"].remove(2), p["blocks"][0]["lines"].append(2))))
    add("T_flags_reading_order", "reading_order_top_to_bottom_per_column" in mut(lambda p: p["blocks"].reverse()))
    o = {"home": TMPROOT, "focus": [{"state": "map", "sent": 0, "score": 1.5, "runner_up": 0.4, "method": "overlap"}, {"state": "map", "sent": 2, "score": 0.5, "runner_up": 0.9, "method": "progress"},
                                     {"state": "show", "sent": 5}, {"state": "clear"}, {"state": "clear", "sent": 1}]}
    v = check_focus(o, o["focus"])
    add("T_flags_sent_gap", bool(v["map_sent_0_to_k_minus_1_in_order"])); add("T_flags_runner_up_gt_score", bool(v["map_runner_up_le_score"]))
    add("T_flags_show_sent_without_map", bool(v["show_sent_refers_to_map"])); add("T_flags_plain_clear_with_sent", bool(v["clear_plain_has_no_sent"]))
    r = {"command": "speak", "page": None, "first_focus_ms": 12.5, "doc_ready_ms": None, "ground_miss_reasons": {"error": 2}, "focus_methods": {"ground-miss": 1}, "ground_provider": "local"}
    v = check_run({"rec": r, "raw": r, "focus": [], "home": TMPROOT, "t0": time.time()})
    add("T_flags_float_ms", bool(v["first_focus_ms_int_or_null"])); add("T_flags_ground_miss_sum", bool(v["ground_miss_reasons_valid"])); add("T_flags_focus_ms_without_show", bool(v["first_focus_ms_null_iff_no_show"]))
    # privacy_audit.py label check flags a planted leak, accepts clean / text-mode logs
    for nm, show_extra, draw_label, want in (("leak", {}, "Figure 3a: Zorblax unit", True), ("clean", {}, "Figure 3a", False), ("textmode", {"text": "x"}, None, False)):
        h = mkhome("pa" + nm); os.makedirs(os.path.join(h, "logs"))
        lab = "Figure 3a: Zorblax unit" if nm != "clean" else "Figure 3a"
        open(os.path.join(h, "logs", "runs.jsonl"), "w").write(json.dumps({"ts": "x", "command": "speak", "image": True, "first_focus_ms": 5, "doc_parse": {"columns": 1, "figs_done": False, "blocks": {"paragraph": 2}}}) + "\n")
        open(os.path.join(h, "logs", "focus.jsonl"), "w").write(json.dumps(dict({"state": "show", "sent": 0, "doc": {"kind": "object", "label": lab, "target": [1, 2, 3, 4], "target_src": "label-object"}}, **show_extra)) + "\n")
        if draw_label: open(os.path.join(h, "logs", "tracker.jsonl"), "w").write(json.dumps({"event": "draw", "seq": 1, "label": draw_label}) + "\n")
        p = subprocess.run([PY, os.path.join(REPO, "tests", "audit", "privacy_audit.py"), "--image", fx(FX["sentfig_png"]), "--thumb", os.path.join(TMPROOT, "t.png")], capture_output=True, text=True, env=env_for(h), timeout=60)
        try: pa = json.loads(p.stdout)
        except Exception: pa = {}
        add(f"T_privacy_audit_label_check_{nm}", ("PRIVACY_LEAK" in pa) == want and "label_audit" in pa and "zorblax" not in p.stdout.lower().replace(os.path.basename(h).lower(), ""), (p.returncode, pa.get("label_audit"), p.stderr[-120:]))
    add("T_textfree_rejects_text", not textfree("doc.target_src", "figure:zorblax") and not textfree("doc.split_from", "Figure 5a: Zorblax") and textfree("doc.split_from", "Figure 5"))

# ================================================================ run
T0 = time.time()
for fn in (selftest, parse_output, parse_version, focus_checks, runs_checks, grounding_checks, privacy_checks, label_privacy, additive, schema_doc, tracker): fn()
for f in glob.glob(os.path.join(tempfile.gettempdir(), "speakeasy_cap_p2*")):
    try: os.remove(f)
    except Exception: pass
add("Z_never_touched_real_speakeasy_home", not any(os.path.realpath(o["home"]).startswith(L.REAL_HOME) for o in ALL), "")
if A.keep or os.environ.get("P2_KEEP") == "1": print(f"# kept {TMPROOT}")
else: shutil.rmtree(TMPROOT, ignore_errors=True)
n = {k: sum(1 for s, _ in R if s == k) for k in ("PASS", "FAIL", "SKIP")}
print(f"SUMMARY checks={len(R)} pass={n['PASS']} fail={n['FAIL']} skip={n['SKIP']} runs={len(ALL)} secs={int(time.time() - T0)}", flush=True)
sys.exit(0 if n["FAIL"] == 0 else 1)
