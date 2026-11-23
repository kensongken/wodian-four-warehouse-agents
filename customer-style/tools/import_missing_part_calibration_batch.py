#!/usr/bin/env python3
"""Import a verified proposal batch into the local private review store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from opc_style.services.missing_part_calibration_review import (
    MissingPartCalibrationReviewStore,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--asset-root", type=Path, default=Path(".local-style-assets"))
    args = parser.parse_args()
    store = MissingPartCalibrationReviewStore(
        args.asset_root.resolve() / "missing-part-calibration-review"
    )
    result = store.import_batch(args.source)
    print(json.dumps({key: value for key, value in result.items() if key != "items"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
