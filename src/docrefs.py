"""Sentence references for focus: which figure, panel, callout, object or device a spoken sentence names (find_ref,
said_panel, callout_ref, names_object, ground_label) and the figure labels listed in the prompt. Stdlib only; docmap
re-exports every name."""
import re
from doccaps import LEX_RE, PANEL_REF_RE, REF_RE
from docgeom import _centre_in, _clip, _inside, _toks
from figparts import CIRCLED, _panel_rect, part_hood

GENERIC = set("""a an the this that these those it its of in on at to for from with by and or as is are was were be been being
shows show shown showing depicts depicting illustrates displays display figure fig diagram image picture panel part left right top bottom
here there which where while what how also then next each both all some data result results overview setup scheme process involves
first second third finally using used use uses into onto over under between""".split())

def figure_context(figs, limit=8, cap_chars=160):
    out = []
    for f in figs[:limit]:
        if not f.get("label") or f.get("captioned") is False: continue      # U-labels are never listed in a prompt (9.1)
        pn = ", ".join(p["label"] for p in f.get("panels") or [])
        cap = re.sub(r"\s+", " ", f.get("caption") or "")
        lexicon = f.get("caption_src") in ("lexicon", "lexicon+in-box")
        if lexicon:
            lm = LEX_RE.match(cap); cap = lm.group("rest") if lm else cap
        else: cap = re.sub(r"^\s*(?:fig(?:ure)?s?\.?|table)\s*\d+\s*[.:|\-\u2013\u2014]?\s*", "", cap, flags=re.I)
        cap = cap[:cap_chars].rsplit(" ", 1)[0] if len(cap) > cap_chars else cap
        out.append(f"{f['printed_label'] if lexicon else f['label']}" + (f" (panels {pn})" if pn else "") + (": " + cap.rstrip(" .,;:") if cap else ""))
    return out

def _alias_re(alias):
    return re.compile(r"(?<!\w)" + re.escape(alias).replace(r"\ ", r"\s*") + r"(?!\d)(?:\s*\(?([a-h])\)?(?![a-z]))?(?!\w)", re.I)

def find_ref(sentence, figs, last=None):
    """('Figure 4', 'b') mentioned in a spoken sentence, or a bare 'panel b' of the last figure. Figures with aliases (Phase 6
    lexicon captions) also match by any alias; when two share one, the one whose printed_label matches exactly wins. Uncaptioned
    figures (U-labels) never match."""
    figs = [f for f in figs if f.get("captioned") is not False]
    hits = []
    for m in REF_RE.finditer(sentence or ""):
        kind = "Table" if m.group(1).lower().startswith("tab") else "Figure"
        f = next((f for f in figs if f.get("label") == f"{kind} {m.group(2)}"), None)
        if f and not any(f.get("aliases") for f in figs): return f, (m.group(3) or "").lower() or None
        if f: hits.append((m.start(), -len(m.group(0)), 1, f, (m.group(3) or "").lower() or None))
    for f in figs:
        for a in f.get("aliases") or []:
            m = _alias_re(a).search(sentence or "")
            if m: hits.append((m.start(), -len(a), 0 if a == f.get("printed_label") else 1, f, (m.group(1) or "").lower() or None))
    if hits:
        *_, f, panel = min(hits, key=lambda h: h[:3])
        return f, panel
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
