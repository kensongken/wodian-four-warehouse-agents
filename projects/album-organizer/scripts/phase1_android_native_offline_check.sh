#!/bin/sh
# Run Android unit tests only with already-installed local tooling.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
GRADLE_BIN=${PHOTO_ORGANIZER_GRADLE:-"$ROOT/apps/mobile/android/gradlew"}
JAVA_BIN=${PHOTO_ORGANIZER_JAVA:-java}

if ! command -v "$JAVA_BIN" >/dev/null 2>&1 || ! "$JAVA_BIN" -version >/dev/null 2>&1; then
  printf '%s\n' 'phase1_android_java_missing' >&2
  exit 1
fi

if [ ! -x "$GRADLE_BIN" ]; then
  printf '%s\n' 'phase1_android_gradle_missing' >&2
  exit 1
fi

cd "$ROOT/apps/mobile/android"
if ! "$GRADLE_BIN" :app:testDebugUnitTest --offline --no-daemon >/dev/null 2>/dev/null; then
  printf '%s\n' 'phase1_android_native_test_failed' >&2
  exit 1
fi

printf '%s\n' 'PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK'
