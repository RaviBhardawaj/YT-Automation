# KahaaniVerse — cinematic 3D-animated story studio

> *Har Kahaani Mein Ek Raaz. Har Raaz Ke Peeche Ek Sach.*

Studio channel #1 (previously "NIFTY 50 Fibonacci Levels") is now **KahaaniVerse**. It makes original Indian
cinematic 3D-animated films in Hindi and Hinglish. The existing studio was extended, not replaced. The finance code,
its data, the compliance gate and every finance video are untouched; only the finance **schedules** were switched off.
The previous channel settings are on the audit trail (event `channel_converted`).

## What was built (phases 1–2)

| Piece | Where | What it does |
|---|---|---|
| Channel identity | `studio/creative/kahaani.py` | Name, tagline, genres with a configurable mix (30/25/20/15/10 to start), `pick_genre()` (weighted, avoids repeating the last two genres) and `convert_channel()` (audited). |
| Character universe | `kahaani.UNIVERSE` | Aarav, Siya, Veer and Dadaji, each with age, face, hair, build, clothing, accessories, personality, backstory, gestures and a fixed voice (Kokoro voice + pitch + speed). |
| Reference library | `kahaani.build_sheets()`, `data/characters/channel_1/` | Eight pictures per character: front, side, back, expressions, emotions, gestures, poses and clothing. The front view is drawn first and every other sheet is drawn **from** it. A sheet drawn without the front reference is thrown away rather than kept inconsistent. The voice profile and identity metadata are stored on the character row (`characters.design`, `characters.voice`). |
| Creative director | `studio/creative/director.py` | `research()` → `concepts()` (10+ concepts answering the ten director questions, scored on ten dimensions, copies dropped) → backlog (`topics`, `why_now='kahaani concept'`) → `pick()` → `screenplay()` → `storyboard()` → `to_film()` → `start_film()`. |
| Screenplay | `director.screenplay()` | Outline with a hook scene first, 2–4 specific locations, scene purposes (hook → setup → conflict → twist → climax → resolution) and one-off characters with full looks. Dialogue is in Devanagari (Hinglish), two scenes per writer call, and every part is validated. `check_story()` checks length, hook, repeated lines and script. |
| Cinematography | `director.storyboard()` | Per scene, every shot carries: type (establishing, wide, medium, close-up, extreme close-up, over-the-shoulder, tracking, insert, reaction, POV), angle, movement, composition, depth of field, lighting, expression, duration, image / video / negative prompts, continuity and transition. Every dialogue line is covered once, in order, with time to be heard. |
| Film pipeline reuse | `studio/creative/film.py`, `studio/film_studio/*` | Films have a **look** (`real` or `animated3d`). The script stage runs the director for KahaaniVerse films and the plan stage uses the storyboard. Casting reuses the library's approved pictures, adds one reference per location and auto-approves new pictures for daily films. Voices keep the library voices, and one-off characters never reuse one. Keyframe and motion prompts come from the storyboard, and each shot gets its own location reference. |
| Daily task | `studio/kahaani_tasks.py` | `kahaani_daily` (research when the backlog is thin → pick → film) and `kahaani_library`. The 09:00 schedule is created **switched off** until a film has passed your look. |
| Page | `/kahaaniverse` | Character library with every sheet, genre mix editor, story backlog with scores, "Make this film" / "Start today's film". |
| Network fix | `studio/media/visuals.py` | Reference pictures are uploaded as ~25 KB JPEGs, with retries on corrupted uploads (the phone hotspot breaks larger PNG uploads with SSL "bad record MAC"). |

## Run it

```bash
cd ~/yt-automation-studio
.venv/bin/python -c "from studio import bootstrap; from studio.creative import film, kahaani; print(kahaani.build_sheets(1, film._gen))"
.venv/bin/python -c "from studio import bootstrap; from studio.creative import director; print(director.start_film(1, minutes=10))"
.venv/bin/python -m unittest tests.test_kahaani tests.test_director -v
```

Or use the **KahaaniVerse** page in the studio (admin): *Make missing pictures*, *Start today's film*.

## Research sources

| Source | Status |
|---|---|
| YouTube Data API search (official; titles, views, lengths only) | used, 5 genre queries a day (~510 quota units) |
| Google Trends daily feed, India | used |
| This channel's own analytics by genre | used once films are published (bonus in the concept score) |
| YouTube search suggestions | not used — there is no official API |
| Reddit | not used — needs a registered Reddit API app |

## Honest limits (free tools, this Mac)

* **Real motion is scarce.** Free image-to-video is about 4 Wan 2.2 clips (ZeroGPU) and 2 LTX-2 talking pieces a
  day. SadTalker on the Mac takes about 1 minute per second of speech. An 8–12 minute film has 100+ shots, so most
  shots are keyframes with a planned camera move, and they are **labelled** that way in the shot list and the QC
  report. A fully animated 10-minute film every day needs a paid GPU, which stays off at the ₹0 budget unless you
  say otherwise.
* **Pictures per day.** Cloudflare's free allowance is about 60–70 storyboard keyframes a day after the library is
  made. A 10-minute film may take more than one day of allowance, and it waits and continues by itself after the
  reset (05:30 IST).
* **Sheet layouts.** The image model copies the reference's framing, so the expression and emotion "sheets" come out
  as several full-body poses with different faces rather than a neat grid of heads. Small extras can creep in (a
  tattoo on Aarav's side view). These pictures are references for the model, not marketing art.

## Still to build (next phases)

1. **Dedicated vertical Short.** Re-stage the film's `short_scene` with 9:16 keyframes and its own hook card and
   captions, instead of re-framing the long film.
2. **Sound design.** Per-location ambience, foley on actions and genre music cues, mixed under the dialogue. The
   existing music and ambience beds are reused today.
3. **2.5D parallax** for still shots (layer the subject over its background), so stills read as camera moves through
   depth.
4. **Thumbnails.** Several concepts per film from the characters' emotion sheets plus the story situation; the
   experiment system picks the winner.
5. **Visual QC.** A contact sheet of every shot, a face-consistency check against the library (free vision model),
   regeneration of failed shots, and a QC report.
6. **Analytics loop.** Rolling 7-day and 28-day results per genre, hook and character, moving the genre mix and the
   concept scores.
