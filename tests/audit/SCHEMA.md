# speakeasy audit log schema (required for auditability)

Timing fields may be top-level or nested under `timings`.

| field | types | required when | meaning |
|---|---|---|---|
| ts | str/int/float | always | run timestamp |
| command | str | always | rewrite|speak|selection|stop|last|stats |
| input_chars | int | always | chars in |
| output_chars | int | always | chars out |
| backend | str | always | llm|fallback|verbatim|none |
| model | str/NoneType | always | text model |
| error | str/NoneType | always | error or null |
| total_ms | int/float | always | wall time |
| llm_ms | int/float/NoneType | always | LLM stage |
| copy_ms | int/float/NoneType | _sel | clipboard copy stage |
| capture_ms | int/float/NoneType | _sel | screenshot stage |
| capture_method | str | _sel | bounds|area|no-selection-window|no-selection-screen|none |
| capture_rect | list/str/dict/NoneType | _sel | x,y,w,h captured |
| image | bool | always | image supplied & valid |
| image_px | str/NoneType | always | original WxH |
| image_sent_px | str/NoneType | _img | sent WxH |
| vision_model | str/NoneType | _img | vision model |
| vision_ms | int/float/NoneType | _img | vision stage |
| tts_first_audio_ms | int/float/NoneType | _spoken | time to first audio |
| speak_ms | int/float/NoneType | _spoken | speech duration |
| dry_run | bool | _spk | dry-run flag (decides whether tts/speak timings are required) |
| capture_path | str/NoneType | _img | kept capture file path, home-relative (`~/...`) when under home |
| peak_rss_mb | int/float | always | peak RSS of speakeasy process |
| ollama_models | list/NoneType | <lambda> | from /api/ps: loaded models + memory |

capture_method in {bounds, area, no-selection-window, no-selection-screen, none}. backend in {llm, fallback, verbatim, none}.
Anomaly flags: slow >30s, fallback while LLM reachable, missing/mistyped fields, capture >50% of screen, full-screen bounds capture, any error.

Optional keys (present only when the condition holds):

| field | types | present when | meaning |
|---|---|---|---|
| ground_mode_note | str | SPEAKEASY_GROUND is not auto/local/openrouter/off/0/no/false | `SPEAKEASY_GROUND=<value> unknown; using auto` (value lowercased, max 24 chars) |
| ground_provider | str | a pick was answered | local|openrouter: the provider of the last answered pick (phase5-parts.md A2.9). `ground_box_convention` is retired with the `bbox_2d` path |
| ground_errors | dict | a pick request failed (either provider) | {count: int, classes: {exception class name: count}}; never model or document text |
| area_page_rect / area_region_src | list / str | area runs on a PDF (`--capture-method area`) | A2.1: the area's page rect (page space, 0.1 pt) used as the run's selection region, and where it came from: `area-fit` (area_pdf ok), `capture-fit` (the capture rect through the capture fit), `none` (no page region: screen-only cues; no `area_page_rect`) |
| area_pdf / area_pdf_error | str / str | area runs with OCR text | `area_pdf`: `ok`, `doc-timeout`, `doc-unresolved`, `doc-failed`, `no-page`, `markdown`, `no-fit`, `empty`, `error`; `area_pdf_error`: the exception class when `error`. No text (A2.8) |
| area_dropped | int | an area cue the area clamp shrank below 8 pt | cues dropped instead of drawn (the previous cue stays; never the whole area) |
| figure_mode_src | str/NoneType | the figure-mode decision was made | `caption`, `vision`, `area-overlap`, `area-labels` or null (A2.2), next to `doc_figure_mode` |
| stand_in_blocked / no_target_chunks | int | A2.3 applied (figure mode, vision, explain / summary, area runs) | chunks where the old rule would have drawn a band over the whole selection or area / chunks that drew nothing new (map `method` `hold` or `none`) |
| figures_degenerate | int | a PDF page was matched | figures of the page whose box is under 10 pt in height or width (A2.4); never drawn, grounded or picked |
| input / output | str | SPEAKEASY_LOG_TEXT=1 | the text read and the text spoken |
| files / records_changed / fields / other_logs_with_text / ms | int / dict / dict / int / int | `command: scrub` (one record per completed `speakeasy logs --scrub`, phase7-usable-app.md A5.4) | Counts only, never text or paths: `files` rewritten, `records_changed` `{"runs": n, "focus": n}`, `fields` `{"text", "src", "ref_src", "doc.label", "doc.anchors", "input", "output", "error", "llm_error", "notes": n}`, `other_logs_with_text` (tracker / app records still holding a forbidden key; never changed). A scrub record has only `ts`, `command` and these keys; `stats` ignores it |
| first_focus_ms | int/NoneType | always for speak / selection | ms from run start to the moment the first `show` record of the run was appended to the focus log, in dry-run too. `null` when the run wrote no `show` |
| doc_ready_ms | int/NoneType | always for speak / selection / rewrite | ms from run start to the moment the document job finished (success or not). `null` when no document job ran |
| sentence_map | dict | speak / selection runs whose focus had selection text (a sentence map was built) | Counts only, never text: `{"sentences": n, "mapped": n, "method": "page\|ax-lines\|band\|none", "ms": int}`. `mapped`: sentences with at least one page rect (method `page`) or screen line rect (`ax-lines`); `ms`: time spent building the map (and attaching page rects). The final state of the run (docs/contracts/phase3-sentence-ids.md F) |
| doc_parse | dict | a PDF page was matched (`page` non-null) | Summary of the matched page's parse, counts only: `{"columns": 1\|2, "figs_done": bool, "blocks": {"paragraph": n, "caption": n, "figure": n, "table": n}, "bbox_src": {"<D enum>": n}, "labels_src": {"pdf": n, "ocr": n, "none": n}, "panels": {"labels": n, "guessed": n, "gutter": n}, "parts": {"text": n, "callout": n, "callout_attached": n}, "split": n, "layout": {...}}`. `panels` counts the page's panels by source (drawn labels, caption even split, whitespace gutters); `parts` its figure parts (docs/contracts/phase5-parts.md 2.5). Without figure parsing (`figs_done: false`), only `columns`, `figs_done` and `blocks.paragraph` are present. `layout` (figs_done pages): the page's `pages[].layout` without `detector_version`: `{"src": "detector", "ms": int, "blocks_by_kind": {...}}` or `{"src": "heuristic", "reason": "disabled\|no-onnxruntime\|model-missing\|model-mismatch\|load-failed\|inference-failed"}` (docs/contracts/phase4-layout.md section 6) |
| layout_events | list | the document job produced the parse (`doc_cache` `miss` / `partial-miss`) or used the PDFKit path (`miss-pdfkit`) | The parse's events, copied as they are: `{"event": "layout_page", "page": n, "ms": int, "detector_version": str, "blocks_by_kind": {...}}` per detected page, `{"event": "layout_detector_unavailable", "reason": <enum>, "error": "<ExceptionClass>"\|null, "detector_version": str\|null, "page": n\|null}` on a fallback (`pdfkit-engine` for the PDFKit path). Numbers, enums and hashes only |
| ground_miss_reasons | dict | present when at least one pick did not end in a drawn oval. This includes picks still undecided when the runs record is written | `{reason: count}`. Each pick that drew no oval is counted exactly once: `no-fit` (the box was held for the capture fit and the fit never landed); `superseded` (an accepted answer arrived after the next chunk started); `timeout` (no answer by its deadline: 15 s from submit, 25 s for a pick sent to OpenRouter); `error`; `low-confidence` (p_top below the threshold); `off-menu` (letters below half of the top-20 mass, or an abstention); `no-logprobs` (the local reply had no top_logprobs); `unavailable` (Ollama or OpenRouter down, HTTP 404 / 5xx, no OpenRouter key, or the run's breaker); `out-of-scope` (an accepted answer centred outside the scope or the label region, A2.6). The retired box path's `no-box` and `too-large` are never written. Invariants: `error + low-confidence + off-menu + no-logprobs + unavailable + out-of-scope == focus_methods["ground-miss"]` (0 when absent), and the sum of all reasons + `focus_methods["ground-pick"]` == picks submitted |
| part_picks | dict | a pick was considered for a figure chunk with no deterministic oval (docs/contracts/phase5-parts.md 6.2, A2.9) | `{"requests": n, "called": n, "accepted": n, "applied": n, "outcomes": {outcome: n}, "skipped": {"too-few": n, "whole-figure": n, "disabled": n, "doc-not-ready": n, "degenerate-figure": n}, "ms": {"p50": int\|null, "max": int\|null}, "threshold": float, "breaker": {"local": null\|"unavailable"\|"no-logprobs", "openrouter": null\|"unavailable"}, "fallback": {"unavailable": n, "no-logprobs": n}, "by_provider": {"local": n, "openrouter": n}}`. `requests`: picks queued (each has one focus-log `part_pick` record); `called`: picks whose request was sent; `applied`: accepted picks drawn while their chunk was current; `skipped.whole-figure`: chunks naming no specific object (A2.5); `doc-not-ready`: chunks located before the page's parse and part hierarchy were final (A3.3); `fallback`: local breaker trips that moved picks to OpenRouter; `by_provider`: the provider that produced each pick's outcome. Outcomes: `accepted`, `low-confidence`, `off-menu`, `no-logprobs`, `unavailable`, `error`, `timeout`, `out-of-scope`. Counts only |
| end | str | speak / selection | `finished` \| `stopped` (SIGTERM, i.e. Stop or a new read, arrived before the last sentence finished playing) \| `error` (a TTS process failed, or nothing was spoken and a stage failed) (phase7-usable-app.md A3) |
| sent_n / sent_spoken | int\|null / int | speak / selection | sentences queued for speech at the end of the rewrite / sentences that started playing (a finished read has `sent_spoken == sent_n`) |
| paused_ms / skips | int / int | speak / selection | total time paused by the app (`SIGUSR2`; `audio_s` excludes it) / "next sentence" requests honoured (`SIGUSR1`) |
| fig_ids | list[str] | speak / selection | distinct `p<page>:fig<n>` / `p<page>:tab<n>` (n: 1-based index among the page parse's figures or tables in reading order) that got a figure box, part oval or table row; `[]` otherwise. Show records carry the same `fig_id` |
| tts_fallback | null\|str | speak / selection | Kokoro requested but `say` used: `unavailable` (no server / no answer) or `error` (it answered badly); null otherwise |
| llm_error_class | str | the LLM or vision stage raised (backend fell back) | the exception class name only (e.g. `URLError`, `TimeoutError`, `ValueError`); the Reading log's `model-down` problem reads this, never `llm_error` |
| capture_bar_hidden | bool | the app's ScreenCaptureKit selection capture failed and it hid the bar for a `screencapture` fallback | always `true` when present (B5.2) |
| library_error | str | writing `library.json` failed | the exception class only; the read goes on without a name |

Stats JSON (`speakeasy stats --json [--window 1|7|30]`): schema 2 exactly as in phase7-usable-app.md A4, plus `since` (`{"items"\|"completion"\|"highlight": "YYYY-MM-DD"}`, the first date a newer field appears when part of the window predates it, A6) and `skipped_lines` (malformed lines in the logs read). Problem ids: `kokoro-down`, `model-down` (`backend: fallback` with an unreachable `llm_error_class`), `cloud-fallback`, `layout-missing`, `highlight-failed` (tracker runs with `draw-failed` or exit `no_screen_recording`), `slow-start` (first audio over 10 s); `permission` is a live check in the app only. `open` = seen in the last 24 h; the app adds its live checks.

Tracker (`logs/tracker.jsonl`): `correction` gains `seq`; new `cue_result` `{ts, tracker_pid, run_id, seq, cue_kind, first_draw_ms, corrections, max_shift_px, landed, hidden}` when a show with a drawn cue ends (`landed`: first draw within 300 ms and no later correction above max(12 pt, half a line); `hidden`: it ended hidden); `exit` gains `cues: {shows, landed, hidden}` (equal to its `cue_result` records); `page_tone {seq, dark}` when the page under the cue turns dark or light (luminance 0.25). The no-Screen-Recording `exit` carries `run_id` and `tracker_pid`.

App (`logs/app.jsonl`, phase7-usable-app.md B7): `{ts, event, run_id?}` plus `bar_state {from, to}`, `bar_move {reason, pos}` (reason `drag`, `collision`, `display`, `fullscreen`, or `occupied` with `occupants` (a count) and `stacked` when another app's floating window holds the bar's spot), `bar_hide {reason}`, `area {via, result}`, `control {action, via}`, `user_flag {seq, cue_kind}`, `problem {id, live}`, `font_fallback {face}`, `focus_kept {ok}` (debug builds). No titles, paths, text or app names.

Live files (not logs): `state.json` (written by every non-dry read, app or CLI) `{state, run_id, t, seq, sent_i, sent_n, cue_kind, paused, label, started_ms, cloud}` (cleared to `{state: idle, run_id, t}` at idle); `cue.json` `{run_id, seq, rect, ts}` (global top-left points, written by the tracker while a cue is drawn, deleted on hide and exit); `library.json` `{doc_path_hash: {name, path, first_ts, last_ts}}` (mode 0600, never rotated, never copied into a log, left alone by `--scrub`).

Focus log (`logs/focus.jsonl`) `part_pick` record (one per pick, written once when its decision is final; never in `focus.json`): `{"state": "part_pick", "sent", "sentence_id", "fig", "panel", "scope": "panel"|"figure", "candidates", "cand_rects": [[x0, y0, x1, y1], ...] (page space, 0.1 pt), "chosen_index" (accepted / low-confidence: the argmax, else null), "p_top", "p_margin", "letter_mass" (2 decimals, null without logprobs), "palette": "red"|"magenta"|"blue"|null, "called", "outcome", "applied", "drop": "superseded"|"no-fit"|null, "ms" (render + request), "queue_ms", "model", "provider": "local"|"openrouter" (whichever produced the outcome), "fallback_from": null|"unavailable"|"no-logprobs" (the local outcome that sent this pick to OpenRouter), "confidence": "logprob"|"letter-only"|null, "ts", "pid"}`. `outcome` also takes `out-of-scope`. No phrase, label text, prompt, image path or model output, whatever SPEAKEASY_LOG_TEXT is. Show records gain `doc.target_src` values `callout` (a callout part found deterministically) and `pick` (an accepted pick); `focus_methods` gains `ground-pick` (accepted picks drawn); `ground-vlm` is retired with the box path. Tracker `draw` records carry `first_draw_src` (`page-fit`, `capture+offset` or `held`) on the first draw of each show (A2.7).

No text from the model, the document or a table appears in `error`, `llm_error`, `stream_note` or `notes` unless SPEAKEASY_LOG_TEXT=1.
