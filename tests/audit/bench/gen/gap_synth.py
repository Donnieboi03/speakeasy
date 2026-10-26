#!/usr/bin/env python3
"""Gap-family generator (docs/contracts/phase6-block-classifier.md 12.2): gap_synth.py SPEC.json OUTDIR -> doc.pdf, gt.json,
cases.json and, for Read-area cases, area-<id>.png.

Ground truth comes from the generator's own drawing coordinates (page space, PDF points, origin top-left), never from
parsing the output. Canvas(invariant=1), the vendored DejaVu font, Pillow rasters and seeded noise, so the same spec gives
byte-identical files. Items are placed absolutely (slides and report pages), not flowed. One painter interface draws on a
reportlab canvas (vector pages) or a Pillow image (scanned pages, raster tables, area captures), so a body looks the same
everywhere and its GT rect is computed once.

Spec keys: doc_id, page [w, h], size, leading, pages, cases, areas.
pages[]: {"scan": null | {"dpi": 150, "rotate": 0.4, "noise": 6, "seed": 1}, "items": [...]}
items (rects are [x0, y0, x1, y1] in pt; paragraphs and tables take [x0, y0, x1] and grow down):
  {"text": "Title", "at": [x, y], "size": 20, "kind": "heading" | "header" | "footer" | "other"}
  {"para": "p1", "rect": [x0, y0, x1], "s": ["Sentence.", ...], "size": 10, "decoy": true}   decoy: its first line is a prose decoy
  {"figure": "F1", "body": "bars" | "line" | "pie" | "scatter" | "flow" | "photo", "rect": [...], "data": [...], "labels": [...],
   "seed": 1, "frame": false, "gap": "uncaptioned" | "lexicon" | "standard" | "no-text",
   "caption": {"text": ["Exhibit 3: Revenue."], "pos": "below" | "above", "style": "exhibit", "label": "Figure 3", "polluted": "ygy"}}
  {"table": "T1", "rect": [x0, y0, x1], "rows": [[...]], "style": "ruled" | "grid" | "borderless", "image": false, "size": 8,
   "gap": ..., "caption": {...}}                                     image: true draws the whole table as one PNG (G4)
  {"decoy": "logo" | "icon" | "band" | "textbox" | "text-image" | "background", "rect": [...], "word": "ACME", "s": [...]}
cases[]: {"id": "c01", "select": "p1"}  (read-through of a paragraph, lines-scored)
         {"id": "r01", "select": "T2:caption", "script": [{"text": "...", "row": 1} | {"text": "...", "score": "none"}]}
areas[]: {"id": "a1", "size": [w, h] (capture px), "origin": [x, y] (screen pt), "items": [...] (px), "script": ["..."]}
GT additions over speakeasy-bench-gt/1: pages[].kind_scored, pages[].scan, pages[].prose_decoys (line rects), figure/table
blocks' gap, caption_style, raster, body, table_style, polluted, rows (row rects); decoy blocks (kind decoration | text-image,
decoy: <type>); areas[] with capture-pixel blocks; table_row sentences {score: "table_row", row_rect, table}."""
import base64, hashlib, io, json, math, os, random, re, sys
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw, ImageFont, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(os.path.dirname(HERE), "fonts", "DejaVuSans.ttf")
FONT = "DejaVuSans"
REF = re.compile(r"\b(Fig\.|Figure|Table)\s*\d")
INK, GRID, BLUE = (35, 38, 48), (120, 124, 135), (70, 110, 170)
PALETTE = [(70, 110, 170), (214, 120, 60), (90, 160, 110), (180, 80, 120), (150, 130, 60), (90, 90, 160)]
if FONT not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont(FONT, FONT_PATH))


def r2(v): return round(v + 0.0, 2)
def rect(x0, y0, x1, y1): return [r2(x0), r2(y0), r2(x1), r2(y1)]
def union(rs):
    rs = [r for r in rs if r]
    return rect(min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)) if rs else None
def thash(s): return hashlib.sha256(re.sub(r"\s+", " ", s).strip().encode("utf-8")).hexdigest()[:12]
def sw(s, size): return pdfmetrics.stringWidth(s, FONT, size)
def metrics(size): return pdfmetrics.getAscentDescent(FONT, size)


# ------------------------------------------------------------------ painters (pt, origin top-left)
class RL:
    def __init__(self, c, H): self.c, self.H = c, H
    def _set(self, stroke, fill, lw):
        if stroke: self.c.setStrokeColorRGB(*[v / 255 for v in stroke]); self.c.setLineWidth(lw)
        if fill: self.c.setFillColorRGB(*[v / 255 for v in fill])
    def rect(self, r, stroke=None, fill=None, lw=0.8, radius=0):
        self._set(stroke, fill, lw); x0, y0, x1, y1 = r
        if radius: self.c.roundRect(x0, self.H - y1, x1 - x0, y1 - y0, radius, stroke=int(bool(stroke)), fill=int(bool(fill)))
        else: self.c.rect(x0, self.H - y1, x1 - x0, y1 - y0, stroke=int(bool(stroke)), fill=int(bool(fill)))
        return rect(*r)
    def line(self, x0, y0, x1, y1, color=INK, lw=0.8):
        self._set(color, None, lw); self.c.line(x0, self.H - y0, x1, self.H - y1); return rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    def poly(self, pts, stroke=None, fill=None, lw=0.8, closed=True):
        self._set(stroke, fill, lw); p = self.c.beginPath(); p.moveTo(pts[0][0], self.H - pts[0][1])
        for x, y in pts[1:]: p.lineTo(x, self.H - y)
        if closed: p.close()
        self.c.drawPath(p, stroke=int(bool(stroke)), fill=int(bool(fill) and closed))
        return rect(min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts))
    def circle(self, cx, cy, r, stroke=None, fill=None, lw=0.8):
        self._set(stroke, fill, lw); self.c.circle(cx, self.H - cy, r, stroke=int(bool(stroke)), fill=int(bool(fill))); return rect(cx - r, cy - r, cx + r, cy + r)
    def wedge(self, cx, cy, r, a0, a1, fill):
        self._set((255, 255, 255), fill, 0.6); self.c.wedge(cx - r, self.H - cy - r, cx + r, self.H - cy + r, -a1, a1 - a0, stroke=1, fill=1)
        return rect(cx - r, cy - r, cx + r, cy + r)
    def text(self, x, ytop, s, size, color=INK, invisible=False):
        asc, desc = metrics(size); base = ytop + asc
        t = self.c.beginText(x, self.H - base); t.setFont(FONT, size); t.setFillColorRGB(*[v / 255 for v in color])
        if invisible: self.c.saveState(); t.setTextRenderMode(3)
        t.textOut(s); self.c.drawText(t)
        if invisible: self.c.restoreState()
        return rect(x, ytop, x + sw(s, size), base - desc)
    def image(self, r, im):
        x0, y0, x1, y1 = r; self.c.drawImage(ImageReader(im), x0, self.H - y1, x1 - x0, y1 - y0); return rect(*r)


class PX:
    """Pillow painter: pt coordinates scaled by s into an RGB image (text becomes pixels, no text layer)."""
    def __init__(self, im, s): self.im, self.s, self.d = im, s, ImageDraw.Draw(im)
    def P(self, *v): return [int(round(x * self.s)) for x in v]
    def w(self, lw): return max(1, int(round(lw * self.s)))
    def rect(self, r, stroke=None, fill=None, lw=0.8, radius=0):
        b = self.P(*r); b = [b[0], b[1], max(b[0], b[2] - 1), max(b[1], b[3] - 1)]
        if radius: self.d.rounded_rectangle(b, int(round(radius * self.s)), outline=stroke, fill=fill, width=self.w(lw))
        else: self.d.rectangle(b, outline=stroke, fill=fill, width=self.w(lw))
        return rect(*r)
    def line(self, x0, y0, x1, y1, color=INK, lw=0.8):
        self.d.line(self.P(x0, y0, x1, y1), fill=color, width=self.w(lw)); return rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    def poly(self, pts, stroke=None, fill=None, lw=0.8, closed=True):
        q = [tuple(self.P(x, y)) for x, y in pts]
        if closed: self.d.polygon(q, fill=fill, outline=stroke)
        else: self.d.line(q, fill=stroke, width=self.w(lw), joint="curve")
        return rect(min(x for x, _ in pts), min(y for _, y in pts), max(x for x, _ in pts), max(y for _, y in pts))
    def circle(self, cx, cy, r, stroke=None, fill=None, lw=0.8):
        self.d.ellipse(self.P(cx - r, cy - r, cx + r, cy + r), outline=stroke, fill=fill, width=self.w(lw)); return rect(cx - r, cy - r, cx + r, cy + r)
    def wedge(self, cx, cy, r, a0, a1, fill):
        self.d.pieslice(self.P(cx - r, cy - r, cx + r, cy + r), a0, a1, fill=fill, outline=(255, 255, 255)); return rect(cx - r, cy - r, cx + r, cy + r)
    def text(self, x, ytop, s, size, color=INK, invisible=False):
        asc, desc = metrics(size); base = ytop + asc
        if not invisible:
            f = ImageFont.truetype(FONT_PATH, max(4, int(round(size * self.s))))
            self.d.text((int(round(x * self.s)), int(round(base * self.s))), s, font=f, fill=color, anchor="ls")
        return rect(x, ytop, x + sw(s, size), base - desc)
    def image(self, r, im):
        b = self.P(*r); self.im.paste(im.convert("RGB").resize((max(1, b[2] - b[0]), max(1, b[3] - b[1])), Image.BILINEAR), (b[0], b[1])); return rect(*r)


# ------------------------------------------------------------------ rasters (Pillow, seeded)
def photo(w_pt, h_pt, seed, k=3):
    """Photo-like raster: sky gradient, sun, layered hills, seeded speckle."""
    rng = random.Random(seed); iw, ih = max(8, int(w_pt * k)), max(8, int(h_pt * k))
    im = Image.new("RGB", (iw, ih)); d = ImageDraw.Draw(im)
    top, bot = (120 + rng.randrange(60), 160 + rng.randrange(50), 220), (230, 220 + rng.randrange(30), 200)
    for y in range(ih):
        t = y / max(1, ih - 1); d.line([(0, y), (iw, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    sx, sy, sr = rng.randrange(iw // 5, 4 * iw // 5), rng.randrange(ih // 8, ih // 3), max(4, ih // 9)
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(250, 220, 120))
    for layer in range(3):
        base = ih * (0.55 + 0.13 * layer); pts = [(0, ih)]
        for i in range(9):
            pts.append((iw * i / 8, base - rng.randrange(int(ih * 0.05), int(ih * 0.22))))
        pts.append((iw, ih)); g = 90 + 35 * layer
        d.polygon(pts, fill=(40 + 20 * layer, g, 60 + 10 * layer))
    px = im.load()
    for _ in range(iw * ih // 40):
        x, y = rng.randrange(iw), rng.randrange(ih); v = rng.randrange(-25, 26); c = px[x, y]
        px[x, y] = tuple(max(0, min(255, a + v)) for a in c)
    return im


def gradient(w, h, seed, k=2):
    rng = random.Random(seed); iw, ih = max(8, int(w * k)), max(8, int(h * k)); im = Image.new("RGB", (iw, ih)); d = ImageDraw.Draw(im)
    a, b = (236, 240, 248), (210 + rng.randrange(20), 222, 238)
    for y in range(ih):
        t = y / max(1, ih - 1); d.line([(0, y), (iw, y)], fill=tuple(int(p + (q - p) * t) for p, q in zip(a, b)))
    for i in range(6):
        cx, cy, r = rng.randrange(iw), rng.randrange(ih), rng.randrange(ih // 8, ih // 3)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(225, 230, 242), width=max(2, iw // 300))
    return im


# ------------------------------------------------------------------ bodies (return drawn piece rects)
def axes(p, r, pieces):
    x0, y0, x1, y1 = r; ax0, ay1 = x0 + 22, y1 - 14
    pieces += [p.line(ax0, y0 + 4, ax0, ay1, INK, 0.8), p.line(ax0, ay1, x1 - 4, ay1, INK, 0.8)]
    return ax0, y0 + 6, x1 - 6, ay1


def body_bars(p, r, it, pieces):
    data = it.get("data") or [3, 5, 2, 6]; ax0, ay0, ax1, ay1 = axes(p, r, pieces); n = len(data); mx = max(data)
    bw = (ax1 - ax0) / (n * 1.5)
    for i, v in enumerate(data):
        bx = ax0 + bw * 0.4 + i * bw * 1.5; bh = (ay1 - ay0 - 2) * v / mx
        pieces.append(p.rect([bx, ay1 - bh, bx + bw, ay1], fill=PALETTE[i % len(PALETTE)] if it.get("multi") else BLUE))
        lab = (it.get("labels") or [])[i] if i < len(it.get("labels") or []) else str(i + 1)
        pieces.append(p.text(bx + bw / 2 - sw(lab, 6.5) / 2, ay1 + 2, lab, 6.5))
    for t in (0, mx): pieces.append(p.text(r[0] + 2, ay1 - (ay1 - ay0 - 2) * t / mx - 4, str(t), 6.5))


def body_line(p, r, it, pieces):
    data = it.get("data") or [2, 4, 3, 6, 5, 7]; ax0, ay0, ax1, ay1 = axes(p, r, pieces); mx, mn = max(data), min(data) - 1
    pts = [(ax0 + 6 + i * (ax1 - ax0 - 12) / (len(data) - 1), ay1 - 4 - (ay1 - ay0 - 8) * (v - mn) / (mx - mn)) for i, v in enumerate(data)]
    for k in range(1, 4): pieces.append(p.line(ax0, ay1 - k * (ay1 - ay0) / 4, ax1, ay1 - k * (ay1 - ay0) / 4, (205, 208, 215), 0.4))
    pieces.append(p.poly(pts, stroke=(200, 80, 60), lw=1.4, closed=False))
    for x, y in pts: pieces.append(p.circle(x, y, 1.8, fill=(200, 80, 60)))
    for t in (mn, mx): pieces.append(p.text(r[0] + 2, ay1 - 4 - (ay1 - ay0 - 8) * (t - mn) / (mx - mn) - 4, str(t), 6.5))


def body_pie(p, r, it, pieces):
    data = it.get("data") or [40, 25, 20, 15]; x0, y0, x1, y1 = r; rad = min(x1 - x0, y1 - y0) / 2 - 3
    cx, cy = x0 + 3 + rad, (y0 + y1) / 2; tot = float(sum(data)); a = -90.0
    for i, v in enumerate(data):
        e = 360.0 * v / tot; pieces.append(p.wedge(cx, cy, rad, a, a + e, PALETTE[i % len(PALETTE)])); a += e
    lx = cx + rad + 10
    for i, lab in enumerate(it.get("labels") or []):
        ly = y0 + 6 + i * 13
        if ly + 9 > y1 or lx + 40 > x1: break
        pieces.append(p.rect([lx, ly, lx + 7, ly + 7], fill=PALETTE[i % len(PALETTE)])); pieces.append(p.text(lx + 10, ly - 0.5, lab, 7))


def body_scatter(p, r, it, pieces):
    rng = random.Random(it.get("seed", 1)); ax0, ay0, ax1, ay1 = axes(p, r, pieces)
    for i in range(it.get("n", 24)):
        u = rng.random(); x = ax0 + 6 + u * (ax1 - ax0 - 12); y = ay1 - 6 - (0.15 + 0.7 * u + 0.25 * (rng.random() - 0.5)) * (ay1 - ay0 - 12)
        pieces.append(p.circle(x, max(ay0 + 3, min(ay1 - 3, y)), 2.0, fill=(90, 120, 180)))


def body_flow(p, r, it, pieces):
    labs = it.get("labels") or ["Input", "Check", "Store"]; x0, y0, x1, y1 = r; n = len(labs)
    gap = 22.0; bw = (x1 - x0 - gap * (n - 1)) / n; bh = min(34.0, y1 - y0); by = (y0 + y1) / 2 - bh / 2
    for i, lab in enumerate(labs):
        bx = x0 + i * (bw + gap)
        pieces.append(p.rect([bx, by, bx + bw, by + bh], stroke=INK, fill=(232, 238, 248), lw=0.9, radius=4))
        size = 8.0
        while size > 5 and sw(lab, size) > bw - 6: size -= 0.5
        pieces.append(p.text(bx + bw / 2 - sw(lab, size) / 2, by + bh / 2 - size * 0.6, lab, size))
        if i < n - 1:
            ax, ay = bx + bw + 2, by + bh / 2; pieces.append(p.line(ax, ay, ax + gap - 6, ay, INK, 0.9))
            pieces.append(p.poly([(ax + gap - 4, ay), (ax + gap - 9, ay - 3), (ax + gap - 9, ay + 3)], fill=INK))


def body_photo(p, r, it, pieces):
    pieces.append(p.image(r, photo(r[2] - r[0], r[3] - r[1], it.get("seed", 1))))


BODIES = {"bars": body_bars, "line": body_line, "pie": body_pie, "scatter": body_scatter, "flow": body_flow, "photo": body_photo}
BODY_CLASS = {"bars": "vector-chart", "line": "vector-chart", "pie": "vector-chart", "scatter": "vector-chart", "flow": "flow-diagram", "photo": "raster-photo"}


def table_layout(rows, x0, y0, x1, size):
    ncols = max(len(r) for r in rows); need = [max(sw(r[j] if j < len(r) else "", size) for r in rows) + 12 for j in range(ncols)]
    sc = (x1 - x0) / sum(need); cws = [v * sc for v in need]; rh = size * 1.75
    return cws, rh, y0 + 3 + rh * len(rows) + 3


def draw_table(p, rows, x0, y0, x1, size, style, pieces):
    """Returns per-row GT rects (table width x the row's cell-text band)."""
    cws, rh, y_end = table_layout(rows, x0, y0, x1, size); asc, desc = metrics(size); row_rects = []
    if style in ("ruled", "grid"): pieces.append(p.line(x0, y0, x1, y0, INK, 1.0))
    yy = y0 + 3
    for i, row in enumerate(rows):
        cx = x0; texts = []
        for j, cell in enumerate(row):
            if cell: texts.append(p.text(cx + 4, yy + (rh - (asc - desc)) / 2, cell, size)); pieces.append(texts[-1])
            cx += cws[j]
        row_rects.append(rect(x0, min(t[1] for t in texts), x1, max(t[3] for t in texts)) if texts else None)
        yy += rh
        if style == "ruled" and i == 0: pieces.append(p.line(x0, yy, x1, yy, INK, 0.6))
        if style == "grid" and i < len(rows) - 1: pieces.append(p.line(x0, yy, x1, yy, GRID, 0.5))
    if style in ("ruled", "grid"): pieces.append(p.line(x0, y_end, x1, y_end, INK, 1.0))
    if style == "grid":
        cx = x0
        for j in range(len(cws) + 1):
            pieces.append(p.line(cx, y0, cx, y_end, GRID, 0.5)); cx += cws[j] if j < len(cws) else 0
    return row_rects, y_end


# ------------------------------------------------------------------ generator
class Gen:
    def __init__(self, spec, out):
        self.S = spec; self.out = out; self.W, self.H = spec.get("page", [595.3, 841.9])
        self.size = spec.get("size", 10); self.leading = spec.get("leading", 13)
        self.c = canvas.Canvas(os.path.join(out, "doc.pdf"), pagesize=(self.W, self.H), invariant=1)
        self.pages = []; self.sentences = []; self.paras = {}; self.items = {}; self.areas = []

    def add_sentences(self, owner, sents, placed, page):
        pos = 0; ids = []
        for st in sents:
            a, b = pos, pos + len(st); pos = b + 1
            hold = [r for r, s0, e0 in placed if s0 < b and e0 > a]
            sid = f"s{len(self.sentences) + 1}"
            self.sentences.append({"id": sid, "page": page, "text_hash": thash(st), "score": "none" if REF.search(st) else "lines",
                                   "line_rects": hold, "_text": st, "_owner": owner})
            ids.append(sid)
        return ids

    def wrap(self, text, width, size):
        toks = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]; lines, cur = [], []
        for a, b in toks:
            if cur and sw(text[cur[0][0]:b], size) > width: lines.append(cur); cur = []
            cur.append((a, b))
        if cur: lines.append(cur)
        return [(text[ln[0][0]:ln[-1][1]], ln[0][0], ln[-1][1]) for ln in lines]

    def cap_lines(self, cap, width):
        """Caption lines; with "break" every text element starts a new line (IEEE `TABLE IV` above its title)."""
        size = cap.get("size", 8.5)
        if not cap.get("break"): return self.wrap(" ".join(cap["text"]), width, size)
        out, pos = [], 0
        for t in cap["text"]:
            out += [(s, a + pos, b + pos) for s, a, b in self.wrap(t, width, size)]; pos += len(t) + 1
        return out

    # ---------------------------------------------------------- items
    def item(self, p, pg, it, text_layer):
        def textline(x, y, s, size, kind=None, body=True, color=INK, invisible=False):
            r = p.text(x, y, s, size, color, invisible)
            if body and text_layer and not invisible: pg["lines"].append(r); pg["_text"].append(s)
            if kind: pg["blocks"].append({"kind": kind, "bbox": r})
            return r
        if "text" in it:
            r = textline(it["at"][0], it["at"][1], it["text"], it.get("size", 14), it.get("kind", "heading"), color=tuple(it.get("color", INK)))
            if it.get("decoy"): pg["prose_decoys"].append(r)
        elif "para" in it:
            size = it.get("size", self.size); lead = it.get("leading", size * 1.3); x0, y, x1 = it["rect"][:3]
            text = " ".join(it["s"]); placed = []
            for s, a, b in self.wrap(text, x1 - x0, size):
                placed.append((textline(x0, y, s, size), a, b)); y += lead
            pg["blocks"].append({"kind": "paragraph", "bbox": union([q[0] for q in placed])})
            if it.get("decoy"): pg["prose_decoys"].append(placed[0][0])
            self.paras[it["para"]] = {"page": pg["n"], "lines": [q[0] for q in placed],
                                      "sids": self.add_sentences(it["para"], it["s"], placed, pg["n"]) if text_layer and pg["n"] else []}
        elif "figure" in it or "table" in it:
            self.fig_or_table(p, pg, it, textline)
        elif "decoy" in it:
            self.decoy(p, pg, it, textline)
        else:
            raise SystemExit(f"{self.S['doc_id']}: unknown item {it}")

    def caption(self, pg, it, body_rect, textline):
        cap = it.get("caption")
        if not cap: return None
        size = cap.get("size", 8.5); lead = size * 1.3; x0 = body_rect[0]; x1 = cap.get("x1", body_rect[2])
        lines = self.cap_lines(cap, x1 - x0)
        y = body_rect[3] + 6 if cap.get("pos", "below") == "below" else body_rect[1] - 6 - lead * len(lines)
        placed = []
        for k, (s, a, b) in enumerate(lines):
            if k == 0 and cap.get("polluted"):
                junk = cap["polluted"] + " "; textline(x0 - sw(junk, size) - 0.5, y - 0.6, junk.strip(), size, invisible=True)
            placed.append((textline(x0, y, s, size), a, b)); y += lead
        key = it.get("figure") or it.get("table"); label = cap.get("label")
        pg["blocks"].append({"kind": "caption", "bbox": union([q[0] for q in placed]), "label": label})
        self.items[key]["caption_lines"] = [q[0] for q in placed]
        self.items[key]["caption_sids"] = self.add_sentences(key + ":caption", cap["text"], placed, pg["n"]) if self.text_layer and pg["n"] else []
        return label

    def fig_or_table(self, p, pg, it, textline):
        key = it.get("figure") or it.get("table"); kind = "figure" if "figure" in it else "table"
        self.items[key] = {"kind": kind, "page": pg["n"], "item": it}
        pieces = []; raster = bool(pg.get("scan")) or it.get("body") == "photo" or bool(it.get("image")); row_rects = None
        cap = it.get("caption") or {}
        if kind == "table" and cap.get("pos", "above") == "above" and cap:
            cap_h = len(self.cap_lines(cap, cap.get("x1", it["rect"][2]) - it["rect"][0])) * cap.get("size", 8.5) * 1.3
            it = dict(it, rect=[it["rect"][0], it["rect"][1] + cap_h + 6, it["rect"][2]]); cap = dict(cap, pos="above")
            it["caption"] = cap
        if kind == "figure":
            r = it["rect"]
            if it.get("frame"): pieces.append(p.rect(r, stroke=GRID, lw=0.6))
            BODIES[it["body"]](p, r, it, pieces)
        else:
            x0, y0, x1 = it["rect"][:3]; size = it.get("size", 8); style = it.get("style", "ruled")
            if it.get("image"):
                k = 3; cws, rh, y_end = table_layout(it["rows"], x0, y0, x1, size)
                im = Image.new("RGB", (int(round((x1 - x0) * k)), int(round((y_end - y0) * k))), (255, 255, 255))
                sub = PX(im, k); tmp = []
                rows_local, _ = draw_table(Translated(sub, -x0, -y0), it["rows"], x0, y0, x1, size, style, tmp)
                pieces.append(p.image([x0, y0, x1, y_end], im)); row_rects = rows_local
            else:
                row_rects, _ = draw_table(p, it["rows"], x0, y0, x1, size, style, pieces)
        bb = union(pieces)
        blk = {"kind": kind, "bbox": bb, "gap": it.get("gap", "uncaptioned"), "caption_style": cap.get("style", "none") if cap else "none",
               "raster": raster}
        if kind == "figure": blk["body"] = BODY_CLASS[it["body"]]
        else: blk["table_style"] = it.get("style", "ruled"); blk["rows"] = row_rects if it.get("image") else None
        if cap.get("polluted"): blk["polluted"] = True
        pg["blocks"].append(blk); self.items[key].update(bbox=bb, block=blk, rows=row_rects)
        label = self.caption(pg, it, bb, textline)
        if label: blk["label"] = label

    def decoy(self, p, pg, it, textline):
        t = it["decoy"]; r = it.get("rect"); pieces = []; kind = "decoration"
        if t == "logo":
            h = r[3] - r[1]; pieces.append(p.circle(r[0] + h / 2, r[1] + h / 2, h / 2 - 1, fill=tuple(it.get("color", (200, 70, 60)))))
            pieces.append(p.poly([(r[0] + h * 0.3, r[1] + h * 0.7), (r[0] + h / 2, r[1] + h * 0.25), (r[0] + h * 0.7, r[1] + h * 0.7)], fill=(255, 255, 255)))
            word = it.get("word", "ACME"); size = min(h * 0.55, 16.0)
            while size > 5 and r[0] + h + 4 + sw(word, size) > r[2]: size -= 0.5
            pieces.append(textline(r[0] + h + 4, r[1] + h / 2 - size * 0.62, word, size, body=False, color=(60, 60, 70)))
        elif t == "icon":
            s = min(r[2] - r[0], r[3] - r[1]); x0, y0 = r[0], r[1]
            pieces.append(p.rect([x0, y0, x0 + s, y0 + s], fill=tuple(it.get("color", (90, 140, 200))), radius=s * 0.2))
            pieces.append(p.circle(x0 + s / 2, y0 + s * 0.42, s * 0.18, fill=(255, 255, 255)))
            pieces.append(p.rect([x0 + s * 0.28, y0 + s * 0.66, x0 + s * 0.72, y0 + s * 0.76], fill=(255, 255, 255)))
        elif t == "band":
            pieces.append(p.rect(r, fill=tuple(it.get("color", (60, 90, 150)))))
            for k in range(1, 4): pieces.append(p.rect([r[0] + k * (r[2] - r[0]) / 4 - 2, r[1], r[0] + k * (r[2] - r[0]) / 4 + 2, r[3]], fill=(240, 200, 90)))
        elif t == "background":
            r = [0, 0, self.W, self.H]; pieces.append(p.image(r, gradient(self.W, self.H, it.get("seed", 1))))
        elif t == "textbox":
            pieces.append(p.rect(r, stroke=tuple(it.get("color", (90, 110, 150))), fill=(246, 248, 252), lw=1.2, radius=6))
            size = it.get("size", 9.5); lead = size * 1.3; y = r[1] + 8; text = " ".join(it["s"]); placed = []
            for s, a, b in self.wrap(text, r[2] - r[0] - 16, size):
                placed.append((textline(r[0] + 8, y, s, size), a, b)); y += lead
            if y > r[3]: raise SystemExit(f"{self.S['doc_id']}: textbox text overflows its box")
            pg["blocks"].append({"kind": "paragraph", "bbox": union([q[0] for q in placed])})
            if self.text_layer and pg["n"]: self.add_sentences("textbox", it["s"], placed, pg["n"])
        elif t == "text-image":
            k = 3; im = Image.new("RGB", (int((r[2] - r[0]) * k), int((r[3] - r[1]) * k)), (252, 252, 250)); sub = Translated(PX(im, k), -r[0], -r[1])
            size = it.get("size", 8.5); y = r[1] + 5
            for s, _, _ in self.wrap(" ".join(it["s"]), r[2] - r[0] - 10, size):
                if y + size * 1.3 > r[3] - 3: break
                sub.text(r[0] + 5, y, s, size); y += size * 1.3
            sub.rect([r[0], r[1], r[2], r[1] + 3], fill=(200, 205, 215))
            pieces.append(p.image(r, im)); kind = "text-image"
        else:
            raise SystemExit(f"{self.S['doc_id']}: unknown decoy {t}")
        pg["blocks"].append({"kind": kind, "bbox": union(pieces), "decoy": t})

    # ---------------------------------------------------------- pages
    def page(self, n, spec):
        scan = spec.get("scan"); self.text_layer = not scan
        pg = {"n": n, "w": r2(self.W), "h": r2(self.H), "blocks_scored": not scan, "kind_scored": True, "scan": bool(scan),
              "lines": [], "blocks": [], "prose_decoys": [], "_text": []}
        self.pages.append(pg)
        if not scan:
            p = RL(self.c, self.H)
            for it in spec["items"]: self.item(p, pg, it, True)
        else:
            s = scan.get("dpi", 150) / 72.0; im = Image.new("RGB", (int(round(self.W * s)), int(round(self.H * s))), (255, 255, 255))
            p = PX(im, s)
            for it in spec["items"]: self.item(p, pg, it, False)
            ang = scan.get("rotate", 0.4); cx, cy = im.size[0] / 2.0, im.size[1] / 2.0
            im = im.convert("L").rotate(ang, resample=Image.BILINEAR, fillcolor=255, center=(cx, cy))
            nz = int(scan.get("noise", 6))
            if nz:
                rng = random.Random(scan.get("seed", 1)); lut = [128 + (v % (2 * nz + 1)) - nz if v < 40 else 128 for v in range(256)]
                noise = Image.frombytes("L", im.size, rng.randbytes(im.size[0] * im.size[1])).point(lut)
                im = ImageChops.add(im, noise, 1.0, -128)
            t = math.radians(ang)
            def tf(x, y):
                X, Y = x * s - cx, y * s - cy
                return ((cx + X * math.cos(t) + Y * math.sin(t)) / s, (cy - X * math.sin(t) + Y * math.cos(t)) / s)
            for b in pg["blocks"]:
                x0, y0, x1, y1 = b["bbox"]; q = [tf(x0, y0), tf(x1, y0), tf(x0, y1), tf(x1, y1)]
                b["bbox"] = rect(max(0, min(a for a, _ in q)), max(0, min(c for _, c in q)), min(self.W, max(a for a, _ in q)), min(self.H, max(c for _, c in q)))
                if b.get("rows"): b["rows"] = None
            buf = io.BytesIO(); im.save(buf, format="PNG", optimize=False); buf.seek(0)
            self.c.drawImage(ImageReader(Image.open(buf)), 0, 0, self.W, self.H)
        self.c.showPage()

    def area(self, spec):
        """A Read-area capture with no document: items in screen points, the PNG at backing scale 2 (capture px = 2 x pt).
        GT blocks are in capture pixels; line_bounds are the app's OCR line-bounds records (screen-point xywh)."""
        w, h = spec["size"]; k = 2.0; im = Image.new("RGB", (int(w * k), int(h * k)), tuple(spec.get("bg", (255, 255, 255)))); p = PX(im, k)
        pg = {"n": 0, "w": w, "h": h, "lines": [], "blocks": [], "prose_decoys": [], "_text": []}; self.text_layer = True
        for it in spec["items"]: self.item(p, pg, it, True)
        ox, oy = spec.get("origin", [120, 140]); im.save(os.path.join(self.out, f"area-{spec['id']}.png"), format="PNG", optimize=False)
        ocr, loc = [], 0
        order = sorted(range(len(pg["lines"])), key=lambda i: (round((pg["lines"][i][1] + pg["lines"][i][3]) / 2 / 3), pg["lines"][i][0]))
        for i in order:
            r, t = pg["lines"][i], pg["_text"][i]
            ocr.append({"loc": loc, "abs": loc, "len": len(t), "rect": [r2(ox + r[0]), r2(oy + r[1]), r2(r[2] - r[0]), r2(r[3] - r[1])], "_text": t})
            loc += len(t) + 1
        blocks = []
        for n, b in enumerate(pg["blocks"]):
            e = {"id": f"{spec['id']}b{n + 1}"}; e.update({x: v for x, v in b.items() if x not in ("rows",) and not x.startswith("_") and v is not None})
            e["bbox"] = rect(*[v * k for v in b["bbox"]])
            if b["kind"] in ("figure", "table"): e.update(gap="no-text", raster=True)
            blocks.append(e)
        self.areas.append({"id": spec["id"], "w": int(w * k), "h": int(h * k), "kind_scored": True, "blocks": blocks})
        sids = []
        for st in spec["script"]:
            sid = f"s{len(self.sentences) + 1}"; sids.append(sid)
            self.sentences.append({"id": sid, "page": None, "text_hash": thash(st), "score": "none", "line_rects": [], "_text": st, "_owner": spec["id"]})
        return {"case_id": f"{self.S['doc_id']}/{spec['id']}", "doc_id": self.S["doc_id"], "page": None, "area": spec["id"], "capture": f"area-{spec['id']}.png",
                "capture_rect": [ox, oy, w, h], "line_bounds": [{x: v for x, v in o.items() if x != "_text"} for o in ocr],
                "selection": [{"text": o["_text"]} for o in ocr], "script": [{"sentence_id": sid, "text": st} for sid, st in zip(sids, spec["script"])]}

    def cases(self):
        out = []; doc = self.S["doc_id"]; sents = {s["id"]: s for s in self.sentences}
        for cs in self.S.get("cases", []):
            sel = cs["select"]
            if sel.endswith(":caption"):
                key = sel.split(":")[0]; it = self.items[key]; rects = it["caption_lines"]; sids = it["caption_sids"]; pg = it["page"]
            else:
                pa = self.paras[sel]; rects = pa["lines"]; sids = pa["sids"]; pg = pa["page"]
            pobj = self.pages[pg - 1]
            selection = [{"text": pobj["_text"][pobj["lines"].index(r)], "rect": r} for r in rects]
            if "script" in cs:
                script = []
                for s in cs["script"]:
                    sid = f"s{len(self.sentences) + 1}"
                    g = {"id": sid, "page": pg, "text_hash": thash(s["text"]), "score": s.get("score", "none"), "line_rects": []}
                    if "row" in s:
                        rows = self.items[key]["rows"]; g.update(score="table_row", row_rect=rows[s["row"]], table=self.items[key]["block"]["_id"])
                    g["_text"] = s["text"]; self.sentences.append(g); script.append({"sentence_id": sid, "text": s["text"]})
            else:
                script = [{"sentence_id": sid, "text": sents[sid]["_text"]} for sid in sids if all(r in rects for r in sents[sid]["line_rects"])]
                if not script: raise SystemExit(f"{doc}/{cs['id']}: selection holds no whole sentence")
            out.append({"case_id": f"{doc}/{cs['id']}", "doc_id": doc, "page": pg, "selection": selection, "script": script})
        return out

    def build(self):
        for i, ps in enumerate(self.S["pages"]): self.page(i + 1, ps)
        self.c.save()
        for p in self.pages:
            for k, b in enumerate(p["blocks"]): b["_id"] = f"p{p['n']}b{k + 1}"
        cases = self.cases()
        cases += [self.area(a) for a in self.S.get("areas", [])]
        return cases

    def gt(self):
        pages = []
        for p in self.pages:
            blocks = [dict({"id": b["_id"]}, **{k: v for k, v in b.items() if not k.startswith("_") and v is not None}) for b in p["blocks"]]
            pages.append({"n": p["n"], "w": p["w"], "h": p["h"], "blocks_scored": p["blocks_scored"], "kind_scored": True, "scan": p["scan"],
                          "lines": p["lines"], "blocks": blocks, "prose_decoys": p["prose_decoys"]})
        return {"schema": "speakeasy-bench-gt/1", "family": "gap", "doc_id": self.S["doc_id"], "subset": "synthetic", "gt_source": "generator",
                "gt_method": "reportlab / Pillow drawing coordinates (gen/gap_synth.py); scanned pages: rotation applied to the drawn rects",
                "reviewed_by_human": False, "license": "synthetic", "pages": pages, "areas": self.areas,
                "sentences": [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.sentences], "parts": [], "features": {}}


class Translated:
    """Painter wrapper shifting page coordinates into a sub-image (raster tables, text images)."""
    def __init__(self, p, dx, dy): self.p, self.dx, self.dy = p, dx, dy
    def _r(self, r): return [r[0] + self.dx, r[1] + self.dy, r[2] + self.dx, r[3] + self.dy]
    def _back(self, r): return rect(r[0] - self.dx, r[1] - self.dy, r[2] - self.dx, r[3] - self.dy)
    def rect(self, r, **kw): return self._back(self.p.rect(self._r(r), **kw))
    def line(self, x0, y0, x1, y1, color=INK, lw=0.8): return self._back(self.p.line(x0 + self.dx, y0 + self.dy, x1 + self.dx, y1 + self.dy, color, lw))
    def text(self, x, ytop, s, size, color=INK, invisible=False): return self._back(self.p.text(x + self.dx, ytop + self.dy, s, size, color, invisible))


def build(spec_path, out):
    spec = json.load(open(spec_path, encoding="utf-8")); os.makedirs(out, exist_ok=True)
    for f in os.listdir(out):
        if f.startswith("area-") and f.endswith(".png"): os.remove(os.path.join(out, f))
    g = Gen(spec, out); cases = g.build(); gt = g.gt()
    with open(os.path.join(out, "gt.json"), "w", encoding="utf-8") as f: json.dump(gt, f, indent=1); f.write("\n")
    with open(os.path.join(out, "cases.json"), "w", encoding="utf-8") as f: json.dump(cases, f, indent=1); f.write("\n")
    return gt, cases


if __name__ == "__main__":
    if len(sys.argv) != 3: raise SystemExit("usage: gap_synth.py SPEC.json OUTDIR")
    gt, cases = build(sys.argv[1], sys.argv[2])
    print(json.dumps({"doc_id": gt["doc_id"], "pages": len(gt["pages"]), "cases": len(cases), "areas": len(gt["areas"])}))
