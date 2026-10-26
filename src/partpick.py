"""Set-of-Mark part picker (docs/contracts/phase5-parts.md section 4): the candidate rects of a panel / figure scope,
the marked crop (rendered in the doc venv: pypdfium2 + Pillow), the Ollama /api/chat body and the letter decision from
the reply's top_logprobs. The main process only uses the stdlib half; `render` runs as a doc-venv subprocess:
    <venv>/bin/python partpick.py render PDF PAGE SCOPE_JSON CANDS_JSON OUT.png   -> prints {"palette": ..., "px": [w, h]}"""
import json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import docmap

PICK_MIN_P = 0.95             # provisional: chosen once on the public live sweep (contract 8.2, Amendment 1), then frozen
MAX_CANDIDATES = 16
LETTERS = "ABCDEFGHIJKLMNOP"
PALETTE = (("red", (0xE0, 0x00, 0x00)), ("magenta", (0xFF, 0x00, 0xFF)), ("blue", (0x00, 0x50, 0xFF)))
NEAR_COLOUR, PALETTE_MAX_FRAC = 60, 0.01      # per-channel distance of a "near-colour" pixel; palette colour must cover < 1 %
SCOPE_MARGIN, MAX_PX = 4.0, 1024

def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1])); i = ix * iy
    u = docmap._area(a) + docmap._area(b) - i
    return i / u if u > 0 else 0.0

def candidates(fig, scope, scope_panel, callout=False):
    """Contract 4.2: page rects lettered A, B, ... in reading order. Attached parts of the scope first (with callout=True
    also its unattached callouts), then its objects of 0.5-60 % of the scope with both sides >= 8 pt, largest first;
    IoU >= 0.7 duplicates of an earlier rect dropped; at most MAX_CANDIDATES."""
    in_scope = lambda letter: scope_panel is None or letter == scope_panel
    sa = docmap._area(scope)
    parts = [p["rect"] for p in fig.get("parts") or [] if in_scope(p["panel"]) and (p["attached"] or (callout and p["kind"] == "callout"))]
    objs = sorted((o[:4] for o in fig.get("objects") or [] if in_scope(o[5] if len(o) > 5 else None)
                   and 0.005 * sa <= docmap._area(o) <= 0.6 * sa and o[2] - o[0] >= 8 and o[3] - o[1] >= 8), key=lambda o: -docmap._area(o))
    kept = []
    for r in parts + objs:
        if len(kept) == MAX_CANDIDATES: break
        if all(_iou(r, k) < 0.7 for k in kept): kept.append(list(r))
    return [kept[i] for i in docmap.rect_order(kept)]

def phrase(sentence, callout=False):
    """The phrase the prompt names: the object phrase of the focus phrase (the whole focus phrase for a callout
    reference), without quotes or braces, at most 120 chars."""
    fp = docmap.focus_phrase(sentence)
    t = fp if callout else docmap.object_phrase(fp)
    return "".join(c for c in t if c not in "\"'{}\u201c\u201d").strip()[:120]

def request_body(model, keep_alive, prompt, png_b64):
    return {"model": model, "stream": False, "keep_alive": keep_alive,
            "messages": [{"role": "user", "content": prompt, "images": [png_b64]}],
            "options": {"temperature": 0, "num_predict": 1}, "logprobs": True, "top_logprobs": 20}

def decide(reply, n, min_p=PICK_MIN_P):
    """Contract 4.5 on an /api/chat reply for n candidates: {outcome, chosen_index, p_top, p_margin, letter_mass}.
    outcome: accepted | low-confidence | off-menu | no-logprobs."""
    out = {"outcome": "no-logprobs", "chosen_index": None, "p_top": None, "p_margin": None, "letter_mass": None}
    lp = (reply or {}).get("logprobs") or []
    top = (lp[0] or {}).get("top_logprobs") if lp and isinstance(lp[0], dict) else None
    if not top: return out
    q = {}
    for e in top:
        tok = (e.get("token") or "").strip()
        if len(tok) == 1 and tok in LETTERS[:n]: q[tok] = q.get(tok, 0.0) + math.exp(e["logprob"])
    mass = sum(q.values())
    out["letter_mass"] = round(mass, 2)
    if mass < 0.5: return dict(out, outcome="off-menu")
    p = sorted(((v / mass, k) for k, v in q.items()), key=lambda x: (-x[0], x[1]))
    p_top = p[0][0]; margin = p_top - p[1][0] if len(p) > 1 else 0.0
    out.update(chosen_index=LETTERS.index(p[0][1]), p_top=round(p_top, 2), p_margin=round(margin, 2))
    out["outcome"] = "accepted" if p_top >= min_p else "low-confidence"
    return out

# ---------------- rendering (doc venv only: pypdfium2 + Pillow) ----------------
def _near_fraction(img, rgb):
    from PIL import ImageChops
    masks = [band.point(lambda v, c=c: 255 if abs(v - c) <= NEAR_COLOUR else 0) for band, c in zip(img.split(), rgb)]
    both = ImageChops.logical_and(ImageChops.logical_and(masks[0].convert("1"), masks[1].convert("1")), masks[2].convert("1"))
    return both.histogram()[255] / max(1, img.size[0] * img.size[1])

def palette(img):
    """(name, rgb): the first palette colour covering < 1 % of the crop, else red."""
    return next(((n, c) for n, c in PALETTE if _near_fraction(img, c) < PALETTE_MAX_FRAC), PALETTE[0])

def _dist(px, py, r):
    dx = max(r[0] - px, px - r[2], 0); dy = max(r[1] - py, py - r[3], 0)
    return (dx * dx + dy * dy) ** 0.5

def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def place_tags(rects, tag_w, tag_h, size, step=4):
    """Tag boxes (px) for candidate rects (px), placed smallest candidate first: the 4 px grid point farthest from the
    candidate's edges and from every smaller candidate, with the tag inside the candidate and clear of earlier tags;
    else outside, against the candidate's top-left corner (the nearest free spot to it)."""
    W, H = size; tags = [None] * len(rects)
    order = sorted(range(len(rects)), key=lambda i: (docmap._area(rects[i]), i))
    for k, i in enumerate(order):
        r = rects[i]; smaller = [rects[j] for j in order[:k]]; placed = [t for t in tags if t]
        free = lambda t: t[0] >= 0 and t[1] >= 0 and t[2] <= W and t[3] <= H and not any(_overlaps(t, o) for o in placed)
        pts = [(x, y) for x in range(int(r[0] + tag_w / 2 + 1), int(r[2] - tag_w / 2), step) for y in range(int(r[1] + tag_h / 2 + 1), int(r[3] - tag_h / 2), step)]
        edge = lambda p: min(p[0] - r[0], r[2] - p[0], p[1] - r[1], r[3] - p[1])
        best, best_score = None, -1.0
        for p in sorted(pts, key=lambda p: (-edge(p), p[1], p[0])):
            if edge(p) <= best_score: break
            score = min([edge(p)] + [_dist(p[0], p[1], s) for s in smaller])
            box = (p[0] - tag_w / 2, p[1] - tag_h / 2, p[0] + tag_w / 2, p[1] + tag_h / 2)
            if score > best_score and free(box): best, best_score = box, score
        x0, y0 = r[0], r[1] - tag_h                          # outside: just above the top-left corner, else the nearest free spot
        for d in range(0, max(W, H) + 1, step):
            if best is not None: break
            ring = {(dx, dy) for dx in range(-d, d + 1, step) for dy in (-d, d)} | {(dx, dy) for dy in range(-d, d + 1, step) for dx in (-d, d)}
            best = next(((x0 + dx, y0 + dy, x0 + dx + tag_w, y0 + dy + tag_h) for dx, dy in sorted(ring, key=lambda q: (q[0] * q[0] + q[1] * q[1], q[1], q[0]))
                         if free((x0 + dx, y0 + dy, x0 + dx + tag_w, y0 + dy + tag_h))), None)
        tags[i] = tuple(int(round(v)) for v in best or (x0, y0, x0 + tag_w, y0 + tag_h))
    return tags

def render(pdf, page_no, scope, cands, out_png):
    """The scope (+ 4 pt) rendered with zoom 1-4 (long side <= 1024 px), each candidate outlined and tagged with its letter."""
    import pdfpage
    from PIL import ImageDraw, ImageFont
    page = pdfpage.open_pdf(pdf)[page_no - 1]
    W, H = page.rect.width, page.rect.height
    clip = [max(0.0, scope[0] - SCOPE_MARGIN), max(0.0, scope[1] - SCOPE_MARGIN), min(W, scope[2] + SCOPE_MARGIN), min(H, scope[3] + SCOPE_MARGIN)]
    zoom = max(1.0, min(4.0, MAX_PX / max(clip[2] - clip[0], clip[3] - clip[1])))
    img = page.image(clip, zoom)
    long_side = max(img.size)
    name, rgb = palette(img)
    px = [[(r[0] - clip[0]) * zoom, (r[1] - clip[1]) * zoom, (r[2] - clip[0]) * zoom, (r[3] - clip[1]) * zoom] for r in cands]
    draw = ImageDraw.Draw(img)
    for r in px: draw.rectangle(r, outline=rgb, width=3 if long_side >= 800 else 2)
    font = ImageFont.load_default(size=max(14, round(0.025 * long_side)))
    boxes = [draw.textbbox((0, 0), LETTERS[i], font=font, stroke_width=1) for i in range(len(px))]
    tag_w = max(b[2] - b[0] for b in boxes) + 8; tag_h = max(b[3] - b[1] for b in boxes) + 8
    for i, t in enumerate(place_tags(px, tag_w, tag_h, img.size)):
        draw.rectangle(t, fill=(255, 255, 255), outline=rgb, width=2)
        b = boxes[i]
        x = t[0] + (t[2] - t[0] - (b[2] - b[0])) / 2 - b[0]; y = t[1] + (t[3] - t[1] - (b[3] - b[1])) / 2 - b[1]
        draw.text((x, y), LETTERS[i], fill=rgb, font=font, stroke_width=1, stroke_fill=rgb)
    img.save(out_png, format="PNG")
    return {"palette": name, "px": list(img.size), "zoom": round(zoom, 3)}

if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "render":
        print(json.dumps(render(sys.argv[2], int(sys.argv[3]), json.loads(sys.argv[4]), json.loads(sys.argv[5]), sys.argv[6]))); sys.exit(0)
    print("usage: partpick.py render PDF PAGE SCOPE_JSON CANDS_JSON OUT.png", file=sys.stderr); sys.exit(2)
