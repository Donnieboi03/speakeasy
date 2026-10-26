# Phase 2 contract: log and parse fields for the benchmark

Scope: the app-side fields that the Phase 2 benchmark (`docs/contracts/phase2-benchmark.md`) and the next phases' gates
read. It covers what is missing today and nothing else. This file describes **observable output**; it does not
prescribe code. Terms are as in `phase1-readability.md`. *Run start* = the backend's `t0`, the same origin as
`first_sentence_ms` and `tts_first_audio_ms`. *Chunk* = one text piece handed to TTS (`Speaker.put`).

The Consumer column says who reads each field:
- `bench`: the scorer or harness;
- `real-log`: `tests/audit/box_accuracy.py` and later phase gates on real runs;
- `diag`: per-category breakdowns in `bench.json` and the next phases' gates.

## Global rules

- **Logging only.** Parse and focus decisions are unchanged in Phase 2. The new fields report what the code already
  decides (column count, splits, box sources); they never change it. The one behaviour change is the label redaction in
  F.
- **Additive only.** Every existing key in the runs log, focus log, live focus file, tracker log and parse cache keeps its
  name, type and meaning. Phase 1 invariants still hold. The one exception is the default value of labels (F).
- **No text.** New fields hold numbers, enums, booleans and figure/table references in the label form of F. They never
  hold document, selection, model or spoken text, file paths or image data, whatever `SPEAKEASY_LOG_TEXT` is.
- **Absent vs null.** "present when …" means the key is absent otherwise. "always" means the key is present, and
  `null` when unknown.
- Logging never delays speech or the focus box, and never raises (unchanged rule).
- Rounding: scores to 2 decimals, page coordinates to 0.1 pt, milliseconds as integers.
- `tests/audit/SCHEMA.md` gains every new runs-log key (C) in its optional-keys table, with the same wording.

## A. Focus log: `map` records

One record per chunk, written as today.

| Field | Type | When | Meaning | Consumer |
|---|---|---|---|---|
| `sent` | int | always | 0-based index of the chunk in the run, in `Speaker.put` order (the order the focus mapper locates chunks). The same chunk has the same `sent` in every record that refers to it | bench |
| `score` | float \| null | always | Line-match score of the chosen span. Method `overlap`: the best line's raw score. Method `progress`: the best forward line's raw score (below the 1.0 threshold). `null` for `tag` methods and image-only progress (no source lines) | diag |
| `runner_up` | float \| null | always | Highest raw score among forward-eligible lines (index ≥ the current line) outside the chosen span. `null` when there is no such line or `score` is `null` | diag |

Existing `method` (final method after document mapping, e.g. `doc-line`, `overlap`, `progress`), `decision` and
`lines` stay as they are. Together with `sent` they give the match method per chunk; no new method field is added.

## B. Focus log `show` records, hold `clear` records and the live focus file

| Field | Type | When | Meaning | Consumer |
|---|---|---|---|---|
| `sent` | int | every `show` record; every `clear` record with `hold: true` | The chunk this box belongs to (same value as its `map` record). Late updates (`late_fit`, grounding updates) keep the chunk's `sent`. Plain `clear` records (stop, end of speech) carry no `sent` | bench |
| `doc.target_src` | string | present when `doc.target` is present | How the page-space target was derived: `lines` (text band from page lines); `figure:<bbox_src>` / `table:<bbox_src>` (the figure or table box, `bbox_src` from D); `panel-labels` (panel split at drawn (a)/(b) labels); `panel-guessed` (panel guessed from the caption); `table-rows` (row band); `label-object` (part oval on a graphic object touching the label); `label-pad` (part oval on the padded label box); `vlm` (vision-model grounding) | diag |
| `doc.split_from` | string | present when the target is a figure, table or table-row box whose parse entry has `split_from` | The label of the figure it was split from (e.g. `Figure 5`) | diag |

These keys also appear in the live focus file. The tracker ignores them.

## C. Runs log (`runs.jsonl`)

| Field | Type | When | Meaning | Consumer |
|---|---|---|---|---|
| `first_focus_ms` | int \| null | always for `speak` / `selection` | ms from run start to the moment the first `show` record of the run was appended to the focus log, in dry-run too. `null` when the run wrote no `show` | bench (speed) |
| `doc_ready_ms` | int \| null | always for `speak` / `selection` / `rewrite` | ms from run start to the moment the document job finished (success or not). `null` when no document job ran | bench (race check) |
| `doc_parse` | object | present when a PDF page was matched (`page` non-null) | Summary of the matched page's parse, counts only: `{"columns": 1\|2, "figs_done": bool, "blocks": {"paragraph": n, "caption": n, "figure": n, "table": n}, "bbox_src": {"<D enum>": n}, "labels_src": {"pdf": n, "ocr": n, "none": n}, "panels": {"labels": n, "guessed": n}, "split": n}`. Without figure parsing (`figs_done: false`), only `columns`, `figs_done` and `blocks.paragraph` are present | diag |
| `ground_miss_reasons` | object | present when at least one grounding request did not end in a drawn oval. This includes requests still unanswered when the runs record is written | `{reason: count}`. Each submitted request that drew no oval is counted exactly once, under the first matching reason: `no-fit` (the box was held for the capture fit, the fit never landed — the `fit-miss` path — and the grounding result was dropped with it); `superseded` (an answer arrived after the next chunk started and was dropped); `timeout` (no answer inside its wait window, or still no answer when the runs record is written); `error` (request raised; the class is already counted in `ground_errors`); `no-box` (answer had no parseable box); `too-large` (every box rejected by the size or generic-label filter). Invariants: `error + no-box + too-large == focus_methods["ground-miss"]` (0 when absent), and the sum of all reasons + `focus_methods["ground-vlm"]` == grounding requests submitted | diag |

Existing keys the benchmark also reads, unchanged: `first_sentence_ms`, `llm_request_at_ms`, `llm_first_token_ms`
(measured from the request, not run start), `tts_first_audio_ms`, `page`, `doc_map_method`, `llm_provider_configured`,
`focus_methods`, `cue_kinds`.

## D. Parse output (`docmap.py parse-pdf` output and the `cache/doc/*.json` cache)

- `PARSE_VERSION` goes up by one, so old caches are rebuilt on the next read. `docmap.py dump` output gains the same
  keys.

| Field | Type | When | Meaning | Consumer |
|---|---|---|---|---|
| `pages[].columns` | int (1 \| 2) | every page with `figs_done: true` | The two-column decision the figure/table logic already makes, reported as is (2 when that rule says two-column, else 1). Not a ground-truth column count: the rule may say 1 on a visually two-column page (e.g. the Fig. 5 fixture) | bench, diag |
| `pages[].blocks` | list | every page with `figs_done: true` | The page's blocks in reading order (column by column, top to bottom). See below | bench |
| `figures[].bbox_src` | string | every PDF figure/table entry | How the box was found. Figure: `graphics` (vector/image chain above the caption), `whitespace` (text-free gap above the caption), `caption-only` (no body found). Table: `graphics`, `text-rows`, `graphics+text-rows`, `caption-only`. `detector` is reserved for a later layout model | diag |

Block entry: `{"kind": "paragraph"|"caption"|"figure"|"table", "bbox": [x0, y0, x1, y1], "lines": [int, ...],
"label": "Figure N"|"Table N", "src": "<bbox_src>"}`.
- `lines` (indexes into `pages[].lines`) is present for paragraph and caption blocks.
- `label` is present for caption, figure and table blocks.
- `src` is present for figure and table blocks.

Invariants, checked by a unit test on the Phase 2 fixtures:
- **One block per figure:** each `figures[]` entry on the page has exactly one figure/table block with the same
  `label` and an identical `bbox`, and exactly one caption block whose `bbox` equals its `caption_bbox`.
- **Paragraph boxes:** a paragraph's `bbox` is the union of its lines' rects.
- **No shared lines:** no line index appears in two blocks.
- **Paragraphs stay outside figures and tables:** a line inside a figure or table `bbox` (2 pt slack) is not in a
  paragraph block.
- **Captions are not paragraphs:** caption lines are only in their caption block.
- **Post-threshold only:** blocks are the set the app acts on. Anything the parse rejects is not listed.

The existing `figures[]` keys (`bbox`, `caption_bbox`, `panels[].guessed`, `labels_src`, `split_from`, `objects`, …)
are unchanged. The app's focus behaviour on the existing suites does not change: the same targets for the same inputs.

## E. Tracker log `draw` records (real logs; not used by the headless benchmark)

| Field | Type | When | Meaning | Consumer |
|---|---|---|---|---|
| `page_rect` | [x0, y0, x1, y1] \| null | every `draw` record | The rect actually drawn (after the window clamp, the same rect as `text_rect`) mapped back to page points through the tracker's current page→screen fit. `null` when the tracker holds no fit for the current box | real-log |
| `page_fit` | string \| null | every `draw` record | The source of that fit. `anchors`: the tracker's own anchor fit (`docMap`), carried forward by its scroll deltas. `event-transform`: the event's `doc_transform` with the tracker's accumulated scroll offset removed. `null` together with `page_rect` | real-log |

`doc_target` and `doc_transform` in draw records stay as they are. The 10 XCTests still pass.

## F. Labels carry no document text by default

This fixes a pre-existing leak. Part ovals today log `doc.label` as `Figure 3a: <part text>`, and that text is copied
into the tracker's draw meta.

- **Scope:** with `SPEAKEASY_LOG_TEXT` unset (or not exactly `1`), this applies to every label that is logged or live:
  - focus log `show` records `doc.label`;
  - the live focus file `doc.label`;
  - tracker log `draw` records `label`;
  - any runs-log field that holds a figure/table label.
- **Allowed form:** a figure/table reference with an optional panel letter, plus fixed suffixes only. It must match
  `^(Figure|Table) \d+[a-h]?( row \d+)?( \(grounded\))?$`. Examples: `Figure 3a`, `Table 2`, `Table 2 row 4`,
  `Figure 3a (grounded)`. A label that can't be put in this form (e.g. a Markdown image with no caption number) is
  `null`.
- **Unchanged:** with `SPEAKEASY_LOG_TEXT=1`, labels are as today (including `: <part text>`). `doc.target`,
  `doc.kind`, shapes and on-screen behaviour are unchanged in both modes. The label is not used to draw.

## Considered and not added

- **Match method or score on `show` records:** these join through `sent` with the `map` record.
- **Per-show timestamps relative to run start:** `ts` (epoch) plus `first_focus_ms` is enough.
- **A separate per-page parse event file:** `doc_parse` in the runs record covers the visible page.
- **Spoken-text fields for attribution:** the benchmark turns text logging on inside its own temp home instead.
- **`match_score` on `clear` records:** not needed.

## Acceptance

- Every baseline suite keeps its pass/fail counts, except checks that assert a part's text inside a default-mode label
  (F). The verifier rewrites those.
- **Key-set pins (verifier, not implementer):** the exact-key-set checks in `tests/audit/readability` and the Phase 1
  holdout suite are updated to allow exactly the new Phase 2 keys and nothing else:
  - focus `map`: `sent`, `score`, `runner_up`;
  - focus `show` and hold `clear`: `sent`; inside `doc`: `target_src`, `split_from`;
  - runs: `first_focus_ms`, `doc_ready_ms`, `doc_parse`, `ground_miss_reasons`;
  - parse output: `pages[].columns`, `pages[].blocks`, `figures[].bbox_src`;
  - tracker `draw`: `page_rect`, `page_fit`.
  
  Any other new key still fails them. Implementers edit nothing under `tests/` except `tests/audit/SCHEMA.md`.
- On the Fig. 5 fixture (`tests/audit/boxacc`):
  - every `map` and `show` record of a run has `sent`, and the values are `0..k-1` with no gaps across `map` records;
  - every show with `doc.target` has `doc.target_src`. A `table` target's starts with `table:`; a `table_row` target has
    `table-rows`;
  - `doc_parse.columns` equals the parse's `pages[].columns`, and both reflect the parse decision (today's rule gives 1
    on this fixture);
  - `doc.split_from` is present on a Table 1 target exactly when the parse entry for Table 1 has `split_from`. Today it
    has none, because Table 1's body ends above Fig. 5;
  - the runs record has `first_focus_ms`, `doc_ready_ms` and `doc_parse`.
  
  The verifier corrects the existing checks to these.
- `docmap.py parse-pdf` on every Phase 2 public fixture satisfies the D invariants and gives the same `figures[]` boxes
  as before Phase 2 (parse behaviour unchanged).
- **Grounding misses:**
  - with grounding forced to fail (mock vision server answering no box), `ground_miss_reasons["no-box"]` equals
    `focus_methods["ground-miss"]`;
  - with the mock delaying its answer past the end of speech, the request is counted as `timeout` or `superseded`, never
    missing;
  - with the capture fit forced to fail on a figure-part sentence, it is counted as `no-fit`.
- **Privacy:**
  - with `SPEAKEASY_LOG_TEXT` unset, every focus-log, live-file and tracker-draw label matches the F pattern, and
    `privacy_audit.py` finds no document text in labels or in the new fields;
  - with `SPEAKEASY_LOG_TEXT=1`, labels equal today's;
  - the verifier adds the F label check to `privacy_audit.py`.
