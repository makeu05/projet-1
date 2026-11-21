#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/build_dataset.sh <categorie> <nb_videos> "<recherche ou URL>"
# Ex:    ./scripts/build_dataset.sh animals_only 5 "dog running park"

CATEGORY="${1:-animals_only}"
COUNT="${2:-3}"
QUERY="${3:-dog playing}"
OUT_DIR="$HOME/vidp-edge/dataset/${CATEGORY}"

mkdir -p "$OUT_DIR"

if [[ "$QUERY" == http* ]]; then
  TARGET="$QUERY"
else
  TARGET="ytsearch${COUNT}:${QUERY}"
fi

echo "[dataset] Telechargement -> $OUT_DIR ($TARGET)"
yt-dlp \
  --match-filter "duration < 300" \
  --download-sections "*0:05-0:35" \
  --force-keyframes-at-cuts \
  -f "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]" \
  --merge-output-format mp4 \
  --ignore-errors \
  -o "${OUT_DIR}/%(id)s.mp4" \
  "$TARGET"
