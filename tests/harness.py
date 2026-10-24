#!/usr/bin/env python3
"""Black-box grid harness for speakeasy. Invokes $SPEAKEASY_BIN as subprocess only."""
import json, os, subprocess, sys, tempfile, time, urllib.request, argparse
sys.path.insert(0, os.path.dirname(__file__))
from fixtures import F
from checks import check_output, check_log
import judge
import vision

BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "../src/speakeasy"))
GOOD_URL = os.environ.get("TEST_LLM_URL", "http://localhost:11434/v1")
BAD_URL = "http://127.0.0.1:9/v1"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

def llm_up(url):
    try: urllib.request.urlopen(url.rstrip("/") + "/models", timeout=3); return True
    except Exception: return False

def run(args, inp, env, timeout=180):
    t = time.time()
    try:
        p = subprocess.run([BIN] + args, input=inp, capture_output=True, text=True, env=env, timeout=timeout)
        return p.returncode, p.stdout, p.stderr, time.time() - t
    except subprocess.TimeoutExpired:
        return -999, "", "TIMEOUT", time.time() - t

def read_logs(home):
    p = os.path.join(home, "logs", "runs.jsonl")
    if not os.path.exists(p): return []
    out = []
    for l in open(p, encoding="utf-8"):
        l = l.strip()
        if not l: continue
        try: out.append(json.loads(l))
        except Exception: out.append({"__invalid_json__": l[:200]})
    return out

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--judge", action="store_true"); ap.add_argument("--only"); ap.add_argument("--no-vision", action="store_true"); ap.add_argument("--vision-only", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    report = {"bin": BIN, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": [], "contract": []}
    if not (os.path.isfile(BIN) and os.access(BIN, os.X_OK)):
        report["status"] = "NOT_READY"; report["reason"] = f"{BIN} missing or not executable"
        return finish(report)
    up = llm_up(GOOD_URL)
    conds = {"llm": GOOD_URL, "unreachable": BAD_URL}
    if a.vision_only: conds = {}
    for cond, url in conds.items():
        for name, inp in F.items():
            if a.only and a.only not in name: continue
            rec = {"fixture": name, "condition": cond, "checks": []}
            if cond == "llm" and not up:
                rec["skipped"] = f"LLM not reachable at {GOOD_URL}"; report["results"].append(rec); continue
            home = tempfile.mkdtemp(prefix="se_home_")
            env = dict(os.environ, SPEAKEASY_HOME=home, SPEAKEASY_LLM_URL=url, HOME=home)
            env.pop("SPEAKEASY_LOG_TEXT", None)
            rc, out, err, dt = run(["rewrite"], inp, env)
            rec.update(rc=rc, secs=round(dt, 2), output=out[:2000], stderr=err[:500], in_chars=len(inp), out_chars=len(out))
            rec["checks"] += check_output(name, inp, out, rc, err, cond)
            rec["checks"].append({"check": "run_under_90s", "pass": dt <= 90, "detail": f"{dt:.1f}s"})
            logs = read_logs(home)
            rec["checks"] += check_log(logs[-1] if logs else None, inp, out, cond, False)
            if a.judge and cond == "llm" and inp.strip():
                rec["judge"] = judge.score(inp, out, GOOD_URL)
            report["results"].append(rec)
    if not a.no_vision:
        vm = os.environ.get("SPEAKEASY_VISION_MODEL", "qwen2.5vl:3b")
        try: vup = up and vm in urllib.request.urlopen(GOOD_URL.rstrip("/") + "/models", timeout=3).read().decode()
        except Exception: vup = False
        report["results"] += vision.run_grid(run, read_logs, GOOD_URL, BAD_URL, up, vm, vup)
    report["contract"] = contract_tests(up)
    report["status"] = "RAN"; report["llm_available"] = up
    finish(report)

def contract_tests(up):
    res = []
    def add(cid, ok, detail=""): res.append({"check": cid, "pass": bool(ok), "detail": str(detail)[:300]})
    home = tempfile.mkdtemp(prefix="se_home_")
    env = dict(os.environ, SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_LLM_URL=GOOD_URL if up else BAD_URL)
    txt = F["md_table_clean"]
    rc, out, err, _ = run(["speak", "--dry-run"], txt, env)
    add("speak_dry_run_exit0", rc == 0, f"rc={rc} {err[:100]}"); add("speak_dry_run_prints", out.strip() != "", out[:100])
    add("speak_dry_run_no_pipes", "|" not in out, out[:100])
    rc, out, err, _ = run(["speak", "--rate", "220", "--voice", "Samantha", "--dry-run"], "Hello there.", env)
    add("speak_rate_voice_dry_run_exit0", rc == 0, f"rc={rc} {err[:100]}")
    rc, out, err, _ = run(["speak", "--dry-run"], "", env); add("speak_dry_run_empty_exit0", rc == 0, f"rc={rc}")
    rc, out, err, _ = run(["stop"], "", env); add("stop_exit0_when_nothing_playing", rc == 0, f"rc={rc} {err[:100]}")
    logs = read_logs(home)
    add("logs_all_valid_json", all("__invalid_json__" not in l for l in logs) and logs, len(logs))
    add("speak_logged_command", any(l.get("command") == "speak" for l in logs), [l.get("command") for l in logs])
    # SPEAKEASY_LOG_TEXT=1 includes text
    home2 = tempfile.mkdtemp(prefix="se_home_")
    env2 = dict(env, SPEAKEASY_HOME=home2, SPEAKEASY_LOG_TEXT="1", SPEAKEASY_LLM_URL=BAD_URL)
    marker = "Zebra marker sentence for logging test number 77."
    run(["rewrite"], marker, env2)
    l2 = read_logs(home2)
    add("log_text_opt_in_includes_text", l2 and "Zebra" in json.dumps(l2[-1]), l2[-1] if l2 else None)
    # fallback determinism
    env3 = dict(env, SPEAKEASY_HOME=tempfile.mkdtemp(), SPEAKEASY_LLM_URL=BAD_URL)
    o1 = run(["rewrite"], F["mixed_md_kitchen_sink"], env3)[1]; o2 = run(["rewrite"], F["mixed_md_kitchen_sink"], env3)[1]
    add("fallback_deterministic", o1 == o2 and o1.strip(), "")
    # fallback latency: unreachable LLM must not hang
    t = time.time(); run(["rewrite"], "Short text.", env3); dt = time.time() - t
    add("fallback_fast_under_10s", dt < 10, f"{dt:.1f}s")
    # default HOME logging location
    home4 = tempfile.mkdtemp(); env4 = {k: v for k, v in env3.items() if k != "SPEAKEASY_HOME"}; env4["HOME"] = home4
    run(["rewrite"], "Default home test.", env4)
    add("default_log_path_~/.speakeasy", os.path.exists(os.path.join(home4, ".speakeasy", "logs", "runs.jsonl")), "")
    return res

def finish(report):
    s = {"pass": 0, "fail": 0, "skipped": 0}
    for r in report["results"]:
        if "skipped" in r: s["skipped"] += 1; continue
        for c in r["checks"]: s["pass" if c["pass"] else "fail"] += 1
    for c in report.get("contract", []): s["pass" if c["pass"] else "fail"] += 1
    report["summary"] = s
    json.dump(report, open(os.path.join(OUT, "report.json"), "w"), indent=2, ensure_ascii=False)
    md = [f"# speakeasy test report", f"- bin: `{report['bin']}`", f"- status: **{report['status']}** {report.get('reason','')}",
          f"- LLM available: {report.get('llm_available')}", f"- checks: {s}", ""]
    if report["results"]:
        md += ["| fixture | condition | result | failed checks | judge |", "|---|---|---|---|---|"]
        for r in report["results"]:
            if "skipped" in r: md.append(f"| {r['fixture']} | {r['condition']} | SKIP | {r['skipped']} | |"); continue
            f = [c["check"] + (f" ({c['detail'][:40]})" if c["detail"] else "") for c in r["checks"] if not c["pass"]]
            j = r.get("judge"); js = "" if not j else f"L{j.get('listenability')} F{j.get('fidelity')} C{j.get('conciseness')}"
            md.append(f"| {r['fixture']} | {r['condition']} | {'PASS' if not f else 'FAIL'} | {'; '.join(f).replace('|','¦')} | {js} |")
    if report.get("contract"):
        md += ["", "## Contract checks", "| check | result | detail |", "|---|---|---|"]
        md += [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:80].replace('|','¦')} |" for c in report["contract"]]
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md[:6])); print(f"reports: {OUT}/report.{{json,md}}")
    sys.exit(0 if report["status"] == "RAN" and s["fail"] == 0 else 1)

if __name__ == "__main__": main()
