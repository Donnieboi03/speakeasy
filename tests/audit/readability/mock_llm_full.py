#!/usr/bin/env python3
"""Mock OpenAI-compatible server for the phase-1 holdout tests. Logs every request body in full (image payloads are
replaced by their sha256 so prompts can be compared byte for byte).
Env: MOCK_PORT, MOCK_LOG, MOCK_TEXT (text reply), MOCK_VISION (streamed/any image reply), MOCK_GROUND (non-streamed image
reply), MOCK_HANG=1 (accept and never answer), MOCK_TOKEN_DELAY, MOCK_FIRST_DELAY."""
import hashlib, json, os, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = os.environ.get("MOCK_LOG", "/dev/null")
def cfg(k, d): return os.environ.get(k, d)
B64 = re.compile(r"data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)")
def scrub(obj):
    if isinstance(obj, str):
        if len(obj) > 400 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", obj): return "b64:" + hashlib.sha256(obj.encode()).hexdigest()[:16]
        return B64.sub(lambda m: "data:image;sha=" + hashlib.sha256(m.group(1).encode()).hexdigest()[:16], obj)
    if isinstance(obj, list): return [scrub(x) for x in obj]
    if isinstance(obj, dict): return {k: scrub(v) for k, v in obj.items()}
    return obj
def has_image(msgs):
    s = json.dumps(msgs)
    return "image_url" in s or any(m.get("images") for m in msgs if isinstance(m, dict))
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _log(self, d):
        with open(LOG, "a") as f: f.write(json.dumps(d) + "\n")
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._log({"t": time.time(), "method": "GET", "path": self.path})
        if "ps" in self.path: return self._json({"models": []})
        return self._json({"object": "list", "data": [{"id": "mock-text"}, {"id": "mock-vision"}]})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        try: req = json.loads(raw)
        except Exception: req = {}
        msgs = req.get("messages", []) or []; img = has_image(msgs)
        self._log({"t": time.time(), "method": "POST", "path": self.path, "image": img, "stream": bool(req.get("stream")), "model": req.get("model"), "body": scrub(req)})
        if cfg("MOCK_HANG", "") == "1":
            time.sleep(3600); return
        if not self.path.rstrip("/").endswith("chat/completions"): return self._json({"done": True, "response": ""})
        if img and not req.get("stream"): reply = cfg("MOCK_GROUND", cfg("MOCK_VISION", "The figure shows bars."))
        elif img: reply = cfg("MOCK_VISION", "The figure shows bars.")
        else: reply = cfg("MOCK_TEXT", "Alpha handles 42 requests per second.")
        time.sleep(float(cfg("MOCK_FIRST_DELAY", "0.05")))
        if req.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            def chunk(s):
                b = s.encode(); self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n"); self.wfile.flush()
            try:
                for i, w in enumerate(reply.split(" ")):
                    chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": w if i == 0 else " " + w}, "finish_reason": None}]}) + "\n\n")
                    time.sleep(float(cfg("MOCK_TOKEN_DELAY", "0.005")))
                chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n"); chunk("data: [DONE]\n\n"); self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError): pass
            return
        self._json({"id": "x", "object": "chat.completion", "model": req.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]})
class S(ThreadingHTTPServer): daemon_threads = True
S(("127.0.0.1", int(cfg("MOCK_PORT", "18960"))), H).serve_forever()
