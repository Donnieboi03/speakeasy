"""Focus box: maps each spoken sentence to its source lines / figure part and writes focus events for the tracker."""
import hashlib, json, os, re, threading, time
from collections import Counter
import partpick
import speakeasy_state as state
from speakeasy_state import docmap
from selog import home, _on, text_logging_on, set_state
from grounding import GROUNDER
from sentence_map import Alignment, SentenceMap, SelectionSpan

def tagging():
    """Ask the model for [L]/[R] focus tags? Opt-in (SPEAKEASY_FOCUS_TAGS=on): tags cost first-audio time
    and accuracy on small models, so by default the prompt is untouched and sentences are mapped afterwards."""
    return state.FOCUS is not None and state.FOCUS.tags

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

def _union_xywh(rs):
    x0 = min(r[0] for r in rs); y0 = min(r[1] for r in rs)
    return [x0, y0, max(r[0] + r[2] for r in rs) - x0, max(r[1] + r[3] for r in rs) - y0]

class FocusRect(list):
    src = None
    rng = None
    doc = None
    shape = None
    pending = None
    sent = None
    rects = None                         # screen [x, y, w, h] per band line (text bands with line bounds)
    sentence = None                      # (sentence_id, match_score, runner_up) when the run has a sentence map

LOGGED_LABEL_RE = re.compile(r"(Figure|Table) \d+[a-h]?( row \d+)?( \(grounded\))?")

def _logged_label(label):
    """doc.label as logged and written to the live file: unchanged with SPEAKEASY_LOG_TEXT=1, else a 'Figure 3a' style
    reference only (never document text), or None."""
    if text_logging_on(): return label
    return label if label and LOGGED_LABEL_RE.fullmatch(label) else None

# Cue style written into show records: the reading band's fill, `ice` at 38 % (docs/design.md 1 and 6.6). The tracker's
# constants block holds the full cue palette; these two values must match it.
CUE_STYLE = {"color": "#A8D8EC", "opacity": 0.38}
FIG_CUE_KINDS = ("figure_box", "part_oval", "table_row")

def _fig_id(page, f):
    """p<page>:fig<n> / p<page>:tab<n>, n = the item's 1-based index among the page parse's figures (or tables) in
    reading order; None when f is not one of the page's items."""
    tables = f.get("kind") == "table"
    same = [x for x in page["figures"] if (x.get("kind") == "table") == tables]
    i = next((k for k, x in enumerate(same) if x is f), None)
    return None if i is None else f"p{page['n']}:{'tab' if tables else 'fig'}{i + 1}"

def _box_target_src(f, kind, panel):
    """doc.target_src of a figure / table / panel box taken from the parse entry f."""
    if kind == "panel":
        return "panel-guessed" if next(p for p in f["panels"] if p["label"] == panel).get("guessed") else "panel-labels"
    return f"{kind}:{f['bbox_src']}"

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
        self.run_t0 = rec.get("_t0") or time.time()
        self.prev_line, self.n, self.events, self.methods = 0, 0, 0, {}
        self.match_score = self.match_runner_up = None
        self.ground_requests = []                       # every grounding request / pick submitted this run
        self.pick_skipped = {"too-few": 0, "whole-figure": 0, "disabled": 0, "doc-not-ready": 0, "degenerate-figure": 0}
        self.prev_start, self.ref_lines, self.map_note = 1, None, None
        self.sentence_id = self.sentence_ids = None
        self.prev_band, self.prev_sentence = None, (None, [])
        self.smap, self.smap_ms, self._attach_lock = None, 0, threading.Lock()
        if self.lines:                                  # before the first chunk; never waits for the document
            t0 = time.time()
            self.smap = SentenceMap(text, line_bounds)
            self.smap_ms = int((time.time() - t0) * 1000)
            self._log_sentence_map()
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
        self.style = dict(CUE_STYLE)
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
    def _log_sentence_map(self):
        """focus-log sentence_map record (counts only) and the runs-log summary; again when page rects attach."""
        summary = dict(self.smap.summary(), ms=self.smap_ms)
        self.rec["sentence_map"] = summary
        try:
            d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "focus.jsonl"), "a") as f:
                f.write(json.dumps({"state": "sentence_map", **summary, "ts": round(time.time(), 3), "pid": os.getpid()}) + "\n")
        except OSError: pass
    def _attach_page(self):
        """Page rects join the map once the document job has matched a page (shows before that are screen-only)."""
        if self.smap is None or self.smap.page_line_of_key is not None or state.DOC is None: return
        d = state.DOC.get()
        if not d or not d.get("page"): return
        with self._attach_lock:                                         # chunks are located and shown on two threads
            if self.smap.page_line_of_key is not None: return
            t0 = time.time()
            self.smap.attach_page(d["page"], self._sel_region(d))
            self.smap_ms += int((time.time() - t0) * 1000)
            self._log_sentence_map()
    def _assign(self, sentence, tag):
        """C.1 / C.3 / E: (method, alignment, back-reference alignment) for one chunk."""
        m = self.smap
        self._attach_page()
        if isinstance(tag, SelectionSpan):
            a = m.span_alignment(tag) if tag.start is not None else None
            if a: return "span", a, None
            return self._fallback(None)
        t = parse_tag(tag) if isinstance(tag, str) else tag
        a = None
        if self.tags and t and t[0] == "L" and m.line_spans:
            a = m.align(sentence, *m.line_window(t[1], t[2]))
        a = a or m.align(sentence)
        if a and a.kind == "verbatim": return "align", a, None
        if self._area_run():                        # area prose read verbatim out of order keeps its text band too (A6)
            back = m.align_back(sentence)
            if back and back.kind == "verbatim": return "align", back, None
        if self._text_table_rows() and not self._has_page():           # C.4.3 (amendment 5): the row line the chunk names
            row = self._named_row(sentence)
            if row is None: return self._fallback(None)
            ka, kb = m.key_range(*m.line_spans[row])
            return "overlap", Alignment("row", ka, kb, self.match_score, self.match_runner_up), None
        if a: return "align-fuzzy", a, None
        return self._fallback(m.align_back(sentence))
    def _has_page(self):
        d = state.DOC.get() if state.DOC is not None else None
        return bool(d and d.get("page"))
    def _text_table_rows(self):
        """Selection lines of a pipe- or tab-separated text table (separator rows excluded), or [] for other text."""
        if not hasattr(self, "_table_rows"):
            cells = [i for i, l in enumerate(self.lines) if ("|" in l or "\t" in l) and not re.fullmatch(r"[\s|:\-+]+", l)]
            self._table_rows = cells if len(cells) >= 2 and len(cells) >= 0.6 * len(self.lines) else []
        return self._table_rows
    def _named_row(self, sentence):
        """The one table row a chunk names by its label or distinctive cells (the Phase 2 line scores), or None."""
        sc, _ = self._scores(sentence)
        rows = self._text_table_rows()
        best = max(rows, key=lambda i: sc[i])
        if sc[best] < 1.0 or any(sc[i] >= max(0.8, 0.6 * sc[best]) for i in rows if i != best): return None
        others = [sc[i] for i in rows if i != best]
        self.match_score, self.match_runner_up = round(sc[best], 2), round(max(others), 2) if others else None
        return best
    def _no_stand_in(self):
        """A2.3: figure mode, vision, explain / summary modes and area runs never fall back to the whole selection or area,
        unless the selection is one source sentence."""
        if len(self.smap.sentences) == 1: return False
        reading = self.rec.get("mode") in ("read-through", "verbatim", "table") and os.environ.get("SPEAKEASY_READ_MODE", "read").lower() != "summary"
        return self._area_run() or bool(self.rec.get("image")) or not reading or getattr(self, "_fm", None) is not None
    def _fallback(self, back):
        """Hold the current band (a back-reference, or a read-through / verbatim chunk below 0.6), else the whole block;
        where stand-ins are not allowed (A2.3), hold the current band, else nothing new ("none")."""
        holds = self.rec.get("mode") in ("read-through", "verbatim", "table") and os.environ.get("SPEAKEASY_READ_MODE", "read").lower() != "summary"
        if self.prev_band is not None and (back is not None or holds): return "hold", None, back
        if self._no_stand_in():
            if getattr(self, "_fm", None) is None:                    # in figure mode the figure cue replaced the block anyway
                self.rec["stand_in_blocked"] = self.rec.get("stand_in_blocked", 0) + 1
            return ("hold", None, back) if self.prev_band is not None else ("none", None, None)
        return "block", None, None
    def _locate_sentence(self, method, a, back):
        """Screen rect for an assigned chunk: one rect per band line (unmerged), the union as the rect."""
        self.n += 1
        m = self.smap
        if a is not None:
            band = dict(m.band_for(a), how="verbatim" if a.kind in ("verbatim", "span") else "row" if a.kind == "row" else "fuzzy")
            if a.kind != "row": m.commit(a)                             # an explanation names table rows in any order
            ids = m.sentence_ids(a.key_start, a.key_end)
            primary = m.primary_sentence(a.key_start, a.key_end)
            self.sentence_id = m.sentences[primary]["id"] if primary is not None else None
            self.sentence_ids = [m.sentences[i]["id"] for i in ids]
            self.match_score = round(a.score, 2); self.match_runner_up = None if a.runner_up is None else round(a.runner_up, 2)
            self.prev_band, self.prev_sentence = band, (self.sentence_id, self.sentence_ids)
        elif method == "hold":                                          # page rects may have joined since it was assigned
            band = dict(self.prev_band, how="hold", page_lines=m.band_for_keys(self.prev_band["keys"])["page_lines"])
            self.sentence_id, self.sentence_ids = self.prev_sentence
        else:
            band = dict(m.block_band(), how="block"); self.sentence_id, self.sentence_ids = None, []
            self.prev_band, self.prev_sentence = band, (None, [])
        sel = band["sel_lines"]
        self.last_lines = [sel[0] + 1, sel[-1] + 1] if sel else None
        if self.last_lines: self.prev_start, self.prev_line = self.last_lines
        self.map_note = "ref-back" if back is not None else "forward"
        if band["rects"]:
            rect = _union_xywh(band["rects"])
        else:                                                           # no line bounds: the span's share of the selection
            x, y, w, h = self.rect
            cs, ce = band["char_span"] or (0, len(self.text_all))
            f0, f1 = cs / max(1, len(self.text_all)), ce / max(1, len(self.text_all))
            rect = [x, y + f0 * h, w, max(14.0, (f1 - f0) * h)]
        r = FocusRect(rect)
        if band["rects"]: r.rects = [[round(v, 1) for v in b] for b in band["rects"]]
        r.nlines = len(band["rects"]) or len(band["page_lines"]) or len(sel) or None
        r.src = [_rendered_line(self.lines[i]) for i in sel][:6] if sel else None
        if m.ax_base is not None and band["char_span"]:
            r.rng = [m.ax_base + band["char_span"][0], band["char_span"][1] - band["char_span"][0]]
        if back is not None:
            ref = m.band_for(back)
            if ref["sel_lines"]: self.ref_lines = [ref["sel_lines"][0] + 1, ref["sel_lines"][-1] + 1]
            if ref["rects"]: r.ref = [round(v, 1) for v in _union_xywh(ref["rects"])]
            elif self.ref_lines: r.ref = [round(v, 1) for v in self._line_rect(*self.ref_lines)]
        r.band = band
        return r, method
    def _no_target(self, sentence):
        """A2.3, no aligned span and no current band: the figure / panel / part cue in figure mode; else the current cue
        stays ("hold", nothing new drawn) or there is none ("none"). Returns (rect or None, method)."""
        self.n += 1; self.last_lines = None; self.map_note = None
        self.sentence_id, self.sentence_ids = None, []
        r = FocusRect(self.rect); r.sent = self.n - 1; r.sentence = (None, None, None)
        try: nr, method = self._doc_target(r, "none", sentence)
        except Exception as e:                                         # document mapping never breaks speech
            self.rec["doc_note"] = f"focus: {type(e).__name__}: {e}"[:160]; nr = r
        if nr is not r and (nr.doc or {}).get("kind") not in (None, "line"):
            nr.sent, nr.sentence = r.sent, r.sentence
            return nr, method
        self.rec["no_target_chunks"] = self.rec.get("no_target_chunks", 0) + 1
        return None, "hold" if getattr(self, "_cued", False) else "none"
    def _band_target(self, r, method, sentence):
        """C.4 on an assigned chunk: its text band (method stays the assignment method, F amendment 3) or a Phase 2
        figure / table cue (doc-<kind>, no score)."""
        try:
            nr, doc_method = self._doc_target(r, method, sentence, r.band)
        except Exception as e:                                         # document mapping never breaks speech
            self.rec["doc_note"] = f"focus: {type(e).__name__}: {e}"[:160]
            return r, method
        if nr is r: return r, method
        nr.sent, nr.band, nr.ref = r.sent, r.band, getattr(r, "ref", None)
        if (nr.doc or {}).get("kind") == "line":                       # D: len(rects) when rects is present, else len(doc.lines)
            nr.sentence, nr.nlines = r.sentence, len(nr.rects) if nr.rects else len(nr.doc["lines"])
            return nr, method
        nr.sentence = (r.sentence[0], None, None)
        return nr, doc_method
    def _late_page(self, rect, text):
        """A band located before the document was read: page rects join it when it starts playing (B, document not ready)."""
        band = getattr(rect, "band", None)
        if self.smap is None or band is None or rect.doc is not None: return rect
        self._attach_page()
        if self.smap.page_line_of_key is None: return rect
        rect.band = dict(band, page_lines=self.smap.band_for_keys(band["keys"])["page_lines"])
        rect.late = True                                                # located before the parse: never picked (A3.3)
        return self._band_target(rect, None, text)[0]
    def locate(self, sentence, tag):
        """(rect, method); the rect also carries .src (source lines, for the live tracker to re-find on screen)
        and .rng (absolute AX character range of those lines, when the hotkey supplied per-line offsets).
        rect is None when the chunk draws nothing new (A2.3)."""
        # the held panel follows the chunks in speech order, whether or not the document was read when each was located,
        # so a chunk's scope never depends on parse timing (A3.3)
        self._held_panel = docmap.said_panel(sentence) or getattr(self, "_held_panel", None)
        r, method = self._locate_chunk(sentence, tag)
        if r is not None: self._cued = True; r.held_panel = self._held_panel
        return r, method
    def _locate_chunk(self, sentence, tag):
        self.ref_lines = None; self.map_note = None; self.match_score = self.match_runner_up = None
        self.sentence_id = self.sentence_ids = None
        if self.smap is not None:
            try:
                assigned = self._assign(sentence, tag)
            except Exception as e:                                     # alignment never breaks speech: Phase 2 matcher
                self.rec["doc_note"] = f"align: {type(e).__name__}: {e}"[:160]
                assigned = None
            if assigned is not None and assigned[0] == "none": return self._no_target(sentence)
            if assigned is not None:
                r, method = self._locate_sentence(*assigned)
                r.sent = self.n - 1
                table_row = assigned[0] == "overlap"                           # a Phase 2 table cue: no show score
                r.sentence = (self.sentence_id, None if table_row else self.match_score, None if table_row else self.match_runner_up)
                return self._band_target(r, method, sentence)
        if isinstance(tag, SelectionSpan): tag = None
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
        r.sent = self.n - 1
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
        return self._eligible(d, out)
    def _selection_page_lines(self):
        """Page lines of the whole selection: the sentence map's page alignment when attached, else by text (Phase 2)."""
        if self.smap is not None and self.smap.page_line_of_key is not None:
            return self.smap.block_band()["page_lines"]
        return self._doc_lines(1, len(self.lines))
    def _table_for_selection(self, d):
        if hasattr(self, "_tb"): return self._tb
        self._tb = None
        st = set(docmap._toks(self.text_all))
        if not st: return None
        for f in d.get("figures") or []:
            if f.get("kind") != "table" or f.get("page") != d["page"]["n"]: continue
            b = docmap._union([f["bbox"], f.get("caption_bbox") or f["bbox"]])   # the table box no longer holds its caption
            inside = [l for l in d["page"]["lines"] if docmap._inside(l[:4], b, 2) and set(docmap._toks(l[4])) & st]
            cov = len(st & set(t for l in inside for t in docmap._toks(l[4]))) / len(st)
            if inside and cov >= 0.6: self._tb = f; break
        return self._tb
    def _area_region(self):
        """A2.1, area runs: the area's page rect from the area fit, else the area mapped through the capture fit once it
        lands, else None (area_region_src "none": screen-only cues)."""
        if self.rec.get("area_page_rect"): return self.rec["area_page_rect"]
        cf = getattr(self, "_cf_done", None)
        if cf is not None and not cf.is_set(): return None                  # the capture fit is still running
        if cf is not None and self._cf:
            s, ox, oy = self._cf[:3]; x, y, w, h = self.area_clip
            self.rec.update(area_page_rect=[round((x - ox) / s, 1), round((y - oy) / s, 1), round((x + w - ox) / s, 1), round((y + h - oy) / s, 1)],
                            area_region_src="capture-fit")
            return self.rec["area_page_rect"]
        self.rec["area_region_src"] = "none"
        return None
    def _area_run(self):
        return getattr(self, "area_clip", None) is not None
    def _eligible(self, d, ix):
        """A2.1: in area runs only page lines whose centre lies inside the area rect may join a band."""
        if not self._area_run(): return ix
        reg = self._area_region()
        return [i for i in ix if reg and docmap._centre_in(d["page"]["lines"][i], reg)]
    def _sel_region(self, d):
        """Page-space region the selection / capture covers (union of the selected lines, and the capture rect mapped back);
        in area runs the area rect (A2.1)."""
        if self._area_run(): return self._area_region()
        if hasattr(self, "_sr"): return self._sr
        self._sr = None
        ix = self._doc_lines(1, len(self.lines)) if self.lines else []
        if ix:
            ls = [d["page"]["lines"][i] for i in ix]
            # keep the dominant block's column: lines from another column that happen to share words must not widen it
            ys = sorted(l[1] for l in ls); med = ys[len(ys) // 2]
            blk = docmap.paragraph_region(d["page"], [sorted(ix, key=lambda i: d["page"]["lines"][i][0])[len(ix) // 2]])
            if blk: ls = [l for l in ls if blk[0] - 4 <= (l[0] + l[2]) / 2 <= blk[2] + 4] or ls
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
        # a degenerate (hairline) figure is never a figure-mode figure (A2.4)
        figs = [f for f in d.get("figures") or [] if f.get("kind") == "figure" and f.get("page") == d["page"]["n"] and f.get("bbox") and not f.get("degenerate")]
        self._fm = None; self.rec["figure_mode_src"] = None
        if not figs: self._fm_done = True; self.rec["doc_figure_mode"] = False; return None
        if self._area_run():
            reg = self._area_region()
            if reg is None:
                self._fm_done = self.rec.get("area_region_src") == "none"; self.rec["doc_figure_mode"] = False
                return None
            elig = [l for l in d["page"]["lines"] if docmap._centre_in(l, reg)]
            best = None
            for f in figs:                                              # A2.2: the area covers the figure, or is mostly its labels
                inter = docmap._area(docmap._clip(f["bbox"], reg)) if docmap._overlap(f["bbox"], reg) else 0.0
                src = ("area-overlap" if inter >= 0.5 * docmap._area(f["bbox"]) or inter >= 0.5 * docmap._area(reg)
                       else "area-labels" if elig and sum(docmap._centre_in(l, f["bbox"]) for l in elig) >= 0.5 * len(elig) else None)
                if src and (best is None or inter > best[0]): best = (inter, f, src)
            if best: self._fm, self.rec["figure_mode_src"] = best[1], best[2]
            self._fm_done = True; self.rec["doc_figure_mode"] = bool(self._fm)
            return self._fm
        sel = set(docmap._toks(self.text_all))
        best = max(figs, key=lambda f: len(sel & set(docmap._toks(f.get("caption") or ""))))
        ov = len(sel & set(docmap._toks(best.get("caption") or ""))) / max(1, len(sel))
        vision = bool(self.rec.get("image"))
        if ov >= 0.4:
            self._fm = best; self.rec["figure_mode_src"] = "caption"      # the selection is the caption
        elif vision:
            # the screenshot must actually contain the figure body: a paragraph that cites "Fig. 3e" stays in text mode
            reg = self._sel_region(d)
            vis = [f for f in figs if reg is None or docmap._overlap(f["bbox"], reg)]
            if vis:
                b2 = max(vis, key=lambda f: len(sel & set(docmap._toks(f.get("caption") or ""))))
                if ov >= 0.15 and b2 is best or len(vis) == 1: self._fm = b2; self.rec["figure_mode_src"] = "vision"
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
    def _doc_target(self, r, method, sentence, band=None):
        """Page-space target. band (runs with a sentence map): the chunk's text band; C.4 decides between it and the
        Phase 2 figure / table cues."""
        d = state.DOC.get() if state.DOC is not None else None
        if not d: return r, method
        f, panel = docmap.find_ref(sentence, d.get("figures") or [], getattr(self, "last_fig", None))
        verbatim = band is not None and band["how"] == "verbatim"
        if d["kind"] == "md":
            sel = self.text_all.strip()
            # table rows ("| Figure skaters | 24 |") and code / heading lines ("# Figure 9 ...") are text, not figures
            if re.match(r"^\s*(\||#|```|~~~|    \S)", sel) or (f and not any(x.get("label") == f.get("label") for x in d.get("figures") or [])):
                return r, method
            if not f and not verbatim:                                  # only when the selection IS a figure's caption
                st = set(docmap._toks(sel))
                for x in d.get("figures") or []:
                    cap = set(docmap._toks(x.get("caption") or ""))
                    if cap and st and len(st & cap) >= 0.6 * len(cap) and len(st & cap) >= 0.5 * len(st): f = x; break
            if f:
                self.last_fig = f
                cap = f.get("caption") or ""
                nr = FocusRect(r); nr.src = [cap[:120]] if cap else getattr(r, "src", None); nr.rng = getattr(r, "rng", None); nr.nlines = getattr(r, "nlines", None)
                nr.doc = {"kind": "figure", "label": _logged_label(f.get("label")), "md_line": f.get("line")}
                return nr, "doc-md"
            return r, method
        if not d.get("page"): return r, method
        if self._area_run() and self._area_region() is None: return r, method   # no page region: screen-only cues (A2.1)
        tgt, kind, label, shape, target_src = None, None, None, None, None
        fm = self._fig_mode(d)
        if f and f.get("page") != d["page"]["n"]:
            f = None                                                    # drawn on another page: named in speech, not boxed
        if f and f.get("degenerate"):
            f = None; self.pick_skipped["degenerate-figure"] += 1      # a hairline box is never drawn or grounded (A2.4)
        named = f is not None and f is not fm and (f.get("kind") == "table" or self._visible(d, f.get("bbox") or [0, 0, 0, 0]))
        text_band = band is not None and not named and (verbatim or (fm is None and not self._table_for_selection(d)))
        if text_band:
            f = None                                                    # C.4.2 / C.4.4 / E: the chunk's own text band
        elif fm is not None and (f is None or f is fm):
            # the run is about a figure: the callout the sentence names, else the object its words name, else its panel,
            # else the whole figure (a Set-of-Mark pick may replace that box later)
            f = fm; self.last_fig = fm
            said_panel = panel or docmap.said_panel(sentence)          # "In Figure 4a, ..." holds until another panel is named
            panel = said_panel or getattr(r, "held_panel", getattr(self, "_held_panel", None))
            prect = next((p["bbox"] for p in fm.get("panels") or [] if p["label"] == panel), None) if panel else None
            area = self._sel_region(d) if self._area_run() else None     # A2.9: area runs scope to the panel / figure in the area
            fp = docmap.focus_phrase(sentence)
            cref, g, cp = docmap.callout_ref(sentence), None, None
            if cref is not None:                                        # a callout reference skips word matching
                cp = docmap.callout_part(fm, cref, said_panel, panel, area)
                said = said_panel if any(p["label"] == said_panel for p in fm.get("panels") or []) else None
                pick_rect, pick_panel = (next(p["bbox"] for p in fm["panels"] if p["label"] == said), said) if said else (fm["bbox"], None)
            else:
                hint = docmap._clip(prect or fm["bbox"], area) if area else prect
                g = docmap.ground_label(fp, fm, hint) or docmap.ground_label(sentence, fm, hint)
                pick_rect, pick_panel = (prect, panel) if prect else (fm["bbox"], None)
            if area: pick_rect = docmap._clip(pick_rect, area)
            if cp:
                tgt, kind, shape = cp["rect"], "object", "ellipse"; label = fm["label"] + (cp["panel"] or "")
                target_src = "callout" if cp["attached"] else "callout-marker"     # no eligible target: the number itself (A4.1)
            elif g:
                tgt, kind, shape = g[0], "object", "ellipse"; label = fm["label"] + (panel or "") + (": " + g[1][:40] if text_logging_on() else "")
                target_src = "label-object" if g[3] else "label-pad"
                if not g[3] and docmap.device_alias(fp, fm):
                    # the label sits on a raster picture: pick the named thing next to it (e.g. "the device" near "WANDmini"),
                    # which must lie within the label's neighbourhood (A2.6)
                    fb = fm["bbox"]; lr = g[0]; pad = 90
                    clip = [max(fb[0], lr[0] - pad), max(fb[1], lr[1] - pad), min(fb[2], lr[2] + pad), min(fb[3], lr[3] + pad)]
                    self._consider_pick(r, d, fm, (pick_rect, pick_panel), sentence, False, region=clip)
            else:
                tgt, kind = (prect, "panel") if prect else (fm["bbox"], "figure"); label = fm["label"] + (panel or "")
                target_src = _box_target_src(fm, kind, panel)
                # A2.5 / Amendment 1: only a chunk that names a specific object may turn the box into an oval
                if cref is not None or docmap.names_object(sentence): self._consider_pick(r, d, fm, (pick_rect, pick_panel), sentence, cref is not None)
                else: self.pick_skipped["whole-figure"] += 1
        elif f and (f.get("kind") == "table" or self._visible(d, f.get("bbox") or [0, 0, 0, 0])):
            self.last_fig = f
            tgt, kind = docmap.target_rect(f, panel); label = f["label"] + (panel or "")
            target_src = _box_target_src(f, kind, panel)
        elif f:
            f = None                                                    # cited but not on screen: stay on the sentence's own lines
        if tgt is None and not f and not text_band:
            # a selection inside a table body (row labels, cells): box the table
            tb = self._table_for_selection(d)
            if tb: tgt, kind, label = tb["bbox"], "table", tb["label"]; f = tb; target_src = _box_target_src(tb, kind, None)
        header = None
        if tgt is not None and kind == "table" and f:
            # table: a band on the row being spoken (forward-only) + a faint header tint for the whole read
            rows = self._rows.get(f["label"]) if hasattr(self, "_rows") else None
            if rows is None:
                if not hasattr(self, "_rows"): self._rows, self._row_at = {}, {}
                lines = [d["page"]["lines"][i] for i in self._eligible(d, range(len(d["page"]["lines"])))]
                rows = self._rows[f["label"]] = docmap.table_rows(lines, f["bbox"], f.get("caption_bbox"))
            if len(rows) >= 2:
                i = docmap.match_row(rows, sentence, self._row_at.get(f["label"], 0))
                if i is not None:
                    self._row_at[f["label"]] = i
                    tgt, kind = rows[i]["rect"], "table_row"; label = f"{f['label']} row {i}"; target_src = "table-rows"
                    header = rows[0]["rect"] if i > 0 else None
        line_ix = None
        if tgt is None and (band is not None or self.last_lines):
            line_ix = band["page_lines"] if band is not None else self._doc_lines(*self.last_lines)   # never bag-of-words for a band
            line_ix = self._eligible(d, line_ix)
            if line_ix:
                ls = [d["page"]["lines"][i] for i in line_ix]
                tgt = [min(l[0] for l in ls), min(l[1] for l in ls), max(l[2] for l in ls), max(l[3] for l in ls)]; kind = "line"; target_src = "lines"
        if not tgt: return r, method
        nr = FocusRect(r); nr.src = getattr(r, "src", None); nr.rng = getattr(r, "rng", None); nr.nlines = getattr(r, "nlines", None); nr.pending = getattr(r, "pending", None)
        if kind == "line": nr.rects = r.rects
        t = self._doc_transform()
        if t == "pending" and not nr.rects:
            nr.hold = tgt; t = None                                     # no box until the capture fit lands (never a wrong one)
        elif t == "pending":
            t = None                                                    # the band's own screen rects draw at once
        elif t and not nr.rects:
            nr[:] = [round(v, 1) for v in docmap.apply(t, tgt)]
        nr.shape = shape or ("roundrect" if kind in ("figure", "panel", "table") else None)
        if header is not None and t and t != "pending":
            nr.header = [round(v, 1) for v in docmap.apply(t, header)]
        anc = d.get("anchors") or []
        if self.lines:
            ix = self._selection_page_lines()
            own = [[round(v, 1) for v in d["page"]["lines"][i][:4]] + [d["page"]["lines"][i][4][:80]] for i in ix if len(d["page"]["lines"][i][4].strip()) >= 14]
            keys = set(a[4] for a in own)
            anc = (own + [a for a in anc if a[4] not in keys])[:48]
        nr.doc = {"kind": kind, "label": _logged_label(label), "page": d["page"]["n"], "page_size": d.get("page_size"), "target": [round(v, 1) for v in tgt],
                  "anchors": anc, "target_src": target_src}
        if kind in ("figure", "table", "table_row") and f and f.get("split_from"): nr.doc["split_from"] = f["split_from"]
        if kind != "line" and f: nr.fig_id = _fig_id(d["page"], f)
        if kind == "line": nr.doc["lines"] = [[round(v, 1) for v in d["page"]["lines"][i][:4]] for i in line_ix]
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
        def log_scores(a, b):                                      # map-record score / runner_up of the chosen 1-based span a..b
            if bf is None: return
            self.match_score = round(sc[bf], 2)
            others = [sc[i] for i in fwd if not a - 1 <= i <= b - 1]
            self.match_runner_up = round(max(others), 2) if others else None
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
            log_scores(a, b)
            return self._line_rect(a, b), "overlap"
        a = min(n, max(cur + 1, lo)); _, b = self._span(a, n, sentence)   # proportional progress: next lines
        if refback: a = min(n, max(cur, lo)); _, b = self._span(a, n, sentence)   # a back-reference stays where we are
        log_scores(a, b)
        self.prev_line = b; self.prev_start = a; self.last_lines = [a, b]
        self.map_note = "ref-back" if refback else "forward"
        return self._line_rect(a, b), "progress"
    def note(self, method, sentence=""):
        self.methods[method] = self.methods.get(method, 0) + 1
        if self.map_note:
            fm = self.rec.setdefault("focus_map", {}); fm[self.map_note] = fm.get(self.map_note, 0) + 1
        rec = {"state": "map", "method": method, "lines": self.last_lines, "decision": self.map_note,
               "ref_trigger": getattr(self, "ref_trigger", None), "ref_lines": self.ref_lines,
               "text": _log_text(sentence)[:120], "ts": round(time.time(), 3), "pid": os.getpid(),
               "sent": self.n - 1, "score": self.match_score, "runner_up": self.match_runner_up}
        if self.smap is not None: rec.update(sentence_id=self.sentence_id, sentence_ids=self.sentence_ids)
        try:                                                       # mapping trace (log only; never shown)
            d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "focus.jsonl"), "a") as f: f.write(json.dumps(focus_log_record(rec)) + "\n")
        except OSError: pass
    # -- events --
    def _write(self, ev):
        ev["ts"] = round(time.time(), 3); ev["pid"] = os.getpid()
        if ev.get("text"): ev["text"] = _log_text(ev["text"])
        try:
            d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "focus.jsonl"), "a") as f: f.write(json.dumps(focus_log_record(ev)) + "\n")
            if ev["state"] == "show" and self.rec.get("first_focus_ms") is None:
                self.rec["first_focus_ms"] = int((time.time() - self.run_t0) * 1000)
        except OSError: pass
        if self.dry: return
        if ev["state"] == "show":
            set_state(seq=ev["seq"], cue_kind=ev.get("cue_kind"), label=(ev.get("doc") or {}).get("label"))
        try:
            path = os.path.join(home(), "focus.json"); tmp = path + ".tmp"
            with open(tmp, "w") as f: json.dump({k: v for k, v in ev.items() if k != "text"}, f)   # the tracker needs src, never the spoken text
            os.replace(tmp, path)
        except OSError: pass
    def show(self, rect, text):
        rect = self._late_page(rect, text)
        clamped = False
        clip = getattr(self, "area_clip", None)
        if clip and rect:                       # Read area: the user drew the region; cues never leave it (12 pt slack)
            x0, y0 = max(rect[0], clip[0] - 12), max(rect[1], clip[1] - 12)
            x1, y1 = min(rect[0] + rect[2], clip[0] + clip[2] + 12), min(rect[1] + rect[3], clip[1] + clip[3] + 12)
            if (x1 - x0 < 8 or y1 - y0 < 8) and [x0, y0, x1 - x0, y1 - y0] != list(rect):   # A2.1: never the whole area; the previous cue stays
                self.rec["area_dropped"] = self.rec.get("area_dropped", 0) + 1
                return
            if [x0, y0, x1 - x0, y1 - y0] != list(rect):
                self.rec["area_clamped"] = self.rec.get("area_clamped", 0) + 1
                nr = FocusRect([x0, y0, x1 - x0, y1 - y0]); nr.__dict__.update(getattr(rect, "__dict__", {}))   # keep doc.target etc.
                if nr.rects:
                    inside = [[max(b[0], clip[0] - 12), max(b[1], clip[1] - 12), min(b[0] + b[2], clip[0] + clip[2] + 12), min(b[1] + b[3], clip[1] + clip[3] + 12)]
                              for b in nr.rects]
                    nr.rects = [[b[0], b[1], b[2] - b[0], b[3] - b[1]] for b in inside if b[2] - b[0] >= 8 and b[3] - b[1] >= 8] or None
                rect = nr; clamped = True
        self.events += 1
        ev = {"state": "show", "seq": self.events, "rect": [round(v, 1) for v in rect], "text": text[:80], **self.style, "sent": rect.sent}
        if self.smap is not None:
            ev["sentence_id"], ev["match_score"], ev["runner_up"] = rect.sentence or (None, None, None)
        if rect.rects: ev["rects"] = rect.rects
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
        fid = getattr(rect, "fig_id", None)
        if fid and ev["cue_kind"] in FIG_CUE_KINDS:
            ev["fig_id"] = fid; ids = self.rec.setdefault("_fig_ids", [])
            if fid not in ids: ids.append(fid)
        if ev.get("header"): ck["table_header"] = ck.get("table_header", 0) + 1
        ev["draw"] = _on("SPEAKEASY_FIGURE_CUES" if ev["cue"] == "figure" else "SPEAKEASY_TEXT_HIGHLIGHT")
        if getattr(rect, "ref", None) and (rect.doc or {}).get("kind") in (None, "line"): ev["ref"] = rect.ref   # C.3 back-reference outline
        fut = getattr(rect, "pending", None)
        hold = getattr(rect, "hold", None)
        if hold is not None:
            seq = self.events
            self._write({"state": "clear", "seq": seq, "hold": True, "sent": rect.sent})       # hide the previous box; nothing wrong is shown
            def after_fit():
                fit_done = self._cf_done.wait(8.0)
                if fit_done and self.events == seq and not state.STOP.is_set():
                    t = self._doc_transform()
                    if t and t != "pending":
                        ev["rect"] = [round(v, 1) for v in docmap.apply(t, hold)]; ev["late_fit"] = True
                        self.events += 1; ev["seq"] = self.events                # new key: the overlay saw this seq as the hold clear
                        if fut is not None:
                            if fut.done.is_set(): self._apply_ground(rect, fut, ev)
                            else: self._late_ground(rect, fut, ev)
                        self._write(ev)
                    if not ev.get("late_fit"):
                        self.methods["fit-miss"] = self.methods.get("fit-miss", 0) + 1
                        if fut is not None: self._ground_missed(fut, "no-fit")
                        self.events += 1; ev["seq"] = self.events; self._write(ev)                                  # fit failed: fall back to the AX-based box
                elif fut is not None and not state.STOP.is_set():
                    self._ground_missed(fut, "superseded" if fit_done else "no-fit")
            threading.Thread(target=after_fit, daemon=True).start()
            return
        if fut is not None and fut.done.is_set(): self._apply_ground(rect, fut, ev)
        self._write(ev)
        if fut is not None and not fut.done.is_set(): self._late_ground(rect, fut, ev)
    def _late_ground(self, rect, fut, ev):
        """The answer arrives after the chunk's show: update the box while the chunk is still the current one."""
        seq = self.events
        def late():
            answered = fut.wait()
            if state.STOP.is_set(): return
            if not answered: return                                     # summary() settles it
            if self.events != seq: self._ground_missed(fut, "superseded"); return
            ev2 = dict(ev); ev2["doc"] = dict(ev["doc"])
            if self._apply_ground(rect, fut, ev2):
                self.events += 1; ev2["seq"] = self.events; self._write(ev2)
        threading.Thread(target=late, daemon=True).start()
    def _consider_pick(self, r, d, fm, scope, sentence, callout, region=None):
        """Contract 4.1 / A3.3: queue a Set-of-Mark pick for a figure chunk with no deterministic oval, located against the
        finished parse of its page, or count why not (part_picks.skipped)."""
        rect, panel = scope
        area = self._sel_region(d) if self._area_run() else None
        if not GROUNDER.pick_enabled(): why = "disabled"
        elif getattr(r, "late", False) or not d["page"].get("figs_done") or any("parts" not in f for f in d["page"]["figures"]): why = "doc-not-ready"
        else:
            cands = [c for c in partpick.candidates(fm, rect, panel, callout) if area is None or docmap._centre_in(c, area)]
            why = "too-few" if len(cands) < 2 else None
        if why: self.pick_skipped[why] += 1; return
        meta = {"sent": r.sent if r.sent is not None else self.n - 1, "sentence_id": r.sentence[0] if r.sentence else self.sentence_id,
                "fig": fm["label"], "panel": panel, "scope": "panel" if panel else "figure"}
        r.pending = GROUNDER.submit_pick(d["page"]["n"], rect, cands, partpick.phrase(sentence, callout), meta, region)
        self.ground_requests.append(r.pending)
    GROUND_MISS = ("error", "low-confidence", "off-menu", "no-logprobs", "unavailable", "out-of-scope")
    def _ground_missed(self, fut, reason):
        """A pick that drew no oval: dropped (superseded / no-fit) once decided; summary() settles undecided ones."""
        if reason in ("superseded", "no-fit"): fut.drop = fut.drop or reason
        return self._pick_final(fut) if fut.done.is_set() else False
    def _pick_final(self, fut, applied=False):
        """Settle a pick once: focus_methods / ground_miss_reasons and its part_pick record (focus log only)."""
        reason = "ground-pick" if applied else (fut.drop or "superseded") if fut.outcome == "accepted" else fut.outcome or "timeout"
        if not fut.settle(reason): return False
        fut.applied = applied
        if applied: self.methods["ground-pick"] = self.methods.get("ground-pick", 0) + 1
        else:
            reasons = self.rec.setdefault("ground_miss_reasons", {}); reasons[reason] = reasons.get(reason, 0) + 1
            if reason in self.GROUND_MISS: self.methods["ground-miss"] = self.methods.get("ground-miss", 0) + 1
        try:
            d = os.path.join(home(), "logs"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "focus.jsonl"), "a") as f: f.write(json.dumps(dict(fut.record(), ts=round(time.time(), 3), pid=os.getpid())) + "\n")
        except OSError as e:
            self.rec["doc_note"] = f"part_pick log: {type(e).__name__}"
        return True
    def _apply_ground(self, rect, fut, ev):
        """An accepted pick turns its own chunk's box into an oval; any other outcome only settles the pick."""
        b = fut.best()
        if not b or not self._pick_final(fut, applied=True):
            self._pick_final(fut); return False
        ev["doc"]["target"] = [round(v, 1) for v in b]; ev["doc"]["kind"] = "object"; ev["shape"] = "ellipse"; ev["cue_kind"] = "part_oval"
        ev["doc"]["target_src"] = "pick"; ev["doc"].pop("split_from", None)
        ev["doc"]["label"] = _logged_label((ev["doc"].get("label") or "") + " (grounded)")
        t = self._doc_transform()
        if t and t != "pending": ev["rect"] = [round(v, 1) for v in docmap.apply(t, b)]
        self.rec["ground_ms"] = fut.ms
        return True
    def clear(self):
        self._write({"state": "clear", "seq": self.events + 1})
    def summary(self):
        picks = self.ground_requests
        for fut in picks:                              # speech is over: wait for each open decision (bounded by its deadline)
            fut.wait()
            if fut.outcome == "accepted" and not fut.drop: fut.drop = "superseded"
            self._pick_final(fut)
        if picks or any(self.pick_skipped.values()):
            ms = sorted(f.ms for f in picks if f.called and f.ms is not None)
            self.rec["part_picks"] = {"requests": len(picks), "called": sum(f.called for f in picks), "accepted": sum(f.outcome == "accepted" for f in picks),
                                      "applied": sum(f.applied for f in picks), "outcomes": dict(sorted(Counter(f.outcome or "timeout" for f in picks).items())),
                                      "skipped": dict(self.pick_skipped), "ms": {"p50": ms[len(ms) // 2] if ms else None, "max": ms[-1] if ms else None},
                                      "threshold": partpick.PICK_MIN_P, "breaker": dict(GROUNDER.breaker), "fallback": dict(GROUNDER.fallback),
                                      "by_provider": {p: sum(f.provider == p for f in picks) for p in ("local", "openrouter")}}
        if self.smap is not None: self._attach_page()
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
