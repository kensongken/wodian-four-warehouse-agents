#!/usr/bin/env python3
"""Create a review-only local calibration proposal batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from opc_style.services.missing_part_calibration_batch import build_calibration_batch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--foreground-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--category", default="bracelet")
    parser.add_argument("--part-kind", default="bead_edge")
    args = parser.parse_args()
    manifest = build_calibration_batch(
        source_dir=args.source_dir,
        foreground_root=args.foreground_root,
        output_dir=args.output_dir,
        category=args.category,
        part_kind=args.part_kind,
    )
    print(
        json.dumps(
            {
                "source_count": manifest["source_count"],
                "proposal_count": manifest["proposal_count"],
                "pending_human_review": manifest["pending_human_review"],
                "training_eligible_count": manifest["training_eligible_count"],
                "provider_call_count": manifest["provider_call_count"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
