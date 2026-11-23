"""Export an offline, draft-only OPC standard product package."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Never
from uuid import UUID

from pydantic import ValidationError

from photo_organizer.catalog.standard_product_package import (
    StandardProductPackageError,
    validate_standard_product_package_manifest,
    write_standard_product_package,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch
from photo_organizer.schemas.standard_product_package import (
    StandardProductPackageManifest,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_OUTPUT = ROOT / "contracts/opc-standard-product-package-v1.schema.json"
FORBIDDEN_KEYS = {
    "access_token",
    "address",
    "authorization",
    "chat_text",
    "customer_id",
    "customer_name",
    "download_url",
    "mobile",
    "order_id",
    "phone",
    "private_key",
    "refresh_token",
    "secret_access_key",
    "source_object_key",
    "support_context",
    "token",
    "wechat",
}


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise StandardProductPackageError("invalid_arguments")


def build_schema() -> dict[str, object]:
    schema = StandardProductPackageManifest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:photo-organizer:opc-standard-product-package:1.0"
    schema["$comment"] = (
        "Structural schema only. Consumers MUST run the required semantic "
        "validator to verify derived identities, evidence links, and paths."
    )
    schema["x-semantic-validation"] = {
        "required": True,
        "command": (
            "python scripts/export_standard_product_package.py "
            "--verify-manifest <manifest.json>"
        ),
        "stable_error": "unsafe_manifest",
    }
    return schema


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser()
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--asset", action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument("--check-artifacts", action="store_true")
    parser.add_argument("--verify-manifest", type=Path)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    artifact_mode = args.write_artifacts or args.check_artifacts
    verification_mode = args.verify_manifest is not None
    ordinary_values = (args.batch, args.asset, args.output)
    if artifact_mode or verification_mode:
        if (
            args.write_artifacts
            and args.check_artifacts
            or artifact_mode
            and verification_mode
            or args.check
            or any(value is not None for value in ordinary_values)
        ):
            raise StandardProductPackageError("invalid_arguments")
    elif any(value is None for value in ordinary_values):
        raise StandardProductPackageError("invalid_arguments")


def _load_batch(path: Path) -> MallProductDraftBatch:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise StandardProductPackageError("invalid_input") from None
    try:
        _assert_safe(value)
        return MallProductDraftBatch.model_validate(value)
    except RecursionError:
        raise StandardProductPackageError("invalid_input") from None
    except ValidationError:
        raise StandardProductPackageError("invalid_input") from None


def _verify_manifest(path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise StandardProductPackageError("invalid_input") from None
    except RecursionError:
        raise StandardProductPackageError("unsafe_manifest") from None
    try:
        _assert_safe(value, error_code="unsafe_manifest")
        validate_standard_product_package_manifest(value)
    except RecursionError:
        raise StandardProductPackageError("unsafe_manifest") from None


def _assert_safe(value: object, *, error_code: str = "unsafe_batch") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_KEYS or normalized.endswith("_token"):
                raise StandardProductPackageError(error_code)
            _assert_safe(child, error_code=error_code)
    elif isinstance(value, list | tuple):
        for child in value:
            _assert_safe(child, error_code=error_code)
    elif isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        raise StandardProductPackageError(error_code)


def _asset_paths(values: list[str]) -> dict[UUID, Path]:
    result: dict[UUID, Path] = {}
    for value in values:
        identity, separator, raw_path = value.partition("=")
        if (
            not separator
            or not raw_path
            or raw_path.lower().startswith(("http://", "https://"))
        ):
            raise StandardProductPackageError("invalid_arguments")
        try:
            asset_id = UUID(identity)
        except ValueError:
            raise StandardProductPackageError("invalid_arguments") from None
        if str(asset_id) != identity or asset_id in result:
            raise StandardProductPackageError("invalid_arguments")
        result[asset_id] = Path(raw_path)
    return result


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
        raise StandardProductPackageError("output_unavailable") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _artifacts(*, check: bool) -> None:
    expected = render_json(build_schema())
    if check:
        try:
            current = SCHEMA_OUTPUT.read_text(encoding="utf-8")
        except OSError:
            raise StandardProductPackageError("package_stale") from None
        if current != expected:
            raise StandardProductPackageError("package_stale")
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
        if args.verify_manifest is not None:
            _verify_manifest(args.verify_manifest)
            return 0
        batch = _load_batch(args.batch)
        write_standard_product_package(
            batch,
            _asset_paths(args.asset),
            args.output,
            check=args.check,
        )
        return 0
    except StandardProductPackageError as error:
        print(error.code, file=sys.stderr)
        return 1 if error.code == "package_stale" else 2
    except (TypeError, ValueError, ValidationError, RecursionError):
        print("invalid_input", file=sys.stderr)
        return 2


def entrypoint() -> Never:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
