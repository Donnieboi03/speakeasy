#!/usr/bin/env python3
"""Phase 2 three-score benchmark (boxes, lines, parts + speed). Contract: docs/contracts/phase2-benchmark.md; app-side fields:
docs/contracts/phase2-logging.md. Black box: drives `src/docmap.py parse-pdf` and the `speakeasy speak` CLI as subprocesses
and scores their outputs and logs; it never imports app code.

Usage (run under a Python that has pypdfium2, e.g. the doc venv; that interpreter is also passed to the app as
SPEAKEASY_DOC_PYTHON):

    python3 tests/audit/bench/run_bench.py [--set public|hidden|all] [--holdout DIR] [--out FILE] [--cases GLOB]
                                           [--record-baseline] [--check-determinism] [--keep-temp]
    python3 tests/audit/bench/bench_gate.py tests/audit/reports/bench.json [--public-only]

Scores
  boxes   docmap parse-pdf `pages[].blocks` vs GT blocks (paragraph, caption, figure, table). Hit = greedy one-to-one match at
          IoU >= 0.75; precision/recall at IoU >= 0.5 (unmatched predictions on header/footer/heading/... regions ignored).
  lines   per read-through sentence: every chunk's last `show` is a `text_band` with a page-space `doc.target` on the right page,
          the targets cover >= ceil(0.75 n) of the sentence's lines, and no target covers more than 1 other page line.
  parts   per sentence naming a figure part: every `part_oval` show of its chunks has its centre inside the part's bbox.
          parts.precision = good ovals / all ovals (stray ovals lower it).
  speed   first_highlight_ms = first_focus_ms - (llm_request_at_ms + llm_first_token_ms), p50/p95; budget < 300 ms.

Each case runs in a fresh temp home (never ~/.speakeasy) with a scrubbed environment, a mock OpenAI-compatible server on an
ephemeral port (one word per token), stubbed `say`, no images, no OCR, no grounding. A warm-up run fills the parse cache;
the measured run must have doc_ready_ms <= first_sentence_ms (retried up to 2 times). Chunks are attributed to script
sentences by text position (focus-log map records, text logging on in the temp home only).

Fixtures: tests/audit/bench/fixtures/public (committed) and <holdout>/fixtures (hidden, never in the repo). Rebuild with
gen/make_fixtures.py [--set hidden] (--check diffs against committed output); validate with validate_fixtures.py.
Output: bench.json (ids, numbers, enums and hashes only); the hidden set carries aggregates only and its per-case detail
goes to <holdout>/last_cases.json. Baselines are written only by --record-baseline.

Exit codes: 0 bench written; 2 preflight/usage/fixture error (nothing run); 3 harness error (bench.json written with
complete: false), including app output that lacks the logging-contract fields; 4 determinism mismatch."""
import argparse, datetime, fnmatch, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__)); AUD = os.path.dirname(HERE); REPO = os.path.dirname(os.path.dirname(AUD))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(AUD, "boxacc"))
import score as S
from validate_fixtures import validate_set, cross_check, set_paths
from geom import page_to_screen, xywh, K

BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(REPO, "src", "speakeasy"))
DOCMAP = os.path.join(REPO, "src", "docmap.py")
MOCK = os.path.join(AUD, "stream", "mock_llm.py")
STUBS = [os.path.join(AUD, "nosel", "stubs"), os.path.join(AUD, "tts", "stubs")]
GEOM = {"zoom": 1.25, "backing": 2, "display": [0, 33]}
SCREEN_W, FIRST_LINE_Y, PAD = 1512, 300, 16
MOCK_FIRST, MOCK_TOKEN, STUB_PLAY = 0.5, 0.002, 0.05
RUN_TIMEOUT = 60
REAL_HOME = os.path.realpath(os.path.expanduser("~/.speakeasy"))


class Harness(Exception):
    def __init__(self, code, message, case_id=None, detail=""):
        super().__init__(message); self.code, self.message, self.case_id, self.detail = code, message, case_id, detail


class FieldsMissing(Harness):
    pass


def log(msg): print(msg, file=sys.stderr, flush=True)


def jl(p):
    return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()] if os.path.exists(p) else []


def atomic_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, indent=1); f.write("\n")
    os.replace(tmp, path)


def versions():
    out = {"python": sys.version.split()[0]}
    from importlib import metadata
    for m in ("pypdfium2", "reportlab", "pdfplumber"):
        try: out[m] = metadata.version(m)
        except metadata.PackageNotFoundError: out[m] = None
    return out


def git_state():
    try:
        sha = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "-C", REPO, "status", "--porcelain"], capture_output=True, text=True, timeout=10).stdout.strip())
        return sha, dirty
    except Exception:
        return None, None


def config():
    c = dict(S.CONFIG); c.update(mock_first_delay_s=MOCK_FIRST, mock_token_delay_s=MOCK_TOKEN, stub_play_s=STUB_PLAY, geometry=dict(GEOM))
    return c


# ------------------------------------------------------------------ environment
class Env:
    def __init__(self, root, port, interp):
        self.root, self.port, self.interp, self.n = root, port, interp, 0

    def home(self, tag):
        self.n += 1; d = os.path.join(self.root, "runs", f"{self.n:04d}-{tag}"); h = os.path.join(d, "home"); t = os.path.join(d, "tmp")
        os.makedirs(h); os.makedirs(t)
        rh = os.path.realpath(h)
        if not rh.startswith(os.path.realpath(self.root) + os.sep) or rh == REAL_HOME or rh.startswith(REAL_HOME + os.sep):
            raise Harness("isolation", "temp home escaped the temp root")
        return d, h, t

    def env(self, home, tmpd):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("SPEAKEASY_", "MOCK_", "STUB_"))}
        e.update(SPEAKEASY_HOME=home, HOME=home, TMPDIR=tmpd, SPEAKEASY_LLM_URL=f"http://127.0.0.1:{self.port}/v1", SPEAKEASY_MODEL="mock-text",
                 SPEAKEASY_LLM="local", SPEAKEASY_VISION="never", SPEAKEASY_GROUND="off", SPEAKEASY_TTS="say", SPEAKEASY_DOC_READ="local",
                 SPEAKEASY_DOC_PYTHON=self.interp, SPEAKEASY_TRACKER=os.path.join(self.root, "no-tracker", "speakeasy-track"),
                 SPEAKEASY_FOCUS_TAGS="off", SPEAKEASY_STREAM="1", SPEAKEASY_CAPTURE_FIT="off", SPEAKEASY_TEXT_HIGHLIGHT="on",
                 SPEAKEASY_FIGURE_CUES="on", SPEAKEASY_KEEP_CAPTURE="0", SPEAKEASY_LOG_TEXT="1", STUB_PLAY_SEC=str(STUB_PLAY),
                 PATH=":".join(STUBS + [os.environ.get("PATH", "/usr/bin:/bin")]))
        return e


class Mock:
    def __init__(self, root):
        self.port_file = os.path.join(root, "mock.port"); self.log = os.path.join(root, "mock.jsonl"); self.reply = os.path.join(root, "reply.txt")
        open(self.reply, "w").close(); open(self.log, "w").close()
        e = {k: v for k, v in os.environ.items() if not k.startswith(("MOCK_", "SPEAKEASY_"))}
        e.update(MOCK_PORT="0", MOCK_PORT_FILE=self.port_file, MOCK_LOG=self.log, MOCK_REPLY_FILE=self.reply,
                 MOCK_FIRST_DELAY=str(MOCK_FIRST), MOCK_TOKEN_DELAY=str(MOCK_TOKEN))
        self.p = subprocess.Popen([sys.executable, MOCK], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.port = None; t0 = time.time()
        while time.time() - t0 < 5:
            if self.port is None and os.path.exists(self.port_file): self.port = int(open(self.port_file).read())
            if self.port:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models", timeout=1).read(); return
                except Exception: pass
            time.sleep(0.05)
        self.stop(); raise Harness("mock_not_ready", "mock LLM did not start")

    def chats(self):
        return sum(1 for r in jl(self.log) if r.get("method") == "POST" and str(r.get("path", "")).rstrip("/").endswith("chat/completions"))

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try: self.p.wait(5)
            except subprocess.TimeoutExpired: self.p.kill()


# ------------------------------------------------------------------ predictions (5.1)
def parse_doc(env, doc_id, pdf):
    d, h, t = env.home("parse")
    cp = os.path.join(d, "doc.pdf"); shutil.copyfile(pdf, cp); out = os.path.join(d, "parse.json")
    try:
        p = subprocess.run([env.interp, DOCMAP, "parse-pdf", cp, out], capture_output=True, text=True, env=env.env(h, t), timeout=180)
    except subprocess.TimeoutExpired:
        raise Harness("parse_failed", f"docmap.py parse-pdf timed out ({doc_id})", None)
    if p.returncode != 0 or not os.path.exists(out):
        last = (p.stderr.strip().splitlines() or [""])[-1]
        raise Harness("parse_failed", f"docmap.py parse-pdf failed ({doc_id})", None, last)
    data = json.load(open(out))
    pages = data.get("pages") or []
    if isinstance(pages, dict): pages = list(pages.values())
    preds, figs_done, with_blocks = {}, 0, 0
    for pg in pages:
        figs_done += bool(pg.get("figs_done")); with_blocks += isinstance(pg.get("blocks"), list)
        preds[pg.get("n")] = [{"kind": b.get("kind"), "bbox": b.get("bbox")} for b in (pg.get("blocks") or []) if isinstance(b.get("bbox"), list)]
    if figs_done and not with_blocks:
        raise FieldsMissing("contract_fields_missing", f"parse output has no pages[].blocks on figs_done pages ({doc_id}): logging contract D not implemented")
    return preds


# ------------------------------------------------------------------ case runs (5.2)
def geometry_for(case, page_w):
    s = GEOM["zoom"] * K; first = case["selection"][0]["rect"]
    g = {"zoom": GEOM["zoom"], "backing": GEOM["backing"], "ox": GEOM["display"][0] + (SCREEN_W - page_w * s) / 2, "oy": FIRST_LINE_Y - first[1] * s}
    rs = [it["rect"] for it in case["selection"]]
    u = page_to_screen([min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)], g)
    cap = [u[0] - PAD, u[1] - PAD, u[2] - u[0] + 2 * PAD, u[3] - u[1] + 2 * PAD]
    lb = [[round(v, 1) for v in xywh(page_to_screen(r, g))] for r in rs]
    return ",".join(f"{v:.1f}" for v in cap), lb


RUN_KEYS = ("first_focus_ms", "doc_ready_ms")


def check_fields(rec, focus):
    miss = [f"runs.{k} (logging contract C)" for k in RUN_KEYS if k not in rec]
    maps = [r for r in focus if r.get("state") == "map"]; shows = [r for r in focus if r.get("state") == "show"]
    if any("sent" not in r for r in maps): miss.append("focus map.sent (logging contract A)")
    if any("sent" not in r for r in shows): miss.append("focus show.sent (logging contract B)")
    if any("text" not in r for r in maps): miss.append("focus map.text (SPEAKEASY_LOG_TEXT=1 not honoured)")
    if miss: raise FieldsMissing("contract_fields_missing", "app output lacks logging-contract fields: " + ", ".join(miss))


def run_case(env, mock, case, pdf, page_w, idx):
    cid = case["case_id"]; d, h, t = env.home("case")
    cp = os.path.join(d, f"{case['doc_id']}.pdf"); shutil.copyfile(pdf, cp)
    hint = os.path.join(d, "hint.json"); lbp = os.path.join(d, "lines.json")
    json.dump({"app": "Preview", "bundle": "com.apple.Preview", "title": f"{case['doc_id']}.pdf", "ax_document": "file://" + cp}, open(hint, "w"))
    cap, lb = geometry_for(case, page_w); json.dump(lb, open(lbp, "w"))
    with open(mock.reply, "w", encoding="utf-8") as f: f.write(" ".join(s["text"] for s in case["script"]))
    cmd = [BIN, "speak", "--capture-method", "bounds", f"--capture-rect={cap}", "--line-bounds", lbp, "--doc-hint", hint, "--run-id", f"bench-{idx}"]
    stdin = "\n".join(it["text"] for it in case["selection"]); e = env.env(h, t)
    def run():
        try: p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=e, timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired: raise Harness("cli_timeout", f"CLI timed out after {RUN_TIMEOUT}s (case {cid})", cid)
        if p.returncode != 0:
            raise Harness("cli_failed", f"CLI exited {p.returncode} (case {cid})", cid, (p.stderr.strip().splitlines() or [""])[-1])
        return p
    run()  # warm-up: fills the parse cache
    for attempt in range(3):
        shutil.rmtree(os.path.join(h, "logs"), ignore_errors=True)
        before = mock.chats(); p = run(); after = mock.chats()
        recs = jl(os.path.join(h, "logs", "runs.jsonl"))
        if len(recs) != 1: raise Harness("no_runs_record", f"measured run wrote {len(recs)} runs records (case {cid})", cid, (p.stderr.strip().splitlines() or [""])[-1])
        rec = recs[0]; focus = jl(os.path.join(h, "logs", "focus.jsonl"))
        if rec.get("llm_provider_configured") != "local" or after <= before:
            raise Harness("not_local", f"run left the machine or bypassed the mock (case {cid})", cid)
        check_fields(rec, focus)
        dr, fs = rec.get("doc_ready_ms"), rec.get("first_sentence_ms")
        if dr is not None and fs is not None and dr <= fs: return rec, focus, attempt
    raise Harness("doc_not_ready", f"doc not ready before first sentence (case {cid})", cid)


# ------------------------------------------------------------------ scoring one case
def score_case(case, gt, rec, focus, acc):
    cid = case["case_id"]; subset = gt["subset"]
    sents = {s["id"]: s for s in gt["sentences"]}; parts = {p["id"]: p for p in gt["parts"]}
    lines_by_page = {p["n"]: p["lines"] for p in gt["pages"]}
    maps = [r for r in focus if r.get("state") == "map"]; shows = [r for r in focus if r.get("state") == "show"]
    try: owner = S.attribute([(s["sentence_id"], s["text"]) for s in case["script"]], maps)
    except S.AttributionError as ex: raise Harness("chunk_not_in_script", f"{ex} (case {cid})", cid)
    cshow = S.chunk_shows(shows)
    chunks_of = {}
    for k, sid in owner.items(): chunks_of.setdefault(sid, []).append(k)
    res = {"case_id": cid, "doc_id": case["doc_id"], "subset": subset, "lines": {"hits": 0, "n": 0}, "parts": {"hits": 0, "n": 0, "ovals": 0}, "misses": []}
    for it in case["script"]:
        s = sents[it["sentence_id"]]; ks = sorted(chunks_of.get(s["id"], [])); sh = [cshow.get(k) for k in ks]
        if s["score"] == "lines":
            hit, why = S.score_line_sentence(s, lines_by_page[s["page"]], sh)
            L = acc["lines"]; L["n"] += 1; L["hits"] += hit; res["lines"]["n"] += 1; res["lines"]["hits"] += hit
            b = L["by_subset"].setdefault(subset, {"hits": 0, "n": 0}); b["n"] += 1; b["hits"] += hit
            if not hit: L["reasons"][why] = L["reasons"].get(why, 0) + 1; res["misses"].append({"sentence_id": s["id"], "category": "lines", "reason": why})
        elif s["score"] == "parts":
            p = parts[s["part"]]; hit, why = S.score_part_sentence(p, sh)
            P = acc["parts"]; P["n"] += 1; P["hits"] += hit; res["parts"]["n"] += 1; res["parts"]["hits"] += hit
            b = P["by_subset"].setdefault(subset, {"hits": 0, "n": 0, "good": 0, "ovals": 0}); b["n"] += 1; b["hits"] += hit
            st = P["by_style"].setdefault(p["style"], {"hits": 0, "n": 0}); st["n"] += 1; st["hits"] += hit
            if not hit: P["reasons"][why] = P["reasons"].get(why, 0) + 1; res["misses"].append({"sentence_id": s["id"], "category": "parts", "reason": why})
    P = acc["parts"]; b = P["by_subset"].setdefault(subset, {"hits": 0, "n": 0, "good": 0, "ovals": 0})
    for k in sorted(cshow):
        sh = cshow[k]
        if sh.get("cue_kind") != "part_oval": continue
        P["ovals"] += 1; b["ovals"] += 1; res["parts"]["ovals"] += 1
        s = sents.get(owner.get(k)) if k in owner else None
        if s and s["score"] == "parts" and S.oval_good(sh, parts[s["part"]]): P["good"] += 1; b["good"] += 1
    ff, rq, ft, ta = rec.get("first_focus_ms"), rec.get("llm_request_at_ms"), rec.get("llm_first_token_ms"), rec.get("tts_first_audio_ms")
    fh = ff - (rq + ft) if None not in (ff, rq, ft) else None
    res["first_highlight_ms"] = fh
    if fh is None: acc["speed"]["missing"] += 1
    else: acc["speed"]["fh"].append(fh)
    if ff is not None and ta is not None: acc["speed"]["a2b"].append(ff - ta)
    return res


def new_acc():
    return {"lines": {"hits": 0, "n": 0, "reasons": {}, "by_subset": {}}, "parts": {"hits": 0, "n": 0, "good": 0, "ovals": 0, "reasons": {}, "by_style": {}, "by_subset": {}},
            "speed": {"fh": [], "a2b": [], "missing": 0}}


def load_set(set_name, holdout):
    fx, man = set_paths(set_name, HERE, holdout)
    m = json.load(open(man)); docs = []
    for d in sorted(m["docs"], key=lambda x: x["doc_id"]):
        p = os.path.join(fx, d["doc_id"])
        docs.append({"doc_id": d["doc_id"], "subset": d["subset"], "pdf": os.path.join(p, "doc.pdf"),
                     "gt": json.load(open(os.path.join(p, "gt.json"))), "cases": json.load(open(os.path.join(p, "cases.json")))})
    return docs, m["manifest_sha256"]


def run_set(set_name, docs, env, mock, glob, errors, counter):
    preds = {}
    for d in docs:
        log(f"bench: {set_name} parse {d['doc_id'] if set_name == 'public' else '#'}")
        preds[d["doc_id"]] = parse_doc(env, d["doc_id"], d["pdf"])
    acc = new_acc(); cases_out = []
    for d in docs:
        gt = d["gt"]; pw = {p["n"]: p["w"] for p in gt["pages"]}
        for case in sorted(d["cases"], key=lambda c: c["case_id"]):
            if glob and not fnmatch.fnmatch(case["case_id"], glob): continue
            counter[0] += 1
            try:
                rec, focus, tries = run_case(env, mock, case, d["pdf"], pw[case["page"]], counter[0])
                r = score_case(case, gt, rec, focus, acc); cases_out.append(r)
                log(f"bench: {set_name} case {counter[0]:3d} {case['case_id'] if set_name == 'public' else ''} lines {r['lines']['hits']}/{r['lines']['n']} "
                    f"parts {r['parts']['hits']}/{r['parts']['n']} ovals {r['parts']['ovals']} fh {r['first_highlight_ms']}{' retry ' + str(tries) if tries else ''}")
            except FieldsMissing:
                raise
            except Harness as ex:
                cid = ex.case_id if set_name == "public" else None
                errors.append({"case_id": cid, "code": ex.code, "message": ex.message if set_name == "public" else ex.code})
                log(f"bench: ERROR {ex.message}" + (f" | stderr: {ex.detail}" if ex.detail else ""))
    real = [d["gt"] for d in docs if d["subset"] == "real"]
    reviewed = bool(real) and all(g.get("reviewed_by_human") for g in real)
    boxes = S.score_boxes([(d["doc_id"], d["subset"], d["gt"]) for d in docs], preds, per_doc=(set_name == "public"))
    L, P, V = acc["lines"], acc["parts"], acc["speed"]
    blk = {"boxes": boxes, "lines": S.lines_summary(L["hits"], L["n"], L["reasons"], L["by_subset"]),
           "parts": S.parts_summary(P["hits"], P["n"], P["good"], P["ovals"], P["reasons"], P["by_style"], P["by_subset"]),
           "speed": S.speed_summary(V["fh"], V["a2b"], V["missing"])}
    for sec in ("boxes", "lines", "parts"):
        if "real" in blk[sec]["by_subset"]: blk[sec]["by_subset"]["real"].update(gt_source="agent", reviewed_by_human=reviewed)
    if set_name == "public": blk["cases"] = cases_out
    return blk, cases_out, boxes.get("by_doc")


def run_once(sets, holdout, glob, interp, keep):
    t0 = time.time(); root = os.path.realpath(tempfile.mkdtemp(prefix="se_bench_"))
    sha, dirty = git_state(); errors = []; out_sets = {}; fixtures = {}; hidden_detail = None; mock = None
    bench = {"schema": "speakeasy-bench/1", "complete": False, "generated_at": None, "duration_s": None, "commit": sha, "dirty": dirty,
             "versions": versions(), "config": config(), "fixtures": fixtures, "sets": out_sets, "errors": errors}
    code = 0
    try:
        mock = Mock(root); env = Env(root, mock.port, interp); counter = [0]
        for s in sets:
            docs, msha = load_set(s, holdout)
            fixtures[s] = {"manifest_sha256": msha, "docs": len(docs), "cases": sum(len(d["cases"]) for d in docs)}
            blk, cases_out, by_doc = run_set(s, docs, env, mock, glob, errors, counter)
            out_sets[s] = blk
            if s == "hidden": hidden_detail = {"generated_at": None, "cases": cases_out, "by_doc": by_doc}
        if errors: code = 3
    except FieldsMissing as ex:
        errors.append({"case_id": None, "code": ex.code, "message": ex.message}); code = 3
        log(f"bench: {ex.message}; refusing to score (the scores would be 0, not a measurement)")
    except Harness as ex:
        errors.append({"case_id": None, "code": ex.code, "message": ex.message}); code = 3
        log(f"bench: ERROR {ex.message}" + (f" | stderr: {ex.detail}" if ex.detail else ""))
    finally:
        if mock: mock.stop()
        if keep: log(f"bench: temp root kept at {root}")
        else: shutil.rmtree(root, ignore_errors=True)
    bench["complete"] = code == 0 and not glob and set(out_sets) == set(sets)
    bench["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"); bench["duration_s"] = round(time.time() - t0, 1)
    if hidden_detail is not None:
        hidden_detail["generated_at"] = bench["generated_at"]
        atomic_json(os.path.join(holdout, "last_cases.json"), hidden_detail)
    return bench, code


def summary(bench):
    for s, b in bench["sets"].items():
        bx, ln, pt, sp = b["boxes"], b["lines"], b["parts"], b["speed"]["first_highlight_ms"]
        print(f"{s}: boxes {bx['score']} (P50 {bx['precision_50']} R50 {bx['recall_50']}) | lines {ln['score']} ({ln['hits']}/{ln['n']}) | "
              f"parts {pt['score']} ({pt['hits']}/{pt['n']}) precision {pt['precision']} ({pt['good_ovals']}/{pt['ovals']}) | "
              f"first_highlight_ms p50 {sp['p50']} p95 {sp['p95']} (n {sp['n']})")


def record_baselines(bench, holdout):
    for s, blk in bench["sets"].items():
        bl = {"schema": "speakeasy-bench-baseline/1", "set": s, "recorded_at": bench["generated_at"], "commit": bench["commit"], "dirty": bench["dirty"],
              "config": bench["config"], "manifest_sha256": bench["fixtures"][s]["manifest_sha256"], "versions": bench["versions"],
              "metrics": S.baseline_metrics(blk), "detail": S.baseline_detail(blk, s == "public")}
        p = os.path.join(HERE, "baseline.json") if s == "public" else os.path.join(holdout, "baseline.json")
        atomic_json(p, bl); log(f"bench: baseline recorded for {s}: {p if s == 'public' else '<holdout>/baseline.json'}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2 benchmark (see module docstring)")
    ap.add_argument("--set", choices=("public", "hidden", "all")); ap.add_argument("--holdout")
    ap.add_argument("--out", default=os.path.join(AUD, "reports", "bench.json")); ap.add_argument("--cases")
    ap.add_argument("--record-baseline", action="store_true"); ap.add_argument("--check-determinism", action="store_true")
    ap.add_argument("--keep-temp", action="store_true")
    a = ap.parse_args()
    interp = sys.executable
    import importlib.util
    if importlib.util.find_spec("pypdfium2") is None:
        log(f"bench: pypdfium2 not importable in {interp}; use the doc venv"); sys.exit(2)
    holdout = os.path.abspath(os.path.expanduser(a.holdout or os.environ.get("SPEAKEASY_HOLDOUT") or "~/speakeasy-holdout/bench"))
    has_hidden = os.path.exists(os.path.join(holdout, "manifest.json"))
    if a.set is None:
        a.set = "all" if has_hidden else "public"
        if not has_hidden: log(f"bench: hidden set not found at {holdout}; public only")
    if a.set in ("hidden", "all") and not has_hidden: log(f"bench: hidden set not found at {holdout}"); sys.exit(2)
    if a.record_baseline and a.cases: log("bench: --record-baseline needs a complete run (no --cases)"); sys.exit(2)
    sets = ["public", "hidden"] if a.set == "all" else [a.set]
    infos = {}
    for s in sets:
        fx, man = set_paths(s, HERE, holdout)
        E, info = validate_set(s, fx, man); infos[s] = info
        if E:
            for e in E[:40]: log(e if s == "public" else e.replace(holdout, "<holdout>"))
            sys.exit(2)
    if len(infos) == 2 or (a.set == "public" and has_hidden):
        if has_hidden and "hidden" not in infos: infos["hidden"] = validate_set("hidden", *set_paths("hidden", HERE, holdout))[1]
        E = cross_check(infos["public"], infos["hidden"])
        if E:
            for e in E: log(e)
            sys.exit(2)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    bench, code = run_once(sets, holdout, a.cases, interp, a.keep_temp)
    atomic_json(a.out, bench); summary(bench)
    if code: sys.exit(code)
    if a.check_determinism:
        log("bench: determinism run 2")
        b2, code2 = run_once(sets, holdout, a.cases, interp, a.keep_temp)
        if code2: atomic_json(a.out, b2); sys.exit(code2)
        diffs = S.diff_paths(S.determinism_view(bench), S.determinism_view(b2))
        if diffs:
            for d in diffs[:60]: print(f"determinism: differs at {d}", file=sys.stderr)
            sys.exit(4)
        log("bench: determinism ok (two runs identical)")
    if a.record_baseline:
        if not bench["complete"]: log("bench: --record-baseline needs a complete run"); sys.exit(3)
        record_baselines(bench, holdout)


if __name__ == "__main__":
    main()
