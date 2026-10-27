"""Shared low-level helpers of the docmap modules: page rectangles [x0, y0, x1, y1] (union, overlap, inside, area, clip,
distances, reading order), the shared fold's match tokens, and the tracker binary (its OCR helper). docmap re-exports every name."""
import os

def _toks(s):
    from textfold import match_tokens                     # the shared fold's match tokens: digits kept, no stopwords
    return match_tokens(s)

def _union(rs):
    if not rs: return None
    return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]

def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def _inside(r, b, pad=2):
    return r[0] >= b[0] - pad and r[2] <= b[2] + pad and r[1] >= b[1] - pad and r[3] <= b[3] + pad

def rect_order(rects, tol=12):
    """Indexes of rects in reading order: rows by top edge (a row holds tops within tol of its first), then by x."""
    rows = []
    for i in sorted(range(len(rects)), key=lambda i: (rects[i][1], rects[i][0])):
        if rows and rects[i][1] - rects[rows[-1][0]][1] <= tol: rows[-1].append(i)
        else: rows.append([i])
    return [i for row in rows for i in sorted(row, key=lambda i: (rects[i][0], rects[i][1]))]

def _area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])

def _rect_dist(a, b):
    dx = max(b[0] - a[2], a[0] - b[2], 0); dy = max(b[1] - a[3], a[1] - b[3], 0)
    return (dx * dx + dy * dy) ** 0.5

def _pt_dist(p, r):
    return _rect_dist([p[0], p[1], p[0], p[1]], r)

def _pad_label(lr):
    h = max(6.0, lr[3] - lr[1])
    return [lr[0] - 2 * h, lr[1] - 1.5 * h, lr[2] + 2 * h, lr[3] + 1.5 * h]

def _clip(r, b):
    return [max(b[0], r[0]), max(b[1], r[1]), min(b[2], r[2]), min(b[3], r[3])]

def _centre_in(r, b):
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return b[0] <= cx <= b[2] and b[1] <= cy <= b[3]

def tracker_bin():
    return os.environ.get("SPEAKEASY_TRACKER") or os.path.join(os.environ.get("SPEAKEASY_HOME") or os.path.expanduser("~/.speakeasy"), "bin", "speakeasy-track")
