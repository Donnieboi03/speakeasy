#!/usr/bin/env python3
"""Fixture validator (contract section 4.5). Exit 0 when valid, 2 on the first set with errors (every error is printed as
`<file>: <json path>: <rule>`).

  validate_fixtures.py [--set public|hidden|all] [--family default|gap] [--holdout DIR]

--family gap validates the Phase 6 gap family (fixtures/public-gap, <holdout>/gap) with validate_gap_set; the cross-set check
then also compares each gap set with the other family's opposite set."""
import argparse, hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from score import SCORED_KINDS, PART_STYLES, text_hash

KINDS = set(SCORED_KINDS) | {"heading", "header", "footer", "page_number", "equation", "other"}
REF = re.compile(r"\b(Fig\.|Figure|Table)\s*\d")
BAD = re.compile(r"[\[\]<>{}]")
MIN = {"synthetic_docs": 6, "real_docs": 3, "real_twocol": 2, "real_multipanel": 1, "cases": 40, "lines_syn": 60, "lines_real": 30,
       "parts_syn": 30, "callouts_syn": 8, "parts_real": 10, "blocks_syn": 60, "blocks_syn_kind": 8, "blocks_real": 40}
SYN_LAYOUTS = {
    "multi-page single column (>= 3 pages, headings, footers)": lambda f: f.get("pages", 0) >= 3 and f.get("columns") == 1 and f.get("headings", 0) >= 1 and f.get("footers"),
    "two-column with >= 10 hyphen breaks, >= 10 ligatures and a paragraph continuing across the column break":
        lambda f: f.get("columns") == 2 and f.get("hyphen_breaks", 0) >= 10 and f.get("ligatures", 0) >= 10 and f.get("col_continuation"),
    "(a)/(b) panel figure with >= 4 named parts per panel and >= 3 callouts": lambda f: f.get("panel_figure"),
    "table directly above a figure plus a standalone ruled table": lambda f: f.get("table_above_figure") and f.get("standalone_table"),
    "full-width figure on a two-column page": lambda f: f.get("fullwidth_figure"),
    "panels named only in the caption plus one raster part": lambda f: f.get("caption_named_panels") and f.get("raster_part"),
}


def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()


def set_paths(set_name, bench_dir, holdout, family="default"):
    if family == "gap":
        if set_name == "public": return os.path.join(bench_dir, "fixtures", "public-gap"), os.path.join(bench_dir, "fixtures", "public-gap", "manifest.json")
        return os.path.join(holdout, "gap", "fixtures"), os.path.join(holdout, "gap", "manifest.json")
    if set_name == "public": return os.path.join(bench_dir, "fixtures", "public"), os.path.join(bench_dir, "fixtures", "public", "manifest.json")
    return os.path.join(holdout, "fixtures"), os.path.join(holdout, "manifest.json")


def _rect_ok(r, w, h, tol=0.6):
    return (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r) and r[0] < r[2] and r[1] < r[3]
            and r[0] >= -tol and r[1] >= -tol and r[2] <= w + tol and r[3] <= h + tol)


def validate_set(set_name, fx, man_path):
    """Returns (errors, info). info: {docs: [(doc_id, subset)], manifest_sha256, pdf_sha: {doc_id: sha}, cases, counts}."""
    E = []; info = {"docs": [], "pdf_sha": {}, "cases": 0}
    if not os.path.exists(man_path): return [f"missing fixture: {set_name}/manifest.json"], info
    try: man = json.load(open(man_path))
    except ValueError as e: return [f"{man_path}: $: not valid JSON ({e})"], info
    if man.get("schema") != "speakeasy-bench-manifest/1" or not isinstance(man.get("docs"), list): return [f"{man_path}: $.schema: not a speakeasy-bench-manifest/1"], info
    body = json.dumps(man["docs"], sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(body.encode()).hexdigest() != man.get("manifest_sha256"): E.append(f"{man_path}: $.manifest_sha256: does not match docs")
    info["manifest_sha256"] = man.get("manifest_sha256")
    for i, d in enumerate(man["docs"]):
        for f in ("doc.pdf", "gt.json", "cases.json"):
            p = os.path.join(fx, d["doc_id"], f)
            if not os.path.exists(p): E.append(f"missing fixture: {set_name}/{d['doc_id']}/{f}"); continue
            if sha(p) != d["sha256"].get(f): E.append(f"fixture changed: {set_name}/{d['doc_id']}/{f}; regenerate the manifest and re-record baselines")
    if E: return E, info
    c = dict(lines_syn=0, lines_real=0, parts_syn=0, callouts_syn=0, parts_real=0, blocks_syn=0, blocks_real=0, real_twocol=0, real_multipanel=0)
    kinds_syn = {k: 0 for k in SCORED_KINDS}; layouts = {k: False for k in SYN_LAYOUTS}; n_syn = n_real = 0
    for d in man["docs"]:
        did = d["doc_id"]; gp = os.path.join(fx, did, "gt.json"); cp = os.path.join(fx, did, "cases.json")
        info["pdf_sha"][did] = d["sha256"]["doc.pdf"]; info["docs"].append((did, d["subset"]))
        try: gt = json.load(open(gp)); cases = json.load(open(cp))
        except ValueError as e: E.append(f"{gp}: $: not valid JSON ({e})"); continue
        for k, t in (("schema", str), ("doc_id", str), ("subset", str), ("gt_source", str), ("gt_method", str), ("reviewed_by_human", bool),
                     ("license", str), ("pages", list), ("sentences", list), ("parts", list)):
            if not isinstance(gt.get(k), t): E.append(f"{gp}: $.{k}: missing or not {t.__name__}")
        if E: continue
        if gt["schema"] != "speakeasy-bench-gt/1": E.append(f"{gp}: $.schema: not speakeasy-bench-gt/1")
        if gt["doc_id"] != did: E.append(f"{gp}: $.doc_id: differs from manifest ({did})")
        if gt["subset"] not in ("synthetic", "real") or gt["subset"] != d["subset"]: E.append(f"{gp}: $.subset: must be synthetic|real and match the manifest")
        if gt["gt_source"] not in ("generator", "agent"): E.append(f"{gp}: $.gt_source: must be generator|agent")
        if gt["license"] not in ("synthetic", "CC-BY-4.0", "CC0-1.0"): E.append(f"{gp}: $.license: must be synthetic|CC-BY-4.0|CC0-1.0")
        syn = gt["subset"] == "synthetic"; n_syn += syn; n_real += not syn
        pages = {}
        for pi, pg in enumerate(gt["pages"]):
            P = f"$.pages[{pi}]"
            if not all(isinstance(pg.get(k), t) for k, t in (("n", int), ("w", (int, float)), ("h", (int, float)), ("blocks_scored", bool), ("lines", list), ("blocks", list))):
                E.append(f"{gp}: {P}: needs n, w, h, blocks_scored, lines, blocks"); continue
            pages[pg["n"]] = pg; w, h = pg["w"], pg["h"]
            for li, r in enumerate(pg["lines"]):
                if not _rect_ok(r, w, h): E.append(f"{gp}: {P}.lines[{li}]: bbox must be inside the page with x0 < x1 and y0 < y1")
            for bi, b in enumerate(pg["blocks"]):
                if not isinstance(b.get("id"), str) or b.get("kind") not in KINDS: E.append(f"{gp}: {P}.blocks[{bi}]: needs id and a known kind")
                if not _rect_ok(b.get("bbox"), w, h): E.append(f"{gp}: {P}.blocks[{bi}].bbox: bbox must be inside the page with x0 < x1 and y0 < y1")
                if pg["blocks_scored"] and b.get("kind") in SCORED_KINDS:
                    if syn: c["blocks_syn"] += 1; kinds_syn[b["kind"]] += 1
                    else: c["blocks_real"] += 1
        parts = {}
        for i, p in enumerate(gt["parts"]):
            P = f"$.parts[{i}]"
            if not isinstance(p.get("id"), str) or not isinstance(p.get("name"), str) or p.get("style") not in PART_STYLES or not isinstance(p.get("snapped"), bool):
                E.append(f"{gp}: {P}: needs id, name, style in {PART_STYLES}, snapped"); continue
            if p.get("page") not in pages: E.append(f"{gp}: {P}.page: not a GT page"); continue
            if not _rect_ok(p.get("bbox"), pages[p["page"]]["w"], pages[p["page"]]["h"]): E.append(f"{gp}: {P}.bbox: bbox must be inside the page with x0 < x1 and y0 < y1")
            parts[p["id"]] = p
        sents = {}
        for i, s in enumerate(gt["sentences"]):
            P = f"$.sentences[{i}]"
            if not isinstance(s.get("id"), str) or not re.fullmatch(r"[0-9a-f]{12}", str(s.get("text_hash"))) or s.get("score") not in ("lines", "parts", "none") or not isinstance(s.get("line_rects"), list):
                E.append(f"{gp}: {P}: needs id, text_hash (12 hex), score lines|parts|none, line_rects"); continue
            if s.get("page") not in pages: E.append(f"{gp}: {P}.page: not a GT page"); continue
            for k, r in enumerate(s["line_rects"]):
                if r not in pages[s["page"]]["lines"]: E.append(f"{gp}: {P}.line_rects[{k}]: not an entry of pages[{s['page']}].lines")
            if s["score"] == "lines" and not s["line_rects"]: E.append(f"{gp}: {P}.line_rects: a lines-scored sentence needs line rects")
            if s["score"] == "parts":
                if s.get("part") not in parts: E.append(f"{gp}: {P}.part: parts-scored sentence needs a known part")
                elif parts[s["part"]]["page"] != s["page"]: E.append(f"{gp}: {P}.part: part is not on the sentence's page")
            sents[s["id"]] = s
        if not isinstance(cases, list): E.append(f"{cp}: $: must be a list"); continue
        for ci, cs in enumerate(cases):
            P = f"$[{ci}]"; info["cases"] += 1
            if not isinstance(cs.get("case_id"), str) or cs.get("doc_id") != did or cs.get("page") not in pages or not cs.get("selection") or not cs.get("script"):
                E.append(f"{cp}: {P}: needs case_id, doc_id {did}, a GT page, selection and script"); continue
            for k, it in enumerate(cs["selection"]):
                if not isinstance(it.get("text"), str) or it.get("rect") not in pages[cs["page"]]["lines"]:
                    E.append(f"{cp}: {P}.selection[{k}]: needs text and a rect that is an entry of pages[{cs['page']}].lines")
            for k, it in enumerate(cs["script"]):
                Q = f"{P}.script[{k}]"; t = it.get("text") or ""; s = sents.get(it.get("sentence_id"))
                if s is None: E.append(f"{cp}: {Q}.sentence_id: not in gt.json sentences"); continue
                if text_hash(t) != s["text_hash"]: E.append(f"{cp}: {Q}.text: does not match its text_hash")
                if not t.rstrip().endswith((".", "!", "?")): E.append(f"{cp}: {Q}.text: must end in . ! or ?")
                if BAD.search(t): E.append(f"{cp}: {Q}.text: must not contain [ ] < > {{ }}")
                if s["score"] == "lines":
                    if REF.search(t): E.append(f"{cp}: {Q}.text: lines-scored sentence contains a figure/table reference")
                    if syn: c["lines_syn"] += 1
                    else: c["lines_real"] += 1
                if s["score"] == "parts" and s.get("part") in parts:
                    if syn:
                        c["parts_syn"] += 1; c["callouts_syn"] += parts[s["part"]]["style"] == "callout"
                    else: c["parts_real"] += 1
        f = gt.get("features") or {}
        if syn:
            for k, fn in SYN_LAYOUTS.items(): layouts[k] |= bool(fn(f))
        else:
            c["real_twocol"] += f.get("columns") == 2; c["real_multipanel"] += bool(f.get("multi_panel"))
    cov = [("synthetic_docs", n_syn), ("real_docs", n_real), ("cases", info["cases"])] + [(k, v) for k, v in c.items()]
    for k, v in cov:
        if v < MIN[k]: E.append(f"{man_path}: $: coverage minimum {k} >= {MIN[k]} not met ({v})")
    for k, v in kinds_syn.items():
        if v < MIN["blocks_syn_kind"]: E.append(f"{man_path}: $: coverage minimum synthetic {k} blocks >= {MIN['blocks_syn_kind']} not met ({v})")
    for k, ok in layouts.items():
        if not ok: E.append(f"{man_path}: $: coverage: no synthetic doc with {k}")
    info["counts"] = dict(c, synthetic_docs=n_syn, real_docs=n_real, cases=info["cases"], **{f"blocks_syn_{k}": v for k, v in kinds_syn.items()})
    return E, info


# ---------------------------------------------------------------- gap family (phase6-block-classifier.md 12.2)
GAP_KINDS = KINDS | {"decoration", "text-image"}
GAP_VALUES = ("uncaptioned", "lexicon", "no-text", "image-table", "standard")
DECOY_TYPES = ("logo", "icon", "band", "textbox", "text-image", "background")
GAP_MIN = {"slide_pages": 4, "slide_uncaptioned_figures": 8, "slide_uncaptioned_tables": 3, "report_pages_uncaptioned_between_paragraphs": 2,
           "lexicon_captions": 10, "lexicon_nouns": 6, "prose_decoys": 4, "polluted_captions": 1, "scan_pages": 2, "scan_figures": 2,
           "scan_tables": 1, "area_cases": 2, "image_tables": 3, "row_cases": 3, "decoys": 12, "real_docs": 2, "kind_regions": 40}
GAP_STYLES_REQUIRED = {"exhibit on a figure": lambda b: b["kind"] == "figure" and b.get("caption_style") == "exhibit",
                       "exhibit on a table": lambda b: b["kind"] == "table" and b.get("caption_style") == "exhibit",
                       "Roman TABLE numeral": lambda b: b["kind"] == "table" and str(b.get("caption_style", "")).endswith("-roman"),
                       "Fig. S<n>": lambda b: b.get("caption_style") == "fig-s", "Abb.": lambda b: b.get("caption_style") == "abb",
                       "Tabelle": lambda b: str(b.get("caption_style", "")).startswith("tabelle"),
                       "Tabla / Tableau": lambda b: b.get("caption_style") in ("tabla", "tableau"), "Chart": lambda b: b.get("caption_style") == "chart"}


def _is_slide(pg): return abs(pg["w"] - 720) < 0.6 and abs(pg["h"] - 405) < 0.6


def _vgap(a, b): return min(a[2], b[2]) - max(a[0], b[0]) > 0


def pdf_text_pages(pdf_path):
    """{page_n: number of text chars} via pypdfium2, or None when it is not importable."""
    try: import pypdfium2 as pdfium
    except Exception: return None
    d = pdfium.PdfDocument(pdf_path)
    try: return {i + 1: len(d[i].get_textpage().get_text_range().strip()) for i in range(len(d))}
    finally: d.close()


def validate_gap_set(set_name, fx, man_path):
    """Gap-family validation: schema, hashes (every file the manifest lists), GT flags, case shapes, the 12.2 coverage minima and
    no text layer on scanned (G3) pages. Returns (errors, info) like validate_set."""
    E = []; info = {"docs": [], "pdf_sha": {}, "cases": 0}
    if not os.path.exists(man_path): return [f"missing fixture: {set_name}/manifest.json"], info
    try: man = json.load(open(man_path))
    except ValueError as e: return [f"{man_path}: $: not valid JSON ({e})"], info
    if man.get("schema") != "speakeasy-bench-manifest/1" or not isinstance(man.get("docs"), list): return [f"{man_path}: $.schema: not a speakeasy-bench-manifest/1"], info
    body = json.dumps(man["docs"], sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(body.encode()).hexdigest() != man.get("manifest_sha256"): E.append(f"{man_path}: $.manifest_sha256: does not match docs")
    info["manifest_sha256"] = man.get("manifest_sha256")
    for d in man["docs"]:
        for f in sorted(set(d["sha256"]) | {"doc.pdf", "gt.json", "cases.json"}):
            p = os.path.join(fx, d["doc_id"], f)
            if not os.path.exists(p): E.append(f"missing fixture: {set_name}/{d['doc_id']}/{f}"); continue
            if sha(p) != d["sha256"].get(f): E.append(f"fixture changed: {set_name}/{d['doc_id']}/{f}; regenerate the manifest and re-record baselines")
    if E: return E, info
    c = {k: 0 for k in GAP_MIN}; nouns = set(); styles = {k: False for k in GAP_STYLES_REQUIRED}; decoy_types = set(); standard = 0
    for d in man["docs"]:
        did = d["doc_id"]; gp = os.path.join(fx, did, "gt.json"); cp = os.path.join(fx, did, "cases.json")
        info["pdf_sha"][did] = d["sha256"]["doc.pdf"]; info["docs"].append((did, d["subset"]))
        try: gt = json.load(open(gp)); cases = json.load(open(cp))
        except ValueError as e: E.append(f"{gp}: $: not valid JSON ({e})"); continue
        for k, t in (("schema", str), ("doc_id", str), ("subset", str), ("gt_source", str), ("reviewed_by_human", bool), ("license", str),
                     ("pages", list), ("sentences", list), ("areas", list)):
            if not isinstance(gt.get(k), t): E.append(f"{gp}: $.{k}: missing or not {t.__name__}")
        if E: continue
        if gt.get("family") != "gap": E.append(f"{gp}: $.family: must be gap")
        if gt["doc_id"] != did: E.append(f"{gp}: $.doc_id: differs from manifest ({did})")
        if gt["subset"] not in ("synthetic", "real") or gt["subset"] != d["subset"]: E.append(f"{gp}: $.subset: must be synthetic|real and match the manifest")
        if gt["license"] not in ("synthetic", "CC-BY-4.0", "CC0-1.0"): E.append(f"{gp}: $.license: must be synthetic|CC-BY-4.0|CC0-1.0")
        c["real_docs"] += gt["subset"] == "real"
        pages = {}; blocks_by_id = {}; scan_pages = []
        for pi, pg in enumerate(gt["pages"]):
            P = f"$.pages[{pi}]"
            if not all(isinstance(pg.get(k), t) for k, t in (("n", int), ("w", (int, float)), ("h", (int, float)), ("blocks_scored", bool), ("kind_scored", bool),
                                                              ("scan", bool), ("lines", list), ("blocks", list), ("prose_decoys", list))):
                E.append(f"{gp}: {P}: needs n, w, h, blocks_scored, kind_scored, scan, lines, blocks, prose_decoys"); continue
            pages[pg["n"]] = pg; w, h = pg["w"], pg["h"]
            if pg["scan"]: scan_pages.append(pg["n"]); c["scan_pages"] += 1
            if _is_slide(pg): c["slide_pages"] += 1
            for li, r in enumerate(pg["lines"] + pg["prose_decoys"]):
                if not _rect_ok(r, w, h): E.append(f"{gp}: {P}.lines/prose_decoys[{li}]: bbox must be inside the page with x0 < x1 and y0 < y1")
            c["prose_decoys"] += len(pg["prose_decoys"])
            paras = [b["bbox"] for b in pg["blocks"] if b.get("kind") == "paragraph"]
            between = False
            for bi, b in enumerate(pg["blocks"]):
                Q = f"{P}.blocks[{bi}]"
                if not isinstance(b.get("id"), str) or b.get("kind") not in GAP_KINDS: E.append(f"{gp}: {Q}: needs id and a known kind"); continue
                if not _rect_ok(b.get("bbox"), w, h): E.append(f"{gp}: {Q}.bbox: bbox must be inside the page with x0 < x1 and y0 < y1"); continue
                blocks_by_id[b["id"]] = (pg["n"], b)
                if b["kind"] in ("decoration", "text-image"):
                    if b.get("decoy") not in DECOY_TYPES: E.append(f"{gp}: {Q}.decoy: must be one of {DECOY_TYPES}")
                    c["decoys"] += 1; decoy_types.add(b.get("decoy"))
                if b["kind"] not in ("figure", "table") or not pg["kind_scored"]: continue
                if b.get("gap") not in GAP_VALUES or not isinstance(b.get("caption_style"), str) or not isinstance(b.get("raster"), bool):
                    E.append(f"{gp}: {Q}: a kind-scored figure/table needs gap in {GAP_VALUES}, caption_style and raster"); continue
                c["kind_regions"] += 1; standard += b["gap"] == "standard"
                for k, fn in GAP_STYLES_REQUIRED.items(): styles[k] |= bool(fn(b))
                if b["gap"] == "lexicon":
                    c["lexicon_captions"] += 1; nouns.add(b["caption_style"].split("-")[0]); c["polluted_captions"] += bool(b.get("polluted"))
                if b["gap"] == "uncaptioned" and _is_slide(pg):
                    c["slide_uncaptioned_figures" if b["kind"] == "figure" else "slide_uncaptioned_tables"] += 1
                if b["gap"] == "uncaptioned" and b["kind"] == "figure" and not _is_slide(pg) and not pg["scan"]:
                    bb = b["bbox"]
                    between |= any(p[3] <= bb[1] and _vgap(p, bb) for p in paras) and any(p[1] >= bb[3] and _vgap(p, bb) for p in paras)
                if pg["scan"]: c["scan_figures" if b["kind"] == "figure" else "scan_tables"] += 1
                if b["gap"] == "image-table":
                    if not pg["lines"]: E.append(f"{gp}: {Q}: an image table must sit on a text page")
                    rows = b.get("rows")
                    if not (isinstance(rows, list) and len(rows) >= 3 and all(_rect_ok(r, w, h) for r in rows)): E.append(f"{gp}: {Q}.rows: an image table needs >= 3 GT row rects")
                    c["image_tables"] += 1
            c["report_pages_uncaptioned_between_paragraphs"] += between
        if scan_pages:
            tp = pdf_text_pages(os.path.join(fx, did, "doc.pdf"))
            if tp is None: E.append(f"{fx}/{did}/doc.pdf: $: pypdfium2 is needed to check that scanned pages have no text layer")
            else:
                for n in scan_pages:
                    if tp.get(n): E.append(f"{fx}/{did}/doc.pdf: page {n}: a scanned (G3) page has a text layer ({tp[n]} chars)")
        areas = {}
        for ai, a in enumerate(gt["areas"]):
            P = f"$.areas[{ai}]"
            if not (isinstance(a.get("id"), str) and isinstance(a.get("w"), int) and isinstance(a.get("h"), int) and isinstance(a.get("blocks"), list)):
                E.append(f"{gp}: {P}: needs id, w, h (capture px), blocks"); continue
            areas[a["id"]] = a
            for bi, b in enumerate(a["blocks"]):
                if b.get("kind") not in GAP_KINDS or not _rect_ok(b.get("bbox"), a["w"], a["h"]): E.append(f"{gp}: {P}.blocks[{bi}]: needs a known kind and a bbox inside the capture"); continue
                if b["kind"] in ("figure", "table"): c["kind_regions"] += 1
                if b["kind"] in ("decoration", "text-image"): c["decoys"] += 1; decoy_types.add(b.get("decoy"))
        sents = {}
        for i, s in enumerate(gt["sentences"]):
            P = f"$.sentences[{i}]"
            if not isinstance(s.get("id"), str) or not re.fullmatch(r"[0-9a-f]{12}", str(s.get("text_hash"))) or s.get("score") not in ("lines", "none", "table_row") or not isinstance(s.get("line_rects"), list):
                E.append(f"{gp}: {P}: needs id, text_hash (12 hex), score lines|none|table_row, line_rects"); continue
            if s.get("page") is not None and s["page"] not in pages: E.append(f"{gp}: {P}.page: not a GT page"); continue
            if s["score"] == "lines":
                if s.get("page") is None or not s["line_rects"]: E.append(f"{gp}: {P}: a lines-scored sentence needs a page and line rects")
                else:
                    for k, r in enumerate(s["line_rects"]):
                        if r not in pages[s["page"]]["lines"]: E.append(f"{gp}: {P}.line_rects[{k}]: not an entry of pages[{s['page']}].lines")
            if s["score"] == "table_row":
                tb = blocks_by_id.get(s.get("table"))
                if not tb or tb[1]["kind"] != "table" or tb[0] != s.get("page") or not _rect_ok(s.get("row_rect"), pages[s["page"]]["w"], pages[s["page"]]["h"]):
                    E.append(f"{gp}: {P}: a table_row sentence needs a table block on its page and a row_rect")
            sents[s["id"]] = s
        if not isinstance(cases, list): E.append(f"{cp}: $: must be a list"); continue
        for ci, cs in enumerate(cases):
            P = f"$[{ci}]"; info["cases"] += 1
            if not isinstance(cs.get("case_id"), str) or cs.get("doc_id") != did or not cs.get("selection") or not cs.get("script"):
                E.append(f"{cp}: {P}: needs case_id, doc_id {did}, selection and script"); continue
            if cs.get("area") is not None:
                a = areas.get(cs["area"]); cap = cs.get("capture")
                if a is None or cap not in d["sha256"] or not (isinstance(cs.get("capture_rect"), list) and len(cs["capture_rect"]) == 4):
                    E.append(f"{cp}: {P}: an area case needs a GT area, a manifest-listed capture PNG and capture_rect"); continue
                if not all(isinstance(lb, dict) and isinstance(lb.get("rect"), list) and len(lb["rect"]) == 4 for lb in cs.get("line_bounds") or [None]):
                    E.append(f"{cp}: {P}.line_bounds: needs OCR line-bounds records with rect")
                c["area_cases"] += 1
            else:
                if cs.get("page") not in pages: E.append(f"{cp}: {P}.page: not a GT page"); continue
                for k, it in enumerate(cs["selection"]):
                    if not isinstance(it.get("text"), str) or it.get("rect") not in pages[cs["page"]]["lines"]:
                        E.append(f"{cp}: {P}.selection[{k}]: needs text and a rect that is an entry of pages[{cs['page']}].lines")
            rows = 0
            for k, it in enumerate(cs["script"]):
                Q = f"{P}.script[{k}]"; t = it.get("text") or ""; s = sents.get(it.get("sentence_id"))
                if s is None: E.append(f"{cp}: {Q}.sentence_id: not in gt.json sentences"); continue
                if text_hash(t) != s["text_hash"]: E.append(f"{cp}: {Q}.text: does not match its text_hash")
                if not t.rstrip().endswith((".", "!", "?")): E.append(f"{cp}: {Q}.text: must end in . ! or ?")
                if BAD.search(t): E.append(f"{cp}: {Q}.text: must not contain [ ] < > {{ }}")
                if s["score"] == "lines" and REF.search(t): E.append(f"{cp}: {Q}.text: lines-scored sentence contains a figure/table reference")
                rows += s["score"] == "table_row"
            if rows: c["row_cases"] += rows >= 3
    c["lexicon_nouns"] = len(nouns)
    for k, v in c.items():
        if v < GAP_MIN[k]: E.append(f"{man_path}: $: gap coverage minimum {k} >= {GAP_MIN[k]} not met ({v})")
    for k, ok in styles.items():
        if not ok: E.append(f"{man_path}: $: gap coverage: no lexicon caption with {k}")
    for t in DECOY_TYPES:
        if t not in decoy_types: E.append(f"{man_path}: $: gap coverage: no {t} decoy")
    if c["kind_regions"] and standard > 0.2 * c["kind_regions"]: E.append(f"{man_path}: $: standard-caption regions {standard} > 20 % of {c['kind_regions']}")
    info["counts"] = dict(c, standard_caption=standard, nouns=sorted(nouns), cases=info["cases"])
    return E, info


def cross_check(a, b):
    E = []
    for d in sorted(set(x for x, _ in a["docs"]) & set(x for x, _ in b["docs"])): E.append(f"manifest: $.docs: doc_id {d} in both sets")
    for s in sorted(set(a["pdf_sha"].values()) & set(b["pdf_sha"].values())): E.append(f"manifest: $.docs: PDF sha256 {s[:12]} in both sets")
    return E


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--set", default="all", choices=("public", "hidden", "all"))
    ap.add_argument("--holdout", default=os.environ.get("SPEAKEASY_HOLDOUT", os.path.expanduser("~/speakeasy-holdout/bench")))
    ap.add_argument("--family", default="default", choices=("default", "gap"))
    a = ap.parse_args(); infos = {}; bad = False
    check = validate_gap_set if a.family == "gap" else validate_set
    for s in (("public", "hidden") if a.set == "all" else (a.set,)):
        fx, man = set_paths(s, HERE, a.holdout, a.family)
        if s == "hidden" and not os.path.exists(man):
            if a.set == "all": print(f"validate: hidden set not found at {a.holdout}; public only", file=sys.stderr); continue
        E, info = check(s, fx, man); infos[s] = info
        for e in E: print(e if s == "public" else e.replace(a.holdout, "<holdout>"), file=sys.stderr)
        bad |= bool(E)
        print(f"{s}: {'INVALID' if E else 'ok'} {json.dumps(info.get('counts', {}), ensure_ascii=False) if s == 'public' else ''}".rstrip())
    if len(infos) == 2:
        E = cross_check(infos["public"], infos["hidden"])
        if a.family == "gap": E += family_cross_check(infos, a.holdout)
        for e in E: print(e, file=sys.stderr)
        bad |= bool(E)
    sys.exit(2 if bad else 0)


def family_cross_check(gap_infos, holdout):
    """Gap public vs default hidden and gap hidden vs default public (no PDF or doc_id on both sides of the split)."""
    E = []
    other = {s: validate_set(s, *set_paths(s, HERE, holdout))[1] for s in ("public", "hidden") if os.path.exists(set_paths(s, HERE, holdout)[1])}
    if "hidden" in other: E += cross_check(gap_infos["public"], other["hidden"])
    if "public" in other: E += cross_check(other["public"], gap_infos["hidden"])
    return E


if __name__ == "__main__":
    main()
