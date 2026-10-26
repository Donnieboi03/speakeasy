#!/usr/bin/env python3
"""Real-PDF ground truth (contract section 4.4): agent annotations snapped to PDF geometry with pdfplumber (MIT, test-only).

  snap_gt.py lines PDF PAGE          list the raw text lines of a page with their index (for writing annotations)
  snap_gt.py objects PDF PAGE        list graphic objects (rects, curves, lines, images) of a page
  snap_gt.py build SPEC.json OUTDIR  OUTDIR/doc.pdf (unmodified arXiv PDF) + annotation spec -> gt.json, cases.json

Lines: pdfplumber character boxes grouped into words, words into text lines (same baseline band, horizontal gap < GAP pt and no gap > 6 pt across the page middle,
so the two columns stay separate). Annotations refer to raw line indexes; pages[].lines drops lines inside figure, table and
equation regions. Blocks: paragraph / caption / heading / header / footer / page_number = union of member line rects;
figure / table = union of graphic objects and characters inside the agent's region. Parts: the agent names the label text
(or a rough region); the part is snapped to the smallest graphic object holding the label (or region); otherwise the
agent's rect is kept with snapped: false. Sentences: located in the case's selected lines by an alphanumeric-only match.
Never uses src/ code, speakeasy runs or bench output."""
import hashlib, json, os, re, sys, unicodedata

GAP = 10.0
SLACK = 1.0


def r2(v): return round(float(v) + 0.0, 2)
def rect(x0, y0, x1, y1): return [r2(x0), r2(y0), r2(x1), r2(y1)]
def union(rs):
    rs = [r for r in rs if r]
    return rect(min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)) if rs else None
def within(a, b, s=SLACK): return a[0] >= b[0] - s and a[1] >= b[1] - s and a[2] <= b[2] + s and a[3] <= b[3] + s
def centre_in(a, b): cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2; return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]
def thash(s): return hashlib.sha256(re.sub(r"\s+", " ", s).strip().encode("utf-8")).hexdigest()[:12]
def alnum(s): return "".join(ch.lower() for ch in unicodedata.normalize("NFKC", s) if ch.isalnum())


def raw_lines(page):
    """[(rect, text)] in reading order: left column top-down, then right column (column split at the page middle)."""
    words = page.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5, y_tolerance=2)
    words.sort(key=lambda w: (round(w["bottom"], 0), w["x0"]))
    lines = []; mid = page.width / 2
    for w in words:
        wr = [w["x0"], w["top"], w["x1"], w["bottom"]]
        for ln in reversed(lines[-12:]):
            r = ln["r"]; ov = min(r[3], wr[3]) - max(r[1], wr[1])
            gap = wr[0] - ln["last_x1"]; gutter = ln["last_x1"] <= mid <= wr[0] and gap > 6
            if ov >= 0.5 * min(r[3] - r[1], wr[3] - wr[1]) and 0 <= gap < GAP and not gutter:
                ln["r"] = [min(r[0], wr[0]), min(r[1], wr[1]), max(r[2], wr[2]), max(r[3], wr[3])]; ln["t"].append(w["text"]); ln["last_x1"] = wr[2]; break
        else:
            lines.append({"r": wr, "t": [w["text"]], "last_x1": wr[2]})
    def key(ln):  # lines crossing the page middle first (titles, full-width captions), then left column, then right column
        r = ln["r"]; col = -1 if (r[0] < mid - 4 and r[2] > mid + 4) else (0 if r[2] <= mid + 4 else 1)
        return (col, round(r[1], 1), r[0])
    return [(rect(*ln["r"]), " ".join(ln["t"])) for ln in sorted(lines, key=key)]


def objects(page):
    out = []
    for kind in ("rects", "curves", "lines", "images"):
        for o in getattr(page, kind):
            r = [o["x0"], o["top"], o["x1"], o["bottom"]]
            if r[2] - r[0] < 0 or r[3] - r[1] < 0: continue
            out.append((kind[:-1], rect(*r)))
    return out


def snap_region(page, region):
    objs = [r for _, r in objects(page) if within(r, region)]
    chars = [rect(c["x0"], c["top"], c["x1"], c["bottom"]) for c in page.chars if within([c["x0"], c["top"], c["x1"], c["bottom"]], region)]
    return union(objs + chars) or rect(*region)


def find_label(page, text, region):
    """Tightest box of words spelling `text` inside region: each token matched by alnum text, every later token taken as the
    candidate nearest to the previous one (labels often wrap inside their box); the smallest union wins."""
    toks = [alnum(t) for t in text.split() if alnum(t)]
    ws = [(alnum(w["text"]), [w["x0"], w["top"], w["x1"], w["bottom"]]) for w in page.extract_words(x_tolerance=1.5, y_tolerance=2)
          if within([w["x0"], w["top"], w["x1"], w["bottom"]], region, 2)]
    cands = [[r for a, r in ws if a == t] for t in toks]
    if not toks or any(not c for c in cands): return None
    def ctr(r): return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
    best = None
    for r0 in cands[0]:
        chosen = [r0]
        for c in cands[1:]:
            cx, cy = ctr(chosen[-1]); chosen.append(min(c, key=lambda r: abs(ctr(r)[0] - cx) + abs(ctr(r)[1] - cy)))
        u = union(chosen); a = (u[2] - u[0]) * (u[3] - u[1])
        if best is None or a < best[0]: best = (a, u)
    return best[1]


def snap_part(page, part, fig_region):
    region = part.get("region") or fig_region
    lab = find_label(page, part["label_text"], region) if part.get("label_text") else None
    target = lab or (rect(*part["region"]) if part.get("region") else None)
    if target is None: raise SystemExit(f"part {part['name']!r}: label not found and no region given")
    cands = [r for k, r in objects(page) if k in ("rect", "curve", "image") and within(target, r, 0.5) and within(r, fig_region, 2)
             and (r[2] - r[0]) * (r[3] - r[1]) < 0.5 * (fig_region[2] - fig_region[0]) * (fig_region[3] - fig_region[1])]
    if cands:
        best = min(cands, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
        return best, True
    return (rect(*part["region"]) if part.get("region") else lab), False


def build(spec_path, out):
    import pdfplumber
    spec = json.load(open(spec_path, encoding="utf-8"))
    pdf_path = os.path.join(out, "doc.pdf")
    sha = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()
    if sha != spec["pdf_sha256"]: raise SystemExit(f"{spec['doc_id']}: doc.pdf sha256 {sha} != spec pdf_sha256")
    pages, sentences, parts, cases = [], [], [], []
    with pdfplumber.open(pdf_path) as pdf:
        raw = {}
        for pa in spec["pages"]:
            pg = pdf.pages[pa["n"] - 1]; L = raw_lines(pg); raw[pa["n"]] = (pg, L)
            blocks, regions = [], []
            for k, b in enumerate(pa["blocks"]):
                if "region" in b:
                    bb = snap_region(pg, b["region"]); regions.append(bb)
                else:
                    i, j = b["lines"]; bb = union([L[x][0] for x in range(i, j + 1)])
                e = {"id": f"p{pa['n']}b{k + 1}", "kind": b["kind"], "bbox": bb}
                if b.get("label"): e["label"] = b["label"]
                blocks.append(e)
            keep = [r for r, t in L if not any(centre_in(r, g) for g in regions)]
            pages.append({"n": pa["n"], "w": r2(pg.width), "h": r2(pg.height), "blocks_scored": True, "lines": keep, "blocks": blocks})
            figs = {b.get("label"): b for b in blocks if b["kind"] in ("figure", "table")}
            for pt in spec.get("parts", []):
                if pt["page"] != pa["n"]: continue
                fb = figs[pt["figure"]]["bbox"]
                bb, snapped = snap_part(pg, pt, fb)
                parts.append({"id": f"pt{len(parts) + 1}", "name": pt["name"], "figure": pt["figure"], "panel": pt.get("panel"), "page": pa["n"],
                              "bbox": bb, "style": pt.get("style", "inside"), "snapped": snapped})
        for cs in spec["cases"]:
            pg, L = raw[cs["page"]]; page_gt = next(p for p in pages if p["n"] == cs["page"])
            i, j = cs["lines"]; sel = [(L[x][0], L[x][1]) for x in range(i, j + 1)]
            if any(r not in page_gt["lines"] for r, _ in sel): raise SystemExit(f"{spec['doc_id']}/{cs['id']}: selection holds a line inside a figure/table region")
            norm, owner = "", []
            for li, (r, t) in enumerate(sel):
                a = alnum(re.sub(r"\[[^\]]*\]", "", t)); norm += a; owner += [li] * len(a)
            script, cursor = [], 0
            for s in cs["script"]:
                sid = f"s{len(sentences) + 1}"
                g = {"id": sid, "page": cs["page"], "text_hash": thash(s["text"]), "score": s.get("score", "lines"), "line_rects": []}
                if "part" in s:
                    panel, _, name = s["part"].rpartition(":") if ":" in s["part"] else (None, None, s["part"])
                    hits = [p for p in parts if p["name"] == name and p["figure"] == s.get("figure", cs.get("figure")) and (panel is None or p["panel"] == panel)]
                    if len(hits) != 1: raise SystemExit(f"{spec['doc_id']}/{cs['id']}: part ref {s['part']!r} matches {len(hits)}")
                    g.update(score="parts", part=hits[0]["id"], page=hits[0]["page"])
                elif g["score"] == "lines":
                    a = alnum(s["text"]); k = norm.find(a, cursor)
                    if k < 0: raise SystemExit(f"{spec['doc_id']}/{cs['id']}: sentence not found in selection: {s['text'][:50]!r}")
                    held = sorted(set(owner[k:k + len(a)])); g["line_rects"] = [sel[x][0] for x in held]; cursor = k + len(a)
                sentences.append(g); script.append({"sentence_id": sid, "text": s["text"]})
            cases.append({"case_id": f"{spec['doc_id']}/{cs['id']}", "doc_id": spec["doc_id"], "page": cs["page"],
                          "selection": [{"text": t, "rect": r} for r, t in sel], "script": script})
    gt = {"schema": "speakeasy-bench-gt/1", "doc_id": spec["doc_id"], "subset": "real", "gt_source": "agent",
          "gt_method": "pdfplumber " + pdfplumber.__version__ + " word boxes grouped into lines; agent-assigned block membership and figure/table "
                       "regions on rendered pages, snapped by gen/snap_gt.py (union of member lines / of graphic objects and chars in region); "
                       "parts snapped to the smallest graphic object holding the label",
          "reviewed_by_human": bool(spec.get("reviewed_by_human", False)), "license": spec["license"], "pages": pages,
          "sentences": sentences, "parts": parts, "features": spec.get("features", {}),
          "source": {k: spec[k] for k in ("arxiv", "title", "license_url") if k in spec}}
    with open(os.path.join(out, "gt.json"), "w", encoding="utf-8") as f: json.dump(gt, f, indent=1); f.write("\n")
    with open(os.path.join(out, "cases.json"), "w", encoding="utf-8") as f: json.dump(cases, f, indent=1); f.write("\n")
    return gt, cases


def main(argv):
    if len(argv) == 3 and argv[0] in ("lines", "objects"):
        import pdfplumber
        with pdfplumber.open(argv[1]) as pdf:
            pg = pdf.pages[int(argv[2]) - 1]
            if argv[0] == "lines":
                for i, (r, t) in enumerate(raw_lines(pg)): print(i, r, t)
            else:
                for k, r in objects(pg): print(k, r)
        return
    if len(argv) == 3 and argv[0] == "build":
        gt, cases = build(argv[1], argv[2]); print(json.dumps({"doc_id": gt["doc_id"], "cases": len(cases), "parts": len(gt["parts"])})); return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
