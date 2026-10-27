"""Layout detection for parsed PDF pages: PP-DocLayout-S (PaddlePaddle, Apache-2.0; ONNX export
stefanj0/PP-DocLayout-S-ONNX) on onnxruntime CPU, plus the glue that turns its boxes into docmap's page blocks:
caption extension, caption-anchored body pairing, snapping to PDF geometry and detector paragraphs.
Runs only inside the doc-venv parse (docmap.py parse-pdf), imported by sibling path. numpy and onnxruntime are imported
lazily, so importing this module never fails. Contract: docs/contracts/phase4-layout.md."""
import hashlib, os, sys, time

MODEL_FILE = "pp_doclayout_s.onnx"
MODEL_URL = "https://huggingface.co/stefanj0/PP-DocLayout-S-ONNX/resolve/7e851b9e5ad66613d7318a3213ab3336ebe0a514/pp_doclayout_s.onnx"
MODEL_SIZE = 4917852
MODEL_SHA256 = "33688dbee1c23e34b81777e97cb428eb40f24b242c02b5f623484959e830aec8"
DETECTOR_VERSION = "pp-doclayout-s/" + MODEL_SHA256[:12]

INPUT_SIZE = 480
RENDER_ZOOM = 1.0
THREADS = 4                      # fixed, never from the environment: two parses of one file give identical boxes
SCORE_MIN = 0.4                  # the one global score threshold
WEAK_CAPTION_MIN = 0.2           # caption boxes below SCORE_MIN kept aside for Phase 6's in-box caption text only (never blocks)
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)

# tolerances, page points unless stated
LINE_SLACK = 2                   # a line belongs to a box when its centre lies inside the box, grown by this much
PAIR_GAP_LINES = 3               # caption-to-body gap, in caption line heights
CHAIN_GAP = 40                   # panels of one figure closer than this join its body
SNAP_OUTWARD = 8                 # a snapped edge moves at most this far past the detector edge
BLOCK_SLACK = 2                  # a body may overlap a caption or paragraph by this much
PAGE_ITEM_MAX = 0.9              # content items covering more of the page are clip rects, not content
COLUMN_SLACK = 10                # as docmap's two-column rule
SPACING_CUE = 1.15               # row pitch over the page's usual pitch that hints at a paragraph break
SPACING_BREAK = 1.3              # ... and that is a paragraph break on its own
DUPLICATE_IOU = 0.5              # body boxes of one role overlapping this much are one object (class-agnostic NMS)

CLASSES = ("paragraph_title", "image", "text", "number", "abstract", "content", "figure_title", "formula", "table",
           "table_title", "reference", "doc_title", "footnote", "header", "algorithm", "footer", "seal", "chart_title",
           "chart", "formula_number", "header_image", "footer_image", "aside_text")
ROLE = {**dict.fromkeys(("text", "abstract", "content", "reference", "footnote", "aside_text"), "paragraph"),
        **dict.fromkeys(("figure_title", "table_title", "chart_title"), "caption"),
        **dict.fromkeys(("image", "chart"), "figure"), "table": "table"}      # every other class: no block ("none")
KINDS = ("paragraph", "caption", "figure", "table")

def layout_off():
    """SPEAKEASY_LAYOUT from the environment, else from $SPEAKEASY_HOME/config (a direct parse-pdf run reads no config)."""
    value = os.environ.get("SPEAKEASY_LAYOUT")
    if value is None:
        cfg = os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "config")
        if os.path.isfile(cfg):
            for line in open(cfg, encoding="utf-8"):
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == "SPEAKEASY_LAYOUT": value = v.strip().strip('"').strip("'")
    return (value or "on").strip().lower() in ("off", "0", "false", "no")

def model_path():
    """SPEAKEASY_LAYOUT_MODEL when set (no fall-through), else $SPEAKEASY_HOME/models, else the app bundle's
    Resources/models next to the backend dir. None when no file is found."""
    override = os.environ.get("SPEAKEASY_LAYOUT_MODEL")
    if override: return override if os.path.isfile(override) else None
    home = os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy")
    for p in (os.path.join(home, "models", MODEL_FILE),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", MODEL_FILE)):
        if os.path.isfile(p): return p
    return None

def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def model_verified(path):
    return os.path.getsize(path) == MODEL_SIZE and file_sha256(path) == MODEL_SHA256

def fetch_model(models_dir):
    """Installer step: a verified model at models_dir/MODEL_FILE. Exit status 0 ok, 1 checksum or size mismatch, 2
    download failure. A good file already there is kept without network access; a bad one never stays.
    SPEAKEASY_LAYOUT_MODEL_URL overrides the pinned source (tests, mirrors)."""
    return fetch_verified(models_dir, MODEL_FILE, os.environ.get("SPEAKEASY_LAYOUT_MODEL_URL") or MODEL_URL, MODEL_SIZE, MODEL_SHA256,
                          "Layout model", "PDF layout detection off")

def fetch_verified(models_dir, file_name, url, size, sha256, what, off_note):
    """A pinned file at models_dir/file_name with its size and sha256 checked (fetch_model's rules, shared by every model)."""
    import subprocess
    dest = os.path.join(models_dir, file_name); part = dest + ".part"
    os.makedirs(models_dir, exist_ok=True)
    if os.path.isfile(dest):
        if os.path.getsize(dest) == size and file_sha256(dest) == sha256: print(f"{what} present: {dest}"); return 0
        os.remove(dest)
    r = subprocess.run(["curl", "-fsSL", "--retry", "2", "--max-time", "300", "-o", part, url], capture_output=True, text=True)
    if r.returncode != 0:
        if os.path.exists(part): os.remove(part)
        cause = (r.stderr.strip().splitlines() or [f"curl exit {r.returncode}"])[-1]
        print(f"WARNING: {what[0].lower() + what[1:]} download failed ({cause}); {off_note}"); return 2
    got = file_sha256(part)
    if os.path.getsize(part) != size or got != sha256:
        os.remove(part)
        print(f"ERROR: {what[0].lower() + what[1:]} checksum mismatch (expected {sha256}, got {got}); {off_note}"); return 1
    os.replace(part, dest)
    print(f"{what} installed: {dest}"); return 0

class Detector:
    """One per parse. The model is checked and loaded on the first detected page and reused for the rest of the parse.
    Every fallback is recorded as a layout_detector_unavailable event plus one stderr line."""
    def __init__(self):
        self.session, self.reason, self.error, self.events, self.failed_pages = None, None, None, [], set()
        self.checked = self.loaded = False

    def _unavailable(self, reason, error=None, page=None):
        version = DETECTOR_VERSION if reason in ("load-failed", "inference-failed") else None
        self.events.append({"event": "layout_detector_unavailable", "reason": reason, "error": type(error).__name__ if error else None,
                            "detector_version": version, "page": page})
        print(f"layout: detector unavailable ({reason})", file=sys.stderr)
        if page is None: self.reason, self.error = reason, error

    def _check(self):
        """The reasons that need no model load, in contract order. Returns the model path, or None (reason recorded)."""
        self.checked = True
        if layout_off(): return self._unavailable("disabled")
        try:
            import numpy as np, onnxruntime as ort
        except ImportError as e:
            return self._unavailable("no-onnxruntime", e)
        self.np, self.ort = np, ort
        path = model_path()
        if path is None: return self._unavailable("model-missing")
        if not model_verified(path): return self._unavailable("model-mismatch")
        return path

    def ready(self):
        if self.loaded: return self.session is not None
        self.loaded = True
        path = self._check()
        if path is None: return False
        ort = self.ort
        try:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads, opts.inter_op_num_threads, opts.log_severity_level = THREADS, 1, 3
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self.session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        except Exception as e:                      # any load error: the parse falls back, it never fails
            self._unavailable("load-failed", e)
        return self.session is not None

    def status(self):
        if not self.checked: self._check()
        if self.reason: return {"status": "unavailable", "detector_version": DETECTOR_VERSION if self.reason == "load-failed" else None, "reason": self.reason}
        return {"status": "ok", "detector_version": DETECTOR_VERSION}

    def _detect(self, page, W, H):
        from PIL import Image
        np = self.np
        img = page.image([0, 0, W, H], RENDER_ZOOM)
        w, h = img.size
        x = np.asarray(img.resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR), dtype=np.float32) / 255.0
        x = ((x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)).transpose(2, 0, 1)[None].astype(np.float32)
        rows, num = self.session.run(None, {"image": x, "scale_factor": np.array([[INPUT_SIZE / h, INPUT_SIZE / w]], np.float32)})
        sx, sy, out, self.weak_captions = W / w, H / h, [], []
        for cid, score, x0, y0, x1, y1 in rows[:int(num[0])].tolist():
            r = [round(min(max(v, 0.0), lim), 1) for v, lim in ((x0 * sx, W), (y0 * sy, H), (x1 * sx, W), (y1 * sy, H))]
            if not (r[2] > r[0] and r[3] > r[1]): continue
            role = ROLE.get(CLASSES[int(cid)], "none")
            if score < SCORE_MIN:
                if role == "caption" and score >= WEAK_CAPTION_MIN: self.weak_captions.append(r)
                continue
            out.append({"cls": CLASSES[int(cid)], "role": role, "score": score, "bbox": r})
        out.sort(key=lambda b: (-b["score"], b["bbox"], b["cls"]))
        self.weak_captions.sort()
        return out

    def layout(self, n, page, lines, W, H, columns):
        """The PageLayout of detected page n, or None when the page falls back to the heuristics."""
        if not self.ready(): return None
        t0 = time.perf_counter()
        try:
            boxes = self._detect(page, W, H)
        except Exception as e:                      # one failed page falls back alone; the other pages keep the detector
            self.failed_pages.add(n); self._unavailable("inference-failed", e, n); return None
        lay = PageLayout(boxes, lines, page, W, H, columns, time.perf_counter() - t0)
        lay.weak_captions = getattr(self, "weak_captions", [])
        return lay

    def page_record(self, n, lay, blocks):
        """pages[].layout of a detected page; a detector page also gets its layout_page event."""
        if lay is None: return {"src": "heuristic", "reason": "inference-failed" if n in self.failed_pages else self.reason}
        counts = {k: sum(b["kind"] == k for b in blocks) for k in KINDS}
        ms = int(round(lay.seconds * 1000))
        self.events.append({"event": "layout_page", "page": n, "ms": ms, "detector_version": DETECTOR_VERSION, "blocks_by_kind": counts})
        return {"src": "detector", "ms": ms, "detector_version": DETECTOR_VERSION, "blocks_by_kind": counts}

# ---------------- geometry ----------------
def _union(rs):
    return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]

def _overlap(a, b, slack=0.0):
    return min(a[2], b[2]) - max(a[0], b[0]) > slack and min(a[3], b[3]) - max(a[1], b[1]) > slack

def _iou(a, b):
    i = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0

def _gap(a, b):
    return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3], 0.0)

def _cx(r): return (r[0] + r[2]) / 2

def _centre_in(r, b, slack=LINE_SLACK):
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return b[0] - slack <= cx <= b[2] + slack and b[1] - slack <= cy <= b[3] + slack

def _inside_frac(r, b):
    """Share of r's own area inside b (rules and hairlines count as 1 pt thick)."""
    w, h = max(1.0, r[2] - r[0]), max(1.0, r[3] - r[1])
    ix = max(0.0, min(r[2], b[2]) - max(r[0], b[0])) if r[2] > r[0] else float(b[0] <= r[0] <= b[2])
    iy = max(0.0, min(r[3], b[3]) - max(r[1], b[1])) if r[3] > r[1] else float(b[1] <= r[1] <= b[3])
    return min(1.0, ix / w) * min(1.0, iy / h)

def _clear(s, o):
    """s with the edge facing o moved to o's edge, keeping the largest remaining area; s when every cut empties it."""
    cuts = [[s[0], o[3], s[2], s[3]], [s[0], s[1], s[2], o[1]], [o[2], s[1], s[2], s[3]], [s[0], s[1], o[0], s[3]]]
    cuts = [c for c in cuts if c[2] > c[0] and c[3] > c[1]]
    return max(cuts, key=lambda c: (c[2] - c[0]) * (c[3] - c[1])) if cuts else s

def body_column(lines, W, columns, cap, region):
    """(x0, x1) a figure / table body of caption cap may use, on both parse paths. On a two-column page: the caption's
    column when the caption sits in one. On any page (also one the two-column rule calls one-column, and for a caption
    that reaches a little past the midline): when two or more column-width text lines (docmap's two-column line rule)
    of the other half sit beside region, the body ends at the gutter in front of them."""
    col = (0.0, W)
    if columns == 2:
        if cap[2] <= W / 2 + COLUMN_SLACK: col = (0.0, W / 2)
        elif cap[0] >= W / 2 - COLUMN_SLACK: col = (W / 2, W)
    right = cap[0] >= W / 2 - COLUMN_SLACK                 # the caption starts in the right column
    beside = [l for l in lines if 0.25 * W < l[2] - l[0] < 0.52 * W and region[1] <= (l[1] + l[3]) / 2 <= region[3]
              and (l[2] <= W / 2 + COLUMN_SLACK if right else l[0] >= W / 2 - COLUMN_SLACK)]
    if len(beside) >= 2:
        col = (max(col[0], max(l[2] for l in beside) + BLOCK_SLACK), col[1]) if right else (col[0], min(col[1], min(l[0] for l in beside) - BLOCK_SLACK))
    return col

class PageLayout:
    """The post-threshold detector boxes of one page and the rules of contract section 4 that use them."""
    def __init__(self, boxes, lines, page, W, H, columns, seconds):
        # the model applies NMS per class only: image and chart (or two table) boxes on one object are one body
        kept = []
        for b in boxes:
            if not (b["role"] in ("figure", "table") and any(k["role"] == b["role"] and _iou(k["bbox"], b["bbox"]) >= DUPLICATE_IOU for k in kept)): kept.append(b)
        boxes = kept
        self.boxes, self.lines, self.page, self.W, self.H, self.columns, self.seconds = boxes, lines, page, W, H, columns, seconds
        for b in boxes: b["lines"] = [i for i, l in enumerate(lines) if _centre_in(l, b["bbox"])]
        self.extended = set()              # first line of every caption group the detector extended
        self.unpaired = set()              # indexes of body boxes that seed no caption and join no body

    def _column(self, r):
        """(x0, x1) of the column holding r on a two-column page; the page width on one column or for a spanning r."""
        if self.columns == 2:
            if r[2] <= self.W / 2 + COLUMN_SLACK: return (0.0, self.W / 2)
            if r[0] >= self.W / 2 - COLUMN_SLACK: return (self.W / 2, self.W)
        return (0.0, self.W)

    def split_groups(self, groups, starts_caption):
        """Text-layer line groups cut around a caption that the text layer ran into the paragraph above or below it. A
        line inside a group that starts_caption (CAP_RE / TAB_RE) starts its own group when the detector separates it from
        the line before: it is the top line of a detector caption box, the line before ended early (see _resegment), or
        the line before is in a detector paragraph box that does not hold it. The
        new group holds the following lines of that caption box (no box: the following lines of no paragraph box)."""
        tops = {}
        for b in self.boxes:
            if b["role"] == "caption" and b["lines"]:
                top = min(b["lines"], key=lambda i: (self.lines[i][1], self.lines[i][0])); tops.setdefault(top, set(b["lines"]))
        para_of = {}
        for b in self.boxes:
            if b["role"] == "paragraph":
                for i in b["lines"]: para_of.setdefault(i, set()).add(id(b))
        out = []
        for g in groups:
            cur = []; k = 0
            while k < len(g):
                i = g[k]
                if k > 0 and starts_caption(self.lines[i][4]) and (i in tops or self._ended_early(g[k - 1], i, g) or
                                                                   (g[k - 1] in para_of and not para_of.get(i, set()) & para_of[g[k - 1]])):
                    if cur: out.append(cur)
                    cur = [i]; k += 1
                    own = (lambda j: j in tops[i]) if i in tops else (lambda j: j not in para_of)
                    while k < len(g) and own(g[k]) and not starts_caption(self.lines[g[k]][4]): cur.append(g[k]); k += 1
                    out.append(cur); cur = []; continue
                cur.append(i); k += 1
            if cur: out.append(cur)
        return out

    def _ended_early(self, p, q, group):
        """Line p ended before the right edge of its group although line q's first word would have fit after it."""
        L = self.lines; words = L[q][4].split()
        per_char = (L[q][2] - L[q][0]) / max(1, len(L[q][4]))
        return bool(words) and max(L[j][2] for j in group) - L[p][2] > per_char * (len(words[0]) + 1)

    def caption_lines(self, group, foreign):
        """A regex caption's line group, extended with the contiguous same-column lines of a detector caption box that
        overlaps its first line. foreign: lines of the other regex captions."""
        first = self.lines[group[0]]
        boxes = [b for b in self.boxes if b["role"] == "caption" and _overlap(b["bbox"], first)]
        col = self._column(_union([self.lines[i] for i in group]))
        cand = sorted({i for b in boxes for i in b["lines"] if i not in foreign and i not in group and col[0] <= _cx(self.lines[i]) <= col[1]})
        if not cand: return group
        own, top, bot = list(group), min(self.lines[i][1] for i in group), max(self.lines[i][3] for i in group)
        hs = sorted(self.lines[i][3] - self.lines[i][1] for i in group); reach = 0.8 * hs[len(hs) // 2]
        grown = True
        while grown:
            grown = False
            for i in [i for i in cand if i not in own]:
                l = self.lines[i]
                if l[1] <= bot + reach and l[3] >= top - reach:
                    own.append(i); top, bot = min(top, l[1]), max(bot, l[3]); grown = True
        if len(own) == len(group): return group
        own.sort(); self.extended.add(own[0])
        return own

    def _paragraph_rects(self, outside):
        """The lines of detector paragraph boxes whose centre lies outside every rect in outside."""
        ix = sorted({i for b in self.boxes if b["role"] == "paragraph" for i in b["lines"]})
        return [self.lines[i][:4] for i in ix if not any(_centre_in(self.lines[i], r, 0) for r in outside)]

    def _items(self, caption_lines):
        """Snapping content: raster images and clustered drawings (clipped to the page, page-sized clip rects dropped)
        and text lines that are neither caption lines nor lines of a detector paragraph box."""
        W, H = self.W, self.H; out = []
        for r in list(self.page.images()) + list(self.page.cluster_drawings()):
            c = [max(0.0, r[0]), max(0.0, r[1]), min(W, r[2]), min(H, r[3])]
            if c[2] >= c[0] and c[3] >= c[1] and (c[2] - c[0]) * (c[3] - c[1]) <= PAGE_ITEM_MAX * W * H: out.append(c)
        para = {i for b in self.boxes if b["role"] == "paragraph" for i in b["lines"]}
        out += [l[:4] for i, l in enumerate(self.lines) if i not in caption_lines and i not in para]
        return out

    def bodies(self, captions, caption_lines):
        """Figure/table bodies for regex captions [(kind, caption_bbox, line_height)], in order: the snapped detector body,
        or None where no detector box pairs (the caller keeps its heuristic body). Always called once per page."""
        t0 = time.perf_counter()
        cands = []
        for ci, (kind, cap, lh) in enumerate(captions):
            col = self._column(cap)
            for bi, b in enumerate(self.boxes):
                r = b["bbox"]
                if b["role"] != kind: continue
                if _overlap(r, cap): cands.append((0, 0.0, -b["score"], ci, bi)); continue
                if not col[0] <= _cx(r) <= col[1]: continue
                if min(r[2], cap[2]) - max(r[0], cap[0]) < 0.5 * min(r[2] - r[0], cap[2] - cap[0]): continue
                above = r[3] <= cap[1]
                gap = cap[1] - r[3] if above else r[1] - cap[3]
                if gap > PAIR_GAP_LINES * lh and not self._empty_between(r, cap): continue
                cands.append((1 if above == (kind == "figure") else 2, round(gap, 1), -b["score"], ci, bi))
        seed, used = {}, set()
        for _, _, _, ci, bi in sorted(cands):
            if ci not in seed and bi not in used: seed[ci] = bi; used.add(bi)
        caps = [c[1] for c in captions]; items = None; out = []
        for ci, (kind, cap, lh) in enumerate(captions):
            if ci not in seed: out.append(None); continue
            r = list(self.boxes[seed[ci]]["bbox"])
            above = cap[1] - r[1] >= r[3] - cap[3]               # the body side of the caption
            if above: r[3] = min(r[3], cap[1])
            else: r[1] = max(r[1], cap[3])
            col = body_column(self.lines, self.W, self.columns, cap, r)
            r[0], r[2] = max(r[0], col[0]), min(r[2], col[1])
            if kind == "figure": r = self._chain(r, above, cap, caps, used, col)
            if items is None: items = self._items(caption_lines)
            out.append(self._snap(r, items, caps, col))
        self.unpaired = {bi for bi, b in enumerate(self.boxes) if b["role"] in ("figure", "table") and bi not in used}
        self.caption_rects = caps
        self.seconds += time.perf_counter() - t0
        return out

    def unpaired_bodies(self, caption_lines):
        """The unpaired body boxes (after bodies()), each snapped as a paired body is: [{"cls", "score", "bbox"}]."""
        items = self._items(caption_lines); caps = getattr(self, "caption_rects", [])
        return [{"cls": self.boxes[bi]["cls"], "score": self.boxes[bi]["score"],
                 "bbox": self._snap(list(self.boxes[bi]["bbox"]), items, caps, self._column(self.boxes[bi]["bbox"]))} for bi in sorted(self.unpaired)]

    def snap_box(self, r, caption_lines):
        """A box from another detector snapped like an unpaired body (Phase 6 A3.2)."""
        return self._snap(list(r), self._items(caption_lines), getattr(self, "caption_rects", []), self._column(r))

    def caption_box_top(self, i):
        """Line i is the top line of a detector caption box (figure_title, table_title, chart_title)."""
        return any(b["role"] == "caption" and b["lines"] and min(b["lines"], key=lambda j: (self.lines[j][1], self.lines[j][0])) == i for b in self.boxes)

    def caption_boxes(self, weak=False):
        """Caption-class box rects; weak: also the sub-threshold ones (Phase 6 in-box caption text only)."""
        return [b["bbox"] for b in self.boxes if b["role"] == "caption"] + (list(getattr(self, "weak_captions", [])) if weak else [])

    def claim(self, box, cap, caption_lines, commit=True):
        """bodies()' steps for one more caption and an unpaired box whose detector class does not give the caption's kind (stage B
        types it): cut at the caption, kept in its column, snapped. commit: the box is no longer unpaired. Returns the body rect."""
        r = list(box["bbox"])
        if cap[1] - r[1] >= r[3] - cap[3]: r[3] = min(r[3], cap[1])
        else: r[1] = max(r[1], cap[3])
        col = body_column(self.lines, self.W, self.columns, cap, r)
        r[0], r[2] = max(r[0], col[0]), min(r[2], col[1])
        if commit: self.unpaired.discard(next(i for i, b in enumerate(self.boxes) if b is box))
        return self._snap(r, self._items(caption_lines), getattr(self, "caption_rects", []), col)

    def widen(self, body, cap, caption_lines):
        """A body that body_column narrowed to one column although its caption spans both: the detector box it came from (the
        same-class box overlapping it most), cut at the caption and snapped across the page width (Phase 6 lexicon captions)."""
        boxes = [b for b in self.boxes if b["role"] in ("figure", "table") and _overlap(b["bbox"], body)]
        if not boxes: return body
        inter = lambda b: max(0.0, min(b[2], body[2]) - max(b[0], body[0])) * max(0.0, min(b[3], body[3]) - max(b[1], body[1]))
        r = list(max(boxes, key=lambda b: (inter(b["bbox"]), b["score"]))["bbox"])
        if cap[1] - r[1] >= r[3] - cap[3]: r[3] = min(r[3], cap[1])
        else: r[1] = max(r[1], cap[3])
        return self._snap(r, self._items(caption_lines), getattr(self, "caption_rects", []), (0.0, self.W))

    def adjacent_body(self, cap, lh, unpaired_only=False):
        """The body box (figure or table role) a caption of unknown kind would pair with under bodies()' limits, on either side:
        (box, side "above" | "below") of the smallest gap, then the higher score; None when none qualifies."""
        col = self._column(cap); best = None
        for bi, b in enumerate(self.boxes):
            r = b["bbox"]
            if b["role"] not in ("figure", "table") or not col[0] <= _cx(r) <= col[1] or (unpaired_only and bi not in self.unpaired): continue
            if _overlap(r, cap):                                  # a box over the caption pairs first in bodies() too
                key = (0.0, -b["score"]); above = cap[1] - r[1] >= r[3] - cap[3]
                if best is None or key < best[0]: best = (key, b, "above" if above else "below")
                continue
            if min(r[2], cap[2]) - max(r[0], cap[0]) < 0.5 * min(r[2] - r[0], cap[2] - cap[0]): continue
            above = r[3] <= cap[1] + BLOCK_SLACK
            gap = cap[1] - r[3] if above else r[1] - cap[3]
            if gap < -BLOCK_SLACK or (gap > PAIR_GAP_LINES * lh and not self._empty_between(r, cap)): continue
            key = (round(gap, 1), -b["score"])
            if best is None or key < best[0]: best = (key, b, "above" if above else "below")
        return best and best[1:]

    def _empty_between(self, r, cap):
        """No text line between box r and the caption cap (where they overlap horizontally): only whitespace separates them."""
        x0, x1 = max(r[0], cap[0]), min(r[2], cap[2])
        y0, y1 = (r[3], cap[1]) if r[3] <= cap[1] else (cap[3], r[1])
        return not any(y0 < (l[1] + l[3]) / 2 < y1 and l[0] < x1 and x0 < l[2] for l in self.lines)

    def _chain(self, body, above, cap, caps, used, col):
        """Multi-panel figures: further figure boxes on the same side and in the body's column, within CHAIN_GAP of the
        body, never across another caption or a paragraph."""
        others = [c for c in caps if c is not cap]
        grown = True
        while grown:
            grown = False
            for bi, b in enumerate(self.boxes):
                r = b["bbox"]
                if bi in used or b["role"] != "figure" or not col[0] <= _cx(r) <= col[1] or _gap(body, r) >= CHAIN_GAP: continue
                if (r[3] > cap[1] + BLOCK_SLACK) if above else (r[1] < cap[3] - BLOCK_SLACK): continue
                u = _union([body, r])
                if any(_overlap(u, c, BLOCK_SLACK) for c in others) or any(_overlap(u, p, BLOCK_SLACK) for p in self._paragraph_rects([body, r])): continue
                body = u; used.add(bi); grown = True
        return body

    def _snap(self, r, items, caps, col):
        """The detector body r snapped to the PDF content it covers inside its column col, kept off captions and paragraphs."""
        kept = [it for it in items if _inside_frac(it, r) >= 0.5 and col[0] <= _cx(it) <= col[1]]
        s = _union(kept) if kept else list(r)
        s = [max(s[0], r[0] - SNAP_OUTWARD, col[0]), max(s[1], r[1] - SNAP_OUTWARD, 0.0),
             min(s[2], r[2] + SNAP_OUTWARD, col[1]), min(s[3], r[3] + SNAP_OUTWARD, self.H)]
        for o in sorted(caps + self._paragraph_rects([r])):
            if _overlap(s, o, BLOCK_SLACK): s = _clear(s, o)
        return [round(v, 1) for v in s]

    def paragraphs(self, excluded):
        """Detector paragraph blocks (line index lists), highest score first, and every line no orphan paragraph may take.
        excluded: caption lines and lines inside figure / table bodies."""
        # a line in a heading / furniture / other box or an unpaired body box joins no paragraph, unless a paragraph box
        # that scores higher holds it (the model's per-class NMS leaves overlapping boxes of different classes)
        best_para = {}
        for b in self.boxes:
            if b["role"] == "paragraph":
                for i in b["lines"]: best_para[i] = max(best_para.get(i, 0.0), b["score"])
        blocked = set(excluded)
        for bi, b in enumerate(self.boxes):
            if b["role"] == "none" or bi in self.unpaired: blocked.update(i for i in b["lines"] if best_para.get(i, 0.0) <= b["score"])
        taken, out = set(), []
        for b in self.boxes:                                     # boxes are sorted by score
            if b["role"] != "paragraph": continue
            mine = [i for i in b["lines"] if i not in taken and i not in blocked]
            parts = [[i for i in mine if _cx(self.lines[i]) < self.W / 2], [i for i in mine if _cx(self.lines[i]) >= self.W / 2]] \
                if self.columns == 2 else [mine]
            for p in parts:
                if p: out.append(p); taken.update(p)
        return self._resegment(out), blocked | taken

    def _rows(self, block):
        """A block's text rows: line fragments sharing one line band (inline math, font changes), cut at gaps wider than
        three line heights. Each row is {"ix": line indexes, "r": rect, "words": words left to right}."""
        L = self.lines; rows = []
        for i in sorted(block, key=lambda i: (L[i][0], L[i][1])):
            l = L[i]; c = (l[1] + l[3]) / 2
            row = next((w for w in rows if w["r"][1] <= c <= w["r"][3] and max(l[0] - w["r"][2], w["r"][0] - l[2]) < 3 * (w["r"][3] - w["r"][1])), None)
            if row is None: rows.append({"ix": [i], "r": list(l[:4]), "words": l[4].split()})
            else: row["ix"].append(i); row["r"] = _union([row["r"], l]); row["words"] += l[4].split()
        return sorted(rows, key=lambda w: (w["r"][1], w["r"][0]))

    def _resegment(self, blocks):
        """Detector paragraphs cut and joined at typographic paragraph breaks. Inside a detector block a break is clearly
        wider line spacing, or a row that ended early (the next row's first word would have fit after it) followed by
        extra spacing or an indent. Two detector blocks with none of these cues between them are one paragraph."""
        rowed = [self._rows(b) for b in blocks]
        def median(vals): vals = sorted(vals); return vals[len(vals) // 2] if vals else None
        pairs = [(p, q) for rs in rowed for p, q in zip(rs, rs[1:]) if q["r"][1] - p["r"][1] > 0.5 * (p["r"][3] - p["r"][1])]
        pitch = median(q["r"][1] - p["r"][1] for p, q in pairs); pitch_low = median(q["r"][3] - p["r"][3] for p, q in pairs)
        if not pitch: return blocks
        def follows(p, q):                                   # row q comes right under row p in the same column
            a, b = p["r"], q["r"]
            return a[3] - 0.3 * (a[3] - a[1]) <= b[1] and b[1] - a[1] <= 1.6 * pitch and b[0] < a[2] and a[0] < b[2]
        def spaced(p, q, factor):                            # tops and bottoms both further apart than the usual pitch
            return q["r"][1] - p["r"][1] > factor * pitch and q["r"][3] - p["r"][3] > factor * pitch_low
        def cues(p, q, rows):                                # (p ended early, extra spacing, q indented)
            a, b = p["r"], q["r"]
            per_char = (b[2] - b[0]) / max(1, sum(len(w) + 1 for w in q["words"]))
            early = bool(q["words"]) and max(w["r"][2] for w in rows) - a[2] > per_char * (len(q["words"][0]) + 1)
            return early, spaced(p, q, SPACING_CUE), b[0] - min(w["r"][0] for w in rows) > 0.6 * (a[3] - a[1])
        def breaks(p, q, rows):
            early, extra, indent = cues(p, q, rows)
            return spaced(p, q, SPACING_BREAK) or (early and (extra or indent))
        cut = []
        for rows in rowed:                                   # each row continues the run it sits under, or starts one
            runs = []
            for q in rows:
                prev = max((r for r in runs if r[-1]["r"][1] < q["r"][1] and q["r"][0] < r[-1]["r"][2] and r[-1]["r"][0] < q["r"][2]),
                           key=lambda r: r[-1]["r"][1], default=None)
                if prev is not None and follows(prev[-1], q) and not breaks(prev[-1], q, rows): prev.append(q)
                else: runs.append([q])
            cut += runs
        cut.sort(key=lambda run: (run[0]["r"][1], run[0]["r"][0]))
        out = []
        for run in cut:
            top = run[0]["r"]
            prev = next((a for a in out if follows(a[-1], run[0]) and not any(
                a[-1]["r"][3] <= (l[1] + l[3]) / 2 <= top[1] and l[0] < top[2] and top[0] < l[2] for l in self.lines)), None)
            if prev and not any(cues(prev[-1], run[0], prev + run)): prev += run
            else: out.append(list(run))
        return [sorted(i for row in run for i in row["ix"]) for run in out]

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "fetch": sys.exit(fetch_model(sys.argv[2]))
    print("usage: layoutdet.py fetch MODELS_DIR", file=sys.stderr); sys.exit(64)
