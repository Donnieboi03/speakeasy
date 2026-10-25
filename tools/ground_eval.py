#!/usr/bin/env python3
"""Grounding accuracy of a vision model against a PDF's own coordinates (same method as the qwen3-vl measurement):
for each unique text label inside a figure, render the figure crop at 2x, ask the model for {"bbox_2d":[x1,y1,x2,y2]}
(0-1000 of the crop), map back to page points, compare with the label's PDF rect (IoU, centre error).

  local (free):      python3 tools/ground_eval.py --pdf paper.pdf --provider local --model qwen3-vl:4b-instruct
  OpenRouter (PAID): python3 tools/ground_eval.py --pdf paper.pdf --provider openrouter --model google/gemini-3.5-flash-lite --yes-paid
The OpenRouter key comes from the Keychain (service speakeasy-openrouter) or SPEAKEASY_OR_KEY. Only figure crops and
label strings from the PDF you pass are sent. Prints one JSON summary line; nothing is written to disk except a temp crop."""
import argparse, base64, json, os, random, re, statistics, sys, tempfile, time, urllib.request
from collections import Counter
here = os.path.dirname(os.path.realpath(__file__))
for p in (os.path.join(here, "..", "src"), os.path.expanduser("~/.speakeasy/bin")):
    if os.path.exists(os.path.join(p, "docmap.py")): sys.path.insert(0, p); break
import docmap, pdfpage, provider

ap = argparse.ArgumentParser()
ap.add_argument("--pdf", action="append", required=True); ap.add_argument("--provider", choices=["local", "openrouter"], default="local")
ap.add_argument("--model", required=True); ap.add_argument("-n", type=int, default=20); ap.add_argument("--yes-paid", action="store_true")
a = ap.parse_args()
if a.provider == "openrouter":
    if not a.yes_paid: sys.exit("OpenRouter calls are paid: add --yes-paid to run (about n calls with one image each).")
    key, base = provider.or_key(), provider.or_url()
    if not key: sys.exit("no OpenRouter key: security add-generic-password -U -s speakeasy-openrouter -a openrouter -w")
    url, headers = base + "/chat/completions", {"Content-Type": "application/json", "Authorization": "Bearer " + key}
else:
    url, headers = os.environ.get("SPEAKEASY_LLM_URL", "http://localhost:11434/v1").rstrip("/") + "/chat/completions", {"Content-Type": "application/json"}

items = []
for pdf in a.pdf:
    d = docmap.parse_pdf(pdf); doc = pdfpage.open_pdf(pdf)
    for p in d["pages"]:
        for f in p["figures"]:
            if f["kind"] != "figure": continue
            fa = (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1])
            for l in f.get("labels", []):
                if len(l[4].strip()) >= 4 and (l[2] - l[0]) * (l[3] - l[1]) < 0.15 * fa: items.append((doc, p["n"], f, l))
cnt = Counter((id(it[0]), it[1], it[2]["label"], it[3][4].strip().lower()) for it in items)
items = [it for it in items if cnt[(id(it[0]), it[1], it[2]["label"], it[3][4].strip().lower())] == 1]   # unambiguous labels only
random.seed(1); random.shuffle(items); items = items[:a.n]

def iou(x, y):
    ix = max(0, min(x[2], y[2]) - max(x[0], y[0])); iy = max(0, min(x[3], y[3]) - max(x[1], y[1])); i = ix * iy
    return i / ((x[2] - x[0]) * (x[3] - x[1]) + (y[2] - y[0]) * (y[3] - y[1]) - i)
res = []
png = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_eval_{os.getpid()}.png")
try:
    for doc, pn, f, l in items:
        clip = f["bbox"]; doc[pn - 1].render(clip, 2, png)
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        body = {"model": a.model, "temperature": 0, "max_tokens": 40, "stream": False, "messages": [{"role": "user", "content": [
            {"type": "text", "text": 'Locate ' + re.sub(r"[\"{}]", "", l[4])[:120] + ' in the image. Answer only JSON {"bbox_2d":[x1,y1,x2,y2]} with coordinates 0-1000.'},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
        t0 = time.time(); m = None
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=60))
            m = re.search(r"\[\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]", r["choices"][0]["message"]["content"] or "")
        except (OSError, ValueError, KeyError) as e:
            print(f"call failed: {type(e).__name__}", file=sys.stderr)
        ms = int((time.time() - t0) * 1000)
        if not m: res.append({"miss": True, "ms": ms}); continue
        v = [float(x) / 1000 for x in m.groups()]; w, h = clip[2] - clip[0], clip[3] - clip[1]
        b = [clip[0] + v[0] * w, clip[1] + v[1] * h, clip[0] + v[2] * w, clip[1] + v[3] * h]
        ce = ((b[0] + b[2] - l[0] - l[2]) ** 2 / 4 + (b[1] + b[3] - l[1] - l[3]) ** 2 / 4) ** 0.5
        res.append({"iou": iou(b, l[:4]), "ce": ce, "ms": ms})
finally:
    if os.path.exists(png): os.remove(png)
ok = [r for r in res if not r.get("miss")]
ce = sorted(r["ce"] for r in ok)
print(json.dumps({"provider": a.provider, "model": a.model, "n": len(res), "misses": len(res) - len(ok),
                  "iou_median": round(statistics.median(r["iou"] for r in ok), 2) if ok else None,
                  "iou_ge_0_5": sum(r["iou"] >= 0.5 for r in ok),
                  "center_err_median_pt": round(statistics.median(ce), 1) if ok else None,
                  "center_err_p90_pt": round(ce[max(0, int(0.9 * len(ce)) - 1)], 1) if ok else None,
                  "ms_median": statistics.median(r["ms"] for r in res) if res else None}))
