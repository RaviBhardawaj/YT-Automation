# Offline capabilities

What works with no internet (the studio itself runs on this Mac at http://127.0.0.1:8765):

| Works offline | Notes |
|---|---|
| Dashboard, library, video pages, history, Activity log, audit trail | all local |
| Editing scripts, approving, downloading finished videos | local |
| Voices: Kokoro, Piper, Chatterbox clone, Parler | local models |
| Pictures: local FLUX on the Mac | ~1–2 min per picture |
| Talking faces: SadTalker | local |
| Rendering, captions, thumbnails, QA, Shorts clipping, Upload & Edit, Whisper transcription | FFmpeg + local models |
| Backups and restore drill | local |

| Needs internet (waits, never fails silently) | Why |
|---|---|
| Script writing / review | no local writer model installed (pull one in Ollama to make writing offline-capable) |
| Research, news stories, stock pictures, vision checks | online sources |
| Online FLUX, ZeroGPU motion, LTX-2 talking | online services |
| Upload, analytics, comments, growth numbers | YouTube APIs |

`STUDIO_OFFLINE=1` (used by the test suite) makes the studio skip online picture services entirely.

**Tested:** the test suite runs with `STUDIO_OFFLINE=1` (388+ tests). **Not tested:** a full production with the
network physically disconnected — the online-only steps above are expected to wait and retry (network errors are
treated as temporary), but this was not observed end to end.

To rebuild on a new Mac without the internet you also need a pip wheel cache (`pip download -r requirements-lock.txt
-d <folder>`) and the model vault — neither is made automatically.
