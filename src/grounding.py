"""Figure-part grounding: the Set-of-Mark picker (partpick.py), one letter from a vision model over marked candidates.
The local vision model always answers first; with SPEAKEASY_GROUND=openrouter the same marked crop and prompt go to
OpenRouter only once the local picker is unavailable or gives no logprobs (phase5-parts.md A2.9)."""
import base64, itertools, json, os, queue, re, subprocess, tempfile, threading, time, urllib.error, urllib.request
import blockcls, partpick, provider
import speakeasy_state as state
from speakeasy_state import docmap
from selog import DEFAULT_URL
from llm_client import deadline_fetch, keep_alive, vision_model
from prompts import PICK_PROMPT

PICK_DEADLINE, OR_DEADLINE, RENDER_TIMEOUT, OR_TIMEOUT = 15.0, 25.0, 10.0, 10.0

class PickFuture:
    """One pick: the picker thread sets outcome (accepted | low-confidence | off-menu | no-logprobs | unavailable | error |
    timeout | out-of-scope), the provider that produced it and the decision once, then done. drop / applied are set by the
    focus code that shows it. region: the label clip a label-pad (device alias) request must stay inside, else None."""
    def __init__(self, page, scope, cands, phrase, meta, region=None):
        self.done = threading.Event(); self.page, self.scope, self.cands, self.phrase, self.meta, self.region = page, scope, cands, phrase, meta, region
        self.t_submit = time.time(); self.deadline = self.t_submit + PICK_DEADLINE
        self.outcome = None; self.decision = {}; self.called = False; self.palette = None; self.ms = self.queue_ms = None
        self.provider, self.model, self.fallback_from, self.confidence = "local", None, None, None
        self.error = None; self.drop = None; self.applied = False
        self.settled = None; self.settle_lock = threading.Lock()
    def best(self):
        return self.cands[self.decision["chosen_index"]] if self.outcome == "accepted" else None
    def settle(self, reason):
        """Record how the pick ended for ground_miss_reasons / focus_methods; the first call wins."""
        with self.settle_lock:
            if self.settled is not None: return False
            self.settled = reason; return True
    def wait(self):
        """Wait for the decision until the (possibly extended) deadline has passed; True when it is in."""
        while not self.done.wait(0.1):
            if state.STOP.is_set() or time.time() > self.deadline + 1.0: return False
        return True
    def record(self):
        """The focus-log part_pick record: ids, numbers, enums and page geometry only (contract 6.1, A2.9)."""
        d = self.decision
        return {"state": "part_pick", **self.meta, "candidates": len(self.cands), "cand_rects": [[round(v, 1) for v in r] for r in self.cands],
                "chosen_index": d.get("chosen_index") if self.outcome in ("accepted", "low-confidence") else None,
                "p_top": d.get("p_top"), "p_margin": d.get("p_margin"), "letter_mass": d.get("letter_mass"), "palette": self.palette,
                "called": self.called, "outcome": self.outcome or "timeout", "applied": self.applied, "drop": self.drop,
                "ms": self.ms, "queue_ms": self.queue_ms, "model": self.model or vision_model(), "provider": self.provider,
                "fallback_from": self.fallback_from, "confidence": self.confidence}

def _mark_busy(busy):
    """vlm_busy holds this run's pid while a pick is queued or in flight: a kind job sends nothing meanwhile (phase6 6.1)."""
    try:
        if busy:
            with open(blockcls.busy_path(), "w") as f: f.write(str(os.getpid()))
        elif os.path.exists(blockcls.busy_path()): os.remove(blockcls.busy_path())
    except OSError:
        pass

def ollama_base():
    """SPEAKEASY_LLM_URL without a trailing /v1 (the native Ollama API root)."""
    u = os.environ.get("SPEAKEASY_LLM_URL", DEFAULT_URL).rstrip("/")
    return u[:-3] if u.endswith("/v1") else u

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

def fetch(url, body, headers, timeout):
    """POST body as JSON under one hard deadline; the parsed JSON reply."""
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    return json.loads(deadline_fetch(req, timeout, label="pick", as_json=False).decode())

def openrouter_body(model, prompt, png_b64):
    return {"model": model, "temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": 20, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt},
                                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64}}]}]}

def centre_in(r, b):
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]

class Grounder:
    """The picker queue: one pick in flight per run, FIFO, each with a deadline from submit. Never blocks speech."""
    def __init__(self):
        self.provider = None                                    # provider of the last answered pick
        self.error_classes = {}; self.errors_lock = threading.Lock()
        self.picks, self.pick_q, self.pick_thread, self.pick_n = [], queue.Queue(), None, itertools.count(1)
        self.breaker = {"local": None, "openrouter": None}
        self.fallback = {"unavailable": 0, "no-logprobs": 0}
        self.t0 = time.time()                                   # a kind job's breaker tripped after this also stops picks
        self.transport = fetch                                  # (url, body, headers, timeout) -> reply; tests stub it
    def enabled(self):
        if os.environ.get("SPEAKEASY_GROUND", "auto").lower() in GROUND_OFF or docmap is None: return False
        return bool(state.DOC is not None and getattr(state.DOC, "path", None) and state.DOC.path.lower().endswith(".pdf") and docmap.venv_python() is not None)
    def pick_enabled(self):
        """SPEAKEASY_GROUND not off and SPEAKEASY_PICK on (default off, Amendment 1), on a PDF with the doc venv."""
        return self.enabled() and os.environ.get("SPEAKEASY_PICK", "off").lower() not in GROUND_OFF
    def _openrouter_mode(self):
        return os.environ.get("SPEAKEASY_GROUND", "auto").lower() == "openrouter"
    def _error(self, fut, e):
        fut.error = type(e).__name__
        with self.errors_lock: self.error_classes[fut.error] = self.error_classes.get(fut.error, 0) + 1
    def submit_pick(self, page, scope, cands, phrase, meta, region=None):
        """Queue one pick (FIFO, one in flight per run); never blocks."""
        fut = PickFuture(page, scope, cands, phrase, meta, region)
        if self.breaker["local"] and self._openrouter_mode(): fut.deadline = fut.t_submit + OR_DEADLINE
        self.picks.append(fut); self.pick_q.put(fut); _mark_busy(True)
        if self.pick_thread is None:
            self.pick_thread = threading.Thread(target=self._pick_loop, daemon=True); self.pick_thread.start()
        return fut
    def _pick_loop(self):
        while True:
            self._pick(self.pick_q.get())
            if self.pick_q.empty(): _mark_busy(False)
    def render(self, fut, png):
        """The marked crop in the doc venv: (palette name, base64 PNG)."""
        r = subprocess.run([docmap.venv_python(), partpick.__file__, "render", state.DOC.path, str(fut.page), json.dumps(fut.scope), json.dumps(fut.cands), png],
                           timeout=min(RENDER_TIMEOUT, max(0.01, fut.deadline - time.time())), capture_output=True, check=True)
        return json.loads(r.stdout)["palette"], base64.b64encode(open(png, "rb").read()).decode()
    def _pick(self, fut):
        t0 = time.time(); fut.queue_ms = int((t0 - fut.t_submit) * 1000)
        png = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_pick_{os.getpid()}_{next(self.pick_n)}.png")
        try:
            if state.STOP.is_set() or t0 >= fut.deadline: fut.outcome = "timeout"; return
            if self.breaker["local"] is None: self.breaker["local"] = blockcls.shared_breaker(self.t0)
            to_or = self.breaker["local"] is not None
            if to_or and not self._openrouter_mode(): fut.outcome = self.breaker["local"]; return
            if to_or and self.breaker["openrouter"]: fut.outcome, fut.provider = self.breaker["openrouter"], "openrouter"; return
            try: fut.palette, b64 = self.render(fut, png)
            except subprocess.TimeoutExpired as e:
                fut.outcome = "timeout" if time.time() >= fut.deadline else "error"; self._error(fut, e); return
            except (subprocess.SubprocessError, OSError, ValueError, KeyError) as e:
                fut.outcome = "error"; self._error(fut, e); return
            prompt = PICK_PROMPT.format(n=len(fut.cands), colour=fut.palette, last=partpick.LETTERS[len(fut.cands) - 1], phrase=fut.phrase)
            if not to_or:
                self._ask_local(fut, prompt, b64)
                if fut.outcome in ("unavailable", "no-logprobs"):
                    self.breaker["local"] = fut.outcome; blockcls.trip_shared_breaker(fut.outcome)
                    if self._openrouter_mode():                  # the pick that tripped the breaker goes to OpenRouter once
                        self.fallback[fut.outcome] += 1; fut.fallback_from = fut.outcome; fut.deadline = fut.t_submit + OR_DEADLINE
                        to_or = True
            if to_or: self._ask_openrouter(fut, prompt, b64)
            if fut.outcome == "accepted" and not (centre_in(fut.best(), fut.scope) and (fut.region is None or centre_in(fut.best(), fut.region))):
                fut.outcome = "out-of-scope"
        except Exception as e:                                   # the picker thread never dies: the chunk keeps its cue
            fut.outcome = "error"; self._error(fut, e)
        finally:
            fut.ms = int((time.time() - t0) * 1000)
            try: os.remove(png)
            except FileNotFoundError: pass
            finally: fut.done.set()
    def _ask_local(self, fut, prompt, b64):
        """Ollama /api/chat (section 4.4): sets the outcome and decision; unavailable / no-logprobs trip the local breaker."""
        fut.provider, fut.model, fut.called = "local", vision_model(), True
        body = partpick.request_body(fut.model, keep_alive(), prompt, b64)
        try: reply = self.transport(ollama_base() + "/api/chat", body, {}, max(0.01, fut.deadline - time.time()))
        except urllib.error.HTTPError as e:
            fut.outcome = "unavailable" if e.code == 404 or e.code >= 500 else "error"; self._error(fut, e); return
        except TimeoutError as e:
            fut.outcome = "timeout"; self._error(fut, e); return
        except urllib.error.URLError as e:
            fut.outcome = "timeout" if isinstance(e.reason, TimeoutError) else "unavailable"; self._error(fut, e); return
        except ConnectionError as e:
            fut.outcome = "unavailable"; self._error(fut, e); return
        except (OSError, ValueError, UnicodeDecodeError) as e:
            fut.outcome = "error"; self._error(fut, e); return
        if time.time() > fut.deadline: fut.outcome = "timeout"; return       # a late reply is ignored
        fut.decision = partpick.decide(reply, len(fut.cands)); fut.outcome = fut.decision["outcome"]
        fut.confidence = "logprob" if fut.decision["p_top"] is not None else None
        if fut.outcome != "no-logprobs": self.provider = "local"
    def _ask_openrouter(self, fut, prompt, b64):
        """The same marked crop and prompt on OpenRouter: logprobs when it gives them, else the first letter of the reply.
        A missing key / URL or a failed request is unavailable and trips the OpenRouter breaker."""
        fut.provider = "openrouter"
        cloud = provider.cloud("ground")
        if not cloud: fut.outcome = self.breaker["openrouter"] = "unavailable"; return
        url, headers, fut.model = cloud
        fut.called = True
        try: reply = self.transport(url + "/chat/completions", openrouter_body(fut.model, prompt, b64), headers, max(0.01, min(OR_TIMEOUT, fut.deadline - time.time())))
        except (OSError, ValueError, UnicodeDecodeError) as e:
            fut.outcome = self.breaker["openrouter"] = "unavailable"; self._error(fut, e); return
        if time.time() > fut.deadline: fut.outcome = "timeout"; return
        choice = ((reply or {}).get("choices") or [{}])[0]
        top = (((choice.get("logprobs") or {}).get("content") or [{}])[0] or {}).get("top_logprobs")
        self.provider = "openrouter"
        if top:
            fut.decision = partpick.decide({"logprobs": [{"top_logprobs": top}]}, len(fut.cands)); fut.outcome = fut.decision["outcome"]
            fut.confidence = "logprob"; return
        text = ((choice.get("message") or {}).get("content") or "").lstrip()
        if text[:1] and text[0] in partpick.LETTERS[:len(fut.cands)]:
            fut.decision = {"outcome": "accepted", "chosen_index": partpick.LETTERS.index(text[0]), "p_top": None, "p_margin": None, "letter_mass": None}
            fut.outcome, fut.confidence = "accepted", "letter-only"
        else: fut.outcome = "off-menu"
    def log_fields(self):
        """Runs-log keys: ground_provider (the last answered pick's), ground_errors (count + exception classes, never text)."""
        out = {}
        if self.provider: out["ground_provider"] = self.provider
        with self.errors_lock:
            if self.error_classes: out["ground_errors"] = {"count": sum(self.error_classes.values()), "classes": dict(self.error_classes)}
        return out
GROUNDER = Grounder()
