from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")


def normalized_sql(statement: object) -> str:
    return " ".join(
        str(
            statement.compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )


def test_latest_review_query_is_tenant_scoped_and_latest_only() -> None:
    from product_engine.services.album_review_inbox import latest_review_rows_statement

    sql = normalized_sql(latest_review_rows_statement(TENANT_ID))

    assert "products.tenant_id =" in sql
    assert "material_packages.tenant_id =" in sql
    assert "source_images.tenant_id =" in sql
    assert "max(material_packages.version)" in sql.lower()
    assert "material_package_assets.position" in sql
    assert "sales_strategy_inputs" in sql
    assert "max(sales_strategy_inputs.version)" in sql.lower()
    assert "outbox_events" in sql
    assert "sales.strategy.input.updated.v1" in sql
    assert "row_number() over" in sql.lower()
    assert "product_classification_versions" in sql
    assert "max(product_classification_versions.version)" in sql.lower()
    assert "product_classification_versions.tenant_id" in sql
    assert "left outer join product_classification_versions" in sql.lower()
    assert "sales_strategy_inputs_v2" in sql
    assert "warehouse_handoffs" in sql
    assert "sales_strategy_inputs_v2.source_classification_id" in sql


class RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class RowsSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return RowsResult(self.rows)


def review_row(
    classification=None,
    strategy_v2=None,
    handoff=None,
    style_input=None,
    style_handoff=None,
):
    product = SimpleNamespace(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        product_code="BCNHDIY914",
        category="相册候选材质",
        form="项链",
    )
    package = SimpleNamespace(
        version=3,
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    package_asset = SimpleNamespace(position=0)
    image = SimpleNamespace(
        upstream_asset_id=ASSET_ID,
        mime_type="image/jpeg",
        role="main_candidate",
    )
    return (
        product,
        package,
        package_asset,
        image,
        None,
        None,
        classification,
        strategy_v2,
        handoff,
        style_input,
        style_handoff,
    )


@pytest.mark.asyncio
async def test_candidate_fallback_is_not_marked_human_confirmed() -> None:
    from product_engine.services.album_review_inbox import SqlAlbumReviewStore

    candidate = (await SqlAlbumReviewStore(RowsSession([review_row()])).list_latest(TENANT_ID))[0]

    assert candidate.classification_source == "album_candidate"
    assert candidate.classification_version is None
    assert candidate.category == "相册候选材质"
    assert candidate.form == "项链"


@pytest.mark.asyncio
async def test_latest_human_classification_overrides_candidate_display_values() -> None:
    from product_engine.services.album_review_inbox import SqlAlbumReviewStore

    classification = SimpleNamespace(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        version=2,
        material_label="信息不足",
        product_form="earring",
    )
    confirmed = (
        await SqlAlbumReviewStore(RowsSession([review_row(classification)])).list_latest(TENANT_ID)
    )[0]

    assert confirmed.classification_source == "human_confirmed"
    assert confirmed.classification_id == UUID("66666666-6666-4666-8666-666666666666")
    assert confirmed.classification_version == 2
    assert confirmed.category == "信息不足"
    assert confirmed.form == "耳饰"


@pytest.mark.asyncio
async def test_latest_classification_exposes_its_own_sales_handoff_receipt() -> None:
    from product_engine.services.album_review_inbox import SqlAlbumReviewStore

    classification = SimpleNamespace(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        version=2,
        material_label="信息不足",
        product_form="earring",
    )
    strategy_v2 = SimpleNamespace(version=1, status="preview_only")
    handoff = SimpleNamespace(
        id=UUID("77777777-7777-4777-8777-777777777777"),
        status="accepted",
        receiver_receipt_id=UUID("88888888-8888-4888-8888-888888888888"),
        rejection_code=None,
    )

    confirmed = (
        await SqlAlbumReviewStore(
            RowsSession([review_row(classification, strategy_v2, handoff)])
        ).list_latest(TENANT_ID)
    )[0]

    assert confirmed.sales_handoff_id == handoff.id
    assert confirmed.sales_handoff_status == "accepted"
    assert confirmed.sales_receiver_receipt_id == handoff.receiver_receipt_id


@pytest.mark.asyncio
async def test_latest_classification_restores_style_rejection_receipt() -> None:
    from product_engine.services.album_review_inbox import SqlAlbumReviewStore

    classification = SimpleNamespace(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        version=2,
        material_label="信息不足",
        product_form="earring",
    )
    style_input = SimpleNamespace(version=3)
    style_handoff = SimpleNamespace(
        id=UUID("99999999-9999-4999-8999-999999999999"),
        status="rejected",
        decision_receipt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        rejection_code="classification_material_unconfirmed",
    )

    first = (
        await SqlAlbumReviewStore(
            RowsSession([review_row(classification, None, None, style_input, style_handoff)])
        ).list_latest(TENANT_ID)
    )[0]
    reloaded = (
        await SqlAlbumReviewStore(
            RowsSession([review_row(classification, None, None, style_input, style_handoff)])
        ).list_latest(TENANT_ID)
    )[0]

    assert reloaded.style_handoff_id == style_handoff.id
    assert reloaded.style_handoff_status == "rejected"
    assert reloaded.style_decision_receipt_id == first.style_decision_receipt_id
    assert reloaded.style_rejection_code == "classification_material_unconfirmed"
    assert reloaded.style_eligibility_version == 3


@dataclass(frozen=True)
class Source:
    object_key: str = f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main.png"
    sha256: str = "6f252e1d85c9df6308cbb574aa764fcb8d35d477e9cca0adfa6257617f1fffd7"
    mime_type: str = "image/png"


class Repository:
    def __init__(self, source: Source | None = None, *, missing: bool = False) -> None:
        self.source = None if missing else source or Source()
        self.requests: list[tuple[UUID, str, int, UUID]] = []

    async def list_latest(self, tenant_id: UUID):
        from product_engine.services.album_review_inbox import (
            AlbumReviewImage,
            AlbumReviewProduct,
        )

        assert tenant_id == TENANT_ID
        return [
            AlbumReviewProduct(
                product_code="SYNTHETIC-001",
                category="绿松石",
                form="手串",
                package_version=2,
                images=(
                    AlbumReviewImage(
                        asset_id=ASSET_ID,
                        mime_type="image/png",
                        role="main_candidate",
                    ),
                ),
            )
        ]

    async def image_source(
        self, tenant_id: UUID, product_code: str, package_version: int, asset_id: UUID
    ) -> Source | None:
        self.requests.append((tenant_id, product_code, package_version, asset_id))
        return self.source


class Objects:
    def __init__(self, content: bytes = b"synthetic-image") -> None:
        self.content = content
        self.keys: list[str] = []

    async def read(self, object_key: str) -> bytes:
        self.keys.append(object_key)
        return self.content


class MissingObjects:
    async def read(self, object_key: str) -> bytes:
        from product_engine.domain.errors import ObjectValidationFailed

        del object_key
        raise ObjectValidationFailed("object_not_found")


@pytest.mark.asyncio
async def test_lists_safe_review_metadata_without_object_keys() -> None:
    from product_engine.services.album_review_inbox import AlbumReviewInbox

    products = await AlbumReviewInbox(Repository(), Objects()).list_latest(TENANT_ID)

    payload = products[0].model_dump(mode="json")
    assert payload["product_code"] == "SYNTHETIC-001"
    assert payload["package_version"] == 2
    assert payload["images"][0]["asset_id"] == str(ASSET_ID)
    assert "object_key" not in str(payload)
    assert "sha256" not in str(payload)


@pytest.mark.asyncio
async def test_reads_exact_tenant_product_version_asset_and_validates_digest() -> None:
    from product_engine.services.album_review_inbox import AlbumReviewInbox

    objects = Objects()
    inbox = AlbumReviewInbox(Repository(), objects)

    image = await inbox.read_image(TENANT_ID, "SYNTHETIC-001", 2, ASSET_ID)

    assert image.content == b"synthetic-image"
    assert image.mime_type == "image/png"
    assert objects.keys == [f"tenants/{TENANT_ID}/products/SYNTHETIC-001/main.png"]


@pytest.mark.asyncio
async def test_unknown_or_changed_image_fails_closed() -> None:
    from product_engine.services.album_review_inbox import (
        AlbumReviewInbox,
        AlbumReviewInboxFailure,
    )

    with pytest.raises(AlbumReviewInboxFailure, match="album_image_not_found"):
        await AlbumReviewInbox(Repository(missing=True), Objects()).read_image(
            TENANT_ID, "SYNTHETIC-001", 2, ASSET_ID
        )

    with pytest.raises(AlbumReviewInboxFailure, match="album_image_digest_mismatch"):
        await AlbumReviewInbox(Repository(), Objects(b"changed")).read_image(
            TENANT_ID, "SYNTHETIC-001", 2, ASSET_ID
        )

    with pytest.raises(AlbumReviewInboxFailure, match="album_image_not_found"):
        await AlbumReviewInbox(Repository(), MissingObjects()).read_image(
            TENANT_ID, "SYNTHETIC-001", 2, ASSET_ID
        )
