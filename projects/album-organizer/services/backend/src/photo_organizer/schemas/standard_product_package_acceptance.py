from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from photo_organizer.categorization.contracts import (
    FrozenModel,
    ProductSourceType,
    StableId,
)
from photo_organizer.schemas.acceptance_receipt import (
    MaterialDataClassification,
    UnevaluatedClaims,
)
from photo_organizer.schemas.standard_product_package import (
    BatchId,
    PackageId,
    Sha256Digest,
)


class AcceptedPackageProductSource(FrozenModel):
    album_product_id: StableId
    snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    confirmation_event_id: UUID


class StandardProductPackageAcceptanceReceipt(FrozenModel):
    schema_version: Literal["opc-standard-product-package-acceptance/1.0"]
    acceptance_id: Annotated[
        str,
        Field(pattern=r"^standard-package-acceptance:[0-9a-f]{64}$"),
    ]
    acceptance_status: Literal["package_ready_for_mall_draft_import_not_listed"]
    package_sha256: Sha256Digest
    package_byte_size: Annotated[int, Field(gt=0, le=4_200_000_000)]
    package_id: PackageId
    source_batch_id: BatchId
    opc_id: StableId
    data_classification: MaterialDataClassification
    product_count: Annotated[int, Field(ge=1, le=10)]
    image_count: Annotated[int, Field(ge=2, le=40)]
    album_product_ids: Annotated[
        tuple[StableId, ...],
        Field(min_length=1, max_length=10),
    ]
    product_sources: Annotated[
        tuple[AcceptedPackageProductSource, ...],
        Field(min_length=1, max_length=10),
    ]
    material_authorization: Literal["opc_human_confirmed_for_draft_material_use"]
    operation: Literal["validate_only"]
    publication_state: Literal["draft"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]
    mall_product_ids: tuple[()]
    bead_ids: tuple[()]
    claims_not_evaluated: UnevaluatedClaims

    @model_validator(mode="after")
    def identities_and_counts_must_be_derived(self) -> Self:
        expected_acceptance_id = f"standard-package-acceptance:{self.package_sha256}"
        expected_package_id = (
            "standard-product-package:"
            + sha256(
                (f"opc-standard-product-package/1.0\n{self.source_batch_id}").encode()
            ).hexdigest()
        )
        album_ids = tuple(sorted(self.album_product_ids))
        if (
            self.acceptance_id != expected_acceptance_id
            or self.package_id != expected_package_id
            or self.album_product_ids != album_ids
            or len(set(album_ids)) != len(album_ids)
            or self.product_count != len(album_ids)
            or tuple(source.album_product_id for source in self.product_sources)
            != album_ids
            or len({source.confirmation_event_id for source in self.product_sources})
            != len(album_ids)
            or not 2 * self.product_count <= self.image_count <= 4 * self.product_count
        ):
            raise ValueError("acceptance identities and counts must match")
        return self
