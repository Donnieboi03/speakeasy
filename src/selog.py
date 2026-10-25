"""Paths, config, run log and the pill state file (split out of speakeasy.py)."""
import base64, json, os, re, signal, subprocess, sys, tempfile, time, urllib.request, urllib.error
DEFAULT_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "qwen3-vl:4b-instruct"     # one model for text, vision and grounding

# ---------------- paths / logging ----------------
def home():
    return os.path.expanduser(os.environ.get("SPEAKEASY_HOME") or "~/.speakeasy")

def _on(key, default="on"):
    return os.environ.get(key, default).strip().lower() not in ("off", "0", "false", "no")

def load_config():
    """Defaults from $SPEAKEASY_HOME/config (KEY=VALUE lines). Real env vars win."""
    try:
        for line in open(os.path.join(home(), "config"), encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k.startswith("SPEAKEASY_") and k != "SPEAKEASY_HOME":
                os.environ.setdefault(k, v)
    except OSError:
        pass

def pidfile():
    return os.path.join(home(), "speak.pid")

RUN_META = {}

def _ram_total_mb():
    """Resident memory of the whole stack right now: this process, Ollama (model runners) and the Kokoro server."""
    try:
        out = subprocess.run(["ps", "-axo", "rss=,pid=,command="], capture_output=True, text=True, timeout=3).stdout
        tot = 0
        for l in out.splitlines():
            parts = l.strip().split(None, 2)
            if len(parts) < 3: continue
            rss, pid, cmd = parts
            if int(pid) == os.getpid() or "ollama" in cmd or "kokoro" in cmd.lower() or "speakeasy-track" in cmd or "/tts-venv/" in cmd:
                tot += int(rss)
        return round(tot / 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

def _home_rel(p):
    if not p: return p
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p.startswith(h + os.sep) else p

def set_state(state):
    """~/.speakeasy/state.json: thinking | speaking | idle, for the menu-bar pill (no text)."""
    if os.environ.get("SPEAKEASY_APP") != "menubar" and RUN_META.get("app") != "menubar": return
    try:
        path = os.path.join(home(), "state.json"); tmp = path + ".tmp"
        with open(tmp, "w") as f: json.dump({"state": state, "run_id": RUN_META.get("run_id"), "t": time.time()}, f)
        os.replace(tmp, path)
    except OSError: pass

LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_KEEP = 5

def rotate(path, max_bytes=LOG_MAX_BYTES, keep=LOG_KEEP):
    """path -> path.1 -> ... -> path.<keep> once it passes max_bytes (the oldest is dropped)."""
    try:
        if os.path.getsize(path) < max_bytes: return
        for k in range(keep - 1, 0, -1):
            if os.path.exists(f"{path}.{k}"): os.replace(f"{path}.{k}", f"{path}.{k + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        pass

def rotate_logs():
    """Every log speakeasy, the tracker and the app write under ~/.speakeasy/logs."""
    d = os.path.join(home(), "logs")
    try: names = os.listdir(d)
    except OSError: return
    for n in names:
        if n.endswith((".jsonl", ".log")): rotate(os.path.join(d, n))

def log_run(rec, inp, out):
    try:
        d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
        for k, v in RUN_META.items(): rec.setdefault(k, v)
        rec.setdefault("doc_source", None)
        if rec.get("capture_path"): rec["capture_path"] = _home_rel(rec["capture_path"])      # never an absolute /Users path
        if rec.get("command") in ("speak", "selection"): rec["ram_total_mb"] = _ram_total_mb()
        if rec.get("command") in ("rewrite", "speak", "selection"):          # counts only, never the text
            import stats as _st
            rec["words_in"] = _st.words(inp)
            spoke = rec.get("command") != "rewrite" and not rec.get("dry_run")
            rec["words_spoken"] = _st.words(out) if spoke else 0
            sm = (rec.get("timings") or {}).get("speak_ms")
            rec["audio_s"] = round(sm / 1000, 1) if spoke and isinstance(sm, (int, float)) else 0
            rec["kind"] = _st.kind_of(rec)
        if os.environ.get("SPEAKEASY_LOG_TEXT") == "1":
            rec["input"] = inp; rec["output"] = out
        with open(os.path.join(d, "runs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:
        print(f"speakeasy: warning: could not write log: {e}", file=sys.stderr)

