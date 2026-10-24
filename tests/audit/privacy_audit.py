#!/usr/bin/env python3
"""Privacy audit of a capture: privacy_audit.py [--image PATH] [--log runs.jsonl] [--screen WxH] [--thumb OUT]
Defaults: ~/.speakeasy/last_capture.png and last log line. Reports captured rect vs screen, highlight bbox, thumbnail to eyeball."""
import json, os, sys, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image
import highlight
home = os.path.expanduser(os.environ.get("SPEAKEASY_HOME", "~/.speakeasy"))
ap = argparse.ArgumentParser(); ap.add_argument("--image", default=os.path.join(home, "last_capture.png"))
ap.add_argument("--log", default=os.path.join(home, "logs", "runs.jsonl")); ap.add_argument("--screen"); ap.add_argument("--thumb", default="/tmp/speakeasy_capture_thumb.png")
a = ap.parse_args(); out = {"image": a.image}
if not os.path.exists(a.image): out["error"] = "no capture image kept"; print(json.dumps(out, indent=2)); sys.exit(1)
im = Image.open(a.image); out["capture_px"] = f"{im.size[0]}x{im.size[1]}"
last = None
if os.path.exists(a.log):
    ls = [l for l in open(a.log) if l.strip()]
    if ls: last = json.loads(ls[-1])
if last: out.update({k: last.get(k) for k in ("ts", "command", "capture_method", "capture_rect", "image_px", "image_sent_px")})
if a.screen:
    sw, sh = map(int, a.screen.split("x")); out["capture_frac_of_screen"] = round(im.size[0] * im.size[1] / (sw * sh), 3)
    out["verdict"] = "FULL SCREEN captured, check this" if out["capture_frac_of_screen"] > 0.9 else ("large capture" if out["capture_frac_of_screen"] > 0.5 else "ok")
im.convert("RGB").copy().thumbnail((640, 640)); t = im.convert("RGB"); t.thumbnail((640, 640)); t.save(a.thumb)
out["thumbnail"] = a.thumb; out["reminder"] = f"Open {a.thumb} to confirm only the intended region was captured."
out["highlight_in_capture"] = highlight.detect(a.image)
print(json.dumps(out, indent=2))
