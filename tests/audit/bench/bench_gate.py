#!/usr/bin/env python3
"""Baseline gate for the Phase 2 benchmark (contract section 8).

  bench_gate.py BENCH_JSON [--baseline FILE] [--hidden-baseline FILE] [--public-only] [--parts-min SCORE]

Fails (exit 1) when a gated metric (boxes.score, boxes.precision_50, lines.score, parts.score, parts.precision) drops by more
than 1 point against its set's baseline, when first_highlight_ms p50 or p95 is >= 300 ms in any set, or when a hidden baseline
exists but the bench has no hidden set (a warning with --public-only). --parts-min (phase5-parts.md 9.1: 70) also fails any set
whose parts.score is below that absolute floor. Exit 2: usage error, incomplete or malformed bench, a pick-mode bench, missing or
stale baseline. Exit 0: GATE PASS.

A gap-family bench (run_bench.py --family gap, docs/contracts/phase6-block-classifier.md 14.2) is gated by score.gap_gate instead:
kind.score >= 80, kind.precision >= 90, by_gap uncaptioned / lexicon / no-text >= 60, kind.wrong_type <= 2, every prose decoy kept
and first highlight p50 / p95 < 300 ms on every set, with the gain over B6-pre reported. Its baselines (B6-pre) default to
tests/audit/bench/baseline-gap.json and <holdout>/gap/baseline.json. A replay bench (--kind / --ocr) is never gated."""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import score as S


def load(p):
    try: return json.load(open(p))
    except (OSError, ValueError): return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("bench")
    ap.add_argument("--baseline"); ap.add_argument("--hidden-baseline")
    ap.add_argument("--public-only", action="store_true")
    ap.add_argument("--parts-min", type=float, help="absolute parts.score floor for every set (Phase 5: 70)")
    a = ap.parse_args()
    bench = load(a.bench)
    if bench is None: print(f"gate: cannot read {a.bench}", file=sys.stderr); sys.exit(2)
    holdout = os.path.expanduser(os.environ.get("SPEAKEASY_HOLDOUT") or "~/speakeasy-holdout/bench")
    if bench.get("family") == "gap":
        pb = a.baseline or os.path.join(HERE, "baseline-gap.json"); hb = a.hidden_baseline or os.path.join(holdout, "gap", "baseline.json")
        code, lines = S.gap_gate(bench, {"public-gap": load(pb), "hidden-gap": load(hb)}, public_only=a.public_only)
    else:
        pb = a.baseline or os.path.join(HERE, "baseline.json"); hb = a.hidden_baseline or os.path.join(holdout, "baseline.json")
        code, lines = S.gate(bench, {"public": load(pb), "hidden": load(hb)}, public_only=a.public_only, parts_min=a.parts_min)
    print("\n".join(lines)); sys.exit(code)


if __name__ == "__main__":
    main()
