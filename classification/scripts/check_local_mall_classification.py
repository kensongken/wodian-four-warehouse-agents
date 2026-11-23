#!/usr/bin/env python3
"""只读检查本地人工确认类目能否生成商城分类交接件。"""

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.services.mall_classification_handoff import (  # noqa: E402
    MallClassificationHandoffFailure,
    build_mall_classification_handoff,
)
from product_engine.services.product_passport_drafts import (  # noqa: E402
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import (  # noqa: E402
    TrainingLabelVersionStore,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template_code")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=REPOSITORY_ROOT / ".local-data",
    )
    args = parser.parse_args()

    store = TrainingLabelVersionStore(
        args.data_root / "training-label-versions.jsonl",
        args.data_root / "training-labels.jsonl",
    )
    record = store.current(args.template_code)
    if record is None:
        print(json.dumps({"error_code": "training_result_not_found"}))
        return 2
    try:
        draft = build_product_passport_draft(record)
        handoff = build_mall_classification_handoff(draft)
    except (ProductPassportDraftFailure, MallClassificationHandoffFailure) as error:
        print(json.dumps({"error_code": str(error)}))
        return 3
    print(
        json.dumps(
            handoff.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
