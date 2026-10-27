# Speakeasy

Wispr Flow in reverse: highlight text on your Mac, press **Ctrl+Option+S**, and hear it read back in plain conversational language. Filler is cut, and tables, charts and figures are *explained* instead of read cell by cell. Free, open source, fully offline.

## How it works
1. **Hotkey (Speakeasy.app, menu bar)** reads the selection through Accessibility (Cmd-C fallback) and takes a screenshot of its bounds, or of the focused window when nothing is selected. The bar at the bottom centre of the screen (see **The bar** below) shows preparing / reading with Pause, Next, Stop and Wrong spot.
2. **Nothing highlighted?** It captures the **focused window** (configurable) and the vision model sums it up in 2 to 4 spoken sentences. If the window can't be found, it captures nothing and logs why. It never silently falls back to full screen.
3. **Local LLM rewrite (Ollama):** one model, `qwen3-vl:4b-instruct` (Apache-2.0), for text, images and figure grounding. Includes prompt-injection delimiters, grounding checks and a superlative check. Fallback chain: vision, then text LLM, then deterministic cleanup.
4. **TTS:** macOS `say` by default; `install.sh` switches to [Kokoro-82M](https://huggingface.co/mlx-community/Kokoro-82M-bf16) through [mlx-audio](https://github.com/Blaizzy/mlx-audio). when its venv is present. Kokoro runs as a local OpenAI-compatible server (`127.0.0.1:8000/v1/audio/speech`) under a LaunchAgent and synthesizes the next sentence while the current one plays. If the server is down, it falls back to macOS `say`.
5. **Streaming:** the rewrite is streamed token by token and handed to TTS sentence by sentence, so speech starts after the first sentence instead of after the whole rewrite. Meta phrases like "in this screenshot" or "the image shows" are removed from the output (counted as `meta_refs_removed` in the log).
6. Press the hotkey again to stop. Any playback and temp WAVs are cleaned up.

Module map, data flow and the files each component writes: [docs/architecture.md](docs/architecture.md).

## Install (Apple Silicon)
```sh
brew install ollama python@3.12
cd src && ./install.sh
```
`install.sh` copies every `src/*.py` module and the launcher to `~/.speakeasy/bin`, links the `speakeasy` CLI into `/usr/local/bin` when it is writable (else `~/.local/bin`), pulls the Ollama model, builds the Kokoro venv at `~/.speakeasy/tts-venv` (Python 3.10–3.12, including the spaCy `en_core_web_sm` model so first use never blocks), installs and warms up the `com.speakeasy.tts` LaunchAgent (and sets `SPEAKEASY_TTS=kokoro`), writes config defaults, and builds `~/Applications/Speakeasy.app` (it does not launch it). Open the app once; it asks for **Accessibility** and **Screen Recording** only when you click Grant in its window. Single-file app builds with a bundled Python: see `docs/release.md`.

## Config: `~/.speakeasy/config` (KEY=VALUE; env vars override)
| Key | Default | Meaning |
|---|---|---|
| `SPEAKEASY_TTS` | `say` | `say`, `kokoro`, or `cmd` (`install.sh` sets `kokoro` when the Kokoro venv is present) |
| `SPEAKEASY_TTS_URL` | `http://127.0.0.1:8000/v1/audio/speech` | Kokoro endpoint (the base URL is accepted too) |
| `SPEAKEASY_TTS_VOICE` | `af_heart` | Kokoro voice |
| `SPEAKEASY_TTS_MODEL` | `mlx-community/Kokoro-82M-bf16` | TTS model id |
| `SPEAKEASY_TTS_CMD` | none | Custom TTS command for `cmd` (`{text}`, `{rate}`) |
| `SPEAKEASY_NO_SELECTION_CAPTURE` | `window` | Nothing highlighted: `window`, `screen`, or `none` |
| `SPEAKEASY_KEEP_CAPTURE` | `1` | Keep the last crop at `~/.speakeasy/last_capture.png` for auditing (`0` deletes it) |
| `SPEAKEASY_VISION` | `auto` | Vision gate: `auto` (only send the screenshot when it likely adds info, e.g. tables or figures), `always`, or `never` |
| `SPEAKEASY_STREAM` | `1` | Stream the LLM output and start speaking the first sentence while the rest is still generating (`0` waits for the full rewrite) |
| `SPEAKEASY_KEEP_ALIVE` | `10m` | Idle unload: Ollama frees the model this long after the last read (the hotkey pre-warms it) |
| `SPEAKEASY_GROUND` | `auto` | Figure-part grounding: `auto` or `local` (the local vision model), `openrouter` (cloud, see below), or `off`. Any other value acts as `auto` and is noted as `ground_mode_note` in the run log |
| `SPEAKEASY_PREWARM` | `1` | On hotkey, load the text/vision models in the background so the first call isn't cold (`0` disables) |
| `SPEAKEASY_VISION_MAX_PX` | `768` | Longest side the screenshot is downscaled to before it goes to the vision model |
| `SPEAKEASY_MODEL` | `qwen3-vl:4b-instruct` | Text model (same as the vision model by default) |
| `SPEAKEASY_DOC_NAMES` | `on` | Keep document names and paths in `~/.speakeasy/library.json` (never in a log) for the Reading log; `off` deletes the file |
| `SPEAKEASY_BAR` | `on` | Show the bar (menu "Show bar"; ⌃⌥B hides it until pressed again) |
| `SPEAKEASY_BAR_HIDE_FULLSCREEN` | `on` | Hide the idle bar while a full-screen app is in front |
| `SPEAKEASY_LOG_TEXT` | unset | `1` adds the input / output text to runs.jsonl and the sentence text, source lines and anchor text to focus.jsonl |

Advanced (same file or env; default in brackets): `SPEAKEASY_HOME` (`~/.speakeasy`); `SPEAKEASY_LLM_URL`
(`http://localhost:11434/v1`, any OpenAI-compatible endpoint; `SPEAKEASY_API_KEY` adds a bearer key); `SPEAKEASY_VISION_MODEL`
(`qwen3-vl:4b-instruct`); `SPEAKEASY_TIMEOUT` and `SPEAKEASY_VISION_TIMEOUT` (60 s per LLM / vision call); `SPEAKEASY_READ_MODE`
(`read`; `summary` summarises prose instead of reading it through); `SPEAKEASY_VERBATIM` (`on`: emails and quotes are read as
written); `SPEAKEASY_FOCUS_TAGS` (`off`: ask the model for line tags); `SPEAKEASY_TRACK_FPS` (5, the tracker's scan rate);
`SPEAKEASY_CAPTURE_FIT` (`on`: fit the page to the screen from the screenshot's text); `SPEAKEASY_PDF_ENGINE` (`auto`; `pdfkit`
reads PDF text through macOS PDFKit instead of the doc venv); `SPEAKEASY_DOC_PYTHON` (`~/.speakeasy/tts-venv/bin/python`);
`SPEAKEASY_LAYOUT_MODEL`, `SPEAKEASY_FIGCLS_MODEL`, `SPEAKEASY_LAYOUT2_MODEL` (model file paths, still sha256-checked; the
`*_MODEL_URL` variants change the installers' download source); `SPEAKEASY_TRACKER` and `SPEAKEASY_OCR_BIN` (the tracker binary
and the OCR helper the block classifier calls).


## Observability
Each run appends a JSONL record to `~/.speakeasy/logs/runs.jsonl`. It includes stage timings (`copy_ms`, `capture_ms`, `vision_ms`, `llm_ms`, `llm_first_token_ms`, `first_sentence_ms`, `tts_first_audio_ms`, `tts_synth_ms`, `speak_ms`, `total_ms`), errors separated by stage, `notes` (informational events such as removed superlatives, kept separate from errors), peak RSS, `tts_server_rss_mb`, loaded Ollama models, and the capture method and rect. Use `speakeasy last` and `speakeasy stats` to inspect them. Schema: `tests/audit/SCHEMA.md`.

No text in runs/focus logs unless `SPEAKEASY_LOG_TEXT=1`; the live `focus.json` holds the current source lines while a run is active. Without it, focus.jsonl records carry `text_len` / `text_hash` and `src_n` / `src_hash` (first 12 hex chars of SHA-256) instead of the text, and notes / errors name no table column, cell or model output. `capture_path` is home-relative (`~/...`) when the file is under your home folder.

- `speakeasy logs --scrub [--yes]` removes text from older `runs.jsonl*` / `focus.jsonl*` records in place (no backup copy),
  refuses while a read is running, and logs one count-only `command: scrub` record.

Temporary files: the crop is kept at `~/.speakeasy/last_capture.png` for auditing unless `SPEAKEASY_KEEP_CAPTURE=0`. The per-line bounds and document-hint files the app passes to the backend are deleted after they are read.

## Tests
These are black-box tests written independently of the implementation. Install their Python deps with `pip install -r tests/requirements.txt`. `tests/run_tests.sh` runs the grid and regression suite (LLM-judged). `tests/audit/run_audit.sh` runs the observability, privacy, no-selection and TTS suites. Each phase's acceptance suite is promoted under `tests/audit` with public data and file-relative defaults: `readability/run_phase1.py`, `logging/run_phase2.py`, `sentences/run_phase3.py`, `layout/run_phase4.py`, `parts/run_phase5.py`, `blocks/run_phase6.py`, `app/run_phase7.py`; `bench/run_bench.py` + `bench/bench_gate.py` is the three-score benchmark and `boxacc/test_box_accuracy.py` the box-accuracy harness. Set `SPEAKEASY_BIN` to test a different build. `cd tracker && swift test` runs the tracker XCTests (also in macOS CI, `.github/workflows/macos.yml`). The highlight-crop test needs your own `HIGHLIGHT_FIXTURE` screenshot, which isn't shipped.

## Resource use (M3 Pro, 18 GB)
- Ollama with `qwen3-vl:4b-instruct` loaded (the one model for text, images and grounding): 3.2 GB, all of it in GPU memory (`ollama ps`, measured 2026-10-26). It unloads after `SPEAKEASY_KEEP_ALIVE` (10 min) idle.
- Kokoro server: about 0.7 GB physical footprint with the voice loaded (measured before October 2026; only about 0.2 GB shows as RSS, because MLX memory is Metal-backed).


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
- PDF parsing uses [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (PDFium; Apache-2.0/BSD), installed into the Kokoro venv by `install.sh`
  (and bundled into the app). Markdown parsing is stdlib only. PyMuPDF (**AGPL-3.0**) is only a test dependency
  (box-accuracy ground truth and doc fixtures in `tests/requirements.txt`); the app never ships or imports it.
- PDF page layout (paragraph, caption, figure and table regions) uses the PP-DocLayout-S model (PaddlePaddle,
  **Apache-2.0**; ONNX export [stefanj0/PP-DocLayout-S-ONNX](https://huggingface.co/stefanj0/PP-DocLayout-S-ONNX)) on
  [onnxruntime](https://github.com/microsoft/onnxruntime) (MIT). `install.sh` fetches the pinned file into
  `~/.speakeasy/models/` and checks its sha256; without it, or with `SPEAKEASY_LAYOUT=off`, the parse uses its heuristics.
- Figures and tables without a usual caption (slides, `Abb. 2`, `TABLE IV`, scanned pages, tables embedded as images) are typed by
  Docling's [DocumentFigureClassifier-v2.5](https://huggingface.co/docling-project/DocumentFigureClassifier-v2.5) (IBM /
  docling-project, **MIT**) on onnxruntime, fetched and checked like the layout model; unsure regions go to the local vision model
  in the background when `SPEAKEASY_BLOCKCLS_VLM=auto|local` (default `off` until its threshold is frozen). Text without a text layer comes from Apple Vision
  (`speakeasy-track ocr`). Slides, scans and graphics-heavy pages also get a second layout detector, Docling's
  [docling-layout-heron-onnx](https://huggingface.co/docling-project/docling-layout-heron-onnx) (docling-project, **Apache-2.0**,
  about 171 MB, fetched and checked like the others; `SPEAKEASY_LAYOUT2=off` turns it off). `SPEAKEASY_BLOCKCLS=off` turns all of it off.

### Figures: panels and sub-objects (phase 3)

When the selection is a figure's caption, or the screenshot goes to the vision model, each spoken sentence is boxed on the
figure itself: a named object ("the WANDmini", "EEG", "Base Station") is circled using the figure's own text labels
(PDF text inside the figure, or text recognition for raster figures) and the graphic next to them; "Figure 4a" / "panel b"
boxes that panel; anything else boxes the whole figure. The vision prompt lists the figure and panel labels so the narration
says "In Figure 4a, ...". Numbered callouts ("Callout 3") are found from the figure's own markers and leader lines.
With `SPEAKEASY_PICK=on` (default off), a sentence that names an object no label matches asks the local vision model to
pick one of the figure's lettered candidate regions; the box becomes an oval only when it is confident. With
`SPEAKEASY_GROUND=openrouter` the same marked crop goes to OpenRouter only when the local model is unavailable.
`SPEAKEASY_GROUND=auto|local|openrouter|off` (default auto). Run logs record `ground_provider`, `part_picks` and
`ground_errors` (counts and error class names).

## Logs (observability)

`~/.speakeasy/logs/tracker.jsonl`, one JSON object per line, written as each event happens:

- `start` {ts, tracker_pid, pid, window_id, accessibility, screen_recording, fps, run_id, build, overlay}
- `draw` {ts, tracker_pid, seq, rect (panel), text_rect, src: ax|ocr|doc|scroll-delta|event, overlay: swift-panel, page_rect, page_fit}:
  `page_rect` is `text_rect` mapped back to page points through the tracker's current fit, `page_fit` its source
  (`anchors`: the tracker's own anchor fit carried by scroll deltas; `event-transform`: the event's `doc_transform` minus the
  scroll offset); both null when the box has no page-space fit
- `transition` {seq, old [w,h], new [w,h], nlines, held}: a new sentence box; `held:true` = oversized provisional box not drawn (previous box kept until OCR/AX finds the tight one). `ref-outline` {seq, rect, ms}: faint dashed outline on earlier lines for a sentence that refers back (the main box only moves forward). `focus.jsonl` map records carry `decision` forward|ref-back and `ref_lines`; runs.jsonl `focus_map` counts them.
- `scroll-rejected` {reason: horizontal|no-content-move|overscroll, counts}: scroll deltas not applied (at most one record per 0.5 s). `scroll-stuck` {dir, pred, actual}: OCR showed the content did not move under accepted deltas (document edge). `correction` {dx, dy, from, to, after_ms}: OCR/AX snapped the box back. `hscroll-confirmed`, `box-out-of-window`. Exit record: `scroll_rejected`, `corrections`.
- `draw-failed` {ts, tracker_pid, seq, why, after_ms: 300}: the panel should be showing but has not drawn 300 ms after the sentence started; the tracker also prints `NATIVE PANEL FAILED TO DRAW` to stderr. There is no fallback box.
- `hide` {ts, tracker_pid, seq, trigger: app-switch|tab-switch|window-switch|not-frontmost|scrolled-off|content-changed|speech-finished, hide_ms}
- `scroll` {ts, tracker_pid, scroll_lag_ms, src}; `ax` {ts, tracker_pid, notif, ax_ms, hit}
- `tap`, `ax-observer`, `ax-selection` (setup status), `frame` (per OCR scan)
- `exit` (also on SIGTERM) {ts, tracker_pid, run_id, reason, draws, hides, scroll_events, ax_events, ax_hits, ocr_hits, scroll_delta_hits,
  scroll_lag_avg_ms, scroll_lag_max_ms, hide_ms_avg, hide_ms_max, rss_mb_peak, cpu_pct, build}

The exit record also carries `draw_failed` (count).

`runs.jsonl` carries the same `run_id`, plus `app` (menubar|cli), `overlay`, `ram_total_mb` and always `doc_source`.
Paths are home-relative (`~/...`) or hashed (`doc_path_hash`); window titles are never logged.

## Focus box

The focus box is drawn only by the native tracker (`~/.speakeasy/bin/speakeasy-track`, an `NSPanel` it moves itself on scroll/AX events and corrects with on-screen text recognition). The menu-bar app only binds the hotkey and starts/stops the tracker and the backend; it installs no scroll or key event taps. If the panel cannot draw, the run logs `draw-failed` (tracker.jsonl) and the tracker prints `NATIVE PANEL FAILED TO DRAW` to stderr. `SPEAKEASY_FOCUS=off` or `SPEAKEASY_TRACK=off` turns the box off.

### Highlight settings

- `SPEAKEASY_TEXT_HIGHLIGHT=on|off` (default on): the soft tinted band over the sentence being read (low-alpha fill, rounded, no border; forward-only), plus the faint dashed outline when a sentence refers back to earlier lines.
- `SPEAKEASY_FIGURE_CUES=on|off` (default on): the outline / circle on the figure or table part being explained. While a figure cue is shown the text band and the dashed outline are not, so there is one cue at a time.

Both live in `~/.speakeasy/config` and can be toggled from the menu-bar app ("Highlight text", "Figure cues"). Each run logs `text_highlight` and `figure_cues` in runs.jsonl; tracker `draw` records carry `cue: text|figure`, and a suppressed cue logs `cue-off`. Design rationale and evidence: [docs/research/highlighting.md](docs/research/highlighting.md).

## Read area (Control-Option-A)

For PDFs and pages where selecting text is awkward: press Control-Option-A (or "Read area" in the menu, or click the bar), drag a rectangle, release. Esc, a click on the bar or Control-Option-A again cancels. The overlay never activates Speakeasy, so the document keeps keyboard focus; a size readout follows the cursor. Speakeasy takes the text the way copying does: the app's Accessibility text inside the rectangle when it has any; for a PDF, the document's own text layer inside the rectangle (the recognised lines fit the page to the screen, then the PDF lines inside the mapped rectangle are taken column by column, like a PDF reader's rectangle selection), so the words and per-line boxes are exact; on-screen text recognition (Vision) only when neither exists; the region also goes to the vision model for figures. The highlight band and figure cues are anchored to that region and follow scrolling like a text selection. The region is captured in memory with ScreenCaptureKit; the only file is the usual temporary capture, deleted after the run. `runs.jsonl` logs `capture_method: area` and `area_text: ax|pdf|ocr`, never the text. The overlay follows the common pattern of open-source capture tools such as Flameshot (one transparent window per screen, crosshair, Esc).

## The bar

A small navy bar sits at the bottom centre of the display, 8 pt above the Dock (contract `docs/contracts/phase7-usable-app.md` B, look `docs/design.md` 6.1). At rest it is a 56 × 8 stub with an ice margin tick; hover it for **Read area ⌃⌥A**, **Read selection ⌃⌥S** and the last place you read. Click it to arm Read area. While a read runs it shows one tick per sentence, "N of M", **Pause** (⌃⌥P), **Next** (⌃⌥N), **Stop** and **Wrong spot** (⌃⌥W, flags a wrong highlight in `logs/app.jsonl`); the last three keys and Esc are registered only while they apply, so they reach your document otherwise. It never takes keyboard focus, follows reads started from the CLI too, stacks 8 pt above another app's floating bar that sits on its spot (else moves to bottom left, then right; a position you dragged it to wins), slides to a bottom corner when the highlight comes near it, follows the pointer's display at rest, hides at rest in full-screen apps, and can be dragged to bottom left or right (remembered per display arrangement). Right-click at rest: *Hide for 1 hour*, *Hide until ⌃⌥B*, *Settings…*. Screen sharing apps can see the bar (macOS offers no way to hide a window from other apps' captures); hide it with ⌃⌥B. Speakeasy's own captures always exclude it. Pause and Next are signals to the running backend (`SIGUSR2`, `SIGUSR1`); runs log `paused_ms`, `skips`, `end`, `sent_spoken`, `sent_n`.

## Reading log (stats)

`speakeasy stats [--json] [--window 1|7|30]` (and the menu bar's **Reading log…**) shows what you act on: per-document progress and the resume point (page, figure, sentence), first audio (median, slowest 1 in 10, local vs OpenRouter, 14-day trend), distinct figures and tables, finished vs stopped reads, highlight quality (on target first time, flagged wrong spot, sentences with no cue), listening speed, and problems (Kokoro down, local model down, cloud fell back, layout model missing, highlight failed, permission missing, slow first audio) with one action each. The menu line reads "Today: 9 reads, 14 min". `--json` is schema 2 (`tests/audit/SCHEMA.md`). Stats read a whitelist of log keys only, never `text`, `input`, `output`, `error`, `llm_error` or `notes`. Document names come from `~/.speakeasy/library.json` (written by the backend after it resolves a document, mode 0600, never rotated or logged; Settings → *Show document names*, on by default). Logs rotate at 5 MB (5 kept).

## Credits

- [open-wispr](https://github.com/human37/open-wispr) (MIT): the menu-bar app's package layout, status-bar controller and the floating panel the bar grew from. See `app/NOTICE`.
- Fonts bundled in the app under the SIL Open Font License 1.1: [Source Serif 4](https://github.com/adobe-fonts/source-serif) (Adobe), [IBM Plex Sans and Plex Mono](https://github.com/IBM/plex) (IBM). Licence texts and file hashes: `app/NOTICE`.
- [KeyboardShortcuts](https://github.com/sindresorhus/KeyboardShortcuts) (MIT), [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (Apache-2.0/BSD), [PP-DocLayout-S](https://github.com/PaddlePaddle/PaddleOCR) (Apache-2.0) and [Docling DocumentFigureClassifier-v2.5](https://huggingface.co/docling-project/DocumentFigureClassifier-v2.5) (IBM / docling-project, MIT) and [docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron-onnx) (docling-project, Apache-2.0) via [onnxruntime](https://github.com/microsoft/onnxruntime) (MIT), [Kokoro](https://github.com/hexgrad/kokoro) via [mlx-audio](https://github.com/Blaizzy/mlx-audio), [Ollama](https://ollama.com), [python-build-standalone](https://github.com/astral-sh/python-build-standalone).

## Optional cloud (OpenRouter)

Off by default; everything runs locally unless you turn it on. Bring your own key, like Raycast or open-wispr:

- Menu bar → **Cloud (OpenRouter)…**: toggle *Rewrite* (sends the selected text) and/or *Find figure parts* (sends crops), pick a model, paste a key. The bar shows the word "Cloud" during a read that uses it, and the window shows the last 7 days' first-audio numbers for cloud and local.
- Or from a terminal: `security add-generic-password -U -s speakeasy-openrouter -a openrouter -w` (prompts for the key), then add `SPEAKEASY_LLM=openrouter`, `SPEAKEASY_GROUND=openrouter`, `SPEAKEASY_OR_MODEL=google/gemini-3.5-flash-lite` lines to `~/.speakeasy/config`.
- The key lives only in the login Keychain (`SPEAKEASY_OR_KEY` env overrides for testing); config holds on/off and the model.
- OpenAI-compatible `/chat/completions` with streaming; falls back to the local model on any error, no key, or no first token within `SPEAKEASY_OR_TIMEOUT` (8 s). Only `https` public hosts are accepted for `SPEAKEASY_OR_URL`.
- Logs record `llm_provider`, `llm_provider_model`, `llm_fallback`, `ground_provider_configured`; never text or the key.
- Grounding eval: `python3 tools/ground_eval.py --pdf paper.pdf --provider openrouter --model google/gemini-3.5-flash-lite --yes-paid` (paid calls; refuses without `--yes-paid`).
