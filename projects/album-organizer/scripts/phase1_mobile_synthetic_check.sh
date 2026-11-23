#!/bin/sh
# Run only SDK-local Flutter validation; never access a photo library or storage.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FLUTTER_BIN=${PHOTO_ORGANIZER_FLUTTER:-flutter}
DART_BIN=${PHOTO_ORGANIZER_DART:-dart}
RUN_ANDROID_TESTS=${PHOTO_ORGANIZER_RUN_ANDROID_TESTS:-0}
APPROVED_JAVA_HOME=${PHOTO_ORGANIZER_JAVA_HOME:-${JAVA_HOME:-}}
FLUTTER_CONFIG_HOME=${PHOTO_ORGANIZER_FLUTTER_CONFIG_HOME:-${TMPDIR:-/tmp}/photo-organizer-flutter-config}

if [ "$RUN_ANDROID_TESTS" != '0' ] && [ "$RUN_ANDROID_TESTS" != '1' ]; then
  printf '%s\n' 'phase1_mobile_android_option_invalid' >&2
  exit 1
fi

if ! command -v "$FLUTTER_BIN" >/dev/null 2>&1; then
  printf '%s\n' 'phase1_mobile_flutter_missing' >&2
  exit 1
fi
if ! command -v "$DART_BIN" >/dev/null 2>&1; then
  printf '%s\n' 'phase1_mobile_dart_missing' >&2
  exit 1
fi
mkdir -p "$FLUTTER_CONFIG_HOME"

cd "$ROOT/apps/mobile"
if ! BOT=true XDG_CONFIG_HOME="$FLUTTER_CONFIG_HOME" "$FLUTTER_BIN" pub get --offline >/dev/null 2>/dev/null; then
  printf '%s\n' 'phase1_mobile_pub_get_failed' >&2
  exit 1
fi
if ! "$DART_BIN" analyze >/dev/null 2>/dev/null; then
  printf '%s\n' 'phase1_mobile_analyze_failed' >&2
  exit 1
fi
if ! BOT=true XDG_CONFIG_HOME="$FLUTTER_CONFIG_HOME" "$FLUTTER_BIN" test >/dev/null 2>/dev/null; then
  printf '%s\n' 'phase1_mobile_test_failed' >&2
  exit 1
fi

if [ "$RUN_ANDROID_TESTS" = '1' ]; then
  GRADLE_BIN=${PHOTO_ORGANIZER_GRADLE:-"$ROOT/apps/mobile/android/gradlew"}
  if [ ! -x "$GRADLE_BIN" ]; then
    printf '%s\n' 'phase1_mobile_gradle_missing' >&2
    exit 1
  fi
  cd "$ROOT/apps/mobile/android"
  if ! JAVA_HOME="$APPROVED_JAVA_HOME" "$GRADLE_BIN" :app:testDebugUnitTest --offline --no-daemon >/dev/null 2>/dev/null; then
    printf '%s\n' 'phase1_mobile_android_test_failed' >&2
    exit 1
  fi
  printf '%s\n' 'PHASE1_MOBILE_SYNTHETIC_CHECK_OK android=checked'
  exit 0
fi

printf '%s\n' 'PHASE1_MOBILE_SYNTHETIC_CHECK_OK android=not_requested'
