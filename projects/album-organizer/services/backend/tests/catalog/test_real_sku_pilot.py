from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from photo_organizer.catalog.real_sku_pilot import (
    RealSkuPilotError,
    build_real_sku_pilot_readiness,
)
from photo_organizer.schemas.real_sku_pilot import (
    RealSkuPilotIntakeManifest,
    ValidatedPilotImage,
)

PRIMARY_ID = UUID("11111111-1111-4111-8111-111111111111")
DETAIL_ID = UUID("22222222-2222-4222-8222-222222222222")
AUTHORIZED_BY = "user:33333333-3333-4333-8333-333333333333"
CAPTURED_AT = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
AUTHORIZED_AT = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
CLAIMS = ["authenticity", "origin", "quality", "warehouse_acceptance"]


def _manifest() -> dict[str, object]:
    return {
        "schema_version": "opc-real-sku-pilot-intake/1.0",
        "opc_id": "opc-pilot",
        "album_product_id": "album-product-pilot-001",
        "source_type": "first_sale_bead",
        "target_chain": "sellable_product_material",
        "data_classification": "opc_owned_product_material_no_customer_data",
        "material_owner": "opc",
        "authorization_basis": "opc_self_owned_product_material",
        "authorized_by": AUTHORIZED_BY,
        "authorized_at": AUTHORIZED_AT.isoformat().replace("+00:00", "Z"),
        "privacy_review_confirmed": True,
        "customer_data_present": False,
        "images": [
            {
                "local_image_id": str(PRIMARY_ID),
                "role": "primary",
                "position": 1,
                "captured_at": CAPTURED_AT.isoformat().replace("+00:00", "Z"),
                "human_privacy_reviewed": True,
            },
            {
                "local_image_id": str(DETAIL_ID),
                "role": "detail",
                "position": 2,
                "captured_at": CAPTURED_AT.isoformat().replace("+00:00", "Z"),
                "human_privacy_reviewed": True,
            },
        ],
        "claims_not_evaluated": CLAIMS,
        "mall_product_id": None,
        "bead_id": None,
        "auto_publish": False,
        "shared_market_opt_in": False,
    }


def _manifest_model() -> RealSkuPilotIntakeManifest:
    return RealSkuPilotIntakeManifest.model_validate(_manifest())


def _validated_images() -> tuple[ValidatedPilotImage, ...]:
    return (
        ValidatedPilotImage(
            local_image_id=PRIMARY_ID,
            role="primary",
            position=1,
            captured_at=CAPTURED_AT,
            human_privacy_reviewed=True,
            sha256="a" * 64,
            byte_size=123,
            media_type="image/jpeg",
            privacy_metadata_status="no_sensitive_metadata_detected",
        ),
        ValidatedPilotImage(
            local_image_id=DETAIL_ID,
            role="detail",
            position=2,
            captured_at=CAPTURED_AT,
            human_privacy_reviewed=True,
            sha256="b" * 64,
            byte_size=456,
            media_type="image/png",
            privacy_metadata_status="no_sensitive_metadata_detected",
        ),
    )


def test_manifest_requires_exact_safe_single_sku_contract() -> None:
    manifest = _manifest_model()

    assert manifest.source_type == "first_sale_bead"
    assert manifest.authorized_by == AUTHORIZED_BY
    assert manifest.authorized_at == AUTHORIZED_AT
    assert manifest.mall_product_id is None
    assert manifest.bead_id is None
    assert tuple(image.position for image in manifest.images) == (1, 2)


@pytest.mark.parametrize(
    "mutation",
    (
        "bad_operator",
        "naive_authorized_at",
        "non_utc_authorized_at",
        "naive_captured_at",
        "duplicate_id",
        "position_gap",
        "bad_primary_role",
        "bad_detail_role",
        "extra_field",
    ),
)
def test_manifest_rejects_noncanonical_boundaries(mutation: str) -> None:
    value = deepcopy(_manifest())
    images = value["images"]
    assert isinstance(images, list)
    if mutation == "bad_operator":
        value["authorized_by"] = "operator-name"
    elif mutation == "naive_authorized_at":
        value["authorized_at"] = "2026-08-18T09:00:00"
    elif mutation == "non_utc_authorized_at":
        value["authorized_at"] = "2026-08-18T17:00:00+08:00"
    elif mutation == "naive_captured_at":
        images[0]["captured_at"] = "2026-08-17T10:00:00"  # type: ignore[index]
    elif mutation == "duplicate_id":
        images[1]["local_image_id"] = str(PRIMARY_ID)  # type: ignore[index]
    elif mutation == "position_gap":
        images[1]["position"] = 3  # type: ignore[index]
    elif mutation == "bad_primary_role":
        images[0]["role"] = "detail"  # type: ignore[index]
    elif mutation == "bad_detail_role":
        images[1]["role"] = "primary"  # type: ignore[index]
    else:
        value["extra"] = "forbidden"

    with pytest.raises(ValidationError):
        RealSkuPilotIntakeManifest.model_validate(value)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("privacy_review_confirmed", False),
        ("customer_data_present", True),
        ("mall_product_id", "mall-1"),
        ("bead_id", "bead-1"),
        ("auto_publish", True),
        ("shared_market_opt_in", True),
        ("claims_not_evaluated", ["authenticity"]),
    ),
)
def test_manifest_rejects_unsafe_business_boundaries(field: str, value: object) -> None:
    manifest = _manifest()
    manifest[field] = value

    with pytest.raises(ValidationError):
        RealSkuPilotIntakeManifest.model_validate(manifest)


def test_builder_binds_manifest_and_images_deterministically() -> None:
    first = build_real_sku_pilot_readiness(_manifest_model(), _validated_images())
    second = build_real_sku_pilot_readiness(
        _manifest_model(), tuple(reversed(_validated_images()))
    )

    assert first == second
    assert first.readiness_id.startswith("opc-real-sku-pilot-readiness:")
    assert first.status == "eligible_for_controlled_import"
    assert first.next_action == "upload_to_controlled_ingestion"
    assert first.mall_product_id is None
    assert first.bead_id is None
    assert first.publication_state == "not_created"
    assert first.auto_publish is False
    assert first.shared_market_opt_in is False
    assert tuple(image.local_image_id for image in first.images) == (
        PRIMARY_ID,
        DETAIL_ID,
    )


@pytest.mark.parametrize("source_type", ("legacy_material", "redesign_output"))
def test_unsupported_source_context_is_stable(source_type: str) -> None:
    manifest = _manifest_model().model_copy(update={"source_type": source_type})

    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(manifest, _validated_images())

    assert failure.value.code == "unsupported_source_context"
    assert str(failure.value) == "unsupported_source_context"


@pytest.mark.parametrize(
    "mutation",
    ("unknown_id", "wrong_role", "wrong_position", "wrong_captured_at", "duplicate"),
)
def test_builder_rejects_image_set_mismatch(mutation: str) -> None:
    images = list(_validated_images())
    first = images[0]
    if mutation == "unknown_id":
        images[0] = first.model_copy(
            update={"local_image_id": UUID("44444444-4444-4444-8444-444444444444")}
        )
    elif mutation == "wrong_role":
        images[0] = first.model_copy(update={"role": "detail"})
    elif mutation == "wrong_position":
        images[0] = first.model_copy(update={"position": 2})
    elif mutation == "wrong_captured_at":
        images[0] = first.model_copy(
            update={"captured_at": datetime(2026, 8, 16, tzinfo=UTC)}
        )
    else:
        images[1] = first

    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(_manifest_model(), tuple(images))

    assert failure.value.code == "image_set_mismatch"


def test_builder_maps_malformed_validated_image_member_to_stable_error() -> None:
    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(
            _manifest_model(),
            (None,),  # type: ignore[arg-type]
        )

    assert failure.value.code == "image_set_mismatch"


def test_builder_revalidates_mutated_manifest_instances() -> None:
    forged = _manifest_model().model_copy(update={"authorized_by": "customer-phone"})

    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(forged, _validated_images())

    assert failure.value.code == "invalid_manifest"


@pytest.mark.parametrize("images", (None, (None,)))
def test_builder_maps_malformed_image_containers_to_stable_error(
    images: object,
) -> None:
    forged = _manifest_model().model_copy(update={"images": images})

    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(forged, _validated_images())

    assert failure.value.code == "invalid_manifest"


@pytest.mark.parametrize(
    "mutation",
    (
        {"material_owner": "customer"},
        {"authorization_basis": "customer_chat"},
    ),
)
def test_builder_maps_authorization_boundary_to_stable_error(
    mutation: dict[str, object],
) -> None:
    forged = _manifest_model().model_copy(update=mutation)

    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(forged, _validated_images())

    assert failure.value.code == "unauthorized_material"


@pytest.mark.parametrize(
    "mutation",
    (
        {"privacy_review_confirmed": False},
        {"customer_data_present": True},
    ),
)
def test_builder_maps_privacy_boundary_to_stable_error(
    mutation: dict[str, object],
) -> None:
    forged = _manifest_model().model_copy(update=mutation)

    with pytest.raises(RealSkuPilotError) as failure:
        build_real_sku_pilot_readiness(forged, _validated_images())

    assert failure.value.code == "privacy_boundary_failed"


def test_receipt_projection_contains_no_private_or_operational_fields() -> None:
    receipt = build_real_sku_pilot_readiness(_manifest_model(), _validated_images())
    rendered = receipt.model_dump_json()

    for forbidden in (
        "path",
        "filename",
        "url",
        "token",
        "object_key",
        "customer_name",
        "customer_phone",
        "order",
        "chat",
        "address",
        "image_bytes",
        "exif",
    ):
        assert forbidden not in rendered.casefold()
    assert receipt.customer_data_present is False
