#!/usr/bin/env bash
# Talking presenter on this Mac (SadTalker, Apache-2.0) — installed WITHOUT GitHub: the patched source is kept in
# this repository (install/vendor/sadtalker). Python packages come from the pinned lock file; model weights from
# Hugging Face at pinned revisions, or from a local folder (WEIGHTS_DIR=/path) when Hugging Face is unavailable.
# Every weight file is checked against install/vendor/sadtalker/weights.sha256.
#   install/install-sadtalker.sh                     # to ~/sadtalker
#   WEIGHTS_DIR=/Volumes/Backup/sadtalker install/install-sadtalker.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V="$HERE/vendor/sadtalker"
DEST="${SADTALKER_DIR:-$HOME/sadtalker}"
PY="${PYTHON:-/opt/homebrew/bin/python3.12}"
mkdir -p "$DEST" && cd "$DEST"
tar -xzf "$V/sadtalker-src-cd4c046-patched.tar.gz"          # patches already applied (see local-patches.diff)
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r "$V/requirements-lock.txt"
mkdir -p checkpoints gfpgan/weights "$HOME/.cache/torch/hub/checkpoints"
fetch() {  # fetch <repo> <revision> <path in repo> <destination>
  local name; name="$(basename "$4")"
  if [ -f "$4" ]; then return; fi
  if [ -n "${WEIGHTS_DIR:-}" ] && [ -f "$WEIGHTS_DIR/$name" ]; then cp "$WEIGHTS_DIR/$name" "$4"; return; fi
  curl -4 -fL --retry 5 -C - -o "$4.part" "https://huggingface.co/$1/resolve/$2/$3" && mv "$4.part" "$4"
}
R1=df6f230f2fbec618d69e47229ea32dd224fc9c5c   # vinthony/SadTalker-V002rc
R2=7caf8d94b767aa60b5a6768ada79fde0c6713b76   # camenduru/SadTalker
R3=4aedd064359e623398a2d73eb8c253ebb2bd516c   # vinthony/SadTalker
for f in SadTalker_V0.0.2_256.safetensors mapping_00109-model.pth.tar mapping_00229-model.pth.tar; do
  fetch vinthony/SadTalker-V002rc $R1 "$f" "checkpoints/$f"; done
for f in alignment_WFLW_4HG.pth detection_Resnet50_Final.pth; do
  fetch camenduru/SadTalker $R2 "new/gfpgan/weights/$f" "gfpgan/weights/$f"; done
for f in 2DFAN4-cd938726ad.zip s3fd-619a316812.pth; do
  fetch vinthony/SadTalker $R3 "hub/checkpoints/$f" "$HOME/.cache/torch/hub/checkpoints/$f"; done
sed "s|~|$HOME|" "$V/weights.sha256" | sed "s|  checkpoints/|  $DEST/checkpoints/|; s|  gfpgan/|  $DEST/gfpgan/|" | shasum -a 256 -c -
echo "SadTalker ready in $DEST"
