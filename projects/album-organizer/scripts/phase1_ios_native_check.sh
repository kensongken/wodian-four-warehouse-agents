#!/bin/sh
# Build the Flutter iOS app and run native tests against an installed Simulator.
# This check stays local and never downloads runtimes, boots devices, or reads photos.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FLUTTER_BIN=${PHOTO_ORGANIZER_FLUTTER:-flutter}
XCODEBUILD_BIN=${PHOTO_ORGANIZER_XCODEBUILD:-xcodebuild}
FLUTTER_CONFIG_HOME=${PHOTO_ORGANIZER_FLUTTER_CONFIG_HOME:-"${TMPDIR:-/tmp}/photo-organizer-flutter-config"}
IOS_GATE=${PHOTO_ORGANIZER_IOS_GATE:-"$ROOT/scripts/phase1_ios_simulator_offline_check.sh"}

if ! command -v "$FLUTTER_BIN" >/dev/null 2>&1; then
  printf '%s\n' 'phase1_ios_native_flutter_missing' >&2
  exit 1
fi
if ! command -v "$XCODEBUILD_BIN" >/dev/null 2>&1; then
  printf '%s\n' 'phase1_ios_native_xcodebuild_missing' >&2
  exit 1
fi

sh "$IOS_GATE" >/dev/null

DEVICE_LIST=$(xcrun simctl list devices available 2>/dev/null) || {
  printf '%s\n' 'phase1_ios_simulator_device_missing' >&2
  exit 1
}
DEVICE_ID=$(printf '%s\n' "$DEVICE_LIST" | awk '
  /^-- iOS .+ --$/ { in_ios = 1; next }
  /^-- / { in_ios = 0; next }
  in_ios && match($0, /[0-9A-Fa-f-]{36}/) && $0 ~ /\((Booted|Shutdown)\)/ {
    print substr($0, RSTART, RLENGTH)
    exit
  }
')
if [ -z "$DEVICE_ID" ]; then
  printf '%s\n' 'phase1_ios_simulator_device_missing' >&2
  exit 1
fi

mkdir -p "$FLUTTER_CONFIG_HOME"
cd "$ROOT/apps/mobile"

if ! BOT=true XDG_CONFIG_HOME="$FLUTTER_CONFIG_HOME" "$FLUTTER_BIN" pub get --offline; then
  printf '%s\n' 'phase1_ios_native_pub_get_failed' >&2
  exit 1
fi
if ! BOT=true XDG_CONFIG_HOME="$FLUTTER_CONFIG_HOME" "$FLUTTER_BIN" build ios --simulator --debug; then
  printf '%s\n' 'phase1_ios_native_build_failed' >&2
  exit 1
fi
if ! "$XCODEBUILD_BIN" test -quiet \
  -project ios/Runner.xcodeproj \
  -scheme Runner \
  -destination "platform=iOS Simulator,id=$DEVICE_ID" \
  CODE_SIGNING_ALLOWED=NO; then
  printf '%s\n' 'phase1_ios_native_test_failed' >&2
  exit 1
fi

printf '%s\n' 'PHASE1_IOS_NATIVE_CHECK_OK'
