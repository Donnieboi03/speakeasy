# Phase 2 contract: the three-score benchmark

Scope: the benchmark harness itself (`tests/audit/bench/`). It covers the command, inputs, fixtures, scoring rules,
`bench.json`, the baseline gate, determinism and failure behaviour. App-side log fields the harness reads are specified
in `docs/contracts/phase2-logging.md`. This file prescribes **observable behaviour**, not code.

Terms: *home* = the per-run `$SPEAKEASY_HOME`, always a fresh temp directory and never `~/.speakeasy`. *Runs log*,
*focus log* and *tracker log* have the same meaning as in the Phase 1 contract. *Page space* = PDF points, origin
top-left, rects `[x0, y0, x1, y1]`. *Case* = one CLI read of one selection. *Chunk* = one piece of text the app hands to
TTS (`Speaker.put`). The streaming releaser can cut one script sentence into several chunks. *Set* = `public` or `hidden`.

Prior art. docling-eval and OmniDocBench (COCO mAP over layout blocks) score boxes only. They need their own prediction
formats and do not cover sentence→line or part-oval mapping. So the harness is **built fresh**: a small stdlib scorer.
It adopts COCO-style greedy one-to-one IoU matching, OmniDocBench's block vocabulary (paragraph / caption / figure /
table, with header, footer, page number and heading as ignore regions), reportlab for synthetic fixtures and pdfplumber
(MIT, test-only) for the line geometry of real-PDF ground truth.

## 1. Files

| Path | Role |
|---|---|
| `tests/audit/bench/run_bench.py` | The one command: runs a set (or both) and writes `bench.json` |
| `tests/audit/bench/bench_gate.py` | Compares a `bench.json` with the public and hidden baselines |
| `tests/audit/bench/score.py` | Pure scoring functions (stdlib only), imported by `run_bench.py` and the self-tests |
| `tests/audit/bench/test_score.py` | Scorer and gate self-tests on hand-made predictions (section 11) |
| `tests/audit/bench/validate_fixtures.py` | Fixture validator (section 4.5), also run by `run_bench.py` before any CLI run |
| `tests/audit/bench/gen/synth.py` | reportlab generator: `synth.py SPEC.json OUTDIR` → `doc.pdf`, `gt.json`, `cases.json` |
| `tests/audit/bench/gen/snap_gt.py` | Real-PDF ground-truth snapping (section 4.4) |
| `tests/audit/bench/gen/make_fixtures.py` | Builds every spec in a specs dir; `--check` regenerates to temp and diffs against committed output |
| `tests/audit/bench/specs/*.json` | Public synthetic specs |
| `tests/audit/bench/fixtures/public/<doc_id>/` | `doc.pdf`, `gt.json`, `cases.json` (committed, generated or real) |
| `tests/audit/bench/fixtures/public/manifest.json` | Per doc: `doc_id`, `subset` (`synthetic`/`real`), sha256 of the three files; `manifest_sha256` over all of it |
| `tests/audit/bench/fixtures/public/ATTRIBUTION.md` | Real PDFs: arXiv id + version, title, authors, licence URL, "unmodified" |
| `tests/audit/bench/fonts/` | One vendored TTF with ligature glyphs (e.g. DejaVu Sans) plus its licence file |
| `tests/audit/bench/baseline.json` | Public baseline (written only by `--record-baseline`) |
| `~/speakeasy-holdout/bench/` | Hidden set: `specs/`, `fixtures/<doc_id>/`, `manifest.json`, `baseline.json`, `last_cases.json`. Never committed |

The existing `tests/audit/boxacc/geom.py` (page→screen geometry) and the `make_fig5.py` layout are reused. The
`tests/audit/stream/mock_llm.py` mock may be reused or extended (per-case reply file, readiness probe).

## 2. Command

```
python3 tests/audit/bench/run_bench.py [--set public|hidden|all] [--holdout DIR] [--out FILE]
                                      [--cases GLOB] [--record-baseline] [--check-determinism] [--keep-temp]
python3 tests/audit/bench/bench_gate.py BENCH_JSON [--baseline FILE] [--hidden-baseline FILE] [--public-only]
```

- `run_bench.py` runs under a Python that has `pypdfium2` (the doc-venv packages). The same interpreter is passed to the
  app as `SPEAKEASY_DOC_PYTHON`. `SPEAKEASY_BIN` overrides the CLI, which defaults to the repo `src/speakeasy`.
- `--holdout` defaults to `$SPEAKEASY_HOLDOUT`, else `~/speakeasy-holdout/bench`.
- `--set` defaults to `all` when the holdout dir has a `manifest.json`. Otherwise it defaults to `public` and prints one
  stderr line: `bench: hidden set not found at <dir>; public only`.
- `--out` defaults to `tests/audit/reports/bench.json`, written atomically (temp file + rename).
- `--cases GLOB` runs only the matching case ids, for debugging. The output then has `"complete": false`.
- `--record-baseline` requires a complete run. It writes `tests/audit/bench/baseline.json` and, when the hidden set ran,
  `<holdout>/baseline.json`. Nothing else ever writes a baseline.
- `--check-determinism` runs the selected sets twice and compares every determinism-covered field (section 9).
- `--keep-temp` keeps the temp root and prints its path. By default the temp root is removed on exit, including on error.

Exit codes for `run_bench.py`: `0` finished and `bench.json` written (whatever the scores); `2` preflight, usage or
fixture error, nothing run; `3` harness error during runs (`bench.json` written with `"complete": false` and `errors`);
`4` determinism mismatch. For `bench_gate.py`: `0` pass; `1` regression; `2` usage error, incomplete or malformed
bench, missing or stale baseline.

## 3. Environment and isolation (offline, headless)

- Each CLI run gets a fresh home under one temp root. The harness asserts before every run that home is under that root
  and is not `~/.speakeasy`. The harness never opens a path under `~/.speakeasy`. In the hidden dir it only reads
  `fixtures/`, `manifest.json` and `baseline.json`, and writes only `last_cases.json` (always) and `baseline.json`
  (with `--record-baseline`).
- **Environment scrub:** every inherited `SPEAKEASY_*`, `MOCK_*` and `STUB_*` variable is removed. Then exactly these are
  set: `SPEAKEASY_HOME` and `HOME` = the run's home, `TMPDIR` = a temp dir under the root,
  `SPEAKEASY_LLM_URL=http://127.0.0.1:<port>/v1`, `SPEAKEASY_MODEL=mock-text`, `SPEAKEASY_LLM=local`,
  `SPEAKEASY_VISION=never`, `SPEAKEASY_GROUND=off`, `SPEAKEASY_TTS=say`, `SPEAKEASY_DOC_READ=local`,
  `SPEAKEASY_DOC_PYTHON=<interpreter>`, `SPEAKEASY_TRACKER=<nonexistent path>` (no OCR, so the result is deterministic),
  `SPEAKEASY_FOCUS_TAGS=off`, `SPEAKEASY_STREAM=1`, `SPEAKEASY_CAPTURE_FIT=off`, `SPEAKEASY_TEXT_HIGHLIGHT=on`,
  `SPEAKEASY_FIGURE_CUES=on`, `SPEAKEASY_KEEP_CAPTURE=0`, `SPEAKEASY_LOG_TEXT=1` (temp home only, deleted after; needed
  for chunk attribution, section 5.3), `STUB_PLAY_SEC=0.05`. `PATH` is prefixed with `tests/audit/nosel/stubs` and
  `tests/audit/tts/stubs`.
- **Mock LLM:** an OpenAI-compatible server on `127.0.0.1` and an ephemeral port (bind port 0; never a fixed port). Each
  chat request is answered with the case's script, streamed one word per token. Settings: `MOCK_FIRST_DELAY=0.5`,
  `MOCK_TOKEN_DELAY=0.002`. Readiness is polled with `GET /v1/models` (up to 5 s), never a fixed sleep.
- **No network:** after each run, the runs record must have `llm_provider_configured == "local"`, and the mock log must
  hold every chat request of the run. Otherwise the harness errors with exit 3.
- **No images:** cases never pass `--image`, so there is no vision call, no capture fit and no "hold" show. The VLM
  grounding path and Read-area (`--capture-method area`) are out of scope for Phase 2 (section 12).

## 4. Fixtures

### 4.1 `gt.json` (one per document, page space)

```json
{"schema": "speakeasy-bench-gt/1", "doc_id": "syn-twocol-01", "subset": "synthetic|real",
 "gt_source": "generator|agent", "gt_method": "<free text>", "reviewed_by_human": false, "license": "synthetic|CC-BY-4.0|CC0-1.0",
 "pages": [{"n": 1, "w": 595.3, "h": 841.9, "blocks_scored": true,
            "lines": [[x0, y0, x1, y1], ...],
            "blocks": [{"id": "p1b3", "kind": "paragraph|caption|figure|table|heading|header|footer|page_number|equation|other",
                        "bbox": [x0, y0, x1, y1], "label": "Figure 5"}]}],
 "sentences": [{"id": "s12", "page": 1, "text_hash": "0123456789ab", "score": "lines|parts|none",
                "line_rects": [[x0, y0, x1, y1], ...], "part": "pt3"}],
 "parts": [{"id": "pt3", "name": "Post-Processing", "figure": "Figure 5", "panel": "a", "page": 1,
            "bbox": [x0, y0, x1, y1], "style": "inside|adjacent|callout|raster", "snapped": true}]}
```

- `pages[].lines` holds every body text line on the page (prose, captions, headings, headers, footers), in both columns.
  Text inside figure and table regions is excluded.
- Block definitions (the same for generator and agent ground truth):
  - **paragraph** = a maximal run of consecutive lines of one paragraph within one column. A paragraph that continues
    in the next column or on the next page is two blocks. bbox = union of its line rects.
  - **caption** = the caption's lines (from "Figure N" / "Table N" to the end of the caption text). bbox = union of
    those line rects.
  - **figure** = union of the figure's graphics (paths, images) and the text inside them (panel labels, part labels,
    callout numbers), **without** the caption.
  - **table** = union of the table's rules and cell text, **without** the caption.
  - Every other kind is an *ignore region* (section 6.1).
- `sentences[].line_rects`: the rects (each equal to an entry of `pages[].lines`) of every line holding at least one
  character of the sentence. A sentence lies on one page.
- `text_hash` = first 12 hex chars of SHA-256 over the sentence text with whitespace runs collapsed to one space and
  stripped. It is used only to cross-check `cases.json`.
- `score` says which category scores the sentence: `lines` (prose or caption read-through), `parts` (names exactly one
  part; `part` is required) or `none` (spoken but not scored, e.g. table rows).

### 4.2 `cases.json`

```json
[{"case_id": "syn-twocol-01/c03", "doc_id": "syn-twocol-01", "page": 1,
  "selection": [{"text": "<line as a PDF viewer copies it, incl. trailing hyphen>", "rect": [x0, y0, x1, y1]}, ...],
  "script": [{"sentence_id": "s12", "text": "<sentence as spoken>"}, ...]}]
```

- `selection` is contiguous page lines on one page, in reading order. The CLI gets them on stdin, joined with `\n`.
- `script` is the mock's reply: the texts joined with single spaces. Every `sentence_id` exists in `gt.json` and its
  `text_hash` matches the text. Fixture text lives only in fixture files. It never appears in `bench.json`.

### 4.3 Synthetic generator (`gen/synth.py`)

- It is driven by a spec file only. Different documents are different specs. No document content lives in generator
  code except defaults.
- reportlab `Canvas(..., invariant=1)` is used, so the same spec gives byte-identical PDFs. `make_fixtures.py --check`
  fails if regenerating a committed fixture changes `doc.pdf`, `gt.json` or `cases.json`.
- Ground truth comes from the generator's own drawing coordinates, never from parsing the PDF.
  - A line rect is x from the string origin to origin + `stringWidth`, and y from baseline − ascent to
    baseline + |descent| (font metrics).
- Ligatures are written as the code points U+FB00–U+FB04 with the vendored TTF. Hyphenation is written as a word split
  at a line end with `-`. The selection keeps the hyphen and line break, and the script uses the joined word.
- Panels are labelled with drawn `a)`/`(a)` text. Named parts are drawn shapes whose label is inside or adjacent to the
  shape. A numbered callout is a circled number with a leader line to a shape; the part's `name` is `"callout N"` and
  its bbox is the pointed-to shape.

### 4.4 Real PDFs (agent-made ground truth, flagged)

- These are 3–5 arXiv papers per set, licence **CC BY 4.0 or CC0 only** (as shown on the arXiv abs page), committed
  unmodified, each ≤ 8 MB. `ATTRIBUTION.md` lists each one.
- `gt_source: "agent"`, a `gt_method` describing the procedure, and `reviewed_by_human: false` until a human signs
  off. `bench.json` reports real-subset scores separately (section 7).
- Procedure:
  - **Lines:** from pdfplumber character boxes grouped into text lines.
  - **Blocks:** the agent assigns membership on the rendered page (which lines form a paragraph or caption, which
    region is a figure or table). `snap_gt.py` then computes the tight bbox: union of member line rects, or union of the
    graphic objects and text inside the region.
  - **Parts:** the agent names the part and gives a rough region. It is snapped to the smallest graphic object holding
    the label or region. If there is none, the agent's rect is kept with `snapped: false`.
- **Forbidden:** producing or adjusting ground truth with `src/docmap.py`, `src/focus.py`, `src/pdfpage.py`, any
  `speakeasy` run or any bench output.

### 4.5 Coverage minima and validation

Each set (public and hidden independently) must contain:

| Item | Minimum |
|---|---|
| Synthetic docs | 6, covering: multi-page single column (≥ 3 pages, headings, footers); two-column with ≥ 10 hyphen breaks, ≥ 10 ligatures and a paragraph continuing across the column break; (a)/(b) panel figure with ≥ 4 named parts per panel and ≥ 3 numbered callouts; table directly above a figure (Fig. 5 layout) plus a standalone ruled table; full-width figure on a two-column page; figure whose panels are named only in the caption, plus one raster part |
| Real docs | 3, with ≥ 2 two-column and ≥ 1 multi-panel figure |
| Cases | 40 |
| Line-scored sentences | 60 synthetic + 30 real |
| Part mentions | 30 synthetic (≥ 8 callouts) + 10 real |
| Scored GT blocks | 60 synthetic (each of the 4 kinds ≥ 8) + 40 real |

The hidden set uses the same generator and format, but different spec files, texts and layout parameters, and different
arXiv papers. When both sets are present, no `doc_id` and no PDF sha256 may appear in both.

`validate_fixtures.py` (run first, fail fast with exit 2) checks:
- schema of `gt.json`, `cases.json` and the manifest;
- sha256 values against the manifest;
- every bbox inside its page with `x0 < x1` and `y0 < y1`;
- every `line_rects` entry is an entry of `pages[].lines`;
- script sentences end in `.`, `!` or `?`, contain none of `[ ] < > { }`, and match their `text_hash`;
- `lines`-scored sentences contain no figure/table reference (`Fig. N`, `Figure N`, `Table N`);
- a part sentence's part is on the sentence's page;
- the coverage minima above;
- the cross-set duplicate check.

Each error message names the file, the JSON path and the rule.

## 5. Run procedure

### 5.1 Block predictions (per document)

For each document, run `<interpreter> src/docmap.py parse-pdf <copy of doc.pdf> <out.json>` (no `--sel`, so it parses
every page; same code the app's subprocess runs) with the scrubbed environment. Predictions are
`pages[n].blocks` of the output (logging contract D). A non-zero exit or missing output is a harness error.

### 5.2 Case runs

For each case:
1. Copy `doc.pdf` into the case's temp dir.
2. Write a doc hint `{"app": "Preview", "bundle": "com.apple.Preview", "title": "<doc_id>.pdf", "ax_document": "file://<copy>"}`.
3. Fixed screen geometry G0, recorded in `bench.json.config`: zoom 1.25, backing 2, display origin `(0, 33)`, page placed
   so the first selected line is at screen y 300 (`geom.page_to_screen`).
   - `--capture-rect` = union of the selected lines mapped to screen, padded 16 pt on each side (the hotkey's bounds
     convention).
   - `--line-bounds` = one `[x, y, w, h]` per selected line.
4. Run `speak --capture-method bounds --capture-rect=… --line-bounds … --doc-hint … --run-id bench-<n>` with the
   selection on stdin:
   - **warm-up** run first (fills the parse cache);
   - delete `home/logs` (keep `home/cache`);
   - **measured** run.
5. The measured run must exit 0 and write exactly one runs record. Otherwise it is a harness error naming the case and
   the last stderr line.
6. **Document-ready check:** the measured run is valid only when `doc_ready_ms` is non-null and
   `doc_ready_ms <= first_sentence_ms`.
   - An invalid run is retried (warm-up not repeated) up to 2 more times, then it is a harness error
     `doc not ready before first sentence (case …)`.
   - This guarantees that a missing `doc.target` is an app mapping failure, not a race.

### 5.3 Chunk → sentence attribution

1. Take the measured run's focus-log `map` records in file order. Each has `sent` (logging contract A) and `text` (text
   logging is on in the temp home).
2. Let S be the script string, with whitespace runs collapsed. Set cursor = 0.
3. For chunk `sent = k`, find the first occurrence of its normalised logged text in S at index ≥ cursor.
   - Not found → harness error `chunk k not in script (case …)`. This means the app altered the text (scrub, demeta);
     it is reported, never scored.
4. The chunk belongs to the script sentence whose span in S contains that index. Set cursor = index + length of the
   logged text.
5. A script sentence that gets no chunk is `not_spoken`.
6. The chunk's **show** is the last focus-log `show` record with the same `sent`. A chunk with no show record has
   `no_show`.

## 6. Scoring rules

Shared definitions:
- `IoU(a, b)` = area(a∩b) / area(a∪b); `0` when the union is empty.
- `centre(r)` = `((x0+x1)/2, (y0+y1)/2)`.
- Percentages are `100 * hits / n`, rounded to 2 decimals. They are `null` when `n == 0`.
- Percentiles are nearest-rank: sort ascending, then `p_q = v[ceil(q*n) - 1]`.

### 6.1 Boxes (from the parse output)

- Scored pages are the GT pages with `blocks_scored: true`. Scored kinds are K = {paragraph, caption, figure, table}.
- GT blocks of any other kind are *ignore regions*.
- **Matching** at threshold t, separately per page and per kind k ∈ K:
  1. Take every (GT g of kind k, prediction p of kind k) pair with `IoU >= t`.
  2. Sort by IoU descending, then GT id ascending, then prediction index ascending.
  3. Accept pairs greedily when both g and p are still unmatched.
- **Hit** = GT block matched at **t = 0.75**.
- **Precision/recall at t = 0.5:** a prediction of a scored kind is *ignored* when it is unmatched at 0.5 and has
  `IoU >= 0.5` with any ignore region. Kinds outside K are ignored.
  - `P'` = the remaining predictions of scored kinds on scored pages. Predictions are by contract the post-threshold
    set the app uses.
  - `recall_50 = 100 * matched50 / |GT|`, `precision_50 = 100 * matched50 / |P'|`.
- **boxes.score** = `100 * hits75 / |GT scored blocks|`. Also reported per kind, per subset and per doc (public set
  only).

### 6.2 Lines (per `lines`-scored sentence s)

Notation: G = `s.line_rects`; L_page = `pages[s.page].lines`; C = chunks of s.

`covers(B, L)` holds when, with ov_x and ov_y the overlap lengths of B and L on each axis:
`ov_y >= 0.5 * height(L)` and `ov_x >= 0.5 * width(L)`.

s is a **hit** when every chunk c ∈ C has a show where:
- `cue_kind == "text_band"`, `doc.kind == "line"`, `doc.target` is present (call it B_c) and `doc.page == s.page`;

and, over all chunks:
- **coverage:** the number of rects in G covered by at least one B_c is ≥ `ceil(0.75 * |G|)`. So 1 line → 1, 2 → 2,
  3 → 3, 4 → 3, 5 → 4;
- **no over-extension:** for every chunk, the rects in L_page \ G covered by B_c number ≤ 1. At most one line beyond
  the sentence, which also catches bands reaching into the other column.

**lines.score** = `100 * hits / n`. Miss reasons are reported as the first failing condition in this order:
`not_spoken`, `no_show`, `no_doc_target`, `wrong_cue`, `wrong_page`, `under_cover`, `over_extend`.

Screen-only bands (no `doc.target`) are misses. The page-space mapping is what is measured, and section 5.2 rules out
the document race.

### 6.3 Parts (per `parts`-scored sentence s naming part p)

O = the shows of s's chunks with `cue_kind == "part_oval"`.

s is a **hit** when O is non-empty and every o ∈ O has `doc.page == p.page` and `centre(o.doc.target)` inside `p.bbox`
(edges inclusive).

- **parts.score** = `100 * hits / n`. Miss reasons: `not_spoken`, `no_show`, `no_oval`, `wrong_page`, `centre_outside`.
- **parts.precision** = `100 * good / total`:
  - total = every `part_oval` show in the set, across all chunks, including chunks of sentences that are not
    parts-scored;
  - good = those whose chunk belongs to a parts-scored sentence and whose centre lies inside that sentence's part bbox.
  - It is reported and gated, so drawing ovals everywhere cannot raise the scores.
- Parts are also reported per `style` (inside, adjacent, callout, raster).

### 6.4 Speed (per measured case run)

Fields are from the runs record, all in ms from run start (logging contract C):
- `first_highlight_ms = first_focus_ms - (llm_request_at_ms + llm_first_token_ms)`. This is the app's own time from
  the first model token to the first box: sentence release, mapping and TTS start. The mock's fixed delays are excluded.
- `audio_to_box_ms = first_focus_ms - tts_first_audio_ms`. Reported only.
- A run with any of these null counts in `speed.n_missing` and is left out of the percentiles.
- Report p50 and p95 per set. **Budget:** p50 and p95 of `first_highlight_ms` < 300 ms.

## 7. `bench.json`

```json
{"schema": "speakeasy-bench/1", "complete": true, "generated_at": "<ISO>", "duration_s": 0.0,
 "commit": "<sha or null>", "dirty": false,
 "versions": {"python": "3.12.x", "pypdfium2": "x", "reportlab": "x|null", "pdfplumber": "x|null"},
 "config": {"box_iou_hit": 0.75, "box_iou_pr": 0.5, "line_cover_frac": 0.75, "line_axis_overlap": 0.5,
            "line_extra_max": 1, "speed_budget_ms": 300, "mock_first_delay_s": 0.5, "mock_token_delay_s": 0.002,
            "stub_play_s": 0.05, "geometry": {"zoom": 1.25, "backing": 2, "display": [0, 33]}},
 "fixtures": {"public": {"manifest_sha256": "…", "docs": 9, "cases": 48}, "hidden": {"manifest_sha256": "…", "docs": 9, "cases": 45}},
 "sets": {
   "public": {
     "boxes": {"score": 0.0, "hits": 0, "gt": 0, "precision_50": 0.0, "recall_50": 0.0, "matched_50": 0, "pred": 0,
               "by_kind": {"paragraph": {"score": 0.0, "hits": 0, "gt": 0, "precision_50": 0.0, "recall_50": 0.0}},
               "by_subset": {"synthetic": {}, "real": {}}, "by_doc": {"<doc_id>": {"hits": 0, "gt": 0, "matched_50": 0, "pred": 0}}},
     "lines": {"score": 0.0, "hits": 0, "n": 0, "miss_reasons": {"no_doc_target": 0}, "by_subset": {}},
     "parts": {"score": 0.0, "hits": 0, "n": 0, "precision": 0.0, "ovals": 0, "miss_reasons": {}, "by_style": {}, "by_subset": {}},
     "speed": {"first_highlight_ms": {"p50": 0, "p95": 0, "n": 0}, "audio_to_box_ms": {"p50": 0, "p95": 0}, "n_missing": 0},
     "cases": [{"case_id": "…", "doc_id": "…", "subset": "synthetic", "lines": {"hits": 0, "n": 0}, "parts": {"hits": 0, "n": 0, "ovals": 0},
                "first_highlight_ms": 0, "misses": [{"sentence_id": "s12", "category": "lines", "reason": "over_extend"}]}]},
   "hidden": {"boxes": {}, "lines": {}, "parts": {}, "speed": {}}},
 "errors": []}
```

- `by_subset.real` is labelled with `"gt_source": "agent"` and `"reviewed_by_human"` (true only if every real doc in
  the set is reviewed).
- The **hidden set carries aggregates only**: no `cases`, no `by_doc`, no ids. Its per-case detail goes to
  `<holdout>/last_cases.json`, never into the repo.
- No document text, file paths or capture data appear anywhere in `bench.json`. It holds ids, numbers, enums and hashes
  only.
- `"complete": false` when `--cases` was used or a harness error occurred. `errors` holds `{case_id|null, code, message}`
  with no text from fixtures.

## 8. Baselines and the gate

- **Baseline file** (`tests/audit/bench/baseline.json`, `<holdout>/baseline.json`):
  `{schema, set, recorded_at, commit, config, manifest_sha256, metrics: {boxes: {score, precision_50, recall_50},
  lines: {score}, parts: {score, precision}, speed: {p50, p95}}}`.
- **Gated metrics** per set: `boxes.score`, `boxes.precision_50`, `lines.score`, `parts.score`, `parts.precision`.
- `bench_gate.py` rules, in order:
  1. A bench that is not `complete`, has a different `schema`, or is malformed → exit 2.
  2. For each set in the bench, its baseline must exist. Its `config` and `manifest_sha256` must equal the bench's →
     else exit 2 `baseline stale for <set>: re-record with run_bench.py --record-baseline`.
  3. A hidden baseline exists but the bench has no hidden set → **fail** (exit 1) `hidden set not run`. With
     `--public-only` it prints a warning instead.
  4. For each gated metric: `delta = current - baseline`. **Fail when `delta < -1.0`** (a drop of more than 1 point).
     Baseline `null` → skipped (`n/a`). Baseline non-null and current `null` → fail.
  5. Speed: **fail when `first_highlight_ms` p50 ≥ 300 or p95 ≥ 300** in any set. This is absolute; speed is never
     compared with the baseline.
  6. Improvements never fail. When any gated metric rose by more than 1 point, the gate prints
     `baseline can be raised (--record-baseline)`.
- Output: one row per set × gated metric (`set metric baseline → current delta PASS|FAIL`) and the speed rows. Then
  informational per-subset and per-kind deltas (public set only for per-doc). The last line is `GATE PASS` or
  `GATE FAIL (<n> regressions)`. Hidden rows show aggregates only.
- **Release step (Phase 2):**
  - The baselines are recorded only after the Phase 2 logging commit has landed. A bench run before that commit cannot
    score, because the fields it needs (`sent`, `first_focus_ms`, `doc_ready_ms`, `pages[].blocks`) don't exist yet.
  - Steps: on the post-commit tree, run `run_bench.py --set all --check-determinism`, then
    `run_bench.py --set all --record-baseline`.
  - Commit `tests/audit/bench/baseline.json` together with the recording commit's sha. The hidden baseline stays in
    the holdout dir.
  - Re-record the same way after any later commit that bumps `PARSE_VERSION`, changes fixtures or changes `config`. The
    gate's stale-baseline check (rule 2) catches fixture and config changes. A `PARSE_VERSION` bump is a release-checklist
    item.

## 9. Determinism

- Two `run_bench.py` runs on the same commit, fixtures and machine give **identical** values for every field under
  `sets.*.boxes`, `sets.*.lines`, `sets.*.parts` (including `cases[*]` except `first_highlight_ms`), `fixtures` and
  `config`. Only `generated_at`, `duration_s`, `speed` and per-case `first_highlight_ms` may differ.
  `--check-determinism` enforces this and exits 4, listing the differing JSON paths.
- What makes it so:
  - committed, byte-stable fixtures;
  - scrubbed environment;
  - no images, no OCR (`SPEAKEASY_TRACKER` unset to a nonexistent path), no grounding;
  - warm parse cache;
  - the document-ready check (5.2);
  - attribution by text position, not timing (5.3);
  - a fixed iteration order (docs by `doc_id`, cases by `case_id`, sentences by script order);
  - integer counts before any percentage;
  - deterministic tie-breaks in matching (6.1).
- `versions` are recorded. When a baseline's `versions.pypdfium2` differs from the bench's, the gate prints a warning
  but does not fail.

## 10. Failure cases

| Condition | Behaviour |
|---|---|
| Interpreter lacks `pypdfium2` | exit 2: `bench: pypdfium2 not importable in <python>; use the doc venv` |
| `make_fixtures.py` / `--check` without reportlab | exit 2: `reportlab not importable` (the bench itself does not need reportlab) |
| Fixture dir, `doc.pdf`, `gt.json`, `cases.json` or manifest missing | exit 2: `missing fixture: <set>/<doc_id>/<file>` |
| sha256 differs from manifest | exit 2: `fixture changed: <set>/<doc_id>/<file>; regenerate the manifest and re-record baselines` |
| Validator rule broken | exit 2 with file, JSON path and rule |
| `--set hidden` / `all` but holdout dir or its manifest missing | exit 2: `hidden set not found at <dir>` |
| Mock not ready within 5 s | exit 3: `mock LLM did not start` |
| CLI non-zero exit, timeout (60 s per run) or no runs record | exit 3 with case id and last stderr line |
| Runs record not `local` or a chat request missing from the mock log | exit 3: `run left the machine or bypassed the mock (case …)` |
| Document not ready after 3 tries | exit 3 (5.2) |
| Chunk not found in script | exit 3 (5.3) |
| `docmap.py parse-pdf` fails | exit 3 with doc id and last stderr line |
| Gate: missing or stale baseline, incomplete bench | exit 2 |

On exit 3 the bench still writes `bench.json` with `"complete": false`, which the gate refuses.

## 11. Acceptance

- `test_score.py` passes. It covers at least:
  - IoU matching: one GT block with two overlapping predictions (only one hit; the other lowers precision); a
    prediction on a heading region is ignored; kind mismatch is not a hit;
  - lines coverage for 1, 2, 4 and 5-line sentences at the `ceil(0.75 n)` boundary;
  - over-extension of exactly 1 line (hit) and 2 lines (miss);
  - a band spanning both columns (miss);
  - a sentence split into two chunks whose bands together cover it (hit);
  - part centre on the bbox edge (hit) and 0.1 pt outside (miss);
  - parts precision with a stray oval on a non-part sentence;
  - nearest-rank percentiles;
  - every gate rule in section 8, including the exactly −1.00 boundary (pass) and −1.01 (fail).
- `validate_fixtures.py` passes on the public set. `make_fixtures.py --check` reports no drift.
- `run_bench.py --set public --check-determinism` exits 0.
- `run_bench.py` followed by `bench_gate.py` passes against a freshly recorded baseline. It fails when one known
  regression is injected into `bench.json`: `lines.score` lowered by 1.5.
- No file under `~/.speakeasy` changes during a bench run (mtime listing before and after).

## 12. Out of scope for Phase 2

- The tracker's own drift (screen boxes vs page targets on real windows) remains measured by `tests/audit/box_accuracy.py`
  on real logs. The tracker `page_rect` field (logging contract E) makes that exact.
- VLM grounding, Read-area OCR, raster-figure OCR labels, vision runs and Markdown documents are excluded. Raster parts
  stay in the fixtures as headroom and are expected to miss.
- Table-row bands (sentences scored `none`).
