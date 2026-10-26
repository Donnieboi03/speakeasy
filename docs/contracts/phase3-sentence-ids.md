# Phase 3 contract: sentence IDs (text band = the spoken sentence's own lines)

Scope: how a spoken chunk finds its place in the source and which lines the text band covers. It covers the shared text
fold, the sentence map, chunk → sentence assignment, what a show carries, what the tracker draws, fallbacks, log fields
and acceptance. Figure, table and part cues keep their Phase 2 logic except where rule C.4 says otherwise. This file
prescribes **observable behaviour**, not code. Terms are as in `phase1-readability.md`, `phase2-logging.md` and
`phase2-benchmark.md`. *Selection text* = the text the run reads (stdin, or the Read-area PDF text that replaces it).
*Chunk* = one `Speaker.put`. *Source sentence* = one entry of the sentence map (B). *Band* = the drawn text cue of a chunk.
*Key* = the fold match key (A).

Prior art. EPUB3 Media Overlays (W3C) pairs each audio clip with a text reference (an element id with a character
span). Readium's TTS navigator speaks and highlights sentence by sentence, with word level as a second layer. Decision:
**build on top**. We adopt the Media Overlays data model (a sentence id plus a char span per chunk, known before
playback) and Readium's sentence granularity. We keep our own splitter (tts `NOT_ABBREVIATION` rules) and use pySBD (MIT)
only if section G's splitter check fails. Alignment uses stdlib only. Word-level highlighting (Kokoro per-word
timestamps) is **parked**: no field in this phase may depend on it.

## 0. Starting point (measured on 890faa4)

Public lines score: 38.36 (56/146). Every miss has a show with a page-space target. The reasons are:

- **under_cover 60.** 54 of these are single-chunk sentences. The matcher picks one source line for a 2–3-line sentence:
  `_span` caps the span, and a neighbour line joins only at ≥ 0.6 of the best line's score. In every one of the 60,
  `doc.lines` holds the same lines as `doc.target`. So drawing per-line rects alone fixes none of them; the span is wrong.
- **wrong_cue 29.**
  - 17 are prose next to a two-column table. The parsed table bbox spans the full page width (e.g. x 50–542 for a table
    at 306–545), so `_table_for_selection` treats the prose as a table body.
  - 12 are captions read verbatim in figure mode. They get figure, panel or part-oval cues, and 6 of those are stray
    ovals that lower parts precision.
- **over_extend 1.** One sentence crosses the column break. Its 2 lines are both covered, but the single union rect spans
  the gutter.

Hidden aggregates: 48.91 (67/137), with under_cover 50 and wrong_cue 20.

Facts used below:
- Every public lines-scored script sentence folds to 100 % token overlap with its selection.
- Parts-scored sentences have a median overlap of 25 %. Only two reach ≥ 0.75 bag-of-words overlap, and both are
  paraphrases, not contiguous substrings.
- A band of the sentence's whole page lines passes 145/146 with a union rect and 146/146 with per-line rects.

## A. Shared text fold (Python and Swift, identical)

`fold(text)` returns the key (a string) and an offset map (for each key character, the index of the original character
it came from). Steps, in order:

1. Unicode NFKC. Ligatures (U+FB00–FB06), full-width forms and compatibility digits are expanded. Each expansion
   character maps to the original character's index.
2. Remove U+00AD (soft hyphen), U+200B–U+200D, U+2060 and U+FEFF.
3. **Line-end hyphen rejoin.** A hyphen (U+002D, U+2010 or U+2011) that follows a letter and is followed by optional
   spaces, one line break, optional spaces and a lowercase letter is removed together with the break and the spaces.
   Any other line break counts as a space.
4. Lowercase. Use the Unicode default lowercase mapping, not casefold.
5. **Number words → digits** (whole words only):
   - `zero`–`nineteen`;
   - `twenty`–`ninety`;
   - tens-units written with a hyphen or a space (`twenty-five`, `twenty five`) → `25`.
   - `percent` / `per cent` and `%` both become `percent`.
   - Nothing else is folded: no ordinals, `hundred`, decimals spoken as words, or symbols.
6. **Key** = only the code points whose Unicode general category is `L*` or `Nd`, in order. Spaces and punctuation
   (including `-`, `|` and quotes) are dropped, so hyphen decisions never change the key.

There are two token layers. Both are identical in Python and Swift:

- **Fold tokens** (the Tokens column below): the maximal L*/Nd runs of steps 1–5. Nothing is dropped.
- **Match tokens** (the Match tokens column): fold tokens with every single-character run removed unless that character
  is a digit (category Nd).
  - Digit runs of any length stay (`3`, `5`, `25`). Letter runs of length ≥ 2 stay.
  - **There is no stopword list.** Common words (`of`, `the`, `and`) stay in the token set. A matcher that must discount
    them does so by weight: document frequency, as in the tracker's `refind`, or C.1's half weight. It never filters
    them out.
  - Match tokens are what docmap `_toks`, the tracker's `tokens()`, fuzzy alignment (C.1), anchors and refind use.
  - Ruling (Phase 3 amendment 1). This replaces two rules that disagreed:
    - Python `_toks` dropped every one-character token, including digits;
    - Swift `tokens()` kept single digits but dropped a 21-word stopword list.

Every text comparison in `focus.py`, `docmap.py` (page match, table and figure mode, line alignment) and `tracker.swift`
(anchors, refind) uses this fold. The unused `_NUMW` table is replaced by step 5. The existing XCTest
`testTokensFoldLigaturesAndCase` already pins this rule (ligature equality; `3` kept and `a` dropped from `a 3 of`) and
stays **unchanged**.

**Shared vectors.** Both implementations must give exactly these keys, fold tokens and match tokens. The verifier turns
them into one Python test and one XCTest reading the same JSON.

| Input | Key | Tokens | Match tokens |
|---|---|---|---|
| `classi\uFB01cation` | `classification` | `classification` | `classification` |
| `classi-\nfication` | `classification` | `classification` | `classification` |
| `state-of-the-\nart` | `stateoftheart` | `state of theart` (known compound-hyphen limit; the key still matches) | `state of theart` |
| `Line-\nBased` | `linebased` | `line based` | `line based` |
| `co\u00ADoperate` | `cooperate` | `cooperate` | `cooperate` |
| `Twenty-five per cent` | `25percent` | `25 percent` | `25 percent` |
| `25 %` | `25percent` | `25 percent` | `25 percent` |
| `Fig. 5 \| Pipeline` | `fig5pipeline` | `fig 5 pipeline` | `fig 5 pipeline` |
| `ＡＢＣ１２` | `abc12` | `abc12` | `abc12` |
| `naïve café` | `naïvecafé` | `naïve café` | `naïve café` |
| `“Quoted”—text` | `quotedtext` | `quoted text` | `quoted text` |
| `one of the 3 rows` | `1ofthe3rows` | `1 of the 3 rows` | `1 of the 3 rows` |
| `2.5 kHz` | `25khz` | `2 5 khz` | `2 5 khz` |
| `a 3 of` | `a3of` | `a 3 of` | `3 of` |
| `Panel (b) at x = 7` | `panelbatx7` | `panel b at x 7` | `panel at 7` |
| `\u2060\uFEFF` (only) | `` (empty) | (none) | (none) |

Offset map check: for `classi\uFB01cation`, key characters `f` and `i` both map to index 6. For `classi-\nfication`, no
key character maps to the hyphen or the break.

## B. Sentence map (built before speech)

- It is built **once per run**, from the selection text, before the first chunk is located. Building it never waits for
  the document job, the model or OCR. Budget: ≤ 25 ms for 6000 characters (`ms` in F).
- **Splitting:**
  - A sentence ends at `.`, `!` or `?` (plus closing quotes or brackets), except:
    - after an abbreviation in the shared list (`Fig.`, `e.g.`, `et al.`, `Dr.` …);
    - inside a number (`3.2`, `0.5`);
    - at a line-end hyphen.
  - A blank line, a list-item start and a bullet are boundaries.
  - A selection line of ≤ 8 words with no terminal punctuation, followed by a line starting with an uppercase letter,
    is its own unit (heading or label).
  - Text with no sentence end at all is one unit.
- **Each source sentence has:**
  - `id`: `s0`, `s1` … in selection order. Run-local, and never derived from text.
  - `span`: `[start, end)` character offsets in the selection text.
  - `sel_lines`: indices of the non-blank selection lines it touches.
  - **Screen rects**, one per visual line holding at least one of its characters, **unmerged**:
    - from `--line-bounds` visual lines (`{loc, len, rect}`): every visual line overlapping the span;
    - from the list form (one rect per selection line): those lines' rects;
    - with no line bounds: none.
  - **Page rects** (PDF with a matched page): the whole rects of every page line holding at least one of its
    characters, in page reading order.
    - **No horizontal clipping.** The parse has no character boxes, and a clipped first or last line could miss the
      bench's 50 % width rule.
- **Selection → page alignment.** The whole selection key is aligned once against the matched page's lines.
  - Page lines are taken in reading order (`pages[].blocks`, then any lines left over) and matched forward and
    contiguously, so every selection character maps to at most one page line.
  - The search starts in the selected region (the Phase 2 `_sel_region`). A header or footer repeated elsewhere on the
    page is never pulled in.
  - If the selection key occurs twice on the page, the occurrence nearest the capture rect mapped to page space wins.
  - Bag-of-words line matching (`_doc_lines`' ≥ 0.8 rule and densest run) is not used for text bands.
  - Selection text that is not on the matched page gets **no page rects**. For example, when a selection runs across a
    page break, the lines from the next page stay unaligned. They are never matched to look-alike lines of the matched
    page.
    - A sentence with no character on the matched page has screen rects only: no `doc.lines` and no `doc.target`. It
      does not count in `mapped` for method `page`.
    - A sentence that crosses the break keeps only its matched-page lines in `doc.lines`, while `rects` keeps every
      visual line of its span.
- **Document not ready when the map is built.** The map starts with screen rects only. Page rects are attached when the
  document job finishes. Shows before that are screen-only (Phase 2 behaviour); shows after carry page rects.
  - **Phase 3 amendment 6.** Shows after that point are also evaluated with the document for C.4 rules 1 and 3. A
    chunk of a caption or figure selection whose show comes after the document lands gets its figure cue, and its
    grounding request when grounding is on.
  - In a cold run (no parse cache), the number of figure-cue shows, anchored shows and grounding requests therefore
    depends on when the document lands relative to playback. This is intended.
  - Tests compare these counts only on warm runs, where the document is ready before the first sentence.

## C. Assigning each chunk to source sentence(s)

1. **Alignment (forward only).** The chunk is folded and aligned against the selection key from a cursor (the end of the
   last assigned span; 0 at start).
   - **Verbatim**: the chunk key occurs as a contiguous substring of the source key at or after the cursor. The first
     such occurrence wins, and `match_score = 1.0`.
     - A near-verbatim match (edit distance ≤ 10 % of the chunk key) also counts. Then
       `match_score = 1 − edits/len`, which is ≥ 0.9.
   - **Fuzzy**: otherwise, an in-order token alignment against the rest of the selection.
     - `match_score` = matched chunk tokens / chunk tokens, over match tokens (A). Common function words weigh 0.5. That weighting list is
       local to this Python scorer: it never removes a token and is not part of the shared fold.
     - It is assigned when the score is ≥ 0.6 and the matched source run is ≤ 1.5× the chunk's length.
   - `runner_up`: the best score of any other forward candidate that does not overlap the chosen span; `null` if there
     is none.
2. **Span → band lines.**
   - Verbatim: the lines holding at least one character of the **aligned span**, not the whole sentence. A clause chunk
     therefore gets its clause's lines, and the chunks of one sentence together cover all of it.
   - Fuzzy: all lines of every source sentence the matched run overlaps.
   - `sentence_id` = the source sentence holding most of the span's characters (on a tie, the earlier one). The map
     record also lists every overlapped id (F).
   - The cursor moves to the span's end.
3. **Never backwards.** A chunk whose only match at ≥ 0.9 lies before the cursor (with < 0.6 ahead) is a back-reference.
   - It keeps the current band (`method: hold`), and the Phase 2 faint `ref` outline goes on the earlier sentence.
   - Repeated text maps to its next occurrence.
   - A skipped sentence is never highlighted.
4. **Cue decision**, the first rule that applies:
   1. The chunk names a figure or table by label and that object is visible (Phase 2 `find_ref` rules): figure or table
      cue, unchanged.
   2. A verbatim chunk: **text band** on its span's lines. This holds even when the selection is a caption (figure mode)
      or the parse puts those lines inside a table bbox.
   3. Figure mode or table-body selection: Phase 2 figure, panel, part or table-row cues, unchanged.
      - **Phase 3 amendment 5.** For a text table with no document (pipe or tab-separated rows), the Phase 2 cue is the
        **row line** the chunk names.
      - A non-verbatim chunk that names one row (its row label or distinctive cells, e.g. "Mirevolt has 42 quintessa
        units." for the row `Mirevolt | 42`) gets a text band on that row's line, and `src` lists only that row. This is
        the 890faa4 behaviour.
      - A chunk that names no single row falls through to rule 5, the whole-block fallback (e.g. "The table compares
        two busy services.").
   4. A fuzzy chunk: text band on its sentences' lines.
   5. Otherwise: fallback (E).
5. **Every path carries a span.**
   - **Streamed** (`Releaser`): as above.
   - **Non-streamed** (`SPEAKEASY_STREAM=0`, a stream failure before speech, model output under the fallback reader):
     with focus active, each chunk holds at most one output sentence. The 120-character `sentences()` merge is not used
     for focus runs.
   - **Verbatim blocks** (email, quote, list, table speech): a sentence built from selection lines carries its selection
     char span. It is assigned directly (`method: span`, `match_score: 1.0`). Generated framing (`Here's the email to
     Lee:`) carries no span and falls back (E).
   - **Input over `CHUNK`** (not streamed): every model chunk's output is aligned against the one selection map. The
     cursor carries across model calls.
   - **Tags on** (`SPEAKEASY_FOCUS_TAGS`): a valid `[Lx-y]` tag narrows the alignment window to those lines ± 1. With no
     match inside the window, the normal forward alignment runs. A tag never moves the band backwards.
6. **Cleanup keeps verbatim text verbatim.** In read-through mode, `strip_openers`, `demeta` and `scrub` must not
   change a sentence whose key occurs in the selection key, except for characters the key ignores (`|`, quotes, dashes).
   This fixes `This page explains …` → `The …` and `the image` → `this` on read-through text that the source itself
   contains.

## D. What a show carries, and what the tracker draws

**Show record and live `focus.json`** (additive; existing keys keep their meaning):

| Field | When | Meaning |
|---|---|---|
| `sentence_id` | always on shows of a run with a sentence map | Primary source sentence id (`s3`), or `null` for a block, figure or table cue with no aligned span |
| `match_score`, `runner_up` | always on such shows | From C.1, 2 decimals; `null` for every `hold` (C.3 back-references included), every `block`, and Phase 2 figure/table cues (F, amendment 4) |
| `rects` | text band with at least one screen line rect | Screen `[x, y, w, h]` per band line, in reading order |
| `rect` | unchanged | Union of `rects` when present |
| `nlines` | unchanged key | `len(rects)` when `rects` is present, else `len(doc.lines)` |
| `range` | unchanged key | Absolute AX range of the **aligned span** (exact characters, not whole lines) when the selection base is known |
| `doc.lines` | text band with page rects | Page rect per band line. This is now exactly the band (Phase 2 used the same set) |
| `doc.target` | unchanged | Union of `doc.lines` (bbox) |

**Tracker:**

1. A show with `rects` and/or `doc.lines` draws **one band of several rects**: one rounded rect per line. Adjacent
   lines in the same column may join into one shape. Nothing is filled across a column gutter or between non-adjacent
   lines. The panel stays click-through.
2. **First draw**: `rects` plus the current scroll offset, at once (the Phase 2 0.3 s draw watchdog still applies).
3. **With a page fit** (`anchors` or `event-transform`, the Phase 2 `page_fit`): every `doc.lines` rect is mapped with
   **that one fit**. There is never a per-line fit.
4. **Refind is a check, not a source.**
   - The check passes when OCR refinds at least one band line and it intersects its mapped rect.
   - After two failed checks in a row the tracker refits from anchors.
   - If the refit fails, it keeps the last good mapping (moved by scroll deltas) and logs `check_failed`.
   - For a show with `doc.lines`, it never replaces the band with the OCR union box.
5. **Clamp**: each rect is clamped to the window, and rects fully outside are dropped. The band hides when less than
   30 % of its total area is visible (the Phase 2 rule, applied to the sum).
6. **Legacy shows** without `rects` or `doc.lines` (figure cues, older backends): single-rect behaviour, unchanged.
7. **Draw records** add `sentence_id` and `line_count` (rects drawn after the clamp). `text_rect` and `page_rect` stay
   the union.

## E. Fallbacks

| Situation | Behaviour |
|---|---|
| No selection text (image-only, no-selection) | No sentence map. Phase 2 behaviour (thirds, R tags) |
| Explain/summary chunk, alignment < 0.6 | **Whole block**: band on every selection line (multi-rect, page rects when known). `sentence_id: null`, `method: block`. The same for each such chunk; never proportional walking |
| Read-through chunk, alignment < 0.6 (connective, opener, hallucinated aside) | Hold the previous band (same `sentence_id`, `method: hold`). The run's first chunk uses block instead |
| Verbatim framing line with no span | Block when it is the first chunk, else hold |
| Map has no page rects (document missing, page not matched, not ready yet) | Screen-only bands from `rects`; no `doc.target`. Never wait for the document |
| No line bounds and no page | Proportional band inside the selection rect, over the span's character fraction of the selection (not whole logical lines) |
| Alignment raises | Logged in `doc_note`. That chunk uses the Phase 2 matcher (`method: overlap/progress`). Speech never breaks |

## F. Log fields (no text anywhere)

- **Focus log `sentence_map` record**:
  - Fields: `{"state": "sentence_map", "sentences": n, "mapped": n, "method": "page|ax-lines|band|none", "ms": int,
    "ts", "pid"}`.
  - `mapped` = sentences with at least one page rect (method `page`) or at least one screen rect (`ax-lines`).
  - It is written once when the map is built, and again with the same fields when page rects attach later. It is never
    copied to `focus.json`.
- **Runs log**: `sentence_map` holds the final `{sentences, mapped, method, ms}`. `focus_methods` counts the same
  `method` values as the map records (next item).
- **Focus log `map` record**: adds `sentence_id` and `sentence_ids` (every overlapped id).
  - **`method` (Phase 3 amendment 3).** For every text-band chunk the sentence map assigned, `method` is the assignment
    method: `align`, `align-fuzzy`, `span`, `hold` or `block`.
    - This holds **whether or not the band has a page target or a page→screen transform**. A band with a page target
      is shown by the show's `doc.kind == "line"` and `doc.target_src == "lines"`, never by the method name.
    - `doc-line` (and `overlap` / `progress`) appear only on the Phase 2 matcher path: a run with no sentence map, or a
      chunk whose alignment raised (E).
    - Figure and table cues reached through C.4 rules 1 or 3 keep their Phase 2 `doc-<kind>` values (`doc-figure`,
      `doc-table`, `doc-table_row`, `doc-object`, `doc-panel`).
    - So the same chunk logs the same method in a bench run (with line bounds and a transform) and in a run without
      line bounds.
  - **Scores (Phase 3 amendment 4).**
    - For `align`, `align-fuzzy` and `span`, `score` and `runner_up` are the C.1 values. The show's `match_score` and
      `runner_up` equal them.
    - For **every `hold`** (a back-reference under C.3, or an unaligned read-through chunk under E) and every `block`,
      `score`, `runner_up`, `match_score` and the show's `runner_up` are all `null`. The held band is not a match for
      this chunk.
    - The back-reference itself stays visible through the Phase 2 `ref_lines` / `ref_trigger` map fields and the
      show's `ref` outline.
  - `lines` stays the 1-based selection-line range.
- **Show record**: the D table.
- **Tracker `draw`**: `sentence_id`, `line_count`.
- Rules: ids are run-local `sN` strings; scores have 2 decimals; whatever `SPEAKEASY_LOG_TEXT` is, no new field holds
  document, selection or spoken text. `tests/audit/SCHEMA.md` gains the runs keys. The verifier widens the readability
  and logging key-set pins to exactly these keys.

## G. Acceptance

1. **Bench gate**: `run_bench.py --set all` then `bench_gate.py` gives `GATE PASS` against the 890faa4 baselines.
   - `lines.score ≥ 95.00` on **both** sets: public ≥ 139/146, hidden ≥ 131/137.
   - No other gated metric drops by more than 1.00. At these n that means:
     - **zero lost parts sentences** on either set (1 sentence = 1.54 public, 1.96 hidden);
     - **no new stray oval** (public 41/63 = 65.08 fails, hidden 31/43 = 72.09 fails);
     - boxes unchanged, unless the parse is changed on purpose (then it must not drop).
   - First-highlight p50 and p95 < 300 ms on both sets, and `sentence_map.ms` p95 ≤ 25.
   - `--check-determinism` exits 0.
2. **Scorer rule (verifier-owned, backward compatible).** When a show has a non-empty `doc.lines`, `B_c` is that set of
   rects:
   - coverage = a GT line covered by any rect of any chunk;
   - over-extension = the other page lines covered by any rect of the chunk, at most 1.
   - Shows without `doc.lines` score as today.
   - The verifier adds `test_score.py` cases (a cross-column 2-line sentence hits with per-line rects and misses with
     the union).
   - It then re-records both baselines **at 890faa4** (a worktree) with the new scorer before gating.
   - Without this rule, the cross-column sentence stays `over_extend` (public ceiling 145/146 = 99.32).
3. **If 95 is not reached on a set**: do **not** tune to the bench. That means:
   - no thresholds chosen per fixture;
   - no reading hidden per-case files;
   - no edits to fixtures, GT, specs or the scorer beyond rule 2.

   Instead, report:
   - each set's score;
   - the aggregate miss reasons (hidden: aggregates only);
   - the public misses grouped by cause class (alignment, splitter, page alignment, cue rule, parse);
   - a ceiling estimate.

   The phase then stays open.
4. **Splitter check**: on the public fixtures, the B splitter's boundaries match the GT script boundaries for ≥ 98 % of
   lines-scored sentences. Otherwise pySBD (MIT) replaces it behind the same interface.
5. **Regression suites** keep their results:
   - `tests/audit/readability` (517) and `tests/audit/logging` (725): pass counts kept, except the key-set pins the
     verifier widens (F);
   - `tests/audit/boxacc`: 135 pass / 13 fail, with the same failing check names or fewer. This includes
     `sel_fig5_caption` (an explanation, so still a figure cue) and `sel_prose_band`;
   - the tracker `swift test` (10 existing tests plus the new ones);
   - the CI smoke (`.github/workflows/macos.yml`), `test_score.py`, `tests/audit/stream` and `privacy_audit.py`.
6. **Test ownership (Phase 3 amendment 2).**
   - Implementers may **add** their own unit tests, in **new files only**:
     - under `tests/` but outside `tests/audit/` (e.g. `tests/test_text_fold.py`, `tests/text_fold_vectors.json`);
     - or as new files in `tracker/Tests/TrackerTests/` (e.g. `SentenceBandTests.swift`).
   - Implementer tests are never acceptance and count toward no gate. The verifier may keep, move or delete them.
   - When an implementer copy of the A vectors differs from this contract, the contract wins.
   - Implementers do **not** edit any existing test file, anything under `tests/audit/` other than
     `tests/audit/SCHEMA.md`, the bench scorer, fixtures or baselines (those are the verifier's, rule 2), or any holdout
     file.
   - Implementer tests must pass on the tree they ship with. CI's `swift test` runs them.

## H. Failure cases (required behaviour)

| Case | Required |
|---|---|
| Spoken chunk is a 40-character first-audio cut of a long sentence | Band on the cut's own lines; the next chunk continues forward; together they cover the sentence |
| Sentence crosses the column break | Two rect groups, one per column; no fill across the gutter; one `sentence_id` |
| Sentence continues onto the next page | Band on the current page's lines only (`doc.page` is one page) |
| Same sentence text twice in the selection | Spoken first time → first copy; second time → second copy |
| Model skips sentence s4 | s4 never highlighted; s5's band follows s3's with no gap fill |
| Model repeats s2 after s3 | Hold s3's band; faint ref outline on s2 |
| Model paraphrases (read-through) | Fuzzy band on the overlapped sentence(s) at ≥ 0.6; below that, hold |
| Caption selected, model reads it verbatim | Text bands on the caption lines; no ovals, no figure box |
| Caption selected, model explains it ("Figure 5 shows …") | Figure / part cues as in Phase 2 |
| Prose beside a full-width mis-parsed table | Verbatim chunks get text bands; table-row cues only for non-verbatim chunks of a table-body selection |
| Selection is a real table body | Table-row bands as in Phase 2 |
| Ligature, soft hyphen, line-end hyphen or number word differs between source and speech | Same key; verbatim alignment |
| Web view, wrapped paragraph (one logical line, five visual lines) | Five unmerged visual rects; the band covers only the visual lines of the span |
| Document parse finishes after the first chunk | First show screen-only and immediate; later shows carry `doc.lines` |
| Tracker OCR refind disagrees with the mapped rects | Refit from anchors after two failures; never draw the OCR union for a `doc.lines` show |
| `SPEAKEASY_LOG_TEXT` unset | No text in `sentence_map`, show, map or draw fields; privacy audit clean |

## I. Out of scope

- Word-level highlighting (parked).
- Fixing the full-width table/figure bbox on two-column pages (a boxes-phase item; rule C.4.2 only stops it from
  hijacking verbatim prose).
- Part-oval accuracy (`no_oval`, `centre_outside`, hidden `wrong_page`).
- Markdown document line mapping beyond the screen-rect path.
- Vision runs, Read-area OCR and VLM grounding.
