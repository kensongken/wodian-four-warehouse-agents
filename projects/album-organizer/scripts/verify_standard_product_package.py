"""Verify an offline OPC standard product package and emit a safe receipt."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from photo_organizer.catalog.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceError,
    verify_standard_product_package,
)
from photo_organizer.schemas.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceReceipt,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_OUTPUT = (
    ROOT / "contracts/opc-standard-product-package-acceptance-v1.schema.json"
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise StandardProductPackageAcceptanceError("invalid_arguments")


def build_schema() -> dict[str, object]:
    schema = StandardProductPackageAcceptanceReceipt.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:photo-organizer:opc-standard-product-package-acceptance:1.0"
    schema["$comment"] = (
        "Structural schema only. Consumers MUST verify this receipt against "
        "the same package ZIP with the required semantic command."
    )
    schema["x-semantic-validation"] = {
        "required": True,
        "command": (
            "python scripts/verify_standard_product_package.py "
            "--package <package.zip> --output <receipt.json> --check"
        ),
        "stable_error": "receipt_stale",
    }
    return schema


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser()
    parser.add_argument("--package")
    parser.add_argument("--output")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument("--check-artifacts", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    artifact_mode = args.write_artifacts or args.check_artifacts
    if args.write_artifacts and args.check_artifacts:
        raise StandardProductPackageAcceptanceError("invalid_arguments")
    if artifact_mode:
        if args.package is not None or args.output is not None or args.check:
            raise StandardProductPackageAcceptanceError("invalid_arguments")
        return
    if args.package is None or args.output is None:
        raise StandardProductPackageAcceptanceError("invalid_arguments")


def _local_path(value: str) -> Path:
    if value.lower().startswith(("http://", "https://")):
        raise StandardProductPackageAcceptanceError("invalid_arguments")
    return Path(value)


def _paths_collide(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.abspath(os.fspath(first)) == os.path.abspath(os.fspath(second))


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError:
        raise StandardProductPackageAcceptanceError("output_unavailable") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_receipt(package: Path, output: Path, *, check: bool) -> None:
    if _paths_collide(package, output):
        raise StandardProductPackageAcceptanceError("invalid_arguments")
    receipt = verify_standard_product_package(package)
    expected = render_json(receipt.model_dump(mode="json"))
    if check:
        try:
            current = output.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise StandardProductPackageAcceptanceError("receipt_stale") from None
        if current != expected:
            raise StandardProductPackageAcceptanceError("receipt_stale")
        return
    _atomic_write(output, expected)


def _artifacts(*, check: bool) -> None:
    expected = render_json(build_schema())
    if check:
        try:
            current = SCHEMA_OUTPUT.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise StandardProductPackageAcceptanceError("receipt_stale") from None
        if current != expected:
            raise StandardProductPackageAcceptanceError("receipt_stale")
        return
    _atomic_write(SCHEMA_OUTPUT, expected)


def main() -> int:
    try:
        args = _parser().parse_args()
        _validate_args(args)
        if args.write_artifacts:
            _artifacts(check=False)
            return 0
        if args.check_artifacts:
            _artifacts(check=True)
            return 0
        package = _local_path(args.package)
        output = _local_path(args.output)
        _write_receipt(package, output, check=args.check)
        return 0
    except StandardProductPackageAcceptanceError as error:
        print(error.code, file=sys.stderr)
        return 1 if error.code == "receipt_stale" else 2
    except (TypeError, ValueError, ValidationError, RecursionError):
        print("invalid_package", file=sys.stderr)
        return 2


def entrypoint() -> Never:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
