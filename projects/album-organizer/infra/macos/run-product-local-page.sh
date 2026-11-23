#!/bin/zsh
set -euo pipefail

config_file="${1:?runtime config path required}"
if [[ ! -f "$config_file" ]]; then
  print -u2 -- "product_local_page_runtime_config_missing"
  exit 1
fi
set -a
source "$config_file"
set +a

: "${PRODUCT_ENGINE_BACKEND:?required}"
: "${PRODUCT_ENGINE_DATABASE_URL:?required}"
: "${LOCAL_ALBUM_SYNC_TENANT_ID:?required}"

cd "$PRODUCT_ENGINE_BACKEND"
export APP_ENV=development
export DATABASE_URL="$PRODUCT_ENGINE_DATABASE_URL"
exec "$PRODUCT_ENGINE_BACKEND/.venv/bin/uvicorn" \
  product_engine.api.local_app:app \
  --host 127.0.0.1 \
  --port 8124

