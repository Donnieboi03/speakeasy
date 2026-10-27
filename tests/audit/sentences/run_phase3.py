#!/usr/bin/env python3
"""Phase-3 acceptance suite (black box, promoted from the holdout) for docs/contracts/phase3-sentence-ids.md, beyond the bench.
Public data only (own synthetic fixtures). Defaults are file-relative (--repo) and this interpreter (needs reportlab and Pillow;
P2_PYTHON overrides); geom.py, make_fig5.py and the mock LLM are shared with tests/audit/logging.
Drives only the CLI (src/speakeasy), `docmap.py parse-pdf`, the tracker built from src/tracker.swift (temp SwiftPM copy outside
the repo), and the logs / live focus.json under temp $SPEAKEASY_HOME. Differential checks use the baseline commit
(--baseline-ref, default 890faa4). Never calls a real LLM (local mock on an ephemeral port), never touches ~/.speakeasy.
Usage: run_phase3.py [--repo PATH] [--baseline-ref REF] [--only F,M,C,L,S,X,T,P,K,D] [--no-tracker] [--keep]
Output: `PASS|FAIL|SKIP <name> <detail>` per check, then `SUMMARY ...`. Exit 0 only if no FAIL."""
import argparse, glob, json, math, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); LOGGING = os.path.join(os.path.dirname(HERE), "logging"); sys.path.insert(0, HERE); sys.path.insert(1, LOGGING)
import p3lib as L
from p3lib import jl, Mock, mkhome, env_for, runs, focus_log, flat, sentinel_hits, TMPROOT, PY
os.environ.setdefault("DOC_VENV", os.path.dirname(os.path.dirname(PY)))
from geom import page_to_screen, xywh, K

ap = argparse.ArgumentParser(); ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", ".."))); ap.add_argument("--baseline-ref", default="890faa4")
ap.add_argument("--tree", help="tree under test (src/, tracker/); default --repo"); ap.add_argument("--only", default=""); ap.add_argument("--no-tracker", action="store_true"); ap.add_argument("--keep", action="store_true"); A = ap.parse_args()
REPO = os.path.abspath(A.repo); TREE = os.path.abspath(A.tree or A.repo); SRC = os.path.join(TREE, "src"); CUR = os.path.join(SRC, "speakeasy"); ONLY = {x.strip().upper() for x in A.only.split(",") if x.strip()}
FONT = os.path.join(REPO, "tests", "audit", "bench", "fonts", "DejaVuSans.ttf")
R = []
def _s(d): return " ".join(str(d).split())[:420]
def add(name, ok, detail=""):
    ok = bool(ok); st = "PASS" if ok else "FAIL"; R.append((st, name)); print(f"{st} {name} {_s(detail) if not ok or os.environ.get('P3_VERBOSE') else ''}".rstrip(), flush=True)
def skip(name, why): R.append(("SKIP", name)); print(f"SKIP {name} {_s(why)}", flush=True)
def section(tag):
    def deco(fn):
        def run():
            if ONLY and tag not in ONLY: return
            try: fn()
            except Exception as e: add(f"{tag}_section_crashed", False, f"{type(e).__name__}: {e} :: {traceback.format_exc()[-700:]}")
        run.__name__ = fn.__name__; return run
    return deco
def isnum(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
def r2(v): return isnum(v) and abs(round(v, 2) - v) < 1e-9
def rect_ok(r): return isinstance(r, list) and len(r) == 4 and all(isnum(v) for v in r)
def union_xyxy(rs): return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]
def union_xywh(rs):
    u = union_xyxy([[r[0], r[1], r[0] + r[2], r[1] + r[3]] for r in rs]); return [u[0], u[1], u[2] - u[0], u[3] - u[1]]
def near(a, b, tol=1.0): return rect_ok(a) and rect_ok(b) and max(abs(x - y) for x, y in zip(a, b)) <= tol
def same_set(got, want, tol=1.0):
    got = [g for g in (got or []) if rect_ok(g)]
    return len(got) == len(want) and all(any(near(g, w, tol) for g in got) for w in want) and all(any(near(g, w, tol) for w in want) for g in got)
def subset(got, want, tol=1.0): return bool(got) and all(rect_ok(g) and any(near(g, w, tol) for w in want) for g in got)
SID = re.compile(r"^s\d+$")
def sidn(s): return int(s[1:]) if isinstance(s, str) and SID.match(s) else None
NEW_METHODS = {"align", "align-fuzzy", "span", "hold", "block"}
SM_KEYS = {"state", "sentences", "mapped", "method", "ms", "ts", "pid"}
RUN_SM_KEYS = {"sentences", "mapped", "method", "ms"}
SM_METHODS = {"page", "ax-lines", "band", "none"}

# ---------------------------------------------------------------- setup: baseline tree, fixtures, geometry
BASE = os.path.join(TMPROOT, "base"); os.makedirs(BASE)
subprocess.run(f"git -C '{REPO}' archive {A.baseline_ref} | tar -x -C '{BASE}'", shell=True, check=True)
BIN_BASE = os.path.join(BASE, "src", "speakeasy")
FXD = os.path.join(TMPROOT, "fx"); os.makedirs(FXD)
subprocess.run([PY, os.path.join(HERE, "make_p3docs.py"), FXD, FONT], check=True, capture_output=True)
subprocess.run([PY, os.path.join(LOGGING, "make_fig5.py"), FXD], check=True, capture_output=True)
BLANK_PNG = os.path.join(FXD, "blank.png")
subprocess.run([PY, "-c", f"from PIL import Image; Image.new('RGB', (64, 48), 'white').save({BLANK_PNG!r})"], check=True)
PROSE = os.path.join(FXD, "p3prose.pdf"); FIG5 = os.path.join(FXD, "fig5.pdf")
P3 = json.load(open(os.path.join(FXD, "p3sent.json")))
LINES = P3["left"] + P3["right"] + P3["p2"]                      # drawn line texts (selection text = these, as Preview copies them)
LOC = [(1, i) for i in range(len(P3["left"]))] + [(1, len(P3["left"]) + i) for i in range(len(P3["right"]))] + [(2, i) for i in range(len(P3["p2"]))]
SENTS = [(t, ix) for t, ix in P3["sents"]]
NA = len(P3["left"]) + len(P3["right"])                           # selection A = page 1 only (lines 0..NA-1)
SENTS_A = [s for s in SENTS if all(i < NA for i in s[1])] + [("The zorblax montage was identical for all of the", [NA - 1])]

def scrub_env():
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("SPEAKEASY_", "STUB_", "MOCK_")): e.pop(k)
    h = mkhome("pp"); e.update(HOME=h, SPEAKEASY_HOME=h, PYTHONDONTWRITEBYTECODE="1"); return e
PARSED = {}
def lines_of(pdf):
    if pdf not in PARSED:
        out = os.path.join(TMPROOT, f"parse_{time.time_ns()}.json"); cp = os.path.join(TMPROOT, f"in_{time.time_ns()}_" + os.path.basename(pdf)); shutil.copy(pdf, cp)
        subprocess.run([PY, os.path.join(BASE, "src", "docmap.py"), "parse-pdf", cp, out], capture_output=True, text=True, env=scrub_env(), timeout=180)
        PARSED[pdf] = json.load(open(out))
    return PARSED[pdf]
G = dict(zoom=1.25, backing=2)
def geom_for(pdf, first_y):
    pg = lines_of(pdf)["pages"][0]; s = 1.25 * K
    g = dict(G); g["ox"] = (1512 - pg["w"] * s) / 2; g["oy"] = 33 + 300 - first_y * s; g["h"] = pg["h"]; return g
def scr(rect, g, page=1):
    gg = dict(g)
    if page == 2: gg["oy"] = g["oy"] + (g["h"] + 12) * g["zoom"] * K
    return [round(v, 1) for v in xywh(page_to_screen(rect[:4], gg))]
def page_line(loc):
    p, i = loc; return lines_of(PROSE)["pages"][p - 1]["lines"][i]

def hint(home, pdf):
    p = os.path.join(os.path.dirname(home), "hint.json")
    json.dump({"app": "Preview", "bundle": "com.apple.Preview", "title": os.path.basename(pdf), "ax_document": "file://" + pdf}, open(p, "w")); return p

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

ALL = []
def cli(name, args, home, env, inp=None, bin_=CUR, timeout=240):
    n_runs = len(runs(home)); n_f = len(focus_log(home)); t0 = time.time()
    with Watch(home) as w:
        try: p = subprocess.run([bin_] + args, input=inp, capture_output=True, text=True, env=env, timeout=timeout); rc, se = p.returncode, p.stderr
        except subprocess.TimeoutExpired: rc, se = "timeout", ""
    new = runs(home)[n_runs:]; F = focus_log(home)[n_f:]
    o = {"name": name, "rc": rc, "stderr": se, "rec": flat(new[-1]) if new else None, "focus": F, "live": w.focus, "home": home, "bin": bin_,
         "logtext": env.get("SPEAKEASY_LOG_TEXT") == "1", "t0": t0}
    if bin_ == CUR: ALL.append(o)
    return o
def base_env(home, mock, **kw):
    b = dict(SPEAKEASY_VISION="never", SPEAKEASY_GROUND="off", SPEAKEASY_TRACKER=os.path.join(TMPROOT, "no-tracker"), SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1",
             SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_TEXT_HIGHLIGHT="on", SPEAKEASY_FIGURE_CUES="on", SPEAKEASY_KEEP_CAPTURE="0", SPEAKEASY_DOC_READ="local", SPEAKEASY_LOG_TEXT="1")
    b.update(kw); return env_for(home, mock.url if mock else "http://127.0.0.1:9/v1", **{k: v for k, v in b.items()})

def maps(o): return [r for r in o["focus"] if r.get("state") == "map"]
def shows(o): return [r for r in o["focus"] if r.get("state") == "show"]
def smaps(o): return [r for r in o["focus"] if r.get("state") == "sentence_map"]
def last_map(o):
    d = {}
    for r in maps(o): d[r.get("sent")] = r
    return d
def last_show(o):
    d = {}
    for r in shows(o): d[r.get("sent")] = r
    return d
def doc_lines(s): d = s.get("doc") if isinstance(s.get("doc"), dict) else {}; return d.get("lines") if isinstance(d.get("lines"), list) else None
def chunk_rows(o):
    """[(sent, map, show)] in sent order."""
    M, S = last_map(o), last_show(o); return [(k, M.get(k), S.get(k)) for k in sorted(set(M) | set(S), key=lambda x: (x is None, x))]
def ids_seq(o): return [s.get("sentence_id") for s in shows(o)]
def meth(mp, want, doc=True):
    """Map method check (contract F amendment 3): sentence-map chunks log the assignment method, never `doc-line`."""
    return (mp or {}).get("method") == want
def nulls(mp, sh):
    """Amendment 4: hold and block carry null map score / runner_up and null show match_score / runner_up."""
    return (mp or {}).get("score") is None and (mp or {}).get("runner_up") is None and (sh or {}).get("match_score") is None and (sh or {}).get("runner_up") is None
def nondecreasing(o):
    v = [sidn(x) for x in ids_seq(o) if x is not None]; return all(b >= a for a, b in zip(v, v[1:])), v

def prose_case(name, script, sel=range(NA), form="list", mock_kw=None, bin_=CUR, n=1, extra_env=None, doc=True, warm=True):
    """p3prose.pdf selection of drawn lines `sel`; list-form (one rect per selection line) or visual-form line bounds."""
    sel = list(sel); g = geom_for(PROSE, page_line(LOC[sel[0]])[1])
    rects = [scr(page_line(LOC[i]), g, LOC[i][0]) for i in sel]
    text = "\n".join(LINES[i] for i in sel)
    kw = dict(MOCK_TEXT=script, MOCK_FIRST_DELAY="0.3", MOCK_TOKEN_DELAY="0.002"); kw.update(mock_kw or {})
    m = Mock(**kw); out = []
    try:
        h = mkhome(name[:10]); lb = os.path.join(os.path.dirname(h), "lb.json")
        if form == "list": json.dump(rects, open(lb, "w"))
        else:
            vis, pos = [], 0
            for i, r in zip(sel, rects): vis.append({"loc": pos, "abs": 7000 + pos, "len": len(LINES[i]), "rect": r}); pos += len(LINES[i]) + 1
            json.dump(vis, open(lb, "w"))
        u = union_xywh(rects); cap = f"{u[0]-16:.1f},{u[1]-16:.1f},{u[2]+32:.1f},{u[3]+32:.1f}"
        args = ["speak", "--capture-method", "bounds", f"--capture-rect={cap}", "--line-bounds", lb] + (["--doc-hint", hint(h, PROSE)] if doc else [])
        e = base_env(h, m, **(extra_env or {}))
        def refresh():
            json.dump(rects if form == "list" else vis, open(lb, "w"))
            if doc: hint(h, PROSE)
        if warm and doc:
            cli(name + "#warm", args, h, e, inp=text, bin_=bin_); refresh()
            shutil.rmtree(os.path.join(h, "logs"), ignore_errors=True)
        for i in range(n):
            o = cli(f"{name}#{i}", args, h, e, inp=text, bin_=bin_); o.update(sel=sel, rects=rects, text=text, doc=doc); out.append(o); refresh()
    finally: m.stop()
    return out if n > 1 else out[0]

def exp_page_rects(k, sents=SENTS_A, page=1):
    return [page_line(LOC[i])[:4] for i in sents[k][1] if LOC[i][0] == page]
def exp_screen_rects(o, k, sents=SENTS_A):
    return [o["rects"][o["sel"].index(i)] for i in sents[k][1] if i in o["sel"]]
def sid_of_text(t, sents=SENTS_A):
    return [f"s{k}" for k, (x, _) in enumerate(sents) if x == t]

# ================================================================ F. shared text fold
# contract A shared vectors: (input, key, fold tokens, match tokens)
VEC = [("classi\ufb01cation", "classification", "classification", "classification"), ("classi-\nfication", "classification", "classification", "classification"),
       ("state-of-the-\nart", "stateoftheart", "state of theart", "state of theart"), ("Line-\nBased", "linebased", "line based", "line based"),
       ("co\u00adoperate", "cooperate", "cooperate", "cooperate"), ("Twenty-five per cent", "25percent", "25 percent", "25 percent"), ("25 %", "25percent", "25 percent", "25 percent"),
       ("Fig. 5 | Pipeline", "fig5pipeline", "fig 5 pipeline", "fig 5 pipeline"), ("\uff21\uff22\uff23\uff11\uff12", "abc12", "abc12", "abc12"),
       ("na\u00efve caf\u00e9", "na\u00efvecaf\u00e9", "na\u00efve caf\u00e9", "na\u00efve caf\u00e9"), ("\u201cQuoted\u201d\u2014text", "quotedtext", "quoted text", "quoted text"),
       ("one of the 3 rows", "1ofthe3rows", "1 of the 3 rows", "1 of the 3 rows"), ("2.5 kHz", "25khz", "2 5 khz", "2 5 khz"),
       ("a 3 of", "a3of", "a 3 of", "3 of"), ("Panel (b) at x = 7", "panelbatx7", "panel b at x 7", "panel at 7"), ("\u2060\ufeff", "", "", "")]
VEC = [(a, b, c.split(), d.split()) for a, b, c, d in VEC]
VEC_JSON = os.path.join(TMPROOT, "fold_vectors.json"); json.dump([{"input": a, "key": b, "tokens": c, "match": d} for a, b, c, d in VEC], open(VEC_JSON, "w"), ensure_ascii=False)
PROBE = r'''
import json, sys, importlib, inspect
sys.path.insert(0, sys.argv[1]); V = json.load(open(sys.argv[2])); out = {"fold": None, "tokens": None, "res": []}
def key_of(r):
    if isinstance(r, str): return r, None
    if isinstance(r, dict): return r.get("key"), r.get("offsets", r.get("map"))
    if isinstance(r, (tuple, list)) and r and isinstance(r[0], str): return r[0], (r[1] if len(r) > 1 else None)
    k = getattr(r, "key", None); return k, getattr(r, "offsets", getattr(r, "map", None))
fold = toks = mtoks = None
for mod in sys.argv[3].split(","):
    try: m = importlib.import_module(mod)
    except Exception: continue
    if fold is None and callable(getattr(m, "fold", None)): fold = m.fold; out["fold"] = mod + ".fold"
    for tn in ("fold_tokens", "foldTokens") + (("tokens",) if mod == "textfold" else ()):
        if toks is None and callable(getattr(m, tn, None)): toks = getattr(m, tn); out["tokens"] = mod + "." + tn
    if mod == "docmap" and callable(getattr(m, "_toks", None)): mtoks = m._toks; out["match"] = "docmap._toks"
for v in V:
    r = {"input": v["input"]}
    if fold:
        try: k, off = key_of(fold(v["input"])); r["key"] = k; r["offsets"] = list(off) if off is not None else None
        except Exception as e: r["key_err"] = repr(e)
    for nm, fn in (("tokens", toks), ("match", mtoks)):
        if fn:
            try: t = fn(v["input"]); r[nm] = list(t) if not isinstance(t, (set, frozenset)) else sorted(t); r[nm + "_is_set"] = isinstance(t, (set, frozenset))
            except Exception as e: r[nm + "_err"] = repr(e)
    out["res"].append(r)
print(json.dumps(out, ensure_ascii=False))
'''
FOLD_CLI = [("Zorblax classi\ufb01cation was checked.", "Zorblax classification was checked."),
            ("Quintessa classi-\nfication was checked.", "Quintessa classification was checked."),
            ("Mirevolt state-of-the-\nart filters were used.", "Mirevolt state of the art filters were used."),
            ("Plumbrine Line-\nBased rules were used.", "Plumbrine line based rules were used."),
            ("Vexhollow co\u00adoperate with peers.", "Vexhollow cooperate with peers."),
            ("Grelquist rates rose twenty-five per cent overall.", "Grelquist rates rose 25 % overall."),
            ("Dunmarrow saw 25 % growth.", "Dunmarrow saw twenty five percent growth."),
            ("Skelvane is shown in Fig. 5 | Pipeline today.", "Skelvane is shown in Fig. 5, Pipeline today."),
            ("Frumplet code \uff21\uff22\uff23\uff11\uff12 was logged.", "Frumplet code ABC12 was logged."),
            ("Wexmoor felt na\u00efve caf\u00e9 vibes.", "Wexmoor felt NA\u00cfVE CAF\u00c9 vibes."),
            ("Zorblax said \u201cQuoted\u201d\u2014text twice.", "Zorblax said quoted text twice."),
            ("Quintessa kept one of the 3 rows.", "Quintessa kept 1 of the three rows."),
            ("Mirevolt ran at 2.5 kHz today.", "Mirevolt ran at 2.5 KHZ today.")]

@section("F")
def fold_checks():
    mods = [os.path.splitext(os.path.basename(p))[0] for p in sorted(glob.glob(os.path.join(SRC, "*.py")))]
    mods = sorted(mods, key=lambda m: (m not in ("focus", "docmap", "textfold", "fold"), m))
    p = subprocess.run([PY, "-c", PROBE, SRC, VEC_JSON, ",".join(mods)], capture_output=True, text=True, env=scrub_env(), timeout=120)
    try: pr = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception: pr = None
    if not pr or not pr.get("fold"): skip("F_py_fold_vectors", f"no importable fold() in src modules ({(p.stderr or '')[-160:]})")
    else:
        for (inp, key, _, _), r in zip(VEC, pr["res"]):
            add(f"F_py_fold_key_{inp.encode('unicode_escape').decode()[:24]}", r.get("key") == key, (pr["fold"], r.get("key"), key, r.get("key_err")))
        lig = pr["res"][0].get("offsets"); hy = pr["res"][1].get("offsets")
        if lig is None: skip("F_py_fold_offsets", "fold() exposes no offset map")
        else:
            add("F_py_fold_offsets_ligature_fi_map_to_6", isinstance(lig, list) and len(lig) == 14 and lig[6] == 6 and lig[7] == 6, lig)
            add("F_py_fold_offsets_hyphen_break_unmapped", isinstance(hy, list) and len(hy) == 14 and 6 not in hy and 7 not in hy and hy == sorted(hy), hy)
            add("F_py_fold_offsets_empty_key", pr["res"][-1].get("offsets") in ([], None) and pr["res"][-1].get("key") == "", pr["res"][-1])
            pb = pr["res"][-2].get("offsets"); src = VEC[-2][0]
            add("F_py_fold_offsets_point_at_source_chars", isinstance(pb, list) and len(pb) == len(VEC[-2][1]) and all(src[i].lower() == c for i, c in zip(pb, VEC[-2][1])), (pb, VEC[-2][1]))
    if not pr or not pr.get("match"): add("F_py_docmap_toks_importable", False, (p.stderr or "")[-200:])
    else:
        for (inp, _, _, mk), r in zip(VEC, pr["res"]):
            got = r.get("match"); ok = got is not None and ((sorted(set(got)) == sorted(set(mk))) if r.get("match_is_set") else got == mk)
            add(f"F_py_match_tokens_{inp.encode('unicode_escape').decode()[:24]}", ok, (pr["match"], got, mk, r.get("match_err")))
    if not pr or not pr.get("tokens"): skip("F_py_fold_tokens_vectors", "no fold_tokens()/foldTokens()/textfold.tokens() in src modules (contract names no Python API)")
    else:
        for (inp, key, tk, _), r in zip(VEC, pr["res"]):
            got = r.get("tokens"); ok = got is not None and ((sorted(set(got)) == sorted(set(tk))) if r.get("tokens_is_set") else got == tk)
            add(f"F_py_fold_tokens_{inp.encode('unicode_escape').decode()[:24]}", ok, (pr["tokens"], got, tk, r.get("tokens_err")))
            if got is not None and not r.get("tokens_is_set"): add(f"F_py_fold_tokens_join_is_key_{inp.encode('unicode_escape').decode()[:24]}", "".join(got) == key, ("".join(got), key))
    # CLI-observable: each selection sentence holds a vector form; the model speaks the other form -> verbatim, score 1.0
    sel = "\n".join(a for a, _ in FOLD_CLI); script = " ".join(b for _, b in FOLD_CLI)
    m = Mock(MOCK_TEXT=script, MOCK_FIRST_DELAY="0.1", MOCK_TOKEN_DELAY="0.002")
    try:
        h = mkhome("foldcli"); nl = sel.split("\n"); lb = os.path.join(os.path.dirname(h), "lb.json")
        rects = [[100.0, 100.0 + 20 * i, 500.0, 16.0] for i in range(len(nl))]; json.dump(rects, open(lb, "w"))
        o = cli("fold_cli", ["speak", "--capture-method", "bounds", "--capture-rect=84,84,532,%d" % (20 * len(nl) + 32), "--line-bounds", lb], h, base_env(h, m), inp=sel)
    finally: m.stop()
    sm = smaps(o); add("F_cli_sentence_map_counts_13_units", bool(sm) and sm[0].get("sentences") == len(FOLD_CLI), [x.get("sentences") for x in sm])
    first_line = []; c = 0
    for a, _ in FOLD_CLI: first_line.append(c); c += a.count("\n") + 1
    for k, (a, b) in enumerate(FOLD_CLI):
        rows = [(mp, sh) for _, mp, sh in chunk_rows(o) if sh and sh.get("sentence_id") == f"s{k}"]
        tag = a.encode("unicode_escape").decode()[:26]
        if not rows: add(f"F_cli_verbatim_{tag}", False, ("no show with", f"s{k}", ids_seq(o))); continue
        ok = all((mp or {}).get("method") == "align" and (mp or {}).get("score") == 1.0 and sh.get("match_score") == 1.0 for mp, sh in rows)
        want = rects[first_line[k]: first_line[k] + a.count("\n") + 1]
        ok_r = same_set([x for _, sh in rows for x in (sh.get("rects") or [])], want, 0.2) if len(rows) == 1 else subset([x for _, sh in rows for x in (sh.get("rects") or [])], want, 0.2)
        add(f"F_cli_verbatim_{tag}", ok and ok_r, [((mp or {}).get("method"), (mp or {}).get("score"), sh.get("match_score"), sh.get("rects")) for mp, sh in rows][:2])
    add("F_cli_ids_in_order", nondecreasing(o)[0] and len(set(x for x in ids_seq(o) if x)) == len(FOLD_CLI), ids_seq(o))
    swift_fold()

# ---------------------------------------------------------------- Swift: tracker build + XCTest copies in temp packages
TRK = {}
def build_pkg(tag, src_dir, extra_tests=None):
    if not shutil.which("swift"): return None, "no swift toolchain"
    # mirror the repo layout outside the repo (tracker/, src/tracker.swift, top-level tests/*.json the package tests may read)
    root = os.path.join(TMPROOT, "trk", tag); pkg = os.path.join(root, "tracker"); os.makedirs(os.path.join(root, "src"), exist_ok=True)
    shutil.copytree(os.path.join(src_dir, "tracker"), pkg, symlinks=True, ignore=shutil.ignore_patterns(".build"))
    shutil.copy(os.path.join(src_dir, "src", "tracker.swift"), os.path.join(root, "src", "tracker.swift"))
    os.makedirs(os.path.join(root, "tests"), exist_ok=True)
    for f in glob.glob(os.path.join(src_dir, "tests", "*.json")): shutil.copy(f, os.path.join(root, "tests"))
    ts = os.path.join(pkg, "Sources", "speakeasy-track", "tracker.swift")
    if not os.path.exists(ts):
        if os.path.lexists(ts): os.remove(ts)
        shutil.copy(os.path.join(src_dir, "src", "tracker.swift"), ts)
    for n, body in (extra_tests or {}).items(): open(os.path.join(pkg, "Tests", "TrackerTests", n), "w").write(body)
    p = subprocess.run(["swift", "build"], cwd=pkg, capture_output=True, text=True, timeout=900)
    exe = os.path.join(pkg, ".build", "debug", "speakeasy-track")
    if p.returncode or not os.path.exists(exe): return None, "swift build failed: " + (p.stdout + p.stderr)[-300:]
    return pkg, None
def tracker(which="cur"):
    if which not in TRK: TRK[which] = build_pkg(which, TREE if which == "cur" else BASE)
    return TRK[which]
def swift_test(pkg, filt=None):
    p = subprocess.run(["swift", "test"] + (["--filter", filt] if filt else []), cwd=pkg, capture_output=True, text=True, timeout=1200)
    mt = re.findall(r"Executed (\d+) tests?, with (\d+) failures", p.stdout + p.stderr)
    mx = max(mt, key=lambda x: int(x[0])) if mt else None
    return p.returncode, mx, (p.stdout + p.stderr)
SW_TOK = r'''
import XCTest
import Foundation
@testable import speakeasy_track
func p3arr(_ t: [String]) -> [String]? { t }
func p3arr(_ t: Set<String>) -> [String]? { nil }
final class P3MatchTokenVectors: XCTestCase {
    func testMatchTokens() throws {
        let d = try Data(contentsOf: URL(fileURLWithPath: "%s"))
        let v = try JSONSerialization.jsonObject(with: d) as! [[String: Any]]
        for x in v {
            let inp = x["input"] as! String, want = x["match"] as! [String]
            let t = tokens(inp)
            XCTAssertEqual(Set(t.map { String($0) }), Set(want), "tokens(\(inp.debugDescription))")
            if let a = p3arr(t) { XCTAssertEqual(a, want, "tokens order (\(inp.debugDescription))") }
        }
    }
}
'''
SW_FOLDTOK = r'''
import XCTest
import Foundation
@testable import speakeasy_track
func p3farr(_ t: [String]) -> [String]? { t }
func p3farr(_ t: Set<String>) -> [String]? { nil }
final class P3FoldTokenVectors: XCTestCase {
    func testFoldTokens() throws {
        let d = try Data(contentsOf: URL(fileURLWithPath: "%s"))
        let v = try JSONSerialization.jsonObject(with: d) as! [[String: Any]]
        for x in v {
            let inp = x["input"] as! String, key = x["key"] as! String, want = x["tokens"] as! [String]
            let t = foldTokens(inp)
            XCTAssertEqual(Set(t.map { String($0) }), Set(want), "foldTokens(\(inp.debugDescription))")
            if let a = p3farr(t) { XCTAssertEqual(a, want, "foldTokens order (\(inp.debugDescription))"); XCTAssertEqual(a.joined(), key, "joined foldTokens(\(inp.debugDescription)) is the key") }
            else { XCTFail("foldTokens returns no ordered array; cannot build the joined key") }
        }
    }
}
'''
SW_FOLD = r'''
import XCTest
import Foundation
@testable import speakeasy_track
func p3key(_ r: Any) -> String? {
    if let s = r as? String { return s }
    let m = Mirror(reflecting: r)
    for c in m.children where c.label == "key" || c.label == ".0" { if let s = c.value as? String { return s } }
    for c in m.children { if let s = c.value as? String { return s } }
    return nil
}
final class P3FoldKeyVectors: XCTestCase {
    func testKeys() throws {
        let d = try Data(contentsOf: URL(fileURLWithPath: "%s"))
        let v = try JSONSerialization.jsonObject(with: d) as! [[String: Any]]
        for x in v {
            let inp = x["input"] as! String, key = x["key"] as! String
            XCTAssertEqual(p3key(%s(inp)), key, "fold(\(inp.debugDescription))")
        }
    }
}
'''
def swift_fold():
    if A.no_tracker: skip("F_swift_vectors", "--no-tracker"); return
    pkg, why = build_pkg("tokvec", TREE, {"P3MatchTokenVectors.swift": SW_TOK % VEC_JSON})
    if not pkg: add("F_swift_tokens_vectors_build", False, why); return
    rc, mx, out = swift_test(pkg, "P3MatchTokenVectors")
    add("F_swift_tokens_match_column_xctest", rc == 0 and mx and mx[1] == "0" and int(mx[0]) >= 1, (rc, mx, re.findall(r"error: .*", out)[:4]))
    td = os.path.join(pkg, "Tests", "TrackerTests"); os.remove(os.path.join(td, "P3MatchTokenVectors.swift"))
    open(os.path.join(td, "P3FoldTokenVectors.swift"), "w").write(SW_FOLDTOK % VEC_JSON)
    rc, mx, out = swift_test(pkg, "P3FoldTokenVectors")
    add("F_swift_foldTokens_tokens_column_and_joined_key_xctest", rc == 0 and mx and mx[1] == "0" and int(mx[0]) >= 1, (rc, mx, re.findall(r"error: .*", out)[:4]))
    os.remove(os.path.join(td, "P3FoldTokenVectors.swift"))
    for fn in ("fold", "textFold", "foldKey", "foldText"):
        open(os.path.join(td, "P3FoldKeyVectors.swift"), "w").write(SW_FOLD % (VEC_JSON, fn))
        rc, mx, out = swift_test(pkg, "P3FoldKeyVectors")
        if f"cannot find '{fn}' in scope" in out: continue
        add(f"F_swift_{fn}_key_vectors_xctest", rc == 0 and mx and mx[1] == "0", (rc, mx, re.findall(r"error: .*|XCTAssertEqual failed.*", out)[:4])); return
    skip("F_swift_fold_key_vectors", "no Swift fold/textFold/foldKey/foldText symbol (contract names none); key checked via joined foldTokens()")

# ================================================================ M. model misbehaviour (read-through, p3prose.pdf page 1, page rects)
SA = lambda k: SENTS_A[k][0]
SC = {}
def scen():
    if SC: return SC
    SC["full"] = prose_case("full", " ".join(SA(k) for k in range(len(SENTS_A))), n=2)
    SC["para"] = prose_case("para", " ".join([SA(0), "Quintessa readings were sampled at a fixed rate on the amplifier.", "That covers the sampling setup in short.", SA(2)]))
    SC["skip"] = prose_case("skip", " ".join([SA(0), SA(1), SA(3), SA(4)]))
    SC["repeat"] = prose_case("repeat", " ".join([SA(0), SA(1), SA(2), SA(1), SA(3)]))
    SC["reorder"] = prose_case("reorder", " ".join([SA(0), SA(2), SA(1), SA(3)]))
    SC["opener"] = prose_case("opener", " ".join(["Sure, here is the passage read aloud for you.", SA(0), "Moving on to the next part now.", SA(1)]))
    SC["dup"] = prose_case("dup", " ".join([SA(4), SA(5), SA(6), SA(7), SA(8)]))
    SC["dup2"] = prose_case("dup2", " ".join([SA(4), SA(5), SA(5), SA(8)]))
    SC["default"] = prose_case("deflt", " ".join([SA(k) for k in range(6)]) + " That part was about zorblax sensors and nothing else.", extra_env={"SPEAKEASY_LOG_TEXT": None})
    return SC

def check_chunk_geometry(tag, o, k, sents=SENTS_A, page_rects=True):
    """Union over the chunks of sentence k: doc.lines == its page lines, rects == its selection-line rects; each chunk a subset."""
    rows = [sh for sh in shows(o) if sh.get("sentence_id") == f"s{k}"]
    if not rows: add(f"{tag}_has_show", False, ids_seq(o)); return
    allr = [r for sh in rows for r in (sh.get("rects") or [])]; want_s = exp_screen_rects(o, k, sents)
    add(f"{tag}_screen_rects_are_its_lines_unmerged", same_set(dedup(allr), want_s, 0.3) and all(subset(sh.get("rects"), want_s, 0.3) for sh in rows), (allr, want_s))
    if page_rects:
        dl = [r for sh in rows for r in (doc_lines(sh) or [])]; want_p = exp_page_rects(k, sents)
        add(f"{tag}_doc_lines_are_its_page_lines", same_set(dedup(dl), want_p, 1.0) and all(subset(doc_lines(sh), want_p, 1.0) for sh in rows), (dl, want_p))
def dedup(rs):
    out = []
    for r in rs:
        if not any(near(r, x, 0.05) for x in out): out.append(r)
    return out

@section("M")
def misbehaviour():
    S = scen(); o = S["full"][0]
    add("M_full_rc0", o["rc"] == 0, o["stderr"][-200:])
    sm = smaps(o); add("M_full_sentence_map_units_match_splitter", bool(sm) and sm[0].get("sentences") == len(SENTS_A), ([x.get("sentences") for x in sm], len(SENTS_A)))
    add("M_full_sentence_map_method_page_mapped_all", bool(sm) and sm[-1].get("method") == "page" and sm[-1].get("mapped") == len(SENTS_A), [(x.get("method"), x.get("mapped")) for x in sm])
    rows = chunk_rows(o)
    bad = [(k, (mp or {}).get("method"), (mp or {}).get("score"), (sh or {}).get("sentence_id")) for k, mp, sh in rows if not (mp and meth(mp, "align") and (sh or {}).get("match_score") == 1.0)]
    add("M_full_every_chunk_verbatim_score_1", rows and not bad, bad[:5])
    want = [f"s{k}" for k in range(len(SENTS_A))]; got = list(dict.fromkeys(x for x in ids_seq(o) if x))
    add("M_full_ids_cover_s0_to_sN_in_order", got == want, (got, want))
    add("M_full_ids_nondecreasing", nondecreasing(o)[0], ids_seq(o))
    for k in (0, 1, 3, 5, 9):
        check_chunk_geometry(f"M_full_s{k}", o, k)
    # map record: sentence_id + sentence_ids; score / runner_up are C.1 values
    bad = [(r.get("sent"), r.get("sentence_id"), r.get("sentence_ids")) for r in maps(o) if r.get("method") in ("align", "align-fuzzy", "span")
           and not (SID.match(str(r.get("sentence_id"))) and isinstance(r.get("sentence_ids"), list) and r.get("sentence_id") in r.get("sentence_ids"))]
    add("M_full_map_sentence_id_in_sentence_ids", maps(o) and not bad, bad[:4])
    # paraphrase
    o = S["para"]; rows = chunk_rows(o)
    fz = [(mp, sh) for _, mp, sh in rows if mp and sh and "fixed rate on the amplifier" in (mp.get("text") or "")]
    add("M_para_fuzzy_band_on_s1", fz and meth(fz[0][0], "align-fuzzy") and fz[0][1].get("sentence_id") == "s1" and isnum(fz[0][1].get("match_score")) and 0.6 <= fz[0][1]["match_score"] <= 1.0,
        [(mp.get("method"), sh.get("sentence_id"), sh.get("match_score")) for mp, sh in fz])
    hd = [(mp, sh) for _, mp, sh in rows if mp and sh and "covers the sampling setup" in (mp.get("text") or "")]
    add("M_para_unaligned_aside_holds_previous", hd and meth(hd[0][0], "hold") and nulls(*hd[0]) and hd[0][1].get("sentence_id") == "s1", [(mp.get("method"), sh.get("sentence_id")) for mp, sh in hd])
    add("M_para_hold_keeps_previous_band", hd and fz and same_set(doc_lines(hd[0][1]), doc_lines(fz[0][1]) or [], 0.05) and hd[0][1].get("match_score") is None,
        [(doc_lines(sh), sh.get("match_score")) for _, sh in hd])
    add("M_para_next_verbatim_s2", "s2" in ids_seq(o) and nondecreasing(o)[0], ids_seq(o))
    # skipped s2
    o = S["skip"]; ids = ids_seq(o)
    add("M_skip_s2_never_highlighted", "s2" not in ids and "s3" in ids and nondecreasing(o)[0], ids)
    s3 = [sh for sh in shows(o) if sh.get("sentence_id") == "s3"]
    add("M_skip_s3_band_no_gap_fill", s3 and all(same_set(doc_lines(sh), exp_page_rects(3), 1.0) for sh in s3), [doc_lines(sh) for sh in s3])
    # repeated s1 after s2
    o = S["repeat"]; rows = chunk_rows(o)
    rep = [(mp, sh) for _, mp, sh in rows if mp and sh and (mp.get("text") or "x").strip() in SA(1)]
    rep = rep[-1:] if len(rep) >= 2 else []
    add("M_repeat_holds_s2", rep and meth(rep[0][0], "hold") and rep[0][1].get("sentence_id") == "s2", [(mp.get("method"), sh.get("sentence_id")) for mp, sh in rep])
    add("M_repeat_backref_hold_scores_null", rep and nulls(*rep[0]), [(mp.get("score"), mp.get("runner_up"), sh.get("match_score"), sh.get("runner_up")) for mp, sh in rep])
    add("M_repeat_backref_visible_via_ref_fields", rep and (rep[0][0].get("ref_lines") or rep[0][0].get("ref_trigger")), [(mp.get("ref_lines"), mp.get("ref_trigger")) for mp, _ in rep])
    add("M_repeat_ref_outline_on_s1", rep and rep[0][1].get("ref") not in (None, False, [], {}), [sh.get("ref") for _, sh in rep])
    add("M_repeat_nondecreasing_then_s3", nondecreasing(o)[0] and ids_seq(o)[-1:] == ["s3"], ids_seq(o))
    # reordered s2 before s1
    o = S["reorder"]; rows = [(mp, sh) for _, mp, sh in chunk_rows(o) if mp and sh and (mp.get("text") or "x").strip() in SA(1)]
    add("M_reorder_backref_holds_s2", rows and all(meth(mp, "hold") and nulls(mp, sh) and sh.get("sentence_id") == "s2" for mp, sh in rows), [(mp.get("method"), sh.get("sentence_id"), mp.get("text")) for mp, sh in rows])
    add("M_reorder_never_backwards", any(ids_seq(o)) and nondecreasing(o)[0] and "s1" not in ids_seq(o), ids_seq(o))
    # inserted openers
    o = S["opener"]; rows = [(mp, sh) for _, mp, sh in chunk_rows(o) if mp and sh]
    op = [(mp, sh) for mp, sh in rows if "passage read aloud" in (mp.get("text") or "")]
    if op: add("M_opener_first_chunk_block_null_id", meth(op[0][0], "block") and nulls(*op[0]) and op[0][1].get("sentence_id") is None and same_set(doc_lines(op[0][1]), [page_line(LOC[i])[:4] for i in range(NA)], 1.0),
               (op[0][0].get("method"), op[0][1].get("sentence_id"), len(doc_lines(op[0][1]) or [])))
    else: skip("M_opener_first_chunk_block_null_id", "opener stripped by cleanup")
    mid = [(mp, sh) for mp, sh in rows if "next part now" in (mp.get("text") or "")]
    if mid: add("M_opener_mid_aside_holds_s0", meth(mid[0][0], "hold") and nulls(*mid[0]) and mid[0][1].get("sentence_id") == "s0", (mid[0][0].get("method"), mid[0][1].get("sentence_id")))
    else: skip("M_opener_mid_aside_holds_s0", "aside stripped by cleanup")
    add("M_opener_ids_s0_s1", [x for x in dict.fromkeys(ids_seq(o)) if x] == ["s0", "s1"], ids_seq(o))
    # duplicate sentence text in the selection
    o = S["dup"]; d = [sh for sh in shows(o) if "baseline was recorded twice" in (sh.get("text") or "")]
    add("M_dup_first_copy_then_second", [sh.get("sentence_id") for sh in d] == ["s5", "s7"], [sh.get("sentence_id") for sh in d])
    add("M_dup_bands_on_each_copy", len(d) == 2 and same_set(doc_lines(d[0]), exp_page_rects(5), 1.0) and same_set(doc_lines(d[1]), exp_page_rects(7), 1.0), [doc_lines(x) for x in d])
    o = S["dup2"]; d = [sh for sh in shows(o) if "baseline was recorded twice" in (sh.get("text") or "")]
    add("M_dup_repeated_text_maps_next_occurrence", [sh.get("sentence_id") for sh in d] == ["s5", "s7"] and nondecreasing(o)[0], ids_seq(o))
    add("M_dup2_skipped_s6_never_highlighted", any(ids_seq(o)) and "s6" not in ids_seq(o), ids_seq(o))
    # every scenario: never backwards, ids run-local sN
    for nm in ("full", "para", "skip", "repeat", "reorder", "opener", "dup", "dup2", "default"):
        oo = S[nm][0] if isinstance(S[nm], list) else S[nm]
        add(f"M_{nm}_sentence_id_never_decreases", any(ids_seq(oo)) and nondecreasing(oo)[0] and all(x is None or SID.match(x) for x in ids_seq(oo)), ids_seq(oo))

# ================================================================ C. column break, next page
@section("C")
def columns():
    o = scen()["full"][0]; k = 9
    rows = [sh for sh in shows(o) if sh.get("sentence_id") == f"s{k}"]
    dl = dedup([r for sh in rows for r in (doc_lines(sh) or [])]); W = lines_of(PROSE)["pages"][0]["w"]
    left = [r for r in dl if r[2] <= W / 2 + 5]; right = [r for r in dl if r[0] >= W / 2 - 5]
    add("C_colbreak_two_rect_groups", len(left) >= 1 and len(right) >= 1 and len(left) + len(right) == len(dl), dl)
    add("C_colbreak_no_rect_across_gutter", dl and not any(r[0] < W / 2 - 10 and r[2] > W / 2 + 10 for r in dl), dl)
    add("C_colbreak_one_sentence_id", rows and all(sh.get("sentence_id") == "s9" for sh in rows) and all((sh.get("doc") or {}).get("page") == 1 for sh in rows), [(sh.get("sentence_id"), (sh.get("doc") or {}).get("page")) for sh in rows])
    sr = dedup([r for sh in rows for r in (sh.get("rects") or [])])
    add("C_colbreak_screen_rects_unmerged_3", len(sr) == 3, sr)
    add("C_colbreak_doc_target_is_union", rows and all(near((sh.get("doc") or {}).get("target"), union_xyxy(doc_lines(sh)), 0.2) for sh in rows if doc_lines(sh)), [((sh.get("doc") or {}).get("target"), doc_lines(sh)) for sh in rows][:2])
    # next-page continuation: full selection (page 1 + page 2 lines)
    nxt = SENTS[18][0]; last = SENTS[19][0]; prev = SENTS[17][0]
    o = prose_case("nextpage", " ".join([prev, nxt, last]), sel=range(len(LINES)))
    sh = [x for x in shows(o) if "participants in the study" in (x.get("text") or "")]
    want = [page_line(LOC[NA - 1])[:4]]
    add("C_nextpage_band_current_page_only", sh and all((x.get("doc") or {}).get("page") == 1 and same_set(doc_lines(x), want, 1.0) for x in sh), [((x.get("doc") or {}).get("page"), doc_lines(x)) for x in sh])
    add("C_nextpage_one_sentence_id", sh and len({x.get("sentence_id") for x in sh}) == 1 and sh[0].get("sentence_id") is not None, [x.get("sentence_id") for x in sh])
    p2 = [x for x in shows(o) if "power rose steadily" in (x.get("text") or "")]
    p1rects = [page_line(LOC[i])[:4] for i in range(NA)]
    add("C_page2_sentence_no_page1_rects", p2 and all(not any(any(near(r, q, 1.0) for q in p1rects) for r in (doc_lines(x) or [])) for x in p2), [doc_lines(x) for x in p2])
    add("C_page2_sentence_no_doc_lines_no_target", p2 and all(not doc_lines(x) and not (x.get("doc") or {}).get("target") for x in p2), [(doc_lines(x), (x.get("doc") or {}).get("target")) for x in p2])
    want_s = [o["rects"][o["sel"].index(i)] for i in SENTS[18][1]]
    add("C_nextpage_rects_keep_every_visual_line", sh and same_set(dedup([r for x in sh for r in (x.get("rects") or [])]), want_s, 0.3), ([x.get("rects") for x in sh], want_s))
    add("C_nextpage_nlines_is_len_rects", sh and all(x.get("nlines") == len(x.get("rects") or doc_lines(x) or []) for x in sh), [(x.get("nlines"), len(x.get("rects") or [])) for x in sh])
    add("C_page2_sentence_screen_rects", p2 and all(x.get("rects") for x in p2), [x.get("rects") for x in p2])
    sm = smaps(o); add("C_nextpage_mapped_counts_page_rects_only", bool(sm) and sm[-1].get("mapped") == sm[-1].get("sentences", 0) - 1, [(x.get("sentences"), x.get("mapped"), x.get("method")) for x in sm])

# ================================================================ L. late document
@section("L")
def late_doc():
    wrap = os.path.join(TMPROOT, "slowpy.sh"); open(wrap, "w").write(f"#!/bin/bash\nsleep 3\nexec {PY} \"$@\"\n"); os.chmod(wrap, 0o755)
    o = prose_case("late", " ".join(SA(k) for k in range(8)), warm=False, mock_kw={"MOCK_FIRST_DELAY": "0.05"}, extra_env={"SPEAKEASY_DOC_PYTHON": wrap, "STUB_PLAY_SEC": "0.7"})
    rec = o["rec"] or {}; S_ = shows(o)
    dr, ff = rec.get("doc_ready_ms"), rec.get("first_focus_ms")
    if dr is None or ff is None or dr <= ff: skip("L_late_doc", f"document was not late (doc_ready_ms {dr}, first_focus_ms {ff})"); return
    f = S_[0] if S_ else {}
    add("L_first_show_screen_only", S_ and not doc_lines(f) and not (f.get("doc") or {}).get("target") and f.get("rects"), (f.get("doc"), f.get("rects")))
    add("L_first_show_has_sentence_id", S_ and f.get("sentence_id") == "s0", f.get("sentence_id"))
    rq, ft = rec.get("llm_request_at_ms"), rec.get("llm_first_token_ms")
    fh = ff - (rq + ft) if None not in (ff, rq, ft) else None
    add("L_first_show_immediate_lt_300ms", fh is not None and fh < 300, (ff, rq, ft))
    later = [s for s in S_ if s.get("ts") and s["ts"] > o["t0"] + dr / 1000.0 + 0.8]
    add("L_later_shows_carry_doc_lines", later and all(doc_lines(s) for s in later), [(s.get("sentence_id"), bool(doc_lines(s))) for s in S_])
    sm = smaps(o)
    add("L_sentence_map_written_twice_same_fields", len(sm) == 2 and set(sm[0]) == set(sm[1]) == SM_KEYS, [sorted(x) for x in sm])
    add("L_second_sentence_map_page_rects", len(sm) == 2 and sm[1].get("method") == "page" and sm[1].get("mapped", 0) > 0 and sm[0].get("method") != "page", [(x.get("method"), x.get("mapped")) for x in sm])
    add("L_runs_sentence_map_final", isinstance(rec.get("sentence_map"), dict) and rec["sentence_map"].get("method") == "page", rec.get("sentence_map"))

# ================================================================ S. speech paths
EMAIL = "To: Dr. Patel\nSubject: Zorblax lab meeting\n\nHi Dr. Patel,\n\nCould we move the quintessa meeting to Friday? I will bring the mirevolt results.\n\nBest,\nDonnie"
def visual_case(name, text, script, width=60, mock_kw=None, extra_env=None, base_abs=9000, bin_=CUR):
    """No document; visual-line bounds [{loc, abs, len, rect}] (web view / AX), one rect per visual line, wrapping at `width`."""
    vis, k, pos = [], 0, 0
    for ln in text.split("\n"):
        i = 0
        while i < len(ln):
            j = min(len(ln), i + width)
            if j < len(ln):
                sp = ln.rfind(" ", i, j); j = sp + 1 if sp > i else j
            vis.append({"loc": pos + i, "abs": base_abs + pos + i, "len": j - i, "rect": [100.0, 100.0 + 18 * k, 7.0 * (j - i), 15.0]}); k += 1; i = j
        pos += len(ln) + 1
    kw = dict(MOCK_TEXT=script, MOCK_FIRST_DELAY="0.1", MOCK_TOKEN_DELAY="0.001"); kw.update(mock_kw or {})
    m = Mock(**kw)
    try:
        h = mkhome(name[:10]); lb = os.path.join(os.path.dirname(h), "lb.json"); json.dump(vis, open(lb, "w"))
        o = cli(name, ["speak", "--capture-method", "bounds", f"--capture-rect=84,84,{7 * width + 40},{18 * k + 40}", "--line-bounds", lb], h, base_env(h, m, **(extra_env or {})), inp=text, bin_=bin_)
    finally: m.stop()
    o["vis"] = vis; o["text"] = text; return o
def sent_terms(t): return len(re.findall(r"[.!?][\"')\]]?(?=\s+[A-Z])", (t or "").strip()))

@section("S")
def speech_paths():
    # SPEAKEASY_STREAM=0: each chunk holds at most one output sentence, and carries a span
    o = prose_case("nostream", " ".join(SA(k) for k in range(6)), extra_env={"SPEAKEASY_STREAM": "0"})
    rows = [(mp, sh) for _, mp, sh in chunk_rows(o) if mp]
    add("S_stream0_one_sentence_per_chunk", rows and all(sent_terms(mp.get("text")) == 0 for mp, _ in rows), [mp.get("text") for mp, _ in rows if sent_terms(mp.get("text"))][:2])
    add("S_stream0_chunks_aligned", rows and all(meth(mp, "align") and (sh or {}).get("sentence_id") for mp, sh in rows), [(mp.get("method"), (sh or {}).get("sentence_id")) for mp, sh in rows])
    add("S_stream0_ids_s0_to_s5", [x for x in dict.fromkeys(ids_seq(o)) if x] == [f"s{k}" for k in range(6)], ids_seq(o))
    # stream failure before speech -> non-streamed / fallback path still carries spans
    o = prose_case("streamfail", " ".join(SA(k) for k in range(4)), mock_kw={"MOCK_STREAM_STATUS": "500"})
    rows = [(mp, sh) for _, mp, sh in chunk_rows(o) if mp]
    add("S_streamfail_rc0_and_spoke", o["rc"] == 0 and rows, (o["rc"], o["stderr"][-160:]))
    add("S_streamfail_one_sentence_per_chunk", rows and all(sent_terms(mp.get("text")) == 0 for mp, _ in rows), [mp.get("text") for mp, _ in rows][:3])
    add("S_streamfail_every_chunk_has_sentence_id", rows and all((sh or {}).get("sentence_id") and mp.get("method") in NEW_METHODS for mp, sh in rows), [(mp.get("method"), (sh or {}).get("sentence_id")) for mp, sh in rows])
    add("S_streamfail_never_backwards", any(ids_seq(o)) and nondecreasing(o)[0], ids_seq(o))
    # verbatim email block: framing has no span (block first), body sentences method span, score 1.0
    o = visual_case("email", EMAIL, "unused")
    rows = [(mp, sh) for _, mp, sh in chunk_rows(o) if mp and sh]
    add("S_email_verbatim_backend", (o["rec"] or {}).get("backend") == "verbatim", (o["rec"] or {}).get("backend"))
    fr = [(mp, sh) for mp, sh in rows if re.match(r"here'?s the", (mp.get("text") or "").lower())]
    add("S_email_framing_block_null_id", fr and fr[0][0].get("method") == "block" and fr[0][1].get("sentence_id") is None and nulls(*fr[0]), [(mp.get("method"), sh.get("sentence_id")) for mp, sh in fr])
    body = [(mp, sh) for mp, sh in rows if (mp, sh) not in fr]
    add("S_email_body_method_span_score_1", body and all(mp.get("method") == "span" and mp.get("score") == 1.0 and sh.get("match_score") == 1.0 and SID.match(str(sh.get("sentence_id"))) for mp, sh in body),
        [(mp.get("method"), mp.get("score"), sh.get("sentence_id")) for mp, sh in body])
    add("S_email_never_backwards", any(ids_seq(o)) and nondecreasing(o)[0], ids_seq(o))
    q = [sh for mp, sh in body if "Friday?" in (mp.get("text") or "")]
    if q:
        st = EMAIL.index("Could we"); en = EMAIL.index("Friday?") + len("Friday?")
        rg = q[0].get("range")
        add("S_email_body_range_is_exact_span", isinstance(rg, list) and len(rg) == 2 and rg[0] == 9000 + st and rg[1] in (en - st, en - st - 1), (rg, 9000 + st, en - st))
    else: skip("S_email_body_range_is_exact_span", "no chunk with the Friday sentence")
    # input over CHUNK (6000 chars), not streamed: one map, cursor carries across model calls
    words = ["zorblax", "quintessa", "mirevolt", "plumbrine", "vexhollow", "grelquist", "dunmarrow", "skelvane", "frumplet", "wexmoor"]
    longs = [f"Note {i} says the {words[i % 10]} unit number {i} stayed stable during the trial run in hall {i % 7}." for i in range(1, 90)]
    text = " ".join(longs[:45]) + "\n\n" + " ".join(longs[45:])
    ef = os.path.join(TMPROOT, "echo_long.json"); json.dump(longs, open(ef, "w"))
    o = visual_case("long", text, "unused", width=90, mock_kw={"MOCK_ECHO_FILE": ef}, extra_env={"SPEAKEASY_STREAM": "0", "STUB_PLAY_SEC": "0.01"})
    add("S_long_input_over_6000", len(text) > 6000, len(text))
    sm = smaps(o)
    add("S_long_one_sentence_map", len(sm) == 1 and sm[0].get("sentences") == len(longs), [(x.get("sentences"), x.get("method"), x.get("ms")) for x in sm])
    add("S_long_sentence_map_ms_le_25", bool(sm) and isnum(sm[0].get("ms")) and sm[0]["ms"] <= 25, [x.get("ms") for x in sm])
    ids = [x for x in ids_seq(o) if x]
    add("S_long_cursor_carries_across_calls", nondecreasing(o)[0] and len(set(ids)) >= 80 and ids[-1:] == [f"s{len(longs) - 1}"], (len(set(ids)), ids[:3], ids[-3:]))
    mm = [mp for _, mp, _ in chunk_rows(o) if mp]
    add("S_long_all_chunks_align", mm and all(mp.get("method") == "align" for mp in mm), {k: sum(1 for x in mm if x.get("method") == k) for k in {x.get("method") for x in mm}})
    # focus tags on: valid tag narrows; a backwards tag never moves the band back
    sc = f"[L1-2] {SA(0)} [L2-4] {SA(1)} [L1-1] {SA(2)} [L5-5] {SA(3)}"
    o = prose_case("tags", sc, extra_env={"SPEAKEASY_FOCUS_TAGS": "on"})
    add("S_tags_ids_s0_to_s3", [x for x in dict.fromkeys(ids_seq(o)) if x] == ["s0", "s1", "s2", "s3"] and nondecreasing(o)[0], ids_seq(o))
    add("S_tags_backward_tag_band_on_s2", any(sh.get("sentence_id") == "s2" and same_set(doc_lines(sh), exp_page_rects(2), 1.0) for sh in shows(o)), [(sh.get("sentence_id"), doc_lines(sh)) for sh in shows(o)])
    add("S_tags_not_spoken", not any("[L" in (mp.get("text") or "") for mp in maps(o)), [mp.get("text") for mp in maps(o)][:2])

# ================================================================ X. explain block, caption, table, web view
TABLE = "| Service | Requests |\n|---|---|\n| Zorblax | 42 |\n| Quintessa | 77 |"
@section("X")
def explain_caption_table_web():
    o = visual_case("explain", TABLE, "The table compares two busy services. One of them carries far more traffic overall.", extra_env={"SPEAKEASY_VERBATIM": "off"})
    S_ = shows(o); allr = [v["rect"] for v in o["vis"]]
    add("X_explain_block_null_sentence_id", S_ and all(s.get("sentence_id") is None for s in S_), ids_seq(o))
    add("X_explain_block_every_selection_line", S_ and all(same_set(s.get("rects"), allr, 0.3) for s in S_), [len(s.get("rects") or []) for s in S_])
    add("X_explain_methods_block", maps(o) and all(mp.get("method") in ("block", "hold") for mp in maps(o)) and maps(o)[0].get("method") == "block", [mp.get("method") for mp in maps(o)])
    add("X_explain_no_proportional_walk", len(S_) >= 2 and all(s.get("rect") == S_[0].get("rect") for s in S_), [s.get("rect") for s in S_])
    add("X_explain_scores_null", S_ and all(s.get("match_score") is None and s.get("runner_up") is None for s in S_), [(s.get("match_score"), s.get("runner_up")) for s in S_])
    # fig5.pdf: caption read verbatim, caption explained, table body, prose beside the table
    pg = lines_of(FIG5)["pages"][0]; Ls = pg["lines"]
    def idx(prefix): return next(i for i, l in enumerate(Ls) if l[4].startswith(prefix))
    def fig_case(name, li, script, bin_=CUR, **kw):
        g = geom_for(FIG5, Ls[li[0]][1]); rects = [scr(Ls[i], g) for i in li]; text = "\n".join(Ls[i][4] for i in li)
        m = Mock(MOCK_TEXT=script, MOCK_FIRST_DELAY="0.3", MOCK_TOKEN_DELAY="0.002")
        try:
            h = mkhome(name[:10]); lb = os.path.join(os.path.dirname(h), "lb.json"); u = union_xywh(rects)
            args = ["speak", "--capture-method", "bounds", f"--capture-rect={u[0]-16:.1f},{u[1]-16:.1f},{u[2]+32:.1f},{u[3]+32:.1f}", "--line-bounds", lb, "--doc-hint", hint(h, FIG5)]
            e = base_env(h, m, **kw)
            json.dump(rects, open(lb, "w")); cli(name + "#warm", args, h, e, inp=text, bin_=bin_); shutil.rmtree(os.path.join(h, "logs"), ignore_errors=True)
            json.dump(rects, open(lb, "w")); o = cli(name, args, h, e, inp=text, bin_=bin_)
        finally: m.stop()
        o["li"] = li; return o
    cap = [idx("Fig. 5 |"), idx("a Ear ExG")]
    o = fig_case("capverb", cap, "Fig. 5 | Drowsiness Detector Training and Validation Diagram. a Ear ExG recordings are re-referenced and filtered. b Cross-validation is similar.")
    body = [(mp, sh) for _, mp, sh in chunk_rows(o) if mp and sh and not re.search(r"\b(fig\.?|figure)\s*5", (mp.get("text") or "").lower())]
    add("X_caption_verbatim_text_bands", body and all(sh.get("cue_kind") == "text_band" and (sh.get("doc") or {}).get("kind") == "line" and sh.get("sentence_id") for mp, sh in body),
        [(sh.get("cue_kind"), (sh.get("doc") or {}).get("kind"), sh.get("sentence_id")) for mp, sh in body])
    add("X_caption_verbatim_no_ovals_no_figure_box_on_body", body and not any(sh.get("cue_kind") in ("part_oval", "figure_box") for _, sh in body), [sh.get("cue_kind") for _, sh in body])
    add("X_caption_verbatim_bands_on_caption_lines", body and all(subset(doc_lines(sh), [Ls[i][:4] for i in cap], 1.0) for _, sh in body), [doc_lines(sh) for _, sh in body])
    ab = [sh for mp, sh in body if (mp.get("text") or "").startswith("a Ear")]
    add("X_caption_verbatim_panel_letter_sentence_on_line_2", ab and all(same_set(doc_lines(sh), [Ls[cap[1]][:4]], 1.0) for sh in ab), [doc_lines(sh) for sh in ab])
    o = fig_case("capexpl", cap, "Figure 5 shows the training and validation pipeline. Panel a shows how the recordings are cleaned before training.")
    ck = [sh.get("cue_kind") for sh in shows(o)]
    add("X_caption_explained_gets_figure_cue", ck and ck[0] != "text_band" and (shows(o)[0].get("doc") or {}).get("kind") not in (None, "line"), (ck, [(sh.get("doc") or {}).get("kind") for sh in shows(o)]))
    add("X_caption_explained_cue_has_null_scores", shows(o) and shows(o)[0].get("match_score") is None, [(sh.get("sentence_id"), sh.get("match_score")) for sh in shows(o)][:2])
    tb = [idx(a) for a in ("Maximum Recording", "Recording Channels Used", "ADC Resolution", "Noise Floor")]
    expl = "Maximum recording channels come to sixty-four in total. The ADC Resolution row lists fifteen bits. The Noise Floor row lists seventy nanovolts."
    o = fig_case("tbody", tb, expl); ob = fig_case("tbody_base", tb, expl, bin_=BIN_BASE)
    kc = [sh.get("cue_kind") for sh in shows(o)]; kb = [sh.get("cue_kind") for sh in shows(ob)]
    add("X_table_body_cues_stay_table_cues", kc and set(kc) <= {"table_row", "figure_box"} and set(kb) <= {"table_row", "figure_box"} and all((s.get("doc") or {}).get("kind") in ("table", "table_row") for s in shows(o)), (kc, kb, [(s.get("doc") or {}).get("kind") for s in shows(o)]))
    if "table_row" in kb: add("X_table_body_gets_table_row_bands", "table_row" in kc, kc)
    else: skip("X_table_body_gets_table_row_bands", f"baseline gave no table_row cue for this explanation {kb}")
    right = [i for i, l in enumerate(Ls) if l[4].startswith("Right column sentence")][:4]
    o = fig_case("beside", right, " ".join(Ls[i][4] for i in right))
    S_ = shows(o)
    add("X_prose_beside_table_text_bands", S_ and all(s.get("cue_kind") == "text_band" and (s.get("doc") or {}).get("kind") == "line" for s in S_), [s.get("cue_kind") for s in S_])
    add("X_prose_beside_table_each_own_line", S_ and all(same_set(doc_lines(s), [Ls[right[k]][:4]], 1.0) for k, s in enumerate(S_[:4])) and ids_seq(o)[:4] == ["s0", "s1", "s2", "s3"], [(s.get("sentence_id"), doc_lines(s)) for s in S_])
    # web view, wrapped paragraph: one logical line, 5 visual lines; rects only the span's visual lines; range exact
    P = ("Zorblax units route traffic between the hosts in the lab. Each quintessa unit has two ports and one fan that hums. "
         "They were installed in May by the mirevolt team after a long delay. Plumbrine checks ran every night.")
    sents = re.findall(r"[^.]+\.\s?", P)
    def nvis(w):
        n, i = 0, 0
        while i < len(P):
            j = min(len(P), i + w)
            if j < len(P): sp = P.rfind(" ", i, j); j = sp + 1 if sp > i else j
            n += 1; i = j
        return n
    o = visual_case("web", P, " ".join(s.strip() for s in sents), width=next(w for w in range(30, 120) if nvis(w) == 5))
    add("X_web_five_visual_lines", len(o["vis"]) == 5, len(o["vis"]))
    for k, s in enumerate(sents):
        st = P.index(s.strip()); en = st + len(s.strip())
        want = [v["rect"] for v in o["vis"] if v["loc"] < en and v["loc"] + v["len"] > st]
        sh = [x for x in shows(o) if x.get("sentence_id") == f"s{k}"]
        if not sh: add(f"X_web_s{k}", False, ("no show", ids_seq(o))); continue
        add(f"X_web_s{k}_rects_unmerged_span_lines", len(sh) == 1 and same_set(sh[0].get("rects"), want, 0.05) and sh[0].get("nlines") == len(want), (sh[0].get("rects"), want, sh[0].get("nlines")))
        rg = sh[0].get("range")
        add(f"X_web_s{k}_range_exact", len(sh) == 1 and isinstance(rg, list) and len(rg) == 2 and rg[0] == 9000 + st and rg[1] in (en - st, en - st - 1), (rg, [9000 + st, en - st]))
        add(f"X_web_s{k}_rect_is_union", near(sh[0].get("rect"), union_xywh(sh[0].get("rects") or [[0, 0, 0, 0]]), 0.2), (sh[0].get("rect"), sh[0].get("rects")))
    sm = smaps(o); add("X_web_sentence_map_ax_lines", bool(sm) and sm[0].get("method") == "ax-lines" and sm[0].get("mapped") == len(sents), [(x.get("method"), x.get("mapped")) for x in sm])

# ================================================================ T. tracker
def tracker_run(exe, tag, show, secs=4):
    T = os.path.join(TMPROOT, "trkrun"); h = os.path.join(T, f"home_{tag}"); os.makedirs(os.path.join(h, "logs"), exist_ok=True)
    parent = subprocess.Popen(["sleep", str(secs)]); open(os.path.join(h, "speak.pid"), "w").write(str(parent.pid))
    json.dump(dict(show, pid=parent.pid, ts=time.time()), open(os.path.join(h, "focus.json"), "w"))
    tp = subprocess.Popen([exe, "--window-id", "999999", "--pid", str(parent.pid), "--home", h, "--fps", "5"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try: tp.wait(25)
    except subprocess.TimeoutExpired: tp.send_signal(signal.SIGTERM); tp.wait(10)
    parent.wait(); return [x for x in jl(os.path.join(h, "logs", "tracker.jsonl")) if x.get("event") == "draw"]

@section("T")
def tracker_checks():
    if A.no_tracker: skip("T_tracker", "--no-tracker"); return
    pkg, why = tracker("cur")
    if not pkg: add("T_tracker_builds", False, why); return
    rc, mx, out = swift_test(pkg)
    add("T_swift_test_existing_plus_new_pass", rc == 0 and mx and int(mx[0]) > 10 and mx[1] == "0", (rc, mx, re.findall(r"error: .*", out)[:3]))
    pf = os.path.join(TMPROOT, "pf.swift"); open(pf, "w").write('import CoreGraphics\nprint("\\(CGPreflightScreenCaptureAccess())")\n')
    subprocess.run(["swiftc", "-O", pf, "-o", pf[:-6]], capture_output=True, timeout=300)
    pre = subprocess.run([pf[:-6]], capture_output=True, text=True).stdout.strip() if os.path.exists(pf[:-6]) else "false"
    if pre != "true": skip("T_tracker_draws", "Screen Recording not granted to this process"); return
    exe = os.path.join(pkg, ".build", "debug", "speakeasy-track")
    bpkg, bwhy = tracker("base"); bexe = os.path.join(bpkg, ".build", "debug", "speakeasy-track") if bpkg else None
    S, OX, OY = 1.5, 100.0, 120.0
    def to_scr(r): return [round(OX + r[0] * S, 1), round(OY + r[1] * S, 1), round((r[2] - r[0]) * S, 1), round((r[3] - r[1]) * S, 1)]
    anchors = [[40.0, 40.0 + 12 * i, 280.0, 48.0 + 12 * i, f"Anchor prose line {i} zorblax"] for i in range(4)]
    def band_show(lines, sid="s4", with_rects=True):
        u = union_xyxy(lines); sr = [to_scr(r) for r in lines]
        d = {"state": "show", "seq": 1, "rect": union_xywh(sr), "color": "#3B82F6", "opacity": 0.22, "src": ["x"], "range": None, "nlines": len(lines), "cue": "text", "cue_kind": "text_band",
             "draw": True, "sent": 0, "sentence_id": sid, "match_score": 1.0, "runner_up": None, "doc_transform": [S, OX, OY],
             "doc": {"kind": "line", "label": None, "page": 1, "page_size": [595.3, 841.9], "target": u, "target_src": "lines", "lines": lines, "anchors": anchors}}
        if with_rects: d["rects"] = sr
        return d
    three = [[40.0, 100.0, 280.0, 108.0], [40.0, 112.0, 280.0, 120.0], [40.0, 124.0, 200.0, 132.0]]
    D = tracker_run(exe, "three", band_show(three))
    if not D: skip("T_tracker_draws", "tracker wrote no draw records headless"); return
    add("T_doc_lines_draw_line_count_3", all(x.get("line_count") == 3 for x in D), [x.get("line_count") for x in D][:5])
    add("T_doc_lines_draw_sentence_id", all(x.get("sentence_id") == "s4" for x in D), [x.get("sentence_id") for x in D][:3])
    ev = [x for x in D if x.get("page_fit") in ("event-transform", "anchors")]
    add("T_doc_lines_one_transform_page_rect_union", ev and all(near(x.get("page_rect"), union_xyxy(three), 1.0) for x in ev), [x.get("page_rect") for x in ev][:3])
    add("T_doc_lines_text_rect_union_of_mapped", all(near(x.get("text_rect"), union_xywh([to_scr(r) for r in three]), 2.0) for x in D if x.get("src") != "scroll-delta"), [x.get("text_rect") for x in D][:3])
    add("T_doc_lines_never_ocr_union", not any(x.get("src") == "ocr" for x in D), [x.get("src") for x in D])
    D = tracker_run(exe, "docl_only", band_show(three, with_rects=False))
    add("T_doc_lines_without_rects_draws_3", D and all(x.get("line_count") == 3 for x in D), [x.get("line_count") for x in D][:4])
    cols = [[40.0, 160.0, 280.0, 168.0], [310.0, 40.0, 550.0, 48.0]]
    D = tracker_run(exe, "cols", band_show(cols, "s9"))
    add("T_column_break_two_rects", D and all(x.get("line_count") == 2 for x in D), [x.get("line_count") for x in D][:4])
    # legacy single-rect events: unchanged vs the baseline tracker
    TGT = [40.7, 77.4, 352.4, 85.8]; sc = to_scr(TGT)
    legacy = {"state": "show", "seq": 1, "rect": sc, "color": "#3B82F6", "opacity": 0.22, "src": ["Wexmoor zorblax line"], "range": None, "nlines": 1, "cue": "text", "cue_kind": "text_band", "draw": True, "sent": 0}
    leg_doc = dict(legacy, doc={"kind": "figure", "label": "Figure 1", "page": 1, "page_size": [595.3, 841.9], "target": TGT, "target_src": "figure:graphics", "anchors": anchors}, doc_transform=[S, OX, OY], cue="figure", cue_kind="figure_box")
    for tag, sh in (("legacy_plain", legacy), ("legacy_figure", leg_doc)):
        Dc = tracker_run(exe, tag, sh)
        Db = tracker_run(bexe, tag + "_base", sh) if bexe else None
        if Db == []: Db = tracker_run(bexe, tag + "_base2", sh)
        if Dc == []: Dc = tracker_run(exe, tag + "_2", sh)
        if Db == [] or Dc == []: skip(f"T_{tag}_first_draw_matches_baseline", f"no draw records headless (cur {len(Dc)}, base {len(Db)})"); continue
        add(f"T_{tag}_line_count_1_or_absent", Dc and all(x.get("line_count") in (None, 1) for x in Dc), [x.get("line_count") for x in Dc][:3])
        if Db is None: skip(f"T_{tag}_matches_baseline", bwhy); continue
        def sig(D_): return [(x.get("text_rect"), x.get("page_rect"), x.get("page_fit")) for x in D_[:1]]
        add(f"T_{tag}_first_draw_matches_baseline", all(near(a, b, 1.0) if rect_ok(a) else a == b for x, y in zip(sig(Dc), sig(Db)) for a, b in zip(x, y)), (sig(Dc), sig(Db)))
        kb = set().union(*[set(x) for x in Db]) if Db else set(); kc = set().union(*[set(x) for x in Dc]) if Dc else set()
        add(f"T_{tag}_draw_keys_superset_of_baseline", kb <= kc, sorted(kb - kc))
    add("T_tracker_log_no_sentinel_text", not any(sentinel_hits(open(f).read()) for f in glob.glob(os.path.join(TMPROOT, "trkrun", "*", "logs", "tracker.jsonl"))), "")

# ================================================================ P. privacy (SPEAKEASY_LOG_TEXT unset)
NEW_SHOW = ("sentence_id", "match_score", "runner_up", "rects")
@section("P")
def privacy():
    o = scen()["default"]
    add("P_default_run_has_sentence_ids", any(ids_seq(o)), ids_seq(o))
    leaks = []
    for r in o["focus"]:
        if r.get("state") == "sentence_map": leaks += [("sentence_map", w) for w in sentinel_hits(json.dumps(r))]
        if r.get("state") == "show":
            leaks += [("show." + k, w) for k in NEW_SHOW for w in sentinel_hits(json.dumps(r.get(k)))]
            leaks += [("show.doc.lines", w) for w in sentinel_hits(json.dumps(doc_lines(r)))]
        if r.get("state") == "map": leaks += [("map." + k, w) for k in ("sentence_id", "sentence_ids") for w in sentinel_hits(json.dumps(r.get(k)))]
    add("P_no_text_in_new_fields", not leaks, leaks[:4])
    add("P_runs_sentence_map_no_text", o["rec"] and not sentinel_hits(json.dumps((o["rec"] or {}).get("sentence_map"))), (o["rec"] or {}).get("sentence_map"))
    lg = {f: open(os.path.join(o["home"], "logs", f)).read() for f in ("runs.jsonl", "focus.jsonl") if os.path.exists(os.path.join(o["home"], "logs", f))}
    add("P_default_logs_no_sentinel", lg and not any(sentinel_hits(t) for t in lg.values()), {f: sentinel_hits(t) for f, t in lg.items()})
    add("P_ids_never_derived_from_text", all(x is None or SID.match(x) for oo in ALL for x in ids_seq(oo)), "")
    pa = subprocess.run([PY, os.path.join(REPO, "tests", "audit", "privacy_audit.py"), "--image", BLANK_PNG, "--thumb", os.path.join(TMPROOT, "thumb.png")], capture_output=True, text=True, env=env_for(o["home"]), timeout=120)
    add("P_privacy_audit_clean", "PRIVACY_LEAK" not in pa.stdout and pa.returncode == 0, (pa.returncode, pa.stdout[-200:], pa.stderr[-160:]))

# ================================================================ K. key sets, formats, live focus.json, perf
@section("K")
def keysets():
    scen(); smr = [(oo["name"], r) for oo in ALL for r in smaps(oo)]
    add("K_sentence_map_records_exist", len(smr) >= 5, len(smr))
    add("K_sentence_map_exact_keys", smr and all(set(r) == SM_KEYS for _, r in smr), [(n, sorted(set(r) ^ SM_KEYS)) for n, r in smr if set(r) != SM_KEYS][:3])
    add("K_sentence_map_method_enum_int_ms", smr and all(r.get("method") in SM_METHODS and isinstance(r.get("ms"), int) and isinstance(r.get("sentences"), int) and isinstance(r.get("mapped"), int) and 0 <= r["mapped"] <= r["sentences"] for _, r in smr),
        [(r.get("method"), r.get("ms")) for _, r in smr][:4])
    rr = [(oo["name"], oo["rec"]) for oo in ALL if oo["rec"] and smaps(oo) and not oo["name"].endswith("#warm")]
    add("K_runs_sentence_map_exact_keys", rr and all(isinstance(r.get("sentence_map"), dict) and set(r["sentence_map"]) == RUN_SM_KEYS for _, r in rr), [(n, r.get("sentence_map")) for n, r in rr if not (isinstance(r.get("sentence_map"), dict) and set(r["sentence_map"]) == RUN_SM_KEYS)][:3])
    rs = [(oo["name"], oo["rec"].get("sentence_map"), {k: smaps(oo)[-1].get(k) for k in RUN_SM_KEYS}) for oo in ALL if oo["rec"] and smaps(oo) and not oo["name"].endswith("#warm")]
    add("K_runs_sentence_map_equals_last_record", rs and all(a == b for _, a, b in rs), [x for x in rs if x[1] != x[2]][:2])
    add("K_sentence_map_never_in_live_focus_json", not any(x.get("state") == "sentence_map" or "sentences" in x for oo in ALL for x in oo["live"]), "")
    shs = [(oo["name"], s) for oo in ALL for s in shows(oo) if smaps(oo)]
    add("K_show_sentence_id_always_present", shs and all("sentence_id" in s and "match_score" in s and "runner_up" in s for _, s in shs), [(n, sorted(s)) for n, s in shs if "sentence_id" not in s][:2])
    add("K_show_scores_2dp_or_null", bool(shs) and all((s.get(k) is None or (r2(s[k]) and 0 <= s[k] <= 1)) for _, s in shs for k in ("match_score", "runner_up")), [(s.get("match_score"), s.get("runner_up")) for _, s in shs if not all(s.get(k) is None or r2(s[k]) for k in ("match_score", "runner_up"))][:3])
    wr = [(n, s) for n, s in shs if s.get("rects")]
    add("K_show_rect_is_union_of_rects", wr and all(near(s.get("rect"), union_xywh(s["rects"]), 0.25) for _, s in wr), [(s.get("rect"), s.get("rects")) for _, s in wr if not near(s.get("rect"), union_xywh(s["rects"]), 0.25)][:2])
    add("K_show_nlines_is_len_rects", wr and all(s.get("nlines") == len(s["rects"]) for _, s in wr), [(n, s.get("sentence_id"), s.get("nlines"), len(s["rects"])) for n, s in wr if s.get("nlines") != len(s["rects"])][:3])
    wo = [(n, s) for n, s in shs if not s.get("rects") and doc_lines(s)]
    if wo: add("K_show_nlines_is_len_doc_lines_without_rects", all(s.get("nlines") == len(doc_lines(s)) for _, s in wo), [(n, s.get("nlines"), len(doc_lines(s))) for n, s in wo if s.get("nlines") != len(doc_lines(s))][:3])
    else: skip("K_show_nlines_is_len_doc_lines_without_rects", "no show with doc.lines and no rects")
    wd = [(n, s) for n, s in shs if doc_lines(s)]
    add("K_show_doc_target_is_union_of_doc_lines", wd and all(near((s.get("doc") or {}).get("target"), union_xyxy(doc_lines(s)), 0.2) for _, s in wd), "")
    mps = [(oo["name"], r) for oo in ALL for r in maps(oo) if smaps(oo)]
    add("K_map_methods_in_enum", mps and all(r.get("method") in NEW_METHODS or (str(r.get("method", "")).startswith("doc-") and r.get("method") != "doc-line") for _, r in mps), sorted({r.get("method") for _, r in mps}))
    tb = [(oo["name"], last_map(oo).get(s.get("sent")), s) for oo in ALL if smaps(oo) for s in shows(oo) if s.get("cue_kind") == "text_band"]
    add("K_text_band_chunks_use_align_family", tb and all((mp or {}).get("method") in NEW_METHODS for _, mp, _ in tb), sorted({(n.split("#")[0], (mp or {}).get("method")) for n, mp, _ in tb if (mp or {}).get("method") not in NEW_METHODS})[:6])
    hb = [(n, mp, s) for n, mp, s in tb if (mp or {}).get("method") in ("hold", "block")]
    add("K_hold_block_scores_all_null", hb and all(nulls(mp, s) for _, mp, s in hb), [(n, mp.get("method"), mp.get("score"), mp.get("runner_up"), s.get("match_score"), s.get("runner_up")) for n, mp, s in hb if not nulls(mp, s)][:3])
    al = [(n, mp, s) for n, mp, s in tb if (mp or {}).get("method") in ("align", "align-fuzzy", "span")]
    add("K_align_show_scores_equal_map_scores", al and all(s.get("match_score") == mp.get("score") and s.get("runner_up") == mp.get("runner_up") for _, mp, s in al), [(n, mp.get("score"), s.get("match_score"), mp.get("runner_up"), s.get("runner_up")) for n, mp, s in al if not (s.get("match_score") == mp.get("score") and s.get("runner_up") == mp.get("runner_up"))][:3])
    pt = [s for _, _, s in tb if doc_lines(s)]
    add("K_page_target_band_shown_by_doc_kind_line_target_src_lines", pt and all((s.get("doc") or {}).get("kind") == "line" and (s.get("doc") or {}).get("target_src") == "lines" for s in pt), "")
    add("K_map_lines_1_based_range", all(isinstance(r.get("lines"), list) and len(r["lines"]) == 2 and r["lines"][0] >= 1 for _, r in mps if r.get("lines") is not None), "")
    fm = set().union(*[set((r.get("focus_methods") or {})) for _, r in rr]) if rr else set()
    add("K_runs_focus_methods_gain_new_values", fm & NEW_METHODS, sorted(fm))
    live = [x for oo in ALL for x in oo["live"] if x.get("state") == "show" and smaps(oo)]
    add("K_live_focus_json_carries_sentence_id", live and all("sentence_id" in x for x in live), len(live))
    schema = open(os.path.join(TREE, "tests", "audit", "SCHEMA.md")).read()
    add("K_schema_md_documents_runs_sentence_map", "sentence_map" in schema, "")
    ms = sorted(r["ms"] for _, r in smr if isnum(r.get("ms")))
    p95 = ms[max(0, math.ceil(0.95 * len(ms)) - 1)] if ms else None
    add("K_sentence_map_ms_p95_le_25", p95 is not None and p95 <= 25, (p95, len(ms)))

# ================================================================ D. determinism
@section("D")
def determinism():
    a, b = scen()["full"]
    def sig(o): return [(s.get("sentence_id"), s.get("match_score"), s.get("runner_up"), doc_lines(s), s.get("rects"), s.get("range"), s.get("cue_kind")) for s in shows(o)]
    def msig(o): return [(r.get("method"), r.get("sentence_id"), r.get("sentence_ids"), r.get("score"), r.get("runner_up")) for r in maps(o)]
    add("D_two_runs_identical_shows", any(ids_seq(a)) and sig(a) == sig(b), [i for i, (x, y) in enumerate(zip(sig(a), sig(b))) if x != y][:4])
    add("D_two_runs_identical_maps", any(r.get("sentence_id") for r in maps(a)) and msig(a) == msig(b), "")
    add("D_two_runs_identical_sentence_map_counts", bool(smaps(a)) and [(x.get("sentences"), x.get("mapped"), x.get("method")) for x in smaps(a)] == [(x.get("sentences"), x.get("mapped"), x.get("method")) for x in smaps(b)], "")

# ================================================================ run
T0 = time.time()
# fixture sanity: drawn lines line up with the parse
pl = [page_line(LOC[i])[4] for i in range(len(LINES))]
add("Z_fixture_lines_match_parse", all(a[:12].replace("\ufb01", "fi") == b[:12].replace("\ufb01", "fi") for a, b in zip(LINES, pl)), [(a[:12], b[:12]) for a, b in zip(LINES, pl) if a[:12] != b[:12]][:3])
for fn in (fold_checks, misbehaviour, columns, late_doc, speech_paths, explain_caption_table_web, tracker_checks, privacy, keysets, determinism): fn()
for f in glob.glob(os.path.join(tempfile.gettempdir(), "speakeasy_cap_p3*")):
    try: os.remove(f)
    except Exception: pass
add("Z_cli_runs_exit_0", all(o["rc"] == 0 for o in ALL), [(o["name"], o["rc"], o["stderr"][-120:]) for o in ALL if o["rc"] != 0][:4])
add("Z_never_touched_real_speakeasy_home", not any(os.path.realpath(o["home"]).startswith(L.REAL_HOME) for o in ALL), "")
if A.keep or os.environ.get("P3_KEEP") == "1": print(f"# kept {TMPROOT}")
else: shutil.rmtree(TMPROOT, ignore_errors=True)
n = {k: sum(1 for s, _ in R if s == k) for k in ("PASS", "FAIL", "SKIP")}
print(f"SUMMARY checks={len(R)} pass={n['PASS']} fail={n['FAIL']} skip={n['SKIP']} runs={len(ALL)} secs={int(time.time() - T0)}", flush=True)
sys.exit(0 if n["FAIL"] == 0 else 1)
