"""Speech: macOS say, a command template or the Kokoro server; sentence batching, streaming playback and stop."""
import json, os, queue, re, shlex, shutil, signal, subprocess, sys, tempfile, threading, time, urllib.request
import speakeasy_state as state
from selog import home, pidfile, set_state
from llm_client import deadline_fetch

def prewarm_tts(a=None):
    """Fire-and-forget tiny Kokoro request at hotkey time: pages the voice model back in while the LLM starts."""
    if os.environ.get("SPEAKEASY_TTS", "say").lower() != "kokoro" or os.environ.get("SPEAKEASY_PREWARM", "1") == "0" \
            or getattr(a, "dry_run", False):
        return
    def go():
        try: os.remove(_kokoro_synth("Ok.", 1.0, timeout=20))
        except Exception: pass
    threading.Thread(target=go, daemon=True).start()

_TITLES = ("Dr", "Mr", "Mrs", "Ms", "Mx", "Prof", "St", "Jr", "Sr", "Mt", "Ft", "Lt", "Col", "Gen", "Capt", "Cpt", "Sgt",
           "Rev", "Hon", "Gov", "Sen", "Rep", "Pres", "Supt", "Dept", "Univ", "Inc", "Ltd", "Co", "Corp", "Messrs", "Mme",
           "Mlle", "Fig", "Figs", "Eq", "Vol", "pp", "vs", "approx", "e.g", "i.e", "cf", "al")
# not a sentence end: a title / abbreviation right before the period ("Dr. Smith", "Fig. 4", "e.g. this")
NOT_ABBREVIATION = "".join(r"(?<!\b" + re.escape(t) + r"\.)" for t in sorted(set(_TITLES), key=len))

def sentences(text):
    """Sentence batches of about 120+ characters for one TTS call each."""
    buf = ""
    for s in re.split(r"(?<=[.!?])" + NOT_ABBREVIATION + r"[\"\u201d')]*\s+", text):
        buf = (buf + " " + s).strip()
        if len(buf) >= 120 or s is None:
            yield buf; buf = ""
    if buf:
        yield buf

def _tts_cmd(chunk, rate, voice):
    """Build the TTS process args for one chunk. Returns (args, stdin_text, use_shell)."""
    mode = os.environ.get("SPEAKEASY_TTS", "say").lower()
    if mode == "cmd":
        tpl = os.environ.get("SPEAKEASY_TTS_CMD", "").strip()
        if not tpl:
            raise FileNotFoundError("SPEAKEASY_TTS=cmd but SPEAKEASY_TTS_CMD is empty")
        cmd = (tpl.replace("{rate}", str(rate or "")).replace("{voice}", shlex.quote(voice or ""))
                  .replace("{text}", shlex.quote(chunk)))
        return cmd, (None if "{text}" in tpl else chunk), True
    args = ["say"]
    if rate: args += ["-r", str(rate)]
    if voice: args += ["-v", voice]
    return args + ["--", chunk], None, False

KOKORO_URL = "http://127.0.0.1:8000/v1/audio/speech"
KOKORO_MODEL = "mlx-community/Kokoro-82M-bf16"
KOKORO_VOICE = "af_heart"

def tts_url():
    """Accept http://host:port, http://host:port/v1, or the full /v1/audio/speech endpoint."""
    u = (os.environ.get("SPEAKEASY_TTS_URL") or KOKORO_URL).strip().rstrip("/")
    if u.endswith("/audio/speech"):
        return u
    if u.endswith("/v1"):
        return u + "/audio/speech"
    return u + "/v1/audio/speech"

def _kokoro_synth(chunk, speed, timeout=30):
    body = {"model": os.environ.get("SPEAKEASY_TTS_MODEL", KOKORO_MODEL), "input": chunk,
            "voice": os.environ.get("SPEAKEASY_TTS_VOICE", KOKORO_VOICE), "speed": speed, "response_format": "wav"}
    req = urllib.request.Request(tts_url(), json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    data = deadline_fetch(req, timeout, label="TTS request", as_json=False)
    if len(data) < 44:
        raise ValueError("empty audio from TTS server")
    fd, path = tempfile.mkstemp(prefix="speakeasy_", suffix=".wav")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path

def _tool(name, *paths):
    for p in paths:
        if os.path.exists(p): return p
    return shutil.which(name)

def _rss_tree_kb(pid, ps, pgrep):
    """RSS (KB) of pid plus its direct and nested children (python child processes of a launcher)."""
    total, seen, stack = 0, set(), [str(pid)]
    while stack:
        p = stack.pop()
        if p in seen: continue
        seen.add(p)
        out = subprocess.run([ps, "-o", "rss=", "-p", p], capture_output=True, text=True, timeout=2).stdout.strip()
        if out.isdigit(): total += int(out)
        if pgrep:
            kids = subprocess.run([pgrep, "-P", p], capture_output=True, text=True, timeout=2).stdout.split()
            stack.extend(kids)
    return total

def tts_server_rss_mb():
    """RSS of the process listening on the TTS port (plus children), else of `mlx_audio.server` via pgrep."""
    from urllib.parse import urlsplit
    ps = _tool("ps", "/bin/ps", "/usr/bin/ps")
    pgrep = _tool("pgrep", "/usr/bin/pgrep", "/bin/pgrep")
    lsof = _tool("lsof", "/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof")
    if not ps:
        return None
    pids = []
    try:
        port = urlsplit(tts_url()).port or 80
        if lsof:
            pids = subprocess.run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                  capture_output=True, text=True, timeout=3).stdout.split()
        if not pids and pgrep:
            pids = subprocess.run([pgrep, "-f", "mlx_audio.server"], capture_output=True, text=True, timeout=2).stdout.split()
        if not pids:
            return None
        if pgrep:   # keep only top-level pids (children are counted via the tree)
            pids = [p for p in pids if not any(p in subprocess.run([pgrep, "-P", q], capture_output=True, text=True, timeout=2).stdout.split() for q in pids if q != p)]
        kb = sum(_rss_tree_kb(p, ps, pgrep) for p in pids)
        return round(kb / 1024, 1) if kb else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

def speak_kokoro(text, rate, timing, t_start, playback):
    """Synthesize batch N+1 while batch N plays (afplay). Returns list of unspoken batches if the server fails."""
    speed = max(0.5, min(2.0, (rate or 175) / 175.0))
    batches = list(sentences(text))
    q = queue.Queue(maxsize=2)
    synth = {"ms": 0, "err": None}
    def producer():
        for i, b in enumerate(batches):
            if playback["stopped"]: break
            t = time.time()
            try:
                path = _kokoro_synth(b, speed)
            except Exception as e:
                synth["err"] = f"{type(e).__name__}: {e}"[:200]
                q.put(("fail", i)); return
            synth["ms"] += int((time.time() - t) * 1000)
            while not playback["stopped"]:
                try: q.put(("ok", path), timeout=0.2); break
                except queue.Full: continue
            else:
                try: os.remove(path)
                except OSError: pass
        q.put(("done", None))
    th = threading.Thread(target=producer, daemon=True); th.start()
    remaining = []
    while True:
        kind, val = q.get()
        if kind == "done" or playback["stopped"]:
            if kind == "ok":
                try: os.remove(val)
                except OSError: pass
            break
        if kind == "fail":
            remaining = batches[val:]; break
        if timing.get("tts_first_audio_ms") is None:
            timing["tts_first_audio_ms"] = int((time.time() - t_start) * 1000)
            set_state("speaking")
        playback["proc"] = subprocess.Popen(["afplay", val], start_new_session=True)
        playback["proc"].wait()
        try: os.remove(val)
        except OSError: pass
    while not q.empty():               # clean up any pending synthesized files
        kind, val = q.get_nowait()
        if kind == "ok":
            try: os.remove(val)
            except OSError: pass
    timing["tts_synth_ms"] = synth["ms"]
    if synth["err"]:
        timing["tts_warning"] = "kokoro unavailable, fell back to say: " + synth["err"]
    return remaining

def speak(text, rate=None, voice=None, timing=None, t_start=None):
    """Speak sentence batches one by one. SPEAKEASY_TTS=say (default) or cmd (SPEAKEASY_TTS_CMD template:
    {text} shell-quoted chunk, {rate}, {voice}; if {text} is absent the chunk is piped to stdin)."""
    os.makedirs(home(), exist_ok=True)
    with open(pidfile(), "w") as f:
        f.write(str(os.getpid()))
    stopped = {"v": False}
    proc = {"p": None}
    kstate = {"stopped": False, "proc": None}
    def on_term(*_):
        stopped["v"] = True; kstate["stopped"] = True
        for pr in (proc["p"], kstate["proc"]):
            if pr:
                try: os.killpg(pr.pid, signal.SIGTERM)
                except OSError:
                    try: pr.terminate()
                    except OSError: pass
    signal.signal(signal.SIGTERM, on_term)
    t_speak = time.time()
    if timing is None: timing = {}
    try:
        engine = os.environ.get("SPEAKEASY_TTS", "say").lower()
        chunks = list(sentences(text))
        if engine == "kokoro":
            rest = speak_kokoro(text, rate, timing, t_start or t_speak, kstate)
            if timing.get("tts_warning"):
                print("speakeasy: warning: Kokoro TTS server unavailable; using say", file=sys.stderr)
                timing["tts_engine_used"] = "say"
            chunks = rest
            os.environ["SPEAKEASY_TTS"] = "say"   # fallback for the remaining batches
        for chunk in chunks:
            if stopped["v"]: break
            args, stdin_text, shell = _tts_cmd(chunk, rate, voice)
            proc["p"] = subprocess.Popen(args, shell=shell, start_new_session=True,
                                         stdin=subprocess.PIPE if stdin_text is not None else None,
                                         text=True if stdin_text is not None else None)
            if timing is not None and timing.get("tts_first_audio_ms") is None:
                timing["tts_first_audio_ms"] = int((time.time() - (t_start or t_speak)) * 1000)
                set_state("speaking")
            if stdin_text is not None:
                try: proc["p"].communicate(stdin_text)
                except BrokenPipeError: pass
            proc["p"].wait()
            if proc["p"].returncode not in (0, None) and not stopped["v"] and timing is not None:
                timing["tts_error"] = f"tts exited {proc['p'].returncode}"
    finally:
        if timing is not None:
            timing["speak_ms"] = int((time.time() - t_speak) * 1000)
        try:
            if open(pidfile()).read().strip() == str(os.getpid()): os.remove(pidfile())
        except OSError:
            pass

class Speaker:
    """Streaming TTS: put(sentence) as text arrives; synthesis (kokoro) and playback overlap with generation.
    say: queued sentences are merged into one `say` call to avoid gaps. Stop (SIGTERM) kills everything."""
    def __init__(self, rate, voice, timing, t0, dry=False):
        self.rate, self.voice, self.timing, self.t0, self.dry = rate, voice, timing, t0, dry
        self.engine = os.environ.get("SPEAKEASY_TTS", "say").lower()
        self.tq, self.pq = queue.Queue(), queue.Queue(maxsize=3)
        self.proc, self.synth_ms, self.warn = None, 0, None
        self.spoken = []
        if not dry:
            os.makedirs(home(), exist_ok=True)
            with open(pidfile(), "w") as f: f.write(str(os.getpid()))
        self.t_start = time.time()
        self.th_s = threading.Thread(target=self._synth, daemon=True); self.th_s.start()
        self.th_p = threading.Thread(target=self._play, daemon=True); self.th_p.start()
    def put(self, text, tag=None):
        if text and text.strip() and not state.STOP.is_set():
            rect = None
            if state.FOCUS is not None:
                rect, method = state.FOCUS.locate(text, tag); state.FOCUS.note(method, text)
            self.tq.put((text.strip(), rect))
    def close(self):
        self.tq.put(None)
    def _synth(self):
        speed = max(0.5, min(2.0, (self.rate or 175) / 175.0))
        while True:
            item = self.tq.get()
            if item is None or state.STOP.is_set():
                self.pq.put(None); return
            t, rect = item
            if self.dry or self.engine != "kokoro" or self.warn:
                self.pq.put(("text", t, rect)); continue
            t1 = time.time()
            try:
                path = _kokoro_synth(t, speed)
                ms = int((time.time() - t1) * 1000)
                self.synth_ms += ms
                if self.timing.get("first_synth_ms") is None:
                    self.timing.update(first_synth_ms=ms, first_chunk_chars=len(t), first_chunk_queued_ms=int((t1 - self.t0) * 1000))
                self.pq.put(("wav", path, rect, t))
            except Exception as e:
                self.warn = f"kokoro unavailable, fell back to say: {type(e).__name__}: {e}"[:200]
                print("speakeasy: warning: Kokoro TTS server unavailable; using say", file=sys.stderr)
                self.pq.put(("text", t, rect))
    def _first(self):
        if self.dry:
            return
        if self.timing.get("tts_first_audio_ms") is None:
            self.timing["tts_first_audio_ms"] = int((time.time() - self.t0) * 1000)
            set_state("speaking")
    def _run(self, args, **kw):
        self.proc = subprocess.Popen(args, start_new_session=True, **kw)
        self.proc.wait()
        rc = self.proc.returncode; self.proc = None
        if rc not in (0, None) and not state.STOP.is_set():
            self.timing["tts_error"] = f"tts exited {rc}"
    def _say(self, val):
        if self.dry:
            print(val, flush=True); return
        if self.engine == "cmd":
            args, stdin_text, shell = _tts_cmd(val, self.rate, self.voice)
            self.proc = subprocess.Popen(args, shell=shell, start_new_session=True,
                                         stdin=subprocess.PIPE if stdin_text is not None else None,
                                         text=True if stdin_text is not None else None)
            if stdin_text is not None:
                try: self.proc.communicate(stdin_text)
                except BrokenPipeError: pass
            self.proc.wait(); self.proc = None
            return
        a = ["say"] + (["-r", str(self.rate)] if self.rate else []) + (["-v", self.voice] if self.voice and self.engine == "say" else [])
        self._run(a + ["--", val])
    def _play(self):
        pending = None
        while True:
            item = pending if pending is not None else self.pq.get()
            pending = None
            if item is None or state.STOP.is_set():
                if item and item[0] == "wav":
                    try: os.remove(item[1])
                    except OSError: pass
                return
            kind, val, rect = item[0], item[1], item[2]
            if kind == "text":            # merge queued text into one call (fewer gaps) unless the focus overlay needs per-sentence starts
                texts = [val]
                while state.FOCUS is None:
                    try: nxt = self.pq.get_nowait()
                    except queue.Empty: break
                    if nxt is not None and nxt[0] == "text":
                        texts.append(nxt[1]); continue
                    pending = nxt if nxt is not None else ("__end__", None); break
                val = " ".join(texts)
                self.spoken.append(val); self._first()
                if state.FOCUS is not None and rect is not None and not state.STOP.is_set():
                    state.FOCUS.show(rect, val)         # audio for this sentence starts now
                try: self._say(val)
                except FileNotFoundError as e: self.timing["tts_error"] = f"unavailable: {e}"
                if pending == ("__end__", None):
                    return
                continue
            self.spoken.append(item[3] if len(item) > 3 else val); self._first()
            if state.FOCUS is not None and rect is not None and not state.STOP.is_set():
                state.FOCUS.show(rect, item[3] if len(item) > 3 else "")
            try: self._run(["afplay", val])
            except FileNotFoundError as e: self.timing["tts_error"] = f"unavailable: {e}"
            try: os.remove(val)
            except OSError: pass
    def kill(self):
        p = self.proc
        if p:
            try: os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                try: p.terminate()
                except OSError: pass
    def wait(self):
        while self.th_p.is_alive():
            self.th_p.join(0.1)
        # drain leftovers (stopped mid-stream): remove any synthesized wavs
        for q in (self.pq,):
            while True:
                try: it = q.get_nowait()
                except queue.Empty: break
                if it and it[0] == "wav":
                    try: os.remove(it[1])
                    except OSError: pass
        if state.FOCUS is not None:
            state.FOCUS.clear()
        self.timing["speak_ms"] = int((time.time() - self.t_start) * 1000)
        self.timing["tts_synth_ms"] = self.synth_ms
        if self.warn:
            self.timing["tts_warning"] = self.warn; self.timing["tts_engine_used"] = "say"
        if not self.dry:
            try:
                if open(pidfile()).read().strip() == str(os.getpid()): os.remove(pidfile())
            except OSError:
                pass

def stop():
    try:
        pid = int(open(pidfile()).read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, signal.SIGTERM); stopped = True
    except OSError:
        stopped = False
    try: os.remove(pidfile())
    except OSError: pass
    return stopped
