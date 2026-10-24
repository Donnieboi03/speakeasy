#!/usr/bin/env bash
# Usage: ./run_tests.sh [--judge] [--only SUBSTR]
# Env: SPEAKEASY_BIN, TEST_LLM_URL (default http://localhost:11434/v1), SPEAKEASY_MODEL, JUDGE_MODEL
cd "$(dirname "$0")" && exec python3 harness.py "$@"
