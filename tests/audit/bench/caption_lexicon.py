"""The verifier's own reading of the caption lexicon (docs/contracts/phase6-block-classifier.md 4.1), written from the contract only.
Used to count lexicon-only caption lines (tools/kind_census.py) and to build lexicon positive / negative fixtures; it never judges
the app's output. Stdlib only."""
import re

FIGURE_NOUNS = ("figure", "fig", "figs", "chart", "graph", "diagram", "plate", "illustration", "scheme", "map", "abbildung", "abb", "bild",
                "figura", "figuur", "grafik", "gráfico", "grafico", "图", "図")
TABLE_NOUNS = ("table", "tab", "tabelle", "tabla", "tableau", "tabella", "tabela", "tabel", "表")
AMBIGUOUS_NOUNS = ("exhibit", "anlage", "anexo", "annexe")
ABBREV = {"fig": "Figure", "figs": "Figure", "abb": "Abbildung", "tab": "Table"}
_NOUN = "|".join(sorted((re.escape(n) for n in FIGURE_NOUNS + TABLE_NOUNS + AMBIGUOUS_NOUNS), key=len, reverse=True))
_ROMAN = r"(?:XL|X{0,3}(?:IX|IV|V?I{0,3}))"
LEX_RE = re.compile(r"^\s*(?P<noun>" + _NOUN + r")(?P<dot>\.)?\s*(?P<num>S?\d+(?:[.\-]\d+)?[a-z]?|[A-Z]\.\d+|" + _ROMAN + r")"
                    r"\s*(?:[.:|\u2013\u2014]|\s-\s|$)", re.IGNORECASE)


def _roman_ok(noun, num):
    """Roman numerals I-XL count only directly after an uppercase or capitalised noun (TABLE IV, Fig. II)."""
    if not num.isalpha() or not re.fullmatch(_ROMAN, num, re.IGNORECASE): return True
    return bool(re.fullmatch(r"[IVXL]+", num)) and noun[:1].isupper()


def match(line):
    """(kind, noun, number) when `line` is a lexicon caption anchor by the 4.1 text rule, else None. kind: figure | table | ambiguous."""
    m = LEX_RE.match(line or "")
    if not m: return None
    noun, num = m.group("noun"), m.group("num")
    if not num or not _roman_ok(noun, num): return None
    if re.fullmatch(r"[a-z]", num): return None
    n = noun.lower()
    kind = "figure" if n in FIGURE_NOUNS else "table" if n in TABLE_NOUNS else "ambiguous"
    return kind, noun, num


def printed_label(line):
    """4.2 printed_label: the noun as printed (abbreviations expanded) plus the number, or None."""
    m = match(line)
    if not m: return None
    _, noun, num = m
    return f"{ABBREV.get(noun.lower(), noun)} {num}"
