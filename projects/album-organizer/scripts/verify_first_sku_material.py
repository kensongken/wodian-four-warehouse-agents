"""Validate one OPC SKU material chain and emit a redacted receipt."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never
from uuid import UUID

from pydantic import ValidationError

from photo_organizer.catalog.acceptance_receipt import (
    MaterialAcceptanceError,
    build_acceptance_receipt,
)
from photo_organizer.categorization.contracts import (
    ImageEvidence,
    Level1Category,
    Level2Category,
    ProductSourceType,
    RankedCandidate,
    ReasonCode,
)
from photo_organizer.schemas.acceptance_receipt import (
    SkuMaterialAcceptanceInputs,
    SkuMaterialAcceptanceReceipt,
    SkuMaterialAcceptanceReceiptV1,
    UnevaluatedClaims,
)
from photo_organizer.schemas.classification_reviews import ClassificationReviewResponse
from photo_organizer.schemas.group_confirmation import (
    AlbumProductGroupConfirmationResponse,
)
from photo_organizer.schemas.product_material_download import (
    ProductMaterialDownloadResponse,
)
from photo_organizer.schemas.product_material_manifest import (
    ProductMaterialClassification,
    ProductMaterialManifestItem,
    ProductMaterialManifestResponse,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_OUTPUT = ROOT / "contracts/opc-sku-material-acceptance-v2.schema.json"
FIXTURE_OUTPUT = (
    ROOT / "contracts/fixtures/opc-sku-material-acceptance-v2-synthetic.json"
)
SYNTHETIC_NOW = datetime(2026, 8, 17, 22, tzinfo=UTC)
SYNTHETIC_SIGNED_URL = "https://signed.invalid/private?redacted=synthetic"
FORBIDDEN_KEYS = {
    "access_token",
    "address",
    "authorization",
    "chat_text",
    "customer_id",
    "customer_name",
    "mobile",
    "order_id",
    "phone",
    "private_key",
    "refresh_token",
    "secret_access_key",
    "source_object_key",
    "token",
    "wechat",
}


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise MaterialAcceptanceError("invalid_arguments")


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_artifacts() -> tuple[dict[str, object], dict[str, object]]:
    schema = SkuMaterialAcceptanceReceipt.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:photo-organizer:opc-sku-material-acceptance:2.0"
    inputs = _parse_bundle(
        synthetic_source_bundle(download_url=SYNTHETIC_SIGNED_URL)
    )
    receipt = build_acceptance_receipt(
        inputs,
        data_classification="synthetic_no_customer_data",
        validated_at=SYNTHETIC_NOW,
    )
    return schema, receipt.model_dump(mode="json")


def synthetic_source_bundle(*, download_url: str) -> dict[str, object]:
    opc_id = "opc-synthetic-first-sku"
    album_product_id = "album-product-00000000-0000-4000-8000-000000000101"
    source_event_id = UUID("00000000-0000-4000-8000-000000000101")
    classification_id = UUID("00000000-0000-4000-8000-000000000102")
    review_id = UUID("00000000-0000-4000-8000-000000000103")
    confirmation_id = UUID("00000000-0000-4000-8000-000000000104")
    evidence_ids = (
        UUID("00000000-0000-4000-8000-000000000201"),
        UUID("00000000-0000-4000-8000-000000000202"),
    )
    asset_ids = (
        UUID("00000000-0000-4000-8000-000000000301"),
        UUID("00000000-0000-4000-8000-000000000302"),
    )
    claims: UnevaluatedClaims = (
        "authenticity",
        "origin",
        "quality",
        "warehouse_acceptance",
    )
    images = tuple(
        ImageEvidence(
            asset_id=asset_id,
            role="original",
            sha256=f"{index + 1:064x}",
            byte_size=1000 + index,
            media_type="image/jpeg",
            captured_at=SYNTHETIC_NOW,
            imported_at=SYNTHETIC_NOW,
            storage_ref=f"evidence/{evidence_id}/original",
            source_asset_id=None,
            transform=None,
        )
        for index, (asset_id, evidence_id) in enumerate(
            zip(asset_ids, evidence_ids, strict=True)
        )
    )
    group = AlbumProductGroupConfirmationResponse(
        schema_version="opc-album-product-group-confirmation/1.0",
        confirmation_event_id=source_event_id,
        album_product_id=album_product_id,
        snapshot_version=1,
        opc_id=opc_id,
        evidence_asset_ids=evidence_ids,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        confirmed_at=SYNTHETIC_NOW,
        claims_not_evaluated=claims,
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )
    candidate = RankedCandidate(
        rank=1,
        level_1=Level1Category.NAN_HONG,
        level_2=Level2Category.BRACELET,
        confidence=0.9,
        reason_codes=(ReasonCode.SEED_MATCH,),
    )
    review = ClassificationReviewResponse(
        id=review_id,
        opc_id=opc_id,
        classification_id=classification_id,
        album_product_id=album_product_id,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        input_snapshot_version=1,
        classification_version=1,
        candidates=(candidate,),
        status="resolved",
        confirmed_level_1=Level1Category.NAN_HONG,
        confirmed_level_2=Level2Category.BRACELET,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        decided_at=SYNTHETIC_NOW,
        created_at=SYNTHETIC_NOW,
        updated_at=SYNTHETIC_NOW,
        claims_not_evaluated=claims,
        mall_product_id=None,
        bead_id=None,
    )
    item = ProductMaterialManifestItem(
        album_product_id=album_product_id,
        snapshot_version=1,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        source_event_id=source_event_id,
        source_import_task_id="00000000-0000-4000-8000-000000000401",
        images=images,
        classification=ProductMaterialClassification(
            classification_id=classification_id,
            review_id=review_id,
            input_snapshot_version=1,
            classification_version=1,
            confirmed_level_1=Level1Category.NAN_HONG,
            confirmed_level_2=Level2Category.BRACELET,
            confirmed_by="user:00000000-0000-4000-8000-000000000001",
            confirmed_at=SYNTHETIC_NOW,
        ),
        confirmation_event_id=confirmation_id,
        confirmation_schema_version="album-product-classification-confirmed/1.0",
        claims_not_evaluated=claims,
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )
    manifest = ProductMaterialManifestResponse(
        schema_version="opc-product-material-manifest/1.0",
        opc_id=opc_id,
        items=[item],
        next_before=None,
    )
    downloads = tuple(
        ProductMaterialDownloadResponse(
            schema_version="opc-product-material-download/1.0",
            confirmation_event_id=confirmation_id,
            asset_id=asset_id,
            download_url=download_url,
            expires_seconds=300,
        )
        for asset_id in asset_ids
    )
    return {
        "group": group.model_dump(mode="json"),
        "review": review.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "downloads": [download.model_dump(mode="json") for download in downloads],
    }


def _parse_bundle(bundle: dict[str, object]) -> SkuMaterialAcceptanceInputs:
    try:
        group_value = _strict_object(
            bundle["group"], AlbumProductGroupConfirmationResponse
        )
        review_value = _strict_object(bundle["review"], ClassificationReviewResponse)
        manifest_value = _strict_object(
            bundle["manifest"], ProductMaterialManifestResponse
        )
        downloads_value = bundle["downloads"]
        if not isinstance(downloads_value, list):
            raise MaterialAcceptanceError("invalid_input")
        downloads = tuple(
            ProductMaterialDownloadResponse.model_validate(
                _strict_download_object(value)
            )
            for value in downloads_value
        )
        return SkuMaterialAcceptanceInputs(
            group=AlbumProductGroupConfirmationResponse.model_validate(group_value),
            review=ClassificationReviewResponse.model_validate(review_value),
            manifest=ProductMaterialManifestResponse.model_validate(manifest_value),
            downloads=downloads,
        )
    except KeyError:
        raise MaterialAcceptanceError("invalid_input") from None
    except ValidationError:
        raise MaterialAcceptanceError("invalid_input") from None


def _strict_object(value: object, model: type[Any]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MaterialAcceptanceError("invalid_input")
    _assert_safe(value, allow_download_url=False)
    if set(value) != set(model.model_fields):
        raise MaterialAcceptanceError("invalid_input")
    return value


def _strict_download_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MaterialAcceptanceError("invalid_input")
    _assert_safe(value, allow_download_url=True)
    if set(value) != set(ProductMaterialDownloadResponse.model_fields):
        raise MaterialAcceptanceError("invalid_input")
    return value


def _assert_safe(value: object, *, allow_download_url: bool) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in FORBIDDEN_KEYS or normalized.endswith("_token"):
                raise MaterialAcceptanceError("unsafe_material_data")
            if normalized == "download_url" and not allow_download_url:
                raise MaterialAcceptanceError("unsafe_material_data")
            _assert_safe(
                child,
                allow_download_url=allow_download_url and normalized == "download_url",
            )
    elif isinstance(value, list):
        for child in value:
            _assert_safe(child, allow_download_url=False)
    elif isinstance(value, str) and not allow_download_url:
        if value.startswith(("http://", "https://", "file:", "content:", "/")):
            raise MaterialAcceptanceError("unsafe_material_data")
        if "X-Amz-" in value or value.startswith("private/"):
            raise MaterialAcceptanceError("unsafe_material_data")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MaterialAcceptanceError("invalid_input") from None


def _load_bundle(args: argparse.Namespace) -> dict[str, object]:
    return {
        "group": _load_json(args.group),
        "review": _load_json(args.review),
        "manifest": _load_json(args.manifest),
        "downloads": _load_json(args.downloads),
    }


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise MaterialAcceptanceError("output_unavailable") from None


def _export_artifacts(*, check: bool) -> int:
    schema, fixture = build_artifacts()
    expected = {
        SCHEMA_OUTPUT: render_json(schema),
        FIXTURE_OUTPUT: render_json(fixture),
    }
    if check:
        return int(
            any(
                not path.is_file()
                or path.read_text(encoding="utf-8") != content
                for path, content in expected.items()
            )
        )
    for path, content in expected.items():
        _atomic_write(path, content)
    return 0


def _receipt(args: argparse.Namespace) -> SkuMaterialAcceptanceReceipt:
    inputs = _parse_bundle(_load_bundle(args))
    if args.check:
        try:
            current_value = _load_json(args.output)
            if not isinstance(current_value, dict):
                raise MaterialAcceptanceError("invalid_input")
            if current_value.get("schema_version") == (
                "opc-sku-material-acceptance/1.0"
            ):
                validated_at = (
                    SkuMaterialAcceptanceReceiptV1.model_validate(
                        current_value
                    ).validated_at
                )
            else:
                validated_at = (
                    SkuMaterialAcceptanceReceipt.model_validate(
                        current_value
                    ).validated_at
                )
        except ValidationError:
            raise MaterialAcceptanceError("invalid_input") from None
    else:
        validated_at = datetime.now(UTC)
    return build_acceptance_receipt(
        inputs,
        data_classification=args.data_classification,
        validated_at=validated_at,
    )


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser()
    parser.add_argument(
        "--data-classification",
        choices=(
            "synthetic_no_customer_data",
            "opc_owned_product_material_no_customer_data",
        ),
    )
    parser.add_argument("--group", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--downloads", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-artifacts", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    artifact_mode = args.check_artifacts or args.write_artifacts
    receipt_values = (
        args.data_classification,
        args.group,
        args.review,
        args.manifest,
        args.downloads,
        args.output,
    )
    if artifact_mode:
        if args.check_artifacts and args.write_artifacts:
            raise MaterialAcceptanceError("invalid_arguments")
        if args.check or any(value is not None for value in receipt_values):
            raise MaterialAcceptanceError("invalid_arguments")
    elif any(value is None for value in receipt_values):
        raise MaterialAcceptanceError("invalid_arguments")


def main() -> int:
    try:
        args = _parser().parse_args()
        _validate_args(args)
        if args.check_artifacts:
            if _export_artifacts(check=True):
                print("acceptance_artifacts_stale", file=sys.stderr)
                return 1
            return 0
        if args.write_artifacts:
            return _export_artifacts(check=False)
        receipt = _receipt(args)
        expected = render_json(receipt.model_dump(mode="json"))
        if args.check:
            try:
                current = args.output.read_text(encoding="utf-8")
            except OSError:
                raise MaterialAcceptanceError("invalid_input") from None
            if current != expected:
                print("acceptance_receipt_stale", file=sys.stderr)
                return 1
            return 0
        _atomic_write(args.output, expected)
        return 0
    except ValidationError:
        print("invalid_input", file=sys.stderr)
        return 2
    except MaterialAcceptanceError as error:
        print(error.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
