#!/usr/bin/env python3
"""Black-box: nothing-selected capture (no-selection-window / -screen), copied-text precedence, cleanup, config key."""
import json, os, re, shutil, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); AUD = os.path.dirname(HERE); sys.path.insert(0, AUD)
from schema import validate, flatten
BIN = os.environ.get("SPEAKEASY_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../src/speakeasy")); URL = os.environ.get("TEST_LLM_URL", "http://localhost:11434/v1")
SHOT = os.path.join(os.path.dirname(AUD), "images", "screenshot.png"); R = []
def add(c, ok, d=""): R.append({"check": c, "pass": bool(ok), "detail": str(d)[:240]})
def env(clip="", **kw):
    h = tempfile.mkdtemp(prefix="se_nosel_")
    e = dict(os.environ, SPEAKEASY_HOME=h, HOME=h, SPEAKEASY_LLM_URL=URL, STUB_CLIP=clip, STUB_SCREEN=SHOT, STUB_LOG=os.path.join(h, "stub.jsonl"),
             PATH=os.path.join(HERE, "stubs") + ":" + os.path.join(AUD, "tts", "stubs") + ":" + os.environ["PATH"])
    for k in ("SPEAKEASY_NO_SELECTION_CAPTURE", "SPEAKEASY_TTS"): e.pop(k, None)
    e.update({k: str(v) for k, v in kw.items()}); return h, e
def copy_shot():
    d = tempfile.mkdtemp(prefix="se_shot_"); c = os.path.join(d, "shot.png"); shutil.copy(SHOT, c); return c
def run(args, e, inp=""):
    t = time.time(); p = subprocess.run([BIN] + args, input=inp, capture_output=True, text=True, env=e, timeout=300); return p.returncode, p.stdout, p.stderr, time.time() - t
def last(h):
    p = os.path.join(h, "logs", "runs.jsonl"); L = [json.loads(x) for x in open(p) if x.strip()] if os.path.exists(p) else []; return flatten(L[-1]) if L else {}
def sentences(t): return [s for s in re.split(r"(?<=[.!?])\s+", t.strip()) if len(s.split()) >= 3]
def leftovers(h):
    keep = {"last_capture.png"}; pngs = []
    for root, _, fs in os.walk(h):
        for f in fs:
            if f.lower().endswith((".png", ".jpg")) and f not in keep: pngs.append(os.path.join(root, f))
    return pngs
if os.path.exists("/tmp/speakeasy_sel.png"): os.remove("/tmp/speakeasy_sel.png")

for method in ("no-selection-window", "no-selection-screen"):
    tag = method.split("-")[-1]
    h, e = env(); img = copy_shot()
    rc, out, err, dt = run(["selection", "--image", img, "--capture-method", method, "--dry-run"], e); l = last(h)
    add(f"{tag}_exit0", rc == 0, f"rc={rc} {err[:120]}")
    n = len(sentences(out)); add(f"{tag}_2to4_sentences", 2 <= n <= 4, f"{n}: {out[:200]}")
    add(f"{tag}_mentions_screen_content", any(w in out.lower() for w in ("latency", "migration", "480", "210")), out[:160])
    add(f"{tag}_log_method", l.get("capture_method") == method, l.get("capture_method"))
    add(f"{tag}_log_image_true", l.get("image") is True, l.get("image"))
    add(f"{tag}_schema", not validate(l), validate(l))
    add(f"{tag}_only_last_capture_remains", not leftovers(h) and not os.path.exists("/tmp/speakeasy_sel.png"), leftovers(h))
    add(f"{tag}_last_capture_kept", os.path.exists(os.path.join(h, "last_capture.png")), "")
    add(f"{tag}_under_90s", dt < 90, f"{dt:.1f}s")

# copied text exists -> image dropped; pre-seed stale last_capture.png
h, e = env(clip="The quarterly report shows revenue of 12 million dollars.")
shutil.copy(SHOT, os.path.join(h, "last_capture.png")); img = copy_shot()
rc, out, err, dt = run(["selection", "--image", img, "--capture-method", "no-selection-window", "--dry-run"], e); l = last(h)
add("text_exists_exit0", rc == 0, f"rc={rc} {err[:100]}")
add("text_exists_method_none", l.get("capture_method") == "none", l.get("capture_method"))
add("text_exists_image_false", l.get("image") is False, l.get("image"))
add("text_exists_capture_error", "capture:" in str(l.get("error")), l.get("error"))
add("text_exists_uses_text", "12" in out or "revenue" in out.lower(), out[:150])
add("text_exists_no_screen_content", not any(w in out.lower() for w in ("latency", "migration", "safari")), out[:150])
add("text_exists_schema", not validate(l), validate(l))
add("text_exists_no_stale_last_capture", not os.path.exists(os.path.join(h, "last_capture.png")), "stale last_capture.png survived a run with no image")
add("text_exists_no_tmp_sel", not os.path.exists("/tmp/speakeasy_sel.png") and not leftovers(h), leftovers(h))
add("text_exists_user_image_kept", os.path.exists(img), f"exists={os.path.exists(img)}")
pa = subprocess.run([sys.executable, os.path.join(AUD, "privacy_audit.py"), "--thumb", os.path.join(h, "t.png")], capture_output=True, text=True, env=e)
add("privacy_audit_stale_check_clean", "STALE_CAPTURE" not in pa.stdout, pa.stdout[:200])

# hotkey temp file with copied text present -> dropped AND deleted
shutil.copy(SHOT, "/tmp/speakeasy_cap_sel.png"); h, e = env(clip="Copied words win here.")
run(["selection", "--image", "/tmp/speakeasy_cap_sel.png", "--capture-method", "no-selection-window", "--dry-run"], e)
add("hotkey_temp_deleted_when_text_wins", not os.path.exists("/tmp/speakeasy_cap_sel.png") and not os.path.exists(os.path.join(h, "last_capture.png")), "")
shutil.copy(SHOT, "/tmp/speakeasy_cap_win.png"); h, e = env()
run(["selection", "--image", "/tmp/speakeasy_cap_win.png", "--capture-method", "no-selection-window", "--dry-run"], e)
add("hotkey_temp_moved_to_last_capture", not os.path.exists("/tmp/speakeasy_cap_win.png") and os.path.exists(os.path.join(h, "last_capture.png")), "")
# plain text selection (no capture method) also must not leave stale capture
h, e = env(clip="Plain copied sentence for the test."); shutil.copy(SHOT, os.path.join(h, "last_capture.png"))
run(["selection", "--dry-run"], e); l = last(h)
add("plain_text_capture_path_null", l.get("capture_path") is None, l.get("capture_path"))
add("plain_text_run_no_stale_last_capture", not (os.path.exists(os.path.join(h, "last_capture.png")) and not l.get("image")), f"image={l.get('image')} exists={os.path.exists(os.path.join(h, 'last_capture.png'))}")

# config key + env precedence (tool captures itself via screencapture stub when no --image)
for val, expect in (("window", "none"), ("screen", "no-selection-screen"), ("none", "none")):
    h, e = env(); open(os.path.join(h, "config"), "w").write(f"SPEAKEASY_NO_SELECTION_CAPTURE={val}\n")
    rc, out, err, dt = run(["selection", "--dry-run"], e); l = last(h)
    stub = open(e["STUB_LOG"]).read() if os.path.exists(e["STUB_LOG"]) else ""
    add(f"config_{val}_exit0", rc == 0, f"rc={rc} {err[:100]}")
    add(f"config_{val}_method", l.get("capture_method") == expect, f"{l.get('capture_method')} stub={stub[:160]}")
    if val == "window":
        add("window_lookup_fail_capture_error", "window lookup failed" in str(l.get("capture_error")) and str(l.get("capture_error")).startswith("capture:"), l.get("capture_error"))
        add("window_lookup_fail_image_false", l.get("image") is False, l.get("image"))
        add("window_lookup_fail_no_screencapture", "screencapture" not in stub, stub[-160:])
        add("window_lookup_fail_capture_ms_numeric", isinstance(l.get("capture_ms"), (int, float)), l.get("capture_ms"))
        add("window_lookup_fail_no_last_capture", not os.path.exists(os.path.join(h, "last_capture.png")) and l.get("capture_path") is None, l.get("capture_path"))
        add("window_lookup_fail_error_joined", "window lookup failed" in str(l.get("error")), l.get("error"))
        add("window_lookup_fail_schema", not validate(l), validate(l))
    if val == "screen":
        add(f"config_{val}_screencapture_called", "screencapture" in stub, stub[-160:])
        add(f"config_{val}_capture_ms_logged", isinstance(l.get("capture_ms"), (int, float)), l.get("capture_ms"))
        add(f"config_{val}_image_true", l.get("image") is True, l.get("image"))
        add(f"config_{val}_schema", not validate(l), validate(l))
    if val == "none": add("config_none_no_screencapture", "screencapture" not in stub, stub[:120])
    if val != "none": add(f"config_{val}_no_leftovers", not leftovers(h) and not os.path.exists("/tmp/speakeasy_sel.png"), leftovers(h))
h, e = env(SPEAKEASY_NO_SELECTION_CAPTURE="none"); open(os.path.join(h, "config"), "w").write("SPEAKEASY_NO_SELECTION_CAPTURE=screen\n")
run(["selection", "--dry-run"], e); l = last(h); add("env_none_overrides_config_screen", l.get("capture_method") == "none" and not l.get("image"), l.get("capture_method"))

res = {"checks": R, "summary": {"pass": sum(c["pass"] for c in R), "fail": sum(not c["pass"] for c in R)}}
json.dump(res, open(os.path.join(AUD, "reports", "nosel.json"), "w"), indent=2)
md = ["# no-selection capture tests", f"summary: {res['summary']}", "", "| check | result | detail |", "|---|---|---|"] + [f"| {c['check']} | {'PASS' if c['pass'] else 'FAIL'} | {c['detail'][:140].replace('|','¦')} |" for c in R]
open(os.path.join(AUD, "reports", "nosel.md"), "w").write("\n".join(md) + "\n"); print("\n".join(md))
