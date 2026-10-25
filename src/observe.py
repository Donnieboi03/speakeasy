"""Observability helpers: memory, Ollama status, captures, `speakeasy last` / `stats` (split out of speakeasy.py)."""
import base64, json, os, re, signal, subprocess, sys, tempfile, time, urllib.request, urllib.error
from selog import *
from selog import DEFAULT_URL, LOG_KEEP
from selog import _home_rel, _on

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

def is_temp_path(p):
    import tempfile
    rp = os.path.realpath(p)
    roots = {os.path.realpath(r) for r in ("/tmp", "/private/tmp", tempfile.gettempdir())}
    return any(rp.startswith(r.rstrip("/") + "/") for r in roots)

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
    runs = [r for r in read_runs() if r.get("command") not in ("last", "stats", "stop")]
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

def cmd_stats(n=50, as_json=False):
    import statistics
    if as_json:
        import stats as _st
        print(json.dumps(_st.compute(read_runs()))); return 0
    runs = [r for r in read_runs() if r.get("command") in ("rewrite", "speak", "selection")][-n:]
    if not runs:
        print("No runs logged yet."); return 0
    print(f"Last {len(runs)} runs")
    def med(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return f"{statistics.median(vals):.0f} ms (n={len(vals)})" if vals else "-"
    keys = ["total_ms", "copy_ms", "capture_ms", "vision_ms", "llm_ms", "tts_first_audio_ms", "speak_ms"]
    for k in keys:
        vals = [(r.get("timings") or {}).get(k, r.get(k)) for r in runs]
        if k in ("vision_ms", "llm_ms"): vals = [v for v in vals if v]
        print(f"  median {k:<20} {med(vals)}")
    from collections import Counter
    print("  backend mix: " + ", ".join(f"{k}={v}" for k, v in Counter(r.get("backend") for r in runs).items()))
    print("  capture mix: " + ", ".join(f"{k}={v}" for k, v in Counter(r.get("capture_method") or "none" for r in runs).items()))
    print(f"  with image: {sum(1 for r in runs if r.get('image'))}")
    rss = [r.get("peak_rss_mb") for r in runs if r.get("peak_rss_mb")]
    if rss: print(f"  peak RSS max: {max(rss)} MB")
    errs = Counter(re.sub(r"\d+", "N", str(r.get("error")))[:90] for r in runs if r.get("error"))
    print(f"  errors: {sum(errs.values())}")
    for e, c in errs.most_common(5):
        print(f"    {c}x {e}")
    return 0

