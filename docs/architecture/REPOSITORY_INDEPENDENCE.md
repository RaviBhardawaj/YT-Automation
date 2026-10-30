# Repository independence

**Goal:** the studio keeps working if any public repository (GitHub or Hugging Face) is deleted, renamed, made
private or blocked.

## Runtime: no public repository is used
- `grep -rn "github.com\|huggingface.co" studio/` (31 Oct 2026) finds only: model **install** URLs in
  `media/tts.py` (Kokoro, Piper, Parler) and a help link to the Hugging Face token page. No code is fetched or
  executed from a repository while the studio runs.
- The reference repository (`darkzOGx/youtube-automation-agent`) is not referenced anywhere in `studio/`.
- Hugging Face **Spaces** (Wan 2.2, LTX-2, FLUX) are online services, not code we depend on: each has a local or
  labelled fallback (see `PROVIDER_FALLBACK_MATRIX.md`).

## Install time: everything essential is in this repository or verifiable
| Part | Before | Now |
|---|---|---|
| SadTalker code (with our Python 3.12 / NumPy / Apple-GPU / PATH fixes) | cloned from GitHub, patches only on this Mac | `install/vendor/sadtalker/sadtalker-src-cd4c046-patched.tar.gz` + `local-patches.diff` + Apache-2.0 licence |
| SadTalker Python packages | unpinned | `install/vendor/sadtalker/requirements-lock.txt` |
| SadTalker weights | downloaded from GitHub releases | pinned Hugging Face revisions + SHA-256 (`weights.sha256`); installer accepts a local folder (`WEIGHTS_DIR=`) |
| All installed models | only on this Mac | `scripts/model_vault.sh <folder>` copies them with a SHA-256 manifest; `... restore` puts them back and verifies |

## Simulation (31 Oct 2026, real run)
`install/install-sadtalker.sh` into an empty temporary folder with `WEIGHTS_DIR` pointing at a local copy (GitHub
and Hugging Face not used): rebuilt in **91 s**, every weight checksum **OK**, `import torch, face_alignment,
facexlib, kornia` **OK**. (Python packages still came from PyPI; a fully offline rebuild needs a pip wheel cache —
see `OFFLINE_CAPABILITIES.md`.)

## Remaining dependencies (honest list)
- PyPI for Python packages at install time.
- Homebrew for Python/FFmpeg at install time.
- The model vault is only as good as its last run — run it after installing a new model, onto an external drive
  (the Mac has ~12 GB free; the vault is ~20 GB).
