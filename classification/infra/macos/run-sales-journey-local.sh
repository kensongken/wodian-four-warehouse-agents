#!/bin/zsh
set -euo pipefail
config_file="${1:?runtime config path required}"
[[ -f "$config_file" ]] || { print -u2 -- "sales_journey_runtime_config_missing"; exit 1; }
set -a; source "$config_file"; set +a
: "${SALES_JOURNEY_ROOT:?required}"
: "${NODE_RUNTIME_BIN:?required}"
: "${PNPM_BIN:?required}"
: "${PRODUCT_STRATEGY_SIGNING_SECRET:?required}"
: "${PRODUCT_STRATEGY_LOCAL_LEDGER_PATH:?required}"
cd "$SALES_JOURNEY_ROOT"
export PATH="$NODE_RUNTIME_BIN:/usr/bin:/bin:/usr/sbin:/sbin"
export NODE_ENV=development
export PRODUCT_STRATEGY_LOCAL_SYNTHETIC_ACCEPTANCE=desensitized-only
export SUPERVISION_LOCAL_SYNTHETIC_ACCEPTANCE=desensitized-only
export NEXT_PUBLIC_SUPERVISION_LOCAL_SYNTHETIC_ACCEPTANCE=desensitized-only
export SUPERVISION_LOCAL_SYNTHETIC_TENANT_ID="${LOCAL_ALBUM_SYNC_TENANT_ID:?required}"
export PRODUCT_IMAGE_ENGINE_BASE_URL=http://127.0.0.1:18023
exec "$PNPM_BIN" run dev:mini
