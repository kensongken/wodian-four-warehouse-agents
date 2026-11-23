#!/usr/bin/env python3
"""只读验证销售之旅通知与策略输入是否指向同一份脱敏合成资料。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
EVENT_SCHEMA = (
    REPOSITORY_ROOT
    / "contracts"
    / "events"
    / "sales-strategy-input-updated.v1.schema.json"
)
INPUT_SCHEMA = REPOSITORY_ROOT / "contracts" / "sales-strategy-input.v1.schema.json"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.contracts.sales_strategy import (  # noqa: E402
    SalesStrategyInputUpdatedV1,
    SalesStrategyInputV1,
    canonical_sales_strategy_sha256,
)


def report(
    status: str,
    issues: list[dict[str, str]],
    *,
    synthetic_only: bool,
) -> dict[str, object]:
    return {
        "status": status,
        "topic": "sales.strategy.input.updated.v1",
        "contract": "SalesStrategyInputUpdatedV1+SalesStrategyInputV1",
        "schema_version": 1,
        "event_schema_sha256": hashlib.sha256(EVENT_SCHEMA.read_bytes()).hexdigest(),
        "input_schema_sha256": hashlib.sha256(INPUT_SCHEMA.read_bytes()).hexdigest(),
        "issue_codes": sorted({issue["code"] for issue in issues}),
        "issues": issues,
        "synthetic_only": synthetic_only,
    }


def validate_handoff(event_payload: Any, input_payload: Any) -> dict[str, object]:
    issues: list[dict[str, str]] = []
    try:
        event = SalesStrategyInputUpdatedV1.model_validate(event_payload)
    except ValidationError as error:
        issues.extend(
            {
                "code": "event_contract_validation_failed",
                "field": "event." + ".".join(str(part) for part in item["loc"]),
                "action_zh": "请按 SalesStrategyInputUpdatedV1 修正通知信封。",
            }
            for item in error.errors()
        )
        event = None
    try:
        strategy_input = SalesStrategyInputV1.model_validate(input_payload)
    except ValidationError as error:
        issues.extend(
            {
                "code": "input_contract_validation_failed",
                "field": "input." + ".".join(str(part) for part in item["loc"]),
                "action_zh": "请按 SalesStrategyInputV1 修正完整策略输入。",
            }
            for item in error.errors()
        )
        strategy_input = None

    if strategy_input is not None:
        expected_sha = canonical_sales_strategy_sha256(strategy_input)
        if strategy_input.content_sha256 != expected_sha:
            issues.append(
                {
                    "code": "input_content_sha256_mismatch",
                    "field": "input.content_sha256",
                    "action_zh": "请重新按固定规则计算完整策略输入的内容指纹。",
                }
            )

    if event is not None and strategy_input is not None:
        event_identity = (
            event.tenant_id,
            event.product_code,
            event.strategy_input_version,
            event.product_data_version,
            event.status,
            event.content_sha256,
        )
        input_identity = (
            strategy_input.tenant_id,
            strategy_input.product_code,
            strategy_input.strategy_input_version,
            strategy_input.product_data_version,
            strategy_input.status,
            strategy_input.content_sha256,
        )
        if event_identity != input_identity:
            issues.append(
                {
                    "code": "handoff_identity_mismatch",
                    "field": "tenant_id+product_code+versions+status+content_sha256",
                    "action_zh": "请让通知中的商家、商品、版本、状态和内容指纹同时对应完整资料。",
                }
            )
        if not strategy_input.product_code.startswith("SYNTHETIC-"):
            issues.append(
                {
                    "code": "synthetic_product_code_required",
                    "field": "product_code",
                    "action_zh": "请仅使用 SYNTHETIC- 开头的脱敏合成商品编号。",
                }
            )
    return report(
        "compatible" if not issues else "incompatible",
        issues,
        synthetic_only=not issues,
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", type=Path)
    parser.add_argument("strategy_input", type=Path)
    parser.add_argument("--confirm-synthetic", action="store_true")
    args = parser.parse_args()
    if not args.confirm_synthetic:
        result = report(
            "incompatible",
            [
                {
                    "code": "synthetic_confirmation_required",
                    "field": "--confirm-synthetic",
                    "action_zh": "请确认两份文件只包含脱敏合成元数据后再运行。",
                }
            ],
            synthetic_only=False,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    try:
        result = validate_handoff(_read_json(args.event), _read_json(args.strategy_input))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        result = report(
            "incompatible",
            [
                {
                    "code": "handoff_file_unreadable",
                    "field": "event|strategy_input",
                    "action_zh": "请提供两份可读取的 UTF-8 JSON 文件。",
                }
            ],
            synthetic_only=True,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "compatible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
