# Speakeasy

Wispr Flow in reverse: highlight text on your Mac, press **Ctrl+Option+S**, and hear it read back in plain conversational language. Filler is cut, and tables, charts and figures are *explained* instead of read cell by cell. Free, open source, fully offline.

## How it works
1. **Hotkey (Speakeasy.app, menu bar)** reads the selection through Accessibility (Cmd-C fallback) and takes a screenshot of its bounds, or of the focused window when nothing is selected. A floating pill shows listening / thinking / speaking with a stop button.
2. **Nothing highlighted?** It captures the **focused window** (configurable) and the vision model sums it up in 2 to 4 spoken sentences. If the window can't be found, it captures nothing and logs why. It never silently falls back to full screen.
3. **Local LLM rewrite (Ollama):** one model, `qwen3-vl:4b-instruct` (Apache-2.0), for text, images and figure grounding (Florence-2 optional with `SPEAKEASY_GROUND=florence`). Includes prompt-injection delimiters, grounding checks and a superlative check. Fallback chain: vision, then text LLM, then deterministic cleanup.
4. **TTS:** [Kokoro-82M](https://huggingface.co/mlx-community/Kokoro-82M-bf16) through [mlx-audio](https://github.com/Blaizzy/mlx-audio). It runs as a local OpenAI-compatible server (`127.0.0.1:8000/v1/audio/speech`) under a LaunchAgent and synthesizes the next sentence while the current one plays. If the server is down, it falls back to macOS `say`.
5. **Streaming:** the rewrite is streamed token by token and handed to TTS sentence by sentence, so speech starts after the first sentence instead of after the whole rewrite. Meta phrases like "in this screenshot" or "the image shows" are removed from the output (counted as `meta_refs_removed` in the log).
6. Press the hotkey again to stop. Any playback and temp WAVs are cleaned up.

## Install (Apple Silicon)
```sh
brew install ollama python@3.12
cd src && ./install.sh
```
`install.sh` installs the `speakeasy` CLI to `~/.local/bin`, pulls the Ollama models, builds the Kokoro venv at `~/.speakeasy/tts-venv` (Python 3.10–3.12, including the spaCy `en_core_web_sm` model so first use never blocks), installs and warms up the `com.speakeasy.tts` LaunchAgent, writes config defaults, and builds `~/Applications/Speakeasy.app` (it does not launch it). Open the app once; it asks for **Accessibility** and **Screen Recording** only when you click Grant in its window. On first launch it takes the hotkey over from an older Hammerspoon speakeasy block, which can then be deleted. Single-file app builds with a bundled Python: see `docs/release.md`.

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
| `SPEAKEASY_KEEP_ALIVE` | `10m` | Idle unload: Ollama frees the model this long after the last read (the hotkey pre-warms it) |
| `SPEAKEASY_FLORENCE` | `off` | Florence-2 phrase grounding (needs torch, ~2 GB RAM); off uses the vision model for grounding |
| `SPEAKEASY_PREWARM` | `1` | On hotkey, load the text/vision models in the background so the first call isn't cold (`0` disables) |
| `SPEAKEASY_VISION_MAX_PX` | `768` | Longest side the screenshot is downscaled to before it goes to the vision model |
| `SPEAKEASY_MODEL` | `qwen3-vl:4b-instruct` | Text model (same as the vision model by default) |


## Observability
Each run appends a JSONL record to `~/.speakeasy/logs/runs.jsonl`. It includes stage timings (`copy_ms`, `capture_ms`, `vision_ms`, `llm_ms`, `llm_first_token_ms`, `first_sentence_ms`, `tts_first_audio_ms`, `tts_synth_ms`, `speak_ms`, `total_ms`), errors separated by stage, `notes` (informational events such as removed superlatives, kept separate from errors), peak RSS, `tts_server_rss_mb`, loaded Ollama models, and the capture method and rect. Use `speakeasy last` and `speakeasy stats` to inspect them. Schema: `tests/audit/SCHEMA.md`.

## Tests
These are black-box tests written independently of the implementation. `tests/run_tests.sh` runs the grid and regression suite (LLM-judged). `tests/audit/run_audit.sh` runs the observability, privacy, no-selection and TTS suites. Set `SPEAKEASY_BIN` to test a different build. `cd tracker && swift test` runs the tracker XCTests (also in macOS CI, `.github/workflows/macos.yml`). The highlight-crop test needs your own `HIGHLIGHT_FIXTURE` screenshot, which isn't shipped.

## Resource use (measured, M-series, 18 GB)
- Ollama 3B text runner: about 2 GB resident (the vision model loads on demand, about 3 GB more).
- Kokoro server: about 0.7 GB physical footprint (only about 0.2 GB shows as RSS, because MLX memory is Metal-backed).


## Document-aware focus (PDF / Markdown)

When the selection comes from a PDF or Markdown file, speakeasy reads the file itself (read-only, local) to get exact
line, figure, caption and panel positions. The narration names figures by label ("In Figure 4b, ...") and the highlight
box targets the exact line, figure or panel; the live tracker maps page coordinates to the screen from recognised text anchors.

- Finding the file: Preview and other native document windows via the window's `AXDocument`; Cursor / VS Code via the
  window title plus a search of recent workspace folders (used only when exactly one file matches); browsers via the
  focused web area's URL (a `file://` URL, or with `SPEAKEASY_DOC_READ=all` a public `https://….pdf` downloaded without
  cookies or auth into the temp dir as `speakeasy_doc_*`, removed after a day). Otherwise the screenshot behaviour is unchanged.
- `SPEAKEASY_DOC_READ=local|all|off` (default `local`). Parsed documents are cached in `~/.speakeasy/cache/doc/`.
  The lookup runs alongside the audio; the prompt waits at most `SPEAKEASY_DOC_WAIT_MS` (80 ms) and never for a first-time parse.
- Logged per run: `doc_source`, `doc_path_hash` (never the path), `page`, `figures_found`, `doc_map_method`, `doc_ms`, `doc_cache`.
- PDF parsing uses [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (PDFium; Apache-2.0/BSD, replacing the AGPL PyMuPDF), installed into the Kokoro venv by `install.sh`.
  PyMuPDF is licensed under **AGPL-3.0** (or a commercial licence); that is fine for local personal use, but keep it in
  mind before redistributing a bundle that includes it. Markdown parsing is stdlib only.

### Figures: panels and sub-objects (phase 3)

When the selection is a figure's caption, or the screenshot goes to the vision model, each spoken sentence is boxed on the
figure itself: a named object ("the WANDmini", "EEG", "Base Station") is circled using the figure's own text labels
(PDF text inside the figure, or text recognition for raster figures) and the graphic next to them; "Figure 4a" / "panel b"
boxes that panel; anything else boxes the whole figure. The vision prompt lists the figure and panel labels so the narration
says "In Figure 4a, ...". When no label matches, Florence-2 (`microsoft/Florence-2-base` family, local, MPS when available,
loaded lazily in the Kokoro venv while vision runs) grounds the phrase on the figure crop and the box moves when it answers.
`SPEAKEASY_GROUND=auto|off` (default auto). The model is downloaded once from Hugging Face (about 0.5 GB).

## Logs (observability)

`~/.speakeasy/logs/tracker.jsonl`, one JSON object per line, written as each event happens:

- `start` {ts, tracker_pid, pid, window_id, accessibility, screen_recording, fps, run_id, build, overlay}
- `draw` {ts, tracker_pid, seq, rect (panel), text_rect, src: ax|ocr|doc|scroll-delta|event, overlay: swift-panel}
- `transition` {seq, old [w,h], new [w,h], nlines, held}: a new sentence box; `held:true` = oversized provisional box not drawn (previous box kept until OCR/AX finds the tight one). `ref-outline` {seq, rect, ms}: faint dashed outline on earlier lines for a sentence that refers back (the main box only moves forward). `focus.jsonl` map records carry `decision` forward|ref-back and `ref_lines`; runs.jsonl `focus_map` counts them.
- `scroll-rejected` {reason: horizontal|no-content-move|overscroll, counts}: scroll deltas not applied (at most one record per 0.5 s). `scroll-stuck` {dir, pred, actual}: OCR showed the content did not move under accepted deltas (document edge). `correction` {dx, dy, from, to, after_ms}: OCR/AX snapped the box back. `hscroll-confirmed`, `box-out-of-window`. Exit record: `scroll_rejected`, `corrections`.
- `draw-failed` {ts, tracker_pid, seq, why, after_ms: 300}: the panel should be showing but has not drawn 300 ms after the sentence started; also written to `hs.log` as `NATIVE PANEL FAILED TO DRAW`. There is no fallback box.
- `hide` {ts, tracker_pid, seq, trigger: app-switch|tab-switch|window-switch|not-frontmost|scrolled-off|content-changed|speech-finished, hide_ms}
- `scroll` {ts, tracker_pid, scroll_lag_ms, src}; `ax` {ts, tracker_pid, notif, ax_ms, hit}
- `tap`, `ax-observer`, `ax-selection` (setup status), `frame` (per OCR scan)
- `exit` (also on SIGTERM) {ts, tracker_pid, run_id, reason, draws, hides, scroll_events, ax_events, ax_hits, ocr_hits, scroll_delta_hits,
  scroll_lag_avg_ms, scroll_lag_max_ms, hide_ms_avg, hide_ms_max, rss_mb_peak, cpu_pct, build}

The exit record also carries `draw_failed` (count).

`runs.jsonl` carries the same `run_id`, plus `app` (menubar|cli), `overlay`, `ram_total_mb` and always `doc_source`.
Paths are home-relative (`~/...`) or hashed (`doc_path_hash`); window titles are never logged.

## Focus box

The focus box is drawn only by the native tracker (`~/.speakeasy/bin/speakeasy-track`, an `NSPanel` it moves itself on scroll/AX events and corrects with on-screen text recognition). The menu-bar app only binds the hotkey and starts/stops the tracker and the backend; it installs no scroll or key event taps. The old Hammerspoon canvas overlay, its 300 ms fallback and `SPEAKEASY_OVERLAY` were removed: if the panel cannot draw, the run logs `draw-failed` (tracker.jsonl) and `NATIVE PANEL FAILED TO DRAW` (hs.log). `SPEAKEASY_FOCUS=off` or `SPEAKEASY_TRACK=off` turns the box off.

### Highlight settings

- `SPEAKEASY_TEXT_HIGHLIGHT=on|off` (default on): the soft tinted band over the sentence being read (low-alpha fill, rounded, no border; forward-only), plus the faint dashed outline when a sentence refers back to earlier lines.
- `SPEAKEASY_FIGURE_CUES=on|off` (default on): the outline / circle on the figure or table part being explained. While a figure cue is shown the text band and the dashed outline are not, so there is one cue at a time.

Both live in `~/.speakeasy/config` and can be toggled from the menu-bar app ("Highlight text", "Figure cues"). Each run logs `text_highlight` and `figure_cues` in runs.jsonl; tracker `draw` records carry `cue: text|figure`, and a suppressed cue logs `cue-off`. Design rationale and evidence: [docs/research/highlighting.md](docs/research/highlighting.md).

## Stats

`speakeasy stats --json` (and the menu bar's Stats window) counts words listened, a daily streak heatmap, figures and tables explained, median time to first audio, and reading time saved, estimated as words read to you ÷ 238 words per minute (average adult silent reading rate; an estimate, not a measurement). `runs.jsonl` stores only counts (`words_in`, `words_spoken`, `audio_s`, `kind`), never the text. Logs rotate at 5 MB (5 kept).

## Credits

- [open-wispr](https://github.com/human37/open-wispr) (MIT): the menu-bar app's package layout, status-bar controller and recording-pill pattern. See `app/NOTICE`.
- [KeyboardShortcuts](https://github.com/sindresorhus/KeyboardShortcuts) (MIT), [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (Apache-2.0/BSD), [Kokoro](https://github.com/hexgrad/kokoro) via [mlx-audio](https://github.com/Blaizzy/mlx-audio), [Ollama](https://ollama.com), [python-build-standalone](https://github.com/astral-sh/python-build-standalone).
