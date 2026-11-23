#!/bin/zsh
set -euo pipefail

config_file="${1:?runtime config path required}"
if [[ ! -f "$config_file" ]]; then
  print -u2 -- "product_receiver_runtime_config_missing"
  exit 1
fi
set -a
source "$config_file"
set +a

: "${PRODUCT_ENGINE_BACKEND:?required}"
: "${PRODUCT_ENGINE_DATABASE_URL:?required}"
: "${PRODUCT_IMAGE_SET_SIGNING_SECRET:?required}"
: "${PRODUCT_STRATEGY_SIGNING_SECRET:?required}"
: "${SALES_WEBHOOK_URL:?required}"

cd "$PRODUCT_ENGINE_BACKEND"
export APP_ENV=development
export DATABASE_URL="$PRODUCT_ENGINE_DATABASE_URL"
export ALBUM_SIGNING_SECRET="$PRODUCT_IMAGE_SET_SIGNING_SECRET"
export SALES_SIGNING_SECRET="$PRODUCT_STRATEGY_SIGNING_SECRET"
export SALES_WEBHOOK_URL
exec "$PRODUCT_ENGINE_BACKEND/.venv/bin/uvicorn" \
  product_engine.api.main:app \
  --host 127.0.0.1 \
  --port 18023
