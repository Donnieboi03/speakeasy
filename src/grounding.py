"""Figure-part grounding: the vision model boxes the object a sentence names inside a figure crop."""
import base64, json, os, re, subprocess, tempfile, threading, time, urllib.request
import provider
import speakeasy_state as state
from speakeasy_state import docmap
from selog import DEFAULT_URL
from llm_client import vision_model

class GroundFuture:
    """One grounding request: done is set when the answer (or a failure) is in; boxes are page-space [x0, y0, x1, y1]."""
    def __init__(self, clip):
        self.done = threading.Event(); self.boxes = []; self.labels = []; self.ms = None; self.clip = clip; self.max_frac = 0.6
    def best(self):
        """The grounded box for the sentence's object: a non-generic phrase, not (almost) the whole figure."""
        c = self.clip; clip_area = max(1.0, (c[2] - c[0]) * (c[3] - c[1]))
        good = [(b, l) for b, l in zip(self.boxes, self.labels or [""] * len(self.boxes))
                if (b[2] - b[0]) * (b[3] - b[1]) < self.max_frac * clip_area and docmap.content_words(l or "x y z") and not docmap.REF_RE.search(l or "")]
        if not good: good = [(b, l) for b, l in zip(self.boxes, self.labels or [""] * len(self.boxes)) if (b[2] - b[0]) * (b[3] - b[1]) < self.max_frac * clip_area]
        return good[0][0] if good else None

def gemini_model(model):
    """Gemini answers boxes natively as box_2d [ymin, xmin, ymax, xmax] on 0..1000 (y first), whatever the prompt says."""
    return "gemini" in (model or "").lower()

def ground_prompt(model):
    if gemini_model(model):
        return 'Answer only JSON {"box_2d":[ymin,xmin,ymax,xmax]} normalized to 0-1000.'
    return 'Answer only JSON {"bbox_2d":[x1,y1,x2,y2]} with coordinates 0-1000.'

def parse_ground_box(content, model):
    """Model answer -> [x0, y0, x1, y1] as 0..1 of the crop, or None. Gemini's box_2d is y-first; Qwen's bbox_2d is x-first."""
    m = re.search(r"\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]", content or "")
    if not m: return None
    a = [float(x) for x in m.groups()]
    if gemini_model(model): a = [a[1], a[0], a[3], a[2]]
    scale = 1.0 if max(a) <= 1.0 else 1000.0              # some models answer 0..1
    v = [min(1.0, max(0.0, x / scale)) for x in a]
    return v if v[2] > v[0] and v[3] > v[1] else None

GROUND_OFF = ("off", "0", "no", "false")
GROUND_MODES = ("", "auto", "local", "openrouter") + GROUND_OFF

def ground_mode_note():
    """Runs-log note for an unknown SPEAKEASY_GROUND value (it behaves like auto), else None."""
    m = os.environ.get("SPEAKEASY_GROUND", "auto").lower()
    return None if m in GROUND_MODES else f"SPEAKEASY_GROUND={m[:24]} unknown; using auto"

class Grounder:
    """Figure-part grounding with the vision model on the figure crop (one request per sentence, in a thread). Never blocks speech."""
    def __init__(self):
        self.provider = None; self.box_convention = None       # last request's provider / last parsed box's axis order
        self.error_classes = {}; self.errors_lock = threading.Lock()
    def enabled(self):
        if os.environ.get("SPEAKEASY_GROUND", "auto").lower() in GROUND_OFF or docmap is None: return False
        return state.DOC is not None and getattr(state.DOC, "path", None) and state.DOC.path.lower().endswith(".pdf") and docmap.venv_python() is not None
    def log_fields(self):
        """Runs-log keys: ground_provider, ground_box_convention, ground_errors (count + exception class names, never text)."""
        out = {}
        if self.provider: out["ground_provider"] = self.provider
        if self.box_convention: out["ground_box_convention"] = self.box_convention
        with self.errors_lock:
            if self.error_classes: out["ground_errors"] = {"count": sum(self.error_classes.values()), "classes": dict(self.error_classes)}
        return out
    def _ground(self, future, pdf, page, clip, text):
        """Qwen3-VL answers bbox_2d [x1,y1,x2,y2], Gemini box_2d [ymin,xmin,ymax,xmax], both 0..1000 of the crop."""
        t0 = time.time()
        png = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_ground_{os.getpid()}_{id(future)}.png")
        try:
            code = ("import sys;sys.path.insert(0,sys.argv[5]);import pdfpage;c=[float(v) for v in sys.argv[3].split(',')];"
                    "pdfpage.open_pdf(sys.argv[1])[int(sys.argv[2])-1].render(c,2,sys.argv[4])")
            subprocess.run([docmap.venv_python(), "-c", code, pdf, str(page), ",".join(str(v) for v in clip), png,
                            os.path.dirname(os.path.realpath(__file__))], timeout=10, capture_output=True, check=True)
            b64 = base64.b64encode(open(png, "rb").read()).decode()
            cloud = provider.cloud("ground")
            url = (cloud[0] if cloud else os.environ.get("SPEAKEASY_LLM_URL", DEFAULT_URL).rstrip("/")) + "/chat/completions"
            headers = {"Content-Type": "application/json", **(cloud[1] if cloud else {})}
            self.provider = "openrouter" if cloud else "local"
            model = cloud[2] if cloud else vision_model()
            body = {"model": model, "temperature": 0, "max_tokens": 40, "stream": False,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": 'Locate ' + re.sub(r"[\"{}]", "", text)[:120] + ' in the image. ' + ground_prompt(model)},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
            reply = json.load(urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=20))
            box = parse_ground_box(reply["choices"][0]["message"]["content"] or "", model)
            if box:
                w, h = clip[2] - clip[0], clip[3] - clip[1]
                self.box_convention = "yx" if gemini_model(model) else "xy"
                future.boxes = [[clip[0] + box[0] * w, clip[1] + box[1] * h, clip[0] + box[2] * w, clip[1] + box[3] * h]]; future.labels = [text[:60]]
        except Exception as e:                                   # a failed request only loses the part oval; speech goes on
            with self.errors_lock: self.error_classes[type(e).__name__] = self.error_classes.get(type(e).__name__, 0) + 1
        finally:
            future.ms = int((time.time() - t0) * 1000); future.done.set()
            try: os.remove(png)
            except OSError: pass
    def submit(self, pdf, page, clip, text):
        future = GroundFuture(clip)
        threading.Thread(target=self._ground, args=(future, pdf, page, clip, text), daemon=True).start()
        return future
GROUNDER = Grounder()
