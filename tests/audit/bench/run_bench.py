#!/usr/bin/env python3
"""Phase 2 three-score benchmark (boxes, lines, parts + speed). Contract: docs/contracts/phase2-benchmark.md; app-side fields:
docs/contracts/phase2-logging.md. Black box: drives `src/docmap.py parse-pdf` and the `speakeasy speak` CLI as subprocesses
and scores their outputs and logs; it never imports app code.

Usage (run under a Python that has pypdfium2, e.g. the doc venv; that interpreter is also passed to the app as
SPEAKEASY_DOC_PYTHON):

    python3 tests/audit/bench/run_bench.py [--set public|hidden|all] [--holdout DIR] [--out FILE] [--cases GLOB]
                                           [--record-baseline] [--check-determinism] [--keep-temp]
                                           [--layout-model PATH] [--doc-python PATH]
                                           [--public-baseline-out FILE] [--hidden-baseline-out FILE] [--last-cases-out FILE]
    python3 tests/audit/bench/bench_gate.py tests/audit/reports/bench.json [--public-only]

Layout detector (docs/contracts/phase4-layout.md section 7): --layout-model PATH (or the caller's SPEAKEASY_LAYOUT_MODEL) is
passed to the app as SPEAKEASY_LAYOUT_MODEL; without either it stays unset and the temp home has no model, so the run measures
the heuristic fallback. --doc-python picks the interpreter for parse-pdf and SPEAKEASY_DOC_PYTHON (default: this one); a
detector run needs onnxruntime and numpy in it. sets.<set>.layout reports pages by layout.src, fallback reasons and detector
ms/page; a detector run with any heuristic page (or a page without pages[].layout) fails with exit 5.

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

Gated mode (the default; docs/contracts/phase5-parts.md 7.1) keeps SPEAKEASY_GROUND=off, so the figure-part picker never runs: a
POST to the mock's /api/chat or a focus-log `part_pick` record in any run (warm-up included) aborts with exit 3
`picker ran in gated mode (case ...)`.

Pick mode (7.2, scored separately, never gated): --pick replay|proxy adds SPEAKEASY_GROUND=local, SPEAKEASY_PICK=on and
SPEAKEASY_VISION_MODEL=qwen3-vl:4b-instruct; the picker's /api/chat goes to the same mock. replay answers from the set's
cassette (--pick-cassette, default tests/audit/bench/pick_cassette.json for public and <holdout>/pick_cassette.json for hidden;
missing -> exit 2); proxy forwards to --pick-upstream (loopback only, default http://127.0.0.1:11434) and, after a
run without harness errors, writes that cassette (hashed keys and letter logprobs only; with --cases, only those cases). sets.<set>.pick is scored from the logged decision
(`part_pick` records, attributed by `sent`), not the draw time: requests, called, by_outcome, replay_miss, precision,
recall, candidate_recall, parts_with_pick {score, precision} and ms. bench.json carries mode gated|pick-replay|pick-proxy;
bench_gate.py refuses a pick bench. In pick mode the determinism check covers boxes, lines and pick (except ms); parts and
cases depend on when a late pick lands (contract 4.6) and are left out. replay with replay_miss > 0 marks the pick block
stale, prints `pick cassette stale: re-record with tools/pick_eval.py --record` and exits 6.

Exit codes: 0 bench written; 2 preflight/usage/fixture error (nothing run); 3 harness error (bench.json written with
complete: false), including app output that lacks the logging-contract fields and a picker run in gated mode; 4 determinism
mismatch; 5 layout coverage failure (detector run with heuristic pages; bench.json written with complete: false); 6 stale pick
cassette (replay_miss > 0; bench.json written)."""
import argparse, datetime, fnmatch, glob, hashlib, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.parse, urllib.request

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


class PickerRan(Harness):
    pass


PICK_MODEL = "qwen3-vl:4b-instruct"
STALE_MSG = "pick cassette stale: re-record with tools/pick_eval.py --record"


def log(msg): print(msg, file=sys.stderr, flush=True)


def jl(p):
    return [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()] if os.path.exists(p) else []


def atomic_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, indent=1); f.write("\n")
    os.replace(tmp, path)


VERSION_PKGS = ("pypdfium2", "reportlab", "pdfplumber", "onnxruntime", "pillow")
PROBE = ("import json, sys\nfrom importlib import metadata\nout = {'python': sys.version.split()[0]}\n"
         "for m in %r:\n    try: out[m] = metadata.version(m)\n    except metadata.PackageNotFoundError: out[m] = None\n"
         "for m in ('pypdfium2', 'onnxruntime', 'numpy'):\n"
         "    try: __import__(m); out['import_' + m] = True\n    except Exception: out['import_' + m] = False\n"
         "print(json.dumps(out))") % (VERSION_PKGS,)


def probe(interp):
    """Versions and importability in the doc interpreter (the one that parses)."""
    try:
        p = subprocess.run([interp, "-c", PROBE], capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def versions(pr):
    return {k: pr.get(k) for k in ("python",) + VERSION_PKGS}


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
    def __init__(self, root, port, interp, layout_model=None, pick=None):
        self.root, self.port, self.interp, self.layout_model, self.pick, self.n = root, port, interp, layout_model, pick, 0

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
        if self.layout_model: e["SPEAKEASY_LAYOUT_MODEL"] = self.layout_model
        if self.pick: e.update(SPEAKEASY_GROUND="local", SPEAKEASY_PICK="on", SPEAKEASY_VISION_MODEL=PICK_MODEL)
        return e


class Mock:
    def __init__(self, root, pick=None, cassette=None, upstream=None, header=None, tag=""):
        self.port_file = os.path.join(root, f"mock{tag}.port"); self.log = os.path.join(root, f"mock{tag}.jsonl"); self.reply = os.path.join(root, "reply.txt")
        open(self.reply, "w").close(); open(self.log, "w").close()
        e = {k: v for k, v in os.environ.items() if not k.startswith(("MOCK_", "SPEAKEASY_"))}
        e.update(MOCK_PORT="0", MOCK_PORT_FILE=self.port_file, MOCK_LOG=self.log, MOCK_REPLY_FILE=self.reply,
                 MOCK_FIRST_DELAY=str(MOCK_FIRST), MOCK_TOKEN_DELAY=str(MOCK_TOKEN), MOCK_PICK=pick or "none")
        if cassette: e["MOCK_PICK_CASSETTE"] = cassette
        if upstream: e["MOCK_PICK_UPSTREAM"] = upstream
        if header: e["MOCK_PICK_HEADER"] = json.dumps(header)
        self.tag_file = os.path.join(root, f"mock{tag}.run"); open(self.tag_file, "w").close(); e["MOCK_RUN_TAG_FILE"] = self.tag_file
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

    def api_chats(self):
        return sum(1 for r in jl(self.log) if r.get("method") == "POST" and str(r.get("path", "")).split("?")[0].rstrip("/").endswith("/api/chat"))

    def pick_replies(self):
        return [r for r in jl(self.log) if r.get("event") == "pick_reply"]

    def tag(self, label):
        with open(self.tag_file, "w") as f: f.write(label)

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
    preds, figs_done, with_blocks, layouts, figs = {}, 0, 0, [], {}
    for pg in pages:
        figs[pg.get("n")] = [{"bbox": f.get("bbox"), "panels": [p.get("bbox") for p in f.get("panels") or [] if isinstance(p, dict) and S._is_rect(p.get("bbox"))]}
                             for f in pg.get("figures") or [] if isinstance(f, dict) and S._is_rect(f.get("bbox"))]
        figs_done += bool(pg.get("figs_done")); with_blocks += isinstance(pg.get("blocks"), list)
        preds[pg.get("n")] = [{"kind": b.get("kind"), "bbox": b.get("bbox")} for b in (pg.get("blocks") or []) if isinstance(b.get("bbox"), list)]
        if pg.get("figs_done"): layouts.append(pg.get("layout") if isinstance(pg.get("layout"), dict) else None)
    if figs_done and not with_blocks:
        raise FieldsMissing("contract_fields_missing", f"parse output has no pages[].blocks on figs_done pages ({doc_id}): logging contract D not implemented")
    top = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    return preds, layouts, top.get("detector_version") if isinstance(top.get("detector_version"), str) else None, figs


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


def run_case(env, mock, case, pdf, page_w, idx, public=True):
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
    def gated_check(api_before, focus_recs):
        if env.pick: return
        if mock.api_chats() > api_before or any(r.get("state") == "part_pick" for r in focus_recs):
            raise PickerRan("picker_in_gated_mode", f"picker ran in gated mode (case {label})", cid)
    label = cid if public else f"#{idx}"
    a0 = mock.api_chats(); mock.tag(f"{idx}:warm")
    run()  # warm-up: fills the parse cache
    gated_check(a0, jl(os.path.join(h, "logs", "focus.jsonl")))
    for attempt in range(3):
        shutil.rmtree(os.path.join(h, "logs"), ignore_errors=True)
        before = mock.chats(); a0 = mock.api_chats(); mock.tag(f"{idx}:m{attempt}"); p = run(); after = mock.chats(); mock.tag("")
        recs = jl(os.path.join(h, "logs", "runs.jsonl"))
        if len(recs) != 1: raise Harness("no_runs_record", f"measured run wrote {len(recs)} runs records (case {cid})", cid, (p.stderr.strip().splitlines() or [""])[-1])
        rec = recs[0]; focus = jl(os.path.join(h, "logs", "focus.jsonl"))
        gated_check(a0, focus)
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
    P = acc["parts"]; b = P["by_subset"].setdefault(subset, {"hits": 0, "n": 0, "good": 0, "ovals": 0}); res["parts"]["good"] = 0
    for k in sorted(cshow):
        sh = cshow[k]
        if sh.get("cue_kind") != "part_oval": continue
        P["ovals"] += 1; b["ovals"] += 1; res["parts"]["ovals"] += 1
        s = sents.get(owner.get(k)) if k in owner else None
        if s and s["score"] == "parts" and S.oval_good(sh, parts[s["part"]]): P["good"] += 1; b["good"] += 1; res["parts"]["good"] += 1
    if "pick" in acc:
        picks = [r for r in focus if r.get("state") == "part_pick"]
        ev = S.pick_eval(picks, owner, sents, parts, cshow); acc["pick"]["evals"] += ev
        res["pick"] = {"requests": len(ev), "called": sum(e["called"] for e in ev), "accepted": sum(e["accepted"] for e in ev), "correct": sum(e["correct"] for e in ev),
                       "sent": [e["sent"] for e in ev], "outcomes": [e["outcome"] for e in ev]}
        pw = S.parts_counts([it["sentence_id"] for it in case["script"]], sents, parts, chunks_of, S.shows_with_picks(cshow, ev), owner)
        for k2, v in pw.items(): acc["pick"]["pw"][k2] += v
    inv = S.case_invariants(case, rec, shows, acc.get("figs") or {}, maps)
    for k, v in inv.items(): acc["inv"][k] = acc["inv"].get(k, 0) + v
    ff, rq, ft, ta = rec.get("first_focus_ms"), rec.get("llm_request_at_ms"), rec.get("llm_first_token_ms"), rec.get("tts_first_audio_ms")
    fh = ff - (rq + ft) if None not in (ff, rq, ft) else None
    res["first_highlight_ms"] = fh
    if fh is None: acc["speed"]["missing"] += 1
    else: acc["speed"]["fh"].append(fh)
    if ff is not None and ta is not None: acc["speed"]["a2b"].append(ff - ta)
    return res


def new_acc(pick=False):
    a = {"lines": {"hits": 0, "n": 0, "reasons": {}, "by_subset": {}}, "parts": {"hits": 0, "n": 0, "good": 0, "ovals": 0, "reasons": {}, "by_style": {}, "by_subset": {}},
         "speed": {"fh": [], "a2b": [], "missing": 0}, "inv": {}}
    if pick: a["pick"] = {"evals": [], "pw": {"hits": 0, "n": 0, "good": 0, "ovals": 0}}
    return a


def load_set(set_name, holdout):
    fx, man = set_paths(set_name, HERE, holdout)
    m = json.load(open(man)); docs = []
    for d in sorted(m["docs"], key=lambda x: x["doc_id"]):
        p = os.path.join(fx, d["doc_id"])
        docs.append({"doc_id": d["doc_id"], "subset": d["subset"], "pdf": os.path.join(p, "doc.pdf"),
                     "gt": json.load(open(os.path.join(p, "gt.json"))), "cases": json.load(open(os.path.join(p, "cases.json")))})
    return docs, m["manifest_sha256"]


def run_set(set_name, docs, env, mock, glob, errors, counter, det_versions):
    preds = {}; layouts = []; figs_of = {}; scored_tags = set()
    for d in docs:
        log(f"bench: {set_name} parse {d['doc_id'] if set_name == 'public' else '#'}")
        preds[d["doc_id"]], pl, dv, figs_of[d["doc_id"]] = parse_doc(env, d["doc_id"], d["pdf"]); layouts += pl
        if dv: det_versions.add(dv)
    lay = S.layout_summary(layouts)
    log(f"bench: {set_name} layout pages {lay['pages']} detector {lay['src']['detector']} heuristic {lay['src']['heuristic']} missing {lay['missing']} "
        f"reasons {lay['reasons']} ms p50 {lay['ms']['p50']} p95 {lay['ms']['p95']}")
    acc = new_acc(pick=bool(env.pick)); cases_out = []; pick_r0 = len(mock.pick_replies())
    for d in docs:
        gt = d["gt"]; pw = {p["n"]: p["w"] for p in gt["pages"]}
        for case in sorted(d["cases"], key=lambda c: c["case_id"]):
            if glob and not fnmatch.fnmatch(case["case_id"], glob): continue
            counter[0] += 1
            try:
                rec, focus, tries = run_case(env, mock, case, d["pdf"], pw[case["page"]], counter[0], public=(set_name == "public"))
                scored_tags.add(f"{counter[0]}:m{tries}"); acc["figs"] = figs_of.get(d["doc_id"], {})
                r = score_case(case, gt, rec, focus, acc); cases_out.append(r)
                log(f"bench: {set_name} case {counter[0]:3d} {case['case_id'] if set_name == 'public' else ''} lines {r['lines']['hits']}/{r['lines']['n']} "
                    f"parts {r['parts']['hits']}/{r['parts']['n']} ovals {r['parts']['ovals']} fh {r['first_highlight_ms']}{' retry ' + str(tries) if tries else ''}")
            except (FieldsMissing, PickerRan):
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
           "speed": S.speed_summary(V["fh"], V["a2b"], V["missing"]), "layout": lay, "invariants": S.invariants_summary(acc["inv"])}
    for sec in ("boxes", "lines", "parts"):
        if "real" in blk[sec]["by_subset"]: blk[sec]["by_subset"]["real"].update(gt_source="agent", reviewed_by_human=reviewed)
    if env.pick:
        reps = mock.pick_replies()[pick_r0:]
        miss = sum(1 for r in reps if r.get("result") == "miss" and r.get("run") in scored_tags)
        miss_w = sum(1 for r in reps if r.get("result") == "miss" and r.get("run") not in scored_tags)
        blk["pick"] = S.pick_summary(acc["pick"]["evals"], acc["pick"]["pw"], miss if env.pick == "replay" else 0)
        blk["pick"]["replay_miss_warmup"] = miss_w if env.pick == "replay" else 0
        pk = blk["pick"]
        log(f"bench: {set_name} pick requests {pk['requests']} called {pk['called']} accepted {pk['accepted']} precision {pk['precision']} recall {pk['recall']} "
            f"candidate_recall {pk['candidate_recall']} parts_with_pick {pk['parts_with_pick']['score']} / {pk['parts_with_pick']['precision']} replay_miss {pk['replay_miss']}")
    if set_name == "public": blk["cases"] = cases_out
    return blk, cases_out, boxes.get("by_doc")


def run_once(sets, holdout, glob, interp, keep, layout_model, pr, last_cases, pick=None, cassettes=None, upstream=None):
    t0 = time.time(); root = os.path.realpath(tempfile.mkdtemp(prefix="se_bench_"))
    sha, dirty = git_state(); errors = []; out_sets = {}; fixtures = {}; hidden_detail = None; mock = None; det_versions = set()
    vers = versions(pr)
    bench = {"schema": "speakeasy-bench/1", "mode": f"pick-{pick}" if pick else "gated", "complete": False, "generated_at": None, "duration_s": None,
             "commit": sha, "dirty": dirty, "versions": vers, "config": config(), "fixtures": fixtures, "sets": out_sets, "errors": errors}
    if pick: bench["pick_cassettes"] = {}
    code = 0; coverage = []
    try:
        mock = None if pick else Mock(root); env = Env(root, mock.port if mock else None, interp, layout_model, pick); counter = [0]
        for s in sets:
            docs, msha = load_set(s, holdout)
            fixtures[s] = {"manifest_sha256": msha, "docs": len(docs), "cases": sum(len(d["cases"]) for d in docs)}
            if pick:
                if mock: mock.stop()
                cas = cassettes[s] if pick == "replay" else os.path.join(root, f"cassette-{s}.json")
                hdr = {"pypdfium2": vers.get("pypdfium2"), "pillow": vers.get("pillow"), "commit": sha, "src_sha": src_sha()}
                mock = Mock(root, pick, cas, upstream, hdr, tag="-" + s); env.port = mock.port
            n_err = len(errors)
            blk, cases_out, by_doc = run_set(s, docs, env, mock, glob, errors, counter, det_versions)
            out_sets[s] = blk
            if pick: bench["pick_cassettes"][s] = cassette_info(cassettes[s] if pick == "replay" else cas)
            if pick == "proxy":
                if len(errors) > n_err: log(f"bench: {s} cassette not written (harness errors)")
                elif os.path.exists(cas):
                    os.makedirs(os.path.dirname(os.path.abspath(cassettes[s])), exist_ok=True)
                    shutil.copyfile(cas, cassettes[s] + ".tmp"); os.replace(cassettes[s] + ".tmp", cassettes[s])
                    log(f"bench: {s} pick cassette written: {cassettes[s] if s == 'public' else '<holdout>/pick_cassette.json'}")
                else: log(f"bench: {s} no pick was recorded; cassette not written")
            if s == "hidden": hidden_detail = {"generated_at": None, "cases": cases_out, "by_doc": by_doc}
            if layout_model: coverage += S.layout_coverage_errors(s, blk["layout"])
            elif blk["layout"]["src"]["detector"]: log(f"bench: WARNING {s}: {blk['layout']['src']['detector']} detector pages without --layout-model (a model outside the temp home?)")
        vers["layout_model"] = sorted(det_versions)[0] if len(det_versions) == 1 else (sorted(det_versions) or None)
        for m in coverage:
            errors.append({"case_id": None, "code": "layout_coverage", "message": m}); log(f"bench: LAYOUT COVERAGE FAIL {m}")
        if errors: code = 5 if coverage and len(errors) == len(coverage) else 3
    except PickerRan as ex:
        errors.append({"case_id": None, "code": ex.code, "message": ex.message}); code = 3
        log(f"bench: {ex.message}")
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
    if "layout_model" not in vers: vers["layout_model"] = None
    if hidden_detail is not None:
        hidden_detail["generated_at"] = bench["generated_at"]
        atomic_json(last_cases or os.path.join(holdout, "last_cases.json"), hidden_detail)
    if pick == "replay" and any((b.get("pick") or {}).get("stale") for b in out_sets.values()):
        log(f"bench: {STALE_MSG}")
        if code == 0: code = 6
    return bench, code


def src_sha():
    """sha256 over the src/*.py contents concatenated in sorted path order, no separators (phase5-parts.md A4.2)."""
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(REPO, "src", "*.py"))): h.update(open(p, "rb").read())
    return h.hexdigest()


def cassette_info(path):
    """Header and entry count of a cassette (no keys, no values)."""
    try: d = json.load(open(path))
    except (OSError, ValueError): return {"present": False}
    return {"present": True, "entries": len(d.get("entries") or {}), "header": d.get("header") or {},
            "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()[:12]}


def summary(bench):
    for s, b in bench["sets"].items():
        bx, ln, pt, sp = b["boxes"], b["lines"], b["parts"], b["speed"]["first_highlight_ms"]
        print(f"{s}: boxes {bx['score']} (P50 {bx['precision_50']} R50 {bx['recall_50']}) | lines {ln['score']} ({ln['hits']}/{ln['n']}) | "
              f"parts {pt['score']} ({pt['hits']}/{pt['n']}) precision {pt['precision']} ({pt['good_ovals']}/{pt['ovals']}) | "
              f"first_highlight_ms p50 {sp['p50']} p95 {sp['p95']} (n {sp['n']})")
        lay = b.get("layout")
        if lay: print(f"{s}: layout pages {lay['pages']} detector {lay['src']['detector']} heuristic {lay['src']['heuristic']} missing {lay['missing']} "
                      f"reasons {json.dumps(lay['reasons'], sort_keys=True)} detector ms/page p50 {lay['ms']['p50']} p95 {lay['ms']['p95']}")
        iv = b.get("invariants")
        if iv: print(f"{s}: invariants (must be 0) " + " ".join(f"{k} {v}" for k, v in iv.items()))
        wt = bx.get("wrong_type")
        if wt: print(f"{s}: boxes wrong type (IoU >= 0.75 with a GT block of another kind) " + " ".join(f"{k} {v['n']}{json.dumps(v['as'], sort_keys=True) if v['as'] else ''}" for k, v in wt.items()))
        pk = b.get("pick")
        if pk:
            pw = pk["parts_with_pick"]
            print(f"{s}: pick requests {pk['requests']} called {pk['called']} accepted {pk['accepted']} precision {pk['precision']} recall {pk['recall']} "
                  f"candidate_recall {pk['candidate_recall']} | parts_with_pick {pw['score']} ({pw['hits']}/{pw['n']}) precision {pw['precision']} | "
                  f"by_outcome {json.dumps(pk['by_outcome'], sort_keys=True)} replay_miss {pk['replay_miss']}{' STALE' if pk['stale'] else ''} "
                  f"(warm-up {pk.get('replay_miss_warmup', 0)}) | ms p50 {pk['ms']['p50']} p95 {pk['ms']['p95']}")


def record_baselines(bench, holdout, public_out=None, hidden_out=None):
    for s, blk in bench["sets"].items():
        bl = {"schema": "speakeasy-bench-baseline/1", "set": s, "recorded_at": bench["generated_at"], "commit": bench["commit"], "dirty": bench["dirty"],
              "config": bench["config"], "manifest_sha256": bench["fixtures"][s]["manifest_sha256"], "versions": bench["versions"],
              "metrics": S.baseline_metrics(blk), "detail": S.baseline_detail(blk, s == "public")}
        p = (public_out or os.path.join(HERE, "baseline.json")) if s == "public" else (hidden_out or os.path.join(holdout, "baseline.json"))
        atomic_json(p, bl); log(f"bench: baseline recorded for {s}: {p if s == 'public' or hidden_out else '<holdout>/baseline.json'}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2 benchmark (see module docstring)")
    ap.add_argument("--set", choices=("public", "hidden", "all")); ap.add_argument("--holdout")
    ap.add_argument("--out", default=os.path.join(AUD, "reports", "bench.json")); ap.add_argument("--cases")
    ap.add_argument("--record-baseline", action="store_true"); ap.add_argument("--check-determinism", action="store_true")
    ap.add_argument("--keep-temp", action="store_true")
    ap.add_argument("--layout-model", help="verified pp_doclayout_s.onnx, passed as SPEAKEASY_LAYOUT_MODEL (default: the caller's SPEAKEASY_LAYOUT_MODEL)")
    ap.add_argument("--doc-python", help="interpreter for parse-pdf and SPEAKEASY_DOC_PYTHON (default: this one)")
    ap.add_argument("--public-baseline-out"); ap.add_argument("--hidden-baseline-out")
    ap.add_argument("--last-cases-out", help="hidden per-case detail (default <holdout>/last_cases.json)")
    ap.add_argument("--pick", choices=("replay", "proxy"), help="pick mode (scored separately, never gated)")
    ap.add_argument("--pick-cassette", help="public cassette (default tests/audit/bench/pick_cassette.json)")
    ap.add_argument("--pick-hidden-cassette", help="hidden cassette (default <holdout>/pick_cassette.json)")
    ap.add_argument("--pick-upstream", help="proxy upstream, loopback only (default $MOCK_PICK_UPSTREAM or http://127.0.0.1:11434)")
    a = ap.parse_args()
    interp = os.path.abspath(a.doc_python) if a.doc_python else sys.executable
    pr = probe(interp)
    if not pr: log(f"bench: cannot run {interp}"); sys.exit(2)
    if not pr.get("import_pypdfium2"):
        log(f"bench: pypdfium2 not importable in {interp}; use the doc venv"); sys.exit(2)
    layout_model = a.layout_model or os.environ.get("SPEAKEASY_LAYOUT_MODEL") or None
    if layout_model:
        layout_model = os.path.abspath(os.path.expanduser(layout_model))
        if not os.path.isfile(layout_model): log(f"bench: layout model not found: {layout_model}"); sys.exit(2)
        if not (pr.get("import_onnxruntime") and pr.get("import_numpy")):
            log(f"bench: --layout-model needs onnxruntime and numpy in {interp} (the run would silently measure the fallback)"); sys.exit(2)
        log(f"bench: layout model {layout_model} (onnxruntime {pr.get('onnxruntime')})")
    else:
        log("bench: no layout model: this run measures the heuristic fallback")
    holdout = os.path.abspath(os.path.expanduser(a.holdout or os.environ.get("SPEAKEASY_HOLDOUT") or "~/speakeasy-holdout/bench"))
    has_hidden = os.path.exists(os.path.join(holdout, "manifest.json"))
    if a.set is None:
        a.set = "all" if has_hidden else "public"
        if not has_hidden: log(f"bench: hidden set not found at {holdout}; public only")
    if a.set in ("hidden", "all") and not has_hidden: log(f"bench: hidden set not found at {holdout}"); sys.exit(2)
    if a.record_baseline and a.cases: log("bench: --record-baseline needs a complete run (no --cases)"); sys.exit(2)
    sets = ["public", "hidden"] if a.set == "all" else [a.set]
    cassettes = {"public": os.path.abspath(os.path.expanduser(a.pick_cassette or os.path.join(HERE, "pick_cassette.json"))),
                 "hidden": os.path.abspath(os.path.expanduser(a.pick_hidden_cassette or os.path.join(holdout, "pick_cassette.json")))}
    upstream = a.pick_upstream or os.environ.get("MOCK_PICK_UPSTREAM") or "http://127.0.0.1:11434"
    if a.pick:
        if a.record_baseline: log("bench: --record-baseline is gated mode only (a pick bench is never a baseline)"); sys.exit(2)
        if a.pick == "replay":
            for s in sets:
                if not os.path.isfile(cassettes[s]):
                    log(f"bench: pick cassette not found for {s}: {cassettes[s] if s == 'public' else '<holdout>/pick_cassette.json'} "
                        "(record it with tools/pick_eval.py --record)"); sys.exit(2)
                hs = (cassette_info(cassettes[s]).get("header") or {}).get("src_sha")
                if hs != src_sha():
                    log(f"bench: {s} pick cassette not current (src changed after recording): header src_sha {str(hs)[:12]}, tree {src_sha()[:12]}"); sys.exit(2)
        elif (urllib.parse.urlparse(upstream).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            log(f"bench: --pick-upstream must be a loopback URL: {upstream}"); sys.exit(2)
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
    lc = os.path.abspath(os.path.expanduser(a.last_cases_out)) if a.last_cases_out else None
    pk = dict(pick=a.pick, cassettes=cassettes, upstream=upstream)
    bench, code = run_once(sets, holdout, a.cases, interp, a.keep_temp, layout_model, pr, lc, **pk)
    atomic_json(a.out, bench); summary(bench)
    if code: sys.exit(code)
    if a.check_determinism:
        log("bench: determinism run 2")
        b2, code2 = run_once(sets, holdout, a.cases, interp, a.keep_temp, layout_model, pr, lc, **pk)
        if code2: atomic_json(a.out, b2); sys.exit(code2)
        diffs = S.diff_paths(S.determinism_view(bench), S.determinism_view(b2))
        if diffs:
            for d in diffs[:60]: print(f"determinism: differs at {d}", file=sys.stderr)
            sys.exit(4)
        log("bench: determinism ok (two runs identical)")
    if a.record_baseline:
        if not bench["complete"]: log("bench: --record-baseline needs a complete run"); sys.exit(3)
        po, ho = (os.path.abspath(os.path.expanduser(x)) if x else None for x in (a.public_baseline_out, a.hidden_baseline_out))
        record_baselines(bench, holdout, po, ho)


if __name__ == "__main__":
    main()
