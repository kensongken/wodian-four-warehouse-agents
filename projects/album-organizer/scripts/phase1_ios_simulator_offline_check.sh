#!/bin/sh
# Verify an installed Simulator SDK, runtime, and usable virtual device.
# This check never downloads, boots a device, or opens the photo library.
set -eu

if ! command -v xcrun >/dev/null 2>&1; then
  printf '%s\n' 'phase1_ios_simulator_sdk_missing' >&2
  exit 1
fi

SDK_PATH=$(xcrun --sdk iphonesimulator --show-sdk-path 2>/dev/null) || {
  printf '%s\n' 'phase1_ios_simulator_sdk_missing' >&2
  exit 1
}
if [ ! -d "$SDK_PATH" ]; then
  printf '%s\n' 'phase1_ios_simulator_sdk_missing' >&2
  exit 1
fi

RUNTIMES=$(xcrun simctl list runtimes available 2>/dev/null) || {
  printf '%s\n' 'phase1_ios_simulator_runtime_missing' >&2
  exit 1
}
if ! printf '%s\n' "$RUNTIMES" | grep -Eq '^iOS .+ - com\.apple\.CoreSimulator\.SimRuntime\.iOS-'; then
  printf '%s\n' 'phase1_ios_simulator_runtime_missing' >&2
  exit 1
fi

DEVICES=$(xcrun simctl list devices available 2>/dev/null) || {
  printf '%s\n' 'phase1_ios_simulator_device_missing' >&2
  exit 1
}
if ! printf '%s\n' "$DEVICES" | awk '
  /^-- iOS .+ --$/ { in_ios = 1; next }
  /^-- / { in_ios = 0; next }
  in_ios && /^[[:space:]]+.+ \([0-9A-Fa-f-]{36}\) \((Booted|Shutdown)\)/ {
    found = 1
    exit
  }
  END { if (!found) exit 1 }
'; then
  printf '%s\n' 'phase1_ios_simulator_device_missing' >&2
  exit 1
fi

printf '%s\n' 'PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK'
