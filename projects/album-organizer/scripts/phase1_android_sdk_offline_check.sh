#!/bin/sh
# Verify only an already-configured local Android SDK; never install tooling.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROPERTIES=${PHOTO_ORGANIZER_ANDROID_LOCAL_PROPERTIES:-"$ROOT/apps/mobile/android/local.properties"}
SDK_PATH=

if [ -r "$PROPERTIES" ]; then
  SDK_PATH=$(sed -n 's/^sdk\.dir=//p' "$PROPERTIES" | head -n 1)
fi
if [ -z "$SDK_PATH" ]; then
  SDK_PATH=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}
fi

if [ -z "$SDK_PATH" ] || [ ! -d "$SDK_PATH/platforms" ] || [ ! -d "$SDK_PATH/build-tools" ]; then
  printf '%s\n' 'phase1_android_sdk_missing' >&2
  exit 1
fi

printf '%s\n' 'PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK'
