#!/bin/zsh
set -euo pipefail

runtime_root=${LOCAL_STYLE_RUNTIME_ROOT:-"$PWD/.local-style-runtime"}
/bin/mkdir -p "$runtime_root/assets"
/bin/chmod 700 "$runtime_root" "$runtime_root/assets"
export DATABASE_URL="sqlite:///$runtime_root/opc_style.sqlite3"
export LOCAL_STYLE_ASSET_ROOT="$runtime_root/assets"

foreground_binary="$runtime_root/bin/macos-vision-foreground"
if [[ ! -x "$foreground_binary" ]]; then
  ./scripts/build_macos_vision_foreground.sh --output "$foreground_binary"
fi
export FOREGROUND_EXTRACTOR_ENABLED=true
export FOREGROUND_EXTRACTOR_BINARY_PATH="$foreground_binary"
export FOREGROUND_EXTRACTOR_VERSION=macos-vision-foreground-v1

pipeline_link="$PWD/.local-style-assets/model-runtimes/sam2-birefnet-biref-primary-candidate"
pipeline_root=$(CDPATH= cd -- "$pipeline_link" && pwd -P)
export FOREGROUND_PIPELINE_ENABLED=true
export FOREGROUND_PIPELINE_EXECUTABLE_PATH="$pipeline_root/runner"
export FOREGROUND_PIPELINE_MODEL_ROOT="$pipeline_root"
export FOREGROUND_PIPELINE_MANIFEST_PATH="$pipeline_root/pipeline-manifest.json"
export FOREGROUND_PIPELINE_MANIFEST_SHA256=504791f0ddf7fddd3ef50150ef86633c908f87e6303deafcfc4e7cc81999992b
export FOREGROUND_PIPELINE_TIMEOUT_SECONDS=240
export FOREGROUND_PIPELINE_MAX_OUTPUT_BYTES=20971520

pilot_openai_key="${OPENAI_API_KEY:?Set OPENAI_API_KEY before starting a paid pilot}"
if [[ "$pilot_openai_key" != sk-* ]]; then
  print -u2 'REAL_IMAGE_API_KEY is missing or invalid in macOS Keychain'
  exit 2
fi
trap 'unset pilot_openai_key REAL_STYLE_ANALYSIS_API_KEY REAL_PRODUCT_IDENTITY_API_KEY REAL_IMAGE_API_KEY REAL_FIDELITY_API_KEY OPC_STYLE_REAL_BACKGROUND_API_KEY MISSING_PART_COMPLETION_API_KEY' EXIT

export REAL_IMAGE_ENABLED=true
export REAL_IMAGE_API_KEY="$pilot_openai_key"
export REAL_IMAGE_MODEL=gpt-image-2
export REAL_IMAGE_MAX_RUN_USD=3.00
export REAL_IMAGE_CANDIDATE_COUNT=1
export OPC_STYLE_REAL_BACKGROUND_ENABLED=true
export OPC_STYLE_REAL_BACKGROUND_API_KEY="$pilot_openai_key"
export OPC_STYLE_REAL_BACKGROUND_MODEL=gpt-image-2
export OPC_STYLE_REAL_BACKGROUND_MAX_RUN_USD=0.30
export OPC_STYLE_REAL_BACKGROUND_TIMEOUT_SECONDS=300
export REAL_STYLE_ANALYSIS_ENABLED=true
export REAL_STYLE_ANALYSIS_API_KEY="$pilot_openai_key"
export REAL_STYLE_ANALYSIS_MODEL=gpt-4.1-mini-2025-04-14
export REAL_STYLE_ANALYSIS_MAX_RUN_USD=0.422722
export REAL_STYLE_ANALYSIS_TIMEOUT_SECONDS=120
export REAL_PRODUCT_IDENTITY_ENABLED=true
export REAL_PRODUCT_IDENTITY_API_KEY="$pilot_openai_key"
export REAL_PRODUCT_IDENTITY_MODEL=gpt-4.1-mini-2025-04-14
export REAL_PRODUCT_IDENTITY_MAX_RUN_USD=0.10
export REAL_FIDELITY_ENABLED=true
export REAL_FIDELITY_API_KEY="$pilot_openai_key"
export REAL_FIDELITY_MODEL=gpt-4.1-mini-2025-04-14
export REAL_FIDELITY_MAX_RUN_USD=1.00
export STYLE_SUITE_CANDIDATE_ESTIMATE_USD=0.12
export MISSING_PART_COMPLETION_ENABLED=true
export MISSING_PART_COMPLETION_API_KEY="$pilot_openai_key"
export MISSING_PART_COMPLETION_MODEL=gpt-image-2.5-sunburst-2026-09-08
export MISSING_PART_COMPLETION_MAX_RUN_USD=0.50
export MISSING_PART_COMPLETION_TIMEOUT_SECONDS=300

exec .venv/bin/python scripts/launch_real_style_pilot.py
