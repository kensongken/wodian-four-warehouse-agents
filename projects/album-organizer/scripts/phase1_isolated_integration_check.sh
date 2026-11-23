#!/bin/sh
# Run only explicitly provisioned PostgreSQL/MinIO integration checks.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTEST_BIN=${PHOTO_ORGANIZER_PYTEST:-"$ROOT/services/backend/.venv/bin/pytest"}

if [ -z "${PHOTO_ORGANIZER_TEST_DATABASE_URL:-}" ] || \
  [ -z "${MINIO_TEST_ENDPOINT:-}" ] || \
  [ -z "${MINIO_TEST_BUCKET:-}" ] || \
  [ -z "${MINIO_TEST_ACCESS_KEY:-}" ] || \
  [ -z "${MINIO_TEST_SECRET_KEY:-}" ]; then
  printf '%s\n' 'phase1_integration_environment_missing' >&2
  exit 1
fi
case "$MINIO_TEST_ENDPOINT" in
  http://127.0.0.1:* | http://localhost:*) ;;
  *)
    printf '%s\n' 'phase1_integration_endpoint_invalid' >&2
    exit 1
    ;;
esac
case "$PHOTO_ORGANIZER_TEST_DATABASE_URL" in
  postgresql://127.0.0.1:* | postgresql://localhost:* | \
  postgresql://*@127.0.0.1:* | postgresql://*@localhost:* | \
  postgresql+asyncpg://127.0.0.1:* | postgresql+asyncpg://localhost:* | \
  postgresql+asyncpg://*@127.0.0.1:* | postgresql+asyncpg://*@localhost:*) ;;
  *)
    printf '%s\n' 'phase1_integration_database_invalid' >&2
    exit 1
    ;;
esac
case "$PHOTO_ORGANIZER_TEST_DATABASE_URL" in
  */photo_organizer_test | */photo_organizer_test\?*) ;;
  *)
    printf '%s\n' 'phase1_integration_database_name_invalid' >&2
    exit 1
    ;;
esac
case "$MINIO_TEST_BUCKET" in
  photo-organizer-test | photo-organizer-test-*) ;;
  *)
    printf '%s\n' 'phase1_integration_bucket_invalid' >&2
    exit 1
    ;;
esac
case "$MINIO_TEST_ACCESS_KEY:$MINIO_TEST_SECRET_KEY" in
  test-*:test-*) ;;
  *)
    printf '%s\n' 'phase1_integration_credentials_invalid' >&2
    exit 1
    ;;
esac
if [ ! -x "$PYTEST_BIN" ]; then
  printf '%s\n' 'phase1_integration_pytest_missing' >&2
  exit 1
fi

cd "$ROOT/services/backend"
if ! "$PYTEST_BIN" tests/integration -q -m integration >/dev/null 2>/dev/null; then
  printf '%s\n' 'phase1_integration_test_failed' >&2
  exit 1
fi
printf '%s\n' 'PHASE1_ISOLATED_INTEGRATION_CHECK_OK'
