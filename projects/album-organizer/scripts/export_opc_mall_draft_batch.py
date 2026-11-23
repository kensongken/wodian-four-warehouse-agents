"""Export validated OPC material receipts as draft-only mall requests."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from photo_organizer.catalog.mall_draft_export import (
    MallDraftExportError,
    build_mall_draft_batch,
)
from photo_organizer.schemas.acceptance_receipt import (
    SkuMaterialAcceptanceReceipt,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_OUTPUT = ROOT / "contracts/opc-mall-product-draft-batch-v1.schema.json"
FIXTURE_OUTPUT = (
    ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"
)
ACCEPTANCE_FIXTURE = (
    ROOT / "contracts/fixtures/opc-sku-material-acceptance-v2-synthetic.json"
)
SYNTHETIC_GENERATED_AT = datetime(2026, 8, 17, 23, 30, tzinfo=UTC)
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
        raise MallDraftExportError("invalid_arguments")


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_artifacts() -> tuple[dict[str, object], dict[str, object]]:
    schema = MallProductDraftBatch.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:photo-organizer:opc-mall-product-draft-batch:1.0"
    batch = build_mall_draft_batch(
        _synthetic_receipts(),
        generated_at=SYNTHETIC_GENERATED_AT,
    )
    fixture = batch.model_dump(mode="json")
    _assert_safe(fixture)
    return schema, fixture


def _synthetic_receipts() -> tuple[SkuMaterialAcceptanceReceipt, ...]:
    base = _load_json(ACCEPTANCE_FIXTURE)
    if not isinstance(base, dict):
        raise MallDraftExportError("invalid_input")
    receipts: list[SkuMaterialAcceptanceReceipt] = []
    for index in range(1, 11):
        value = copy.deepcopy(base)
        source_event_id = f"00000000-0000-4000-8000-{index:012x}"
        value["acceptance_id"] = f"sku-material-acceptance:{source_event_id}"
        value["album_product_id"] = f"album-product-{index:04d}"
        value["source_event_id"] = source_event_id
        value["download_confirmation_event_id"] = (
            f"50000000-0000-4000-8000-{index:012x}"
        )
        value["classification_id"] = (
            f"10000000-0000-4000-8000-{index:012x}"
        )
        value["review_id"] = f"20000000-0000-4000-8000-{index:012x}"
        value["source_type"] = (
            "first_sale_bead"
            if index <= 6
            else "legacy_material"
            if index <= 8
            else "redesign_output"
        )
        images = value.get("images")
        if not isinstance(images, list):
            raise MallDraftExportError("invalid_input")
        asset_ids: list[str] = []
        for offset, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                raise MallDraftExportError("invalid_input")
            asset_id = (
                f"30000000-0000-4000-8000-{index * 10 + offset:012x}"
            )
            evidence_id = (
                f"40000000-0000-4000-8000-{index * 10 + offset:012x}"
            )
            asset_ids.append(asset_id)
            image["asset_id"] = asset_id
            image["evidence_asset_id"] = evidence_id
            image["sha256"] = f"{index * 10 + offset:064x}"
            image["storage_ref"] = f"evidence/{evidence_id}/original"
        value["download_verified_asset_ids"] = asset_ids
        receipts.append(SkuMaterialAcceptanceReceipt.model_validate(value))
    return tuple(receipts)


def _load_json(path: Path) -> object:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MallDraftExportError("invalid_input") from None
    _assert_safe(value)
    return value


def _assert_safe(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_KEYS or normalized.endswith("_token"):
                raise MallDraftExportError("unsafe_receipt")
            _assert_safe(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _assert_safe(child)
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        raise MallDraftExportError("unsafe_receipt")


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
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise MallDraftExportError("output_unavailable") from None


def _export_artifacts(*, check: bool) -> int:
    schema, fixture = build_artifacts()
    outputs = {
        SCHEMA_OUTPUT: render_json(schema),
        FIXTURE_OUTPUT: render_json(fixture),
    }
    if check:
        for path, expected in outputs.items():
            try:
                current = path.read_text(encoding="utf-8")
            except OSError:
                return 1
            if current != expected:
                return 1
        return 0
    for path, content in outputs.items():
        _atomic_write(path, content)
    return 0


def _parse_generated_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MallDraftExportError("invalid_input") from None
    return parsed


def _batch(args: argparse.Namespace) -> MallProductDraftBatch:
    receipts = tuple(
        SkuMaterialAcceptanceReceipt.model_validate(_load_json(path))
        for path in args.receipt
    )
    return build_mall_draft_batch(
        receipts,
        generated_at=_parse_generated_at(args.generated_at),
    )


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser()
    parser.add_argument("--receipt", action="append", type=Path)
    parser.add_argument("--generated-at")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-artifacts", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    artifact_mode = args.check_artifacts or args.write_artifacts
    ordinary_values = (args.receipt, args.generated_at, args.output)
    if artifact_mode:
        if (
            args.check_artifacts and args.write_artifacts
            or args.check
            or any(value is not None for value in ordinary_values)
        ):
            raise MallDraftExportError("invalid_arguments")
    elif any(value is None for value in ordinary_values):
        raise MallDraftExportError("invalid_arguments")


def main() -> int:
    try:
        args = _parser().parse_args()
        _validate_args(args)
        if args.check_artifacts:
            if _export_artifacts(check=True):
                print("mall_draft_artifacts_stale", file=sys.stderr)
                return 1
            return 0
        if args.write_artifacts:
            return _export_artifacts(check=False)
        batch = _batch(args)
        expected = render_json(batch.model_dump(mode="json"))
        if args.check:
            try:
                current = args.output.read_text(encoding="utf-8")
            except OSError:
                raise MallDraftExportError("invalid_input") from None
            if current != expected:
                print("mall_draft_batch_stale", file=sys.stderr)
                return 1
            return 0
        _atomic_write(args.output, expected)
        return 0
    except ValidationError:
        print("invalid_input", file=sys.stderr)
        return 2
    except MallDraftExportError as error:
        print(error.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
