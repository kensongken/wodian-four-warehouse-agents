from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from photo_organizer.catalog.controlled_upload_candidate import (
    ControlledUploadCandidateError,
    build_controlled_upload_candidate,
)
from photo_organizer.catalog.real_sku_pilot import build_real_sku_pilot_readiness
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
)
from photo_organizer.schemas.real_sku_pilot import (
    RealSkuPilotIntakeManifest,
    RealSkuPilotReadinessReceipt,
    ValidatedPilotImage,
)

PRIMARY_ID = UUID("11111111-1111-4111-8111-111111111111")
DETAIL_ID = UUID("22222222-2222-4222-8222-222222222222")
CAPTURED_AT = datetime(2026, 8, 17, 10, tzinfo=UTC)
AUTHORIZED_AT = datetime(2026, 8, 18, 9, tzinfo=UTC)


def _validated_images(
    *,
    primary_media_type: str = "image/jpeg",
    detail_media_type: str = "image/png",
) -> tuple[ValidatedPilotImage, ...]:
    return (
        ValidatedPilotImage(
            local_image_id=PRIMARY_ID,
            role="primary",
            position=1,
            captured_at=CAPTURED_AT,
            human_privacy_reviewed=True,
            sha256="a" * 64,
            byte_size=123,
            media_type=primary_media_type,
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
            media_type=detail_media_type,
            privacy_metadata_status="no_sensitive_metadata_detected",
        ),
    )


def _readiness(
    *,
    primary_media_type: str = "image/jpeg",
    detail_media_type: str = "image/png",
) -> RealSkuPilotReadinessReceipt:
    images = _validated_images(
        primary_media_type=primary_media_type,
        detail_media_type=detail_media_type,
    )
    manifest = RealSkuPilotIntakeManifest.model_validate(
        {
            "schema_version": "opc-real-sku-pilot-intake/1.0",
            "opc_id": "opc-synthetic-pilot",
            "album_product_id": "album-product-synthetic-pilot-001",
            "source_type": "first_sale_bead",
            "target_chain": "sellable_product_material",
            "data_classification": "opc_owned_product_material_no_customer_data",
            "material_owner": "opc",
            "authorization_basis": "opc_self_owned_product_material",
            "authorized_by": "user:33333333-3333-4333-8333-333333333333",
            "authorized_at": AUTHORIZED_AT,
            "privacy_review_confirmed": True,
            "customer_data_present": False,
            "images": [
                {
                    "local_image_id": str(image.local_image_id),
                    "role": image.role,
                    "position": image.position,
                    "captured_at": image.captured_at,
                    "human_privacy_reviewed": True,
                }
                for image in images
            ],
            "claims_not_evaluated": [
                "authenticity",
                "origin",
                "quality",
                "warehouse_acceptance",
            ],
            "mall_product_id": None,
            "bead_id": None,
            "auto_publish": False,
            "shared_market_opt_in": False,
        }
    )
    return build_real_sku_pilot_readiness(manifest, images)


def test_candidate_binds_readiness_and_upload_fields_deterministically() -> None:
    first = build_controlled_upload_candidate(_readiness(), _validated_images())
    second = build_controlled_upload_candidate(
        _readiness(), tuple(reversed(_validated_images()))
    )

    assert first == second
    assert first.candidate_id.startswith("opc-controlled-upload-candidate:")
    assert first.source_readiness_id == _readiness().readiness_id
    assert first.opc_id == "opc-synthetic-pilot"
    assert first.opc_authority_state == "requires_authenticated_revalidation"
    assert first.album_product_id == "album-product-synthetic-pilot-001"
    assert first.target_task_id is None
    assert first.task_binding_state == "not_bound"
    assert first.upload_state == "not_started"
    assert first.next_action == "bind_to_verified_opc_upload_task"
    assert first.mall_product_id is None
    assert first.bead_id is None
    assert first.publication_state == "not_created"
    assert first.auto_publish is False
    assert first.shared_market_opt_in is False
    assert tuple(item.filename for item in first.items) == (
        "11111111-1111-4111-8111-111111111111.jpg",
        "22222222-2222-4222-8222-222222222222.png",
    )
    assert tuple(item.local_asset_id for item in first.items) == (
        PRIMARY_ID,
        DETAIL_ID,
    )
    assert all(item.retry is False for item in first.items)


@pytest.mark.parametrize(
    "field",
    (
        "sha256",
        "byte_size",
        "media_type",
        "local_image_id",
        "role",
        "position",
        "captured_at",
        "human_privacy_reviewed",
        "privacy_metadata_status",
    ),
)
def test_candidate_rejects_any_readiness_image_drift(field: str) -> None:
    images = list(_validated_images())
    replacements: dict[str, object] = {
        "sha256": "c" * 64,
        "byte_size": 124,
        "media_type": "image/heic",
        "local_image_id": UUID("44444444-4444-4444-8444-444444444444"),
        "role": "detail",
        "position": 2,
        "captured_at": datetime(2026, 8, 17, 11, tzinfo=UTC),
        "human_privacy_reviewed": False,
        "privacy_metadata_status": "unchecked",
    }
    images[0] = images[0].model_copy(update={field: replacements[field]})

    with pytest.raises(ControlledUploadCandidateError) as failure:
        build_controlled_upload_candidate(_readiness(), tuple(images))

    assert failure.value.code == "readiness_image_mismatch"


def test_candidate_rejects_heif_until_upload_boundary_supports_it() -> None:
    readiness = _readiness(detail_media_type="image/heif")
    images = _validated_images(detail_media_type="image/heif")

    with pytest.raises(ControlledUploadCandidateError) as failure:
        build_controlled_upload_candidate(readiness, images)

    assert failure.value.code == "unsupported_upload_media_type"


def test_candidate_rejects_stale_source_readiness_identity() -> None:
    forged = _readiness().model_copy(
        update={
            "readiness_id": f"opc-real-sku-pilot-readiness:{'0' * 64}",
        }
    )

    with pytest.raises(ControlledUploadCandidateError) as failure:
        build_controlled_upload_candidate(forged, _validated_images())

    assert failure.value.code == "invalid_readiness"


def test_offline_candidate_does_not_claim_opc_identity_authenticity() -> None:
    source = _readiness()
    manifest = RealSkuPilotIntakeManifest.model_validate(
        {
            **source.model_dump(
                mode="json",
                exclude={
                    "readiness_id",
                    "status",
                    "next_action",
                    "publication_state",
                },
            ),
            "schema_version": "opc-real-sku-pilot-intake/1.0",
            "opc_id": "opc-operator-entered",
            "images": [
                {
                    "local_image_id": str(image.local_image_id),
                    "role": image.role,
                    "position": image.position,
                    "captured_at": image.captured_at,
                    "human_privacy_reviewed": True,
                }
                for image in source.images
            ],
        }
    )
    recomputed = build_real_sku_pilot_readiness(manifest, source.images)

    candidate = build_controlled_upload_candidate(recomputed, source.images)

    assert candidate.opc_id == "opc-operator-entered"
    assert candidate.target_task_id is None
    assert candidate.opc_authority_state == "requires_authenticated_revalidation"
    assert candidate.next_action == "bind_to_verified_opc_upload_task"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ({"material_owner": "customer"}, "unauthorized_material"),
        ({"authorization_basis": "customer_chat"}, "unauthorized_material"),
        ({"privacy_review_confirmed": False}, "privacy_boundary_failed"),
        ({"customer_data_present": True}, "privacy_boundary_failed"),
        ({"images": None}, "invalid_readiness"),
        ({"images": (None,)}, "invalid_readiness"),
    ),
)
def test_candidate_maps_forged_readiness_to_stable_errors(
    mutation: dict[str, object],
    expected: str,
) -> None:
    forged = _readiness().model_copy(update=mutation)

    with pytest.raises(ControlledUploadCandidateError) as failure:
        build_controlled_upload_candidate(forged, _validated_images())

    assert failure.value.code == expected


def test_candidate_model_rejects_semantic_id_drift() -> None:
    candidate = build_controlled_upload_candidate(_readiness(), _validated_images())
    forged = candidate.model_copy(update={"opc_id": "opc-other"})

    with pytest.raises(ValidationError):
        ControlledUploadCandidate.model_validate(forged.model_dump(mode="json"))


def test_candidate_projection_contains_no_private_or_external_fields() -> None:
    candidate = build_controlled_upload_candidate(_readiness(), _validated_images())
    rendered = candidate.model_dump_json()

    for forbidden in (
        "path",
        "download_url",
        "object_key",
        "access_token",
        "refresh_token",
        "image_bytes",
        "exif",
        "customer_name",
        "customer_id",
        "chat",
        "order",
        "address",
        "phone",
        "provider_upload_id",
        "signed_url",
    ):
        assert forbidden not in rendered.casefold()
    assert candidate.customer_data_present is False
