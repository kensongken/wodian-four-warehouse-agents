from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from photo_organizer.handoff.product_image_set import (
    ClassificationCandidateSource,
    ConfirmedClassificationSource,
    HandoffAdapterError,
    OpcExternalTenantBinding,
    PreparedImageHandoff,
    ProductCodeBinding,
    ProductImageRefV1,
    ProductImageSetReadyV1,
    ProductImageSetSource,
    SourceImage,
    build_product_image_set_ready,
    canonical_body_sha256,
    replay_disposition,
)

NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
OPC_ID = "synthetic-opc-001"
TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
CLASSIFICATION_ID = UUID("55555555-5555-4555-8555-555555555555")


def _inputs(**image_overrides: object):
    source = ProductImageSetSource(
        opc_id=OPC_ID,
        album_product_id="album-product-synthetic-001",
        snapshot_version=1,
        source_import_task_id=str(TASK_ID),
        images=(
            SourceImage(
                asset_id=ASSET_ID,
                sha256="a" * 64,
                mime_type="image/png",
                kind="original",
            ),
        ),
    )
    classification = ConfirmedClassificationSource(
        classification_id=CLASSIFICATION_ID,
        opc_id=OPC_ID,
        album_product_id=source.album_product_id,
        input_snapshot_version=1,
        status="confirmed",
        confirmed_level_1="南红",
        confirmed_level_2="手串",
        confirmed_at=NOW,
        candidates=(
            ClassificationCandidateSource(
                level_1="南红", level_2="手串", confidence=0.8
            ),
        ),
    )
    tenant = OpcExternalTenantBinding(
        binding_id=UUID("66666666-6666-4666-8666-666666666666"),
        binding_source="operator_provisioned",
        opc_id=OPC_ID,
        tenant_id=TENANT_ID,
    )
    product = ProductCodeBinding(
        binding_id=UUID("77777777-7777-4777-8777-777777777777"),
        binding_source="operator_provisioned",
        opc_id=OPC_ID,
        album_product_id=source.album_product_id,
        product_code="SYNTHETIC-001",
    )
    image_values = {
        "asset_id": ASSET_ID,
        "object_key": (f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main.png"),
        "role": "main_candidate",
        "quality_confidence": 0.9,
        "storage_status": "ready",
        **image_overrides,
    }
    prepared = (PreparedImageHandoff.model_validate(image_values),)
    return source, classification, tenant, product, prepared


def test_adapter_builds_the_closed_frozen_contract() -> None:
    event = build_product_image_set_ready(*_inputs())

    assert event.model_dump(mode="json") == {
        "schema_version": 1,
        "event_id": str(event.event_id),
        "occurred_at": "2026-08-30T08:00:00Z",
        "tenant_id": str(TENANT_ID),
        "source_task_id": str(TASK_ID),
        "product_code": "SYNTHETIC-001",
        "category": "南红",
        "form": "手串",
        "classification_confidence": 0.8,
        "images": [
            {
                "asset_id": str(ASSET_ID),
                "object_key": (f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main.png"),
                "sha256": "a" * 64,
                "mime_type": "image/png",
                "kind": "original",
                "role": "main_candidate",
                "quality_confidence": 0.9,
            }
        ],
    }
    with pytest.raises(ValidationError):
        event.__class__.model_validate({**event.model_dump(), "extra": True})


def test_tenant_binding_requires_an_auditable_provisioning_source() -> None:
    with pytest.raises(ValidationError):
        OpcExternalTenantBinding.model_validate(
            {
                "binding_id": "66666666-6666-4666-8666-666666666666",
                "opc_id": OPC_ID,
                "tenant_id": str(TENANT_ID),
            }
        )


@pytest.mark.parametrize(
    ("target", "update", "code"),
    (
        (
            "tenant",
            {"binding_source": "self_asserted"},
            "invalid_adapter_input",
        ),
        (
            "product",
            {"binding_source": "self_asserted"},
            "invalid_adapter_input",
        ),
        ("prepared", {"storage_status": "pending"}, "invalid_adapter_input"),
        ("tenant", {"unexpected": True}, "invalid_adapter_input"),
    ),
)
def test_builder_revalidates_even_model_copy_inputs(
    target: str,
    update: dict[str, object],
    code: str,
) -> None:
    source, classification, tenant, product, prepared = _inputs()
    if target == "tenant":
        tenant = tenant.model_copy(update=update)
    elif target == "product":
        product = product.model_copy(update=update)
    else:
        prepared = (prepared[0].model_copy(update=update),)

    with pytest.raises(HandoffAdapterError, match=code):
        build_product_image_set_ready(source, classification, tenant, product, prepared)


def test_output_contract_itself_rejects_cross_tenant_and_duplicate_keys() -> None:
    event = build_product_image_set_ready(*_inputs())
    image = event.images[0]
    with pytest.raises(ValidationError, match="object_key_tenant_mismatch"):
        ProductImageSetReadyV1.model_validate(
            {
                **event.model_dump(mode="python"),
                "images": [
                    image.model_copy(
                        update={
                            "object_key": "tenants/99999999-9999-4999-8999-999999999999/wrong.png"
                        }
                    ).model_dump(mode="python")
                ],
            }
        )
    second = ProductImageRefV1.model_validate(
        image.model_copy(
            update={"asset_id": UUID("88888888-8888-4888-8888-888888888888")}
        ).model_dump(mode="python")
    )
    with pytest.raises(ValidationError, match="duplicate_object_key"):
        ProductImageSetReadyV1.model_validate(
            {**event.model_dump(mode="python"), "images": [image, second]}
        )


def test_builder_rejects_duplicate_object_keys() -> None:
    source, classification, tenant, product, prepared = _inputs()
    second_asset = UUID("88888888-8888-4888-8888-888888888888")
    source = source.model_copy(
        update={
            "images": (
                source.images[0],
                source.images[0].model_copy(update={"asset_id": second_asset}),
            )
        }
    )
    prepared = (
        prepared[0],
        prepared[0].model_copy(update={"asset_id": second_asset}),
    )

    with pytest.raises(HandoffAdapterError, match="duplicate_object_key"):
        build_product_image_set_ready(source, classification, tenant, product, prepared)


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ("tenant_opc", "tenant_binding_mismatch"),
        ("product_opc", "product_binding_mismatch"),
        ("classification_product", "classification_source_mismatch"),
        ("bad_task", "invalid_source_task_id"),
        ("heif", "unsupported_handoff_media"),
        ("wrong_prefix", "object_key_tenant_mismatch"),
        ("missing_image", "prepared_image_set_mismatch"),
    ),
)
def test_adapter_fails_closed_when_provenance_is_not_exact(
    mutation: str,
    code: str,
) -> None:
    source, classification, tenant, product, prepared = _inputs()
    if mutation == "tenant_opc":
        tenant = tenant.model_copy(update={"opc_id": "other-opc"})
    elif mutation == "product_opc":
        product = product.model_copy(update={"opc_id": "other-opc"})
    elif mutation == "classification_product":
        classification = classification.model_copy(
            update={"album_product_id": "another-product"}
        )
    elif mutation == "bad_task":
        source = source.model_copy(update={"source_import_task_id": "legacy-task"})
    elif mutation == "heif":
        source = source.model_copy(
            update={
                "images": (
                    source.images[0].model_copy(update={"mime_type": "image/heif"}),
                )
            }
        )
    elif mutation == "wrong_prefix":
        prepared = (
            prepared[0].model_copy(update={"object_key": "users/wrong/main.png"}),
        )
    elif mutation == "missing_image":
        prepared = ()

    with pytest.raises(HandoffAdapterError, match=code):
        build_product_image_set_ready(source, classification, tenant, product, prepared)


@pytest.mark.parametrize("duplicate", (False, True))
def test_classification_confidence_requires_one_exact_confirmed_candidate(
    duplicate: bool,
) -> None:
    source, classification, tenant, product, prepared = _inputs()
    candidate = classification.candidates[0]
    replacement = (
        candidate if duplicate else candidate.model_copy(update={"level_1": "翡翠"})
    )
    classification = classification.model_copy(
        update={"candidates": (candidate, replacement) if duplicate else (replacement,)}
    )

    with pytest.raises(
        HandoffAdapterError, match="classification_confidence_ambiguous"
    ):
        build_product_image_set_ready(source, classification, tenant, product, prepared)


def test_event_identity_and_canonical_body_are_stable() -> None:
    first = build_product_image_set_ready(*_inputs())
    second = build_product_image_set_ready(*_inputs())
    first_hash = canonical_body_sha256(first)

    assert first.event_id == second.event_id
    assert first_hash == canonical_body_sha256(second)
    assert replay_disposition(first_hash, first_hash) == "replayed"
    assert replay_disposition(None, first_hash) == "created"
    with pytest.raises(HandoffAdapterError, match="event_identity_conflict"):
        replay_disposition("0" * 64, first_hash)


def test_operator_reassociated_batch_reuses_existing_product_code() -> None:
    """A later import may reuse a code only after it is tied to the same product."""
    first_source, first_classification, tenant, product, prepared = _inputs()
    first_event = build_product_image_set_ready(
        first_source, first_classification, tenant, product, prepared
    )

    later_task_id = UUID("99999999-9999-4999-8999-999999999999")
    later_source = first_source.model_copy(
        update={
            "snapshot_version": 2,
            "source_import_task_id": str(later_task_id),
        }
    )
    later_classification = first_classification.model_copy(
        update={
            "classification_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            "input_snapshot_version": 2,
        }
    )
    later_event = build_product_image_set_ready(
        later_source, later_classification, tenant, product, prepared
    )

    assert later_event.product_code == first_event.product_code == "SYNTHETIC-001"
    assert later_event.source_task_id == later_task_id
    assert later_event.event_id != first_event.event_id

    unassociated_batch = later_source.model_copy(
        update={"album_product_id": "album-product-new-unassociated-batch"}
    )
    with pytest.raises(HandoffAdapterError, match="product_binding_mismatch"):
        build_product_image_set_ready(
            unassociated_batch, later_classification, tenant, product, prepared
        )


def test_changed_image_release_requires_new_classification_and_keeps_old_replayable() -> (
    None
):
    """A changed image release gets a new event; the immutable prior release replays."""
    source, classification, tenant, product, prepared = _inputs()
    old_event = build_product_image_set_ready(
        source, classification, tenant, product, prepared
    )
    old_digest = canonical_body_sha256(old_event)

    new_source = source.model_copy(
        update={
            "snapshot_version": 2,
            "images": (source.images[0].model_copy(update={"sha256": "b" * 64}),),
        }
    )
    new_classification = classification.model_copy(
        update={
            "classification_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            "input_snapshot_version": 2,
        }
    )
    new_prepared = (
        prepared[0].model_copy(
            update={
                "object_key": (
                    f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main-v2.png"
                )
            }
        ),
    )
    new_event = build_product_image_set_ready(
        new_source, new_classification, tenant, product, new_prepared
    )

    assert new_event.product_code == old_event.product_code
    assert new_event.images[0].asset_id == old_event.images[0].asset_id
    assert new_event.images[0].sha256 != old_event.images[0].sha256
    assert new_event.images[0].object_key != old_event.images[0].object_key
    assert new_event.event_id != old_event.event_id
    assert (
        replay_disposition(old_digest, canonical_body_sha256(old_event)) == "replayed"
    )
    with pytest.raises(HandoffAdapterError, match="event_identity_conflict"):
        replay_disposition(old_digest, canonical_body_sha256(new_event))


def test_source_contract_rejects_more_than_3000_images() -> None:
    image = SourceImage(
        asset_id=ASSET_ID,
        sha256="a" * 64,
        mime_type="image/png",
        kind="original",
    )
    with pytest.raises(ValidationError):
        ProductImageSetSource(
            opc_id=OPC_ID,
            album_product_id="synthetic-product",
            snapshot_version=1,
            source_import_task_id=str(TASK_ID),
            images=tuple(
                image.model_copy(update={"asset_id": UUID(int=i + 1)})
                for i in range(3001)
            ),
        )
