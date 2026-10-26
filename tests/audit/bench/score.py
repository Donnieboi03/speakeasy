"""Pure scoring functions for the Phase 2 benchmark (stdlib only). Contract: docs/contracts/phase2-benchmark.md, sections 5.3, 6, 8.
Rects are page-space [x0, y0, x1, y1] (PDF points, origin top-left)."""
import hashlib, math, re

SCORED_KINDS = ("paragraph", "caption", "figure", "table")
LINE_REASONS = ("not_spoken", "no_show", "no_doc_target", "wrong_cue", "wrong_page", "under_cover", "over_extend")
PART_REASONS = ("not_spoken", "no_show", "no_oval", "wrong_page", "centre_outside")
PART_STYLES = ("inside", "adjacent", "callout", "raster")
CONFIG = {"box_iou_hit": 0.75, "box_iou_pr": 0.5, "line_cover_frac": 0.75, "line_axis_overlap": 0.5, "line_extra_max": 1, "speed_budget_ms": 300}
GATED = (("boxes", "score"), ("boxes", "precision_50"), ("lines", "score"), ("parts", "score"), ("parts", "precision"))


class AttributionError(Exception):
    pass


def norm_ws(s):
    return re.sub(r"\s+", " ", s or "").strip()


def text_hash(s):
    return hashlib.sha256(norm_ws(s).encode("utf-8")).hexdigest()[:12]


def area(r):
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy; union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def centre(r):
    return ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0)


def inside(pt, r):
    return r[0] <= pt[0] <= r[2] and r[1] <= pt[1] <= r[3]


def pct(hits, n):
    return round(100.0 * hits / n, 2) if n else None


def nearest_rank(vals, q):
    v = sorted(vals)
    if not v: return None
    return v[max(0, math.ceil(q * len(v)) - 1)]


# ---------------------------------------------------------------- boxes (6.1)
def match(gts, preds, t):
    """gts: [(gt_id, bbox)], preds: [bbox] (one page, one kind). Greedy one-to-one: pairs with IoU >= t sorted by IoU desc,
    GT id asc, prediction index asc. Returns {gt_index: pred_index}."""
    pairs = []
    for gi, (gid, gb) in enumerate(gts):
        for pi, pb in enumerate(preds):
            v = iou(gb, pb)
            if v >= t: pairs.append((-v, gid, pi, gi))
    pairs.sort()
    used_g, used_p, out = set(), set(), {}
    for _, _, pi, gi in pairs:
        if gi in used_g or pi in used_p: continue
        used_g.add(gi); used_p.add(pi); out[gi] = pi
    return out


def _zero_box():
    return {"hits": 0, "gt": 0, "matched_50": 0, "pred": 0}


def box_counts_page(gt_blocks, pred_blocks, t_hit=0.75, t_pr=0.5):
    """One scored page. gt_blocks: [{id, kind, bbox}], pred_blocks: [{kind, bbox}]. Returns {kind: counts} for kinds in K."""
    ignore = [b["bbox"] for b in gt_blocks if b["kind"] not in SCORED_KINDS]
    out = {}
    for k in SCORED_KINDS:
        g = [(b["id"], b["bbox"]) for b in gt_blocks if b["kind"] == k]
        p = [b["bbox"] for b in pred_blocks if b.get("kind") == k]
        m75 = match(g, p, t_hit); m50 = match(g, p, t_pr)
        matched_p = set(m50.values())
        ignored = sum(1 for pi, pb in enumerate(p) if pi not in matched_p and any(iou(pb, ib) >= t_pr for ib in ignore))
        out[k] = {"hits": len(m75), "gt": len(g), "matched_50": len(m50), "pred": len(p) - ignored}
    return out


def _add(a, b):
    for k in ("hits", "gt", "matched_50", "pred"): a[k] += b[k]


def _box_summary(c):
    return {"score": pct(c["hits"], c["gt"]), "hits": c["hits"], "gt": c["gt"], "precision_50": pct(c["matched_50"], c["pred"]),
            "recall_50": pct(c["matched_50"], c["gt"]), "matched_50": c["matched_50"], "pred": c["pred"]}


def score_boxes(docs, preds, per_doc=True, t_hit=0.75, t_pr=0.5):
    """docs: [(doc_id, subset, gt)] sorted by doc_id; preds: {doc_id: {page_n: [blocks]}}."""
    tot = _zero_box(); kinds = {k: _zero_box() for k in SCORED_KINDS}; subs = {}; by_doc = {}
    for doc_id, subset, gt in docs:
        dc = _zero_box(); sc = subs.setdefault(subset, _zero_box())
        for pg in gt["pages"]:
            if not pg.get("blocks_scored"): continue
            per = box_counts_page(pg["blocks"], preds.get(doc_id, {}).get(pg["n"], []), t_hit, t_pr)
            for k, c in per.items(): _add(kinds[k], c); _add(tot, c); _add(dc, c); _add(sc, c)
        by_doc[doc_id] = dict(dc)
    out = _box_summary(tot)
    out["by_kind"] = {k: {x: v for x, v in _box_summary(c).items() if x in ("score", "hits", "gt", "precision_50", "recall_50")} for k, c in kinds.items()}
    out["by_subset"] = {s: _box_summary(c) for s, c in sorted(subs.items())}
    if per_doc: out["by_doc"] = by_doc
    return out


# ---------------------------------------------------------------- attribution (5.3)
def attribute(script, map_records):
    """script: [(sentence_id, text)] in order; map_records: focus-log map records in file order (need sent, text).
    Returns {sent: sentence_id}. Raises AttributionError when a chunk is not found in the script."""
    S = ""; spans = []
    for sid, txt in script:
        t = norm_ws(txt)
        if S: S += " "
        spans.append((len(S), len(S) + len(t), sid)); S += t
    cursor = 0; out = {}
    for r in map_records:
        k = r["sent"]; t = norm_ws(r.get("text"))
        if not t: raise AttributionError(f"chunk {k} has no logged text")
        i = S.find(t, cursor)
        if i < 0: raise AttributionError(f"chunk {k} not in script")
        sid = next((s for a, b, s in spans if a <= i < b), None)
        if sid is None: raise AttributionError(f"chunk {k} not in script")
        out.setdefault(k, sid); cursor = i + len(t)
    return out


def chunk_shows(show_records):
    """Last show record per sent."""
    out = {}
    for r in show_records:
        out[r["sent"]] = r
    return out


# ---------------------------------------------------------------- lines (6.2)
def covers(B, L, frac=0.5):
    ovx = min(B[2], L[2]) - max(B[0], L[0]); ovy = min(B[3], L[3]) - max(B[1], L[1])
    return ovy >= frac * (L[3] - L[1]) and ovx >= frac * (L[2] - L[0])


def _target(show):
    d = show.get("doc") if isinstance(show, dict) else None
    t = d.get("target") if isinstance(d, dict) else None
    return t if isinstance(t, list) and len(t) == 4 else None


def score_line_sentence(sent, page_lines, shows, cover_frac=0.75, axis=0.5, extra_max=1):
    """sent: GT sentence (page, line_rects); page_lines: GT lines of its page; shows: [show or None] one per chunk of the sentence.
    Returns (hit, reason|None)."""
    if not shows: return False, "not_spoken"
    if any(s is None for s in shows): return False, "no_show"
    if any(_target(s) is None for s in shows): return False, "no_doc_target"
    if any(s.get("cue_kind") != "text_band" or s["doc"].get("kind") != "line" for s in shows): return False, "wrong_cue"
    if any(s["doc"].get("page") != sent["page"] for s in shows): return False, "wrong_page"
    G = [list(r) for r in sent["line_rects"]]; B = [_target(s) for s in shows]
    covered = sum(1 for g in G if any(covers(b, g, axis) for b in B))
    if covered < math.ceil(cover_frac * len(G)): return False, "under_cover"
    others = [l for l in page_lines if list(l) not in G]
    for b in B:
        if sum(1 for l in others if covers(b, l, axis)) > extra_max: return False, "over_extend"
    return True, None


# ---------------------------------------------------------------- parts (6.3)
def score_part_sentence(part, shows):
    """part: GT part; shows: [show or None] one per chunk of the sentence. Returns (hit, reason|None)."""
    if not shows: return False, "not_spoken"
    real = [s for s in shows if s is not None]
    if not real: return False, "no_show"
    O = [s for s in real if s.get("cue_kind") == "part_oval"]
    if not O: return False, "no_oval"
    for o in O:
        if (o.get("doc") or {}).get("page") != part["page"]: return False, "wrong_page"
    for o in O:
        t = _target(o)
        if t is None or not inside(centre(t), part["bbox"]): return False, "centre_outside"
    return True, None


def oval_good(show, part):
    t = _target(show)
    return bool(part and t and (show.get("doc") or {}).get("page") == part["page"] and inside(centre(t), part["bbox"]))


# ---------------------------------------------------------------- aggregation helpers
def lines_summary(hits, n, reasons, by_subset):
    return {"score": pct(hits, n), "hits": hits, "n": n, "miss_reasons": {r: reasons.get(r, 0) for r in LINE_REASONS},
            "by_subset": {s: dict(v, score=pct(v["hits"], v["n"])) for s, v in sorted(by_subset.items())}}


def parts_summary(hits, n, good, ovals, reasons, by_style, by_subset):
    return {"score": pct(hits, n), "hits": hits, "n": n, "precision": pct(good, ovals), "ovals": ovals, "good_ovals": good,
            "miss_reasons": {r: reasons.get(r, 0) for r in PART_REASONS},
            "by_style": {s: {"score": pct(by_style.get(s, {}).get("hits", 0), by_style.get(s, {}).get("n", 0)), "hits": by_style.get(s, {}).get("hits", 0), "n": by_style.get(s, {}).get("n", 0)} for s in PART_STYLES},
            "by_subset": {s: dict(v, score=pct(v["hits"], v["n"]), precision=pct(v.get("good", 0), v.get("ovals", 0))) for s, v in sorted(by_subset.items())}}


def speed_summary(first_hl, a2b, n_missing):
    return {"first_highlight_ms": {"p50": nearest_rank(first_hl, 0.5), "p95": nearest_rank(first_hl, 0.95), "n": len(first_hl)},
            "audio_to_box_ms": {"p50": nearest_rank(a2b, 0.5), "p95": nearest_rank(a2b, 0.95)}, "n_missing": n_missing}


# ---------------------------------------------------------------- determinism (9)
def determinism_view(bench):
    """The fields that must be identical across two runs."""
    out = {"fixtures": bench.get("fixtures"), "config": bench.get("config"), "sets": {}}
    for s, v in (bench.get("sets") or {}).items():
        d = {k: v.get(k) for k in ("boxes", "lines", "parts")}
        if "cases" in v: d["cases"] = [{k: x for k, x in c.items() if k != "first_highlight_ms"} for c in v["cases"]]
        out["sets"][s] = d
    return out


def diff_paths(a, b, path="$"):
    if type(a) != type(b): return [path]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b: out.append(f"{path}.{k}")
            else: out += diff_paths(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b): return [path]
        out = []
        for i, (x, y) in enumerate(zip(a, b)): out += diff_paths(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path]


# ---------------------------------------------------------------- baseline + gate (8)
def baseline_metrics(set_block):
    b, l, p, sp = set_block["boxes"], set_block["lines"], set_block["parts"], set_block["speed"]["first_highlight_ms"]
    return {"boxes": {"score": b["score"], "precision_50": b["precision_50"], "recall_50": b["recall_50"]}, "lines": {"score": l["score"]},
            "parts": {"score": p["score"], "precision": p["precision"]}, "speed": {"p50": sp["p50"], "p95": sp["p95"]}}


def baseline_detail(set_block, per_doc):
    b = set_block["boxes"]
    d = {"boxes.by_kind": {k: v["score"] for k, v in b["by_kind"].items()}, "boxes.by_subset": {k: v["score"] for k, v in b["by_subset"].items()},
         "lines.by_subset": {k: v["score"] for k, v in set_block["lines"]["by_subset"].items()},
         "parts.by_subset": {k: v["score"] for k, v in set_block["parts"]["by_subset"].items()},
         "parts.by_style": {k: v["score"] for k, v in set_block["parts"]["by_style"].items()}}
    if per_doc and "by_doc" in b: d["boxes.by_doc"] = {k: pct(v["hits"], v["gt"]) for k, v in b["by_doc"].items()}
    return d


def _fmt(v):
    return "null" if v is None else f"{v:.2f}"


def gate(bench, baselines, public_only=False, budget=300):
    """bench: parsed bench.json; baselines: {set: parsed baseline or None}. Returns (exit_code, [output lines])."""
    out = []
    if not isinstance(bench, dict) or bench.get("schema") != "speakeasy-bench/1" or not isinstance(bench.get("sets"), dict) or not bench["sets"]:
        return 2, ["gate: bench is malformed or has a different schema"]
    if bench.get("complete") is not True:
        return 2, ["gate: bench is not complete (--cases used or a harness error); refusing to gate"]
    for s, blk in bench["sets"].items():
        try:
            for sec, key in GATED: blk[sec][key]
            blk["speed"]["first_highlight_ms"]["p50"]; blk["speed"]["first_highlight_ms"]["p95"]
        except (KeyError, TypeError):
            return 2, [f"gate: bench set {s} is malformed"]
        bl = baselines.get(s)
        if not bl:
            return 2, [f"gate: no baseline for {s}: record one with run_bench.py --record-baseline"]
        if bl.get("config") != bench.get("config") or bl.get("manifest_sha256") != (bench.get("fixtures") or {}).get(s, {}).get("manifest_sha256"):
            return 2, [f"baseline stale for {s}: re-record with run_bench.py --record-baseline"]
        bv = (bl.get("versions") or {}).get("pypdfium2"); cv = (bench.get("versions") or {}).get("pypdfium2")
        if bv != cv: out.append(f"warning: pypdfium2 {bv} in the {s} baseline, {cv} now")
    fails = 0; raised = False
    if baselines.get("hidden") and "hidden" not in bench["sets"]:
        if public_only: out.append("warning: hidden set not run (--public-only)")
        else: out.append("hidden set not run  FAIL"); fails += 1
    for s in sorted(bench["sets"]):
        blk = bench["sets"][s]; m = baselines[s]["metrics"]
        for sec, key in GATED:
            cur = blk[sec][key]; base = (m.get(sec) or {}).get(key)
            if base is None: out.append(f"{s} {sec}.{key} {_fmt(base)} -> {_fmt(cur)} n/a"); continue
            if cur is None: out.append(f"{s} {sec}.{key} {_fmt(base)} -> null FAIL"); fails += 1; continue
            d = round(cur - base, 2); bad = d < -1.0; fails += bad; raised |= d > 1.0
            out.append(f"{s} {sec}.{key} {_fmt(base)} -> {_fmt(cur)} {d:+.2f} {'FAIL' if bad else 'PASS'}")
        sp = blk["speed"]["first_highlight_ms"]
        for q in ("p50", "p95"):
            v = sp[q]; bad = v is None or v >= budget; fails += bad
            out.append(f"{s} speed.first_highlight_ms.{q} budget<{budget} -> {v if v is not None else 'null'} {'FAIL' if bad else 'PASS'}")
    out.append("-- informational")
    for s in sorted(bench["sets"]):
        det = (baselines[s].get("detail") or {}); cur = baseline_detail(bench["sets"][s], s == "public")
        for grp in sorted(cur):
            for k, v in sorted(cur[grp].items()):
                b = (det.get(grp) or {}).get(k)
                dd = "" if v is None or b is None else f" {v - b:+.2f}"
                out.append(f"{s} {grp}.{k} {_fmt(b)} -> {_fmt(v)}{dd}")
    if raised: out.append("baseline can be raised (--record-baseline)")
    out.append("GATE PASS" if not fails else f"GATE FAIL ({fails} regressions)")
    return (0 if not fails else 1), out
