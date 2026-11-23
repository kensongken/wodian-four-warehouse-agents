#!/bin/bash
set -euo pipefail

weights=""; weights_sha256=""; config=""; preprocessor_config=""
processor_config=""; license_file=""; source_url=""; model_version=""
sam_root=""; sam_weights_sha256=""; sam_source_url=""; sam_source_revision=""; sam_model_version=""
birefnet_root=""; birefnet_weights_sha256=""; birefnet_code_sha256=""; birefnet_source_url=""; birefnet_source_revision=""; birefnet_model_version=""
worker=""; python_bin=""; destination=""; runtime_versions_json=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --weights) weights="$2"; shift 2 ;;
    --weights-sha256) weights_sha256="$2"; shift 2 ;;
    --config) config="$2"; shift 2 ;;
    --preprocessor-config) preprocessor_config="$2"; shift 2 ;;
    --processor-config) processor_config="$2"; shift 2 ;;
    --license) license_file="$2"; shift 2 ;;
    --source-url) source_url="$2"; shift 2 ;;
    --model-version) model_version="$2"; shift 2 ;;
    --sam-root) sam_root="$2"; shift 2 ;;
    --sam-weights-sha256) sam_weights_sha256="$2"; shift 2 ;;
    --sam-source-url) sam_source_url="$2"; shift 2 ;;
    --sam-source-revision) sam_source_revision="$2"; shift 2 ;;
    --sam-model-version) sam_model_version="$2"; shift 2 ;;
    --birefnet-root) birefnet_root="$2"; shift 2 ;;
    --birefnet-weights-sha256) birefnet_weights_sha256="$2"; shift 2 ;;
    --birefnet-code-sha256) birefnet_code_sha256="$2"; shift 2 ;;
    --birefnet-source-url) birefnet_source_url="$2"; shift 2 ;;
    --birefnet-source-revision) birefnet_source_revision="$2"; shift 2 ;;
    --birefnet-model-version) birefnet_model_version="$2"; shift 2 ;;
    --worker) worker="$2"; shift 2 ;;
    --python) python_bin="$2"; shift 2 ;;
    --destination) destination="$2"; shift 2 ;;
    --runtime-versions-json) runtime_versions_json="$2"; shift 2 ;;
    *) echo "foreground_candidate_argument_invalid" >&2; exit 2 ;;
  esac
done

is_sha_revision() { [[ "$1" =~ ^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$ ]]; }
is_apache_license() { /usr/bin/grep -q "Apache License" "$1" && /usr/bin/grep -q "Version 2.0" "$1"; }
is_mit_license() { /usr/bin/grep -q "MIT License" "$1" && /usr/bin/grep -q "Permission is hereby granted" "$1"; }
sha_file() { /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'; }

validate_exact_root() {
  local root="$1"; shift
  local actual_count
  actual_count="$(/usr/bin/find "$root" -mindepth 1 -maxdepth 1 -print | /usr/bin/wc -l | /usr/bin/tr -d ' ')"
  [[ "$actual_count" == "$#" ]] || return 1
  local name
  for name in "$@"; do
    [[ -f "$root/$name" && ! -L "$root/$name" ]] || return 1
  done
}

install_pipeline() {
  for value in "$sam_root" "$sam_weights_sha256" "$sam_source_url" "$sam_source_revision" "$sam_model_version" "$birefnet_root" "$birefnet_weights_sha256" "$birefnet_code_sha256" "$birefnet_source_url" "$birefnet_source_revision" "$birefnet_model_version" "$worker" "$python_bin" "$destination" "$runtime_versions_json"; do
    [[ -n "$value" ]] || { echo "foreground_candidate_argument_invalid" >&2; exit 2; }
  done
  is_sha_revision "$sam_source_revision" && is_sha_revision "$birefnet_source_revision" || { echo "foreground_candidate_source_revision_invalid" >&2; exit 2; }
  [[ "$sam_source_url" == https://* && "$birefnet_source_url" == https://* ]] || { echo "foreground_candidate_source_invalid" >&2; exit 2; }
  [[ -d "$sam_root" && ! -L "$sam_root" && -d "$birefnet_root" && ! -L "$birefnet_root" ]] || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  [[ -f "$worker" && ! -L "$worker" && -x "$python_bin" && -f "$python_bin" && ! -e "$destination" ]] || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  local sam_names=(LICENSE config.json model.safetensors preprocessor_config.json processor_config.json worker.py)
  local birefnet_names=(BiRefNet_config.py LICENSE birefnet.py config.json model.safetensors)
  validate_exact_root "$sam_root" "${sam_names[@]}" || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  validate_exact_root "$birefnet_root" "${birefnet_names[@]}" || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  is_apache_license "$sam_root/LICENSE" || { echo "foreground_candidate_license_invalid" >&2; exit 2; }
  is_mit_license "$birefnet_root/LICENSE" || { echo "foreground_candidate_license_invalid" >&2; exit 2; }
  [[ "$(sha_file "$sam_root/model.safetensors")" == "$sam_weights_sha256" && "$(sha_file "$birefnet_root/model.safetensors")" == "$birefnet_weights_sha256" ]] || { echo "foreground_candidate_checksum_mismatch" >&2; exit 2; }
  local actual_birefnet_code_sha256
  actual_birefnet_code_sha256="$("$python_bin" -c 'import hashlib,sys; from pathlib import Path; r=Path(sys.argv[1]); names=("BiRefNet_config.py","birefnet.py","config.json"); p="".join(f"{n}{chr(0)}{hashlib.sha256((r/n).read_bytes()).hexdigest()}{chr(10)}" for n in names).encode(); print(hashlib.sha256(p).hexdigest())' "$birefnet_root")"
  [[ "$actual_birefnet_code_sha256" == "$birefnet_code_sha256" ]] || { echo "foreground_candidate_checksum_mismatch" >&2; exit 2; }

  /bin/mkdir -m 700 "$destination" "$destination/sam2" "$destination/birefnet"
  for name in "${sam_names[@]}"; do
    /bin/cp "$sam_root/$name" "$destination/sam2/$name"
  done
  for name in "${birefnet_names[@]}"; do
    /bin/cp "$birefnet_root/$name" "$destination/birefnet/$name"
  done
  /bin/cp "$worker" "$destination/worker.py"
  /bin/chmod 600 "$destination/worker.py" "$destination"/sam2/* "$destination"/birefnet/*
  printf '#!/bin/bash\nexec %q %q "$@"\n' "$python_bin" "$destination/worker.py" > "$destination/runner"
  /bin/chmod 700 "$destination/runner"

  "$python_bin" - "$destination" "$sam_source_url" "$sam_source_revision" "$sam_model_version" "$birefnet_source_url" "$birefnet_source_revision" "$birefnet_model_version" "$runtime_versions_json" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]); runtime = json.loads(sys.argv[8])
required = {"einops", "kornia", "pillow", "python", "safetensors", "timm", "torch", "torchvision", "transformers"}
assert isinstance(runtime, dict) and set(runtime) == required
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def tree_digest(path, names):
    payload = "".join(f"{name}\0{digest(path / name)}\n" for name in names).encode()
    return hashlib.sha256(payload).hexdigest()
artifacts = []
for role, name, version, url, revision, license_id, names, code_names in (
    ("coarse_segmenter", "sam2", sys.argv[4], sys.argv[2], sys.argv[3], "Apache-2.0", ("LICENSE", "config.json", "model.safetensors", "preprocessor_config.json", "processor_config.json", "worker.py"), ("config.json", "preprocessor_config.json", "processor_config.json", "worker.py")),
    ("alpha_refiner", "birefnet", sys.argv[7], sys.argv[5], sys.argv[6], "MIT", ("BiRefNet_config.py", "LICENSE", "birefnet.py", "config.json", "model.safetensors"), ("BiRefNet_config.py", "birefnet.py", "config.json")),
):
    path = root / name
    artifacts.append({"role": role, "model_name": name, "model_version": version, "source_url": url, "source_revision": revision, "license_id": license_id, "license_sha256": digest(path / "LICENSE"), "weights_sha256": digest(path / "model.safetensors"), "code_tree_sha256": tree_digest(path, code_names), "weights_source_url": f"{url}/resolve/{revision}/model.safetensors", "weights_source_revision": revision, "code_source_url": f"{url}/tree/{revision}", "code_source_revision": revision, "license_source_url": f"{url}/blob/{revision}/LICENSE", "license_source_revision": revision, "files_sha256": [(filename, digest(path / filename)) for filename in names]})
total = (root / "runner").stat().st_size + (root / "worker.py").stat().st_size + sum(path.stat().st_size for directory in (root / "sam2", root / "birefnet") for path in directory.iterdir() if path.is_file())
manifest = {"pipeline_version": "sam2-birefnet-official-v1", "artifacts": artifacts, "executable_sha256": digest(root / "runner"), "worker_sha256": digest(root / "worker.py"), "runtime_versions": sorted(runtime.items()), "total_bytes": total}
content = json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
(root / "pipeline-manifest.json").write_text(content, encoding="utf-8")
print(json.dumps({"ok": True, "model_manifest_sha256": hashlib.sha256(content.encode()).hexdigest(), "total_bytes": total}, sort_keys=True))
PY
  /bin/chmod 600 "$destination/pipeline-manifest.json"
}

install_legacy() {
  for value in "$weights" "$weights_sha256" "$config" "$preprocessor_config" "$processor_config" "$license_file" "$worker" "$python_bin" "$destination" "$source_url" "$model_version" "$runtime_versions_json"; do
    [[ -n "$value" ]] || { echo "foreground_candidate_argument_invalid" >&2; exit 2; }
  done
  [[ -f "$weights" && ! -L "$weights" && -f "$config" && ! -L "$config" ]] || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  [[ -f "$preprocessor_config" && ! -L "$preprocessor_config" && -f "$processor_config" && ! -L "$processor_config" ]] || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  [[ -f "$license_file" && ! -L "$license_file" && -f "$worker" && ! -L "$worker" ]] || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  [[ -x "$python_bin" && -f "$python_bin" && "$source_url" == https://* && ! -e "$destination" ]] || { echo "foreground_candidate_file_invalid" >&2; exit 2; }
  [[ "$(sha_file "$weights")" == "$weights_sha256" ]] || { echo "foreground_candidate_checksum_mismatch" >&2; exit 2; }
  is_apache_license "$license_file" || { echo "foreground_candidate_license_invalid" >&2; exit 2; }
  local weights_bytes; weights_bytes="$(/usr/bin/stat -f%z "$weights")"
  [[ "$weights_bytes" -gt 0 && "$weights_bytes" -le 3221225472 ]] || { echo "foreground_candidate_size_invalid" >&2; exit 2; }
  /bin/mkdir -m 700 "$destination"
  /bin/cp "$weights" "$destination/model.safetensors"; /bin/cp "$config" "$destination/config.json"
  /bin/cp "$preprocessor_config" "$destination/preprocessor_config.json"; /bin/cp "$processor_config" "$destination/processor_config.json"
  /bin/cp "$license_file" "$destination/LICENSE"; /bin/cp "$worker" "$destination/worker.py"
  /bin/chmod 600 "$destination"/*
  printf '#!/bin/bash\nexec %q %q "$@"\n' "$python_bin" "$destination/worker.py" > "$destination/runner"; /bin/chmod 700 "$destination/runner"
  local executable_sha256 model_tree_sha256 model_bytes
  executable_sha256="$(sha_file "$destination/runner")"
  model_tree_sha256="$("$python_bin" -c 'import hashlib,sys; from pathlib import Path; r=Path(sys.argv[1]); names=("config.json","model.safetensors","preprocessor_config.json","processor_config.json","worker.py"); p="".join(f"{n}\\0{hashlib.sha256((r/n).read_bytes()).hexdigest()}\\n" for n in names).encode(); print(hashlib.sha256(p).hexdigest())' "$destination")"
  model_bytes="$(( $(/usr/bin/stat -f%z "$destination/model.safetensors") + $(/usr/bin/stat -f%z "$destination/config.json") + $(/usr/bin/stat -f%z "$destination/preprocessor_config.json") + $(/usr/bin/stat -f%z "$destination/processor_config.json") + $(/usr/bin/stat -f%z "$destination/worker.py") ))"
  "$python_bin" -c 'import json,sys; v=json.loads(sys.argv[7]); required={"pillow","python","safetensors","torch","torchvision","transformers"}; assert isinstance(v,dict) and set(v)==required; p={"model_name":"sam2","model_version":sys.argv[1],"source_url":sys.argv[2],"license_id":"Apache-2.0","weights_sha256":sys.argv[3],"model_tree_sha256":sys.argv[4],"executable_sha256":sys.argv[5],"runtime_versions":sorted(v.items()),"total_bytes":int(sys.argv[6])}; open(sys.argv[8],"w",encoding="utf-8").write(json.dumps(p,ensure_ascii=True,sort_keys=True,separators=(",",":")))' "$model_version" "$source_url" "$weights_sha256" "$model_tree_sha256" "$executable_sha256" "$model_bytes" "$runtime_versions_json" "$destination/model-manifest.json"
  /bin/chmod 600 "$destination/model-manifest.json"
  "$python_bin" -c 'import hashlib,json,sys; p=json.load(open(sys.argv[1],encoding="utf-8")); c=json.dumps(p,ensure_ascii=True,sort_keys=True,separators=(",",":")).encode(); print(json.dumps({"ok":True,"model_manifest_sha256":hashlib.sha256(c).hexdigest(),"total_bytes":p["total_bytes"]},sort_keys=True))' "$destination/model-manifest.json"
}

if [[ -n "$sam_root" || -n "$birefnet_root" ]]; then install_pipeline; else install_legacy; fi
