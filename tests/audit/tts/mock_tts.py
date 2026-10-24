#!/usr/bin/env python3
"""Mock OpenAI-style TTS server. POST /v1/audio/speech -> WAV. Logs each request (JSON lines) to $MOCK_LOG.
Env: MOCK_PORT, MOCK_DELAY (s per request), MOCK_LOG."""
import io, json, os, time, wave, struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = os.environ.get("MOCK_LOG", "/tmp/mock_tts.jsonl"); DELAY = float(os.environ.get("MOCK_DELAY", "0.3"))
def wav(sec=0.5):
    b = io.BytesIO(); w = wave.open(b, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
    w.writeframes(struct.pack("<h", 0) * int(24000 * sec)); w.close(); return b.getvalue()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"status": "ok", "rss_mb": 321.5}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        t0 = time.time(); n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        try: req = json.loads(raw)
        except Exception: req = {"_raw": raw[:200].decode("latin1")}
        time.sleep(DELAY); data = wav()
        open(LOG, "a").write(json.dumps({"t_start": t0, "t_end": time.time(), "path": self.path, "auth": self.headers.get("Authorization"), "req": req}) + "\n")
        if self.path.rstrip("/") not in ("/v1/audio/speech", "/audio/speech"): self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "audio/wav"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("MOCK_PORT", "18880"))), H).serve_forever()
