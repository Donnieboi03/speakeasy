"""Find the bounding box of all selection-highlight pixels in a screenshot (stdlib + optional Pillow/sips)."""
import os, re, struct, subprocess, sys, tempfile, zlib

STEP = 4          # sample every Nth pixel
CELL = 4          # sampled pixels per grid cell (=> 16px cells)
MIN_HITS = 3      # hits needed for a cell to count
MIN_CELLS = 3     # drop components smaller than this (noise)
DILATE = 2        # merge cells within this many cells
PAD = 16          # padding in image pixels

def _system_color():
    """macOS AppleHighlightColor as (r,g,b) 0-255, or None."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleHighlightColor"], capture_output=True, text=True, timeout=2).stdout
        r, g, b = [float(x) for x in out.split()[:3]]
        return (int(r * 255), int(g * 255), int(b * 255))
    except Exception:
        return None

def make_matcher(extra=None):
    """pdf.js/Chromium-style translucent blue selection over white, plus optional system highlight color."""
    def m(r, g, b):
        if b >= 225 and 140 <= r <= 215 and 140 <= g <= 220 and abs(r - g) <= 22 and b - max(r, g) >= 30:
            return True
        if extra:
            er, eg, eb = extra
            return abs(r - er) <= 10 and abs(g - eg) <= 10 and abs(b - eb) <= 10
        return False
    return m

# ---------- pixel sources: yield (width, height, get(x, y) -> (r,g,b)) ----------
def _load_pillow(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    small = im.resize((w // STEP, h // STEP), Image.NEAREST)
    data = small.tobytes()
    sw = small.size[0]
    return w, h, small.size, lambda x, y: data[(y * sw + x) * 3:(y * sw + x) * 3 + 3]

def _load_png(path):
    raw = open(path, "rb").read()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i, idat, hdr = 8, [], None
    while i < len(raw):
        n, t = struct.unpack(">I4s", raw[i:i + 8])
        if t == b"IHDR": hdr = struct.unpack(">IIBBBBB", raw[i + 8:i + 21])
        elif t == b"IDAT": idat.append(raw[i + 8:i + 8 + n])
        elif t == b"IEND": break
        i += 12 + n
    w, h, depth, ctype, _, _, inter = hdr
    if depth != 8 or ctype not in (2, 6) or inter:
        raise ValueError("unsupported PNG format")
    bpp = 3 if ctype == 2 else 4
    data = zlib.decompress(b"".join(idat))
    stride = w * bpp
    prev = bytearray(stride); rows = []
    for y in range(h):
        f = data[y * (stride + 1)]; line = bytearray(data[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for x in range(bpp, stride): line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            line = bytearray((a + b) & 255 for a, b in zip(line, prev))
        elif f == 3:
            for x in range(stride): line[x] = (line[x] + ((line[x - bpp] if x >= bpp else 0) + prev[x]) // 2) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0; b = prev[x]; c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        if y % STEP == 0: rows.append(bytes(line))
        prev = line
    sw, sh = w // STEP, len(rows)
    return w, h, (sw, sh), lambda x, y: rows[y][x * STEP * bpp:x * STEP * bpp + 3]

def _load_sips_bmp(path):
    tmp = tempfile.mktemp(suffix=".bmp")
    subprocess.run(["sips", "-s", "format", "bmp", path, "--out", tmp], capture_output=True, check=True)
    raw = open(tmp, "rb").read(); os.remove(tmp)
    off, = struct.unpack("<I", raw[10:14]); w, h = struct.unpack("<ii", raw[18:26]); bpp = struct.unpack("<H", raw[28:30])[0] // 8
    flip = h > 0; h = abs(h); stride = (w * bpp + 3) & ~3
    def get(x, y):
        yy = y * STEP; yy = h - 1 - yy if flip else yy
        o = off + yy * stride + x * STEP * bpp
        b, g, r = raw[o:o + 3]; return bytes((r, g, b))
    return w, h, (w // STEP, h // STEP), get

def load(path):
    errs = []
    for f in (_load_pillow, _load_sips_bmp, _load_png):
        try: return f(path)
        except Exception as e: errs.append(f"{f.__name__}: {e}")
    raise ValueError("cannot decode image: " + "; ".join(errs))

# ---------- detection ----------
def detect(path, use_system=True):
    """Return (x, y, w, h) bounding box in image pixels covering ALL highlight regions, or None."""
    W, H, (sw, sh), get = load(path)
    match = make_matcher(_system_color() if use_system else None)
    gw, gh = (sw + CELL - 1) // CELL, (sh + CELL - 1) // CELL
    hits = [[0] * gw for _ in range(gh)]
    for y in range(sh):
        row = hits[y // CELL]
        for x in range(sw):
            r, g, b = get(x, y)
            if match(r, g, b):
                row[x // CELL] += 1
    on = {(cx, cy) for cy in range(gh) for cx in range(gw) if hits[cy][cx] >= MIN_HITS}
    seen, keep = set(), []
    for c in on:                       # connected components with dilation (merge nearby blobs)
        if c in seen: continue
        stack, comp = [c], []
        seen.add(c)
        while stack:
            cx, cy = stack.pop(); comp.append((cx, cy))
            for dx in range(-DILATE, DILATE + 1):
                for dy in range(-DILATE, DILATE + 1):
                    n = (cx + dx, cy + dy)
                    if n in on and n not in seen:
                        seen.add(n); stack.append(n)
        if len(comp) >= MIN_CELLS:
            keep.extend(comp)
    if not keep:
        return None
    k = STEP * CELL
    x0 = min(c[0] for c in keep) * k - PAD; y0 = min(c[1] for c in keep) * k - PAD
    x1 = (max(c[0] for c in keep) + 1) * k + PAD; y1 = (max(c[1] for c in keep) + 1) * k + PAD
    x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(W, x1), min(H, y1)
    return (x0, y0, x1 - x0, y1 - y0)

def crop(src, dst, box):
    x, y, w, h = box
    try:
        from PIL import Image
        Image.open(src).crop((x, y, x + w, y + h)).save(dst); return
    except ImportError:
        pass
    subprocess.run(["sips", "-c", str(h), str(w), "--cropOffset", str(y), str(x), src, "--out", dst], capture_output=True, check=True)

def main(argv):
    """highlight.py IN.png OUT.png -> prints 'x,y,w,h' (image px) and writes crop; exit 1 if none found."""
    src, dst = argv[0], argv[1]
    box = detect(src)
    if not box:
        print("none"); return 1
    crop(src, dst, box)
    print("%d,%d,%d,%d" % box); return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
