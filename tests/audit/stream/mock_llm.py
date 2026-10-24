#!/usr/bin/env python3
"""Mock OpenAI-compatible chat server. Env: MOCK_PORT, MOCK_LOG, MOCK_TEXT (text-model reply), MOCK_VISION (vision reply),
MOCK_TOKEN_DELAY (s between streamed tokens), MOCK_FIRST_DELAY (s before first token). Logs every request incl. image dims."""
import base64, io, json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = os.environ.get("MOCK_LOG", "/tmp/mock_llm.jsonl")
def cfg(k, d): return os.environ.get(k, d)
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
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _log(self, d): open(LOG, "a").write(json.dumps(d) + "\n")
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
        msgs = req.get("messages", []); dims = img_dims(msgs); vision = bool(dims)
        allt = json.dumps(msgs)
        self._log({"t": time.time(), "method": "POST", "path": self.path, "model": req.get("model"), "stream": req.get("stream"), "keep_alive": req.get("keep_alive"),
                   "options": req.get("options"), "image_dims": dims, "n_msgs": len(msgs), "prompt_tail": allt[-300:]})
        if not self.path.rstrip("/").endswith("chat/completions"):
            return self._json({"done": True, "response": ""})
        reply = cfg("MOCK_VISION", "The image shows a chart. I can see three bars.") if vision else cfg("MOCK_TEXT", "Alpha handles 42 requests per second. Beta handles 77.")
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
ThreadingHTTPServer(("127.0.0.1", int(cfg("MOCK_PORT", "18890"))), H).serve_forever()
