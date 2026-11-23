#!/usr/bin/env python3
"""只读验证销售之旅策略输入的脱敏合成样例。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
SCHEMA_PATH = REPOSITORY_ROOT / "contracts" / "sales-strategy-input.v1.schema.json"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.contracts.sales_strategy import (  # noqa: E402
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)


def report(
    status: str,
    issues: list[dict[str, str]],
    *,
    synthetic_only: bool,
    content_sha256: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "contract": "SalesStrategyInputV1",
        "contract_version": "1-sales-strategy-input",
        "schema_sha256": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "content_sha256": content_sha256,
        "issue_codes": sorted({issue["code"] for issue in issues}),
        "issues": issues,
        "synthetic_only": synthetic_only,
    }


def validate_sample(payload: Any) -> dict[str, object]:
    try:
        value = SalesStrategyInputV1.model_validate(payload)
    except ValidationError as error:
        issues = [
            {
                "code": "contract_validation_failed",
                "field": ".".join(str(part) for part in item["loc"]),
                "action_zh": "请按 SalesStrategyInputV1 修正字段，并保持 preview_only。",
            }
            for item in error.errors()
        ]
        return report("incompatible", issues, synthetic_only=True)
    expected = canonical_sales_strategy_sha256(value)
    if expected != value.content_sha256:
        return report(
            "incompatible",
            [
                {
                    "code": "content_sha256_mismatch",
                    "field": "content_sha256",
                    "action_zh": "请重新按标准正文计算内容指纹。",
                }
            ],
            synthetic_only=True,
            content_sha256=expected,
        )
    if not value.product_code.startswith("SYNTHETIC-"):
        return report(
            "incompatible",
            [
                {
                    "code": "synthetic_product_code_required",
                    "field": "product_code",
                    "action_zh": "请使用 SYNTHETIC- 开头的脱敏商品编号。",
                }
            ],
            synthetic_only=False,
            content_sha256=expected,
        )
    return report(
        "compatible",
        [],
        synthetic_only=True,
        content_sha256=expected,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--confirm-synthetic", action="store_true")
    args = parser.parse_args()
    if not args.confirm_synthetic:
        print(
            json.dumps(
                report(
                    "incompatible",
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print(
            json.dumps(
                report(
                    "incompatible",
                    [
                        {
                            "code": "sample_unreadable",
                            "field": "sample",
                            "action_zh": "请提供可读取的 UTF-8 JSON 文件。",
                        }
                    ],
                    synthetic_only=True,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    result = validate_sample(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "compatible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
