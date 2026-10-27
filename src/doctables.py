"""Tables for docmap: parse-time cell rows and the layout cue for an ambiguous caption noun (phase6 A4.3), image-table rows
(phase6 8), and read-time table rows and row matching for table_row bands. docmap re-exports every name."""
from docgeom import _area, _clip, _inside, _overlap, _toks

def _cell_rows(lines, cap, lh, W, above):
    """Rows of table cells right above / below a caption: consecutive line rows (one line band; the first within Phase 4's
    pairing gap of 3 line heights, then gaps under 2.5) that hold two or more separate fragments, from the caption outward.
    Returns the rows' lines."""
    x0, x1 = cap[0] - 20, (W / 2 if cap[2] <= W / 2 + 10 else W)
    cand = sorted((l for l in lines if l[0] < x1 and l[2] > x0 and not _overlap(l[:4], cap)
                   and ((l[3] <= cap[1] + 1 and cap[1] - l[3] < 8 * lh) if above else (l[1] >= cap[3] - 1 and l[1] - cap[3] < 8 * lh))),
                  key=lambda l: -l[1] if above else l[1])
    rows = []
    for l in cand:
        c = (l[1] + l[3]) / 2
        if rows and abs(c - rows[-1][0]) < 0.6 * max(4.0, l[3] - l[1]): rows[-1][1].append(l); continue
        edge = (cap[1] if above else cap[3]) if not rows else rows[-1][2]
        if abs(edge - (l[3] if above else l[1])) > (3.0 if not rows else 2.5) * lh: break
        rows.append([c, [l], l[1] if above else l[3]])
    k = 0
    while k < len(rows) and len(rows[k][1]) >= 2: k += 1
    return [r[1] for r in rows[:k]]

def _kind_by_layout(cap, lh, gfx, lines, W):
    """4.2 without a stage B verdict or a detector box: the body side decides. Table cells next to the caption, else graphics
    above it (a figure), else graphics below it (a table), else figure."""
    if len(_cell_rows(lines, cap, lh, W, False)) >= 2 or len(_cell_rows(lines, cap, lh, W, True)) >= 2: return "table"
    overlaps = lambda g: g[0] < cap[2] and cap[0] < g[2]
    if any(overlaps(g) and 0 <= cap[1] - g[3] < 60 for g in gfx): return "figure"
    if any(overlaps(g) and 0 <= g[1] - cap[3] < 60 for g in gfx): return "table"
    return "figure"

def _image_table(page, lines, bbox):
    """Section 8: fewer than 2 PDF text lines inside the body and a raster image covering >= 50 % of it."""
    if sum(1 for l in lines if _inside(l[:4], bbox, 2)) >= 2: return False
    try: imgs = page.images()
    except (RuntimeError, ValueError, OSError): return False
    a = max(1.0, _area(bbox))
    return any(_area(_clip(r, bbox)) >= 0.5 * a for r in imgs if _overlap(r, bbox))

def table_rows(lines, tb, cap=None):
    """Row rects of a table from its PDF text lines: lines inside the table box (caption excluded) grouped by vertical
    centre. Returns [{"rect": [x0,y0,x1,y1], "toks": set, "n": cells}] top to bottom; rows[0] is the header row."""
    inn = [l for l in lines if _inside(l[:4], tb, 3) and not (cap and _inside(l[:4], cap, 2)) and l[4].strip()]
    inn.sort(key=lambda l: (l[1] + l[3]) / 2)
    rows = []
    for l in inn:
        cy, h = (l[1] + l[3]) / 2, max(4.0, l[3] - l[1])
        if rows and abs(cy - rows[-1]["cy"]) < 0.6 * h:
            r = rows[-1]; r["rect"] = [min(r["rect"][0], l[0]), min(r["rect"][1], l[1]), max(r["rect"][2], l[2]), max(r["rect"][3], l[3])]
            r["toks"] |= set(_toks(l[4])); r["n"] += 1
        else:
            rows.append({"cy": cy, "rect": list(l[:4]), "toks": set(_toks(l[4])), "n": 1})
    for r in rows:                                   # a row band spans the table's width
        r["rect"] = [min(r["rect"][0], tb[0]), r["rect"][1] - 1, max(r["rect"][2], tb[2]), r["rect"][3] + 1]
    return rows

def match_row(rows, sentence, start=0):
    """Forward-only: the first row at or after start that the spoken sentence covers best (table speech reads one row per
    sentence). Returns the row index or None."""
    st = set(_toks(sentence))
    if not st: return None
    best, bi = 0.0, None
    for i in range(start, len(rows)):
        rt = rows[i]["toks"]
        if not rt: continue
        sc = len(rt & st) / len(rt)
        if sc > best + 0.05: best, bi = sc, i
    return bi if best >= 0.4 else None

def match_row_ocr(rows, sentence, start=0):
    """match_row for OCR-row tables (Phase 6 A2.3): when the header row (rows[0]) and a data row score equally, the data row
    wins; the header is the target only when it scores strictly higher than every data row."""
    i = match_row(rows, sentence, start)
    if i != 0: return i
    j = match_row(rows, sentence, 1)
    if j is None: return 0
    st = set(_toks(sentence))
    score = lambda r: len(r["toks"] & st) / len(r["toks"]) if r["toks"] else 0.0
    return 0 if score(rows[0]) > score(rows[j]) else j
