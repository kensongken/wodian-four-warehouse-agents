#!/bin/zsh
set -euo pipefail

config_file="${1:?runtime config path required}"
if [[ ! -f "$config_file" ]]; then
  print -u2 -- "album_bridge_runtime_config_missing"
  exit 1
fi
set -a
source "$config_file"
set +a

: "${PHOTO_ORGANIZER_BACKEND:?required}"
: "${PHOTO_ORGANIZER_DATABASE_URL:?required}"
: "${PRODUCT_IMAGE_SET_SIGNING_SECRET:?required}"
: "${PRODUCT_IMAGE_SET_ENDPOINT:?required}"

cd "$PHOTO_ORGANIZER_BACKEND"
export APP_ENV=development
export DATABASE_URL="$PHOTO_ORGANIZER_DATABASE_URL"
export PYTHONPATH="$PHOTO_ORGANIZER_BACKEND/src"
exec "$PHOTO_ORGANIZER_BACKEND/.venv/bin/python" \
  -m photo_organizer.cli.product_image_set_bridge \
  --endpoint "$PRODUCT_IMAGE_SET_ENDPOINT" \
  --limit 100 \
  --interval-seconds 5

