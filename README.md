# Speakeasy

Wispr Flow in reverse: highlight text on your Mac, press **Ctrl+Option+S**, and hear it read back in plain conversational language. Filler is cut, and tables, charts and figures are *explained* instead of read cell by cell. Free, open source, fully offline.

## How it works
1. **Hotkey (Hammerspoon)** copies the selection and takes a screenshot cropped to the full bounding box of the highlighted area (AX selection bounds, then highlight-colour detection, then an optional drag-select).
2. **Nothing highlighted?** It captures the **focused window** (configurable) and the vision model sums it up in 2 to 4 spoken sentences. If the window can't be found, it captures nothing and logs why. It never silently falls back to full screen.
3. **Local LLM rewrite (Ollama):** `qwen2.5:3b-instruct` for text, `qwen2.5vl:3b` for images. Includes prompt-injection delimiters, grounding checks and a superlative check. Fallback chain: vision, then text LLM, then deterministic cleanup.
4. **TTS:** [Kokoro-82M](https://huggingface.co/mlx-community/Kokoro-82M-bf16) through [mlx-audio](https://github.com/Blaizzy/mlx-audio). It runs as a local OpenAI-compatible server (`127.0.0.1:8000/v1/audio/speech`) under a LaunchAgent and synthesizes the next sentence while the current one plays. If the server is down, it falls back to macOS `say`.
5. **Streaming:** the rewrite is streamed token by token and handed to TTS sentence by sentence, so speech starts after the first sentence instead of after the whole rewrite. Meta phrases like "in this screenshot" or "the image shows" are removed from the output (counted as `meta_refs_removed` in the log).
6. Press the hotkey again to stop. Any playback and temp WAVs are cleaned up.

## Install (Apple Silicon)
```sh
brew install ollama hammerspoon python@3.12
cd src && ./install.sh
```
`install.sh` installs the `speakeasy` CLI to `~/.local/bin`, pulls the Ollama models, builds the Kokoro venv at `~/.speakeasy/tts-venv` (Python 3.10–3.12, including the spaCy `en_core_web_sm` model so first use never blocks), installs and warms up the `com.speakeasy.tts` LaunchAgent, writes config defaults, and prints the Hammerspoon block. Paste it into `~/.hammerspoon/init.lua`, replacing any older block, then Reload Config. Hammerspoon needs **Accessibility** and **Screen Recording** permission.

## Config: `~/.speakeasy/config` (KEY=VALUE; env vars override)
| Key | Default | Meaning |
|---|---|---|
| `SPEAKEASY_TTS` | `kokoro` | `kokoro`, `say`, or `cmd` |
| `SPEAKEASY_TTS_URL` | `http://127.0.0.1:8000/v1/audio/speech` | Kokoro endpoint (the base URL is accepted too) |
| `SPEAKEASY_TTS_VOICE` | `af_heart` | Kokoro voice |
| `SPEAKEASY_TTS_MODEL` | `mlx-community/Kokoro-82M-bf16` | TTS model id |
| `SPEAKEASY_TTS_CMD` | none | Custom TTS command for `cmd` (`{text}`, `{rate}`) |
| `SPEAKEASY_NO_SELECTION_CAPTURE` | `window` | Nothing highlighted: `window`, `screen`, or `none` |
| `SPEAKEASY_KEEP_CAPTURE` | `1` | Keep the last crop at `~/.speakeasy/last_capture.png` for auditing (`0` deletes it) |
| `SPEAKEASY_VISION` | `auto` | Vision gate: `auto` (only send the screenshot when it likely adds info, e.g. tables or figures), `always`, or `never` |
| `SPEAKEASY_STREAM` | `1` | Stream the LLM output and start speaking the first sentence while the rest is still generating (`0` waits for the full rewrite) |
| `SPEAKEASY_KEEP_ALIVE` | `30m` | How long Ollama keeps the models loaded after use |
| `SPEAKEASY_PREWARM` | `1` | On hotkey, load the text/vision models in the background so the first call isn't cold (`0` disables) |
| `SPEAKEASY_VISION_MAX_PX` | `768` | Longest side the screenshot is downscaled to before it goes to the vision model |
| `SPEAKEASY_MODEL` | `qwen2.5:3b-instruct` | Text model |

The `CAPTURE` variable in the Hammerspoon block can be `auto`, `select`, `mouse`, or `off`.

## Observability
Each run appends a JSONL record to `~/.speakeasy/logs/runs.jsonl`. It includes stage timings (`copy_ms`, `capture_ms`, `vision_ms`, `llm_ms`, `llm_first_token_ms`, `first_sentence_ms`, `tts_first_audio_ms`, `tts_synth_ms`, `speak_ms`, `total_ms`), errors separated by stage, peak RSS, `tts_server_rss_mb`, loaded Ollama models, and the capture method and rect. Use `speakeasy last` and `speakeasy stats` to inspect them. Schema: `tests/audit/SCHEMA.md`.

## Tests
These are black-box tests written independently of the implementation. `tests/run_tests.sh` runs the grid and regression suite (LLM-judged). `tests/audit/run_audit.sh` runs the observability, privacy, no-selection and TTS suites. Set `SPEAKEASY_BIN` to test a different build. The highlight-crop test needs your own `HIGHLIGHT_FIXTURE` screenshot, which isn't shipped.

## Resource use (measured, M-series, 18 GB)
- Ollama 3B text runner: about 2 GB resident (the vision model loads on demand, about 3 GB more).
- Kokoro server: about 0.7 GB physical footprint (only about 0.2 GB shows as RSS, because MLX memory is Metal-backed).

