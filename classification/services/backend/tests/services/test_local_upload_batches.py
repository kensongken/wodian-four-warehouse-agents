from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from product_engine.services.local_upload_batches import (
    InitialRecognitionResult,
    LocalUploadBatchFailure,
    LocalUploadBatchStore,
)
from product_engine.services.photo_usage_classifier import PhotoUsageDecision
from product_engine.services.template_registry import TemplateRecord

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
BINDING = {
    "customer_test_id": "test-customer-local",
    "product_id": "test-product-turquoise-bracelet",
    "product_category": "turquoise",
    "product_form": "bracelet",
}


def template(code: str, sha256: str) -> TemplateRecord:
    return TemplateRecord(
        id=uuid4(),
        tenant_id=TENANT_ID,
        template_code=code,
        version=1,
        category="待识别",
        image_type="main",
        object_key=f"objects/{code}/source.jpg",
        sha256=sha256,
        mime_type="image/jpeg",
        byte_size=12,
    )


def confirmed_bound_batch(
    store: LocalUploadBatchStore,
    code_prefix: str,
    hash_chars: tuple[str, str],
):  # type: ignore[no-untyped-def]
    batch = store.create(2)
    decision = PhotoUsageDecision(
        category="real_photo",
        confidence="high",
        reason_codes=("fixture",),
    )
    items = []
    for position, hash_char in enumerate(hash_chars):
        item = store.add_image(
            batch.batch_id,
            position,
            f"studio-{code_prefix}-{position}.jpg",
            template(f"{code_prefix}-{position}", hash_char * 64),
        )
        store.append_photo_classification_fact(
            batch.batch_id,
            item.item_id,
            automatic_decision=decision,
            human_correction=None,
            final_category="real_photo",
            confirmation_version=1,
        )
        items.append(item)
    version = store.bind_style_test_product(batch.batch_id, **BINDING)
    store.confirm_style_test_batch(
        batch.batch_id,
        **BINDING,
        main_photo_id=items[0].item_id,
        transform_permission=True,
    )
    return batch, items, version


def test_batch_preserves_positions_and_derives_counts(tmp_path) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=3)

    second = store.add_image(batch.batch_id, 1, "第二张.jpg", template("LOCAL-2", "b" * 64))
    first = store.add_image(batch.batch_id, 0, "第一张.jpg", template("LOCAL-1", "a" * 64))
    current = store.get(batch.batch_id)

    assert current is not None
    assert [item.position for item in current.items] == [0, 1]
    assert first.status == "pending_review"
    assert second.status == "pending_review"
    assert current.counts == {
        "expected": 3,
        "uploaded": 2,
        "success": 2,
        "duplicate": 0,
        "failed": 0,
        "pending_review": 2,
        "reviewed": 0,
    }


def test_equal_sha256_is_skipped_and_references_first_item(tmp_path) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")
    batch = store.create(expected_count=2)
    first = store.add_image(batch.batch_id, 0, "正面.jpg", template("LOCAL-1", "a" * 64))

    duplicate = store.add_image(
        batch.batch_id,
        1,
        "正面副本.jpg",
        template("LOCAL-2", "a" * 64),
    )

    assert duplicate.status == "duplicate"
    assert duplicate.duplicate_of_item_id == first.item_id
    assert duplicate.template_code == first.template_code
    assert store.get(batch.batch_id).counts["duplicate"] == 1  # type: ignore[union-attr]


def test_position_retry_is_idempotent_but_changed_digest_conflicts(tmp_path) -> None:
    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    batch = store.create(expected_count=1)
    original = store.add_image(batch.batch_id, 0, "商品.jpg", template("LOCAL-1", "a" * 64))

    retried = store.add_image(batch.batch_id, 0, "商品.jpg", template("LOCAL-X", "a" * 64))

    assert retried.item_id == original.item_id
    before = path.read_bytes()
    with pytest.raises(LocalUploadBatchFailure, match="position_conflict"):
        store.add_image(batch.batch_id, 0, "另一张.jpg", template("LOCAL-2", "b" * 64))
    assert path.read_bytes() == before


def test_batch_survives_fresh_store_instance(tmp_path) -> None:
    path = tmp_path / "upload-batches.json"
    first_store = LocalUploadBatchStore(path)
    batch = first_store.create(expected_count=1)
    first_store.add_image(batch.batch_id, 0, "商品.jpg", template("LOCAL-1", "a" * 64))

    reloaded = LocalUploadBatchStore(path).get(batch.batch_id)

    assert reloaded is not None
    assert reloaded.items[0].filename == "商品.jpg"
    assert reloaded.items[0].initial_result.material_label == "待确认"
    assert reloaded.items[0].initial_result.product_form == "unknown"
    assert reloaded.items[0].initial_result.training_state == "system_initial_model_not_trained"


def test_initial_result_uses_unknown_code_and_migrates_only_legacy_pending_text() -> None:
    assert InitialRecognitionResult().product_form == "unknown"
    assert (
        InitialRecognitionResult.model_validate({"product_form": "待确认"}).product_form
        == "unknown"
    )

    with pytest.raises(ValidationError):
        InitialRecognitionResult.model_validate({"product_form": "客户姓名张三"})


def test_missing_batch_is_rejected(tmp_path) -> None:
    store = LocalUploadBatchStore(tmp_path / "upload-batches.json")

    with pytest.raises(LocalUploadBatchFailure, match="batch_not_found"):
        store.add_image("missing", 0, "商品.jpg", template("LOCAL-1", "a" * 64))


def test_same_product_version_reservations_are_concurrent_unique_and_restart_stable(
    tmp_path,
) -> None:
    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    batches = (store.create(1), store.create(1))
    start = Barrier(2)

    def bind(batch_id: str) -> int:
        start.wait()
        return store.bind_style_test_product(
            batch_id,
            customer_test_id="test-customer-local",
            product_id="test-product-turquoise-bracelet",
            product_category="turquoise",
            product_form="bracelet",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = tuple(pool.map(bind, (batch.batch_id for batch in batches)))

    assert set(versions) == {1, 2}
    restarted = LocalUploadBatchStore(path)
    assert {
        restarted.bind_style_test_product(
            batch.batch_id,
            customer_test_id="test-customer-local",
            product_id="test-product-turquoise-bracelet",
            product_category="turquoise",
            product_form="bracelet",
        )
        for batch in batches
    } == {1, 2}
    abandoned = restarted.create(1)
    assert (
        restarted.bind_style_test_product(
            abandoned.batch_id,
            customer_test_id="test-customer-local",
            product_id="test-product-turquoise-bracelet",
            product_category="turquoise",
            product_form="bracelet",
        )
        == 3
    )


def test_batch_correction_failure_is_atomic_when_an_active_item_has_no_fact(tmp_path) -> None:
    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    batch = store.create(2)
    first = store.add_image(batch.batch_id, 0, "ambient-product.jpg", template("L-1", "a" * 64))
    store.add_image(batch.batch_id, 1, "studio-product.jpg", template("L-2", "b" * 64))
    automatic = PhotoUsageDecision(
        category="scene_photo",
        confidence="low",
        reason_codes=("fixture",),
    )
    store.append_photo_classification_fact(
        batch.batch_id,
        first.item_id,
        automatic_decision=automatic,
        human_correction=None,
        final_category="scene_photo",
        confirmation_version=1,
    )
    before = path.read_bytes()

    with pytest.raises(LocalUploadBatchFailure, match="photo_classification_required"):
        store.apply_photo_classification_correction(
            batch.batch_id,
            first.item_id,
            corrected_category="real_photo",
            reviewer_note="审核确认。",
        )

    assert path.read_bytes() == before


def test_concurrent_product_bind_and_bound_correction_reserve_unique_global_versions(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiting correction must observe the v2 reserved by a concurrent binder."""

    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    automatic = PhotoUsageDecision(
        category="scene_photo",
        confidence="low",
        reason_codes=("fixture",),
    )
    batch_a = store.create(1)
    item_a = store.add_image(batch_a.batch_id, 0, "ambient-a.jpg", template("A", "a" * 64))
    store.append_photo_classification_fact(
        batch_a.batch_id,
        item_a.item_id,
        automatic_decision=automatic,
        human_correction=None,
        final_category="scene_photo",
        confirmation_version=1,
    )
    binding = {
        "customer_test_id": "test-customer-local",
        "product_id": "test-product-turquoise-bracelet",
        "product_category": "turquoise",
        "product_form": "bracelet",
    }
    assert store.bind_style_test_product(batch_a.batch_id, **binding) == 1

    batch_b = store.create(1)
    item_b = store.add_image(batch_b.batch_id, 0, "studio-b.jpg", template("B", "b" * 64))
    store.append_photo_classification_fact(
        batch_b.batch_id,
        item_b.item_id,
        automatic_decision=PhotoUsageDecision(
            category="real_photo",
            confidence="high",
            reason_codes=("fixture",),
        ),
        human_correction=None,
        final_category="real_photo",
        confirmation_version=1,
    )

    original_write = store._write
    binder_inside_write = Event()
    release_binder = Event()
    correction_started = Event()

    def delayed_binding_write(batches) -> None:  # type: ignore[no-untyped-def]
        persisted_b = next(batch for batch in batches if batch.batch_id == batch_b.batch_id)
        if not binder_inside_write.is_set() and any(
            fact.confirmation_version == 2 for fact in persisted_b.style_test_confirmation_versions
        ):
            binder_inside_write.set()
            assert release_binder.wait(timeout=5)
        original_write(batches)

    monkeypatch.setattr(store, "_write", delayed_binding_write)

    def correct_a() -> int:
        correction_started.set()
        return store.apply_photo_classification_correction(
            batch_a.batch_id,
            item_a.item_id,
            corrected_category="real_photo",
            reviewer_note="审核确认。",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        bound = pool.submit(store.bind_style_test_product, batch_b.batch_id, **binding)
        assert binder_inside_write.wait(timeout=5)
        corrected = pool.submit(correct_a)
        assert correction_started.wait(timeout=5)
        release_binder.set()
        assert bound.result(timeout=5) == 2
        assert corrected.result(timeout=5) == 3

    current_a = store.get(batch_a.batch_id)
    current_b = store.get(batch_b.batch_id)
    assert current_a is not None and current_b is not None
    assert [fact.confirmation_version for fact in current_a.style_test_confirmation_versions] == [
        1,
        3,
    ]
    assert [fact.confirmation_version for fact in current_b.style_test_confirmation_versions] == [2]


def test_failed_move_rebase_mutates_neither_batch_nor_consumes_global_version(
    tmp_path,
) -> None:
    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    binding = {
        "customer_test_id": "test-customer-local",
        "product_id": "test-product-turquoise-bracelet",
        "product_category": "turquoise",
        "product_form": "bracelet",
    }
    target = store.create(2)
    assert store.bind_style_test_product(target.batch_id, **binding) == 1
    store.add_image(target.batch_id, 0, "missing-fact.jpg", template("T", "a" * 64))
    reserved = store.create(1)
    assert store.bind_style_test_product(reserved.batch_id, **binding) == 2

    source = store.create(1)
    source_item = store.add_image(source.batch_id, 0, "ambient-source.jpg", template("S", "b" * 64))
    automatic = PhotoUsageDecision(
        category="scene_photo",
        confidence="low",
        reason_codes=("fixture",),
    )
    store.append_photo_classification_fact(
        source.batch_id,
        source_item.item_id,
        automatic_decision=automatic,
        human_correction=None,
        final_category="scene_photo",
        confirmation_version=1,
    )
    assert (
        store.apply_photo_classification_correction(
            source.batch_id,
            source_item.item_id,
            corrected_category="real_photo",
            reviewer_note="审核确认。",
        )
        == 2
    )
    before = path.read_bytes()

    with pytest.raises(LocalUploadBatchFailure, match="photo_classification_required"):
        store.move_item_to_batch(
            source.batch_id,
            source_item.item_id,
            target.batch_id,
            target_customer_test_id="test-customer-local",
            target_product_id="test-product-turquoise-bracelet",
            target_product_category="turquoise",
            target_product_form="bracelet",
        )

    assert path.read_bytes() == before
    next_batch = store.create(1)
    assert store.bind_style_test_product(next_batch.batch_id, **binding) == 3


def test_failed_payload_mutation_writes_nothing_and_consumes_no_product_version(
    tmp_path,
) -> None:
    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    batch, _, version = confirmed_bound_batch(store, "A", ("a", "b"))
    assert version == 1
    before = path.read_bytes()

    with pytest.raises(LocalUploadBatchFailure, match="image_not_found"):
        store.remove_item(batch.batch_id, uuid4())

    assert path.read_bytes() == before
    next_batch = store.create(1)
    assert store.bind_style_test_product(next_batch.batch_id, **BINDING) == 2


def test_concurrent_bound_payload_mutations_allocate_unique_monotonic_versions(
    tmp_path,
) -> None:
    path = tmp_path / "upload-batches.json"
    store = LocalUploadBatchStore(path)
    batch_a, items_a, version_a = confirmed_bound_batch(store, "A", ("a", "b"))
    batch_b, items_b, version_b = confirmed_bound_batch(store, "B", ("c", "d"))
    assert (version_a, version_b) == (1, 2)
    start = Barrier(2)

    def remove_detail(batch_id: str, item_id: UUID) -> int:
        start.wait()
        store.remove_item(batch_id, item_id)
        version = store.batch_confirmation_version(batch_id)
        assert version is not None
        return version

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutations = (
            pool.submit(remove_detail, batch_a.batch_id, items_a[1].item_id),
            pool.submit(remove_detail, batch_b.batch_id, items_b[1].item_id),
        )
        versions = {future.result(timeout=5) for future in mutations}

    assert versions == {3, 4}
    current_batches = [
        store.get(batch_a.batch_id),
        store.get(batch_b.batch_id),
    ]
    assert all(batch is not None for batch in current_batches)
    reservation_versions = {
        fact.confirmation_version
        for batch in current_batches
        if batch is not None
        for fact in batch.style_test_confirmation_versions
    }
    assert reservation_versions == {1, 2, 3, 4}
    for batch in current_batches:
        assert batch is not None
        current_version = store.batch_confirmation_version(batch.batch_id)
        assert batch.items[0].photo_classification_facts[-1].confirmation_version == (
            current_version
        )
        assert batch.style_test_product_confirmation is not None
        assert batch.style_test_permission is not None
        assert batch.style_test_product_confirmation.confirmation_version == current_version
        assert batch.style_test_permission.confirmation_version == current_version
