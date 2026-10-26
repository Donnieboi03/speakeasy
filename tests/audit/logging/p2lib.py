"""Shared helpers for the phase-2 logging holdout suite (black box: drives the CLI, docmap.py parse-pdf and the logs only).
Adapted from tests/audit/readability/p1lib.py (mock LLM, stub bin dir, scrubbed env)."""
import json, os, socket, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.environ.get("P2_PYTHON", sys.executable)
SENT = ["zorblax", "quintessa", "mirevolt", "plumbrine", "vexhollow", "grelquist", "dunmarrow", "skelvane", "frumplet", "wexmoor"]
TMPROOT = tempfile.mkdtemp(prefix="se_p2_")
REAL_HOME = os.path.realpath(os.path.expanduser("~/.speakeasy"))


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
    """Local OpenAI-compatible mock (mock_llm_full.py). kw -> MOCK_* env (MOCK_TEXT, MOCK_GROUND, MOCK_VISION, MOCK_HANG, ...)."""
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
    d = os.path.join(TMPROOT, "stubs"); os.makedirs(d, exist_ok=True)
    S = {
        "say": r'''#!/usr/bin/env bash
txt=""; f=""; args=()
while [ $# -gt 0 ]; do case "$1" in -f) f="$2"; shift 2;; -v|-r|-o|--voice|--rate) shift 2;; --) shift; args+=("$@"); break;; -*) shift;; *) args+=("$1"); shift;; esac; done
if [ -n "$f" ] && [ -f "$f" ]; then txt=$(cat "$f"); elif [ ${#args[@]} -gt 0 ]; then txt="${args[*]}"; elif [ ! -t 0 ]; then txt=$(cat); fi
[ -n "$STUB_SPOKEN" ] && printf '%s\n' "$txt" >> "$STUB_SPOKEN"
trap 'exit 143' TERM INT
sleep "${STUB_PLAY_SEC:-0.1}" & wait $!
''',
        "afplay": '#!/usr/bin/env bash\ntrap \'exit 143\' TERM INT\nsleep "${STUB_PLAY_SEC:-0.1}" & wait $!\n',
        "pbpaste": '#!/usr/bin/env bash\nprintf \'%s\' "${STUB_CLIP:-}"\n',
        "pbcopy": "#!/usr/bin/env bash\ncat > /dev/null\n",
        "osascript": "#!/usr/bin/env bash\ncase \"$*\" in *id*|*window*) echo 12345;; esac; exit 0\n",
        "screencapture": '#!/usr/bin/env bash\nout="${@: -1}"; cp "${STUB_SCREEN:?}" "$out"\n',
        "security": "#!/usr/bin/env bash\nexit 44\n",
        "hs": "#!/usr/bin/env bash\nexit 0\n",
    }
    for n, body in S.items():
        p = os.path.join(d, n); open(p, "w").write(body); os.chmod(p, 0o755)
    return d
STUBS = make_stubs()


def mkhome(tag="h"):
    parent = tempfile.mkdtemp(prefix=f"se_p2{tag}_", dir=TMPROOT); h = os.path.join(parent, "home"); os.makedirs(h)
    assert os.path.realpath(h).startswith(os.path.realpath(TMPROOT)) and not os.path.realpath(h).startswith(REAL_HOME)
    return h


def env_for(home, url="http://127.0.0.1:9/v1", **kw):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("SPEAKEASY_", "STUB_", "MOCK_")): e.pop(k)
    tmpd = os.path.join(os.path.dirname(home), "tmp"); os.makedirs(tmpd, exist_ok=True)
    e.update(SPEAKEASY_HOME=home, HOME=home, SPEAKEASY_LLM_URL=url, SPEAKEASY_LLM="local", SPEAKEASY_MODEL="mock-text", SPEAKEASY_VISION_MODEL="mock-vision",
             SPEAKEASY_OR_URL="http://127.0.0.1:9/v1", SPEAKEASY_TTS="say", SPEAKEASY_DOC_PYTHON=PY, SPEAKEASY_PREWARM="0",
             PATH=STUBS + ":" + os.environ["PATH"], STUB_SPOKEN=os.path.join(os.path.dirname(home), "spoken.txt"),
             STUB_PLAY_SEC="0.05", PYTHONDONTWRITEBYTECODE="1")
    e.update({k: str(v) for k, v in kw.items() if v is not None})
    for k, v in kw.items():
        if v is None: e.pop(k, None)
    return e


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
