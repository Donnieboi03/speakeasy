# Speakeasy design system

One user, one job: listen to a paper while the screen shows where the voice is. Every surface serves that job and
gets out of the document's way. This file is the source of truth for colours, type, spacing, copy and the look of each
surface. Behaviour (states, events, acceptance) lives in `docs/contracts/phase7-usable-app.md`.

Rules that apply everywhere:

1. One dominant element per screen. Everything else is smaller, quieter and to its right or below it.
2. Real content over decoration: real document titles (from the local document library), real page and figure numbers, real
   timings. No sparkles, robots, brains, waveforms that do not reflect audio, or stock icons standing in for content.
3. Flat: hairline borders, no drop shadows, no glows, no blur or vibrancy, no gradients.
4. Nothing on screen may take keyboard focus from the document being read (contract B4).

## 1. Palette

Confirmed by the user on 2026-10-26: this palette, not TYTIS colours.

Navy carries text and dark surfaces; ice blue is the reading colour. One warm accent marks "this exact thing". No
purple, no pastels, no neon, no pure white (`#FFFFFF`) and no pure black (`#000000`) anywhere.

| Token | Hex | Role |
|---|---|---|
| `ink` | `#14233B` | Primary text on light surfaces; the bar's text-on-ice |
| `navy` | `#1C3150` | The bar's surface; dark headers |
| `navy-line` | `#2C4468` | Hairlines on navy (bar border, dividers inside the bar) |
| `slate` | `#566577` | Secondary text on light surfaces (5.6:1 on `paper`) |
| `paper` | `#F3F6F8` | Window background (light) |
| `paper-2` | `#E8EEF2` | Selected row, pressed button, empty-state well |
| `line` | `#CBD5DE` | Hairlines on light surfaces |
| `ice` | `#A8D8EC` | Brand ice blue. Text and marks on `navy`; the reading band fill (at 38 % alpha) |
| `glacier` | `#1F6E99` | Ice's dark partner: strokes and marks on light surfaces (figure box, focus ring, caret, links). 5.3:1 on `paper` |
| `ember` | `#E0892B` | The one accent. Only two uses: the part oval stroke on the document, and the current sentence's tick in the bar's progress |
| `brick` | `#B23A2B` | Errors only (text and the error tick). Never decorative |

Dark appearance (windows only; the bar and on-screen cues look the same in both appearances):

| Token | Dark value |
|---|---|
| `paper` | `#101B2C` |
| `paper-2` | `#182841` |
| `ink` (text) | `#E3ECF2` |
| `slate` | `#9AAABB` |
| `line` | `#2A3D58` |
| `glacier` (marks on dark) | use `ice` `#A8D8EC` |

Notes:
- The old focus colour `#3B82F6` is Tailwind's `blue-500`, the most common default blue on the web. It is retired.
- Green is retired too (pill "speaking" dot, the heatmap). Success needs no colour: a granted permission shows a
  `glacier` check and the word "On".
- State is never shown by colour alone: every coloured state also has a word or a shape.
- Tokens live in one place per language (`app/.../Tokens.swift`, the tracker's constants, `focus.py`'s cue style).
  No other hex value may appear in UI code (contract C1).

## 2. Type

| Role | Face | Weights | Licence | Why |
|---|---|---|---|---|
| Headings, document titles, the one dominant number | **Source Serif 4** (Adobe) | 600, optical size on | SIL OFL 1.1, [adobe-fonts/source-serif](https://github.com/adobe-fonts/source-serif) | Speakeasy reads papers. A text serif for titles echoes the documents and sets the app apart from every sans-only utility |
| UI text, labels, buttons | **IBM Plex Sans** | 400, 500 | SIL OFL 1.1, [IBM/plex](https://github.com/IBM/plex) | Engineered, slightly technical, clear at 11 to 13 px; not Inter, Geist or SF |
| Numbers, timings, page and figure refs, hotkeys | **IBM Plex Mono** | 400, 500 | SIL OFL 1.1, same repo | Mono only for data. Fixed-width digits line up in columns without extra work |

- Bundled in `Speakeasy.app/Contents/Resources/Fonts/` and registered at launch for the process only
  (`CTFontManagerRegisterFontsForURL`, scope `.process`). The OFL texts go into `app/NOTICE`.
- Fallback if registration fails: Charter, then SF Pro Text, then SF Mono (all shipped with macOS). Logged once per
  launch as `font_fallback` (contract C3).
- Rejected: Manrope + DM Sans (two soft geometric sans, the stock startup pairing), Inter, Geist, Space Grotesk.

Scale (px, line height in brackets), all on a 4 px baseline grid:

| Token | Size | Face / weight | Use |
|---|---|---|---|
| `t-label` | 11 (16) | Plex Sans 500, +2 % tracking | Labels above values, hotkey hints |
| `t-body` | 13 (20) | Plex Sans 400 | Body, menu-like rows, buttons |
| `t-data` | 13 (20) | Plex Mono 400 | Any number in body text |
| `t-head` | 15 (20) | Source Serif 4 600 | Section heads |
| `t-title` | 20 (28) | Source Serif 4 600 | Window titles inside content, document titles |
| `t-hero` | 28 (32) | Source Serif 4 600, lining figures | The single dominant number of a screen (at most one) |

Details:
- Sentence case everywhere. No all caps except hotkey glyphs.
- Numbers in Plex Mono align on the right in tables; units follow in Plex Sans at `t-label` (`3.9` then `s`).
- Baselines align across columns: rows are laid out with `firstTextBaseline` alignment, never `center`.

## 3. Spacing, radii, borders, motion

- Spacing scale: **4, 8, 12, 16, 24, 40**. Window content inset 24. Gap between related items 8, between groups 24.
  The same inner padding (12 horizontal, 8 vertical) on every button and row.
- Radii: **2** (band on the document, chips), **3** (buttons, fields, figure box), **4** (bar, panels inside windows).
  Nothing rounder. The part oval is an ellipse because it means "circled", not because it is a rounded box.
- Borders: 1 px hairline (`line` on light, `navy-line` on navy). 2 px strokes only for cues drawn on the document,
  where they must stay visible over arbitrary content.
- No shadow (`hasShadow = false` on every panel), no `NSVisualEffectView`, no blur, no gradient, no glow, no pulsing.
- Motion: 120 ms ease-out for expand and collapse, 160 ms for the bar sliding aside. Nothing loops. Respect
  "Reduce motion": the bar then changes size instantly.

## 4. The signature: the margin tick

One mark appears on every screen: a **3 px wide vertical tick at the left edge of the dominant element**, the same
mark a reader pencils in a margin. It is `ice` on navy, `glacier` on light surfaces, `brick` when the element is an
error, and `ember` never.

- On the document: the reading band gets the tick at its left edge, 4 px left of the first line, spanning the band's
  lines. The band itself stays a soft fill (no outline), so the research rule "fill, not a box" still holds
  (`docs/research/highlighting.md`).
- In the bar: the tick is the bar's left edge in every state. At idle the bar is little more than the tick.
- In windows: the tick sits left of the dominant element (the document list in Reading, the first missing permission in
  Permissions, the active toggle group in Cloud).
- The text caret and the focus ring use the same colour (`glacier`), so typing in a field shows the reading colour.

Everything else stays plain so the tick is the one thing that is recognisably Speakeasy.

## 5. Copy rules

- Name the thing: "Figure 4, page 5", "Kokoro", "OpenRouter", "Screen Recording". Never "content", "item", "AI".
- Numbers with units: "3.9 s to first audio", "14 min listened", "7 pages".
- Buttons say what happens: "Open Screen Recording settings", "Restart Kokoro", "Read area", "Save key". Never "OK",
  "Grant…", "Continue", "Learn more".
- No em dashes. No "it's not X, it's Y". No "seamless", "effortless", "magic", "supercharge", "unlock", "smart",
  "powered by AI". No exclamation marks.
- Errors say what happened, what Speakeasy did about it, and the one action: "Kokoro is not running. This read used the
  macOS voice. Restart Kokoro".
- Estimates are labelled as estimates or not shown.
- Privacy statements are true at the moment they are shown: "Everything stays on this Mac" only when cloud is off.

Before and after (current copy to Phase 7 copy):

| Where | Now | Phase 7 |
|---|---|---|
| Pill, waiting | "Thinking" | "Preparing · 2.1 s" (elapsed, Plex Mono) |
| Pill, speaking | animated bars | "Reading 3 of 12" plus progress ticks |
| Menu line | "Today 412 words · 2-day streak" | "Today: 9 reads, 14 min" |
| Menu | "Stats…" | "Reading log…" |
| Onboarding button | "Grant…" | "Open Accessibility settings" |
| Stats footnote | "Time saved is an estimate: …" | removed with the metric |

## 6. Surfaces

Wireframes are to scale in spirit, not in pixels. `▌` is the margin tick. `·` is a hairline divider.

### 6.1 The bar (bottom centre, above the Dock)

Navy surface, `navy-line` hairline, radius 4, no shadow. Text `ice` and `#E3ECF2`. Placement and behaviour:
contract section B. Bottom centre is the default home spot. If another app's floating window already sits there, the bar
stacks 8 pt above it when that window is bar-shaped, and otherwise uses bottom left, then bottom right. A position the
user dragged to is always kept (contract B1 rule 8).

Idle (56 × 8 visible, 72 × 24 hit area). Only the tick and a short navy stub:

```
                         ▌▀▀▀▀▀▀
```

Hover (after 120 ms dwell), 300 × 36. Two actions with their hotkeys, the last-read place on the right:

```
 ┌──────────────────────────────────────────────────────┐
 ▌ Read area ⌃⌥A  ·  Read selection ⌃⌥S  ·  p. 6, Fig. 4 │
 └──────────────────────────────────────────────────────┘
```

Area-select armed (the crosshair overlay is up; the bar sits above it):

```
 ┌──────────────────────────────────────────────────────┐
 ▌ Drag over what to read            Esc cancels      │
 └──────────────────────────────────────────────────────┘
```

Preparing (until first audio). The counter is real elapsed time, the same number the Reading log reports:

```
 ┌──────────────────────────────────────────────────────┐
 ▌ Preparing Figure 4            2.1 s            ■ Stop │
 └──────────────────────────────────────────────────────┘
```

Reading, 340 × 36. One tick per sentence; read ticks `ice`, the current one `ember`, the rest `navy-line`:

```
 ┌───────────────────────────────────────────────────────────┐
 ▌ ▮▮▮▮▯▯▯▯▯▯▯▯  4 of 12    ❙❙ Pause   ⏭ Next   ■ Stop   ⚑ │
 └───────────────────────────────────────────────────────────┘
```

`⚑` is "Wrong spot": one click flags that the highlight is on the wrong place (contract A, highlight quality).
Paused: "❙❙ Pause" becomes "▶ Resume", the ticks keep their state, the current tick turns `ice` outline only.

Error (brick tick, one sentence, one action; collapses after 8 s to idle with a brick tick until hovered):

```
 ┌────────────────────────────────────────────────────────────┐
 ▌ Kokoro is not running. Read with the macOS voice.  Restart │
 └────────────────────────────────────────────────────────────┘
```

Cloud on: the word "Cloud" in `t-label` sits right of the progress while the current read leaves the Mac. A word, not
an icon.

Cover-the-logo test: navy stub with an ice tick at the bottom centre, a mono counter, the ember current tick.

### 6.2 Menu-bar menu

The status item icon becomes a custom template glyph: a 3 px tick beside two short lines (a margin mark next to text),
drawn as a template image so macOS tints it. No speech bubble.

```
  Today: 9 reads, 14 min
  Kokoro is not running              ← only when a problem is open (brick text)
  ─────────────
  Read selection             ⌃⌥S
  Read area                  ⌃⌥A
  Stop                       (only while reading)
  ─────────────
  Highlight text         ✓
  Figure cues            ✓
  Show bar               ✓          ⌃⌥B
  ─────────────
  Reading log…
  Settings…
  Cloud (OpenRouter)…    Off
  Permissions…           (only when one is missing)
  ─────────────
  Check for updates…
  Quit Speakeasy
```

NSMenu uses the system font and cannot take the tick; the menu is the one surface that stays native. The cover-the-logo
test for it is the status glyph and the first line's real numbers.

### 6.3 Reading log (the stats window, 640 × 480)

Dominant element: the document list (left two thirds). Right column: speed, highlight, problems. No heatmap, no
streak, no "time saved".

```
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ Reading log                                         Last 7 days ▾        │
 │                                                                          │
 │ ▌ [PDF title or file name]                  │  First audio               │
 │   p. 3  4  5  6  7          4 figures       │  3.9 s  median             │
 │   ▮  ▮  ▮  ▮  ▮             Oct 26, 07:39   │  16.5 s slowest 1 in 10    │
 │   Resume at p. 5, Figure 4, sentence 8      │  Local 6.1 s · Cloud 2.3 s │
 │ · · · · · · · · · · · · · · · · · · · · · · │                            │
 │                                             │  Highlight                 │
 │                                             │  87 % on target first time │
 │                                             │  0 flagged wrong spot      │
 │                                             │                            │
 │                                             │  Problems                  │
 │                                             │  Kokoro off, 1 read, Oct 24│
 │                                             │  Restart Kokoro            │
 │                                             │                            │
 │ 27 reads · 19.9 min listened · 175 wpm                                   │
 └──────────────────────────────────────────────────────────────────────────┘
```

- The numbers above are the user's own log on 2026-10-26 (one document, pages 3 to 7, 27 menu-bar reads); "4 figures"
  and the resume sentence are illustrative.
- Document title in Source Serif 4 `t-title`, from the local document library (contract A5.3, on by default; the
  user approved storing names and paths outside the logs). The bracketed title in the wireframe stands for the real
  one. A document read before the library existed, or with names turned off, reads "Document 4eb5ec" in Plex Mono with
  "(name hidden)" in `slate`.
- The page strip is real: one cell per page the user listened to (`▮`) between the first and last page read; gaps
  show as `▯`. Figures and tables are counted as distinct items, not runs.
- Right column numbers in Plex Mono, labels in `t-label`. The bottom line is the only place totals appear.
- Empty state (no reads yet): the left column shows one line, "Nothing read yet. Select a paragraph and press ⌃⌥S, or
  drag over a figure with ⌃⌥A.", and the right column is not drawn.
- Error state (`speakeasy stats --json` failed): "Could not read the log at ~/.speakeasy/logs/runs.jsonl" plus the
  exit status, and "Open logs folder".

Cover-the-logo test: serif document titles with mono page strips and the margin tick on the first document.

### 6.4 Permissions (onboarding, 480 × 320)

Dominant element: the first missing permission. Granted rows collapse to one line.

```
 ┌───────────────────────────────────────────────────────────┐
 │ Two permissions, then press ⌃⌥S on any text               │
 │                                                           │
 │ ▌ Screen Recording                                        │
 │   Lets Speakeasy see figures and keep the highlight on    │
 │   the line while you scroll.                              │
 │   [ Open Screen Recording settings ]                      │
 │   Turned it on already? macOS needs a restart of the app. │
 │   [ Relaunch Speakeasy ]                                  │
 │ · · · · · · · · · · · · · · · · · · · · · · · · · · · · · │
 │ ✓ Accessibility   On                                      │
 │                                                           │
 │ Everything stays on this Mac.                             │
 └───────────────────────────────────────────────────────────┘
```

The last line is replaced by "Cloud is on: text you read goes to OpenRouter." when cloud is on.

### 6.5 Settings (hotkeys, bar, highlight) and Cloud

Settings (420 × 440): four groups, the active group carries the tick. Hotkey recorders show the current keys in Plex
Mono.

```
 ┌──────────────────────────────────────────────┐
 │ Settings                                     │
 │ ▌ Hotkeys                                    │
 │   Read selection        [ ⌃⌥S ]              │
 │   Read area             [ ⌃⌥A ]              │
 │   Show or hide bar      [ ⌃⌥B ]              │
 │ · · · · · · · · · · · · · · · · · · · · · ·  │
 │   Bar                                        │
 │   (•) Bottom centre ( ) Bottom left ( ) Right│
 │   [x] Hide the idle bar in full-screen apps  │
 │ · · · · · · · · · · · · · · · · · · · · · ·  │
 │   Highlight                                  │
 │   [x] Band on the sentence being read        │
 │   [x] Boxes and circles on figures           │
 │ · · · · · · · · · · · · · · · · · · · · · ·  │
 │   Reading log                                │
 │   [x] Show document names                    │
 │   Names and paths stay in library.json on    │
 │   this Mac, never in the logs. Turning this  │
 │   off deletes the file.                      │
 └──────────────────────────────────────────────┘
```

Cloud (480 × 340): dominant element is the privacy sentence, because it is the decision.

```
 ┌───────────────────────────────────────────────────────────┐
 │ ▌ While on, the text you read and figure screenshots go   │
 │   to OpenRouter and google/gemini-3.5-flash-lite.         │
 │                                                           │
 │   [x] Rewrite with OpenRouter        sends selected text  │
 │   [x] Find figure parts with OpenRouter   sends crops     │
 │   Model  [ google/gemini-3.5-flash-lite      ] [Save]     │
 │   Key    [ saved in Keychain            ] [Replace] [Remove]│
 │                                                           │
 │   Last 7 days: 14 reads used OpenRouter, 2.3 s to first   │
 │   audio (local: 6.1 s).                                   │
 └───────────────────────────────────────────────────────────┘
```

The last line uses real numbers from the Reading log so the speed trade-off is visible where it is decided.

### 6.6 On-screen cues (drawn by the tracker over the document)

| Cue | Look | Token use |
|---|---|---|
| Text band | Fill `ice` at 38 % alpha, radius 2, no outline; 3 px `glacier` margin tick 4 px left of the first line, spanning the band's lines | `ice`, `glacier` |
| Back-reference | 1 px dashed `glacier` at 60 % alpha, radius 2, 1.5 s | `glacier` |
| Figure or panel box | 2 px `glacier` stroke, radius 3, fill `ice` at 10 % | `glacier`, `ice` |
| Part oval | 2 px `ember` ellipse, no fill | `ember` (the accent's main use) |
| Table row | Same as the text band, no tick | `ice` |
| Table header | `ice` at 18 % | `ice` |
| Read-area overlay | Screen dim `ink` at 12 %; selection 1 px `glacier` solid, fill `ice` at 12 %; size readout in Plex Mono next to the cursor ("412 × 230") | `ink`, `glacier`, `ice` |

- Legibility: text under the band must keep at least 4.5:1 contrast for black text on a white page (the 38 % `ice`
  fill over `#FFFFFF` page pixels gives about `#DFF0F7`; black on it stays above 15:1).
- One cue at a time, forward only, as today. The ember oval is the only warm colour on the screen, so "this part" is
  unmistakable next to the blue "this figure".
- Dark documents: the tracker samples the page background under the cue; if luminance is below 0.25 the band uses
  `glacier` at 45 % and the box stroke uses `ice`.

### 6.7 Update alert

Native `NSAlert`, copy only: "Speakeasy 0.9.2 is available. You have 0.9.1." Buttons "Open download page" and "Not
now". "Up to date: 0.9.1." when current.

## 7. Cover-the-logo test (per screen)

Hide the name and the status icon. A reviewer who has seen Speakeasy once must still recognise:

| Screen | Recognisable by |
|---|---|
| Bar | Navy stub with an ice margin tick, Plex Mono counters, the ember current tick |
| Reading log | Serif document titles over mono page strips, the margin tick on the first document |
| Permissions | The tick on the missing permission, a button that names the settings pane |
| Settings and Cloud | The tick on the active group, real numbers in the cloud trade-off line |
| On the document | The soft ice band with the glacier margin tick, the ember oval |

A screen that passes only because of the app name fails the test.
