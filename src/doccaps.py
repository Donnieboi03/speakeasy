"""Caption and figure-reference vocabulary for docmap: the standard caption regexes (CAP_RE, TAB_RE), panel and reference
regexes, and the Phase 6 caption lexicon (lexicon_match, caption_info). Stdlib only; docmap re-exports every name."""
import re

# captions need a separator after the number ("Figure 3:", "Fig. 3.", "Table 2 |"), so prose like "Table 2 summarizes" is not one
CAP_RE = re.compile(r"^\s*(?:fig(?:ure)?s?\.?)\s*(\d+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(.*)$", re.I)
TAB_RE = re.compile(r"^\s*table\s*(\d+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(.*)$", re.I)
PANEL_RE = re.compile(r"^\s*\(?([a-hA-H])\)\s*$|^\s*\(([a-h])\)\s+\S|^\s*([a-h])\s*$")
REF_RE = re.compile(r"\b(fig(?:ure)?s?\.?|table)\s*(\d+)\s*(?:\(?([a-h])\)?(?![a-z]))?", re.I)
PANEL_REF_RE = re.compile(r"\b(?:panel|subfigure|part)\s*\(?([a-h])\)?(?![a-z])", re.I)
PAGE_OF_RE = re.compile(r"(?:page\s*)?(\d{1,4})\s*(?:of|/)\s*(\d{1,4})\b", re.I)

# Phase 6 caption lexicon (phase6-block-classifier.md 4.1): the same separators as CAP_RE / TAB_RE, more nouns and numbers
LEX_NOUNS = {"figure": ("figure", "fig", "figs", "chart", "graph", "diagram", "plate", "illustration", "scheme", "map", "abbildung", "abb",
                        "bild", "figura", "figuur", "grafik", "gr\u00e1fico", "grafico", "\u56fe", "\u56f3"),
             "table": ("table", "tab", "tabelle", "tabla", "tableau", "tabella", "tabela", "tabel", "\u8868"),
             "ambiguous": ("exhibit", "anlage", "anexo", "annexe")}
LEX_KIND = {n: k for k, ns in LEX_NOUNS.items() for n in ns}
LEX_EXPAND = {"fig": "Figure", "figs": "Figure", "abb": "Abbildung", "tab": "Table"}      # abbreviations: a trailing "." is optional
LEX_RE = re.compile(r"^\s*(?P<noun>(?i:" + "|".join(re.escape(n) for n in sorted(LEX_KIND, key=len, reverse=True)) + r"))(?P<dot>\.)?(?![^\W\d_])\s*"
                    r"(?P<num>(?:S|[A-Z]\.)?\d+(?:[.\-]\d+)?[a-h]?|[IVXL]+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(?P<rest>.*)$")
ROMAN = {v: k + 1 for k, v in enumerate("I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX XXI XXII XXIII XXIV XXV "
                                         "XXVI XXVII XXVIII XXIX XXX XXXI XXXII XXXIII XXXIV XXXV XXXVI XXXVII XXXVIII XXXIX XL".split())}

def lexicon_match(text):
    """A caption anchor by the lexicon: {"noun", "kind" (figure | table | ambiguous), "num", "n", "printed_label"}, else None. Roman
    numerals I-XL count only directly after an uppercase or capitalised noun; prose ("Table 2 shows") has no separator."""
    m = LEX_RE.match(text or "")
    if not m: return None
    noun, num = m.group("noun"), m.group("num")
    if num.isalpha():
        if num not in ROMAN or not noun[0].isupper(): return None
        n = ROMAN[num]
    else:
        n = int(re.search(r"\d+", num).group(0))
    key = noun.lower()
    printed = LEX_EXPAND.get(key, noun)
    sep = "" if ord(noun[0]) > 0x2e7f else " "
    return {"noun": noun, "kind": LEX_KIND[key], "num": num, "n": n, "printed_label": f"{printed}{sep}{num}"}

def caption_info(text, kind=None):
    """A caption line's label fields with Phase 6 on: {"kind", "label", "n", "printed_label", "aliases", "src", "kind_src"}. A standard
    caption (CAP_RE / TAB_RE) decides exactly as today; else the lexicon. kind: the decided kind of an ambiguous noun. None when no match."""
    m = CAP_RE.match(text or ""); k = "figure"
    if not m: m = TAB_RE.match(text or ""); k = "table"
    lx = lexicon_match(text)
    if m:
        label = f"{'Figure' if k == 'figure' else 'Table'} {m.group(1)}"
        printed = lx["printed_label"] if lx and lx["kind"] == k and lx["n"] == int(m.group(1)) else label
        return {"kind": k, "label": label, "n": int(m.group(1)), "printed_label": printed, "aliases": list(dict.fromkeys([printed, label])),
                "src": "regex", "kind_src": "caption"}
    if not lx: return None
    k = kind or lx["kind"]
    label = f"{'Figure' if k == 'figure' else 'Table'} {lx['num']}" if k != "ambiguous" else None
    return {"kind": k, "label": label, "n": lx["n"], "num": lx["num"], "printed_label": lx["printed_label"],
            "aliases": list(dict.fromkeys([lx["printed_label"]] + ([label] if label else []))), "src": "lexicon", "kind_src": "lexicon"}
