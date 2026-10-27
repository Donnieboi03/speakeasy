"""Stub executables and a fake onnxruntime package for the phase-4 holdout suite (black box: nothing here imports app code).

- fake onnxruntime: shadows the real package via PYTHONPATH, loads the real one from FAKE_ORT_REAL and wraps InferenceSession.
  FAKE_ORT_MODE=wrap (default) | load-fail; FAKE_ORT_FAIL_RUNS=2,3 (1-based session.run calls that raise RuntimeError);
  FAKE_ORT_LOG=file (one JSON line per session creation / run).
- installer stubs: curl / wget serve the model from STUB_MODEL_<MODE> files (STUB_CURL_MODE=good|corrupt|trunc|fail|http404),
  uv (python install + pip install), codesign (records whether the bundled model is in place), swift/swiftc/ollama (fail),
  launchctl (no-op). Every call is appended to STUB_LOG."""
import os, stat

FAKE_ORT = r'''
import importlib.util, json, os, sys
_LOG = os.environ.get("FAKE_ORT_LOG")
def _log(ev):
    if _LOG:
        with open(_LOG, "a") as f: f.write(json.dumps(dict(ev, pid=os.getpid())) + "\n")
_real = os.environ["FAKE_ORT_REAL"]
_spec = importlib.util.spec_from_file_location("onnxruntime", os.path.join(_real, "__init__.py"), submodule_search_locations=[_real])
_m = importlib.util.module_from_spec(_spec); sys.modules["onnxruntime"] = _m; _spec.loader.exec_module(_m)
_Real = _m.InferenceSession
class InferenceSession(_Real):
    def __init__(self, *a, **k):
        _log({"ev": "session", "arg": os.path.basename(str(a[0])) if a and isinstance(a[0], (str, bytes, os.PathLike)) else type(a[0]).__name__ if a else None})
        if os.environ.get("FAKE_ORT_MODE") == "load-fail": raise RuntimeError("fake load failure")
        super().__init__(*a, **k); self._fake_n = 0
    def run(self, *a, **k):
        self._fake_n += 1; _log({"ev": "run", "n": self._fake_n})
        if str(self._fake_n) in os.environ.get("FAKE_ORT_FAIL_RUNS", "").split(","): raise RuntimeError("fake inference failure")
        return super().run(*a, **k)
_m.InferenceSession = InferenceSession
'''

CURL = r'''#!{py}
import json, os, shutil, sys
a = sys.argv[1:]; log = os.environ.get("STUB_LOG")
if log: open(log, "a").write(json.dumps({"tool": "{tool}", "argv": a, "cwd": os.getcwd()}) + "\n")
out = None; url = None; wfmt = None; i = 0; remote = False
while i < len(a):
    x = a[i]
    if "{tool}" == "curl":
        if x in ("-o", "--output"): out = a[i + 1]; i += 2; continue
        if x in ("-w", "--write-out"): wfmt = a[i + 1]; i += 2; continue
        if x in ("-O", "--remote-name"): remote = True; i += 1; continue
        if x.startswith("-") and not x.startswith("--") and x.endswith("o") and len(x) > 2: out = a[i + 1]; i += 2; continue
        if x.startswith("-") and not x.startswith("--") and x.endswith("O") and len(x) > 2: remote = True; i += 1; continue
        if x in ("-H", "-d", "--data", "-X", "--max-time", "-m", "--retry", "--connect-timeout", "-A", "-u", "-e", "--retry-delay", "--retry-max-time", "-C"): i += 2; continue
    else:
        if x in ("-O", "--output-document"): out = a[i + 1]; i += 2; continue
        if x.startswith("--output-document="): out = x.split("=", 1)[1]; i += 1; continue
        if x.startswith("-") and not x.startswith("--") and x.endswith("O") and len(x) > 2: out = a[i + 1]; i += 2; continue
        if x in ("-T", "--timeout", "-t", "--tries"): i += 2; continue
    if x.startswith("http://") or x.startswith("https://"): url = x
    i += 1
if not url or ("huggingface.co" not in url and "pp_doclayout" not in url):
    sys.stderr.write("{tool}: (7) Failed to connect (stub)\n"); sys.exit(7 if "{tool}" == "curl" else 4)
mode = os.environ.get("STUB_CURL_MODE", "fail")
if mode == "fail": sys.stderr.write("{tool}: (6) Could not resolve host: huggingface.co\n"); sys.exit(6 if "{tool}" == "curl" else 4)
if mode == "http404":
    if wfmt is not None: sys.stdout.write("404")
    sys.stderr.write("{tool}: (22) The requested URL returned error: 404\n"); sys.exit(22 if "{tool}" == "curl" else 8)
src = os.environ["STUB_MODEL_" + mode.upper()]
if remote and not out: out = url.rsplit("/", 1)[-1].split("?")[0]
if out and out != "-":
    shutil.copyfile(src, out)
else:
    sys.stdout.buffer.write(open(src, "rb").read()); sys.stdout.flush()
if wfmt is not None: sys.stdout.write("200")
sys.exit(0)
'''

UV = r'''#!{py}
import json, os, sys
a = sys.argv[1:]; log = os.environ.get("STUB_LOG")
if log: open(log, "a").write(json.dumps({"tool": "uv", "argv": a}) + "\n")
if a[:2] == ["python", "install"]:
    d = a[a.index("--install-dir") + 1] if "--install-dir" in a else os.path.join(os.environ.get("TMPDIR", "/tmp"), "uvpy")
    b = os.path.join(d, "cpython-3.12-stub", "bin"); os.makedirs(b, exist_ok=True)
    for n in ("python3", "python", "python3.12"):
        p = os.path.join(b, n); open(p, "w").write("#!/bin/sh\nexec {realpy} \"$@\"\n"); os.chmod(p, 0o755)
sys.exit(0)
'''

CODESIGN = r'''#!{py}
import hashlib, json, os, sys
a = sys.argv[1:]; log = os.environ.get("STUB_LOG")
app = next((x for x in reversed(a) if x.endswith(".app")), a[-1] if a else "")
m = os.path.join(app, "Contents", "Resources", "models", "pp_doclayout_s.onnx")
sha = hashlib.sha256(open(m, "rb").read()).hexdigest() if os.path.isfile(m) else None
if log: open(log, "a").write(json.dumps({"tool": "codesign", "argv": a, "model_sha": sha}) + "\n")
sys.exit(0)
'''

SIMPLE = {"swift": "exit 1", "swiftc": "exit 1", "ollama": "exit 1", "launchctl": "exit 0", "open": "exit 0", "osascript": "exit 0"}


def _w(p, body):
    open(p, "w").write(body); os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_fake_ort(root):
    d = os.path.join(root, "fake_ort"); os.makedirs(os.path.join(d, "onnxruntime"), exist_ok=True)
    open(os.path.join(d, "onnxruntime", "__init__.py"), "w").write(FAKE_ORT)
    return d


def make_install_stubs(root, py, realpy):
    d = os.path.join(root, "istubs"); os.makedirs(d, exist_ok=True)
    for tool in ("curl", "wget"): _w(os.path.join(d, tool), CURL.replace("{py}", py).replace("{tool}", tool))
    _w(os.path.join(d, "uv"), UV.replace("{py}", py).replace("{realpy}", realpy))
    _w(os.path.join(d, "codesign"), CODESIGN.replace("{py}", py))
    for n, body in SIMPLE.items():
        _w(os.path.join(d, n), f'#!/bin/sh\n[ -n "$STUB_LOG" ] && printf \'{{"tool": "{n}"}}\\n\' >> "$STUB_LOG"\n{body}\n')
    return d


def make_fake_venv(home, realpy):
    """$SPEAKEASY_HOME/tts-venv whose python runs `realpy` but turns `-m pip ...` into a logged no-op."""
    b = os.path.join(home, "tts-venv", "bin"); os.makedirs(b, exist_ok=True)
    _w(os.path.join(b, "python"), '#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then [ -n "$STUB_LOG" ] && printf \'{"tool": "pip", "argv": "%s"}\\n\' "$*" >> "$STUB_LOG"; exit 0; fi\n'
       f'exec {realpy} "$@"\n')
    _w(os.path.join(b, "pip"), '#!/bin/sh\n[ -n "$STUB_LOG" ] && printf \'{"tool": "pip", "argv": "%s"}\\n\' "$*" >> "$STUB_LOG"\nexit 0\n')
    for n in ("python3", "python3.12"):
        p = os.path.join(b, n)
        if not os.path.exists(p): os.symlink("python", p)
    return os.path.join(b, "python")
