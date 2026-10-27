"""Document-aware focus: read the source PDF / Markdown file itself (read-only, local) to get exact
line, figure, caption and panel positions.

Two halves:
  * parse_pdf()  needs pypdfium2 (installed in the Kokoro venv); speakeasy runs it as a subprocess:
        <venv>/bin/python docmap.py parse-pdf FILE OUT.json
    The parse is cached by (path, size, mtime) under ~/.speakeasy/cache/doc/.
  * everything else is stdlib, imported by speakeasy.py (resolution, Markdown, page matching, figure refs, transforms).
Paths are never logged; only a short hash.

This file keeps the cache, the parse and its blocks, Markdown, loading, file resolution, page matching and page fits. The
sibling modules hold the rest: doccaps (caption vocabulary), docgeom (rectangles, tokens), figparts (figure > panel > part
hierarchy), doctables (table rows), doctyped (Phase 6 typed blocks), docrefs (sentence references).
"""
import hashlib, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from collections import Counter
# Moved verbatim into flat sibling modules (docs/architecture.md); re-exported because callers use docmap.<name>.
from doccaps import (CAP_RE, caption_info, LEX_EXPAND, LEX_KIND, LEX_NOUNS, LEX_RE, lexicon_match, PAGE_OF_RE, PANEL_RE, PANEL_REF_RE,
                     REF_RE, ROMAN, TAB_RE)
from docgeom import (_area, _centre_in, _clip, _inside, _overlap, _pad_label, _pt_dist, _rect_dist, _toks, _union, rect_order, tracker_bin)
from figparts import (_callout_parts, _caption_panels, _detector_substitute, _graphics, _guess_panels, _gutter_panels, _gutters, _hairline,
                      _hierarchy, _label_rows, _leader, _objects, _ocr_lines, _panel_letter, _panel_rect, _panels, _panels_below,
                      callout_number, CIRCLED, GUTTER_MIN, GUTTER_SPAN, HAIRLINE, LABEL_GAP, LEADER_MIN, part_hood)
from doctables import (_cell_rows, _image_table, _kind_by_layout, match_row, match_row_ocr, table_rows)
from doctyped import (_added_fig, _ambiguous_kind, _in_box_caption, _layout2, _pair_typed, _reading_sort, _scan_captions, _split_shared,
                      _type_regions, _widen_spanning, IN_BOX_JUNK_TOKENS)
from docrefs import (_alias_re, _CIRCLED_RE, _UNITS, _word_stem, callout_part, callout_ref, CALLOUT_REF_RE, content_words, device_alias,
                     DEVICE_WORDS, docmap_generic, figure_context, FIGURE_STEMS, FIGURE_WORDS, find_ref, focus_phrase, GENERIC,
                     ground_label, names_object, NUMBER_WORDS, NUMBER_WORDS_ANY, object_phrase, said_panel, target_rect)
# the re-exported names docmap does not use itself, so pyflakes counts them as used (nothing imports docmap with *)
__all__ = ["_alias_re", "_area", "_callout_parts", "_caption_panels", "_CIRCLED_RE", "_clip", "_graphics", "_guess_panels", "_gutter_panels",
           "_gutters", "_kind_by_layout", "_label_rows", "_layout2", "_leader", "_pad_label", "_panel_letter", "_panel_rect", "_panels",
           "_panels_below", "_pt_dist", "_rect_dist", "_scan_captions", "_UNITS", "_word_stem", "callout_number", "callout_part",
           "callout_ref", "CALLOUT_REF_RE", "CIRCLED", "content_words", "device_alias", "DEVICE_WORDS", "docmap_generic", "figure_context",
           "FIGURE_STEMS", "FIGURE_WORDS", "find_ref", "focus_phrase", "GENERIC", "ground_label", "GUTTER_MIN", "GUTTER_SPAN", "HAIRLINE",
           "IN_BOX_JUNK_TOKENS", "LABEL_GAP", "LEADER_MIN", "LEX_EXPAND", "LEX_KIND", "LEX_NOUNS", "LEX_RE", "lexicon_match", "match_row",
           "match_row_ocr", "names_object", "NUMBER_WORDS", "NUMBER_WORDS_ANY", "object_phrase", "PANEL_REF_RE", "part_hood", "rect_order",
           "REF_RE", "ROMAN", "said_panel", "table_rows", "target_rect"]

def path_hash(p):
    return hashlib.sha256((p or "").encode()).hexdigest()[:12] if p else None

def cache_dir():
    d = os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "cache", "doc")
    os.makedirs(d, exist_ok=True)
    return d

PARSE_VERSION = 9          # bump when parse_pdf output changes, so cached parses are rebuilt

def _key(path):
    st = os.stat(path)
    return hashlib.sha256(f"{os.path.realpath(path)}|{st.st_size}|{int(st.st_mtime)}|v{PARSE_VERSION}".encode()).hexdigest()[:20]

def _split_overlaps(figs):
    """A table's body grows over graphics below its caption; when that swallows a figure on the same page
    (Table 1 directly above Fig. 5), the table ends where the figure starts, and the figure keeps its own box."""
    for t in figs:
        if t.get("kind") != "table": continue
        for f in figs:
            if f.get("kind") != "figure": continue
            a, b = t["bbox"], f["bbox"]
            ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
            fa = max(1.0, (b[2] - b[0]) * (b[3] - b[1])); ta = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
            if ix * iy < 0.3 * fa or "caption_bbox" not in t:
                # the reverse: a figure body that runs over a table on the same page stops at the table's edge
                if ix * iy >= 0.3 * ta and not ("caption_bbox" in f and _inside(f["caption_bbox"], a)):
                    if (a[1] + a[3]) / 2 < (b[1] + b[3]) / 2: b[1] = min(b[3] - 1, a[3] + 2)
                    else: b[3] = max(b[1] + 1, a[1] - 2)
                    f["bbox"] = [round(v, 1) for v in b]; f["split_from"] = t["label"]
                    f["labels"] = [l for l in f.get("labels", []) if _inside(l[:4], f["bbox"])]
                    f["objects"] = [o for o in f.get("objects", []) if _inside(o[:4], f["bbox"])]
                continue
            cap = t["caption_bbox"]
            # a body that is only the swallowed figure leaves the table the space between its caption and the figure
            if b[1] >= cap[3] - 2:                                   # figure below the table caption: cut the table there
                if a[1] >= b[1] - 2: a[1] = cap[3]
                a[3] = max(a[1] + 1, b[1] - 2)
            elif b[3] <= cap[1] + 2:                                 # figure above it: the table starts below the figure
                a[1] = cap[3] if a[3] > cap[3] else min(a[3] - 1, b[3] + 2)
            t["bbox"] = [round(v, 1) for v in a]
            t["labels"] = [l for l in t.get("labels", []) if _inside(l[:4], t["bbox"])]
            t["split_from"] = f["label"]
    return figs

# ---------------- PDF (pypdfium2; runs in the doc venv) ----------------
def _text_lines(page):
    lines, blocks = page.text_lines()
    return [[*[round(v, 1) for v in bb], t] for bb, t in lines], blocks

def parse_pdf(path, fig_pages=None, prev=None, kinds_covered=()):
    """Text lines for every page; figures (bbox, caption, panels, text labels, graphic objects) for fig_pages (None = all).
    prev: an earlier partial parse whose figure pages are kept. kinds_covered: pages an existing kind sidecar covers (A2.5)."""
    import pdfpage, layoutdet, blockcls
    doc = pdfpage.open_pdf(path)
    detector = layoutdet.Detector()
    bc = blockcls.Session()
    pages = []
    old = {p["n"]: p for p in (prev or {}).get("pages", []) if p.get("figs_done")}
    if bc.on: bc.events = [e for e in (prev or {}).get("blockcls_events") or [] if e.get("page") in old and e.get("stage") == "classifier"]
    for pno, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        if pno + 1 in old and (fig_pages is None or True):
            pages.append(old[pno + 1]); continue
        if fig_pages is not None and pno + 1 not in fig_pages:
            lines, _ = _text_lines(page)
            pages.append({"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": [], "figs_done": False}); continue
        lines, blocks = _text_lines(page)
        columns = page_columns(lines, W); two_col = columns == 2
        gfx = []
        try:
            for r in page.images():
                if r and (r[2] - r[0]) > 20 and (r[3] - r[1]) > 20: gfx.append(list(r))
        except Exception: pass
        try:
            for r in page.cluster_drawings(on_page=True):
                if r.width > 20 and r.height > 20 and not (r.width > 0.9 * W and r.height > 0.9 * H): gfx.append([r.x0, r.y0, r.x1, r.y1])
        except Exception: pass
        lay = detector.layout(pno + 1, page, lines, W, H, columns)
        starts = (lambda s: caption_info(s) is not None) if bc.on else (lambda s: CAP_RE.match(s) or TAB_RE.match(s))
        if lay: blocks = lay.split_groups(blocks, starts)
        found, inbox = [], {}                               # captions: (line group, label fields, kind); polluted lines' in-box part
        for bl in blocks:
            if not bc.on:
                m = CAP_RE.match(lines[bl[0]][4]); kind = "figure"
                if not m: m = TAB_RE.match(lines[bl[0]][4]); kind = "table"
                if m: found.append((bl, {"label": f"{'Figure' if kind == 'figure' else 'Table'} {m.group(1)}", "n": int(m.group(1))}, kind))
                continue
            info = caption_info(lines[bl[0]][4])
            if info is None and lay:
                hit = _in_box_caption(page, lines[bl[0]], lay)
                if hit: info, inbox[bl[0]] = hit
            if info: found.append((bl, info, info["kind"]))
        lrect = lambda i: inbox[i][0] if i in inbox else lines[i][:4]
        ltext = lambda i: inbox[i][1] if i in inbox else lines[i][4]
        for k, (bl, info, kind) in enumerate(found):
            if kind == "ambiguous":
                cap0 = _union([lrect(i) for i in bl]); lh0 = sorted(lines[i][3] - lines[i][1] for i in bl)[len(bl) // 2]
                kind, ksrc = _ambiguous_kind(bc, pno + 1, page, lay, cap0, lh0, gfx, lines, W, H)
                found[k] = (bl, dict(caption_info(ltext(bl[0]), kind) or info, kind_src=ksrc, src=info["src"]), kind)
        if lay:
            regex_lines = {i for bl, _, _ in found for i in bl}
            found = [(lay.caption_lines(bl, regex_lines - set(bl)), m, kind) for bl, m, kind in found]
            paired = lay.bodies([(kind, _union([lrect(i) for i in bl]), sorted(lines[i][3] - lines[i][1] for i in bl)[len(bl) // 2])
                                 for bl, _, kind in found], {i for bl, _, _ in found for i in bl})
            if bc.on:
                paired = _pair_typed(bc, pno + 1, page, lay, found, paired, lrect, lines, W, H)
                paired = _split_shared(found, _widen_spanning(lay, found, paired, lrect, W), lrect, lines, gfx)
        else: paired = [None] * len(found)
        # graphics right at a caption's body side belong to that caption: another caption's body never chains over them
        own_gfx = []
        for bl, _, kind in found:
            c = _union([lrect(i) for i in bl])
            own_gfx.append({k for k, g in enumerate(gfx) if g[0] < c[2] and c[0] < g[2] and (0 <= c[1] - g[3] < 20 if kind == "figure" else -8 <= g[1] - c[3] < 20)})
        figs, caption_line_groups, hier = [], [], []
        for ci, ((bl, m, kind), detected_body) in enumerate(zip(found, paired)):
            others = set().union(*own_gfx[:ci], *own_gfx[ci + 1:]) - own_gfx[ci]
            free_gfx = [g for k, g in enumerate(gfx) if k not in others]
            cap = [round(v, 1) for v in _union([lrect(i) for i in bl])]
            # the caption's column on a two-column page: a table never takes the other column's text
            nxt = min((l for l in lines if l[1] >= cap[3] - 1), key=lambda l: l[1], default=None)
            in_col = nxt is not None and nxt[2] <= W / 2 + 10 if cap[2] <= W / 2 + 10 else nxt is not None and nxt[0] >= W / 2 - 10
            if not (two_col and in_col): col = (0, W)
            elif cap[2] <= W / 2 + 10: col = (cap[0] - 12, W / 2 + 6)
            else: col = (W / 2 - 6, W)
            lex = bc.on and m.get("src") in ("lexicon", "lexicon+in-box")
            if lex and col == (0, W) and (cap[2] <= W / 2 + 10 or cap[0] >= W / 2 - 10):
                # a lexicon caption in one half with content in the other half beside it: side by side, its body stays in its half
                left = cap[2] <= W / 2 + 10; lh_c = max(4.0, (cap[3] - cap[1]) / max(1, len(bl)))
                if any(l[1] < cap[3] + 0.5 * lh_c and l[3] > cap[1] - 0.5 * lh_c and (l[0] >= W / 2 - 10 if left else l[2] <= W / 2 + 10) for l in lines):
                    col = (cap[0] - 12, W / 2 + 6) if left else (W / 2 - 6, W)
            text = " ".join(ltext(i) for i in bl)
            right_col, narrowed_by_prose = cap[0] >= W / 2 - 10, False
            for second_pass in (False, True):              # again inside the gutter when other-column prose sits beside it
                # figure: graphics directly above the caption (chain upward); table: content below
                # (graphics that run into the caption are clipped at its top)
                cand = sorted([[g[0], g[1], g[2], min(g[3], cap[1] - 2)] for g in free_gfx if g[1] < cap[1] - 20 and (not second_pass or (g[0] >= col[0] - 4 if right_col else g[2] <= col[1] + 4)) and g[3] >= cap[1] - 0.7 * H
                               and not (g[3] > cap[3] + 20) and (not lex or (g[0] < col[1] and g[2] > col[0]))], key=lambda g: -g[3]) if kind == "figure" \
                    else sorted([g for g in free_gfx if g[1] >= cap[3] - 8 and g[1] <= cap[3] + 0.7 * H and g[0] >= col[0] - 4 and g[2] <= col[1] + 4], key=lambda g: g[1])
                body = None
                for g in cand:
                    if body is None:
                        gap = cap[1] - g[3] if kind == "figure" else g[1] - cap[3]
                        if gap < 60: body = list(g)
                    elif (body[1] - g[3] < 40) if kind == "figure" else (g[1] - body[3] < 40):
                        body = _union([body, g])
                bbox_src = "graphics" if body else "caption-only"
                if kind == "table":
                    # the body is the grid / text block right below the caption (until a big gap, the next caption or the page end)
                    below = sorted([l for l in lines if l[1] >= cap[3] - 1 and l[0] >= col[0] and l[2] <= col[1]], key=lambda l: l[1])
                    y = cap[3]; rows = []
                    lh = max(8.0, (cap[3] - cap[1]) / max(1, len(bl)))
                    for l in below:
                        if l[1] - y > 2.2 * lh and rows: break
                        if starts(l[4]): break
                        rows.append(l); y = max(y, l[3])
                    tbody = _union([r[:4] for r in rows]) if rows else None
                    if tbody and (body is None or tbody[3] > body[3]):
                        bbox_src = "graphics+text-rows" if body else "text-rows"
                        body = _union([body, tbody]) if body else tbody
                    if body: body[1] = max(body[1], cap[3])        # the caption is never part of the table box
                if body is None and kind == "figure":            # no vector/image found: the text-free gap above the caption
                    above = [l for l in lines if l[3] <= cap[1] - 2 and l[0] < cap[2] and l[2] > cap[0]]
                    top = max([l[3] for l in above if cap[1] - l[3] > 30] or [None]) if above else None
                    if top is not None and cap[1] - top > 40: body = [cap[0], top + 2, cap[2], cap[1] - 2]; bbox_src = "whitespace"
                narrowed = layoutdet.body_column(lines, W, columns, cap, body) if body else col
                if second_pass or (narrowed[0] <= col[0] and narrowed[1] >= col[1]): break
                col, narrowed_by_prose = (max(col[0], narrowed[0]), min(col[1], narrowed[1])), True
            if body and narrowed_by_prose: body = [max(body[0], col[0]), *body[1:]] if right_col else [*body[:2], min(body[2], col[1]), body[3]]   # the gutter side only
            if lex and kind == "table" and not detected_body:
                # a lexicon table caption under its table: cell rows above it and none below it make the body
                lh_r = sorted(lines[i][3] - lines[i][1] for i in bl)[len(bl) // 2]
                up, down = _cell_rows(lines, cap, lh_r, W, True), _cell_rows(lines, cap, lh_r, W, False)
                if len(up) >= 2 and len(down) < 2:
                    body = _union([l[:4] for row in up for l in row]); bbox_src = "text-rows"
                    body = _union([body] + [g for g in free_gfx if _overlap(g, body)])
            if detected_body: body, bbox_src = detected_body, "detector"
            # 4.1 evidence: a lexicon caption tops a detector caption box (or is its in-box part) or has a body of its kind
            if bc.on and m["src"] != "regex" and not (m["src"] == "lexicon+in-box" or detected_body or bbox_src in ("graphics", "graphics+text-rows")
                                                      or (kind == "table" and bbox_src == "text-rows") or (lay and lay.caption_box_top(bl[0]))): continue
            # caption-only: a 1-pt strip on the body side, so the box never includes the caption
            fb = body or ([cap[0], max(0, cap[1] - 1), cap[2], cap[1]] if kind == "figure" else [cap[0], cap[3], cap[2], min(H, cap[3] + 1)])
            fb = [round(v, 1) for v in (max(0, fb[0]), max(0, fb[1]), min(W, fb[2]), min(H, fb[3]))]
            if kind == "figure" and lay and _hairline(fb):
                sub = _detector_substitute(lay.boxes, cap)
                if sub: fb, body, bbox_src = [round(v, 1) for v in sub], sub, "detector-substitute"
            labels = [] if kind == "table" else [{"l": (PANEL_RE.match(l[4]).group(1) or PANEL_RE.match(l[4]).group(2) or PANEL_RE.match(l[4]).group(3)).lower(), "r": l[:4]}
                      for l in lines if PANEL_RE.match(l[4]) and len(l[4]) <= 40 and body
                      and l[0] >= fb[0] - 6 and l[2] <= fb[2] + 6 and l[1] >= fb[1] - 6 and l[3] <= fb[3] + 6]
            seen = set(); labels = [x for x in labels if not (x["l"] in seen or seen.add(x["l"]))]
            labels.sort(key=lambda x: x["l"])
            # the figure's own text labels ("WANDmini", "Base Station", "EEG"): words, not tick numbers
            tl = [l for l in lines if body and _inside(l[:4], fb) and re.search(r"[A-Za-z]{2,}", l[4]) and not PANEL_RE.match(l[4])]
            src_l, ocr = "pdf", []
            if body and kind == "figure" and len(tl) < 3:
                ocr = _ocr_lines(page, fb)
                tl = [o for o in ocr if re.search(r"[A-Za-z]{2,}", o[4])] or tl; src_l = "ocr" if tl else "none"
            figs.append({"label": m["label"], "kind": kind, "n": m["n"],
                         "page": pno + 1, "bbox": fb, "caption_bbox": cap, "caption": text[:300], "panels": [], "parts": [],
                         "labels": tl[:200], "labels_src": src_l, "objects": _objects(page, fb) if body and kind == "figure" else [], "bbox_src": bbox_src})
            if bc.on: figs[-1].update(captioned=True, printed_label=m["printed_label"], aliases=m["aliases"], caption_src=m["src"], kind_src=m["kind_src"])
            if kind == "figure": hier.append((figs[-1], labels, ocr))
            caption_line_groups.append(bl)
        kinds = layout2 = None
        if bc.on and lay:
            kinds, layout2 = _type_regions(bc, pno + 1, page, lay, lines, figs, caption_line_groups, hier, gfx, W, H)
        figs = _split_overlaps(figs)
        shapes = []
        def page_shapes():
            if not shapes: shapes.extend([page.paths(), page.images()])
            return shapes
        for f, panel_labels, ocr in hier:
            f["degenerate"] = _hairline(f["bbox"])
            _hierarchy(f, lines, [p for p in panel_labels if _inside(p["r"], f["bbox"], 6)], ocr, page_shapes)
        if bc.on:
            for k, f in enumerate(figs):
                if f["kind"] == "table" and _image_table(page, lines, f["bbox"]):
                    o = blockcls.ocr_region(bc.ocr_bin, page, f["bbox"], W, H, f"t{pno + 1}_{k}")
                    f["ocr_rows"], f["rows_src"] = blockcls.ocr_rows(o, f["bbox"])
                    if kinds is not None and kinds.get("ocr") != "unavailable": kinds["ocr"] = "ok" if o is not None else "unavailable"
        page_bl = page_blocks(lines, blocks, figs, caption_line_groups, W, columns, lay)
        if bc.on and any(f.get("kind_src") != "caption" for f in figs): figs = _reading_sort(figs, page_bl)
        pg = {"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": figs, "figs_done": True,
              "columns": columns, "blocks": page_bl, "layout": detector.page_record(pno + 1, lay, page_bl)}
        if kinds is not None: pg["kinds"] = kinds; pg["layout2"] = layout2
        pages.append(pg)
    out = {"kind": "pdf", "pages": pages, "n_pages": len(pages), "layout": detector.status(), "layout_events": detector.events, "blockcls": bc.top(kinds_covered)}
    if bc.on:
        out["blockcls_events"] = sorted(bc.events + bc.det2.events, key=lambda e: e.get("page") or 0)
        out["layout2"] = dict(bc.det2.status(), routed=bc.layout2_routed)
    return out

def page_columns(lines, W):
    """2 when at least 8 half-width lines sit in each half of the page (the figure/table column logic), else 1."""
    half = [l for l in lines if 0.25 * W < l[2] - l[0] < 0.52 * W]
    return 2 if sum(1 for l in half if l[2] <= W / 2 + 10) >= 8 and sum(1 for l in half if l[0] >= W / 2 - 10) >= 8 else 1

def line_groups(lines):
    """pdfpage.text_lines' block rule on page lines: consecutive lines that follow closely below and overlap horizontally."""
    groups, cur = [], []
    for k, l in enumerate(lines):
        if cur:
            prev = lines[cur[-1]]; hgt = max(1.0, prev[3] - prev[1])
            if not (0 <= l[1] - prev[3] < 0.8 * hgt and l[0] < prev[2] and prev[0] < l[2]): groups.append(cur); cur = []
        cur.append(k)
    if cur: groups.append(cur)
    return groups

def page_blocks(lines, groups, figs, caption_line_groups, W, columns, lay=None):
    """The page's blocks in reading order: a figure/table block and a caption block per figures[] entry; paragraphs are
    the remaining runs of each line group, without caption lines and lines inside a figure or table box.
    A caption block holds its own line group plus every line inside its caption box (inline-math fragments that the
    text layer puts in groups of their own). With a detector layout (lay), its paragraph boxes take their lines first
    and only the lines no detector box claims or excludes form line-group paragraphs."""
    out, caption_lines = [], set().union(*[cl for cl in caption_line_groups if cl])
    for f, cl in zip(figs, caption_line_groups):
        out.append({"kind": f["kind"], "bbox": list(f["bbox"]), "label": f["label"], "src": f["bbox_src"]})
        if "kind_src" in f: out[-1]["kind_src"] = f["kind_src"]
        if cl is None: continue                          # an uncaptioned figure / table: no caption block
        own = sorted(set(cl) | {i for i, l in enumerate(lines) if i not in caption_lines and _inside(l[:4], f["caption_bbox"])})
        out.append({"kind": "caption", "bbox": list(f["caption_bbox"]), "lines": own, "label": f["label"],
                    "src": "regex+detector" if lay and cl and cl[0] in lay.extended else "regex"})
        caption_lines.update(own)
    taken = caption_lines | {i for i, l in enumerate(lines) if any(_inside(l[:4], f["bbox"]) for f in figs)}
    if lay:
        paragraphs, taken = lay.paragraphs(taken)
        out += [{"kind": "paragraph", "bbox": _union([lines[j][:4] for j in p]), "lines": p, "src": "detector"} for p in paragraphs]
    for g in groups:
        run = []
        for i in list(g) + [None]:
            if i is not None and i not in taken:
                run.append(i); continue
            if run: out.append({"kind": "paragraph", "bbox": _union([lines[j][:4] for j in run]), "lines": run, "src": "lines"}); run = []
    return reading_order(out, W, columns)

def reading_order(blocks, W, columns):
    """Column by column, top to bottom. On a two-column page a block spanning both columns ends the band above it."""
    top_left = lambda b: (b["bbox"][1], b["bbox"][0])
    if columns == 1: return sorted(blocks, key=top_left)
    def column(b):
        x0, _, x1, _ = b["bbox"]
        return None if x0 < W / 2 - 10 and x1 > W / 2 + 10 else 0 if x0 + x1 < W else 1
    out, rest = [], [b for b in blocks if column(b) is not None]
    for span in sorted([b for b in blocks if column(b) is None], key=top_left) + [None]:
        cut = span["bbox"][1] if span else float("inf")
        out += sorted([b for b in rest if b["bbox"][1] < cut], key=lambda b: (column(b), *top_left(b)))
        rest = [b for b in rest if b["bbox"][1] >= cut]
        if span: out.append(span)
    return out

def paragraph_region(page, line_ix):
    """Union of the page's paragraph blocks holding any of the page lines line_ix; None when no paragraph holds one."""
    ix = set(line_ix)
    return _union([b["bbox"] for b in page.get("blocks") or [] if b["kind"] == "paragraph" and ix & set(b["lines"])])

def parse_summary(pg):
    """Counts-only summary of a parsed page for the runs log (never text)."""
    if not pg.get("figs_done"):
        return {"columns": page_columns(pg["lines"], pg["w"]), "figs_done": False, "blocks": {"paragraph": len(line_groups(pg["lines"]))}}
    figs = pg["figures"]; kinds = [b["kind"] for b in pg["blocks"]]
    panels = [p for f in figs for p in f.get("panels") or []]; parts = [p for f in figs for p in f.get("parts") or []]
    out = {"columns": pg["columns"], "figs_done": True, "blocks": {k: kinds.count(k) for k in ("paragraph", "caption", "figure", "table")},
           "bbox_src": dict(Counter(f["bbox_src"] for f in figs)), "labels_src": {k: sum(f.get("labels_src") == k for f in figs) for k in ("pdf", "ocr", "none")},
           "panels": {"labels": sum(p.get("src") == "label" for p in panels), "guessed": sum(p.get("src") == "caption" for p in panels),
                      "gutter": sum(p.get("src") == "gutter" for p in panels)},
           "parts": {"text": sum(p["kind"] == "text" for p in parts), "callout": sum(p["kind"] == "callout" for p in parts),
                     "callout_attached": sum(p["kind"] == "callout" and p["attached"] for p in parts)},
           "split": sum(1 for f in figs if f.get("split_from")), "layout": {k: v for k, v in pg["layout"].items() if k != "detector_version"}}
    if any("kind_src" in f for f in figs): out["kind_src"] = dict(Counter(f["kind_src"] for f in figs if "kind_src" in f))
    return out

# ---------------- Markdown (stdlib) ----------------
def parse_md(path):
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    els, figs, in_code = [], [], None
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s.startswith("```") or s.startswith("~~~"):
            if in_code is None: in_code = i
            else: els.append({"kind": "code", "line": in_code, "end": i}); in_code = None
            continue
        if in_code is not None: continue
        if re.match(r"^#{1,6}\s", s): els.append({"kind": "heading", "level": len(s) - len(s.lstrip("#")), "line": i, "text": s.lstrip("#").strip()})
        elif re.match(r"^([-*+]|\d+[.)])\s", s): els.append({"kind": "list", "line": i, "text": re.sub(r"^([-*+]|\d+[.)])\s+", "", s)})
        elif s.startswith("|"): els.append({"kind": "table", "line": i, "text": s})
        for m in re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)", l):
            figs.append({"kind": "image", "alt": m.group(1), "line": i})
        s = re.sub(r"^\s*(?:[*_]{1,3}|<(?:em|i|b|strong|figcaption)>)\s*|\s*(?:[*_]{1,3}|</(?:em|i|b|strong|figcaption)>)\s*$", "", s)
        if CAP_RE.match(s) or TAB_RE.match(s):
            m = CAP_RE.match(s) or TAB_RE.match(s)
            figs.append({"kind": "caption", "label": f"{'Figure' if CAP_RE.match(s) else 'Table'} {m.group(1)}", "line": i, "caption": s[:300]})
    # label images by an adjacent caption (or their alt text)
    out, n = [], 0
    for f in figs:
        if f["kind"] == "image":
            cap = next((c for c in figs if c["kind"] == "caption" and abs(c["line"] - f["line"]) <= 2), None)
            m = CAP_RE.match(f["alt"]) if f["alt"] else None
            n += 1
            label = cap["label"] if cap else (f"Figure {m.group(1)}" if m else None)
            out.append({"label": label, "kind": "figure", "line": f["line"], "caption": (cap or {}).get("caption") or f["alt"][:300],
                        "caption_line": cap["line"] if cap else None, "order": n})
        elif not any(abs(f["line"] - g["line"]) <= 2 for g in figs if g["kind"] == "image"):
            out.append({"label": f["label"], "kind": "figure" if f["label"].startswith("Figure") else "table", "line": f["line"],
                        "caption": f["caption"], "caption_line": f["line"]})
    return {"kind": "md", "lines": lines, "elements": els, "figures": out}

# ---------------- loading with cache ----------------
def venv_python():
    p = os.environ.get("SPEAKEASY_DOC_PYTHON") or os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "tts-venv", "bin", "python")
    return p if os.path.exists(p) else None

def cached(path):
    try: return os.path.exists(os.path.join(cache_dir(), _key(path) + ".json"))
    except OSError: return False

def load(path, timeout=30, sel_text=""):
    """Parsed document (cached). PDFs: text for all pages, figures for the selection's page (and its neighbours) only,
    so a first read stays short; later reads of other pages add their figures. Returns (doc, cache_state)."""
    kind = "pdf" if path.lower().endswith(".pdf") else "md" if re.search(r"\.(md|markdown|mdx)$", path, re.I) else None
    if not kind: return None, "unsupported"
    c = os.path.join(cache_dir(), _key(path) + ".json")
    prev = None
    if os.path.exists(c):
        try:
            prev = json.load(open(c))
            if kind == "md": return prev, "hit"
            pg, _ = match_page(prev, sel_text)
            if pg is None or pg.get("figs_done", True): return merge_kinds(prev, c), "hit"
        except ValueError: prev = None
    if kind == "md":
        doc = parse_md(path)
    else:
        py = venv_python()
        if not py or os.environ.get("SPEAKEASY_PDF_ENGINE", "auto").lower() == "pdfkit":
            # PDFKit (macOS, via the tracker binary): text lines for every page; figure boxes still need the doc venv parse
            trk = tracker_bin()
            if not os.path.exists(trk): return None, "no-pdf-engine"
            try:
                r = subprocess.run([trk, "--pdf-lines", path], capture_output=True, timeout=timeout)
                doc = json.loads(r.stdout.decode() or "{}")
            except Exception as e:
                return None, f"pdfkit-failed: {type(e).__name__}"
            if not doc.get("pages"): return None, "pdfkit-failed"
            for pg in doc["pages"]:                       # no figure parse: paragraph blocks only
                pg["columns"] = page_columns(pg["lines"], pg["w"])
                pg["blocks"] = page_blocks(pg["lines"], line_groups(pg["lines"]), [], [], pg["w"], pg["columns"])
                pg["layout"] = {"src": "heuristic", "reason": "pdfkit-engine"}
            doc["layout"] = {"status": "unavailable", "detector_version": None, "reason": "pdfkit-engine"}
            doc["layout_events"] = [{"event": "layout_detector_unavailable", "reason": "pdfkit-engine", "error": None, "detector_version": None, "page": None}]
            json.dump(doc, open(c, "w"))
            return doc, "miss-pdfkit"
        tmp = c + f".{os.getpid()}.tmp"
        selp = tmp + ".sel"
        with open(selp, "w") as f: f.write(sel_text or "")
        args = [py, os.path.realpath(__file__), "parse-pdf", path, tmp, "--sel", selp, "--kinds", c[:-5] + ".kinds.json"] + (["--prev", c] if prev else [])
        try:
            r = subprocess.run(args, capture_output=True, timeout=timeout)
        finally:
            try: os.remove(selp)
            except OSError: pass
        if r.returncode != 0 or not os.path.exists(tmp):
            return None, "parse-failed: " + r.stderr.decode(errors="replace").strip().splitlines()[-1][:120] if r.stderr else "parse-failed"
        os.replace(tmp, c)
        return merge_kinds(json.load(open(c)), c), "partial-miss" if prev else "miss"
    json.dump(doc, open(c, "w"))
    return doc, "miss"

def cache_path(path):
    return os.path.join(cache_dir(), _key(path) + ".json")

def merge_kinds(doc, cache_json):
    """A1.2: a done / partial kind sidecar merged into the loaded parse (never into the cache file): stage C figures and tables
    become blocks (kind_src vlm, U-labels), text answers release their lines to the orphan-line rule, and the stage C records are
    appended to blockcls_events. Entries for another KIND_PROMPT or vision model are ignored. Nothing from a failed sidecar."""
    side_p = cache_json[:-5] + ".kinds.json"
    if not doc or doc.get("kind") != "pdf" or not os.path.exists(side_p): return doc
    try:
        with open(side_p) as f: side = json.load(f)
    except (OSError, ValueError):
        return doc
    if side.get("state") not in ("done", "partial"): return doc
    import blockcls
    from llm_client import vision_model
    phash, model = blockcls.prompt_sha(), vision_model()
    pages = {p["n"]: p for p in doc["pages"] if p.get("figs_done")}
    for e in side.get("entries") or []:
        pg = pages.get(e.get("page"))
        if pg is None or e.get("prompt_sha") != phash or e.get("model") != model: continue
        r = [round(v, 1) for v in e["rect"]]
        if e["outcome"] == "accepted" and e.get("kind") in ("figure", "table"):
            k = 1 + sum(1 for f in pg["figures"] if f["kind"] == e["kind"] and f.get("captioned") is False)
            f = _added_fig(e["kind"], f"{'Figure' if e['kind'] == 'figure' else 'Table'} U{pg['n']}-{k}", pg["n"], r, "vlm", pg["lines"])
            f["degenerate"] = _hairline(f["bbox"])
            pg["figures"].append(f)
            pg["blocks"].append({"kind": f["kind"], "bbox": list(f["bbox"]), "label": f["label"], "src": "detector", "kind_src": "vlm"})
        elif e["outcome"] == "text":
            held = {i for b in pg["blocks"] for i in b.get("lines") or []}
            free = [i for i, l in enumerate(pg["lines"]) if i not in held and _centre_in(l, r)]
            for g in line_groups([pg["lines"][i] for i in free]):
                ix = [free[j] for j in g]
                pg["blocks"].append({"kind": "paragraph", "bbox": _union([pg["lines"][i][:4] for i in ix]), "lines": ix, "src": "lines"})
        else: continue
        pg["blocks"] = reading_order(pg["blocks"], pg["w"], pg.get("columns", 1))
        pg["figures"] = _reading_sort(pg["figures"], pg["blocks"])
    doc.setdefault("blockcls_events", []).extend({"event": "block_kind", "page": e["page"], "rect": e["rect"], "stage": "vlm", "kind": e.get("kind"),
                                                  "p_top": e.get("p_top"), "p_margin": e.get("p_margin"), "outcome": e["outcome"], "model": e.get("model"),
                                                  "provider": e.get("provider"), "confidence": e.get("confidence")} for e in side.get("entries") or [])
    doc["kind_sidecar"] = {"state": side["state"], "entries": len(side.get("entries") or [])}
    return doc

# ---------------- finding the file ----------------
def _from_url(u):
    if u and u.startswith("file://"):
        p = urllib.parse.unquote(urllib.parse.urlparse(u).path)
        return p if os.path.isfile(p) else None
    return None

def _editor_roots(app):
    base = os.path.expanduser("~/Library/Application Support/" + ("Cursor" if "cursor" in app.lower() else "Code"))
    roots = set()
    ws = os.path.join(base, "User", "workspaceStorage")
    try:
        for d in sorted(os.listdir(ws), key=lambda d: -os.path.getmtime(os.path.join(ws, d)))[:60]:
            try:
                j = json.load(open(os.path.join(ws, d, "workspace.json")))
                p = _from_url(j.get("folder", "")) or (urllib.parse.unquote(urllib.parse.urlparse(j.get("folder", "")).path) if j.get("folder", "").startswith("file://") else None)
                if p and os.path.isdir(p): roots.add(p)
            except (OSError, ValueError): pass
    except OSError: pass
    return roots

def _search(root, name, limit=4000):
    hits, n = [], 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "venv", ".venv", "__pycache__", "build", "dist", "tests", "test")]
        n += 1
        if n > limit: break
        if name in fns: hits.append(os.path.join(dp, name))
    return hits

def resolve(hint, mode="local"):
    """hint: {app, bundle, title, ax_document, url}. Returns (source, path_or_url, note)."""
    if mode == "off" or not hint: return None, None, "off"
    app = (hint.get("app") or ""); title = hint.get("title") or ""
    p = _from_url(hint.get("ax_document") or "")
    if p and re.search(r"\.(pdf|md|markdown|mdx)$", p, re.I):
        return ("preview" if "preview" in app.lower() else "ax-document"), p, "ax"
    if re.search(r"cursor|code", app, re.I):
        title = re.sub(r"^\s*(?:\[preview\]\s*|preview\s+)", "", title, flags=re.I)      # Markdown preview tabs
        m = re.match(r"^[\u25cf\s]*([^\s\u2014][^\u2014]*?\.(?:md|markdown|mdx|pdf))(?:\s*\([^)]*\))?\s+[\u2014-]\s+(.+?)(?:\s+[\u2014-]\s+.*)?$", title)
        if m:
            name, proj = m.group(1).strip(), m.group(2).strip()
            roots = [r for r in _editor_roots(app) if os.path.basename(r) == proj] or []
            hits = sorted(set(h for r in roots for h in _search(r, name)))
            if len(hits) == 1: return "editor", hits[0], "workspace-search"
            return None, None, f"editor: {len(hits)} matches"
        return None, None, "editor: no file in title"
    u = hint.get("url") or (hint.get("ax_document") if (hint.get("ax_document") or "").startswith("http") else None)
    if u:
        p = _from_url(u)
        if p: return "browser-file", p, "url"
        pth = urllib.parse.urlparse(u).path.lower()
        if mode == "all" and re.match(r"^https://", u) and (pth.endswith(".pdf") or "/pdf/" in pth or title.lower().rstrip().endswith(".pdf")):
            if not public_host(u): return None, None, "browser: not a public host"
            return "browser", u, "url"
        return None, None, "browser: not a public pdf" if mode == "all" else "browser: web off (SPEAKEASY_DOC_READ=local)"
    return None, None, "no document"

def public_host(url):
    """Only public internet hosts: no localhost, loopback, private, link-local, reserved or multicast addresses."""
    import ipaddress, socket
    h = (urllib.parse.urlparse(url).hostname or "").strip("[]").lower()
    if not h or h == "localhost" or h.endswith(".localhost") or h.endswith(".local") or h.endswith(".internal"): return False
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)}
    except OSError:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified: return False
    return bool(addrs)

def clean_tmp(tmpdir, max_age=86400):
    """Delete our own old downloads (speakeasy_doc_*, > 1 day). Only files this tool created."""
    for f in os.listdir(tmpdir):
        if f.startswith("speakeasy_doc_"):
            q = os.path.join(tmpdir, f)
            try:
                if time.time() - os.path.getmtime(q) > max_age: os.remove(q)
            except OSError: pass

def fetch_pdf(url, tmpdir, max_mb=40, timeout=8):
    """Public PDFs only: no cookies, no auth, refuse redirects to other hosts or non-PDF answers."""
    clean_tmp(tmpdir)
    if not public_host(url): raise ValueError("not a public host")
    dst = os.path.join(tmpdir, "speakeasy_doc_" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".pdf")
    if os.path.exists(dst): return dst
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            a, b = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
            if b.scheme != "https" or b.netloc != a.netloc or re.search(r"log ?in|sign ?in|auth|sso|account", b.path, re.I) or not public_host(newurl): return None
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)
    op = urllib.request.build_opener(NoRedir)
    req = urllib.request.Request(url, headers={"User-Agent": "speakeasy/1 (local reader)"})
    with op.open(req, timeout=timeout) as r:
        if "pdf" not in (r.headers.get("Content-Type") or "").lower(): raise ValueError("not a pdf")
        data = r.read(max_mb * 1024 * 1024 + 1)
    if len(data) > max_mb * 1024 * 1024 or not data.startswith(b"%PDF"): raise ValueError("not a pdf / too big")
    tmp = dst + ".part"
    with open(tmp, "wb") as f: f.write(data)
    os.replace(tmp, dst)
    return dst

# ---------------- matching ----------------
def match_page(doc, sel_text, title=""):
    """Visible page: the page whose text best covers the selection; else the window title's 'N of M'."""
    st = set(_toks(sel_text))
    if st:
        best, bs = None, 0.0
        for pg in doc["pages"]:
            pt = set(t for l in pg["lines"] for t in _toks(l[4]))
            s = len(st & pt) / len(st)
            if s > bs: best, bs = pg, s
        if best and bs >= 0.6: return best, "selection-text"
    m = PAGE_OF_RE.search(title or "")
    if m and 1 <= int(m.group(1)) <= len(doc["pages"]): return doc["pages"][int(m.group(1)) - 1], "title-page"
    return None, "none"

def sel_lines(page, sel_text):
    """Indices of page lines that are part of the selection, in order."""
    st = set(_toks(sel_text)); out = []
    for i, l in enumerate(page["lines"]):
        lt = set(_toks(l[4]))
        if lt and len(lt & st) / len(lt) >= 0.8: out.append(i)
    return out

def fit(pairs):
    """Uniform scale + offset from (page_rect, screen_rect) pairs: X = s*x + ox, Y = s*y + oy. Returns (s, ox, oy, rms) or None."""
    if len(pairs) < 2: return None
    P = [((a[0] + a[2]) / 2, (a[1] + a[3]) / 2) for a, _ in pairs]
    S = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for _, b in pairs]
    n = len(P); mpx = sum(p[0] for p in P) / n; mpy = sum(p[1] for p in P) / n; msx = sum(s[0] for s in S) / n; msy = sum(s[1] for s in S) / n
    num = sum((p[0] - mpx) * (s[0] - msx) + (p[1] - mpy) * (s[1] - msy) for p, s in zip(P, S))
    den = sum((p[0] - mpx) ** 2 + (p[1] - mpy) ** 2 for p in P)
    if den < 100: return None
    sc = num / den
    if sc <= 0: return None
    ox, oy = msx - sc * mpx, msy - sc * mpy
    rms = (sum((sc * p[0] + ox - s[0]) ** 2 + (sc * p[1] + oy - s[1]) ** 2 for p, s in zip(P, S)) / n) ** 0.5
    return sc, ox, oy, rms

def ocr_capture(png, frame, timeout=15):
    """Recognised text lines of a screenshot, boxes in screen points: [[x0, y0, x1, y1, text], ...]."""
    trk = tracker_bin()
    if not os.path.exists(trk) or not os.path.exists(png): return []
    try:
        r = subprocess.run([trk, "--ocr-lines", png, "--frame", ",".join(str(v) for v in frame)], capture_output=True, timeout=timeout)
        return [[float(v) for v in o["r"]] + [o.get("t", "")] for o in json.loads(r.stdout.decode() or "[]")]
    except Exception:
        return []

def anchor_fit(ocr, anchors, frame=None):
    """The tracker's page->screen fit: unique token matches of page lines to recognised lines (left edge, vertical centre),
    skipping lines cut by the capture edge, one outlier-rejection pass. Returns (s, ox, oy, rms, n) or None."""
    ol = [(set(_toks(o[4])), o) for o in ocr]
    P, S = [], []
    for a in anchors:
        at = set(_toks(a[4]))
        if len(at) < 3: continue
        c = [o for ot, o in ol if len(ot & at) / len(at) >= 0.75 and len(ot & at) / max(1, len(ot)) >= 0.6]
        if len(c) != 1: continue
        b = c[0]
        if frame and (b[0] <= frame[0] + 3 or b[2] >= frame[0] + frame[2] - 3): continue
        P.append((a[0], (a[1] + a[3]) / 2)); S.append((b[0], (b[1] + b[3]) / 2))
    def f(P, S):
        n = len(P)
        if n < 2: return None
        mpx = sum(p[0] for p in P) / n; mpy = sum(p[1] for p in P) / n; msx = sum(q[0] for q in S) / n; msy = sum(q[1] for q in S) / n
        num = sum((p[0] - mpx) * (q[0] - msx) + (p[1] - mpy) * (q[1] - msy) for p, q in zip(P, S))
        den = sum((p[0] - mpx) ** 2 + (p[1] - mpy) ** 2 for p in P)
        if den < 100: return None
        sc = num / den; return sc, msx - sc * mpx, msy - sc * mpy
    t = f(P, S)
    if t and len(P) >= 4:
        res = [((t[0] * p[0] + t[1] - q[0]) ** 2 + (t[0] * p[1] + t[2] - q[1]) ** 2) ** 0.5 for p, q in zip(P, S)]
        med = sorted(res)[len(res) // 2]
        keep = [i for i, v in enumerate(res) if v <= max(6, 2.5 * med)]
        if 2 <= len(keep) < len(P): P = [P[i] for i in keep]; S = [S[i] for i in keep]; t = f(P, S)
    if not t or not (0.3 <= t[0] <= 6): return None
    rms = (sum((t[0] * p[0] + t[1] - q[0]) ** 2 + (t[0] * p[1] + t[2] - q[1]) ** 2 for p, q in zip(P, S)) / len(P)) ** 0.5
    return (t[0], t[1], t[2], rms, len(P)) if rms <= 10 else None

def area_lines(lines, prect):
    """PDF text inside a rectangle (page space), like a PDF reader's rectangle / column selection (PDFKit
    PDFPage.selection(for:), Preview's Option-drag): lines whose vertical centre is inside and at least half of whose width
    is inside, ordered column by column (a gutter is an x no chosen line crosses), top to bottom within a column."""
    x0, y0, x1, y1 = prect
    sel = []
    for l in lines:
        cy = (l[1] + l[3]) / 2; w = max(0.1, l[2] - l[0])
        if y0 - 1 <= cy <= y1 + 1 and (min(l[2], x1) - max(l[0], x0)) / w >= 0.5 and l[4].strip(): sel.append(l)
    if not sel: return []
    # gutters: sweep the x extents; a gap between merged spans splits columns
    spans = sorted([max(l[0], x0), min(l[2], x1)] for l in sel)
    cols = [spans[0][:]]
    for a0, a1 in spans[1:]:
        if a0 > cols[-1][1] + 4: cols.append([a0, a1])
        else: cols[-1][1] = max(cols[-1][1], a1)
    def col(l):
        c = max(l[0], x0)
        return next(i for i, (c0, c1) in enumerate(cols) if c0 - 0.5 <= c <= c1 + 0.5)
    return sorted(sel, key=lambda l: (col(l), round(l[1], 0), l[0]))

def apply(t, r):
    s, ox, oy = t[:3]
    return [s * r[0] + ox, s * r[1] + oy, s * (r[2] - r[0]), s * (r[3] - r[1])]

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "parse-pdf":
        opt = dict(zip(sys.argv[4::2], sys.argv[5::2]))
        prev = json.load(open(opt["--prev"])) if opt.get("--prev") and os.path.exists(opt["--prev"]) else None
        sel = open(opt["--sel"]).read() if opt.get("--sel") else ""
        covered = set()
        if opt.get("--kinds"):
            import blockcls
            try: side = json.load(open(opt["--kinds"]))
            except (OSError, ValueError): side = None
            covered = set() if blockcls.rerun_due(side) else blockcls.covered_pages(side)
        if sel.strip():
            import pdfpage
            d0 = pdfpage.open_pdf(sys.argv[2])
            txt = {"pages": [{"n": i + 1, "lines": _text_lines(pg)[0]} for i, pg in enumerate(d0)]}
            pg, _ = match_page(txt, sel)
            want = {pg["n"] - 1, pg["n"], pg["n"] + 1} if pg else {1}
            out = parse_pdf(sys.argv[2], want, prev, covered)
        else:
            out = parse_pdf(sys.argv[2], None, prev, covered)
        json.dump(out, open(sys.argv[3], "w")); sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "dump":
        print(json.dumps(parse_pdf(sys.argv[2]) if sys.argv[2].lower().endswith(".pdf") else parse_md(sys.argv[2]), indent=1)[:6000]); sys.exit(0)
    print("usage: docmap.py parse-pdf FILE OUT.json | dump FILE", file=sys.stderr); sys.exit(2)
