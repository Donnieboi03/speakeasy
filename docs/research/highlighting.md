# Highlighting while reading aloud: evidence and design

Speakeasy reads a selection aloud and shows where it is. This note records the evidence behind how that cue looks and behaves, so later changes can be checked against it. Figures below are as reported by the cited papers; entries marked **unverified** were not checked against a primary source.

## Design (what Speakeasy does)

1. **Plain text: a soft tinted band** over the sentence or lines being spoken: low-alpha fill, rounded corners, no strong border. It moves **forward only**, sentence by sentence, with no flashing between sentences (an oversized provisional box is never drawn; the previous band is kept until the new tight one is known).
2. **Back-references:** when a sentence refers back to earlier content, the band stays where reading is, and a **faint dashed outline** marks the earlier lines for about 1.5 s.
3. **Figures and tables: a distinct cue** (outline, or a circle around an object) on the part being explained. While a figure cue is shown, the text band and the dashed outline are hidden: **one cue at a time**.
4. **Pacing:** the cue follows the audio (audio-paced), not the reader's own scrolling or eye position.
5. **Settings:** `SPEAKEASY_TEXT_HIGHLIGHT=on|off` and `SPEAKEASY_FIGURE_CUES=on|off`, both on by default, toggled independently (menu-bar app or `~/.speakeasy/config`), logged per run.

## Evidence

| Source | What it found | How it shapes the design |
|---|---|---|
| Schneider, Beege, Nebel & Rey (2018). A meta-analysis of how signaling affects learning with media. *Educational Research Review*, 23, 1-24. https://doi.org/10.1016/j.edurev.2017.11.001 | 103 studies, N = 12,201. Signaling (cues that direct attention) improved recall (g = .53) and transfer (g = .33). | Cueing what is being spoken is worth doing; keep the cue on the current content. |
| Richter, Scheiter & Eitel (2016). Signaling text-picture relations in multimedia learning: a comprehensive meta-analysis. *Educational Research Review*, 17, 19-36. https://doi.org/10.1016/j.edurev.2015.12.003 | 27 studies, N = 2,464. Signaling text-picture relations helped (r = .17), with larger effects for learners with low prior knowledge. | Figure cues that tie the spoken explanation to the part of the figure are justified, especially for unfamiliar material. |
| Wood, Moxley, Tighe & Wagner (2018). Does use of text-to-speech and related read-aloud tools improve reading comprehension for students with reading disabilities? A meta-analysis. *Journal of Learning Disabilities*, 51(1), 73-84. https://doi.org/10.1177/0022219416688170 | Text-to-speech and read-aloud tools improved comprehension, g = .35. | Supports the read-aloud core; highlighting is an addition to it, not a replacement. |
| Clinton-Lisell. Reading-while-listening meta-analysis. Preprint https://doi.org/10.31219/osf.io/ku6tv ; published version https://files.eric.ed.gov/fulltext/EJ1403866.pdf | Reading while listening: g = .18 overall; g = .41 when audio-paced, g = .06 when self-paced. | The cue follows the audio (audio-paced), not the reader. |
| *Language Learning* registered report. https://doi.org/10.1111/lang.12721 | Second-language readers did worse with reading-while-listening. | Text highlighting can be turned off on its own (`SPEAKEASY_TEXT_HIGHLIGHT=off`). |
| *Aphasiology* (2020). https://doi.org/10.1080/02687038.2020.1787728 | No comprehension difference with highlighting, but participants preferred it. | On by default, but low-key (soft band) and switchable. |
| *American Journal of Speech-Language Pathology* (2024). https://doi.org/10.1044/2024_AJSLP-23-00334 | Eyes lagging behind the voice was associated with lower comprehension. | The band must track the voice closely and never jump backwards. When content is referred back to, a secondary outline marks it instead of moving the band. |
| *iJET* (2018). https://doi.org/10.3991/ijet.v13i09.8736 | A sentence-level band was rated best among the highlighting styles compared (n = 7; small sample). | Sentence or line band, not word-by-word karaoke. |
| SLaTE 2015. https://doi.org/10.21437/slate.2015-11 | Workshop study of synchronised text highlighting with speech for language learning. | Background for synchronised highlighting; not used for a specific parameter. |

## Unverified

- **Product behaviour:** macOS Spoken Content (highlight content when speaking), Microsoft Immersive Reader, Speechify and Kindle (Immersion Reading) all highlight the spoken text, with sentence and/or word modes. **Unverified:** summarised from general knowledge, not checked against current product documentation.
- **Mayer's redundancy principle:** the idea that showing the same words on screen while narrating them can hurt learning for some material, which is one reason to keep the text cue soft and optional. **Unverified wording:** not quoted from a primary source here.

## What is logged

- runs.jsonl: `text_highlight`, `figure_cues` (true/false per run), `mode`.
- tracker.jsonl:
  - `draw` (with `cue: text|figure`)
  - `cue-off` (a cue suppressed by settings)
  - `transition` (old/new box sizes, `held`)
  - `ref-outline`
  - `draw-failed`
- focus.jsonl: map records with `decision: forward|ref-back`.
