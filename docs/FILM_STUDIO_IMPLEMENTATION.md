# Film Studio and talking presenter — implementation notes

## Flow (unchanged architecture)

screenplay → casting (character/location references, `film_studio/casting.py`) → shot list
(`shotlist.py`: master, coverage, inserts, continuity notes) → shot generation (`generate.py`) → dialogue and
sound (`audio.py`) → edit (`edit.py`) → QA (`qa.py`) → optional Shorts proposals (`shorts.py`).

Takes are never overwritten; every take records provider, model, motion type and what really happened.

## What changed in this upgrade

### Edit cache (risk 2)
`edit.cache_key()` now covers everything that changes a timeline clip:
- the shot's fields (kind, source, key picture, seconds, subtitle, motion type, speech, speaker, transition);
- the **identity** of the source clip and key picture — path + size + modification time — so a re-made take at
  the same path is rebuilt (`render.file_id`);
- title/end card text, frame size, encoder preset/CRF and `render.RENDER_VERSION`.

Bump `RENDER_VERSION` in `studio/media/render.py` whenever drawing/encoding code changes what an identical
input renders to.

### No jump back at the end of a shot (risk 8)
A real-motion clip shorter than its shot used to finish on the shot's *starting* key picture. It now continues
from the clip's **last frame** (`render.last_frame`) with a gentle camera move. Clips are never slowed down or
looped.

### Safe joins (risk 7)
`render.concat()` stream-copies only when every clip has the same codec, size, pixel format, frame rate and audio
layout; otherwise it scales/pads every clip to the frame, normalises fps and pixel format and re-encodes. Used for
the two-part shots and the whole film timeline.

### Render manifest
Each export folder gets `render_manifest.json`: renderer version, size, fps, join method (copy/reencode),
subtitle count, every clip's cache key (inputs and their file identity) and the real output streams.

### Talking clips
| Order | Provider | Where | Limit | Notes |
|---|---|---|---|---|
| 1 | LTX-2 (`hf_ltx2`, Space `alexnasa/ltx-2-TURBO`) | Hugging Face ZeroGPU | ~2 three-second pieces a day (shared free GPU minutes) | most realistic; audio-driven lip-sync |
| 2 | SadTalker (`sadtalker_local`) | this Mac, `~/sadtalker` | none | face only; ~1 min of Mac time per second of speech; 256 model |
| — | still picture | — | — | kept when both are unavailable; the reason is logged |

News presenters use this order in `creative/news.make_presenter_talk`. Film dialogue shots still use LTX-2
(`generate._talk_take`) and fall back to the labelled still/camera move.

SadTalker install (done 30 Oct 2026): own Python 3.12 venv, PyTorch with Apple GPU (MPS), weights from the
author's Hugging Face repo `vinthony/SadTalker-V002rc` (GitHub release downloads crawl on the hotspot). Local
patches for new NumPy and MPS are in `~/sadtalker` (not in this repository). Uninstall from Storage →
Installed AI tools.

## Honest labelling (unchanged rules, still enforced)
- A still with a camera move is labelled `camera_move`, never video.
- Providers without an execution adapter are listed for information and never chosen.
- Lip-sync is reported as "audio-driven, not measured"; no sync score is invented.
