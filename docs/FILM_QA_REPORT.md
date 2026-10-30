# QA — what is checked, on what, and what it means

Every result is **pass / warn / fail / not checked**. A skipped check is never shown as passed.

| Severity | Effect |
|---|---|
| fail | blocks upload; the video goes to "needs attention" |
| warn | the video can be uploaded only after the owner approves it — never published automatically |
| info / not checked | shown for honesty; no effect |

## Regular videos (`studio/media/qa.py`, run in `pipeline.stage_qa`) — on the FINAL rendered file

| Check | Measured on | Severity |
|---|---|---|
| resolution, frame rate, codecs | final file (ffprobe) | fail |
| duration matches timeline; long-form / Shorts length | final file | fail |
| file size | final file | fail |
| audio present and audible; no clipping | final file (volumedetect) | fail / warn |
| full decode to the end | final file (ffmpeg null decode) | fail |
| black frames | final file (blackdetect) | fail |
| **frozen picture** (new) — stretches of 2 s+ with no change; warns on one ≥ 8 s or ≥ 40 % of the video | final file (freezedetect) | warn |
| visual variety, real-footage share | assets used | fail / info |
| title, description, tags, chapters | metadata | fail / warn |
| asset licences recorded, voice licence | assets / voice | fail / warn |
| fact checks, originality (7-word phrase overlap), editorial review | script + sources | warn |
| **not a repeat of an earlier upload (all channels)** (new) — title ≥ 80 % alike or ≥ 35 % shared script phrases with any upload of the last 180 days | studio database | warn |
| financial-recommendation language (finance-related videos) | script + title + description | warn |
| viral checklist | metadata | info |

Thresholds for the frozen-picture check were set from real uploads: NIFTY chart videos hold static charts
for 2–5.4 s on purpose (#151, #153, #155); news videos had none ≥ 2 s.

## Films (`studio/film_studio/qa.py`)

| Check | Measured on |
|---|---|
| all shots present, file exists, plays to the end, sound present, length, resolution & fps | final film file |
| no unintended black; **no frozen picture** (new); audio not clipped; dialogue audible; subtitles timed | final film file / SRT |
| real motion share; lip-sync (reported, not measured) | shot records |
| script-to-shot relevance | **key pictures** (vision model) — labelled as such |
| character consistency, movement quality, object interaction | **not checked** — needs face matching/video analysis this setup doesn't have |

## Thumbnails

`thumbnail.pick_background()` chooses the strongest picture: scene relevance (vision score where checked), sharpness
/ colour / exposure, and size; cards and drawn backgrounds are never used. The choice and its scores are saved
with the thumbnail result (`background`). Text contrast is measured (≥ 4.5:1).

## Shorts

Each Short runs its own QA (same checks, vertical frame) and keeps its parent link; Shorts cut from their own
episode are not counted as repeats.

## Not verified in this pass
- Visible lip-sync accuracy (no measurement tool).
- Caption safe-zone collisions with on-screen graphics beyond the fixed caption band.
- Live end-to-end SadTalker inside a scheduled video (unit-tested and run by hand; first scheduled use is the
  next AI News slot).
