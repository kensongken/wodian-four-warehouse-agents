#!/bin/zsh
set -euo pipefail
config_file="${1:?runtime config path required}"
[[ -f "$config_file" ]] || { print -u2 -- "sales_outbox_runtime_config_missing"; exit 1; }
set -a; source "$config_file"; set +a
: "${PRODUCT_ENGINE_BACKEND:?required}"
: "${PRODUCT_ENGINE_DATABASE_URL:?required}"
: "${PRODUCT_STRATEGY_SIGNING_SECRET:?required}"
: "${SALES_WEBHOOK_URL:?required}"
cd "$PRODUCT_ENGINE_BACKEND"
export APP_ENV=development
export DATABASE_URL="$PRODUCT_ENGINE_DATABASE_URL"
export SALES_SIGNING_SECRET="$PRODUCT_STRATEGY_SIGNING_SECRET"
export SALES_WEBHOOK_URL
export PYTHONPATH="$PRODUCT_ENGINE_BACKEND/src"
exec "$PRODUCT_ENGINE_BACKEND/.venv/bin/python" \
  -m product_engine.workers.outbox_daemon --limit 100 --interval-seconds 2
