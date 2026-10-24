#!/usr/bin/env bash
# Observability tests + audit. Usage: ./run_audit.sh   (audit a Mac log copy: python3 audit_logs.py /path/runs.jsonl --screen 3024x1964 --md out.md)
cd "$(dirname "$0")" && python3 test_observe.py; python3 tts/test_tts.py; python3 nosel/test_nosel.py; python3 gate/test_gate.py; python3 stream/test_stream.py; python3 superlative/test_superlative.py; python3 focus/test_focus.py; python3 verbatim/test_verbatim.py
