"""Pure scoring functions for the Phase 2 benchmark (stdlib only). Contract: docs/contracts/phase2-benchmark.md, sections 5.3, 6, 8.
Rects are page-space [x0, y0, x1, y1] (PDF points, origin top-left)."""
import hashlib, json, math, re

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


def wrong_type_page(gt_blocks, pred_blocks, t=0.75):
    """Predicted blocks of a scored kind that are not a hit for their own kind but overlap a GT block of a different kind at
    IoU >= t (right box, wrong type). Returns {pred_kind: {gt_kind: n}} (the best-IoU other-kind GT block names the type)."""
    out = {}
    for k in SCORED_KINDS:
        own = [(b["id"], b["bbox"]) for b in gt_blocks if b["kind"] == k]
        p = [b["bbox"] for b in pred_blocks if b.get("kind") == k]
        hit = set(match(own, p, t).values())
        for pi, pb in enumerate(p):
            if pi in hit: continue
            best = max(((iou(pb, g["bbox"]), g["kind"]) for g in gt_blocks if g["kind"] != k), default=(0.0, None))
            if best[0] >= t: out.setdefault(k, {}); out[k][best[1]] = out[k].get(best[1], 0) + 1
    return out


def _add(a, b):
    for k in ("hits", "gt", "matched_50", "pred"): a[k] += b[k]


def _box_summary(c):
    return {"score": pct(c["hits"], c["gt"]), "hits": c["hits"], "gt": c["gt"], "precision_50": pct(c["matched_50"], c["pred"]),
            "recall_50": pct(c["matched_50"], c["gt"]), "matched_50": c["matched_50"], "pred": c["pred"]}


def score_boxes(docs, preds, per_doc=True, t_hit=0.75, t_pr=0.5):
    """docs: [(doc_id, subset, gt)] sorted by doc_id; preds: {doc_id: {page_n: [blocks]}}."""
    tot = _zero_box(); kinds = {k: _zero_box() for k in SCORED_KINDS}; subs = {}; by_doc = {}; wrong = {k: {} for k in SCORED_KINDS}
    for doc_id, subset, gt in docs:
        dc = _zero_box(); sc = subs.setdefault(subset, _zero_box())
        for pg in gt["pages"]:
            if not pg.get("blocks_scored"): continue
            per = box_counts_page(pg["blocks"], preds.get(doc_id, {}).get(pg["n"], []), t_hit, t_pr)
            for k, c in per.items(): _add(kinds[k], c); _add(tot, c); _add(dc, c); _add(sc, c)
            for k, a in wrong_type_page(pg["blocks"], preds.get(doc_id, {}).get(pg["n"], []), t_hit).items():
                for gk, n in a.items(): wrong[k][gk] = wrong[k].get(gk, 0) + n
        by_doc[doc_id] = dict(dc)
    out = _box_summary(tot)
    out["by_kind"] = {k: {x: v for x, v in _box_summary(c).items() if x in ("score", "hits", "gt", "precision_50", "recall_50")} for k, c in kinds.items()}
    out["by_subset"] = {s: _box_summary(c) for s, c in sorted(subs.items())}
    out["wrong_type"] = {k: {"n": sum(a.values()), "as": dict(sorted(a.items()))} for k, a in wrong.items()}
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


def _is_rect(r):
    return isinstance(r, (list, tuple)) and len(r) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in r)


def _band_rects(show):
    """B_c for one chunk: the show's doc.lines rects when non-empty (phase 3 contract G.2), else [doc.target]."""
    d = show.get("doc") if isinstance(show, dict) else None
    ls = d.get("lines") if isinstance(d, dict) else None
    if isinstance(ls, list):
        rs = [list(r) for r in ls if _is_rect(r)]
        if rs: return rs
    t = _target(show)
    return [t] if t is not None else None


def score_line_sentence(sent, page_lines, shows, cover_frac=0.75, axis=0.5, extra_max=1):
    """sent: GT sentence (page, line_rects); page_lines: GT lines of its page; shows: [show or None] one per chunk of the sentence.
    A chunk's band is its doc.lines rects when present, else its doc.target. Returns (hit, reason|None)."""
    if not shows: return False, "not_spoken"
    if any(s is None for s in shows): return False, "no_show"
    if any(_band_rects(s) is None for s in shows): return False, "no_doc_target"
    if any(s.get("cue_kind") != "text_band" or s["doc"].get("kind") != "line" for s in shows): return False, "wrong_cue"
    if any(s["doc"].get("page") != sent["page"] for s in shows): return False, "wrong_page"
    G = [list(r) for r in sent["line_rects"]]; B = [_band_rects(s) for s in shows]
    covered = sum(1 for g in G if any(covers(r, g, axis) for rs in B for r in rs))
    if covered < math.ceil(cover_frac * len(G)): return False, "under_cover"
    others = [l for l in page_lines if list(l) not in G]
    for rs in B:
        if sum(1 for l in others if any(covers(r, l, axis) for r in rs)) > extra_max: return False, "over_extend"
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


def parts_counts(script_ids, sents, parts, chunks_of, cshow, owner):
    """Parts hits over the script's parts sentences and good/all part ovals over every chunk (the gated rules)."""
    out = {"hits": 0, "n": 0, "good": 0, "ovals": 0}
    for sid in script_ids:
        s = sents[sid]
        if s["score"] != "parts": continue
        hit, _ = score_part_sentence(parts[s["part"]], [cshow.get(k) for k in sorted(chunks_of.get(sid, []))])
        out["n"] += 1; out["hits"] += hit
    for k in sorted(cshow):
        sh = cshow[k]
        if sh.get("cue_kind") != "part_oval": continue
        out["ovals"] += 1; s = sents.get(owner.get(k)) if k in owner else None
        if s and s["score"] == "parts" and oval_good(sh, parts[s["part"]]): out["good"] += 1
    return out


# ---------------------------------------------------------------- picks (phase5-parts.md 7.2)
PICK_OUTCOMES = ("accepted", "low-confidence", "off-menu", "no-logprobs", "unavailable", "error", "timeout", "out-of-scope")


def _isint(v):
    return isinstance(v, int) and not isinstance(v, bool)


def pick_eval(picks, owner, sents, parts, cshow):
    """One entry per focus-log part_pick record, scored from the logged decision. A pick is attributed to the script sentence
    of its chunk (`sent`); it is correct when it is accepted, its sentence is parts-scored, cand_rects[chosen_index] has its
    centre inside the GT part bbox, and the chunk's show (when it has a page) is on the part's page."""
    out = []
    for r in picks:
        k = r.get("sent"); s = sents.get(owner.get(k)) if k in owner else None
        part = parts.get(s["part"]) if s and s["score"] == "parts" else None
        page = ((cshow.get(k) or {}).get("doc") or {}).get("page")
        rects = [list(x) for x in (r.get("cand_rects") or []) if _is_rect(x)] if isinstance(r.get("cand_rects"), list) else []
        acc = r.get("outcome") == "accepted"; ci = r.get("chosen_index")
        chosen = rects[ci] if acc and _isint(ci) and 0 <= ci < len(rects) and len(rects) == len(r.get("cand_rects") or []) else None
        page_ok = part is not None and (page is None or page == part["page"])
        out.append({"sent": k, "outcome": r.get("outcome"), "called": r.get("called") is True, "accepted": acc, "on_parts": part is not None,
                    "correct": bool(acc and chosen and page_ok and inside(centre(chosen), part["bbox"])),
                    "cand_hit": bool(page_ok and any(inside(centre(x), part["bbox"]) for x in rects)),
                    "chosen": chosen, "page": page, "bad": bool(acc and chosen is None),
                    "ms": r.get("ms") if isinstance(r.get("ms"), (int, float)) and not isinstance(r.get("ms"), bool) else None})
    return out


def shows_with_picks(cshow, evals):
    """Last show per chunk with each accepted pick drawn as that chunk's oval (replacing its fallback cue)."""
    out = dict(cshow)
    for e in evals:
        if e["accepted"] and e["chosen"] is not None:
            out[e["sent"]] = {"cue_kind": "part_oval", "doc": {"kind": "object", "page": e["page"], "target": e["chosen"]}}
    return out


def pick_summary(evals, pw, replay_miss=0):
    """sets.<set>.pick. evals: pick_eval entries of the set; pw: parts_counts summed over the set with shows_with_picks."""
    by = {o: 0 for o in PICK_OUTCOMES}
    for e in evals:
        o = e["outcome"] if e["outcome"] in PICK_OUTCOMES else "other"; by[o] = by.get(o, 0) + 1
    acc = [e for e in evals if e["accepted"]]; on = [e for e in evals if e["on_parts"]]; correct = sum(e["correct"] for e in evals)
    ms = [e["ms"] for e in evals if e["called"] and e["ms"] is not None]
    return {"requests": len(evals), "called": sum(e["called"] for e in evals), "by_outcome": by, "replay_miss": replay_miss, "stale": replay_miss > 0,
            "accepted": len(acc), "correct": correct, "on_parts": len(on), "bad_records": sum(e["bad"] for e in evals),
            "precision": pct(correct, len(acc)), "recall": pct(correct, len(on)), "candidate_recall": pct(sum(e["cand_hit"] for e in on), len(on)),
            "parts_with_pick": {"score": pct(pw["hits"], pw["n"]), "hits": pw["hits"], "n": pw["n"], "precision": pct(pw["good"], pw["ovals"]),
                                "good_ovals": pw["good"], "ovals": pw["ovals"]},
            "ms": {"p50": nearest_rank(ms, 0.5), "p95": nearest_rank(ms, 0.95), "n": len(ms)}}


# ---------------------------------------------------------------- hard invariants (phase5-parts.md A2.11 item 1, A4.1)
INVARIANTS = ("stand_in", "hairline_draw", "callout_oversize")


def _sentences(text):
    return [x for x in re.split(r"(?<=[.!?])\s+", norm_ws(text)) if x]


ALIGNED_METHODS = {"align", "align-fuzzy", "span", "overlap", "progress", "doc-line"}


def case_invariants(case, rec, shows, figs, maps=()):
    """Counts for one measured run; each must be 0. figs: {page: [{bbox, panels: [bbox]}]} from the parse.
    stand_in (figure-mode runs only, A2.3): a show on the case page whose doc.target has IoU >= 0.9 with the selection union,
    except a figure_box equal to a figure or panel bbox, a selection of one sentence, and a band for a chunk aligned to its own
    source sentence (map method align / span / ...: a one-line caption read verbatim covers the whole selection). hairline_draw (A2.4): a figure_box
    under 10 pt in height or width. callout_oversize (A4.1): a callout / callout-marker show whose target covers >= 40 % of
    the panel (else figure) holding its centre."""
    out = {k: 0 for k in INVARIANTS}; out["figure_mode_runs"] = int(bool(rec.get("doc_figure_mode")))
    rs = [it["rect"] for it in case.get("selection") or [] if _is_rect(it.get("rect"))]
    U = [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)] if rs else None
    one = len(_sentences(" ".join(it.get("text", "") for it in case.get("selection") or []))) <= 1
    method = {m.get("sent"): m.get("method") for m in maps}
    for sh in shows:
        d = sh.get("doc") if isinstance(sh.get("doc"), dict) else {}; t = d.get("target")
        if not _is_rect(t): continue
        pf = figs.get(d.get("page")) or []
        boxes = [f["bbox"] for f in pf] + [p for f in pf for p in f["panels"]]
        if sh.get("cue_kind") == "figure_box" and (t[2] - t[0] < 10 or t[3] - t[1] < 10): out["hairline_draw"] += 1
        if d.get("target_src") in ("callout", "callout-marker"):
            c = centre(t); f = next((f for f in pf if inside(c, f["bbox"])), None)
            if f:
                ref = next((p for p in f["panels"] if inside(c, p)), f["bbox"])
                if area(t) >= 0.4 * area(ref): out["callout_oversize"] += 1
        if rec.get("doc_figure_mode") and U and not one and d.get("page") == case.get("page") and iou(t, U) >= 0.9:
            aligned = sh.get("cue_kind") == "text_band" and method.get(sh.get("sent")) in ALIGNED_METHODS
            if not aligned and not (sh.get("cue_kind") == "figure_box" and any(iou(t, b) >= 0.98 for b in boxes)): out["stand_in"] += 1
    return out


def invariants_summary(acc):
    return {k: acc.get(k, 0) for k in INVARIANTS + ("figure_mode_runs",)}


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


def layout_summary(page_layouts):
    """sets.<set>.layout (phase4-layout.md section 7). page_layouts: pages[].layout of every figs_done page of the set's parses
    (None when the parse wrote no layout field: pre-Phase-4 code). Counts only, never text."""
    src = {"detector": 0, "heuristic": 0}; reasons = {}; ms = []; missing = 0
    for pl in page_layouts:
        s = pl.get("src") if isinstance(pl, dict) else None
        if s not in src: missing += 1; continue
        src[s] += 1
        if s == "heuristic":
            r = pl.get("reason") if isinstance(pl.get("reason"), str) else "none"
            reasons[r] = reasons.get(r, 0) + 1
        elif isinstance(pl.get("ms"), (int, float)) and not isinstance(pl.get("ms"), bool): ms.append(pl["ms"])
    return {"pages": len(page_layouts), "src": src, "missing": missing, "reasons": dict(sorted(reasons.items())),
            "ms": {"p50": nearest_rank(ms, 0.5), "p95": nearest_rank(ms, 0.95), "n": len(ms)}}


def layout_coverage_errors(set_name, lay):
    """A detector run must put every parsed page through the detector: [] when it did, else one message."""
    if not lay: return [f"{set_name}: no layout summary"]
    h, m = lay["src"]["heuristic"], lay["missing"]
    if not h and not m and lay["pages"]: return []
    rs = ", ".join(f"{k} {v}" for k, v in lay["reasons"].items()) or "none"
    return [f"{set_name}: detector run but {h} of {lay['pages']} pages heuristic (reasons: {rs}) and {m} without pages[].layout"]


# ---------------------------------------------------------------- kind (phase6-block-classifier.md 12.3)
KIND_KINDS = ("figure", "table")
KIND_IGNORE = ("equation", "other")          # out of scope (contract 15): a prediction on one is neither a hit nor a miss
DECOY_KINDS = ("decoration", "text-image")
TEXT_KINDS = ("paragraph", "caption", "heading", "header", "footer", "page_number")
GAP_VALUES = ("uncaptioned", "lexicon", "no-text", "image-table", "standard")
UNMATCHED = ("wrong_type", "on_decoy", "on_text", "on_nothing")
KIND_GATE = {"score": 80.0, "precision": 90.0, "by_gap_min": 60.0, "by_gap_gated": ("uncaptioned", "lexicon", "no-text"), "wrong_type_max": 2}


def _zero_kind():
    return {"gt": 0, "hits": 0, "pred": 0, "matched": 0}


def new_kind_acc():
    return {"tot": _zero_kind(), "by_kind": {k: _zero_kind() for k in KIND_KINDS}, "by_gap": {}, "by_kind_src": {}, "by_anchor": {},
            "wrong_type": {"figure_as_table": 0, "table_as_figure": 0}, "unmatched": {u: 0 for u in UNMATCHED}, "spurious_on_decoy": 0,
            "ignored": 0, "prose_decoys": {"n": 0, "kept": 0}}


def kind_page(gt_blocks, pred_blocks, acc, anchor="page", t=0.5):
    """One kind-scored page (or area capture, anchor "area"). gt_blocks: GT blocks; pred_blocks: [{kind, bbox, kind_src?}] (any kinds;
    only figure/table count). Greedy one-to-one match per kind at IoU >= t; unmatched predictions are classified as wrong_type (IoU >= t
    with a GT region of the other kind), on_decoy, ignored (out-of-scope GT kind, left out of precision), on_text or on_nothing."""
    an = acc["by_anchor"].setdefault(anchor, _zero_kind())
    def near(bb, kinds, centre_too=False):
        return any(iou(bb, d["bbox"]) >= t or (centre_too and inside(centre(bb), d["bbox"])) for d in gt_blocks if d.get("kind") in kinds and _is_rect(d.get("bbox")))
    for k in KIND_KINDS:
        g = [b for b in gt_blocks if b.get("kind") == k]
        p = [b for b in pred_blocks if b.get("kind") == k and _is_rect(b.get("bbox"))]
        m = match([(b["id"], b["bbox"]) for b in g], [b["bbox"] for b in p], t); hit_p = set(m.values()); ignored = 0
        other = tuple(x for x in KIND_KINDS if x != k)
        for gi, b in enumerate(g):
            gp = acc["by_gap"].setdefault(b.get("gap") or "none", {"hits": 0, "n": 0}); gp["n"] += 1; gp["hits"] += gi in m
        for pi, b in enumerate(p):
            if pi in hit_p: why = None
            elif near(b["bbox"], other): why = "wrong_type"; acc["wrong_type"]["figure_as_table" if k == "table" else "table_as_figure"] += 1
            elif near(b["bbox"], DECOY_KINDS): why = "on_decoy"; acc["spurious_on_decoy"] += 1
            elif near(b["bbox"], KIND_IGNORE): acc["ignored"] += 1; ignored += 1; continue
            elif near(b["bbox"], TEXT_KINDS, centre_too=True): why = "on_text"
            else: why = "on_nothing"
            if why: acc["unmatched"][why] += 1
            src = acc["by_kind_src"].setdefault(b.get("kind_src") if isinstance(b.get("kind_src"), str) else "none", {"pred": 0, "matched": 0})
            src["pred"] += 1; src["matched"] += why is None
        for c in (acc["tot"], acc["by_kind"][k], an):
            c["gt"] += len(g); c["hits"] += len(m); c["pred"] += len(p) - ignored; c["matched"] += len(m)


def prose_decoys_page(decoys, pred_blocks, acc):
    """A prose decoy line (G2) stays prose unless a predicted caption block covers >= 50 % of it or a predicted figure/table holds
    its centre."""
    for r in decoys:
        a = area(r); broken = False
        for b in pred_blocks:
            bb = b.get("bbox")
            if not _is_rect(bb): continue
            ov = max(0.0, min(r[2], bb[2]) - max(r[0], bb[0])) * max(0.0, min(r[3], bb[3]) - max(r[1], bb[1]))
            if (b.get("kind") == "caption" and a > 0 and ov >= 0.5 * a) or (b.get("kind") in KIND_KINDS and inside(centre(r), bb)): broken = True; break
        acc["prose_decoys"]["n"] += 1; acc["prose_decoys"]["kept"] += not broken


def _kc(c):
    return {"score": pct(c["hits"], c["gt"]), "hits": c["hits"], "gt": c["gt"], "precision": pct(c["matched"], c["pred"]), "matched": c["matched"], "pred": c["pred"]}


def kind_summary(acc):
    """sets.<set>.kind (or kind_vlm)."""
    out = _kc(acc["tot"])
    out["by_kind"] = {k: _kc(c) for k, c in acc["by_kind"].items()}
    out["by_gap"] = {g: {"score": pct(v["hits"], v["n"]), "hits": v["hits"], "n": v["n"]} for g, v in sorted(acc["by_gap"].items())}
    out["by_kind_src"] = {s: {"pred": v["pred"], "matched": v["matched"], "precision": pct(v["matched"], v["pred"])} for s, v in sorted(acc["by_kind_src"].items())}
    out["by_anchor"] = {a: _kc(c) for a, c in sorted(acc["by_anchor"].items())}
    out["wrong_type"] = dict(acc["wrong_type"], total=sum(acc["wrong_type"].values()))
    out["spurious_on_decoy"] = acc["spurious_on_decoy"]; out["unmatched"] = dict(acc["unmatched"]); out["ignored"] = acc["ignored"]
    pd = acc["prose_decoys"]; out["prose_decoys"] = {"n": pd["n"], "kept": pd["kept"], "broken": pd["n"] - pd["kept"]}
    return out


def score_kind(docs, preds, area_preds=None):
    """docs: [(doc_id, gt)]; preds: {doc_id: {page_n: [blocks]}}; area_preds: {(doc_id, area_id): runs-record area_kinds or None}.
    Area predictions are area_kinds {figure: [[rect]], table: [[rect]]} in capture pixels (contract 12.3)."""
    acc = new_kind_acc()
    for doc_id, gt in docs:
        for pg in gt.get("pages") or []:
            if not pg.get("kind_scored"): continue
            pb = (preds.get(doc_id) or {}).get(pg["n"]) or []
            kind_page(pg["blocks"], pb, acc, "page"); prose_decoys_page(pg.get("prose_decoys") or [], pb, acc)
        for a in gt.get("areas") or []:
            if area_preds is None or (doc_id, a["id"]) not in area_preds: continue
            kind_page(a["blocks"], area_blocks(area_preds[(doc_id, a["id"])]), acc, "area")
    return kind_summary(acc)


def area_blocks(area_kinds):
    """Runs-record area_kinds -> [{kind, bbox, kind_src}] (kind_src is not logged per rect there)."""
    if not isinstance(area_kinds, dict): return []
    return [{"kind": k, "bbox": list(r), "kind_src": "area"} for k in KIND_KINDS for r in (area_kinds.get(k) or []) if _is_rect(r)]


# ---------------------------------------------------------------- image-table rows (12.3)
ROW_REASONS = ("not_spoken", "no_show", "no_row_show", "wrong_page", "low_iou")


def is_row_show(show):
    d = show.get("doc") if isinstance(show, dict) else None
    if not isinstance(d, dict): return False
    return show.get("cue_kind") == "table_row" or d.get("kind") == "table_row" or "table-rows" in str(d.get("target_src") or "")


def score_row_sentence(sent, shows, t=0.5):
    """sent: GT table_row sentence (page, row_rect); shows: [show or None] one per chunk. Hit: some chunk's show is a table_row show
    on the sentence's page whose doc.target has IoU >= t with the GT row rect."""
    if not shows: return False, "not_spoken"
    real = [s for s in shows if s is not None]
    if not real: return False, "no_show"
    rows = [s for s in real if is_row_show(s)]
    if not rows: return False, "no_row_show"
    on_page = [s for s in rows if s["doc"].get("page") == sent["page"]]
    if not on_page: return False, "wrong_page"
    if any(_target(s) is not None and iou(_target(s), sent["row_rect"]) >= t for s in on_page): return True, None
    return False, "low_iou"


def rows_summary(hits, n, reasons, stale=False, reason=None):
    out = {"score": pct(hits, n), "hits": hits, "n": n, "miss_reasons": {r: reasons.get(r, 0) for r in ROW_REASONS}}
    if reason: out["not_scored"] = reason
    return out


# ---------------------------------------------------------------- blockcls cost block (12.3, 14.4)
def blockcls_summary(parses):
    """sets.<set>.blockcls from the parse outputs (counts, enums and ms only). parses: parse-pdf JSON dicts."""
    pages = 0; cand = 0; by_stage = {}; by_outcome = {}; ms_c = []; ms_p = []; status = {}; versions = set(); per_page = []
    for d in parses:
        top = d.get("blockcls") if isinstance(d.get("blockcls"), dict) else {}
        status[str(top.get("status"))] = status.get(str(top.get("status")), 0) + 1
        if isinstance(top.get("classifier_version"), str): versions.add(top["classifier_version"])
        for pg in d.get("pages") or []:
            k = pg.get("kinds") if isinstance(pg, dict) else None
            if not isinstance(k, dict): continue
            pages += 1; n = k.get("candidates") if isinstance(k.get("candidates"), int) else 0; cand += n; per_page.append(n)
            if n and isinstance(k.get("ms"), (int, float)) and not isinstance(k.get("ms"), bool): ms_p.append(k["ms"])
        for e in d.get("blockcls_events") or []:
            if not isinstance(e, dict): continue
            s = str(e.get("stage")); o = str(e.get("outcome")); by_stage[s] = by_stage.get(s, 0) + 1; by_outcome[o] = by_outcome.get(o, 0) + 1
            if e.get("stage") == "classifier" and isinstance(e.get("ms"), (int, float)) and not isinstance(e.get("ms"), bool): ms_c.append(e["ms"])
    return {"pages": pages, "candidates": cand, "by_stage": dict(sorted(by_stage.items())), "by_outcome": dict(sorted(by_outcome.items())),
            "status": dict(sorted(status.items())), "classifier_version": sorted(versions) or None,
            "candidates_per_page": {"p50": nearest_rank(per_page, 0.5), "max": max(per_page) if per_page else None},
            "ms": {"p50": nearest_rank(ms_c, 0.5), "p95": nearest_rank(ms_c, 0.95), "n": len(ms_c)},
            "ms_page": {"p50": nearest_rank(ms_p, 0.5), "p95": nearest_rank(ms_p, 0.95), "n": len(ms_p)}}


def _no_ms(d):
    if isinstance(d, dict): return {k: _no_ms(v) for k, v in d.items() if k not in ("ms", "ms_page")}
    return d


# ---------------------------------------------------------------- determinism (9)
def determinism_view(bench):
    """The fields that must be identical across two runs. A pick bench (mode pick-*) leaves out parts and cases, which depend on
    when a late pick lands, and covers the pick block except ms."""
    gated = bench.get("mode", "gated") == "gated"
    out = {"fixtures": bench.get("fixtures"), "config": bench.get("config"), "mode": bench.get("mode", "gated"), "sets": {}}
    for s, v in (bench.get("sets") or {}).items():
        d = {k: v.get(k) for k in (("boxes", "lines", "parts") if gated else ("boxes", "lines"))}
        if "cases" in v and gated: d["cases"] = [{k: x for k, x in c.items() if k != "first_highlight_ms"} for c in v["cases"]]
        if isinstance(v.get("pick"), dict): d["pick"] = {k: x for k, x in v["pick"].items() if k != "ms"}
        if isinstance(v.get("layout"), dict): d["layout"] = {k: v["layout"].get(k) for k in ("pages", "src", "missing", "reasons")}
        for k in ("kind", "kind_vlm", "image_table_rows", "blockcls"):                   # gap family (phase6-block-classifier.md 12.4)
            if isinstance(v.get(k), dict): d[k] = _no_ms(v[k])
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


def gate(bench, baselines, public_only=False, budget=300, parts_min=None):
    """bench: parsed bench.json; baselines: {set: parsed baseline or None}; parts_min: absolute parts.score floor per set
    (phase5-parts.md 9.1), None = no floor. Returns (exit_code, [output lines])."""
    out = []
    if not isinstance(bench, dict) or bench.get("schema") != "speakeasy-bench/1" or not isinstance(bench.get("sets"), dict) or not bench["sets"]:
        return 2, ["gate: bench is malformed or has a different schema"]
    if bench.get("mode", "gated") != "gated":
        return 2, [f"gate: {bench.get('mode')} bench is never gated (run without --pick)"]
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
        for vk in ("pypdfium2", "onnxruntime", "layout_model"):
            bv = (bl.get("versions") or {}).get(vk); cv = (bench.get("versions") or {}).get(vk)
            if bv != cv: out.append(f"warning: {vk} {bv} in the {s} baseline, {cv} now")
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
        if parts_min is not None:
            cur = blk["parts"]["score"]; bad = cur is None or cur < parts_min; fails += bad
            out.append(f"{s} parts.score min {parts_min:.2f} -> {_fmt(cur)} ({blk['parts'].get('hits')}/{blk['parts'].get('n')}) {'FAIL' if bad else 'PASS'}")
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
        lay = bench["sets"][s].get("layout")
        if isinstance(lay, dict):
            out.append(f"{s} layout pages {lay.get('pages')} detector {(lay.get('src') or {}).get('detector')} heuristic {(lay.get('src') or {}).get('heuristic')} "
                       f"missing {lay.get('missing')} reasons {json.dumps(lay.get('reasons'), sort_keys=True)} ms p50 {(lay.get('ms') or {}).get('p50')} p95 {(lay.get('ms') or {}).get('p95')}")
    if raised: out.append("baseline can be raised (--record-baseline)")
    out.append("GATE PASS" if not fails else f"GATE FAIL ({fails} regressions)")
    return (0 if not fails else 1), out


# ---------------------------------------------------------------- gap-family baseline (B6-pre) + gate (phase6-block-classifier.md 12.1, 14.2)
def baseline_metrics_gap(set_block):
    k, sp = set_block["kind"], set_block["speed"]["first_highlight_ms"]
    return {"kind": {"score": k["score"], "precision": k["precision"], "wrong_type": k["wrong_type"]["total"],
                     "by_gap": {g: v["score"] for g, v in k["by_gap"].items()}, "prose_decoys_kept": k["prose_decoys"]["kept"]},
            "speed": {"p50": sp["p50"], "p95": sp["p95"]}}


def gap_gate(bench, baselines, public_only=False, budget=300):
    """Gap family, gated mode, every set (contract 14.2): kind.score >= 80, kind.precision >= 90, by_gap uncaptioned / lexicon / no-text
    >= 60 each, kind.wrong_type total <= 2, every G2 prose decoy kept, first highlight p50 / p95 < budget. The gain over B6-pre (the
    set's gap baseline) is reported, never gated. Returns (exit_code, [output lines])."""
    G = KIND_GATE; out = []
    if not isinstance(bench, dict) or bench.get("schema") != "speakeasy-bench/1" or not isinstance(bench.get("sets"), dict) or not bench["sets"]:
        return 2, ["gate: bench is malformed or has a different schema"]
    if bench.get("family") != "gap": return 2, ["gate: not a gap-family bench (run_bench.py --family gap)"]
    if bench.get("mode", "gated") != "gated": return 2, [f"gate: {bench.get('mode')} bench is never gated (run without --kind / --ocr / --pick)"]
    if bench.get("complete") is not True: return 2, ["gate: bench is not complete (--cases used or a harness error); refusing to gate"]
    for s, blk in bench["sets"].items():
        try: blk["kind"]["score"]; blk["kind"]["by_gap"]; blk["kind"]["wrong_type"]["total"]; blk["kind"]["prose_decoys"]["n"]; blk["speed"]["first_highlight_ms"]["p50"]
        except (KeyError, TypeError): return 2, [f"gate: bench set {s} is malformed (no kind block)"]
        bl = baselines.get(s)
        if not bl: return 2, [f"gate: no B6-pre baseline for {s}: record one on post-Phase-7 main with run_bench.py --family gap --record-baseline"]
        if bl.get("config") != bench.get("config") or bl.get("manifest_sha256") != (bench.get("fixtures") or {}).get(s, {}).get("manifest_sha256"):
            return 2, [f"baseline stale for {s}: re-record B6-pre with run_bench.py --family gap --record-baseline"]
    fails = 0
    if baselines.get("hidden-gap") and "hidden-gap" not in bench["sets"]:
        if public_only: out.append("warning: hidden-gap set not run (--public-only)")
        else: out.append("hidden-gap set not run  FAIL"); fails += 1
    for s in sorted(bench["sets"]):
        k = bench["sets"][s]["kind"]; base = (baselines[s].get("metrics") or {}).get("kind") or {}
        def line(name, cur, ok, ref=None):
            nonlocal fails
            fails += not ok; gain = "" if ref is None or cur is None else f" (B6-pre {_fmt(ref)}, {cur - ref:+.2f})"
            out.append(f"{s} {name} -> {_fmt(cur) if isinstance(cur, float) or cur is None else cur}{gain} {'PASS' if ok else 'FAIL'}")
        line(f"kind.score min {G['score']:.2f}", k["score"], k["score"] is not None and k["score"] >= G["score"], base.get("score"))
        line(f"kind.precision min {G['precision']:.2f}", k["precision"], k["precision"] is not None and k["precision"] >= G["precision"], base.get("precision"))
        for g in G["by_gap_gated"]:
            v = (k["by_gap"].get(g) or {}).get("score")
            line(f"kind.by_gap.{g} min {G['by_gap_min']:.2f}", v, v is not None and v >= G["by_gap_min"], (base.get("by_gap") or {}).get(g))
        wt = k["wrong_type"]["total"]; line(f"kind.wrong_type max {G['wrong_type_max']}", wt, wt <= G["wrong_type_max"])
        pd = k["prose_decoys"]; line("kind.prose_decoys kept", f"{pd['kept']}/{pd['n']}", pd["kept"] == pd["n"])
        sp = bench["sets"][s]["speed"]["first_highlight_ms"]
        for q in ("p50", "p95"):
            v = sp[q]; bad = v is None or v >= budget; fails += bad
            out.append(f"{s} speed.first_highlight_ms.{q} budget<{budget} -> {v if v is not None else 'null'} {'FAIL' if bad else 'PASS'}")
    out.append("-- informational")
    for s in sorted(bench["sets"]):
        k = bench["sets"][s]["kind"]
        out.append(f"{s} kind by_gap " + " ".join(f"{g} {_fmt(v['score'])} ({v['hits']}/{v['n']})" for g, v in k["by_gap"].items()))
        out.append(f"{s} kind by_kind_src " + " ".join(f"{x} {v['matched']}/{v['pred']}" for x, v in k["by_kind_src"].items()) +
                   f" | unmatched {json.dumps(k['unmatched'], sort_keys=True)} spurious_on_decoy {k['spurious_on_decoy']} ignored {k['ignored']}")
        bc = bench["sets"][s].get("blockcls") or {}
        if bc: out.append(f"{s} blockcls pages {bc.get('pages')} candidates {bc.get('candidates')} by_outcome {json.dumps(bc.get('by_outcome'), sort_keys=True)} "
                          f"ms p50 {(bc.get('ms') or {}).get('p50')} p95 {(bc.get('ms') or {}).get('p95')} page ms p95 {(bc.get('ms_page') or {}).get('p95')}")
    out.append("GATE PASS" if not fails else f"GATE FAIL ({fails} checks)")
    return (0 if not fails else 1), out
