#!/bin/sh
set -eu

if [ "$#" -ne 2 ] || [ "$1" != "--output" ]; then
  echo "usage: scripts/build_macos_vision_foreground.sh --output ABSOLUTE_OR_RELATIVE_PATH" >&2
  exit 2
fi

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output=$2
case "$output" in
  /*) ;;
  *) output="$repository_root/$output" ;;
esac

if [ "$(uname -s)" != "Darwin" ] || ! command -v xcrun >/dev/null 2>&1; then
  echo "foreground_extractor_unavailable" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$output")"
xcrun swiftc \
  -O \
  -framework Foundation \
  -framework Vision \
  -framework CoreImage \
  -framework ImageIO \
  -framework UniformTypeIdentifiers \
  "$repository_root/tools/macos_vision_foreground/main.swift" \
  -o "$output"
chmod 700 "$output"

echo "built=$output"
printf "smoke_command=smoke_dir=\$(mktemp -d /tmp/opc-foreground-smoke.XXXXXX); cp '%s' \"\$smoke_dir/source.png\"; '%s' --input \"\$smoke_dir/source.png\" --mask-output \"\$smoke_dir/mask.png\" --foreground-output \"\$smoke_dir/foreground.png\"\n" \
  "$repository_root/tests/fixtures/foreground/simple-product.png" \
  "$output"
