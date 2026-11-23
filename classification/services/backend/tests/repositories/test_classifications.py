from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from product_engine.contracts.classification import (
    ClassificationAssetConfirmation,
    ClassificationConfirmationCommand,
    canonical_classification_sha256,
)
from product_engine.repositories.classifications import (
    ClassificationLedgerFailure,
    ClassificationLedgerRepository,
    ClassificationSourceAsset,
    ClassificationWriteContext,
    classification_by_content_statement,
    classification_by_request_statement,
    latest_classification_statement,
    locked_product_statement,
)

TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PRODUCT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PACKAGE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
MAIN_ASSET_ID = UUID("22222222-2222-4222-8222-222222222222")
GALLERY_ASSET_ID = UUID("33333333-3333-4333-8333-333333333333")


def command() -> ClassificationConfirmationCommand:
    return ClassificationConfirmationCommand(
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        product_code="BCNHDIY914",
        local_batch_id="BATCH-20260920033509-8C21C203",
        base_material_package_version=1,
        material_label="信息不足",
        product_form="earring",
        main_asset_id=MAIN_ASSET_ID,
        visual_observations=[],
        selling_point_candidates=[],
        inventory_status=None,
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=[],
        review_source="local_trainer_review",
        assets=[
            ClassificationAssetConfirmation(
                asset_id=MAIN_ASSET_ID,
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            ),
            ClassificationAssetConfirmation(
                asset_id=GALLERY_ASSET_ID,
                position=1,
                role="gallery",
                sha256="b" * 64,
            ),
        ],
    )


def context() -> ClassificationWriteContext:
    return ClassificationWriteContext(
        product_id=PRODUCT_ID,
        latest_material_package_version=1,
        requested_material_package_id=PACKAGE_ID,
        assets=(
            ClassificationSourceAsset(
                upstream_asset_id=MAIN_ASSET_ID,
                source_image_id=UUID("44444444-4444-4444-8444-444444444444"),
                position=0,
                role="main_candidate",
                sha256="a" * 64,
            ),
            ClassificationSourceAsset(
                upstream_asset_id=GALLERY_ASSET_ID,
                source_image_id=UUID("55555555-5555-4555-8555-555555555555"),
                position=1,
                role="gallery",
                sha256="b" * 64,
            ),
        ),
    )


def compiled(statement) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


def existing_record(value: ClassificationConfirmationCommand):
    return SimpleNamespace(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        request_id=value.request_id,
        product_id=PRODUCT_ID,
        version=1,
        base_material_package_version=1,
        material_label=value.material_label,
        content_sha256=canonical_classification_sha256(value),
    )


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return ScalarResult(self.responses.pop(0))

    def add(self, value) -> None:
        self.added.append(value)

    def add_all(self, values) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        self.flush_count += 1

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def test_repository_queries_are_tenant_and_product_scoped() -> None:
    request_sql = compiled(
        classification_by_request_statement(TENANT_ID, UUID("11111111-1111-4111-8111-111111111111"))
    )
    content_sql = compiled(classification_by_content_statement(TENANT_ID, PRODUCT_ID, "a" * 64))
    latest_sql = compiled(latest_classification_statement(TENANT_ID, PRODUCT_ID))
    lock_sql = compiled(locked_product_statement(TENANT_ID, PRODUCT_ID))

    assert "tenant_id" in request_sql and "request_id" in request_sql
    assert "tenant_id" in content_sql and "product_id" in content_sql
    assert "tenant_id" in latest_sql and "product_id" in latest_sql
    assert "ORDER BY product_classification_versions.version DESC" in latest_sql
    assert "tenant_id" in lock_sql and "products.id" in lock_sql
    assert "FOR UPDATE" in lock_sql


async def test_append_creates_version_and_duplicate_request_reuses_it() -> None:
    value = command()
    create_session = FakeSession([PRODUCT_ID, None, None, None])
    created = await ClassificationLedgerRepository(create_session, TENANT_ID).append(
        value, context(), "local-operator"
    )

    assert created.disposition == "created"
    assert created.receipt.classification_version == 1
    assert created.receipt.downstream_status == "not_sent"
    assert len(create_session.added) == 3
    assert create_session.flush_count == 2

    existing = existing_record(value)
    duplicate_session = FakeSession([PRODUCT_ID, existing])
    duplicate = await ClassificationLedgerRepository(duplicate_session, TENANT_ID).append(
        value, context(), "local-operator"
    )
    assert duplicate.disposition == "duplicate"
    assert duplicate.receipt.classification_id == existing.id


async def test_same_request_with_changed_content_conflicts() -> None:
    value = command()
    changed = value.model_copy(update={"product_form": "necklace"})
    session = FakeSession([PRODUCT_ID, existing_record(value)])

    with pytest.raises(ClassificationLedgerFailure, match="classification_request_conflict"):
        await ClassificationLedgerRepository(session, TENANT_ID).append(
            changed, context(), "local-operator"
        )


async def test_same_content_with_new_request_reuses_existing_version() -> None:
    value = command()
    replay = value.model_copy(update={"request_id": UUID("77777777-7777-4777-8777-777777777777")})
    existing = existing_record(value)
    session = FakeSession([PRODUCT_ID, None, existing])

    result = await ClassificationLedgerRepository(session, TENANT_ID).append(
        replay, context(), "local-operator"
    )

    assert result.disposition == "duplicate"
    assert result.receipt.classification_id == existing.id


async def test_latest_returns_highest_version_for_bound_tenant_product() -> None:
    value = command()
    existing = existing_record(value)
    existing.version = 4
    session = FakeSession([existing])

    result = await ClassificationLedgerRepository(session, TENANT_ID).latest(PRODUCT_ID)

    assert result is not None
    assert result.version == 4
    sql = compiled(session.statements[0])
    assert "tenant_id" in sql and "product_id" in sql
