"""Block classifier (docs/contracts/phase6-block-classifier.md): what kind a page region is when no standard caption says.
Stage B: Docling DocumentFigureClassifier-v2.5 (IBM / docling-project, MIT; EfficientNet-B0, 26 classes, ONNX) on onnxruntime CPU,
run inside the doc-venv parse on detector body boxes without a caption. Stage C: the local vision model's fixed-option letter pick,
in a background kind job (`blockcls.py job`) that writes cache/doc/<parse key>.kinds.json once. `blockcls.py capture` types the
regions of a Read-area capture that has no document page. The OCR helper (`speakeasy-track ocr IN.png OUT.json`) gives text for
regions without a text layer. numpy / onnxruntime / Pillow are imported lazily, so the main process imports this module with the
stdlib only. Logged records hold numbers, enums, hashes and geometry, never text."""
import base64, hashlib, json, math, os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

MODEL_FILE = "docling_figure_classifier_v2_5.onnx"
MODEL_URL = ("https://huggingface.co/docling-project/DocumentFigureClassifier-v2.5/resolve/"
             "f859dfbff5c9916cd996942d4b0db7fa25808220/model.onnx")
MODEL_SIZE = 16940439
MODEL_SHA256 = "27ffc48c27ae4e12c99b6f6de0dd730005245e47b70dd0c1339e62cbac3ec4c0"
CLASSIFIER_MODEL = "docling-figcls-v2.5"
CLASSIFIER_VERSION = CLASSIFIER_MODEL + "/" + MODEL_SHA256[:12]

LABELS = ("logo", "photograph", "icon", "engineering_drawing", "line_chart", "bar_chart", "other", "table", "flow_chart",
          "screenshot_from_computer", "signature", "screenshot_from_manual", "geographical_map", "pie_chart", "page_thumbnail",
          "stamp", "music", "calendar", "qr_code", "bar_code", "full_page_image", "scatter_plot", "chemistry_structure",
          "topographical_map", "crossword_puzzle", "box_plot")                     # the model's id2label order
FAMILY = {"table": "table",
          **dict.fromkeys(("photograph", "engineering_drawing", "line_chart", "bar_chart", "flow_chart", "geographical_map", "pie_chart",
                           "scatter_plot", "chemistry_structure", "topographical_map", "box_plot"), "figure"),
          **dict.fromkeys(("logo", "icon", "signature", "stamp", "qr_code", "bar_code"), "decoration"),
          **dict.fromkeys(("other", "screenshot_from_computer", "screenshot_from_manual", "page_thumbnail", "full_page_image", "music",
                           "calendar", "crossword_puzzle"), "textish")}
DETECTOR_FAMILY = {"image": "figure", "chart": "figure", "table": "table", "picture": "figure"}     # PP-DocLayout-S and heron body classes

INPUT_SIZE, CROP_ZOOM, CROP_MARGIN = 224, 2.0, 4.0
MEAN, STD = (0.485, 0.456, 0.406), (0.47853944, 0.4732864, 0.47434163)
THREADS = 2                      # fixed, never from the environment: two parses of one file give identical decisions
T_B = 0.50                       # section 13 rule on the public gap set (gated): the smallest grid value with >= 10 accepted at >= 95 % precision
T_C = 0.95                       # provisional until the verifier's public live sweep (Amendment 2 A2.2a)
VLM_DEFAULT = "off"              # stage C ships default-off until T_C is frozen (A2.2)
KIND_CROP_PX = 448               # stage C crop long side, fixed before the sweep (section 0: no difference across 448-1024)
OCR_ZOOM = 3.0

MIN_SIDE_PT, MIN_PAGE_FRAC, MAX_PAGE_FRAC, COVER_FRAC = 24.0, 0.01, 0.9, 0.3
DUPLICATE_IOU, CONTAINER_FRAC = 0.5, 0.9     # A2.4: one object under two classes; a box around two or more candidates
PAGE_CAP, DOC_CAP = 8, 32                     # stage B candidates per page / per parse
JOB_PAGE_CAP, JOB_CAP = 4, 16                 # stage C requests per page / per job
REQUEST_DEADLINE, JOB_DEADLINE, OCR_TIMEOUT = 8.0, 60.0, 20.0
RERUN_UNAVAILABLE_S = 600
KIND_LETTERS = "ABCDE"
LETTER_KIND = {"A": "figure", "B": "figure", "C": "table", "D": "text", "E": "none"}
OFF = ("off", "0", "false", "no")

# ---------------- settings ----------------
def home():
    return os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy")

def setting(name, default):
    """name from the environment, else from $SPEAKEASY_HOME/config (a direct parse-pdf run reads no config), else default."""
    value = os.environ.get(name)
    if value is None:
        cfg = os.path.join(home(), "config")
        if os.path.isfile(cfg):
            for line in open(cfg, encoding="utf-8"):
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == name: value = v.strip().strip('"').strip("'")
    return (value or default).strip().lower()

def blockcls_off():
    """SPEAKEASY_BLOCKCLS=off: all of Phase 6 off, the lexicon included (A1.3)."""
    return setting("SPEAKEASY_BLOCKCLS", "on") in OFF

def vlm_mode():
    """auto | local | off (default VLM_DEFAULT). SPEAKEASY_GROUND=off also turns stage C off."""
    mode = setting("SPEAKEASY_BLOCKCLS_VLM", VLM_DEFAULT)
    if mode in OFF or setting("SPEAKEASY_GROUND", "auto") in OFF: return "off"
    return "local" if mode == "local" else "auto"

def disabled_record():
    """The one key a SPEAKEASY_BLOCKCLS=off parse adds (A1.3)."""
    return {"status": "unavailable", "classifier_version": None, "reason": "disabled", "T_B": T_B, "T_C": T_C, "kind_job": "none", "routed": 0}

# ---------------- model install and lookup (as Phase 4 section 1) ----------------
def model_path():
    """SPEAKEASY_FIGCLS_MODEL when set (no fall-through), else $SPEAKEASY_HOME/models, else the app bundle's Resources/models."""
    override = os.environ.get("SPEAKEASY_FIGCLS_MODEL")
    if override: return override if os.path.isfile(override) else None
    for p in (os.path.join(home(), "models", MODEL_FILE), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", MODEL_FILE)):
        if os.path.isfile(p): return p
    return None

def model_verified(path):
    import layoutdet
    return os.path.getsize(path) == MODEL_SIZE and layoutdet.file_sha256(path) == MODEL_SHA256

def fetch_model(models_dir):
    """Installer step (exit 0 ok, 1 checksum mismatch, 2 download failure); SPEAKEASY_FIGCLS_MODEL_URL overrides the source."""
    import layoutdet
    return layoutdet.fetch_verified(models_dir, MODEL_FILE, os.environ.get("SPEAKEASY_FIGCLS_MODEL_URL") or MODEL_URL, MODEL_SIZE, MODEL_SHA256,
                                    "Block classifier model", "block classifier off")

# ---------------- stage B ----------------
def route(det_family, label, p_top, available=True):
    """Contract 5 routing (first match wins): (outcome, route). An unavailable classifier routes everything as low-p."""
    if not available: return "pending", "low-p"
    fam = FAMILY[label]
    if fam == "decoration": return "rejected", None
    if fam == "textish": return "pending", "textish"
    if fam != det_family: return "pending", "disagree"
    if p_top < T_B: return "pending", "low-p"
    return "accepted", None

class Classifier:
    """Stage B, checked and loaded on first use, reused for the rest of the parse. Reasons as layoutdet's."""
    def __init__(self):
        self.session, self.reason, self.error, self.loaded = None, None, None, False

    def ready(self):
        if self.loaded: return self.session is not None
        self.loaded = True
        try:
            import numpy as np, onnxruntime as ort
        except ImportError as e:
            self.reason, self.error = "no-onnxruntime", e; return False
        self.np = np
        path = model_path()
        if path is None: self.reason = "model-missing"; return False
        if not model_verified(path): self.reason = "model-mismatch"; return False
        try:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads, opts.inter_op_num_threads, opts.log_severity_level = THREADS, 1, 3
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self.session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
            self.input_name = self.session.get_inputs()[0].name
        except Exception as e:                      # any load error: candidates route to stage C, the parse never fails
            self.reason, self.error = "load-failed", e
        if self.reason: print(f"blockcls: classifier unavailable ({self.reason})", file=sys.stderr)
        return self.session is not None

    def status(self):
        self.ready()
        if self.reason: return {"status": "unavailable", "classifier_version": CLASSIFIER_VERSION if self.reason == "load-failed" else None, "reason": self.reason}
        return {"status": "ok", "classifier_version": CLASSIFIER_VERSION, "reason": None}

    def classify(self, img):
        """(label, p_top, p_margin) of a PIL image."""
        from PIL import Image
        np = self.np
        x = np.asarray(img.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR), dtype=np.float32) / 255.0
        x = ((x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)).transpose(2, 0, 1)[None].astype(np.float32)
        logits = self.session.run(None, {self.input_name: x})[0][0].astype(np.float64)
        p = np.exp(logits - logits.max()); p /= p.sum()
        order = np.argsort(-p, kind="stable")
        return LABELS[int(order[0])], float(p[order[0]]), float(p[order[0]] - p[order[1]])

def crop_clip(rect, W, H, margin=CROP_MARGIN):
    return [max(0.0, rect[0] - margin), max(0.0, rect[1] - margin), min(W, rect[2] + margin), min(H, rect[3] + margin)]

def r1(r):
    return [round(v, 1) for v in r]

def event(page, rect, anchor, det_cls, det_score, stage="classifier", **kw):
    """A block_kind record (9.2): numbers, enums and geometry only."""
    ev = {"event": "block_kind", "page": page, "rect": r1(rect), "anchor": anchor, "det_cls": det_cls,
          "det_score": None if det_score is None else round(det_score, 2), "cls_label": None, "cls_p": None, "route": None, "stage": stage,
          "kind": None, "p_top": None, "p_margin": None, "outcome": None, "ms": None,
          "model": CLASSIFIER_MODEL if stage == "classifier" else None, "provider": None, "confidence": None}
    ev.update(kw)
    return ev

class Session:
    """One per parse: the kill switch, stage B, the candidate caps and the block_kind records."""
    def __init__(self):
        self.on = not blockcls_off()
        self.classifier = Classifier()
        self.events, self.n_candidates = [], 0
        self.vlm = vlm_mode() if self.on else "off"
        self.ocr_bin = ocr_helper() if self.on else None
        self.det2, self.layout2_routed, self.layout2_ms = Detector2(), 0, []

    def available(self):
        return self.classifier.ready()

    def stage_b(self, page, rect, W, H):
        """(label, p_top, p_margin, ms) for a page region (rendered at CROP_ZOOM with CROP_MARGIN), or None when stage B is unavailable."""
        if not self.available(): return None
        t0 = time.perf_counter()
        try:
            label, p_top, p_margin = self.classifier.classify(page.image(crop_clip(rect, W, H), CROP_ZOOM))
        except Exception as e:                      # a failed region routes alone; the rest keep the classifier
            print(f"blockcls: classify failed ({type(e).__name__})", file=sys.stderr); return None
        return label, p_top, p_margin, int(round((time.perf_counter() - t0) * 1000))

    def _skip(self, n, c, anchor, kinds, reason, **kw):
        kinds["skipped"][reason] = kinds["skipped"].get(reason, 0) + 1
        self.events.append(event(n, c["bbox"], anchor, c["cls"], c["score"], outcome="skipped", skip=reason, **kw))

    def classify_candidates(self, n, page, cands, W, H, anchor, dropped=()):
        """Stage B and routing for one page's candidates [{"cls", "score", "bbox"}] in reading order. Returns (accepted
        [(kind, cand)], kinds record); each candidate (and each filter drop [(cand, reason)]) gets one block_kind record.
        Routed candidates stay pending for the kind job (pending also while stage C is off: no job starts then); on an area
        capture they are skipped (area-no-vlm, A2.5)."""
        kinds = {"candidates": 0, "accepted": {"figure": 0, "table": 0}, "routed": 0, "rejected": 0, "skipped": {}, "ms": 0}
        accepted = []
        tag = lambda c: {"det": c["det"]} if c.get("det") else {}
        for c, reason in dropped: self._skip(n, c, c.get("anchor", anchor), kinds, reason, **tag(c))
        for k, c in enumerate(cands):
            a = c.get("anchor", anchor)
            if k >= PAGE_CAP or self.n_candidates >= DOC_CAP:
                self._skip(n, c, a, kinds, "over-cap", **tag(c)); continue
            self.n_candidates += 1; kinds["candidates"] += 1
            res = self.stage_b(page, c["bbox"], W, H)
            label, p_top, p_margin, ms = res or (None, None, None, 0)
            kinds["ms"] += ms
            det_family = DETECTOR_FAMILY.get(c["cls"]) or (FAMILY[label] if res else None)      # ink regions: stage B's family (A3.3)
            outcome, why = route(det_family, label, p_top, res is not None)
            ev = event(n, c["bbox"], a, c["cls"], c["score"], cls_label=label, cls_p=None if p_top is None else round(p_top, 2), route=why,
                       p_top=None if p_top is None else round(p_top, 2), p_margin=None if p_margin is None else round(p_margin, 2), ms=ms,
                       model=CLASSIFIER_MODEL if res else None, **tag(c))
            if outcome == "accepted":
                ev.update(kind=det_family, outcome="accepted"); kinds["accepted"][det_family] += 1; accepted.append((det_family, c))
            elif outcome == "rejected":
                ev.update(kind="none", outcome="rejected"); kinds["rejected"] += 1
            else:
                kinds["routed"] += 1
                if a == "area": ev.update(outcome="skipped", skip="area-no-vlm"); kinds["skipped"]["area-no-vlm"] = kinds["skipped"].get("area-no-vlm", 0) + 1
                else: ev["outcome"] = "pending"
            self.events.append(ev)
        return accepted, kinds

    def body_family(self, n, page, rect, W, H, det_cls, det_score, anchor="ambiguous-caption"):
        """4.2 / A2.4: stage B's family for the body of a lexicon caption when it is not textish or decoration and p_top >= T_B,
        else None."""
        res = self.stage_b(page, rect, W, H)
        if res is None:                             # stage B unavailable: recorded, never queued for stage C (a caption exists)
            self.events.append(event(n, rect, anchor, det_cls, det_score, route="low-p", outcome="unavailable", model=None, ms=0))
            return None
        label, p_top, p_margin, ms = res
        fam = FAMILY[label] if FAMILY[label] in ("figure", "table") and p_top >= T_B else None
        ev = event(n, rect, anchor, det_cls, det_score, cls_label=label, cls_p=round(p_top, 2), p_top=round(p_top, 2),
                   p_margin=round(p_margin, 2), kind=fam, outcome="accepted" if fam else "low-confidence", ms=ms)
        if anchor == "lexicon-caption": ev["kind_src"] = "lexicon"
        self.events.append(ev)
        return fam

    def routed_pages(self):
        return sorted({e["page"] for e in self.events if e.get("outcome") == "pending"})

    def top(self, covered_pages=()):
        """A2.5: the kind job is queued when stage C is on and routed candidates sit on pages an existing sidecar does not cover."""
        if not self.on: return disabled_record()
        st = self.classifier.status()
        routed = sum(e.get("outcome") == "pending" for e in self.events)
        uncovered = set(self.routed_pages()) - set(covered_pages)
        return {"status": st["status"], "classifier_version": st["classifier_version"], "reason": st["reason"], "T_B": T_B, "T_C": T_C,
                "kind_job": "queued" if uncovered and self.vlm != "off" else "none", "routed": routed}

# ---------------- candidate filter (contract 3) ----------------
def _area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])

def _inter(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))

def _iou(a, b):
    i = _inter(a, b); u = _area(a) + _area(b) - i
    return i / u if u > 0 else 0.0

def filter_candidates(bodies, captioned, paragraphs, W, H, pt=1.0):
    """Section 3 on unpaired detector bodies [{"cls", "score", "bbox"}]: not covering >= 30 % of a captioned body or a paragraph
    block, not smaller than 24 pt on a side or 1 % of the page, not >= 90 % of the page (pt: units per point, capture pixels for an
    area capture). Then (A2.4) one object under two classes keeps the higher score (IoU >= 0.5; ties: the earlier in reading
    order) and a box holding two or more other candidates (each >= 90 % inside) is their container. Returns (candidates in
    reading order, dropped [(body, "duplicate" | "container")])."""
    import docmap
    page = W * H
    kept = [b for b in bodies if min(b["bbox"][2] - b["bbox"][0], b["bbox"][3] - b["bbox"][1]) >= MIN_SIDE_PT * pt
            and MIN_PAGE_FRAC * page <= _area(b["bbox"]) < MAX_PAGE_FRAC * page
            and not any(_inter(b["bbox"], o) >= COVER_FRAC * max(1.0, _area(o)) for o in list(captioned) + list(paragraphs))]
    order = {id(b): k for k, b in enumerate(kept[i] for i in docmap.rect_order([b["bbox"] for b in kept]))}
    out, dropped = [], []
    for b in sorted(kept, key=lambda b: (-b["score"], order[id(b)])):
        if any(_iou(b["bbox"], k["bbox"]) >= DUPLICATE_IOU for k in out): dropped.append((b, "duplicate"))
        else: out.append(b)
    inside = lambda o, r: _inter(o, r) >= CONTAINER_FRAC * max(1.0, _area(o))
    containers = [b for b in out if sum(inside(o["bbox"], b["bbox"]) for o in out if o is not b) >= 2]
    dropped += [(b, "container") for b in containers]
    out = [b for b in out if not any(b is c for c in containers)]
    return sorted(out, key=lambda b: order[id(b)]), sorted(dropped, key=lambda d: order[id(d[0])])

# ---------------- OCR helper (A1.1) ----------------
def ocr_helper():
    """A2.6, first match wins: SPEAKEASY_OCR_BIN when set (no fall-through), else SPEAKEASY_TRACKER, else
    $SPEAKEASY_HOME/bin/speakeasy-track. None (OCR unavailable) unless that path is an executable file."""
    p = os.environ.get("SPEAKEASY_OCR_BIN")
    if p is None: p = os.environ.get("SPEAKEASY_TRACKER") or os.path.join(home(), "bin", "speakeasy-track")
    return p if p and os.path.isfile(p) and os.access(p, os.X_OK) else None

def _rect_ok(r, size):
    return (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r)
            and 0 <= r[0] < r[2] <= size[0] + 0.05 and 0 <= r[1] < r[3] <= size[1] + 0.05)

def ocr_valid(out):
    """True when OUT.json follows the A1.1 schema closely enough to use (rects inside image_size, text strings)."""
    if not isinstance(out, dict) or out.get("version") != 1 or out.get("unit") != "px" or out.get("origin") != "top-left": return False
    size = out.get("image_size")
    if not (isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and v > 0 for v in size)): return False
    lines, tables = out.get("lines"), out.get("tables")
    if not isinstance(lines, list) or not isinstance(tables, list): return False
    if not all(isinstance(l, dict) and _rect_ok(l.get("rect"), size) and isinstance(l.get("text"), str) for l in lines): return False
    return all(isinstance(t, dict) and isinstance(t.get("rows"), list) and all(isinstance(r, dict) and _rect_ok(r.get("rect"), size)
               and isinstance(r.get("cells"), list) and all(isinstance(c, dict) and isinstance(c.get("text"), str) for c in r["cells"]) for r in t["rows"])
               for t in tables)

def run_ocr(helper, png, timeout=OCR_TIMEOUT):
    """The helper's OUT.json for png, or None (non-zero exit, no file, bad schema: OCR failed). OUT.json goes to TMPDIR and is
    deleted in finally."""
    out = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_ocr_{os.getpid()}_{os.path.basename(png)}.json")
    try:
        r = subprocess.run([helper, "ocr", png, out], capture_output=True, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(out): return None
        with open(out, encoding="utf-8") as f: data = json.load(f)
        return data if ocr_valid(data) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        try: os.remove(out)
        except OSError: pass

def ocr_region(helper, page, rect, W, H, tag):
    """OCR of a page region (+ 4 pt, at OCR_ZOOM) through the helper: {"lines": [[x0, y0, x1, y1, text]], "tables": [{"rows":
    [{"rect", "cells": [text]}]}]} in page points (clip origin + px / zoom, 0.1 pt), or None when OCR failed."""
    if not helper: return None
    clip = crop_clip(rect, W, H)
    png = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_ocrin_{os.getpid()}_{tag}.png")
    try:
        page.image(clip, OCR_ZOOM).save(png, format="PNG")
        data = run_ocr(helper, png)
    except (OSError, ValueError):
        data = None
    finally:
        try: os.remove(png)
        except OSError: pass
    if data is None: return None
    to_pt = lambda r: [round(clip[0] + r[0] / OCR_ZOOM, 1), round(clip[1] + r[1] / OCR_ZOOM, 1), round(clip[0] + r[2] / OCR_ZOOM, 1), round(clip[1] + r[3] / OCR_ZOOM, 1)]
    return {"lines": [to_pt(l["rect"]) + [" ".join(l["text"].split())] for l in data["lines"] if l["text"].strip()],
            "tables": [{"rows": [{"rect": to_pt(r["rect"]), "cells": [c["text"] for c in r["cells"]]} for r in t["rows"]]} for t in data["tables"]],
            "engine": data.get("engine")}

def ocr_rows(ocr, table_rect):
    """Section 8 rows of an image table from its OCR: RecognizeDocumentsRequest rows first (ocr-table), else the OCR lines grouped
    by docmap.table_rows (ocr-lines). Returns (rows [{"rect", "tokens"}], rows_src); tokens are match tokens, never raw text."""
    import docmap
    from textfold import match_tokens
    if ocr is None: return [], "none"
    tables = [t for t in ocr["tables"] if t["rows"]]
    if tables:
        t = max(tables, key=lambda t: len(t["rows"]))
        rows = [{"rect": [min(r["rect"][0], table_rect[0]), r["rect"][1], max(r["rect"][2], table_rect[2]), r["rect"][3]],
                 "tokens": sorted(set(match_tokens(" ".join(r["cells"]))))} for r in t["rows"]]
        return rows, "ocr-table"
    rows = docmap.table_rows(ocr["lines"], table_rect)
    if not rows: return [], "none"
    return [{"rect": r1(r["rect"]), "tokens": sorted(r["toks"])} for r in rows], "ocr-lines"

# ---------------- stage C (contract 6) ----------------
def kind_decide(reply):
    """Phase 5 4.5 letter rules (uppercase letters only, "A" and " A" merged, letter mass < 0.5 off-menu, normalised over the valid
    letters), then p(figure) = p_A + p_B, p(table) = p_C, p(text) = p_D, p(none) = p_E; p_top / p_margin over the four kinds.
    Returns {outcome, kind, p_top, p_margin, letter_mass}; outcome accepted | text | low-confidence | off-menu | no-logprobs."""
    out = {"outcome": "no-logprobs", "kind": None, "p_top": None, "p_margin": None, "letter_mass": None}
    lp = (reply or {}).get("logprobs") or []
    top = (lp[0] or {}).get("top_logprobs") if lp and isinstance(lp[0], dict) else None
    if not top: return out
    q = {}
    for e in top:
        tok = (e.get("token") or "").strip()
        if len(tok) == 1 and tok in KIND_LETTERS: q[tok] = q.get(tok, 0.0) + math.exp(e["logprob"])
    mass = sum(q.values())
    out["letter_mass"] = round(mass, 2)
    if mass < 0.5: return dict(out, outcome="off-menu")
    kinds = {}
    for letter, v in q.items(): kinds[LETTER_KIND[letter]] = kinds.get(LETTER_KIND[letter], 0.0) + v / mass
    ranked = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
    p_top = ranked[0][1]; margin = p_top - (ranked[1][1] if len(ranked) > 1 else 0.0)
    out.update(kind=ranked[0][0], p_top=round(p_top, 2), p_margin=round(margin, 2))
    if ranked[0][0] in ("figure", "table") and p_top >= T_C: out["outcome"] = "accepted"
    elif ranked[0][0] == "text" and p_top >= T_C: out["outcome"] = "text"
    else: out["outcome"] = "low-confidence"
    return out

def prompt_sha():
    from prompts import KIND_PROMPT
    return hashlib.sha256(KIND_PROMPT.encode()).hexdigest()

def entry_key(pdf_sha, page, rect, model, model_digest, prompt_hash):
    """The sidecar key of one candidate (6.1): PARSE_VERSION, PDF sha256, page, rect at 0.1 pt, model, model digest, KIND_PROMPT sha."""
    import docmap
    parts = [str(docmap.PARSE_VERSION), pdf_sha, str(page), ",".join(f"{v:.1f}" for v in rect), model, model_digest or "", prompt_hash]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]

def kind_png(page, rect, W, H, out_png):
    """The stage C crop: the candidate + 4 pt, long side KIND_CROP_PX. Deterministic bytes (no metadata)."""
    clip = crop_clip(rect, W, H)
    from PIL import Image
    w, h = clip[2] - clip[0], clip[3] - clip[1]
    img = page.image(clip, KIND_CROP_PX / max(1.0, w, h))
    size = (KIND_CROP_PX, max(1, round(KIND_CROP_PX * h / w))) if w >= h else (max(1, round(KIND_CROP_PX * w / h)), KIND_CROP_PX)
    if img.size != size: img = img.resize(size, Image.BILINEAR)      # the renderer rounds the scaled clip by a pixel
    img.save(out_png, format="PNG")

def request_body(model, keep_alive, png_b64):
    from prompts import KIND_PROMPT
    return {"model": model, "stream": False, "keep_alive": keep_alive,
            "messages": [{"role": "user", "content": KIND_PROMPT, "images": [png_b64]}],
            "options": {"temperature": 0, "num_predict": 1}, "logprobs": True, "top_logprobs": 20}

def openrouter_body(model, png_b64):
    from prompts import KIND_PROMPT
    return {"model": model, "temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": 20, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "text", "text": KIND_PROMPT},
                                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64}}]}]}

def ollama_base():
    from selog import DEFAULT_URL
    u = os.environ.get("SPEAKEASY_LLM_URL", DEFAULT_URL).rstrip("/")
    return u[:-3] if u.endswith("/v1") else u

def post_json(url, body, headers, timeout):
    import urllib.request
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=max(0.05, timeout)) as r: return json.loads(r.read().decode())

def model_digest(model, timeout=3.0):
    """The local model's digest from /api/tags, or None (Ollama down, model not pulled)."""
    import urllib.request
    try:
        with urllib.request.urlopen(ollama_base() + "/api/tags", timeout=timeout) as r: tags = json.loads(r.read().decode())
    except (OSError, ValueError):
        return None
    return next((m.get("digest") for m in tags.get("models") or [] if model in (m.get("name"), m.get("model"))), None)

# ---------------- shared vision-model state with the Phase 5 picker (6.1 precedence) ----------------
def busy_path(): return os.path.join(home(), "vlm_busy")
def breaker_path(): return os.path.join(home(), "vlm_breaker.json")

def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0: return False
    try: os.kill(pid, 0)
    except PermissionError: return True
    except OSError: return False
    return True

def pick_busy():
    """A live run's pick is queued or in flight (grounding.py writes vlm_busy with its pid)."""
    try:
        with open(busy_path()) as f: return _pid_alive(int(f.read().strip() or 0))
    except (OSError, ValueError):
        return False

def shared_breaker(since):
    """The picker breaker ("unavailable" / "no-logprobs") one of the runs or jobs tripped at or after since, else None."""
    try:
        with open(breaker_path()) as f: b = json.load(f)
        return b.get("breaker") if b.get("ts", 0) >= since and b.get("breaker") in ("unavailable", "no-logprobs") else None
    except (OSError, ValueError, AttributeError):
        return None

def trip_shared_breaker(outcome):
    try:
        tmp = breaker_path() + f".{os.getpid()}.tmp"
        with open(tmp, "w") as f: json.dump({"breaker": outcome, "ts": time.time(), "pid": os.getpid()}, f)
        os.replace(tmp, breaker_path())
    except OSError:
        pass

# ---------------- the kind job (6.1, A1.2) ----------------
def sidecar_path(cache_json): return cache_json[:-5] + ".kinds.json"
def lock_path(cache_json): return cache_json[:-5] + ".kinds.lock"

def log_records(recs):
    """Append records to logs/blockcls.jsonl (rotated like the other logs). Never raises."""
    try:
        import selog
        d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True); p = os.path.join(d, "blockcls.jsonl")
        selog.rotate(p)
        with open(p, "a", encoding="utf-8") as f:
            for r in recs: f.write(json.dumps(r) + "\n")
    except (OSError, TypeError, ValueError):
        pass

def read_sidecar(cache_json):
    """The kind sidecar of a parse cache entry, or None (absent or unreadable)."""
    try:
        with open(sidecar_path(cache_json)) as f: s = json.load(f)
        return s if isinstance(s, dict) else None
    except (OSError, ValueError):
        return None

def covered_pages(side):
    return set(side.get("pages") or []) if side else set()

def rerun_due(side):
    """A1.2: a failed + unavailable sidecar may be re-run at most once per 10 minutes."""
    return bool(side) and side.get("state") == "failed" and side.get("breaker") == "unavailable" and time.time() - (side.get("done_ts") or 0) >= RERUN_UNAVAILABLE_S

def pending_pages(parse):
    return {e["page"] for e in (parse or {}).get("blockcls_events") or [] if e.get("outcome") == "pending" and e.get("stage") == "classifier"}

def job_should_start(cache_json, parse):
    """A1.2 / A2.5: a queued parse whose routed pages the sidecar does not cover yet (or a re-run that is due), with no live lock."""
    if ((parse or {}).get("blockcls") or {}).get("kind_job") != "queued": return False
    side = read_sidecar(cache_json)
    if side is None and os.path.exists(sidecar_path(cache_json)): return False          # unreadable: never overwritten blindly
    if side is not None and pending_pages(parse) <= covered_pages(side) and not rerun_due(side): return False
    try:
        with open(lock_path(cache_json)) as f:
            if _pid_alive(int(f.read().strip() or 0)): return False
    except (OSError, ValueError):
        pass
    return True

def start_job(pdf, cache_json, python):
    """Start the detached kind job for one parse (one per parse key); returns True when it started."""
    try:
        p = subprocess.Popen([python, os.path.realpath(__file__), "job", pdf, cache_json], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        with open(lock_path(cache_json), "w") as f: f.write(str(p.pid))
        return True
    except OSError:
        return False

class Job:
    """Stage C for one parse's routed candidates: one request at a time, picks first, breakers shared with the picker."""
    def __init__(self, pdf, cache_json):
        self.pdf, self.cache_json = pdf, cache_json
        self.t0 = time.time(); self.deadline = self.t0 + JOB_DEADLINE
        self.entries, self.records, self.breaker, self.error = [], [], None, None
        self.prev, self.pages = read_sidecar(cache_json), []     # an earlier job's sidecar: its entries survive this job (A2.5)
        self.model = os.environ.get("SPEAKEASY_VISION_MODEL") or "qwen3-vl:4b-instruct"
        self.keep_alive = os.environ.get("SPEAKEASY_KEEP_ALIVE", "10m")
        self.mode = vlm_mode()
        self.transport = post_json

    def _openrouter(self):
        if self.mode != "auto" or setting("SPEAKEASY_GROUND", "auto") != "openrouter": return None
        import provider
        return provider.cloud("ground")

    def ask(self, ev, b64):
        """One candidate's stage C answer: (decision dict, provider, confidence)."""
        if self.breaker is None:
            while pick_busy() and time.time() < self.deadline: time.sleep(0.1)
            self.breaker = self.breaker or shared_breaker(self.t0)
        if time.time() >= self.deadline: return {"outcome": "skipped"}, None, None
        req_deadline = min(self.deadline, time.time() + REQUEST_DEADLINE)
        if self.breaker is None:
            try:
                reply = self.transport(ollama_base() + "/api/chat", request_body(self.model, self.keep_alive, b64), {}, req_deadline - time.time())
            except TimeoutError:
                return {"outcome": "timeout"}, "local", None
            except OSError as e:
                if isinstance(getattr(e, "reason", None), TimeoutError): return {"outcome": "timeout"}, "local", None
                code = getattr(e, "code", None)
                if code is not None and code != 404 and code < 500: return {"outcome": "error", "error": type(e).__name__}, "local", None
                self.breaker = "unavailable"; trip_shared_breaker("unavailable")
                reply = None
            except (ValueError, UnicodeDecodeError) as e:
                return {"outcome": "error", "error": type(e).__name__}, "local", None
            if reply is not None:
                if time.time() > req_deadline: return {"outcome": "timeout"}, "local", None
                d = kind_decide(reply)
                if d["outcome"] != "no-logprobs": return d, "local", "logprob"
                self.breaker = "no-logprobs"; trip_shared_breaker("no-logprobs")
        cloud = self._openrouter()
        if not cloud: return {"outcome": self.breaker}, "local", None
        url, headers, model = cloud
        try: reply = self.transport(url + "/chat/completions", openrouter_body(model, b64), headers, req_deadline - time.time())
        except (OSError, ValueError, UnicodeDecodeError):
            return {"outcome": "unavailable"}, "openrouter", None
        choice = ((reply or {}).get("choices") or [{}])[0]
        top = (((choice.get("logprobs") or {}).get("content") or [{}])[0] or {}).get("top_logprobs")
        if top: return kind_decide({"logprobs": [{"top_logprobs": top}]}), "openrouter", "logprob"
        text = ((choice.get("message") or {}).get("content") or "").lstrip()
        kind = LETTER_KIND.get(text[:1]) if text[:1] else None
        family = FAMILY.get(ev.get("cls_label") or "", DETECTOR_FAMILY.get(ev.get("det_cls")))
        if kind in ("figure", "table") and kind == family:
            return {"outcome": "accepted", "kind": kind, "p_top": None, "p_margin": None}, "openrouter", "letter-only"
        return {"outcome": "low-confidence" if kind else "off-menu", "kind": kind, "p_top": None, "p_margin": None}, "openrouter", None

    def run(self):
        import pdfpage
        with open(self.cache_json) as f: parse = json.load(f)
        covered = set() if rerun_due(self.prev) else covered_pages(self.prev)
        routed = [e for e in parse.get("blockcls_events") or [] if e.get("outcome") == "pending" and e.get("stage") == "classifier"
                  and e.get("page") not in covered]
        self.pages = sorted({e["page"] for e in routed})
        with open(self.pdf, "rb") as f: pdf_sha = hashlib.sha256(f.read()).hexdigest()
        digest, phash = model_digest(self.model), prompt_sha()
        doc = pdfpage.open_pdf(self.pdf); per_page = {}
        for k, ev in enumerate(routed):
            rec = dict(ev, stage="vlm", model=self.model, ms=None, provider=None, confidence=None, kind=None, p_top=None, p_margin=None)
            per_page[ev["page"]] = per_page.get(ev["page"], 0) + 1
            if per_page[ev["page"]] > JOB_PAGE_CAP or k >= JOB_CAP or time.time() >= self.deadline or (self.breaker and not self._openrouter()):
                rec["outcome"] = "skipped"; rec["skip"] = "breaker" if self.breaker else "budget"; self._keep(rec, pdf_sha, digest, phash); continue
            png = os.path.join(tempfile.gettempdir(), f"speakeasy_cap_kind_{os.getpid()}_{k}.png")
            t0 = time.time()
            try:
                page = doc[ev["page"] - 1]
                kind_png(page, ev["rect"], page.rect.width, page.rect.height, png)
                with open(png, "rb") as f: b64 = base64.b64encode(f.read()).decode()
                d, prov, conf = self.ask(ev, b64)
            except Exception as e:                      # render failure or anything unexpected: this candidate is an error
                d, prov, conf = {"outcome": "error", "error": type(e).__name__}, None, None
            finally:
                try: os.remove(png)
                except OSError: pass
            if d["outcome"] == "skipped": rec["skip"] = "budget"
            rec.update(outcome=d["outcome"], kind=d.get("kind"), p_top=d.get("p_top"), p_margin=d.get("p_margin"), provider=prov, confidence=conf,
                       ms=int((time.time() - t0) * 1000))
            if d.get("error"): rec["error"] = d["error"]
            self._keep(rec, pdf_sha, digest, phash)

    def _keep(self, rec, pdf_sha, digest, phash):
        self.records.append(rec)
        self.entries.append({"key": entry_key(pdf_sha, rec["page"], rec["rect"], self.model, digest, phash), "page": rec["page"], "rect": rec["rect"],
                             "outcome": rec["outcome"], "kind": rec["kind"], "p_top": rec["p_top"], "p_margin": rec["p_margin"],
                             "model": self.model, "model_digest": digest, "prompt_sha": phash, "provider": rec["provider"], "confidence": rec["confidence"],
                             "ms": rec["ms"]})

    def finish(self):
        """Write the sidecar once per job (temp file + rename) as the union with an earlier job's (A2.5: its entries for other
        pages kept, pages united, state / done_ts / done_ms this job's), then this job's kind_job_done record (A1.2)."""
        mine = {}
        for e in self.entries: mine[e["outcome"]] = mine.get(e["outcome"], 0) + 1
        answered = sum(e["outcome"] != "skipped" for e in self.entries)
        state = "failed" if self.error else "partial" if mine.get("skipped") else "done"
        new_pages = set(self.pages)
        old = [e for e in (self.prev or {}).get("entries") or [] if e.get("page") not in new_pages]
        entries = old + self.entries
        outcomes = {}
        for e in entries: outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
        try: parse_key = os.path.basename(self.cache_json)[:-5]
        except TypeError: parse_key = None
        side = {"version": 1, "parse_key": parse_key, "state": state, "pages": sorted(covered_pages(self.prev) | new_pages),
                "routed": len(entries), "answered": sum(e["outcome"] != "skipped" for e in entries), "outcomes": outcomes,
                "breaker": self.breaker, "error": self.error, "started_ts": round(self.t0, 3), "done_ts": None, "done_ms": None, "entries": entries}
        path = sidecar_path(self.cache_json); tmp = path + f".{os.getpid()}.tmp"
        side["done_ts"] = round(time.time(), 3); side["done_ms"] = int((time.time() - self.t0) * 1000)
        with open(tmp, "w", encoding="utf-8") as f: json.dump(side, f)
        os.replace(tmp, path)
        log_records([dict(r, ts=round(time.time(), 3)) for r in self.records] +
                    [{"event": "kind_job_done", "parse_key": parse_key, "state": state, "routed": len(self.entries), "answered": answered,
                      "done_ms": side["done_ms"], "breaker": self.breaker, "ts": round(time.time(), 3)}])

def run_job(pdf, cache_json):
    job = Job(pdf, cache_json)
    try:
        job.run()
    except Exception as e:
        job.error = type(e).__name__
    finally:
        try: job.finish()
        finally:
            try: os.remove(lock_path(cache_json))
            except OSError: pass
    return 0

# ---------------- second detector on gap pages, structural fallback (Amendment 3) ----------------
LAYOUT2_FILE = "docling_layout_heron.onnx"
LAYOUT2_URL = ("https://huggingface.co/docling-project/docling-layout-heron-onnx/resolve/"
               "40bde044036bb181c130ddf6c51792187268748f/model.onnx")
LAYOUT2_SIZE = 171220471
LAYOUT2_SHA256 = "59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a"
DETECTOR2_VERSION = "docling-heron/" + LAYOUT2_SHA256[:12]
HERON_LABELS = ("caption", "footnote", "formula", "list_item", "page_footer", "page_header", "picture", "section_header", "table", "text",
                "title", "document_index", "code", "checkbox_selected", "checkbox_unselected", "form", "key_value_region")   # config.json id2label
HERON_FAMILY = {"picture": "figure", "table": "table"}
HERON_TEXT = ("text", "list_item", "section_header", "title", "caption", "footnote")
PPS_TEXT = ("text", "paragraph_title", "doc_title", "abstract", "content", "reference", "footnote", "aside_text")
HERON_INPUT, HERON_THREADS, HERON_SCORE_MIN, HERON_DUP_IOU = 640, 4, 0.5, 0.5
R1_MIN_LINES, R1_MIN_COVER = 3, 0.02                       # no or near-empty text layer
R2_ASPECT, R2_MAX_LINES = 1.2, 25                          # slide-like
R3_MIN_FRAC, R3_MIN_SIDE, R3_MAX_PAGE, R3_INSIDE = 0.08, 24.0, 0.9, 0.5   # graphics PP-DocLayout-S leaves unexplained
ROUTE_CAP = 8                                              # routed pages per parse
INK_DELTA, INK_DILATE_PX, INK_CELL_PX = 48, 6, 4
STRUCT_MIN_SIDE, STRUCT_MIN_FRAC, STRUCT_MIN_DENSITY, STRUCT_IOU, STRUCT_CAP, TEXT_PAD = 36.0, 0.02, 0.03, 0.3, 4, 2.0

def layout2_off():
    """SPEAKEASY_LAYOUT2=off: no second detector and no structural fallback (SPEAKEASY_BLOCKCLS=off covers it too)."""
    return setting("SPEAKEASY_LAYOUT2", "on") in OFF

def layout2_model_path():
    """SPEAKEASY_LAYOUT2_MODEL when set (no fall-through), else $SPEAKEASY_HOME/models, else the app bundle's Resources/models."""
    override = os.environ.get("SPEAKEASY_LAYOUT2_MODEL")
    if override: return override if os.path.isfile(override) else None
    for p in (os.path.join(home(), "models", LAYOUT2_FILE), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", LAYOUT2_FILE)):
        if os.path.isfile(p): return p
    return None

def layout2_verified(path):
    import layoutdet
    return os.path.getsize(path) == LAYOUT2_SIZE and layoutdet.file_sha256(path) == LAYOUT2_SHA256

def fetch_layout2(models_dir):
    """Installer step for the heron model (exit 0 ok, 1 checksum mismatch, 2 download failure); SPEAKEASY_LAYOUT2_MODEL_URL overrides."""
    import layoutdet
    return layoutdet.fetch_verified(models_dir, LAYOUT2_FILE, os.environ.get("SPEAKEASY_LAYOUT2_MODEL_URL") or LAYOUT2_URL, LAYOUT2_SIZE,
                                    LAYOUT2_SHA256, "Second layout model", "second layout detector off")

def union_area(rects):
    """Area of the union of rects (coordinate compression; a page has few)."""
    rects = [r for r in rects if r[2] > r[0] and r[3] > r[1]]
    xs = sorted({v for r in rects for v in (r[0], r[2])}); total = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        spans = sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1)
        y, cov = None, 0.0
        for a, b in spans:
            if y is None or a > y: cov += b - a; y = b
            elif b > y: cov += b - y; y = b
        total += cov * (x1 - x0)
    return total

def covers_at_least(rects, target):
    """union_area(rects) >= target, cheaply: the area sum bounds the union from above, and the union of the largest rects (taken
    8, 16, 32, ... at a time) from below, so a dense text page stops after a few of its lines."""
    rects = sorted((r for r in rects if r[2] > r[0] and r[3] > r[1]), key=lambda r: -_area(r))
    if sum(_area(r) for r in rects) < target: return False
    k = 8
    while True:
        if union_area(rects[:k]) >= target: return True
        if k >= len(rects): return False
        k *= 2

def route_page(lines, W, H, gfx, explained):
    """A3.2: the rules a detected page meets (R1 no / near-empty text layer, R2 slide-like, R3 unexplained graphics).
    gfx: raster images and clustered drawings; explained: PP-DocLayout-S body and paragraph boxes and captioned bodies."""
    page, by = W * H, []
    if len(lines) < R1_MIN_LINES or not covers_at_least([l[:4] for l in lines], R1_MIN_COVER * page): by.append("R1")
    if W / max(1.0, H) >= R2_ASPECT and len(lines) < R2_MAX_LINES: by.append("R2")
    loose = [g for g in gfx if min(g[2] - g[0], g[3] - g[1]) >= R3_MIN_SIDE and _area(g) < R3_MAX_PAGE * page
             and not any(_inter(g, e) >= R3_INSIDE * _area(g) for e in explained)]
    if loose and covers_at_least(loose, R3_MIN_FRAC * page): by.append("R3")
    return by

class Detector2:
    """Docling layout heron (RT-DETRv2, ONNX) on routed gap pages only: checked and loaded on the first routed page of a parse.
    Every fallback is one layout2_unavailable event plus one stderr line; the page then keeps today's path."""
    def __init__(self):
        self.session, self.reason, self.error, self.loaded, self.load_ms, self.events = None, None, None, False, None, []

    def _unavailable(self, reason, error=None, page=None):
        self.events.append({"event": "layout2_unavailable", "reason": reason, "error": type(error).__name__ if error else None,
                            "detector_version": DETECTOR2_VERSION if reason in ("load-failed", "inference-failed") else None, "page": page})
        print(f"layout2: detector unavailable ({reason})", file=sys.stderr)
        if page is None: self.reason, self.error = reason, error

    def ready(self):
        if self.loaded: return self.session is not None
        self.loaded = True
        if layout2_off(): self._unavailable("disabled"); return False
        try:
            import numpy as np, onnxruntime as ort
        except ImportError as e:
            self._unavailable("no-onnxruntime", e); return False
        self.np = np
        path = layout2_model_path()
        if path is None: self._unavailable("model-missing"); return False
        if not layout2_verified(path): self._unavailable("model-mismatch"); return False
        t0 = time.perf_counter()
        try:
            opts = ort.SessionOptions()
            opts.intra_op_num_threads, opts.inter_op_num_threads, opts.log_severity_level = HERON_THREADS, 1, 3
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self.session = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        except Exception as e:                      # any load error: today's path, the parse never fails
            self._unavailable("load-failed", e)
        self.load_ms = int(round((time.perf_counter() - t0) * 1000))
        return self.session is not None

    def status(self):
        if not self.loaded: return {"status": "idle", "detector_version": None, "reason": None, "load_ms": None}
        if self.reason: return {"status": "unavailable", "detector_version": DETECTOR2_VERSION if self.reason == "load-failed" else None,
                                "reason": self.reason, "load_ms": None}
        return {"status": "ok", "detector_version": DETECTOR2_VERSION, "reason": None, "load_ms": self.load_ms}

    def detect(self, page, W, H):
        """[{"cls", "score", "bbox"}] above HERON_SCORE_MIN in page points: the zoom-1 render resized to 640 x 640 (uint8, no
        normalisation), boxes in render pixels scaled back."""
        from PIL import Image
        np = self.np
        img = page.image([0, 0, W, H], 1.0)
        w, h = img.size
        x = np.asarray(img.resize((HERON_INPUT, HERON_INPUT), Image.BILINEAR), dtype=np.uint8).transpose(2, 0, 1)[None]
        labels, boxes, scores = self.session.run(None, {"images": x, "orig_target_sizes": np.array([[w, h]], np.int64)})
        sx, sy, out = W / w, H / h, []
        for c, b, s in zip(labels[0].tolist(), boxes[0].tolist(), scores[0].tolist()):
            if s < HERON_SCORE_MIN or not 0 <= int(c) < len(HERON_LABELS): continue
            r = [round(min(max(v, 0.0), lim), 1) for v, lim in ((b[0] * sx, W), (b[1] * sy, H), (b[2] * sx, W), (b[3] * sy, H))]
            if r[2] > r[0] and r[3] > r[1]: out.append({"cls": HERON_LABELS[int(c)], "score": s, "bbox": r})
        out.sort(key=lambda b: (-b["score"], b["bbox"], b["cls"]))
        return out

def structural_candidates(page, W, H, text_boxes, existing):
    """A3.3 on an R1 page: connected regions of ink (>= INK_DELTA darker than the page's median, dilated INK_DILATE_PX) once text
    evidence (padded TEXT_PAD) is removed, with both sides >= 36 pt, >= 2 % of the page, ink density >= 3 %, and IoU < 0.3 with
    every existing candidate or body. At most STRUCT_CAP, largest first: [{"cls": "ink", "score": None, "bbox"}]. Components are
    found on an INK_CELL_PX grid (the dilation rounds up to whole cells)."""
    import numpy as np
    g = np.asarray(page.image([0, 0, W, H], 1.0).convert("L"))
    h, w = g.shape; sx, sy = W / w, H / h
    ink = g <= int(np.median(g)) - INK_DELTA
    for r in text_boxes:
        x0, y0 = max(0, int((r[0] - TEXT_PAD) / sx)), max(0, int((r[1] - TEXT_PAD) / sy))
        x1, y1 = min(w, int(math.ceil((r[2] + TEXT_PAD) / sx))), min(h, int(math.ceil((r[3] + TEXT_PAD) / sy)))
        ink[y0:y1, x0:x1] = False
    if not ink.any(): return []
    c = INK_CELL_PX; gh, gw = (h + c - 1) // c, (w + c - 1) // c
    pad = np.zeros((gh * c, gw * c), bool); pad[:h, :w] = ink
    cells = pad.reshape(gh, c, gw, c).any(axis=(1, 3))
    d = -(-INK_DILATE_PX // c); grown = cells.copy()
    for dy in range(-d, d + 1):
        for dx in range(-d, d + 1):
            if dy or dx:
                grown[max(0, dy):gh + min(0, dy), max(0, dx):gw + min(0, dx)] |= cells[max(0, -dy):gh + min(0, -dy), max(0, -dx):gw + min(0, -dx)]
    seen = np.zeros_like(grown); on = grown.tolist(); regions = []
    for y0, x0 in zip(*[v.tolist() for v in np.nonzero(grown)]):
        if seen[y0, x0]: continue
        stack, box = [(y0, x0)], [x0, y0, x0, y0]; seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            if x < box[0]: box[0] = x
            if y < box[1]: box[1] = y
            if x > box[2]: box[2] = x
            if y > box[3]: box[3] = y
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < gh and 0 <= nx < gw and on[ny][nx] and not seen[ny, nx]: seen[ny, nx] = True; stack.append((ny, nx))
        px = [box[0] * c, box[1] * c, min(w, (box[2] + 1) * c), min(h, (box[3] + 1) * c)]
        r = [round(px[0] * sx, 1), round(px[1] * sy, 1), round(px[2] * sx, 1), round(px[3] * sy, 1)]
        if min(r[2] - r[0], r[3] - r[1]) < STRUCT_MIN_SIDE or _area(r) < STRUCT_MIN_FRAC * W * H: continue
        if float(ink[px[1]:px[3], px[0]:px[2]].mean()) < STRUCT_MIN_DENSITY or any(_iou(r, e) >= STRUCT_IOU for e in existing): continue
        regions.append(r)
    regions.sort(key=lambda r: (-_area(r), r))
    return [{"cls": "ink", "score": None, "bbox": r, "anchor": "no-text-structural"} for r in regions[:STRUCT_CAP]]

# ---------------- Read-area captures without a document page (7.2) ----------------
class RasterPage:
    """A capture PNG with the page interface layoutdet and stage B use (units: capture pixels; no vector content)."""
    def __init__(self, img):
        self.img = img.convert("RGB")
        w, h = self.img.size
        self.rect = type("Rect", (), {"width": float(w), "height": float(h)})()
    def image(self, clip, zoom):
        from PIL import Image
        box = (int(math.floor(clip[0])), int(math.floor(clip[1])), int(math.ceil(clip[2])), int(math.ceil(clip[3])))
        crop = self.img.crop(box)
        if zoom == 1.0: return crop
        return crop.resize((max(1, round(crop.size[0] * zoom)), max(1, round(crop.size[1] * zoom))), Image.BILINEAR)
    def images(self): return []
    def drawings(self): return []
    def cluster_drawings(self, *a, **k): return []
    def paths(self): return []

def classify_capture(png, ocr_json):
    """Detector + lexicon captions + stage B on an area capture. Returns area_kinds {"candidates", "figure", "table", "outcomes",
    "ms"} (capture-pixel rects) plus the block_kind records (anchor "area"), or None when the detector is unavailable."""
    import docmap, layoutdet
    from PIL import Image
    t0 = time.time()
    with open(ocr_json, encoding="utf-8") as f: ocr = json.load(f)
    page = RasterPage(Image.open(png))
    W, H = page.rect.width, page.rect.height
    pt = float(ocr.get("scale") or 1.0)               # capture pixels per screen point
    lines = [[*r1(l["rect"]), " ".join(l["text"].split())] for l in ocr.get("lines") or [] if l.get("text", "").strip()]
    lines.sort(key=lambda l: (l[1], l[0]))
    det = layoutdet.Detector()
    lay = det.layout(1, page, lines, W, H, 1)
    if lay is None: return None
    sess = Session(); sess.vlm = "off"
    caps = []
    for i, l in enumerate(lines):
        info = docmap.caption_info(l[4])
        if info and info["kind"] != "ambiguous": caps.append((i, info))
    found = [(info["kind"], lines[i][:4], max(1.0, lines[i][3] - lines[i][1])) for i, info in caps]
    paired = lay.bodies(found, {i for i, _ in caps})
    out = {"figure": [], "table": []}
    for (i, info), body in zip(caps, paired):
        if body is not None: out[info["kind"]].append(r1(body))
    captioned = [b for k in ("figure", "table") for b in out[k]]
    paragraphs = [docmap._union([lines[j][:4] for j in p]) for p in lay.paragraphs(set())[0]]
    cands, dropped = filter_candidates(lay.unpaired_bodies(set(i for i, _ in caps)), captioned, paragraphs, W, H, pt)
    accepted, kinds = sess.classify_candidates(1, page, cands, W, H, "area", dropped)
    for kind, c in accepted: out[kind].append(r1(c["bbox"]))
    outcomes = {}
    for e in sess.events: outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1
    rec = {"candidates": kinds["candidates"], "figure": out["figure"], "table": out["table"], "outcomes": outcomes, "ms": int((time.time() - t0) * 1000)}
    return {"area_kinds": rec, "events": sess.events, "status": sess.classifier.status()["status"]}

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "fetch": sys.exit(fetch_model(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == "fetch-layout2": sys.exit(fetch_layout2(sys.argv[2]))
    if len(sys.argv) == 4 and sys.argv[1] == "job": sys.exit(run_job(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 5 and sys.argv[1] == "capture":
        res = classify_capture(sys.argv[2], sys.argv[3])
        tmp = sys.argv[4] + ".tmp"
        with open(tmp, "w") as f: json.dump(res, f)
        os.replace(tmp, sys.argv[4]); sys.exit(0)
    print("usage: blockcls.py fetch MODELS_DIR | fetch-layout2 MODELS_DIR | job PDF CACHE_JSON | capture PNG OCR_JSON OUT_JSON", file=sys.stderr); sys.exit(64)
