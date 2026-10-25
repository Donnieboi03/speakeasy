"""Document-aware focus: read the source PDF / Markdown file itself (read-only, local) to get exact
line, figure, caption and panel positions.

Two halves:
  * parse_pdf()  needs PyMuPDF (installed in the Kokoro venv); speakeasy runs it as a subprocess:
        <venv>/bin/python docmap.py parse-pdf FILE OUT.json
    The parse is cached by (path, size, mtime) under ~/.speakeasy/cache/doc/.
  * everything else is stdlib, imported by speakeasy.py (resolution, Markdown, page matching, figure refs, transforms).
Paths are never logged; only a short hash.
"""
import hashlib, json, os, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request

# captions need a separator after the number ("Figure 3:", "Fig. 3.", "Table 2 |"), so prose like "Table 2 summarizes" is not one
CAP_RE = re.compile(r"^\s*(?:fig(?:ure)?s?\.?)\s*(\d+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(.*)$", re.I)
TAB_RE = re.compile(r"^\s*table\s*(\d+)\s*(?:[.:|\u2013\u2014]|\s-\s|$)\s*(.*)$", re.I)
PANEL_RE = re.compile(r"^\s*\(?([a-hA-H])\)\s*$|^\s*\(([a-h])\)\s+\S")
REF_RE = re.compile(r"\b(fig(?:ure)?s?\.?|table)\s*(\d+)\s*(?:\(?([a-h])\)?(?![a-z]))?", re.I)
PANEL_REF_RE = re.compile(r"\b(?:panel|subfigure|part)\s*\(?([a-h])\)?(?![a-z])", re.I)
PAGE_OF_RE = re.compile(r"(?:page\s*)?(\d{1,4})\s*(?:of|/)\s*(\d{1,4})\b", re.I)

def path_hash(p):
    return hashlib.sha256((p or "").encode()).hexdigest()[:12] if p else None

def cache_dir():
    d = os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "cache", "doc")
    os.makedirs(d, exist_ok=True)
    return d

def _key(path):
    st = os.stat(path)
    return hashlib.sha256(f"{os.path.realpath(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:20]

def _toks(s):
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1]

# ---------------- PDF (PyMuPDF; runs in the venv) ----------------
def _union(rs):
    if not rs: return None
    return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]

def _panels(fig, labels):
    """Split a figure box at its (a)/(b)/... labels: rows by label y, columns by label x."""
    if len(labels) < 2: return []
    out = []
    for lb in labels:
        x0, y0 = lb["r"][0], lb["r"][1]
        same_row = [o for o in labels if o is not lb and abs(o["r"][1] - y0) < 12]
        right = [o["r"][0] for o in same_row if o["r"][0] > x0 + 5]
        below = [o["r"][1] for o in labels if o["r"][1] > y0 + 12 and o["r"][0] < x0 + (min(right) - x0 if right else 1e9) - 5]
        x1 = min(right) - 2 if right else fig[2]
        y1 = min(below) - 2 if below else fig[3]
        out.append({"label": lb["l"], "bbox": [round(x0 - 2, 1), round(y0 - 2, 1), round(x1, 1), round(y1, 1)]})
    return out

def parse_pdf(path):
    import pymupdf
    doc = pymupdf.open(path)
    pages = []
    for pno, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        d = page.get_text("dict")
        lines, blocks = [], []
        for b in d.get("blocks", []):
            if b.get("type") != 0: continue
            bl = []
            for l in b.get("lines", []):
                t = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
                if t:
                    r = [round(v, 1) for v in l["bbox"]]
                    lines.append([*r, t]); bl.append(len(lines) - 1)
            if bl: blocks.append(bl)
        gfx = []
        try:
            for im in page.get_image_info():
                r = im.get("bbox")
                if r and (r[2] - r[0]) > 20 and (r[3] - r[1]) > 20: gfx.append(list(r))
        except Exception: pass
        try:
            for r in page.cluster_drawings():
                if r.width > 20 and r.height > 20 and not (r.width > 0.9 * W and r.height > 0.9 * H): gfx.append([r.x0, r.y0, r.x1, r.y1])
        except Exception: pass
        figs = []
        for bl in blocks:
            first = lines[bl[0]]
            m = CAP_RE.match(first[4]); kind = "figure"
            if not m: m = TAB_RE.match(first[4]); kind = "table"
            if not m: continue
            cap = [round(v, 1) for v in _union([lines[i][:4] for i in bl])]
            text = " ".join(lines[i][4] for i in bl)
            # figure: graphics directly above the caption (chain upward); table: content below
            # (graphics that run into the caption are clipped at its top)
            cand = sorted([[g[0], g[1], g[2], min(g[3], cap[1] - 2)] for g in gfx if g[1] < cap[1] - 20 and g[3] >= cap[1] - 0.7 * H
                           and not (g[3] > cap[3] + 20)], key=lambda g: -g[3]) if kind == "figure" \
                else sorted([g for g in gfx if g[1] >= cap[3] - 8 and g[1] <= cap[3] + 0.7 * H], key=lambda g: g[1])
            body = None
            for g in cand:
                if body is None:
                    gap = cap[1] - g[3] if kind == "figure" else g[1] - cap[3]
                    if gap < 60: body = list(g)
                elif (body[1] - g[3] < 40) if kind == "figure" else (g[1] - body[3] < 40):
                    body = _union([body, g])
            if body is None and kind == "figure":            # no vector/image found: the text-free gap above the caption
                above = [l for l in lines if l[3] <= cap[1] - 2 and l[0] < cap[2] and l[2] > cap[0]]
                top = max([l[3] for l in above if cap[1] - l[3] > 30] or [None]) if above else None
                if top is not None and cap[1] - top > 40: body = [cap[0], top + 2, cap[2], cap[1] - 2]
            fb = [round(v, 1) for v in (body or cap)]
            labels = [] if kind == "table" else [{"l": (PANEL_RE.match(l[4]).group(1) or PANEL_RE.match(l[4]).group(2)).lower(), "r": l[:4]}
                      for l in lines if PANEL_RE.match(l[4]) and len(l[4]) <= 40 and body
                      and l[0] >= fb[0] - 6 and l[2] <= fb[2] + 6 and l[1] >= fb[1] - 6 and l[3] <= fb[3] + 6]
            seen = set(); labels = [x for x in labels if not (x["l"] in seen or seen.add(x["l"]))]
            figs.append({"label": f"{'Figure' if kind == 'figure' else 'Table'} {m.group(1)}", "kind": kind, "n": int(m.group(1)),
                         "page": pno + 1, "bbox": fb, "caption_bbox": cap, "caption": text[:300],
                         "panels": _panels(fb, labels)})
        pages.append({"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": figs})
    return {"kind": "pdf", "pages": pages, "n_pages": len(pages)}

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

def load(path, timeout=20):
    """Parsed document (cached). Returns (doc, cache_state)."""
    kind = "pdf" if path.lower().endswith(".pdf") else "md" if re.search(r"\.(md|markdown|mdx)$", path, re.I) else None
    if not kind: return None, "unsupported"
    c = os.path.join(cache_dir(), _key(path) + ".json")
    if os.path.exists(c):
        try: return json.load(open(c)), "hit"
        except ValueError: pass
    if kind == "md":
        doc = parse_md(path)
    else:
        py = venv_python()
        if not py: return None, "no-pymupdf"
        tmp = c + f".{os.getpid()}.tmp"
        r = subprocess.run([py, os.path.realpath(__file__), "parse-pdf", path, tmp], capture_output=True, timeout=timeout)
        if r.returncode != 0 or not os.path.exists(tmp):
            return None, "parse-failed: " + r.stderr.decode(errors="replace").strip().splitlines()[-1][:120] if r.stderr else "parse-failed"
        os.replace(tmp, c)
        return json.load(open(c)), "miss"
    json.dump(doc, open(c, "w"))
    return doc, "miss"

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
            return "browser", u, "url"
        return None, None, "browser: not a public pdf" if mode == "all" else "browser: web off (SPEAKEASY_DOC_READ=local)"
    return None, None, "no document"

def fetch_pdf(url, tmpdir, max_mb=40, timeout=8):
    """Public PDFs only: no cookies, no auth, refuse redirects to other hosts or non-PDF answers."""
    for f in os.listdir(tmpdir):                     # clean our own old downloads (>1 day)
        if f.startswith("speakeasy_doc_") and time.time() - os.path.getmtime(os.path.join(tmpdir, f)) > 86400:
            try: os.remove(os.path.join(tmpdir, f))
            except OSError: pass
    dst = os.path.join(tmpdir, "speakeasy_doc_" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".pdf")
    if os.path.exists(dst): return dst
    class NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            a, b = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
            if b.scheme != "https" or b.netloc != a.netloc or re.search(r"log ?in|sign ?in|auth|sso|account", b.path, re.I): return None
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

def figure_context(figs, limit=8):
    out = []
    for f in figs[:limit]:
        if not f.get("label"): continue
        pn = ", ".join(p["label"] for p in f.get("panels") or [])
        cap = re.sub(r"\s+", " ", f.get("caption") or "")
        cap = re.sub(r"^\s*(?:fig(?:ure)?s?\.?|table)\s*\d+\s*[.:|\-\u2013\u2014]?\s*", "", cap, flags=re.I)
        out.append(f"{f['label']}" + (f" (panels {pn})" if pn else "") + ": " + cap[:160])
    return out

def find_ref(sentence, figs, last=None):
    """('Figure 4', 'b') mentioned in a spoken sentence, or a bare 'panel b' of the last figure."""
    for m in REF_RE.finditer(sentence or ""):
        kind = "Table" if m.group(1).lower().startswith("tab") else "Figure"
        f = next((f for f in figs if f.get("label") == f"{kind} {m.group(2)}"), None)
        if f: return f, (m.group(3) or "").lower() or None
    m = PANEL_REF_RE.search(sentence or "")
    if m and last: return last, m.group(1).lower()
    return None, None

def target_rect(f, panel):
    if panel:
        p = next((p for p in f.get("panels") or [] if p["label"] == panel), None)
        if p: return p["bbox"], "panel"
    return f.get("bbox"), "figure"

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

def apply(t, r):
    s, ox, oy = t[:3]
    return [s * r[0] + ox, s * r[1] + oy, s * (r[2] - r[0]), s * (r[3] - r[1])]

if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "parse-pdf":
        json.dump(parse_pdf(sys.argv[2]), open(sys.argv[3], "w")); sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "dump":
        print(json.dumps(parse_pdf(sys.argv[2]) if sys.argv[2].lower().endswith(".pdf") else parse_md(sys.argv[2]), indent=1)[:6000]); sys.exit(0)
    print("usage: docmap.py parse-pdf FILE OUT.json | dump FILE", file=sys.stderr); sys.exit(2)
