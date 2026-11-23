"""Validate or freeze a private local foreground benchmark dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from opc_style.domain.errors import DomainRuleError
from opc_style.services.foreground_benchmarks import freeze_dataset, validate_dataset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "freeze"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--split", choices=("acceptance",), default="acceptance")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        dataset = (
            freeze_dataset(args.root)
            if args.command == "freeze"
            else validate_dataset(args.root)
        )
    except DomainRuleError as error:
        print(json.dumps({"ok": False, "error": error.code}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "acceptance_count": dataset.acceptance_count,
                "calibration_count": dataset.calibration_count,
                "categories": dataset.categories,
                "seal_sha256": dataset.seal_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
