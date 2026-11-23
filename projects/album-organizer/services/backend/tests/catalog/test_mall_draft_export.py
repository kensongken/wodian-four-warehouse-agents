from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from photo_organizer.catalog.mall_draft_export import (
    MallDraftExportError,
    build_mall_draft_batch,
)
from photo_organizer.categorization.contracts import (
    Level1Category,
    Level2Category,
    ProductSourceType,
)
from photo_organizer.schemas.acceptance_receipt import (
    AcceptedImageEvidence,
    SkuMaterialAcceptanceReceipt,
    UnevaluatedClaims,
)

NOW = datetime(2026, 8, 17, 23, tzinfo=UTC)
CLAIMS: UnevaluatedClaims = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)
BUSINESS_FIELDS = (
    "title",
    "selling_points",
    "price",
    "inventory",
    "sku",
    "customization",
    "origin_evidence",
)


def valid_receipt(
    index: int,
    *,
    opc_id: str = "opc-mall-draft-export",
) -> SkuMaterialAcceptanceReceipt:
    source_event_id = UUID(f"00000000-0000-4000-8000-{index:012x}")
    download_confirmation_event_id = UUID(f"50000000-0000-4000-8000-{index:012x}")
    classification_id = UUID(f"10000000-0000-4000-8000-{index:012x}")
    review_id = UUID(f"20000000-0000-4000-8000-{index:012x}")
    asset_ids = (
        UUID(f"30000000-0000-4000-8000-{index * 10 + 1:012x}"),
        UUID(f"30000000-0000-4000-8000-{index * 10 + 2:012x}"),
    )
    evidence_ids = (
        UUID(f"40000000-0000-4000-8000-{index * 10 + 1:012x}"),
        UUID(f"40000000-0000-4000-8000-{index * 10 + 2:012x}"),
    )
    images = tuple(
        AcceptedImageEvidence(
            asset_id=asset_id,
            evidence_asset_id=evidence_id,
            role="original",
            sha256=f"{index * 10 + offset:064x}",
            byte_size=1000 + offset,
            media_type="image/jpeg",
            captured_at=NOW,
            imported_at=NOW,
            storage_ref=f"evidence/{evidence_id}/original",
        )
        for offset, (asset_id, evidence_id) in enumerate(
            zip(asset_ids, evidence_ids, strict=True),
            start=1,
        )
    )
    return SkuMaterialAcceptanceReceipt(
        schema_version="opc-sku-material-acceptance/2.0",
        acceptance_id=f"sku-material-acceptance:{source_event_id}",
        validated_at=NOW,
        data_classification="synthetic_no_customer_data",
        acceptance_status="material_ready_for_mall_draft_not_listed",
        opc_id=opc_id,
        album_product_id=f"album-product-{index:04d}",
        snapshot_version=1,
        source_type=ProductSourceType.FIRST_SALE_BEAD,
        source_event_id=source_event_id,
        download_confirmation_event_id=download_confirmation_event_id,
        classification_id=classification_id,
        review_id=review_id,
        classification_version=1,
        confirmed_level_1=Level1Category.NAN_HONG,
        confirmed_level_2=Level2Category.BRACELET,
        confirmed_by="user:00000000-0000-4000-8000-000000000001",
        confirmed_at=NOW,
        images=images,
        download_verified_asset_ids=asset_ids,
        download_ttl_seconds=300,
        claims_not_evaluated=CLAIMS,
        mall_product_id=None,
        bead_id=None,
        publication_mode="draft_only",
        auto_publish=False,
    )


def test_builds_sorted_draft_only_requests_without_downstream_identity() -> None:
    first = valid_receipt(1)
    second = valid_receipt(2)

    batch = build_mall_draft_batch((second, first), generated_at=NOW)

    assert batch.schema_version == "opc-mall-product-draft-batch/1.0"
    assert batch.operation == "create_draft_only"
    assert batch.existing_product_policy == "emit_change_proposal_do_not_overwrite"
    assert batch.auto_publish is False
    assert batch.shared_market_opt_in is False
    assert [item.source_acceptance_id for item in batch.items] == [
        first.acceptance_id,
        second.acceptance_id,
    ]
    item = batch.items[0]
    assert item.draft_request_id == (
        "mall-draft-request:"
        "23efe10a5a1341e4bb06b49b61c1346bdbe092b3d50427d1b5fb80f1f98d0723"
    )
    assert item.source_group_confirmation_event_id == first.source_event_id
    assert item.source_album_product_id == first.album_product_id
    assert item.source_snapshot_version == 1
    assert item.mall_product_id is None
    assert item.bead_id is None
    assert item.publication_state == "draft"
    assert item.required_business_fields == BUSINESS_FIELDS
    assert (
        item.download_lookup[0].confirmation_event_id
        == first.download_confirmation_event_id
    )
    assert (
        item.download_lookup[0].confirmation_event_id
        != item.source_group_confirmation_event_id
    )
    assert item.download_lookup[0].asset_id == first.images[0].asset_id
    serialized = batch.model_dump_json()
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "download_url" not in serialized


def test_batch_identity_is_independent_of_input_order_and_generation_time() -> None:
    first = valid_receipt(1)
    second = valid_receipt(2)

    forward = build_mall_draft_batch((first, second), generated_at=NOW)
    reverse = build_mall_draft_batch(
        (second, first),
        generated_at=NOW + timedelta(minutes=5),
    )

    assert forward.batch_id == reverse.batch_id
    assert forward.items == reverse.items
    assert forward.generated_at == NOW
    assert reverse.generated_at == NOW + timedelta(minutes=5)


def test_draft_request_identity_hashes_the_complete_max_length_acceptance_id() -> None:
    receipt = valid_receipt(1).model_copy(update={"acceptance_id": "a" * 128})

    batch = build_mall_draft_batch((receipt,), generated_at=NOW)

    assert batch.items[0].draft_request_id == (
        "mall-draft-request:"
        "6836cf13bac400e9105071cd6af47084dfacad4e5e302c94bfed24e013afb73e"
    )


def test_accepts_exactly_ten_unique_receipts() -> None:
    batch = build_mall_draft_batch(
        tuple(valid_receipt(index) for index in range(1, 11)),
        generated_at=NOW,
    )

    assert len(batch.items) == 10


@pytest.mark.parametrize(
    ("boundary", "expected_code"),
    (
        ("empty_batch", "invalid_input"),
        ("eleven_items", "invalid_input"),
        ("non_utc_generated_at", "invalid_input"),
        ("mixed_opc", "mixed_opc_batch"),
        ("mixed_classification", "invalid_input"),
        ("unknown_classification", "invalid_input"),
        ("duplicate_acceptance", "duplicate_source_identity"),
        ("duplicate_event", "duplicate_source_identity"),
        ("duplicate_download_confirmation", "duplicate_source_identity"),
        ("duplicate_snapshot", "duplicate_source_identity"),
        ("duplicate_album_other_version", "duplicate_source_identity"),
        ("duplicate_classification", "duplicate_source_identity"),
        ("duplicate_review", "duplicate_source_identity"),
        ("reused_asset_across_products", "duplicate_source_identity"),
        ("reused_evidence_across_products", "duplicate_source_identity"),
        ("reused_hash_across_products", "duplicate_source_identity"),
        ("wrong_status", "receipt_not_draft_ready"),
        ("wrong_claims", "receipt_not_draft_ready"),
        ("non_null_mall_id", "receipt_not_draft_ready"),
        ("non_null_bead_id", "receipt_not_draft_ready"),
        ("auto_publish", "receipt_not_draft_ready"),
        ("wrong_publication_mode", "receipt_not_draft_ready"),
        ("download_set_mismatch", "unsafe_receipt"),
        ("duplicate_asset", "unsafe_receipt"),
        ("duplicate_evidence", "unsafe_receipt"),
        ("duplicate_hash", "unsafe_receipt"),
        ("non_utc_receipt_time", "unsafe_receipt"),
        ("noncanonical_storage_ref", "unsafe_receipt"),
        ("url_in_confirmed_by", "unsafe_receipt"),
    ),
)
def test_rejects_unsafe_batches_with_stable_codes(
    boundary: str,
    expected_code: str,
) -> None:
    first = valid_receipt(1)
    second = valid_receipt(2)
    receipts = (first, second)
    generated_at = NOW

    if boundary == "empty_batch":
        receipts = ()
    elif boundary == "eleven_items":
        receipts = tuple(valid_receipt(index) for index in range(1, 12))
    elif boundary == "non_utc_generated_at":
        generated_at = datetime(2026, 8, 17, 23)  # noqa: DTZ001
    elif boundary == "mixed_opc":
        receipts = (first, valid_receipt(2, opc_id="opc-other"))
    elif boundary == "mixed_classification":
        receipts = (
            first,
            second.model_copy(
                update={
                    "data_classification": (
                        "opc_owned_product_material_no_customer_data"
                    )
                }
            ),
        )
    elif boundary == "unknown_classification":
        receipts = (first.model_copy(update={"data_classification": "private_data"}),)
    elif boundary == "duplicate_acceptance":
        receipts = (
            first,
            second.model_copy(update={"acceptance_id": first.acceptance_id}),
        )
    elif boundary == "duplicate_event":
        receipts = (
            first,
            second.model_copy(update={"source_event_id": first.source_event_id}),
        )
    elif boundary == "duplicate_download_confirmation":
        receipts = (
            first,
            second.model_copy(
                update={
                    "download_confirmation_event_id": (
                        first.download_confirmation_event_id
                    )
                }
            ),
        )
    elif boundary == "duplicate_snapshot":
        receipts = (
            first,
            second.model_copy(
                update={
                    "album_product_id": first.album_product_id,
                    "snapshot_version": first.snapshot_version,
                }
            ),
        )
    elif boundary == "duplicate_album_other_version":
        receipts = (
            first,
            second.model_copy(
                update={
                    "album_product_id": first.album_product_id,
                    "snapshot_version": first.snapshot_version + 1,
                }
            ),
        )
    elif boundary == "duplicate_classification":
        receipts = (
            first,
            second.model_copy(update={"classification_id": first.classification_id}),
        )
    elif boundary == "duplicate_review":
        receipts = (
            first,
            second.model_copy(update={"review_id": first.review_id}),
        )
    elif boundary in {
        "reused_asset_across_products",
        "reused_evidence_across_products",
        "reused_hash_across_products",
    }:
        second_images = list(second.images)
        if boundary == "reused_asset_across_products":
            second_images[0] = second_images[0].model_copy(
                update={"asset_id": first.images[0].asset_id}
            )
            second_downloads = (
                first.images[0].asset_id,
                second.download_verified_asset_ids[1],
            )
            second = second.model_copy(
                update={
                    "images": tuple(second_images),
                    "download_verified_asset_ids": second_downloads,
                }
            )
        elif boundary == "reused_evidence_across_products":
            second_images[0] = second_images[0].model_copy(
                update={
                    "evidence_asset_id": first.images[0].evidence_asset_id,
                    "storage_ref": first.images[0].storage_ref,
                }
            )
            second = second.model_copy(update={"images": tuple(second_images)})
        else:
            second_images[0] = second_images[0].model_copy(
                update={"sha256": first.images[0].sha256}
            )
            second = second.model_copy(update={"images": tuple(second_images)})
        receipts = (first, second)
    elif boundary == "wrong_status":
        receipts = (first.model_copy(update={"acceptance_status": "not_ready"}),)
    elif boundary == "wrong_claims":
        receipts = (
            first.model_copy(
                update={
                    "claims_not_evaluated": (
                        "origin",
                        "authenticity",
                        "quality",
                        "warehouse_acceptance",
                    )
                }
            ),
        )
    elif boundary == "non_null_mall_id":
        receipts = (first.model_copy(update={"mall_product_id": "mall-1"}),)
    elif boundary == "non_null_bead_id":
        receipts = (first.model_copy(update={"bead_id": "bead-1"}),)
    elif boundary == "auto_publish":
        receipts = (first.model_copy(update={"auto_publish": True}),)
    elif boundary == "wrong_publication_mode":
        receipts = (first.model_copy(update={"publication_mode": "published"}),)
    elif boundary == "download_set_mismatch":
        receipts = (
            first.model_copy(
                update={
                    "download_verified_asset_ids": (
                        first.images[0].asset_id,
                        UUID("90000000-0000-4000-8000-000000000001"),
                    )
                }
            ),
        )
    elif boundary in {
        "duplicate_asset",
        "duplicate_evidence",
        "duplicate_hash",
        "noncanonical_storage_ref",
    }:
        images = list(first.images)
        if boundary == "duplicate_asset":
            images[1] = images[1].model_copy(update={"asset_id": images[0].asset_id})
        elif boundary == "duplicate_evidence":
            images[1] = images[1].model_copy(
                update={
                    "evidence_asset_id": images[0].evidence_asset_id,
                    "storage_ref": images[0].storage_ref,
                }
            )
        elif boundary == "duplicate_hash":
            images[1] = images[1].model_copy(update={"sha256": images[0].sha256})
        else:
            images[0] = images[0].model_copy(
                update={
                    "storage_ref": (
                        "evidence/4000000000004000800000000000000b/original"
                    )
                }
            )
        receipts = (first.model_copy(update={"images": tuple(images)}),)
    elif boundary == "non_utc_receipt_time":
        non_utc = datetime(
            2026,
            8,
            18,
            7,
            tzinfo=timezone(timedelta(hours=8)),
        )
        receipts = (first.model_copy(update={"validated_at": non_utc}),)
    elif boundary == "url_in_confirmed_by":
        receipts = (
            first.model_copy(update={"confirmed_by": "https://private.invalid"}),
        )

    with pytest.raises(MallDraftExportError, match=f"^{expected_code}$") as error:
        build_mall_draft_batch(receipts, generated_at=generated_at)

    assert error.value.code == expected_code
