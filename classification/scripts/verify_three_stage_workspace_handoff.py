#!/usr/bin/env python3
"""只读核对分门别类清单与销售之旅收货回执是否逐字节一致。"""

import argparse
import hashlib
import json
from pathlib import Path

REQUIRED_SALES_ARTIFACTS = {
    "sales-strategy-input.three-stage.synthetic.json",
    "sales-strategy-input-updated.three-stage.synthetic.json",
}
ALBUM_SOURCE_FIXTURES = {
    "product-image-set-ready.album-95daa.synthetic.json": Path(
        "services/backend/fixtures/v0.1/product-image-set-ready.synthetic.json"
    ),
    "product-image-set-ready.album-v2.synthetic.json": Path(
        "services/backend/fixtures/v0.1/product-image-set-ready-v2.synthetic.json"
    ),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(
    manifest_path: Path, sales_root: Path, album_root: Path
) -> dict[str, object]:
    issues: list[str] = []
    resolved_manifest_path = manifest_path.resolve()
    producer_root = resolved_manifest_path.parents[2]
    resolved_sales_root = sales_root.resolve()
    resolved_album_root = album_root.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_path = (
        sales_root / "fixtures/integrations/three-stage-handoff-receipt.synthetic.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    synthetic_only = (
        manifest.get("synthetic_only") is True and receipt.get("synthetic_only") is True
    )
    if not synthetic_only:
        issues.append("synthetic_only_required")
    if receipt.get("source_manifest_sha256") != sha256(manifest_path):
        issues.append("source_manifest_sha256_mismatch")
    accepted_artifacts = receipt.get("accepted_artifacts", [])
    if not accepted_artifacts:
        issues.append("accepted_artifacts_required")
    accepted_names = {Path(item["path"]).name for item in accepted_artifacts}
    if accepted_artifacts and not REQUIRED_SALES_ARTIFACTS.issubset(accepted_names):
        issues.append("required_sales_artifact_missing")
    producer: dict[str, str] = {}
    for item in manifest["artifacts"]:
        producer_path = (producer_root / item["path"]).resolve()
        if not producer_path.is_relative_to(producer_root):
            issues.append("producer_artifact_path_outside_workspace")
            continue
        if sha256(producer_path) != item["sha256"]:
            issues.append("producer_artifact_sha256_mismatch")
        producer[producer_path.name] = item["sha256"]
    album_source_artifacts = [
        item
        for item in manifest["artifacts"]
        if Path(item["path"]).name.startswith("product-image-set-ready.album-")
    ]
    if not album_source_artifacts:
        issues.append("album_source_manifest_artifact_required")
    for artifact in album_source_artifacts:
        artifact_name = Path(artifact["path"]).name
        album_relative_path = ALBUM_SOURCE_FIXTURES.get(artifact_name)
        if album_relative_path is None:
            issues.append("album_source_manifest_artifact_unknown")
            continue
        album_source_path = (resolved_album_root / album_relative_path).resolve()
        if not album_source_path.is_relative_to(resolved_album_root):
            issues.append("album_source_path_outside_workspace")
        elif not album_source_path.is_file():
            issues.append("album_source_fixture_missing")
        elif sha256(album_source_path) != artifact["sha256"]:
            issues.append("album_source_fixture_sha256_mismatch")
    for artifact in accepted_artifacts:
        path = (resolved_sales_root / artifact["path"]).resolve()
        if not path.is_relative_to(resolved_sales_root):
            issues.append("sales_artifact_path_outside_workspace")
            continue
        actual = sha256(path)
        if actual != artifact["sha256"]:
            issues.append("sales_artifact_sha256_mismatch")
        if producer.get(path.name) != actual:
            issues.append("producer_consumer_artifact_mismatch")
    return {
        "status": "compatible" if not issues else "incompatible",
        "issue_codes": sorted(set(issues)),
        "synthetic_only": synthetic_only,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("sales_workspace", type=Path)
    parser.add_argument("--album-workspace", required=True, type=Path)
    parser.add_argument("--confirm-synthetic", action="store_true")
    args = parser.parse_args()
    if not args.confirm_synthetic:
        result = {
            "status": "incompatible",
            "issue_codes": ["synthetic_confirmation_required"],
            "synthetic_only": False,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    try:
        result = verify(args.manifest, args.sales_workspace, args.album_workspace)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        result = {
            "status": "incompatible",
            "issue_codes": ["handoff_artifact_unreadable"],
            "synthetic_only": True,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "compatible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
