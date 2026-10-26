# Phase 7 contract: a usable app (metrics, the bar, the design system)

Scope: make Speakeasy pleasant to use every day for its one user. Three workstreams: **A** metrics the user acts on,
**B** a persistent bar at the bottom of the screen that makes Read area one click away, **C** the design system of
`docs/design.md` applied to every surface. This file prescribes **observable behaviour**, not code. Terms are as in the
earlier contracts (`phase1-readability.md` to `phase6-block-classifier.md`).

- *Bar* = the persistent floating control (section B); it replaces today's pill (`Pill.swift`).
- *Read* = one `speak` / `selection` run started from the app, the CLI or the bar.
- *Show* = one focus-log `show` record (one cue for one sentence).
- *Reading log* = the stats window (today "Speakeasy stats").
- *Live state* = `~/.speakeasy/state.json` (written by the backend, read by the app; never a log).
- *Tokens* = the colour, type, spacing and radius values of `docs/design.md` sections 1 to 3.

Privacy rule for the whole phase: **no document text, window title or path in any log**. Document identity is
`doc_path_hash` only. Document names and paths live in a separate local library file outside the logs (A5.3; the user
approved it, on by default). Old records that still hold text are scrubbed in place (A5.4, required).

## Prior art (decided before this contract)

| Workstream | Decision | What is adopted | Licence / link | Why |
|---|---|---|---|---|
| A. Metrics | **Build fresh** on the existing logs | The run, focus and tracker JSONL logs Speakeasy already writes, read by `stats.py` | Own code | Wispr Flow's Home (word count, WPM, streak; [help](https://docs.wisprflow.ai/articles/5096240724-navigating-the-wispr-flow-app-desktop-ios-and-android)) is the pattern being moved away from: streaks and word totals change nothing a single user does. Per-document progress follows reading tools such as Zotero and Readwise Reader (concept only; Zotero is AGPL-3.0, no code). ActivityWatch (MPL-2.0) was rejected: a second daemon and database for one user, when JSONL plus `stats.py` already hold every needed field |
| B. Bar | **Build on top** of Speakeasy's own pill | The existing non-activating `NSPanel` pill (already adapted from [open-wispr](https://github.com/human37/open-wispr), MIT, credited in `app/NOTICE`); behaviour modelled on Wispr Flow's Flow Bar ([placement](https://docs.wisprflow.ai/articles/1790396454-move-and-dock-the-flow-bar-on-desktop), [troubleshooting](https://docs.wisprflow.ai/articles/5002934560-why-is-the-wispr-bar-is-not-appearing-or-disappearing)); temporary hotkeys through [KeyboardShortcuts](https://github.com/sindresorhus/KeyboardShortcuts) (MIT, already a dependency) | MIT | Wispr's bar: bottom centre above the Dock, follows the pointer's display, flush to the edge in full-screen Spaces, draggable to a side and remembered, right-click "Hide for 1 hour" at rest only, shortcut hint on hover. [VoiceInk](https://github.com/Beingpax/VoiceInk) (GPL-3.0) mini recorder: behaviour reference only, **no code**, GPL would bind the app. [DynamicNotchKit](https://github.com/MrKai77/DynamicNotchKit) (MIT) rejected: top / notch placement sits on PDF viewer toolbars and tab bars, and it adds a dependency for about 150 lines of panel code Speakeasy already has. VoiceInk's issue [#572](https://github.com/Beingpax/VoiceInk/issues/572) (panel rebuilt on every trigger, 250 ms late) is the reason the bar is created once and kept |
| B. Hiding from capture | **Use as-is** what macOS offers, no workaround | Own captures exclude the app with `SCContentFilter(excludingApplications:)` (already in `AreaSelector.capture`) | Apple frameworks | Apple DTS: "there are no public APIs for preventing screen capture"; `sharingType = .none` is ignored by ScreenCaptureKit on macOS 15.4+ ([forum](https://developer.apple.com/forums/thread/792152)). Third-party screen sharing will see the bar; the user hides it with a hotkey (B5) |
| C. Type | **Use as-is** | Source Serif 4, IBM Plex Sans, IBM Plex Mono | SIL OFL 1.1 ([source-serif](https://github.com/adobe-fonts/source-serif), [IBM/plex](https://github.com/IBM/plex)) | Bundling is allowed with the licence text; reserved names only bind modified fonts |
| C. Palette, quirk, layouts | **Build fresh** | `docs/design.md` | Own | Brand-specific by definition |

## 0. Evidence (the user's own logs, read-only, 2026-10-26)

Counted from `~/.speakeasy/logs/` with field names and counts only; no text was printed.

| Measurement | Result | Consequence |
|---|---|---|
| Runs | 120 records, 2026-10-24 to 2026-10-26. 27 from the menu-bar app: 15 Read area, 9 selection, 3 nothing captured | Read area is already the main path. The bar (B) targets it |
| What was read (menu-bar runs) | 24 of 27 in figure mode; one document hash; pages 3 to 7 | Per-document progress and distinct figures are the useful counts; "words in" is not |
| Time to first audio | Menu-bar: median 3.9 s, slowest 1 in 10 at 16.5 s or more, range 1.1 to 22.1 s. All runs by provider: OpenRouter 2.3 s median (14 runs), local 6.1 s (85 runs) | The user's real trade-off (cloud is faster) is invisible today. Show median, slowest 1 in 10, and the provider split |
| Listening | 3,485 words spoken in 19.9 min, 175 words per minute | Listening speed is cheap to show; a speed control is later |
| Speech engine | Kokoro 105 runs, `say` 3; one recorded Kokoro outage fell back to `say` | A silent fallback the user should hear about |
| End of read (tracker exits) | 53 runs: 38 finished, 14 terminated (stop or a new read), 1 without Screen Recording | No runs field says finished or stopped. Added (A3) |
| Highlight | 586 shows, 75 tracker corrections, 1 `draw-failed`. Hides: 83 content-changed, 36 speech-finished, 16 out-of-window, 10 tab-switch | At most about 87 % of shows needed no correction (corrections carry no show id yet, so this is an upper bound). Added: per-show result (A3) and a user "wrong spot" flag (B6) |
| Logs the app writes | None | Added: `logs/app.jsonl` (B7) |
| Old records holding text | `focus.jsonl`: 1,265 `map` / `show` records up to 2026-10-25 04:24 carry a `text` value; of those, 395 also carry source lines (`src`), 11 carry `ref_src` and 12 carry a multi-word `doc.label`. `runs.jsonl`: 7 records on 2026-10-24 carry document numbers and a column name inside `error` and `llm_error` (the superlative note written while text logging was on). Records written with `SPEAKEASY_LOG_TEXT` unset are clean; today's writer already emits the text-free superlative note | Stats read a whitelist of fields only (A5). The user approved scrubbing these records: the scrub command is a required build step (A5.4, build step 8) |

## 1. Where the code goes (merge rule)

**Order: Phase 7 before Phase 6** (the user's decision, 2026-10-26). Phase 7 starts from the `main` commit that lands
Phase 5. All Phase 7 steps build on that commit and on each other only; none waits for Phase 6.

Phase 6 (`phase6-block-classifier.md`) starts after Phase 7 merges and **builds on it**:
- Phase 6 work starts from post-Phase-7 `main`, not post-Phase-5 `main`. Its benchmark baselines (B5') are recorded on
  that commit. Phase 7 must leave the Phase 2 bench's gated metrics unchanged against the Phase 5 baseline (A7.10), so
  the re-recorded baselines equal B5' apart from run-to-run noise.
- Phase 6 keeps Phase 7's outputs and extends them: uncaptioned figures and tables get `fig_id`s (`p<page>:fig<n>` /
  `tab<n>` in the parse's reading order, A2), their cues use the C1 tokens and the margin-tick rules, its tracker `ocr`
  subcommand sits next to the Phase 7 drawing and `cue.json` code without changing them, and any new log key joins
  the A5 whitelist or stays out of `stats.py`.
- Phase 6's new runs keys follow A5 (no text); `speakeasy logs --scrub` (A5.4) learns any new text-bearing key Phase 6
  adds under `SPEAKEASY_LOG_TEXT=1`.
- Phase 6's contract section 1 ("Work starts from post-Phase-5 `main`") is read as "post-Phase-7 `main`".

| File | Phase 7 changes |
|---|---|
| **new `app/Sources/Speakeasy/Tokens.swift`** | Every colour, font, size, spacing and radius of `docs/design.md`; the only place a hex value may appear in the app |
| **new `app/Sources/Speakeasy/Bar.swift`** | The bar (replaces `Pill.swift`, which is deleted) |
| **new `app/Sources/Speakeasy/Snapshot.swift`** | `--snapshot` and `--self-check` modes (C4) |
| `app/Sources/Speakeasy/App.swift` | Menu (design 6.2), settings window, temporary hotkeys, `app.jsonl` writer, live problem checks |
| `app/Sources/Speakeasy/AreaSelector.swift` | No activation (B4); crosshair through an always-active tracking area; size readout; bar above the overlay |
| `app/Sources/Speakeasy/Reader.swift` | Selection capture through ScreenCaptureKit with the app excluded (B5.2); sends control signals (B6) |
| `app/Sources/Speakeasy/Stats.swift` | The Reading log (design 6.3) on stats schema 2 |
| `app/Sources/Speakeasy/Cloud.swift`, `Permissions.swift`, `Updater.swift` | Restyle and copy only |
| `app/Resources/Fonts/` (new), `app/build.sh`, `app/NOTICE` | The six font files (3 faces, 2 weights) and their OFL texts |
| `src/stats.py`, `src/observe.py` | Schema 2 (A4), the field whitelist (A5), the text CLI output, `speakeasy logs --scrub` (A5.4) |
| `src/document.py` | Writes the document library entry (name, path) after resolving a document (A5.3) |
| `src/speakeasy.py`, `src/tts.py`, `src/selog.py` | `end`, sentence counts, pause / skip signals, live-state fields (A3, B6) |
| `src/focus.py`, `src/tracker.swift` | `fig_ids`, cue tokens, the margin tick, `cue.json`, per-show results (A3, C) |
| Docs | `README.md` (Stats, the bar, fonts in Credits), `docs/architecture.md` (new files written), `tests/audit/SCHEMA.md` (new keys) |

Implementers add tests in **new files only** (`tests/test_stats_v2.py`, `tests/test_controls.py`, and an
`app/Tests/SpeakeasyTests` target). The UI harness and fixtures under `tests/audit/ui/` and `tests/audit/stats/` belong
to the verifier.

## A. Metrics the user acts on

### A1. Keep, delete, add

| Metric today | Decision | Why |
|---|---|---|
| Reads | **Keep**, as today / 7 days | Basic use count |
| Audio minutes listened | **Keep** (renamed "min listened") | Honest measure of use |
| Median first audio | **Keep and extend**: median, slowest 1 in 10, split local / OpenRouter, 14-day trend | The one speed number the user feels and can change (cloud on or off, model warm) |
| Figures explained | **Keep, redefine**: distinct figures, not runs | 24 figure-mode runs covered far fewer figures; re-reads inflated it |
| Tables read | **Merge** into distinct figures and tables | Same reason |
| Lists read | **Delete** | Nothing to act on |
| Words in | **Delete** from UI and JSON | Internal; for figure runs it counts caption and OCR words |
| Words listened | **Delete** from UI; kept only inside listening speed | Vanity total |
| Time saved (238 wpm) | **Delete** | A guess shown as a number; meaningless for figure explanations |
| Streak, longest streak, heatmap | **Delete** | Gamification for a single user; no decision follows from it |
| Today words (menu line) | **Replace** with "Today: N reads, M min" | |
| (new) | **Add** per-document progress and resume point | "Where was I in this paper" is the daily question |
| (new) | **Add** highlight quality: on target first time, flagged wrong spot, sentences with no cue | Tells the user whether to trust the box, and gives the developer a real-use signal |
| (new) | **Add** finished vs stopped | A read stopped early usually means a wrong area or a bad rewrite |
| (new) | **Add** problems: Kokoro down, local model down, cloud fell back, layout model missing, highlight failed, permission missing, slow first audio | Silent fallbacks the user should know about |
| (new) | **Add** listening speed (words per minute over the window) | Cheap now; the base for a later speed control |

### A2. Definitions

Window: the Reading log has "Today", "Last 7 days" (default) and "Last 30 days". Reads = `speak` / `selection` runs
with `dry_run` false, from `app` `menubar` or `cli`.

| Metric | Definition | Source | Shown in |
|---|---|---|---|
| `reads` | Count of reads in the window | existing: `command`, `dry_run`, `ts` | Menu line, Reading log footer, CLI |
| `listened_min` | Sum of `audio_s` / 60, one decimal | existing: `audio_s` | Menu line, Reading log footer |
| `first_audio_s.median`, `.p90` | Median and 90th percentile of `timings.tts_first_audio_ms` / 1000 over reads that spoke | existing | Reading log (hero number), Cloud window line, CLI |
| `first_audio_s.by_provider` | The same per `llm_provider` (`local` when absent) | existing: `llm_provider` | Reading log, Cloud window |
| `first_audio_s.trend` | Daily median, last 14 days | existing | Reading log (14 marks in Plex Mono, no chart library) |
| `items` | Distinct `(doc_path_hash, fig_id)` pairs explained in the window, split figures / tables | **new** `fig_ids` (A3) | Reading log, per document |
| `completion` | `finished`, `stopped`, `error` counts and `finished / reads` | **new** `end` (A3) | Reading log (secondary line), CLI |
| `documents[]` | Per `doc_path_hash`: pages listened (set), items (set), reads, last read time, resume point | existing `doc_path_hash`, `page`; new `fig_ids`, `sent_spoken`, `sent_n` | Reading log (dominant element), bar hover (last place) |
| `resume` | For a document's newest read: `page`, its last `fig_id` (or null), `sentence = sent_spoken`, `of = sent_n` | new | Reading log, bar hover ("p. 5, Fig. 4") |
| `highlight.on_target_rate` | Shows with `landed: true` / shows with a cue (A3 per-show result), window-wide | **new** tracker `cue_result` | Reading log |
| `highlight.flagged` | `user_flag` events in the window | **new** `app.jsonl` | Reading log; the list of flagged reads (time, page, cue kind) |
| `highlight.no_cue` | Sentences whose map method is `hold` or `none` / sentences | existing `focus_methods`, `sentence_map.sentences` | Reading log (secondary) |
| `speech_wpm` | Sum `words_spoken` / sum `audio_s` × 60 over the window | existing | Reading log footer |
| `problems[]` | Section A2.1 | mixed | Menu (newest open problem only), bar error state (current read), Reading log list |

`fig_id` format: `p<page>:fig<n>` or `p<page>:tab<n>`, where `n` is the item's 1-based index in the page parse's
reading order. A page-local index is not text. Area runs without a document page have no `fig_id`.

### A2.1 Problems

Each problem has an id, a count in the window, the last time, one sentence of copy (design section 5) and one action.

| id | Detected when | Source | Copy | Action |
|---|---|---|---|---|
| `kokoro-down` | `tts_requested` = `kokoro` and `tts` = `say`; or the live check fails | existing fields; live: the TTS URL's host answers within 300 ms | "Kokoro is not running. N reads used the macOS voice." | "Restart Kokoro" (`launchctl kickstart -k gui/<uid>/com.speakeasy.tts`) |
| `model-down` | `backend` = `fallback` with an unreachable-model error class; or live `/api/tags` fails or lacks the model | existing `backend`, `llm_error` class; live | "The local model did not answer. N reads were read without a rewrite." | "Open Ollama" |
| `cloud-fallback` | `llm_provider_configured` = `openrouter` and `llm_provider` = `local` | existing | "OpenRouter failed N times. Those reads used the local model." | "Open Cloud settings" |
| `layout-missing` | `doc_parse.layout.src` = `heuristic` with reason `model-missing`, `model-mismatch`, `no-onnxruntime` or `load-failed` | existing | "The PDF layout model is missing. Figures are found by rules only." | "Copy fetch command" (`layoutdet.py fetch`) |
| `highlight-failed` | Tracker `draw-failed`, or exit reason `no_screen_recording` | existing tracker | "The highlight could not be drawn on N reads." | "Open Screen Recording settings" |
| `permission` | Live: Accessibility or Screen Recording missing | live | "Screen Recording is off. Figures and the highlight need it." | "Open Screen Recording settings" |
| `slow-start` | Reads with first audio above 10 s | existing | "N reads took over 10 s to start." | "Open Reading log" (no fix button; information only) |

Live checks run off the main thread, at most once per 60 s, when the menu opens or the Reading log is shown, and at
launch. A problem is **open** if it occurred in the last 24 h or its live check fails now.

### A3. New log fields (no text)

`runs.jsonl` (speak / selection):

| Field | Type | Meaning |
|---|---|---|
| `end` | `finished` \| `stopped` \| `error` | `stopped` when SIGTERM (stop, or a new read) arrived before the last sentence finished playing |
| `sent_n` | int \| null | Sentences planned for speech (the speaker's queue size at the end of the rewrite) |
| `sent_spoken` | int | Sentences that started playing |
| `paused_ms` | int | Total time paused (B6); `audio_s` excludes it |
| `skips` | int | "Next sentence" requests honoured |
| `fig_ids` | list[str] | Distinct `fig_id`s that got a figure box, part oval or table row in this read |
| `tts_fallback` | null \| `unavailable` \| `error` | Kokoro requested but `say` used, and why (class only) |

`tracker.jsonl`:
- `correction` gains `seq` (the show it corrects).
- New `cue_result` {ts, tracker_pid, run_id, seq, cue_kind, first_draw_ms, corrections, max_shift_px, landed, hidden}:
  written when a show ends. `landed` = drawn within 300 ms of the show **and** no correction after the first draw that
  moved the box more than max(12 px, half a line height). `hidden` = the show ended hidden (app switch, scrolled off);
  hidden shows are left out of `on_target_rate`.
- `exit` gains `cues: {shows, landed, hidden}`.

Live files (not logs; cleared at idle):
- `state.json` gains `run_id`, `seq`, `sent_i`, `sent_n`, `cue_kind`, `paused`, `label` (for example "Figure 4"; live
  only, never copied into a log), `started_ms` (for the bar's elapsed counter).
- New `cue.json`, written by the tracker at most 4 times a second while a cue is drawn: `{run_id, seq, rect}` in global
  screen points. Deleted on tracker exit.

### A4. `speakeasy stats --json` schema 2

```json
{
  "schema": 2,
  "window_days": 7,
  "reads": {"today": 9, "window": 27},
  "listened_min": {"today": 14.0, "window": 19.9},
  "first_audio_s": {"median": 3.9, "p90": 16.5, "n": 27,
                    "by_provider": {"local": {"median": 6.1, "n": 13}, "openrouter": {"median": 2.3, "n": 14}},
                    "trend": [{"date": "2026-10-25", "median": 4.8}]},
  "items": {"figures": 4, "tables": 1},
  "completion": {"finished": 20, "stopped": 6, "error": 1},
  "highlight": {"shows": 120, "landed": 104, "hidden": 9, "flagged": 0, "no_cue_sentences": 11, "sentences": 160},
  "speech_wpm": 175,
  "documents": [{"doc": "4eb5eccca1cd", "name": "<PDF title or file name>", "pages": [3, 4, 5, 6, 7], "items": ["p5:fig1"],
                 "reads": 27, "last_ts": "2026-10-26T07:39:40-0700",
                 "resume": {"page": 5, "fig_id": "p5:fig1", "sentence": 8, "of": 12}}],
  "problems": [{"id": "kokoro-down", "count": 1, "last_ts": "2026-10-24T11:41:29-0700", "open": false}]
}
```

`--window 1|7|30` selects the window (default 7). Numbers above are illustrative except where they match section 0.
`speakeasy stats` (text) prints the same in five lines: reads and minutes, first audio, items and completion,
highlight, open problems. The CI call (`stats --json`, exit 0) keeps working. Schema 1 keys are removed in the same
release as the new Reading log (build step 3).

### A5. Privacy

1. `stats.py` reads a **whitelist** of keys from each record (the Source column of A2 and A2.1). It never reads
   `text`, `input`, `output`, `error`, `llm_error`, `notes`, `stream_note` or any `*_note` string. Error classes come
   from fields that hold class names only.
2. Output strings are limited to enums, hashes, ISO times, `fig_id`s and, when names are on, document names.
3. **Document library (on by default; the user approved storing names and paths).** Setting "Show document names in
   the Reading log", default on (config `SPEAKEASY_DOC_NAMES=on|off`).
   - Writer: the **backend** (`document.py`), the only component that knows the resolved path and its hash, adds or
     updates `{doc_path_hash: {"name", "path", "first_ts", "last_ts"}}` in `~/.speakeasy/library.json` after it
     resolves a document. Name = the PDF's title metadata when it is non-empty and under 120 characters, else the
     file name; for a browser PDF, the URL's last path segment. Written atomically (temp file in the same folder, then
     rename), file mode `0600`.
   - Not a log: it lives outside `logs/`, is never rotated, never copied into a log record, and `--scrub` leaves it
     alone.
   - Turning the setting off deletes the file and stops new entries; turning it on starts an empty file (earlier reads
     show as "Document 4eb5ec" until read again).
   - `stats.py` joins `name` into `documents[]` only from this file; `path` is never printed by `stats` and is stored
     for a later "open at page" action (out of scope here).
4. **Scrubbing old records (required, build step 8; approved by the user).** `speakeasy logs --scrub` removes text
   from existing logs **in place**:
   - Files: `logs/runs.jsonl`, `logs/focus.jsonl` and their rotated copies `.1` to `.5`. It also scans
     `tracker.jsonl*` and `app.jsonl*` and reports any forbidden key it finds there (expected 0) without changing them.
   - `focus.jsonl` records: each record is rewritten into the form the writer produces with `SPEAKEASY_LOG_TEXT`
     unset (README Observability): `text` becomes `text_len` + `text_hash`, `src` becomes `src_n` + `src_hash`, using
     the same hashing helper as `focus.py` (first 12 hex of SHA-256); `ref_src` is removed (`ref_lines` stays); a
     `doc.label` longer than a figure label (more than 3 words) is removed. Keys already present are not overwritten.
   - `runs.jsonl` records: `input` and `output` are removed. In `error`, `llm_error` and `notes`, a superlative note
     with a value and a column name ("superlative 'w' on <number> contradicts <name>") becomes today's text-free form
     ("superlative 'w' contradicts its column"); the stage prefix ("llm: superlative removed: ") is kept.
   - **No plain-text backup**: no `.bak`, no copy, no temp file left behind. Each file is rewritten through a temp file
     in the same folder that only ever holds scrubbed records, then renamed over the original; on any error the temp
     file is deleted and the original is untouched.
   - **Idempotent**: a file with nothing to scrub is not rewritten (same bytes, same modification time). A second run
     changes 0 records.
   - **Count-only event**: every run appends one record to `runs.jsonl`: `{"ts", "command": "scrub", "files": n,
     "records_changed": {"runs": n, "focus": n}, "fields": {"text": n, "src": n, "ref_src": n, "doc.label": n,
     "input": n, "output": n, "error": n, "llm_error": n, "notes": n}, "other_logs_with_text": n, "ms": n}`. No text,
     no paths. `stats.py` ignores `command: scrub`.
   - Safety: refuses while a read is running (`speak.pid` alive): "A read is running. Run the scrub after it ends."
     exit 2. Asks "Remove text from N records in M files? [y/N]" unless `--yes`. Prints counts only. If
     `SPEAKEASY_LOG_TEXT=1` is set, it scrubs anyway and prints "SPEAKEASY_LOG_TEXT=1 is set: new records will hold
     text again."
   - Not covered: `last_capture.png` (an image kept by `SPEAKEASY_KEEP_CAPTURE`, not a log) and `library.json`.
5. `app.jsonl` (B7) and `cue.json` follow the same rule: no titles, paths, text or app names.

### A6. Failure cases

| Case | Behaviour |
|---|---|
| `runs.jsonl` missing or empty | Schema 2 with zeros and empty lists; the Reading log shows its empty state |
| A malformed line | Skipped and counted in `skipped_lines` |
| Old records without the new fields | Counted where possible; `items`, `completion`, `highlight` count only records that have the fields; the Reading log prints "since Oct 27" under a metric with a shorter history |
| `tracker.jsonl` absent | `highlight` is null; the Reading log shows "No highlight data yet" |
| `library.json` unreadable | Names null, no error |
| Stats over 500 ms | Never blocks the menu: the menu line shows the previous value until the new one arrives |

### A7. Acceptance (metrics)

1. Fixture logs (verifier, `tests/audit/stats/`) with known answers: two documents, the same figure read three times
   (counts once in `items`), a stopped read, a Kokoro fallback, a cloud fallback, a layout-missing parse, records
   without new fields. `stats --json --window 7` equals the expected JSON exactly.
2. No key named `time_saved*`, `streak*`, `longest*`, `lists_read`, `words_in`, `days` in the output.
3. Privacy: a fixture whose records carry `text`, `input`, `output`, a sentence inside `error` and a title-like string
   inside `notes`. None of those strings appear in `stats --json`, `stats`, the Reading log snapshot's accessibility
   text, or `app.jsonl`.
4. Library: with names on (default), a read of the PDF fixture adds one `library.json` entry whose key equals the
   run's `doc_path_hash`, mode `0600`; `stats --json` shows its `name` and never its `path`; no log file contains the
   name or the path (grep over `logs/`). Names off: the file is deleted, every `documents[].name` is null, and new
   reads add nothing.
5. `cue_result` invariants on a recorded tracker fixture: `landed + not-landed == shows`; every `correction` has a `seq`
   that exists; `exit.cues` equals the per-show records.
6. Runs fields: a read stopped after its second sentence logs `end: stopped`, `sent_spoken: 2`; a finished read logs
   `sent_spoken == sent_n`.
7. Speed: `stats --json` on a copy of the user's real logs (120 runs, 30k tracker lines) returns in under 500 ms.
8. Problems: the fixture yields exactly the expected problem ids with counts; with Kokoro's LaunchAgent stopped, the
   live check opens `kokoro-down` within 60 s of the menu opening.
9. Scrub (fixture logs, verifier, `tests/audit/scrub/`), then on the user's real logs:
   - after `logs --scrub --yes`, zero records in `runs.jsonl*` / `focus.jsonl*` have `text`, `src`, `ref_src`,
     `input`, `output`, a `doc.label` over 3 words, or a superlative note with a number; on the real logs the event
     reports `records_changed` `{"focus": 1265, "runs": 7}` (or the counts present at run time if rotation moved
     records);
   - every scrubbed `focus.jsonl` record still parses and keeps its `ts`, `state`, `seq`, `rect`, `sentence_id`;
     `text_len` equals the removed string's length and `text_hash` matches `focus.py`'s helper on it;
   - record count and order per file are unchanged;
   - the logs folder holds no new file after the run (no `.bak`, no temp file), checked by listing before and after;
   - a second run reports 0 changed records and leaves every file's bytes and modification time unchanged;
   - an injected write failure leaves the original file byte-identical and no temp file;
   - with a fake live `speak.pid`, exit status 2 and nothing changed;
   - `stats --json` output is identical before and after the scrub (stats never read the scrubbed fields).
10. Bench unchanged: after steps 2 and 6 (the only steps that touch `focus.py`), the Phase 2 bench's gated metrics
    equal the Phase 5 baseline within its recorded noise (`phase2-benchmark.md`).

## B. The bar

### B1. Placement (decided)

**Bottom centre of the display, 8 pt above the Dock (inside `visibleFrame`)**, with two collision rules. Reasons:

- Read area is the main path (section 0) and needs a target near the content; the menu-bar item is a long trip and a
  menu closes on use.
- The user knows the spot from Wispr Flow, so there is no new habit to learn.
- Top centre was rejected: it sits on PDF viewer toolbars and editor tab bars, and a notch exists only on the built-in
  display. Side edges were rejected as the default: Stage Manager's strip lives at the left, and a side bar is farther
  from a centred reading column. Both edges stay available as user positions.
- The highlight moves with the voice and is usually above the bottom fifth of the screen; when it is not, the bar moves
  (collision rule 1).

Rules:
1. **Collision with the cue.** While a read runs, if the bar's frame grown by 24 pt intersects `cue.json`'s rect (fresh
   within 1 s), the bar slides (160 ms) to the bottom corner of the same display that is farther from the cue's centre.
   It returns 1.5 s after the cue stops intersecting the centre spot.
2. **Collision with the area being dragged.** While armed, the overlay's live rectangle counts as the cue.
3. **Display.** At idle the bar is on the display that holds the pointer. While armed, preparing, reading, paused or
   in error, it is on the display holding the source window's centre (the area's centre for Read area).
4. **Positions.** Bottom centre (default), bottom left, bottom right. Dragging the bar more than 4 pt snaps it to the
   nearest of the three on release; the choice is remembered per display arrangement.
5. **Dock and Stage Manager.** Always inside `visibleFrame`, so it clears the Dock at bottom, left or right. An
   auto-hide Dock may cover it while revealed. The panel uses `[.canJoinAllSpaces, .fullScreenAuxiliary, .stationary,
   .ignoresCycle]`, so it stays put in Mission Control and is never part of Stage Manager.
6. **Full-screen apps.** When the frontmost window covers its whole display, the idle bar is hidden (setting, default
   on). Armed, preparing, reading, paused and error states still show, 8 pt above the screen's bottom edge.
7. **Window level.** `.statusBar` normally (above documents, below menus and system alerts); above the area overlay's
   level while armed, so its hint stays visible.

### B2. States

Sizes and copy: `docs/design.md` 6.1.

| State | Shows | Entered when | Left when |
|---|---|---|---|
| `idle` | 56 × 8 stub with the margin tick (72 × 24 hit area) | App launch; a read ends; armed cancelled | Hover, click, hotkey, read starts |
| `hover` | Read area ⌃⌥A · Read selection ⌃⌥S · last place | Pointer rests 120 ms on the idle bar | Pointer leaves for 300 ms |
| `armed` | "Drag over what to read · Esc cancels" | Bar click, ⌃⌥A, menu "Read area" | Drag released (≥ 8 pt) → `preparing`; Esc, click on bar, ⌃⌥A, or a drag under 8 pt → `idle` |
| `preparing` | "Preparing <label>" + elapsed seconds + Stop | Capture done, backend started | First audio → `reading`; backend exit with no audio → `error` or `idle` |
| `reading` | Sentence ticks, "N of M", Pause, Next, Stop, Wrong spot; "Cloud" when the read used OpenRouter | `state.json` reports speaking | Speech ends → `idle`; Stop → `idle`; Pause → `paused`; error → `error` |
| `paused` | As reading, "Resume" instead of "Pause" | Pause | Resume → `reading`; Stop → `idle` |
| `error` | Brick tick, one sentence, one action | A problem detected in the current read (A2.1) or a capture failure | 8 s → `idle` with a brick tick until hovered; action clicked |
| `hidden` | Nothing | ⌃⌥B, right-click "Hide for 1 hour", full-screen rule, setting off | ⌃⌥B again, the hour ends, any read starts (the bar shows for that read only, unless the setting is off) |

### B3. Pointer and keys

Pointer:
- Click on the idle or hover bar (outside its buttons): arm Read area.
- Hover buttons: "Read area" arms; "Read selection" reads the frontmost app's selection (works because the bar never
  activates the app, so the selection stays live). The last place is plain text in Phase 7, not a button.
- Drag the bar: move and snap (B1 rule 4); never arms.
- Right-click at idle or hover only: "Hide for 1 hour", "Hide until ⌃⌥B", "Settings…".
- Reading: Pause / Resume, Next sentence, Stop, Wrong spot.

Keys (all rebindable in Settings, through KeyboardShortcuts):

| Key | Action | Registered |
|---|---|---|
| ⌃⌥S | Read selection; stop while reading (today's toggle) | Always |
| ⌃⌥A | Arm Read area; cancel while armed; while reading: stop, then arm (today's pill behaviour) | Always |
| ⌃⌥B | Show or hide the bar | Always |
| Esc | Cancel armed | **Only while armed**, then released, so Esc reaches the document at all other times |
| ⌃⌥P | Pause / resume | Only while preparing, reading or paused |
| ⌃⌥N | Next sentence | Same |
| ⌃⌥W | Wrong spot | Same |

### B4. Focus: never steal it

- The bar is an `NSPanel` with `[.borderless, .nonactivatingPanel]`, `canBecomeKey` and `canBecomeMain` false,
  `becomesKeyOnlyIfNeeded` true, `hidesOnDeactivate` false; its controls accept the first mouse click.
- The armed overlay no longer calls `NSApp.activate` and no longer re-activates the previous app afterwards (today's
  `AreaSelector.pick` does both). Esc comes from the temporary hotkey (B3); the crosshair from a tracking area with
  `.activeAlways` and `.cursorUpdate`.
- Settings, Reading log, Cloud and Permissions are normal windows and do activate the app; they are opened only by an
  explicit menu or button choice.

### B5. Hiding and captures

1. **Third-party screen sharing and screenshots.** macOS has no public way to keep a window out of other apps'
   ScreenCaptureKit captures (Prior art). The panel still sets `sharingType = .none` (older capture paths honour it).
   The idle bar is 56 × 8 pt. The user hides it with ⌃⌥B or "Hide for 1 hour". Settings says so in one line: "Screen
   sharing apps can see the bar. Hide it with ⌃⌥B."
2. **Speakeasy's own captures never include the bar.** Read area already excludes the app. The selection path
   (`Reader.start`, today `screencapture -R`) moves to the same ScreenCaptureKit capture with the app excluded; if that
   call fails, the bar is ordered out for the capture and restored, and the run logs `capture_bar_hidden: true`.
   `no-selection-window` captures one window and is unaffected.

### B6. Control channel (pause, next, flag)

No sockets (architecture rule). The app signals the backend's pid from `speak.pid`:

| Signal | Backend behaviour |
|---|---|
| `SIGUSR2` | Toggle pause: `SIGSTOP` / `SIGCONT` the current `afplay` process group; synthesis keeps running ahead; `state.json` `paused` flips; the tracker keeps the current cue (no hide) |
| `SIGUSR1` | Next sentence: terminate the current `afplay`; the speaker plays the next queued sentence; focus advances as for a natural sentence end; `skips` + 1 |
| `SIGTERM` | Stop (today) |

"Wrong spot" needs no backend call: the app appends `user_flag` to `app.jsonl` with the live `run_id`, `seq` and
`cue_kind` from `state.json`, and shows "Flagged" in the bar for 1.5 s.

### B7. Events (`logs/app.jsonl`, new; rotated like the others; no text)

Every record has `ts` and `event`; `run_id` when a read is live.

| event | Fields |
|---|---|
| `bar_state` | `from`, `to` (B2 names) |
| `bar_move` | `reason`: `drag` \| `collision` \| `display` \| `fullscreen`; `pos`: `centre` \| `left` \| `right` |
| `bar_hide` | `reason`: `hotkey` \| `hour` \| `fullscreen` \| `setting` |
| `area` | `via`: `bar` \| `hotkey` \| `menu`; `result`: `read` \| `cancel-esc` \| `cancel-click` \| `cancel-hotkey` \| `tiny` |
| `control` | `action`: `pause` \| `resume` \| `next` \| `stop` \| `flag`; `via`: `bar` \| `hotkey` \| `menu` |
| `user_flag` | `seq`, `cue_kind` |
| `problem` | `id` (A2.1), `live`: bool |
| `font_fallback` | `face`: `serif` \| `sans` \| `mono` |
| `focus_kept` | `ok`: bool (debug builds only: the frontmost pid before and after a bar click) |

### B8. Failure cases

| Case | Behaviour |
|---|---|
| `state.json` missing or unreadable | `preparing` with the elapsed counter until the backend exits; then `idle` |
| Backend exits with no audio | `error` with the run's problem if any, else "Nothing was read. The selection was empty." and no action |
| Signal to a dead pid | Ignored; the bar goes `idle` |
| Resume after the player died | Acts as Next sentence |
| `cue.json` stale (over 1 s) or absent | Collision rules off for that moment; the bar stays where it is |
| Display removed while reading | The bar moves to the display holding the pointer |
| ⌃⌥A pressed twice quickly | Second press cancels (no second overlay) |
| Screen Recording missing at arm | `error`: "Screen Recording is off. Read area needs it." with "Open Screen Recording settings" |

### B9. Acceptance (bar)

Harness: `tests/audit/ui/` (verifier). The Terminal running it needs Screen Recording. Fixtures: a TextEdit document
with a known selection, and a two-page PDF open in Preview.

1. **Idle pixels.** With the bar at idle, `screencapture -x -R` of the bottom-centre region finds the navy surface
   (`#1C3150` ± 3 per channel) on at least 80 % of the expected 56 × 8 pt box, and the 3 pt tick in `ice`.
2. **No shadow or glow.** The same region captured with the bar hidden (⌃⌥B) and shown: pixels 2 to 10 pt outside the
   bar's frame differ by at most 2 per channel.
3. **Focus kept.** Click the idle bar, a hover button, and each reading button with synthetic mouse events. Before and
   after each: `lsappinfo front` is the document app, and its focused element's AX selected-text range is unchanged.
4. **Area from the bar.** Click the idle bar, drag over the PDF fixture: a new run with `capture_method: area`,
   `app.jsonl` `area {via: bar, result: read}`, focus kept (check 3) through arm, drag and read.
5. **Esc released.** After armed is cancelled, Esc pressed in TextEdit reaches TextEdit (a fixture that closes a sheet
   on Esc closes it).
6. **Collision.** Scroll the PDF fixture so the read line sits at the bottom centre; within 300 ms of the cue,
   `bar_move {reason: collision}` is logged and a screenshot shows the bar's frame disjoint from the cue rect.
7. **Own capture.** With the bar forced over the selection (`SPEAKEASY_BAR_TEST_POS=selection`, test builds only),
   read; `~/.speakeasy/last_capture.png` holds no bar pixels (check 1's colour test finds under 1 %).
8. **Pause, next, stop.** ⌃⌥P, wait 2 s, ⌃⌥P, ⌃⌥N, then ⌃⌥S: the run logs `paused_ms` ≥ 1500, `skips` 1, `end: stopped`;
   `app.jsonl` has the four `control` records.
9. **Full-screen.** Preview full-screen: the idle bar is absent from a screenshot; ⌃⌥A shows `armed` 8 pt from the
   bottom edge.
10. **Hidden.** ⌃⌥B: the bar is absent; the menu's "Show bar" is unchecked; a read shows the bar for that read only.
11. **Multi-display** (manual unless the runner has two displays): at idle the bar follows the pointer's display;
    during a read it stays on the source window's display.
12. **Created once.** Twenty consecutive reads create the panel once (`bar_state` from `idle` to `preparing` within
    50 ms of capture end on every read).

## C. Design system on every surface

### C1. Tokens in one place

- Swift: `Tokens.swift` only. Tracker: one constants block. Backend: the cue style in `focus.py` reads the same values
  (today it writes `color`, `opacity`, `border` into show records).
- `#3B82F6`, every `NSColor.system*` / `Color.green|blue|orange|purple` and the capsule shape are removed from UI code.

### C2. Fonts

Bundled and registered at launch for the process (design section 2). Fallback chain on failure, logged once as
`font_fallback`. `app/NOTICE` carries both OFL texts and the reserved names.

### C3. Copy

All user-visible strings follow design section 5. The before/after table there is the minimum change set.

### C4. Snapshot mode (for screenshot checks without permissions)

`Speakeasy --snapshot <surface>:<state> --out <png> [--appearance light|dark] [--fixture <stats.json>]` renders one
surface offscreen at 2x (`ImageRenderer`) and writes `<png>` plus `<png>.json` with the frames of the dominant element
and its margin tick. Surfaces and states: every wireframe in design section 6 (bar: all B2 states; reading log: data,
empty, error; permissions: none granted, one granted, all granted; settings; cloud: off, on with key, on without key).
`Speakeasy --self-check fonts` prints the three registered PostScript names and exits 0, or exits 1 naming the missing
face.

### C5. Acceptance (design)

1. **Token lint** over `app/Sources` (excluding `Tokens.swift`) and the tracker's drawing code: zero matches for a hex
   literal `#[0-9A-Fa-f]{6}`, `NSColor.system`, `Color.(green|blue|orange|purple|pink|red)`, `hasShadow = true`,
   `.shadow(`, `NSVisualEffectView`, `Gradient`, `.blur(`, `Capsule(`, a corner radius above 4.
2. **Copy lint** over UI strings: zero `—`, "it's not", "seamless", "effortless", "magic", "supercharge", "unlock",
   "smart", " AI", "!", button titles "OK", "Grant…", "Continue", "Learn more".
3. **Snapshot pixels** (each C4 snapshot, light and dark):
   - no exact `#FFFFFF` or `#000000` pixels in window surfaces;
   - pixels outside the panel or window frame have alpha 0 (no shadow);
   - the bar's corner pixel (0, 0) is transparent and pixel (4, 4) is the navy surface (radius ≤ 4);
   - the margin tick: a 3 pt column at the sidecar's tick frame is `ice`, `glacier` or `brick` (± 3) for at least 90 %
     of its height;
   - only token colours (± 3, plus antialiasing between them) appear in non-text areas.
4. **Fonts:** `--self-check fonts` exits 0 on a clean install; no `font_fallback` in `app.jsonl` after launch.
5. **Cues on the document:** the tracker's offscreen draw test renders band, back-reference, figure box, part oval and
   table row over a white and a dark page; sampled stroke and fill colours match C1 tokens (± 3, alpha ± 0.03); the
   band has the 3 pt margin tick; black text under the band keeps contrast ≥ 4.5:1. Existing tracker tests
   (`FirstDrawTests`) still pass.
6. **Numbers:** in the Reading log snapshot every number is in Plex Mono and right-aligned within its column; baselines
   of a label and its value differ by 0 px.
7. **Cover-the-logo** (manual, the user, not gated): the verifier produces each C4 snapshot with the app name and
   status glyph blacked out; the user marks each screen recognisable or not. Results go into the verification note.

## 2. Build order (each step ships on its own)

| Step | What ships | Touches | Done when |
|---|---|---|---|
| 1 | Tokens, fonts and copy on today's surfaces: pill restyled (navy, radius 4, no shadow, no pulse, elapsed counter), stats window type, onboarding and cloud copy | `app/` only | C5 checks 1, 2, 4 pass; behaviour unchanged |
| 2 | New log fields: `end`, sentence counts, `fig_ids`, `tts_fallback`; tracker `cue_result`, `correction.seq`, `cue.json`; live-state fields | backend, tracker | A7 checks 5, 6, 10; old stats output unchanged |
| 3 | Stats schema 2, the document library (`library.json`, names on by default), the Reading log window, the menu line "Today: 9 reads, 14 min", problems with live checks | `stats.py`, `observe.py`, `document.py`, `app/` | A7 checks 1 to 4, 7, 8 |
| 4 | The bar, first version: all B2 states except `paused`; Stop only while reading; placement and collision; full-screen; hide; no activation in Read area; selection capture through ScreenCaptureKit; `app.jsonl` | `app/` | B9 checks 1 to 7, 9 to 12 |
| 5 | Pause, Next, Wrong spot: signals, temporary hotkeys, `user_flag`; `highlight.flagged` in the Reading log | backend `tts.py` / `speakeasy.py`, `app/` | B9 check 8; A7 check 1 with flags |
| 6 | On-screen cues in the design: colours, margin tick, ember oval, dark-page rule, area overlay readout | `tracker.swift`, `focus.py`, `AreaSelector.swift` | C5 check 5; A7 check 10 |
| 7 | Settings window (hotkeys, bar position, full-screen rule, highlight, "Show document names"), Permissions and Cloud layouts, status glyph | `app/` | C5 checks 3, 6, 7 |
| 8 (required) | `speakeasy logs --scrub` (A5.4), then one run on the user's real logs | `observe.py`, `speakeasy.py` (CLI entry), `tests/audit/SCHEMA.md` (`command: scrub`) | A7 check 9 on fixtures, then on the real logs |

Step 8 depends on no other step and may ship first; the phase is not done until it has run once on the user's logs.
Phase 6 starts after all eight steps have merged (section 1).

## 3. Out of scope

- Opening a document at its resume page (`library.json` already stores the path, so it can be added later).
- A speech-rate control (listening speed is shown; the control is later).
- Word-by-word highlighting; changes to the cue logic itself (shapes and colours only).
- Detecting third-party screen sharing automatically.
- Sparkle auto-update, sync, multi-user, a Dock icon.

## 4. Decisions by the user (2026-10-26)

1. **Palette:** the proposed one (`docs/design.md` section 1: navy `#14233B` / `#1C3150`, ice `#A8D8EC`, glacier
   `#1F6E99`, one ember accent `#E0892B`). Not TYTIS colours.
2. **Document names and paths:** stored locally in `library.json`, outside the logs, on by default (A5.3). Logs stay
   text-free.
3. **Menu line:** "Today: 9 reads, 14 min" (reads and minutes listened).
4. **Old text-bearing records:** scrubbed by a required build step (A5.4, step 8): in place, no plain-text backup,
   idempotent, one count-only event per run.
5. **Order:** Phase 7 before Phase 6, starting from the commit that lands Phase 5 on `main` (section 1).
