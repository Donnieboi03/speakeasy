"""Phase 6 typed blocks in the PDF parse (phase6-block-classifier.md): in-box and lexicon captions, ambiguous nouns, typed
pairing, spanning and shared bodies, uncaptioned figures and tables (stage B), the second detector on gap pages, and the
caption OCR scan on no-text pages. Runs inside the doc venv parse; docmap re-exports every name."""
import re, time
from doccaps import PANEL_RE, caption_info
from docgeom import _area, _clip, _inside, _overlap, _union
from doctables import _kind_by_layout
from figparts import _gutters, _objects

IN_BOX_JUNK_TOKENS = 3            # leading text-layer tokens inside a caption box that may precede its caption anchor

def _in_box_caption(page, line, lay):
    """4.1 polluted text layer: the part of a non-matching line inside a detector caption box, when that part is a caption,
    after up to IN_BOX_JUNK_TOKENS leading tokens that are not ("ygy Fig. 3." -> "Fig. 3."). Returns (caption info, (rect of
    the matched characters, their text)) or None."""
    strong = lay.caption_boxes(); boxes = lay.caption_boxes(weak=True)
    cover = lambda b: _area(_clip(line[:4], b))
    for box in sorted(boxes, key=lambda b: (b not in strong, -cover(b), b)):        # detector boxes first, then the most of the line
        if not _overlap(line[:4], box): continue
        r = [max(line[0], box[0]), max(line[1], box[1]), min(line[2], box[2]), min(line[3], box[3])]
        if r[2] - r[0] < 4: continue
        words, cur = [], []
        for c, bb in page.chars_in(r) + [(" ", None)]:
            if c.isspace():
                if cur: words.append(cur); cur = []
            else: cur.append((c, bb))
        for w in range(min(len(words), IN_BOX_JUNK_TOKENS + 1)):
            t = " ".join("".join(c for c, _ in word) for word in words[w:])
            info = caption_info(t)
            if info:
                rect = _union([bb for word in words[w:] for _, bb in word])
                return dict(info, src="lexicon+in-box"), ([round(v, 1) for v in _clip(rect, r)], t)
    return None

def _ambiguous_kind(bc, n, page, lay, cap, lh, gfx, lines, W, H):
    """4.2: an ambiguous noun's kind and kind_src: stage B's family on its body (not textish, p_top >= T_B), else the detector
    class family, else the body side (_kind_by_layout). The body: the detector box it would pair with, else the nearest graphics
    above / below (60 pt)."""
    import blockcls
    if lay:
        adj = lay.adjacent_body(cap, lh)
        if adj:
            box = adj[0]
            fam = bc.body_family(n, page, lay.claim(box, cap, set(), commit=False), W, H, box["cls"], box["score"])
            return (fam, "classifier") if fam else (blockcls.DETECTOR_FAMILY[box["cls"]], "lexicon")
    else:
        near = [(cap[1] - g[3], g) for g in gfx if 0 <= cap[1] - g[3] < 60 and g[0] < cap[2] and cap[0] < g[2]]
        near += [(g[1] - cap[3], g) for g in gfx if 0 <= g[1] - cap[3] < 60 and g[0] < cap[2] and cap[0] < g[2]]
        fam = bc.body_family(n, page, min(near)[1], W, H, None, None) if near else None
        if fam: return fam, "classifier"
    return _kind_by_layout(cap, lh, gfx, lines, W), "lexicon"

def _pair_typed(bc, n, page, lay, found, paired, lrect, lines, W, H):
    """A2.4: a lexicon caption the detector classes leave unpaired takes the unpaired body box next to it (Phase 4 4.2's column,
    overlap and gap limits) when stage B types that box as the caption's kind at p_top >= T_B (TABLE I over a box the detector
    calls an image). Standard captions never do."""
    cap_lines = {i for bl, _, _ in found for i in bl}
    out = list(paired)
    for k, ((bl, info, kind), body) in enumerate(zip(found, paired)):
        if body is not None or info["src"] not in ("lexicon", "lexicon+in-box"): continue
        cap = _union([lrect(i) for i in bl]); lh = sorted(lines[i][3] - lines[i][1] for i in bl)[len(bl) // 2]
        adj = lay.adjacent_body(cap, lh, unpaired_only=True)
        if not adj: continue
        box = adj[0]; rect = lay.claim(box, cap, cap_lines, commit=False)
        if bc.body_family(n, page, rect, W, H, box["cls"], box["score"], anchor="lexicon-caption") == kind: out[k] = lay.claim(box, cap, cap_lines)
    return out

def _widen_spanning(lay, found, paired, lrect, W):
    """A lexicon caption across both columns of a two-column page keeps a full-width body: Phase 4's beside-prose narrowing
    reads column-width text inside a wide figure as the other column. Standard captions keep Phase 4's rule."""
    if lay.columns != 2: return paired
    cap_lines = {i for bl, _, _ in found for i in bl}
    out = list(paired)
    for k, ((bl, info, _), body) in enumerate(zip(found, paired)):
        cap = _union([lrect(i) for i in bl])
        if body is None or info["src"] == "regex" or not (cap[0] < W / 2 - 10 and cap[2] > W / 2 + 10): continue
        if body[2] - body[0] < 0.75 * (cap[2] - cap[0]): out[k] = lay.widen(body, cap, cap_lines)
    return out

def _split_shared(found, paired, lrect, lines, gfx):
    """Side-by-side captions over one detector box (two tables the detector saw as one): a lexicon caption's body that reaches
    under another caption of its row is cut at the rightmost whitespace gutter between the two captions' left edges. The other
    side goes to that caption when it is a lexicon caption without a body. Standard captions' bodies are never changed."""
    cap_lines = {i for bl, _, _ in found for i in bl}
    caps = [_union([lrect(i) for i in bl]) for bl, _, _ in found]
    out = list(paired)
    for k, (_, info, _) in enumerate(found):
        b = out[k]
        if b is None or info["src"] == "regex": continue
        for j, (_, info_j, _) in enumerate(found):
            ck, cj = caps[k], caps[j]
            h = max(ck[3] - ck[1], cj[3] - cj[1])
            if j == k or abs(ck[1] - cj[1]) > 2 * h or _overlap([ck[0], 0, ck[2], 1], [cj[0], 0, cj[2], 1]) or not b[0] < (cj[0] + cj[2]) / 2 < b[2]: continue
            items = [_clip(l[:4], b) for i, l in enumerate(lines) if i not in cap_lines and _overlap(l[:4], b)] + [_clip(g, b) for g in gfx if _overlap(g, b)]
            left, right = (ck, cj) if ck[0] < cj[0] else (cj, ck)
            cuts = [x for x in _gutters(b, items, 0) if left[0] < x <= right[0] + 2]
            if not cuts: continue
            x = round(max(cuts), 1)
            mine, other = ([b[0], b[1], x, b[3]], [x, b[1], b[2], b[3]]) if ck is left else ([x, b[1], b[2], b[3]], [b[0], b[1], x, b[3]])
            out[k] = b = mine
            if out[j] is None and info_j["src"] != "regex": out[j] = other
    return out

def _added_fig(kind, label, pno, bbox, kind_src, lines, page=None):
    """An uncaptioned figure / table entry (9.1): U-label, captioned false, no caption_bbox."""
    tl = [l for l in lines if _inside(l[:4], bbox) and re.search(r"[A-Za-z]{2,}", l[4]) and not PANEL_RE.match(l[4])] if kind == "figure" else []
    return {"label": label, "kind": kind, "n": None, "page": pno, "bbox": [round(v, 1) for v in bbox], "caption": "", "panels": [], "parts": [],
            "labels": tl[:200], "labels_src": "pdf" if tl else "none", "objects": _objects(page, bbox) if page is not None and kind == "figure" else [],
            "bbox_src": "detector", "captioned": False, "printed_label": None, "aliases": [], "caption_src": None, "kind_src": kind_src}

def _reading_sort(figs, blocks):
    """figures[] in the page's block reading order (fig_id order, 9.4)."""
    pos = {}
    for k, b in enumerate(blocks):
        if b["kind"] in ("figure", "table"): pos.setdefault((b["kind"], tuple(b["bbox"])), k)
    return sorted(figs, key=lambda f: pos.get((f["kind"], tuple(f["bbox"])), len(blocks)))

def _type_regions(bc, n, page, lay, lines, figs, caption_line_groups, hier, gfx, W, H):
    """Contract 3 / 5 / 7.1 / A3 on one detected page, after its captioned figures: OCR captions on a no-text page; the gap-page
    router; on a routed page heron's figure / table boxes join the unpaired PP-DocLayout-S bodies as candidates, and on an R1 page
    ink regions no detector explains do too; then the filters and stage B. Accepted regions are appended to figs (U-labels).
    Returns (pages[].kinds, pages[].layout2)."""
    import blockcls
    no_text = not lines
    if no_text and bc.ocr_bin: _scan_captions(bc, n, page, lay, figs, caption_line_groups, W, H)
    cap_lines = set().union(*[set(cl or []) for cl in caption_line_groups]) if caption_line_groups else set()
    taken = cap_lines | {i for i, l in enumerate(lines) if any(_inside(l[:4], f["bbox"]) for f in figs)}
    paragraphs = [_union([lines[j][:4] for j in p]) for p in lay.paragraphs(taken)[0]]
    added, l2 = _layout2(bc, n, page, lay, lines, figs, paragraphs, cap_lines, gfx, W, H)
    cands, dropped = blockcls.filter_candidates(lay.unpaired_bodies(cap_lines) + added, [f["bbox"] for f in figs], paragraphs, W, H)
    l2["added"] = sum(c.get("det") == "heron" for c in cands)
    if "R1" in l2["by"] and not blockcls.layout2_off():
        t0 = time.perf_counter()
        bodies = [b["bbox"] for b in lay.boxes if b["role"] in ("figure", "table")]
        existing = [c["bbox"] for c in cands] + [f["bbox"] for f in figs] + bodies
        # text evidence: the detectors' text-like boxes; a full-page OCR (seconds) would break the 50 ms fallback budget
        ink = blockcls.structural_candidates(page, W, H, l2.pop("_text"), existing)
        l2["structural"] = len(ink); l2["ms"] += int(round((time.perf_counter() - t0) * 1000)); cands += ink
    l2.pop("_text", None)
    if l2["routed"]:
        bc.events.append({"event": "layout2_page", "page": n, "by": l2["by"], "ms": l2["ms"], "added": l2["added"], "structural": l2["structural"],
                          "detector_version": blockcls.DETECTOR2_VERSION if l2["src"] == "heron" else None})
    accepted, kinds = bc.classify_candidates(n, page, cands, W, H, "no-text" if no_text else "uncaptioned", dropped)
    count = {"figure": 0, "table": 0}
    for kind, c in accepted:
        count[kind] += 1
        f = _added_fig(kind, f"{'Figure' if kind == 'figure' else 'Table'} U{n}-{count[kind]}", n, c["bbox"], "classifier", lines, page)
        figs.append(f); caption_line_groups.append(None)
        if kind == "figure": hier.append((f, [], []))
    kinds["no_text"] = no_text
    if no_text and not bc.ocr_bin: kinds["ocr"] = "unavailable"
    return kinds, l2

def _layout2(bc, n, page, lay, lines, figs, paragraphs, cap_lines, gfx, W, H):
    """A3.2 / A3.4: route the page; on a routed page (at most ROUTE_CAP per parse) run heron once. Returns (heron candidates that no
    PP-DocLayout-S body box holds at IoU >= 0.5 and no captioned body or paragraph block covers by more than 30 %, the page's
    layout2 record with the text evidence for A3.3 under "_text")."""
    import blockcls
    text = [b["bbox"] for b in lay.boxes if b["cls"] in blockcls.PPS_TEXT]
    bodies = [b["bbox"] for b in lay.boxes if b["role"] in ("figure", "table")]
    explained = bodies + [b["bbox"] for b in lay.boxes if b["role"] == "paragraph"] + [f["bbox"] for f in figs]
    by = blockcls.route_page(lines, W, H, gfx, explained)
    l2 = {"routed": bool(by), "by": by, "src": None, "ms": 0, "boxes": {"figure": 0, "table": 0}, "added": 0, "structural": 0, "_text": text}
    if not by: return [], l2
    if bc.layout2_routed >= blockcls.ROUTE_CAP: l2.update(src="skipped", skip="over-cap"); return [], l2
    bc.layout2_routed += 1
    det2 = bc.det2
    if not det2.ready(): l2.update(src="unavailable", reason=det2.reason); return [], l2
    t0 = time.perf_counter()
    try: boxes = det2.detect(page, W, H)
    except Exception as e:                          # one failed page keeps today's path alone
        det2._unavailable("inference-failed", e, n); l2.update(src="unavailable", reason="inference-failed"); return [], l2
    l2.update(src="heron", ms=int(round((time.perf_counter() - t0) * 1000)))
    bc.layout2_ms.append(l2["ms"])
    text += [b["bbox"] for b in boxes if b["cls"] in blockcls.HERON_TEXT]
    out = []
    for b in boxes:
        fam = blockcls.HERON_FAMILY.get(b["cls"])
        if not fam: continue
        l2["boxes"][fam] += 1
        r = b["bbox"]
        if any(blockcls._iou(r, p) >= blockcls.HERON_DUP_IOU for p in bodies): continue
        if any(blockcls._inter(r, o) > blockcls.COVER_FRAC * max(1.0, _area(r)) for o in [f["bbox"] for f in figs] + paragraphs): continue
        out.append({"cls": b["cls"], "score": b["score"], "bbox": lay.snap_box(r, cap_lines), "det": "heron"})
    return out, l2

def _scan_captions(bc, n, page, lay, figs, caption_line_groups, W, H):
    """7.1: on a no-text page, OCR the detector caption boxes; a standard or lexicon caption pairs with its detector body as usual."""
    import blockcls
    caps = []
    for k, box in enumerate(lay.caption_boxes()):
        o = blockcls.ocr_region(bc.ocr_bin, page, box, W, H, f"c{n}_{k}")
        if not o or not o["lines"]: continue
        ls = sorted(o["lines"], key=lambda l: (l[1], l[0]))
        info = caption_info(ls[0][4])
        if info and info["kind"] != "ambiguous":
            caps.append((info, [round(v, 1) for v in _union([l[:4] for l in ls])], " ".join(l[4] for l in ls), sorted(l[3] - l[1] for l in ls)[len(ls) // 2]))
    if not caps: return
    paired = lay.bodies([(info["kind"], cap, lh) for info, cap, _, lh in caps], set())
    for (info, cap, text, _), body in zip(caps, paired):
        if body is None: continue
        figs.append({"label": info["label"], "kind": info["kind"], "n": info["n"], "page": n, "bbox": [round(v, 1) for v in body], "caption_bbox": cap,
                     "caption": text[:300], "panels": [], "parts": [], "labels": [], "labels_src": "none", "objects": [], "bbox_src": "detector",
                     "captioned": True, "printed_label": info["printed_label"], "aliases": info["aliases"], "caption_src": info["src"], "kind_src": info["kind_src"]})
        caption_line_groups.append([])
