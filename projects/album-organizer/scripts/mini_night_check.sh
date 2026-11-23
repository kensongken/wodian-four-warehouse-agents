#!/bin/sh
# Run only the locally reproducible, synthetic Phase 1 checks for a Mini handoff.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PHOTO_ORGANIZER_PYTHON:-"$ROOT/services/backend/.venv/bin/python"}
FIXTURE_COUNT=${PHOTO_ORGANIZER_FIXTURE_COUNT:-1000}

if [ ! -x "$PYTHON_BIN" ]; then
  printf '%s\n' 'mini_python_missing' >&2
  exit 1
fi

if ! TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/photo-organizer-mini.XXXXXX" 2>/dev/null); then
  printf '%s\n' 'mini_temp_directory_unavailable' >&2
  exit 1
fi

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT HUP INT TERM

if ! STATIC_OUTPUT=$("$PYTHON_BIN" "$ROOT/scripts/verify_mobile_controlled_upload_static.py" 2>&1); then
  printf '%s\n' 'mini_mobile_static_check_failed' >&2
  exit 1
fi
if [ "$STATIC_OUTPUT" != 'mobile_controlled_upload_static_ok' ]; then
  printf '%s\n' 'mini_mobile_static_check_invalid' >&2
  exit 1
fi
printf '%s\n' "$STATIC_OUTPUT"

if ! OFFLINE_OUTPUT=$("$PYTHON_BIN" "$ROOT/scripts/verify_mobile_controlled_upload_offline.py" 2>&1); then
  printf '%s\n' 'mini_mobile_offline_check_failed' >&2
  exit 1
fi
if [ "$OFFLINE_OUTPUT" != 'mobile_controlled_upload_offline_ok sdk=missing' ] && \
  [ "$OFFLINE_OUTPUT" != 'mobile_controlled_upload_offline_ok sdk=ready' ]; then
  printf '%s\n' 'mini_mobile_offline_check_invalid' >&2
  exit 1
fi
printf '%s\n' "$OFFLINE_OUTPUT"

if SDK_OUTPUT=$("$PYTHON_BIN" "$ROOT/scripts/check_mobile_sdk_gate.py" 2>&1); then
  if [ "$SDK_OUTPUT" != 'mobile_sdk_ready' ]; then
    printf '%s\n' 'mini_mobile_sdk_gate_invalid' >&2
    exit 1
  fi
  printf '%s\n' "$SDK_OUTPUT"
  SDK_STATUS=ready
else
  SDK_EXIT=$?
  if [ "$SDK_EXIT" -ne 1 ] || [ "$SDK_OUTPUT" != 'mobile_sdk_missing' ]; then
    printf '%s\n' 'mini_mobile_sdk_gate_invalid' >&2
    exit 1
  fi
  printf '%s\n' "$SDK_OUTPUT" >&2
  SDK_STATUS=missing
fi

if ! "$PYTHON_BIN" "$ROOT/scripts/generate_fixture_images.py" \
  --count "$FIXTURE_COUNT" \
  --output "$TEMP_DIR/fixtures" >/dev/null 2>/dev/null; then
  printf '%s\n' 'mini_fixture_generation_failed' >&2
  exit 1
fi
EXPECTED_SMOKE_OUTPUT="OFFLINE PASS: $FIXTURE_COUNT accepted, $FIXTURE_COUNT uploaded, 0 omitted, task sealed once"
if ! SMOKE_OUTPUT=$("$PYTHON_BIN" "$ROOT/scripts/ingestion_smoke.py" \
  --offline-manifest "$TEMP_DIR/fixtures/manifest.json" 2>/dev/null); then
  printf '%s\n' 'mini_offline_smoke_failed' >&2
  exit 1
fi
if [ "$SMOKE_OUTPUT" != "$EXPECTED_SMOKE_OUTPUT" ]; then
  printf '%s\n' 'mini_offline_smoke_invalid' >&2
  exit 1
fi
printf '%s\n' "$SMOKE_OUTPUT"

printf 'MINI_OFFLINE_CHECK_OK count=%s sdk=%s\n' "$FIXTURE_COUNT" "$SDK_STATUS"
