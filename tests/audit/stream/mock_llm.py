#!/usr/bin/env python3
"""Mock OpenAI-compatible chat server. Env: MOCK_PORT, MOCK_LOG, MOCK_TEXT (text-model reply), MOCK_VISION (vision reply),
MOCK_TOKEN_DELAY (s between streamed tokens), MOCK_FIRST_DELAY (s before first token), MOCK_REPLY_FILE (text reply re-read per
request; overrides MOCK_TEXT), MOCK_PORT_FILE (with MOCK_PORT=0: the bound port is written there). Logs every request incl. image dims.

Ollama-native picker endpoint (docs/contracts/phase5-parts.md 7.2): a POST to /api/chat whose message carries `images` is a *pick*.
MOCK_PICK selects the reply:
  none (default)  Ollama-shaped reply with content "A" and no `logprobs` (the app must treat it as no-logprobs)
  replay          reply from the cassette MOCK_PICK_CASSETTE, keyed sha256(model 0x00 prompt 0x00 sha256hex(png)); a key not in the
                  cassette gets a reply without logprobs and a `pick_reply` log record with result "miss"
  proxy           forward to MOCK_PICK_UPSTREAM (loopback only), store the valid-letter top_logprobs (+ one "<other>" entry with the
                  remaining mass) in the cassette and reply with exactly what replay would return
  fixed           reply with MOCK_PICK_TOP_LOGPROBS (JSON list of {token, logprob}) as top_logprobs
  malformed       HTTP 200 with a body that is not JSON
  status:NNN      HTTP NNN with a JSON error body
  reset           close the connection without a reply
MOCK_PICK_DELAY (s) delays every pick reply. MOCK_PICK_HEADER (JSON) adds pypdfium2/pillow/commit/src_sha to a recorded cassette
header. MOCK_RUN_TAG_FILE: a file the harness rewrites before each CLI run; its content is logged as "run" on every request record, so
warm-up and measured requests can be told apart without any field from the app (phase5-parts.md A3.3).
Every pick also logs {"event": "pick_reply", t0, t1, key, result, n_letters}. Non-pick /api/chat requests (load-only prewarm) get an
empty successful reply. Cassettes hold hashes and letter logprobs only: no prompt, image or model text.

Block-kind requests (docs/contracts/phase6-block-classifier.md 12.4): an image /api/chat whose prompt is the stage C KIND_PROMPT (its
sha256 is in MOCK_KIND_PROMPT_SHA256, comma-separated, or, without that, the prompt names the fixed options: running text, table and
chart/plot) is a *kind* request. It is answered exactly like a pick, but from the MOCK_KIND* settings (MOCK_KIND none | replay |
proxy | fixed | malformed | status:NNN | reset, MOCK_KIND_CASSETTE, MOCK_KIND_UPSTREAM, MOCK_KIND_TOP_LOGPROBS, MOCK_KIND_DELAY,
MOCK_KIND_HEADER) and its own cassette, and logs {"event": "kind_reply", ..., "prompt_sha", "png_sha"} (hashes only). The cassette key
is the pick key (model, prompt, crop PNG sha256)."""
import base64, hashlib, io, json, math, os, re, socket, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = os.environ.get("MOCK_LOG", "/tmp/mock_llm.jsonl")
def cfg(k, d): return os.environ.get(k, d)
LETTERS = [chr(c) for c in range(ord("A"), ord("P") + 1)]
_CAS_LOCK = threading.Lock(); _CAS = {"header": {}, "entries": {}}; _KCAS = {"header": {}, "entries": {}}
KIND_HINT = re.compile(r"running text", re.I)


def is_kind_prompt(prompt):
    """True for the stage C KIND_PROMPT (by sha256 when MOCK_KIND_PROMPT_SHA256 is set, else by its fixed option words)."""
    shas = [x.strip() for x in cfg("MOCK_KIND_PROMPT_SHA256", "").split(",") if x.strip()]
    if shas: return hashlib.sha256((prompt or "").encode()).hexdigest() in shas
    p = prompt or ""
    return bool(KIND_HINT.search(p) and re.search(r"\btable\b", p, re.I) and re.search(r"\b(chart|plot)\b", p, re.I))


def pick_key(model, prompt, png):
    return hashlib.sha256((model or "").encode() + b"\x00" + (prompt or "").encode() + b"\x00" + hashlib.sha256(png).hexdigest().encode()).hexdigest()


def pick_parts(req):
    """(prompt, png bytes) of a pick-shaped /api/chat body, else None."""
    for m in req.get("messages") or []:
        ims = m.get("images") if isinstance(m, dict) else None
        if ims:
            c = m.get("content")
            if isinstance(c, list): c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            try: png = base64.b64decode(ims[0])
            except Exception: png = str(ims[0]).encode()
            return (c if isinstance(c, str) else json.dumps(c)), png
    return None


def letter_entries(top):
    """Valid-letter entries (raw token kept, so "A" and " A" stay separate) plus one <other> entry for the remaining mass."""
    keep = []
    for e in top or []:
        if not isinstance(e, dict): continue
        tok, lp = e.get("token"), e.get("logprob")
        if isinstance(tok, str) and tok.strip() in LETTERS and isinstance(lp, (int, float)): keep.append({"token": tok, "logprob": round(float(lp), 6)})
    rest = 1.0 - sum(math.exp(e["logprob"]) for e in keep)
    keep.append({"token": "<other>", "logprob": round(math.log(rest), 6) if rest > 1e-9 else -30.0})
    return keep


def chat_reply(model, top):
    """Ollama /api/chat non-streaming reply; top=None -> no logprobs field at all."""
    out = {"model": model, "created_at": "1970-01-01T00:00:00Z", "done": True, "done_reason": "length", "response": ""}
    if top is None:
        out["message"] = {"role": "assistant", "content": "A"}; return out
    best = max(top, key=lambda e: e["logprob"]) if top else {"token": "", "logprob": 0.0}
    out["message"] = {"role": "assistant", "content": best["token"]}
    out["logprobs"] = [{"token": best["token"], "logprob": best["logprob"], "top_logprobs": top}]
    return out


def cas_load(path, cas=None):
    cas = _CAS if cas is None else cas
    try:
        d = json.load(open(path))
        if isinstance(d, dict) and isinstance(d.get("entries"), dict): cas.update(header=d.get("header") or {}, entries=d["entries"])
    except (OSError, ValueError): pass


def cas_save(path, cas=None):
    cas = _CAS if cas is None else cas; tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump({"header": cas["header"], "entries": dict(sorted(cas["entries"].items()))}, f, indent=1, sort_keys=True); f.write("\n")
    os.replace(tmp, path)


def upstream_json(base, path, body=None, timeout=60):
    rq = urllib.request.Request(base.rstrip("/") + path, data=body, headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(rq, timeout=timeout) as r: return json.loads(r.read())


def loopback(url):
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return h in ("127.0.0.1", "localhost", "::1")


if cfg("MOCK_PICK", "none") in ("replay", "proxy") and cfg("MOCK_PICK_CASSETTE", ""): cas_load(cfg("MOCK_PICK_CASSETTE", ""))
if cfg("MOCK_KIND", "none") in ("replay", "proxy") and cfg("MOCK_KIND_CASSETTE", ""): cas_load(cfg("MOCK_KIND_CASSETTE", ""), _KCAS)
def img_dims(msgs):
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                u = (p.get("image_url") or {}).get("url") if isinstance(p, dict) else None
                if u and "base64," in u:
                    try:
                        from PIL import Image
                        im = Image.open(io.BytesIO(base64.b64decode(u.split("base64,", 1)[1]))); out.append(f"{im.size[0]}x{im.size[1]}")
                    except Exception as e: out.append(f"err:{e}")
    for m in msgs:
        for b in m.get("images", []) or []:
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(base64.b64decode(b))); out.append(f"{im.size[0]}x{im.size[1]}")
            except Exception: pass
    return out
def png_dims(msgs):
    """[(n_images, ["WxH" of each Ollama-style PNG image])] without PIL (IHDR header)."""
    n, dims = 0, []
    for m in msgs:
        if not isinstance(m, dict): continue
        c = m.get("content")
        if isinstance(c, list): n += sum(1 for p in c if isinstance(p, dict) and p.get("image_url"))
        for b in m.get("images", []) or []:
            n += 1
            try:
                raw = base64.b64decode(b)
                if raw[:8] == b"\x89PNG\r\n\x1a\n": dims.append(f"{int.from_bytes(raw[16:20], 'big')}x{int.from_bytes(raw[20:24], 'big')}")
            except Exception: pass
    return n, dims
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _log(self, d):
        tf = cfg("MOCK_RUN_TAG_FILE", "")
        if tf:
            try: d = dict(d, run=open(tf).read().strip() or None)
            except OSError: d = dict(d, run=None)
        open(LOG, "a").write(json.dumps(d) + "\n")
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _api_chat(self, req):
        model = req.get("model"); pp = pick_parts(req)
        if pp is None: return self._json({"model": model, "created_at": "1970-01-01T00:00:00Z", "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "load", "response": ""})
        kind = is_kind_prompt(pp[0]); P = "MOCK_KIND" if kind else "MOCK_PICK"; CAS = _KCAS if kind else _CAS
        t0 = time.time(); mode = cfg(P, "none"); key = pick_key(model, *pp); result = mode; top = None; n_letters = None
        time.sleep(float(cfg(P + "_DELAY", "0") or 0))
        def done():
            rec = {"t": time.time(), "event": "kind_reply" if kind else "pick_reply", "t0": t0, "t1": time.time(), "key": key, "result": result, "n_letters": n_letters,
                   "logprobs_req": req.get("logprobs"), "top_logprobs_req": req.get("top_logprobs"), "options": req.get("options"), "stream": req.get("stream")}
            if kind: rec.update(prompt_sha=hashlib.sha256((pp[0] or "").encode()).hexdigest(), png_sha=hashlib.sha256(pp[1]).hexdigest(), keep_alive=req.get("keep_alive"), model=model)
            self._log(rec)
        try:
            if mode == "reset":
                try: self.connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                self.close_connection = True; return
            if mode == "malformed":
                b = b'{"model": "x", "message": {"content": "A"'; self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            if mode.startswith("status:"):
                return self._json({"error": "mock status"}, int(mode.split(":", 1)[1]))
            if mode == "fixed":
                top = json.loads(cfg(P + "_TOP_LOGPROBS", "[]"))
            elif mode == "replay":
                e = CAS["entries"].get(key)
                if e is None: result = "miss"
                else: result = "hit"; top = e.get("top_logprobs")
            elif mode == "proxy":
                up = cfg(P + "_UPSTREAM", "http://127.0.0.1:11434")
                if not loopback(up): result = "upstream-refused"; return self._json({"error": "upstream must be loopback"}, 502)
                try: r = upstream_json(up, "/api/chat", json.dumps(req).encode(), timeout=float(cfg(P + "_UPSTREAM_TIMEOUT", "120")))
                except Exception as ex: result = "upstream-error:" + type(ex).__name__; return self._json({"error": "upstream failed"}, 502)
                lp = r.get("logprobs") if isinstance(r, dict) else None
                raw = (lp[0] or {}).get("top_logprobs") if isinstance(lp, list) and lp and isinstance(lp[0], dict) else None
                if not raw: result = "upstream-no-logprobs"
                else:
                    top = letter_entries(raw); result = "recorded"
                    with _CAS_LOCK:
                        CAS["entries"][key] = {"top_logprobs": top}
                        if not CAS["header"].get("recorded_at"):
                            hdr = {"model": model, "model_digest": None, "ollama_version": None, "pypdfium2": None, "pillow": None, "commit": None, "src_sha": None}
                            try: hdr["ollama_version"] = upstream_json(up, "/api/version", timeout=5).get("version")
                            except Exception: pass
                            try: hdr["model_digest"] = next((m.get("digest") for m in upstream_json(up, "/api/tags", timeout=5).get("models", []) if m.get("name") == model or m.get("model") == model), None)
                            except Exception: pass
                            try: hdr.update({k: v for k, v in json.loads(cfg(P + "_HEADER", "{}")).items() if k in ("pypdfium2", "pillow", "commit", "src_sha", "figcls_model")})
                            except ValueError: pass
                            if kind: hdr["prompt_sha"] = hashlib.sha256((pp[0] or "").encode()).hexdigest()
                            hdr["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()); CAS["header"] = hdr
                        if cfg(P + "_CASSETTE", ""): cas_save(cfg(P + "_CASSETTE", ""), CAS)
            elif mode != "none":
                result = "unknown-mode"
            n_letters = sum(1 for e in top or [] if isinstance(e, dict) and str(e.get("token", "")).strip() in LETTERS) if top is not None else None
            try: return self._json(chat_reply(model, top))
            except (BrokenPipeError, ConnectionResetError): result += "+client-gone"
        finally:
            done()
    def do_GET(self):
        self._log({"t": time.time(), "method": "GET", "path": self.path})
        if "ps" in self.path: return self._json({"models": []})
        return self._json({"object": "list", "data": [{"id": "mock-text"}, {"id": "mock-vision"}]})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        try: req = json.loads(raw)
        except Exception: req = {}
        msgs = req.get("messages", []); dims = img_dims(msgs); vision = bool(dims)
        allt = json.dumps(msgs)
        self._log({"t": time.time(), "method": "POST", "path": self.path, "model": req.get("model"), "stream": req.get("stream"), "keep_alive": req.get("keep_alive"),
                   "options": req.get("options"), "image_dims": dims, "n_images": png_dims(msgs)[0], "png_dims": png_dims(msgs)[1], "n_msgs": len(msgs), "prompt_tail": allt[-300:]})
        if self.path.split("?")[0].rstrip("/").endswith("/api/chat"):
            return self._api_chat(req)
        if not self.path.rstrip("/").endswith("chat/completions"):
            return self._json({"done": True, "response": ""})
        reply = cfg("MOCK_VISION", "The image shows a chart. I can see three bars.") if vision else cfg("MOCK_TEXT", "Alpha handles 42 requests per second. Beta handles 77.")
        if not vision and cfg("MOCK_REPLY_FILE", ""):
            try: reply = open(cfg("MOCK_REPLY_FILE", ""), encoding="utf-8").read()
            except OSError: pass
        time.sleep(float(cfg("MOCK_FIRST_DELAY", "0.2")))
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            def chunk(s):
                b = s.encode(); self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n"); self.wfile.flush()
            try:
                for i, w in enumerate(reply.split(" ")):
                    tok = (w if i == 0 else " " + w)
                    chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": tok}, "finish_reason": None}]}) + "\n\n")
                    time.sleep(float(cfg("MOCK_TOKEN_DELAY", "0.05")))
                chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"); chunk("data: [DONE]\n\n"); self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError): self._log({"t": time.time(), "event": "client_disconnected"})
            return
        time.sleep(float(cfg("MOCK_TOKEN_DELAY", "0.05")) * len(reply.split(" ")))
        self._json({"id": "x", "object": "chat.completion", "model": req.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]})
SRV = ThreadingHTTPServer(("127.0.0.1", int(cfg("MOCK_PORT", "18890"))), H)
if cfg("MOCK_PORT_FILE", ""):
    with open(cfg("MOCK_PORT_FILE", "") + ".tmp", "w") as f: f.write(str(SRV.server_address[1]))
    os.replace(cfg("MOCK_PORT_FILE", "") + ".tmp", cfg("MOCK_PORT_FILE", ""))
SRV.serve_forever()
