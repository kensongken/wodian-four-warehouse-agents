import json
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Final
from uuid import RFC_4122, UUID

from pydantic import ValidationError

from photo_organizer.schemas.acceptance_receipt import (
    AcceptedImageEvidence,
    SkuMaterialAcceptanceReceipt,
)
from photo_organizer.schemas.mall_draft_export import (
    BusinessFieldRequirements,
    MallDraftClassification,
    MallDraftDownloadLookup,
    MallProductDraftBatch,
    MallProductDraftRequest,
)

EXPECTED_CLAIMS: Final = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)
REQUIRED_BUSINESS_FIELDS: Final[BusinessFieldRequirements] = (
    "title",
    "selling_points",
    "price",
    "inventory",
    "sku",
    "customization",
    "origin_evidence",
)


class MallDraftExportError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_mall_draft_batch(
    receipts: tuple[SkuMaterialAcceptanceReceipt, ...],
    *,
    generated_at: datetime,
) -> MallProductDraftBatch:
    if (
        not 1 <= len(receipts) <= 10
        or generated_at.tzinfo is None
        or generated_at.utcoffset() != timedelta(0)
    ):
        raise MallDraftExportError("invalid_input")
    ordered = tuple(
        sorted(
            receipts,
            key=lambda receipt: (
                receipt.album_product_id,
                receipt.snapshot_version,
                receipt.acceptance_id,
            ),
        )
    )
    opc_id = ordered[0].opc_id
    data_classification = ordered[0].data_classification
    if any(receipt.opc_id != opc_id for receipt in ordered):
        raise MallDraftExportError("mixed_opc_batch")
    if data_classification not in {
        "synthetic_no_customer_data",
        "opc_owned_product_material_no_customer_data",
    } or any(receipt.data_classification != data_classification for receipt in ordered):
        raise MallDraftExportError("invalid_input")
    for receipt in ordered:
        _validate_receipt(receipt)
    _validate_unique_source_identities(ordered)
    items = tuple(_draft_request(receipt) for receipt in ordered)
    canonical = json.dumps(
        {
            "opc_id": opc_id,
            "data_classification": data_classification,
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": ("emit_change_proposal_do_not_overwrite"),
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": [item.model_dump(mode="json") for item in items],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    batch_id = f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"
    return MallProductDraftBatch(
        schema_version="opc-mall-product-draft-batch/1.0",
        batch_id=batch_id,
        opc_id=opc_id,
        data_classification=data_classification,
        export_version=1,
        generated_at=generated_at,
        operation="create_draft_only",
        existing_product_policy="emit_change_proposal_do_not_overwrite",
        auto_publish=False,
        shared_market_opt_in=False,
        items=items,
    )


def _validate_unique_source_identities(
    receipts: tuple[SkuMaterialAcceptanceReceipt, ...],
) -> None:
    acceptance_ids = {receipt.acceptance_id for receipt in receipts}
    event_ids = {receipt.source_event_id for receipt in receipts}
    download_event_ids = {
        receipt.download_confirmation_event_id for receipt in receipts
    }
    album_product_ids = {receipt.album_product_id for receipt in receipts}
    classification_ids = {receipt.classification_id for receipt in receipts}
    review_ids = {receipt.review_id for receipt in receipts}
    snapshots = {
        (receipt.album_product_id, receipt.snapshot_version) for receipt in receipts
    }
    if not (
        len(acceptance_ids)
        == len(event_ids)
        == len(download_event_ids)
        == len(album_product_ids)
        == len(classification_ids)
        == len(review_ids)
        == len(snapshots)
        == len(receipts)
    ):
        raise MallDraftExportError("duplicate_source_identity")
    images = tuple(image for receipt in receipts for image in receipt.images)
    if not (
        len({image.asset_id for image in images})
        == len({image.evidence_asset_id for image in images})
        == len({image.sha256 for image in images})
        == len(images)
    ):
        raise MallDraftExportError("duplicate_source_identity")


def _draft_request(
    receipt: SkuMaterialAcceptanceReceipt,
) -> MallProductDraftRequest:
    try:
        return MallProductDraftRequest(
            draft_request_id=(
                "mall-draft-request:"
                f"{sha256(receipt.acceptance_id.encode()).hexdigest()}"
            ),
            source_acceptance_id=receipt.acceptance_id,
            source_group_confirmation_event_id=receipt.source_event_id,
            source_album_product_id=receipt.album_product_id,
            source_snapshot_version=receipt.snapshot_version,
            source_type=receipt.source_type,
            classification=MallDraftClassification(
                classification_id=receipt.classification_id,
                review_id=receipt.review_id,
                classification_version=receipt.classification_version,
                confirmed_level_1=receipt.confirmed_level_1,
                confirmed_level_2=receipt.confirmed_level_2,
                confirmed_by=receipt.confirmed_by,
                confirmed_at=receipt.confirmed_at,
            ),
            images=receipt.images,
            download_lookup=tuple(
                MallDraftDownloadLookup(
                    confirmation_event_id=receipt.download_confirmation_event_id,
                    asset_id=image.asset_id,
                )
                for image in receipt.images
            ),
            required_business_fields=REQUIRED_BUSINESS_FIELDS,
            claims_not_evaluated=EXPECTED_CLAIMS,
            mall_product_id=None,
            bead_id=None,
            publication_state="draft",
            auto_publish=False,
            shared_market_opt_in=False,
        )
    except ValidationError:
        raise MallDraftExportError("unsafe_receipt") from None


def _validate_receipt(receipt: SkuMaterialAcceptanceReceipt) -> None:
    if (
        receipt.acceptance_status != "material_ready_for_mall_draft_not_listed"
        or receipt.publication_mode != "draft_only"
        or receipt.auto_publish is not False
        or receipt.mall_product_id is not None
        or receipt.bead_id is not None
        or receipt.claims_not_evaluated != EXPECTED_CLAIMS
        or receipt.download_ttl_seconds != 300
    ):
        raise MallDraftExportError("receipt_not_draft_ready")
    if (
        not _utc(receipt.validated_at)
        or not _utc(receipt.confirmed_at)
        or not 2 <= len(receipt.images) <= 4
    ):
        raise MallDraftExportError("unsafe_receipt")
    asset_ids = tuple(image.asset_id for image in receipt.images)
    evidence_ids = tuple(image.evidence_asset_id for image in receipt.images)
    hashes = tuple(image.sha256 for image in receipt.images)
    if (
        set(asset_ids) != set(receipt.download_verified_asset_ids)
        or len(set(receipt.download_verified_asset_ids))
        != len(receipt.download_verified_asset_ids)
        or len(set(asset_ids)) != len(asset_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
        or len(set(hashes)) != len(hashes)
        or any(not _safe_image(image) for image in receipt.images)
    ):
        raise MallDraftExportError("unsafe_receipt")


def _safe_image(image: AcceptedImageEvidence) -> bool:
    if (
        image.role != "original"
        or not _utc(image.imported_at)
        or (image.captured_at is not None and not _utc(image.captured_at))
    ):
        return False
    parts = image.storage_ref.split("/")
    if len(parts) != 3 or parts[0] != "evidence" or parts[2] != "original":
        return False
    try:
        evidence_id = UUID(parts[1])
    except ValueError:
        return False
    return (
        str(evidence_id) == parts[1]
        and evidence_id.version in {1, 2, 3, 4, 5}
        and evidence_id.variant == RFC_4122
        and evidence_id == image.evidence_asset_id
    )


def _utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)
