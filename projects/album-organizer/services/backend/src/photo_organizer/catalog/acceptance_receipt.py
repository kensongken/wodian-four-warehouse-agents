from datetime import datetime, timedelta
from typing import Final
from uuid import RFC_4122, UUID

from photo_organizer.categorization.contracts import ImageEvidence
from photo_organizer.schemas.acceptance_receipt import (
    AcceptedImageEvidence,
    MaterialDataClassification,
    SkuMaterialAcceptanceInputs,
    SkuMaterialAcceptanceReceipt,
)
from photo_organizer.schemas.product_material_manifest import (
    ProductMaterialManifestItem,
)

EXPECTED_CLAIMS: Final = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)


class MaterialAcceptanceError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_acceptance_receipt(
    inputs: SkuMaterialAcceptanceInputs,
    *,
    data_classification: MaterialDataClassification,
    validated_at: datetime,
) -> SkuMaterialAcceptanceReceipt:
    if (
        validated_at.tzinfo is None
        or validated_at.utcoffset() != timedelta(0)
        or data_classification
        not in {
            "synthetic_no_customer_data",
            "opc_owned_product_material_no_customer_data",
        }
    ):
        raise MaterialAcceptanceError("invalid_input")
    item = _single_manifest_item(inputs)
    _validate_chain(inputs, item)
    images = _validated_images(inputs, item.images)
    download_asset_ids = _validated_downloads(inputs, item)
    classification = item.classification
    return SkuMaterialAcceptanceReceipt(
        schema_version="opc-sku-material-acceptance/2.0",
        acceptance_id=(f"sku-material-acceptance:{inputs.group.confirmation_event_id}"),
        validated_at=validated_at,
        data_classification=data_classification,
        acceptance_status="material_ready_for_mall_draft_not_listed",
        opc_id=inputs.manifest.opc_id,
        album_product_id=item.album_product_id,
        snapshot_version=item.snapshot_version,
        source_type=item.source_type,
        source_event_id=item.source_event_id,
        download_confirmation_event_id=item.confirmation_event_id,
        classification_id=classification.classification_id,
        review_id=classification.review_id,
        classification_version=classification.classification_version,
        confirmed_level_1=classification.confirmed_level_1,
        confirmed_level_2=classification.confirmed_level_2,
        confirmed_by=classification.confirmed_by,
        confirmed_at=classification.confirmed_at,
        images=images,
        download_verified_asset_ids=download_asset_ids,
        download_ttl_seconds=300,
        claims_not_evaluated=EXPECTED_CLAIMS,
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )


def _single_manifest_item(
    inputs: SkuMaterialAcceptanceInputs,
) -> ProductMaterialManifestItem:
    if len(inputs.manifest.items) != 1 or inputs.manifest.next_before is not None:
        raise MaterialAcceptanceError("acceptance_chain_mismatch")
    return inputs.manifest.items[0]


def _validate_chain(
    inputs: SkuMaterialAcceptanceInputs,
    item: ProductMaterialManifestItem,
) -> None:
    group = inputs.group
    review = inputs.review
    classification = item.classification
    confirmation_times = (
        group.confirmed_at,
        review.decided_at,
        classification.confirmed_at,
    )
    if any(
        value is None or value.tzinfo is None or value.utcoffset() != timedelta(0)
        for value in confirmation_times
    ):
        raise MaterialAcceptanceError("invalid_input")
    if not (
        group.opc_id == review.opc_id == inputs.manifest.opc_id
        and group.album_product_id == review.album_product_id == item.album_product_id
        and group.source_type == review.source_type == item.source_type
        and group.snapshot_version
        == review.input_snapshot_version
        == item.snapshot_version
        == classification.input_snapshot_version
        and group.confirmation_event_id == item.source_event_id
        and review.id == classification.review_id
        and review.classification_id == classification.classification_id
        and review.classification_version == classification.classification_version
        and review.status == "resolved"
        and review.confirmed_level_1 == classification.confirmed_level_1
        and review.confirmed_level_2 == classification.confirmed_level_2
        and review.confirmed_by == classification.confirmed_by
        and review.decided_at == classification.confirmed_at
        and group.claims_not_evaluated
        == review.claims_not_evaluated
        == item.claims_not_evaluated
        == EXPECTED_CLAIMS
    ):
        raise MaterialAcceptanceError("acceptance_chain_mismatch")


def _validated_images(
    inputs: SkuMaterialAcceptanceInputs,
    images: tuple[ImageEvidence, ...],
) -> tuple[AcceptedImageEvidence, ...]:
    if not 2 <= len(images) <= 4:
        raise MaterialAcceptanceError("invalid_evidence_set")
    accepted: list[AcceptedImageEvidence] = []
    for image in images:
        evidence_id = _evidence_id(image)
        accepted.append(
            AcceptedImageEvidence(
                asset_id=image.asset_id,
                evidence_asset_id=evidence_id,
                role="original",
                sha256=image.sha256,
                byte_size=image.byte_size,
                media_type=image.media_type,
                captured_at=image.captured_at,
                imported_at=image.imported_at,
                storage_ref=image.storage_ref,
            )
        )
    asset_ids = tuple(image.asset_id for image in accepted)
    evidence_ids = tuple(image.evidence_asset_id for image in accepted)
    hashes = tuple(image.sha256 for image in accepted)
    if (
        evidence_ids != inputs.group.evidence_asset_ids
        or len(set(asset_ids)) != len(asset_ids)
        or len(set(evidence_ids)) != len(evidence_ids)
        or len(set(hashes)) != len(hashes)
    ):
        raise MaterialAcceptanceError("invalid_evidence_set")
    return tuple(accepted)


def _evidence_id(image: ImageEvidence) -> UUID:
    parts = image.storage_ref.split("/")
    if len(parts) != 3 or parts[0] != "evidence" or parts[2] != "original":
        raise MaterialAcceptanceError("invalid_evidence_set")
    if image.role != "original" or image.source_asset_id is not None:
        raise MaterialAcceptanceError("invalid_evidence_set")
    try:
        evidence_id = UUID(parts[1])
    except ValueError:
        raise MaterialAcceptanceError("invalid_evidence_set") from None
    if (
        str(evidence_id) != parts[1]
        or evidence_id.version not in {1, 2, 3, 4, 5}
        or evidence_id.variant != RFC_4122
    ):
        raise MaterialAcceptanceError("invalid_evidence_set")
    return evidence_id


def _validated_downloads(
    inputs: SkuMaterialAcceptanceInputs,
    item: ProductMaterialManifestItem,
) -> tuple[UUID, ...]:
    asset_ids = tuple(image.asset_id for image in item.images)
    download_ids = tuple(download.asset_id for download in inputs.downloads)
    if (
        set(download_ids) != set(asset_ids)
        or len(set(download_ids)) != len(download_ids)
        or any(
            download.confirmation_event_id != item.confirmation_event_id
            or download.expires_seconds != 300
            or not download.download_url
            for download in inputs.downloads
        )
    ):
        raise MaterialAcceptanceError("download_verification_failed")
    return asset_ids
