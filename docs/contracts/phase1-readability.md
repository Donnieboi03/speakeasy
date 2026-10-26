# Phase 1 contract: readable codebase, behaviour unchanged

Scope: changes A–H of the approved Phase 1 plan. This file describes **observable behaviour** (CLI, files, logs,
processes, exit codes). It does not prescribe code. Anything not listed here must behave exactly as before the phase
(baseline recorded at commit `465da43`).

Terms: *home* = `$SPEAKEASY_HOME` (default `~/.speakeasy`). *runs log* = `home/logs/runs.jsonl`. *focus log* =
`home/logs/focus.jsonl`. *live focus file* = `home/focus.json`. *tracker log* = `home/logs/tracker.jsonl`.
*Text logging on* = environment/config `SPEAKEASY_LOG_TEXT` is exactly `1` (same rule `runs.jsonl` uses today).

## Global invariants (hold after every change)

- CLI subcommands, flags, defaults, stdout, stderr warnings and exit codes are unchanged, except the
  `--capture-method` choices in C.
- For the same input and environment, a runs-log record has the same key set as before. The only exceptions are the
  keys removed or added in B, D and E, which are listed there.
- The focus log, live focus file and tracker log keep every field they had, except the fields removed in C and the
  text fields replaced in E.
- No new files are written under home, `/tmp` or `$TMPDIR`. Files removed in C are simply no longer written.
- Speech never waits on logging, document reading or grounding (unchanged).
- The CI smoke in `.github/workflows/macos.yml` passes unchanged: the pyflakes gate, the backend `speak --dry-run`
  (prints `Ann: ZIP is 94110.`, exit 0) and `stats --json` (exit 0).
- Every baseline suite gives the same pass/fail counts and the same failing check names as the baseline, except
  checks that assert something this contract removes. Those are the `highlight` capture method, `expires`, `track.json`,
  `overlay.json`, `hs.log` and text in the focus log; the verifier rewrites them. Implementers edit nothing under `tests/`
  except `tests/audit/SCHEMA.md` and `tests/audit/run_audit.sh` (see H).

## A. Automatic copy lists

- `src/install.sh` installs **every** `src/*.py` plus the `src/speakeasy` launcher into `home/bin/`. A new module
  added to `src/` is installed with no edit to `install.sh`.
- `app/bundle_python.sh` copies **every** `src/*.py` into `Contents/Resources/backend/`. Its generated `speakeasy`
  launcher is unchanged: bundled CPython, `SPEAKEASY_DOC_PYTHON` and `SPEAKEASY_TRACKER` defaults as today.
- Neither script copies `__pycache__`, `tracker.swift`, `install.sh` or files outside `src/`.
- After A+B, `src/ground.py` does not exist, so it is not installed or bundled. `install.sh` deletes a stale
  `home/bin/ground.py` left by an earlier install.
- Failure cases are unchanged: `install.sh` still stops (`set -e`) if a copy fails, and `bundle_python.sh` still
  exits non-zero (`set -euo pipefail`).

## B. Florence path removed

- **No process whose argv contains `ground.py` is ever spawned**, in any mode, for any `SPEAKEASY_GROUND` value. This
  includes reading a PDF whose page has figures with `SPEAKEASY_GROUND=auto`. That was the live bug: reading any PDF
  figure started the Florence worker.
- Grounding is only the vision-model path. Per grounding request, the doc-venv python is spawned once with `-c` to
  render the crop through `pdfpage` (unchanged). After that comes one non-streaming `/chat/completions` POST: to
  OpenRouter when `provider.cloud("ground")` is usable, else to the local `SPEAKEASY_LLM_URL` with the vision model.
- `SPEAKEASY_GROUND` (case-insensitive) accepts:
  - `auto` (default; also used when unset or empty) and `local`: local vision-model grounding.
  - `openrouter`: cloud grounding when a key and URL are usable, else local. `ground_provider_configured` is
    unchanged.
  - `off`, `0`, `no`, `false`: no grounding requests at all. The focus box falls back to label/panel/figure targets,
    as today.
- **Any other value** (including the legacy `florence` and `vlm`) behaves exactly like `auto`. It is reported, never
  routed silently:
  - Every runs-log record of that run carries the new key `ground_mode_note` =
    `"SPEAKEASY_GROUND=<value> unknown; using auto"`, with `<value>` lowercased and cut to 24 chars.
  - The key is absent when the value is valid.
  - Nothing goes to stderr (stderr stays as today).
- `SPEAKEASY_FLORENCE` and `SPEAKEASY_GROUND_MODEL` have no effect. `install.sh` no longer writes `SPEAKEASY_FLORENCE`
  to config. An existing line in config is ignored, and install may leave it.
- Focus-method counters (`focus_methods` in the runs log) only ever use `ground-vlm` and `ground-miss`. The key
  `ground-florence` never appears.
- README and `provider.py` docs no longer mention Florence-2, `florence`, `vlm`, `SPEAKEASY_FLORENCE` or a Hugging
  Face download.

## C. Hammerspoon leftovers removed

- **Tracker files:**
  - The tracker never creates or writes `home/track.json` (not per frame, not at exit) or `home/overlay.json`.
  - All tracker log records (`start`, `frame`, `draw`, `hide`, `draw-failed`, `exit`, …) keep their fields.
  - `overlay` in the `start`/`exit` records is always `"swift-panel"`. The `--overlay` argument is accepted and
    ignored.
- **Draw failures:** `draw-failed` is recorded **only in the tracker log**, with the same fields
  (`ts, tracker_pid, seq, why, after_ms: 300`), and still counts towards `draw_failed` in the exit record. The tracker
  never writes `home/logs/hs.log`. The one-line stderr message (`NATIVE PANEL FAILED TO DRAW: …`) stays.
- **Tracker unchanged:** frame-by-frame box behaviour, hide/unhide, exit reasons and exit timing are unchanged. The 10
  XCTests still pass.
- **App:**
  - Launching Speakeasy.app never writes `SPEAKEASY_HOTKEY` to config and never runs `hs`.
  - An existing `SPEAKEASY_HOTKEY` config line is ignored.
  - `install.sh`'s closing message no longer mentions the Hammerspoon handover.
- **CLI:**
  - `--capture-method` accepts exactly `bounds`, `area`, `no-selection-window`, `no-selection-screen` and `none`.
  - `highlight`, `select` and `mouse` are rejected by argument parsing: exit 2, usage on stderr. No runs-log record is
    written, no capture file is deleted, and the live focus file is not touched.
  - The `speakeasy highlight SRC DST` subcommand is unchanged.
- **Focus events** (show records, in both the focus log and the live focus file):
  - No longer carry `expires`, `border` or `ref_src`.
  - Still carry, with unchanged meaning: `state, seq, ts, pid, rect, color, opacity, src, range, doc, shape, nlines,
    cue, cue_kind, header, clamped, doc_transform, draw, ref`, plus `hold`/`late_fit` where emitted today.
  - `ref` (the faint back-reference outline) is still emitted and still drawn.
- Audit scripts that read the removed files or fields (`audit_tracker.py`, `audit_perf.py`, `run_health.py`,
  `audit_focus.py`) are updated by the verifier, not the implementer.

## D. Dead code removed

- No observable change from removing `_SUP`, `_RATE`, `_LOWBETTER`, the unreachable tail of `check_superlatives`, the
  `if False` / `pass` no-ops, the unused imports and `Speaker.used_say`. `_NUMW` stays.
- The superlative guard gives the same text on the same inputs. Superlative suite results are unchanged.
- **Merged deadline fetch:** one helper with the same semantics as both old ones:
  - Hard wall-clock deadline over connect, send, server time and read.
  - The JSON variant returns parsed JSON; the TTS variant returns raw bytes.
  - On timeout it raises `TimeoutError`, with the same messages as today: `request exceeded <t>s` for LLM calls and
    `TTS request exceeded <t>s` for TTS. Those strings appear in runs-log `error` / `tts_error` and must not change.
  - Any other exception is re-raised unchanged.
- **Grounding attributes now logged:** the two write-only grounding attributes become runs-log keys:
  - `ground_provider`: `openrouter` | `local`, for the last grounding request sent in the run.
  - `ground_box_convention`: `yx` (Gemini `box_2d`) | `xy` (Qwen `bbox_2d`), for the last grounding answer that
    parsed into a box.
  - Each is present only when a grounding request was sent in the run, or an answer parsed for the convention. Both
    are absent otherwise and are never the model text.
- pyflakes on `src/*.py` no longer reports these: `selog._ram_total_mb`, `selog._home_rel` and
  `observe.is_temp_path` / `observe.read_runs` unused in `speakeasy.py`, and the unused local `ents`. The CI pyflakes
  gate still passes.

## E. No text in persistent logs by default

- **Focus log, text logging off (default):**
  - `map` records: no `text`. They gain `text_len` (int) and `text_hash`.
  - `show` records: no `text`. They gain `text_len` and `text_hash`.
  - No `src`. They gain `src_n` (number of lines) and `src_hash`.
  - `doc.anchors`, when present, is a list of 4-number boxes `[x0, y0, x1, y1]` (the 5th text element is dropped).
    Same order and count as before.
  - No caption text anywhere in the record. A figure caption used as a tracker anchor travels in `src`, so it is
    covered above.
  - `doc.label` is unchanged (see residuals in the Host brief).
- **Hash rule:** `*_hash` = first 12 hex chars of SHA-256 over the UTF-8 of the exact value that text logging would
  log. For `text` that is tag-stripped and cut to 120 chars (map) or 80 chars (show). For a list, the items are joined
  with `\n`. `*_len` = character length of that same value.
  - The `*_len` / `*_hash` / `src_n` keys are present in **both** modes whenever the source field is non-empty.
- **Focus log, text logging on:** records also carry `text`, `src` and full anchors exactly as before the phase,
  minus the C removals.
- **Live focus file:**
  - Carries `src` and full `doc.anchors` with text, because the tracker needs them.
  - **Never carries `text`**, whatever text logging is set to.
  - Not written in `--dry-run` (unchanged). Overwritten by the next event and reset to `{"state": "clear"}` on stop
    (unchanged).
- **Runs log:**
  - `error`, `llm_error`, `stream_note` and `notes` contain no model-output snippet and no table column name or cell
    value unless text logging is on.
  - Default for the superlative note: `superlative removed: superlative '<word>' contradicts its column`. `<word>` is
    the matched superlative from the fixed vocabulary (e.g. `highest`).
  - With text logging on, the note stays exactly as today: `… '<word>' on <value> contradicts <column>`.
  - Default for the injection check: `… injection obeyed (output reduced to <n> words)`. With text logging on:
    today's `(output reduced to: '<first 40 chars>')`.
  - All other error/note wording is unchanged.
  - `input`/`output` keys only with text logging on (unchanged).
- Rotation (`rotate_logs`) is unchanged.

## F. `speakeasy.py` split into flat modules

- **New modules:** `src/` gains `speakeasy_state.py`, `focus.py`, `document.py`, `grounding.py`, `tts.py`,
  `prompts.py` and `llm_client.py`, all flat in `src/`.
  - Each imports cleanly on its own, with `src/` on `sys.path`, under both Python 3.12 (runtime venv) and the system
    `python3`, with no import cycle errors.
  - `docmap.py` and `pdfpage.py` stay flat and keep their names: they are imported by sibling path from
    subprocesses.
  - On the case-insensitive macOS filesystem, no new module name may shadow a stdlib module or a package installed in
    the runtime or bundled venv (e.g. `tts.py` vs a `TTS` package).
- **Entry point:** `src/speakeasy.py` is still the entry point (`python3 src/speakeasy.py …` and the `speakeasy`
  launcher). `import speakeasy` still exposes `main`, `parse_ground_box` and `ground_prompt` with identical results.
  `tests/test_ground_box.py` passes unedited.
- **One shared state:** focus, document, sink, stop event, grounder, area fit and the docmap handle exist once per
  process, and every module sees the same live value. For example:
  - a SIGTERM (`speakeasy stop` or a hotkey toggle) stops TTS and clears the focus box exactly as before;
  - a document read in the background is visible to focus mapping and grounding.
- **Unchanged:** prompts sent to the LLM (system, read, vision, screen, focus-tag and doc rules) are byte-identical.
  Model names, endpoints, timeouts, streaming behaviour and fallbacks are unchanged.
- Installed (`home/bin`) and bundled (`Resources/backend`) copies include the new modules through A. Both work with no
  repo checkout present.

## G. Renames

- Renames only change internal identifiers in touched modules.
- Unchanged: every log key, focus-event field, runs-log value, `focus_methods` key, CLI flag, env var, file name and
  `docmap` function used by tests or `tools/` (`docmap._toks`, `ground_label`, `match_page`, …).
- No behaviour change on any baseline suite.

## H. Documentation matches behaviour

- **README:**
  - Install target is `/usr/local/bin` when writable, else `~/.local/bin`.
  - TTS default is `say` in the code. `install.sh` sets `kokoro` when the Kokoro venv is present.
  - One local model, `qwen3-vl:4b-instruct`, handles text, images and grounding (no separate 3B text model). Resource
    numbers are re-measured or clearly dated.
  - No Florence and no `SPEAKEASY_FLORENCE` row. Grounding is `SPEAKEASY_GROUND=auto|local|openrouter|off`.
  - The PyMuPDF/AGPL caveat applies only to test dependencies, not the runtime.
  - Temporary files: the crop is kept at `home/last_capture.png` unless `SPEAKEASY_KEEP_CAPTURE=0`. The line-bounds
    and hint files are deleted.
  - Text claims say "no text in runs/focus logs unless `SPEAKEASY_LOG_TEXT=1`; the live `focus.json` holds the
    current source lines while a run is active".
  - `capture_path` is home-relative when under home.
  - No `hs.log`, `track.json` or Hammerspoon handover mentions.
- `tests/audit/SCHEMA.md`:
  - `capture_method` ∈ {bounds, area, no-selection-window, no-selection-screen, none}.
  - `backend` ∈ {llm, fallback, verbatim, none}.
- `tests/audit/run_audit.sh` also runs `privacy_audit.py` against `$SPEAKEASY_HOME` (default `~/.speakeasy`),
  read-only, and only when a kept capture exists. A missing capture does not stop the remaining steps.
- New `docs/architecture.md`, one page:
  - module map (one line per `src/` module);
  - data flow from hotkey → app → backend → LLM/TTS → live focus file → tracker → panel;
  - files each component writes;
  - the "flat modules, subprocess-imported by path" rule.
