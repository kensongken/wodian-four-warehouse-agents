#!/bin/sh
# Summarize only local, fail-closed runtime prerequisites.  This never installs
# tooling, starts services, uploads media, or emits raw diagnostic output.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PHOTO_ORGANIZER_PYTHON:-"$ROOT/services/backend/.venv/bin/python"}
FLUTTER_SDK=${PHOTO_ORGANIZER_FLUTTER_SDK:-}
ANDROID_GATE=${PHOTO_ORGANIZER_ANDROID_GATE:-"$ROOT/scripts/phase1_android_native_offline_check.sh"}
ANDROID_SDK_GATE=${PHOTO_ORGANIZER_ANDROID_SDK_GATE:-"$ROOT/scripts/phase1_android_sdk_offline_check.sh"}
INTEGRATION_GATE=${PHOTO_ORGANIZER_INTEGRATION_GATE:-"$ROOT/scripts/phase1_isolated_integration_check.sh"}
IOS_GATE=${PHOTO_ORGANIZER_IOS_GATE:-"$ROOT/scripts/phase1_ios_simulator_offline_check.sh"}

if [ -n "$FLUTTER_SDK" ]; then
  mobile_output=$("$PYTHON_BIN" "$ROOT/scripts/check_mobile_sdk_gate.py" --flutter-sdk "$FLUTTER_SDK" 2>&1 || true)
else
  mobile_output=$("$PYTHON_BIN" "$ROOT/scripts/check_mobile_sdk_gate.py" 2>&1 || true)
fi
case "$mobile_output" in
  mobile_sdk_ready) mobile_state=ready ;;
  mobile_sdk_missing) mobile_state=missing ;;
  *) mobile_state=unknown ;;
esac

android_output=$(sh "$ANDROID_GATE" 2>&1 || true)
case "$android_output" in
  PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK) android_state=ready ;;
  phase1_android_java_missing) android_state=java_missing ;;
  phase1_android_gradle_missing) android_state=gradle_missing ;;
  *) android_state=unknown ;;
esac

android_sdk_output=$(sh "$ANDROID_SDK_GATE" 2>&1 || true)
case "$android_sdk_output" in
  PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK) android_sdk_state=ready ;;
  phase1_android_sdk_missing) android_sdk_state=missing ;;
  *) android_sdk_state=unknown ;;
esac

ios_output=$(sh "$IOS_GATE" 2>&1 || true)
case "$ios_output" in
  PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK) ios_state=ready ;;
  phase1_ios_simulator_sdk_missing) ios_state=simulator_sdk_missing ;;
  phase1_ios_simulator_runtime_missing) ios_state=simulator_runtime_missing ;;
  phase1_ios_simulator_device_missing) ios_state=simulator_device_missing ;;
  *) ios_state=unknown ;;
esac

integration_output=$(sh "$INTEGRATION_GATE" 2>&1 || true)
case "$integration_output" in
  PHASE1_ISOLATED_INTEGRATION_CHECK_OK) integration_state=ready ;;
  phase1_integration_environment_missing) integration_state=environment_missing ;;
  *) integration_state=unknown ;;
esac

printf 'PHASE1_RUNTIME_READINESS mobile=%s android=%s android_sdk=%s ios=%s integration=%s\n' \
  "$mobile_state" "$android_state" "$android_sdk_state" "$ios_state" "$integration_state"
