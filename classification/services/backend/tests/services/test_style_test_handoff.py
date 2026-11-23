from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from time import sleep
from uuid import UUID

import pytest
from pydantic import ValidationError

from product_engine.services.local_upload_batches import (
    LocalUploadBatchFailure,
    LocalUploadBatchStore,
    PhotoClassificationConfirmationFact,
)
from product_engine.services.photo_usage_classifier import (
    LOCAL_CLASSIFIER_VERSION,
    PhotoUsageCorrectionEvent,
    PhotoUsageDecision,
)
from product_engine.services.style_test_handoff import (
    ConfirmedProductIdentity,
    ConfirmedUploadItem,
    StyleHandoffFailure,
    StyleHandoffIdempotencyRegistry,
    assess_style_handoff_readiness,
    build_style_handoff,
)
from product_engine.services.template_registry import TemplateRecord

PHOTO_ID = UUID("21111111-1111-4111-8111-111111111111")
RECEIPT_ID = UUID("31111111-1111-4111-8111-111111111111")
SECOND_PHOTO_ID = UUID("22222222-2222-4222-8222-222222222222")
SECOND_RECEIPT_ID = UUID("32222222-2222-4222-8222-222222222222")


def automatic_decision() -> PhotoUsageDecision:
    return PhotoUsageDecision(
        category="real_photo",
        confidence="high",
        reason_codes=("fixture_auto_classification",),
        classifier_version=LOCAL_CLASSIFIER_VERSION,
    )


def confirmed_item() -> ConfirmedUploadItem:
    decision = automatic_decision()
    return ConfirmedUploadItem(
        photo_id=PHOTO_ID,
        content_sha256="a" * 64,
        mime_type="image/jpeg",
        width=1200,
        height=900,
        automatic_decision=decision,
        human_correction=None,
        final_category="real_photo",
        classifier_version=LOCAL_CLASSIFIER_VERSION,
        confirmation_version=3,
        product_identity=ConfirmedProductIdentity(
            confirmed=True,
            product_id="test-product-001",
            product_category="turquoise",
            product_form="bracelet",
        ),
        is_main_photo=True,
        transform_permission=True,
        conflict_free=True,
        desensitized_test_only=True,
        source="classification_local_page",
        publishable=False,
        production_eligible=False,
        style_service_available=True,
    )


def supporting_real_photo() -> ConfirmedUploadItem:
    return replace(
        confirmed_item(),
        photo_id=SECOND_PHOTO_ID,
        content_sha256="b" * 64,
        is_main_photo=False,
    )


def media_receipts_for(items: tuple[ConfirmedUploadItem, ...]) -> dict[UUID, UUID]:
    known = {PHOTO_ID: RECEIPT_ID, SECOND_PHOTO_ID: SECOND_RECEIPT_ID}
    return {item.photo_id: known[item.photo_id] for item in items}


@pytest.mark.parametrize(
    ("broken_items", "expected_code"),
    [
        (
            lambda: (
                replace(
                    confirmed_item(),
                    product_identity=replace(confirmed_item().product_identity, confirmed=False),
                ),
            ),
            "product_confirmation_required",
        ),
        (
            lambda: (replace(confirmed_item(), final_category=None), supporting_real_photo()),
            "photo_classification_required",
        ),
        (
            lambda: (
                replace(
                    confirmed_item(),
                    automatic_decision=replace(automatic_decision(), category="scene_photo"),
                    final_category="scene_photo",
                ),
            ),
            "real_photo_required",
        ),
        (lambda: (replace(confirmed_item(), is_main_photo=False),), "main_photo_required"),
        (
            lambda: (replace(confirmed_item(), transform_permission=False),),
            "transform_permission_required",
        ),
        (lambda: (replace(confirmed_item(), conflict_free=False),), "photo_conflict_present"),
        (
            lambda: (replace(confirmed_item(), desensitized_test_only=False),),
            "desensitized_test_boundary_required",
        ),
        (
            lambda: (replace(confirmed_item(), style_service_available=False),),
            "style_service_unavailable",
        ),
    ],
    ids=(
        "product-identity",
        "final-category",
        "real-photo",
        "main-photo",
        "transform-permission",
        "conflict-free",
        "test-boundary",
        "consumer-readiness",
    ),
)
def test_each_missing_readiness_requirement_blocks_handoff(
    broken_items,
    expected_code: str,
) -> None:
    items = broken_items()
    receipts = media_receipts_for(items)

    readiness = assess_style_handoff_readiness(items, receipts)

    assert readiness.ready is False
    assert readiness.missing_codes == (expected_code,)
    with pytest.raises(StyleHandoffFailure, match=expected_code):
        build_style_handoff(
            customer_test_id="test-customer-001",
            product_id="test-product-001",
            confirmed_items=items,
            media_receipts=receipts,
        )


def test_handoff_preserves_automatic_decision_and_separate_human_correction() -> None:
    automatic = automatic_decision()
    corrected = PhotoUsageCorrectionEvent(
        automatic_decision=automatic,
        corrected_category="wear_photo",
        reviewer_note="由审核员纠正为佩戴展示。",
    )
    item = replace(
        confirmed_item(),
        automatic_decision=automatic,
        human_correction=corrected,
        final_category="wear_photo",
    )

    handoff = build_style_handoff(
        customer_test_id="test-customer-001",
        product_id="test-product-001",
        confirmed_items=(item, supporting_real_photo()),
        media_receipts={PHOTO_ID: RECEIPT_ID, SECOND_PHOTO_ID: SECOND_RECEIPT_ID},
    )

    assert handoff.photos[0].automatic_category == "real_photo"
    assert handoff.photos[0].confirmed_category == "wear_photo"
    assert handoff.photos[0].human_corrected is True
    assert handoff.classification_version == "classification-v1"
    assert handoff.confirmation_version == 3
    assert handoff.main_photo_id == PHOTO_ID
    assert handoff.style_generation_allowed is True
    assert handoff.photos[0].media_receipt_id == RECEIPT_ID


def test_identical_facts_and_idempotency_key_reuse_the_handoff_id() -> None:
    registry = StyleHandoffIdempotencyRegistry()
    item = confirmed_item()
    first = registry.build(
        customer_test_id="test-customer-001",
        product_id="test-product-001",
        confirmed_items=(item,),
        media_receipts={PHOTO_ID: RECEIPT_ID},
    )
    retried = registry.build(
        customer_test_id="test-customer-001",
        product_id="test-product-001",
        confirmed_items=(item,),
        media_receipts={PHOTO_ID: RECEIPT_ID},
    )

    assert retried.handoff_id == first.handoff_id
    assert retried.idempotency_key == "style-test:test-product-001:v3"


def test_changed_facts_with_the_same_idempotency_key_are_rejected() -> None:
    registry = StyleHandoffIdempotencyRegistry()
    original = confirmed_item()
    registry.build(
        customer_test_id="test-customer-001",
        product_id="test-product-001",
        confirmed_items=(original,),
        media_receipts={PHOTO_ID: RECEIPT_ID},
    )

    with pytest.raises(StyleHandoffFailure, match="handoff_idempotency_conflict"):
        registry.build(
            customer_test_id="test-customer-001",
            product_id="test-product-001",
            confirmed_items=(replace(original, width=1201),),
            media_receipts={PHOTO_ID: RECEIPT_ID},
        )


def test_batch_store_appends_confirmation_facts_without_replacing_automatic_fact(tmp_path) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=1)
    uploaded = store.add_image(
        batch.batch_id,
        0,
        "studio-product.jpg",
        TemplateRecord(
            id=UUID("41111111-1111-4111-8111-111111111111"),
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            template_code="LOCAL-1",
            version=1,
            category="待识别",
            image_type="main",
            object_key="objects/LOCAL-1/source.jpg",
            sha256="a" * 64,
            mime_type="image/jpeg",
            byte_size=12,
        ),
    )
    automatic = automatic_decision()
    first = store.append_photo_classification_fact(
        batch.batch_id,
        uploaded.item_id,
        automatic_decision=automatic,
        human_correction=None,
        final_category="real_photo",
        confirmation_version=1,
    )
    corrected = PhotoUsageCorrectionEvent(
        automatic_decision=automatic,
        corrected_category="wear_photo",
        reviewer_note="审核员纠正。",
    )
    second = store.append_photo_classification_fact(
        batch.batch_id,
        uploaded.item_id,
        automatic_decision=automatic,
        human_correction=corrected,
        final_category="wear_photo",
        confirmation_version=2,
    )
    reloaded = store.get(batch.batch_id)

    assert first.automatic_decision == automatic
    assert second.human_correction == corrected
    assert reloaded is not None
    assert len(reloaded.items[0].photo_classification_facts) == 2
    assert (
        reloaded.items[0].photo_classification_facts[0].automatic_decision.category == "real_photo"
    )
    assert reloaded.items[0].photo_classification_facts[0].final_category == "real_photo"
    assert reloaded.items[0].photo_classification_facts[1].automatic_decision == automatic
    assert reloaded.items[0].photo_classification_facts[1].human_correction == corrected
    assert reloaded.items[0].photo_classification_facts[1].final_category == "wear_photo"


def test_product_identity_is_bound_to_the_requested_product_id() -> None:
    item = replace(
        confirmed_item(),
        product_identity=ConfirmedProductIdentity(
            confirmed=True,
            product_id="test-product-other",
            product_category="turquoise",
            product_form="bracelet",
        ),
    )

    readiness = assess_style_handoff_readiness(
        (item,), {PHOTO_ID: RECEIPT_ID}, product_id="test-product-001"
    )

    assert readiness.ready is False
    assert readiness.missing_codes == ("product_confirmation_required",)
    with pytest.raises(StyleHandoffFailure, match="product_confirmation_required"):
        build_style_handoff(
            customer_test_id="test-customer-001",
            product_id="test-product-001",
            confirmed_items=(item,),
            media_receipts={PHOTO_ID: RECEIPT_ID},
        )


def test_cross_photo_identity_and_confirmation_version_are_readiness_failures() -> None:
    second = replace(
        supporting_real_photo(),
        product_identity=ConfirmedProductIdentity(
            confirmed=True,
            product_id="test-product-other",
            product_category="turquoise",
            product_form="bracelet",
        ),
        confirmation_version=4,
    )
    items = (confirmed_item(), second)

    readiness = assess_style_handoff_readiness(
        items,
        media_receipts_for(items),
        product_id="test-product-001",
    )

    assert readiness.ready is False
    assert readiness.missing_codes == (
        "product_confirmation_required",
        "photo_classification_required",
    )
    with pytest.raises(StyleHandoffFailure, match="product_confirmation_required"):
        build_style_handoff(
            customer_test_id="test-customer-001",
            product_id="test-product-001",
            confirmed_items=items,
            media_receipts=media_receipts_for(items),
        )


@pytest.mark.parametrize(
    "items",
    [
        lambda: (
            confirmed_item(),
            replace(supporting_real_photo(), photo_id=PHOTO_ID),
        ),
        lambda: (
            confirmed_item(),
            replace(supporting_real_photo(), content_sha256="a" * 64),
        ),
    ],
    ids=("duplicate-photo-id", "duplicate-content-sha256"),
)
def test_internal_duplicate_detection_blocks_handoff_without_contract_leak(items) -> None:
    duplicated_items = items()
    receipts = {item.photo_id: RECEIPT_ID for item in duplicated_items}

    readiness = assess_style_handoff_readiness(
        duplicated_items,
        receipts,
        product_id="test-product-001",
    )

    assert readiness.ready is False
    assert readiness.missing_codes == ("photo_conflict_present",)
    with pytest.raises(StyleHandoffFailure, match="^photo_conflict_present$"):
        build_style_handoff(
            customer_test_id="test-customer-001",
            product_id="test-product-001",
            confirmed_items=duplicated_items,
            media_receipts=receipts,
        )


def test_later_confirmation_cannot_replace_the_first_automatic_decision(tmp_path) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=1)
    uploaded = store.add_image(
        batch.batch_id,
        0,
        "studio-product.jpg",
        TemplateRecord(
            id=UUID("41111111-1111-4111-8111-111111111111"),
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            template_code="LOCAL-1",
            version=1,
            category="待识别",
            image_type="main",
            object_key="objects/LOCAL-1/source.jpg",
            sha256="a" * 64,
            mime_type="image/jpeg",
            byte_size=12,
        ),
    )
    store.append_photo_classification_fact(
        batch.batch_id,
        uploaded.item_id,
        automatic_decision=automatic_decision(),
        human_correction=None,
        final_category="real_photo",
        confirmation_version=1,
    )
    invented_automatic = replace(automatic_decision(), category="scene_photo")

    with pytest.raises(LocalUploadBatchFailure, match="automatic_decision_conflict"):
        store.append_photo_classification_fact(
            batch.batch_id,
            uploaded.item_id,
            automatic_decision=invented_automatic,
            human_correction=None,
            final_category="scene_photo",
            confirmation_version=2,
        )


def test_concurrent_confirmation_appends_preserve_both_versions(tmp_path, monkeypatch) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=1)
    uploaded = store.add_image(
        batch.batch_id,
        0,
        "studio-product.jpg",
        TemplateRecord(
            id=UUID("41111111-1111-4111-8111-111111111111"),
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            template_code="LOCAL-1",
            version=1,
            category="待识别",
            image_type="main",
            object_key="objects/LOCAL-1/source.jpg",
            sha256="a" * 64,
            mime_type="image/jpeg",
            byte_size=12,
        ),
    )
    original_write = store._write

    def delayed_write(batches) -> None:
        sleep(0.02)
        original_write(batches)

    monkeypatch.setattr(store, "_write", delayed_write)
    start = Barrier(2)

    def append(version: int) -> int:
        start.wait()
        return store.append_photo_classification_fact(
            batch.batch_id,
            uploaded.item_id,
            automatic_decision=automatic_decision(),
            human_correction=None,
            final_category="real_photo",
            confirmation_version=version,
        ).confirmation_version

    with ThreadPoolExecutor(max_workers=2) as pool:
        returned = tuple(pool.map(append, (1, 2)))
    reloaded = store.get(batch.batch_id)

    assert returned == (1, 2)
    assert reloaded is not None
    assert [fact.confirmation_version for fact in reloaded.items[0].photo_classification_facts] == [
        1,
        2,
    ]


def test_concurrent_conflicting_idempotency_submissions_have_one_winner() -> None:
    registry = StyleHandoffIdempotencyRegistry()
    start = Barrier(2)
    original = confirmed_item()
    changed = replace(original, width=1201)

    def submit(item: ConfirmedUploadItem) -> str:
        start.wait()
        try:
            return str(
                registry.build(
                    customer_test_id="test-customer-001",
                    product_id="test-product-001",
                    confirmed_items=(item,),
                    media_receipts={PHOTO_ID: RECEIPT_ID},
                ).handoff_id
            )
        except StyleHandoffFailure as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(submit, (original, changed)))

    assert outcomes.count("handoff_idempotency_conflict") == 1
    assert len({outcome for outcome in outcomes if outcome != "handoff_idempotency_conflict"}) == 1


def test_concurrent_identical_idempotency_submissions_return_the_same_handoff() -> None:
    registry = StyleHandoffIdempotencyRegistry()
    start = Barrier(2)

    def submit() -> str:
        start.wait()
        return str(
            registry.build(
                customer_test_id="test-customer-001",
                product_id="test-product-001",
                confirmed_items=(confirmed_item(),),
                media_receipts={PHOTO_ID: RECEIPT_ID},
            ).handoff_id
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: submit(), range(2)))

    assert len(set(outcomes)) == 1


@pytest.mark.parametrize("invalid_version", (0, True), ids=("zero", "boolean"))
def test_invalid_confirmation_versions_fail_readiness_before_builder_validation(
    invalid_version: object,
) -> None:
    item = replace(confirmed_item(), confirmation_version=invalid_version)  # type: ignore[arg-type]

    readiness = assess_style_handoff_readiness(
        (item,),
        {PHOTO_ID: RECEIPT_ID},
        product_id="test-product-001",
    )

    assert readiness.ready is False
    assert readiness.missing_codes == ("photo_classification_required",)
    with pytest.raises(StyleHandoffFailure, match="^photo_classification_required$"):
        build_style_handoff(
            customer_test_id="test-customer-001",
            product_id="test-product-001",
            confirmed_items=(item,),
            media_receipts={PHOTO_ID: RECEIPT_ID},
        )


def test_terminal_batch_items_preserve_facts_but_reject_new_classification_facts(tmp_path) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=2)
    uploaded = store.add_image(
        batch.batch_id,
        0,
        "studio-product.jpg",
        TemplateRecord(
            id=UUID("41111111-1111-4111-8111-111111111111"),
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            template_code="LOCAL-1",
            version=1,
            category="待识别",
            image_type="main",
            object_key="objects/LOCAL-1/source.jpg",
            sha256="a" * 64,
            mime_type="image/jpeg",
            byte_size=12,
        ),
    )
    store.append_photo_classification_fact(
        batch.batch_id,
        uploaded.item_id,
        automatic_decision=automatic_decision(),
        human_correction=None,
        final_category="real_photo",
        confirmation_version=1,
    )
    stored = store.get(batch.batch_id)
    assert stored is not None
    duplicate = store.add_duplicate(
        batch.batch_id,
        1,
        "studio-product-copy.jpg",
        "a" * 64,
        "image/jpeg",
        stored.items[0],
    )
    current = store.get(batch.batch_id)
    assert current is not None
    store._replace(
        current.model_copy(
            update={
                "items": [
                    item.model_copy(update={"status": "failed"})
                    if item.item_id == uploaded.item_id
                    else item
                    for item in current.items
                ]
            }
        )
    )
    failed = store.mark_failed(
        batch.batch_id,
        0,
        "studio-product.jpg",
        "a" * 64,
        "decode_failed",
    )

    assert len(duplicate.photo_classification_facts) == 1
    assert len(failed.photo_classification_facts) == 1
    for terminal_item in (duplicate, failed):
        with pytest.raises(LocalUploadBatchFailure, match="item_not_confirmable"):
            store.append_photo_classification_fact(
                batch.batch_id,
                terminal_item.item_id,
                automatic_decision=automatic_decision(),
                human_correction=None,
                final_category="real_photo",
                confirmation_version=2,
            )


@pytest.mark.parametrize("invalid_version", (True, "1", 1.0), ids=("bool", "string", "float"))
def test_photo_fact_model_rejects_confirmation_version_coercion(
    invalid_version: object,
) -> None:
    with pytest.raises(ValidationError, match="confirmation_version"):
        PhotoClassificationConfirmationFact.model_validate(
            {
                "automatic_decision": automatic_decision(),
                "final_category": "real_photo",
                "confirmation_version": invalid_version,
            }
        )


@pytest.mark.parametrize("invalid_version", (True, "1", 1.0), ids=("bool", "string", "float"))
def test_public_append_rejects_confirmation_version_coercion(
    tmp_path, invalid_version: object
) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=1)
    uploaded = store.add_image(
        batch.batch_id,
        0,
        "studio-product.jpg",
        TemplateRecord(
            id=UUID("41111111-1111-4111-8111-111111111111"),
            tenant_id=UUID("00000000-0000-4000-8000-000000000001"),
            template_code="LOCAL-1",
            version=1,
            category="待识别",
            image_type="main",
            object_key="objects/LOCAL-1/source.jpg",
            sha256="a" * 64,
            mime_type="image/jpeg",
            byte_size=12,
        ),
    )

    with pytest.raises(LocalUploadBatchFailure, match="invalid_confirmation_version"):
        store.append_photo_classification_fact(
            batch.batch_id,
            uploaded.item_id,
            automatic_decision=automatic_decision(),
            human_correction=None,
            final_category="real_photo",
            confirmation_version=invalid_version,  # type: ignore[arg-type]
        )
