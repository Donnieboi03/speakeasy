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
PANEL_RE = re.compile(r"^\s*\(?([a-hA-H])\)\s*$|^\s*\(([a-h])\)\s+\S|^\s*([a-h])\s*$")
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

GENERIC = set("""a an the this that these those it its of in on at to for from with by and or as is are was were be been being
shows show shown showing depicts depicting illustrates displays display figure fig diagram image picture panel part left right top bottom
here there which where while what how also then next each both all some data result results overview setup scheme process involves
first second third finally using used use uses into onto over under between""".split())

def _text_lines(page):
    d = page.get_text("dict")
    lines, blocks = [], []
    for b in d.get("blocks", []):
        if b.get("type") != 0: continue
        bl = []
        for l in b.get("lines", []):
            t = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
            if t:
                lines.append([*[round(v, 1) for v in l["bbox"]], t]); bl.append(len(lines) - 1)
        if bl: blocks.append(bl)
    return lines, blocks

def _inside(r, b, pad=2):
    return r[0] >= b[0] - pad and r[2] <= b[2] + pad and r[1] >= b[1] - pad and r[3] <= b[3] + pad

def _objects(page, fb):
    """Graphic sub-objects inside a figure: raster images and small drawing clusters (page space)."""
    area = max(1.0, (fb[2] - fb[0]) * (fb[3] - fb[1])); out = []
    try:
        for im in page.get_image_info():
            r = list(im.get("bbox") or [])
            if len(r) == 4 and _inside(r, fb, 4) and (r[2] - r[0]) * (r[3] - r[1]) < 0.7 * area and r[2] - r[0] > 12 and r[3] - r[1] > 12:
                out.append([round(v, 1) for v in r] + ["image"])
    except Exception: pass
    try:                                              # individual vector paths (boxes, plots, outlines)
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None: continue
            rr = [r.x0, r.y0, r.x1, r.y1]
            if _inside(rr, fb, 2) and r.width > 10 and r.height > 10 and 150 < r.width * r.height < 0.35 * area:
                out.append([round(v, 1) for v in rr] + ["drawing"])
    except Exception: pass
    out.sort(key=lambda o: -(o[2] - o[0]) * (o[3] - o[1]))
    return out[:400]

def _ocr_labels(page, fb, tmpdir=None):
    """Raster figures carry no PDF text: render the figure and read its labels with the tracker's text recognition."""
    trk = os.environ.get("SPEAKEASY_TRACKER") or os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "bin", "speakeasy-track")
    if not os.path.exists(trk): return []
    import pymupdf
    png = os.path.join(tmpdir or tempfile.gettempdir(), f"speakeasy_cap_fig_{os.getpid()}.png")
    try:
        page.get_pixmap(clip=pymupdf.Rect(*fb), matrix=pymupdf.Matrix(3, 3)).save(png)
        r = subprocess.run([trk, "--ocr-lines", png, "--frame", ",".join(str(v) for v in [fb[0], fb[1], fb[2] - fb[0], fb[3] - fb[1]])],
                           capture_output=True, timeout=20)
        out = json.loads(r.stdout.decode() or "[]")
        return [[round(v, 1) for v in o["r"]] + [o["t"]] for o in out if re.search(r"[A-Za-z]{2,}", o.get("t", ""))]
    except Exception:
        return []
    finally:
        try: os.remove(png)
        except OSError: pass

def _guess_panels(fb, caption):
    """No drawn (a)/(b) labels but the caption names panels: split the figure evenly (side by side when wide)."""
    cap = caption or ""
    ls = set(re.findall(r"\(([a-f])\)", cap))
    ls |= set(re.findall(r"(?:^|[.;|]\s+)([a-f])\s+(?=[A-Z])", cap))          # Nature style: "... scheme. a Subjects sit ... b Recorded"
    ls = [l for l in "abcdef" if l in ls]
    if len(ls) < 2 or ls[0] != "a": return []
    n = len(ls); w, h = fb[2] - fb[0], fb[3] - fb[1]; out = []
    for k, l in enumerate(ls):
        if w >= h: r = [fb[0] + k * w / n, fb[1], fb[0] + (k + 1) * w / n, fb[3]]
        else: r = [fb[0], fb[1] + k * h / n, fb[2], fb[1] + (k + 1) * h / n]
        out.append({"label": l, "bbox": [round(v, 1) for v in r], "guessed": True})
    return out

def parse_pdf(path, fig_pages=None, prev=None):
    """Text lines for every page; figures (bbox, caption, panels, text labels, graphic objects) for fig_pages (None = all).
    prev: an earlier partial parse whose figure pages are kept."""
    import pymupdf
    doc = pymupdf.open(path)
    pages = []
    old = {p["n"]: p for p in (prev or {}).get("pages", []) if p.get("figs_done")}
    for pno, page in enumerate(doc):
        W, H = page.rect.width, page.rect.height
        if pno + 1 in old and (fig_pages is None or True):
            pages.append(old[pno + 1]); continue
        if fig_pages is not None and pno + 1 not in fig_pages:
            lines, _ = _text_lines(page)
            pages.append({"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": [], "figs_done": False}); continue
        lines, blocks = _text_lines(page)
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
            if kind == "table":
                # the body is the grid / text block right below the caption (until a big gap, the next caption or the page end)
                below = sorted([l for l in lines if l[1] >= cap[3] - 1 and l[0] < cap[2] + 200 and l[2] > cap[0] - 200], key=lambda l: l[1])
                y = cap[3]; rows = []
                lh = max(8.0, (cap[3] - cap[1]) / max(1, len(bl)))
                for l in below:
                    if l[1] - y > 2.2 * lh and rows: break
                    if CAP_RE.match(l[4]) or TAB_RE.match(l[4]): break
                    rows.append(l); y = max(y, l[3])
                tbody = _union([r[:4] for r in rows]) if rows else None
                if tbody and (body is None or tbody[3] > body[3]): body = _union([body, tbody]) if body else tbody
                if body: body = _union([body, cap])
            if body is None and kind == "figure":            # no vector/image found: the text-free gap above the caption
                above = [l for l in lines if l[3] <= cap[1] - 2 and l[0] < cap[2] and l[2] > cap[0]]
                top = max([l[3] for l in above if cap[1] - l[3] > 30] or [None]) if above else None
                if top is not None and cap[1] - top > 40: body = [cap[0], top + 2, cap[2], cap[1] - 2]
            fb = [round(v, 1) for v in (body or cap)]
            labels = [] if kind == "table" else [{"l": (PANEL_RE.match(l[4]).group(1) or PANEL_RE.match(l[4]).group(2) or PANEL_RE.match(l[4]).group(3)).lower(), "r": l[:4]}
                      for l in lines if PANEL_RE.match(l[4]) and len(l[4]) <= 40 and body
                      and l[0] >= fb[0] - 6 and l[2] <= fb[2] + 6 and l[1] >= fb[1] - 6 and l[3] <= fb[3] + 6]
            seen = set(); labels = [x for x in labels if not (x["l"] in seen or seen.add(x["l"]))]
            labels.sort(key=lambda x: x["l"])
            # the figure's own text labels ("WANDmini", "Base Station", "EEG"): words, not tick numbers
            tl = [l for l in lines if body and _inside(l[:4], fb) and re.search(r"[A-Za-z]{2,}", l[4]) and not PANEL_RE.match(l[4])]
            src_l = "pdf"
            if body and kind == "figure" and len(tl) < 3:
                tl = _ocr_labels(page, fb) or tl; src_l = "ocr" if tl else "none"
            figs.append({"label": f"{'Figure' if kind == 'figure' else 'Table'} {m.group(1)}", "kind": kind, "n": int(m.group(1)),
                         "page": pno + 1, "bbox": fb, "caption_bbox": cap, "caption": text[:300],
                         "panels": _panels(fb, labels) or (_guess_panels(fb, text) if body and kind == "figure" else []), "labels": tl[:200], "labels_src": src_l,
                         "objects": _objects(page, fb) if body and kind == "figure" else []})
        pages.append({"n": pno + 1, "w": round(W, 1), "h": round(H, 1), "lines": lines, "figures": figs, "figs_done": True})
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
            if pg is None or pg.get("figs_done", True): return prev, "hit"
        except ValueError: prev = None
    if kind == "md":
        doc = parse_md(path)
    else:
        py = venv_python()
        if not py: return None, "no-pymupdf"
        tmp = c + f".{os.getpid()}.tmp"
        selp = tmp + ".sel"
        with open(selp, "w") as f: f.write(sel_text or "")
        args = [py, os.path.realpath(__file__), "parse-pdf", path, tmp, "--sel", selp] + (["--prev", c] if prev else [])
        try:
            r = subprocess.run(args, capture_output=True, timeout=timeout)
        finally:
            try: os.remove(selp)
            except OSError: pass
        if r.returncode != 0 or not os.path.exists(tmp):
            return None, "parse-failed: " + r.stderr.decode(errors="replace").strip().splitlines()[-1][:120] if r.stderr else "parse-failed"
        os.replace(tmp, c)
        return json.load(open(c)), "partial-miss" if prev else "miss"
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

def fetch_pdf(url, tmpdir, max_mb=40, timeout=8):
    """Public PDFs only: no cookies, no auth, refuse redirects to other hosts or non-PDF answers."""
    for f in os.listdir(tmpdir):                     # clean our own old downloads (>1 day)
        if f.startswith("speakeasy_doc_") and time.time() - os.path.getmtime(os.path.join(tmpdir, f)) > 86400:
            try: os.remove(os.path.join(tmpdir, f))
            except OSError: pass
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

def figure_context(figs, limit=8, cap_chars=160):
    out = []
    for f in figs[:limit]:
        if not f.get("label"): continue
        pn = ", ".join(p["label"] for p in f.get("panels") or [])
        cap = re.sub(r"\s+", " ", f.get("caption") or "")
        cap = re.sub(r"^\s*(?:fig(?:ure)?s?\.?|table)\s*\d+\s*[.:|\-\u2013\u2014]?\s*", "", cap, flags=re.I)
        cap = cap[:cap_chars].rsplit(" ", 1)[0] if len(cap) > cap_chars else cap
        out.append(f"{f['label']}" + (f" (panels {pn})" if pn else "") + (": " + cap.rstrip(" .,;:") if cap else ""))
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
    fa = (fig["bbox"][2] - fig["bbox"][0]) * (fig["bbox"][3] - fig["bbox"][1])
    objs = [o for o in fig.get("objects") or [] if (o[2] - o[0]) * (o[3] - o[1]) < 0.35 * fa]
    def hood(lr):
        def dist(o):
            dx = max(o[0] - lr[2], lr[0] - o[2], 0); dy = max(o[1] - lr[3], lr[1] - o[3], 0); return (dx * dx + dy * dy) ** 0.5
        h = max(6.0, lr[3] - lr[1])
        near = sorted([o for o in objs if dist(o) <= 14 and (o[2] - o[0]) * (o[3] - o[1]) > 4 * (lr[2] - lr[0]) * h],
                      key=lambda o: (dist(o) > 0, (o[2] - o[0]) * (o[3] - o[1])))
        if near: return _union([near[0][:4], lr]), True
        return [lr[0] - 2 * h, lr[1] - 1.5 * h, lr[2] + 2 * h, lr[3] + 1.5 * h], False
    scored = [(hood(c[2][:4]), c) for c in top]
    (r, attached), c = max(scored, key=lambda x: x[0][1])
    l = c[2]
    fb = fig["bbox"]
    r = [max(fb[0], r[0]), max(fb[1], r[1]), min(fb[2], r[2]), min(fb[3], r[3])]
    return r, l[4], round(-c[1], 2), attached

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
    if len(sys.argv) >= 4 and sys.argv[1] == "parse-pdf":
        opt = dict(zip(sys.argv[4::2], sys.argv[5::2]))
        prev = json.load(open(opt["--prev"])) if opt.get("--prev") and os.path.exists(opt["--prev"]) else None
        sel = open(opt["--sel"]).read() if opt.get("--sel") else ""
        if sel.strip():
            import pymupdf
            d0 = pymupdf.open(sys.argv[2])
            txt = {"pages": [{"n": i + 1, "lines": _text_lines(pg)[0]} for i, pg in enumerate(d0)]}
            pg, _ = match_page(txt, sel)
            want = {pg["n"] - 1, pg["n"], pg["n"] + 1} if pg else {1}
            out = parse_pdf(sys.argv[2], want, prev)
        else:
            out = parse_pdf(sys.argv[2], None, prev)
        json.dump(out, open(sys.argv[3], "w")); sys.exit(0)
    if len(sys.argv) == 3 and sys.argv[1] == "dump":
        print(json.dumps(parse_pdf(sys.argv[2]) if sys.argv[2].lower().endswith(".pdf") else parse_md(sys.argv[2]), indent=1)[:6000]); sys.exit(0)
    print("usage: docmap.py parse-pdf FILE OUT.json | dump FILE", file=sys.stderr); sys.exit(2)
