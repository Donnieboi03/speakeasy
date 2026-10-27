# Phase 5 contract: figure parts (the part oval lands on the named part)

Scope: the `parts` score of the benchmark (`phase2-benchmark.md` 6.3): a parts-scored sentence hits when every
`part_oval` show of its chunks has its centre inside the named part's bbox. This file prescribes **observable
behaviour**, not code. Terms are as in `phase1-readability.md`, `phase2-logging.md`, `phase2-benchmark.md`,
`phase3-sentence-ids.md` and `phase4-layout.md`. *Scope* = the panel rect a part search is limited to (or the figure
bbox when there is none). *Pick* = one picker request for one chunk. *Gated mode* = the default bench run.

Prior art (decided before this contract):
- **Use as-is:** Set-of-Mark prompting (Yang et al. 2023, SoM). Marks are drawn on the image and the model answers with
  one mark id. Ollama's native `/api/chat` with `logprobs` / `top_logprobs` gives the confidence. The model is the
  local `qwen3-vl:4b-instruct`, which the app already ships. The plan was checked against Ollama 0.40.10: it returns
  `top_logprobs`, a warm pick takes 110–550 ms with an image prompt of about 1,100 tokens, and the first call loads the
  model in about 8 s.
- **Build on top:** only the Speakeasy glue: the figure > panel > part hierarchy, callouts, candidate selection, mark
  drawing, letter normalisation, fallback and events.
- **Record/replay for the bench:** the vcrpy pattern (record real answers once, replay them offline), rebuilt in the
  stdlib mock, because the mock is stdlib-only.
- **Rejected:** keeping the single-box `bbox_2d` answer for the local path. It has no confidence, its box drifts off
  small parts, and its errors become stray ovals.

## 0. Starting point and target

| Set | parts.score | n | by style (hits/n) | precision |
|---|---|---|---|---|
| public (0c30faa) | 63.08 (41/65) | 65 | inside 32/43, adjacent 9/13, **callout 0/8**, raster 0/1 | 66.13 (Phase 3 about 79; Phase 4 branch about 73) |
| hidden | 60.78 (31/51) | 51 | inside 22/30, adjacent 9/10, **callout 0/8**, raster 0/3 | 73.81 (Phase 3 about 91) |

Public miss reasons: `no_oval` 16, `centre_outside` 8. Hidden: `no_oval` 17, `wrong_page` 3.

- **Baseline B5** = both baselines re-recorded on the main commit that merges Phase 3 and Phase 4, before any Phase 5
  code. Phase 5 is gated against B5.
- **Target:** `parts.score >= 70.00` on **both** sets, in gated mode (section 7). At today's n that is public
  >= 46/65 and hidden >= 36/51.
- **Guardrails:**
  - no other gated metric drops by more than 1.00 against B5; this includes `parts.precision`, so stray ovals count;
  - first-highlight p50 and p95 < 300 ms on both sets.

## 1. Where the code goes (merge rule)

Work starts from post-merge `main` (Phase 3 + Phase 4). Implementers do not edit the Phase 3 or Phase 4 trees.

| File | Phase 5 changes |
|---|---|
| `src/docmap.py` | The hierarchy inside `parse_pdf`: panels, parts and callouts, computed after `_split_overlaps` so they use the final figure box. Panel-limited matching (`ground_label` scope), callout reference parsing, `parse_summary` counts. `PARSE_VERSION` = post-merge value + 1 |
| `src/pdfpage.py` | At most a path-segment helper (for leader lines) |
| **new `src/partpick.py`** | Candidate selection, mark drawing (runs inside the doc venv: pypdfium2 + Pillow), request body, logprob parsing. Its name shadows no stdlib module or installed package |
| `src/grounding.py` | `Grounder` routes every request to the picker: local first, and OpenRouter only as the A2.9 fallback. The `bbox_2d` path is retired |
| `src/focus.py` | The part branch of `_doc_target`, `_apply_ground`, `_ground_missed`, `summary`, plus the `part_pick` record |
| `src/prompts.py` | The one picker prompt |
| Docs | `docs/architecture.md` (module row), `tests/audit/SCHEMA.md` (new keys) |

Implementers may add unit tests **in new files only** (`tests/test_*.py`). Everything else under `tests/audit/`
(harness, mock, scorer, fixtures, baselines, cassettes) belongs to the verifier (Phase 3 G.6 rules). `tools/pick_eval.py`
belongs to the implementer.

## 2. Hierarchy built at parse time (`figures[]`, additive)

### 2.1 Panels: `figures[].panels[]`

Each panel is `{label, bbox, src, guessed}`. The first source that applies wins:
1. **`label`:** drawn `(a)` / `a)` / `a` labels. This is today's `_panels`, unchanged.
2. **`gutter`:** a whitespace split. A gutter is a clear band inside the figure bbox, at least 6 pt wide, crossed by no
   text line and no graphic object, that spans at least 90 % of the figure's other dimension. Gutters cut the figure
   into a grid of cells.
   - The split is used **only** when the caption names N >= 2 panels (today's `_guess_panels` letter rule) and the grid
     has exactly N non-empty cells.
   - Cells get letters in reading order: rows top to bottom, then left to right.
3. **`caption`:** today's even split (`_guess_panels`).
- No panels named anywhere: `panels` stays `[]`, and the scope is the figure bbox.
- `guessed` keeps its Phase 2 meaning: true only for `caption`.
- Every panel bbox lies inside the final figure bbox.

### 2.2 Parts: `figures[].parts[]` (new; parse cache only, never logged)

Each part is `{id, kind, n, t, label, rect, panel, attached, src}`:

| Field | Meaning |
|---|---|
| `id` | `p1`, `p2`, … in reading order within the figure |
| `kind` | `text` or `callout` |
| `n` | the callout number (1–99), else `null` |
| `t` | the label text (the cache only) |
| `label` | the label's own rect |
| `rect` | the part's **target**: where the oval goes (2.3 and 2.4) |
| `panel` | the letter of the panel holding the label's centre, else `null` |
| `attached` | true when `rect` is a graphic object, not a padded label box |
| `src` | `pdf` or `ocr` |

- **`text` parts:** one per entry of today's `labels`. The `[A-Za-z]{2,}` filter stays for text labels, so axis ticks
  stay out. `rect` = today's `ground_label` neighbourhood (`hood`), which never crosses a panel boundary.
- **`labels` is unchanged.** It still feeds `ground_label`, `device_alias` and `figure_context`. Callouts are **not**
  added to `labels`.

### 2.3 Callouts (`kind: "callout"`)

A **callout marker** is a text item inside the figure bbox whose whole text is:
- a number 1–99; or
- one circled digit (U+2460–U+2473, U+24EA, U+24F5–U+24FE, U+2776–U+2793).

It also needs **at least one** of these:
- **enclosed:** a path or image of at most 24 × 24 pt, with aspect ratio 0.6–1.6, contains the digit box with at most
  4 pt slack;
- **circled glyph:** the text is itself a circled digit;
- **leader:** an unfilled path of 1–3 straight segments has one end within 3 pt of the marker (or of its enclosure).

Unenclosed numbers with no leader (axis ticks, bar indexes, equation numbers) are never callouts. The same rules apply
to OCR labels.

**Target `rect`**, first match wins:
1. **Leader present:** the smallest graphic object (path or image, area >= 100 pt², not the marker, not the leader) that
   contains the leader's **far end** or lies within 4 pt of it. `attached: true`.
2. **No leader:** the nearest such object within 14 pt of the marker (edge distance). `attached: true`.
3. **Otherwise:** the marker box padded like a label. `attached: false`.

The rect is clipped to the marker's panel.

### 2.4 Objects

`objects[]` is today's list. Nothing changes except that each entry is assigned to a panel, the same way as parts.

### 2.5 Summary and determinism

- `parse_summary` gains `parts: {"text": n, "callout": n, "callout_attached": n}` and `panels.gutter: n`.
- The hierarchy is deterministic: two parses of the same file give identical output, except `ms` fields.
- Extra parse time per figure page <= 30 ms (p95 over the public fixtures). The document-ready check (benchmark 5.2)
  must still pass with no more retries than at B5.

## 3. Deterministic path (no model call)

Each chunk is evaluated in figure mode (today's `_doc_target` branch, after Phase 3 rule C.4). Rules apply in this
order.

### 3.1 Scope

- A panel named in **this** sentence → that panel.
- Else the held panel (`cur_panel`) → that panel.
- Else the figure.

This is exactly today's `prect` rule.

### 3.2 Callout reference

**Detection.** The sentence has a callout reference when it contains a callout noun followed by a number. Callout nouns:
`callout`, `marker`, `label`, `number`, `no.`, `item`, `circle`. The number may be written as digits, `(N)`, a number
word one–twenty, or a circled digit. A circled digit on its own also counts.

These are **not** callout references:
- `Figure N`, `Table N`, `Fig. N`, `step N`, `panel N`;
- a number followed by a unit or `%`.

**Matching.** When the sentence has a callout reference:
- word matching is **skipped** for the chunk. So "Callout 3 is the probe after the sand filter" never grabs the
  "Sand filter" label;
- the callout's scope is the panel named in the sentence, else the **whole figure**. A held panel does not restrict it,
  because callout numbers are usually unique per figure;
- exactly one callout part with that number and `attached: true` → **oval** on its `rect`, `target_src: "callout"`;
- the number appears in several panels → prefer the one in the held panel. If it is still ambiguous, or the callout is
  unattached, there is no deterministic oval and the picker runs (section 4).

### 3.3 Label match

Today's `ground_label(focus_phrase)` / `ground_label(sentence)` runs with today's scoring and the scope from 3.1. A
result draws the oval exactly as today (`label-object` / `label-pad`).

**No lost hit:** every public parts sentence hit at B5 by this path is still a hit with the same `target_src`.

### 3.4 No match

The show is today's panel box (or figure box): `cue_kind: "figure_box"`, `doc.kind: "panel"` or `"figure"`. The picker
may replace it later (section 4).

**The fallback is never an oval around the whole panel.** A panel-sized `part_oval` would count as a stray oval in
`parts.precision`.

### 3.5 Speed and offline

- Map-time cost of 3.1–3.4 is <= 5 ms per chunk. They are lookups into the parse; no subprocess.
- The deterministic path does not depend on `SPEAKEASY_GROUND` and never calls a model.

## 4. Picker (one local model call, in the background)

### 4.1 When a pick is submitted

A pick is submitted at locate time (`Speaker.put`, before playback) when **all** of these hold:
- the chunk is in figure mode and 3.2 / 3.3 drew no oval. One exception: a `device_alias` label-pad oval, which today
  triggers the VLM, also triggers a pick;
- the phrase has content words: `content_words(object_phrase(focus_phrase(sentence)))` is non-empty, or the chunk has
  an unresolved callout reference;
- the picker is enabled:
  - `SPEAKEASY_GROUND` is `auto` or `local`;
  - `SPEAKEASY_PICK` is not `off`. Its default is `on`, unless acceptance 9.4 says otherwise;
  - the document is a PDF and the doc venv is present;
- the scope has **>= 2 candidates** (4.2).

Otherwise no pick is sent, and `part_picks.skipped[reason]` counts it, with reason `too-few`, `generic-phrase` or
`disabled`.

`SPEAKEASY_GROUND=openrouter` also runs the local picker first. OpenRouter receives a pick only under the A2.9
fallback order.

### 4.2 Candidates (at most 16, from the scope only)

1. The scope's parts with `attached: true` (their `rect`). For an unresolved callout reference, the scope's callout
   parts are included even when unattached.
2. The scope's objects with area between 0.5 % and 60 % of the scope, and both sides >= 8 pt.
3. **Dedupe:** a rect with IoU >= 0.7 against an earlier kept rect is dropped.
4. **Cap at 16:** keep parts first (reading order), then objects largest first, until there are 16.
5. **Letters A–P** in reading order of the kept rects: rows by top edge (12 pt tolerance), then by x.

### 4.3 Marked image (Set-of-Mark)

- **Crop:** the scope plus a 4 pt margin, rendered by the doc-venv subprocess. Zoom 1–4, so the long side is
  <= 1024 px. It is one subprocess per pick (render + marks) with a 10 s timeout.
- **Palette:** the first of red `#E00000`, magenta `#FF00FF` and blue `#0050FF` whose near-colour pixels cover < 1 % of
  the crop. When none qualifies, use red. The prompt names the colour used.
- **Outlines:** each candidate gets a rectangle outline, 2 px (3 px when the long side is >= 800 px).
- **Tags:** a white-filled box with a palette-colour border and a bold palette-colour letter, glyph height
  `max(14 px, 2.5 % of the long side)`.
  - Placement is **deep inside** the candidate: the point on a 4 px grid that is farthest from the candidate's edges
    and from every smaller candidate.
  - Candidates are tagged **smallest first**. A tag never overlaps an earlier tag. When no free spot fits inside, the
    tag goes outside, against the candidate's top-left corner.
- **Determinism:** the same parse, scope and Pillow/pypdfium2 versions give byte-identical PNG bytes.

### 4.4 Request

`POST <base>/api/chat`, where `<base>` = `SPEAKEASY_LLM_URL` without a trailing `/v1` (default
`http://localhost:11434`).

```json
{"model": "<vision_model()>", "stream": false, "keep_alive": "<llm_client.keep_alive()>",
 "messages": [{"role": "user", "content": "<PICK_PROMPT with colour, letters and phrase>", "images": ["<base64 png>"]}],
 "options": {"temperature": 0, "num_predict": 1}, "logprobs": true, "top_logprobs": 20}
```

- `PICK_PROMPT` lives in `prompts.py`. It names the colour and the valid letters (`A`–`<last>`), embeds the phrase, and
  asks for the letter only.
- The phrase is `object_phrase(focus_phrase(sentence))`, with quotes and braces stripped, at most 120 chars. For a
  callout reference, it is the focus phrase including the callout words.
- **Concurrency:** one pick in flight per run. Picks run FIFO, and none is cancelled because a later chunk started
  (bench determinism, 7.2). Each pick has a deadline of **15 s** from submit. A pick still queued at its deadline is
  never sent (`timeout`).
- **Prewarm:** when `vision_model()` differs from the text model and the matched page has a figure, the existing
  load-only warm request fires once per run.

### 4.5 Answer → decision

1. Take `reply.logprobs[0].top_logprobs`. If it is missing or empty → **`no-logprobs`**.
2. For each entry, strip whitespace from `token`. Keep entries equal to a **valid uppercase letter** (lowercase never
   counts, because `a` is an article). Sum `exp(logprob)` per letter (so `"A"` and `" A"` merge). This gives q_i.
3. `letter_mass = sum(q_i)`. If `letter_mass < 0.5` → **`off-menu`**.
4. `p_i = q_i / letter_mass`. `p_top` = max p_i. `p_margin = p_top − second p_i` (0 when only one letter appears).
5. If `p_top >= PICK_MIN_P` → **`accepted`**: the oval goes on that candidate's rect, `target_src: "pick"`, label
   suffix ` (grounded)`. Otherwise → **`low-confidence`**: the show keeps its deterministic cue (the panel or figure
   box, or the label-pad oval).

`PICK_MIN_P` is a constant in `src/partpick.py`, chosen **on the public set only** (8.2) and then frozen.

### 4.6 Applying

An accepted pick updates the chunk's show only while that chunk is the current one (today's late-update rule). The same
`seq` rules apply:
- answered after the next chunk started → `drop: "superseded"`;
- the capture fit never landed → `drop: "no-fit"`.

A pick never delays a show and never touches the first-highlight path.

## 5. Fallbacks and failure cases

| Case | Behaviour |
|---|---|
| Ollama down (connection refused, DNS, HTTP 5xx) | `unavailable`. The exception class goes into `ground_errors`. **Breaker:** no further local pick is sent in this run; later picks log `unavailable` with `called: false`, unless `SPEAKEASY_GROUND=openrouter` sends them to OpenRouter (A2.9). Speech and every show are unaffected |
| Model not pulled (HTTP 404) | Same as down, `unavailable` |
| Reply has no `logprobs` / `top_logprobs` (old Ollama, a non-Ollama server, the default mock) | `no-logprobs`, and the same breaker (with the same OpenRouter fallback) |
| Letters below 50 % of the top-20 mass | `off-menu`; the deterministic cue stays |
| `p_top < PICK_MIN_P` | `low-confidence`; the deterministic cue stays |
| Malformed JSON, render subprocess fails or times out (10 s) | `error` (class counted); the deterministic cue stays |
| No answer by the 15 s deadline (cold model load included) | `timeout`; a late reply is ignored |
| Fewer than 2 candidates, or a generic phrase | No request (`skipped`) |
| `SPEAKEASY_GROUND=off`, `SPEAKEASY_PICK=off`, no doc venv, or a Markdown document | No request (`skipped.disabled`); the deterministic path still runs |
| Chunk already superseded when the answer arrives | The decision is logged; nothing is drawn |
| Run stopped (`STOP`) | Picks in flight are abandoned; at most one `part_pick` record per pick, written at summary time |

Every failure falls back to the Phase 2 / deterministic cue for that chunk. No failure raises out of the picker thread.

## 6. Events and logs (no text anywhere)

### 6.1 Focus log `part_pick` record

One record per pick, written once, when the decision is final (applied, dropped, or at summary). It is never copied to
`focus.json`.

```json
{"state": "part_pick", "sent": 7, "sentence_id": "s12", "fig": "Figure 2", "panel": "b", "scope": "panel|figure",
 "candidates": 9, "cand_rects": [[x0, y0, x1, y1], ...], "chosen_index": 3, "p_top": 0.93, "p_margin": 0.88,
 "letter_mass": 0.99, "palette": "red", "called": true, "outcome": "accepted", "applied": true, "drop": null,
 "ms": 412, "queue_ms": 0, "model": "qwen3-vl:4b-instruct", "provider": "local", "ts": 0.0, "pid": 0}
```

- **`outcome`:** `accepted`, `low-confidence`, `off-menu`, `no-logprobs`, `unavailable`, `error` or `timeout`.
- **`chosen_index`:** set for `accepted` and `low-confidence` (the argmax), else `null`.
- **`p_*`:** 2 decimals; `null` when there are no logprobs.
- **`ms`:** render + request time. `queue_ms` is the FIFO wait.
- **`cand_rects`:** page space, 0.1 pt. This is geometry only; the bench scores from it.
- **`drop`:** `superseded`, `no-fit` or `null`.
- No phrase, label text, prompt, image path or model output appears in the record, **whatever `SPEAKEASY_LOG_TEXT`
  is**.

### 6.2 Runs log

- **`part_picks`** (new; present when any pick was considered):

  ```json
  {"requests": n, "called": n, "accepted": n, "applied": n, "outcomes": {...},
   "skipped": {"too-few": n, "generic-phrase": n, "disabled": n}, "ms": {"p50": int, "max": int},
   "threshold": PICK_MIN_P, "breaker": null | "unavailable" | "no-logprobs"}
  ```

- **`ground_miss_reasons`:** new reasons `low-confidence`, `off-menu`, `no-logprobs` and `unavailable`
  (`out-of-scope` comes from A2.6). The existing reasons `no-fit`, `superseded`, `timeout` and `error` keep their
  meaning. `no-box` and `too-large` are retired along with `bbox_2d` (A2.9).
- **`focus_methods`:**
  - `ground-pick` counts applied accepted picks, from either provider;
  - `ground-vlm` is retired;
  - `ground-miss` counts `error + low-confidence + off-menu + no-logprobs + unavailable + out-of-scope`.
- **Invariant:** the sum of `ground_miss_reasons` + `ground-pick` = picks with a `part_pick` record. A pick that fell
  back to OpenRouter is one pick: one record, with the final provider.
- **`ground_provider`:** the provider of the last answered pick (A2.9).
- **`doc_parse` summary** gains the 2.5 counts.

### 6.3 Show record

- New `doc.target_src` values: `callout` and `pick`.
- `cue_kind` is unchanged: a picked or callout part is `part_oval`; a fallback panel is `figure_box`.

## 7. Benchmark (verifier-owned, under `tests/audit/`)

### 7.1 Gated mode: unchanged, so the mock has nothing to give away

- The environment is exactly the Phase 2 scrub: `SPEAKEASY_GROUND=off`. **The picker never runs.** The parts score
  comes from the deterministic path (section 3) only.
- That path takes no input from the mock: the mock only streams the script.
- The harness asserts that the mock log has **no** `/api/chat` POST and that the focus logs have **no** `part_pick`
  record. Either one → exit 3 `picker ran in gated mode (case …)`.
- `config` and fixtures are unchanged, so B5 stays valid.

### 7.2 Pick mode: `run_bench.py --pick replay|proxy` (scored separately, never gated)

- **Environment:** gated mode plus `SPEAKEASY_GROUND=local`, `SPEAKEASY_PICK=on` and
  `SPEAKEASY_VISION_MODEL=qwen3-vl:4b-instruct`. The picker's `/api/chat` goes to the same mock port.
- **The mock's `/api/chat`:**
  - `MOCK_PICK=none` (the default, and gated mode): the reply has no `logprobs`.
  - `replay`: the reply comes from a **cassette**. The key is
    `sha256(model ‖ 0x00 ‖ prompt ‖ 0x00 ‖ sha256(png bytes))`. Each entry stores **only** the valid-letter
    `top_logprobs` plus one `{"token": "<other>", "logprob": log(remaining mass)}` entry. No model text and no prompt
    are stored.
    - A key that is not in the cassette gets a reply without logprobs and adds 1 to `pick.replay_miss`.
  - `proxy`: forwards to the real local Ollama (`MOCK_PICK_UPSTREAM`, loopback only) and records into the cassette.
- **Cassettes:**
  - public: `tests/audit/bench/pick_cassette.json` (committed);
  - hidden: `<holdout>/pick_cassette.json` (never committed);
  - header: `{model, model_digest, ollama_version, pypdfium2, pillow, commit, recorded_at}`.
- **Scoring** reads the focus-log `part_pick` records and attributes each to a script sentence through its `sent`
  (benchmark 5.3). It scores the **decision**, not the draw time, because bench chunks last 50 ms. The output is a new
  block `sets.<set>.pick`:
  - `requests`, `called`, `by_outcome`, `replay_miss`;
  - **`precision`** = 100 × correct / accepted. *Correct* = an accepted pick whose `cand_rects[chosen_index]` centre
    lies inside the attributed sentence's GT part bbox. An accepted pick on a non-parts sentence is incorrect.
  - **`recall`** = 100 × correct / picks on parts-scored sentences.
  - **`candidate_recall`** = 100 × (picks on parts sentences where any candidate centre lies in the GT bbox) / (those
    picks). This is the ceiling.
  - **`parts_with_pick`** = `{score, precision}`: the gated-mode rules, with each accepted pick counted as that chunk's
    oval (replacing a fallback box). This is what a user with Ollama sees, independent of timing.
  - `ms` p50/p95 (informational).
- **Determinism:** with `replay`, everything in `pick` except `ms` is determinism-covered (`--check-determinism`).
- **Stale cassette:** `replay_miss > 0` → `"stale": true` and a stderr line
  `pick cassette stale: re-record with tools/pick_eval.py --record`. The hidden block is aggregates only.

### 7.3 Live eval: `tools/pick_eval.py` (optional, never gated)

- **Command:** `python3 tools/pick_eval.py [--set public|hidden] [--record] [--sweep] [--ollama URL]`.
- **What it does:** runs `run_bench.py --pick proxy` against the real local Qwen on the chosen set. It prints the `pick`
  block, warm latency p50/p95, and the first-call (load) time.
- **`--sweep`** prints, for T in {0.40, 0.45, …, 0.95}: accepted, precision and recall. This is allowed on the **public
  set only**.
- **`--record`** writes that set's cassette. For hidden, it prints aggregates only, and no per-case line ever goes to
  the terminal or the repo.
- **Preflight:**
  - Ollama reachable;
  - the model present (`/api/tags`);
  - one probe request returns `top_logprobs`.

  Any failure → exit 2 with the cause.
- Temp homes only; it never touches `~/.speakeasy`.

## 8. Privacy and limits

### 8.1 Privacy

- Picks go to the configured local endpoint (`<base>/api/chat`).
- OpenRouter gets a pick only under the A2.9 fallback. That needs `SPEAKEASY_GROUND=openrouter`, a key, and a local
  picker that returned `unavailable` or `no-logprobs`. It then receives only the marked crop and the fixed letter
  prompt (A2.9 privacy).
- What is sent: one figure-scope crop with marks, and the short object phrase. No other document text.
- The crop is a temp file `speakeasy_cap_pick_<pid>_<n>.png` under `TMPDIR`. It is deleted in `finally` on every path,
  and is never kept, even with `SPEAKEASY_KEEP_CAPTURE=1`. `privacy_audit.py` finds none after a run.
- Logs, `bench.json`, cassettes and tool output carry ids, numbers, enums, hashes and page geometry only.

### 8.2 Threshold choice (once, then frozen)

- **Rule:** `PICK_MIN_P` = the smallest T in {0.40, 0.45, …, 0.95} at which the public live sweep gives precision >= 90
  with >= 5 accepted picks.
- **If no T qualifies:** T = 0.95 and the picker ships default-off (`SPEAKEASY_PICK=off`).
- The chosen value and the public sweep table are appended to this file as **Amendment 1**. The threshold is never
  re-chosen against hidden numbers.

## 9. Acceptance

1. **Gate (gated mode)**, run on the Phase 5 commit:
   - `run_bench.py --set all` then `bench_gate.py` against B5 gives `GATE PASS`;
   - `parts.score >= 70.00` on both sets;
   - no other gated metric (`boxes.score`, `boxes.precision_50`, `lines.score`, `parts.precision`) drops by more
     than 1.00;
   - first-highlight p50/p95 < 300 ms on both sets;
   - `--check-determinism` exits 0.
2. **No lost hit:** every public parts sentence hit at B5 is still a hit (verifier diff of `cases[].misses`). Hidden:
   `parts.hits` and `by_style` aggregates do not fall in any style except callout and raster, which can only rise.
3. **Pick replay:**
   - `run_bench.py --set public --pick replay --check-determinism` exits 0 with `replay_miss == 0`;
   - public `pick.precision >= 90`, which holds by construction (8.2), and `parts_with_pick.precision` is no lower than
     gated-mode `parts.precision` − 1.
4. **Hidden pick replay** (aggregates only): `pick.precision >= 80` → the picker ships default-on. Otherwise it ships
   default-off, and the phase reports the number without re-tuning.
5. **Fallback runs** (verifier; the mock in the named mode, one public case with a pick-eligible chunk):
   - port closed;
   - `MOCK_PICK=none`;
   - a reply delayed 16 s;
   - malformed JSON.

   Each run must exit 0, speak every chunk, keep first-highlight unchanged within noise, log the outcome section 5
   names (with `breaker` set where section 5 says so), and leave no temp PNG.
6. **Unit tests** (implementer, new files). They cover:
   - letter merge (`"A"` + `" A"`), lowercase excluded, `letter_mass` boundary 0.5;
   - `p_top == PICK_MIN_P` accepts;
   - the 16-candidate cap and IoU dedupe;
   - byte-identical PNG on a repeat;
   - tags never overlapping;
   - callout-reference vectors: `Callout 3`, `callout (3)`, `marker three`, `③` are references; `Figure 3`, `step 3`,
     `3 mm` are not;
   - an enclosed or leader number is a callout; an axis tick is not;
   - leader far-end target selection.
7. **Regression suites** keep their results (Phase 3 G.5): readability, logging, boxacc, stream, `test_score.py`,
   `privacy_audit.py`, CI smoke, and `swift test`. The verifier widens key pins for the 6.x keys only.
8. **Nothing under `~/.speakeasy` changes** during any bench or `pick_eval` run.
9. **If 70 is not reached on a set:**
   - no fixture-specific thresholds, no reading of hidden per-case files, no edits to fixtures, GT, specs or the
     scorer;
   - report the score, the miss reasons, and the public misses by cause (panel scope, callout detection, leader target,
     label match, hood, wrong page).

## 10. Out of scope

- Raster parts without OCR (the bench has no tracker; public 0/1 and hidden 0/3 stay headroom).
- The hidden `wrong_page` misses (3), Markdown documents, and vision runs.
- OpenRouter as a first choice for picks: it is a fallback only (A2.9). The `bbox_2d` grounding path is retired.
- Using the Phase 4 detector for panels or parts (it is used only as the replacement figure box in A2.4).

## Amendment 1: picker threshold (frozen, round 1)

**Public live sweep, round 1** (local `qwen3-vl:4b-instruct`, abstain prompt from A3.2(e)):
- 19 picks in total, of which only 4 were on parts-scored sentences. The other 15 were on figure-overview sentences.
- No T in {0.40, …, 0.95} reaches precision >= 90 with >= 5 accepted. The best was 3 accepted at precision 66.67.

**Ruling:**
1. **Frozen now:** `PICK_MIN_P = 0.95`, picker **default-off** (`SPEAKEASY_PICK` default `off`). This is the 8.2
   no-qualifier rule. The reason: most picks fired on whole-figure sentences, where no answer can be correct, and the
   few remaining picks are too few to show 90 % precision.
2. **One re-sweep is allowed**, after Amendment 2 lands, with the **narrower activation rule** defined below. The rule
   is set before the re-sweep, so it is not tuned to it.
   - **Narrower activation rule:** a pick is submitted only for a chunk that names a specific object under A2.5 (a
     label word, a callout reference, or an object-phrase content word that is not in the caption title). Every other
     chunk is `skipped["whole-figure"]`. This removes the overview-sentence picks.
   - **Re-sweep conditions:** the same 8.2 rule (public only; precision >= 90 with >= 5 accepted; smallest qualifying
     T), and the same prompt, candidates and model.
   - **A T qualifies:** it replaces 0.95 here as Amendment 1b. Default-on then still needs the 9.4 hidden replay check
     (>= 80).
   - **No T qualifies:** the picker stays default-off for Phase 5, final. The picker code, events and pick mode still
     ship and are still verified (9.3 with default-off, 9.5).
3. **The gate is not affected.** The 70 target is met in gated mode, where the picker is off (round 1: public 75.38).
4. **Final (round 2, recorded in Amendment 5):** the re-sweep under the narrower rule reached at best 5 accepted at
   precision 80.0 (T 0.40–0.75). No T qualifies. `PICK_MIN_P = 0.95` and the picker is **default-off for Phase 5,
   final**. There is no Amendment 1b, and the 9.4 hidden default-on check does not apply.

## Amendment 2: Read-area and real-use fixes

Source: the user's installed build `91166c4`, diagnosed from their logs. These behaviours are **in Phase 5 scope**. They
apply to every PDF run unless an item names Read area only. *Area run* = `--capture-method area`. *Area rect* = the
drawn rectangle in page space. *Stand-in cue* = a cue whose target is the whole selection, the whole area or the capture
rect, used because nothing narrower was found.

### A2.1 The area rect is the selection region (area runs on a PDF)

- When `area_pdf` succeeds, the page rect `area_pdf_text` already computes (`document.py`, from the anchor fit) is
  **kept**. The runs log records it as `area_page_rect` (page space, 0.1 pt) with `area_region_src: "area-fit"`.
- For the rest of the run, it **replaces** the line-derived `_sel_region`:
  - **Figure visibility:** a figure or table is visible only when its bbox overlaps the area rect.
  - **Figure mode:** decided by A2.2.
  - **Eligible page lines:** a page line may join any band (text band, block, hold, table row) only when its centre
    lies inside the area rect. **No line outside it ever joins a band**: page headers, footers, page numbers and the
    other column included.
- **No `area_pdf` fit** (any reason other than `ok`): the region is the capture rect mapped through the capture fit, when
  one lands (`area_region_src: "capture-fit"`). Otherwise there is no page region (`"none"`): screen-only cues, clamped
  to the area as today.
- **Clamp collapse:** a cue that the area clamp would shrink below 8 pt is **dropped**. Nothing new is drawn, the
  previous cue stays, and `area_dropped` += 1. It is **never** replaced by the whole area. Today `show` substitutes
  the clip rect; that is removed.

### A2.2 A figure inside the area enters figure mode (area runs)

- An area run is in figure mode for figure F (page matched, `kind: figure`, not degenerate under A2.4) when **either**:
  - **`area-overlap`:** F's bbox ∩ area rect covers >= 50 % of F's bbox **or** >= 50 % of the area rect; or
  - **`area-labels`:** >= 50 % of the area's eligible text lines lie inside F's bbox (the area is mostly F's labels).
- When several figures qualify, the one with the largest intersection wins.
- This holds **whatever the amount of text** in the area and whatever the caption overlap. The Phase 2 rules (caption
  >= 40 % of the selected words; vision + `_sel_region` overlap) stay as they are for non-area runs.
- **Prose in the same area:** a chunk that aligns verbatim to area prose lines still gets its text band (Phase 3 C.4
  verbatim rule). All other chunks use the figure / panel / part cues.
- **Runs log:** `doc_figure_mode` (existing) plus `figure_mode_src`: `caption`, `vision`, `area-overlap`,
  `area-labels` or `null`.

### A2.3 No stand-in cues

- **Applies to:** figure mode, vision runs, explain/summary modes, and every area run.
- **Rule:** a chunk with no aligned span and no figure, panel, part or table-row target does this, in order:
  1. holds the current cue (map `method: "hold"`);
  2. if there is no current cue: in figure mode, shows the figure box (or the current panel's box);
  3. otherwise nothing new is drawn (map `method: "none"`, no show).
- **Never allowed:**
  - a band over every selection or area line (Phase 3 E "whole block");
  - the area rect;
  - the capture rect.
- **Phase 3 E "whole block" row is replaced** in these modes by the rule above. It still applies to a plain text
  selection in read-through mode.
- **One exception:** the selection or area holds exactly one source sentence, so the block is that sentence's lines.
- **Runs log:** `stand_in_blocked: n` (chunks where the old rule would have drawn a stand-in) and
  `no_target_chunks: n`.

### A2.4 Degenerate (hairline) figure boxes

- A figure whose bbox is shorter than 10 pt in height **or** width is `degenerate: true` in the parse. This is typically
  `bbox_src: "caption-only"`, the 1-pt strip.
- What happens to it:
  1. **Detector box available:** the parse substitutes a Phase 4 detector figure block on the same page. It must sit on
     the caption's body side (above for figures) within a 60 pt gap and overlap the caption horizontally by >= 50 %.
     The figure keeps `degenerate: false` and gets `bbox_src: "detector-substitute"`.
  2. **Otherwise:** the figure is **never** drawn as a `figure_box`, never a grounding or pick scope (pick
     `skipped["degenerate-figure"]`), never counted as visible for figure mode (A2.2), and its chunks fall back to text
     cues (Phase 3 bands).
- **Runs log:** `figures_degenerate: n` for the matched page. `parse_summary.bbox_src` gains `detector-substitute`.

### A2.5 A whole-figure sentence keeps the figure box

- **Whole-figure sentence:** a sentence that refers to the figure (`Figure N`, `Fig. N`, `this/the figure`, `diagram`,
  `chart`, `plot`, `image`) and names **no specific object**. It names a specific object when **one** of these holds:
  - a label match (3.3);
  - a callout reference (3.2);
  - its object phrase has a content word that is not in the caption's first sentence (the title). So "Figure 2 shows the
    sensor drawing", with the title "Sensor drawing.", names no object.
- **Behaviour:**
  - it keeps the figure box (or the named panel's box);
  - **no** grounding or pick request is submitted (`skipped["whole-figure"]`);
  - a late answer from an earlier chunk never turns its box into an oval;
  - an oval replaces a figure or panel box only for a chunk that names a specific object. This applies to the
    picker (local or OpenRouter fallback) and every late update.

### A2.6 Out-of-scope answers are rejected

- An accepted pick (from either provider) is **rejected** when its centre lies outside:
  1. the scope (the current panel, **guessed panels included**, else the figure); or
  2. for a request that started from a label match (the `device_alias` label-pad case), that label's region: the
     request clip.
- **Outcome:** new `ground_miss_reasons` value **`out-of-scope`**; `part_pick.outcome: "out-of-scope"`. It counts in
  `ground-miss` and in the 6.2 invariant.
- The deterministic cue stays.

### A2.7 First draw after a scroll uses the current fit (tracker)

- **Rule:** when a show carries `doc.lines` or `doc.target` and the tracker holds a page→screen fit (`anchors`, or
  `event-transform` adjusted by the scroll since the hotkey), the **first draw** maps the page geometry with **that
  fit**. It does not use the capture-time screen rect or `rects` plus the offset. This covers figure boxes, panel boxes
  and ovals too.
- **No fit** and the view scrolled since the capture: the first draw is the capture rect plus the offset (today), and a
  targeted scan is requested at once (`wantScan`). The Phase 3 D.4 check then applies.
- **Draw record:** `first_draw_src`: `page-fit`, `capture+offset` or `held`.

### A2.8 Area reads that cannot match a page say why

`area_pdf` becomes an enum, with no text:

| Value | When |
|---|---|
| `ok` | as today |
| `doc-timeout` | parse not done within the 4 s wait |
| `doc-unresolved` | the document hint resolved to no file |
| `doc-failed` | the parse failed |
| `no-page` | the parse is done but no page matched |
| `markdown` | the source is Markdown |
| `no-fit` | as today |
| `empty` | as today |
| `error` | an exception; its class goes in `area_pdf_error` |

- `no-doc` and the `error: <Class>` string are retired. `area_pdf_ms` stays.

### A2.9 Phase 5 part behaviour in area runs; the OpenRouter decision

- **Area runs** get sections 2–6 unchanged, with one change: the scope is the panel (or figure) **∩ the area rect**.
  Candidates and callout matches whose centre lies outside the area are excluded.
- **`SPEAKEASY_GROUND=openrouter`: local first** (the user's decision). The letter picker always runs on the local Qwen,
  whatever `SPEAKEASY_GROUND` is (`auto`, `local` or `openrouter`). The legacy single-box `bbox_2d` path is **retired**
  on every setting.
- **Fallback order, per pick:**
  1. **Local Qwen** (`<base>/api/chat`, section 4.4). It is always tried first, until the local breaker trips.
  2. **OpenRouter**, only when **all** of these hold:
     - `SPEAKEASY_GROUND=openrouter`;
     - `provider.cloud("ground")` is usable (key present, valid URL);
     - in this run, the local picker returned **`unavailable`** or **`no-logprobs`** (the breaker in section 5).

     The pick that tripped the breaker is re-sent to OpenRouter once, and every later pick in the run goes straight
     to OpenRouter.

     Local outcomes `low-confidence`, `off-menu`, `error`, `timeout` and `out-of-scope` **never** fall back. They
     keep the deterministic cue: there is no second opinion.
  3. **Deterministic cue.** OpenRouter missing (no key, bad URL), or its request fails (HTTP error, timeout): outcome
     `unavailable` with `provider: "openrouter"`, an OpenRouter breaker for the rest of the run, and the deterministic
     cue stays.

  With `auto` or `local`, step 2 never happens: an `unavailable` local picker means no picks for the rest of the run.
- **The OpenRouter request** is the same marked PNG and the same `PICK_PROMPT`, sent to `<or_url>/chat/completions`.
  - Body: model = `provider.cloud("ground")` model, `temperature: 0`, `max_tokens: 1`, `logprobs: true`,
    `top_logprobs: 20`.
  - Timeout: 10 s. The pick's deadline becomes submit + 25 s. The answer still applies only while its chunk is current
    (4.6).
  - **The reply has `logprobs`:** 4.5 applies unchanged, with the same `PICK_MIN_P`.
  - **It does not:** the first non-space character of the content must be a valid letter. Then the outcome is
    `accepted` with `p_top: null`, `p_margin: null` and `confidence: "letter-only"`. Anything else is `off-menu`.
  - A2.5 and A2.6 apply to OpenRouter answers exactly as to local ones.
- **Events: which provider answered.**
  - **`part_pick` gains:**
    - `provider`: `local` or `openrouter`, whichever produced this record's outcome;
    - `fallback_from`: `null`, `unavailable` or `no-logprobs`; this is the local outcome that sent this pick to
      OpenRouter, and it is `null` for local answers and for later picks sent straight to OpenRouter after the breaker;
    - `confidence`: `logprob`, `letter-only` or `null`.
  - **Runs `part_picks` gains:**
    - `by_provider: {"local": n, "openrouter": n}`;
    - `fallback: {"unavailable": n, "no-logprobs": n}`, counting local breaker trips that moved picks to OpenRouter;
    - `breaker`, which may now be `{"local": <reason>, "openrouter": <reason>|null}`.
  - **Other runs keys:**
    - `ground_provider` = the provider of the last answered pick;
    - `ground_errors` counts exception classes from both providers;
    - `ground_box_convention`, `ground-vlm`, `no-box` and `too-large` are no longer written. They are retired with
      `bbox_2d`.
  - No text, key, URL or model output goes in any of these.
- **Privacy (OpenRouter fallback):** OpenRouter receives **only** two things:
  - the marked crop: the scope region (panel or figure, ∩ area rect in area runs) with outlines and letters;
  - the fixed letter prompt: the `PICK_PROMPT` template, whose only variable parts are the mark colour, the letter
    range and the ≤ 120-char object phrase.

  It never receives other document or selection text, the page image outside the scope, the file name, path or title,
  logs, or ids. Nothing is sent to OpenRouter with `auto`, `local` or `off`, or without a key.
- **Bench and tests:**
  - The gated bench is unaffected (`SPEAKEASY_GROUND=off`). Pick mode uses `local` only.
  - The fallback order is covered by implementer unit tests with a stubbed transport. They cover: local `unavailable` →
    OpenRouter called once and then for every later pick; local `no-logprobs` → OpenRouter; local `low-confidence` →
    no OpenRouter call; `auto` with local down → no OpenRouter call; OpenRouter without logprobs → `letter-only`;
    OpenRouter failure → `unavailable`, provider `openrouter`; the request body holds exactly one image and the prompt
    template.

### A2.10 Failure cases (additions to section 5)

| Case | Required |
|---|---|
| Area over a figure plus a paragraph, narration mostly paraphrased | Figure mode (`area-overlap`). Verbatim prose chunks get bands; paraphrased chunks hold or get figure, panel or part cues; no stand-in |
| Area whose band would include the page header or footer just outside it | The line is never in the band |
| Area cue clamped below 8 pt | Dropped; the previous cue stays; never the whole area |
| Caption-only hairline figure, detector box present | The detector box is used for the figure box and the scope |
| Caption-only hairline figure, no detector box | No `figure_box`, no request; text cues |
| "Figure 3 shows the overall setup." | Figure box; no oval; no request |
| Late pick or grounding arrives for a whole-figure chunk | Ignored; the box stays |
| Grounded box in panel b while the scope is guessed panel a | Rejected, `out-of-scope` |
| User scrolls between capture and a show | First draw from the current page fit |
| Area read with Markdown, an unresolved hint, or a slow parse | `area_pdf` = `markdown`, `doc-unresolved` or `doc-timeout`; the OCR text is read as today |

### A2.11 Acceptance (additions to section 9)

1. **Hard invariants**, checked by the verifier on the bench (public and hidden) and in the area suite. Each must be
   **0**:
   - `area_leak`: a band rect covering a page line whose centre is outside the area rect;
   - `stand_in`: a show whose target is the selection union, area rect or capture rect (IoU >= 0.9), other than a
     `figure_box` whose `doc.target` equals the figure or panel bbox, and other than a one-sentence selection;
   - `hairline_draw`: a `figure_box` with height or width < 10 pt;
   - `whole_figure_oval`: a `part_oval` on a chunk scored `figure` (A2.12).
2. **Area figure mode:** `figure_mode_src` is `area-overlap` or `area-labels` on 100 % of the area-over-figure cases.
3. **`out-of-scope`:** unit tests with a box centred in a sibling panel and in a guessed panel are rejected; the
   invariant in 6.2 holds.
4. **Tracker:** a `swift test` (new file) feeds a show with `doc.target`, then a 300 pt scroll plus a refit. The first
   draw equals the fit-mapped rect within 2 pt, and `first_draw_src == "page-fit"`.
5. **`area_pdf` enum:** fallback runs produce each of `doc-timeout`, `doc-unresolved`, `no-page` and `markdown`; no
   value carries text.
6. **The section 9 gate still holds:** parts >= 70 on both sets, no other gated metric drops by more than 1, and
   first-highlight < 300 ms.

### A2.12 Bench and hidden coverage (verifier-owned)

New fixtures go in through the Phase 2 rules: specs, the validator, a new manifest. The baselines are re-recorded as B5'
**after** the section 9 gate passes against B5, so the B5 gate is never mixed with new fixtures. Each set gets its own
texts (hidden: different specs).

- **Area-read cases:**
  - `cases.json` gains `"capture": {"method": "area", "area_page_rect": [x0, y0, x1, y1]}`.
  - The harness writes `--line-bounds` as OCR-like lines (the GT lines inside the area mapped by G0, PDF text as the
    OCR text), so `area_pdf` can fit without a tracker.
  - At least 4 area cases per set over figures, with mostly paraphrased narration (>= 60 % of script sentences not
    verbatim). At least 2 of them have a header or footer line within 20 pt outside the area. At least 1 area is mostly
    figure labels with little prose.
- **Hairline figure:** at least 1 per set whose graphics the heuristic cannot pair (e.g. a figure made only of text, or
  raster tiles below the size floors). It is run with and without the layout model.
- **Whole-figure sentences:** at least 3 per set, with the new GT score `"figure"` (hit = a `figure_box` whose
  `doc.target` IoU with the GT figure is >= 0.75 and no `part_oval`). They are reported as `figure_cues`; any oval on
  them also counts against `parts.precision`.
- **Scroll-then-draw:** tracker only (A2.11 item 4) plus `tests/audit/tracker`. Not in the bench, because the bench
  runs without a tracker.
- **Reported per set** (hidden: aggregates only): `area` = `{cases, figure_mode, area_leak, stand_in}`,
  `hairline_draw`, `figure_cues` = `{score, n}`, `area_pdf` reason counts.

## Amendment 3: round-1 rulings

Round 1 status, uncommitted, public, gated mode:
- parts 75.38 (49/65), precision 84.48, boxes 100, lines 98.63, first highlight 38 / 58 ms;
- without the layout model, parts is also 75.38.

Round 1 built A2.9 the old way and did not build Amendment 2. Where an amendment and an earlier section disagree, the
amendment wins.

### A3.1 Threshold

See Amendment 1: default-off at 0.95, with one re-sweep allowed under the A2.5 activation rule after Amendment 2.

### A3.2 Implementer deviations

| Deviation | Ruling | Contract text it replaces or adds |
|---|---|---|
| (a) A leader is at least 6 pt long and points away from the marker | **Accepted** | 2.3 *leader*: a segment of >= 6 pt whose far end is farther from the marker centre than its near end. Shorter segments and segments pointing back are marker outline, not leaders |
| (b) The enclosure is larger than the digit box on both axes | **Accepted** | 2.3 *enclosed*: the enclosing shape is strictly larger than the digit box in width **and** height (the digit's own glyph path never encloses it) |
| (c) Callout targets are < 60 % of the figure area | **Accepted** | 2.3 *target*: an object covering >= 60 % of the figure bbox (a frame or background) is not a target. The next rule in the 2.3 order applies |
| (d) Gutter detection ignores shapes covering >= 60 % of the figure | **Accepted** | 2.1 *gutter*: such shapes (frames, backgrounds) do not block a gutter. Everything else in 2.1 is unchanged |
| (e) `PICK_PROMPT` adds "If no single region shows it, answer 0.", and an abstention maps to `off-menu` | **Accepted** | 4.4 / 4.5: the abstain clause is part of the fixed prompt. `0` is a non-letter token, so its mass counts only in `1 − letter_mass`. A top-ranked `0` (or `letter_mass < 0.5`) gives `off-menu`; no new outcome value. Cassettes keep `0` inside the `<other>` mass. The A2.9 privacy note is unchanged: the prompt is still a fixed template plus the object phrase |

### A3.3 Replay determinism (one of three replay passes reported stale)

Both sides change. Each one alone is not enough.

1. **Verifier: score the measured run only.**
   - `replay_miss`, `stale` and the pick determinism check count picks from **measured runs only**. Warm-up runs
     exist only to fill the cache (benchmark 5.2) and are never scored.
   - The mock tags each request with the run id it came from: the harness passes `--run-id`, and the app sends no
     extra field. That is how warm-up requests are told apart.
   - Warm-up misses are reported as `replay_miss_warmup` (informational, never stale).
2. **Implementer: never pick from a partial parse.**
   - A pick may be submitted only when the matched page has `figs_done: true` and the Phase 5 hierarchy (panels,
     parts) is present. A chunk located before that is `skipped["doc-not-ready"]`, with no pick and no request.
   - So each chunk's scope, candidates and image depend only on the final parse, never on parse timing. This also
     fixes the same defect in real use: a pick against a half-built hierarchy picks from the wrong candidates.
3. **Acceptance (adds to 9.3):** three consecutive `--pick replay` passes on public with `replay_miss == 0` in measured
   runs and identical `pick` blocks (excluding `ms`).

### A3.4 Panel labels drawn below their panels (arx-2406 c07)

**In scope**, as a general extension of 2.1 source 1. It is not a fixture fix.

- **Layout detection, per figure.** The figure uses the **labels-below** layout when **every** drawn panel label meets
  both conditions:
  - it has graphic content directly above it, in its column, within 24 pt;
  - it has **no** graphic object below it inside the figure before the next label row (or the figure bottom).

  Otherwise the figure uses today's labels-above layout (`_panels`).
- **Panel bboxes under labels-below:**
  - **vertically:** from the bottom of the previous label row + 2 pt (or the figure top) down to the label's bottom;
  - **horizontally:** from the midpoint to the left neighbour label in the same row (or the figure's left edge) to
    the midpoint to the right neighbour (or the right edge).
  - `src: "label"`, `guessed: false`.
- **Mixed or ambiguous figures** (some labels above, some below): labels-above, as today.
- **Guards:**
  - **No lost hit:** 9.2 applies to every currently hit public sentence.
  - **Hidden:** aggregates may not fall.
  - **Unit tests** (new file) cover a 2×1 and a 2×2 synthetic figure in each layout, plus one mixed figure.
  - No threshold may be chosen per fixture (9.9).

### A3.5 OpenRouter

**Confirmed: A2.9 (local first) overrides round 1's OpenRouter behaviour.** Round 2 must:
- remove the `bbox_2d` path on every `SPEAKEASY_GROUND` setting, together with its runs keys and miss reasons (6.2 as
  amended);
- run the local picker first even with `SPEAKEASY_GROUND=openrouter`;
- send the same marked image and fixed prompt to OpenRouter only after a local `unavailable` or `no-logprobs` (the
  breaker);
- record `provider`, `fallback_from` and `confidence` as A2.9 states.

Round 1's OpenRouter behaviour is a deviation, not an accepted variant.

### A3.6 Round 2 scope

In this order:
1. Amendment 2 (A2.1–A2.12), including A2.9 as amended;
2. A3.3 item 2;
3. A3.4;
4. the Amendment 1 re-sweep.

The section 9 gate is then re-run against B5, plus the A2.11 hard invariants once the verifier lands the A2.12
fixtures.

## Amendment 4: callout targets never enclose the marker; cassette ownership

Verifier round 1:
- the gated gate **passes** against B5: parts 75.38 public / 76.47 hidden, precision 84.48 / 92.86, callouts 8/8 on
  both sets, no lost hits, no new stray ovals, first-highlight p95 64 / 71 ms;
- 6 of the 10 hidden-suite FAILs share one ambiguity in 2.3. The edge distance to an object that **encloses** the
  marker is 0, so a marker inside a panel frame took the frame as its target and got a panel-sized oval. This is the
  same class of off-target oval the user reported.

### A4.1 Callout target eligibility (replaces the object choice in 2.3)

1. **Never a callout target:**
   - an object that **contains** the marker box or the marker's enclosure, with 1 pt tolerance. This covers panel
     frames, figure frames, backgrounds and a photo the number is printed on;
   - an object whose area is **>= 40 % of the marker's panel** (or of the figure, when the marker is in no panel);
   - the marker's own enclosure, its leader, and any other callout marker or enclosure;
   - an object covering >= 60 % of the figure (A3.2(c), unchanged).
2. **Distances** (the 14 pt rule, and the 4 pt far-end rule) are measured **only** to eligible objects. An eligible
   object that only partly overlaps the marker has distance 0 and stays eligible.
3. **Order of rules, unchanged otherwise:**
   1. leader far end → the smallest eligible object containing it or within 4 pt of it;
   2. no leader → the nearest eligible object within 14 pt.
4. **No eligible object:**
   - the target is the **marker's own padded box**: the marker (or its enclosure) padded by its height on each side
     and clipped to its panel, with `attached: false`;
   - **amends 3.2:** a resolved callout reference to such a callout **draws the oval on the padded marker**
     (`target_src: "callout-marker"`) and submits no pick. The number is printed at the spot the text means, and the
     oval can never be panel-sized;
   - a callout with an eligible target keeps `target_src: "callout"`.
5. **Scope:** callouts only. Text-label hoods (`ground_label`) are unchanged in Phase 5. They are still covered by the
   no-lost-hit rule (9.2), and a text-hood case of the same class is raised as a separate item.

**Hard invariant** (verifier, bench and hidden suite): no show with `target_src` `callout` or `callout-marker` has a
`doc.target` area >= 40 % of its panel (figure when there is no panel). Count must be 0.

**Unit tests** (implementer, new file):
- a marker inside a panel frame, with a small shape 10 pt away → the shape;
- a marker inside a frame with nothing else near → the padded marker;
- a number printed on a raster photo → the padded marker;
- a leader whose far end is inside both a panel frame and a small shape → the small shape;
- a marker partly overlapping a small shape → the shape.

**Acceptance:**
- the 6 hidden-suite FAILs of this class pass;
- the section 9 gate is re-run against B5 and still passes, with callouts 8/8 on both sets and no lost hit.

### A4.2 Pick cassettes: the implementer records, the verifier replays

- **Ownership exception.** The implementer records and **commits** `tests/audit/bench/pick_cassette.json` with
  `tools/pick_eval.py --set public --record`. This is the one file under `tests/audit/` the implementer may write. It
  is a data file produced by the implementer's tool and is never hand-edited.
- **Hidden cassette:** the **verifier** writes it (as amended by Amendment 5; implementers never touch the holdout),
  with `tools/pick_eval.py --set hidden --record`, into `<holdout>/pick_cassette.json`. It is never committed, and the
  tool prints aggregates only.
- **Header:** the cassette header gains `src_sha`, the sha256 over the sorted `src/*.py` contents at recording time,
  next to `commit`.
- **When to record:** after the **last** `src/` change of the round. Any later `src/` change makes both cassettes stale,
  and they must be re-recorded.
- **Verifier rule:** replay determinism (A3.3 item 3, three passes) runs **only** when the tree's `src_sha` equals both
  cassette headers. If not, the verifier reports `cassette not current (src changed after recording)` and does not run
  the replay check. That is a hand-back to the implementer, not a FAIL of the picker.

## Amendment 5: round-2 rulings

Implementer round 2, public, gated mode:
- parts 83.08 (54/65), precision 93.1, boxes 100, lines 98.63, first highlight 45 / 69 ms;
- the same parts score without the layout model;
- 0 lost and 13 gained hits;
- three replay passes identical, with `replay_miss` 0.

### A5.1 Picker threshold: final

The re-sweep under the narrower rule reached at best 5 accepted at precision 80.0 (T 0.40–0.75). No T qualifies, so
the picker is **default-off for Phase 5, final** at `PICK_MIN_P = 0.95` (Amendment 1, item 4). The picker code, events,
pick mode and fallbacks still ship and are verified by 9.3 (with default-off), 9.5, A3.3 and A4.2.

### A5.2 Deviations

| Deviation | Ruling | Reason, and the contract text it sets |
|---|---|---|
| (a) The A3.4 "no graphic object below it before the next label row" condition implemented as "nothing below the label in its column nearer than the graphics directly above it (<= 24 pt)" | **Accepted** | The literal rule can never hold for row 1 of a labels-below 2×2, because row 2's graphics lie below row-1 labels. A3.4 now reads: *every drawn panel label has graphic content directly above it in its column within 24 pt, and no graphic object below it in its column at a gap smaller than that gap above.* The mixed/ambiguous fallback and the guards are unchanged |
| (b) Panel sides use midpoints between label centres | **Accepted** | It is the precise reading of A3.4's "midpoint to the neighbour label". A3.4 now reads: *x-bounds at the midpoint between this label's centre and its row neighbour's centre (figure edge when there is none)* |
| (c) `ground_label` tie rule for duplicate label text | **Accepted** | Duplicate labels (the same name on several graphics) were a source of stray ovals after A3.4, and 0 hits were lost. 3.3 now reads: *when the best score is shared by labels with the same text on different graphics, the candidate whose **other labels** (the labels in the same panel, else within its hood's 14 pt neighbourhood) are named by the sentence wins. Exactly one such candidate must exist. Otherwise there is no deterministic oval and 3.4 applies (the panel or figure box). With the picker on, the tied graphics become its first candidates.* Ties between **different** label texts keep today's order rule. 9.2 (no lost hit) still governs |
| (d) `stand_in_blocked` counted only outside figure mode | **Accepted** | In figure mode the old path drew the figure box, never the block band, so nothing was blocked there. Counting it would inflate the counter. The **hard invariant** `stand_in` (A2.11) still applies to every show in every mode |
| (e) Said panel and held panel are decided in text (speech) order | **Accepted** | `cur_panel` is updated when each chunk is located (`Speaker.put`), in chunk order, never at show or playback time. So a chunk's scope depends only on the text before it, which is what A3.3 determinism needs and what the listener hears. A `hold` or back-reference chunk does not change `cur_panel` |

### A5.3 Hidden cassette: the verifier records it

This replaces the hidden-cassette line of A4.2.
- **Implementers never read or write the holdout.**
- The **verifier** records `<holdout>/pick_cassette.json` with `tools/pick_eval.py --set hidden --record`. It does so
  after the implementer has committed the round's last `src/` change and the public cassette (same `src_sha`), then
  runs the hidden replay checks.
- The public cassette stays implementer-recorded and committed (A4.2).
- A `src_sha` mismatch on either cassette is a hand-back, as in A4.2.

## Amendment 6: whole-figure sentences, verbatim area prose, report-only guards, known baseline failures

Chat-approved. Item 1 follows the user's real-use complaint about ovals on whole-figure sentences.

### A6.1 Whole-figure sentences: A2.10 wins over A2.5

**The conflict:** "Figure 1 shows the overall setup." was picked and got an oval. A2.10 lists it as a whole-figure
sentence, but A2.5's caption-title test let `overall` / `setup` through. **The A2.10 examples win.**

A2.5's "names a specific object" test now reads:

1. **Ignored words.** These never count as object words: stopwords; numbers that are not callout references; and the
   generic figure-level words:
   - `overall setup overview figure panel shows illustrates depicts compares results pipeline architecture system`
   - `method approach process workflow framework summary diagram plot chart data example layout design`

   Inflections of these words (`shown`, `showing`, `compared`, `designs`, …) are ignored too. The list is a fixed
   constant in code, with no per-fixture additions.
2. **A chunk names a specific object only when one of these holds:**
   - a **label match** (3.3);
   - a **callout reference** (3.2);
   - a remaining content word (after step 1 and after removing the caption-title words) **matches a part, label or
     object name in the current figure**. "Matches" means the 3.3 token rule: equal, or a shared 5-character prefix
     for words of at least 4 characters.
3. A remaining content word that matches nothing in the figure does **not** count. The chunk is whole-figure.

Everything else in A2.5 stands: the figure (or panel) box stays, no pick or grounding request is sent
(`skipped["whole-figure"]`), and no late answer ovals the chunk.

The Amendment 1 narrower activation rule uses this test. The A2.11 invariant `whole_figure_oval` (0) and the A2.12
`figure` GT score cover it.

**Unit vectors** (implementer, new file):
- **Whole-figure:** "Figure 1 shows the overall setup."; "This diagram compares the two approaches."; "The figure
  summarises the results."
- **Object:** "The Pump circulates the wash water." (label); "Callout 3 is the probe." (callout); "The coil sits
  above the fan." (each label-matched in a figure labelled Coil/Fan).

### A6.2 Verbatim prose in area figure mode (clarifies A2.2)

- A chunk that aligns **verbatim** to prose lines inside the area rect gets its **text band**, even when the area run
  is in figure mode.
- Figure mode decides the cue only for:
  - **non-verbatim** chunks (paraphrase, explanation, narration of the figure);
  - chunks whose text comes from **figure-region text** (labels inside the figure bbox).
- A caption that is explained (not read verbatim) keeps its figure cue, as in Phase 3 H. A caption read verbatim gets
  caption text bands, as in Phase 3 H.
- A2.1's eligible-line rule still holds: the band never includes a line outside the area.

### A6.3 `parts_with_pick` precision guard (9.3): report-only while the picker is default-off

- 9.3's condition (`parts_with_pick.precision` no lower than gated-mode `parts.precision` − 1) applies **only when the
  picker is enabled by default**.
- While Amendment 5 keeps it default-off, the value is **report-only**: it is still computed and logged in the `pick`
  block, but it never fails acceptance.
- The other 9.3 items (replay determinism, `replay_miss == 0`) still apply.

### A6.4 Out of scope: label-first area reads with `no-fit`

- **The case:** a Read-area capture whose text is mostly figure labels gets `area_pdf: "no-fit"` (the same at B5),
  because too few long prose anchors exist for the anchor fit.
- **Phase 5 behaviour:** A2.1's `capture-fit` / `none` region fallback and A2.3 (no stand-ins) apply. No further fix
  in Phase 5.
- **Follow-up for Phase 6/7:** a label-anchored area fit, using short figure labels as anchors, inside the figure bbox
  only. Logged as an open item; it does not count against the Phase 5 acceptance.

### A6.5 Known baseline failures (pre-existing, not Phase 5 regressions)

These fail **at B5 as well** and are excluded from Phase 5 regression counting:
- `tests/audit/readability/run_phase1.py`: `E_on_texts_equal_baseline`;
- `tests/audit/logging/run_phase2.py`: the 5 checks that fail at B5. The verifier lists them by name in its round
  report.

**Rule:** a check in this list may not newly pass-then-fail, and no **other** check may start failing (9.7 /
Phase 3 G.5). They are recorded as known baseline failures for the **circle-back step** after Phase 6, where they are
fixed or re-pinned with a reason.

### A6.1b Correction to A6.1: any concrete content word names an object (replaces A6.1 items 2–3)

- **The error:** A6.1 required a remaining word to match a part, label or object **name** in the figure. That made
  sentences about unlabelled objects ("The red valve controls the inflow.") count as whole-figure, so the picker could
  never run for exactly the sentences it exists for. It also contradicts the verifier check
  `W_object_sentence_still_picks`.
- **Corrected rule:**
  1. Remove, in this order:
     - the figure reference (`Figure 1`, `Fig. 2b`, `panel b`);
     - the A6.1 generic figure-level words, with their inflections and spelling variants (`shown`, `compared`,
       `summarises`, `approaches`, …);
     - stopwords;
     - numbers (digits or number words) that are not callout references.
  2. If **no content word remains**, the chunk is **whole-figure**: A2.5 behaviour (the box stays, no pick or
     grounding, no late oval).
  3. If **any** concrete content word remains (e.g. `red valve`, `inflow`), the chunk **names an object**. The order
     is then:
     1. label match (3.3);
     2. else callout (3.2);
     3. else **picker-eligible** (4.1; it runs only when the picker is enabled).
- **The caption-title subtraction (A2.5 third bullet) is dropped.** It is superseded by this rule. "Figure 2 shows the
  sensor drawing" now names an object (`sensor`).
- **The A6.1 item 1 word list stands.** "Figure 1 shows the overall setup." stays whole-figure.
- **Unit vectors (replace A6.1's):**
  - **Whole-figure:** "Figure 1 shows the overall setup."; "This diagram compares the two approaches."; "Fig. 2b
    summarises the results."
  - **Object, label:** "The Pump circulates the wash water."
  - **Object, callout:** "Callout 3 is the probe."
  - **Object, picker-eligible:** "The red valve controls the inflow." (no label matches).
- **Unchanged:** the Amendment 1 narrower activation rule, the A2.11 `whole_figure_oval` invariant and the A2.12
  `figure` score all use this corrected test.

### A6.3 confirmed

While the picker is default-off (Amendment 5), the 9.3 `parts_with_pick` precision check is **report-only**: it is
computed and logged, and it **cannot FAIL** acceptance.

## Final audit (2026-10-26)

- A6.5: the known baseline failures are resolved as pins (they are intended Phase 3 and Phase 4 behaviour; see the final
  audit notes in those contracts). No check is excluded any more.
- A6.4 is still open: a Read-area drag over a label-dominated figure (boxacc's Fig. 5 area cases) gets `area_pdf: no-fit`
  and screen-only cues; the label-anchored area fit was not built in Phase 6 or 7.
- The public pick cassette is re-recorded for the current `src/` (same 25 request keys).
