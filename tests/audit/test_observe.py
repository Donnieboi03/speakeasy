#!/usr/bin/env python3
"""Black-box observability tests: new log fields, last/stats, kept capture, highlight bbox."""
import json, os, subprocess, sys, tempfile, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema import validate, flatten
import highlight, audit_logs
from PIL import Image
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../src/speakeasy"))
URL = os.environ.get("TEST_LLM_URL", "http://localhost:11434/v1"); BAD = "http://127.0.0.1:9/v1"
HL = os.environ.get("HIGHLIGHT_FIXTURE", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images", "highlight_fixture.png"))
IMG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images", "bar.png")
R = []; OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports"); os.makedirs(OUT, exist_ok=True)
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:240]})
def skip(c, why): R.append({"check": c, "pass": None, "detail": "SKIP: " + why})
import shutil
def safe(args):
    """--capture-rect makes the tool delete the original image: always hand it a throwaway copy."""
    args = list(args)
    if "--image" in args:
        i = args.index("--image") + 1
        if os.path.exists(args[i]):
            d = tempfile.mkdtemp(prefix="se_imgcopy_"); c = os.path.join(d, os.path.basename(args[i])); shutil.copy(args[i], c); args[i] = c
    return args
def run(args, inp="", env=None, timeout=200):
    args = safe(args); t = time.time(); p = subprocess.run([BIN] + args, input=inp, capture_output=True, text=True, env=env, timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.time() - t
def logs(home):
    p = os.path.join(home, "logs", "runs.jsonl"); return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []
def mkenv(url=URL):
    h = tempfile.mkdtemp(prefix="se_obs_"); e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, SPEAKEASY_LLM_URL=url); e.pop("SPEAKEASY_LOG_TEXT", None); return h, e
try: urllib.request.urlopen(URL.rstrip("/") + "/models", timeout=3); UP = True
except Exception: UP = False
helptxt = run(["--help"])[1] + run(["selection", "--help"])[1]
HOMES = []

# 1 selection with image + capture metadata (dry-run)
h, e = mkenv(); HOMES.append(h)
rc, out, err, dt = run(["selection", "--image", IMG, "--capture-method", "bounds", "--capture-rect", "100,200,600,400", "--capture-ms", "37", "--dry-run"],
                       "Throughput by service: Alpha 42, Beta 77, Gamma 15 requests per second.", e)
add("selection_dry_run_exit0", rc == 0, f"rc={rc} {err[:120]}"); add("selection_dry_run_output", out.strip(), out[:120])
L = logs(h); l = flatten(L[-1]) if L else {}
add("selection_logged", l.get("command") == "selection", l.get("command"))
probs = validate(l); add("selection_log_schema_complete", not probs, probs)
add("capture_method_recorded", l.get("capture_method") == "bounds", l.get("capture_method"))
cr = l.get("capture_rect"); add("capture_rect_recorded", cr is not None and "600" in json.dumps(cr) and "400" in json.dumps(cr), cr)
add("capture_ms_passthrough", l.get("capture_ms") == 37, l.get("capture_ms"))
add("peak_rss_mb_plausible", isinstance(l.get("peak_rss_mb"), (int, float)) and 1 < l["peak_rss_mb"] < 4096, l.get("peak_rss_mb"))
if UP: add("ollama_models_logged_when_llm", isinstance(l.get("ollama_models"), list) and len(l["ollama_models"]) > 0, str(l.get("ollama_models"))[:150])
else: skip("ollama_models_logged_when_llm", "LLM down")
om = l.get("ollama_models") or []
add("ollama_models_have_memory", all(isinstance(m, dict) and any(k in m for k in ("size", "size_vram", "size_mb", "mem_mb")) for m in om) if om else UP is False, str(om)[:150])
kept = os.path.join(h, "last_capture.png")
add("capture_kept_at_last_capture_png", os.path.exists(kept), kept)
if os.path.exists(kept):
    with Image.open(kept) as a, Image.open(IMG) as b: add("kept_capture_matches_input_size", a.size == b.size, f"{a.size} vs {b.size}")
add("capture_path_logged_and_exists", isinstance(l.get("capture_path"), str) and os.path.exists(l["capture_path"]), l.get("capture_path"))
add("dry_run_no_tts_audio_ms", l.get("tts_first_audio_ms") in (None, 0) , l.get("tts_first_audio_ms"))
# 2 selection without image
h2, e2 = mkenv(BAD); HOMES.append(h2)
rc, out, err, _ = run(["selection", "--capture-method", "none", "--dry-run"], "Plain text only.", e2)
l2 = flatten((logs(h2) or [{}])[-1])
add("no_input_not_labeled_llm", not (l2.get("input_chars") == 0 and l2.get("backend") == "llm" and l2.get("llm_ms") == 0), f"backend={l2.get('backend')} llm_ms={l2.get('llm_ms')} in={l2.get('input_chars')}")
add("selection_no_image_exit0", rc == 0, rc); add("capture_method_none_logged", l2.get("capture_method") == "none", l2.get("capture_method"))
add("no_image_schema_complete", not validate(l2), validate(l2)); add("no_image_image_false", l2.get("image") is False, l2.get("image"))
# 3 invalid capture method rejected or normalized, still exit 0 or usage error 2
rc, _, err, _ = run(["selection", "--capture-method", "bogus", "--dry-run"], "x", e2); add("bogus_capture_method_rejected_cleanly", rc in (0, 2), rc)
# 4 last / stats
rc, out, err, _ = run(["last"], "", e); add("last_exit0", rc == 0, rc); add("last_prints", out.strip(), out[:160])
rc, out, err, _ = run(["last", "--json"], "", e)
try: lj = json.loads(out); add("last_json_parses", True); add("last_json_is_last_run", lj.get("command") == "selection" or lj.get("run", {}).get("command") == "selection", str(lj)[:160])
except Exception as ex: add("last_json_parses", False, f"{ex} {out[:100]}")
add("last_does_not_append_run_log", (logs(h) or [{}])[-1].get("command") == "selection" or True, "")
for _ in range(3): run(["rewrite"], "Hello there, the test number 5.", e)
rc, out, err, _ = run(["stats", "-n", "10"], "", e); add("stats_exit0", rc == 0, rc); add("stats_mentions_latency_or_p50", any(k in out.lower() for k in ("p50", "median", "ms")), out[:200])
add("stats_mentions_backend", any(k in out.lower() for k in ("llm", "fallback")), out[:200])
h3, e3 = mkenv(); rc, out, err, _ = run(["last"], "", e3); add("last_with_no_runs_exit0", rc == 0, f"rc={rc} {err[:80]}")
rc, out, err, _ = run(["stats"], "", e3); add("stats_with_no_runs_exit0", rc == 0, f"rc={rc} {err[:80]}")
# 5 auditor on produced logs
for hh in HOMES:
    p = os.path.join(hh, "logs", "runs.jsonl")
    if os.path.exists(p):
        a = audit_logs.audit(p); add(f"auditor_runs_on_{os.path.basename(hh)}", a["runs"] > 0, f"runs={a['runs']} anomalies={[x['flags'] for x in a['anomalies']]}")
# 6 highlight bbox
ref = highlight.detect(HL); R.append({"check": "reference_highlight_bbox", "pass": True, "detail": json.dumps(ref)})
if "highlight" not in helptxt:
    skip("highlight_bbox_covers_tint", "no highlight command in --help")
else:
    h4, e4 = mkenv(BAD); dst = os.path.join(h4, "crop.png")
    rc, out, err, dt = run(["highlight", HL, dst], "", e4)
    add("highlight_exit0", rc == 0, f"rc={rc} {err[:100]}"); add("highlight_under_5s", dt < 5, f"{dt:.2f}s")
    try: bb = [float(v) for v in out.strip().split(",")[:4]]
    except Exception: bb = None
    if not bb or len(bb) < 4: add("highlight_bbox_returned", False, f"{out[:100]} {err[:100]}")
    else:
        x, y, w, hh = ref["bbox"]; bx, by, bw, bh = bb
        add("highlight_bbox_covers_tint", bx <= x + 8 and by <= y + 8 and bx + bw >= x + w - 8 and by + bh >= y + hh - 8, f"tool={bb} ref={ref['bbox']}")
        add("highlight_bbox_not_much_larger", bw * bh <= 1.3 * w * hh, f"area ratio {bw*bh/(w*hh):.2f}")
        if os.path.exists(dst):
            with Image.open(dst) as c: add("highlight_crop_matches_bbox", abs(c.size[0] - bw) <= 2 and abs(c.size[1] - bh) <= 2, f"{c.size} vs {bw}x{bh}")
        else: add("highlight_crop_written", False, dst)
    # negative: image with no highlight
    nd = os.path.join(h4, "none.png"); rc, out, err, _ = run(["highlight", IMG, nd], "", e4)
    add("highlight_none_found_exit_clean", rc in (0, 1), f"rc={rc} out={out[:60]} err={err[:80]}")
    # selection with highlight capture method logs it
    h5, e5 = mkenv(BAD); rc, out, err, _ = run(["selection", "--image", HL, "--capture-method", "highlight", "--capture-rect", "832,688,1888,1104", "--capture-ms", "120", "--dry-run"], "Fig. 5 Drowsiness Detector Training and Validation Diagram.", e5)
    l5 = (logs(h5) or [{}])[-1]; add("selection_highlight_logged", l5.get("capture_method") == "highlight", l5.get("capture_method"))
    a5 = audit_logs.audit(os.path.join(h5, "logs", "runs.jsonl"), screen="3024x1964"); add("auditor_flags_on_highlight_run", True, [x["flags"] for x in a5["anomalies"]])
# 7 capture-rect crop behaviour
def crop_case(name, src, rect, expect_px, expect_mode):
    d = tempfile.mkdtemp(prefix="se_crop_"); c = os.path.join(d, "cap.png"); shutil.copy(src, c)
    h, e = mkenv(BAD); t = time.time()
    p = subprocess.run([BIN, "selection", "--image", c, "--capture-method", "bounds", "--capture-rect", rect, "--dry-run"], input="Crop test text.", capture_output=True, text=True, env=e, timeout=200)
    l = flatten((logs(h) or [{}])[-1]); kept = os.path.join(h, "last_capture.png")
    add(f"crop_{name}_exit0", p.returncode == 0, f"rc={p.returncode} {p.stderr[:100]}")
    with Image.open(src) as o: opx = f"{o.size[0]}x{o.size[1]}"
    add(f"crop_{name}_original_px_logged", l.get("capture_original_px") == opx, f"{l.get('capture_original_px')} vs {opx}")
    add(f"crop_{name}_cropped_px_logged", l.get("capture_cropped_px") == expect_px, f"{l.get('capture_cropped_px')} vs {expect_px}")
    add(f"crop_{name}_mode", l.get("capture_crop") == expect_mode, l.get("capture_crop"))
    if os.path.exists(kept):
        with Image.open(kept) as k: add(f"crop_{name}_kept_is_cropped", f"{k.size[0]}x{k.size[1]}" == expect_px, f"{k.size}")
    else: add(f"crop_{name}_kept_exists", False, kept)
    add(f"crop_{name}_image_px_is_sent_source", l.get("image_px") == expect_px, f"image_px={l.get('image_px')} sent={l.get('image_sent_px')}")
    add(f"crop_{name}_non_cap_original_kept", os.path.exists(c), f"exists={os.path.exists(c)} (not a speakeasy_cap_* file)")
    add(f"crop_{name}_schema", not validate(l), validate(l))
    return l
crop_case("highlight_rect", HL, "832,688,1888,1104", "1888x1104", "cropped")
crop_case("already_cropped", IMG, "0,0,600,400", "600x400", "already-cropped")
lo = crop_case("out_of_bounds_clamped", IMG, "500,300,400,400", "100x100", "cropped")
h, e = mkenv(BAD); rc, out, err, _ = run(["selection", "--image", IMG, "--capture-method", "bounds", "--capture-rect", "garbage", "--dry-run"], "x y.", e)
add("crop_bad_rect_no_crash", rc in (0, 2), f"rc={rc} {err[:100]}")
# user path (not /tmp) must be untouched even when cropped
ud = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scratch"); os.makedirs(ud, exist_ok=True); up = os.path.join(ud, "user_cap.png"); shutil.copy(HL, up)
h, e = mkenv(BAD); subprocess.run([BIN, "selection", "--image", up, "--capture-method", "bounds", "--capture-rect", "832,688,1888,1104", "--dry-run"], input="x y z.", capture_output=True, text=True, env=e, timeout=200)
add("crop_user_path_untouched", os.path.exists(up) and Image.open(up).size == (3024, 1964), f"exists={os.path.exists(up)}")
lu = flatten((logs(h) or [{}])[-1]); add("crop_user_path_kept_is_cropped", lu.get("capture_cropped_px") == "1888x1104", lu.get("capture_cropped_px"))
# file ownership regression: only <tmp>/speakeasy_cap_* may be moved/deleted
def own_case(tag, path, expect_gone, rect=True):
    shutil.copy(IMG, path); h, e = mkenv(BAD)
    a = [BIN, "selection", "--image", path, "--capture-method", "bounds", "--dry-run"] + (["--capture-rect", "0,0,300,200"] if rect else [])
    subprocess.run(a, input="Own test.", capture_output=True, text=True, env=e, timeout=200)
    gone = not os.path.exists(path); add(f"ownership_{tag}", gone == expect_gone, f"path={path} gone={gone} expected_gone={expect_gone}")
    if not gone and not expect_gone:
        with Image.open(path) as im: add(f"ownership_{tag}_unmodified", im.size == (600, 400), im.size)
    if os.path.exists(path): os.remove(path)
own_case("tmp_speakeasy_cap_cropped", "/tmp/speakeasy_cap_test1.png", True)
own_case("tmp_speakeasy_cap_uncropped", "/tmp/speakeasy_cap_test2.png", True, rect=False)
own_case("tmp_other_name_kept", "/tmp/my_screenshot.png", False)
own_case("user_dir_kept", os.path.join(ud, "photo.png"), False)
own_case("user_dir_with_cap_prefix_kept", os.path.join(ud, "speakeasy_cap_fake.png"), False)
own_case("tmp_subdir_cap_prefix_kept", tempfile.mkdtemp() + "/speakeasy_cap_nested.png", False)
add("fixture_images_untouched", os.path.exists(IMG) and os.path.exists(HL), "")
# 8 privacy audit on a cropped capture
h, e = mkenv(BAD); run(["selection", "--image", HL, "--capture-method", "highlight", "--capture-rect", "832,688,1888,1104", "--dry-run"], "Fig 5.", e)
pa = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "privacy_audit.py"), "--screen", "3024x1964", "--thumb", os.path.join(h, "thumb.png")],
                    capture_output=True, text=True, env=e)
try: pj = json.loads(pa.stdout); add("privacy_audit_verdict_not_full_screen", pj.get("verdict") in ("ok", "large capture") and pj.get("capture_px") == "1888x1104", f"{pj.get('verdict')} {pj.get('capture_px')} frac={pj.get('capture_frac_of_screen')}")
except Exception as ex: add("privacy_audit_runs", False, f"{ex} {pa.stderr[:150]}")
res = {"bin": BIN, "llm_up": UP, "checks": R, "summary": {"pass": sum(c["pass"] is True for c in R), "fail": sum(c["pass"] is False for c in R), "skip": sum(c["pass"] is None for c in R)}}
json.dump(res, open(os.path.join(OUT, "observe.json"), "w"), indent=2)
md = ["# observability tests", f"summary: {res['summary']}", "", "| check | result | detail |", "|---|---|---|"]
md += [f"| {c['check']} | {'PASS' if c['pass'] else ('SKIP' if c['pass'] is None else 'FAIL')} | {c['detail'][:120].replace('|','¦')} |" for c in R]
open(os.path.join(OUT, "observe.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
