#!/usr/bin/env bash
# Pre-Phase-6 baselines on post-Phase-7 main (docs/contracts/phase6-block-classifier.md: B5' and 12.1), one command:
#
#   tests/audit/bench/record_pre_phase6.sh <post-Phase-7 main commit>
#
# Run it from the phase6-bench worktree after Phase 7 is merged and before any Phase 6 code exists. It
#   1. checks out <ref> into a temporary detached worktree (the main tree is never touched);
#   2. B5': runs that tree's own bench harness on the existing families (--set all, detector, determinism check) and records
#      both baselines: public -> $OUT/b5prime_public_baseline.json (commit it on main as tests/audit/bench/baseline.json),
#      hidden -> <holdout>/baseline.json (the B5 hidden baseline is backed up to $OUT/b5_hidden_baseline.json first);
#      boxes.wrong_type at B5' (12.1 item 1) is in $OUT/b5prime_bench.json and the printed summary;
#   3. census (12.1 item 2): tools/kind_census.py on both sets with <ref>'s detector code (hidden: totals only);
#   4. B6-pre (12.1 item 3): this branch's gap harness against <ref>'s app code (--app-repo), --family gap --set all, determinism
#      check, recorded into tests/audit/bench/baseline-gap.json (commit on phase6-bench) and <holdout>/gap/baseline.json;
#   5. removes the temporary worktree.
# Env: DOC_PYTHON (default ~/.speakeasy/tts-venv/bin/python, read-only), LAYOUT_MODEL (default /tmp/pp_doclayout_s.onnx),
#      SPEAKEASY_HOLDOUT (default ~/speakeasy-holdout/bench), OUT_DIR (default ~/speakeasy-holdout/phase6/b5prime), MAIN_REPO.
set -euo pipefail
REF="${1:?usage: record_pre_phase6.sh <post-Phase-7 main commit>}"
P6="$(cd "$(dirname "$0")/../../.." && pwd)"
MAIN_REPO="${MAIN_REPO:-/Users/donnieb/Desktop/Code/speakeasy}"
DOCPY="${DOC_PYTHON:-$HOME/.speakeasy/tts-venv/bin/python}"
LAYOUT="${LAYOUT_MODEL:-/tmp/pp_doclayout_s.onnx}"
HOLD="${SPEAKEASY_HOLDOUT:-$HOME/speakeasy-holdout/bench}"
OUT="${OUT_DIR:-$HOME/speakeasy-holdout/phase6/b5prime}"
[ -f "$LAYOUT" ] || { echo "layout model not found: $LAYOUT" >&2; exit 2; }
[ -x "$DOCPY" ] || { echo "doc python not found: $DOCPY" >&2; exit 2; }
git -C "$MAIN_REPO" cat-file -e "$REF^{commit}" || { echo "unknown commit $REF in $MAIN_REPO" >&2; exit 2; }
if git -C "$MAIN_REPO" cat-file -e "$REF:src/blockcls.py" 2>/dev/null; then echo "$REF already has src/blockcls.py: not a pre-Phase-6 commit" >&2; exit 2; fi
mkdir -p "$OUT"
W="$(mktemp -d /tmp/se_p7_XXXXXX)/tree"
cleanup() { git -C "$MAIN_REPO" worktree remove --force "$W" >/dev/null 2>&1 || true; }
trap cleanup EXIT
git -C "$MAIN_REPO" worktree add --detach "$W" "$REF" >/dev/null
echo "== B5' on $REF (its own harness)"
[ -f "$HOLD/baseline.json" ] && cp "$HOLD/baseline.json" "$OUT/b5_hidden_baseline.json"
(cd "$W" && "$DOCPY" tests/audit/bench/run_bench.py --set all --layout-model "$LAYOUT" --check-determinism --record-baseline \
   --public-baseline-out "$OUT/b5prime_public_baseline.json" --hidden-baseline-out "$HOLD/baseline.json" \
   --last-cases-out "$OUT/b5prime_hidden_last_cases.json" --out "$OUT/b5prime_bench.json")
echo "== census on $REF"
for s in public hidden; do
  "$DOCPY" "$P6/tools/kind_census.py" --app-repo "$W" --set "$s" --layout-model "$LAYOUT" | tee -a "$OUT/census.txt"
  "$DOCPY" "$P6/tools/kind_census.py" --app-repo "$W" --set "$s" --family gap --layout-model "$LAYOUT" | tee -a "$OUT/census.txt"
done
echo "== B6-pre on $REF (phase6-bench gap harness)"
(cd "$P6" && "$DOCPY" tests/audit/bench/run_bench.py --app-repo "$W" --family gap --set all --layout-model "$LAYOUT" --check-determinism --record-baseline \
   --public-baseline-out "$P6/tests/audit/bench/baseline-gap.json" --hidden-baseline-out "$HOLD/gap/baseline.json" \
   --last-cases-out "$OUT/b6pre_hidden_last_cases.json" --out "$OUT/b6pre_bench.json")
echo "== done: B5' public $OUT/b5prime_public_baseline.json (commit on main as tests/audit/bench/baseline.json), B5' hidden $HOLD/baseline.json,"
echo "   B6-pre public $P6/tests/audit/bench/baseline-gap.json (commit on phase6-bench), B6-pre hidden $HOLD/gap/baseline.json, census $OUT/census.txt"
