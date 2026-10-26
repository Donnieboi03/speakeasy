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


if __name__ == "__main__":
    fails = 0; tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for n, f in tests:
        try: f(); print(f"PASS {n}")
        except AssertionError as e: fails += 1; print(f"FAIL {n}: {e}")
    print(f"{len(tests) - fails}/{len(tests)} passed"); sys.exit(1 if fails else 0)
