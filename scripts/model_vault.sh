#!/usr/bin/env bash
# Keep a private copy of every model the studio has downloaded, so a new or repaired Mac can be set up even if
# GitHub / Hugging Face / a model's page disappears. Copies + a SHA-256 manifest; restore = run with "restore".
#   scripts/model_vault.sh /Volumes/Backup/studio-models            # save (only new/changed files are copied)
#   scripts/model_vault.sh /Volumes/Backup/studio-models restore    # put them back
set -euo pipefail
VAULT="${1:?give a folder, e.g. /Volumes/Backup/studio-models}"
MODE="${2:-save}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PARTS=("$ROOT/data/voices" "$ROOT/data/models" "$HOME/.cache/huggingface/hub" "$HOME/sadtalker/checkpoints"
       "$HOME/sadtalker/gfpgan" "$HOME/.cache/torch/hub/checkpoints")
mkdir -p "$VAULT"
for p in "${PARTS[@]}"; do
  name="$(echo "${p#$HOME/}" | tr '/' '_')"
  if [ "$MODE" = "restore" ]; then
    [ -d "$VAULT/$name" ] && mkdir -p "$p" && rsync -a "$VAULT/$name/" "$p/" && echo "restored $p"
  elif [ -d "$p" ]; then
    rsync -a --copy-links "$p/" "$VAULT/$name/" && echo "saved $p"
  fi
done
if [ "$MODE" != "restore" ]; then
  (cd "$VAULT" && find . -type f ! -name MANIFEST.sha256 -print0 | xargs -0 shasum -a 256 > MANIFEST.sha256)
  echo "manifest: $VAULT/MANIFEST.sha256 ($(wc -l < "$VAULT/MANIFEST.sha256") files)"
else
  (cd "$VAULT" && shasum -a 256 -c --quiet MANIFEST.sha256) && echo "all files verified"
fi
