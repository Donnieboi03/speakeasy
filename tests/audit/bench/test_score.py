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


# ---------------------------------------------------------------- kind (phase6-block-classifier.md 12.3)
def gblk(i, kind, bbox, gap="uncaptioned", **kw):
    return dict({"id": i, "kind": kind, "bbox": bbox, "gap": gap, "caption_style": "none", "raster": False}, **kw)


def kpage(blocks, decoys=()):
    return {"n": 1, "w": 720, "h": 405, "blocks_scored": True, "kind_scored": True, "scan": False, "lines": [], "blocks": blocks, "prose_decoys": list(decoys)}


def test_kind_match_at_half_iou_and_precision():
    gt = {"pages": [kpage([gblk("f", "figure", [0, 0, 100, 100]), gblk("t", "table", [200, 0, 300, 100], gap="lexicon")])], "areas": []}
    pr = {"d": {1: [{"kind": "figure", "bbox": [0, 0, 100, 50]}, {"kind": "table", "bbox": [200, 0, 300, 49]}]}}   # IoU 0.50 and 0.49
    k = S.score_kind([("d", gt)], pr)
    assert (k["score"], k["hits"], k["gt"], k["precision"], k["matched"], k["pred"]) == (50.0, 1, 2, 50.0, 1, 2), k
    assert k["by_gap"]["uncaptioned"] == {"score": 100.0, "hits": 1, "n": 1} and k["by_gap"]["lexicon"]["score"] == 0.0, k["by_gap"]
    assert k["unmatched"]["on_nothing"] == 1 and k["wrong_type"]["total"] == 0, k


def test_kind_wrong_type_decoy_text_ignored_and_kind_src():
    blocks = [gblk("f", "figure", [0, 0, 100, 100]), gblk("t", "table", [200, 0, 300, 100]),
              {"id": "d", "kind": "decoration", "bbox": [400, 0, 480, 40], "decoy": "logo"}, {"id": "p", "kind": "paragraph", "bbox": [0, 200, 300, 260]},
              {"id": "a", "kind": "other", "bbox": [400, 200, 600, 300]}]
    pr = [{"kind": "table", "bbox": [0, 0, 100, 100], "kind_src": "classifier"},        # GT figure typed as table
          {"kind": "table", "bbox": [200, 0, 300, 100], "kind_src": "classifier"},      # hit
          {"kind": "figure", "bbox": [400, 0, 480, 40], "kind_src": "vlm"},             # on a logo
          {"kind": "figure", "bbox": [10, 210, 120, 250], "kind_src": "classifier"},    # on prose
          {"kind": "figure", "bbox": [400, 200, 600, 300]},                             # on an out-of-scope algorithm box: ignored
          {"kind": "caption", "bbox": [0, 0, 100, 100]}]                                # not a kind prediction
    k = S.score_kind([("d", {"pages": [kpage(blocks)], "areas": []})], {"d": {1: pr}})
    assert k["wrong_type"] == {"figure_as_table": 1, "table_as_figure": 0, "total": 1}, k["wrong_type"]
    assert k["spurious_on_decoy"] == 1 and k["unmatched"] == {"wrong_type": 1, "on_decoy": 1, "on_text": 1, "on_nothing": 0}, k["unmatched"]
    assert k["ignored"] == 1 and k["pred"] == 4 and k["matched"] == 1 and k["precision"] == 25.0 and k["score"] == 50.0, k
    assert k["by_kind_src"]["classifier"] == {"pred": 3, "matched": 1, "precision": 33.33} and k["by_kind_src"]["vlm"]["pred"] == 1, k["by_kind_src"]
    assert k["by_kind"]["figure"]["score"] == 0.0 and k["by_kind"]["table"]["score"] == 100.0, k["by_kind"]


def test_kind_one_to_one_and_unscored_pages():
    gt = {"pages": [kpage([gblk("f", "figure", [0, 0, 100, 100])]), dict(kpage([gblk("g", "figure", [0, 0, 100, 100])]), n=2, kind_scored=False)], "areas": []}
    pr = {"d": {1: [{"kind": "figure", "bbox": [0, 0, 100, 100]}, {"kind": "figure", "bbox": [0, 0, 100, 90]}], 2: [{"kind": "figure", "bbox": [500, 0, 600, 50]}]}}
    k = S.score_kind([("d", gt)], pr)
    assert k["hits"] == 1 and k["gt"] == 1 and k["pred"] == 2 and k["precision"] == 50.0, k      # duplicate box costs precision; page 2 unscored


def test_kind_area_predictions_in_capture_pixels():
    gt = {"pages": [], "areas": [{"id": "a1", "w": 1000, "h": 700, "kind_scored": True, "blocks": [gblk("a1b1", "figure", [40, 140, 480, 600], gap="no-text"),
                                                                                                    gblk("a1b2", "table", [540, 160, 960, 620], gap="no-text")]}]}
    ak = {"figure": [[42, 150, 470, 590]], "table": [], "outcomes": {"accepted": 1}, "candidates": 2, "ms": 30}
    k = S.score_kind([("d", gt)], {}, {("d", "a1"): ak})
    assert k["by_anchor"]["area"]["hits"] == 1 and k["by_anchor"]["area"]["gt"] == 2 and k["by_gap"]["no-text"] == {"score": 50.0, "hits": 1, "n": 2}, k
    assert S.score_kind([("d", gt)], {}, {("d", "a1"): None})["hits"] == 0          # run without area_kinds: every area region missed
    assert S.score_kind([("d", gt)], {}, {})["gt"] == 0                             # area case not run (--cases): not scored


def test_kind_prose_decoys():
    line = [56, 82, 400, 94]
    gt = {"pages": [kpage([], decoys=[line, [56, 300, 400, 312]])], "areas": []}
    pr = {"d": {1: [{"kind": "caption", "bbox": [56, 82, 400, 106]}, {"kind": "paragraph", "bbox": [56, 290, 500, 340]}]}}
    pd = S.score_kind([("d", gt)], pr)["prose_decoys"]
    assert pd == {"n": 2, "kept": 1, "broken": 1}, pd
    pr = {"d": {1: [{"kind": "figure", "bbox": [0, 0, 600, 200]}]}}                 # a figure swallowing the decoy line breaks it too
    assert S.score_kind([("d", gt)], pr)["prose_decoys"]["broken"] == 1


def test_row_sentence_scoring():
    snt = {"page": 1, "row_rect": [100, 200, 400, 212]}
    row = {"cue_kind": "text_band", "doc": {"kind": "table_row", "page": 1, "target": [100, 199, 400, 213], "target_src": "table-rows"}}
    assert S.score_row_sentence(snt, [row]) == (True, None)
    assert S.score_row_sentence(snt, [dict(row, doc=dict(row["doc"], target=[100, 230, 400, 242]))]) == (False, "low_iou")
    assert S.score_row_sentence(snt, [dict(row, doc=dict(row["doc"], page=2))]) == (False, "wrong_page")
    assert S.score_row_sentence(snt, [{"cue_kind": "figure_box", "doc": {"kind": "table", "page": 1, "target": [90, 150, 410, 300], "target_src": "table:Table 2"}}]) == (False, "no_row_show")
    assert S.score_row_sentence(snt, [None]) == (False, "no_show") and S.score_row_sentence(snt, []) == (False, "not_spoken")
    assert S.rows_summary(0, 0, {}, reason="gated")["not_scored"] == "gated"


def test_blockcls_summary_counts_only():
    parse = {"blockcls": {"status": "ok", "classifier_version": "docling-figcls-v2.5/27ffc48c27ae", "reason": None, "T_B": 0.8, "T_C": 0.8},
             "pages": [{"n": 1, "kinds": {"candidates": 3, "accepted": {"figure": 1, "table": 1}, "routed": 1, "rejected": 0, "skipped": {}, "ms": 31, "no_text": False}},
                       {"n": 2, "kinds": {"candidates": 0, "accepted": {"figure": 0, "table": 0}, "routed": 0, "rejected": 0, "skipped": {}, "ms": 0, "no_text": False}}],
             "blockcls_events": [{"event": "block_kind", "stage": "classifier", "outcome": "accepted", "ms": 9}, {"event": "block_kind", "stage": "classifier", "outcome": "accepted", "ms": 11},
                                 {"event": "block_kind", "stage": "classifier", "outcome": "pending", "ms": 10}]}
    b = S.blockcls_summary([parse, {"pages": []}])
    assert b["pages"] == 2 and b["candidates"] == 3 and b["by_stage"] == {"classifier": 3} and b["by_outcome"] == {"accepted": 2, "pending": 1}, b
    assert b["status"] == {"None": 1, "ok": 1} and b["ms"] == {"p50": 10, "p95": 11, "n": 3} and b["ms_page"]["n"] == 1, b
    assert "ms" not in S._no_ms(b) and "ms_page" not in S._no_ms(b)


def gap_bench(score=85.0, precision=95.0, by_gap=None, wt=0, kept=(4, 4), p95=120):
    by_gap = by_gap or {"uncaptioned": 80.0, "lexicon": 90.0, "no-text": 70.0, "image-table": 100.0, "standard": 100.0}
    k = {"score": score, "precision": precision, "hits": 40, "gt": 47, "matched": 40, "pred": 42, "by_gap": {g: {"score": v, "hits": 1, "n": 1} for g, v in by_gap.items()},
         "by_kind_src": {}, "wrong_type": {"figure_as_table": wt, "table_as_figure": 0, "total": wt}, "unmatched": {}, "spurious_on_decoy": 0, "ignored": 0,
         "prose_decoys": {"n": kept[1], "kept": kept[0], "broken": kept[1] - kept[0]}}
    blk = {"kind": k, "speed": {"first_highlight_ms": {"p50": 50, "p95": p95, "n": 10}}}
    b = {"schema": "speakeasy-bench/1", "family": "gap", "mode": "gated", "complete": True, "config": {"c": 1}, "fixtures": {"public-gap": {"manifest_sha256": "m"}},
         "sets": {"public-gap": blk}}
    bl = {"config": {"c": 1}, "manifest_sha256": "m", "metrics": S.baseline_metrics_gap(dict(blk, kind=dict(k, score=19.0, precision=100.0)))}
    return b, bl


def test_gap_gate_thresholds_and_refusals():
    b, bl = gap_bench(); code, out = S.gap_gate(b, {"public-gap": bl})
    assert code == 0 and out[-1] == "GATE PASS" and any("kind.score min 80.00 -> 85.00 (B6-pre 19.00, +66.00) PASS" in l for l in out), out
    for kw, frag in ((dict(score=79.99), "kind.score"), (dict(precision=89.9), "kind.precision"), (dict(wt=3), "kind.wrong_type"),
                     (dict(kept=(3, 4)), "prose_decoys"), (dict(p95=300), "p95"),
                     (dict(by_gap={"uncaptioned": 59.0, "lexicon": 90.0, "no-text": 70.0}), "by_gap.uncaptioned")):
        b2, bl2 = gap_bench(**kw); code, out = S.gap_gate(b2, {"public-gap": bl2})
        assert code == 1 and any(frag in l and l.endswith("FAIL") for l in out), (kw, out)
    b2, _ = gap_bench(by_gap={"uncaptioned": 80.0, "lexicon": 90.0}); code, out = S.gap_gate(b2, {"public-gap": bl})
    assert code == 1 and any("by_gap.no-text" in l and l.endswith("FAIL") for l in out), out        # a gated gap with no regions fails
    assert S.gap_gate(b, {"public-gap": None})[0] == 2
    assert S.gap_gate(b, {"public-gap": dict(bl, manifest_sha256="x")})[1][0].startswith("baseline stale")
    assert S.gap_gate(dict(b, mode="kind-replay"), {"public-gap": bl})[0] == 2
    assert S.gap_gate(dict(b, family=None), {"public-gap": bl})[0] == 2
    assert S.gap_gate(dict(b, complete=False), {"public-gap": bl})[0] == 2
    code, out = S.gap_gate(b, {"public-gap": bl, "hidden-gap": bl}); assert code == 1 and "hidden-gap set not run  FAIL" in out, out
    assert S.gap_gate(b, {"public-gap": bl, "hidden-gap": bl}, public_only=True)[0] == 0
    assert S.gate(b, {"public": bl})[0] == 2                                                         # the Phase 2 gate never takes a gap bench


def test_determinism_view_covers_kind_not_ms():
    b, _ = gap_bench(); b["sets"]["public-gap"].update(boxes={}, lines={}, parts={}, blockcls={"candidates": 3, "ms": {"p50": 9}, "ms_page": {"p50": 30}})
    v1 = S.determinism_view(b); b2 = copy.deepcopy(b); b2["sets"]["public-gap"]["blockcls"]["ms"]["p50"] = 12
    assert S.diff_paths(v1, S.determinism_view(b2)) == []
    b2["sets"]["public-gap"]["kind"]["hits"] = 39
    assert S.diff_paths(v1, S.determinism_view(b2)) == ["$.sets.public-gap.kind.hits"]


KIND_PROMPT_FAKE = "Which option describes the image? A: figure, diagram, illustration or photo. B: chart or plot of data. C: table. D: running text. E: none of these. Answer with the letter only."


def test_mock_kind_routing_cassette_and_modes():
    import base64, hashlib, json, math
    png = b"\x89PNG crop"; body = {"model": "qwen3-vl:4b-instruct", "stream": False, "logprobs": True, "top_logprobs": 20, "options": {"temperature": 0, "num_predict": 1},
                                   "messages": [{"role": "user", "content": KIND_PROMPT_FAKE, "images": [base64.b64encode(png).decode()]}]}
    key = hashlib.sha256(b"qwen3-vl:4b-instruct\x00" + KIND_PROMPT_FAKE.encode() + b"\x00" + hashlib.sha256(png).hexdigest().encode()).hexdigest()
    d0 = __import__("tempfile").mkdtemp(prefix="se_kcas_"); cas = os.path.join(d0, "k.json")
    json.dump({"header": {}, "entries": {key: {"top_logprobs": [{"token": "C", "logprob": math.log(0.97)}, {"token": "<other>", "logprob": math.log(0.03)}]}}}, open(cas, "w"))
    p, post, lg, d = _mock({"MOCK_KIND": "replay", "MOCK_KIND_CASSETTE": cas, "MOCK_PICK": "fixed", "MOCK_PICK_TOP_LOGPROBS": json.dumps([{"token": "B", "logprob": -0.01}])})
    try:
        r = post(body); assert r["message"]["content"] == "C" and r["logprobs"][0]["top_logprobs"][0]["token"] == "C", r      # kind -> kind cassette
        pick = dict(body, messages=[dict(body["messages"][0], content="Which marked region is the pump? Answer with one letter.")])
        assert post(pick)["message"]["content"] == "B"                                                                   # non-kind prompt -> pick settings
        miss = dict(body, messages=[dict(body["messages"][0], images=[base64.b64encode(b"other").decode()])]); assert "logprobs" not in post(miss)
        recs = [json.loads(x) for x in open(lg)]
        kr = [r for r in recs if r.get("event") == "kind_reply"]; pr = [r for r in recs if r.get("event") == "pick_reply"]
        assert [r["result"] for r in kr] == ["hit", "miss"] and len(pr) == 1, (kr, pr)
        assert kr[0]["prompt_sha"] == hashlib.sha256(KIND_PROMPT_FAKE.encode()).hexdigest() and kr[0]["png_sha"] == hashlib.sha256(png).hexdigest()
        assert all(KIND_PROMPT_FAKE not in json.dumps({k: v for k, v in r.items() if k != "prompt_tail"}) for r in kr)      # kind records: hashes only
    finally: p.terminate(); p.wait()
    p, post, lg, d = _mock({"MOCK_KIND_PROMPT_SHA256": "0" * 64})                       # exact-hash mode: the fake prompt is no longer a kind prompt
    try: post(body); assert not any('"kind_reply"' in x for x in open(lg))
    finally: p.terminate(); p.wait()
    p, post, lg, d = _mock({"MOCK_KIND": "status:503"})
    try:
        import urllib.error
        try: post(body); assert False, "expected HTTP 503"
        except urllib.error.HTTPError as e: assert e.code == 503
    finally: p.terminate(); p.wait()


OCR_OUT = {"version": 1, "engine": "vision-text", "image_size": [400, 120], "unit": "px", "origin": "top-left",
           "lines": [{"rect": [1.0, 2.0, 30.0, 10.0], "text": "North 42 12", "conf": 0.97}], "tables": [], "ms": 41}


def test_ocr_stub_replay_miss_failure_and_record():
    import hashlib, json, subprocess, tempfile
    sys.path.insert(0, HERE); import ocr_stub as OS
    d = tempfile.mkdtemp(prefix="se_ocr_t_"); png = os.path.join(d, "in.png"); open(png, "wb").write(b"\x89PNG table")
    key = hashlib.sha256(b"\x89PNG table").hexdigest(); bad_key = hashlib.sha256(b"\x89PNG broken").hexdigest()
    cas = os.path.join(d, "ocr.json"); lg = os.path.join(d, "ocr.jsonl")
    json.dump({"header": {}, "entries": {key: {"out": OCR_OUT, "rc": 0}, bad_key: {"out": None, "rc": 4}}}, open(cas, "w"))
    stub = os.path.join(HERE, "ocr_stub"); env = dict(os.environ, OCR_STUB_MODE="replay", OCR_STUB_CASSETTE=cas, OCR_STUB_LOG=lg)
    o = os.path.join(d, "o.json")
    r = subprocess.run([stub, "ocr", png, o], env=env); assert r.returncode == 0 and json.load(open(o)) == OCR_OUT
    png2 = os.path.join(d, "elsewhere", "renamed.png"); os.makedirs(os.path.dirname(png2)); open(png2, "wb").write(open(png, "rb").read())
    r = subprocess.run([stub, png2, os.path.join(d, "o1.json")], env=dict(env, SOME_VAR="x")); assert r.returncode == 0       # key = bytes only: path/env ignored
    open(o, "w").write("stale")
    open(png, "wb").write(b"\x89PNG other")
    r = subprocess.run([stub, png, o], env=env); assert r.returncode == 3 and not os.path.exists(o)                    # miss: exit 3, stale file removed
    open(png, "wb").write(b"\x89PNG broken")
    r = subprocess.run([stub, png, o], env=env); assert r.returncode == 4 and not os.path.exists(o)                    # stored failure replays its code, no file
    r = subprocess.run([stub, os.path.join(d, "nope.png"), o], env=env); assert r.returncode == 3                       # unreadable IN.png
    r = subprocess.run([stub, "ocr", png], env=env); assert r.returncode == 2                                           # usage
    real = os.path.join(d, "real.sh"); good = json.dumps(dict(OCR_OUT, lines=[]))
    open(real, "w").write("#!/bin/sh\ncase \"$2\" in *fail*) exit 4;; esac\necho '" + good + "' > \"$3\"\n"); os.chmod(real, 0o755)
    cas2 = os.path.join(d, "rec.json"); env2 = dict(env, OCR_STUB_MODE="record", OCR_STUB_CASSETTE=cas2, OCR_STUB_REAL=real)
    open(png, "wb").write(b"\x89PNG other")
    r = subprocess.run([stub, "ocr", png, os.path.join(d, "o3.json")], env=env2); assert r.returncode == 0
    fpng = os.path.join(d, "fail.png"); open(fpng, "wb").write(b"\x89PNG failing")
    r = subprocess.run([stub, "ocr", fpng, os.path.join(d, "o4.json")], env=env2); assert r.returncode == 4 and not os.path.exists(os.path.join(d, "o4.json"))
    rec = json.load(open(cas2))
    assert rec["entries"][hashlib.sha256(b"\x89PNG other").hexdigest()] == {"out": dict(OCR_OUT, lines=[]), "rc": 0}, rec
    assert rec["entries"][hashlib.sha256(b"\x89PNG failing").hexdigest()] == {"out": None, "rc": 4}, rec               # failures are recorded with their code
    assert set(rec["header"]) == {"helper_sha256", "macos", "recorded_at"}, rec["header"]
    res = [json.loads(x)["result"] for x in open(lg)]
    assert res == ["hit", "hit", "miss", "hit", "unreadable", "bad-args", "recorded", "real-failed"], res
    assert all("North" not in x for x in open(lg))                                       # the log carries hashes and codes, never OCR text
    assert OS.schema_errors(OCR_OUT) == [] and OS.schema_errors(dict(OCR_OUT, unit="pt")) and OS.schema_errors({k: v for k, v in OCR_OUT.items() if k != "ms"})
    assert OS.schema_errors(dict(OCR_OUT, lines=[{"rect": [1, 2, 500, 10], "text": "x", "conf": 0.5}]))                 # outside image_size
    assert OS.schema_errors(dict(OCR_OUT, tables=[{"rect": [0, 0, 10, 10], "rows": []}]))                               # vision-text has no tables
    tab = {"rect": [0, 0, 100, 40], "rows": [{"rect": [0, 0, 100, 20], "cells": [{"rect": [0, 0, 50, 20], "text": "North"}]}]}
    assert OS.schema_errors(dict(OCR_OUT, engine="vision-documents", tables=[tab])) == []
    app = dict(OCR_OUT, engine="app-lines", lines=[dict(OCR_OUT["lines"][0], conf=None)], ms=0)
    assert OS.schema_errors(app) == []


def _kind_home(kind_job, sidecar=None, routed=2, routed_pages=(1,)):
    import json, tempfile
    h = tempfile.mkdtemp(prefix="se_kj_t_"); c = os.path.join(h, "cache", "doc"); os.makedirs(c)
    pages = [{"n": n, "kinds": {"routed": 1 if n in routed_pages else 0}} for n in (1, 2, 3)]
    json.dump({"pages": pages, "blockcls": {"status": "unavailable", "kind_job": kind_job, "routed": routed}}, open(os.path.join(c, "0123456789abcdef0123.json"), "w"))
    if sidecar is not None: json.dump(sidecar, open(os.path.join(c, "0123456789abcdef0123.kinds.json"), "w"))
    return h, os.path.join(c, "0123456789abcdef0123.kinds.json")


def test_wait_kind_job_amendment_1_rule():
    import json, tempfile, threading, time
    sys.path.insert(0, HERE); import run_bench as RB
    assert RB.wait_kind_job(tempfile.mkdtemp(prefix="se_kj_e_"), 0.2)["status"] == "no-parse"
    h, _ = _kind_home("none"); t = time.time(); assert RB.wait_kind_job(h, 5.0)["status"] == "none" and time.time() - t < 1.0    # nothing to wait for
    h, _ = _kind_home("queued"); assert RB.wait_kind_job(h, 0.3)["status"] == "timeout"                                     # no sidecar by the cap
    h, sp = _kind_home("queued", {"state": "running"}); assert RB.wait_kind_job(h, 0.3)["status"] == "timeout"             # not a final state
    ent = [{"page": 1, "rect": [10.0, 20.0, 110.0, 90.0], "kind": "table", "outcome": "accepted", "p_top": 0.97, "p_margin": 0.95},
           {"page": 1, "rect": [10.0, 120.0, 110.0, 190.0], "kind": "text", "outcome": "text"}]
    h, sp = _kind_home("queued", {"version": 1, "state": "done", "pages": [2], "outcomes": {}, "entries": []}, routed_pages=(1, 2))
    assert RB.wait_kind_job(h, 0.3)["status"] == "timeout"                                                                  # A2.5: pages must cover routed pages 1, 2
    h, sp = _kind_home("queued", {"version": 1, "state": "done", "outcomes": {}, "entries": []})
    assert RB.wait_kind_job(h, 0.3)["status"] == "timeout"                                                                  # no pages: not covering
    h, sp = _kind_home("queued", {"version": 1, "state": "partial", "pages": [1, 3], "outcomes": {"accepted": 1, "skipped": 2}, "breaker": None, "error": None, "entries": ent})
    info = RB.wait_kind_job(h, 1.0); j = info["jobs"][0]
    assert info["status"] == "done" and j["state"] == "partial" and j["skipped"] == 2 and j["pages"] == [1, 3] and j["routed_pages"] == [1], info
    assert RB.sidecar_accepted(info) == {1: [{"kind": "table", "bbox": [10.0, 20.0, 110.0, 90.0], "kind_src": "vlm"}]}
    assert RB.sidecars_changed(info) == []
    json.dump({"state": "done", "pages": [1], "entries": []}, open(sp, "w")); assert RB.sidecars_changed(info) == [j["key"]]               # a second job rewrote it
    h, sp = _kind_home("queued", {"state": "failed", "pages": [1], "error": "RuntimeError", "entries": []})
    info = RB.wait_kind_job(h, 1.0); assert info["status"] == "failed" and info["jobs"][0]["error"] == "RuntimeError", info
    h, sp = _kind_home("queued")                                                                                            # sidecar lands mid-wait
    threading.Timer(0.3, lambda: json.dump({"state": "done", "pages": [1], "outcomes": {}, "entries": []}, open(sp, "w"))).start()
    assert RB.wait_kind_job(h, 3.0)["status"] == "done"
    assert RB._accepted_set({1: [{"kind": "table", "bbox": [10.04, 20, 110, 90]}]}) == RB._accepted_set({1: [{"kind": "table", "bbox": [10.0, 20.0, 110.0, 90.0]}]})


def test_ocr_helper_env_amendment_2_6():
    sys.path.insert(0, HERE); import run_bench as RB
    root = __import__("tempfile").mkdtemp(prefix="se_env_t_"); e = RB.Env(root, 1, sys.executable)
    g = e.env(os.path.join(root, "h"), os.path.join(root, "t"))
    assert "SPEAKEASY_OCR_BIN" not in g and not os.path.exists(g["SPEAKEASY_TRACKER"]), g.get("SPEAKEASY_TRACKER")   # gated: OCR unavailable
    e.ocr = {"mode": "replay", "cassette": "c.json", "log": "l.jsonl", "tag": "", "real": None}
    g = e.env(os.path.join(root, "h"), os.path.join(root, "t"))
    assert g["SPEAKEASY_OCR_BIN"] == RB.OCR_STUB and os.access(RB.OCR_STUB, os.X_OK) and g["OCR_STUB_MODE"] == "replay"
    os.environ["SPEAKEASY_OCR_BIN"] = "/elsewhere"
    try: assert "SPEAKEASY_OCR_BIN" not in RB.Env(root, 1, sys.executable).env("h", "t")                                # the caller's value never leaks in
    finally: os.environ.pop("SPEAKEASY_OCR_BIN")


def _l2page(n, routed=False, by=(), src=None, ms=0, added=0, structural=0, reason=None):
    d = {"routed": routed, "by": list(by), "src": src, "ms": ms, "boxes": {"figure": 0, "table": 0}, "added": added, "structural": structural}
    if reason: d["reason"] = reason
    return {"n": n, "layout2": d}


def test_layout2_summary_counts_rules_src_ms_and_reasons():
    import json
    heron = {"layout2": {"status": "ok", "detector_version": "docling-heron/59c81a3a2923", "reason": None, "load_ms": 133, "routed": 2},
             "pages": [_l2page(1, True, ("R1",), "heron", 405, 0, 1), _l2page(2, True, ("R1", "R2", "R3"), "heron", 419, 3), _l2page(3)],
             "blockcls_events": [{"event": "layout2_page", "page": 1}, {"event": "block_kind", "stage": "classifier", "outcome": "accepted", "ms": 9}]}
    miss = {"layout2": {"status": "unavailable", "detector_version": None, "reason": "model-missing", "load_ms": None, "routed": 1},
            "pages": [_l2page(1, True, ("R2",), "unavailable", 11, reason="model-missing")],
            "blockcls_events": [{"event": "layout2_unavailable", "reason": "model-missing", "error": None, "detector_version": None, "page": None}]}
    l2 = S.layout2_summary([heron, miss, {"pages": [{"n": 1}]}])
    assert (l2["pages"], l2["routed"], l2["by_rule"], l2["src"]) == (4, 3, {"R1": 2, "R2": 2, "R3": 1}, {"heron": 2, "unavailable": 1}), l2
    assert (l2["added"], l2["structural"], l2["routed_unavailable"]) == (3, 1, {"model-missing": 1}), l2
    assert l2["ms"] == {"p50": 405, "p95": 419, "n": 2} and l2["load_ms"] == {"p50": 133, "max": 133, "n": 1}, l2   # unavailable pages are not heron ms
    assert l2["reasons"] == {"model-missing": 1} and l2["unavailable_events"] == {"model-missing": 1}, l2
    assert S.layout2_coverage_errors("public-gap", l2) == ['public-gap: second-detector run but routed pages with layout2 reasons {"model-missing": 1}']
    assert S.layout2_coverage_errors("public-gap", S.layout2_summary([heron])) == []
    failed = {"layout2": {"status": "ok", "reason": None, "routed": 2}, "pages": [_l2page(1, True, ("R1",), "unavailable", 0, reason="inference-failed"),
                                                                              _l2page(2, True, ("R2",), "skipped", 0, reason="over-cap")]}
    assert S.layout2_summary([failed])["routed_unavailable"] == {"inference-failed": 1}                          # any fallback reason; the cap is not one
    b = S.blockcls_summary([heron, miss])
    assert b["by_stage"] == {"classifier": 1} and b["by_outcome"] == {"accepted": 1} and "None" not in b["by_stage"], b
    assert b["events"] == {} and b["layout2"] == {"pages": 1, "added": 0, "structural": 0, "unavailable": {"model-missing": 1}}, b
    heron["blockcls_events"][0].update(added=4, structural=2, ms=400)
    assert S.blockcls_summary([heron])["layout2"] == {"pages": 1, "added": 4, "structural": 2, "unavailable": {}}
    assert "ms" not in json.dumps(S._no_ms(S.blockcls_summary([heron])["layout2"]))


def test_layout2_determinism_and_existing_family_gate():
    b, bl = bench_and_baseline()
    b["sets"]["public"]["layout2"] = {"pages": 29, "routed": 0, "by_rule": {"R1": 0, "R2": 0, "R3": 0}, "ms": {"p50": None}, "load_ms": {"p50": None, "max": None, "n": 0}}
    # A5.1 pin update: existing-family routing is an informational count, never a gate failure (scores vs B5' stay the blocker)
    code, out = S.gate(b, {"public": bl}); assert code == 0 and 'public layout2 routed 0 by_rule {"R1": 0, "R2": 0, "R3": 0} heron ms p50 None p95 None' in out, out
    b2 = copy.deepcopy(b); b2["sets"]["public"]["layout2"]["routed"] = 1
    code, out = S.gate(b2, {"public": bl}); assert code == 0 and out.index("-- informational") < out.index(next(l for l in out if l.startswith("public layout2 routed 1"))), out
    assert not any("layout2" in l and ("FAIL" in l or "PASS" in l) for l in out), out
    b3 = copy.deepcopy(b); b3["sets"]["public"]["layout2"]["load_ms"] = {"p50": 150, "max": 150, "n": 1}; b3["sets"]["public"]["layout2"]["ms"] = {"p50": 400}
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b3)) == []                                       # timing is not determinism-covered
    assert S.diff_paths(S.determinism_view(b), S.determinism_view(b2)) == ["$.sets.public.layout2.routed"]
    nb, _ = bench_and_baseline(); assert "layout2" not in S.determinism_view(nb)["sets"]["public"]                 # B5'-era benches unchanged


def test_layout2_model_env_passthrough():
    sys.path.insert(0, HERE); import run_bench as RB
    root = __import__("tempfile").mkdtemp(prefix="se_env_l2_"); e = RB.Env(root, 1, sys.executable)
    os.environ["SPEAKEASY_LAYOUT2_MODEL"] = "/caller/heron.onnx"; os.environ["SPEAKEASY_LAYOUT2"] = "off"
    try:
        g = e.env("h", "t"); assert "SPEAKEASY_LAYOUT2_MODEL" not in g and "SPEAKEASY_LAYOUT2" not in g, g           # the scrub drops the caller's values
        e.layout2_model = "/tmp/p6models/docling_layout_heron.onnx"
        assert e.env("h", "t")["SPEAKEASY_LAYOUT2_MODEL"] == "/tmp/p6models/docling_layout_heron.onnx"
    finally: os.environ.pop("SPEAKEASY_LAYOUT2_MODEL"); os.environ.pop("SPEAKEASY_LAYOUT2")


def test_merge_preds_adds_vlm_rects_once():
    sys.path.insert(0, HERE); import run_bench as RB
    g = {1: [{"kind": "figure", "bbox": [0, 0, 100, 100], "kind_src": "classifier"}]}
    m = RB.merge_preds(g, {1: [{"kind": "figure", "bbox": [0, 0, 100, 99], "kind_src": "vlm"}, {"kind": "table", "bbox": [200, 0, 300, 100], "kind_src": "vlm"}],
                           2: [{"kind": "table", "bbox": [0, 0, 50, 50], "kind_src": "vlm"}]})
    assert [b["kind_src"] for b in m[1]] == ["classifier", "vlm"] and len(m[2]) == 1 and len(g[1]) == 1, m


def test_gap_validator_on_committed_public_fixtures():
    import validate_fixtures as V
    E, info = V.validate_gap_set("public", *V.set_paths("public", HERE, "/nonexistent", "gap"))
    if E and any("pypdfium2 is needed" in e for e in E): return                          # validator needs the doc venv for the G3 text-layer check
    assert not E, E[:5]
    c = info["counts"]
    assert c["kind_regions"] >= 40 and c["standard_caption"] <= 0.2 * c["kind_regions"] and c["decoys"] >= 12 and c["area_cases"] >= 2, c


if __name__ == "__main__":
    fails = 0; tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try: f(); print(f"PASS {n}")
        except AssertionError as e: fails += 1; print(f"FAIL {n}: {e}")
    print(f"{len(tests) - fails}/{len(tests)} passed"); sys.exit(1 if fails else 0)
