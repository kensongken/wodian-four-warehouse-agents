"""Export the checked-in Mall SaaS handoff schema deterministically."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from opc_style.contracts.mall_style_handoff_v1 import (
    MallProductAssetImportReceiptV1,
    MallStyleGenerationApprovalV1,
    MallStyleGenerationQueueReceiptV1,
    MallStyleHandoffV1,
    MallStyleResultListV1,
    MallStyleSelectionListV1,
    MallSubjectConfirmationMetadataV1,
    MallSubjectConfirmationReceiptV1,
    MallSubjectInspectionV1,
)


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "contracts"
    exports: tuple[tuple[str, type[BaseModel]], ...] = (
        ("mall-style-handoff.v1.schema.json", MallStyleHandoffV1),
        ("mall-style-selection-list.v1.schema.json", MallStyleSelectionListV1),
        (
            "mall-product-asset-import-receipt.v1.schema.json",
            MallProductAssetImportReceiptV1,
        ),
        (
            "mall-style-generation-approval.v1.schema.json",
            MallStyleGenerationApprovalV1,
        ),
        (
            "mall-style-generation-queue-receipt.v1.schema.json",
            MallStyleGenerationQueueReceiptV1,
        ),
        ("mall-style-result-list.v1.schema.json", MallStyleResultListV1),
        (
            "mall-subject-confirmation-metadata.v1.schema.json",
            MallSubjectConfirmationMetadataV1,
        ),
        (
            "mall-subject-inspection.v1.schema.json",
            MallSubjectInspectionV1,
        ),
        (
            "mall-subject-confirmation-receipt.v1.schema.json",
            MallSubjectConfirmationReceiptV1,
        ),
    )
    for filename, model in exports:
        (root / filename).write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
