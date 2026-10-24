# Speakeasy

Wispr Flow in reverse: highlight text on your Mac, press a hotkey, and hear it explained in plain, conversational speech. Fully local and free.

- Copies your selection and (optionally) a cropped screenshot of just the highlighted region
- A small local LLM (Ollama, `qwen2.5:3b-instruct`) rewrites it for listening: tables become comparisons, charts become trends, code becomes "what it does", filler is cut
- A local vision model (`qwen2.5vl:3b`) adds structure from figures and diagrams
- Speaks with macOS `say`, or a neural voice (Kokoro via mlx-audio)
- Every run is logged to `~/.speakeasy/logs/runs.jsonl` with per-stage timings, memory, and capture details (`speakeasy last`, `speakeasy stats`)

## Layout
- `src/` – the tool (`speakeasy`, `speakeasy.py`, `highlight.py`, `install.sh`)
- `tests/` – independent black-box test suite, written without reading `src/` (`run_tests.sh`)
- `tests/audit/` – log auditor, schema, and privacy audit for captures

## Install (macOS, Apple Silicon)
```
brew install ollama && brew services start ollama
cd src && bash install.sh
```
Then follow the printed Hammerspoon snippet (Ctrl+Option+S) and grant Accessibility + Screen Recording.
