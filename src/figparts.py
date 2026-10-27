"""Figure > panel > part hierarchy at parse time (phase5-parts.md): panel boxes from (a)/(b) labels, labels below panels and
gutters, figure objects and OCR labels, numbered callouts with leader lines, hairline and detector-substitute figures, and the
part neighbourhood a label's oval may use. docmap re-exports every name."""
import json, os, re, subprocess, tempfile
from docgeom import _area, _clip, _inside, _overlap, _pad_label, _pt_dist, _rect_dist, _union, rect_order, tracker_bin

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

CIRCLED = {**{0x2460 + k: k + 1 for k in range(20)}, **{0x24F5 + k: k + 1 for k in range(10)}, **{0x2776 + k: k % 10 + 1 for k in range(30)}}

def callout_number(text):
    """A callout marker's number: the whole text is a number 1-99 or one circled digit; else None."""
    t = (text or "").strip()
    if re.fullmatch(r"\d{1,2}", t): return int(t) if 1 <= int(t) <= 99 else None
    return CIRCLED.get(ord(t)) if len(t) == 1 else None

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
