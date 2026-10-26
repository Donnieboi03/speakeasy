#!/usr/bin/env python3
"""OCR helper stand-in for the bench (docs/contracts/phase6-block-classifier.md 12.4, --ocr replay|record). The harness points
SPEAKEASY_OCR_BIN at the `ocr_stub` wrapper next to this file; the app calls it like the tracker's subcommand:

  ocr_stub [ocr] IN.png OUT.json

OCR_STUB_MODE=replay   answer from OCR_STUB_CASSETTE, keyed by sha256(IN.png): write the stored OUT.json verbatim and exit with the
                       stored code; a key not in the cassette exits 3 without output (the app must treat it as a failing helper)
OCR_STUB_MODE=record   run the real helper (OCR_STUB_REAL, e.g. speakeasy-track) with the same arguments, store its OUT.json and exit
                       code under the PNG's sha256, and pass both through
OCR_STUB_LOG           JSONL of {"event": "ocr_reply", "png_sha", "result": hit|miss|recorded|real-failed|bad-args, "run"} (no text);
                       "run" is the content of OCR_STUB_RUN_TAG_FILE, so warm-up and measured runs can be told apart.
The cassette stores the helper's output as returned (line boxes and recognised text of fixture images), so the hidden cassette lives
in the holdout dir only. Stdlib only."""
import fcntl, hashlib, json, os, platform, subprocess, sys, time


def log(rec):
    p = os.environ.get("OCR_STUB_LOG")
    if not p: return
    tf = os.environ.get("OCR_STUB_RUN_TAG_FILE")
    try: rec["run"] = open(tf).read().strip() or None if tf else None
    except OSError: rec["run"] = None
    with open(p, "a") as f: f.write(json.dumps(dict(rec, t=time.time())) + "\n")


def load(path):
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) and isinstance(d.get("entries"), dict) else {"header": {}, "entries": {}}
    except (OSError, ValueError):
        return {"header": {}, "entries": {}}


def main(argv):
    args = argv[1:] if argv[:1] == ["ocr"] else argv
    if len(args) != 2: log({"event": "ocr_reply", "png_sha": None, "result": "bad-args"}); return 2
    src, dst = args; mode = os.environ.get("OCR_STUB_MODE", "replay"); cas_path = os.environ.get("OCR_STUB_CASSETTE", "")
    try: key = hashlib.sha256(open(src, "rb").read()).hexdigest()
    except OSError: log({"event": "ocr_reply", "png_sha": None, "result": "bad-args"}); return 2
    if mode == "record":
        real = os.environ.get("OCR_STUB_REAL", "")
        p = subprocess.run([real, "ocr", src, dst], capture_output=True, timeout=120) if real and os.access(real, os.X_OK) else None
        if p is None or p.returncode != 0 or not os.path.exists(dst):
            log({"event": "ocr_reply", "png_sha": key, "result": "real-failed"}); return p.returncode if p and p.returncode else 4
        out = json.load(open(dst))
        with open(cas_path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX); cas = load(cas_path)
            cas["entries"][key] = {"out": out, "rc": 0}
            if not cas["header"].get("recorded_at"):
                cas["header"] = {"helper_sha256": hashlib.sha256(open(real, "rb").read()).hexdigest()[:16], "macos": platform.mac_ver()[0],
                                 "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            tmp = cas_path + ".tmp"
            with open(tmp, "w") as f: json.dump({"header": cas["header"], "entries": dict(sorted(cas["entries"].items()))}, f, indent=1, sort_keys=True, ensure_ascii=False); f.write("\n")
            os.replace(tmp, cas_path)
        log({"event": "ocr_reply", "png_sha": key, "result": "recorded"}); return 0
    e = load(cas_path)["entries"].get(key)
    if e is None: log({"event": "ocr_reply", "png_sha": key, "result": "miss"}); return 3
    with open(dst, "w") as f: json.dump(e.get("out"), f, ensure_ascii=False)
    log({"event": "ocr_reply", "png_sha": key, "result": "hit"}); return int(e.get("rc", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
