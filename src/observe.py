"""Observability helpers: memory, Ollama status, captures, `speakeasy last` / `stats` (split out of speakeasy.py)."""
import json, os, re, subprocess, sys, urllib.request
from selog import DEFAULT_URL, LOG_KEEP, home

def image_size(data):
    """(w, h) from PNG/JPEG/GIF header bytes, stdlib only; None if unknown."""
    import struct
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF: i += 1; continue
            m = data[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9]); return (w, h)
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7: i += 2; continue
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


# ---------------- observability helpers ----------------
def peak_rss_mb():
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)

def ollama_ps():
    """Loaded Ollama models and their memory, or None if not an Ollama endpoint / unreachable."""
    from urllib.parse import urlsplit
    u = urlsplit(os.environ.get("SPEAKEASY_LLM_URL", DEFAULT_URL))
    try:
        req = urllib.request.Request(f"{u.scheme}://{u.netloc}/api/ps")
        with urllib.request.urlopen(req, timeout=1) as r:
            data = json.loads(r.read().decode())
        return [{"name": m.get("name"), "size_mb": round(m.get("size", 0) / 1048576),
                 "size_vram_mb": round(m.get("size_vram", 0) / 1048576)} for m in data.get("models", [])]
    except (OSError, ValueError):
        return None

def capture_keep_path():
    return os.path.expanduser(os.environ.get("SPEAKEASY_CAPTURE_PATH") or os.path.join(home(), "last_capture.png"))

def crop_to_rect(img, rect, rec, owned):
    """Crop img to --capture-rect before it is sent or saved. rect is x,y,w,h in screen points.
    If the image already has the rect's size (x1/x2/x3 Retina scale) it is already a crop and is left alone.
    Otherwise rect is applied as image coordinates (times SPEAKEASY_CAPTURE_SCALE, default 1).
    Returns the path to use; the uncropped original is deleted when we own it (selection)."""
    import tempfile
    try:
        x, y, w, h = [int(round(float(v))) for v in rect.split(",")]
        assert w > 0 and h > 0
    except (ValueError, AssertionError):
        raise ValueError(f"bad --capture-rect {rect!r}")
    data = open(img, "rb").read()
    size = image_size(data)
    if not size:
        return img
    W, H = size
    rec["capture_original_px"] = f"{W}x{H}"
    for sc in (1, 2, 3):
        if abs(W - w * sc) <= 2 * sc and abs(H - h * sc) <= 2 * sc:
            rec["capture_cropped_px"] = f"{W}x{H}"; rec["capture_crop"] = "already-cropped"
            return img
    r1, r2 = W / w, H / h
    if 1 <= r1 <= 3.5 and abs(r1 - r2) / r1 < 0.02:     # same aspect, uniform scale: already this rect
        rec["capture_cropped_px"] = f"{W}x{H}"; rec["capture_crop"] = "already-cropped"
        return img
    sc = float(os.environ.get("SPEAKEASY_CAPTURE_SCALE", "1"))
    x0, y0 = max(0, int(x * sc)), max(0, int(y * sc))
    x1, y1 = min(W, int((x + w) * sc)), min(H, int((y + h) * sc))
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"capture rect {rect} is outside the {W}x{H} image")
    fd, out = tempfile.mkstemp(prefix=CAP_PREFIX + "crop_", suffix=".png", dir=tempfile_dir()); os.close(fd)
    try:
        from PIL import Image
        Image.open(img).crop((x0, y0, x1, y1)).save(out, "PNG")
    except ImportError:
        r = subprocess.run(["sips", "-c", str(y1 - y0), str(x1 - x0), "--cropOffset", str(y0), str(x0), img, "--out", out], capture_output=True)
        if r.returncode != 0:
            os.remove(out); raise ValueError("cannot crop (no Pillow/sips)")
    rec["capture_cropped_px"] = f"{x1 - x0}x{y1 - y0}"; rec["capture_crop"] = "cropped"
    if owned and is_owned_capture(img):
        try: os.remove(img)
        except OSError: pass
    return out

def tempfile_dir():
    import tempfile
    return "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()

CAP_PREFIX = "speakeasy_cap_"

def is_owned_capture(p):
    """True only for files this tool or the hotkey created: <tempdir>/speakeasy_cap_*. Everything else is copied, never moved/deleted."""
    import tempfile
    if not p: return False
    rp = os.path.realpath(p)
    if not os.path.basename(rp).startswith(CAP_PREFIX): return False
    roots = {os.path.realpath(r) for r in ("/tmp", "/private/tmp", tempfile.gettempdir())}
    return os.path.dirname(rp) in roots

def handle_capture_file(img, owned=False):
    """Keep a copy of the screenshot for review (default) or delete it (SPEAKEASY_KEEP_CAPTURE=0)."""
    import shutil
    if not img or not os.path.exists(img):
        return None
    if os.environ.get("SPEAKEASY_KEEP_CAPTURE", "1") == "0":
        if owned and is_owned_capture(img):
            try: os.remove(img)
            except OSError: pass
        try: os.remove(capture_keep_path())
        except OSError: pass
        return None
    dst = capture_keep_path()
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.abspath(img) != os.path.abspath(dst):
            if owned and is_owned_capture(img):
                shutil.move(img, dst)      # only the final image survives
            else:
                shutil.copyfile(img, dst)
        return dst
    except OSError:
        return None

def read_runs():
    """All logged runs, oldest first (rotated runs.jsonl.N files included so stats keep their history)."""
    base = os.path.join(home(), "logs", "runs.jsonl")
    runs = []
    for path in [f"{base}.{k}" for k in range(LOG_KEEP, 0, -1)] + [base]:
        try:
            for line in open(path, encoding="utf-8"):
                try: runs.append(json.loads(line))
                except ValueError: pass
        except OSError:
            pass
    return runs

def cmd_last(as_json=False):
    runs = [r for r in read_runs() if r.get("command") not in ("last", "stats", "stop", "scrub")]
    if not runs:
        print("No runs logged yet (" + os.path.join(home(), "logs", "runs.jsonl") + ")"); return 0
    r = runs[-1]
    if as_json:
        print(json.dumps(r, indent=2, ensure_ascii=False)); return 0
    t = r.get("timings") or {}
    rows = [("time", r.get("ts")), ("command", r.get("command")), ("backend", r.get("backend")),
            ("text model", r.get("model")), ("vision model", r.get("vision_model") if r.get("image") else None),
            ("input/output chars", f"{r.get('input_chars')} -> {r.get('output_chars')}"),
            ("capture", f"{r.get('capture_method')} rect={r.get('capture_rect')}"),
            ("image", f"{r.get('image_px')} (sent {r.get('image_sent_px')})" if r.get("image") else "none"),
            ("capture file", r.get("capture_path") or "(not kept)"),
            ("total ms", r.get("total_ms"))]
    rows += [(k, v) for k, v in t.items()]
    rows += [("dry run", r.get("dry_run")), ("tts", r.get("tts")), ("tts voice", r.get("tts_voice")),
             ("tts synth ms", r.get("tts_synth_ms")), ("tts server RSS MB", r.get("tts_server_rss_mb"))]
    rows += [("peak RSS MB", r.get("peak_rss_mb"))]
    for m in r.get("ollama_models") or []:
        rows.append(("ollama " + str(m.get("name")), f"{m.get('size_mb')} MB ({m.get('size_vram_mb')} MB GPU)"))
    rows.append(("error", r.get("error")))
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        if v is not None and v != "None rect=None":
            print(f"{k.ljust(w)}  {v}")
    return 0

def cmd_stats(as_json=False, window=7):
    """`speakeasy stats [--json] [--window 1|7|30]`: the Reading log's numbers (schema 2), from whitelisted log keys."""
    import stats as _st
    s = _st.load(home(), window)
    if as_json: print(json.dumps(s, ensure_ascii=False)); return 0
    print("\n".join(_st.text_lines(s))); return 0


# ---------------- speakeasy logs --scrub (phase7 A5.4) ----------------
SCRUB_FIELDS = ("text", "src", "ref_src", "doc.label", "doc.anchors", "input", "output", "error", "llm_error", "notes")
_SUPERLATIVE_RE = re.compile(r"superlative '([^']*)' on \S+ contradicts .*?(?=; |$)")

def _scrub_focus_rec(rec, len_hash, label_re):
    """A focus.jsonl record -> the form focus.focus_log_record writes with SPEAKEASY_LOG_TEXT unset, in place.
    Keys already present are kept. Returns the fields it changed."""
    hit = []
    if "text" in rec:
        v = rec.pop("text"); hit.append("text")
        if isinstance(v, str) and v:
            n, h = len_hash(v); rec.setdefault("text_len", n); rec.setdefault("text_hash", h)
    if "src" in rec:
        v = rec.pop("src"); hit.append("src")
        if isinstance(v, list) and v:
            rec.setdefault("src_n", len(v)); rec.setdefault("src_hash", len_hash("\n".join(map(str, v)))[1])
    if "ref_src" in rec:
        del rec["ref_src"]; hit.append("ref_src")
    doc = rec.get("doc")
    if isinstance(doc, dict):
        label = doc.get("label")
        if isinstance(label, str) and len(label.split()) > 3 and not label_re.fullmatch(label):
            doc["label"] = None; hit.append("doc.label")        # the writer's text-off form: label null
        anchors = doc.get("anchors")
        if isinstance(anchors, list) and any(isinstance(a, list) and len(a) > 4 for a in anchors):
            doc["anchors"] = [a[:4] if isinstance(a, list) else a for a in anchors]; hit.append("doc.anchors")
    return hit

def _scrub_run_rec(rec):
    """A runs.jsonl record without input / output and with the text-free superlative note, in place. Returns the fields it changed."""
    hit = [k for k in ("input", "output") if k in rec]
    for k in hit: del rec[k]
    for k in ("error", "llm_error", "notes"):
        v = rec.get(k)
        if isinstance(v, str):
            s = _SUPERLATIVE_RE.sub(r"superlative '\1' contradicts its column", v)
            if s != v: rec[k] = s; hit.append(k)
    return hit

def _scrub_file(path, scrub, dumps, fields):
    """(scrubbed bytes, or None when nothing changed; records changed). Untouched lines stay byte-for-byte."""
    with open(path, "rb") as f: lines = f.read().split(b"\n")
    changed = 0
    for i, line in enumerate(lines):
        try: rec = json.loads(line)
        except ValueError: continue                              # malformed or blank line: kept as it is
        hit = scrub(rec) if isinstance(rec, dict) else []
        if hit:
            changed += 1; lines[i] = dumps(rec).encode("utf-8")
            for k in hit: fields[k] += 1
    return (b"\n".join(lines) if changed else None), changed

def _holds_text(line):
    try: rec = json.loads(line)
    except ValueError: return False
    # tracker `draw` / `scroll` records carry `src` as an enum string; source lines are a list
    return isinstance(rec, dict) and (any(rec.get(k) for k in ("text", "input", "output"))
                                      or any(isinstance(rec.get(k), list) for k in ("src", "ref_src")))

def _read_running():
    from selog import pidfile
    try:
        with open(pidfile()) as f: pid = int(f.read().strip())
    except (OSError, ValueError): return False
    if pid <= 0: return False                                    # os.kill(0, ...) would signal our own group
    try: os.kill(pid, 0)
    except PermissionError: return True
    except ProcessLookupError: return False
    return True

def _replace_with(path, data, st):
    """Write data to a temp file next to path, then rename it over path. The temp file never outlives an error."""
    import stat, tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".scrub_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        now = os.stat(path)
        if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise RuntimeError("log changed during the scrub")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp); raise

def cmd_scrub(yes=False):
    """`speakeasy logs --scrub`: remove text from old runs / focus records in place. Prints counts only."""
    import time
    from focus import _len_and_hash, LOGGED_LABEL_RE
    t0 = time.time()
    if _read_running():
        print("A read is running. Run the scrub after it ends.", file=sys.stderr); return 2
    d = os.path.join(home(), "logs")
    rotated = lambda base: [os.path.join(d, base)] + [os.path.join(d, f"{base}.{k}") for k in range(1, LOG_KEEP + 1)]
    kinds = {"runs": (_scrub_run_rec, lambda r: json.dumps(r, ensure_ascii=False)),                 # selog.log_run
             "focus": (lambda r: _scrub_focus_rec(r, _len_and_hash, LOGGED_LABEL_RE), json.dumps)}  # focus.py writers
    fields = dict.fromkeys(SCRUB_FIELDS, 0); changed = {"runs": 0, "focus": 0}; plan = []
    try:
        for kind, (scrub, dumps) in kinds.items():
            for path in rotated(kind + ".jsonl"):
                if not os.path.isfile(path): continue
                st = os.stat(path)
                data, n = _scrub_file(path, scrub, dumps, fields)
                if data is not None: plan.append((path, data, st)); changed[kind] += n
        other = 0
        for path in rotated("tracker.jsonl") + rotated("app.jsonl") + rotated("blockcls.jsonl"):    # report only; blockcls holds no text key
            if os.path.isfile(path):
                with open(path, "rb") as f: other += sum(1 for line in f if _holds_text(line))
        total = sum(changed.values())
        if os.environ.get("SPEAKEASY_LOG_TEXT") == "1":
            print("SPEAKEASY_LOG_TEXT=1 is set: new records will hold text again.")
        if not total:
            print("Nothing to scrub.")
        elif not yes:
            try: answer = input(f"Remove text from {total} records in {len(plan)} files? [y/N] ")
            except EOFError: answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Scrub cancelled; nothing changed.", file=sys.stderr); return 1
        for path, data, st in plan:
            _replace_with(path, data, st)
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "command": "scrub", "files": len(plan),
                 "records_changed": changed, "fields": fields, "other_logs_with_text": other,
                 "ms": int((time.time() - t0) * 1000)}
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "runs.jsonl"), "a+b") as f:
            f.seek(0, 2); sep = b""
            if f.tell():
                f.seek(-1, 2); sep = b"" if f.read(1) == b"\n" else b"\n"   # a torn last line stays its own line
            f.write(sep + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
    except (OSError, ValueError, TypeError, RuntimeError) as e:
        print(f"speakeasy: scrub failed ({type(e).__name__}); unfinished files are unchanged.", file=sys.stderr); return 1
    if total:
        print(f"Scrubbed {total} records in {len(plan)} files (runs: {changed['runs']}, focus: {changed['focus']}).")
    if other:
        print(f"Other logs with text: {other} records (not changed).")
    return 0

