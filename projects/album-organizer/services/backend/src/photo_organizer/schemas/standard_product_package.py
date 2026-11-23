from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from photo_organizer.categorization.contracts import (
    FrozenModel,
    ProductSourceType,
    StableId,
)
from photo_organizer.schemas.acceptance_receipt import (
    MaterialDataClassification,
    UnevaluatedClaims,
)
from photo_organizer.schemas.mall_draft_export import (
    BusinessFieldRequirements,
    MallDraftClassification,
)

WorkbookPackagePath = Annotated[
    str,
    Field(
        min_length=37,
        max_length=37,
        pattern=r"^export-[0-9a-f]{16}/products\.xlsx$",
    ),
]
ImagePackagePath = Annotated[
    str,
    Field(
        min_length=60,
        max_length=63,
        pattern=(
            r"^export-[0-9a-f]{16}/images/album-[0-9a-f]{16}/"
            r"(?:main|0[2-4])\.(?:jpg|png|heic|heif)$"
        ),
    ),
]
Sha256Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BatchId = Annotated[
    str,
    Field(pattern=r"^mall-draft-batch:[0-9a-f]{64}$"),
]
PackageId = Annotated[
    str,
    Field(pattern=r"^standard-product-package:[0-9a-f]{64}$"),
]
ImageMediaType = Literal[
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
]


class PackageFileDigest(FrozenModel):
    package_path: WorkbookPackagePath
    sha256: Sha256Digest
    byte_size: Annotated[int, Field(gt=0)]
    media_type: Literal[
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ]


class PackagedImageEvidence(FrozenModel):
    package_path: ImagePackagePath
    asset_id: UUID
    evidence_asset_id: UUID
    role: Literal["original"]
    sha256: Sha256Digest
    byte_size: Annotated[int, Field(gt=0, le=104_857_600)]
    media_type: ImageMediaType
    captured_at: datetime | None
    imported_at: datetime
    storage_ref: Annotated[
        str,
        Field(
            min_length=54,
            max_length=54,
            pattern=(
                r"^evidence/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/original$"
            ),
        ),
    ]
    download_confirmation_event_id: UUID

    @field_validator("captured_at", "imported_at")
    @classmethod
    def timestamps_must_be_utc(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("package image timestamps must be UTC")
        return value

    @model_validator(mode="after")
    def storage_reference_must_match_evidence_identity(self) -> Self:
        if self.storage_ref != f"evidence/{self.evidence_asset_id}/original":
            raise ValueError("storage reference must match evidence identity")
        return self


class StandardPackageProduct(FrozenModel):
    draft_request_id: StableId
    source_acceptance_id: StableId
    source_group_confirmation_event_id: UUID
    source_album_product_id: StableId
    source_snapshot_version: Annotated[int, Field(ge=1)]
    source_type: ProductSourceType
    classification: MallDraftClassification
    images: Annotated[
        tuple[PackagedImageEvidence, ...],
        Field(min_length=2, max_length=4),
    ]
    required_business_fields: BusinessFieldRequirements
    claims_not_evaluated: UnevaluatedClaims
    mall_product_id: None
    bead_id: None
    publication_state: Literal["draft"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]

    @model_validator(mode="after")
    def source_identities_must_be_derived(self) -> Self:
        expected_acceptance_id = (
            f"sku-material-acceptance:{self.source_group_confirmation_event_id}"
        )
        expected_draft_request_id = (
            f"mall-draft-request:{sha256(expected_acceptance_id.encode()).hexdigest()}"
        )
        if (
            self.source_acceptance_id != expected_acceptance_id
            or self.draft_request_id != expected_draft_request_id
        ):
            raise ValueError("package source identities must be derived")
        return self


class StandardProductPackageManifest(FrozenModel):
    schema_version: Literal["opc-standard-product-package/1.0"]
    package_id: PackageId
    source_batch_id: BatchId
    opc_id: StableId
    data_classification: MaterialDataClassification
    root_directory: Annotated[
        str,
        Field(pattern=r"^export-[0-9a-f]{16}$"),
    ]
    operation: Literal["create_draft_only"]
    existing_product_policy: Literal["emit_change_proposal_do_not_overwrite"]
    auto_publish: Literal[False]
    shared_market_opt_in: Literal[False]
    products_xlsx: PackageFileDigest
    items: Annotated[
        tuple[StandardPackageProduct, ...],
        Field(min_length=1, max_length=10),
    ]

    @model_validator(mode="after")
    def immutable_identities_and_paths_must_match(self) -> Self:
        batch_digest = self.source_batch_id.removeprefix("mall-draft-batch:")
        expected_root = f"export-{batch_digest[:16]}"
        expected_package_id = (
            "standard-product-package:"
            + sha256(
                (f"opc-standard-product-package/1.0\n{self.source_batch_id}").encode()
            ).hexdigest()
        )
        ordered = tuple(
            sorted(
                self.items,
                key=lambda item: (
                    item.source_album_product_id,
                    item.source_snapshot_version,
                    item.draft_request_id,
                ),
            )
        )
        if (
            self.root_directory != expected_root
            or self.package_id != expected_package_id
            or self.products_xlsx.package_path != f"{expected_root}/products.xlsx"
            or self.items != ordered
        ):
            raise ValueError("package identities and paths must match")
        identity_sets = (
            {item.draft_request_id for item in self.items},
            {item.source_acceptance_id for item in self.items},
            {item.source_group_confirmation_event_id for item in self.items},
            {item.source_album_product_id for item in self.items},
            {item.classification.classification_id for item in self.items},
            {item.classification.review_id for item in self.items},
            {
                (item.source_album_product_id, item.source_snapshot_version)
                for item in self.items
            },
        )
        images = tuple(image for item in self.items for image in item.images)
        if any(len(values) != len(self.items) for values in identity_sets) or (
            len({image.asset_id for image in images}) != len(images)
            or len({image.evidence_asset_id for image in images}) != len(images)
            or len({image.sha256 for image in images}) != len(images)
        ):
            raise ValueError("package source identities must be unique")
        extensions = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/heic": "heic",
            "image/heif": "heif",
        }
        for item in self.items:
            album_digest = sha256(item.source_album_product_id.encode()).hexdigest()[
                :16
            ]
            directory = f"{expected_root}/images/album-{album_digest}"
            if (
                len({image.download_confirmation_event_id for image in item.images})
                != 1
            ):
                raise ValueError("one product requires one download confirmation")
            for position, image in enumerate(item.images, start=1):
                stem = "main" if position == 1 else f"{position:02d}"
                expected_path = f"{directory}/{stem}.{extensions[image.media_type]}"
                if image.package_path != expected_path:
                    raise ValueError("package image path must match source identity")
        return self
