from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from product_engine.repositories.sales_handoffs_v3 import (
    SalesV3Asset,
    SalesV3Source,
)
from product_engine.services.sales_strategy_v3 import SalesPreviewPreparerV3

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PRODUCT_ID = UUID("22222222-2222-4222-8222-222222222222")
CLASSIFICATION_ID = UUID("33333333-3333-4333-8333-333333333333")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
SOURCE_IMAGE_ID = UUID("55555555-5555-4555-8555-555555555555")


def source(**updates: object) -> SalesV3Source:
    value = SalesV3Source(
        classification_id=CLASSIFICATION_ID,
        tenant_id=TENANT_ID,
        product_id=PRODUCT_ID,
        product_code="BCNHDIY914",
        version=2,
        is_latest=True,
        base_material_package_version=1,
        material_label="南红",
        claim_basis="merchant_declared",
        evidence_active=True,
        product_form="bracelet",
        inventory_status=None,
        visual_observations=(),
        selling_point_candidates=(),
        price_status="pending",
        price_candidate=None,
        service_rule_summaries=(),
        content_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        assets=(
            SalesV3Asset(
                asset_id=ASSET_ID,
                source_image_id=SOURCE_IMAGE_ID,
                position=0,
                role="main_candidate",
                sha256="c" * 64,
                mime_type="image/jpeg",
            ),
        ),
        created_at=datetime(2026, 9, 21, 8, tzinfo=UTC),
    )
    return replace(value, **updates)


class FakeRepository:
    async def append_preview(self, candidate):
        return candidate


@pytest.mark.parametrize(
    ("basis", "warning"),
    [
        ("document_supported", "凭证支持，具体以凭证为准"),
        ("merchant_declared", "商家声明，非鉴定结论"),
        ("information_insufficient", "材质待人工确认"),
    ],
)
async def test_sales_warning_is_fixed_by_claim_basis(
    basis: str,
    warning: str,
) -> None:
    value = source(
        claim_basis=basis,
        material_label="信息不足" if basis == "information_insufficient" else "南红",
        evidence_active=basis != "information_insufficient",
    )
    result = await SalesPreviewPreparerV3(FakeRepository()).prepare_source(value)

    assert result.material.warning_text == warning


async def test_same_label_with_different_basis_changes_sales_digest() -> None:
    service = SalesPreviewPreparerV3(FakeRepository())
    documented = await service.prepare_source(source(claim_basis="document_supported"))
    declared = await service.prepare_source(source(claim_basis="merchant_declared"))

    assert documented.content_sha256 != declared.content_sha256
