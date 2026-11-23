#!/usr/bin/env python3
"""稳定导出跨系统 JSON Schema 与 OpenAPI 文档。"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.api.main import create_app  # noqa: E402
from product_engine.contracts.album import ProductImageSetReadyV1  # noqa: E402
from product_engine.contracts.materials import ProductMaterialsUpdatedV1  # noqa: E402
from product_engine.contracts.sales import SalesMaterialUsageV1  # noqa: E402
from product_engine.contracts.sales_strategy import (  # noqa: E402
    SalesStrategyInputUpdatedV1,
    SalesStrategyInputV1,
)
from product_engine.contracts.screenshots import ScreenshotImportedV1  # noqa: E402

EVENT_MODELS = {
    "product-image-set-ready.v1.schema.json": ProductImageSetReadyV1,
    "product-materials-updated.v1.schema.json": ProductMaterialsUpdatedV1,
    "sales-material-usage.v1.schema.json": SalesMaterialUsageV1,
    "screenshot-imported.v1.schema.json": ScreenshotImportedV1,
    "sales-strategy-input-updated.v1.schema.json": SalesStrategyInputUpdatedV1,
}

ROOT_MODELS = {
    "sales-strategy-input.v1.schema.json": SalesStrategyInputV1,
}


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(f"{serialized}\n", encoding="utf-8")


def export_contracts(output_dir: Path) -> None:
    events_dir = output_dir / "events"
    for filename, model in EVENT_MODELS.items():
        write_json(events_dir / filename, model.model_json_schema())
    for filename, model in ROOT_MODELS.items():
        write_json(output_dir / filename, model.model_json_schema())
    write_json(output_dir / "openapi.json", create_app().openapi())


def main() -> None:
    parser = argparse.ArgumentParser(description="导出稳定的跨系统契约文件")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "contracts",
        help="输出目录，默认写入项目 contracts 目录",
    )
    args = parser.parse_args()
    export_contracts(args.output_dir.resolve())


if __name__ == "__main__":
    main()
