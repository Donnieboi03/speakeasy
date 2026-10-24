"""Independent macOS selection-tint (blue highlight) detector. Usage: highlight.py IMAGE [--thumb OUT]"""
import sys, json
import numpy as np
from PIL import Image

def mask(im):
    a = np.asarray(im.convert("RGB")).astype(int); r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (abs(r - g) <= 8) & (b - r >= 45) & (b - r <= 80) & (b >= 55)

def detect(path, block=16, frac=0.05):
    im = Image.open(path); m = mask(im); H, W = m.shape
    bh, bw = H // block, W // block
    blk = m[:bh*block, :bw*block].reshape(bh, block, bw, block).mean(axis=(1, 3)) >= frac
    # largest 8-connected component of blocks (dilated by 2 to bridge line gaps)
    from collections import deque
    dil = blk.copy()
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2): dil |= np.roll(np.roll(blk, dy, 0), dx, 1)
    seen = np.zeros_like(dil); best = []; comps = []
    for y, x in zip(*np.nonzero(dil)):
        if seen[y, x]: continue
        comp = []; q = deque([(y, x)]); seen[y, x] = True
        while q:
            cy, cx = q.popleft(); comp.append((cy, cx))
            for ny in (cy-1, cy, cy+1):
                for nx in (cx-1, cx, cx+1):
                    if 0 <= ny < bh and 0 <= nx < bw and dil[ny, nx] and not seen[ny, nx]: seen[ny, nx] = True; q.append((ny, nx))
        comps.append(comp)
        if len(comp) > len(best): best = comp
    if not best: return {"image_px": f"{W}x{H}", "bbox": None, "pixels": 0}
    big = [c for c in comps if sum(blk[y, x] for y, x in c) >= max(4, 0.05 * sum(blk[y, x] for y, x in best))]
    keep = np.zeros_like(blk)
    for c in big:
        for y, x in c: keep[y, x] = blk[y, x]
    km = np.kron(keep, np.ones((block, block), bool)); full = np.zeros_like(m); full[:bh*block, :bw*block] = km
    sel = m & full; ys, xs = np.nonzero(sel)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    return {"image_px": f"{W}x{H}", "bbox": [x0, y0, x1 - x0, y1 - y0], "pixels": int(sel.sum()), "components": len(big),
            "bbox_frac_of_screen": round((x1 - x0) * (y1 - y0) / (W * H), 3), "stray_tint_pixels": int(m.sum() - sel.sum())}

if __name__ == "__main__":
    res = detect(sys.argv[1])
    if "--thumb" in sys.argv:
        out = sys.argv[sys.argv.index("--thumb") + 1]; im = Image.open(sys.argv[1]).convert("RGB")
        if res["bbox"]:
            from PIL import ImageDraw; x, y, w, h = res["bbox"]; ImageDraw.Draw(im).rectangle([x, y, x+w, y+h], outline="red", width=8)
        im.thumbnail((800, 800)); im.save(out); res["thumb"] = out
    print(json.dumps(res))
