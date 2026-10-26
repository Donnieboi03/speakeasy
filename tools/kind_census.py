#!/usr/bin/env python3
"""Uncaptioned and image-table census (docs/contracts/phase6-block-classifier.md 12.1 item 2, 12.6; verifier-owned, read-only).

  <doc venv python> tools/kind_census.py --set public|hidden [--family default|gap] [--subset real|all] [--layout-model PATH]
                                         [--holdout DIR] [--per-doc]

Runs the PP-DocLayout-S detector over every page of the set's real PDFs (--subset all: every fixture PDF, as in the contract's
section 0 measurement) and prints totals only:
  pages, no_text_pages, body boxes by class (image / chart / table), unpaired body boxes (no standard caption near them) and how many
  of those overlap a captioned body (panels the Phase 4 chaining takes), title boxes (figure_title / table_title / chart_title) whose
  text is not a standard caption (and how many of those are lexicon captions), lexicon-only caption lines (tests/audit/bench/
  caption_lexicon.py, not CAP_RE / TAB_RE), and image tables (section 8: a table box with < 2 text lines inside and a raster image over
  >= 50 % of it).
--per-doc adds one line per document and is refused for the hidden set: no per-doc or per-page line of a hidden set ever reaches the
terminal. "Standard caption" is today's docmap CAP_RE / TAB_RE. The detector, page and caption-regex APIs are the app's (src/
layoutdet.py, src/pdfpage.py, src/docmap.py), imported read-only with SPEAKEASY_HOME in a temp dir and no bytecode written; a missing
API fails with exit 2 instead of a partial count. Nothing under ~/.speakeasy is read or written."""
import argparse, collections, glob, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE); BENCH = os.path.join(REPO, "tests", "audit", "bench")
sys.dont_write_bytecode = True
sys.path.insert(0, BENCH)
import caption_lexicon as LEX
from validate_fixtures import set_paths

KEYS = ("pages", "no_text_pages", "body_image", "body_chart", "body_table", "unpaired_image", "unpaired_chart", "unpaired_table",
        "unpaired_in_captioned_body", "title_boxes", "title_not_standard", "title_lexicon", "lexicon_only_lines", "image_tables")


def area(r): return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
def inter(a, b): return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
def centre_in(r, b, s=2.0): cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2; return b[0] - s <= cx <= b[2] + s and b[1] - s <= cy <= b[3] + s


def near_caption(bb, caps, lh=12.0):
    """A standard caption line within 9 line heights above or below the body, overlapping it horizontally (the throwaway census rule)."""
    for cb, _ in caps:
        hov = min(cb[2], bb[2]) - max(cb[0], bb[0]) > 0.3 * min(cb[2] - cb[0], bb[2] - bb[0])
        if hov and (abs(cb[1] - bb[3]) < 9 * lh or abs(bb[1] - cb[3]) < 9 * lh or (cb[1] < bb[3] and bb[1] < cb[3])): return True
    return False


def census_pdf(det, pdfpage, std_caption, pdf):
    c = collections.Counter(); d = pdfpage.open_pdf(pdf)
    for page in d:
        W, H = page.rect.width, page.rect.height; lines, _ = page.text_lines(); c["pages"] += 1
        if not lines: c["no_text_pages"] += 1
        caps = [(bb, s) for bb, s in lines if std_caption(s)]
        imgs = [r for r in page.images() if area(r) > 400]
        boxes = det._detect(page, W, H)
        bodies = [b for b in boxes if b.get("role") in ("figure", "table")]
        captioned = [b["bbox"] for b in bodies if near_caption(b["bbox"], caps)]
        for b in bodies:
            bb = b["bbox"]; c["body_" + b["cls"]] += 1
            if not near_caption(bb, caps):
                c["unpaired_" + b["cls"]] += 1
                c["unpaired_in_captioned_body"] += any(inter(bb, o) >= 0.3 * area(bb) for o in captioned if o is not bb)
            if b["cls"] == "table":
                inside = sum(1 for lb, _ in lines if centre_in(lb, bb, 0))
                on_img = any(inter(r, bb) >= 0.5 * area(bb) for r in imgs)
                c["image_tables"] += on_img and inside < 2
        for b in boxes:
            if b.get("cls") not in ("figure_title", "table_title", "chart_title"): continue
            c["title_boxes"] += 1; bb = b["bbox"]
            txt = " ".join(s for lb, s in lines if centre_in(lb, bb))
            if not std_caption(txt):
                c["title_not_standard"] += 1; c["title_lexicon"] += LEX.match(txt) is not None
        for _, s in lines:
            if LEX.match(s) and not std_caption(s): c["lexicon_only_lines"] += 1
    return c


def main():
    ap = argparse.ArgumentParser(description="Phase 6 census (aggregates only)")
    ap.add_argument("--set", required=True, choices=("public", "hidden")); ap.add_argument("--family", default="default", choices=("default", "gap"))
    ap.add_argument("--layout-model", default=os.environ.get("SPEAKEASY_LAYOUT_MODEL") or "/tmp/pp_doclayout_s.onnx")
    ap.add_argument("--holdout", default=os.environ.get("SPEAKEASY_HOLDOUT") or os.path.expanduser("~/speakeasy-holdout/bench"))
    ap.add_argument("--per-doc", action="store_true"); ap.add_argument("--subset", default="real", choices=("real", "all"))
    ap.add_argument("--app-repo", default=REPO, help="app checkout whose detector / page / caption-regex code is used (default: this tree)")
    a = ap.parse_args()
    if a.per_doc and a.set == "hidden": print("census: --per-doc is refused for the hidden set (aggregates only)", file=sys.stderr); sys.exit(2)
    if not os.path.isfile(a.layout_model): print(f"census: layout model not found: {a.layout_model}", file=sys.stderr); sys.exit(2)
    fx, man = set_paths(a.set, BENCH, os.path.abspath(os.path.expanduser(a.holdout)), a.family)
    if not os.path.isfile(man): print(f"census: no manifest for {a.set} ({a.family})", file=sys.stderr); sys.exit(2)
    docs = [d for d in json.load(open(man))["docs"] if a.subset == "all" or d["subset"] == "real"]
    home = tempfile.mkdtemp(prefix="se_census_")
    try:
        os.environ.update(SPEAKEASY_LAYOUT_MODEL=os.path.abspath(a.layout_model), SPEAKEASY_HOME=home)
        sys.path.insert(0, os.path.join(os.path.abspath(os.path.expanduser(a.app_repo)), "src"))
        try:
            import layoutdet, pdfpage
            from docmap import CAP_RE, TAB_RE
            det = layoutdet.Detector(); _ = (det._detect, pdfpage.open_pdf)
        except (ImportError, AttributeError) as ex:
            print(f"census: app API not available ({type(ex).__name__}: {ex}); the census needs layoutdet.Detector()._detect, pdfpage.open_pdf "
                  "and docmap.CAP_RE / TAB_RE", file=sys.stderr); sys.exit(2)
        if not det.ready(): print(f"census: detector not ready ({det.reason})", file=sys.stderr); sys.exit(2)
        std = lambda s: bool(CAP_RE.match(s) or TAB_RE.match(s))
        tot = collections.Counter()
        for d in sorted(docs, key=lambda x: x["doc_id"]):
            c = census_pdf(det, pdfpage, std, os.path.join(fx, d["doc_id"], "doc.pdf")); tot.update(c)
            if a.per_doc: print(d["doc_id"], json.dumps({k: c.get(k, 0) for k in KEYS}))
        print(f"census {a.set} {a.family} {a.subset} docs {len(docs)} " + json.dumps({k: tot.get(k, 0) for k in KEYS}))
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
