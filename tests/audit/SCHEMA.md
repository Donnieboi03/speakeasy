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

No text from the model, the document or a table appears in `error`, `llm_error`, `stream_note` or `notes` unless SPEAKEASY_LOG_TEXT=1.
