#!/bin/bash
set -euo pipefail

executable=""
model_root=""
manifest_sha256=""
input=""
prompt_json=""
output_dir=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --executable) executable="$2"; shift 2 ;;
    --model-root) model_root="$2"; shift 2 ;;
    --model-manifest-sha256) manifest_sha256="$2"; shift 2 ;;
    --input) input="$2"; shift 2 ;;
    --prompt-json) prompt_json="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    *) echo "foreground_candidate_argument_invalid" >&2; exit 2 ;;
  esac
done

for value in "$executable" "$model_root" "$manifest_sha256" "$input" "$prompt_json" "$output_dir"; do
  [[ -n "$value" ]] || { echo "foreground_candidate_argument_invalid" >&2; exit 2; }
done
[[ -x "$executable" && ! -L "$executable" && -d "$model_root" && ! -L "$model_root" ]] || { echo "foreground_candidate_unavailable" >&2; exit 2; }
[[ -f "$input" && ! -L "$input" && -f "$prompt_json" && ! -L "$prompt_json" ]] || { echo "foreground_candidate_input_invalid" >&2; exit 2; }
[[ ! -e "$output_dir" ]] || { echo "foreground_candidate_output_exists" >&2; exit 2; }

/bin/mkdir -m 700 "$output_dir"
start_seconds="$(/bin/date +%s)"
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 NO_PROXY='*' /usr/bin/time -l -o "$output_dir/resource.txt" "$executable" \
  --input "$input" \
  --mask-output "$output_dir/mask.png" \
  --foreground-output "$output_dir/foreground.png" \
  --receipt-output "$output_dir/receipt.json" \
  --prompt-json "$prompt_json" \
  --model-root "$model_root" \
  --model-manifest-sha256 "$manifest_sha256"
elapsed_seconds="$(( $(/bin/date +%s) - start_seconds ))"
[[ "$elapsed_seconds" -le 20 ]] || { echo "foreground_candidate_runtime_exceeded" >&2; exit 2; }
for output in mask.png foreground.png receipt.json resource.txt; do
  [[ -s "$output_dir/$output" && ! -L "$output_dir/$output" ]] || { echo "foreground_candidate_output_invalid" >&2; exit 2; }
  /bin/chmod 600 "$output_dir/$output"
done
/usr/bin/printf '{"ok":true,"elapsed_seconds":%s,"resource_record":"resource.txt"}\n' "$elapsed_seconds"
