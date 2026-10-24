# speakeasy audit log schema (required for auditability)

Timing fields may be top-level or nested under `timings`.

| field | types | required when | meaning |
|---|---|---|---|
| ts | str/int/float | always | run timestamp |
| command | str | always | rewrite|speak|selection|stop|last|stats |
| input_chars | int | always | chars in |
| output_chars | int | always | chars out |
| backend | str | always | llm|fallback |
| model | str/NoneType | always | text model |
| error | str/NoneType | always | error or null |
| total_ms | int/float | always | wall time |
| llm_ms | int/float/NoneType | always | LLM stage |
| copy_ms | int/float/NoneType | _sel | clipboard copy stage |
| capture_ms | int/float/NoneType | _sel | screenshot stage |
| capture_method | str | _sel | bounds|highlight|select|mouse|none |
| capture_rect | list/str/dict/NoneType | _sel | x,y,w,h captured |
| image | bool | always | image supplied & valid |
| image_px | str/NoneType | always | original WxH |
| image_sent_px | str/NoneType | _img | sent WxH |
| vision_model | str/NoneType | _img | vision model |
| vision_ms | int/float/NoneType | _img | vision stage |
| tts_first_audio_ms | int/float/NoneType | _spoken | time to first audio |
| speak_ms | int/float/NoneType | _spoken | speech duration |
| dry_run | bool | _spk | dry-run flag (decides whether tts/speak timings are required) |
| capture_path | str/NoneType | _img | kept capture file path |
| peak_rss_mb | int/float | always | peak RSS of speakeasy process |
| ollama_models | list/NoneType | <lambda> | from /api/ps: loaded models + memory |

capture_method in {bounds, highlight, select, mouse, none}. Anomaly flags: slow >30s, fallback while LLM reachable, missing/mistyped fields, capture >50% of screen, full-screen highlight/bounds capture, any error.
