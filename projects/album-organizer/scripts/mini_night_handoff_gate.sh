#!/bin/sh
# Verify a local-only, synthetic Mini candidate before any separate handoff.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
GIT_BIN=${PHOTO_ORGANIZER_GIT:-git}

if ! "$GIT_BIN" diff --check >/dev/null 2>/dev/null; then
  printf '%s\n' 'mini_candidate_diff_invalid' >&2
  exit 1
fi
if ! WORKTREE_STATUS=$("$GIT_BIN" status --porcelain 2>/dev/null); then
  printf '%s\n' 'mini_candidate_worktree_status_invalid' >&2
  exit 1
fi
if [ -n "$WORKTREE_STATUS" ]; then
  printf '%s\n' 'mini_candidate_worktree_dirty' >&2
  exit 1
fi
if ! CANDIDATE_SHA=$("$GIT_BIN" rev-parse --verify 'HEAD^{commit}' 2>/dev/null); then
  printf '%s\n' 'mini_candidate_sha_invalid' >&2
  exit 1
fi
if [ "${#CANDIDATE_SHA}" -ne 40 ] || \
  ! printf '%s' "$CANDIDATE_SHA" | LC_ALL=C grep -Eq '^[0-9a-f]{40}$'; then
  printf '%s\n' 'mini_candidate_sha_invalid' >&2
  exit 1
fi

sh "$ROOT/scripts/mini_night_check.sh"
printf 'MINI_NIGHT_HANDOFF_GATE_OK sha=%s scope=synthetic_only\n' "$CANDIDATE_SHA"
