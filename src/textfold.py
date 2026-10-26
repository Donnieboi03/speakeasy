"""Shared text fold (docs/contracts/phase3-sentence-ids.md, section A): the match key and word tokens every text
comparison uses. tracker.swift carries the same steps (fold / foldTokens); keep the two identical.
Stdlib only, imported by sibling path from the doc venv too (docmap._toks)."""
import re, unicodedata

INVISIBLE_RX = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")
NON_ASCII_RUN_RX = re.compile(r"[\x00-\x7f]?[^\x00-\x7f]+")       # plus the ASCII base a combining mark may attach to
LINE_END_HYPHEN_RX = re.compile(r"[-\u2010\u2011][ \t]*(?:\r\n|\r|\n)[ \t]*")
LINE_BREAK_RX = re.compile(r"\r\n|\r|\n")
UNIT_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
              "seventeen eighteen nineteen").split()
TENS_WORDS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
# longest first so "seventeen" wins over "seven"; the Swift port tries the alternatives in this same order
UNIT_WORDS_LONGEST_FIRST = sorted(UNIT_WORDS, key=len, reverse=True)
NUMBER_WORD_RX = re.compile(r"(" + "|".join(TENS_WORDS) + r")(?:([-\u2010\u2011]|[ \t]+)(one|two|three|four|five|six|seven|eight|nine))?"
                            r"|(" + "|".join(UNIT_WORDS_LONGEST_FIRST) + r")|(per)[ \t]+(cent)|%")

def is_key_char(c):
    """Unicode general category L* or Nd."""
    if c < "\x80": return c.isalnum()
    cat = unicodedata.category(c)
    return cat[0] == "L" or cat == "Nd"

def _is_mark(c):
    return c >= "\x80" and unicodedata.category(c)[0] == "M"

def _rebuild(s, offs, rx, replace):
    """Apply replace(match) -> (text, [offset per char]) | None (keep) to every match of rx; offs may be None."""
    out, out_offs, pos = [], [] if offs is not None else None, 0
    for m in rx.finditer(s):
        rep = replace(m, s, offs)
        if rep is None: continue
        out.append(s[pos:m.start()]); out.append(rep[0])
        if offs is not None: out_offs.extend(offs[pos:m.start()]); out_offs.extend(rep[1])
        pos = m.end()
    if not out: return s, offs
    out.append(s[pos:])
    if offs is not None: out_offs.extend(offs[pos:])
    return "".join(out), out_offs

def _nfkc_run(m, s, offs):
    """Step 1 on one non-ASCII run: NFKC per cluster (a base plus its combining marks); every output char maps to the
    cluster's first original char."""
    run, base, text, out_offs, i = m.group(0), m.start(), [], [], 0
    while i < len(run):
        j = i + 1
        while j < len(run) and _is_mark(run[j]): j += 1
        piece = unicodedata.normalize("NFKC", run[i:j])
        text.append(piece)
        if offs is not None: out_offs.extend([offs[base + i]] * len(piece))
        i = j
    return "".join(text), out_offs

def _drop(m, s, offs):
    return "", []

def _rejoin_hyphen(m, s, offs):
    """Step 3: a hyphen after a letter, one line break, then a lowercase letter: removed with the break and spaces."""
    if m.start() == 0 or m.end() >= len(s): return None
    before, after = s[m.start() - 1], s[m.end()]
    if unicodedata.category(before)[0] == "L" and unicodedata.category(after) == "Ll": return "", []
    return None

def _break_to_space(m, s, offs):
    return " ", [offs[m.start()]] if offs is not None else None

def _is_word_at(s, i):
    return 0 <= i < len(s) and is_key_char(s[i])

def _number_word(m, s, offs):
    """Step 5: number words to digits and per cent / % to 'percent', whole words only."""
    at = (lambda i: offs[i]) if offs is not None else (lambda i: 0)
    if m.group(0) == "%":
        text = (" " if _is_word_at(s, m.start() - 1) else "") + "percent" + (" " if _is_word_at(s, m.end()) else "")
        return text, [at(m.start())] * len(text)
    if _is_word_at(s, m.start() - 1): return None
    if m.group(1):
        tens = str(TENS_WORDS.index(m.group(1)) + 2)
        if m.group(3) and not _is_word_at(s, m.end()):
            return tens + str(UNIT_WORDS.index(m.group(3))), [at(m.start()), at(m.start(3))]
        end1 = m.end(1)
        if _is_word_at(s, end1): return None
        return tens + "0" + s[end1:m.end()], [at(m.start()), at(m.start() + 1)] + [at(i) for i in range(end1, m.end())]
    if _is_word_at(s, m.end()): return None
    if m.group(4):
        digits = str(UNIT_WORDS.index(m.group(4)))
        return digits, [at(m.start() + i) for i in range(len(digits))]
    return "percent", [at(m.start(5) + i) for i in range(3)] + [at(m.start(6) + i) for i in range(4)]

def _lowercase(s, offs):
    """Step 4: Unicode default lowercase mapping, one character at a time (no context rules)."""
    if s.isascii(): return s.lower(), offs
    parts = [c.lower() for c in s]
    if all(len(p) == 1 for p in parts): return "".join(parts), offs
    out_offs = [o for p, o in zip(parts, offs) for _ in p] if offs is not None else None
    return "".join(parts), out_offs

def folded_text(text, track=True):
    """Steps 1-5: (text, offsets or None). Offsets give, for each output char, the index of its original char."""
    s = text or ""
    offs = list(range(len(s))) if track else None
    if not s.isascii():
        s, offs = _rebuild(s, offs, NON_ASCII_RUN_RX, _nfkc_run)
        s, offs = _rebuild(s, offs, INVISIBLE_RX, _drop)
    s, offs = _rebuild(s, offs, LINE_END_HYPHEN_RX, _rejoin_hyphen)
    s, offs = _rebuild(s, offs, LINE_BREAK_RX, _break_to_space)
    s, offs = _lowercase(s, offs)
    return _rebuild(s, offs, NUMBER_WORD_RX, _number_word)

def fold(text):
    """(key, offsets): the key keeps only L*/Nd characters of steps 1-5; offsets[i] is the original index of key[i]."""
    s, offs = folded_text(text)
    if s.isascii():
        keep = [i for i, c in enumerate(s) if c.isalnum()]
    else:
        keep = [i for i, c in enumerate(s) if is_key_char(c)]
    return "".join(s[i] for i in keep), [offs[i] for i in keep]

def fold_key(text):
    s, _ = folded_text(text, track=False)
    return "".join(c for c in s if is_key_char(c)) if not s.isascii() else re.sub(r"[\W_]+", "", s)

TOKEN_RX = re.compile(r"[^\W_]+")

def _key_runs(s):
    """Maximal runs of L*/Nd characters: (start, end) in s."""
    out = []
    for m in TOKEN_RX.finditer(s):
        a, b = m.start(), m.end()
        if m.group(0).isascii(): out.append((a, b)); continue
        i = a
        while i < b:                                       # \w also takes No/Nl numerics: they split tokens
            while i < b and not is_key_char(s[i]): i += 1
            j = i
            while j < b and is_key_char(s[j]): j += 1
            if j > i: out.append((i, j))
            i = j
    return out

def fold_tokens(text):
    """Word tokens: maximal L*/Nd runs of steps 1-5."""
    s, _ = folded_text(text, track=False)
    return [s[a:b] for a, b in _key_runs(s)]

def is_match_token(token):
    """Match-token layer: every digit run and every run of two or more characters (no stopword list)."""
    return len(token) >= 2 or unicodedata.category(token) == "Nd"

def match_tokens(text):
    """Fold tokens without single-character non-digit runs: what docmap, fuzzy alignment and the tracker compare."""
    return [t for t in fold_tokens(text) if is_match_token(t)]

def fold_token_spans(text):
    """[(token, key_start, key_end)]: each token with its range in fold(text)'s key."""
    s, _ = folded_text(text, track=False)
    out, key_pos = [], 0
    for a, b in _key_runs(s):
        out.append((s[a:b], key_pos, key_pos + b - a)); key_pos += b - a
    return out
