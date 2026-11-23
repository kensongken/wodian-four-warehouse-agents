#!/usr/bin/env python3
"""只读验证相册项目交付的脱敏合成元数据样例。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.contracts.album import ProductImageSetReadyV1  # noqa: E402

ACTION_ZH = "请按版本 1 输入规格补齐或修正该字段。"


def _field_path(location: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in location)


def _rejected(
    issues: list[dict[str, str]],
    *,
    synthetic_only: bool = True,
) -> dict[str, object]:
    return {
        "status": "rejected",
        "contract": "ProductImageSetReadyV1",
        "schema_version": 1,
        "issue_codes": sorted({issue["code"] for issue in issues}),
        "issues": issues,
        "synthetic_only": synthetic_only,
    }


def validate_sample(payload: Any) -> dict[str, object]:
    try:
        event = ProductImageSetReadyV1.model_validate(payload)
    except ValidationError as error:
        issues = [
            {
                "code": "contract_validation_failed",
                "field": _field_path(item["loc"]),
                "action_zh": ACTION_ZH,
            }
            for item in error.errors()
        ]
        return _rejected(issues)

    issues: list[dict[str, str]] = []
    asset_ids: set[object] = set()
    object_keys: set[str] = set()
    tenant_prefix = f"tenants/{event.tenant_id}/"
    for index, image in enumerate(event.images):
        if image.asset_id in asset_ids:
            issues.append(
                {
                    "code": "duplicate_asset_id",
                    "field": f"images.{index}.asset_id",
                    "action_zh": "请为每张图片提供唯一的图片编号。",
                }
            )
        asset_ids.add(image.asset_id)
        if image.object_key in object_keys:
            issues.append(
                {
                    "code": "duplicate_object_key",
                    "field": f"images.{index}.object_key",
                    "action_zh": "请勿在同一事件中重复引用同一图片路径。",
                }
            )
        object_keys.add(image.object_key)
        if not image.object_key.startswith(tenant_prefix):
            issues.append(
                {
                    "code": "object_outside_tenant_prefix",
                    "field": f"images.{index}.object_key",
                    "action_zh": "请把图片路径放在当前商家的 tenants/{tenant_id}/ 目录内。",
                }
            )
    if issues:
        return _rejected(issues)
    return {
        "status": "compatible",
        "contract": "ProductImageSetReadyV1",
        "schema_version": 1,
        "issue_codes": [],
        "issues": [],
        "synthetic_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--confirm-synthetic", action="store_true")
    args = parser.parse_args()
    if not args.confirm_synthetic:
        print(
            json.dumps(
                _rejected(
                    [
                        {
                            "code": "synthetic_confirmation_required",
                            "field": "--confirm-synthetic",
                            "action_zh": "请确认文件只包含脱敏合成元数据后再运行。",
                        }
                    ],
                    synthetic_only=False,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    try:
        payload = json.loads(args.sample.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(
            json.dumps(
                _rejected(
                    [
                        {
                            "code": "sample_unreadable",
                            "field": "sample",
                            "action_zh": "请提供可读取且格式正确的 UTF-8 JSON 文件。",
                        }
                    ]
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    report = validate_sample(payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "compatible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
