"""Focus box: maps each spoken sentence to its source lines / figure part and writes focus events for the tracker."""
import hashlib, json, os, re, threading, time
import speakeasy_state as state
from speakeasy_state import docmap
from selog import home, _on, text_logging_on
from grounding import GROUNDER

def tagging():
    """Ask the model for [L]/[R] focus tags? Opt-in (SPEAKEASY_FOCUS_TAGS=on): tags cost first-audio time
    and accuracy on small models, so by default the prompt is untouched and sentences are mapped afterwards."""
    return state.FOCUS is not None and state.FOCUS.tags

_NUMW = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_FEATURE_STOPWORDS = {"the", "and", "thi", "tha", "wit", "for", "are", "was", "fro", "has", "have", "its", "into", "also", "than",
          "then", "there", "their", "they", "which", "while", "when", "will", "can", "not", "but", "you", "your", "all",
          "one", "more", "most", "some", "each", "about", "over", "only", "like", "just", "very", "use", "uses", "used"}

_SYMBOL_WORDS = {"asterisk": "*", "asterisks": "*", "star": "*", "stars": "*", "dash": "-", "dashes": "-", "hyphen": "-",
         "hyphens": "-", "underscore": "_", "underscores": "_", "hash": "#", "pound": "#", "pipe": "|", "pipes": "|",
         "equals": "=", "tilde": "~", "backtick": "`", "backticks": "`", "slash": "/", "plus": "+"}
_SENTENCE_STARTERS = {"this", "these", "that", "those", "there", "here", "then", "when", "while", "where", "what", "both",
             "each", "inside", "finally", "first", "next", "after", "before", "overall", "also", "however", "since",
             "because", "yes", "it", "in", "on", "at", "if", "for", "so", "and", "but", "or", "an", "a", "its"}

def _match_features(s, sentence=False):
    """Weighted features: numbers (x3), capitalised names / acronyms / CamelCase in the spoken sentence (x2.5),
    other content words (x1, 5-char stems), and symbol runs (a line of ***, ---, ___ ...) named in words."""
    f = {}
    for m in re.finditer(r"\d+(?:[.,]\d+)*", s):
        v = re.sub(r"\.0+$", "", m.group(0).replace(",", ""))
        f["#" + v] = 3.0
    if not sentence:
        for m in re.finditer(r"([*\-_#|=~`/+])\1{2,}", s):
            f["@" + m.group(1)] = 1.0
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'+-]*", s):
        lw = w.lower()
        if sentence and lw in _SYMBOL_WORDS:
            f["@" + _SYMBOL_WORDS[lw]] = 2.0; continue
        if len(lw) < 3 or lw in _FEATURE_STOPWORDS or lw[:5] in _FEATURE_STOPWORDS: continue
        k = lw[:5]
        proper = sentence and lw not in _SENTENCE_STARTERS and (w[0].isupper() or (w.isupper() and len(w) >= 2) or re.search(r"[a-z][A-Z]", w))
        f[k] = max(f.get(k, 0), 2.5 if proper else 1.0)
    return f

TAG_RX = re.compile(r"\[\s*(?:L\s*(\d+)(?:\s*(?:-|\u2013|to|,)\s*L?\s*(\d+))?|R\s*\(?\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)?)\s*\]", re.I)
TAG_ANY = re.compile(
    r"\[\s*L\s*\d+(?:\s*(?:-|\u2013|to|,)\s*L?\s*\d+)?\s*\]"               # [L3] [L3-5] [L 3 to 4]
    r"|\[\s*R\s*\(?\s*[\d.]+(?:\s*,\s*[\d.]+){3}\s*\)?\s*\]"                  # [R x0,y0,x1,y1]
    r"|\[[LR](?![a-z]{2})[\w\-\u2013,.]{0,6}\]"                              # short junk with no spaces: [Lx-] [L] [R1]
    r"|[\u3010\u3016]\s*[LR]?\s*[\d\-\u2013,.\s]{0,12}[\u3011\u3017]"      # 【3】 〖L2〗
    r"|<\s*/?\s*[LR]\s*\d+(?:\s*[-\u2013,]\s*\d+)*\s*/?>"                  # <L2> <R 1,2,3,4> </L2>
    r"|\{\s*[LR]\s*\d+(?:\s*[-\u2013,]\s*\d+)*\s*\}"                      # {L4}
    r"|\(\s*L\d+(?:\s*[-\u2013]\s*L?\d+)?\s*\)"                            # (L3-4)
    r"|^\s*L\d+\s*[:.)-]\s*|(?<=[.!?]\s)L\d+\s*[:.)]\s*", re.I | re.M)

_WORD_TAG = re.compile(r"(?i)[\[(<{\u3010]\s*(?:lines?|ln|l\.|rows?|region)\s*#?\s*\d+(?:\s*(?:-|\u2013|to|and|through|,)\s*\d+)*\s*[\])>}\u3011]")
_EMPTY_BR = re.compile(r"[\[\u3010]\s*[^\w\s\[\]'\"]{0,3}\s*[\]\u3011]|<\s*>|\{\s*\}")  # [ ] [,] 【 】 left after stripping

def _drop_orphans(t):
    """Remove unbalanced [ ] (and a tag fragment glued to an orphan '[', e.g. '[[L There'), and any lone
    CJK lenticular bracket. Balanced pairs such as '[1,2,3]' are kept; text between brackets is never deleted."""
    t = re.sub(r"[\u3010\u3011\u3016\u3017]", " ", t)
    stack, drop = [], set()
    for i, ch in enumerate(t):
        if ch == "[": stack.append(i)
        elif ch == "]":
            if stack: stack.pop()
            else: drop.add(i)
    drop.update(stack)
    if not drop: return t
    out, i = [], 0
    while i < len(t):
        if i in drop:
            i += 1
            if t[i - 1] == "[":                                   # eat a glued tag fragment: "[L ", "[L9", "[R "
                m = re.match(r"\s*[LR](?:\d+(?:-\d*)?)?(?=\s|$)", t[i:])
                if m and not re.match(r"\s*[LR][a-z]", t[i:]): i += m.end()
            out.append(" "); continue
        out.append(t[i]); i += 1
    return "".join(out)

def _log_text(t):
    """Text for the focus log: tags stripped and no bracket characters at all."""
    return re.sub(r"\s{2,}", " ", re.sub(r"[\[\]\u3010\u3011<>{}]", " ", strip_tags(t or ""))).strip()

def _len_and_hash(value):
    """(character length, first 12 hex chars of SHA-256 over the UTF-8) of a text value."""
    return len(value), hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

def focus_log_record(ev):
    """Focus-log copy of an event: lengths and hashes always; text, source lines and anchor text only with SPEAKEASY_LOG_TEXT=1."""
    out = dict(ev)
    if ev.get("text"): out["text_len"], out["text_hash"] = _len_and_hash(ev["text"])
    if ev.get("src"): out["src_n"] = len(ev["src"]); out["src_hash"] = _len_and_hash("\n".join(ev["src"]))[1]
    if text_logging_on(): return out
    out.pop("text", None); out.pop("src", None)
    anchors = (ev.get("doc") or {}).get("anchors")
    if anchors: out["doc"] = dict(ev["doc"], anchors=[a[:4] for a in anchors])
    return out

def strip_tags(t):
    """Remove focus tags ([L3-5], [R 0.1,0.2,0.5,0.6], 【3】, <L2>, {L4}), echoed line labels and orphan
    brackets. Only well-formed tags are removed whole, so no spoken text is lost. They are never spoken."""
    for _ in range(3):                                          # nested tags: [[L3]] -> [ ] -> gone
        u = TAG_ANY.sub(" ", t)
        u = _WORD_TAG.sub(" ", u)
        u = _EMPTY_BR.sub(" ", u)
        if u == t: break
        t = u
    t = re.sub(r"\[\s*(?:L|R)\s*[\d.,\s\-]*$", " ", t)          # dangling, unterminated tag at the end
    t = _drop_orphans(t)
    t = _EMPTY_BR.sub(" ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return re.sub(r"\s+([,.;:!?])", r"\1", t)

def parse_tag(t):
    m = TAG_RX.search(t or "")
    if not m: return None
    if m.group(1):
        a = int(m.group(1)); b = int(m.group(2) or a)
        return ("L", min(a, b), max(a, b))
    try:
        v = [float(m.group(i)) for i in range(3, 7)]
    except (TypeError, ValueError):
        return None
    if any(x > 1.0 for x in v):                  # percentages or pixels: normalise if it looks like 0-100
        if all(x <= 100 for x in v): v = [x / 100 for x in v]
        else: return None
    x0, y0, x1, y1 = v
    if x1 <= x0 or y1 <= y0: return None
    return ("R", x0, y0, x1, y1)

def source_lines(text):
    return [l.strip() for l in (text or "").splitlines() if l.strip()]

def numbered(text):
    return "\n".join(f"L{i}: {l}" for i, l in enumerate(source_lines(text), 1))

def _token_stems(s):
    return {w[:5] for w in re.findall(r"[a-z0-9]{3,}", s.lower())} - {"the", "and", "thi", "tha", "wit", "for", "are", "was", "fro"}

class FocusRect(list):
    src = None
    rng = None
    doc = None
    shape = None
    pending = None

def _rendered_line(l):
    """A source line as it looks rendered on screen (markdown/table syntax removed), for OCR re-finding."""
    l = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", l)
    l = re.sub(r"^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)", "", l)
    l = re.sub(r"[*_`|]+", " ", l)
    return re.sub(r"\s{2,}", " ", l).strip()

class Focus:
    def __init__(self, text, rect, line_bounds, image_mode, dry, rec):
        self.lines = source_lines(text)
        self.text_all = text or ""
        self.rect = rect                 # (x, y, w, h) screen points
        self.ranges = None
        self.line_bounds = self._map_bounds(text, line_bounds)
        self.image_mode = image_mode     # True when the output may be about an image (R tags)
        self.dry, self.rec = dry, rec
        self.prev_line, self.n, self.events, self.methods = 0, 0, 0, {}
        self.prev_start, self.ref_lines, self.map_note = 1, None, None
        self.toks = [_token_stems(l) for l in self.lines]
        self.tags = os.environ.get("SPEAKEASY_FOCUS_TAGS", "off").lower() in ("on", "1", "true", "yes")
        self.feats = [_match_features(l) for l in self.lines]
        df = {}
        for fl in self.feats:
            for k in fl: df[k] = df.get(k, 0) + 1
        self.df = df
        self.last_lines = None
        w = [max(1, len(l)) + 20 for l in self.lines]     # longer logical lines wrap into more screen lines
        tot = float(sum(w)) or 1.0
        acc, self.bands = 0.0, []
        for x in w:
            self.bands.append((acc / tot, (acc + x) / tot)); acc += x
        cfg = os.environ
        self.style = {"color": cfg.get("SPEAKEASY_FOCUS_COLOR", "#3B82F6"),
                      "opacity": float(cfg.get("SPEAKEASY_FOCUS_OPACITY", "0.22") or 0.22)}
    def _map_bounds(self, text, lb):
        """Per-source-line screen rects from the hotkey's AX data, or None (band fallback).
        Accepts [[x,y,w,h], ...] with one entry per non-blank source line, or visual (wrapped) lines
        [{"loc": char offset in the selection, "len": n, "rect": [x,y,w,h]}, ...] mapped by character span."""
        try:
            if not lb or not self.lines: return None
            if all(isinstance(r, (list, tuple)) and len(r) == 4 for r in lb):
                return [list(map(float, r)) for r in lb] if len(lb) == len(self.lines) else None
            vis = [(int(d["loc"]), int(d["len"]), list(map(float, d["rect"]))) for d in lb]
            base = None
            if all("abs" in d for d in lb):
                base = int(lb[0]["abs"]) - int(lb[0]["loc"])           # selection start in the element's text
            out, pos, rngs = [], 0, []
            for raw in (text or "").splitlines(True):
                st, en = pos, pos + len(raw); pos = en
                if not raw.strip(): continue
                rs = [r for (lo, n, r) in vis if lo < en and lo + max(n, 1) > st]
                if not rs: return None
                lead = len(raw) - len(raw.lstrip())
                body = len(raw.rstrip("\r\n")) - lead
                rngs.append([base + st + lead, max(1, body)] if base is not None else None)
                x0 = min(r[0] for r in rs); y0 = min(r[1] for r in rs)
                out.append([x0, y0, max(r[0] + r[2] for r in rs) - x0, max(r[1] + r[3] for r in rs) - y0])
            if len(out) == len(self.lines):
                self.ranges = rngs
                return out
            return None
        except (KeyError, TypeError, ValueError):
            return None
    # -- mapping --
    def _line_rect(self, a, b):
        a = max(1, min(a, len(self.lines))); b = max(a, min(b, len(self.lines)))
        if self.line_bounds:
            bs = self.line_bounds[a - 1:b]
            x0 = min(r[0] for r in bs); y0 = min(r[1] for r in bs)
            x1 = max(r[0] + r[2] for r in bs); y1 = max(r[1] + r[3] for r in bs)
            return [x0, y0, x1 - x0, y1 - y0]
        x, y, w, h = self.rect
        f0, f1 = self.bands[a - 1][0], self.bands[b - 1][1]
        return [x, y + f0 * h, w, max(14.0, (f1 - f0) * h)]
    def _norm_rect(self, x0, y0, x1, y1):
        x, y, w, h = self.rect
        return [x + x0 * w, y + y0 * h, max(20.0, (x1 - x0) * w), max(14.0, (y1 - y0) * h)]
    def locate(self, sentence, tag):
        """(rect, method); the rect also carries .src (source lines, for the live tracker to re-find on screen)
        and .rng (absolute AX character range of those lines, when the hotkey supplied per-line offsets)."""
        self.ref_lines = None; self.map_note = None
        rect, method = self._locate(sentence, tag)
        r = FocusRect(rect)
        r.nlines = (self.last_lines[1] - self.last_lines[0] + 1) if self.last_lines else None
        if self.ref_lines:
            r.ref = [round(v, 1) for v in self._line_rect(*self.ref_lines)]
        if self.last_lines and self.lines:
            a, b = self.last_lines
            r.src = [_rendered_line(l) for l in self.lines[a - 1:b]][:6]
            if self.ranges and a - 1 < len(self.ranges) and b - 1 < len(self.ranges) and self.ranges[a - 1] and self.ranges[b - 1]:
                r.rng = [self.ranges[a - 1][0], self.ranges[b - 1][0] + self.ranges[b - 1][1] - self.ranges[a - 1][0]]
        try:
            r, method = self._doc_target(r, method, sentence)
        except Exception as e:                                     # document mapping never breaks speech
            self.rec["doc_note"] = f"focus: {type(e).__name__}: {e}"[:160]
        return r, method
    def _doc_lines(self, a, b):
        """Page-line indices for source lines a..b (1-based), by text."""
        d = state.DOC.get() if state.DOC is not None else None
        if not d or not d.get("page"): return []
        want = set(docmap._toks(" ".join(self.lines[a - 1:b])))
        out = []
        for i, l in enumerate(d["page"]["lines"]):
            lt = set(docmap._toks(l[4]))
            if len(lt) >= 3 and len(lt & want) / len(lt) >= 0.8: out.append(i)      # short labels ("EEG") match anything
        if out and sum(len(set(docmap._toks(d["page"]["lines"][i][4]))) for i in out) > 1.6 * len(want) + 4:
            # more page text matched than was selected (common words): keep the lines that cover the selection best
            ranked = sorted(out, key=lambda i: -len(set(docmap._toks(d["page"]["lines"][i][4])) & want))
            keep, cov = [], set()
            for i in ranked:
                lt = set(docmap._toks(d["page"]["lines"][i][4]))
                if (lt & want) - cov: keep.append(i); cov |= lt & want
                if len(cov) >= 0.9 * len(want): break
            out = sorted(keep)
        r = getattr(self, "_sr", None)
        if r and out:                                                   # stay inside the selected block (other columns share words)
            out = [i for i in out if docmap._overlap(d["page"]["lines"][i][:4], r)] or out
        if len(out) > 1:                                    # keep the densest run: stray matches elsewhere on the page made page-tall boxes
            runs, cur = [], [out[0]]
            for i in out[1:]:
                if i - cur[-1] <= 3: cur.append(i)
                else: runs.append(cur); cur = [i]
            runs.append(cur)
            out = max(runs, key=len)
        return out
    def _table_for_selection(self, d):
        if hasattr(self, "_tb"): return self._tb
        self._tb = None
        st = set(docmap._toks(self.text_all))
        if not st: return None
        for f in d.get("figures") or []:
            if f.get("kind") != "table" or f.get("page") != d["page"]["n"]: continue
            b = f["bbox"]
            inside = [l for l in d["page"]["lines"] if docmap._inside(l[:4], b, 2) and set(docmap._toks(l[4])) & st]
            cov = len(st & set(t for l in inside for t in docmap._toks(l[4]))) / len(st)
            if inside and cov >= 0.6: self._tb = f; break
        return self._tb
    def _sel_region(self, d):
        """Page-space region the selection / capture covers (union of the selected lines, and the capture rect mapped back)."""
        if hasattr(self, "_sr"): return self._sr
        self._sr = None
        ix = self._doc_lines(1, len(self.lines)) if self.lines else []
        if ix:
            ls = [d["page"]["lines"][i] for i in ix]
            # keep the dominant block: lines from another column that happen to share words must not widen it
            ys = sorted(l[1] for l in ls); med = ys[len(ys) // 2]
            xs = sorted(l[0] for l in ls); mx = xs[len(xs) // 2]
            ls = [l for l in ls if abs(l[0] - mx) < 60] or ls
            ls.sort(key=lambda l: l[1]); i0 = min(range(len(ls)), key=lambda i: abs(ls[i][1] - med))
            a = b = i0                                                  # the contiguous run of lines around the median
            while a > 0 and ls[a][1] - ls[a - 1][3] < 25: a -= 1
            while b + 1 < len(ls) and ls[b + 1][1] - ls[b][3] < 25: b += 1
            ls = ls[a:b + 1]
            self._sr = [min(l[0] for l in ls) - 4, min(l[1] for l in ls) - 4, max(l[2] for l in ls) + 4, max(l[3] for l in ls) + 4]
        return self._sr
    def _visible(self, d, bbox):
        r = self._sel_region(d)
        return r is None or docmap._overlap(bbox, r)
    def _fig_mode(self, d):
        """The figure this run is about: the selection is (mostly) its caption, or the screenshot went to the vision model."""
        if getattr(self, "_fm_done", False): return self._fm
        figs = [f for f in d.get("figures") or [] if f.get("kind") == "figure" and f.get("page") == d["page"]["n"] and f.get("bbox")]
        self._fm = None
        if not figs: self._fm_done = True; return None
        sel = set(docmap._toks(self.text_all))
        best = max(figs, key=lambda f: len(sel & set(docmap._toks(f.get("caption") or ""))))
        ov = len(sel & set(docmap._toks(best.get("caption") or ""))) / max(1, len(sel))
        vision = bool(self.rec.get("image"))
        if ov >= 0.4:
            self._fm = best                                             # the selection is the caption
        elif vision:
            # the screenshot must actually contain the figure body: a paragraph that cites "Fig. 3e" stays in text mode
            reg = self._sel_region(d)
            vis = [f for f in figs if reg is None or docmap._overlap(f["bbox"], reg)]
            if vis:
                b2 = max(vis, key=lambda f: len(sel & set(docmap._toks(f.get("caption") or ""))))
                if ov >= 0.15 and b2 is best or len(vis) == 1: self._fm = b2
        self._fm_done = vision or self.rec.get("vision_gate") is not None   # decided once the vision choice is known
        self.rec["doc_figure_mode"] = bool(self._fm)
        return self._fm
    def start_capture_fit(self, png, frame):
        """Screenshot taken: fit page->screen from text recognition on it, alongside the LLM (AX rects in Preview are unreliable)."""
        self._cf, self._cf_done = None, threading.Event()
        if state.AREA_FIT:                                   # Read area already fit the page to the screen from the full OCR lines
            self._cf = state.AREA_FIT; self._cf_done.set()
            self.rec.update(doc_transform="area-fit"); return
        def run():
            t0 = time.time()
            try:
                ocr = docmap.ocr_capture(png, frame)
                d = state.DOC.get(wait=20) if (ocr and state.DOC is not None) else None
                if d and d.get("page"):
                    seen, anc = set(), []
                    for l in d["page"]["lines"]:
                        k = re.sub(r"\s+", " ", l[4].strip().lower())
                        if len(k) >= 14 and k not in seen: seen.add(k); anc.append(l)
                    t = docmap.anchor_fit(ocr, anc, frame)
                    if t:
                        self._cf = (t[0], t[1], t[2], t[3])
                        self.rec.update(doc_transform="capture-ocr", capture_fit_anchors=t[4], capture_fit_rms=round(t[3], 1))
                self.rec["capture_fit_ms"] = int((time.time() - t0) * 1000)
            except Exception as e:
                self.rec["doc_note"] = f"capture fit: {type(e).__name__}"[:160]
            finally:
                self._cf_done.set()
        threading.Thread(target=run, daemon=True).start()
    def _doc_transform(self):
        """Page -> screen: capture OCR fit when a screenshot exists; else the hotkey's per-line AX rects, else the selection rect.
        Returns "pending" while the capture fit is still running."""
        cf = getattr(self, "_cf_done", None)
        if cf is not None:
            if not cf.is_set(): return "pending"
            if self._cf: return self._cf
        if hasattr(self, "_dt"): return self._dt
        self._dt = None
        d = state.DOC.get()
        if d and d.get("page") and not self.line_bounds and self.rect:
            sr = self._sel_region(d)
            if sr:
                pr = [sr[0] + 4, sr[1] + 4, sr[2] - 4, sr[3] - 4]
                x, y, w, h = self.rect
                if pr[3] - pr[1] > 4:
                    sc = h / (pr[3] - pr[1])                       # height is reliable; web views often report the column width
                    if 0.4 <= sc <= 5:
                        self._dt = (sc, x - sc * pr[0], y - sc * pr[1], 0.0)
                        self.rec["doc_transform"] = "selection-rect"
            return self._dt
        d = state.DOC.get()
        if d and d.get("page") and self.line_bounds:
            pairs = []
            for i in range(len(self.lines)):
                ix = self._doc_lines(i + 1, i + 1)
                if len(ix) == 1:
                    l = d["page"]["lines"][ix[0]]; b = self.line_bounds[i]
                    pairs.append((l[:4], [b[0], b[1], b[0] + b[2], b[1] + b[3]]))
            t = docmap.fit(pairs)
            if t and t[3] < 12: self._dt = t; self.rec["doc_transform"] = "line-bounds"
        return self._dt
    def _doc_target(self, r, method, sentence):
        d = state.DOC.get() if state.DOC is not None else None
        if not d: return r, method
        f, panel = docmap.find_ref(sentence, d.get("figures") or [], getattr(self, "last_fig", None))
        if d["kind"] == "md":
            sel = self.text_all.strip()
            # table rows ("| Figure skaters | 24 |") and code / heading lines ("# Figure 9 ...") are text, not figures
            if re.match(r"^\s*(\||#|```|~~~|    \S)", sel) or (f and not any(x.get("label") == f.get("label") for x in d.get("figures") or [])):
                return r, method
            if not f:                                                   # only when the selection IS a figure's caption
                st = set(docmap._toks(sel))
                for x in d.get("figures") or []:
                    cap = set(docmap._toks(x.get("caption") or ""))
                    if cap and st and len(st & cap) >= 0.6 * len(cap) and len(st & cap) >= 0.5 * len(st): f = x; break
            if f:
                self.last_fig = f
                cap = f.get("caption") or ""
                nr = FocusRect(r); nr.src = [cap[:120]] if cap else getattr(r, "src", None); nr.rng = getattr(r, "rng", None); nr.nlines = getattr(r, "nlines", None)
                nr.doc = {"kind": "figure", "label": f.get("label"), "md_line": f.get("line")}
                return nr, "doc-md"
            return r, method
        if not d.get("page"): return r, method
        tgt, kind, label, shape = None, None, None, None
        fm = self._fig_mode(d)
        if f and f.get("page") != d["page"]["n"]:
            f = None                                                    # drawn on another page: named in speech, not boxed
        if fm is not None and (f is None or f is fm):
            # the run is about a figure: box the object the sentence names, else its panel, else the whole figure
            f = fm; self.last_fig = fm
            if panel: self.cur_panel = panel                             # "In Figure 4a, ..." holds until another panel is named
            panel = panel or getattr(self, "cur_panel", None)
            prect = next((p["bbox"] for p in fm.get("panels") or [] if p["label"] == panel), None) if panel else None
            fp = docmap.focus_phrase(sentence)
            use_g = GROUNDER.enabled() and docmap.content_words(fp)
            g = docmap.ground_label(fp, fm, prect) or docmap.ground_label(sentence, fm, prect)
            if g:
                tgt, kind, shape = g[0], "object", "ellipse"; label = fm["label"] + (panel or "") + ": " + g[1][:40]
                if not g[3] and use_g and docmap.device_alias(fp, fm):
                    # the label sits on a raster picture: find the named thing next to it (e.g. "the device" near "WANDmini")
                    fb = fm["bbox"]; lr = g[0]; pad = 90
                    clip = [max(fb[0], lr[0] - pad), max(fb[1], lr[1] - pad), min(fb[2], lr[2] + pad), min(fb[3], lr[3] + pad)]
                    ph = docmap.object_phrase(fp)
                    if docmap.docmap_generic(ph) or not docmap.content_words(ph): ph = g[1]
                    r.pending = GROUNDER.submit(state.DOC.path, d["page"]["n"], clip, ph); r.pending.max_frac = 0.9
            else:
                tgt, kind = (prect, "panel") if prect else (fm["bbox"], "figure"); label = fm["label"] + (panel or "")
                if use_g:
                    r.pending = GROUNDER.submit(state.DOC.path, d["page"]["n"], prect or fm["bbox"], docmap.object_phrase(fp))
        elif f and (f.get("kind") == "table" or self._visible(d, f.get("bbox") or [0, 0, 0, 0])):
            self.last_fig = f
            tgt, kind = docmap.target_rect(f, panel); label = f["label"] + (panel or "")
        elif f:
            f = None                                                    # cited but not on screen: stay on the sentence's own lines
        if tgt is None and not f:
            # a selection inside a table body (row labels, cells): box the table
            tb = self._table_for_selection(d)
            if tb: tgt, kind, label = tb["bbox"], "table", tb["label"]; f = tb
        header = None
        if tgt is not None and kind == "table" and f:
            # table: a band on the row being spoken (forward-only) + a faint header tint for the whole read
            rows = self._rows.get(f["label"]) if hasattr(self, "_rows") else None
            if rows is None:
                if not hasattr(self, "_rows"): self._rows, self._row_at = {}, {}
                rows = self._rows[f["label"]] = docmap.table_rows(d["page"]["lines"], f["bbox"], f.get("caption_bbox"))
            if len(rows) >= 2:
                i = docmap.match_row(rows, sentence, self._row_at.get(f["label"], 0))
                if i is not None:
                    self._row_at[f["label"]] = i
                    tgt, kind = rows[i]["rect"], "table_row"; label = f"{f['label']} row {i}"
                    header = rows[0]["rect"] if i > 0 else None
        if tgt is None and self.last_lines:
            ix = self._doc_lines(*self.last_lines)
            if ix:
                ls = [d["page"]["lines"][i] for i in ix]
                tgt = [min(l[0] for l in ls), min(l[1] for l in ls), max(l[2] for l in ls), max(l[3] for l in ls)]; kind = "line"
        if not tgt: return r, method
        nr = FocusRect(r); nr.src = getattr(r, "src", None); nr.rng = getattr(r, "rng", None); nr.nlines = getattr(r, "nlines", None); nr.pending = getattr(r, "pending", None)
        t = self._doc_transform()
        if t == "pending":
            nr.hold = tgt; t = None                                     # no box until the capture fit lands (never a wrong one)
        elif t:
            nr[:] = [round(v, 1) for v in docmap.apply(t, tgt)]
        nr.shape = shape or ("roundrect" if kind in ("figure", "panel", "table") else None)
        if header is not None and t and t != "pending":
            nr.header = [round(v, 1) for v in docmap.apply(t, header)]
        anc = d.get("anchors") or []
        if self.lines:
            ix = self._doc_lines(1, len(self.lines))
            own = [[round(v, 1) for v in d["page"]["lines"][i][:4]] + [d["page"]["lines"][i][4][:80]] for i in ix if len(d["page"]["lines"][i][4].strip()) >= 14]
            keys = set(a[4] for a in own)
            anc = (own + [a for a in anc if a[4] not in keys])[:48]
        nr.doc = {"kind": kind, "label": label, "page": d["page"]["n"], "page_size": d.get("page_size"), "target": [round(v, 1) for v in tgt],
                  "anchors": anc}
        if kind == "line": nr.doc["lines"] = [[round(v, 1) for v in d["page"]["lines"][i][:4]] for i in self._doc_lines(*self.last_lines)]
        if kind != "line" and f and f.get("caption"):
            nr.src = [re.sub(r"\s+", " ", f["caption"])[:100]]          # tracker finds the caption when anchors fail
        return nr, ("doc-" + kind) if (t or kind != "line" or getattr(nr, "hold", None) is not None) else method
    def _locate(self, sentence, tag):
        """Return (screen rect, method) for one spoken sentence."""
        self.n += 1
        t = parse_tag(tag) if isinstance(tag, str) else tag
        if not self.tags: t = None             # tags off: map after the fact, ignore any stray tag
        if t and t[0] == "L" and self.lines and t[1] <= len(self.lines):
            self.prev_line = min(t[2], len(self.lines)); self.last_lines = [t[1], self.prev_line]
            return self._line_rect(t[1], t[2]), "tag"
        if t and t[0] == "R":
            self.last_lines = None
            return self._norm_rect(*t[1:]), "tag"
        if self.lines:
            return self._match(sentence)
        x, y, w, h = self.rect                                    # image only, no tag: walk down in thirds
        k = min(self.n - 1, 2)
        self.last_lines = None
        return [x, y + k * h / 3.0, w, h / 3.0], "progress"
    def _scores(self, sentence):
        sf = _match_features(sentence, sentence=True)
        if not sf: return [0.0] * len(self.lines), sf
        N = len(self.lines)
        out = []
        for fl in self.feats:
            sc = 0.0
            for k, w in sf.items():
                if k in fl:
                    idf = (1.0 / self.df[k]) ** 0.5 if N > 1 else 1.0      # features found on many lines say less
                    sc += w * idf
                elif k[0] not in "#@" and len(k) >= 3:                     # arr ~ array, req ~ requests
                    hit = [j for j in fl if j[0] not in "#@" and len(j) >= 3 and (j.startswith(k) or k.startswith(j))]
                    if hit:
                        sc += 0.6 * w * (1.0 / self.df[hit[0]]) ** 0.5 if N > 1 else 0.6 * w
            out.append(sc / max(1.0, len(fl)) ** 0.15)
        return out, sf
    _REFBACK = re.compile(r"(?i)\b(as (?:mentioned|noted|said|discussed|described|shown|stated) (?:above|earlier|before|previously)|"
                          r"(?:mentioned|noted|described|discussed|shown) (?:above|earlier|previously)|as (?:we|I) (?:said|saw|noted)|recall(?:ing)? that|going back to|returning to)")
    def _span(self, a, b, sentence):
        """Cap a sentence's line span to about what its words need (no whole-selection boxes)."""
        words = len((sentence or "").split())
        wpl = max(4.0, sum(len(l.split()) for l in self.lines) / max(1, len(self.lines)))
        cap = max(1, int(words / wpl + 0.999) + 1)
        return a, min(b, a + cap - 1)
    def _match(self, sentence):
        """Forward-only overlap with the source lines: the main box never moves back up. A sentence that refers back
        (wording or a strong match on earlier lines) keeps the box forward and gets a faint secondary outline there."""
        self.ref_lines = None
        r = self._match_inner(sentence)
        return r
    def _match_inner(self, sentence):
        sc, sf = self._scores(sentence)
        cur = self.prev_line                                       # 1-based last line highlighted (0 = none yet)
        n = len(self.lines)
        adj = [v / (1.0 + 0.06 * max(0, i + 1 - (cur + 1))) for i, v in enumerate(sc)]   # prefer the next lines
        fwd = [i for i in range(n) if i + 1 >= max(1, cur)]        # current line or later
        bwd = [i for i in range(n) if i + 1 < max(1, cur)]
        bf = max(fwd, key=lambda i: adj[i]) if fwd else None
        bb = max(bwd, key=lambda i: sc[i]) if bwd else None
        T = 1.0
        best = bf
        phrase = self._REFBACK.search(sentence or "")
        # strong: earlier lines match far better than anything ahead AND share a number or a rare name (df == 1)
        uniq = [k for k, v in sf.items() if bb is not None and k in self.feats[bb] and (k.startswith("#") or (v > 1.0 and self.df.get(k, 9) == 1))]
        strong = bb is not None and sc[bb] >= T * 2.5 and sc[bb] >= 2.5 * max(0.2, sc[bf] if bf is not None else 0) and len(uniq) >= 2
        refback = bb is not None and ((phrase and sc[bb] >= T) or strong)
        self.ref_trigger = ("phrase: " + phrase.group(0)) if (refback and phrase) else (("terms: " + ",".join(k.lstrip("#") for k in uniq[:4])) if refback else None)
        if refback:                                                # never jump back: faint outline on the earlier lines
            a2 = b2 = bb
            while a2 - 1 >= 0 and sc[a2 - 1] >= max(T * 0.8, 0.6 * sc[bb]): a2 -= 1
            while b2 + 1 < n and b2 + 1 < max(1, cur) - 1 and sc[b2 + 1] >= max(T * 0.8, 0.6 * sc[bb]): b2 += 1
            a2, b2 = self._span(a2 + 1, b2 + 1, sentence)
            self.ref_lines = [a2, b2]
        lo = max(1, self.prev_start)                               # forward-only: never start above the last sentence
        if best is not None and sc[best] >= T:
            a = b = best
            while a - 1 >= lo - 1 and sc[a - 1] >= max(T * 0.8, 0.6 * sc[best]): a -= 1
            while b + 1 < n and sc[b + 1] >= max(T * 0.8, 0.6 * sc[best]): b += 1
            a, b = self._span(a + 1, b + 1, sentence)
            self.prev_line = b; self.prev_start = a; self.last_lines = [a, b]
            self.map_note = "ref-back" if refback else "forward"
            return self._line_rect(a, b), "overlap"
        a = min(n, max(cur + 1, lo)); _, b = self._span(a, n, sentence)   # proportional progress: next lines
        if refback: a = min(n, max(cur, lo)); _, b = self._span(a, n, sentence)   # a back-reference stays where we are
        self.prev_line = b; self.prev_start = a; self.last_lines = [a, b]
        self.map_note = "ref-back" if refback else "forward"
        return self._line_rect(a, b), "progress"
    def note(self, method, sentence=""):
        self.methods[method] = self.methods.get(method, 0) + 1
        if self.map_note:
            fm = self.rec.setdefault("focus_map", {}); fm[self.map_note] = fm.get(self.map_note, 0) + 1
        try:                                                       # mapping trace (log only; never shown)
            d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "focus.jsonl"), "a") as f:
                f.write(json.dumps(focus_log_record({"state": "map", "method": method, "lines": self.last_lines, "decision": self.map_note,
                                                     "ref_trigger": getattr(self, "ref_trigger", None), "ref_lines": self.ref_lines,
                                                     "text": _log_text(sentence)[:120], "ts": round(time.time(), 3), "pid": os.getpid()})) + "\n")
        except OSError: pass
    # -- events --
    def _write(self, ev):
        ev["ts"] = round(time.time(), 3); ev["pid"] = os.getpid()
        if ev.get("text"): ev["text"] = _log_text(ev["text"])
        try:
            d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "focus.jsonl"), "a") as f: f.write(json.dumps(focus_log_record(ev)) + "\n")
        except OSError: pass
        if self.dry: return
        try:
            path = os.path.join(home(), "focus.json"); tmp = path + ".tmp"
            with open(tmp, "w") as f: json.dump({k: v for k, v in ev.items() if k != "text"}, f)   # the tracker needs src, never the spoken text
            os.replace(tmp, path)
        except OSError: pass
    def show(self, rect, text):
        clamped = False
        clip = getattr(self, "area_clip", None)
        if clip and rect:                       # Read area: the user drew the region; cues never leave it (12 pt slack)
            x0, y0 = max(rect[0], clip[0] - 12), max(rect[1], clip[1] - 12)
            x1, y1 = min(rect[0] + rect[2], clip[0] + clip[2] + 12), min(rect[1] + rect[3], clip[1] + clip[3] + 12)
            if x1 - x0 < 8 or y1 - y0 < 8: x0, y0, x1, y1 = clip[0], clip[1], clip[0] + clip[2], clip[1] + clip[3]
            if [x0, y0, x1 - x0, y1 - y0] != list(rect):
                self.rec["area_clamped"] = self.rec.get("area_clamped", 0) + 1
                nr = FocusRect([x0, y0, x1 - x0, y1 - y0]); nr.__dict__.update(getattr(rect, "__dict__", {}))   # keep doc.target etc.
                rect = nr; clamped = True
        self.events += 1
        ev = {"state": "show", "seq": self.events, "rect": [round(v, 1) for v in rect], "text": text[:80], **self.style}
        if getattr(rect, "src", None): ev["src"] = rect.src
        ev["range"] = getattr(rect, "rng", None)                    # absolute AX character range, or null when unknown
        if getattr(rect, "doc", None): ev["doc"] = rect.doc          # page-space target + text anchors for the tracker
        if getattr(rect, "shape", None): ev["shape"] = rect.shape
        if getattr(rect, "nlines", None): ev["nlines"] = rect.nlines
        dk = (getattr(rect, "doc", None) or {}).get("kind")
        ev["cue"] = "figure" if (dk and dk != "line") or ev.get("shape") == "ellipse" else "text"
        # shape scheme (docs/research/highlighting.md): text_band | part_oval | figure_box | table_row (+ table_header)
        ev["cue_kind"] = ("part_oval" if ev.get("shape") == "ellipse" else "table_row" if dk == "table_row"
                          else "figure_box" if ev["cue"] == "figure" else "text_band")
        if getattr(rect, "header", None): ev["header"] = rect.header
        if clamped: ev["clamped"] = True
        dt = self._doc_transform() if getattr(rect, "doc", None) and state.DOC is not None else None
        if dt and dt != "pending": ev["doc_transform"] = [round(dt[0], 4), round(dt[1], 1), round(dt[2], 1)]
        ck = self.rec.setdefault("cue_kinds", {}); ck[ev["cue_kind"]] = ck.get(ev["cue_kind"], 0) + 1
        if ev.get("header"): ck["table_header"] = ck.get("table_header", 0) + 1
        ev["draw"] = _on("SPEAKEASY_FIGURE_CUES" if ev["cue"] == "figure" else "SPEAKEASY_TEXT_HIGHLIGHT")
        if getattr(rect, "ref", None) and not getattr(rect, "doc", None): ev["ref"] = rect.ref
        fut = getattr(rect, "pending", None)
        hold = getattr(rect, "hold", None)
        if hold is not None:
            seq = self.events
            self._write({"state": "clear", "seq": seq, "hold": True})       # hide the previous box; nothing wrong is shown
            def after_fit():
                if self._cf_done.wait(8.0) and self.events == seq and not state.STOP.is_set():
                    t = self._doc_transform()
                    if t and t != "pending":
                        ev["rect"] = [round(v, 1) for v in docmap.apply(t, hold)]; ev["late_fit"] = True
                        self.events += 1; ev["seq"] = self.events                # new key: the overlay saw this seq as the hold clear
                        if fut is not None and fut.done.is_set(): self._apply_ground(rect, fut, ev)
                        self._write(ev)
                    if not ev.get("late_fit"):
                        self.methods["fit-miss"] = self.methods.get("fit-miss", 0) + 1
                        self.events += 1; ev["seq"] = self.events; self._write(ev)                                  # fit failed: fall back to the AX-based box
            threading.Thread(target=after_fit, daemon=True).start()
            return
        if fut is not None and fut.done.is_set(): self._apply_ground(rect, fut, ev)
        self._write(ev)
        if fut is not None and not fut.done.is_set():
            seq = self.events
            def late():                                             # grounding finished after the sentence started: update the box
                if fut.done.wait(6.0) and self.events == seq and not state.STOP.is_set():
                    ev2 = dict(ev); ev2["doc"] = dict(ev["doc"])
                    if self._apply_ground(rect, fut, ev2):
                        self.events += 1; ev2["seq"] = self.events; self._write(ev2)
            threading.Thread(target=late, daemon=True).start()
    def _apply_ground(self, rect, fut, ev):
        b = fut.best()
        if not b: self.methods["ground-miss"] = self.methods.get("ground-miss", 0) + 1; return False
        ev["doc"]["target"] = [round(v, 1) for v in b]; ev["doc"]["kind"] = "object"; ev["shape"] = "ellipse"; ev["cue_kind"] = "part_oval"
        ev["doc"]["label"] = (ev["doc"].get("label") or "") + " (grounded)"
        t = self._doc_transform()
        if t and t != "pending": ev["rect"] = [round(v, 1) for v in docmap.apply(t, b)]
        self.methods["ground-vlm"] = self.methods.get("ground-vlm", 0) + 1
        self.rec["ground_ms"] = fut.ms
        return True
    def clear(self):
        self._write({"state": "clear", "seq": self.events + 1})
    def summary(self):
        tot = sum(self.methods.values())
        self.rec["focus_events"] = self.events
        self.rec["focus_tags"] = self.tags
        self.rec["focus_tag_hit_rate"] = (round(self.methods.get("tag", 0) / tot, 2) if tot else None) if self.tags else None
        fb = {k: v for k, v in self.methods.items() if k != "tag"}
        self.rec["focus_fallback"] = max(fb, key=fb.get) if fb else None
        self.rec["focus_methods"] = self.methods or None

def clear_focus_file():
    try:
        path = os.path.join(home(), "focus.json")
        if os.path.exists(path):
            with open(path + ".tmp", "w") as f: json.dump({"state": "clear", "ts": round(time.time(), 3)}, f)
            os.replace(path + ".tmp", path)
    except OSError: pass
