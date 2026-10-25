"""Phrase grounding for figure sub-objects (Florence-2, local). Runs in the Kokoro venv (torch + transformers),
loaded lazily by speakeasy only when a sentence names something the figure's own labels do not cover.

Protocol (JSON lines): stdin {"id", "pdf", "page", "clip": [x0,y0,x1,y1], "text"} ->
stdout {"id", "boxes": [[x0,y0,x1,y1], ...] (page space), "labels": [...], "ms"}; first line {"ready": true, "device", "load_ms"}.
"""
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

def main():
    t0 = time.time()
    import torch, pdfpage
    from PIL import Image
    import transformers
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    ids = [m for m in [os.environ.get("SPEAKEASY_GROUND_MODEL"), "florence-community/Florence-2-base", "microsoft/Florence-2-base"] if m]
    model = proc = None; err = None
    for mid in ids:
        try:
            if hasattr(transformers, "Florence2ForConditionalGeneration"):
                model = transformers.Florence2ForConditionalGeneration.from_pretrained(mid, dtype=torch.float32)
                proc = transformers.AutoProcessor.from_pretrained(mid)
            else:
                model = transformers.AutoModelForCausalLM.from_pretrained(mid, trust_remote_code=True, torch_dtype=torch.float32)
                proc = transformers.AutoProcessor.from_pretrained(mid, trust_remote_code=True)
            model = model.to(dev).eval(); break
        except Exception as e:
            err = f"{mid}: {type(e).__name__}: {e}"[:300]; model = None
    if model is None:
        print(json.dumps({"ready": False, "error": err}), flush=True); return 1
    print(json.dumps({"ready": True, "device": dev, "load_ms": int((time.time() - t0) * 1000)}), flush=True)
    docs, crops = {}, {}
    task = "<CAPTION_TO_PHRASE_GROUNDING>"
    for line in sys.stdin:
        try:
            q = json.loads(line); t1 = time.time()
            key = (q["pdf"], q["page"], tuple(q["clip"]))
            if key not in crops:
                d = docs.get(q["pdf"]) or docs.setdefault(q["pdf"], pdfpage.open_pdf(q["pdf"]))
                pg = d[q["page"] - 1]; c = q["clip"]
                z = min(3.0, max(1.0, 768.0 / max(1.0, max(c[2] - c[0], c[3] - c[1]))))
                crops[key] = (pg.image(c, z), z)
            img, z = crops[key]
            inputs = proc(text=task + q["text"], images=img, return_tensors="pt").to(dev)
            with torch.no_grad():
                gen = model.generate(input_ids=inputs["input_ids"], pixel_values=inputs["pixel_values"], max_new_tokens=128, num_beams=1, do_sample=False)
            txt = proc.batch_decode(gen, skip_special_tokens=False)[0]
            res = proc.post_process_generation(txt, task=task, image_size=img.size).get(task, {})
            c = q["clip"]
            boxes = [[round(b[0] / z + c[0], 1), round(b[1] / z + c[1], 1), round(b[2] / z + c[0], 1), round(b[3] / z + c[1], 1)] for b in res.get("bboxes", [])]
            print(json.dumps({"id": q.get("id"), "boxes": boxes, "labels": res.get("labels", []), "ms": int((time.time() - t1) * 1000)}), flush=True)
        except Exception as e:
            print(json.dumps({"id": (q or {}).get("id") if isinstance(q, dict) else None, "boxes": [], "error": f"{type(e).__name__}: {e}"[:200]}), flush=True)
    return 0

if __name__ == "__main__":
    q = None
    sys.exit(main())
