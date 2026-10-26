"""Sentence map (docs/contracts/phase3-sentence-ids.md, sections B and C): the selection's source sentences with their
screen and page line rects, built before speech, and the forward-only alignment of each spoken chunk to a source span."""
import bisect, re
from textfold import fold, fold_key, fold_token_spans, is_match_token, match_tokens
from tts import NOT_ABBREVIATION

SENTENCE_END_RX = re.compile(r"[.!?]" + NOT_ABBREVIATION + r"[\"'\u201d\u2019)\]]*(?=\s|$)")
TERMINAL_LINE_END_RX = re.compile(r"[.!?][\"'\u201d\u2019)\]]*\s*$")
LIST_ITEM_RX = re.compile(r"[ \t]*(?:[-*+\u2022\u2023\u25e6\u2043\u00b7\u25aa\u25cf]|\d{1,3}[.)]|[a-z][.)])\s+\S")
STOPWORDS = frozenset("""a an the and or but nor so of in on at to for from by with as into onto over under is are was were be been
being it its this that these those there here which who whom whose what when where while than then also not no do does did
has have had will would can could may might shall should i we you he she they them our your their his her my me us""".split())
VERBATIM_MAX_EDIT_FRACTION = 0.1
FUZZY_MIN_SCORE = 0.6
FUZZY_MAX_RUN_FACTOR = 1.5
BACK_REFERENCE_MIN_SCORE = 0.9
SPAN_OPENERS = "\"'\u201c\u2018(["
SPAN_CLOSERS = ".,;:!?\u2026\"'\u201d\u2019)]"

def _trimmed(text, a, b):
    while a < b and text[a].isspace(): a += 1
    while b > a and text[b - 1].isspace(): b -= 1
    return a, b

def line_spans(text):
    """(start, end) of every line without its line break."""
    out, pos = [], 0
    for raw in (text or "").splitlines(True):
        body = raw.rstrip("\r\n")
        out.append((pos, pos + len(body))); pos += len(raw)
    return out

def split_sentences(text):
    """[(start, end)] char spans of the source sentences in selection order, whitespace trimmed."""
    if not (text or "").strip(): return []
    if not SENTENCE_END_RX.search(text): return [_trimmed(text, 0, len(text))]
    cuts = {m.end() for m in SENTENCE_END_RX.finditer(text)}
    lines = line_spans(text)
    prev_blank, prev_ended = True, True
    for k, (a, b) in enumerate(lines):
        body = text[a:b]
        if not body.strip(): prev_blank = True; continue
        if prev_blank or LIST_ITEM_RX.match(body): cuts.add(a)
        nxt = text[lines[k + 1][0]:lines[k + 1][1]].lstrip() if k + 1 < len(lines) else ""
        # a heading / label line starts at a boundary, so a short wrapped prose line is never cut out of its sentence
        if (prev_blank or prev_ended) and len(body.split()) <= 8 and not TERMINAL_LINE_END_RX.search(body) \
                and not re.search(r"[-,;\u2010\u2011]\s*$", body) and nxt[:1].isupper():
            cuts.add(a); cuts.add(b)
        prev_blank, prev_ended = False, bool(TERMINAL_LINE_END_RX.search(body))
    out, pos = [], 0
    for c in sorted(cuts | {len(text)}):
        a, b = _trimmed(text, pos, c)
        if b > a: out.append((a, b))
        pos = c
    return out

def screen_lines(text, spans, line_bounds):
    """([(char_start, char_end, [x, y, w, h])], ax_base) from the hotkey's line bounds: visual lines
    [{loc, len, rect[, abs]}] as given (unmerged), or one rect per non-blank selection line. ([], None) without bounds."""
    if not line_bounds: return [], None
    nonblank = [(a, b) for a, b in spans if text[a:b].strip()]
    if all(isinstance(r, (list, tuple)) and len(r) == 4 for r in line_bounds):
        if len(line_bounds) != len(nonblank): return [], None
        return [(*_trimmed(text, a, b), [float(v) for v in r]) for (a, b), r in zip(nonblank, line_bounds)], None
    vis = [(int(d["loc"]), int(d["loc"]) + max(1, int(d["len"])), [float(v) for v in d["rect"]]) for d in line_bounds]
    base = int(line_bounds[0]["abs"]) - int(line_bounds[0]["loc"]) if all("abs" in d for d in line_bounds) else None
    return vis, base

def _stem(token):
    return token[:5] if len(token) >= 5 else token

def _weighted_lcs(chunk_stems, weights, window_stems):
    """In-order token alignment: (matched weight, [matched window positions])."""
    m, n = len(chunk_stems), len(window_stems)
    dp = [[0.0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        ci, wi, row, prev = chunk_stems[i - 1], weights[i - 1], dp[i], dp[i - 1]
        for j in range(1, n + 1):
            row[j] = prev[j - 1] + wi if ci == window_stems[j - 1] else (prev[j] if prev[j] >= row[j - 1] else row[j - 1])
    i, j, hits = m, n, []
    while i > 0 and j > 0:
        if chunk_stems[i - 1] == window_stems[j - 1] and dp[i][j] == dp[i - 1][j - 1] + weights[i - 1]:
            hits.append((j - 1, i - 1)); i -= 1; j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]: i -= 1
        else: j -= 1
    return dp[m][n], hits[::-1]

def _semi_global_edits(pattern, text):
    """Fewest edits turning pattern into some substring of text: (edits, start, end) of that substring."""
    n = len(text)
    prev = [0] * (n + 1); start = list(range(n + 1))
    for i, pc in enumerate(pattern, 1):
        cur = [i] + [0] * n; cur_start = [0] + [0] * n
        for j in range(1, n + 1):
            best, bs = prev[j - 1] + (pc != text[j - 1]), start[j - 1]
            if prev[j] + 1 < best: best, bs = prev[j] + 1, start[j]
            if cur[j - 1] + 1 < best: best, bs = cur[j - 1] + 1, cur_start[j - 1]
            cur[j], cur_start[j] = best, bs
        prev, start = cur, cur_start
    end = min(range(n + 1), key=lambda j: (prev[j], j))
    return prev[end], start[end], end

class Alignment:
    __slots__ = ("kind", "key_start", "key_end", "score", "runner_up", "chars")
    def __init__(self, kind, key_start, key_end, score, runner_up=None, chars=None):
        self.kind, self.key_start, self.key_end, self.score, self.runner_up = kind, key_start, key_end, score, runner_up
        self.chars = chars                              # exact selection char span (verbatim-block sentences), else from the key
    def __repr__(self):
        return f"Alignment({self.kind}, {self.key_start}:{self.key_end}, {self.score}, {self.runner_up})"

class SelectionSpan:
    """Speech tag for a sentence built from selection lines (verbatim blocks): its selection char span, or no span
    (generated framing). verbatim: the sentence is the span's text, else a reworded row / item of it."""
    def __init__(self, start=None, end=None, verbatim=True):
        self.start, self.end, self.verbatim = start, end, verbatim

class SentenceMap:
    def __init__(self, text, line_bounds):
        self.text = text
        self.key, self.key_offsets = fold(text)
        self.tokens = fold_token_spans(text)                            # fold tokens: page-mapping pieces
        self.match_spans = [t for t in self.tokens if is_match_token(t[0])]   # match tokens: fuzzy alignment
        self.match_starts = [t[1] for t in self.match_spans]
        self.match_stems = [_stem(t[0]) for t in self.match_spans]
        self.line_spans = [(a, b) for a, b in line_spans(text) if text[a:b].strip()]
        self.screen_lines, self.ax_base = screen_lines(text, line_spans(text), line_bounds)
        self.sentences = []
        for i, (a, b) in enumerate(split_sentences(text)):
            ka, kb = self.key_range(a, b)
            self.sentences.append({"id": f"s{i}", "span": (a, b), "keys": (ka, kb), "sel_lines": self._sel_lines([(a, b)]),
                                   "rects": self._screen_rects([(a, b)])})
        self.sentence_key_starts = [s["keys"][0] for s in self.sentences]
        self.cursor = 0
        self.page_line_of_key = None
        self.page_rank = {}

    # -- spans --
    def key_range(self, a, b):
        return bisect.bisect_left(self.key_offsets, a), bisect.bisect_left(self.key_offsets, b)
    def char_span(self, ka, kb):
        if kb <= ka: return None
        return self.key_offsets[ka], self.key_offsets[kb - 1] + 1
    def _sel_lines(self, spans):
        return [i for i, (a, b) in enumerate(self.line_spans) if any(a < e and b > s for s, e in spans)]
    def _screen_rects(self, spans):
        return [r for (a, b, r) in self.screen_lines if any(a < e and b > s for s, e in spans)]
    def sentence_ids(self, ka, kb):
        """Indices of the sentences whose key range overlaps [ka, kb)."""
        return [i for i, s in enumerate(self.sentences) if s["keys"][0] < kb and s["keys"][1] > ka]
    def primary_sentence(self, ka, kb):
        """The sentence holding most of the span's key characters (the earlier one on a tie)."""
        ids = self.sentence_ids(ka, kb)
        if not ids: return None
        return max(ids, key=lambda i: (min(kb, self.sentences[i]["keys"][1]) - max(ka, self.sentences[i]["keys"][0]), -i))

    # -- bands --
    def _band(self, key_ranges):
        spans = [s for s in (self.char_span(ka, kb) for ka, kb in key_ranges) if s]
        page = []
        if self.page_line_of_key is not None:
            seen = {self.page_line_of_key[k] for ka, kb in key_ranges for k in range(ka, kb)} - {-1}
            page = sorted(seen, key=lambda i: self.page_rank[i])
        return {"rects": self._screen_rects(spans), "page_lines": page, "sel_lines": self._sel_lines(spans),
                "char_span": (min(s[0] for s in spans), max(s[1] for s in spans)) if spans else None}
    def band_for(self, alignment):
        """Verbatim (and span): the lines holding the aligned span. Fuzzy: every line of each overlapped sentence."""
        if alignment.kind == "fuzzy":
            return self.band_for_keys([self.sentences[i]["keys"] for i in self.sentence_ids(alignment.key_start, alignment.key_end)])
        band = self.band_for_keys([(alignment.key_start, alignment.key_end)])
        if alignment.chars: band["char_span"] = alignment.chars
        return band
    def band_for_keys(self, key_ranges):
        """A band from key ranges; it remembers them so page rects can join when the document arrives later."""
        return dict(self._band(key_ranges), keys=list(key_ranges))
    def block_band(self):
        return self.band_for_keys([(0, len(self.key))])

    # -- page rects --
    def attach_page(self, page, region=None):
        """Align the whole selection key once against the page lines in reading order, forward and contiguous.
        region: page-space [x0, y0, x1, y1] the selection covers; it picks between repeated occurrences."""
        lines = page["lines"]
        order, seen = [], set()
        for b in page.get("blocks") or []:
            for i in b.get("lines") or []:
                if i not in seen: seen.add(i); order.append(i)
        order += [i for i in range(len(lines)) if i not in seen]
        owner, parts = [], []
        for i in order:
            k = fold_key(lines[i][4]); parts.append(k); owner.extend([i] * len(k))
        page_key = "".join(parts)
        def in_region(i):
            r = lines[i]
            return region is None or (r[0] < region[2] and region[0] < r[2] and r[1] < region[3] and region[1] < r[3])
        mapping = [-1] * len(self.key)
        occurrences, pos = [], page_key.find(self.key) if self.key else -1
        while pos >= 0:
            occurrences.append(pos); pos = page_key.find(self.key, pos + 1)
        if occurrences:
            n = len(self.key)
            p0 = max(occurrences, key=lambda p: (sum(1 for i in set(owner[p:p + n]) if in_region(i)), -p))
            mapping = owner[p0:p0 + n]
        else:
            self._map_by_lines(page_key, owner, in_region, mapping, lines)
        self.page_rank = {i: r for r, i in enumerate(order)}
        self.page_line_of_key = mapping
        for s in self.sentences:
            s["page_lines"] = self._band([s["keys"]])["page_lines"]

    def _map_by_lines(self, page_key, owner, in_region, mapping, page_lines):
        """The selection is not one contiguous run of the page (a run-in heading parsed as its own line, a caption or
        column the parse orders differently, text past a page break): map each whole selection line forward from the last
        match; a line found only behind it is taken when it sits in the selected region or occurs once on the page.
        A line found in no whole piece is mapped by its longest token runs only between whole-matched lines, or when the
        runs cover it and sit on or next to the rows of the matched lines (a run-in heading). Text of another page stays
        unmapped."""
        def locate(piece, cur):
            ahead, pos = [], page_key.find(piece, cur)
            while pos >= 0 and len(ahead) < 8:
                ahead.append(pos); pos = page_key.find(piece, pos + 1)
            if ahead: return next((p for p in ahead if in_region(owner[p])), ahead[0]), True
            anywhere, pos = [], page_key.find(piece)
            while pos >= 0 and len(anywhere) < 8:
                anywhere.append(pos); pos = page_key.find(piece, pos + 1)
            inside = [p for p in anywhere if in_region(owner[p])]
            if inside: return inside[0], False
            return (anywhere[0], False) if len(anywhere) == 1 else (-1, False)
        cur, cur_before, whole = 0, [], []
        for n, (a, b) in enumerate(self.line_spans):
            cur_before.append(cur)
            ka, kb = self.key_range(a, b)
            if kb <= ka: continue
            p, ahead = locate(self.key[ka:kb], cur)
            if p < 0: continue
            mapping[ka:kb] = owner[p:p + kb - ka]; whole.append(n)
            if ahead: cur = p + kb - ka
        matched_rows = [page_lines[i] for i in set(mapping) - {-1}]
        def on_matched_rows(i):                                 # the same row as a matched line or the row next to it
            l = page_lines[i]
            return any(abs((l[1] + l[3]) - (r[1] + r[3])) / 2 <= 1.6 * max(1.0, r[3] - r[1]) and l[0] < r[2] and r[0] < l[2]
                       for r in matched_rows)
        for n, (a, b) in enumerate(self.line_spans):
            ka, kb = self.key_range(a, b)
            if kb <= ka or n in whole: continue
            cur, pieces = cur_before[n], []
            toks = [(max(ka, t[1]), min(kb, t[2])) for t in self.tokens if t[1] < kb and t[2] > ka]   # a rejoined word crosses lines
            i = 0
            while i < len(toks):
                best, j = None, i
                while j < len(toks):
                    p, ahead = locate(self.key[toks[i][0]:toks[j][1]], cur)
                    if p < 0: break
                    best = (j, p, ahead); j += 1
                if best is None: i += 1; continue
                j, p, ahead = best; s, e = toks[i][0], toks[j][1]
                if e - s >= 4:                                  # shorter pieces match anywhere
                    pieces.append((s, e, p))
                    if ahead: cur = p + e - s
                i = j + 1
            between = bool(whole) and whole[0] < n < whole[-1]
            covered = sum(e - s for s, e, _ in pieces) >= 0.9 * (kb - ka)
            if between or (covered and all(on_matched_rows(owner[p]) for _, _, p in pieces)):
                for s, e, p in pieces: mapping[s:e] = owner[p:p + e - s]

    def summary(self):
        """{sentences, mapped, method} for the sentence_map log record (counts only)."""
        if self.page_line_of_key is not None and any(s.get("page_lines") for s in self.sentences):
            return {"sentences": len(self.sentences), "mapped": sum(1 for s in self.sentences if s.get("page_lines")), "method": "page"}
        if self.screen_lines:
            return {"sentences": len(self.sentences), "mapped": sum(1 for s in self.sentences if s["rects"]), "method": "ax-lines"}
        return {"sentences": len(self.sentences), "mapped": 0, "method": "band"}

    # -- alignment --
    def _near_verbatim(self, chunk_key, lo, hi):
        """(start, end, edits) of the first source substring in [lo, hi) within 10 % edits of chunk_key, or None.
        Seeds: of k + 1 pieces of the chunk key, one survives k edits unchanged."""
        m = len(chunk_key); k = int(VERBATIM_MAX_EDIT_FRACTION * m)
        if k == 0: return None
        piece = m // (k + 1); starts = set()
        for j in range(k + 1):
            p = chunk_key[j * piece:(j + 1) * piece]; pos, n = self.key.find(p, lo, hi), 0
            while pos >= 0 and n < 4:
                starts.add(pos - j * piece); n += 1; pos = self.key.find(p, pos + 1, hi)
        for s in sorted(starts)[:6]:
            a, b = max(lo, s - k), min(hi, s + m + k)
            if b - a < m - k: continue
            edits, sa, sb = _semi_global_edits(chunk_key, self.key[a:b])
            if edits <= k: return a + sa, a + sb, edits
        return None

    def _fuzzy_candidates(self, chunk, chunk_key_len, lo, hi, min_score):
        """[(score, key_start, key_end)] forward in-order token alignments starting at lo or at a sentence start."""
        toks = match_tokens(chunk)
        if not toks: return []
        stems = [_stem(t) for t in toks]; weights = [0.5 if t in STOPWORDS else 1.0 for t in toks]; total = sum(weights)
        t_lo = bisect.bisect_left(self.match_starts, lo)
        t_hi = bisect.bisect_right(self.match_starts, hi) - 1
        while t_hi >= t_lo and self.match_spans[t_hi][2] > hi: t_hi -= 1
        t_hi += 1
        starts = {t_lo} | {bisect.bisect_left(self.match_starts, s) for s in self.sentence_key_starts if lo < s < hi}
        span_len = int(FUZZY_MAX_RUN_FACTOR * len(toks)) + 2
        out, seen = [], set()
        for s in sorted(starts):
            window = self.match_stems[s:min(t_hi, s + span_len)]
            if not window: continue
            ws = set(window)
            if sum(w for st, w in zip(stems, weights) if st in ws) / total < min_score: continue
            got, hits = _weighted_lcs(stems, weights, window)
            content = [h for h in hits if toks[h[1]] not in STOPWORDS] or hits
            if not content: continue
            ks, ke = self.match_spans[s + content[0][0]][1], self.match_spans[s + content[-1][0]][2]
            if (ks, ke) in seen or ke - ks > FUZZY_MAX_RUN_FACTOR * max(1, chunk_key_len): continue
            seen.add((ks, ke)); out.append((round(got / total, 2), ks, ke))
        return out

    def _runner_up(self, chunk, chunk_key, lo, hi, ka, kb):
        """Best score of another forward candidate not overlapping [ka, kb): a repeat of the chunk key, else fuzzy."""
        if self.key.find(chunk_key, kb, hi) >= 0: return 1.0
        others = [c[0] for c in self._fuzzy_candidates(chunk, len(chunk_key), lo, hi, FUZZY_MIN_SCORE) if c[2] <= ka or c[1] >= kb]
        return max(others) if others else None

    def align(self, chunk, lo=0, hi=None):
        """Forward alignment of a spoken chunk from the cursor (optionally inside the key window [lo, hi)):
        verbatim, near-verbatim (<= 10 % edits) or fuzzy (in-order tokens, score >= 0.6). None when nothing qualifies."""
        chunk_key = fold_key(chunk)
        if not chunk_key: return None
        lo = max(self.cursor, lo); hi = len(self.key) if hi is None else min(hi, len(self.key))
        if lo >= hi: return None
        i = self.key.find(chunk_key, lo, hi)
        if i >= 0:
            return Alignment("verbatim", i, i + len(chunk_key), 1.0, self._runner_up(chunk, chunk_key, lo, hi, i, i + len(chunk_key)))
        near = self._near_verbatim(chunk_key, lo, hi)
        if near:
            a, b, edits = near
            return Alignment("verbatim", a, b, round(1 - edits / len(chunk_key), 2), self._runner_up(chunk, chunk_key, lo, hi, a, b))
        cands = self._fuzzy_candidates(chunk, len(chunk_key), lo, hi, FUZZY_MIN_SCORE)
        if not cands: return None
        best = max(cands, key=lambda c: (c[0], -c[1]))
        others = [c[0] for c in cands if c[2] <= best[1] or c[1] >= best[2]]
        return Alignment("fuzzy", best[1], best[2], best[0], max(others) if others else None)

    def align_back(self, chunk):
        """The latest match before the cursor at >= 0.9 (a back-reference), or None."""
        chunk_key = fold_key(chunk)
        if not chunk_key or self.cursor == 0: return None
        i = self.key.rfind(chunk_key, 0, self.cursor)
        if i >= 0: return Alignment("verbatim", i, i + len(chunk_key), 1.0)
        near = self._near_verbatim(chunk_key, 0, self.cursor)
        if near and 1 - near[2] / len(chunk_key) >= BACK_REFERENCE_MIN_SCORE:
            return Alignment("verbatim", near[0], near[1], round(1 - near[2] / len(chunk_key), 2))
        cands = self._fuzzy_candidates(chunk, len(chunk_key), 0, self.cursor, BACK_REFERENCE_MIN_SCORE)
        best = max(cands, key=lambda c: (c[0], c[1]), default=None)
        return Alignment("fuzzy", best[1], best[2], best[0]) if best and best[0] >= BACK_REFERENCE_MIN_SCORE else None

    def span_alignment(self, span):
        """A verbatim-block sentence's own selection span (method span)."""
        ka, kb = self.key_range(span.start, span.end)
        if kb <= ka: return None
        return Alignment("span" if span.verbatim else "fuzzy", ka, kb, 1.0, chars=(span.start, span.end))

    def find_span(self, sentence, from_char=0):
        """Where a sentence built from selection lines sits in the selection, at or after from_char:
        SelectionSpan (verbatim or reworded) or SelectionSpan() when it is not there (generated framing)."""
        saved = self.cursor
        self.cursor = self.key_range(from_char, from_char)[0]
        try: a = self.align(sentence)
        finally: self.cursor = saved
        if a is None: return SelectionSpan()
        start, end = self.char_span(a.key_start, a.key_end)
        while start > 0 and self.text[start - 1] in SPAN_OPENERS: start -= 1      # the sentence's own quotes and punctuation
        while end < len(self.text) and self.text[end] in SPAN_CLOSERS: end += 1
        return SelectionSpan(start, end, verbatim=a.kind == "verbatim")

    def commit(self, alignment):
        self.cursor = max(self.cursor, alignment.key_end)

    def line_window(self, first_line, last_line):
        """Key window of 1-based selection lines first..last widened by one line each side (focus tags)."""
        a = self.line_spans[max(0, first_line - 2)][0] if self.line_spans else 0
        b = self.line_spans[min(len(self.line_spans), last_line + 1) - 1][1] if self.line_spans else 0
        return self.key_range(a, b)
