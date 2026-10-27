#!/usr/bin/env python3
"""Live stage C eval against the real local Ollama (docs/contracts/phase6-block-classifier.md 12.5; optional, never gated).

    python3 tools/kind_eval.py [--set public|hidden] [--record] [--sweep] [--ollama URL]
                               [--layout-model PATH] [--figcls-model PATH] [--doc-python PATH] [--holdout DIR]

Runs the gap bench in kind proxy mode (run_bench.py --family gap --kind proxy, KIND_PROMPT requests forwarded by the mock to
Ollama) and prints the set's kind_vlm block, the warm stage C p50/p95 and the first-call time (the preflight probe, which loads the
model when it is not loaded yet). --record writes the set's kind cassette (run_bench's default path for the set); without it the
cassette goes to a temp file.
  --sweep   public only: every public gap doc is parsed in a temp home with stage C on, its kind job runs against Ollama, and each
            stage C answer (kind and p_top, logged whatever the threshold) is matched to the GT regions (IoU >= 0.5, same kind).
            Prints decisions, correct and precision for T in 0.50, 0.55, ..., 0.95 (contract 13; the choice goes into Amendment 2).
Hidden: aggregates only, no sweep. Temp homes only (never ~/.speakeasy). Exit 2 when the preflight fails."""
import argparse, base64, json, os, struct, subprocess, sys, tempfile, time, urllib.parse, urllib.request, zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC, BENCH = os.path.join(REPO, "src"), os.path.join(REPO, "tests", "audit", "bench")
sys.path.insert(0, SRC); sys.path.insert(0, BENCH)
import blockcls
import score as S

MODEL = "qwen3-vl:4b-instruct"
SWEEP = [round(0.50 + 0.05 * k, 2) for k in range(10)]

def fail(msg):
    print(f"kind_eval: {msg}", file=sys.stderr); sys.exit(2)

def http_json(url, body=None, timeout=120):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode())

def white_png(side):
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\xff" * 3 * side for _ in range(side))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

def preflight(ollama):
    """Ollama reachable, the model present, one KIND_PROMPT probe returns top_logprobs. Returns the probe's ms."""
    try: http_json(ollama + "/api/version")
    except (OSError, ValueError) as e: fail(f"Ollama not reachable at {ollama} ({type(e).__name__})")
    if not any(MODEL in (m.get("name"), m.get("model")) for m in http_json(ollama + "/api/tags").get("models") or []):
        fail(f"model {MODEL} not present (ollama pull {MODEL})")
    t0 = time.time()
    try: reply = http_json(ollama + "/api/chat", blockcls.request_body(MODEL, "10m", base64.b64encode(white_png(32)).decode()))
    except (OSError, ValueError) as e: fail(f"probe request failed ({type(e).__name__})")
    if not ((reply.get("logprobs") or [{}])[0].get("top_logprobs")): fail("probe reply has no top_logprobs (Ollama too old?)")
    return int((time.time() - t0) * 1000)

def env_for(home, ollama, layout_model, figcls_model):
    e = {k: v for k, v in os.environ.items() if not k.startswith("SPEAKEASY_")}
    e.update(SPEAKEASY_HOME=home, SPEAKEASY_LLM_URL=ollama + "/v1", SPEAKEASY_GROUND="local", SPEAKEASY_BLOCKCLS_VLM="local",
             SPEAKEASY_VISION_MODEL=MODEL, SPEAKEASY_TRACKER=os.path.join(home, "no-tracker"))
    if layout_model: e["SPEAKEASY_LAYOUT_MODEL"] = layout_model
    if figcls_model: e["SPEAKEASY_FIGCLS_MODEL"] = figcls_model
    return e

def sweep(interp, ollama, layout_model, figcls_model, tmp):
    """(rows [(p_top, kind, correct)], ms) over the public gap docs' stage C answers."""
    fx = os.path.join(BENCH, "fixtures", "public-gap"); rows, ms = [], []
    for d in sorted(x for x in os.listdir(fx) if os.path.isdir(os.path.join(fx, x))):
        home = tempfile.mkdtemp(dir=tmp); os.makedirs(os.path.join(home, "cache", "doc"))
        cache = os.path.join(home, "cache", "doc", "0" * 20 + ".json"); env = env_for(home, ollama, layout_model, figcls_model)
        subprocess.run([interp, os.path.join(SRC, "docmap.py"), "parse-pdf", os.path.join(fx, d, "doc.pdf"), cache], env=env, check=True, capture_output=True)
        if json.load(open(cache))["blockcls"]["kind_job"] != "queued": continue
        subprocess.run([interp, os.path.join(SRC, "blockcls.py"), "job", os.path.join(fx, d, "doc.pdf"), cache], env=env, check=True, capture_output=True)
        side = json.load(open(blockcls.sidecar_path(cache)))
        gt = {p["n"]: p for p in json.load(open(os.path.join(fx, d, "gt.json")))["pages"] if p.get("kind_scored")}
        for e in side["entries"]:
            if e["page"] not in gt or e["p_top"] is None: continue
            if isinstance(e.get("ms"), int): ms.append(e["ms"])
            hit = lambda k: any(g["kind"] == k and S.iou(g["bbox"], e["rect"]) >= 0.5 for g in gt[e["page"]]["blocks"])
            rows.append((e["p_top"], e["kind"], hit(e["kind"]) if e["kind"] in ("figure", "table") else not (hit("figure") or hit("table"))))
    return rows, ms

def main():
    ap = argparse.ArgumentParser(description="live stage C eval (see module docstring)")
    ap.add_argument("--set", choices=("public", "hidden"), default="public"); ap.add_argument("--record", action="store_true")
    ap.add_argument("--sweep", action="store_true"); ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--layout-model"); ap.add_argument("--figcls-model"); ap.add_argument("--doc-python"); ap.add_argument("--holdout")
    a = ap.parse_args()
    if a.sweep and a.set != "public": fail("--sweep is allowed on the public set only")
    if (urllib.parse.urlparse(a.ollama).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"): fail("--ollama must be a loopback URL")
    ollama = a.ollama.rstrip("/"); interp = os.path.abspath(a.doc_python) if a.doc_python else sys.executable
    layout_model = a.layout_model or os.environ.get("SPEAKEASY_LAYOUT_MODEL"); figcls_model = a.figcls_model or os.environ.get("SPEAKEASY_FIGCLS_MODEL")
    if not figcls_model: fail("--figcls-model (or SPEAKEASY_FIGCLS_MODEL) is required: without stage B every candidate routes low-p")
    first_ms = preflight(ollama)
    tmp = tempfile.mkdtemp(prefix="se_kind_eval_")
    out = os.path.join(tmp, "bench.json")
    cmd = [interp, os.path.join(BENCH, "run_bench.py"), "--family", "gap", "--set", a.set, "--kind", "proxy", "--kind-upstream", ollama,
           "--figcls-model", figcls_model, "--out", out] + (["--layout-model", layout_model] if layout_model else []) + (["--holdout", a.holdout] if a.holdout else [])
    if not a.record: cmd += ["--kind-hidden-cassette" if a.set == "hidden" else "--kind-cassette", os.path.join(tmp, "kind_cassette.json")]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if not os.path.exists(out): fail(f"bench run did not complete (exit {r.returncode})")
    blk = json.load(open(out))["sets"][a.set + "-gap"]
    kv = blk.get("kind_vlm") or {}
    print(f"kind_vlm {kv.get('score')} ({kv.get('hits')}/{kv.get('gt')}) precision {kv.get('precision')} ({kv.get('matched')}/{kv.get('pred')}) "
          f"requests {kv.get('requests')} replay_miss {kv.get('replay_miss')} job_timeouts {kv.get('job_timeouts')}")
    print(f"stage C warm p50 {(kv.get('ms') or {}).get('p50')} ms p95 {(kv.get('ms') or {}).get('p95')} ms | first call {first_ms} ms | bench exit {r.returncode}")
    if a.sweep:
        rows, ms = sweep(interp, ollama, layout_model, figcls_model, tmp)
        print(f"sweep: {len(rows)} stage C answers on kind-scored pages, request ms p50 {S.nearest_rank(ms, 0.5)} p95 {S.nearest_rank(ms, 0.95)}")
        for T in SWEEP:
            dec = [x for x in rows if x[1] in ("figure", "table") and x[0] >= T]
            good = sum(x[2] for x in dec); txt = [x for x in rows if x[1] == "text" and x[0] >= T]
            print(f"T_C {T:.2f}: figure/table decisions {len(dec)} correct {good} precision {S.pct(good, len(dec))} | text {len(txt)} correct {sum(x[2] for x in txt)}")

if __name__ == "__main__":
    main()
