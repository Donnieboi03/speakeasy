"""OpenAI-compatible chat calls: local Ollama or OpenRouter (provider.py), one-shot and streamed, under hard deadlines."""
import json, os, threading, time, urllib.request
import provider
import speakeasy_state as state
from selog import DEFAULT_URL, DEFAULT_MODEL, RUN_META

DEFAULT_VISION = "qwen3-vl:4b-instruct"
CLOUD_TIMEOUT = float(os.environ.get("SPEAKEASY_OR_TIMEOUT", "8"))   # first byte from OpenRouter, else the local model answers

def _endpoint(model, stream, purpose="llm", local=False):
    """(url, headers, body extras, model, provider): OpenRouter when switched on (provider.py), else the local server."""
    cloud = None if local else provider.cloud(purpose)
    if cloud:
        url, headers, cloud_model = cloud
        return url + "/chat/completions", headers, {}, cloud_model, "openrouter"
    url = os.environ.get("SPEAKEASY_LLM_URL", DEFAULT_URL).rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {}
    key = os.environ.get("SPEAKEASY_API_KEY")
    if key: headers["Authorization"] = "Bearer " + key
    return url, headers, {"keep_alive": keep_alive()}, model, "local"

def _note_provider(prov, model, fell_back=None):
    RUN_META.setdefault("llm_provider", prov); RUN_META.setdefault("llm_provider_model", model)
    if fell_back: RUN_META["llm_fallback"] = fell_back

def chat(model, system, content, timeout, local=False):
    url, h, extra, m, prov = _endpoint(model, False, local=local)
    body = {"model": m, "temperature": 0, "stream": False, **extra,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json", **h}
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        data = deadline_fetch(req, min(timeout, CLOUD_TIMEOUT) if prov != "local" else timeout)
        _note_provider(prov, m)
    except (OSError, ValueError, TimeoutError) as e:
        if prov == "local": raise
        _note_provider("local", model, f"openrouter: {type(e).__name__}")   # cloud failed: the local model answers
        return chat(model, system, content, timeout, local=True)
    out = data["choices"][0]["message"]["content"]
    if not isinstance(out, str):
        raise ValueError("bad LLM response")
    return out

def deadline_fetch(req, timeout, label="request", as_json=True):
    """Run the whole request (connect + send + server think time + read) under one hard wall-clock deadline.
    Parsed JSON, or the raw body bytes with as_json=False. The timeout message ('<label> exceeded <t>s') is logged."""
    box = {}
    def run():
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                box["data"] = json.loads(body.decode()) if as_json else body
        except BaseException as e:
            box["err"] = e
    t = threading.Thread(target=run, daemon=True); t.start(); t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{label} exceeded {timeout:g}s")
    if "err" in box:
        raise box["err"]
    return box["data"]

def keep_alive():
    # idle unload: Ollama frees the model this long after the last read (the hotkey pre-warms it again)
    return os.environ.get("SPEAKEASY_KEEP_ALIVE", "10m")

def _ollama_base():
    from urllib.parse import urlsplit
    u = urlsplit(os.environ.get("SPEAKEASY_LLM_URL", DEFAULT_URL))
    return f"{u.scheme}://{u.netloc}"

def prewarm(models):
    """Fire-and-forget: ask Ollama to load models and keep them for keep_alive (no prompt = load only)."""
    if os.environ.get("SPEAKEASY_PREWARM", "1") == "0":
        return
    def go(m):
        try:
            req = urllib.request.Request(_ollama_base() + "/api/generate",
                                         json.dumps({"model": m, "keep_alive": keep_alive()}).encode(),
                                         {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=120).read()
        except (OSError, ValueError):
            pass
    for m in models:
        if m: threading.Thread(target=go, args=(m,), daemon=True).start()

def chat_stream(model, system, content, timeout, rec, t_req, local=False):
    """Yield content deltas from an OpenAI-compatible SSE stream. Overall deadline = timeout.
    OpenRouter (when on) falls back to the local model if it fails or is slow before the first token."""
    url, h, extra, m, prov = _endpoint(model, True, local=local)
    if prov != "local":
        got = False
        try:
            for d in _stream(url, h, extra, m, system, content, timeout, rec, t_req, first_byte=CLOUD_TIMEOUT):
                got = True; yield d
            _note_provider(prov, m); return
        except (OSError, ValueError, TimeoutError) as e:
            if got: raise                                   # mid-stream: the caller's normal error path
            _note_provider("local", model, f"openrouter: {type(e).__name__}")
        yield from chat_stream(model, system, content, timeout, rec, t_req, local=True); return
    _note_provider(prov, m)
    yield from _stream(url, h, extra, m, system, content, timeout, rec, t_req)

def _stream(url, h, extra, model, system, content, timeout, rec, t_req, first_byte=None):
    body = {"model": model, "temperature": 0, "stream": True, **extra,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **h}
    deadline = time.time() + timeout
    resp = urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=first_byte or timeout)
    try:
        for raw in resp:
            if state.STOP.is_set():
                return
            if time.time() > deadline:
                raise TimeoutError(f"stream exceeded {timeout:g}s")
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                choice = json.loads(data)["choices"][0]
            except (ValueError, KeyError, IndexError):
                continue
            delta = (choice.get("delta") or {}).get("content") or ""
            if delta:
                if rec.get("llm_first_token_ms") is None:
                    rec["llm_first_token_ms"] = int((time.time() - t_req) * 1000)
                    if rec.get("_t0"): rec["llm_request_at_ms"] = int((t_req - rec["_t0"]) * 1000)   # run start -> LLM request sent
                yield delta
    finally:
        try: resp.close()
        except OSError: pass

def model_name():
    return os.environ.get("SPEAKEASY_MODEL", DEFAULT_MODEL)

def vision_model():
    return os.environ.get("SPEAKEASY_VISION_MODEL", DEFAULT_VISION)
