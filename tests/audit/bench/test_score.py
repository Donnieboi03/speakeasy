#!/usr/bin/env python3
"""Scorer and gate self-tests on hand-made predictions (contract section 11). Run: python3 test_score.py (or pytest)."""
import copy, os, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import score as S


# ---------------------------------------------------------------- boxes
def test_one_gt_two_predictions_only_one_hit():
    gt = [{"id": "b1", "kind": "paragraph", "bbox": [0, 0, 100, 100]}]
    pr = [{"kind": "paragraph", "bbox": [0, 0, 100, 100]}, {"kind": "paragraph", "bbox": [0, 0, 100, 95]}]
    c = S.box_counts_page(gt, pr)["paragraph"]
    assert c == {"hits": 1, "gt": 1, "matched_50": 1, "pred": 2}, c
    out = S.score_boxes([("d", "synthetic", {"pages": [{"n": 1, "blocks_scored": True, "blocks": gt}]})], {"d": {1: pr}})
    assert out["score"] == 100.0 and out["precision_50"] == 50.0 and out["recall_50"] == 100.0, out


def test_greedy_prefers_higher_iou():
    gt = [("g1", [0, 0, 100, 100])]; pr = [[0, 0, 100, 80], [0, 0, 100, 100]]
    assert S.match(gt, pr, 0.5) == {0: 1}


def test_prediction_on_heading_is_ignored():
    gt = [{"id": "b1", "kind": "heading", "bbox": [0, 0, 100, 10]}, {"id": "b2", "kind": "paragraph", "bbox": [0, 20, 100, 100]}]
    pr = [{"kind": "paragraph", "bbox": [0, 0, 100, 10]}, {"kind": "paragraph", "bbox": [0, 20, 100, 100]}]
    c = S.box_counts_page(gt, pr)["paragraph"]
    assert c["pred"] == 1 and c["matched_50"] == 1 and c["hits"] == 1, c


def test_kind_mismatch_is_not_a_hit():
    gt = [{"id": "b1", "kind": "figure", "bbox": [0, 0, 100, 100]}]
    pr = [{"kind": "table", "bbox": [0, 0, 100, 100]}]
    c = S.box_counts_page(gt, pr)
    assert c["figure"]["hits"] == 0 and c["table"]["pred"] == 1 and c["table"]["matched_50"] == 0


def test_unscored_page_and_hit_threshold():
    gt = {"pages": [{"n": 1, "blocks_scored": False, "blocks": [{"id": "x", "kind": "paragraph", "bbox": [0, 0, 10, 10]}]},
                    {"n": 2, "blocks_scored": True, "blocks": [{"id": "y", "kind": "caption", "bbox": [0, 0, 100, 100]}]}]}
    pr = {"d": {1: [{"kind": "paragraph", "bbox": [0, 0, 10, 10]}], 2: [{"kind": "caption", "bbox": [0, 0, 100, 70]}]}}
    out = S.score_boxes([("d", "real", gt)], pr)
    assert out["gt"] == 1 and out["hits"] == 0 and out["matched_50"] == 1 and out["pred"] == 1, out


# ---------------------------------------------------------------- lines
L = [[0, 10 * i, 100, 10 * i + 8] for i in range(10)]  # one column of 10 lines


def show(t, page=1, cue="text_band", kind="line"):
    return {"cue_kind": cue, "doc": {"kind": kind, "page": page, "target": t}}


def sent(idx):
    return {"page": 1, "line_rects": [L[i] for i in idx]}


def band(i, j):
    return [0, 10 * i, 100, 10 * j + 8]


def test_lines_coverage_boundaries():
    # 1 line needs 1; 2 need 2; 4 need 3; 5 need 4
    assert S.score_line_sentence(sent([2]), L, [show(band(2, 2))]) == (True, None)
    assert S.score_line_sentence(sent([2, 3]), L, [show(band(2, 2))])[1] == "under_cover"
    assert S.score_line_sentence(sent([2, 3]), L, [show(band(2, 3))]) == (True, None)
    assert S.score_line_sentence(sent([2, 3, 4, 5]), L, [show(band(2, 4))]) == (True, None)
    assert S.score_line_sentence(sent([2, 3, 4, 5]), L, [show(band(2, 3))])[1] == "under_cover"
    assert S.score_line_sentence(sent([2, 3, 4, 5, 6]), L, [show(band(2, 5))]) == (True, None)
    assert S.score_line_sentence(sent([2, 3, 4, 5, 6]), L, [show(band(2, 4))])[1] == "under_cover"


def test_lines_over_extension():
    assert S.score_line_sentence(sent([2, 3]), L, [show(band(2, 4))]) == (True, None)
    assert S.score_line_sentence(sent([2, 3]), L, [show(band(1, 4))])[1] == "over_extend"


def test_band_spanning_both_columns_misses():
    left = [[0, 10 * i, 100, 10 * i + 8] for i in range(5)]; right = [[120, 10 * i, 220, 10 * i + 8] for i in range(5)]
    s = {"page": 1, "line_rects": [left[1], left[2]]}
    assert S.score_line_sentence(s, left + right, [show([0, 10, 220, 28])])[1] == "over_extend"


def lshow(rects, target=None, **kw):
    s = show(target if target is not None else [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)], **kw)
    s["doc"]["lines"] = [list(r) for r in rects]; return s


def test_cross_column_per_line_rects_hit_union_misses():
    left = [[0, 10 * i, 100, 10 * i + 8] for i in range(5)]; right = [[120, 10 * i, 220, 10 * i + 8] for i in range(5)]
    s = {"page": 1, "line_rects": [left[4], right[0]]}  # sentence crosses the column break
    union = [0, 0, 220, 48]
    assert S.score_line_sentence(s, left + right, [show(union)])[1] == "over_extend"
    assert S.score_line_sentence(s, left + right, [lshow([left[4], right[0]], target=union)]) == (True, None)


def test_doc_lines_rects_override_target():
    # a target that covers nothing is ignored when doc.lines is non-empty
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[2], L[3]], target=[500, 500, 510, 510])]) == (True, None)
    # doc.lines that misses a line is under_cover even when the target union would cover it
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[2]], target=band(2, 3))])[1] == "under_cover"


def test_doc_lines_empty_or_malformed_falls_back_to_target():
    s = show(band(2, 3)); s["doc"]["lines"] = []
    assert S.score_line_sentence(sent([2, 3]), L, [s]) == (True, None)
    s = show(band(2, 2)); s["doc"]["lines"] = [[0, 1], "x", None]
    assert S.score_line_sentence(sent([2, 3]), L, [s])[1] == "under_cover"
    s = {"cue_kind": "text_band", "doc": {"kind": "line", "page": 1, "lines": []}}
    assert S.score_line_sentence(sent([2]), L, [s]) == (False, "no_doc_target")


def test_doc_lines_over_extension_counts_lines_not_rects():
    # one extra line covered by two rects of the same chunk is still one extra line (allowed)
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[2], L[3], L[4], band(4, 4)])]) == (True, None)
    # two extra non-adjacent lines in one chunk -> over_extend
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[2], L[3], L[5], L[7]])])[1] == "over_extend"
    # extras are counted per chunk: one extra each in two chunks passes
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[1], L[2]]), lshow([L[3], L[4]])]) == (True, None)


def test_doc_lines_mixed_with_legacy_chunks():
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[2]]), show(band(3, 3))]) == (True, None)
    assert S.score_line_sentence(sent([2, 3]), L, [lshow([L[2]]), show(band(3, 3), cue="figure_box", kind="figure")])[1] == "wrong_cue"


def test_split_chunks_cover_together():
    assert S.score_line_sentence(sent([2, 3]), L, [show(band(2, 2)), show(band(3, 3))]) == (True, None)


def test_line_miss_reason_order():
    s = sent([2])
    assert S.score_line_sentence(s, L, []) == (False, "not_spoken")
    assert S.score_line_sentence(s, L, [None]) == (False, "no_show")
    assert S.score_line_sentence(s, L, [{"cue_kind": "text_band", "doc": None}]) == (False, "no_doc_target")
    assert S.score_line_sentence(s, L, [show(band(2, 2), cue="figure_box", kind="figure")]) == (False, "wrong_cue")
    assert S.score_line_sentence(s, L, [show(band(2, 2), page=2)]) == (False, "wrong_page")


# ---------------------------------------------------------------- parts
P = {"page": 1, "bbox": [10, 10, 50, 50]}


def oval(cx, cy, page=1):
    return {"cue_kind": "part_oval", "doc": {"kind": "object", "page": page, "target": [cx - 5, cy - 5, cx + 5, cy + 5]}}


def test_part_centre_on_edge_hits_and_outside_misses():
    assert S.score_part_sentence(P, [oval(50, 30)]) == (True, None)
    assert S.score_part_sentence(P, [oval(50.1, 30)]) == (False, "centre_outside")
    assert S.score_part_sentence(P, [oval(30, 30, page=2)]) == (False, "wrong_page")
    assert S.score_part_sentence(P, [show(band(1, 1))]) == (False, "no_oval")
    assert S.score_part_sentence(P, [None]) == (False, "no_show")
    assert S.score_part_sentence(P, []) == (False, "not_spoken")


def test_parts_precision_with_stray_oval():
    shows = {0: oval(30, 30), 1: oval(200, 200)}  # chunk 1 belongs to a sentence that is not parts-scored
    owner = {0: P, 1: None}
    good = sum(1 for k, s in shows.items() if S.oval_good(s, owner[k])); total = len(shows)
    assert (good, total, S.pct(good, total)) == (1, 2, 50.0)


# ---------------------------------------------------------------- attribution, percentiles
def test_attribution_by_text_position():
    script = [("s1", "Alpha beta gamma."), ("s2", "Delta epsilon.")]
    maps = [{"sent": 0, "text": "Alpha beta"}, {"sent": 1, "text": "gamma."}, {"sent": 2, "text": "Delta  epsilon."}]
    assert S.attribute(script, maps) == {0: "s1", 1: "s1", 2: "s2"}
    try: S.attribute(script, [{"sent": 0, "text": "Zeta."}]); assert False
    except S.AttributionError as e: assert "chunk 0 not in script" in str(e)


def test_nearest_rank_percentiles():
    v = [5, 1, 4, 2, 3]
    assert S.nearest_rank(v, 0.5) == 3 and S.nearest_rank(v, 0.95) == 5 and S.nearest_rank([7], 0.95) == 7 and S.nearest_rank([], 0.5) is None
    assert S.nearest_rank(list(range(1, 101)), 0.95) == 95


# ---------------------------------------------------------------- gate
def bench_and_baseline(**m):
    blk = {"boxes": {"score": 50.0, "precision_50": 60.0, "recall_50": 70.0, "by_kind": {k: {"score": 1.0} for k in S.SCORED_KINDS}, "by_subset": {}},
           "lines": {"score": 40.0, "by_subset": {}}, "parts": {"score": 30.0, "precision": 80.0, "by_subset": {}, "by_style": {}},
           "speed": {"first_highlight_ms": {"p50": 50, "p95": 100, "n": 3}}}
    bench = {"schema": "speakeasy-bench/1", "complete": True, "config": {"x": 1}, "versions": {"pypdfium2": "1"},
             "fixtures": {"public": {"manifest_sha256": "abc"}}, "sets": {"public": blk}}
    base = {"config": {"x": 1}, "manifest_sha256": "abc", "versions": {"pypdfium2": "1"}, "metrics": S.baseline_metrics(blk), "detail": S.baseline_detail(blk, True)}
    return bench, base


def test_gate_rules():
    b, bl = bench_and_baseline()
    assert S.gate(b, {"public": bl})[0] == 0
    x = copy.deepcopy(b); x["sets"]["public"]["lines"]["score"] = 39.0          # exactly -1.00: pass
    assert S.gate(x, {"public": bl})[0] == 0
    x["sets"]["public"]["lines"]["score"] = 38.99                                # -1.01: fail
    assert S.gate(x, {"public": bl})[0] == 1
    x = copy.deepcopy(b); x["sets"]["public"]["lines"]["score"] = 38.5           # injected regression of 1.5
    code, out = S.gate(x, {"public": bl}); assert code == 1 and out[-1] == "GATE FAIL (1 regressions)", out
    x = copy.deepcopy(b); x["complete"] = False; assert S.gate(x, {"public": bl})[0] == 2
    x = copy.deepcopy(b); x["schema"] = "other"; assert S.gate(x, {"public": bl})[0] == 2
    assert S.gate(b, {"public": None})[0] == 2
    st = copy.deepcopy(bl); st["manifest_sha256"] = "zzz"; code, out = S.gate(b, {"public": st}); assert code == 2 and "baseline stale for public" in out[0]
    st = copy.deepcopy(bl); st["config"] = {"x": 2}; assert S.gate(b, {"public": st})[0] == 2
    code, out = S.gate(b, {"public": bl, "hidden": bl}); assert code == 1 and any("hidden set not run" in l for l in out)
    assert S.gate(b, {"public": bl, "hidden": bl}, public_only=True)[0] == 0
    x = copy.deepcopy(b); x["sets"]["public"]["speed"]["first_highlight_ms"]["p95"] = 300; assert S.gate(x, {"public": bl})[0] == 1
    x = copy.deepcopy(b); x["sets"]["public"]["speed"]["first_highlight_ms"]["p50"] = 299; assert S.gate(x, {"public": bl})[0] == 0
    x = copy.deepcopy(b); x["sets"]["public"]["parts"]["score"] = None; assert S.gate(x, {"public": bl})[0] == 1
    nb = copy.deepcopy(bl); nb["metrics"]["parts"]["score"] = None; assert S.gate(b, {"public": nb})[0] == 0
    x = copy.deepcopy(b); x["sets"]["public"]["boxes"]["score"] = 60.0
    code, out = S.gate(x, {"public": bl}); assert code == 0 and "baseline can be raised (--record-baseline)" in out
    pv = copy.deepcopy(bl); pv["versions"]["pypdfium2"] = "0"; code, out = S.gate(b, {"public": pv}); assert code == 0 and out[0].startswith("warning: pypdfium2")


def test_determinism_view_ignores_speed():
    b, _ = bench_and_baseline(); b["sets"]["public"]["cases"] = [{"case_id": "c", "first_highlight_ms": 1, "lines": {"hits": 1}}]
    b2 = copy.deepcopy(b); b2["sets"]["public"]["speed"]["first_highlight_ms"]["p50"] = 99; b2["sets"]["public"]["cases"][0]["first_highlight_ms"] = 7
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == []
    b2["sets"]["public"]["cases"][0]["lines"]["hits"] = 0
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == ["$.sets.public.cases[0].lines.hits"]


# ---------------------------------------------------------------- layout detector (phase4-layout.md section 7)
def test_layout_summary_and_coverage():
    det = {"src": "detector", "ms": 30, "detector_version": "v", "blocks_by_kind": {}}
    pls = [det, dict(det, ms=10), {"src": "heuristic", "reason": "inference-failed"}, None]
    lay = S.layout_summary(pls)
    assert lay["pages"] == 4 and lay["src"] == {"detector": 2, "heuristic": 1} and lay["missing"] == 1, lay
    assert lay["reasons"] == {"inference-failed": 1} and lay["ms"] == {"p50": 10, "p95": 30, "n": 2}, lay
    assert S.layout_coverage_errors("public", S.layout_summary([det, det])) == []
    assert "1 of 4 pages heuristic" in S.layout_coverage_errors("public", lay)[0]
    assert S.layout_coverage_errors("public", S.layout_summary([None]))           # pre-Phase-4 output in a detector run
    assert S.layout_coverage_errors("public", S.layout_summary([]))


def test_determinism_view_covers_layout_src_not_ms():
    b, _ = bench_and_baseline(); b["sets"]["public"]["layout"] = S.layout_summary([{"src": "detector", "ms": 5}])
    b2 = copy.deepcopy(b); b2["sets"]["public"]["layout"]["ms"]["p50"] = 99
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == []
    b2["sets"]["public"]["layout"]["src"]["heuristic"] = 1
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == ["$.sets.public.layout.src.heuristic"]


def test_gate_warns_on_layout_versions():
    b, bl = bench_and_baseline()
    x = copy.deepcopy(b); x["versions"].update(onnxruntime="1.31.0", layout_model="pp-doclayout-s/33688dbee1c2")
    code, out = S.gate(x, {"public": bl})
    assert code == 0 and "warning: onnxruntime None in the public baseline, 1.31.0 now" in out, out
    assert "warning: layout_model None in the public baseline, pp-doclayout-s/33688dbee1c2 now" in out, out
    x["sets"]["public"]["layout"] = S.layout_summary([{"src": "heuristic", "reason": "model-missing"}])
    code, out = S.gate(x, {"public": bl})
    assert code == 0 and any(l.startswith("public layout pages 1 detector 0 heuristic 1") for l in out), out


# ---------------------------------------------------------------- boxes wrong type (informational)
def test_wrong_type_counts_right_box_wrong_kind():
    gt = [{"id": "f", "kind": "figure", "bbox": [0, 0, 100, 100]}, {"id": "p", "kind": "paragraph", "bbox": [0, 200, 100, 300]},
          {"id": "h", "kind": "heading", "bbox": [0, 400, 100, 420]}]
    pr = [{"kind": "table", "bbox": [0, 0, 100, 95]},        # figure GT called table -> wrong type
          {"kind": "paragraph", "bbox": [0, 200, 100, 300]},  # hit
          {"kind": "paragraph", "bbox": [0, 400, 100, 420]},  # heading GT called paragraph -> wrong type (any GT kind)
          {"kind": "caption", "bbox": [0, 0, 100, 60]}]       # IoU 0.6 with the figure: not counted
    assert S.wrong_type_page(gt, pr) == {"table": {"figure": 1}, "paragraph": {"heading": 1}}
    out = S.score_boxes([("d", "synthetic", {"pages": [{"n": 1, "blocks_scored": True, "blocks": gt}]})], {"d": {1: pr}})
    assert out["wrong_type"]["table"] == {"n": 1, "as": {"figure": 1}} and out["wrong_type"]["caption"] == {"n": 0, "as": {}}, out["wrong_type"]
    assert out["score"] == 50.0  # the wrong-type report changes no score


def test_wrong_type_skips_own_kind_hits():
    gt = [{"id": "a", "kind": "figure", "bbox": [0, 0, 100, 100]}, {"id": "b", "kind": "table", "bbox": [0, 0, 100, 98]}]
    assert S.wrong_type_page(gt, [{"kind": "figure", "bbox": [0, 0, 100, 100]}]) == {}


# ---------------------------------------------------------------- pick mode (phase5-parts.md 7.2)
PART = {"id": "pt1", "page": 2, "bbox": [10, 10, 50, 50], "style": "callout"}
SENTS = {"s1": {"id": "s1", "score": "parts", "part": "pt1"}, "s2": {"id": "s2", "score": "lines"}}
IN, OUT = [20, 20, 30, 30], [100, 100, 120, 120]


def pk(sent, outcome="accepted", ci=0, rects=(IN, OUT), called=True, ms=400):
    return {"state": "part_pick", "sent": sent, "outcome": outcome, "chosen_index": ci, "cand_rects": [list(r) for r in rects], "called": called, "ms": ms}


def fbox(page=2):
    return {"cue_kind": "figure_box", "doc": {"kind": "figure", "page": page, "target": [0, 0, 200, 200]}}


def test_pick_eval_correct_wrong_and_non_parts():
    owner = {0: "s1", 1: "s1", 2: "s2"}; cshow = {0: fbox(), 1: fbox(), 2: fbox()}
    ev = S.pick_eval([pk(0), pk(1, ci=1), pk(2)], owner, SENTS, {"pt1": PART}, cshow)
    assert [e["correct"] for e in ev] == [True, False, False] and [e["on_parts"] for e in ev] == [True, True, False]
    assert [e["cand_hit"] for e in ev] == [True, True, False]
    s = S.pick_summary(ev, {"hits": 0, "n": 0, "good": 0, "ovals": 0})
    assert (s["accepted"], s["correct"], s["precision"], s["recall"], s["candidate_recall"]) == (3, 1, 33.33, 50.0, 100.0), s


def test_pick_eval_wrong_page_bad_index_and_outcomes():
    owner = {0: "s1", 1: "s1", 2: "s1", 3: "s1"}; cshow = {0: fbox(page=3), 1: fbox(), 2: fbox(), 3: fbox()}
    ev = S.pick_eval([pk(0), pk(1, ci=5), pk(2, outcome="low-confidence"), pk(3, outcome="no-logprobs", ci=None, rects=(), called=True, ms=3)],
                     owner, SENTS, {"pt1": PART}, cshow)
    assert [e["correct"] for e in ev] == [False] * 4 and ev[1]["bad"] and not ev[2]["bad"] and not ev[0]["cand_hit"]
    s = S.pick_summary(ev, {"hits": 0, "n": 0, "good": 0, "ovals": 0}, replay_miss=2)
    assert s["by_outcome"]["accepted"] == 2 and s["by_outcome"]["low-confidence"] == 1 and s["by_outcome"]["no-logprobs"] == 1 and s["by_outcome"]["timeout"] == 0
    assert s["stale"] is True and s["replay_miss"] == 2 and s["bad_records"] == 1 and s["recall"] == 0.0 and s["ms"]["n"] == 4


def test_pick_unknown_owner_counts_as_non_parts():
    ev = S.pick_eval([pk(9)], {}, SENTS, {"pt1": PART}, {})
    assert ev[0]["on_parts"] is False and ev[0]["correct"] is False
    s = S.pick_summary(ev, {"hits": 0, "n": 0, "good": 0, "ovals": 0})
    assert s["precision"] == 0.0 and s["recall"] is None


def test_parts_with_pick_replaces_fallback_and_counts_strays():
    sents = {"s1": SENTS["s1"], "s2": SENTS["s2"]}; owner = {0: "s1", 1: "s2"}; chunks_of = {"s1": [0], "s2": [1]}
    cshow = {0: fbox(), 1: fbox()}
    base = S.parts_counts(["s1", "s2"], sents, {"pt1": PART}, chunks_of, cshow, owner)
    assert base == {"hits": 0, "n": 1, "good": 0, "ovals": 0}
    ev = S.pick_eval([pk(0), pk(1)], owner, sents, {"pt1": PART}, cshow)
    pw = S.parts_counts(["s1", "s2"], sents, {"pt1": PART}, chunks_of, S.shows_with_picks(cshow, ev), owner)
    assert pw == {"hits": 1, "n": 1, "good": 1, "ovals": 2}, pw  # the pick on the lines sentence is a stray oval
    lc = S.pick_eval([pk(0, outcome="low-confidence")], owner, sents, {"pt1": PART}, cshow)
    assert S.shows_with_picks(cshow, lc) == cshow  # only accepted picks draw


def test_determinism_view_pick_mode():
    b, _ = bench_and_baseline(); b["mode"] = "pick-replay"
    b["sets"]["public"]["pick"] = {"requests": 3, "ms": {"p50": 1}}; b["sets"]["public"]["cases"] = [{"case_id": "c", "parts": {"hits": 1}}]
    b2 = copy.deepcopy(b); b2["sets"]["public"]["pick"]["ms"]["p50"] = 9; b2["sets"]["public"]["parts"]["score"] = 99.0
    b2["sets"]["public"]["cases"][0]["parts"]["hits"] = 0
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == []
    b2["sets"]["public"]["pick"]["requests"] = 4
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == ["$.sets.public.pick.requests"]
    g, _ = bench_and_baseline(); g2 = copy.deepcopy(g); g2["sets"]["public"]["parts"]["score"] = 99.0
    assert S.diff_paths(S.determinism_view(g), S.determinism_view(g2)) == ["$.sets.public.parts.score"]


def test_gate_refuses_pick_bench_and_parts_floor():
    b, bl = bench_and_baseline()
    x = copy.deepcopy(b); x["mode"] = "pick-replay"; code, out = S.gate(x, {"public": bl}); assert code == 2 and "never gated" in out[0], out
    x = copy.deepcopy(b); x["mode"] = "gated"; assert S.gate(x, {"public": bl})[0] == 0
    x = copy.deepcopy(b); x["sets"]["public"]["parts"].update(score=70.0, hits=46, n=65); bl2 = copy.deepcopy(bl); bl2["metrics"]["parts"]["score"] = 63.08
    code, out = S.gate(x, {"public": bl2}, parts_min=70.0); assert code == 0 and "public parts.score min 70.00 -> 70.00 (46/65) PASS" in out, out
    x["sets"]["public"]["parts"]["score"] = 69.23; code, out = S.gate(x, {"public": bl2}, parts_min=70.0)
    assert code == 1 and any(l.startswith("public parts.score min 70.00 -> 69.23") and l.endswith("FAIL") for l in out), out
    assert S.gate(x, {"public": bl2})[0] == 0  # no floor without parts_min


def _mock(env_extra):
    import json, socket, subprocess, tempfile, time, urllib.request
    d = tempfile.mkdtemp(prefix="se_mock_t_"); pf = os.path.join(d, "port"); lg = os.path.join(d, "log.jsonl")
    e = {k: v for k, v in os.environ.items() if not k.startswith("MOCK_")}
    e.update(MOCK_PORT="0", MOCK_PORT_FILE=pf, MOCK_LOG=lg, **env_extra)
    p = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(HERE), "stream", "mock_llm.py")], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while not os.path.exists(pf) and time.time() - t0 < 5: time.sleep(0.02)
    port = int(open(pf).read())
    def post(body):
        rq = urllib.request.Request(f"http://127.0.0.1:{port}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=5) as r: return json.loads(r.read())
    return p, post, lg, d


def test_mock_pick_replay_none_and_fixed():
    import base64, hashlib, json, math
    png = b"\x89PNG fake"; body = {"model": "qwen3-vl:4b-instruct", "stream": False, "logprobs": True, "top_logprobs": 20,
                                    "messages": [{"role": "user", "content": "pick a letter", "images": [base64.b64encode(png).decode()]}]}
    key = hashlib.sha256(b"qwen3-vl:4b-instruct\x00pick a letter\x00" + hashlib.sha256(png).hexdigest().encode()).hexdigest()
    p, post, lg, d = _mock({})
    try:
        r = post(body); assert "logprobs" not in r and r["done"] is True, r                      # MOCK_PICK=none: no logprobs
        assert post({"model": "m", "messages": []})["done"] is True                             # load-only prewarm
    finally: p.terminate(); p.wait()
    cas = os.path.join(d, "cas.json")
    json.dump({"header": {}, "entries": {key: {"top_logprobs": [{"token": "B", "logprob": math.log(0.9)}, {"token": "<other>", "logprob": math.log(0.1)}]}}}, open(cas, "w"))
    p, post, lg, d2 = _mock({"MOCK_PICK": "replay", "MOCK_PICK_CASSETTE": cas})
    try:
        r = post(body); tl = r["logprobs"][0]["top_logprobs"]
        assert [e["token"] for e in tl] == ["B", "<other>"] and r["message"]["content"] == "B", r
        r2 = post(dict(body, messages=[dict(body["messages"][0], content="other prompt")])); assert "logprobs" not in r2
        res = [json.loads(x)["result"] for x in open(lg) if '"pick_reply"' in x]; assert res == ["hit", "miss"], res
    finally: p.terminate(); p.wait()
    p, post, lg, d3 = _mock({"MOCK_PICK": "fixed", "MOCK_PICK_TOP_LOGPROBS": json.dumps([{"token": " a", "logprob": -0.1}])})
    try: assert post(body)["logprobs"][0]["top_logprobs"] == [{"token": " a", "logprob": -0.1}]
    finally: p.terminate(); p.wait()


def test_mock_letter_entries_keep_letters_only():
    import importlib.util, math
    src = open(os.path.join(os.path.dirname(HERE), "stream", "mock_llm.py")).read().split("\nclass H(")[0]
    ns = {}; exec(compile(src, "mock_llm_head", "exec"), ns)
    out = ns["letter_entries"]([{"token": "A", "logprob": math.log(0.5)}, {"token": " A", "logprob": math.log(0.2)}, {"token": "The", "logprob": math.log(0.2)},
                                {"token": "a", "logprob": math.log(0.05)}, {"token": "Q", "logprob": math.log(0.05)}])
    assert [e["token"] for e in out] == ["A", " A", "<other>"] and abs(math.exp(out[-1]["logprob"]) - 0.3) < 1e-4, out


# ---------------------------------------------------------------- hard invariants (phase5-parts.md A2.11, A4.1)
def test_case_invariants():
    case = {"page": 1, "selection": [{"text": "Figure 1: Two stages. Stage one", "rect": [10, 10, 200, 20]}, {"text": "is left.", "rect": [10, 22, 80, 32]}]}
    figs = {1: [{"bbox": [0, 100, 400, 300], "panels": [[0, 100, 200, 300], [200, 100, 400, 300]]}]}
    fm = {"doc_figure_mode": True}
    def sh(t, cue="part_oval", src=None):
        return {"cue_kind": cue, "doc": {"page": 1, "target": t, "target_src": src}}
    inv = S.case_invariants(case, fm, [sh([10, 10, 200, 32], cue="text_band")], figs)
    assert inv["stand_in"] == 1 and inv["figure_mode_runs"] == 1, inv                 # band over the whole selection in figure mode
    assert S.case_invariants(case, {}, [sh([10, 10, 200, 32], cue="text_band")], figs)["stand_in"] == 0     # read-through: not A2.3
    al = dict(sh([10, 10, 200, 32], cue="text_band"), sent=3)
    assert S.case_invariants(case, fm, [al], figs, [{"sent": 3, "method": "align"}])["stand_in"] == 0       # verbatim sentence band
    assert S.case_invariants(case, fm, [al], figs, [{"sent": 3, "method": "block"}])["stand_in"] == 1       # whole-block stand-in
    one = dict(case, selection=[{"text": "One sentence only.", "rect": [10, 10, 200, 20]}])
    assert S.case_invariants(one, fm, [sh([10, 10, 200, 20], cue="text_band")], figs)["stand_in"] == 0
    assert S.case_invariants(case, fm, [sh([0, 100, 400, 300], cue="figure_box")], figs) == {"stand_in": 0, "hairline_draw": 0, "callout_oversize": 0, "figure_mode_runs": 1}
    assert S.case_invariants(case, fm, [sh([0, 300, 400, 301], cue="figure_box")], figs)["hairline_draw"] == 1
    assert S.case_invariants(case, fm, [sh([10, 110, 190, 290], src="callout")], figs)["callout_oversize"] == 1      # 81 % of panel a, 20 % of the figure
    assert S.case_invariants(case, fm, [sh([20, 120, 60, 160], src="callout-marker")], figs)["callout_oversize"] == 0
    assert S.case_invariants(case, fm, [sh([10, 110, 190, 290], src="pick")], figs)["callout_oversize"] == 0          # only callout targets are pinned


def test_out_of_scope_is_a_pick_outcome():
    ev = S.pick_eval([pk(0, outcome="out-of-scope", ci=None)], {0: "s1"}, SENTS, {"pt1": PART}, {0: fbox()})
    assert S.pick_summary(ev, {"hits": 0, "n": 0, "good": 0, "ovals": 0})["by_outcome"]["out-of-scope"] == 1


if __name__ == "__main__":
    fails = 0; tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try: f(); print(f"PASS {n}")
        except AssertionError as e: fails += 1; print(f"FAIL {n}: {e}")
    print(f"{len(tests) - fails}/{len(tests)} passed"); sys.exit(1 if fails else 0)
