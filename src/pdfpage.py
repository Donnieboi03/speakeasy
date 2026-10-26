"""Small PDF page layer on pypdfium2 (Apache-2.0/BSD; replaces PyMuPDF, which is AGPL).
Coordinates are PyMuPDF-style: points, origin top-left, rects [x0, y0, x1, y1]."""
import ctypes
import pypdfium2 as pdfium
import pypdfium2.raw as raw

class R(list):
    """[x0, y0, x1, y1] with the attribute names docmap uses."""
    x0 = property(lambda s: s[0]); y0 = property(lambda s: s[1]); x1 = property(lambda s: s[2]); y1 = property(lambda s: s[3])
    width = property(lambda s: s[2] - s[0]); height = property(lambda s: s[3] - s[1])

def open_pdf(path):
    return Doc(path)

class Doc:
    def __init__(self, path): self.pdf = pdfium.PdfDocument(path)
    def __len__(self): return len(self.pdf)
    def __getitem__(self, i): return Page(self.pdf[i])
    def __iter__(self): return (self[i] for i in range(len(self)))

class Page:
    def __init__(self, p):
        self.p = p; w, h = p.get_size(); self.rect = R([0, 0, w, h]); self.H = h
    def _flip(self, l, b, r, t): return R([l, self.H - t, r, self.H - b])
    def text_lines(self):
        """[(bbox, text)] per text line, in reading order, plus blocks (lists of line indexes)."""
        tp = self.p.get_textpage()
        segs = []
        for i in range(tp.count_rects()):
            l, b, r, t = tp.get_rect(i)
            s = tp.get_text_bounded(l, b, r, t).replace("\r", " ").replace("\n", " ").strip()
            if s: segs.append([self._flip(l, b, r, t), s])
        # merge segments that share a line band and follow each other horizontally (pdfium splits at font changes)
        lines = []
        for bb, s in segs:
            if lines:
                pb, ps = lines[-1]; hgt = max(1.0, min(bb.height, pb.height))
                ov = min(bb[3], pb[3]) - max(bb[1], pb[1])        # vertical overlap: sub/superscripts stay on their line
                if ov > 0.3 * hgt and -0.5 * hgt <= bb[0] - pb[2] < 0.6 * max(bb.height, pb.height) \
                        and max(bb.height, pb.height) < 2.5 * hgt:          # (rotated words / table cells stay apart)
                    gap = " " if bb[0] - pb[2] > 0.15 * hgt else ""
                    lines[-1] = [R([pb[0], min(pb[1], bb[1]), bb[2], max(pb[3], bb[3])]), ps + gap + s]; continue
            lines.append([bb, s])
        blocks, cur = [], []
        for k, (bb, s) in enumerate(lines):
            if cur:
                pb = lines[cur[-1]][0]; hgt = max(1.0, pb.height)
                if not (0 <= bb[1] - pb[3] < 0.8 * hgt and bb[0] < pb[2] and pb[0] < bb[2]): blocks.append(cur); cur = []
            cur.append(k)
        if cur: blocks.append(cur)
        return lines, blocks
    def _walk(self, kind):
        """(object, enclosing forms' matrix) for every object of this type, including ones nested in form XObjects."""
        stack = {}
        for o in self.p.get_objects(max_depth=6):
            parent = stack.get(o.level - 1, (1, 0, 0, 1, 0, 0))
            if o.type == raw.FPDF_PAGEOBJ_FORM:
                stack[o.level] = _compose(o.get_matrix().get(), parent); continue
            if o.type == kind: yield o, parent
    def _page_rect(self, m, l, bt, r, t):
        """Form-space bounds -> page rect (top-left origin) under matrix m."""
        pts = [_apply(m, x, y) for x, y in ((l, bt), (r, bt), (l, t), (r, t))]
        return self._flip(min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    def _objs(self, kind):
        """Bounds of every object of this type (form XObject bounds are in form space, so the forms' matrices are applied)."""
        out = []
        for o, parent in self._walk(kind):
            try: out.append(self._page_rect(parent, *o.get_bounds()))
            except pdfium.PdfiumError: continue            # object without bounds (empty path): skip it
        return out
    def images(self): return self._objs(raw.FPDF_PAGEOBJ_IMAGE)
    def drawings(self): return self._objs(raw.FPDF_PAGEOBJ_PATH)
    def paths(self):
        """Every vector path with its paint mode and segment count; Path.points() reads its segments on demand."""
        out = []
        for o, parent in self._walk(raw.FPDF_PAGEOBJ_PATH):
            try: r = self._page_rect(parent, *o.get_bounds())
            except pdfium.PdfiumError: continue
            fill, stroke = ctypes.c_int(0), ctypes.c_int(0)
            raw.FPDFPath_GetDrawMode(o.raw, ctypes.byref(fill), ctypes.byref(stroke))
            out.append(Path(self, o, parent, r, fill.value != raw.FPDF_FILLMODE_NONE, bool(stroke.value), raw.FPDFPath_CountSegments(o.raw)))
        return out
    def cluster_drawings(self, tol=3, on_page=False):
        """Union of vector paths that touch or nearly touch (like PyMuPDF's cluster_drawings). on_page: paths that stick out
        of the page by more than 2 pt (a figure canvas larger than its clip) are left out."""
        W, H = self.rect.width, self.rect.height
        rs = [list(r) for r in self.drawings() if not (r.width > 0.95 * W and r.height > 0.95 * H)
              and not (on_page and (r[0] < -2 or r[1] < -2 or r[2] > W + 2 or r[3] > H + 2))]
        merged = True
        while merged:
            merged = False; out = []
            for r in rs:
                for o in out:
                    if r[0] <= o[2] + tol and o[0] <= r[2] + tol and r[1] <= o[3] + tol and o[1] <= r[3] + tol:
                        o[:] = [min(o[0], r[0]), min(o[1], r[1]), max(o[2], r[2]), max(o[3], r[3])]; merged = True; break
                else: out.append(r)
            rs = out
        return [R(r) for r in rs]
    def image(self, clip, zoom):
        """PIL image of clip [x0,y0,x1,y1] (top-left origin) at zoom x."""
        W, H = self.rect.width, self.rect.height
        crop = (max(0, clip[0]), max(0, H - clip[3]), max(0, W - clip[2]), max(0, clip[1]))   # left, bottom, right, top margins
        return self.p.render(scale=zoom, crop=crop).to_pil().convert("RGB")
    def render(self, clip, zoom, png):
        self.image(clip, zoom).save(png)

def _compose(m, parent):
    """Matrix m applied inside parent (PDF row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m; a0, b0, c0, d0, e0, f0 = parent
    return (a1 * a0 + b1 * c0, a1 * b0 + b1 * d0, c1 * a0 + d1 * c0, c1 * b0 + d1 * d0, e1 * a0 + f1 * c0 + e0, e1 * b0 + f1 * d0 + f0)

def _apply(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]

class Path:
    """One vector path: page rect r, filled / stroked, n segments (the first is the move-to)."""
    def __init__(self, page, obj, parent, r, filled, stroked, n):
        self.page, self.obj, self.parent, self.r, self.filled, self.stroked, self.n = page, obj, parent, r, filled, stroked, n
    def points(self):
        """[(x, y, kind, closes)] in page space (top-left origin); kind is "move", "line" or "bezier"."""
        m = _compose(self.obj.get_matrix().get(), self.parent); out = []
        kinds = {raw.FPDF_SEGMENT_MOVETO: "move", raw.FPDF_SEGMENT_LINETO: "line", raw.FPDF_SEGMENT_BEZIERTO: "bezier"}
        for i in range(self.n):
            seg = raw.FPDFPath_GetPathSegment(self.obj.raw, i)
            x, y = ctypes.c_float(0), ctypes.c_float(0)
            raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
            px, py = _apply(m, x.value, y.value)
            out.append((px, self.page.H - py, kinds.get(raw.FPDFPathSegment_GetType(seg), "unknown"), bool(raw.FPDFPathSegment_GetClose(seg))))
        return out
