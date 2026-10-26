#!/usr/bin/env python3
"""Live Set-of-Mark picker eval against the real local Ollama (docs/contracts/phase5-parts.md 7.3; optional, never gated).

    python3 tools/pick_eval.py [--set public|hidden] [--record] [--sweep] [--ollama URL]
                               [--cassette PATH] [--layout-model PATH] [--doc-python PATH] [--cases GLOB] [--holdout DIR]

Runs the bench in pick proxy mode (run_bench.py --pick proxy, in process): every case of the set through the speakeasy CLI with
SPEAKEASY_GROUND=local, SPEAKEASY_PICK=on and qwen3-vl:4b-instruct, each pick's /api/chat forwarded by the bench mock to Ollama.
Prints the set's pick block, warm pick latency p50/p95 and the first-call time (the preflight probe, which loads the model when
it is not loaded yet).
  --sweep   public only: accepted, precision, recall (and parts_with_pick) for each threshold T in 0.40, 0.45, ..., 0.95, from
            the logged p_top / chosen_index of every pick (accepted and low-confidence picks both log their argmax).
  --record  writes the set's cassette (hashed keys and letter logprobs only): --cassette, else tests/audit/bench/pick_cassette.json
            for public and <holdout>/pick_cassette.json for hidden. The header carries src_sha (sha256 over the src/*.py
            contents concatenated in sorted path order). Without --record the cassette goes to a temp file.
Hidden: aggregates only, no per-case line and no sweep. Temp homes only (never ~/.speakeasy). Exit 2 when the preflight fails
(Ollama unreachable, model missing, no top_logprobs) or the bench run does not complete."""
import argparse, base64, glob, hashlib, json, os, shutil, struct, sys, tempfile, time, urllib.parse, urllib.request, zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "tests", "audit", "bench")
sys.path.insert(0, BENCH); sys.path.insert(0, os.path.join(REPO, "src"))
import run_bench as RB
import score as S
import partpick

SWEEP = [round(0.40 + 0.05 * k, 2) for k in range(12)]

def fail(msg):
    print(f"pick_eval: {msg}", file=sys.stderr); sys.exit(2)

def http_json(url, body=None, timeout=120):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode())

def white_png(side):
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff" * 3 * side for _ in range(side))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

def src_sha():
    """sha256 over the sorted src/*.py contents: a cassette is current only while it matches the tree (A4.2)."""
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(REPO, "src", "*.py"))): h.update(open(p, "rb").read())
    return h.hexdigest()

def preflight(ollama):
    """Ollama reachable, the model present, one probe returns top_logprobs. Returns the probe's ms."""
    try: http_json(ollama + "/api/version")
    except (OSError, ValueError) as e: fail(f"Ollama not reachable at {ollama} ({type(e).__name__})")
    if not any(m.get("name") == RB.PICK_MODEL or m.get("model") == RB.PICK_MODEL for m in http_json(ollama + "/api/tags").get("models") or []):
        fail(f"model {RB.PICK_MODEL} not present (ollama pull {RB.PICK_MODEL})")
    body = partpick.request_body(RB.PICK_MODEL, "10m", "Answer with one letter, A or B: which comes first?", base64.b64encode(white_png(32)).decode())
    t0 = time.time()
    try: reply = http_json(ollama + "/api/chat", body)
    except (OSError, ValueError) as e: fail(f"probe request failed ({type(e).__name__})")
    if not ((reply.get("logprobs") or [{}])[0].get("top_logprobs")): fail("probe reply has no top_logprobs (Ollama too old?)")
    return int((time.time() - t0) * 1000)

class Capture:
    """Wraps score.pick_eval / score.parts_counts inside the bench run: keeps every pick's logged p_top and argmax with its
    scoring context, so any threshold can be scored from one live run."""
    def __init__(self):
        self.picks, self.pw, self.ctx = [], {T: {"hits": 0, "n": 0, "good": 0, "ovals": 0} for T in SWEEP}, None
        self.pick_eval, self.parts_counts, self.score_case, self.skipped = S.pick_eval, S.parts_counts, RB.score_case, {}
        S.pick_eval, S.parts_counts, RB.score_case = self._pick_eval, self._parts_counts, self._score_case
    def _score_case(self, case, gt, rec, focus, acc):
        for k, v in ((rec.get("part_picks") or {}).get("skipped") or {}).items(): self.skipped[k] = self.skipped.get(k, 0) + v
        return self.score_case(case, gt, rec, focus, acc)
    def _pick_eval(self, picks, owner, sents, parts, cshow):
        ev = self.pick_eval(picks, owner, sents, parts, cshow)
        rows = []
        for r, e in zip(picks, ev):
            ci, rects = r.get("chosen_index"), r.get("cand_rects") or []
            s = sents.get(owner.get(r.get("sent"))); part = parts.get(s["part"]) if s and s["score"] == "parts" else None
            chosen = rects[ci] if isinstance(ci, int) and 0 <= ci < len(rects) else None
            page_ok = part is not None and (e["page"] is None or e["page"] == part["page"])
            rows.append(dict(e, p_top=r.get("p_top"), argmax=chosen, argmax_correct=bool(chosen and page_ok and S.inside(S.centre(chosen), part["bbox"]))))
        self.picks += rows; self.ctx = (rows, cshow)
        return ev
    def _parts_counts(self, script_ids, sents, parts, chunks_of, cshow, owner):
        out = self.parts_counts(script_ids, sents, parts, chunks_of, cshow, owner)
        rows, raw = self.ctx
        for T in SWEEP:                           # a pick that landed live stays drawn: exact for every T <= PICK_MIN_P
            ev = [dict(e, accepted=e["p_top"] is not None and e["argmax"] is not None and e["p_top"] >= T, chosen=e["argmax"]) for e in rows]
            for k, v in self.parts_counts(script_ids, sents, parts, chunks_of, S.shows_with_picks(raw, ev), owner).items(): self.pw[T][k] += v
        return out
    def at(self, T):
        acc = [e for e in self.picks if e["p_top"] is not None and e["argmax"] is not None and e["p_top"] >= T]
        correct = sum(e["argmax_correct"] for e in acc); on = sum(e["on_parts"] for e in self.picks)
        pw = self.pw[T]
        return {"T": T, "accepted": len(acc), "correct": correct, "precision": S.pct(correct, len(acc)), "recall": S.pct(correct, on),
                "parts_with_pick": {"score": S.pct(pw["hits"], pw["n"]), "precision": S.pct(pw["good"], pw["ovals"])}}

def main():
    ap = argparse.ArgumentParser(description="live picker eval (see module docstring)")
    ap.add_argument("--set", choices=("public", "hidden"), default="public"); ap.add_argument("--record", action="store_true")
    ap.add_argument("--sweep", action="store_true"); ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--cassette"); ap.add_argument("--layout-model"); ap.add_argument("--doc-python"); ap.add_argument("--cases"); ap.add_argument("--holdout")
    a = ap.parse_args()
    public = a.set == "public"
    if a.sweep and not public: fail("--sweep is allowed on the public set only")
    if (urllib.parse.urlparse(a.ollama).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"): fail("--ollama must be a loopback URL")
    if a.record and a.cases: fail("--record needs a complete run (no --cases)")
    ollama = a.ollama.rstrip("/")
    interp = os.path.abspath(a.doc_python) if a.doc_python else sys.executable
    pr = RB.probe(interp)
    if not pr or not pr.get("import_pypdfium2"): fail(f"pypdfium2 not importable in {interp}; use the doc venv")
    layout_model = a.layout_model or os.environ.get("SPEAKEASY_LAYOUT_MODEL")
    layout_model = os.path.abspath(os.path.expanduser(layout_model)) if layout_model else None
    holdout = os.path.abspath(os.path.expanduser(a.holdout or os.environ.get("SPEAKEASY_HOLDOUT") or "~/speakeasy-holdout/bench"))
    first_ms = preflight(ollama)
    sha0 = src_sha()
    tmp = tempfile.mkdtemp(prefix="se_pick_eval_")
    default = os.path.join(BENCH, "pick_cassette.json") if public else os.path.join(holdout, "pick_cassette.json")
    cassette = os.path.abspath(os.path.expanduser(a.cassette or default)) if a.record else os.path.join(tmp, "cassette.json")
    cap = Capture()
    try:
        bench, code = RB.run_once([a.set], holdout, a.cases, interp, False, layout_model, pr, os.path.join(tmp, "last_cases.json"),
                                  pick="proxy", cassettes={a.set: cassette}, upstream=ollama)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if code not in (0, 5) or a.set not in bench["sets"]:
        fail(f"bench run failed (exit {code}): " + "; ".join(e["code"] for e in bench["errors"]))
    if a.record and os.path.exists(cassette):
        if src_sha() != sha0: fail("src/ changed during the run: the cassette is not current; record again")
        c = json.load(open(cassette)); c["header"]["src_sha"] = sha0
        RB.atomic_json(cassette, c)
    pk = bench["sets"][a.set]["pick"]
    warm = sorted(e["ms"] for e in cap.picks if e["called"] and e["ms"] is not None)[1:]      # the first called pick may load the model
    out = {"set": a.set, "pick": pk, "skipped": dict(sorted(cap.skipped.items())), "threshold": partpick.PICK_MIN_P,
           "latency_ms": {"warm_p50": S.nearest_rank(warm, 0.5), "warm_p95": S.nearest_rank(warm, 0.95), "first_call": first_ms},
           "cassette": "written" if a.record and os.path.exists(cassette) else "temp (removed)" if not a.record else "none"}
    if public: out["parts"] = {k: bench["sets"][a.set]["parts"][k] for k in ("score", "precision", "hits", "n")}
    print(json.dumps(out, indent=1))
    if a.sweep:
        print("T     accepted  correct  precision  recall  parts_with_pick score / precision")
        for T in SWEEP:
            r = cap.at(T); pw = r["parts_with_pick"]
            print(f"{T:.2f}  {r['accepted']:8d}  {r['correct']:7d}  {str(r['precision']):>9}  {str(r['recall']):>6}  {pw['score']} / {pw['precision']}")

if __name__ == "__main__":
    main()
