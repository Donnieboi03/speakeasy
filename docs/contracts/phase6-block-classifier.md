# Phase 6 contract: block classifier (figures and tables without a usable caption)

Scope: decide **what kind** a page region is (figure, table, text, none) where today's caption-anchored typing cannot:
uncaptioned figures and tables, unusual or non-English captions, regions with no text layer, and tables embedded as
images. This file prescribes **observable behaviour**, not code. Terms are as in `phase1-readability.md`,
`phase2-logging.md`, `phase2-benchmark.md`, `phase3-sentence-ids.md`, `phase4-layout.md`, `phase5-parts.md` and
`phase7-usable-app.md`. Colours and cue styles are from `docs/design.md`.

**Order:** Phase 7 ships **before** Phase 6 (the user's decision, 2026-10-26). Phase 6 builds on post-Phase-7 `main`
and keeps every Phase 7 output (section 9.4).

- *Candidate* = a region this phase types (section 3).
- *Standard caption* = a line that today's `CAP_RE` / `TAB_RE` match.
- *Lexicon caption* = a caption found only by the section 4 lexicon.
- *Stage B* = the local image classifier (section 5).
- *Stage C* = the VLM fixed-option pick (section 6).
- *Kind job* = the background job that runs stage C for one document (section 6.1).
- *Gap family* = the new fixtures of section 12.
- *B5'* = the public and hidden bench baselines re-recorded **on post-Phase-7 `main`**, before any Phase 6 code
  (Phase 7 section 1). Phase 7 A7.10 keeps the gated metrics equal to the Phase 5 baseline, so B5' matches it within
  run-to-run noise. Every "against B5'" in this file means against these baselines.

Caption regex first, unchanged: a standard caption decides its figure's kind exactly as today. Nothing in this phase
changes a figure or table that already has a standard caption.

## Prior art (decided before this contract)

Candidates were checked against the public bench on 2026-10-26 (section 0). Licences were checked on the model cards.

| Gap | Decision | What is adopted | Licence / size | Why |
|---|---|---|---|---|
| 1. Uncaptioned figures / tables | **Build on top** | **PP-DocLayout-S** (already shipped, Phase 4) proposes the region and its class. **Docling `DocumentFigureClassifier-v2.5`** (EfficientNet-B0, 26 classes, ONNX), used as-is, types it. The **VLM pick** only arbitrates | Classifier: MIT (HF card; docling.rs re-hosts it as Apache-2.0), 16.9 MB ONNX, runs on the onnxruntime the doc venv already has | Measured: 26/27 figure-vs-table right at about 10 ms per crop on CPU. It has **no text class**: 20 of 44 text crops came out as `table`, so it may only see regions the detector already calls a figure or table |
| 2. Unusual / non-English captions | **Build fresh** (deterministic) | A caption-noun lexicon with Roman numerals. The detector's `figure_title` / `table_title` / `chart_title` boxes act as evidence. Stage B decides only ambiguous nouns ("Exhibit") | None (code) | Measured on the public PDFs: the two real misses are `TABLE I` (Roman numeral) and a `Fig. 3.` line merged with hidden text-layer junk. Both are text rules, not a vision problem |
| 3. No text layer (scanned pages, screenshots, Read-area runs with no document) | **Build on top** | The detector and stage B run on the raster (the page render or the area capture). Text comes from **Apple Vision OCR**: the app's existing OCR lines for area runs, and a tracker-binary OCR subcommand for scanned pages | OS framework, no download | Already used by `tracker.swift` and `AreaSelector.swift`. On-device, 26 languages |
| 4. Tables embedded as images | **Use as-is** (structure) + build on top (glue) | Stage B types the region (Docling `table` precision 0.986 / recall 0.968 on its test set). Rows come from **Apple Vision `RecognizeDocumentsRequest`** (macOS 26+: tables → cells → rows). Below macOS 26, `VNRecognizeTextRequest` lines are grouped by today's `table_rows` rule | OS framework | Speakeasy's `table_rows` only needs row rects plus row tokens. OCR supplies both |

**Rejected:**
- **Microsoft Table Transformer** (MIT, 110 MB, repository archived). It is detection/structure only and still needs OCR,
  and PP-DocLayout-S already detects tables.
- **TableFormer / Docling table structure** (CDLA-Permissive-2.0 / Apache-2.0): heavier, and it needs OCR too. Kept as an
  upgrade path only if `image_table_rows` misses its target and the misses are structure misses.
- **RapidOCR** (Apache-2.0): it brings opencv. It would also be a second OCR engine that disagrees with the tracker's.
- **PP-DocLayout_plus-L** (Apache-2.0, 130 MB): an upgrade path for detector recall only (as Phase 4's egret-medium),
  behind the same adapter.
- **VLM-only typing:** it was perfect on the public crops (71/71), but it needs Ollama, and under load it took 3.9 s p50
  per crop. It stays the arbiter for the cases stage B cannot settle.
- **Fine-tuning (Visual Jev answer SFT, +0.055 macro):** out of scope. It is a later option if stage C becomes the
  bottleneck.

## 0. Evidence and target

Measured on `91166c4` (public set only; throwaway scripts under `/tmp/p6`; nothing in the repo was run or written):

| Measurement | Result |
|---|---|
| Public GT figure/table blocks on scored pages | 27 (16 figures, 11 tables). **All** have standard captions. 0 uncaptioned, 0 image tables, 0 pages without a text layer |
| `boxes.wrong_type` (the Phase 5 verifier's scorer applied to a HEAD parse; it reproduces boxes 100.0 / precision_50 81.88) | figure 0, table 0. Paragraph 2 (as equation 1, as heading 1) |
| Detector census over **all** 29 pages of the 9 public PDFs | 58 body boxes (25 image, 18 chart, 15 table). 5 have no standard caption nearby. 3 of those are panels of captioned multi-panel figures (the chaining takes them). **2 are real drops**, both on the unscored page 3 of `arx-2402.10153`: an IEEE `TABLE I` (Roman numeral; the detector class is `image` at 0.57, so the type is wrong too) and `Fig. 3.`, whose text-layer line reads `ygy Fig. 3. …` |
| Detector title boxes whose text is not a standard caption | 18 of 51. A title box alone is too noisy to create a caption |
| Stage B on 71 public GT crops (16 figures, 11 tables, 44 paragraphs/headings) | Figure vs table: 26/27 (one figure read as `table`). Text crops: `table` 20, `music` 12, `logo` 7, `icon` 3, other 2. CPU: 8.8–10.5 ms p50, 48 ms max |
| Stage C on the same 71 crops (`qwen3-vl:4b-instruct`, 5 options, logprobs) | 71/71 right, `p_top >= 0.95` on all. Latency 3.9 s p50 / 8.6 s p95 with Ollama shared by running Phase 5 bench/pick jobs; the same at a 448-px and a 1024-px long side. The user's uncontended warm measurement is 110–550 ms |

Consequences:
- **The existing bench cannot measure Phase 6.** Its figures and tables are all standard-captioned, and its `wrong_type`
  for figure/table is 0. Phase 6 is scored on a **new gap family** (section 12). On the existing fixtures it is held to
  "no gated metric drops by more than 1" against B5'.
- The 71-crop check is small and easy. **No threshold is chosen from it** (section 13).

**Target** (gated mode, section 12.4): on the gap family, `kind.score >= 80` and `kind.precision >= 90` on **both**
sets. Why:
- **Baseline:** by fixture rule, at most 20 % of kind-scored regions have a standard caption, so today's code scores
  about 20 or less.
- **Stage B:** typed 26/27 public crops correctly, and its model card gives table precision/recall of 0.99/0.97.
- **Headroom:** about 15 points are left for detector recall on slides and scans. PP-DocLayout-S is the small model;
  the egret / plus-L upgrade path exists for that.
- **Precision >= 90** follows Phase 5's precision-first rule: a wrong figure box on prose is worse than none.

## 1. Where the code goes (merge rule)

Work starts from **post-Phase-7 `main`** (which already contains Phase 5), after B5' is recorded on that commit.
Implementers do not edit Phase 5 or Phase 7 trees in flight.

| File | Phase 6 changes |
|---|---|
| **new `src/blockcls.py`** | Candidate filter, stage B session (model lookup, sha check, preprocessing), routing table, stage C request and letter normalisation, the kind job (`blockcls.py job …`), the area-capture entry (`blockcls.py capture …`), event records. It imports `numpy` / `onnxruntime` lazily. Its name shadows no stdlib module or installed package. It is imported by sibling path inside the doc venv, like `layoutdet` |
| `src/docmap.py` | The caption lexicon (section 4) next to `CAP_RE` / `TAB_RE`, uncaptioned blocks in `parse_pdf` / `page_blocks`, merging the kind sidecar in `load`, `parse_summary` counts. `PARSE_VERSION` = **post-Phase-7 value + 1** |
| `src/layoutdet.py` | Expose the unpaired body boxes (class, score, snapped rect). Their pairing and snapping are unchanged |
| `src/document.py` | Start the kind job after a parse; start the area-capture classify for area runs; copy events and summaries into the runs record |
| `src/focus.py` | Uncaptioned figures and tables become visible targets with `fig_id`s; image-table rows (section 8); area no-doc figure/table cues (section 7.2). All of them use Phase 7's cue style values (section 9.4) |
| `src/prompts.py` | The one `KIND_PROMPT` |
| `src/tracker.swift` | An `ocr` subcommand (section 7.1), placed next to Phase 7's drawing and `cue.json` code without changing them. Live tracking is unchanged |
| `src/observe.py` | `speakeasy logs --scrub` (Phase 7 A5.4) scans `logs/blockcls.jsonl*` and learns any text-bearing key this phase adds (section 9.4) |
| `src/stats.py` | Nothing. Phase 6 keys stay out of the Phase 7 A5 whitelist unless a later contract adds them |
| Installers | `src/install.sh`, `app/bundle_python.sh` (classifier model fetch, section 2), `app/NOTICE`, `README.md` |
| Docs | `docs/architecture.md` (module row), `tests/audit/SCHEMA.md` (new keys) |

Implementers add unit tests in **new files only** (`tests/test_blockcls*.py`). Everything else under `tests/audit/`
(harness, mock, scorer, gap fixtures, cassettes, baselines) belongs to the verifier. `tools/kind_eval.py` belongs to the
implementer. `tools/kind_census.py` (section 12.6) belongs to the verifier.

## 2. Model install (stage B)

| Item | Value |
|---|---|
| File | `docling_figure_classifier_v2_5.onnx` |
| Source | `https://huggingface.co/docling-project/DocumentFigureClassifier-v2.5/resolve/f859dfbff5c9916cd996942d4b0db7fa25808220/model.onnx` (pinned revision, never `main`) |
| Size | 16,940,439 bytes |
| sha256 | `27ffc48c27ae4e12c99b6f6de0dd730005245e47b70dd0c1339e62cbac3ec4c0` |
| Install path | `$SPEAKEASY_HOME/models/docling_figure_classifier_v2_5.onnx` |
| `classifier_version` | `docling-figcls-v2.5/27ffc48c27ae` |

- Install, verification, runtime lookup (`SPEAKEASY_FIGCLS_MODEL` override, then the home, then the bundle's
  `Resources/models`) and messages follow Phase 4 section 1 **exactly**, with "PDF layout detection off" replaced by
  "block classifier off".
- Preprocessing (from the model's `preprocessor_config.json`):
  - render the crop at zoom 2;
  - bilinear resize to 224 × 224, scale `/255`;
  - normalise with mean `[0.485, 0.456, 0.406]` and std `[0.47853944, 0.4732864, 0.47434163]`;
  - feed `float32 [1,3,224,224]`, then softmax over the 26 logits.
- **Deterministic:** a fixed intra-op thread count (a constant in code).
- **Kill switch:** `SPEAKEASY_BLOCKCLS=off` (env or `config`) turns **all** of Phase 6 off, the lexicon included. The
  parse is then identical to the post-Phase-7 parse apart from `PARSE_VERSION` and the explicit
  `blockcls` disabled key (Amendment 1, A1.3). The default is `on`.
- **Licence line:** `app/NOTICE` and `README.md` name the model, Docling (IBM / docling-project) and MIT.

## 3. Candidates (what gets typed)

Classification runs **only** on these regions, at parse time, inside the parse subprocess:

1. **Unpaired detector body boxes** (`image`, `chart`, `table`; post-threshold, snapped as in Phase 4 4.3), except boxes
   that:
   - seed or chain into a captioned body (Phase 4 4.2);
   - cover >= 30 % of a captioned figure/table body or of a detector paragraph block;
   - are smaller than 24 pt on either side or 1 % of the page area;
   - cover >= 90 % of the page (page-sized backgrounds; a slide's full-bleed image is handled by 3.3).
2. **Bodies of lexicon captions whose noun is ambiguous** (section 4.2), paired or heuristic.
3. **Regions with no text layer:**
   - detector body boxes on a *no-text page* (no text lines, e.g. a scanned page), filtered as in item 1;
   - detector body boxes on the **area capture** of a Read-area run whose `area_pdf` is not `ok` (section 7.2).

Limits: at most 8 candidates per page and 32 per document job, in reading order. The rest are logged as `skipped`
(`over-cap`). Captioned figures and tables are **never** candidates, except under item 2.

## 4. Caption lexicon (gap 2)

### 4.1 What matches

A caption anchor is a line (or, see below, the text inside a detector caption box) that matches:

```
^ <noun> \s* <number> \s* ( [.:|–—] | \s-\s | $ )
```

These are the same separators as `CAP_RE` / `TAB_RE` today.

- **`<number>`:**
  - Arabic `\d+`, optionally with a dotted or hyphenated sub-number (`3.2`, `2-1`) and an optional panel letter (`3a`);
  - a supplementary prefix `S` (`S2`);
  - an appendix prefix `A.`–`Z.` (`A.1`);
  - an uppercase Roman numeral I–XL, but **only** directly after an uppercase or capitalised noun (`TABLE IV`,
    `Fig. II`).
- **Nouns** (case-insensitive unless noted; a trailing `.` is optional on abbreviations):

  | Kind | Nouns |
  |---|---|
  | figure | `figure`, `fig`, `figs`, `chart`, `graph`, `diagram`, `plate`, `illustration`, `scheme`, `map`, `abbildung`, `abb`, `bild`, `figura`, `figuur`, `grafik`, `gráfico`, `grafico`, `图`, `図` |
  | table | `table`, `tab`, `tabelle`, `tabla`, `tableau`, `tabella`, `tabela`, `tabel`, `表` |
  | ambiguous | `exhibit`, `anlage`, `anexo`, `annexe` |

- **Evidence rule.** A standard caption needs nothing extra (unchanged). A lexicon caption is accepted only when **one**
  of these holds:
  - the line is the top line of a detector caption box (`figure_title`, `table_title`, `chart_title`);
  - a detector or heuristic body of a compatible kind lies on the caption's body side within Phase 4's pairing limits.

  Phase 4 4.1's "caption run into prose" rules apply unchanged.
- **Polluted text layer.** On a detector page, when a line does not match but the part of it inside a detector caption
  box does, the match uses the in-box text. The caption block then holds only the in-box part of the line's rect.
  This fixes `ygy Fig. 3. …`.
- **Never a caption:**
  - prose such as `Table 2 shows …` or `Chart 1 of the plan …` (no separator after the number);
  - `TABLE OF CONTENTS`, `Figure skating`, `Exhibit A` (a letter is not a number);
  - lines held by a detector paragraph box with a full line before them (Phase 4 4.1).

### 4.2 Kind and label

- **Kind:**
  - figure and table nouns give their kind;
  - an **ambiguous** noun takes the kind of its body. That is stage B's family when stage B is not `textish` and
    `p_top >= T_B` (section 5), else the detector class family, else `figure`. No VLM call is made, because a caption
    exists, so the region is not running text.
- **`label`** keeps today's form so that every call site that assumes `Figure N` / `Table N` stays valid:
  `"Figure <number>"` or `"Table <number>"` by kind, with the number as printed (`Table IV`, `Figure S2`, `Figure 3.2`).
- **`printed_label`** (new) = the noun as printed, with these abbreviations expanded: `Fig.`/`Figs.` → `Figure`,
  `Abb.` → `Abbildung`, `Tab.` → `Table`, plus the number (`Abbildung 2`, `Exhibit 3`).
- **`aliases`** (new) = `printed_label` plus `label`. Sentence references (`ground_label`, figure-mode detection,
  Phase 5 A2.5) match any alias. When two figures share an alias, the one whose `printed_label` matches exactly wins.
- `figures[].caption_src` (new): `regex`, `lexicon` or `lexicon+in-box`.

## 5. Stage B: the local classifier (deterministic, in the parse)

For each candidate, crop the region plus a 4 pt margin and run the classifier. That gives a label L (one of 26),
`p_top` and `p_margin`. Families:

| Family | Labels |
|---|---|
| `table` | `table` |
| `figure` | `photograph`, `engineering_drawing`, `line_chart`, `bar_chart`, `flow_chart`, `geographical_map`, `pie_chart`, `scatter_plot`, `chemistry_structure`, `topographical_map`, `box_plot` |
| `decoration` | `logo`, `icon`, `signature`, `stamp`, `qr_code`, `bar_code` |
| `textish` | `other`, `screenshot_from_computer`, `screenshot_from_manual`, `page_thumbnail`, `full_page_image`, `music`, `calendar`, `crossword_puzzle` |

**Routing.** The detector family is `figure` for `image` / `chart` and `table` for `table`. First match wins:

| # | Condition | Decision |
|---|---|---|
| B1 | family `decoration` | **none**: no block. `outcome: rejected` |
| B2 | family `textish` | route to stage C (`route: textish`) |
| B3 | family ≠ detector family | route to stage C (`route: disagree`) |
| B4 | `p_top < T_B` | route to stage C (`route: low-p`) |
| B5 | otherwise | **accept** the family as the kind. `kind_src: classifier` |

- A routed candidate has **no block until stage C accepts it**. Until then its lines keep Phase 4's rule (they join no
  paragraph), so a pending or failed candidate behaves exactly as today.
- `T_B` is a constant in `blockcls.py`, chosen on the public gap set only (section 13) and then frozen.
- **Speed:** stage B adds <= 40 ms p95 per candidate (render + inference) and <= 80 ms p95 per page with candidates.
  Both are reported (section 14.4).

## 6. Stage C: the VLM fixed-option pick (background, local first)

### 6.1 When and where it runs

- **Not inside the parse.** The parse finishes with stage B only, so `doc_ready_ms` and Read-area's 4 s wait
  (Phase 5 A2.8 `doc-timeout`) are unaffected.
- **Start:** after a parse that produced routed candidates, `document.py` starts **one kind job** per document: a
  doc-venv subprocess, detached from speech. It renders the routed crops, asks stage C, and writes the answers to a
  sidecar next to the parse cache: `cache/doc/<parse key>.kinds.json`.
  - The sidecar key is `PARSE_VERSION`, the PDF sha256, the page, the candidate rect at 0.1 pt, the model, the model
    digest and the sha256 of `KIND_PROMPT`.
  - A cache hit (parse and sidecar both present) runs nothing.
- **Use of answers:**
  - Later loads merge the sidecar, so accepted candidates become blocks.
  - Within the run that started the job, an answer landing mid-run applies only to chunks mapped after it lands
    (today's late-update rule). A show is never redrawn because of it.
- **Precedence over picks:**
  - the job sends at most one request at a time;
  - it sends nothing while a Phase 5 pick of a live run is in flight or queued;
  - a run's picker breaker (`unavailable` / `no-logprobs`) also stops the job, and vice versa.
- **Budget:** at most 4 routed candidates per page and 16 per job. Each request has an 8 s deadline (a cold model load
  counts against it). The job has a 60 s deadline. Candidates left over are logged `skipped` (`budget`).

### 6.2 Request

`POST <base>/api/chat` (the same base as Phase 5 4.4):

```json
{"model": "<vision_model()>", "stream": false, "keep_alive": "<llm_client.keep_alive()>",
 "messages": [{"role": "user", "content": "<KIND_PROMPT>", "images": ["<base64 png>"]}],
 "options": {"temperature": 0, "num_predict": 1}, "logprobs": true, "top_logprobs": 20}
```

- **`KIND_PROMPT`** (in `prompts.py`) holds **no document text**. It lists the fixed options in a fixed order (never
  shuffled) and asks for the letter only:
  - A: figure, diagram, illustration or photo;
  - B: chart or plot of data;
  - C: table;
  - D: running text (paragraphs, headings, lists);
  - E: none of these (logo, decoration, blank).
- **Crop:** the candidate plus a 4 pt margin. The long side is `KIND_CROP_PX`, a constant in `[448, 1024]` fixed before
  the sweep (section 0 found no accuracy or latency difference between the two ends). It is a temp file
  `speakeasy_cap_kind_<pid>_<n>.png`, deleted in `finally` on every path. The crop bytes are deterministic, as in
  Phase 5 4.3.

### 6.3 Answer → decision

The letter rules are Phase 5 4.5 steps 1–4 unchanged:
- uppercase letters only;
- `"A"` and `" A"` merge;
- `letter_mass < 0.5` → `off-menu`;
- otherwise normalise over the valid letters.

Then:
- **Kind probabilities:** `p(figure) = p_A + p_B`, `p(table) = p_C`, `p(text) = p_D`, `p(none) = p_E`.
  `p_top` / `p_margin` are taken over these four kinds.
- **Top kind `figure` or `table` with `p_top >= T_C`** → `accepted`. The block is created (`kind_src: vlm`).
- **Top kind `text` with `p_top >= T_C`** → `text`. The candidate's PDF lines are released to the orphan-line rule
  (Phase 4 4.6, `src: "lines"`), so vector text the detector called an image becomes highlightable.
- **Anything else** (`none`, below `T_C`, `off-menu`, `no-logprobs`, `unavailable`, `timeout`, `error`) → no block, and
  Phase 4's line rule stays.

`T_C` is a constant in `blockcls.py`, chosen on public data only (section 13) and then frozen.

### 6.4 Provider (local first; the user's Phase 5 decision, option a)

- `SPEAKEASY_BLOCKCLS_VLM` = `auto` (default) | `local` | `off`.
- `auto` / `local`: the local Qwen only.
- **OpenRouter** is used only when **all** of these hold:
  - the setting is `auto`;
  - the user has opted into cloud grounding (`SPEAKEASY_GROUND=openrouter`, with a key);
  - the local breaker tripped (`unavailable` or `no-logprobs`) for this job.

  The same crop and prompt then go to OpenRouter chat completions with `temperature: 0`, `max_tokens: 1`,
  `logprobs: true`, `top_logprobs: 20`.
- **An OpenRouter reply without logprobs:** a valid first letter is accepted **only** when its kind equals stage B's
  family for that candidate (`confidence: "letter-only"`). Otherwise the outcome is `low-confidence`.
- `SPEAKEASY_GROUND=off` also turns stage C off. The gated bench is therefore unaffected (section 12.4).

## 7. No text layer (gap 3)

### 7.1 Scanned PDF pages

- A **no-text page** (no text lines) on a detected page still gets the detector (Phase 4 runs on the raster) and
  section 3 candidates. Every body on it is a candidate. There are no paragraph blocks (out of scope).
- **Captions on scans:** when the OCR helper is available, the parse OCRs **only** the detector caption boxes and
  table-candidate boxes. The OCR helper is `speakeasy-track ocr IN.png OUT.json`: Apple Vision, accurate level, line
  boxes plus text, plus a table structure on macOS 26+.
  - OCR caption text goes through sections 4.1 / 4.2. A match makes the body captioned, with the usual pairing.
  - No helper, or the OCR fails → bodies stay uncaptioned candidates (`ocr: unavailable`).
- OCR text lives **only in the parse cache**, like PDF text today, and never in a log.

### 7.2 Read-area runs with no document page (screenshots, web pages, `area_pdf` ≠ `ok`)

- **When:** for an area run (`--capture-method area`), the backend starts `blockcls.py capture` in the background at
  run start. It runs the detector and stage B on the capture PNG and uses the app's OCR line bounds as the text layer.
  The result is used only when `area_pdf` ends ≠ `ok`, and it never delays the first highlight.
- **Captions:** sections 4.1 / 4.2 apply on the OCR lines.
- **Uncaptioned candidates:** section 5 applies. Stage C may run for routed candidates under 6.1's budget.
- **Output:** `area_kinds` (runs log, geometry only: capture-space rects, kind, `kind_src`) plus screen-only cues:
  - **figure mode:** Phase 5 A2.2's rules with the capture-space figure rects in place of page figures (`figure_mode_src:
    area-capture`);
  - **cues:** `figure_box` on a figure rect; `table_row` bands on a table (section 8, from OCR lines). Both use the
    design styles of section 9.4;
  - clamped to the area as in A2.1. Phase 5 A2.3 (no stand-in cues) applies.
- Nothing is cached across runs: the capture is not a document.

## 8. Tables embedded as images (gap 4)

- **Image table** = a table block (captioned, lexicon or classifier-accepted) with fewer than 2 PDF text lines inside
  its body and a raster image covering >= 50 % of it.
- **Rows:** the parse OCRs the body with the helper (7.1).
  - macOS 26+: `RecognizeDocumentsRequest` table rows. Each row rect is the union of its cells, and its tokens are the
    cells' text.
  - Otherwise: OCR lines grouped by today's `table_rows` rule.
  - The rows are stored on the figure as `ocr_rows` (rects + tokens, parse cache only), with `rows_src: ocr-table |
    ocr-lines | none`.
- **Reading:** `focus` uses `ocr_rows` wherever it would call `docmap.table_rows` for that table. `match_row`, the header
  tint and the `table_row` cue are unchanged (their style is Phase 7's, section 9.4). Row bands span the table width,
  as today.
- **No OCR:** the table still gets its `table` box. Reading it falls back to the table box (no row bands, never line
  bands). `rows_src: none`.

## 9. Output fields and events (no text)

All new fields hold numbers, enums, booleans, hashes and page or capture geometry. They never hold document text, OCR
text, prompts, model output, file paths or image data, whatever `SPEAKEASY_LOG_TEXT` is. Logging never raises and never
delays speech.

### 9.1 Parse output (`parse-pdf` output, `cache/doc/*.json`, `docmap.py dump`)

| Field | Meaning |
|---|---|
| `blockcls` | `{"status": "ok"\|"unavailable", "classifier_version": str\|null, "reason": <enum>, "T_B": float, "T_C": float, "kind_job": "none"\|"queued", "routed": n}` (A1.2) |
| `pages[].kinds` | Every detected page: `{"candidates": n, "accepted": {"figure": n, "table": n}, "routed": n, "rejected": n, "skipped": {...}, "ms": int, "no_text": bool}` |
| `figures[]` | Gains `captioned` (bool), `printed_label`, `aliases`, `caption_src`, `kind_src` (`caption` \| `lexicon` \| `classifier` \| `vlm`), and for tables `rows_src` and `ocr_rows` |
| `pages[].blocks[]` | Figure/table blocks gain `kind_src`. Their `src` keeps the Phase 4 `bbox_src` enum |
| `blockcls_events` | This parse's `block_kind` records (9.2), in page order. After a sidecar merge, the stage C records are appended |

- **Uncaptioned figures and tables** get:
  - `label` = `"Figure U<page>-<k>"` / `"Table U<page>-<k>"` (k = reading order on the page) and `captioned: false`;
  - a `fig_id` by Phase 7's rule (section 9.4);
  - no `caption_bbox`.

  These labels are **never** listed in a prompt's figure labels and never match a sentence reference. They are reached
  through the selection region, area overlap and `_table_for_selection`.

### 9.2 `block_kind` record

```json
{"event": "block_kind", "page": 3, "rect": [x0, y0, x1, y1], "anchor": "uncaptioned|ambiguous-caption|no-text|area",
 "det_cls": "image|chart|table", "det_score": 0.57, "cls_label": "table", "cls_p": 0.93,
 "route": null, "stage": "classifier|vlm", "kind": "figure|table|text|none", "p_top": 0.93, "p_margin": 0.88,
 "outcome": "accepted|rejected|text|low-confidence|off-menu|no-logprobs|unavailable|timeout|error|pending|skipped",
 "ms": 11, "model": "docling-figcls-v2.5|qwen3-vl:4b-instruct", "provider": "local|openrouter|null",
 "confidence": "logprob|letter-only|null"}
```

- `route` is `textish`, `disagree`, `low-p` or `null`.
- `p_*` have 2 decimals, `null` without logprobs.
- `rect` is in page space at 0.1 pt; capture pixels for `anchor: area`.

### 9.3 Runs log

| Field | When | Meaning |
|---|---|---|
| `blockcls_events` | the run's document job produced the parse or merged a new sidecar | Copied as they are (cf. `layout_events`) |
| `doc_parse.kinds` | the matched page was detected | That page's `pages[].kinds` |
| `kind_job` | a kind job was started or found running | `{"started": bool, "routed": n, "called": n, "accepted": n, "outcomes": {...}, "breaker": null\|"unavailable"\|"no-logprobs", "ms": {"p50": int, "max": int}}` |
| `area_kinds` | area run with `area_pdf` ≠ `ok` | `{"candidates": n, "figure": [[rect]...], "table": [[rect]...], "outcomes": {...}, "ms": int}` |

- The kind job also appends its records to `logs/blockcls.jsonl` (rotated like the other logs).
- `tests/audit/SCHEMA.md` gains every new key.

### 9.4 Phase 7 outputs kept and extended

- **`fig_id`s.** Every figure or table this phase adds (uncaptioned, lexicon-captioned, or accepted by stage B or C)
  gets a `fig_id` by Phase 7 A2:
  - format `p<page>:fig<n>` / `p<page>:tab<n>`;
  - `n` = the 1-based index among that page's figures (or tables) in the page parse's reading order, counting captioned
    and added items together;
  - it appears in Phase 7's `fig_ids` (A3) whenever it gets a figure box, part oval or table row in a read.

  Rules:
  - Adding an item can shift the `n` of a later item on the same page against a Phase 7 parse. The `PARSE_VERSION`
    bump rebuilds every cache, so one parse never mixes the two numberings. Reads logged before Phase 6 keep their ids.
  - Area runs without a document page (section 7.2) get **no** `fig_id`, as in Phase 7 A2. `area_kinds` carries none.
  - A `fig_id` is not text. It is the only figure reference allowed in `blockcls_events`, `kind_job` or
    `doc_parse.kinds` when one is added there.
- **Highlight style.** This phase adds no new cue kind and no new colour. Every highlight it draws uses the shared
  values of `docs/design.md` (sections 1 and 6.6), read from Phase 7's single token sources (C1: `Tokens.swift`, the
  tracker's constants block, and the cue style in `focus.py`). No hex value appears in Phase 6 code:
  - figure box on an uncaptioned or area-capture figure: 2 px `glacier` stroke, radius 3, `ice` fill at 10 %;
  - table row band on an image table or an area-capture table: `ice` at 38 %, radius 2, no tick;
  - table header tint: `ice` at 18 %;
  - part oval on an uncaptioned figure (Phase 5 picker scope): 2 px `ember`, no fill;
  - the dark-document rule of design 6.6 and Phase 7's margin-tick rules apply unchanged.
- **Scrub.** Every Phase 6 log field is text-free whatever `SPEAKEASY_LOG_TEXT` is (section 9 header). So far the
  phase adds **no** text-bearing key. The rule for any it does add:
  - **Any key that can hold text under `SPEAKEASY_LOG_TEXT=1`** (a debug note, an OCR snippet, a caption or label
    string) must be added to `speakeasy logs --scrub` (Phase 7 A5.4) in the same change:
    - the scrub removes it, or rewrites it to `*_len` + `*_hash` like `text`;
    - it is counted under its own name in the scrub event's `fields`;
    - `privacy_audit.py` lists it.
  - The scrub's report-only scan (A5.4: `tracker.jsonl*`, `app.jsonl*`) also covers `logs/blockcls.jsonl*`, and
    expects 0 forbidden keys there.
  - Phase 6 keys never join the A5 whitelist that `stats.py` reads.

## 10. Fallbacks and failure cases (never silent, never raise)

| Case | Behaviour |
|---|---|
| `SPEAKEASY_BLOCKCLS=off` | Phase 6 is off entirely. The parse equals the post-Phase-7 parse apart from `PARSE_VERSION`. `blockcls.reason: disabled` |
| Classifier model missing / mismatch / load failure / no onnxruntime | `blockcls.status: unavailable` with Phase 4's reason enum. The lexicon still runs. **Every** candidate routes to stage C (`route: low-p`). With stage C off too → no new blocks |
| Detector unavailable (Phase 4 fallback) | No section 3 candidates. Lexicon captions pair with heuristic bodies. An ambiguous noun takes stage B's family on the heuristic body (4.2), else `figure` |
| Ollama down / HTTP 5xx / model not pulled | `unavailable`, breaker for the job. Routed candidates keep no block. Speech and shows are unaffected |
| Reply without `logprobs` | `no-logprobs`, breaker |
| Letters below 50 % of the mass | `off-menu`; no block |
| `p_top < T_C` | `low-confidence`; no block |
| Request deadline (8 s) or job deadline (60 s) | `timeout`, or `skipped: budget` for the rest. A late reply is ignored |
| Render failure, malformed JSON | `error` (exception class counted); no block |
| OCR helper missing or failing | `ocr: unavailable`. Scans stay uncaptioned; image tables get no rows (section 8) |
| Area run, `area_pdf` = `ok` | The capture classify result is discarded. Phase 5 behaviour is unchanged |
| Run stopped (`STOP`) | The kind job keeps running to its own deadline (it is per document, not per run). Area capture work is abandoned |
| Parse re-run with `--prev` | Pages copied from `--prev` keep their `kinds` and blocks. The sidecar still merges |

Every failure falls back to the post-Phase-7 behaviour for that region. No failure creates a block.

## 11. Interplay with Phase 4, Phase 5 and Phase 7

- **Phase 4:**
  - 4.2 "unpaired boxes create no block" becomes "**no block unless section 5 or 6 accepts it**". Accepted bodies are
    snapped (4.3), stay in their column (4.4) and go through `_split_overlaps` (4.5).
  - The 4.7 invariants hold, except that the caption-overlap invariant applies to captioned figures only.
  - 9's out-of-scope line "creating figures or tables without a `Figure N` / `Table N` caption" is superseded by this
    contract.
  - Class mapping, the score threshold and paragraph building are unchanged.
- **Phase 5:**
  - The parts / panels / callouts hierarchy (2.x) runs on every figure, uncaptioned ones included. Uncaptioned figures
    have `panels: []`, because no caption names panels.
  - The picker may use an uncaptioned figure as its scope.
  - A2.4 `detector-substitute` keeps priority for hairline captioned figures. A box it substitutes is not a candidate.
  - A2.2 area figure mode counts accepted uncaptioned figures as visible.
  - A2.5 whole-figure sentences match via `aliases`.
  - A2.6 out-of-scope rejection is unchanged.
  - Amendment 2 is not duplicated here: 7.2 only adds the no-page branch that A2.1 left as "screen-only cues".
- **Ollama sharing:** picks always take precedence over kind requests (6.1). Both use the same model and keep-alive,
  so a kind job also warms the model for later picks.
- **Phase 7** (shipped first): Phase 6 keeps all of its outputs and extends them as in section 9.4:
  - `fig_id`s for every added figure and table;
  - design-token cue styles;
  - the scrub learns any text-bearing key.

  Phase 6 changes nothing in the bar, the Reading log, `stats.py`, `app.jsonl` or `cue.json`. Phase 6 cues carry
  Phase 7's cue tokens and per-show results like any other cue.

## 12. Benchmark (verifier-owned, under `tests/audit/`)

### 12.1 What is measured before building

1. **`wrong_type` breakdown:** from the Phase 5 verifier's `boxes.wrong_type`, at B5' on both sets (hidden: aggregates
   only). On public at `91166c4` it is figure 0 / table 0 / paragraph 2, so the existing fixtures contain no type
   errors to fix.
2. **Uncaptioned and image-table census:** `tools/kind_census.py --set public|hidden` runs the detector over **every**
   page of each set's real PDFs. It prints aggregates only:
   - body boxes by class;
   - unpaired after Phase 4 chaining;
   - title boxes that are not standard captions;
   - lexicon-only caption lines;
   - image tables (as defined in section 8);
   - no-text pages.

   For hidden, no per-doc or per-page line ever reaches the terminal or the repo. Public at `91166c4`: 58 bodies,
   2 real drops (`TABLE I`, polluted `Fig. 3.`), 0 image tables, 0 no-text pages.
3. **Gap-family baseline:** the gap family (12.2) runs on post-Phase-7 code **before** any Phase 6 code. The result is
   recorded as `B6-pre` (`kind` block only). By fixture rule it should be about 20 or less.

### 12.2 Gap family fixtures (new; public and hidden, split like Phase 2)

- **Paths:**
  - public: `tests/audit/bench/specs-gap/` and `tests/audit/bench/fixtures/public-gap/` (own `manifest.json` and
    `ATTRIBUTION.md`);
  - hidden: `<holdout>/gap/` (own specs, fixtures, manifest, baseline).
- **Rules:**
  - The existing families and B5' are untouched, so the B5' gate stays valid.
  - Hidden uses different spec files, texts, layout parameters, languages and real docs. The cross-set duplicate rule
    of Phase 2 4.5 applies.
- **GT:** generator coordinates (reportlab `invariant=1`, the vendored DejaVu font), never Speakeasy output.
  `gt.json` pages gain `kind_scored: true`, and figure/table blocks gain these flags (scorer breakdowns only):
  - `gap`: `uncaptioned` | `lexicon` | `no-text` | `image-table` | `standard`;
  - `caption_style`;
  - `raster`.

  New ignore kinds for decoys: `decoration` and `text-image`.
- **Coverage minima, per set:**

  | Gap | Minimum |
  |---|---|
  | G1 uncaptioned | >= 4 slide pages (16:9, 720 × 405 pt) with >= 8 uncaptioned figures (vector chart, raster photo-like image, flow diagram) and >= 3 uncaptioned tables (ruled and borderless). >= 2 report pages with an uncaptioned figure between paragraphs |
  | G2 lexicon | >= 10 captions over >= 6 nouns, including `Exhibit` on a figure **and** on a table, a Roman `TABLE` numeral, `Fig. S<n>`, `Abb.`, `Tabelle`, `Tabla`/`Tableau`, `Chart`. >= 4 prose decoys (`Table 2 shows …`, `Chart 1 of …`) that must stay prose. >= 1 polluted text-layer caption (invisible text overlapping the caption line) |
  | G3 no text layer | >= 2 scanned pages: a generated page rasterised at 150 dpi, ±0.5° rotation and mild noise, embedded as the page's only image, GT transformed exactly. >= 2 figures and >= 1 table on them. >= 2 Read-area cases with no document hint (`area_pdf: doc-unresolved`): capture PNG plus OCR-like `--line-bounds`, GT in capture pixels |
  | G4 image tables | >= 3 tables rendered to PNG (Pillow + vendored font) on text pages, with GT body and GT row rects. Cases whose scripts read >= 3 rows each (`score: "table_row"`) |
  | Decoys | >= 12: logos, icons, decorative bands, bordered text boxes, a raster screenshot of prose (`text-image`), a full-bleed slide background |
  | Real | >= 2 docs under Phase 2 4.4's licence rules (CC BY 4.0 / CC0, unmodified, agent GT flagged), chosen for gap features with the census tool (e.g. IEEE `TABLE I` captions; a German or Spanish paper). Public may add `arx-2402.10153` page 3 as a gap doc id (same PDF, only page 3 scored) |
  | Totals | >= 40 kind-scored figure/table regions. Standard-caption regions <= 20 % of them |

- `validate_fixtures.py` gains these minima and a check that no gap-family PDF has a text layer on G3 pages.

### 12.3 The `kind` score (scorer, stdlib only)

On kind-scored pages of the gap family:
- **Matching:** greedy, per page and per kind k ∈ {figure, table}, at **IoU >= 0.5** (Phase 2 6.1's ordering). `boxes`
  already scores extents at 0.75; `kind` scores typing.
- **`kind.score`** = 100 × matched GT figure/table regions / all GT figure/table regions.
- **`kind.precision`** = 100 × matched predictions / all predicted figure/table blocks on kind-scored pages. Predictions
  on decoys, wrong-type predictions and predictions on nothing all count against it.
- **`kind.wrong_type`** = `{figure_as_table, table_as_figure}`: a prediction matched at 0.5 only to a GT of the other
  kind.
- **Breakdowns:** `kind.by_gap`, `kind.by_kind_src`, `kind.spurious_on_decoy`.
- **Area cases:** predictions are `area_kinds` rects in capture pixels, against capture-pixel GT, with the same rules.
- **`image_table_rows`:** per G4 row sentence, a hit is a `table_row` show whose `doc.target` IoU with the GT row rect is
  >= 0.5. `score = 100 × hits / n`.
- **Other blocks:** `sets.<set>.blockcls` = `{pages, candidates, by_stage, by_outcome, ms: {p50, p95}}` (`ms` is not
  determinism-covered). `boxes`, `lines`, `parts` and speed are computed on the gap family too, and reported only.

### 12.4 Modes

- **Gated mode** (default):
  - the Phase 2 scrub plus `SPEAKEASY_BLOCKCLS_VLM=off`, with the stage B model passed by `--figcls-model PATH`, like
    Phase 4's `--layout-model`;
  - no OCR (`SPEAKEASY_TRACKER` points nowhere), so G3 scans are typed uncaptioned and G4 rows are not scored here;
  - the harness asserts that the mock log has no `KIND_PROMPT` request. Otherwise it exits 3,
    `kind VLM ran in gated mode (case …)`;
  - `kind` is determinism-covered.
- **Replay mode, `--kind replay|proxy` and `--ocr replay|record`** (scored separately, never gated by `bench_gate`):
  - **Stage C:** the mock's `/api/chat` routes `KIND_PROMPT` requests to `kind_cassette.json` (public: committed;
    hidden: `<holdout>/gap/`, never committed). The key and stored content are as Phase 5 7.2: valid-letter logprobs
    plus one "other" entry, no text. `proxy` forwards to the real local Ollama (loopback only) and records.
  - **OCR:** the harness sets `SPEAKEASY_OCR_BIN` to a stub that answers from `ocr_cassette.json`, keyed by the PNG's
    sha256. `record` runs the real helper.
  - **Timing:** the harness waits for the kind job's done marker before the measured run, so answers never race the
    measured run.
  - **Output:** `sets.<set>.kind_vlm` (the same block as `kind`) and `image_table_rows`. A cassette miss → `"stale":
    true` and a stderr re-record line.

### 12.5 Live eval (`tools/kind_eval.py`, implementer-owned, never gated)

`python3 tools/kind_eval.py [--set public|hidden] [--record] [--sweep] [--ollama URL]`:
- runs replay mode as `proxy`;
- prints `kind_vlm`, stage C warm p50/p95 and the first-call load time;
- `--sweep` (public only) prints the section 13 sweep tables;
- Phase 5 7.3's preflight, temp homes only, hidden aggregates only.

### 12.6 Census tool (`tools/kind_census.py`, verifier-owned)

Read-only, aggregates only (12.1 item 2). It is the generalised form of the throwaway `/tmp/p6/count_unpaired.py`.

## 13. Threshold choice (once, then frozen)

- **`T_B`:** the smallest T in {0.50, 0.55, …, 0.95} at which the stage-B-accepted candidates of the **public** gap set
  (gated mode) reach kind precision >= 95 with >= 10 accepted. If none qualifies: T_B = 0.95.
- **`T_C`:** the smallest T in the same grid at which stage-C-accepted candidates of the public gap set (live sweep) reach
  kind precision >= 95 with >= 10 decisions (superseded by Amendment 2, A2.2: precision >= 90 with >= 5 accepted). If
  none qualifies: T_C = 0.95 and stage C ships default-off
  (`SPEAKEASY_BLOCKCLS_VLM=off`).
- Both values and the sweep tables go into **Amendment 2**. They are never re-chosen against hidden numbers. Any
  constant changed after a hidden run is reported with before/after hidden aggregates (Phase 4 rule).

## 14. Acceptance

1. **No regression on the existing families** (gated, against B5'):
   - `run_bench.py --set all --layout-model … --figcls-model …` then `bench_gate.py` gives `GATE PASS`;
   - no gated metric (`boxes.score`, `boxes.precision_50`, `lines.score`, `parts.score`, `parts.precision`) drops by
     more than 1.00 on either set;
   - first-highlight p50 and p95 < 300 ms;
   - `--check-determinism` exits 0;
   - `boxes.wrong_type` figure/table does not rise on either set.
2. **Gap family, gated mode, both sets:**
   - `kind.score >= 80`;
   - `kind.precision >= 90`;
   - each of `by_gap.uncaptioned`, `lexicon` and `no-text` >= 60;
   - `kind.wrong_type` total <= 2;
   - all prose decoys of G2 stay prose;
   - first-highlight p50/p95 < 300 ms;
   - `--check-determinism` exits 0;
   - the gain over `B6-pre` is reported.
3. **Gap family, replay mode:**
   - public: `kind_vlm.score >= kind.score − 1` and `kind_vlm.precision >= 95`;
   - hidden: `kind_vlm.precision >= 90` → stage C ships default-on; otherwise default-off, reported without re-tuning;
   - `image_table_rows.score >= 80` on both sets;
   - `replay_miss == 0` on public.
4. **Cost report** (no target beyond section 5's budgets):
   - stage B ms per candidate and per page (p50/p95);
   - candidates per page;
   - `parse-pdf` cold wall time per public gap doc, with and without the classifier model;
   - the document-ready check passes with no more retries than at B5';
   - from `kind_eval`: stage C calls per document, warm p50/p95 and the first-call load.
5. **Fallback runs** (verifier, temp homes):
   - `SPEAKEASY_BLOCKCLS=off` gives blocks identical to the post-Phase-7 tree on every public fixture;
   - classifier model missing and corrupted;
   - Ollama port closed, `MOCK_KIND=none`, a reply delayed 9 s, malformed JSON;
   - OCR helper missing;
   - an area run with `area_pdf: ok` (capture result discarded).

   Each exits 0, speaks every chunk, logs the section 10 outcome, creates no block from a routed candidate and leaves
   no temp PNG.
6. **Privacy:**
   - `privacy_audit.py` finds no document or OCR text in `blockcls_events`, `kind_job`, `area_kinds`,
     `doc_parse.kinds`, `logs/blockcls.jsonl` or the kind cassette, with `SPEAKEASY_LOG_TEXT` unset and 1;
   - `speakeasy logs --scrub` (Phase 7 A5.4) on fixture logs written with `SPEAKEASY_LOG_TEXT=1` after Phase 6 runs:
     every text-bearing key section 9.4 adds is removed or hashed and counted, a second run changes 0 records, and the
     `blockcls.jsonl*` scan reports 0;
   - no crop survives a run;
   - no request leaves loopback unless 6.4's three conditions hold.
7. **Unit tests** (implementer, new files):
   - the routing table B1–B5;
   - the letter merge and `p(figure) = p_A + p_B`;
   - the `letter_mass` 0.5 boundary;
   - `p_top == T` accepts;
   - lexicon positives (`Exhibit 3:`, `Abb. 2:`, `Tabelle 1.`, `TABLE IV`, `Fig. S2.`, `Figura 3 -`, `Chart 1:`,
     `Figure 3.2:`);
   - lexicon negatives (`Table 2 shows`, `Chart 1 of the plan`, `TABLE OF CONTENTS`, `Figure skating`, `Exhibit A`);
   - Roman numerals I–XL;
   - the in-box match for a polluted line;
   - the candidate filters and caps;
   - byte-identical crops;
   - `U`-labels absent from prompt label lists and unmatched by references;
   - `fig_id`s for a page holding captioned and added items (`p<page>:fig<n>` / `tab<n>` in reading order), and no
     `fig_id` for area-capture items;
   - added cues carry the design-token style values (section 9.4), and no hex literal appears in Phase 6 code;
   - the sidecar key changing with the model digest and the prompt hash.
8. **Regression suites** as Phase 5 9.7, plus Phase 7's acceptance checks (A7, B9, C5), on the post-Phase-7 results.
   The verifier widens key pins for section 9's keys only.
9. **Nothing under `~/.speakeasy` changes** during any bench, eval or census run.
10. **If a target is missed:**
    - report the best honest score per set and the public misses by cause: `detector-miss`, `filtered`,
      `classifier-wrong`, `routed-unanswered`, `vlm-wrong`, `lexicon-miss`, `ocr-miss`, `gt-convention`;
    - hidden aggregates only;
    - no fixture-specific constants and no reading of hidden per-case files;
    - say whether the PP-DocLayout_plus-L upgrade would address the `detector-miss` share.

## 15. Out of scope

- Paragraph, heading or line blocks on scanned pages (typing only).
- Formulas, algorithms and code listings as kinds.
- Markdown documents.
- Panel splitting of uncaptioned figures.
- CJK fixtures: the lexicon ships `图` / `図` / `表`, but they are unscored until a CJK font is vendored.
- Fine-tuning the VLM, and Visual Jev shared-prefix batching (one question per crop, so nothing to share).
- GPU / CoreML execution providers.
- Fetching models at run time.
- Using OpenRouter for anything but 6.4's case.

## Amendment 1: OCR output, the kind job's done signal, the kill switch (rulings, 2026-10-26)

Context: `main` at `ac83185` (Phase 7 shipped, `phase6-bench` merged). B5' and B6-pre are recorded there, and
`PARSE_VERSION` is 8 there, so Phase 6 takes **9**. The verifier's bench left three points open. These rulings bind
both the implementer and the harness. Where they are more precise than sections 6, 7, 10 or 14, they win.

### A1.1 OCR helper output (`speakeasy-track ocr`) and the Read-area lines

**Command:** `speakeasy-track ocr IN.png OUT.json`. Exactly two arguments; no flags, environment or config change the
result.

- **Vision settings:** recognition level `accurate`, `usesLanguageCorrection = true`,
  `automaticallyDetectsLanguage = true`. These are constants in `tracker.swift`.
- **Engine:** on macOS 26+, `RecognizeDocumentsRequest` (`engine: "vision-documents"`). Below macOS 26,
  `VNRecognizeTextRequest` (`engine: "vision-text"`).

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Success. `OUT.json` is written atomically (temp file + rename), even with zero lines |
| 2 | Usage error |
| 3 | `IN.png` is unreadable |
| 4 | Vision error |

On any non-zero code, `OUT.json` is not written; a stale file at that path is removed first. The caller treats every
non-zero code, a missing file and a file that fails the schema below as **OCR failed** (`ocr: unavailable`, section 10).
The helper never prints recognised text to stdout or stderr.

**`OUT.json`** (UTF-8, `ensure_ascii` off, keys in this order):

```json
{"version": 1, "engine": "vision-documents|vision-text|app-lines",
 "image_size": [w_px, h_px], "unit": "px", "origin": "top-left",
 "lines": [{"rect": [x0, y0, x1, y1], "text": "…", "conf": 0.97}],
 "tables": [{"rect": [x0, y0, x1, y1],
             "rows": [{"rect": [x0, y0, x1, y1], "cells": [{"rect": [x0, y0, x1, y1], "text": "…"}]}]}],
 "ms": 41}
```

- **Coordinates:** pixels of `IN.png` as decoded, origin top-left, y downwards, `x0 < x1`, `y0 < y1`, clipped to
  `image_size`, rounded to 0.1 px. The helper never knows page points.
- **Caller conversion:** the caller rendered the PNG and knows its page clip and zoom, so it converts each rect to page
  points as `clip origin + px / zoom`, rounded to 0.1 pt.
- **`lines`:** one entry per recognised text line, with whitespace runs collapsed and the text stripped. Empty lines
  are dropped.
  - Order: sorted by `y0`. Two lines whose vertical centres differ by less than half the smaller line height count
    as one row and are ordered by `x0`.
  - `conf` is Vision's top candidate confidence, in [0, 1], 2 decimals.
- **`tables`:** `[]` for `vision-text` and `app-lines`.
  - For `vision-documents`: rows top to bottom, cells left to right. A row rect is the union of its cells.
  - Section 8 uses `tables` first (`rows_src: ocr-table`), else `lines` grouped by `table_rows` (`ocr-lines`).
- **`ms`:** wall time of the request. It is the only field allowed to differ between two calls on the same bytes.
- **What the parse OCRs** (section 7.1): each region is rendered at the constant zoom `OCR_ZOOM` (in `blockcls.py`)
  plus a 4 pt margin. The render goes through the same deterministic path as the stage C crops, so the PNG bytes
  depend only on the PDF, the region, `OCR_ZOOM` and the pypdfium2 / Pillow versions.
- **Read-area runs** (section 7.2) call **no** helper. The backend writes the app's existing OCR lines in the same
  schema to a temp file `speakeasy_cap_ocr_<pid>.json` (deleted in `finally`) and passes it to `blockcls.py capture`:
  - `engine: "app-lines"`;
  - `image_size` = the capture PNG's pixel size;
  - each rect = the line's screen rect mapped to capture pixels (screen points × backing scale, minus the capture
    origin);
  - `text` = the line's OCR text;
  - `conf: null`, `tables: []`, `ms: 0`.

  Area cases therefore need no OCR cassette.
- **Privacy:** `text` exists only in `OUT.json`, the temp file and the parse cache (`ocr_rows`, OCR caption text). It
  never reaches a log, event or `bench.json`. `OUT.json` written by the parse goes to `TMPDIR` and is deleted in
  `finally`.

**Bench replay stub (`tests/audit/bench/ocr_stub`):**
- **Key:** `sha256` (hex) of the exact `IN.png` bytes. Nothing else enters the key: no path, no arguments, no
  environment.
- **Value:** the helper's `OUT.json` object as returned, plus its exit code.
- **Replay:** writes the stored object and exits with the stored code.
- **A key miss** exits 3 with no output. The app then follows the OCR-failed path; the harness counts a `replay_miss`
  and marks the cassette stale (exit 6).
- **Header:** the cassette header records the helper sha, the macOS version and the recording time. A change in
  pypdfium2 / Pillow that changes PNG bytes surfaces as replay misses, never as silent drift.

### A1.2 The kind job's done signal

The real signal is the **sidecar itself, written once, atomically, with a final state**. No quiet window is needed.

- **The parse output** (written before any job starts) gains `blockcls.kind_job`:
  - `"none"`: no routed candidates, or stage C off (`SPEAKEASY_BLOCKCLS_VLM=off`, `SPEAKEASY_GROUND=off`, classifier
    disabled). No job ever starts for this parse and no sidecar will appear.
  - `"queued"`: routed candidates exist and stage C is enabled.

  `blockcls.routed` = the number of routed candidates.
- **The sidecar** `cache/doc/<parse key>.kinds.json`:
  - It is written **only** by the job, **only** at its end (in `finally`), through a temp file in the same folder and
    a rename. It is never written incrementally, and a partial file is never visible.
  - Its top level:

    ```json
    {"version": 1, "parse_key": "<20-hex parse cache key>", "state": "done|partial|failed", "routed": n, "answered": n,
     "outcomes": {"accepted": n, ...}, "breaker": null, "error": null, "started_ts": 0.0, "done_ts": 0.0, "done_ms": n,
     "entries": [...]}
    ```

    - `breaker` is `null`, `"unavailable"` or `"no-logprobs"`.
    - `error` is `null` or an exception class name.
    - `entries` are the stage C decisions (section 6.1 key fields, outcome, kind, `p_top`, `p_margin`, rect).
  - **`state`:**
    - `done`: every routed candidate has a stage C outcome from a sent request;
    - `partial`: at least one candidate is `skipped` (budget, job deadline, breaker) and the job ended normally;
    - `failed`: the job raised before finishing. `error` is set; `entries` holds what was decided.
  - **`done_ms`** = job wall time from start to the rename.
- **Log record:** the same moment appends one record to `logs/blockcls.jsonl`, after the rename:
  `{"event": "kind_job_done", "parse_key", "state", "routed", "answered", "done_ms", "breaker"}`. It holds no text.
- **One job per parse key:** a lock file `cache/doc/<parse key>.kinds.lock` holds the job's pid while it runs.
  - A document job that finds `kind_job: "queued"`, no sidecar and a live lock starts nothing.
  - With a dead or missing lock, it starts the job.
  - A sidecar in any state is never re-run. The one exception: `failed` with `breaker: "unavailable"` may be re-run by
    a later document job at most once per 10 minutes (by `done_ts`). The harness never meets this case, because the
    measured run follows a warm-up whose job reached a final state.
- **Merging:** `load` merges `done` and `partial` sidecars, and nothing from `failed`.
- **Harness rule** (it replaces the current "sidecar unchanged for 1 s" heuristic, which is retired):
  1. after the warm-up run, read the parse cache entry;
  2. if `blockcls.kind_job == "none"`, there is nothing to wait for;
  3. otherwise poll for `<parse key>.kinds.json` and read it. A readable file with `state` in {done, partial, failed}
     ends the wait.
  4. The cap is **75 s** (job deadline 60 s plus 15 s for the start and a cold model load). At the cap it is a harness
     error, `kind job not finished (case …)`, exit 3.
  5. `failed` is reported as a harness error naming `error` (exit 3). `partial` is scored as it stands, with its
     `skipped` counts reported.
  6. After the measured run, the harness asserts that the sidecar's bytes are unchanged (no second job ran).
- **The measured run** loads the merged parse, so its stage C blocks are fixed before any chunk maps. Scoring stage C
  from the parse blocks (`kind_src: vlm`) and from the `blockcls.jsonl` records must give the same set.

### A1.3 The kill switch (`SPEAKEASY_BLOCKCLS=off`): what "identical" means

- **Name:** the switch is `SPEAKEASY_BLOCKCLS` (env or `config`), values `on` (default) and `off`.
  `SPEAKEASY_BLOCKCLS_VLM=off` only turns stage C off and is **not** the kill switch.
- **With `off`,** the parse output equals the pre-Phase-6 parse (post-Phase-7 `main`, `ac83185`) of the same file,
  except for:
  1. one added top-level key, exactly
     `"blockcls": {"status": "unavailable", "classifier_version": null, "reason": "disabled", "T_B": <const>, "T_C": <const>, "kind_job": "none", "routed": 0}`;
  2. `PARSE_VERSION` (9 against 8). It is not a field of the parse output; it appears only in the cache key, and so
     in the cache file name.
- **Nothing else is added, even as `null` or empty:** no `pages[].kinds`, no `blockcls_events`, no `captioned`,
  `printed_label`, `aliases`, `caption_src`, `kind_src`, `rows_src` or `ocr_rows`, no new `parse_summary` keys. The
  lexicon, candidates, stage B, the OCR helper and the kind job do not run, so no `U`-label, no lexicon caption and no
  OCR-derived block exists.
- **Runs log with `off`:** no `blockcls_events`, `doc_parse.kinds`, `kind_job` or `area_kinds`. The area capture
  classify does not start.
- **The K check** (verifier; acceptance 14.5 item 1): for every fixture of every public family (existing and gap):
  1. parse it on `ac83185` and on the Phase 6 commit with `SPEAKEASY_BLOCKCLS=off`, same interpreter, same models;
  2. remove from the Phase 6 output **only** the top-level `blockcls` key, after asserting it equals item 1 above;
  3. remove from **both** outputs only the timing fields the Phase 2 / Phase 4 determinism view already excludes: keys
     named `ms` and keys ending in `_ms`;
  4. compare the rest by **exact JSON equality**: same keys, same order-insensitive object members, same list order,
     same numbers as written, no tolerance.

  This covers every block's `kind`, `bbox`, `src`, `id`, every figure's `label`, `fig_id`, `bbox`, `caption_bbox`,
  `bbox_src`, `caption`, `panels`, `parts`, `labels`, `objects`, every page's lines and text, `layout`,
  `layout_events` and the summary.
  - Any other difference fails the check, and so does any key from the list above appearing in the `off` output.
  - The hidden families get the same check, reported as a pass/fail count only.

## Amendment 2: thresholds, row ties, implementer additions, known gaps (rulings, 2026-10-26)

**Context.** The implementer's uncommitted tree on `ac83185` reports, for the public gap set in gated mode:

| Measure | Result |
|---|---|
| `kind.score` | 82.61 (38/46) |
| `kind.precision` | 100 |
| `by_gap` | uncaptioned 64.29, lexicon 94.44, no-text 77.78, image-table 100, standard 100 |
| Errors | `wrong_type` 0; prose decoys kept 6/6 |
| Determinism | deterministic |
| Stage B cost | 13 / 16 ms p50 / p95 per candidate |
| Guardrails | B5' public unchanged; K check 17/17 identical; `PARSE_VERSION` 9 |
| `image_table_rows` (replay) | 72.73 (8/11) |

These rulings win over sections 6, 7, 8, 9, 13 and 14 where they differ.

### A2.1 `T_B` (frozen)

- **`T_B = 0.50`**, frozen.
- The public gated sweep gave 14 stage-B accepts at 100 % precision at every T from 0.50 to 0.95. The smallest
  qualifying T under section 13's rule (precision >= 95, >= 10 accepted) is 0.50.
- **Sweep table** (public gap set, gated):

  | T | Accepted | Precision |
  |---|---|---|
  | 0.50 … 0.95 (every grid step) | 14 | 100.0 |

- It is never re-chosen against hidden numbers.

### A2.2 `T_C` (frozen from the verifier's public live sweep)

- **Rule** (replaces section 13's `T_C` line; Phase 5 8.2 style, because the routed set is small): `T_C` = the
  smallest T in {0.40, 0.45, …, 0.95} at which the verifier's **public** live sweep (`tools/kind_eval.py --sweep`)
  gives stage-C-accepted kind precision >= 90 with >= 5 accepted.
- **Recording:** the verifier appends the chosen value and the full sweep table (T, accepted, correct, precision) to
  this amendment as **A2.2a**. Until then, the provisional `T_C = 0.95` stands.
- **No T qualifies** → `T_C = 0.95` and stage C ships **default-off** (`SPEAKEASY_BLOCKCLS_VLM` default `off`). This
  is acceptable: stage B plus the lexicon alone meet the kind target on public (82.61 >= 80, precision 100 >= 90).
- **Default-on** still also requires section 14 item 3:
  - public `kind_vlm.score >= kind.score − 1` and `kind_vlm.precision >= 95`;
  - hidden `kind_vlm.precision >= 90`.

  If either fails, stage C ships default-off and the number is reported without re-tuning.
- **Unchanged:** section 14 item 2 (gated `kind`, both sets) remains the phase gate; the hidden gated `kind` result is
  still required.

#### A2.2a `T_C` frozen: 0.95, stage C default-off (verifier's live public sweep, 2026-10-26)

- **Sweep result:** only **2** stage C answers fell on kind-scored pages. Both were `kind: text` with `p_top` 1.00:
  - 1 was correct;
  - 1 was a GT figure/table wrongly typed as text (no block; the region stays as Phase 4 left it).

  | T | Figure/table accepted | Correct | Precision |
  |---|---|---|---|
  | 0.40 … 0.95 (every grid step) | 0 | 0 | null |

- **Ruling:**
  - No T qualifies (A2.2 needs >= 5 accepted at precision >= 90), so **`T_C = 0.95`** (frozen).
  - Stage C ships **default-off** for Phase 6 (`SPEAKEASY_BLOCKCLS_VLM` default `off`). Section 14 item 3's default-on
    test is moot.
- **What still ships:** the stage C code, the kind job, its events, the done signal (A1.2, A2.5) and every section 10
  fallback. They are verified as usual:
  - the replay mode runs;
  - fallback runs with `SPEAKEASY_BLOCKCLS_VLM=local`;
  - privacy and unit tests.

  A user can opt in with `SPEAKEASY_BLOCKCLS_VLM=local` (or `auto`).
- **Small sample:** the public routed set is only 2 candidates. This is a small-sample decision. It is revisited, under
  the A2.2 rule on the public set only, once more gap documents exist (enough routed candidates for >= 5 figure/table
  decisions). It is never re-chosen against hidden numbers.

### A2.3 `image_table_rows` (72.73 vs 80): gated, and the header tie is in scope

- **Gated.** It is an acceptance target (section 14 item 3). It is not a `bench_gate.py` metric, but the phase does
  not pass below 80 on either set.
- **The tie belongs to Phase 6**, fixed for OCR-row tables only. Tables with `rows_src` `ocr-table` or `ocr-lines`:
  - When `match_row` finds the header row (`rows[0]`) and a later row with an **equal** best score, the later (data)
    row wins.
  - A header row is a row-band target only when it scores **strictly** higher than every data row.
  - Forward-only order, the header tint and every other `match_row` rule are unchanged.
- **PDF-text tables** (`table_rows` from PDF lines) keep the Phase 2 rule unchanged. The K check and B5' stay valid.
- **Constraints:**
  - No fixture-specific constants. The rule is the one sentence above.
  - Unit tests (new file): a tie goes to the data row; a strict header win stays the header; a PDF-text table is
    unchanged.
- **After the fix,** below 80 → report the ceiling with the misses by cause (`ocr-miss`, `row-grouping`, `match`,
  `gt-convention`), with no further tuning.

### A2.4 Implementer additions beyond the contract

- **`_pair_typed`: accepted, for lexicon captions only.**
  - A lexicon caption with no body of its detector kind may take an adjacent **unpaired** detector body box of
    another class when stage B types that box as the caption's kind.
  - Conditions:
    - `p_top >= T_B`;
    - not `textish`;
    - not `decoration`;
    - Phase 4 4.2's side, column, overlap and gap limits all hold.
  - The box is then not a section 3 candidate. Its `block_kind` record has `anchor: "lexicon-caption"` and
    `kind_src: lexicon`.
  - **Standard captions never use it** ("caption regex first, unchanged"; B5' and the K check depend on it).
- **Class-agnostic de-dup at IoU 0.5 in `filter_candidates`: accepted.** It is the candidate-side twin of Phase 4's
  `DUPLICATE_IOU`. The higher detector score is kept (ties: the earlier box in reading order). The dropped box is
  logged `skipped` (`duplicate`).
- **Container drop: accepted, with this definition.**
  - A candidate is dropped when **two or more** other candidates each have >= 90 % of their own area inside it. The
    inner boxes stay candidates.
  - The 90 % is a constant in `blockcls.py`.
  - The drop is logged `skipped` (`container`).
  - It never removes a captioned body or a Phase 5 panel.
- **`anchor: "lexicon-caption"`: accepted.** The `block_kind.anchor` enum (9.2) is now `uncaptioned |
  ambiguous-caption | lexicon-caption | no-text | area`. `skipped` reasons are `over-cap | budget | duplicate |
  container | area-no-vlm`.
  - `tests/audit/SCHEMA.md` and the `privacy_audit.py` pins take the new values as a pure pin update.

### A2.5 Known gaps

| Gap | Ruling |
|---|---|
| No macOS 26 `RecognizeDocumentsRequest` (the build SDK is 15.2) | **Known limitation, accepted.** The helper reports `engine: "vision-text"`, `tables: []` (A1.1), and section 8 uses `rows_src: ocr-lines`. Adding the macOS 26 engine is a follow-up once the build SDK has it; it requires re-recording both OCR cassettes |
| Stage C not run for area-capture candidates | **Known limitation, accepted.** Section 7.2's "may run" becomes "does not run in Phase 6". Routed area candidates get no cue (precision first), with `outcome: skipped` and reason `area-no-vlm`. Area cases are scored in gated mode, where stage C is off anyway |
| Stage C figures merged in-process get no `objects` / `parts` | **Known limitation while stage C ships default-off; required fix before it may ship default-on.** The fix: the kind job (doc venv) computes the Phase 5 hierarchy (`objects`, `parts`, `panels: []`) for each figure it accepts and stores it in the sidecar entry, so `load` merges it with the box. Until then a stage C figure is a figure box only: no picker scope (`part_picks.skipped["no-parts"]`) and no part ovals |
| A partial re-parse (`--sel`, then `--prev`) keeps the same parse key, so an existing sidecar blocks stage C for pages parsed later | **Required fix** (it corrupts the done signal for the app). The fix is below |

**The partial re-parse fix:**
- The sidecar gains `pages`: the sorted list of pages whose routed candidates the job covered.
- The parse sets `blockcls.kind_job: "queued"` when routed candidates exist on pages not yet in an existing
  sidecar's `pages`; otherwise `"none"`.
- A document job that starts on `"queued"` runs stage C **only for the uncovered pages**. At its end it rewrites the
  sidecar atomically as the union:
  - earlier entries are kept;
  - `pages` is the union of the old and new pages;
  - `state` is the new job's state;
  - `done_ms` / `done_ts` are the new job's.
- A1.2's harness rule becomes: the wait ends when the sidecar's `pages` ⊇ the routed pages of the current parse **and**
  its `state` is final.
- The bench parses every page with no `--sel`, so its numbers are unaffected. A unit test covers the two-step parse.

### A2.6 OCR helper resolution (one rule; reconciles section 12.4 and A1.1)

- **Path:** first match wins.
  1. `SPEAKEASY_OCR_BIN` (tests, bench stub). When it is set but is not an executable file, OCR is **unavailable**,
     with no fall-through.
  2. Otherwise the tracker binary the backend already resolves: `SPEAKEASY_TRACKER` when set, else
     `$SPEAKEASY_HOME/bin/speakeasy-track`.
  3. Not an executable file → OCR unavailable (`ocr: unavailable`).
- **Invocation:** always `<path> ocr IN.png OUT.json`, with the output format and exit codes of A1.1. The bench stub
  accepts the same argument list.
- **Bench:**
  - gated mode leaves `SPEAKEASY_OCR_BIN` unset and points `SPEAKEASY_TRACKER` at a nonexistent path, so OCR is
    unavailable;
  - `--ocr replay|record` sets `SPEAKEASY_OCR_BIN` to `tests/audit/bench/ocr_stub`.
- **Read-area runs** call no helper (A1.1).

## Amendment 3: one generalization round (second detector on gap pages, structural fallback), 2026-10-26

**Context** (non-gated indicative runs on the implementer's tree):

| Set | `kind.score` | `kind.precision` | Uncaptioned | No-text | Lexicon | Image-table |
|---|---|---|---|---|---|---|
| Public gap | 82.61 | 100 | 64.29 | 77.78 | 94.44 | 100 |
| Hidden gap | **68.09** | **88.89** | 42.86 (6/14) | 33.33 (3/9) | 94.74 | 100 |

- Hidden stage C routed 0 candidates.
- The implementer's diagnosis: most misses are **detector misses**. PP-DocLayout-S never proposes the box, so stage B
  never sees it.
- The user chose **one** generalization round. This amendment is that round. It wins over sections 3, 7, 12 and 14
  where they differ.

### A3.1 Prior art (checked 2026-10-26; measured on the PUBLIC gap set only)

Measurement: page renders at zoom 1, 640 × 640 bilinear, onnxruntime 1.31 CPU, 4 intra-op threads. The score is the
recall of the 42 GT figure/table page regions of the public gap set (area cases excluded) at IoU >= 0.5, before
candidate filters and stage B. Scripts: `/tmp/p6/det_compare*.py`.

| Model | Source / licence | Size | Same-kind recall | Any body | Unmatched body boxes | CPU per page (p50 / max) | Load |
|---|---|---|---|---|---|---|---|
| PP-DocLayout-S (today) | Apache-2.0 (Phase 4) | 4.9 MB | 34/42 | 36/42 | 1 | 22 / 27 ms | — |
| **Docling `docling-layout-heron-onnx`** (RT-DETRv2) | **official** (`docling-project`), Apache-2.0 | 171.2 MB | **42/42** | 42/42 | 7 | 388 / 448 ms | 128 ms |
| Docling egret-medium, community ONNX `ningpp/docling-layout-egret-medium-ONNX` (D-FINE) | single-maintainer export, card says Apache-2.0 (base model Apache-2.0) | 78.5 MB | 41/42 | 41/42 | 6 | 193 / 210 ms | 515 ms |
| PP-DocLayout_plus-L (official ONNX) / PP-DocLayout-M | Apache-2.0 | 130 MB / smaller | not measured | — | — | — | — |

- Repeated runs on the same input are bit-identical for heron and egret.
- egret's input must **not** be ImageNet-normalised: with normalisation it found 6/42. Its
  `preprocessor_config.json` has `do_normalize: false`.

**Decision: build on top, using the official heron ONNX as a second detector, on gap pages only (A3.2).**
- Reasons:
  - it is the only Docling layout model with official ONNXRuntime support (Docling's model catalog), it is
    Docling-maintained and widely used (about 140 k downloads);
  - it had the best recall: 42/42 public, against 34/42 for PP-S;
  - its cost is paid only on routed pages.
- **Rejected as primary:**
  - egret-medium community export: provenance (one maintainer, 53 downloads, not Docling's), although it is half the
    size and twice as fast. It stays the documented alternative if the user rejects heron's bundle size. The same
    adapter takes it, with its own pin and input contract (below);
  - PP-DocLayout_plus-L / -M: the same model family whose misses this round targets, and not measured.

**Pin:**

| Item | Value |
|---|---|
| File | `docling_layout_heron.onnx` |
| Source | `https://huggingface.co/docling-project/docling-layout-heron-onnx/resolve/40bde044036bb181c130ddf6c51792187268748f/model.onnx` |
| Size | 171,220,471 bytes |
| sha256 | `59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a` |
| `detector2_version` | `docling-heron/59c81a3a2923` |

**Input/output contract (heron):**
- Inputs:
  - `images`: `uint8 [1,3,640,640]`, RGB, the page render bilinear-resized to 640 × 640, with no rescale and no
    normalisation;
  - `orig_target_sizes`: `int64 [[W, H]]`, the render size in pixels.
- Outputs, post-processed inside the graph:
  - `labels [1,300]`;
  - `boxes [1,300,4]`, `x0, y0, x1, y1` in render pixels;
  - `scores [1,300]`.
- Classes (`config.json` `id2label`): `caption`, `footnote`, `formula`, `list_item`, `page_footer`, `page_header`,
  `picture`, `section_header`, `table`, `text`, `title`, `document_index`, `code`, `checkbox_selected`,
  `checkbox_unselected`, `form`, `key_value_region`.
- Only `picture` (→ figure family) and `table` (→ table family) seed candidates. The text-like classes (`text`,
  `list_item`, `section_header`, `title`, `caption`, `footnote`) count as text evidence (A3.3).
- `HERON_SCORE_MIN` is one constant in `[0.3, 0.6]`, chosen under A3.5.

**egret alternative (only if heron is rejected):**
- Pin: revision `06b27d85c5a004eb024ca82f78e094fb7bf47e47`, 78,481,216 bytes, sha256
  `e64ebaa061f6f8b4c049fa109590876532a67b77a4f6c00508a7def24844757b`.
- Input: `pixel_values float32 [1,3,640,640]` = RGB / 255, not normalised.
- Outputs: `logits [1,Q,17]` (sigmoid, arg-max class) and `pred_boxes [1,Q,4]` (normalised cx, cy, w, h). Labels as
  heron's.

### A3.2 Routing: heron runs only on gap pages, defined from page structure

The router runs in the parse on every detected page, after PP-DocLayout-S. It uses the page's own text layer,
geometry and PP-S boxes, never doc ids, file names or page numbers.

A page is a **gap page** when **any** of these holds:

| Rule | Condition |
|---|---|
| **R1 no or near-empty text layer** | fewer than 3 text lines, **or** the text-line boxes cover < 2 % of the page area |
| **R2 slide-like** | landscape (`W / H >= 1.2`) **and** fewer than 25 text lines |
| **R3 unexplained graphics** | the union of raster images and clustered vector drawings is at least 8 % of the page area, counting only items with both sides >= 24 pt that cover < 90 % of the page and have < 50 % of their area inside any PP-S body box, PP-S paragraph box or captioned body |

- On a gap page, heron runs once.
  - Its `picture` / `table` boxes join section 3 as candidates (`det: heron`) when they overlap no PP-S body box at
    IoU >= 0.5 and no captioned body or paragraph block by more than 30 %.
  - Section 3's filters, A2.4's de-dup and container rules, the caps and stage B then apply unchanged.
- PP-S stays the only source of captions, pairing, paragraphs and headings, on every page.
- Heron never changes a block that PP-S or a caption produced. It can only **add** candidates.
- **Never on normal papers:** acceptance A3.6 requires that the router fires on **0** pages of the existing families
  (public and hidden). If R3 fires on any existing-family page during public development, R3's constants are
  tightened on public data. Hidden counts are reported as a number only.
- **Caps:**
  - at most 8 routed pages per parse, in page order; the rest are `layout2.src: "skipped"` (`over-cap`);
  - with `--sel`, routing applies only to the pages the parse detects.

### A3.3 Structural fallback for no-text pages

On an **R1 page** where PP-S and heron together leave no body candidate over a region with ink, candidate regions come
from page structure:

1. Render the page at zoom 1 to grayscale. **Ink** = pixels at least 48 levels darker than the page's median
   (background) value. Dilate by 6 px.
2. **Remove text evidence**, padded by 2 pt:
   - the OCR line boxes when the OCR helper is available (A2.6);
   - otherwise, the text-like detector boxes of PP-S (`text`, `paragraph_title`, `doc_title`, `abstract`, `content`,
     `reference`, `footnote`, `aside_text`) and of heron (A3.1).
3. Connected components of the remaining ink are **candidates** when:
   - both sides are >= 36 pt;
   - the area is >= 2 % of the page;
   - ink density inside is >= 3 %;
   - they overlap no existing candidate or body at IoU >= 0.3.

   At most 4 per page, largest first.
4. **Stage B types them** as in section 5, and routing B1–B4 applies. Stage C is off (A2.2a), so only B5 accepts. The
   `block_kind` record has `anchor: "no-text-structural"`, `det_cls: "ink"` and `det_score: null`.
5. **Never on a page with a text layer:** R1 must hold. Measured risk: stage B read 20 of 44 text crops as `table`.
   That is why text evidence is removed first, and why the fallback is limited to R1 pages.

The ink threshold, dilation, size, density and cap are constants in `blockcls.py`, chosen under A3.5.

### A3.4 Install, fallback, events, budget

- **Install and bundle:** exactly as section 2 / Phase 4 section 1, for the A3.1 pin:
  - install path `$SPEAKEASY_HOME/models/docling_layout_heron.onnx`;
  - override `SPEAKEASY_LAYOUT2_MODEL` (set but missing → `model-missing`, no fall-through);
  - `bundle_python.sh` fails the build on a mismatch;
  - `app/NOTICE` and `README.md` name docling-layout-heron (docling-project, Apache-2.0);
  - the bundle grows by about 171 MB, which the user should know at the release step.
- **Kill switches:** `SPEAKEASY_LAYOUT2=off` turns off heron and the structural fallback. `SPEAKEASY_BLOCKCLS=off`
  turns off all of Phase 6, these included, so the K check (A1.3) is unchanged.
- **Fallback reasons** (`layout2.reason`, Phase 4 enum): `disabled`, `no-onnxruntime`, `model-missing`,
  `model-mismatch`, `load-failed`, plus `inference-failed` per page.
  - Any of them → **today's path**: PP-S candidates only. The structural fallback still runs on R1 pages, because it
    needs no model.
  - One stderr line per parse: `layout2: detector unavailable (<reason>)`.
- **Events and fields** (no text, whatever `SPEAKEASY_LOG_TEXT` is):
  - `pages[].layout2`: `{"routed": bool, "by": ["R1"|"R2"|"R3", ...], "src": "heron"|"skipped"|"unavailable"|null,
    "ms": int, "boxes": {"figure": n, "table": n}, "added": n, "structural": n}`;
  - `layout2_page`: `{"event", "page", "by", "ms", "added", "structural", "detector_version"}`, one per routed page,
    in `blockcls_events`;
  - `layout2_unavailable`: `{"event", "reason", "error", "detector_version", "page"}`;
  - runs log `doc_parse.layout2` = the matched page's `pages[].layout2`;
  - bench `sets.<set>.layout2` = `{"pages", "routed", "by_rule", "ms": {"p50", "p95"}}`. `ms` is not
    determinism-covered.

  The A2.4 enums gain `anchor: "no-text-structural"` and `det_cls: "ink"`. These keys hold no text, so the Phase 7
  scrub learns nothing new (9.4). SCHEMA and the privacy pins are updated as a pure pin update.
- **Per-page budget** (CPU, 4 threads; reported, and gated on public):

  | Item | Budget | Measured |
  |---|---|---|
  | Router, every detected page | <= 2 ms p95 | — |
  | Heron, per routed page | <= 600 ms p95 | 388 ms p50 / 448 ms max |
  | Heron session load, once per parse with a routed page | <= 300 ms | 128 ms |
  | Structural fallback, per R1 page | <= 50 ms p95 | — |
  | Unrouted pages | zero heron cost | — |

  The document-ready check must still pass with no more retries than at B5'.

### A3.5 Anti-overfitting (binding for this round)

- **Where constants may be chosen.** Every new or changed constant may be chosen **only** on the public gap set or on
  **generated variants** of the public gap specs. Constants include R1–R3, `HERON_SCORE_MIN`, the fallback constants,
  the caps, and any filter retouched in this round.
  - Variants are new spec files made with the verifier's generator: different seeds, texts and layout parameters,
    never the hidden specs. They live under `tests/audit/bench/specs-gap-variants/` (verifier-owned).
  - Results on the variants are reported next to public.
- **No identifying constants:** no doc ids, file names, page numbers or fixture-specific constants in code.
- **Hidden set:**
  - read as aggregates only (`by_gap`, miss-cause counts);
  - **run once, at the gate of this round**. The first hidden result decides shipping (A3.6);
  - no constant is changed in response to it;
  - any later change is reported with before/after hidden aggregates and does not re-open the ship decision.
- **One round:** there is no Amendment 4 tuning loop on the gap family. Further gains wait for more gap documents
  (A2.2a).

### A3.6 Acceptance for this round

1. **Target:** hidden gated `kind.score >= 80` **and** `kind.precision >= 90`. Public stays at `kind.score >= 81.61`
   (today's 82.61 − 1) with `kind.precision >= 90`.
2. **Ship rule if the target is missed:**
   - ship when hidden `kind.score >= 73.09` (68.09 + 5) with hidden `kind.precision >= 90`, items 3–6 holding;
   - record the ceiling: the best honest hidden and public scores, `by_gap`, and public misses by cause
     (`detector-miss` by both detectors, `router-not-fired`, `filtered`, `structural-miss`, `classifier-wrong`,
     `ocr-miss`, `gt-convention`) with hidden cause counts only;
   - otherwise ship with `SPEAKEASY_LAYOUT2` default `off` and report.
3. **The existing families are byte-equal:**
   - `run_bench.py --set all` on the existing families gives `boxes`, `lines`, `parts` and their `by_kind` /
     `by_subset` blocks **exactly equal** to B5' (not "within 1");
   - first-highlight p50/p95 < 300 ms;
   - `bench_gate.py` PASS;
   - `sets.*.layout2.routed == 0` on both sets.
4. **K check unchanged:** A1.3 still passes 17/17 (or its current count) with `SPEAKEASY_BLOCKCLS=off`.
5. **Gap-family guardrails:**
   - `--check-determinism` exits 0;
   - prose decoys all kept;
   - `kind.wrong_type` total <= 2;
   - `image_table_rows >= 80` (A2.3);
   - A3.4's per-page budget met on public;
   - first-highlight p50/p95 < 300 ms.
6. **Fallback and privacy:**
   - each A3.4 reason, checked as in Phase 4 (env off, missing override, corrupted copy, a non-ONNX file with the
     patched hash, one failing page), gives the reason event, `layout2.src` and blocks identical to a
     `SPEAKEASY_LAYOUT2=off` run;
   - `privacy_audit.py` finds no text in the new keys;
   - unit tests in new files cover R1–R3 vectors (a portrait paper page is never routed), the structural fallback
     removing text evidence, and heron's class mapping.

## Amendment 4: rulings after implementer round 5 (heron), 2026-10-26

**Context.** The implementer's tree is stable. Public gap set, gated mode, with heron:

| Measure | Result |
|---|---|
| `kind.score` / `kind.precision` | 91.30 (42/46) / 100 |
| Uncaptioned / no-text | 78.57 / 88.89 |
| Heron missing (`model-missing`) | 86.96 |
| `image_table_rows` | 100 |
| B5' main bench | equal; heron routes 0/29 main pages |
| K check | 17/17 |
| Heron cost | 396–458 ms p50 per routed page; load about 210 ms, sha check included |
| Router / structural fallback | 0.21 ms max / 5–14 ms per page |

These rulings win over Amendment 3 where they differ. The hidden gap set is still run **once**, at the gate (A3.5).

### A4.1 Structural fallback vs the `SPEAKEASY_LAYOUT2` switch (A3.4 vs A3.6 item 6)

Adopted, with one narrowing.
- **A3.4 wins:** the structural fallback (A3.3) still runs when heron is unavailable (`no-onnxruntime`,
  `model-missing`, `model-mismatch`, `load-failed`, and on an `inference-failed` page). It needs no model.
- **`SPEAKEASY_LAYOUT2=off` is the kill switch** for this round. It disables **both** heron and the structural
  fallback. `SPEAKEASY_BLOCKCLS=off` still disables all of Phase 6 (A1.3, K check unchanged).
- **A3.6 item 6 restated:** for each heron unavailable reason, the parse's blocks equal a `SPEAKEASY_LAYOUT2=off` run,
  except for blocks whose `block_kind.anchor` is `no-text-structural`, on **R1 pages only**. The reason event,
  `layout2.src: "unavailable"` and the stderr line are required as before.
- **Narrowing:** Chat's wording "routed no-text/slide pages" is narrowed to R1 pages. A3.3 limits the fallback to R1
  (no or near-empty text layer), and an R2-only slide page has a text layer, where stage B misreads text crops.

### A4.2 Structural fallback text evidence: detector boxes, not OCR

Accepted.
- A3.3 step 2 uses **only** the text-like detector boxes of PP-S and heron (when heron ran), **never** OCR, in every
  mode.
- Reason: a full-page OCR measured 2.8–3.6 s per page. That breaks document readiness (Read-area's 4 s wait,
  benchmark 5.2) and A3.4's 50 ms fallback budget.
- OCR text evidence stays a later option. If it is ever added, it runs in the kind job (background), never in the
  parse.
- A3.3's other constants are unchanged.

### A4.3 Round-4 additions (lexicon captions only)

**Accepted**, each under these conditions:
- it acts only on lexicon captions (`caption_src` `lexicon` / `lexicon+in-box`);
- it never changes a standard caption or its body, so B5' and the K check depend on none of them;
- it is off with `SPEAKEASY_BLOCKCLS=off`;
- its constants are named in code and were chosen on the public gap set or its generated variants (A3.5).

| Addition | Accepted behaviour |
|---|---|
| `WEAK_CAPTION_MIN = 0.2` (`layoutdet.py`) | Detector caption boxes scoring between 0.2 and `SCORE_MIN` are kept aside **only** as in-box text evidence for 4.1's polluted-line match and evidence rule. They never create a block, never extend a caption block (Phase 4 4.1) and never pair a body |
| `_widen_spanning` | On a two-column page, a lexicon caption crossing the midline keeps a full-width body (the body is widened to the caption's span when it is narrower than 75 % of it) |
| `_kind_by_layout` / `_cell_rows` | Amends 4.2's kind order for an ambiguous noun: stage B family → detector class family → **layout cue** → `figure`. The layout cue is, in order: >= 2 rows of multi-fragment cell lines adjacent to the caption → `table`; else graphics within 60 pt above → `figure`; else graphics within 60 pt below → `table`. `kind_src: lexicon` |
| Heuristic half rule | On a page where Phase 2's two-column rule says one column, a lexicon caption lying in one half with text beside it in the other half keeps its body in its own half |
| Caption-under-table rule | A lexicon **table** caption with cell rows above it and none below it takes the body above it (tables captioned underneath) |
| `_split_shared` | Two side-by-side captions over one detector box: a lexicon caption's body that reaches under the other caption is cut at the rightmost whitespace gutter between the two captions' left edges. The other part goes to the other caption only when that is a lexicon caption without a body |
| `_pair_typed` | As A2.4 |

- **Unit tests** (new files): one positive and one standard-caption-unchanged vector per row.

### A4.4 Harness work (verifier-owned)

Adopted.
- **`run_bench.py --layout2-model PATH`** (and the caller's `SPEAKEASY_LAYOUT2_MODEL`):
  - it passes the path into the scrubbed environment, like `--layout-model` / `--figcls-model`;
  - without it the temp home has no heron, so that run measures the `model-missing` fallback;
  - `versions` gains `layout2_model` (the `detector2_version`, or `null`);
  - a run with `--layout2-model` whose parse reports `layout2.reason` other than null on a routed page exits 5
    (coverage), like the classifier check.
- **`sets.<set>.layout2`** on every family: `{"pages", "routed", "by_rule": {"R1", "R2", "R3"}, "src": {...},
  "reasons": {...}, "structural": n, "ms": {"p50", "p95"}}`.
  - `pages`, `routed`, `by_rule`, `src`, `reasons` and `structural` are determinism-covered; `ms` is not.
  - On the existing families, `routed == 0` is asserted (A3.6 item 3).
- **`score.blockcls_summary`** counts `layout2_page` records (pages, `added`, `structural`) and `layout2_unavailable`
  records (by `reason`) from `blockcls_events`, excluding `ms` from the determinism view. A `test_score.py` case pins
  it.
- **The gate run** uses `--figcls-model` and `--layout2-model`. The `model-missing` run (public 86.96) is reported as
  the fallback figure.

### A4.5 Bundle size (recorded)

- The bundle grows by about 171 MB with heron (A3.1). **The user accepted this size for now** (2026-10-26).
- `README.md` and the release notes state the size.
- The egret-medium community export (78.5 MB, pinned in A3.1) stays the documented swap behind the same adapter, if
  size becomes a problem. Swapping needs public re-measurement under A3.5 and does not re-open the hidden run.

## Amendment 5: main-bench guarantee, `picture`, absorption, layout2 fields (Chat's rulings, 2026-10-26)

These rulings win over Amendments 3 and 4 where they differ.

### A5.1 What blocks shipping on the existing families (restates A3.6 item 3)

- **Ship blocker:** the existing-family (main bench) scores are **exactly equal** to B5' on **public and hidden**:
  - the `boxes`, `lines` and `parts` blocks, with their `by_kind` / `by_subset` sub-blocks;
  - `bench_gate.py` PASS;
  - first-highlight p50/p95 < 300 ms.
- **The router firing on an existing-family page is reported, not blocking, as long as the scores stay equal.**
  - Report: per set, the count of routed pages, the rule(s) that fired (`by_rule`) and heron `ms` (p50/max). Hidden
    gives aggregates only.
  - It is recorded as a known item: heron is add-only (A3.2), and its cost stays within A3.4's 600 ms p95 per routed
    page.
  - `sets.*.layout2.routed == 0` on the existing families is therefore no longer asserted. The A4.4 harness reports
    it instead.
- **Rationale:** the verifier's partial run saw the router fire on 1 hidden main-family page.
  - The implementer is **not** told which page: the hidden set stays blind.
  - R1–R3 are **not** re-tuned against hidden (A3.5).
  - Public main-family routing stays at 0/29, and any public firing is fixed on public data.

### A5.2 `det_cls` gains `picture`

- The `block_kind.det_cls` enum is now `image | chart | table | picture | ink`.
- `picture` is heron's native class. It maps to the **figure** family, like `image` / `chart`, in routing rule B3
  (section 5) and everywhere a detector family is used (4.2, A4.3). Heron's `table` keeps `det_cls: table`.
- `tests/audit/SCHEMA.md` and the privacy pins take the value as a pure pin update.

### A5.3 Absorption of orphan-line paragraphs by new bodies

- When a heron or structural body is accepted as a figure/table block, any **orphan-line paragraph** (`src: "lines"`,
  Phase 4 4.6) whose bbox centre lies inside that body is **removed** from the paragraphs. Its lines become lines inside
  a figure/table body.
  - This is Phase 4 4.6's existing rule (lines inside a figure or table body join no paragraph), applied to the new
    body.
  - Section 5's rule covers the pending case: lines of a routed, not-yet-accepted candidate join no paragraph.
  - Chat's "per section 5" is read as these two rules together.
- **How to read the earlier wording.** A3.2's "heron never changes a block" and A4.1's "blocks equal except
  structural" are read as follows:
  - no block or figure produced by PP-S, by a caption or by a standard caption changes: geometry, kind, label,
    `fig_id`, parts and paragraphs from detector boxes (`src: "detector"`) all stay as they were;
  - the only permitted side effect of an added body is absorbing orphan-line paragraphs whose centre lies inside it;
  - paragraphs from detector boxes are never absorbed. A new body overlapping one by more than 30 % is not a candidate
    (A3.2).
- **A4.1's comparison** (heron unavailable vs `SPEAKEASY_LAYOUT2=off`) allows two kinds of difference:
  - structural-fallback blocks on R1 pages;
  - the orphan-line paragraphs those blocks absorb.

### A5.4 `layout2` fields the suite already tolerates become spec

**Top-level `layout2` object** (parse output, every PDF parse while Phase 6 is on):

```json
{"status": "ok|idle|unavailable", "detector_version": "docling-heron/59c81a3a2923"|null, "reason": <enum>|null}
```

| `status` | When | `detector_version` | `reason` |
|---|---|---|---|
| `idle` | No page of this parse was routed, so heron was never loaded or checked | `null` | `null` |
| `ok` | At least one page routed and heron ran | set | `null` |
| `unavailable` | At least one page routed and heron could not run (A3.4 reasons) | `null` | set |

- **`SPEAKEASY_LAYOUT2=off`:**
  - with nothing routed: `idle`, `reason: null`, not `disabled`. The router still evaluates pages, but nothing needed
    heron;
  - with any page routed: `unavailable`, `reason: "disabled"`.
- **`SPEAKEASY_BLOCKCLS=off`:** the top-level `layout2` key is **absent**, as are all other Phase 6 keys. A1.3's list
  gains `layout2` and `pages[].layout2`, so the K check is unchanged.

**`pages[].layout2`** (A3.4):
- the key `reason` is **optional**: it is present only when `src` is `unavailable` or `skipped` (`over-cap`);
- consumers (scorer, `doc_parse.layout2`, tests) treat a missing `reason` as `null`;
- all other A3.4 fields are unchanged.

## Final audit (2026-10-26)

**As shipped (f2126a9):** `SPEAKEASY_BLOCKCLS` on, the classifier and the lexicon on; heron on (`SPEAKEASY_LAYOUT2` on)
under A3.6 item 2, with the hidden gap set at the recorded ceiling; stage C off (`SPEAKEASY_BLOCKCLS_VLM=off`, A2.2). Public
gap gate: `kind.score` 91.30, precision 100, wrong_type 0, decoys 6/6; the existing families equal B5'.

- Gap baselines are labelled by classifier state and commit (`B6@f2126a9`, `B6-pre@<sha>` with the classifier off).
- The blocks suite's B section replays with `--layout2-model` (A4.4) and accepts the A3.6 item 2 ceiling for the hidden
  gap gate.
- Open: on `arx-2402.10153` page 3 with no layout model, the lexicon caption `TABLE I`'s caption block lists a 0.9 pt glyph
  that lies 1 pt outside its `caption_bbox` (`tests/audit/logging`
  `D_pub_arx-2402.10153_caption_block_lines_inside_caption_bbox`).
