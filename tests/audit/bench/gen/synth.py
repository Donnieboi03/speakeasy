#!/usr/bin/env python3
"""Spec-driven reportlab generator for the Phase 2 benchmark: synth.py SPEC.json OUTDIR -> doc.pdf, gt.json, cases.json.

Ground truth comes from the generator's own drawing coordinates (page space, origin top-left), never from parsing the PDF.
A line rect is x from the string origin to origin + stringWidth and y from baseline - ascent to baseline + |descent|.
Canvas(invariant=1), so the same spec gives byte-identical files. All document text lives in the spec.

Spec keys (defaults in DEFAULTS): doc_id, page [w, h], margins [l, t, r, b], columns, gutter, font, bold, size, leading,
para_gap, caption_size, caption_leading, hyphenate, ligatures, header, footer, page_numbers, flow, cases.
flow items:
  {"heading": "2 Method"}
  {"para": "p3", "s": ["Sentence one.", ...]}
  {"break": "column" | "page"}
  {"figure": "F2", "label": "Figure 2", "span": "column" | "full", "h": 170, "frame": true, "panel_dir": "row" | "col",
   "shape": "rect" | "round" | "ellipse", "caption": ["Figure 2: Title.", "More caption."],
   "panels": [{"label": "a", "label_style": "a)" | "(a)" | "none", "cols": 2,
               "parts": [{"name": "Pump", "style": "inside" | "adjacent" | "callout" | "raster", "n": 1}],
               "bars": [3, 5, 2]}]}
  {"table": "T1", "label": "Table 1", "span": "column" | "full", "caption": ["Table 1: ..."], "rows": [[...], ...],
   "grid": false, "w_frac": 1.0}
cases: [{"id": "c01", "select": "p3" | "F2:caption" | "T1:caption", "sentences": [i, j], "page": n,
         "script": [{"text": "...", "part": "a:Pump" | "Pump" | "callout 2"} | {"text": "...", "score": "none"}]}]
Without "script" a case speaks the selected sentences (read-through)."""
import hashlib, json, math, os, re, sys
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(os.path.dirname(HERE), "fonts")
DEFAULTS = {"page": [595.3, 841.9], "margins": [56, 64, 56, 64], "columns": 1, "gutter": 18, "font": "Helvetica", "bold": "Helvetica-Bold",
            "size": 10, "leading": 13, "para_gap": 5, "heading_size": 12, "caption_size": 9, "caption_leading": 11, "hyphenate": False,
            "ligatures": False, "header": None, "footer": None, "page_numbers": False, "fig_gap": 12, "cases": []}
LIG = [("ffi", "\ufb03"), ("ffl", "\ufb04"), ("ff", "\ufb00"), ("fi", "\ufb01"), ("fl", "\ufb02")]
REF = re.compile(r"\b(Fig\.|Figure|Table)\s*\d")
BITMAP = {  # 5x7 glyphs for raster labels (pixels, not text objects)
    "A": "01110100011000111111100011000110001", "B": "11110100011000111110100011000111110", "C": "01110100011000010000100001000101110",
    "D": "11110100011000110001100011000111110", "E": "11111100001000011110100001000011111", "F": "11111100001000011110100001000010000",
    "G": "01110100011000010111100011000101111", "H": "10001100011000111111100011000110001", "I": "01110001000010000100001000010001110",
    "J": "00111000100001000010000101001001100", "K": "10001100101010011000101001001010001", "L": "10000100001000010000100001000011111",
    "M": "10001110111010110101100011000110001", "N": "10001100011100110101100111000110001", "O": "01110100011000110001100011000101110",
    "P": "11110100011000111110100001000010000", "Q": "01110100011000110001101011001001101", "R": "11110100011000111110101001001010001",
    "S": "01111100001000001110000010000111110", "T": "11111001000010000100001000010000100", "U": "10001100011000110001100011000101110",
    "V": "10001100011000110001100010101000100", "W": "10001100011000110101101011010101010", "X": "10001100010101000100010101000110001",
    "Y": "10001100011000101010001000010000100", "Z": "11111000010001000100010001000011111", "0": "01110100011001110101110011000101110",
    "1": "00100011000010000100001000010001110", "2": "01110100010000100010001000100011111", "3": "11111000100010000010000011000101110",
    "4": "00010001100101010010111110001000010", "5": "11111100001111000001000011000101110", "6": "00110010001000011110100011000101110",
    "7": "11111000010001000100010000100001000", "8": "01110100011000101110100011000101110", "9": "01110100011000101111000010001001100",
    " ": "0" * 35, "-": "00000000000000011111000000000000000"}


def r2(v): return round(v + 0.0, 2)
def rect(x0, y0, x1, y1): return [r2(x0), r2(y0), r2(x1), r2(y1)]
def union(rs):
    rs = [r for r in rs if r]
    return rect(min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)) if rs else None
def thash(s): return hashlib.sha256(re.sub(r"\s+", " ", s).strip().encode("utf-8")).hexdigest()[:12]


class Gen:
    def __init__(self, spec, out):
        self.S = dict(DEFAULTS, **spec); S = self.S
        self.W, self.H = S["page"]; self.ml, self.mt, self.mr, self.mb = S["margins"]
        self.ncol = S["columns"]; self.cw = (self.W - self.ml - self.mr - (self.ncol - 1) * S["gutter"]) / self.ncol
        self.tw = self.W - self.ml - self.mr
        for f in {S["font"], S["bold"]}:
            if f.startswith("DejaVu") and f not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(f, os.path.join(FONT_DIR, f + ".ttf")))
        self.c = canvas.Canvas(os.path.join(out, "doc.pdf"), pagesize=(self.W, self.H), invariant=1)
        self.pages = []; self.page = None; self.col = 0; self.y = 0; self.band_top = 0
        self.sentences = []; self.parts = []; self.paras = {}; self.items = {}
        self.stats = {"hyphen_breaks": 0, "ligatures": 0, "headings": 0, "col_continuation": False}

    # ------------------------------------------------------------ primitives
    def lig(self, s):
        if not self.S["ligatures"]: return s
        for a, b in LIG: s = s.replace(a, b)
        return s

    def sw(self, s, font=None, size=None): return pdfmetrics.stringWidth(s, font or self.S["font"], size or self.S["size"])

    def text(self, x, ytop, s, font, size, body=True, kind=None):
        """Draws s with its line box top at ytop. Returns the GT line rect; body lines go into pages[].lines."""
        asc, desc = pdfmetrics.getAscentDescent(font, size)
        base = ytop + asc
        self.c.setFont(font, size); self.c.drawString(x, self.H - base, s)
        self.stats["ligatures"] += sum(s.count(b) for _, b in LIG)
        r = rect(x, base - asc, x + pdfmetrics.stringWidth(s, font, size), base - desc)
        if body: self.page["lines"].append(r); self.page["_text"].append(s)
        if kind: self.page["blocks"].append({"kind": kind, "bbox": r})
        return r

    def new_page(self):
        if self.page is not None: self.c.showPage()
        n = len(self.pages) + 1
        self.page = {"n": n, "w": r2(self.W), "h": r2(self.H), "blocks_scored": True, "lines": [], "blocks": [], "_text": []}
        self.pages.append(self.page)
        S = self.S
        if S["header"]: self.text(self.ml, self.mt - 30, S["header"], S["font"], 8, kind="header")
        if S["footer"]: self.text(self.ml, self.H - self.mb + 18, S["footer"], S["font"], 8, kind="footer")
        if S["page_numbers"]:
            s = str(n); self.text(self.W - self.mr - self.sw(s, size=8), self.H - self.mb + 18, s, S["font"], 8, kind="page_number")
        self.col = 0; self.y = self.mt; self.band_top = self.mt

    def colx(self): return self.ml + self.col * (self.cw + self.S["gutter"])

    def next_col(self):
        if self.col < self.ncol - 1: self.col += 1; self.y = self.band_top
        else: self.new_page()

    def ensure(self, h):
        if self.y + h > self.H - self.mb:
            self.next_col()
            if self.y + h > self.H - self.mb: raise SystemExit(f"{self.S['doc_id']}: item of height {h:.0f} does not fit a fresh column")

    # ------------------------------------------------------------ text flow
    def wrap(self, text, width, font, size, hyphenate):
        toks = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        lines, cur, curw, i, pend = [], [], 0.0, 0, None
        space = pdfmetrics.stringWidth(" ", font, size)
        def w(a, b, hy=False): return pdfmetrics.stringWidth(self.lig(text[a:b]) + ("-" if hy else ""), font, size)
        while i < len(toks):
            s, e = (pend, toks[i][1]) if pend is not None else toks[i]
            sp = space if cur else 0.0
            if curw + sp + w(s, e) <= width:
                cur.append((s, e, False)); curw += sp + w(s, e); i += 1; pend = None; continue
            alpha = re.match(r"[A-Za-z]+", text[s:e]); split = None
            if hyphenate and cur and alpha and alpha.end() >= 7:
                for k in range(alpha.end() - 3, 2, -1):
                    if curw + sp + w(s, s + k, True) <= width: split = k; break
            if split:
                cur.append((s, s + split, True)); lines.append(cur); cur, curw, pend = [], 0.0, s + split
                self.stats["hyphen_breaks"] += 1; continue
            if not cur:
                cur.append((s, e, False)); i += 1; pend = None; lines.append(cur); cur, curw = [], 0.0; continue
            lines.append(cur); cur, curw = [], 0.0
        if cur: lines.append(cur)
        return [(" ".join(self.lig(text[a:b]) + ("-" if hy else "") for a, b, hy in ln), ln[0][0], ln[-1][1]) for ln in lines]

    def add_sentences(self, owner, sents, placed):
        """placed: [(page_n, rect, char_start, char_end)] for the owner's text = ' '.join(sents)."""
        pos = 0; ids = []
        for st in sents:
            a, b = pos, pos + len(st); pos = b + 1
            hold = [(pn, r) for pn, r, s0, e0 in placed if s0 < b and e0 > a]
            pg = hold[0][0]
            sid = f"s{len(self.sentences) + 1}"
            score = "lines"
            if REF.search(st) or any(pn != pg for pn, _ in hold): score = "none"
            self.sentences.append({"id": sid, "page": pg, "text_hash": thash(st), "score": score, "line_rects": [r for pn, r in hold if pn == pg],
                                   "_text": st, "_owner": owner})
            ids.append(sid)
        return ids

    def para(self, item):
        S = self.S; text = " ".join(item["s"])
        lines = self.wrap(text, self.cw, S["font"], S["size"], S["hyphenate"])
        placed, groups = [], {}
        for s, a, b in lines:
            self.ensure(S["leading"])
            r = self.text(self.colx(), self.y, s, S["font"], S["size"]); self.y += S["leading"]
            placed.append((self.page["n"], r, a, b)); groups.setdefault((self.page["n"], self.col), []).append(r)
        self.y += S["para_gap"]
        cols_on_page = {}
        for (pn, col) in groups: cols_on_page.setdefault(pn, set()).add(col)
        if any(len(v) > 1 for v in cols_on_page.values()): self.stats["col_continuation"] = True
        for (pn, col), rs in groups.items():
            self.pages[pn - 1]["blocks"].append({"kind": "paragraph", "bbox": union(rs), "_para": item["para"]})
        self.paras[item["para"]] = {"lines": [(pn, r) for pn, r, _, _ in placed], "sids": self.add_sentences(item["para"], item["s"], placed)}

    def heading(self, item):
        S = self.S; size = S["heading_size"]
        self.ensure(size * 1.4 + 6 + S["leading"] * 2)
        self.y += 4
        self.text(self.colx(), self.y, item["heading"], S["bold"], size, kind="heading"); self.y += size * 1.4 + 2
        self.stats["headings"] += 1

    def caption(self, item, x, y, width):
        S = self.S; text = " ".join(item["caption"])
        lines = self.wrap(text, width, S["font"], S["caption_size"], False)
        placed = []
        for s, a, b in lines:
            r = self.text(x, y, s, S["font"], S["caption_size"]); y += S["caption_leading"]; placed.append((self.page["n"], r, a, b))
        self.page["blocks"].append({"kind": "caption", "bbox": union([p[1] for p in placed]), "label": item["label"]})
        key = item.get("figure") or item.get("table")
        self.items[key]["caption_lines"] = [(pn, r) for pn, r, _, _ in placed]
        self.items[key]["caption_sids"] = self.add_sentences(key + ":caption", item["caption"], placed)
        return y

    def cap_height(self, item, width):
        n = len(self.wrap(" ".join(item["caption"]), width, self.S["font"], self.S["caption_size"], False))
        return n * self.S["caption_leading"]

    def place_block(self, item, body_h, caption_above):
        full = item.get("span") == "full" and self.ncol > 1
        width = self.tw if (item.get("span") == "full") else self.cw
        total = body_h + 6 + self.cap_height(item, width)
        if full:
            if not (self.col == 0 and abs(self.y - self.band_top) < 0.01): self.new_page()
            x0, y0 = self.ml, self.y
            if y0 + total > self.H - self.mb: raise SystemExit(f"{self.S['doc_id']}: full-width item does not fit")
        else:
            self.ensure(total); x0, y0 = self.colx(), self.y
        return full, width, total, x0, y0

    # ------------------------------------------------------------ figures
    def shape(self, kind, x0, y0, x1, y1, fill=None):
        c = self.c; c.setLineWidth(0.8); c.setStrokeColorRGB(0.15, 0.15, 0.2)
        if fill: c.setFillColorRGB(*fill)
        f = 1 if fill else 0
        if kind == "ellipse": c.ellipse(x0, self.H - y1, x1, self.H - y0, stroke=1, fill=f)
        elif kind == "round": c.roundRect(x0, self.H - y1, x1 - x0, y1 - y0, 5, stroke=1, fill=f)
        else: c.rect(x0, self.H - y1, x1 - x0, y1 - y0, stroke=1, fill=f)
        c.setFillColorRGB(0, 0, 0)
        return rect(x0, y0, x1, y1)

    def fit_size(self, s, width, size=8.0):
        while size > 5 and pdfmetrics.stringWidth(s, self.S["font"], size) > width: size -= 0.5
        return size

    def raster(self, x0, y0, x1, y1, name):
        from PIL import Image, ImageDraw
        k = 3; iw, ih = max(8, int(round((x1 - x0) * k))), max(8, int(round((y1 - y0) * k)))
        im = Image.new("RGB", (iw, ih), (226, 236, 248)); d = ImageDraw.Draw(im)
        d.rectangle([2, 2, iw - 3, ih - 3], outline=(40, 70, 120), width=3)
        label = name.upper(); px = max(1, min((iw - 16) // max(1, 6 * len(label)), (ih - 12) // 9))
        tx = (iw - 6 * px * len(label)) // 2; ty = (ih - 7 * px) // 2
        for ci, ch in enumerate(label):
            g = BITMAP.get(ch, BITMAP[" "])
            for j, bit in enumerate(g):
                if bit == "1":
                    gx = tx + ci * 6 * px + (j % 5) * px; gy = ty + (j // 5) * px
                    d.rectangle([gx, gy, gx + px - 1, gy + px - 1], fill=(20, 30, 60))
        self.c.drawImage(ImageReader(im), x0, self.H - y1, x1 - x0, y1 - y0)
        return rect(x0, y0, x1, y1)

    def panel(self, fig, p, x0, y0, x1, y1, pieces):
        S = self.S; style = p.get("label_style", "a)"); top = y0 + 4
        if style != "none":
            lab = f"({p['label']})" if style.startswith("(") else f"{p['label']})"
            pieces.append(self.text(x0 + 4, y0 + 3, lab, S["bold"], 9, body=False)); top = y0 + 16
        if p.get("bars"):
            bx0, by0, bx1, by1 = x0 + 26, top + 4, x1 - 8, y1 - 16
            self.c.setLineWidth(0.8); self.c.line(bx0, self.H - by1, bx1, self.H - by1); self.c.line(bx0, self.H - by0, bx0, self.H - by1)
            pieces += [rect(bx0, by1, bx1, by1), rect(bx0, by0, bx0, by1)]
            n = len(p["bars"]); bw = (bx1 - bx0) / (n * 1.6); mx = max(p["bars"])
            for i, v in enumerate(p["bars"]):
                bx = bx0 + bw * 0.6 + i * bw * 1.6; bh = (by1 - by0 - 4) * v / mx
                pieces.append(self.shape("rect", bx, by1 - bh, bx + bw, by1, fill=(0.35, 0.5, 0.75)))
                pieces.append(self.text(bx, by1 + 2, str(i + 1), S["font"], 7, body=False))
            for t in (0, mx):
                pieces.append(self.text(x0 + 6, by1 - (by1 - by0 - 4) * t / mx - 4, str(t), S["font"], 7, body=False))
            return
        parts = p.get("parts", [])
        if not parts: return
        cols = p.get("cols", min(len(parts), 3)); rows = math.ceil(len(parts) / cols)
        cw = (x1 - x0 - 8) / cols; ch = (y1 - top - 4) / rows
        for i, pt in enumerate(parts):
            st = pt.get("style", "inside")
            gh = {"adjacent": min(ch - 16, 44) + 12, "callout": min(ch - 4, 52), "raster": min(ch - 4, 48)}.get(st, min(ch - 4, 46))
            cx0 = x0 + 4 + (i % cols) * cw; cy0 = top + (i // cols) * ch + max(0.0, (ch - gh - 4) / 2)
            sx0, sx1 = cx0 + 6, cx0 + cw - 6
            shp = fig.get("shape", "rect")
            if st == "adjacent":
                sy0 = cy0 + 4; sy1 = cy0 + min(ch - 16, 44)
                bb = self.shape(shp, sx0, sy0, sx1, sy1, fill=(0.93, 0.95, 0.9)); pieces.append(bb)
                size = self.fit_size(pt["name"], sx1 - sx0 + 8)
                lw = pdfmetrics.stringWidth(pt["name"], S["font"], size)
                pieces.append(self.text((sx0 + sx1) / 2 - lw / 2, sy1 + 2, pt["name"], S["font"], size, body=False))
                name = pt["name"]
            elif st == "callout":
                sy0 = cy0 + 12; sy1 = cy0 + min(ch - 4, 52)
                sx1c = sx1 - 14
                bb = self.shape(shp, sx0, sy0, sx1c, sy1, fill=(0.8, 0.82, 0.86)); pieces.append(bb)
                ccx, ccy, rad = sx1 - 6, cy0 + 6, 5.5
                self.c.setLineWidth(0.6); self.c.line(ccx - rad * 0.7, self.H - (ccy + rad * 0.7), sx1c - 2, self.H - (sy0 + 4))
                self.c.circle(ccx, self.H - ccy, rad, stroke=1, fill=0)
                pieces.append(rect(ccx - rad, ccy - rad, ccx + rad, ccy + rad)); pieces.append(rect(sx1c - 2, ccy, ccx, sy0 + 4))
                num = str(pt["n"]); nw = pdfmetrics.stringWidth(num, S["bold"], 7)
                pieces.append(self.text(ccx - nw / 2, ccy - 3.6, num, S["bold"], 7, body=False))
                name = f"callout {pt['n']}"
            elif st == "raster":
                sy0 = cy0 + 4; sy1 = cy0 + min(ch - 4, 48)
                bb = self.raster(sx0, sy0, sx1, sy1, pt["name"]); pieces.append(bb); name = pt["name"]
            else:
                sy0 = cy0 + 4; sy1 = cy0 + min(ch - 4, 46)
                bb = self.shape(shp, sx0, sy0, sx1, sy1, fill=(0.9, 0.93, 0.98)); pieces.append(bb)
                size = self.fit_size(pt["name"], (sx1 - sx0) * (0.7 if shp == "ellipse" else 0.9))
                lw = pdfmetrics.stringWidth(pt["name"], S["font"], size)
                pieces.append(self.text((sx0 + sx1) / 2 - lw / 2, (sy0 + sy1) / 2 - size * 0.55, pt["name"], S["font"], size, body=False))
                name = pt["name"]
            self.parts.append({"id": f"pt{len(self.parts) + 1}", "name": name, "figure": fig["label"], "panel": p.get("label"),
                               "page": self.page["n"], "bbox": bb, "style": st, "snapped": True, "_fig": fig["figure"]})

    def figure(self, item):
        self.items[item["figure"]] = {"kind": "figure", "item": item}
        full, width, total, x0, y0 = self.place_block(item, item["h"], False)
        x1, y1 = x0 + width, y0 + item["h"]; pieces = []
        if item.get("frame", True):
            self.c.setLineWidth(0.6); self.c.rect(x0, self.H - y1, x1 - x0, y1 - y0); pieces.append(rect(x0, y0, x1, y1))
        panels = item.get("panels", []); n = max(1, len(panels)); gap = 8
        for i, p in enumerate(panels):
            if item.get("panel_dir", "row") == "row":
                pw = (width - 8 - gap * (n - 1)) / n; px0 = x0 + 4 + i * (pw + gap); box = (px0, y0 + 4, px0 + pw, y1 - 4)
            else:
                ph = (item["h"] - 8 - gap * (n - 1)) / n; py0 = y0 + 4 + i * (ph + gap); box = (x0 + 4, py0, x1 - 4, py0 + ph)
            self.panel(item, p, *box, pieces)
        self.page["blocks"].append({"kind": "figure", "bbox": union(pieces), "label": item["label"]})
        self.items[item["figure"]].update(page=self.page["n"], bbox=union(pieces), full=full, width=width)
        self.caption(item, x0, y1 + 6, width)
        if full: self.band_top = y0 + total + self.S["fig_gap"]; self.y = self.band_top
        else: self.y = y0 + total + self.S["fig_gap"]

    def table(self, item):
        S = self.S; self.items[item["table"]] = {"kind": "table", "item": item}
        rows = item["rows"]; nr = len(rows); rh = 12.0; body_h = nr * rh + 8
        full, width, total, x0, y0 = self.place_block(item, body_h, True)
        y = self.caption(item, x0, y0, width) + 4
        tw = width * item.get("w_frac", 1.0); ncols = max(len(r) for r in rows)
        need = [max(pdfmetrics.stringWidth(r[j] if j < len(r) else "", S["font"], 8) for r in rows) + 10 for j in range(ncols)]
        scale = tw / sum(need); cws = [v * scale for v in need]
        pieces = []; c = self.c; c.setLineWidth(0.7)
        def hline(yy, lw=0.7):
            c.setLineWidth(lw); c.line(x0, self.H - yy, x0 + tw, self.H - yy); pieces.append(rect(x0, yy, x0 + tw, yy))
        hline(y, 1.0); yy = y + 2
        for i, r in enumerate(rows):
            cx = x0
            for j, cell in enumerate(r):
                if cell: pieces.append(self.text(cx + 3, yy + 1, cell, S["bold"] if i == 0 else S["font"], 8, body=False))
                cx += cws[j]
            yy += rh
            if i == 0 or (item.get("grid") and i < nr - 1): hline(yy - 1)
        hline(yy + 2, 1.0)
        if item.get("grid"):
            cx = x0
            for j in range(ncols + 1):
                c.line(cx, self.H - y, cx, self.H - (yy + 2)); pieces.append(rect(cx, y, cx, yy + 2)); cx += cws[j] if j < ncols else 0
        self.page["blocks"].append({"kind": "table", "bbox": union(pieces), "label": item["label"]})
        self.items[item["table"]].update(page=self.page["n"], bbox=union(pieces), full=full)
        if full: self.band_top = y0 + total + S["fig_gap"]; self.y = self.band_top
        else: self.y = y0 + total + S["fig_gap"]

    # ------------------------------------------------------------ build
    def build(self):
        self.new_page()
        for it in self.S["flow"]:
            if "heading" in it: self.heading(it)
            elif "para" in it: self.para(it)
            elif "figure" in it: self.figure(it)
            elif "table" in it: self.table(it)
            elif it.get("break") == "column": self.next_col()
            elif it.get("break") == "page": self.new_page()
            else: raise SystemExit(f"unknown flow item {it}")
        self.c.showPage(); self.c.save()

    def find_part(self, ref, fig):
        panel, _, name = ref.rpartition(":") if ":" in ref else (None, None, ref)
        hits = [p for p in self.parts if p["_fig"] == fig and p["name"] == name and (panel is None or p["panel"] == panel)]
        if len(hits) != 1: raise SystemExit(f"{self.S['doc_id']}: part ref {ref!r} in {fig} matches {len(hits)} parts")
        return hits[0]

    def cases(self):
        out = []; doc = self.S["doc_id"]
        for cs in self.S["cases"]:
            sel = cs["select"]
            if sel.endswith(":caption"):
                key = sel.split(":")[0]; it = self.items[key]; lines = it["caption_lines"]; sids = it["caption_sids"]; fig = key
            else:
                pa = self.paras[sel]; lines = pa["lines"]; sids = pa["sids"]; fig = None
            sents = {s["id"]: s for s in self.sentences}
            if "sentences" in cs:
                i, j = cs["sentences"]; want = sids[i:j + 1]
                pg = sents[want[0]]["page"]
                first = sents[want[0]]["line_rects"][0]; last = sents[want[-1]]["line_rects"][-1]
                ln = [(pn, r) for pn, r in lines if pn == pg]
                a = next(k for k, (_, r) in enumerate(ln) if r == first); b = next(k for k, (_, r) in enumerate(ln) if r == last)
                ln = ln[a:b + 1]
            else:
                pg = cs.get("page", lines[0][0]); ln = [(pn, r) for pn, r in lines if pn == pg]
            rects = [r for _, r in ln]
            pobj = self.pages[pg - 1]
            selection = [{"text": pobj["_text"][pobj["lines"].index(r)], "rect": r} for r in rects]
            if "script" in cs:
                script = []
                for s in cs["script"]:
                    sid = f"s{len(self.sentences) + 1}"
                    if "part" in s:
                        p = self.find_part(s["part"], fig or cs.get("figure"))
                        g = {"id": sid, "page": p["page"], "text_hash": thash(s["text"]), "score": "parts", "line_rects": [], "part": p["id"]}
                    else:
                        g = {"id": sid, "page": pg, "text_hash": thash(s["text"]), "score": s.get("score", "none"), "line_rects": []}
                    g["_text"] = s["text"]; self.sentences.append(g); script.append({"sentence_id": sid, "text": s["text"]})
            else:
                script = [{"sentence_id": sid, "text": sents[sid]["_text"]} for sid in sids
                          if sents[sid]["page"] == pg and sents[sid]["line_rects"] and all(r in rects for r in sents[sid]["line_rects"])]
                if not script: raise SystemExit(f"{doc}/{cs['id']}: selection holds no whole sentence")
            out.append({"case_id": f"{doc}/{cs['id']}", "doc_id": doc, "page": pg, "selection": selection, "script": script})
        return out

    def features(self):
        f = dict(self.stats); f["pages"] = len(self.pages); f["columns"] = self.ncol
        f["footers"] = any(b["kind"] == "footer" for p in self.pages for b in p["blocks"])
        figs = [it for it in self.items.values() if it["kind"] == "figure"]; tabs = [it for it in self.items.values() if it["kind"] == "table"]
        callouts = sum(1 for p in self.parts if p["style"] == "callout")
        f["callouts"] = callouts
        f["panel_figure"] = any(len(it["item"].get("panels", [])) >= 2 and all(p.get("label_style", "a)") != "none" and
                                sum(1 for q in p.get("parts", []) if q.get("style", "inside") in ("inside", "adjacent")) >= 4 for p in it["item"]["panels"])
                                for it in figs) and callouts >= 3
        def above(t, g):
            return t["page"] == g["page"] and 0 <= g["bbox"][1] - t["bbox"][3] <= 40 and min(t["bbox"][2], g["bbox"][2]) > max(t["bbox"][0], g["bbox"][0])
        f["table_above_figure"] = any(above(t, g) for t in tabs for g in figs)
        f["standalone_table"] = any(not any(above(t, g) for g in figs) for t in tabs) and f["table_above_figure"]
        f["fullwidth_figure"] = self.ncol > 1 and any(it["full"] for it in figs)
        f["caption_named_panels"] = any(len(it["item"].get("panels", [])) >= 2 and all(p.get("label_style") == "none" for p in it["item"]["panels"]) for it in figs)
        f["raster_part"] = any(p["style"] == "raster" for p in self.parts)
        return f

    def gt(self):
        pages = []
        for p in self.pages:
            blocks = []
            for k, b in enumerate(p["blocks"]):
                e = {"id": f"p{p['n']}b{k + 1}", "kind": b["kind"], "bbox": b["bbox"]}
                if b.get("label"): e["label"] = b["label"]
                blocks.append(e)
            pages.append({"n": p["n"], "w": p["w"], "h": p["h"], "blocks_scored": True, "lines": p["lines"], "blocks": blocks})
        return {"schema": "speakeasy-bench-gt/1", "doc_id": self.S["doc_id"], "subset": "synthetic", "gt_source": "generator",
                "gt_method": "reportlab drawing coordinates (gen/synth.py); line y from font ascent/descent", "reviewed_by_human": False,
                "license": "synthetic", "pages": pages,
                "sentences": [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.sentences],
                "parts": [{k: v for k, v in p.items() if not k.startswith("_")} for p in self.parts], "features": self.features()}


def build(spec_path, out):
    spec = json.load(open(spec_path, encoding="utf-8")); os.makedirs(out, exist_ok=True)
    g = Gen(spec, out); g.build(); cases = g.cases(); gt = g.gt()
    with open(os.path.join(out, "gt.json"), "w", encoding="utf-8") as f: json.dump(gt, f, indent=1); f.write("\n")
    with open(os.path.join(out, "cases.json"), "w", encoding="utf-8") as f: json.dump(cases, f, indent=1); f.write("\n")
    return gt, cases


if __name__ == "__main__":
    if len(sys.argv) != 3: raise SystemExit("usage: synth.py SPEC.json OUTDIR")
    gt, cases = build(sys.argv[1], sys.argv[2])
    print(json.dumps({"doc_id": gt["doc_id"], "pages": len(gt["pages"]), "cases": len(cases), "features": gt["features"]}))
