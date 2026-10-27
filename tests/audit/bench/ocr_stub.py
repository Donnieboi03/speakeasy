#!/usr/bin/env python3
"""OCR helper stand-in for the bench (docs/contracts/phase6-block-classifier.md 12.4 and Amendment 1 A1.1, --ocr replay|record).
The harness points SPEAKEASY_OCR_BIN at the `ocr_stub` wrapper next to this file; the app calls it like the tracker's subcommand:

  ocr_stub [ocr] IN.png OUT.json

Exit codes and files follow the helper (A1.1): 0 success with OUT.json written atomically; 2 usage error; 3 IN.png unreadable (and,
in replay, a cassette miss); 4 helper error. On every non-zero code OUT.json is not written and a stale file at that path is removed.

OCR_STUB_MODE=replay   the key is sha256 (hex) of the exact IN.png bytes, nothing else (no path, arguments or environment). A hit writes
                       the stored OUT.json object and exits with the stored code (a stored failure writes nothing); a miss exits 3
OCR_STUB_MODE=record   runs the real helper (OCR_STUB_REAL) as `<helper> ocr IN.png OUT.json`, stores its OUT.json object (or null) and
                       exit code under the key, and passes both through. An OUT.json that fails the A1.1 schema is passed through and
                       logged as result "bad-schema"
OCR_STUB_CASSETTE      {"header": {helper_sha256, macos, recorded_at}, "entries": {sha256: {"out": object|null, "rc": int}}}
OCR_STUB_LOG           JSONL of {"event": "ocr_reply", "png_sha", "result": hit|miss|recorded|real-failed|bad-schema|bad-args|unreadable,
                       "rc", "run"} (hashes and codes only, never text); "run" is the content of OCR_STUB_RUN_TAG_FILE.
The cassette holds the helper's output as returned (line boxes and recognised text of fixture images), so the hidden cassette lives in
the holdout dir only. Stdlib only."""
import fcntl, hashlib, json, os, platform, subprocess, sys, time

ENGINES = ("vision-documents", "vision-text", "app-lines")


def _rect_ok(r, w=None, h=None):
    return (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r) and r[0] < r[2] and r[1] < r[3]
            and (w is None or (r[0] >= 0 and r[1] >= 0 and r[2] <= w + 0.05 and r[3] <= h + 0.05)))


def schema_errors(out):
    """A1.1 OUT.json schema problems ([] when valid). Keys in order: version, engine, image_size, unit, origin, lines, tables, ms."""
    if not isinstance(out, dict): return ["not an object"]
    E = []
    if list(out) != ["version", "engine", "image_size", "unit", "origin", "lines", "tables", "ms"]: E.append(f"keys {list(out)}")
    if out.get("version") != 1: E.append("version")
    if out.get("engine") not in ENGINES: E.append("engine")
    sz = out.get("image_size")
    if not (isinstance(sz, list) and len(sz) == 2 and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in sz)): E.append("image_size"); sz = [None, None]
    if out.get("unit") != "px" or out.get("origin") != "top-left": E.append("unit/origin")
    if not isinstance(out.get("ms"), (int, float)) or isinstance(out.get("ms"), bool): E.append("ms")
    lines = out.get("lines")
    if not isinstance(lines, list): E.append("lines")
    else:
        for i, l in enumerate(lines):
            if not (isinstance(l, dict) and list(l) == ["rect", "text", "conf"] and _rect_ok(l["rect"], *sz) and isinstance(l["text"], str) and l["text"] == " ".join(l["text"].split())
                    and l["text"] and (l["conf"] is None or (isinstance(l["conf"], (int, float)) and 0 <= l["conf"] <= 1))):
                E.append(f"lines[{i}]"); break
    tables = out.get("tables")
    if not isinstance(tables, list): E.append("tables")
    else:
        if out.get("engine") in ("vision-text", "app-lines") and tables: E.append("tables not [] for " + str(out.get("engine")))
        for i, t in enumerate(tables):
            rows = t.get("rows") if isinstance(t, dict) else None
            if not (_rect_ok(t.get("rect") if isinstance(t, dict) else None, *sz) and isinstance(rows, list)
                    and all(isinstance(r, dict) and _rect_ok(r.get("rect"), *sz) and isinstance(r.get("cells"), list)
                            and all(isinstance(c, dict) and _rect_ok(c.get("rect"), *sz) and isinstance(c.get("text"), str) for c in r["cells"]) for r in rows)):
                E.append(f"tables[{i}]"); break
    return E


def log(rec):
    p = os.environ.get("OCR_STUB_LOG")
    if not p: return
    tf = os.environ.get("OCR_STUB_RUN_TAG_FILE")
    try: rec["run"] = (open(tf).read().strip() or None) if tf else None
    except OSError: rec["run"] = None
    with open(p, "a") as f: f.write(json.dumps(dict(rec, t=time.time())) + "\n")


def load(path):
    try:
        d = json.load(open(path))
        return d if isinstance(d, dict) and isinstance(d.get("entries"), dict) else {"header": {}, "entries": {}}
    except (OSError, ValueError):
        return {"header": {}, "entries": {}}


def write_atomic(dst, obj):
    tmp = f"{dst}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, dst)


def fail(dst, rc, rec):
    """Non-zero exit: no OUT.json (a stale one is removed first)."""
    try: os.remove(dst)
    except OSError: pass
    log(dict(rec, rc=rc)); return rc


def main(argv):
    args = argv[1:] if argv[:1] == ["ocr"] else argv
    if len(args) != 2: log({"event": "ocr_reply", "png_sha": None, "result": "bad-args", "rc": 2}); return 2
    src, dst = args; mode = os.environ.get("OCR_STUB_MODE", "replay"); cas_path = os.environ.get("OCR_STUB_CASSETTE", "")
    try: key = hashlib.sha256(open(src, "rb").read()).hexdigest()
    except OSError: return fail(dst, 3, {"event": "ocr_reply", "png_sha": None, "result": "unreadable"})
    if mode == "record":
        real = os.environ.get("OCR_STUB_REAL", "")
        if not (real and os.access(real, os.X_OK)): return fail(dst, 4, {"event": "ocr_reply", "png_sha": key, "result": "real-failed"})
        try: os.remove(dst)
        except OSError: pass
        p = subprocess.run([real, "ocr", src, dst], capture_output=True, timeout=120)
        out = None
        if p.returncode == 0:
            try: out = json.load(open(dst, encoding="utf-8"))
            except (OSError, ValueError): out = None
        rc = p.returncode if (p.returncode != 0 or out is not None) else 4
        with open(cas_path + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX); cas = load(cas_path)
            cas["entries"][key] = {"out": out, "rc": rc}
            if not cas["header"].get("recorded_at"):
                cas["header"] = {"helper_sha256": hashlib.sha256(open(real, "rb").read()).hexdigest()[:16], "macos": platform.mac_ver()[0],
                                 "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            tmp = cas_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump({"header": cas["header"], "entries": dict(sorted(cas["entries"].items()))}, f, indent=1, sort_keys=True, ensure_ascii=False); f.write("\n")
            os.replace(tmp, cas_path)
        if rc != 0: return fail(dst, rc, {"event": "ocr_reply", "png_sha": key, "result": "real-failed"})
        log({"event": "ocr_reply", "png_sha": key, "result": "bad-schema" if schema_errors(out) else "recorded", "rc": 0}); return 0
    e = load(cas_path)["entries"].get(key)
    if e is None: return fail(dst, 3, {"event": "ocr_reply", "png_sha": key, "result": "miss"})
    rc = int(e.get("rc", 0))
    if rc != 0 or e.get("out") is None: return fail(dst, rc or 4, {"event": "ocr_reply", "png_sha": key, "result": "hit"})
    write_atomic(dst, e["out"])
    log({"event": "ocr_reply", "png_sha": key, "result": "hit", "rc": 0}); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
