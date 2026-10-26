# Architecture

Three processes per read: the menu-bar app (Swift), the Python backend (`src/speakeasy.py`, one run per hotkey press)
and the focus tracker (`speakeasy-track`, Swift). They talk through command-line arguments and small JSON files under
`~/.speakeasy` (`$SPEAKEASY_HOME`), never over sockets.

## Module map (`src/`)

| Module | Role |
|---|---|
| `speakeasy.py` | Entry point and CLI (`speak`, `selection`, `rewrite`, `stop`, `last`, `stats`, `highlight`): text cleanup, fallback reader, verbatim blocks, vision gate, the run record |
| `speakeasy_state.py` | The live per-process state every module shares: `STOP`, `SINK`, `FOCUS`, `DOC`, `AREA_FIT`, the `docmap` handle |
| `prompts.py` | Every prompt text sent to the LLM (rewrite, read-through, vision, screen, focus-tag and document-label rules) |
| `llm_client.py` | OpenAI-compatible chat calls (local Ollama or OpenRouter), streaming, hard deadlines, model names |
| `tts.py` | Speech: `say`, a command template or the Kokoro server; sentence batching, streamed playback, `stop` |
| `focus.py` | Maps each spoken sentence to source lines / figure parts and writes focus events (`focus.json`, `focus.jsonl`) |
| `sentence_map.py` | The selection's source sentences (ids, char spans, screen and page line rects) built before speech; forward-only alignment of each spoken chunk to its source span |
| `textfold.py` | The shared text fold (match key with offsets, fold and match tokens) every text comparison uses; `tracker.swift` carries the same steps |
| `document.py` | Reads the source PDF / Markdown in the background, figure labels for the prompt, PDF text for Read area |
| `grounding.py` | Vision-model grounding of a named figure part on the figure crop (`ground_prompt`, `parse_ground_box`) |
| `docmap.py` | Document parsing and page / figure / line matching (also run by path inside the doc venv) |
| `pdfpage.py` | pypdfium2 page text and rendering, imported by sibling path from doc-venv subprocesses |
| `layoutdet.py` | PDF layout detector (PP-DocLayout-S on onnxruntime, inside the doc-venv parse only): model lookup and sha256 check, caption pairing, snapping, detector paragraphs, `layout_*` events; `layoutdet.py fetch` is the installers' model download |
| `provider.py` | Which endpoint a purpose (`llm`, `ground`) uses: local or OpenRouter; Keychain key lookup |
| `selog.py` | Paths, config loading, the run log (`runs.jsonl`), log rotation, the pill state file |
| `observe.py` | Capture cropping / keeping, memory and Ollama status, `speakeasy last` / `stats` output |
| `stats.py` | Usage counts from `runs.jsonl` (`speakeasy stats --json`) |
| `highlight.py` | `speakeasy highlight SRC DST`: crops a screenshot to the selection-highlight pixels |
| `tracker.swift` | The focus tracker (`speakeasy-track`): native panel, AX / OCR following |
| `install.sh`, `speakeasy` | Installer (copies every `src/*.py` into `~/.speakeasy/bin`) and the CLI launcher |

## Data flow

1. **Hotkey → app** (`app/Sources/Speakeasy`): Control-Option-S reads the selection through Accessibility, screenshots
   its bounds (or the focused window when nothing is selected), and writes the per-line bounds and document hint for the backend.
2. **App → backend**: the app runs `speakeasy speak --capture-method … --capture-rect … --line-bounds … --doc-hint …` with
   the text on stdin, and starts the tracker for the source window.
3. **Backend → LLM**: `speakeasy.py` decides text vs vision (`vision_gate`), builds the prompt (`prompts.py`) and streams
   the rewrite through `llm_client.py`. `document.py` reads the source document alongside; `grounding.py` asks the vision
   model for figure parts. Falls back to the deterministic reader when the model is down or rejected.
4. **Backend → TTS**: each finished sentence goes to `tts.Speaker` (via `state.SINK`), which synthesises and plays it.
5. **Backend → live focus file**: as each sentence starts playing, `focus.py` writes `~/.speakeasy/focus.json` (rect,
   source lines, document anchors; never the spoken text) and appends a hashed copy to `logs/focus.jsonl`.
6. **Tracker → panel**: `speakeasy-track` polls `focus.json`, re-finds the source lines on screen (AX ranges, OCR, document
   anchors) and moves its click-through `NSPanel`. It exits when speech finishes or the speaker exits.

## Files each component writes

| Component | Files |
|---|---|
| App | `~/.speakeasy/config` (menu toggles), `~/.speakeasy/tracker.pid`; `/tmp/speakeasy_cap_sel.png`, `/tmp/speakeasy_cap_lines.json`, `/tmp/speakeasy_cap_doc.json` (capture, line bounds, document hint for one run) |
| Backend | `logs/runs.jsonl` (one record per run), `logs/focus.jsonl`, `focus.json`, `state.json` (pill state), `speak.pid` while speaking, `last_capture.png` (unless `SPEAKEASY_KEEP_CAPTURE=0`), `cache/doc/` (parsed documents); temp files `speakeasy_cap_*` (crops, grounding crops) and `speakeasy_*.wav` (Kokoro audio), removed after the run; `speakeasy_doc_*` (downloaded PDFs, removed after a day) |
| Tracker | `logs/tracker.jsonl` only |
| `install.sh` | `~/.speakeasy/bin/*`, `config` defaults, `logs/tracker_build.log`, `logs/app_build.log`, the `com.speakeasy.tts` LaunchAgent (its log: `logs/tts-server.log`) |

Paths are relative to `~/.speakeasy` unless shown otherwise. Logs rotate at 5 MB (5 kept).

## Rule: flat modules, imported by path

Every backend module is a flat file in `src/` (no packages). The backend runs as `python3 src/speakeasy.py`, from
`~/.speakeasy/bin`, or from `Speakeasy.app/Contents/Resources/backend`. Each puts its own directory on `sys.path`. The
doc-venv subprocesses import `docmap` / `pdfpage` the same way (`sys.path.insert(0, <src dir>)`), so those two keep their
names and stay importable on their own. `install.sh` and `app/bundle_python.sh` copy every `src/*.py`, so a new module
needs no installer edit. Shared state lives only in `speakeasy_state.py`: modules read and assign `state.FOCUS`, never a
copied name. Module names must not shadow a stdlib module or an installed package (macOS file names are case-insensitive).
