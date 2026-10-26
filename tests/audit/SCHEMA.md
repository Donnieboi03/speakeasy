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
| ground_provider | str | a grounding request was sent | openrouter|local (last request of the run) |
| ground_box_convention | str | a grounding answer parsed into a box | yx (Gemini box_2d)|xy (Qwen bbox_2d) |
| ground_errors | dict | a grounding request failed | {count: int, classes: {exception class name: count}}; never model or document text |
| input / output | str | SPEAKEASY_LOG_TEXT=1 | the text read and the text spoken |
| first_focus_ms | int/NoneType | always for speak / selection | ms from run start to the moment the first `show` record of the run was appended to the focus log, in dry-run too. `null` when the run wrote no `show` |
| doc_ready_ms | int/NoneType | always for speak / selection / rewrite | ms from run start to the moment the document job finished (success or not). `null` when no document job ran |
| doc_parse | dict | a PDF page was matched (`page` non-null) | Summary of the matched page's parse, counts only: `{"columns": 1\|2, "figs_done": bool, "blocks": {"paragraph": n, "caption": n, "figure": n, "table": n}, "bbox_src": {"<D enum>": n}, "labels_src": {"pdf": n, "ocr": n, "none": n}, "panels": {"labels": n, "guessed": n}, "split": n, "layout": {...}}`. Without figure parsing (`figs_done: false`), only `columns`, `figs_done` and `blocks.paragraph` are present. `layout` (figs_done pages): the page's `pages[].layout` without `detector_version`: `{"src": "detector", "ms": int, "blocks_by_kind": {...}}` or `{"src": "heuristic", "reason": "disabled\|no-onnxruntime\|model-missing\|model-mismatch\|load-failed\|inference-failed"}` (docs/contracts/phase4-layout.md section 6) |
| layout_events | list | the document job produced the parse (`doc_cache` `miss` / `partial-miss`) or used the PDFKit path (`miss-pdfkit`) | The parse's events, copied as they are: `{"event": "layout_page", "page": n, "ms": int, "detector_version": str, "blocks_by_kind": {...}}` per detected page, `{"event": "layout_detector_unavailable", "reason": <enum>, "error": "<ExceptionClass>"\|null, "detector_version": str\|null, "page": n\|null}` on a fallback (`pdfkit-engine` for the PDFKit path). Numbers, enums and hashes only |
| ground_miss_reasons | dict | present when at least one grounding request did not end in a drawn oval. This includes requests still unanswered when the runs record is written | `{reason: count}`. Each submitted request that drew no oval is counted exactly once, under the first matching reason: `no-fit` (the box was held for the capture fit, the fit never landed — the `fit-miss` path — and the grounding result was dropped with it); `superseded` (an answer arrived after the next chunk started and was dropped); `timeout` (no answer inside its wait window, or still no answer when the runs record is written); `error` (request raised; the class is already counted in `ground_errors`); `no-box` (answer had no parseable box); `too-large` (every box rejected by the size or generic-label filter). Invariants: `error + no-box + too-large == focus_methods["ground-miss"]` (0 when absent), and the sum of all reasons + `focus_methods["ground-vlm"]` == grounding requests submitted |

No text from the model, the document or a table appears in `error`, `llm_error`, `stream_note` or `notes` unless SPEAKEASY_LOG_TEXT=1.
