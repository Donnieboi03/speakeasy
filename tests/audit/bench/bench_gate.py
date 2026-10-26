#!/usr/bin/env python3
"""Baseline gate for the Phase 2 benchmark (contract section 8).

  bench_gate.py BENCH_JSON [--baseline FILE] [--hidden-baseline FILE] [--public-only]

Fails (exit 1) when a gated metric (boxes.score, boxes.precision_50, lines.score, parts.score, parts.precision) drops by more
than 1 point against its set's baseline, when first_highlight_ms p50 or p95 is >= 300 ms in any set, or when a hidden baseline
exists but the bench has no hidden set (a warning with --public-only). Exit 2: usage error, incomplete or malformed bench,
missing or stale baseline. Exit 0: GATE PASS."""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import score as S


def load(p):
    try: return json.load(open(p))
    except (OSError, ValueError): return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("bench")
    ap.add_argument("--baseline", default=os.path.join(HERE, "baseline.json"))
    ap.add_argument("--hidden-baseline", default=os.path.join(os.path.expanduser(os.environ.get("SPEAKEASY_HOLDOUT") or "~/speakeasy-holdout/bench"), "baseline.json"))
    ap.add_argument("--public-only", action="store_true")
    a = ap.parse_args()
    bench = load(a.bench)
    if bench is None: print(f"gate: cannot read {a.bench}", file=sys.stderr); sys.exit(2)
    code, lines = S.gate(bench, {"public": load(a.baseline), "hidden": load(a.hidden_baseline)}, public_only=a.public_only)
    print("\n".join(lines)); sys.exit(code)


if __name__ == "__main__":
    main()
