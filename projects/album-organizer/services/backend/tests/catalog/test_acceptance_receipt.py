from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from uuid import UUID

import pytest

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
    MaterialDataClassification,
    SkuMaterialAcceptanceInputs,
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

NOW = datetime(2026, 8, 17, 22, tzinfo=UTC)
OPC_ID = "opc-first-sku-acceptance"
ALBUM_PRODUCT_ID = "album-product-00000000-0000-4000-8000-000000000101"
SOURCE_EVENT_ID = UUID("00000000-0000-4000-8000-000000000101")
CLASSIFICATION_ID = UUID("00000000-0000-4000-8000-000000000102")
REVIEW_ID = UUID("00000000-0000-4000-8000-000000000103")
CONFIRMATION_ID = UUID("00000000-0000-4000-8000-000000000104")
EVIDENCE_IDS = (
    UUID("00000000-0000-4000-8000-000000000201"),
    UUID("00000000-0000-4000-8000-000000000202"),
)
ASSET_IDS = (
    UUID("00000000-0000-4000-8000-000000000301"),
    UUID("00000000-0000-4000-8000-000000000302"),
)
CLAIMS = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)


def valid_inputs() -> SkuMaterialAcceptanceInputs:
    images = tuple(
        ImageEvidence(
            asset_id=asset_id,
            role="original",
            sha256=f"{index + 1:064x}",
            byte_size=1000 + index,
            media_type="image/jpeg",
            captured_at=NOW,
            imported_at=NOW,
            storage_ref=f"evidence/{evidence_id}/original",
            source_asset_id=None,
            transform=None,
        )
        for index, (asset_id, evidence_id) in enumerate(
            zip(ASSET_IDS, EVIDENCE_IDS, strict=True)
        )
    )
    group = AlbumProductGroupConfirmationResponse(
        schema_version="opc-album-product-group-confirmation/1.0",
        confirmation_event_id=SOURCE_EVENT_ID,
        album_product_id=ALBUM_PRODUCT_ID,
        snapshot_version=1,
        opc_id=OPC_ID,
        evidence_asset_ids=EVIDENCE_IDS,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        confirmed_at=NOW,
        claims_not_evaluated=CLAIMS,
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
        id=REVIEW_ID,
        opc_id=OPC_ID,
        classification_id=CLASSIFICATION_ID,
        album_product_id=ALBUM_PRODUCT_ID,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        input_snapshot_version=1,
        classification_version=1,
        candidates=(candidate,),
        status="resolved",
        confirmed_level_1=Level1Category.NAN_HONG,
        confirmed_level_2=Level2Category.BRACELET,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        decided_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        claims_not_evaluated=CLAIMS,
        mall_product_id=None,
        bead_id=None,
    )
    item = ProductMaterialManifestItem(
        album_product_id=ALBUM_PRODUCT_ID,
        snapshot_version=1,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        source_event_id=SOURCE_EVENT_ID,
        source_import_task_id="00000000-0000-4000-8000-000000000401",
        images=images,
        classification=ProductMaterialClassification(
            classification_id=CLASSIFICATION_ID,
            review_id=REVIEW_ID,
            input_snapshot_version=1,
            classification_version=1,
            confirmed_level_1=Level1Category.NAN_HONG,
            confirmed_level_2=Level2Category.BRACELET,
            confirmed_by="user:00000000-0000-4000-8000-000000000001",
            confirmed_at=NOW,
        ),
        confirmation_event_id=CONFIRMATION_ID,
        confirmation_schema_version="album-product-classification-confirmed/1.0",
        claims_not_evaluated=CLAIMS,
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )
    manifest = ProductMaterialManifestResponse(
        schema_version="opc-product-material-manifest/1.0",
        opc_id=OPC_ID,
        items=[item],
        next_before=None,
    )
    downloads = tuple(
        ProductMaterialDownloadResponse(
            schema_version="opc-product-material-download/1.0",
            confirmation_event_id=CONFIRMATION_ID,
            asset_id=asset_id,
            download_url=f"https://signed.invalid/{asset_id}?secret=do-not-persist",
            expires_seconds=300,
        )
        for asset_id in ASSET_IDS
    )
    return SkuMaterialAcceptanceInputs(
        group=group,
        review=review,
        manifest=manifest,
        downloads=downloads,
    )


def test_builds_redacted_receipt_from_one_complete_chain() -> None:
    inputs = valid_inputs()

    receipt = build_acceptance_receipt(
        inputs,
        data_classification="synthetic_no_customer_data",
        validated_at=NOW,
    )

    assert receipt.schema_version == "opc-sku-material-acceptance/2.0"
    assert receipt.acceptance_status == "material_ready_for_mall_draft_not_listed"
    assert receipt.acceptance_id == f"sku-material-acceptance:{SOURCE_EVENT_ID}"
    assert receipt.album_product_id == ALBUM_PRODUCT_ID
    assert receipt.download_confirmation_event_id == CONFIRMATION_ID
    assert receipt.download_verified_asset_ids == ASSET_IDS
    assert receipt.claims_not_evaluated == CLAIMS
    assert receipt.mall_product_id is None
    assert receipt.bead_id is None
    assert receipt.auto_publish is False
    serialized = receipt.model_dump_json()
    assert "download_url" not in serialized
    assert "secret=do-not-persist" not in serialized


@pytest.mark.parametrize(
    "boundary",
    (
        "opc",
        "album_product",
        "source_type",
        "snapshot_version",
        "source_event",
        "review_id",
        "classification_id",
        "classification_version",
        "unresolved_review",
        "confirmed_level",
        "confirmed_by",
        "confirmed_at",
        "claims",
        "multiple_manifest_items",
        "manifest_cursor",
    ),
)
def test_chain_mismatches_fail_with_a_stable_code(boundary: str) -> None:
    inputs = valid_inputs()
    group = inputs.group
    review = inputs.review
    manifest = inputs.manifest
    item = manifest.items[0]
    classification = item.classification

    if boundary == "opc":
        review = review.model_copy(update={"opc_id": "opc-other"})
    elif boundary == "album_product":
        group = group.model_copy(update={"album_product_id": "album-product-other"})
    elif boundary == "source_type":
        review = review.model_copy(
            update={"source_type": ProductSourceType.LEGACY_MATERIAL}
        )
    elif boundary == "snapshot_version":
        group = group.model_copy(update={"snapshot_version": 2})
    elif boundary == "source_event":
        item = item.model_copy(
            update={"source_event_id": UUID("00000000-0000-4000-8000-000000000999")}
        )
    elif boundary == "review_id":
        classification = classification.model_copy(
            update={"review_id": UUID("00000000-0000-4000-8000-000000000999")}
        )
    elif boundary == "classification_id":
        classification = classification.model_copy(
            update={"classification_id": UUID("00000000-0000-4000-8000-000000000999")}
        )
    elif boundary == "classification_version":
        review = review.model_copy(update={"classification_version": 2})
    elif boundary == "unresolved_review":
        review = review.model_copy(update={"status": "open"})
    elif boundary == "confirmed_level":
        review = review.model_copy(update={"confirmed_level_1": Level1Category.OTHER})
    elif boundary == "confirmed_by":
        review = review.model_copy(update={"confirmed_by": "user:other"})
    elif boundary == "confirmed_at":
        review = review.model_copy(
            update={"decided_at": datetime(2026, 8, 17, 23, tzinfo=UTC)}
        )
    elif boundary == "claims":
        review = review.model_copy(update={"claims_not_evaluated": ("authenticity",)})
    elif boundary == "multiple_manifest_items":
        manifest = manifest.model_copy(update={"items": [item, item]})
    else:
        manifest = manifest.model_copy(update={"next_before": "unexpected-cursor"})

    if classification is not item.classification:
        item = item.model_copy(update={"classification": classification})
    if boundary not in {"multiple_manifest_items", "manifest_cursor"}:
        manifest = manifest.model_copy(update={"items": [item]})
    changed = inputs.model_copy(
        update={"group": group, "review": review, "manifest": manifest}
    )

    with pytest.raises(
        MaterialAcceptanceError, match="^acceptance_chain_mismatch$"
    ) as error:
        build_acceptance_receipt(
            changed,
            data_classification="synthetic_no_customer_data",
            validated_at=NOW,
        )

    assert error.value.code == "acceptance_chain_mismatch"


@pytest.mark.parametrize(
    "boundary",
    (
        "traversal_ref",
        "non_uuid_ref",
        "uppercase_ref",
        "crop_ref",
        "evidence_order",
        "duplicate_asset",
        "duplicate_hash",
        "crop_role",
        "one_image",
        "five_images",
    ),
)
def test_invalid_evidence_sets_fail_closed(boundary: str) -> None:
    inputs = valid_inputs()
    item = inputs.manifest.items[0]
    images = list(item.images)
    group = inputs.group

    if boundary == "traversal_ref":
        images[0] = images[0].model_copy(update={"storage_ref": "evidence/../original"})
    elif boundary == "non_uuid_ref":
        images[0] = images[0].model_copy(
            update={"storage_ref": "evidence/not-a-uuid/original"}
        )
    elif boundary == "uppercase_ref":
        uppercase_evidence_id = UUID("abcdefab-cdef-4abc-8def-abcdefabcdef")
        images[0] = images[0].model_copy(
            update={
                "storage_ref": (
                    f"evidence/{str(uppercase_evidence_id).upper()}/original"
                )
            }
        )
        group = group.model_copy(
            update={"evidence_asset_ids": (uppercase_evidence_id, EVIDENCE_IDS[1])}
        )
    elif boundary == "crop_ref":
        images[0] = images[0].model_copy(
            update={"storage_ref": f"evidence/{EVIDENCE_IDS[0]}/crop"}
        )
    elif boundary == "evidence_order":
        group = group.model_copy(update={"evidence_asset_ids": EVIDENCE_IDS[::-1]})
    elif boundary == "duplicate_asset":
        images[1] = images[1].model_copy(update={"asset_id": images[0].asset_id})
    elif boundary == "duplicate_hash":
        images[1] = images[1].model_copy(update={"sha256": images[0].sha256})
    elif boundary == "crop_role":
        images[0] = images[0].model_copy(
            update={"role": "crop", "source_asset_id": images[1].asset_id}
        )
    elif boundary == "one_image":
        images = images[:1]
        group = group.model_copy(update={"evidence_asset_ids": EVIDENCE_IDS[:1]})
    else:
        images = [*images, images[0], images[1], images[0]]

    item = item.model_copy(update={"images": tuple(images)})
    manifest = inputs.manifest.model_copy(update={"items": [item]})
    changed = inputs.model_copy(update={"group": group, "manifest": manifest})

    with pytest.raises(MaterialAcceptanceError, match="^invalid_evidence_set$"):
        build_acceptance_receipt(
            changed,
            data_classification="synthetic_no_customer_data",
            validated_at=NOW,
        )


@pytest.mark.parametrize(
    "boundary",
    (
        "missing",
        "duplicate",
        "wrong_confirmation",
        "wrong_ttl",
        "empty_url",
    ),
)
def test_invalid_download_receipts_fail_closed(boundary: str) -> None:
    inputs = valid_inputs()
    downloads = list(inputs.downloads)

    if boundary == "missing":
        downloads = downloads[:1]
    elif boundary == "duplicate":
        downloads[1] = downloads[0]
    elif boundary == "wrong_confirmation":
        downloads[0] = downloads[0].model_copy(
            update={
                "confirmation_event_id": UUID("00000000-0000-4000-8000-000000000999")
            }
        )
    elif boundary == "wrong_ttl":
        downloads[0] = downloads[0].model_copy(update={"expires_seconds": 301})
    else:
        downloads[0] = downloads[0].model_copy(update={"download_url": ""})

    changed = inputs.model_copy(update={"downloads": tuple(downloads)})

    with pytest.raises(MaterialAcceptanceError, match="^download_verification_failed$"):
        build_acceptance_receipt(
            changed,
            data_classification="synthetic_no_customer_data",
            validated_at=NOW,
        )


def test_download_receipt_order_does_not_change_manifest_order() -> None:
    inputs = valid_inputs()
    changed = inputs.model_copy(update={"downloads": inputs.downloads[::-1]})

    receipt = build_acceptance_receipt(
        changed,
        data_classification="synthetic_no_customer_data",
        validated_at=NOW,
    )

    assert receipt.download_verified_asset_ids == ASSET_IDS


@pytest.mark.parametrize(
    ("data_classification", "validated_at"),
    (
        ("untrusted", NOW),
        (
            "synthetic_no_customer_data",
            datetime(2026, 8, 17, 22),  # noqa: DTZ001 - intentional invalid input
        ),
        (
            "synthetic_no_customer_data",
            datetime(2026, 8, 17, 22, tzinfo=timezone(timedelta(hours=8))),
        ),
    ),
)
def test_invalid_receipt_metadata_has_a_stable_error(
    data_classification: str,
    validated_at: datetime,
) -> None:
    with pytest.raises(MaterialAcceptanceError, match="^invalid_input$"):
        build_acceptance_receipt(
            valid_inputs(),
            data_classification=cast(MaterialDataClassification, data_classification),
            validated_at=validated_at,
        )
