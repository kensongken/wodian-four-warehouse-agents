"""Persistence contracts for customer-owned product image outputs."""

from collections.abc import Callable, Hashable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.output_rules import (
    validate_job_status_transition,
    validate_product_count,
)
from opc_style.models import (
    AuthenticityCheck,
    Base,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ProductAsset,
    ProductAssetLink,
    ProductAssetPermissionEvent,
)
from opc_style.repositories import ProductImageRepository

CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 8, 30, tzinfo=UTC)


def _asset(customer_id: UUID, product_id: str) -> ProductAsset:
    return ProductAsset(
        id=uuid4(),
        customer_id=customer_id,
        product_id=product_id,
        photo_id=f"photo-{product_id}",
        confirmed=True,
        source_type="synthetic_test",
        quality_flags=[],
        occurred_at=NOW,
        content_sha256="a" * 64,
        mime_type="image/jpeg",
        width=1200,
        height=1200,
    )


def _job(customer_id: UUID, *, output_type: str = "scene") -> ImageOutputJob:
    return ImageOutputJob(
        id=uuid4(),
        customer_id=customer_id,
        output_type=output_type,
        output_ratio="1:1",
        adapter_version="deterministic-local-image-v1",
        status="queued",
        request_metadata={"mode": "synthetic_test_only"},
        idempotency_key=f"job-{uuid4()}",
    )


def _candidate(
    customer_id: UUID, job_id: UUID, *, sequence_number: int = 1
) -> ImageCandidate:
    return ImageCandidate(
        id=uuid4(),
        customer_id=customer_id,
        output_job_id=job_id,
        sequence_number=sequence_number,
        identity="ai_style_generated",
        artifact_path="synthetic/candidate-1.jpg",
        content_sha256="b" * 64,
        mime_type="image/jpeg",
        width=1024,
        height=1024,
        adapter_version="deterministic-local-image-v1",
        artifact_metadata={"mode": "synthetic_test_only"},
    )


def test_scene_requires_exactly_one_distinct_product() -> None:
    product_id = uuid4()

    assert validate_product_count("scene", [product_id]) == (product_id,)
    with pytest.raises(DomainRuleError, match="scene_requires_one_product"):
        validate_product_count("scene", [])
    with pytest.raises(DomainRuleError, match="scene_requires_one_product"):
        validate_product_count("scene", [product_id, uuid4()])


def test_wear_requires_exactly_one_distinct_product() -> None:
    product_id = uuid4()

    assert validate_product_count("wear", [product_id]) == (product_id,)
    with pytest.raises(DomainRuleError, match="wear_requires_one_product"):
        validate_product_count("wear", [])
    with pytest.raises(DomainRuleError, match="wear_requires_one_product"):
        validate_product_count("wear", [product_id, uuid4()])


@pytest.mark.parametrize("count", [2, 3, 4, 5])
def test_multi_product_accepts_two_to_five_distinct_products(count: int) -> None:
    product_ids = [uuid4() for _ in range(count)]

    assert validate_product_count("multi_product", product_ids) == tuple(product_ids)


@pytest.mark.parametrize("product_ids", [[], ["one"], list(range(6)), ["one", "one"]])
def test_multi_product_rejects_invalid_or_duplicate_product_counts(
    product_ids: list[Hashable],
) -> None:
    with pytest.raises(
        DomainRuleError, match="multi_product_requires_2_to_5_distinct_products"
    ):
        validate_product_count("multi_product", product_ids)


def test_job_status_transition_rule_allows_forward_progress_only() -> None:
    validate_job_status_transition("queued", "generating")
    validate_job_status_transition("generating", "machine_check")
    validate_job_status_transition("generating", "cleanup_pending")
    validate_job_status_transition("cleanup_pending", "generation_failed")
    validate_job_status_transition("cleanup_pending", "cleanup_abandoned")
    validate_job_status_transition("machine_check", "awaiting_customer_review")
    validate_job_status_transition("awaiting_customer_review", "customer_approved")

    with pytest.raises(
        DomainRuleError, match="invalid_image_output_job_status_transition"
    ):
        validate_job_status_transition("queued", "customer_approved")


@pytest.mark.parametrize("sequence_number", [0, 4])
def test_candidate_rejects_sequence_numbers_outside_three_slots(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    sequence_number: int,
) -> None:
    customer_factory(CUSTOMER_A_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    session.add(_candidate(CUSTOMER_A_ID, job.id, sequence_number=sequence_number))

    with pytest.raises(IntegrityError):
        session.flush()


def test_candidate_rejects_duplicate_sequence_within_a_job(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    session.add(_candidate(CUSTOMER_A_ID, job.id, sequence_number=1))
    session.flush()
    session.add(_candidate(CUSTOMER_A_ID, job.id, sequence_number=1))

    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("id", UUID("33333333-3333-3333-3333-333333333333")),
        ("customer_id", CUSTOMER_B_ID),
        ("output_type", "multi_product"),
        ("style_card_version_id", UUID("44444444-4444-4444-4444-444444444444")),
        ("scene_style_id", UUID("55555555-5555-5555-5555-555555555555")),
        ("output_ratio", "4:5"),
        ("adapter_version", "changed-adapter"),
        ("request_metadata", {"mode": "changed"}),
        ("idempotency_key", "changed-key"),
        ("created_at", NOW + timedelta(seconds=1)),
    ],
)
def test_job_rejects_orm_provenance_changes(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    attribute: str,
    value: object,
) -> None:
    customer_factory(CUSTOMER_A_ID)
    customer_factory(CUSTOMER_B_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    setattr(job, attribute, value)

    with pytest.raises(DomainRuleError, match="image_output_job_provenance_immutable"):
        session.flush()


def test_job_allows_legal_orm_state_changes_only_through_gateway(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    repository = ProductImageRepository(session)

    transitioned = repository.transition_output_job(
        CUSTOMER_A_ID, job.id, "generating", failure_reason=None
    )
    session.flush()

    assert transitioned is job
    assert job.status == "generating"
    assert job.failure_reason is None
    assert job.updated_at >= job.created_at


def test_job_rejects_invalid_direct_orm_status_jump(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    job.status = "customer_approved"

    with pytest.raises(
        DomainRuleError, match="invalid_image_output_job_status_transition"
    ):
        session.flush()


def test_job_allows_failure_reason_and_updated_at_changes(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    new_updated_at = NOW + timedelta(seconds=1)
    job.failure_reason = "diagnostic-only"
    job.updated_at = new_updated_at

    session.flush()

    assert job.failure_reason == "diagnostic-only"
    assert job.updated_at == new_updated_at


def test_job_rejects_orm_delete(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    session.delete(job)

    with pytest.raises(DomainRuleError, match="image_output_job_provenance_immutable"):
        session.flush()


def test_asset_link_rejects_an_asset_owned_by_another_customer(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    customer_factory(CUSTOMER_B_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    job = _job(CUSTOMER_B_ID)
    session.add_all([asset, job])
    session.flush()
    session.add(
        ProductAssetLink(
            customer_id=CUSTOMER_B_ID,
            product_asset_id=asset.id,
            output_job_id=job.id,
            position=1,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_candidate_rejects_a_job_owned_by_another_customer(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    customer_factory(CUSTOMER_B_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    session.add(_candidate(CUSTOMER_B_ID, job.id))

    with pytest.raises(IntegrityError):
        session.flush()


def test_check_and_review_reject_candidates_owned_by_another_customer(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    customer_factory(CUSTOMER_B_ID)
    job = _job(CUSTOMER_A_ID)
    session.add(job)
    session.flush()
    candidate = _candidate(CUSTOMER_A_ID, job.id)
    session.add(candidate)
    session.flush()
    candidate_id = candidate.id
    session.commit()
    session.add(
        AuthenticityCheck(
            customer_id=CUSTOMER_B_ID,
            image_candidate_id=candidate_id,
            check_type="subject_match",
            passed=True,
            reason="synthetic-pass",
            checker_version="deterministic-local-checker-v1",
            details={},
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()
    assert session.get(ImageCandidate, candidate_id) is not None
    session.add(
        ImageReview(
            customer_id=CUSTOMER_B_ID,
            image_candidate_id=candidate_id,
            decision="approved",
            reason=None,
            idempotency_key=f"review-{uuid4()}",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_permission_event_rejects_an_asset_owned_by_another_customer(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    customer_factory(CUSTOMER_B_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    session.add(asset)
    session.flush()
    session.add(
        ProductAssetPermissionEvent(
            customer_id=CUSTOMER_B_ID,
            product_asset_id=asset.id,
            transform_permission=True,
            occurred_at=NOW,
            provenance="customer_intake",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_permission_events_are_append_only_and_latest_is_customer_scoped(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    session.add(asset)
    session.flush()
    granted = ProductAssetPermissionEvent(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        transform_permission=True,
        occurred_at=NOW,
        provenance="customer_intake",
    )
    withdrawn = ProductAssetPermissionEvent(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        transform_permission=False,
        occurred_at=NOW + timedelta(seconds=1),
        provenance="customer_withdrawal",
    )
    session.add_all([granted, withdrawn])
    session.flush()
    repository = ProductImageRepository(session)

    assert repository.get_latest_asset_permission(CUSTOMER_A_ID, asset.id) is withdrawn
    assert repository.get_latest_asset_permission(CUSTOMER_B_ID, asset.id) is None

    granted.transform_permission = False
    with pytest.raises(
        DomainRuleError, match="product_asset_permission_event_immutable"
    ):
        session.flush()


def test_permission_event_rejects_orm_delete(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    session.add(asset)
    session.flush()
    event = ProductAssetPermissionEvent(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        transform_permission=True,
        occurred_at=NOW,
        provenance="customer_intake",
    )
    session.add(event)
    session.flush()
    session.delete(event)

    with pytest.raises(
        DomainRuleError, match="product_asset_permission_event_immutable"
    ):
        session.flush()


@pytest.mark.parametrize(
    ("model_name", "attribute", "error_code"),
    [
        ("product_asset", "mime_type", "product_asset_immutable"),
        ("product_asset_link", "position", "product_asset_link_immutable"),
        ("candidate", "artifact_path", "image_candidate_immutable"),
        ("check", "reason", "authenticity_check_immutable"),
        ("review", "reason", "image_review_immutable"),
    ],
)
def test_immutable_output_history_rejects_orm_updates(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    model_name: str,
    attribute: str,
    error_code: str,
) -> None:
    customer_factory(CUSTOMER_A_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    job = _job(CUSTOMER_A_ID)
    session.add_all([asset, job])
    session.flush()
    link = ProductAssetLink(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        output_job_id=job.id,
        position=1,
    )
    candidate = _candidate(CUSTOMER_A_ID, job.id)
    session.add_all([link, candidate])
    session.flush()
    check = AuthenticityCheck(
        customer_id=CUSTOMER_A_ID,
        image_candidate_id=candidate.id,
        check_type="subject_match",
        passed=True,
        reason="synthetic-pass",
        checker_version="deterministic-local-checker-v1",
        details={},
    )
    review = ImageReview(
        customer_id=CUSTOMER_A_ID,
        image_candidate_id=candidate.id,
        decision="approved",
        reason=None,
        idempotency_key=f"review-{uuid4()}",
    )
    session.add_all([check, review])
    session.flush()
    target = {
        "product_asset": asset,
        "product_asset_link": link,
        "candidate": candidate,
        "check": check,
        "review": review,
    }[model_name]
    setattr(target, attribute, "mutated" if attribute != "position" else 2)

    with pytest.raises(DomainRuleError, match=error_code):
        session.flush()


@pytest.mark.parametrize(
    ("model_name", "error_code"),
    [
        ("product_asset", "product_asset_immutable"),
        ("product_asset_link", "product_asset_link_immutable"),
        ("candidate", "image_candidate_immutable"),
        ("check", "authenticity_check_immutable"),
        ("review", "image_review_immutable"),
    ],
)
def test_immutable_output_history_rejects_orm_deletes(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    model_name: str,
    error_code: str,
) -> None:
    customer_factory(CUSTOMER_A_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    job = _job(CUSTOMER_A_ID)
    session.add_all([asset, job])
    session.flush()
    link = ProductAssetLink(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        output_job_id=job.id,
        position=1,
    )
    candidate = _candidate(CUSTOMER_A_ID, job.id)
    session.add_all([link, candidate])
    session.flush()
    check = AuthenticityCheck(
        customer_id=CUSTOMER_A_ID,
        image_candidate_id=candidate.id,
        check_type="subject_match",
        passed=True,
        reason="synthetic-pass",
        checker_version="deterministic-local-checker-v1",
        details={},
    )
    review = ImageReview(
        customer_id=CUSTOMER_A_ID,
        image_candidate_id=candidate.id,
        decision="approved",
        reason=None,
        idempotency_key=f"review-{uuid4()}",
    )
    session.add_all([check, review])
    session.flush()
    target = {
        "product_asset": asset,
        "product_asset_link": link,
        "candidate": candidate,
        "check": check,
        "review": review,
    }[model_name]
    session.delete(target)

    with pytest.raises(DomainRuleError, match=error_code):
        session.flush()


def test_raw_sql_guards_protect_originals_and_history(
    migrated_session: Session,
) -> None:
    migrated_session.add_all(
        [
            Customer(id=CUSTOMER_A_ID, display_name="A"),
            Customer(id=CUSTOMER_B_ID, display_name="B"),
        ]
    )
    asset = _asset(CUSTOMER_A_ID, "product-a")
    job = _job(CUSTOMER_A_ID)
    other_customer_job = _job(CUSTOMER_B_ID)
    migrated_session.add_all([asset, job, other_customer_job])
    migrated_session.flush()
    permission_event = ProductAssetPermissionEvent(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        transform_permission=True,
        occurred_at=NOW,
        provenance="customer_intake",
    )
    link = ProductAssetLink(
        customer_id=CUSTOMER_A_ID,
        product_asset_id=asset.id,
        output_job_id=job.id,
        position=1,
    )
    candidate = _candidate(CUSTOMER_A_ID, job.id)
    migrated_session.add_all([permission_event, link, candidate])
    migrated_session.flush()
    check = AuthenticityCheck(
        customer_id=CUSTOMER_A_ID,
        image_candidate_id=candidate.id,
        check_type="subject_match",
        passed=True,
        reason="synthetic-pass",
        checker_version="deterministic-local-checker-v1",
        details={},
    )
    review = ImageReview(
        customer_id=CUSTOMER_A_ID,
        image_candidate_id=candidate.id,
        decision="approved",
        idempotency_key=f"review-{uuid4()}",
    )
    migrated_session.add_all([check, review])
    migrated_session.commit()

    with pytest.raises(IntegrityError):
        migrated_session.execute(
            text(
                "INSERT INTO product_asset_links "
                "(id, customer_id, product_asset_id, output_job_id, position, created_at) "
                "VALUES (:id, :customer_id, :asset_id, :job_id, 1, :created_at)"
            ),
            {
                "id": str(uuid4()),
                "customer_id": str(CUSTOMER_B_ID),
                "asset_id": str(asset.id),
                "job_id": str(other_customer_job.id),
                "created_at": NOW.isoformat(),
            },
        )
    migrated_session.rollback()

    protected_rows = [
        ("product_assets", asset.id, "mime_type", "image/png"),
        (
            "product_asset_permission_events",
            permission_event.id,
            "transform_permission",
            False,
        ),
        ("product_asset_links", link.id, "position", 2),
        ("image_candidates", candidate.id, "artifact_path", "changed.jpg"),
        ("authenticity_checks", check.id, "reason", "changed"),
        ("image_reviews", review.id, "reason", "changed"),
    ]
    for table_name, row_id, column_name, value in protected_rows:
        with pytest.raises(IntegrityError):
            migrated_session.execute(
                text(
                    f"UPDATE {table_name} SET {column_name} = :value WHERE id = :id"
                ),
                {"value": value, "id": str(row_id)},
            )
        migrated_session.rollback()
        with pytest.raises(IntegrityError):
            migrated_session.execute(
                text(f"DELETE FROM {table_name} WHERE id = :id"),
                {"id": str(row_id)},
            )
        migrated_session.rollback()


def test_raw_sql_job_guard_allows_only_legal_state_progress(
    migrated_session: Session,
) -> None:
    migrated_session.add(Customer(id=CUSTOMER_A_ID, display_name="A"))
    job = _job(CUSTOMER_A_ID)
    migrated_session.add(job)
    migrated_session.commit()

    with pytest.raises(IntegrityError, match="image_output_job_provenance_immutable"):
        migrated_session.execute(
            text(
                "UPDATE image_output_jobs SET output_ratio = '4:5' WHERE id = :id"
            ),
            {"id": str(job.id)},
        )
    migrated_session.rollback()

    with pytest.raises(IntegrityError, match="image_output_job_provenance_immutable"):
        migrated_session.execute(
            text(
                "UPDATE image_output_jobs SET artifact_store_namespace = 'hijacked' "
                "WHERE id = :id"
            ),
            {"id": str(job.id)},
        )
    migrated_session.rollback()

    with pytest.raises(
        IntegrityError, match="invalid_image_output_job_status_transition"
    ):
        migrated_session.execute(
            text(
                "UPDATE image_output_jobs SET status = 'customer_approved', "
                "updated_at = :updated_at WHERE id = :id"
            ),
            {"id": str(job.id), "updated_at": (NOW + timedelta(seconds=1)).isoformat()},
        )
    migrated_session.rollback()

    migrated_session.execute(
        text(
            "UPDATE image_output_jobs SET status = 'generating', failure_reason = NULL, "
            "updated_at = :updated_at WHERE id = :id"
        ),
        {"id": str(job.id), "updated_at": (NOW + timedelta(seconds=1)).isoformat()},
    )
    migrated_session.commit()
    assert migrated_session.get(ImageOutputJob, job.id).status == "generating"

    migrated_session.execute(
        text(
            "UPDATE image_output_jobs SET generation_claim_token = :token, "
            "updated_at = :updated_at WHERE id = :id"
        ),
        {
            "id": str(job.id),
            "token": str(uuid4()),
            "updated_at": (NOW + timedelta(seconds=1, microseconds=1)).isoformat(),
        },
    )
    migrated_session.commit()

    migrated_session.execute(
        text(
            "UPDATE image_output_jobs SET status = 'cleanup_pending', "
            "failure_reason = 'image_generation_failed;cleanup_pending:store_busy', "
            "updated_at = :updated_at WHERE id = :id"
        ),
        {"id": str(job.id), "updated_at": (NOW + timedelta(seconds=2)).isoformat()},
    )
    migrated_session.commit()
    assert migrated_session.get(ImageOutputJob, job.id).status == "cleanup_pending"

    migrated_session.execute(
        text(
            "UPDATE image_output_jobs SET status = 'cleanup_abandoned', "
            "failure_reason = 'image_generation_failed;cleanup_abandoned:unknown', "
            "updated_at = :updated_at WHERE id = :id"
        ),
        {"id": str(job.id), "updated_at": (NOW + timedelta(seconds=3)).isoformat()},
    )
    migrated_session.commit()
    assert migrated_session.get(ImageOutputJob, job.id).status == "cleanup_abandoned"

    with pytest.raises(IntegrityError, match="image_output_job_provenance_immutable"):
        migrated_session.execute(
            text("DELETE FROM image_output_jobs WHERE id = :id"),
            {"id": str(job.id)},
        )
    migrated_session.rollback()


def test_alembic_schema_matches_model_tables_and_columns(
    session: Session, migrated_session: Session
) -> None:
    model_inspector = inspect(session.bind)
    migrated_inspector = inspect(migrated_session.bind)
    table_names = {
        "product_assets",
        "product_asset_permission_events",
        "product_asset_links",
        "image_output_jobs",
        "image_candidates",
        "authenticity_checks",
        "image_reviews",
    }

    assert table_names <= set(migrated_inspector.get_table_names())
    for table_name in table_names:
        model_columns = {column["name"] for column in model_inspector.get_columns(table_name)}
        migrated_columns = {
            column["name"] for column in migrated_inspector.get_columns(table_name)
        }
        assert migrated_columns == model_columns

    migration_context = MigrationContext.configure(migrated_session.connection())
    assert compare_metadata(migration_context, Base.metadata) == []


def test_repository_hides_assets_jobs_and_candidates_from_other_customers(
    session: Session, customer_factory: Callable[[UUID], Customer]
) -> None:
    customer_factory(CUSTOMER_A_ID)
    asset = _asset(CUSTOMER_A_ID, "product-a")
    job = _job(CUSTOMER_A_ID)
    session.add_all([asset, job])
    session.flush()
    candidate = _candidate(CUSTOMER_A_ID, job.id)
    session.add(candidate)
    session.flush()
    repository = ProductImageRepository(session)

    assert repository.get_asset(CUSTOMER_A_ID, asset.id) is asset
    assert repository.get_asset(CUSTOMER_B_ID, asset.id) is None
    assert repository.get_output_job(CUSTOMER_A_ID, job.id) is job
    assert repository.get_output_job(CUSTOMER_B_ID, job.id) is None
    assert repository.get_candidate(CUSTOMER_A_ID, candidate.id) is candidate
    assert repository.get_candidate(CUSTOMER_B_ID, candidate.id) is None
