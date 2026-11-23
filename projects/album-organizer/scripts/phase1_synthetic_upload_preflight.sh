#!/bin/sh
# Fail closed before a synthetic upload exercise. This never selects or uploads media.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
READINESS_GATE=${PHOTO_ORGANIZER_RUNTIME_READINESS_GATE:-"$ROOT/scripts/phase1_runtime_readiness.sh"}
EXPECTED='PHASE1_RUNTIME_READINESS mobile=ready android=ready android_sdk=ready ios=ready integration=ready'

readiness_output=$(sh "$READINESS_GATE" 2>&1 || true)
if [ "$readiness_output" != "$EXPECTED" ]; then
  printf '%s\n' 'phase1_synthetic_upload_preflight_not_ready' >&2
  exit 1
fi

printf '%s\n' 'PHASE1_SYNTHETIC_UPLOAD_PREFLIGHT_OK'
