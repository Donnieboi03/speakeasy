"""The source document (PDF / Markdown) behind a selection: read in the background, figure labels for the prompt,
PDF text for Read area."""
import html, json, os, re, tempfile, threading, time, urllib.parse
import speakeasy_state as state
from selog import home, _on
from speakeasy_state import docmap
from observe import is_owned_capture, tempfile_dir
from prompts import DOC_RULE
from grounding import GROUNDER
from llm_client import model_name, prewarm, vision_model

class DocJob(threading.Thread):
    """Find and read the source document (read-only, local) alongside the audio; never blocks speech."""
    _ctx = None
    path = None
    img_given = False
    def __init__(self, hint_path, sel_text, rec, run_t0):
        super().__init__(daemon=True)
        self.hint_path, self.sel, self.rec, self.t0, self.run_t0 = hint_path, sel_text or "", rec, time.time(), run_t0
        self.out = None
        self.done = threading.Event()
        self._fill(doc_source=None, doc_path_hash=None, page=None, figures_found=None, doc_map_method=None, doc_ms=None, doc_cache=None, doc_note=None)
    def _fill(self, **kw):
        for k, v in kw.items(): self.rec[k] = v
    def run(self):
        try: self._run()
        except Exception as e:
            self._fill(doc_note=type(e).__name__)                # class only: the message can hold the document's path
        finally:
            self._fill(doc_ms=int((time.time() - self.t0) * 1000), doc_ready_ms=int((time.time() - self.run_t0) * 1000)); self.done.set()
    def _run(self):
        mode = os.environ.get("SPEAKEASY_DOC_READ", "local").lower()
        if mode not in ("local", "all"): self._fill(doc_note="SPEAKEASY_DOC_READ=off"); return
        hint = {}
        if self.hint_path:
            try: hint = json.load(open(self.hint_path))
            except (OSError, ValueError): hint = {}
            if is_owned_capture(self.hint_path):
                try: os.remove(self.hint_path)
                except OSError: pass
        src, path, note = docmap.resolve(hint, mode)
        self._fill(doc_note=note)
        if not path: return
        if src == "browser":
            path = docmap.fetch_pdf(path, tempfile_dir())
        self.path = path
        if not docmap.cached(path): self.cold = True            # first read of this file: the prompt will not wait for it
        doc, cache = docmap.load(path, sel_text=self.sel)
        location = hint.get("url") if src == "browser" else path
        self._fill(doc_source=src, doc_path_hash=docmap.path_hash(location), doc_cache=cache)
        try: note_library(self.rec["doc_path_hash"], location, url_name(location) if src == "browser" else pdf_title(path) or os.path.basename(path))
        except (OSError, ValueError) as e: self._fill(library_error=type(e).__name__)   # the read goes on without a name
        if not doc: return
        if cache in ("miss", "partial-miss", "miss-pdfkit") and doc["kind"] == "pdf": self._fill(layout_events=doc["layout_events"])
        out = {"kind": doc["kind"], "figures": [], "page": None, "anchors": []}
        if doc["kind"] == "pdf":
            pg, meth = docmap.match_page(doc, self.sel, hint.get("title") or "")
            self._fill(doc_map_method=meth)
            if pg:
                out["page"] = pg; out["page_size"] = [pg["w"], pg["h"]]
                figs = list(pg["figures"])
                for m in docmap.REF_RE.finditer(self.sel):          # figures named in the selection but drawn on another page
                    lab = ("Table " if m.group(1).lower().startswith("tab") else "Figure ") + m.group(2)
                    if not any(f["label"] == lab for f in figs):
                        figs += [f for p in doc["pages"] for f in p["figures"] if f["label"] == lab][:1]
                out["figures"] = figs
                out["page_figures"] = len(pg["figures"])
                seen, anc = set(), []
                for l in pg["lines"]:
                    k = re.sub(r"\s+", " ", l[4].strip().lower())
                    if len(k) >= 14 and k not in seen: seen.add(k); anc.append(l)
                out["anchors"] = [[round(v, 1) for v in a[:4]] + [a[4][:80]] for a in anc][:48]
                self._fill(page=pg["n"], doc_parse=docmap.parse_summary(pg), figures_degenerate=sum(bool(f.get("degenerate")) for f in pg["figures"]))
                if any(f["kind"] == "figure" for f in pg["figures"]) and vision_model() != model_name() and GROUNDER.pick_enabled():
                    prewarm([vision_model()])                       # load the picker's model while the text model speaks
        else:
            lines = doc["lines"]; first = next((l.strip() for l in self.sel.splitlines() if l.strip()), "")
            at = None
            if first:
                ft = set(docmap._toks(first))
                for i, l in enumerate(lines):
                    lt = set(docmap._toks(l))
                    if ft and lt and len(ft & lt) / len(ft) >= 0.8: at = i + 1; break
            n = max(1, len(self.sel.splitlines()))
            figs = doc["figures"]
            if at: figs = [f for f in figs if at - 12 <= f["line"] <= at + n + 12] or figs
            out["figures"] = figs[:8]; out["md_line"] = at; out["page_figures"] = len(doc["figures"])
            self._fill(doc_map_method="md-line" if at else "md-none")
        # figures_found: every figure/table on the visible page (a Markdown file: in the file), independent of the selection
        self._fill(figures_found=out.get("page_figures", len(out["figures"])), figures_referenced=len(out["figures"]) - (out.get("page_figures") or 0)
                   if doc["kind"] == "pdf" else None)
        self.out = out
    cold = False
    def relevant(self):
        """Figures/tables the selection mentions, is the caption of, or sits next to (within ~60 pt)."""
        d = self.out
        if not d: return []
        sel = self.sel; st = set(docmap._toks(sel)); out = []
        named = {("Table " if m.group(1).lower().startswith("tab") else "Figure ") + m.group(2) for m in docmap.REF_RE.finditer(sel)}
        near_y = None
        if d.get("page"):
            ix = [i for i, l in enumerate(d["page"]["lines"]) if len(set(docmap._toks(l[4]))) >= 3 and set(docmap._toks(l[4])) <= st | set()]
            if ix: near_y = (min(d["page"]["lines"][i][1] for i in ix), max(d["page"]["lines"][i][3] for i in ix))
        for f in d.get("figures") or []:
            cap = set(docmap._toks(f.get("caption") or ""))
            hit = f.get("label") in named or (st and len(st & cap) >= 0.4 * len(st))
            if not hit and near_y and f.get("bbox") and f.get("page") == (d.get("page") or {}).get("n"):
                b = f["bbox"]; c = f.get("caption_bbox") or b
                top, bot = min(b[1], c[1]), max(b[3], c[3])
                hit = near_y[0] <= bot + 60 and near_y[1] >= top - 60
            if not hit and d["kind"] == "md" and d.get("md_line"):
                hit = abs(f.get("line", -99) - d["md_line"]) <= 6
            if not hit and self.img_given and d.get("page") and f.get("kind") == "figure": hit = True
            if hit: out.append(f)
        return out
    def get(self, wait=0.0):
        if wait > 0 and not self.cold:
            end = time.time() + wait
            while not self.done.is_set() and not self.cold and time.time() < end: self.done.wait(0.01)
        return self.out if self.done.is_set() else None

def doc_context_text():
    """A short figure/table label list appended to the END of the system prompt (keeps the cached prompt prefix),
    only when the selection mentions, is, or sits next to a figure/table and the document was read in time. <= ~200 chars."""
    doc = state.DOC
    if doc is None: return ""
    if doc._ctx is not None: return doc._ctx
    read = doc.get(float(os.environ.get("SPEAKEASY_DOC_WAIT_MS", "80")) / 1000)
    if not read: return ""   # not ready: do not cache, a later prompt may still get it
    figs = doc.relevant()
    items = docmap.figure_context(figs, limit=3, cap_chars=70) if figs else []
    ctx = ""
    if items:
        body = "; ".join(items)
        ctx = DOC_RULE + body[:200].rstrip(" ;,") + "."
        doc.rec["doc_in_prompt"] = True
    doc._ctx = ctx
    return ctx

def area_pdf_text(ocr_text, args, rec, wait=4.0):
    """Read area on a PDF: swap the OCR text for the document's own text layer inside the drawn rectangle.
    Page->screen comes from the OCR lines (same anchor fit the tracker uses); returns the PDF text and rewrites the
    per-line rects file with the PDF lines mapped to screen, or None to keep OCR."""
    t0 = time.time()
    try:
        end = time.time() + wait
        while not state.DOC.done.is_set() and time.time() < end: state.DOC.done.wait(0.02)
        read = state.DOC.out
        why = ("doc-timeout" if not state.DOC.done.is_set() else "doc-unresolved" if not state.DOC.path else "doc-failed" if read is None
               else "markdown" if read.get("kind") == "md" else "no-page" if not read.get("page") else None)
        if why: rec["area_pdf"] = why; return None
        line_bounds_json = json.load(open(args.line_bounds))
        ocr = []
        for e in line_bounds_json:
            t = ocr_text[int(e["loc"]):int(e["loc"]) + int(e["len"])]; x, y, w, h = map(float, e["rect"])
            ocr.append([x, y, x + w, y + h, t])
        frame = [float(v) for v in args.capture_rect.split(",")]
        seen, anchors = set(), []
        for l in read["page"]["lines"]:
            k = re.sub(r"\s+", " ", l[4].strip().lower())
            if len(k) >= 14 and k not in seen: seen.add(k); anchors.append(l)
        fit = docmap.anchor_fit(ocr, anchors, frame)
        if not fit or fit[4] < 3 or fit[3] > 6: rec["area_pdf"] = "no-fit"; return None
        scale, dx, dy = fit[0], fit[1], fit[2]
        state.AREA_FIT = (scale, dx, dy, fit[3])
        page_rect = [(frame[0] - dx) / scale, (frame[1] - dy) / scale, (frame[0] + frame[2] - dx) / scale, (frame[1] + frame[3] - dy) / scale]
        lines = docmap.area_lines(read["page"]["lines"], page_rect)
        if not lines: rec["area_pdf"] = "empty"; return None
        out, loc, parts = [], 0, []
        for l in lines:
            t = l[4].strip()
            out.append({"loc": loc, "abs": loc, "len": len(t),
                        "rect": [round(scale * l[0] + dx, 1), round(scale * l[1] + dy, 1), round(scale * (l[2] - l[0]), 1), round(scale * (l[3] - l[1]), 1)]})
            parts.append(t); loc += len(t) + 1
        with open(args.line_bounds, "w") as f: json.dump(out, f)
        rec.update(area_text="pdf", area_pdf="ok", area_pdf_lines=len(lines), area_fit_rms=round(fit[3], 1), area_fit_anchors=fit[4],
                   area_page_rect=[round(v, 1) for v in page_rect], area_region_src="area-fit")   # the run's selection region (A2.1)
        return "\n".join(parts)
    except (OSError, ValueError, KeyError, TypeError) as e:
        rec.update(area_pdf="error", area_pdf_error=type(e).__name__); return None
    finally:
        rec["area_pdf_ms"] = int((time.time() - t0) * 1000)

# ---------------- document library (contract A5.3): names and paths, outside the logs ----------------
def library_path():
    return os.path.join(home(), "library.json")

def doc_names_on():
    return _on("SPEAKEASY_DOC_NAMES")

def _pdf_string(raw):
    """A PDF literal string body (escapes, octal) as text; UTF-16BE when it starts with a byte-order mark."""
    out, i = bytearray(), 0
    esc = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
    while i < len(raw):
        c = raw[i:i + 1]
        if c != b"\\": out += c; i += 1; continue
        nxt = raw[i + 1:i + 2]
        m = re.match(rb"[0-7]{1,3}", raw[i + 1:i + 4])
        if m: out.append(int(m.group(0), 8) & 255); i += 1 + len(m.group(0))
        else: out += esc.get(nxt, nxt); i += 2
    b = bytes(out)
    return b[2:].decode("utf-16-be", "replace") if b[:2] == b"\xfe\xff" else b.decode("latin-1")

def pdf_title(path, limit=64 << 20):
    """The title from the PDF's Info dictionary (or uncompressed XMP dc:title) when non-empty and under 120 characters."""
    if not path.lower().endswith(".pdf"): return None
    with open(path, "rb") as f: data = f.read(limit)
    title = None
    refs = re.findall(rb"/Info\s+(\d+)\s+(\d+)\s+R", data)
    if refs:
        n, g = refs[-1]
        obj = re.search(rb"(?<!\d)" + n + rb"\s+" + g + rb"\s+obj(.*?)endobj", data, re.S)
        if obj:
            m = re.search(rb"/Title\s*\(((?:\\.|[^\\)])*)\)", obj.group(1), re.S)
            h = None if m else re.search(rb"/Title\s*<([0-9A-Fa-f\s]*)>", obj.group(1))
            if m: title = _pdf_string(m.group(1))
            elif h:
                b = bytes.fromhex(re.sub(rb"\s", b"", h.group(1)).decode())
                title = b[2:].decode("utf-16-be", "replace") if b[:2] == b"\xfe\xff" else b.decode("latin-1")
    if not title:
        m = re.search(rb"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", data, re.S)
        if m: title = html.unescape(m.group(1).decode("utf-8", "replace"))
    title = re.sub(r"\s+", " ", title or "").strip()
    return title if 0 < len(title) < 120 else None

def url_name(url):
    """A browser PDF's name: the URL's last path segment."""
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or None

def note_library(doc_hash, location, name):
    """Add or update {doc_path_hash: {name, path, first_ts, last_ts}} in library.json (atomic, mode 0600). Names off:
    the file is deleted and nothing is written."""
    lp = library_path()
    if not doc_names_on():
        if os.path.exists(lp): os.remove(lp)
        return
    try:
        with open(lp, encoding="utf-8") as f: lib = json.load(f)
    except FileNotFoundError: lib = {}
    if not isinstance(lib, dict): raise ValueError("library.json is not an object")
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    entry = lib.get(doc_hash) or {"first_ts": now}
    entry.update(name=name, path=location, last_ts=now); lib[doc_hash] = entry
    fd, tmp = tempfile.mkstemp(dir=home(), prefix=".library_", suffix=".json")     # mkstemp files are 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(lib, f, ensure_ascii=False)
        os.replace(tmp, lp)
    except BaseException:
        os.remove(tmp); raise
