#!/usr/bin/env bash
# One-command report on the latest speakeasy run(s): box shown?, source ax/ocr/scroll-delta, lag avg/max, hide latency, exit reason,
# health flags, plus perf budgets. Read-only; works on a log copy too.
# Usage: tests/audit/run_report.sh [LOGDIR=~/.speakeasy/logs] [--last N | --all]
D="${1:-$HOME/.speakeasy/logs}"; shift 2>/dev/null; H="$(cd "$(dirname "$0")" && pwd)"
python3 "$H/perf/run_health.py" "$D" "$@"; rc=$?
echo; python3 "$H/perf/audit_perf.py" "$D" | sed -n '1,40p'
exit $rc
