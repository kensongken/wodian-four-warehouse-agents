#!/usr/bin/env python3
"""只读检查人工确认资料能否进入本地合成小试。"""

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.services.pilot_release_gate import (  # noqa: E402
    evaluate_pilot_release,
)
from product_engine.services.training_label_versions import (  # noqa: E402
    TrainingLabelFailure,
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
    try:
        record = store.current(args.template_code)
    except TrainingLabelFailure as error:
        reason = str(error)
        if reason != "forbidden_unsupported_claim":
            print(json.dumps({"error_code": reason}))
            return 2
        print(
            json.dumps(
                {
                    "template_code": args.template_code,
                    "source_version": None,
                    "decision": "blocked",
                    "reason_codes": [reason],
                    "local_synthetic_only": True,
                    "real_customer_data_allowed": False,
                    "external_actions_allowed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 3
    if record is None:
        print(json.dumps({"error_code": "training_result_not_found"}))
        return 2

    decision = evaluate_pilot_release(record)
    print(
        json.dumps(
            decision.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )
    return {
        "allowed_for_local_pilot": 0,
        "needs_human_input": 1,
        "blocked": 3,
    }[decision.decision]


if __name__ == "__main__":
    raise SystemExit(main())
