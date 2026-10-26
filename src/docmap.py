"""Document-aware focus: read the source PDF / Markdown file itself (read-only, local) to get exact
line, figure, caption and panel positions.

Two halves:
  * parse_pdf()  needs PyMuPDF (installed in the Kokoro venv); speakeasy runs it as a subprocess:
        <venv>/bin/python docmap.py parse-pdf FILE OUT.json
    The parse is cached by (path, size, mtime) under ~/.speakeasy/cache/doc/.
  * everything else is stdlib, imported by speakeasy.py (resolution, Markdown, page matching, figure refs, transforms).
Paths are never logged; only a short hash.
"""
import hashlib, json, os, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from collections import Counter

# captions need a separator after the number ("Figure 3:", "Fig. 3.", "Table 2 |"), so prose like "Table 2 summarizes" is not one
CAP_RE = re.compile(r"^\s*(?:fig(?:ure)?s?\.?)\s*(\d+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(.*)$", re.I)
TAB_RE = re.compile(r"^\s*table\s*(\d+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(.*)$", re.I)
PANEL_RE = re.compile(r"^\s*\(?([a-hA-H])\)\s*$|^\s*\(([a-h])\)\s+\S|^\s*([a-h])\s*$")
REF_RE = re.compile(r"\b(fig(?:ure)?s?\.?|table)\s*(\d+)\s*(?:\(?([a-h])\)?(?![a-z]))?", re.I)
PANEL_REF_RE = re.compile(r"\b(?:panel|subfigure|part)\s*\(?([a-h])\)?(?![a-z])", re.I)
PAGE_OF_RE = re.compile(r"(?:page\s*)?(\d{1,4})\s*(?:of|/)\s*(\d{1,4})\b", re.I)

def path_hash(p):
    return hashlib.sha256((p or "").encode()).hexdigest()[:12] if p else None

def cache_dir():
    d = os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "cache", "doc")
    os.makedirs(d, exist_ok=True)
    return d

PARSE_VERSION = 8          # bump when parse_pdf output changes, so cached parses are rebuilt

def _key(path):
    st = os.stat(path)
    return hashlib.sha256(f"{os.path.realpath(path)}|{st.st_size}|{int(st.st_mtime)}|v{PARSE_VERSION}".encode()).hexdigest()[:20]

def _split_overlaps(figs):
    """A table's body grows over graphics below its caption; when that swallows a figure on the same page
    (Table 1 directly above Fig. 5), the table ends where the figure starts, and the figure keeps its own box."""
    for t in figs:
        if t.get("kind") != "table": continue
        for f in figs:
            if f.get("kind") != "figure": continue
            a, b = t["bbox"], f["bbox"]
            ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
            fa = max(1.0, (b[2] - b[0]) * (b[3] - b[1])); ta = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
            if ix * iy < 0.3 * fa:
                # the reverse: a figure body that runs over a table on the same page stops at the table's edge
                if ix * iy >= 0.3 * ta and not _inside(f["caption_bbox"], a):
                    if (a[1] + a[3]) / 2 < (b[1] + b[3]) / 2: b[1] = min(b[3] - 1, a[3] + 2)
                    else: b[3] = max(b[1] + 1, a[1] - 2)
                    f["bbox"] = [round(v, 1) for v in b]; f["split_from"] = t["label"]
                    f["labels"] = [l for l in f.get("labels", []) if _inside(l[:4], f["bbox"])]
                    f["objects"] = [o for o in f.get("objects", []) if _inside(o[:4], f["bbox"])]
                continue
            cap = t["caption_bbox"]
            # a body that is only the swallowed figure leaves the table the space between its caption and the figure
            if b[1] >= cap[3] - 2:                                   # figure below the table caption: cut the table there
                if a[1] >= b[1] - 2: a[1] = cap[3]
                a[3] = max(a[1] + 1, b[1] - 2)
            elif b[3] <= cap[1] + 2:                                 # figure above it: the table starts below the figure
                a[1] = cap[3] if a[3] > cap[3] else min(a[3] - 1, b[3] + 2)
            t["bbox"] = [round(v, 1) for v in a]
            t["labels"] = [l for l in t.get("labels", []) if _inside(l[:4], t["bbox"])]
            t["split_from"] = f["label"]
    return figs

def _toks(s):
    from textfold import match_tokens                     # the shared fold's match tokens: digits kept, no stopwords
    return match_tokens(s)

# ---------------- PDF (PyMuPDF; runs in the venv) ----------------
def _union(rs):
    if not rs: return None
    return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]

def _panels(fig, labels):
    """Split a figure box at its (a)/(b)/... labels: rows by label y, columns by label x."""
    if len(labels) < 2: return []
    out = []
    for lb in labels:
        x0, y0 = lb["r"][0], lb["r"][1]
        same_row = [o for o in labels if o is not lb and abs(o["r"][1] - y0) < 12]
        right = [o["r"][0] for o in same_row if o["r"][0] > x0 + 5]
        below = [o["r"][1] for o in labels if o["r"][1] > y0 + 12 and o["r"][0] < x0 + (min(right) - x0 if right else 1e9) - 5]
        x1 = min(right) - 2 if right else fig[2]
        y1 = min(below) - 2 if below else fig[3]
        out.append({"label": lb["l"], "bbox": [round(x0 - 2, 1), round(y0 - 2, 1), round(x1, 1), round(y1, 1)], "src": "label", "guessed": False})
    return out

GENERIC = set("""a an the this that these those it its of in on at to for from with by and or as is are was were be been being
shows show shown showing depicts depicting illustrates displays display figure fig diagram image picture panel part left right top bottom
here there which where while what how also then next each both all some data result results overview setup scheme process involves
first second third finally using used use uses into onto over under between""".split())

def _text_lines(page):
    lines, blocks = page.text_lines()
    return [[*[round(v, 1) for v in bb], t] for bb, t in lines], blocks

def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def _inside(r, b, pad=2):
    return r[0] >= b[0] - pad and r[2] <= b[2] + pad and r[1] >= b[1] - pad and r[3] <= b[3] + pad

def _objects(page, fb):
    """Graphic sub-objects inside a figure: raster images and small drawing clusters (page space)."""
    area = max(1.0, (fb[2] - fb[0]) * (fb[3] - fb[1])); out = []
    try:
        for r in page.images():
            if len(r) == 4 and _inside(r, fb, 4) and (r[2] - r[0]) * (r[3] - r[1]) < 0.7 * area and r[2] - r[0] > 12 and r[3] - r[1] > 12:
                out.append([round(v, 1) for v in r] + ["image"])
    except (RuntimeError, ValueError, OSError): pass
    try:                                              # individual vector paths (boxes, plots, outlines)
        for r in page.drawings():
            rr = list(r)
            if _inside(rr, fb, 2) and r.width > 10 and r.height > 10 and 150 < r.width * r.height < 0.35 * area:
                out.append([round(v, 1) for v in rr] + ["drawing"])
    except Exception: pass
    out.sort(key=lambda o: -(o[2] - o[0]) * (o[3] - o[1]))
    return out[:400]

def _ocr_lines(page, fb, tmpdir=None):
    """Raster figures carry no PDF text: render the figure and read its text (labels, callout numbers) with the tracker's
    text recognition."""
    trk = tracker_bin()
    if not os.path.exists(trk): return []
    png = os.path.join(tmpdir or tempfile.gettempdir(), f"speakeasy_cap_fig_{os.getpid()}.png")
    try:
        page.render(fb, 3, png)
        r = subprocess.run([trk, "--ocr-lines", png, "--frame", ",".join(str(v) for v in [fb[0], fb[1], fb[2] - fb[0], fb[3] - fb[1]])],
                           capture_output=True, timeout=20)
        out = json.loads(r.stdout.decode() or "[]")
        return [[round(v, 1) for v in o["r"]] + [o["t"]] for o in out if o.get("t", "").strip()]
    except Exception:
        return []
    finally:
        try: os.remove(png)
        except OSError: pass

def _caption_panels(caption):
    """The panel letters a caption names ("(a) ... (b) ...", Nature style "a Subjects ... b Recorded"), or []."""
    cap = caption or ""
    ls = set(re.findall(r"\(([a-f])\)", cap))
    ls |= set(re.findall(r"(?:^|[.;|]\s+)([a-f])\s+(?=[A-Z])", cap))          # Nature style: "... scheme. a Subjects sit ... b Recorded"
    ls = [l for l in "abcdef" if l in ls]
    return ls if len(ls) >= 2 and ls[0] == "a" else []

def _guess_panels(fb, caption):
    """No drawn (a)/(b) labels but the caption names panels: split the figure evenly (side by side when wide)."""
    ls = _caption_panels(caption)
    n = len(ls); w, h = fb[2] - fb[0], fb[3] - fb[1]; out = []
    for k, l in enumerate(ls):
        if w >= h: r = [fb[0] + k * w / n, fb[1], fb[0] + (k + 1) * w / n, fb[3]]
        else: r = [fb[0], fb[1] + k * h / n, fb[2], fb[1] + (k + 1) * h / n]
        out.append({"label": l, "bbox": [round(v, 1) for v in r], "src": "caption", "guessed": True})
    return out

GUTTER_MIN, GUTTER_SPAN = 6.0, 0.9

def _gutters(fb, items, axis):
    """Cut positions (centres of clear bands) across the figure: a band >= GUTTER_MIN pt wide, away from the figure's edges,
    that no item crosses over at least GUTTER_SPAN of the other dimension. axis 0: vertical bands (x cuts), 1: horizontal."""
    a0, a1 = fb[axis], fb[axis + 2]; b0, b1 = fb[1 - axis], fb[3 - axis]
    clear = []
    for k in range(int(a1 - a0) + 1):
        x = a0 + k + 0.5
        spans = sorted((max(b0, it[1 - axis]), min(b1, it[3 - axis])) for it in items if it[axis] < x < it[axis + 2])
        gap, y = 0.0, b0
        for s0, s1 in spans:
            gap = max(gap, s0 - y); y = max(y, s1)
        clear.append(max(gap, b1 - y) >= GUTTER_SPAN * (b1 - b0))
    cuts, k = [], 0
    while k < len(clear):
        if not clear[k]: k += 1; continue
        j = k
        while j + 1 < len(clear) and clear[j + 1]: j += 1
        if k > 0 and j < len(clear) - 1 and j - k + 1 >= GUTTER_MIN: cuts.append(a0 + (k + j + 1) / 2)
        k = j + 1
    return cuts

def _gutter_panels(fb, caption, items):
    """The caption names N >= 2 panels and whitespace gutters cut the figure into exactly N non-empty cells: those cells,
    lettered rows top to bottom, then left to right. Else []."""
    ls = _caption_panels(caption)
    if not ls: return []
    xs = [fb[0]] + _gutters(fb, items, 0) + [fb[2]]; ys = [fb[1]] + _gutters(fb, items, 1) + [fb[3]]
    cells = [[xs[i], ys[j], xs[i + 1], ys[j + 1]] for j in range(len(ys) - 1) for i in range(len(xs) - 1)]
    full = [c for c in cells if any(_overlap(c, it) for it in items)]
    if len(full) != len(ls): return []
    return [{"label": l, "bbox": [round(v, 1) for v in c], "src": "gutter", "guessed": False} for l, c in zip(ls, full)]

def rect_order(rects, tol=12):
    """Indexes of rects in reading order: rows by top edge (a row holds tops within tol of its first), then by x."""
    rows = []
    for i in sorted(range(len(rects)), key=lambda i: (rects[i][1], rects[i][0])):
        if rows and rects[i][1] - rects[rows[-1][0]][1] <= tol: rows[-1].append(i)
        else: rows.append([i])
    return [i for row in rows for i in sorted(row, key=lambda i: (rects[i][0], rects[i][1]))]

CIRCLED = {**{0x2460 + k: k + 1 for k in range(20)}, **{0x24F5 + k: k + 1 for k in range(10)}, **{0x2776 + k: k % 10 + 1 for k in range(30)}}

def callout_number(text):
    """A callout marker's number: the whole text is a number 1-99 or one circled digit; else None."""
    t = (text or "").strip()
    if re.fullmatch(r"\d{1,2}", t): return int(t) if 1 <= int(t) <= 99 else None
    return CIRCLED.get(ord(t)) if len(t) == 1 else None

def _area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])

def _rect_dist(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0); dy = max(b[1] - a[3], a[1] - b[3], 0)
    return (dx * dx + dy * dy) ** 0.5

def _pt_dist(p, r):
    return _rect_dist([p[0], p[1], p[0], p[1]], r)

def _pad_label(lr):
    h = max(6.0, lr[3] - lr[1])
    return [lr[0] - 2 * h, lr[1] - 1.5 * h, lr[2] + 2 * h, lr[3] + 1.5 * h]

def _clip(r, b):
    return [max(b[0], r[0]), max(b[1], r[1]), min(b[2], r[2]), min(b[3], r[3])]

LEADER_MIN = 6.0              # axis tick marks (2-5 pt) next to tick numbers are not leaders

def _leader(paths, box, skip):
    """The leader of a marker box: an unfilled, open path of 1-3 straight segments with one end within 3 pt of the box,
    running away from the box (an axis line that starts beside a tick number runs along it) for >= LEADER_MIN pt.
    Returns (path, far end) or None; the nearest end wins, then the longest leader."""
    best = None; cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for p in paths:
        if p is skip or p.filled or not 2 <= p.n <= 4 or _rect_dist(p.r, box) > 3: continue
        pts = p.points()
        if pts[0][2] != "move" or any(k != "line" or closes for _, _, k, closes in pts[1:]): continue
        ends = [pts[0][:2], pts[-1][:2]]; d = [_pt_dist(e, box) for e in ends]
        near = 0 if d[0] <= d[1] else 1; (nx, ny), far = ends[near], ends[1 - near]
        span = ((far[0] - nx) ** 2 + (far[1] - ny) ** 2) ** 0.5
        if d[near] > 3 or span < LEADER_MIN or (nx - cx) * (far[0] - nx) + (ny - cy) * (far[1] - ny) <= 0: continue
        key = (d[near], -span)
        if best is None or key < best[0]: best = (key, p, far)
    return best and best[1:]

def _callout_parts(fig, markers, paths, images):
    """Callout parts (kind "callout") for numeric markers [(rect, text, n, src)]: enclosed, a circled glyph, or with a leader.
    The target (A4.1) is the smallest eligible object at the leader's far end, else the nearest eligible object within
    14 pt, else the marker (or its enclosure) padded by its height (attached false); clipped to the marker's panel.
    Never eligible: an object that contains the marker or its enclosure (frames, backgrounds, a photo the number is printed
    on), one covering >= 40 % of the marker's panel (figure when none) or >= 60 % of the figure, the marker's enclosure or
    leader, and any marker's enclosure."""
    fb, out = fig["bbox"], []
    shapes = [(p.r, p) for p in paths if _inside(p.r, fb, 4)] + [(r, None) for r in images if _inside(r, fb, 4)]
    found = []
    for mr, t, n, src in markers:
        # an enclosure is larger than the digit on both axes (a digit drawn as a glyph outline path is not one)
        enc = min(((r, p) for r, p in shapes if r[2] - r[0] <= 24 and r[3] - r[1] <= 24 and 0.6 <= (r[2] - r[0]) / max(0.1, r[3] - r[1]) <= 1.6
                   and _inside(mr, r, 4) and r[2] - r[0] >= mr[2] - mr[0] + 1 and r[3] - r[1] >= mr[3] - mr[1] + 1), key=lambda e: _area(e[0]), default=None)
        lead = _leader([p for _, p in shapes if p is not None], enc[0] if enc else mr, enc and enc[1])
        if enc or ord(t.strip()[0]) in CIRCLED or lead: found.append((mr, t, n, src, enc, lead))
    enclosures = [f[4][0] for f in found if f[4]]
    for mr, t, n, src, enc, lead in found:
        box = enc[0] if enc else mr
        pr = _panel_rect(mr, fig) or fb
        objs = [r for r, p in shapes if 100 <= _area(r) < 0.6 * _area(fb) and _area(r) < 0.4 * _area(pr)
                and not _inside(box, r, 1) and not _inside(mr, r, 1) and not any(r is e for e in enclosures) and (lead is None or p is not lead[0])]
        if lead: hits = sorted((_area(r), r) for r in objs if _pt_dist(lead[1], r) <= 4)
        else: hits = sorted((_rect_dist(r, mr), _area(r), r) for r in objs if _rect_dist(r, mr) <= 14)
        h = box[3] - box[1]
        out.append({"kind": "callout", "n": n, "t": t.strip(), "label": [round(v, 1) for v in mr],
                    "rect": [round(v, 1) for v in _clip(hits[0][-1] if hits else [box[0] - h, box[1] - h, box[2] + h, box[3] + h], pr)],
                    "attached": bool(hits), "src": src})
    return out

def _panel_rect(r, fig):
    """The bbox of the panel holding r's centre, or None."""
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return next((p["bbox"] for p in fig.get("panels") or [] if p["bbox"][0] <= cx <= p["bbox"][2] and p["bbox"][1] <= cy <= p["bbox"][3]), None)

def _panel_letter(r, fig):
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return next((p["label"] for p in fig.get("panels") or [] if p["bbox"][0] <= cx <= p["bbox"][2] and p["bbox"][1] <= cy <= p["bbox"][3]), None)

HAIRLINE = 10.0

def _hairline(r):
    """A figure box shorter than HAIRLINE pt in height or width (typically the 1-pt caption-only strip) is degenerate."""
    return r[2] - r[0] < HAIRLINE or r[3] - r[1] < HAIRLINE

def _detector_substitute(boxes, cap):
    """For a hairline figure: the nearest detector figure box above its caption (gap <= 60 pt) that overlaps the caption
    horizontally by >= 50 % of the narrower of the two, else None."""
    hits = [(cap[1] - b["bbox"][3], b["bbox"]) for b in boxes if b["role"] == "figure" and -2 <= cap[1] - b["bbox"][3] <= 60
            and min(b["bbox"][2], cap[2]) - max(b["bbox"][0], cap[0]) >= 0.5 * min(b["bbox"][2] - b["bbox"][0], cap[2] - cap[0])]
    return [*min(hits)[1][:3], min(min(hits)[1][3], cap[1])] if hits else None

def _graphics(fb, paths, images):
    """The figure's graphic objects (paths and images overlapping it), without frames and backgrounds (>= 60 % of it)."""
    return [r for r in [p.r for p in paths] + list(images) if _overlap(r, fb) and _area(r) < 0.6 * _area(fb)]

def _label_rows(labels):
    """Drawn panel labels grouped into rows (tops within 12 pt), top to bottom, each row left to right."""
    rows = []
    for lb in sorted(labels, key=lambda lb: (lb["r"][1], lb["r"][0])):
        if rows and abs(lb["r"][1] - rows[-1][0]["r"][1]) < 12: rows[-1].append(lb)
        else: rows.append([lb])
    return [sorted(row, key=lambda lb: lb["r"][0]) for row in rows]

LABEL_GAP = 24.0

def _panels_below(fb, labels, gfx):
    """Panels for figures whose every drawn label sits below its panel's graphics: graphics directly above the label in its
    column within LABEL_GAP pt, and nothing below it nearer than that (the next row's graphics lie farther down). Each panel
    runs from the previous label row (+2 pt, else the figure top) down to the label's bottom, and sideways to the midpoints
    between neighbouring label centres (else the figure edges). [] when any label does not qualify."""
    rows, out = _label_rows(labels), []
    for ri, row in enumerate(rows):
        for k, lb in enumerate(row):
            r = lb["r"]; cx = lambda l: (l["r"][0] + l["r"][2]) / 2
            x0 = (cx(row[k - 1]) + cx(lb)) / 2 if k else fb[0]; x1 = (cx(lb) + cx(row[k + 1])) / 2 if k + 1 < len(row) else fb[2]
            col = [g for g in gfx if g[0] < x1 and x0 < g[2]]
            above = min((r[1] - g[3] for g in col if -2 <= r[1] - g[3] <= LABEL_GAP), default=None)
            below = min((g[1] - r[3] for g in col if g[1] >= r[3] - 2 and g[1] <= fb[3]), default=None)
            if above is None or (below is not None and below <= above): return []
            top = max(l["r"][3] for l in rows[ri - 1]) + 2 if ri else fb[1]
            out.append({"label": lb["l"], "bbox": [round(x0, 1), round(top, 1), round(x1, 1), round(r[3], 1)], "src": "label", "guessed": False})
    return sorted(out, key=lambda p: p["label"])

def _hierarchy(fig, page_lines, panel_labels, ocr, page_shapes):
    """Panels (drawn labels, else whitespace gutters, else the caption's even split), each object's panel, and the parts:
    one text part per label (rect = its neighbourhood) plus the callouts. page_shapes() -> (paths, images), read once per page."""
    fb = fig["bbox"]
    panels = (len(panel_labels) >= 2 and _panels_below(fb, panel_labels, _graphics(fb, *page_shapes()))) or _panels(fb, panel_labels)
    if not panels and fig["bbox_src"] != "caption-only" and _caption_panels(fig["caption"]):
        items = [l[:4] for l in page_lines if _overlap(l[:4], fb)] + _graphics(fb, *page_shapes())   # frames cross every band
        panels = _gutter_panels(fb, fig["caption"], [_clip(r, fb) for r in items]) or _guess_panels(fb, fig["caption"])
    fig["panels"] = [dict(p, bbox=[round(v, 1) for v in _clip(p["bbox"], fb)]) for p in panels]
    fig["objects"] = [o[:5] + [_panel_letter(o, fig)] for o in fig["objects"]]
    parts = []
    for l in fig["labels"]:
        r, attached = part_hood(l[:4], fig)
        parts.append({"kind": "text", "n": None, "t": l[4], "label": [round(v, 1) for v in l[:4]],
                      "rect": [round(v, 1) for v in _clip(r, _panel_rect(l[:4], fig) or fb)], "attached": attached, "src": fig["labels_src"]})
    markers = [(l[:4], l[4], callout_number(l[4]), "pdf") for l in page_lines if _inside(l[:4], fb) and callout_number(l[4])]
    markers += [(o[:4], o[4], callout_number(o[4]), "ocr") for o in ocr if callout_number(o[4])]
    if markers: parts += _callout_parts(fig, markers, *page_shapes())
    fig["parts"] = [{"id": f"p{k + 1}", "kind": p["kind"], "n": p["n"], "t": p["t"], "label": p["label"], "rect": p["rect"],
                     "panel": _panel_letter(p["label"], fig), "attached": p["attached"], "src": p["src"]}
                    for k, p in enumerate(parts[i] for i in rect_order([p["label"] for p in parts]))]

def parse_pdf(path, fig_pages=None, prev=None):
    """Text lines for every page; figures (bbox, caption, panels, text labels, graphic objects) for fig_pages (None = all).
    prev: an earlier partial parse whose figure pages are kept."""
    import pdfpage, layoutdet
    doc = pdfpage.open_pdf(path)
    detector = layoutdet.Detector()
    pages = []
    old = {p["n"]: p for p in (prev or {}).get("pages", []) if p.get("figs_done")}
    for pno, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        if pno + 1 in old and (fig_pages is None or True):
            pages.append(old[pno + 1]); continue
        if fig_pages is not None and pno + 1 not in fig_pages:
            lines, _ = _text_lines(page)
            pages.append({"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": [], "figs_done": False}); continue
        lines, blocks = _text_lines(page)
        columns = page_columns(lines, W); two_col = columns == 2
        gfx = []
        try:
            for r in page.images():
                if r and (r[2] - r[0]) > 20 and (r[3] - r[1]) > 20: gfx.append(list(r))
        except Exception: pass
        try:
            for r in page.cluster_drawings(on_page=True):
                if r.width > 20 and r.height > 20 and not (r.width > 0.9 * W and r.height > 0.9 * H): gfx.append([r.x0, r.y0, r.x1, r.y1])
        except Exception: pass
        lay = detector.layout(pno + 1, page, lines, W, H, columns)
        if lay: blocks = lay.split_groups(blocks, lambda s: CAP_RE.match(s) or TAB_RE.match(s))
        found = []                                          # regex captions: (line group, match, kind)
        for bl in blocks:
            m = CAP_RE.match(lines[bl[0]][4]); kind = "figure"
            if not m: m = TAB_RE.match(lines[bl[0]][4]); kind = "table"
            if m: found.append((bl, m, kind))
        if lay:
            regex_lines = {i for bl, _, _ in found for i in bl}
            found = [(lay.caption_lines(bl, regex_lines - set(bl)), m, kind) for bl, m, kind in found]
            paired = lay.bodies([(kind, _union([lines[i][:4] for i in bl]), sorted(lines[i][3] - lines[i][1] for i in bl)[len(bl) // 2])
                                 for bl, _, kind in found], {i for bl, _, _ in found for i in bl})
        else: paired = [None] * len(found)
        # graphics right at a caption's body side belong to that caption: another caption's body never chains over them
        own_gfx = []
        for bl, _, kind in found:
            c = _union([lines[i][:4] for i in bl])
            own_gfx.append({k for k, g in enumerate(gfx) if g[0] < c[2] and c[0] < g[2] and (0 <= c[1] - g[3] < 20 if kind == "figure" else -8 <= g[1] - c[3] < 20)})
        figs, caption_line_groups, hier = [], [], []
        for ci, ((bl, m, kind), detected_body) in enumerate(zip(found, paired)):
            others = set().union(*own_gfx[:ci], *own_gfx[ci + 1:]) - own_gfx[ci]
            free_gfx = [g for k, g in enumerate(gfx) if k not in others]
            cap = [round(v, 1) for v in _union([lines[i][:4] for i in bl])]
            # the caption's column on a two-column page: a table never takes the other column's text
            nxt = min((l for l in lines if l[1] >= cap[3] - 1), key=lambda l: l[1], default=None)
            in_col = nxt is not None and nxt[2] <= W / 2 + 10 if cap[2] <= W / 2 + 10 else nxt is not None and nxt[0] >= W / 2 - 10
            if not (two_col and in_col): col = (0, W)
            elif cap[2] <= W / 2 + 10: col = (cap[0] - 12, W / 2 + 6)
            else: col = (W / 2 - 6, W)
            text = " ".join(lines[i][4] for i in bl)
            right_col, narrowed_by_prose = cap[0] >= W / 2 - 10, False
            for second_pass in (False, True):              # again inside the gutter when other-column prose sits beside it
                # figure: graphics directly above the caption (chain upward); table: content below
                # (graphics that run into the caption are clipped at its top)
                cand = sorted([[g[0], g[1], g[2], min(g[3], cap[1] - 2)] for g in free_gfx if g[1] < cap[1] - 20 and (not second_pass or (g[0] >= col[0] - 4 if right_col else g[2] <= col[1] + 4)) and g[3] >= cap[1] - 0.7 * H
                               and not (g[3] > cap[3] + 20)], key=lambda g: -g[3]) if kind == "figure" \
                    else sorted([g for g in free_gfx if g[1] >= cap[3] - 8 and g[1] <= cap[3] + 0.7 * H and g[0] >= col[0] - 4 and g[2] <= col[1] + 4], key=lambda g: g[1])
                body = None
                for g in cand:
                    if body is None:
                        gap = cap[1] - g[3] if kind == "figure" else g[1] - cap[3]
                        if gap < 60: body = list(g)
                    elif (body[1] - g[3] < 40) if kind == "figure" else (g[1] - body[3] < 40):
                        body = _union([body, g])
                bbox_src = "graphics" if body else "caption-only"
                if kind == "table":
                    # the body is the grid / text block right below the caption (until a big gap, the next caption or the page end)
                    below = sorted([l for l in lines if l[1] >= cap[3] - 1 and l[0] >= col[0] and l[2] <= col[1]], key=lambda l: l[1])
                    y = cap[3]; rows = []
                    lh = max(8.0, (cap[3] - cap[1]) / max(1, len(bl)))
                    for l in below:
                        if l[1] - y > 2.2 * lh and rows: break
                        if CAP_RE.match(l[4]) or TAB_RE.match(l[4]): break
                        rows.append(l); y = max(y, l[3])
                    tbody = _union([r[:4] for r in rows]) if rows else None
                    if tbody and (body is None or tbody[3] > body[3]):
                        bbox_src = "graphics+text-rows" if body else "text-rows"
                        body = _union([body, tbody]) if body else tbody
                    if body: body[1] = max(body[1], cap[3])        # the caption is never part of the table box
                if body is None and kind == "figure":            # no vector/image found: the text-free gap above the caption
                    above = [l for l in lines if l[3] <= cap[1] - 2 and l[0] < cap[2] and l[2] > cap[0]]
                    top = max([l[3] for l in above if cap[1] - l[3] > 30] or [None]) if above else None
                    if top is not None and cap[1] - top > 40: body = [cap[0], top + 2, cap[2], cap[1] - 2]; bbox_src = "whitespace"
                narrowed = layoutdet.body_column(lines, W, columns, cap, body) if body else col
                if second_pass or (narrowed[0] <= col[0] and narrowed[1] >= col[1]): break
                col, narrowed_by_prose = (max(col[0], narrowed[0]), min(col[1], narrowed[1])), True
            if body and narrowed_by_prose: body = [max(body[0], col[0]), *body[1:]] if right_col else [*body[:2], min(body[2], col[1]), body[3]]   # the gutter side only
            if detected_body: body, bbox_src = detected_body, "detector"
            # caption-only: a 1-pt strip on the body side, so the box never includes the caption
            fb = body or ([cap[0], max(0, cap[1] - 1), cap[2], cap[1]] if kind == "figure" else [cap[0], cap[3], cap[2], min(H, cap[3] + 1)])
            fb = [round(v, 1) for v in (max(0, fb[0]), max(0, fb[1]), min(W, fb[2]), min(H, fb[3]))]
            if kind == "figure" and lay and _hairline(fb):
                sub = _detector_substitute(lay.boxes, cap)
                if sub: fb, body, bbox_src = [round(v, 1) for v in sub], sub, "detector-substitute"
            labels = [] if kind == "table" else [{"l": (PANEL_RE.match(l[4]).group(1) or PANEL_RE.match(l[4]).group(2) or PANEL_RE.match(l[4]).group(3)).lower(), "r": l[:4]}
                      for l in lines if PANEL_RE.match(l[4]) and len(l[4]) <= 40 and body
                      and l[0] >= fb[0] - 6 and l[2] <= fb[2] + 6 and l[1] >= fb[1] - 6 and l[3] <= fb[3] + 6]
            seen = set(); labels = [x for x in labels if not (x["l"] in seen or seen.add(x["l"]))]
            labels.sort(key=lambda x: x["l"])
            # the figure's own text labels ("WANDmini", "Base Station", "EEG"): words, not tick numbers
            tl = [l for l in lines if body and _inside(l[:4], fb) and re.search(r"[A-Za-z]{2,}", l[4]) and not PANEL_RE.match(l[4])]
            src_l, ocr = "pdf", []
            if body and kind == "figure" and len(tl) < 3:
                ocr = _ocr_lines(page, fb)
                tl = [o for o in ocr if re.search(r"[A-Za-z]{2,}", o[4])] or tl; src_l = "ocr" if tl else "none"
            figs.append({"label": f"{'Figure' if kind == 'figure' else 'Table'} {m.group(1)}", "kind": kind, "n": int(m.group(1)),
                         "page": pno + 1, "bbox": fb, "caption_bbox": cap, "caption": text[:300], "panels": [], "parts": [],
                         "labels": tl[:200], "labels_src": src_l, "objects": _objects(page, fb) if body and kind == "figure" else [], "bbox_src": bbox_src})
            if kind == "figure": hier.append((figs[-1], labels, ocr))
            caption_line_groups.append(bl)
        figs = _split_overlaps(figs)
        shapes = []
        def page_shapes():
            if not shapes: shapes.extend([page.paths(), page.images()])
            return shapes
        for f, panel_labels, ocr in hier:
            f["degenerate"] = _hairline(f["bbox"])
            _hierarchy(f, lines, [p for p in panel_labels if _inside(p["r"], f["bbox"], 6)], ocr, page_shapes)
        page_bl = page_blocks(lines, blocks, figs, caption_line_groups, W, columns, lay)
        pages.append({"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": figs, "figs_done": True,
                      "columns": columns, "blocks": page_bl, "layout": detector.page_record(pno + 1, lay, page_bl)})
    return {"kind": "pdf", "pages": pages, "n_pages": len(pages), "layout": detector.status(), "layout_events": detector.events}

def page_columns(lines, W):
    """2 when at least 8 half-width lines sit in each half of the page (the figure/table column logic), else 1."""
    half = [l for l in lines if 0.25 * W < l[2] - l[0] < 0.52 * W]
    return 2 if sum(1 for l in half if l[2] <= W / 2 + 10) >= 8 and sum(1 for l in half if l[0] >= W / 2 - 10) >= 8 else 1

def line_groups(lines):
    """pdfpage.text_lines' block rule on page lines: consecutive lines that follow closely below and overlap horizontally."""
    groups, cur = [], []
    for k, l in enumerate(lines):
        if cur:
            prev = lines[cur[-1]]; hgt = max(1.0, prev[3] - prev[1])
            if not (0 <= l[1] - prev[3] < 0.8 * hgt and l[0] < prev[2] and prev[0] < l[2]): groups.append(cur); cur = []
        cur.append(k)
    if cur: groups.append(cur)
    return groups

def page_blocks(lines, groups, figs, caption_line_groups, W, columns, lay=None):
    """The page's blocks in reading order: a figure/table block and a caption block per figures[] entry; paragraphs are
    the remaining runs of each line group, without caption lines and lines inside a figure or table box.
    A caption block holds its own line group plus every line inside its caption box (inline-math fragments that the
    text layer puts in groups of their own). With a detector layout (lay), its paragraph boxes take their lines first
    and only the lines no detector box claims or excludes form line-group paragraphs."""
    out, caption_lines = [], set().union(*caption_line_groups)
    for f, cl in zip(figs, caption_line_groups):
        own = sorted(set(cl) | {i for i, l in enumerate(lines) if i not in caption_lines and _inside(l[:4], f["caption_bbox"])})
        out.append({"kind": f["kind"], "bbox": list(f["bbox"]), "label": f["label"], "src": f["bbox_src"]})
        out.append({"kind": "caption", "bbox": list(f["caption_bbox"]), "lines": own, "label": f["label"],
                    "src": "regex+detector" if lay and cl[0] in lay.extended else "regex"})
        caption_lines.update(own)
    taken = caption_lines | {i for i, l in enumerate(lines) if any(_inside(l[:4], f["bbox"]) for f in figs)}
    if lay:
        paragraphs, taken = lay.paragraphs(taken)
        out += [{"kind": "paragraph", "bbox": _union([lines[j][:4] for j in p]), "lines": p, "src": "detector"} for p in paragraphs]
    for g in groups:
        run = []
        for i in list(g) + [None]:
            if i is not None and i not in taken:
                run.append(i); continue
            if run: out.append({"kind": "paragraph", "bbox": _union([lines[j][:4] for j in run]), "lines": run, "src": "lines"}); run = []
    return reading_order(out, W, columns)

def reading_order(blocks, W, columns):
    """Column by column, top to bottom. On a two-column page a block spanning both columns ends the band above it."""
    top_left = lambda b: (b["bbox"][1], b["bbox"][0])
    if columns == 1: return sorted(blocks, key=top_left)
    def column(b):
        x0, _, x1, _ = b["bbox"]
        return None if x0 < W / 2 - 10 and x1 > W / 2 + 10 else 0 if x0 + x1 < W else 1
    out, rest = [], [b for b in blocks if column(b) is not None]
    for span in sorted([b for b in blocks if column(b) is None], key=top_left) + [None]:
        cut = span["bbox"][1] if span else float("inf")
        out += sorted([b for b in rest if b["bbox"][1] < cut], key=lambda b: (column(b), *top_left(b)))
        rest = [b for b in rest if b["bbox"][1] >= cut]
        if span: out.append(span)
    return out

def paragraph_region(page, line_ix):
    """Union of the page's paragraph blocks holding any of the page lines line_ix; None when no paragraph holds one."""
    ix = set(line_ix)
    return _union([b["bbox"] for b in page.get("blocks") or [] if b["kind"] == "paragraph" and ix & set(b["lines"])])

def parse_summary(pg):
    """Counts-only summary of a parsed page for the runs log (never text)."""
    if not pg.get("figs_done"):
        return {"columns": page_columns(pg["lines"], pg["w"]), "figs_done": False, "blocks": {"paragraph": len(line_groups(pg["lines"]))}}
    figs = pg["figures"]; kinds = [b["kind"] for b in pg["blocks"]]
    panels = [p for f in figs for p in f.get("panels") or []]; parts = [p for f in figs for p in f.get("parts") or []]
    return {"columns": pg["columns"], "figs_done": True, "blocks": {k: kinds.count(k) for k in ("paragraph", "caption", "figure", "table")},
            "bbox_src": dict(Counter(f["bbox_src"] for f in figs)), "labels_src": {k: sum(f.get("labels_src") == k for f in figs) for k in ("pdf", "ocr", "none")},
            "panels": {"labels": sum(p.get("src") == "label" for p in panels), "guessed": sum(p.get("src") == "caption" for p in panels),
                       "gutter": sum(p.get("src") == "gutter" for p in panels)},
            "parts": {"text": sum(p["kind"] == "text" for p in parts), "callout": sum(p["kind"] == "callout" for p in parts),
                      "callout_attached": sum(p["kind"] == "callout" and p["attached"] for p in parts)},
            "split": sum(1 for f in figs if f.get("split_from")), "layout": {k: v for k, v in pg["layout"].items() if k != "detector_version"}}

# ---------------- Markdown (stdlib) ----------------
def parse_md(path):
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    els, figs, in_code = [], [], None
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.startswith("```") or s.startswith("~~~"):
            if in_code is None: in_code = i
            else: els.append({"kind": "code", "line": in_code, "end": i}); in_code = None
            continue
        if in_code is not None: continue
        if re.match(r"^#{1,6}\s", s): els.append({"kind": "heading", "level": len(s) - len(s.lstrip("#")), "line": i, "text": s.lstrip("#").strip()})
        elif re.match(r"^([-*+]|\d+[.)])\s", s): els.append({"kind": "list", "line": i, "text": re.sub(r"^([-*+]|\d+[.)])\s+", "", s)})
        elif s.startswith("|"): els.append({"kind": "table", "line": i, "text": s})
        for m in re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)", l):
            figs.append({"kind": "image", "alt": m.group(1), "line": i})
        s = re.sub(r"^\s*(?:[*_]{1,3}|<(?:em|i|b|strong|figcaption)>)\s*|\s*(?:[*_]{1,3}|</(?:em|i|b|strong|figcaption)>)\s*$", "", s)
        if CAP_RE.match(s) or TAB_RE.match(s):
            m = CAP_RE.match(s) or TAB_RE.match(s)
            figs.append({"kind": "caption", "label": f"{'Figure' if CAP_RE.match(s) else 'Table'} {m.group(1)}", "line": i, "caption": s[:300]})
    # label images by an adjacent caption (or their alt text)
    out, n = [], 0
    for f in figs:
        if f["kind"] == "image":
            cap = next((c for c in figs if c["kind"] == "caption" and abs(c["line"] - f["line"]) <= 2), None)
            m = CAP_RE.match(f["alt"]) if f["alt"] else None
            n += 1
            label = cap["label"] if cap else (f"Figure {m.group(1)}" if m else None)
            out.append({"label": label, "kind": "figure", "line": f["line"], "caption": (cap or {}).get("caption") or f["alt"][:300],
                        "caption_line": cap["line"] if cap else None, "order": n})
        elif not any(abs(f["line"] - g["line"]) <= 2 for g in figs if g["kind"] == "image"):
            out.append({"label": f["label"], "kind": "figure" if f["label"].startswith("Figure") else "table", "line": f["line"],
                        "caption": f["caption"], "caption_line": f["line"]})
    return {"kind": "md", "lines": lines, "elements": els, "figures": out}

# ---------------- loading with cache ----------------
def venv_python():
    p = os.environ.get("SPEAKEASY_DOC_PYTHON") or os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "tts-venv", "bin", "python")
    return p if os.path.exists(p) else None

def cached(path):
    try: return os.path.exists(os.path.join(cache_dir(), _key(path) + ".json"))
    except OSError: return False

def load(path, timeout=30, sel_text=""):
    """Parsed document (cached). PDFs: text for all pages, figures for the selection's page (and its neighbours) only,
    so a first read stays short; later reads of other pages add their figures. Returns (doc, cache_state)."""
    kind = "pdf" if path.lower().endswith(".pdf") else "md" if re.search(r"\.(md|markdown|mdx)$", path, re.I) else None
    if not kind: return None, "unsupported"
    c = os.path.join(cache_dir(), _key(path) + ".json")
    prev = None
    if os.path.exists(c):
        try:
            prev = json.load(open(c))
            if kind == "md": return prev, "hit"
            pg, _ = match_page(prev, sel_text)
            if pg is None or pg.get("figs_done", True): return prev, "hit"
        except ValueError: prev = None
    if kind == "md":
        doc = parse_md(path)
    else:
        py = venv_python()
        if not py or os.environ.get("SPEAKEASY_PDF_ENGINE", "auto").lower() == "pdfkit":
            # PDFKit (macOS, via the tracker binary): text lines for every page; figure boxes still need PyMuPDF
            trk = tracker_bin()
            if not os.path.exists(trk): return None, "no-pdf-engine"
            try:
                r = subprocess.run([trk, "--pdf-lines", path], capture_output=True, timeout=timeout)
                doc = json.loads(r.stdout.decode() or "{}")
            except Exception as e:
                return None, f"pdfkit-failed: {type(e).__name__}"
            if not doc.get("pages"): return None, "pdfkit-failed"
            for pg in doc["pages"]:                       # no figure parse: paragraph blocks only
                pg["columns"] = page_columns(pg["lines"], pg["w"])
                pg["blocks"] = page_blocks(pg["lines"], line_groups(pg["lines"]), [], [], pg["w"], pg["columns"])
                pg["layout"] = {"src": "heuristic", "reason": "pdfkit-engine"}
            doc["layout"] = {"status": "unavailable", "detector_version": None, "reason": "pdfkit-engine"}
            doc["layout_events"] = [{"event": "layout_detector_unavailable", "reason": "pdfkit-engine", "error": None, "detector_version": None, "page": None}]
            json.dump(doc, open(c, "w"))
            return doc, "miss-pdfkit"
        tmp = c + f".{os.getpid()}.tmp"
        selp = tmp + ".sel"
        with open(selp, "w") as f: f.write(sel_text or "")
        args = [py, os.path.realpath(__file__), "parse-pdf", path, tmp, "--sel", selp] + (["--prev", c] if prev else [])
        try:
            r = subprocess.run(args, capture_output=True, timeout=timeout)
        finally:
            try: os.remove(selp)
            except OSError: pass
        if r.returncode != 0 or not os.path.exists(tmp):
            return None, "parse-failed: " + r.stderr.decode(errors="replace").strip().splitlines()[-1][:120] if r.stderr else "parse-failed"
        os.replace(tmp, c)
        return json.load(open(c)), "partial-miss" if prev else "miss"
    json.dump(doc, open(c, "w"))
    return doc, "miss"

# ---------------- finding the file ----------------
def _from_url(u):
    if u and u.startswith("file://"):
        p = urllib.parse.unquote(urllib.parse.urlparse(u).path)
        return p if os.path.isfile(p) else None
    return None

def _editor_roots(app):
    base = os.path.expanduser("~/Library/Application Support/" + ("Cursor" if "cursor" in app.lower() else "Code"))
    roots = set()
    ws = os.path.join(base, "User", "workspaceStorage")
    try:
        for d in sorted(os.listdir(ws), key=lambda d: -os.path.getmtime(os.path.join(ws, d)))[:60]:
            try:
                j = json.load(open(os.path.join(ws, d, "workspace.json")))
                p = _from_url(j.get("folder", "")) or (urllib.parse.unquote(urllib.parse.urlparse(j.get("folder", "")).path) if j.get("folder", "").startswith("file://") else None)
                if p and os.path.isdir(p): roots.add(p)
            except (OSError, ValueError): pass
    except OSError: pass
    return roots

def _search(root, name, limit=4000):
    hits, n = [], 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "venv", ".venv", "__pycache__", "build", "dist", "tests", "test")]
        n += 1
        if n > limit: break
        if name in fns: hits.append(os.path.join(dp, name))
    return hits

def resolve(hint, mode="local"):
    """hint: {app, bundle, title, ax_document, url}. Returns (source, path_or_url, note)."""
    if mode == "off" or not hint: return None, None, "off"
    app = (hint.get("app") or ""); title = hint.get("title") or ""
    p = _from_url(hint.get("ax_document") or "")
    if p and re.search(r"\.(pdf|md|markdown|mdx)$", p, re.I):
        return ("preview" if "preview" in app.lower() else "ax-document"), p, "ax"
    if re.search(r"cursor|code", app, re.I):
        title = re.sub(r"^\s*(?:\[preview\]\s*|preview\s+)", "", title, flags=re.I)      # Markdown preview tabs
        m = re.match(r"^[\u25cf\s]*([^\s\u2014][^\u2014]*?\.(?:md|markdown|mdx|pdf))(?:\s*\([^)]*\))?\s+[\u2014-]\s+(.+?)(?:\s+[\u2014-]\s+.*)?$", title)
        if m:
            name, proj = m.group(1).strip(), m.group(2).strip()
            roots = [r for r in _editor_roots(app) if os.path.basename(r) == proj] or []
            hits = sorted(set(h for r in roots for h in _search(r, name)))
            if len(hits) == 1: return "editor", hits[0], "workspace-search"
            return None, None, f"editor: {len(hits)} matches"
        return None, None, "editor: no file in title"
    u = hint.get("url") or (hint.get("ax_document") if (hint.get("ax_document") or "").startswith("http") else None)
    if u:
        p = _from_url(u)
        if p: return "browser-file", p, "url"
        pth = urllib.parse.urlparse(u).path.lower()
        if mode == "all" and re.match(r"^https://", u) and (pth.endswith(".pdf") or "/pdf/" in pth or title.lower().rstrip().endswith(".pdf")):
            if not public_host(u): return None, None, "browser: not a public host"
            return "browser", u, "url"
        return None, None, "browser: not a public pdf" if mode == "all" else "browser: web off (SPEAKEASY_DOC_READ=local)"
    return None, None, "no document"

def public_host(url):
    """Only public internet hosts: no localhost, loopback, private, link-local, reserved or multicast addresses."""
    import ipaddress, socket
    h = (urllib.parse.urlparse(url).hostname or "").strip("[]").lower()
    if not h or h == "localhost" or h.endswith(".localhost") or h.endswith(".local") or h.endswith(".internal"): return False
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified: return False
    return bool(addrs)

def clean_tmp(tmpdir, max_age=86400):
    """Delete our own old downloads (speakeasy_doc_*, > 1 day). Only files this tool created."""
    for f in os.listdir(tmpdir):
        if f.startswith("speakeasy_doc_"):
            q = os.path.join(tmpdir, f)
            try:
                if time.time() - os.path.getmtime(q) > max_age: os.remove(q)
            except OSError: pass

def fetch_pdf(url, tmpdir, max_mb=40, timeout=8):
    """Public PDFs only: no cookies, no auth, refuse redirects to other hosts or non-PDF answers."""
    clean_tmp(tmpdir)
    if not public_host(url): raise ValueError("not a public host")
    dst = os.path.join(tmpdir, "speakeasy_doc_" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".pdf")
    if os.path.exists(dst): return dst
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            a, b = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
            if b.scheme != "https" or b.netloc != a.netloc or re.search(r"log ?in|sign ?in|auth|sso|account", b.path, re.I) or not public_host(newurl): return None
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)
    op = urllib.request.build_opener(NoRedir)
    req = urllib.request.Request(url, headers={"User-Agent": "speakeasy/1 (local reader)"})
    with op.open(req, timeout=timeout) as r:
        if "pdf" not in (r.headers.get("Content-Type") or "").lower(): raise ValueError("not a pdf")
        data = r.read(max_mb * 1024 * 1024 + 1)
    if len(data) > max_mb * 1024 * 1024 or not data.startswith(b"%PDF"): raise ValueError("not a pdf / too big")
    tmp = dst + ".part"
    with open(tmp, "wb") as f: f.write(data)
    os.replace(tmp, dst)
    return dst

# ---------------- matching ----------------
def match_page(doc, sel_text, title=""):
    """Visible page: the page whose text best covers the selection; else the window title's 'N of M'."""
    st = set(_toks(sel_text))
    if st:
        best, bs = None, 0.0
        for pg in doc["pages"]:
            pt = set(t for l in pg["lines"] for t in _toks(l[4]))
            s = len(st & pt) / len(st)
            if s > bs: best, bs = pg, s
        if best and bs >= 0.6: return best, "selection-text"
    m = PAGE_OF_RE.search(title or "")
    if m and 1 <= int(m.group(1)) <= len(doc["pages"]): return doc["pages"][int(m.group(1)) - 1], "title-page"
    return None, "none"

def sel_lines(page, sel_text):
    """Indices of page lines that are part of the selection, in order."""
    st = set(_toks(sel_text)); out = []
    for i, l in enumerate(page["lines"]):
        lt = set(_toks(l[4]))
        if lt and len(lt & st) / len(lt) >= 0.8: out.append(i)
    return out

def figure_context(figs, limit=8, cap_chars=160):
    out = []
    for f in figs[:limit]:
        if not f.get("label"): continue
        pn = ", ".join(p["label"] for p in f.get("panels") or [])
        cap = re.sub(r"\s+", " ", f.get("caption") or "")
        cap = re.sub(r"^\s*(?:fig(?:ure)?s?\.?|table)\s*\d+\s*[.:|\-\u2013\u2014]?\s*", "", cap, flags=re.I)
        cap = cap[:cap_chars].rsplit(" ", 1)[0] if len(cap) > cap_chars else cap
        out.append(f"{f['label']}" + (f" (panels {pn})" if pn else "") + (": " + cap.rstrip(" .,;:") if cap else ""))
    return out

def find_ref(sentence, figs, last=None):
    """('Figure 4', 'b') mentioned in a spoken sentence, or a bare 'panel b' of the last figure."""
    for m in REF_RE.finditer(sentence or ""):
        kind = "Table" if m.group(1).lower().startswith("tab") else "Figure"
        f = next((f for f in figs if f.get("label") == f"{kind} {m.group(2)}"), None)
        if f: return f, (m.group(3) or "").lower() or None
    m = PANEL_REF_RE.search(sentence or "")
    if m and last: return last, m.group(1).lower()
    return None, None

def said_panel(sentence):
    """The panel letter a sentence names by text alone ("Figure 4a", "panel b"), or None."""
    m = REF_RE.search(sentence or "")
    if m and m.group(3): return m.group(3).lower()
    m = PANEL_REF_RE.search(sentence or "")
    return m.group(1).lower() if m else None

def target_rect(f, panel):
    if panel:
        p = next((p for p in f.get("panels") or [] if p["label"] == panel), None)
        if p: return p["bbox"], "panel"
    return f.get("bbox"), ("table" if f.get("kind") == "table" else "figure")

def content_words(sentence):
    return [t for t in _toks(sentence) if t not in GENERIC and not t.isdigit() and len(t) >= 3]

def focus_phrase(sentence):
    """The part of a sentence that names its object: drop a leading "In Figure 4a," and cut at the first clause break
    ("the device worn by the subject" -> "the device")."""
    t = re.sub(r"^\s*(?:in|as shown in|as seen in)?\s*(?:fig(?:ure)?s?\.?|table)\s*\d+\s*\(?[a-h]?\)?\s*[,:]?\s*", "", sentence or "", flags=re.I)
    t = re.sub(r"^\s*(?:in\s+)?panel\s*\(?[a-h]\)?\s*[,:]?\s*", "", t, flags=re.I)
    m = re.search(r",|;|\s(?:that|which|who|where|while|worn|used|placed|located|connected|secured|shown|by|with|via|to)\s", t)
    return (t[:m.start()] if m and m.start() > 8 else t).strip()

NUMBER_WORDS = {w: k + 1 for k, w in enumerate("one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                                "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_CIRCLED_RE = re.compile("[\u2460-\u2473\u24f5-\u24fe\u2776-\u2793]")
_UNITS = r"(?:mm|cm|km|nm|um|\u00b5m|m|s|ms|us|min|h|hz|khz|mhz|ghz|kg|mg|g|v|mv|kv|ma|w|mw|kw|db|px|pt|deg|percent)\b"
CALLOUT_REF_RE = re.compile(r"\b(?:callouts?|markers?|labels?|numbers?|no\.|items?|circles?)\s*#?\s*(?:\(\s*(\d{1,2})\s*\)|(\d{1,2})(?![\d.,]*\d)"
                            r"(?!\s*(?:%|\u00b0|" + _UNITS + "))|(" + "|".join(NUMBER_WORDS) + r")\b)", re.I)

def callout_ref(sentence):
    """The callout number a sentence refers to: a callout noun with a number ("Callout 3", "callout (3)", "marker three"),
    or a circled digit on its own ("③"). None for "Figure 3", "step 3", "3 mm" and numbers outside 1-99."""
    m = CALLOUT_REF_RE.search(sentence or "")
    if m:
        n = int(m.group(1) or m.group(2)) if (m.group(1) or m.group(2)) else NUMBER_WORDS[m.group(3).lower()]
        return n if 1 <= n <= 99 else None
    c = _CIRCLED_RE.search(sentence or "")
    return CIRCLED[ord(c.group(0))] if c else None

def callout_part(fig, n, named_panel, held_panel, area=None):
    """Contract 3.2: the callout part numbered n in the named panel (else the whole figure; in an area run only parts
    centred in the area); when several, the held panel's. Returns the part when exactly one remains (an unattached one
    carries its padded marker box, A4.1), else None."""
    scope = named_panel if any(p["label"] == named_panel for p in fig.get("panels") or []) else None
    cands = [p for p in fig.get("parts") or [] if p["kind"] == "callout" and p["n"] == n and (scope is None or p["panel"] == scope)
             and (area is None or _centre_in(p["rect"], area))]
    if len(cands) > 1 and held_panel: cands = [p for p in cands if p["panel"] == held_panel]
    return cands[0] if len(cands) == 1 else None

def _centre_in(r, b):
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]

FIGURE_WORDS = set("""overall setup overview figure panel show shown illustrate depict compare result pipeline architecture system
method approach process workflow framework summary summarise summarize diagram plot chart data example layout design""".split())
NUMBER_WORDS_ANY = set("one two three four five six seven eight nine ten eleven twelve both several each".split())

def _word_stem(w):
    """One inflection off: approaches -> approach, summaries -> summary, compared / comparing / compare -> compar."""
    for suffix, repl in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""), ("e", "")):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3: return w[:-len(suffix)] + repl
    return w

FIGURE_STEMS = FIGURE_WORDS | {_word_stem(w) for w in FIGURE_WORDS}

def names_object(sentence):
    """A2.5 / A6.1b: a sentence names an object unless nothing concrete remains once its figure references ("Figure 1",
    "Fig. 2b"), stopwords, figure-level words in any form ("compares the approaches", "summarised"), number words and
    numbers are removed."""
    words = content_words(REF_RE.sub(" ", sentence or ""))
    return any(w not in FIGURE_STEMS and _word_stem(w) not in FIGURE_STEMS and w not in NUMBER_WORDS_ANY and not w.isdigit() for w in words)

DEVICE_WORDS = {"device", "devices", "hardware", "sensor", "sensors", "headset", "wearable", "board", "unit", "module", "prototype", "instrument", "gadget"}

def object_phrase(fp):
    """The last determiner phrase: "This shows a diagram of the device" -> "the device"."""
    ms = list(re.finditer(r"\b(the|a|an|this|these|those|its|their)\s+((?:[\w-]+\s+){0,2}[\w-]+)", fp or "", re.I))
    for m in reversed(ms):
        if docmap_generic(m.group(2)): continue
        return m.group(0).strip()
    return (fp or "").strip()

def docmap_generic(words):
    return all(t in GENERIC for t in _toks(words))

def device_alias(fp, fig):
    """'the device' / 'the hardware' -> the named device the caption talks about (a mixed-case label such as 'WANDmini')."""
    if not set(_toks(fp)) & DEVICE_WORDS: return None
    cap = fig.get("caption") or ""
    names = {}
    for l in fig.get("labels") or []:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", l[4]):
            if re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|[A-Za-z]\d|\d[A-Za-z]", w) and w in cap:
                names[w] = names.get(w, 0) + 1
    return max(names, key=names.get) if names else None

def ground_label(sentence, fig, panel_hint=None):
    """Match the sentence's words to the figure's own text labels. Returns (rect, label_text, score) or None.
    The rect is the label's neighbourhood: the smallest graphic object holding or touching the label, else a padded label box."""
    alias = device_alias(sentence, fig)
    words = content_words(alias) if alias else content_words(sentence)
    if not words or not fig.get("labels"): return None
    cands = []
    for l in fig["labels"]:
        lt = [t for t in _toks(l[4]) if len(t) >= 2]
        if not lt: continue
        sc, pos = 0.0, None
        for t in lt:
            for i, w in enumerate(words):
                if t == w or (len(t) >= 4 and len(w) >= 4 and (t.startswith(w[:5]) or w.startswith(t[:5]))):
                    sc += 1.0 if t == w else 0.7; pos = i if pos is None else min(pos, i); break
        if sc <= 0: continue
        sc = sc / len(lt) ** 0.5 + 0.1 * min(len(l[4]), 20) / 20
        if sc >= 0.6: cands.append((pos, -sc, l))
    if panel_hint:                                   # a named panel: only its own labels
        cands = [c for c in cands if _inside(c[2][:4], panel_hint, 2)]
    if not cands: return None
    # a sentence that lists several things ("recording EEG data, calculating features, training a model") is about the first;
    # among equally good labels (the same name twice), prefer the one attached to a graphic (the photo, the drawn device)
    key0 = min((c[0], c[1]) for c in cands)
    top = [c for c in cands if (c[0], c[1]) == key0]
    scored = [(part_hood(c[2][:4], fig), c) for c in top]
    if len({tuple(h[0]) for h, _ in scored if h[1]}) > 1:
        # the same name on several graphics ("LoRA" under 1st / k-th / new): the graphic whose other labels the sentence
        # also names ("the new LoRA module"); when that does not single one out, none of them
        said = set(content_words(sentence))
        def named(h, own): return len({t for l in fig["labels"] if l is not own and _centre_in(l, h) for t in _toks(l[4])} & said)
        att = [(h, c) for h, c in scored if h[1]]
        best = max(named(h[0], c[2]) for h, c in att)
        scored = [(h, c) for h, c in att if named(h[0], c[2]) == best]
        if best == 0 or len({tuple(h[0]) for h, _ in scored}) > 1: return None
    (r, attached), c = max(scored, key=lambda x: x[0][1])
    l = c[2]
    r = _clip(r, _panel_rect(l[:4], fig) or fig["bbox"])
    return r, l[4], round(-c[1], 2), attached

def part_hood(lr, fig):
    """A label's neighbourhood: (the label joined with the smallest graphic object holding or within 14 pt of it, True),
    else (the padded label box, False). Never reaches across a panel boundary (Fig. 4a "WANDmini" grabbed a 4b box)."""
    fa = (fig["bbox"][2] - fig["bbox"][0]) * (fig["bbox"][3] - fig["bbox"][1])
    pl = _panel_rect(lr, fig)
    objs = [o for o in fig.get("objects") or [] if (o[2] - o[0]) * (o[3] - o[1]) < 0.35 * fa and (pl is None or _inside(o[:4], pl, 2))]
    h = max(6.0, lr[3] - lr[1])
    near = sorted([o for o in objs if _rect_dist(o, lr) <= 14 and (o[2] - o[0]) * (o[3] - o[1]) > 4 * (lr[2] - lr[0]) * h],
                  key=lambda o: (_rect_dist(o, lr) > 0, (o[2] - o[0]) * (o[3] - o[1])))
    if near: return _union([near[0][:4], lr]), True
    return _pad_label(lr), False

def fit(pairs):
    """Uniform scale + offset from (page_rect, screen_rect) pairs: X = s*x + ox, Y = s*y + oy. Returns (s, ox, oy, rms) or None."""
    if len(pairs) < 2: return None
    P = [((a[0] + a[2]) / 2, (a[1] + a[3]) / 2) for a, _ in pairs]
    S = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for _, b in pairs]
    n = len(P); mpx = sum(p[0] for p in P) / n; mpy = sum(p[1] for p in P) / n; msx = sum(s[0] for s in S) / n; msy = sum(s[1] for s in S) / n
    num = sum((p[0] - mpx) * (s[0] - msx) + (p[1] - mpy) * (s[1] - msy) for p, s in zip(P, S))
    den = sum((p[0] - mpx) ** 2 + (p[1] - mpy) ** 2 for p in P)
    if den < 100: return None
    sc = num / den
    if sc <= 0: return None
    ox, oy = msx - sc * mpx, msy - sc * mpy
    rms = (sum((sc * p[0] + ox - s[0]) ** 2 + (sc * p[1] + oy - s[1]) ** 2 for p, s in zip(P, S)) / n) ** 0.5
    return sc, ox, oy, rms

def tracker_bin():
    return os.environ.get("SPEAKEASY_TRACKER") or os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "bin", "speakeasy-track")

def ocr_capture(png, frame, timeout=15):
    """Recognised text lines of a screenshot, boxes in screen points: [[x0, y0, x1, y1, text], ...]."""
    trk = tracker_bin()
    if not os.path.exists(trk) or not os.path.exists(png): return []
    try:
        r = subprocess.run([trk, "--ocr-lines", png, "--frame", ",".join(str(v) for v in frame)], capture_output=True, timeout=timeout)
        return [[float(v) for v in o["r"]] + [o.get("t", "")] for o in json.loads(r.stdout.decode() or "[]")]
    except Exception:
        return []

def anchor_fit(ocr, anchors, frame=None):
    """The tracker's page->screen fit: unique token matches of page lines to recognised lines (left edge, vertical centre),
    skipping lines cut by the capture edge, one outlier-rejection pass. Returns (s, ox, oy, rms, n) or None."""
    ol = [(set(_toks(o[4])), o) for o in ocr]
    P, S = [], []
    for a in anchors:
        at = set(_toks(a[4]))
        if len(at) < 3: continue
        c = [o for ot, o in ol if len(ot & at) / len(at) >= 0.75 and len(ot & at) / max(1, len(ot)) >= 0.6]
        if len(c) != 1: continue
        b = c[0]
        if frame and (b[0] <= frame[0] + 3 or b[2] >= frame[0] + frame[2] - 3): continue
        P.append((a[0], (a[1] + a[3]) / 2)); S.append((b[0], (b[1] + b[3]) / 2))
    def f(P, S):
        n = len(P)
        if n < 2: return None
        mpx = sum(p[0] for p in P) / n; mpy = sum(p[1] for p in P) / n; msx = sum(q[0] for q in S) / n; msy = sum(q[1] for q in S) / n
        num = sum((p[0] - mpx) * (q[0] - msx) + (p[1] - mpy) * (q[1] - msy) for p, q in zip(P, S))
        den = sum((p[0] - mpx) ** 2 + (p[1] - mpy) ** 2 for p in P)
        if den < 100: return None
        sc = num / den; return sc, msx - sc * mpx, msy - sc * mpy
    t = f(P, S)
    if t and len(P) >= 4:
        res = [((t[0] * p[0] + t[1] - q[0]) ** 2 + (t[0] * p[1] + t[2] - q[1]) ** 2) ** 0.5 for p, q in zip(P, S)]
        med = sorted(res)[len(res) // 2]
        keep = [i for i, v in enumerate(res) if v <= max(6, 2.5 * med)]
        if 2 <= len(keep) < len(P): P = [P[i] for i in keep]; S = [S[i] for i in keep]; t = f(P, S)
    if not t or not (0.3 <= t[0] <= 6): return None
    rms = (sum((t[0] * p[0] + t[1] - q[0]) ** 2 + (t[0] * p[1] + t[2] - q[1]) ** 2 for p, q in zip(P, S)) / len(P)) ** 0.5
    return (t[0], t[1], t[2], rms, len(P)) if rms <= 10 else None

def table_rows(lines, tb, cap=None):
    """Row rects of a table from its PDF text lines: lines inside the table box (caption excluded) grouped by vertical
    centre. Returns [{"rect": [x0,y0,x1,y1], "toks": set, "n": cells}] top to bottom; rows[0] is the header row."""
    inn = [l for l in lines if _inside(l[:4], tb, 3) and not (cap and _inside(l[:4], cap, 2)) and l[4].strip()]
    inn.sort(key=lambda l: (l[1] + l[3]) / 2)
    rows = []
    for l in inn:
        cy, h = (l[1] + l[3]) / 2, max(4.0, l[3] - l[1])
        if rows and abs(cy - rows[-1]["cy"]) < 0.6 * h:
            r = rows[-1]; r["rect"] = [min(r["rect"][0], l[0]), min(r["rect"][1], l[1]), max(r["rect"][2], l[2]), max(r["rect"][3], l[3])]
            r["toks"] |= set(_toks(l[4])); r["n"] += 1
        else:
            rows.append({"cy": cy, "rect": list(l[:4]), "toks": set(_toks(l[4])), "n": 1})
    for r in rows:                                   # a row band spans the table's width
        r["rect"] = [min(r["rect"][0], tb[0]), r["rect"][1] - 1, max(r["rect"][2], tb[2]), r["rect"][3] + 1]
    return rows

def match_row(rows, sentence, start=0):
    """Forward-only: the first row at or after start that the spoken sentence covers best (table speech reads one row per
    sentence). Returns the row index or None."""
    st = set(_toks(sentence))
    if not st: return None
    best, bi = 0.0, None
    for i in range(start, len(rows)):
        rt = rows[i]["toks"]
        if not rt: continue
        sc = len(rt & st) / len(rt)
        if sc > best + 0.05: best, bi = sc, i
    return bi if best >= 0.4 else None

def area_lines(lines, prect):
    """PDF text inside a rectangle (page space), like a PDF reader's rectangle / column selection (PDFKit
    PDFPage.selection(for:), Preview's Option-drag): lines whose vertical centre is inside and at least half of whose width
    is inside, ordered column by column (a gutter is an x no chosen line crosses), top to bottom within a column."""
    x0, y0, x1, y1 = prect
    sel = []
    for l in lines:
        cy = (l[1] + l[3]) / 2; w = max(0.1, l[2] - l[0])
        if y0 - 1 <= cy <= y1 + 1 and (min(l[2], x1) - max(l[0], x0)) / w >= 0.5 and l[4].strip(): sel.append(l)
    if not sel: return []
    # gutters: sweep the x extents; a gap between merged spans splits columns
    spans = sorted([max(l[0], x0), min(l[2], x1)] for l in sel)
    cols = [spans[0][:]]
    for a0, a1 in spans[1:]:
        if a0 > cols[-1][1] + 4: cols.append([a0, a1])
        else: cols[-1][1] = max(cols[-1][1], a1)
    def col(l):
        c = max(l[0], x0)
        return next(i for i, (c0, c1) in enumerate(cols) if c0 - 0.5 <= c <= c1 + 0.5)
    return sorted(sel, key=lambda l: (col(l), round(l[1], 0), l[0]))

def apply(t, r):
    s, ox, oy = t[:3]
    return [s * r[0] + ox, s * r[1] + oy, s * (r[2] - r[0]), s * (r[3] - r[1])]

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "parse-pdf":
        opt = dict(zip(sys.argv[4::2], sys.argv[5::2]))
        prev = json.load(open(opt["--prev"])) if opt.get("--prev") and os.path.exists(opt["--prev"]) else None
        sel = open(opt["--sel"]).read() if opt.get("--sel") else ""
        if sel.strip():
            import pdfpage
            d0 = pdfpage.open_pdf(sys.argv[2])
            txt = {"pages": [{"n": i + 1, "lines": _text_lines(pg)[0]} for i, pg in enumerate(d0)]}
            pg, _ = match_page(txt, sel)
            want = {pg["n"] - 1, pg["n"], pg["n"] + 1} if pg else {1}
            out = parse_pdf(sys.argv[2], want, prev)
        else:
            out = parse_pdf(sys.argv[2], None, prev)
        json.dump(out, open(sys.argv[3], "w")); sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "dump":
        print(json.dumps(parse_pdf(sys.argv[2]) if sys.argv[2].lower().endswith(".pdf") else parse_md(sys.argv[2]), indent=1)[:6000]); sys.exit(0)
    print("usage: docmap.py parse-pdf FILE OUT.json | dump FILE", file=sys.stderr); sys.exit(2)
