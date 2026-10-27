# Phase 4 contract: layout detector for page blocks

Scope: a layout model that finds paragraph, caption, figure and table regions on each parsed PDF page, so that
`pages[].blocks` and `figures[].bbox` match the page as a reader sees it. Goal: raise the benchmark `boxes` score
(`docs/contracts/phase2-benchmark.md` section 6.1) without lowering any other gated metric. This file describes
**observable behaviour**; it does not prescribe code. Terms are as in `phase1-readability.md`, `phase2-logging.md` and
`phase2-benchmark.md`. *Parse* = one `docmap.py parse-pdf` invocation (the doc-venv subprocess). *Detected page* = a page
this parse gives figure parsing (`figs_done: true`) and does not copy from `--prev`.

Prior art (decided before this contract):
- **Use as-is:** PP-DocLayout-S (PaddlePaddle, Apache-2.0), ONNX export `stefanj0/PP-DocLayout-S-ONNX`, run with
  onnxruntime (MIT) on CPU. It is small (4.9 MB), fast (about 24-35 ms per page on CPU, about 80 ms one-off import + load)
  and has caption classes (`figure_title`, `table_title`) separate from bodies.
- **Build on top:** only the Speakeasy glue: class → block-kind mapping, snapping to PDF geometry, caption pairing, fallback
  and events.
- **Upgrade path**, only if the target is missed and the remaining misses are detector misses rather than glue misses:
  Docling `egret-medium` (Apache-2.0, about 78 MB ONNX) behind the same adapter.
- **Rejected:** DocLayout-YOLO (AGPL-3.0).

## 0. Where the code goes (merge rule)

Phase 3 (sentence IDs) is being built in parallel on `main` and may touch `src/docmap.py`, `src/focus.py`,
`src/document.py` and `src/speakeasy.py`. To keep the merge small:

- **New module `src/layoutdet.py`** holds everything specific to Phase 4: model lookup and verification, the onnxruntime
  session, preprocessing, class mapping, caption pairing, snapping and the event records. Its name shadows no stdlib
  module or installed package. Like `docmap` and `pdfpage`, it is imported by sibling path inside the doc venv. It
  imports `numpy` and `onnxruntime` lazily, so importing it never fails.
- **`src/docmap.py`:** limited to `PARSE_VERSION`, the figure/caption/paragraph assembly inside `parse_pdf` /
  `page_blocks` / `_split_overlaps` (one call into `layoutdet` per detected page), `parse_summary`, and copying events
  out of the parse output in `load`. Do not reformat or move other functions.
- **`src/pdfpage.py`:** at most a page-raster helper for the detector input.
- **`src/focus.py`:** at most one call site in `_sel_region` (section 4.5), committed last and on its own so it can be
  rebased onto Phase 3 or dropped.
- **`src/document.py`:** at most the lines that copy the layout summary and events into the runs record (section 6).
- **Installers and notices:** `src/install.sh`, `app/bundle_python.sh`, `app/NOTICE`, `README.md` (licence line).
- **Docs:** `docs/architecture.md` (module-map row for `layoutdet.py`), `tests/audit/SCHEMA.md` (new runs keys).
- **`PARSE_VERSION`:** goes from 5 to 6. If Phase 3 also bumps it, whichever branch merges second takes
  `max(both) + 1`.

## 1. Model install

| Item | Value |
|---|---|
| File | `pp_doclayout_s.onnx` |
| Source | `https://huggingface.co/stefanj0/PP-DocLayout-S-ONNX/resolve/7e851b9e5ad66613d7318a3213ab3336ebe0a514/pp_doclayout_s.onnx` (pinned revision, never `main`) |
| Size | 4,917,852 bytes |
| sha256 | `33688dbee1c23e34b81777e97cb428eb40f24b242c02b5f623484959e830aec8` |
| Install path | `$SPEAKEASY_HOME/models/pp_doclayout_s.onnx` (`$SPEAKEASY_HOME` defaults to `~/.speakeasy`) |
| `detector_version` | `pp-doclayout-s/33688dbee1c2` (model name + first 12 hex of the sha256) |

- **`src/install.sh`:**
  - Installs `onnxruntime` and `numpy` into the doc venv (`$SPEAKEASY_HOME/tts-venv`), next to `pypdfium2` and `pillow`.
  - Fetches the model. A file already at the install path with the right sha256 is kept: no network access.
  - Otherwise it downloads to `<install path>.part`, checks size and sha256, and renames into place atomically.
  - **Checksum or size mismatch:** deletes the `.part` file, prints
    `ERROR: layout model checksum mismatch (expected <sha>, got <sha>); PDF layout detection off`, finishes the rest of
    the install, and exits non-zero.
  - **Download failure** (offline, HTTP error): prints a `WARNING: … PDF layout detection off` line naming the cause,
    and the install still succeeds.
  - Re-running is safe. A bad file never stays at the install path.
- **`app/bundle_python.sh`:**
  - Installs `onnxruntime` and `numpy` into the bundled Python.
  - Fetches and verifies the model into `$SPEAKEASY_HOME/models/` with the same rules. That copy is a download cache
    shared by later builds.
  - Copies the verified file into `Contents/Resources/models/` before `codesign`, so a shipped app does not depend on
    the build machine's home.
  - Any download failure or mismatch **fails the bundle build** (non-zero exit, message as above). A bundle without a
    verified model is never produced silently.
- **Runtime lookup** (inside the parse), first match wins:
  1. `SPEAKEASY_LAYOUT_MODEL`, when set. This is the override for tests. If it is set but the file is missing, the
     detector is unavailable (`model-missing`); the lookup does not fall through to the other paths.
  2. `$SPEAKEASY_HOME/models/pp_doclayout_s.onnx`.
  3. `<backend dir>/../models/pp_doclayout_s.onnx` (the app bundle's `Resources/models`).
- **Runtime verification:** the parse verifies size and sha256 of the chosen file once per subprocess, before loading it.
  A mismatch makes the detector unavailable (`model-mismatch`), including for override paths.
- **Kill switch:** `SPEAKEASY_LAYOUT=off` (env or `config`) turns the detector off (`disabled`). The default is `on`.
- **Licence:** `app/NOTICE` and `README.md` name PP-DocLayout-S (PaddlePaddle, Apache-2.0) and the ONNX export repo.
- Tests never use `~/.speakeasy`: they set `SPEAKEASY_HOME` to a temp dir and `SPEAKEASY_LAYOUT_MODEL` to a verified
  copy (e.g. `/tmp/pp_doclayout_s.onnx`).

## 2. When detection runs

- **Only inside the parse subprocess,** never in the backend process and never in the tracker.
- **Detected pages only.** The detector runs once per detected page:
  - with `--sel`: the selection's page and its neighbours, the same set as today's figure pages;
  - without `--sel`: every page.
  
  Text-only pages (`figs_done: false`) and pages copied from `--prev` are not run again.
- **One session per parse.** It is created lazily on the first detected page and reused for the rest of the parse.
  Nothing is loaded when the parse has no detected page.
- **Cached with the parse.** The result is part of the parse output and the `cache/doc/*.json` cache. A cache hit runs
  nothing. `PARSE_VERSION` 6 rebuilds every older cache.
- **Input:** the page rendered to RGB, bilinear-resized to 480×480, scaled `/255`, normalised with the ImageNet mean/std,
  `float32 [1,3,480,480]`; `scale_factor = [[480/h, 480/w]]` for the render size `h × w`. Output rows are
  `[class_id, score, x1, y1, x2, y2]` in render pixels, `num_dets` valid rows, NMS already applied by the model. Boxes are
  mapped back to page points (origin top-left) and clipped to the page.
- **Deterministic:** a fixed intra-op thread count (a constant in code, not taken from the environment), one global score
  threshold (a constant in code, in `[0.3, 0.5]`), and page coordinates rounded to 0.1 pt. Two parses of the same file
  give identical output except `ms`.
- **Speed:** per-page `ms` = render + preprocess + inference + snapping, reported in the output (section 6). It is not
  added to the time to first highlight: speech never waits for a cold parse (unchanged rule).

## 3. Class mapping

| Model classes (id) | Role |
|---|---|
| `text` (2), `abstract` (4), `content` (5), `reference` (10), `footnote` (12), `aside_text` (22) | **paragraph** seed |
| `figure_title` (6), `table_title` (9), `chart_title` (17) | **caption** evidence: extends a caption found by `CAP_RE` / `TAB_RE`; never creates a caption on its own |
| `image` (1), `chart` (18) | **figure** body candidate |
| `table` (8) | **table** body candidate |
| `paragraph_title` (0), `doc_title` (11) | heading: no block; its lines join no paragraph |
| `header` (13), `footer` (15), `number` (3), `header_image` (20), `footer_image` (21), `seal` (16) | page furniture: no block; its lines join no paragraph |
| `formula` (7), `formula_number` (19), `algorithm` (14) | other: no block; its lines join no paragraph |

A line *belongs to* a detector box when the line's centre lies inside the box (2 pt slack). Detector boxes below the score
threshold are dropped before any of the rules below.

## 4. Building the page's blocks

The Phase 2 block format and invariants (`phase2-logging.md` D) still hold; changes are listed in 4.6. Figures and tables
are still **anchored on captions**: a `figures[]` entry exists only for a caption line group that matches
`CAP_RE` / `TAB_RE`, as today. The detector decides extents, not which figures exist.

### 4.1 Captions

- A caption block holds its regex line group, as today.
- When a detector caption box overlaps that group's first line, the block also takes every line that belongs to that
  detector box, in the same column, that is contiguous with the group and is not a regex caption of another figure.
  This fixes multi-line captions that the text layer splits into several groups.
- **Caption run into prose (gate ruling, accepted).** On a detector page only, a line in the *middle* of a text-layer
  line group that matches `CAP_RE` / `TAB_RE` starts its own caption group when the page gives one of these signs that
  a new block begins there:
  - it is the top line of a detector caption box;
  - the line before it is in a detector paragraph box that does not hold it;
  - the line before it ended early (the caption line's first word would have fit after it).
  
  The new group takes the following lines of that detector caption box (with no caption box: the following lines that
  no detector paragraph box holds). A caption-shaped line that one detector paragraph box holds together with a full
  line before it is never split off; prose that wraps to start with "Figure 3." stays prose. The fallback never splits
  groups.
- `caption_bbox` = union of the caption block's lines. `figures[].caption` is that text (300 chars max, as today).
- Caption `src`: `regex` (no detector extension) or `regex+detector`.

### 4.2 Figure and table bodies: pairing (the caption seeds the body)

For each caption anchor, the body seed is a detector box of the matching kind (`image`/`chart` for a Figure, `table` for
a Table), chosen in this order:
1. A box that **encloses or overlaps the caption**. The model sometimes runs a body box over its own caption; the box is
   then cut at the caption edge on the body side.
2. Otherwise, the nearest box on the **expected side**: above the caption for a figure, below it for a table. It must be
   in the caption's column (full width when the caption spans both columns), overlap the caption horizontally by at least
   half of the narrower of the two, and have a vertical gap of at most 3 caption line heights. **Gate ruling,
   accepted:** the gap limit is waived when no text line lies between the box and the caption, within their horizontal
   overlap; only whitespace separates them. Column, overlap and side order still apply.
3. Otherwise, the same rule on the other side (figures captioned above, tables captioned below).

Then:
- **Ties:** smaller gap first, then higher score.
- **One caption per box:** each detector box seeds at most one caption.
- **Multi-panel figures:** further `image`/`chart` boxes on the same side, in the same column, chained to the seed with
  gaps under 40 pt (today's chaining distance), are added to the body. Chaining never crosses another caption or a
  paragraph block.
- **Unpaired boxes:** detector body boxes that seed no caption create **no block** (post-threshold rule). Lines that
  belong to them join no paragraph.
- **No pair:** a caption with no paired box gets today's heuristic body (`graphics`, `whitespace`, `text-rows`,
  `caption-only`), with the caption excluded (4.4).

### 4.3 Snapping to PDF geometry

A paired detector body is snapped to the PDF content it covers, so its edges follow real objects, not the 480-px raster:

- **Content items:**
  - raster images and clustered vector drawings, clipped to the page; any item covering more than 90% of the page is
    dropped, which drops page-sized clip rects;
  - text lines that are neither caption lines nor lines that belong to a detector paragraph box.
- **Items kept:** those with at least half their own area inside the detector box. On a two-column page, a body inside
  one column keeps only items in that column.
- **Snapped box** = union of the kept items.
  - An edge never moves outward by more than 8 pt past the detector edge; a larger item is clipped there.
  - With no kept items (e.g. an empty raster region), the box is the detector box clipped to the page.
- **No running into the caption or text:** the snapped box overlaps no caption box and no paragraph block (2 pt slack).
  Where it would, the edge facing that block is moved to the block's edge.
- `bbox_src` / block `src` of a snapped detector body: `detector`. A body chained from detector boxes and heuristic
  graphics is still `detector`.

### 4.4 Captions excluded from bodies (benchmark GT convention)

- `figures[].bbox` and its figure/table block **never include the caption**, for tables as well as figures. This is a
  behaviour change from Phase 2, where a table's box included its caption by construction.
- It applies to the heuristic path too (fallback and unpaired captions), so `figures[].bbox` means the same thing
  whichever path ran.
- On screen, a `table:` focus target now covers the table without its caption. `table_rows` keeps receiving
  `caption_bbox` separately.
- **Reading a table is unchanged apart from the box** (both paths):
  - a read-through of a table (caption plus rows) still maps to `doc-table` and then `doc-table_row` targets, one row
    band per row sentence;
  - a Read-area drag over a table still gets a page-space `table` target.
  
  Excluding the caption from the box must not turn these into line bands or screen-only bands.
- **A body stays in its caption's column** (both paths; gate ruling on the open item, **rejected as failing**):
  - this applies even on pages the Phase 2 column rule calls one-column, including a caption that reaches a few points
    past the page midline;
  - when column-width prose lines sit in the other half of the page beside the body, the table or figure body ends at
    the gutter and contains none of them;
  - those lines stay in paragraph blocks.
  
  This holds for detector-paired bodies and for the heuristic `text-rows` / `graphics` bodies.

### 4.5 `_split_overlaps` stays the arbiter

`_split_overlaps` runs after bodies are built, on both paths. A table body that swallows a figure on the same page is cut
where the figure starts (or the table starts below it), and `split_from` is set as today. It no longer re-unions the
caption into the table box (4.4).

**Gate ruling, accepted: it also cuts figures.**
- A figure body that covers at least 30% of a table on the same page stops at the table's edge, unless the figure's own
  caption lies inside the table box.
- The figure then carries `split_from` = the table's label.
- Its `labels`, `objects` and `panels` are reduced to the cut box.
- `phase2-logging.md` B already defines `doc.split_from` for figure targets.

### 4.6 Paragraphs

- **Detector paragraphs.** Detector paragraph-seed boxes, highest score first, each take the lines that belong to them,
  except lines already taken and lines that:
  - are caption lines;
  - lie inside a figure or table body;
  - belong to a heading, furniture or other box (section 3);
  - belong to an unpaired detector body box.
  
  A box whose lines fall in both columns of a two-column page gives one block per column. Each non-empty set of taken
  lines is one paragraph block. `bbox` = union of its lines, `src: "detector"`.
- **Gate ruling, accepted: score precedence.** The model suppresses overlaps only within one class. So a line held by
  both a paragraph-seed box and a heading / furniture / other / unpaired body box goes to the paragraph when the
  paragraph box scores strictly higher; otherwise it is excluded.
- **Gate ruling, accepted: typographic re-cut.** Detector paragraphs are cut and re-joined at typographic paragraph
  breaks, because the benchmark and the reader both define a paragraph typographically.
  - **Cut** inside a detector block when the row pitch is at least 1.3× the page's usual pitch. Also cut when a row
    ended early (the next row's first word would have fit) *and* either the pitch is at least 1.15× or the next row is
    indented.
  - **Join** two detector blocks that follow each other in one column, with no other line between them and none of
    these cues between them, into one paragraph.
  - The thresholds are fixed constants in `layoutdet.py`.
- **Orphan lines.** Lines not taken and not excluded by the list above form paragraph blocks by today's line-group rule
  (`line_groups`), `src: "lines"`. Text the detector missed stays highlightable.
- **Reading order:** `reading_order`, unchanged.
- **`pages[].columns`:** unchanged; the Phase 2 two-column rule, not the detector.
- **Selection region.** Paragraph blocks are the unit the selection region uses:
  - `docmap` exposes a helper that, given page-line indexes, returns the union of the paragraph blocks holding them;
  - `focus._sel_region` uses it in place of its 60-pt x-median filter, to drop matched lines outside the dominant block's
    column;
  - this is the one `focus.py` call site of section 0. It ships only if `lines.score` does not drop on either set;
    otherwise it is left out and the reason is reported.

### 4.7 Invariants (additions to `phase2-logging.md` D)

The verifier checks these on every Phase 2 public fixture and the logging fixtures, with the detector on and with it
forced off:
- All D invariants still hold (one block per figure, paragraph bbox = union of lines, no shared lines, paragraphs outside
  figures and tables, captions are not paragraphs, post-threshold only).
- Every figure/table `bbox` lies inside the page and overlaps its own `caption_bbox` by at most 2 pt vertically.
- Every block has `src`:
  - figure/table: the `bbox_src` enum, which now includes `detector`;
  - caption: `regex` or `regex+detector`;
  - paragraph: `detector` or `lines`.
- With the detector unavailable, no block has `src` `detector` or `regex+detector`.

## 5. Fallback (never silent)

The detector is **unavailable** for a parse when any of these holds. The `reason` enum, in check order:

| `reason` | When |
|---|---|
| `disabled` | `SPEAKEASY_LAYOUT=off` |
| `no-onnxruntime` | `onnxruntime` or `numpy` does not import in the doc venv |
| `model-missing` | no model file at the lookup path (section 1) |
| `model-mismatch` | the file's size or sha256 differs from section 1 |
| `load-failed` | creating the session raised (the exception class goes in `error`) |
| `pdfkit-engine` | the backend used the PDFKit text path (no doc python, or `SPEAKEASY_PDF_ENGINE=pdfkit`); emitted by the backend, not the parse |

`inference-failed` (`page` and `error` set) applies to one page only: that page falls back alone, and the other pages
still use the detector.

On an unavailable detector or failed page:
- The parse output is **the Phase 2 heuristics** for those pages, with these changes (gate ruling, accepted):
  - caption exclusion (4.4);
  - the two-way `_split_overlaps` (4.5);
  - the column rule for bodies (4.4);
  - **graphics reserved per caption:** on a page with two or more captions, a graphic within 20 pt of a caption's body
    side that overlaps it horizontally belongs to that caption, and another caption's body never chains over it.
  
  These can change figure/table boxes and release lines into paragraphs only on pages with two or more captions, and
  only inside the extent the Phase 2 boxes covered there. Single-caption pages differ from Phase 2 only by 4.4 and the
  column rule. The parse still exits 0 and never raises because of the detector.
- A `layout_detector_unavailable` event is recorded (section 6) once per parse, or once per page for
  `inference-failed`.
- `parse-pdf` prints one stderr line: `layout: detector unavailable (<reason>)`.
- The page's `layout.src` is `heuristic` with the `reason`.

## 6. Output fields and events (no text)

All new fields hold numbers, enums, booleans and hashes. They never hold document text, file paths or image data,
whatever `SPEAKEASY_LOG_TEXT` is. Logging never raises and never delays speech.

### 6.1 Parse output (`parse-pdf` output, `cache/doc/*.json`, `docmap.py dump`)

| Field | Type | When | Meaning |
|---|---|---|---|
| `layout` | object | always (PDF) | `{"status": "ok"\|"unavailable", "detector_version": str\|null, "reason": <enum>}`. `reason` is present only when unavailable |
| `pages[].layout` | object | every `figs_done: true` page | `{"src": "detector"\|"heuristic", "ms": int, "detector_version": str, "blocks_by_kind": {"paragraph": n, "caption": n, "figure": n, "table": n}}` for detected pages. `{"src": "heuristic", "reason": <enum>}` otherwise. Pages copied from `--prev` keep their original value |
| `layout_events` | list | always (PDF) | The events of **this** parse invocation only (6.2), in page order |
| `pages[].blocks[].src` | string | every block | Section 4.7 |
| `figures[].bbox_src` | string | as today | Enum gains `detector` for figures and tables |

### 6.2 Events

- **`layout_page`**, one per detected page where the detector ran:
  `{"event": "layout_page", "page": n, "ms": int, "detector_version": str, "blocks_by_kind": {...}}`.
  `blocks_by_kind` counts the page's final blocks, i.e. what the app acts on.
- **`layout_detector_unavailable`:**
  `{"event": "layout_detector_unavailable", "reason": <enum>, "error": "<ExceptionClass>"|null, "detector_version": str|null, "page": n|null}`.

### 6.3 Runs log

| Field | Type | When | Meaning |
|---|---|---|---|
| `layout_events` | list | present when this run's document job produced the parse (cache state `miss` / `partial-miss`) or used the PDFKit path | The parse's `layout_events`, copied as they are; for PDFKit, one `layout_detector_unavailable` with `pdfkit-engine` |
| `doc_parse.layout` | object | inside `doc_parse` when the matched page has `figs_done: true` | That page's `pages[].layout`, without `detector_version` |

`tests/audit/SCHEMA.md` gains both runs keys. A cache hit adds no events.

## 7. Benchmark harness (verifier-owned, under `tests/`)

Implementers edit nothing under `tests/` except `tests/audit/SCHEMA.md`. The verifier makes these harness changes. None
of them touch `config` or the fixtures, so the Phase 2 baselines stay valid for the gate:

- **`run_bench.py` model passthrough:**
  - it sets `SPEAKEASY_LAYOUT_MODEL` in the scrubbed environment, from `--layout-model PATH` or the caller's
    `SPEAKEASY_LAYOUT_MODEL`;
  - without either, it leaves it unset. The temp home has no model, so that run measures the fallback.
- **Interpreter:** the bench runs under a Python with `pypdfium2`, `onnxruntime` and `numpy`, e.g. the 3.12 doc venv used
  read-only as an interpreter.
- **`versions`** gains `onnxruntime` and `layout_model` (the `detector_version`, or `null`). The gate warns, never fails,
  on a difference.
- **New `sets.<set>.layout` block** (aggregates only; hidden set included):
  - `{"pages": n, "src": {"detector": n, "heuristic": n}, "reasons": {...}, "ms": {"p50": int, "p95": int}}`, from the
    parse outputs of section 5.1 of the benchmark contract;
  - `src` and `reasons` are determinism-covered; `ms` is not.

## 8. Acceptance

**Primary gate.** `run_bench.py --set all --layout-model <verified copy>` followed by `bench_gate.py` against the current
Phase 2 baselines must give:

| Check | Requirement |
|---|---|
| `boxes.score` | **≥ 90 on public AND on hidden** |
| Other gated metrics | `boxes.precision_50`, `lines.score`, `parts.score`, `parts.precision`: none drops by more than 1 point on either set (`bench_gate.py` exits 0) |
| Speed | `first_highlight_ms` p50 and p95 < 300 ms on both sets |
| Detector timing | `sets.*.layout.ms` p50/p95 reported per page. `parse-pdf --sel` cold wall time reported per public doc, with and without the model |
| Coverage | `sets.*.layout.src.heuristic == 0`: every scored page went through the detector |

**If 90 is not reachable** without bench-specific tuning, the deliverable is the ceiling and its causes, not more tuning:
- the best honest score per set;
- per-kind remaining misses on public, labelled:
  - `detector-miss`: no detector box of the right class;
  - `snap`: a detector box exists but the snapped IoU is under 0.75;
  - `pairing`: the wrong box or no box was paired with the caption;
  - `gt-convention`: the GT and the contract disagree on the extent;
  - `heuristic`: a fallback page;
- hidden aggregates by kind only;
- whether `egret-medium` would address the `detector-miss` share.

**Anti-overfitting rules:**
- No doc ids, page numbers or fixture-specific constants in code.
- One global score threshold.
- Tolerances are fixed constants, stated in `layoutdet.py`.
- Hidden per-case files are never read.
- Any constant changed after a hidden-set run is reported, with before/after hidden aggregates.

**Fallback is not a regression.** A bench run without the model (detector unavailable on every page) passes
`bench_gate.py`: no gated metric drops by more than 1. The fallback is the Phase 2 heuristics plus the changes listed
in section 5.

**Determinism.** `run_bench.py --set public --check-determinism --layout-model …` exits 0.

**Install and fallback checks** (temp `SPEAKEASY_HOME`, never `~/.speakeasy`):
- `install.sh` with a good cached model makes no network access and leaves the file unchanged.
- A corrupted model (one byte flipped) makes `install.sh` exit non-zero with the ERROR line and leaves no model at the
  install path; `bundle_python.sh` fails the build.
- Each reason in section 5 has a check that it produces:
  - the stderr line;
  - the `layout_detector_unavailable` event with that reason;
  - `pages[].layout.src == "heuristic"`;
  - blocks identical to a run with `SPEAKEASY_LAYOUT=off`.
  
  Use `SPEAKEASY_LAYOUT=off`, a missing override path, a corrupted copy, an interpreter without onnxruntime (e.g.
  a plain venv with pypdfium2), and a model file that is not ONNX but has the patched expected hash (`load-failed`), or a mocked
  session.
- An `inference-failed` page falls back alone while its neighbours keep `src: detector`.

**Privacy.** `privacy_audit.py` finds no document text in `layout_events`, `doc_parse.layout` or `pages[].layout`, with
`SPEAKEASY_LOG_TEXT` unset and set to 1.
- The audit predates the layout keys. It is updated at the gate as a pure pin update:
  - `doc_parse.layout` may hold `src` / `reason` enums, an integer `ms` and per-kind counts;
  - every `layout_events` entry must have exactly the section 6.2 keys, with numbers, the reason enum, an exception
    class name and the detector-version hash.

**Regression suites,** all with the same pass/fail counts as on `890faa4`, except checks the verifier rewrites for the
intended changes (`bbox_src`/`src` enums gaining `detector`, table boxes without captions, the new keys of section 6 in
the key-set pins):
- `tests/audit/readability` (`run_phase1.py`);
- `tests/audit/logging` (`run_phase2.py`);
- `BOXACC_QUICK=1 tests/audit/boxacc/test_box_accuracy.py`;
- `tests/audit/bench/test_score.py`;
- `cd tracker && swift test`;
- the CI smoke steps of `.github/workflows/macos.yml`. Its venv has no onnxruntime, so CI exercises the fallback, and
  pyflakes stays clean;
- `bash app/build.sh build`;
- `bundle_python.sh` on a scratch app copy, with the model present in `Contents/Resources/models/` and the app still
  codesign-valid.

**Release step.** After acceptance, re-record both baselines (`PARSE_VERSION` bump, benchmark contract section 8) on the
merged tree, under an interpreter with the model.

## 9. Out of scope

- Using the detector for reading order, column detection, panel splitting, part grounding or Markdown.
- Creating figures or tables without a `Figure N` / `Table N` caption.
- GPU or CoreML execution providers.
- Fetching the model at run time; only the installers download.
