"""Shared helpers for the phase-1 holdout suite (black box: drives the CLI, scripts and logs only)."""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
DOC_PY = os.environ.get("P1_DOC_PYTHON", sys.executable)
SENT = ["zorblax", "quintessa", "mirevolt", "plumbrine", "vexhollow", "grelquist", "dunmarrow", "skelvane"]
TMPROOT = tempfile.mkdtemp(prefix="se_p1_")
def jl(p):
    out = []
    if os.path.exists(p):
        for x in open(p, errors="ignore"):
            x = x.strip()
            if x:
                try: out.append(json.loads(x))
                except Exception: out.append({"_bad": x[:200]})
    return out
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class Mock:
    def __init__(self, **kw):
        self.port = free_port(); self.log = os.path.join(TMPROOT, f"mock_{self.port}.jsonl")
        env = dict(os.environ, MOCK_PORT=str(self.port), MOCK_LOG=self.log, **{k: str(v) for k, v in kw.items()})
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_llm_full.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try: socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close(); break
            except OSError: time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}/v1"
    def reqs(self): return [r for r in jl(self.log) if r.get("method") == "POST"]
    def reset(self): open(self.log, "w").close()
    def stop(self):
        self.p.terminate()
        try: self.p.wait(3)
        except Exception: self.p.kill()

def make_stubs():
    """Stub bin dir: say/afplay/pbpaste/screencapture/osascript from the repo-independent copies below, plus security (no keychain)."""
    d = os.path.join(TMPROOT, "stubs"); os.makedirs(d, exist_ok=True)
    S = {
        "say": r'''#!/usr/bin/env bash
s=$(date +%s); txt=""; f=""; args=()
while [ $# -gt 0 ]; do case "$1" in -f) f="$2"; shift 2;; -v|-r|-o|--voice|--rate) shift 2;; --) shift; args+=("$@"); break;; -*) shift;; *) args+=("$1"); shift;; esac; done
if [ -n "$f" ] && [ -f "$f" ]; then txt=$(cat "$f"); elif [ ${#args[@]} -gt 0 ]; then txt="${args[*]}"; elif [ ! -t 0 ]; then txt=$(cat); fi
[ -n "$STUB_SPOKEN" ] && printf '%s\n' "$txt" >> "$STUB_SPOKEN"
trap 'echo "{\"tool\":\"say\",\"killed\":1}" >> "${STUB_LOG:-/dev/null}"; exit 143' TERM INT
sleep "${STUB_PLAY_SEC:-0.2}" & wait $!
echo "{\"tool\":\"say\",\"killed\":0}" >> "${STUB_LOG:-/dev/null}"
''',
        "afplay": '#!/usr/bin/env bash\ntrap \'exit 143\' TERM INT\nsleep "${STUB_PLAY_SEC:-0.2}" & wait $!\n',
        "pbpaste": '#!/usr/bin/env bash\nprintf \'%s\' "${STUB_CLIP:-}"\n',
        "pbcopy": "#!/usr/bin/env bash\ncat > /dev/null\n",
        "osascript": "#!/usr/bin/env bash\ncase \"$*\" in *id*|*window*) echo 12345;; esac; exit 0\n",
        "screencapture": '#!/usr/bin/env bash\nout="${@: -1}"; cp "${STUB_SCREEN:?}" "$out"\n',
        "security": "#!/usr/bin/env bash\nexit 44\n",
        "hs": '#!/usr/bin/env bash\necho "hs $*" >> "${P1_HS_LOG:-/dev/null}"\n',
    }
    for n, body in S.items():
        p = os.path.join(d, n); open(p, "w").write(body); os.chmod(p, 0o755)
    return d
STUBS = make_stubs()

def doc_wrapper(home):
    """home/tts-venv/bin/python = logging wrapper around the test venv python (the doc-venv python the CLI spawns)."""
    b = os.path.join(home, "tts-venv", "bin"); os.makedirs(b, exist_ok=True); w = os.path.join(b, "python")
    open(w, "w").write(f'#!/bin/sh\nprintf "%s\\0" "$0" "$@" >> "{home}/../argv_{os.path.basename(home)}.log"; printf "\\n" >> "{home}/../argv_{os.path.basename(home)}.log"\nexec "{DOC_PY}" "$@"\n')
    os.chmod(w, 0o755)
    for n in ("python3",):
        try: os.symlink(w, os.path.join(b, n))
        except FileExistsError: pass
    return w
def doc_argv(home):
    p = os.path.join(os.path.dirname(home), f"argv_{os.path.basename(home)}.log")
    if not os.path.exists(p): return []
    return [l.strip("\0").split("\0") for l in open(p, errors="ignore").read().split("\n") if l.strip("\0")]

def mkhome(tag="h"):
    parent = tempfile.mkdtemp(prefix=f"se_p1{tag}_", dir=TMPROOT); h = os.path.join(parent, "home"); os.makedirs(h); return h
def env_for(home, url="http://127.0.0.1:9/v1", **kw):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("SPEAKEASY_", "STUB_", "MOCK_")): e.pop(k)
    e.update(SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_LLM_URL=url, SPEAKEASY_MODEL="mock-text", SPEAKEASY_VISION_MODEL="mock-vision",
             SPEAKEASY_OR_URL="http://127.0.0.1:9/v1", SPEAKEASY_TTS="say", PATH=STUBS + ":" + os.environ["PATH"],
             STUB_LOG=os.path.join(os.path.dirname(home), "stub.jsonl"), STUB_SPOKEN=os.path.join(os.path.dirname(home), "spoken.txt"),
             STUB_PLAY_SEC="0.15", PYTHONDONTWRITEBYTECODE="1")
    e.update({k: str(v) for k, v in kw.items() if v is not None})
    for k, v in kw.items():
        if v is None: e.pop(k, None)
    return e

class Watch:
    """Polls the process table (argv containing needles) and the live focus file while a command runs."""
    def __init__(self, home=None, needles=("ground.py",), dt=0.03):
        self.home, self.needles, self.dt = home, needles, dt; self.hits = []; self.focus = []; self._stop = False; self.ps_samples = 0
        self.t = threading.Thread(target=self._run, daemon=True)
    def _run(self):
        last = None; fp = os.path.join(self.home, "focus.json") if self.home else None
        while not self._stop:
            try:
                out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=5).stdout; self.ps_samples += 1
                for l in out.splitlines():
                    if any(n in l for n in self.needles) and "ps -axww" not in l: self.hits.append(l.strip()[:300])
            except Exception: pass
            if fp:
                try:
                    raw = open(fp).read()
                    if raw != last: last = raw; self.focus.append(json.loads(raw))
                except Exception: pass
            time.sleep(self.dt)
    def __enter__(self): self.t.start(); return self
    def __exit__(self, *a): self._stop = True; self.t.join(5)

def run_cli(bin_, args, env, inp=None, timeout=240, watch=None):
    t = time.time()
    p = subprocess.run([bin_] + args, input=inp, capture_output=True, text=True, env=env, timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.time() - t

def runs(home): return jl(os.path.join(home, "logs", "runs.jsonl"))
def focus_log(home): return jl(os.path.join(home, "logs", "focus.jsonl"))
def flat(l):
    t = l.get("timings")
    if isinstance(t, dict):
        l = dict(l)
        for k, v in t.items(): l.setdefault(k, v)
    return l
def all_text_under(d, exclude=()):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            if any(x in p for x in exclude): continue
            try: out[os.path.relpath(p, d)] = open(p, errors="ignore").read()
            except Exception: pass
    return out
def sentinel_hits(text, words=SENT):
    lo = text.lower(); return [w for w in words if w in lo]
def cap_copy(src, tag):
    p = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_p1{os.getpid()}_{tag}_{int(time.time()*1000)}.png"); shutil.copy(src, p); return p
