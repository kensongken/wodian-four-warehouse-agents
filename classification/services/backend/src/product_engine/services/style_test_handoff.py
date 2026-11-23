"""Build local-only, confirmed classification handoff packages.

This is the producer-side safety latch: it translates already-confirmed facts into
the frozen bridge contract, but never grants publication or production authority.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from threading import RLock
from typing import Literal
from uuid import UUID, uuid5

from product_engine.contracts.style_test_bridge import (
    ClassificationStyleHandoffV1,
    ConfirmedPhotoV1,
    PhotoCategory,
    ProductCategory,
    ProductForm,
    canonical_handoff_bytes,
)
from product_engine.services.photo_usage_classifier import (
    LOCAL_CLASSIFIER_VERSION,
    PhotoUsageCorrectionEvent,
    PhotoUsageDecision,
)

_HANDOFF_NAMESPACE = UUID("7946f586-862a-4c32-a390-9cb533a14435")
_ZERO_HANDOFF_ID = UUID("00000000-0000-0000-0000-000000000000")
CLASSIFICATION_VERSION: Literal["classification-v1"] = "classification-v1"

_READINESS_CODES = (
    "product_confirmation_required",
    "photo_classification_required",
    "real_photo_required",
    "main_photo_required",
    "transform_permission_required",
    "photo_conflict_present",
    "desensitized_test_boundary_required",
    "style_service_unavailable",
)


class StyleHandoffFailure(ValueError):
    pass


@dataclass(frozen=True)
class StyleHandoffReadiness:
    ready: bool
    missing_codes: tuple[str, ...]


@dataclass(frozen=True)
class ConfirmedProductIdentity:
    """The operator-confirmed product facts required for every photo in a package."""

    confirmed: bool
    product_id: str
    product_category: ProductCategory | None
    product_form: ProductForm | None


@dataclass(frozen=True)
class ConfirmedUploadItem:
    """Immutable local confirmation facts for one uploaded photo.

    ``automatic_decision`` is always retained even when a reviewer supplied the
    optional correction.  ``final_category`` is the only category transmitted.
    """

    photo_id: UUID
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png", "image/heic"]
    width: int
    height: int
    automatic_decision: PhotoUsageDecision
    human_correction: PhotoUsageCorrectionEvent | None
    final_category: PhotoCategory | None
    classifier_version: str
    confirmation_version: int
    product_identity: ConfirmedProductIdentity
    is_main_photo: bool
    transform_permission: bool
    conflict_free: bool
    desensitized_test_only: bool
    source: str
    publishable: bool
    production_eligible: bool
    style_service_available: bool


def assess_style_handoff_readiness(
    confirmed_items: Sequence[ConfirmedUploadItem],
    media_receipts: Mapping[UUID, UUID],
    *,
    product_id: str | None = None,
) -> StyleHandoffReadiness:
    """Return every unmet local-test gate in stable, user-actionable order."""

    items = tuple(confirmed_items)
    missing: list[str] = []
    identity = items[0].product_identity if items else None
    if (
        identity is None
        or not identity.confirmed
        or identity.product_category is None
        or identity.product_form is None
        or (product_id is not None and identity.product_id != product_id)
        or any(
            item.product_identity != identity
            or not item.product_identity.confirmed
            or item.product_identity.product_category is None
            or item.product_identity.product_form is None
            or (product_id is not None and item.product_identity.product_id != product_id)
            for item in items
        )
    ):
        missing.append("product_confirmation_required")
    if (
        not items
        or any(not _has_valid_final_classification(item) for item in items)
        or any(not _is_positive_confirmation_version(item.confirmation_version) for item in items)
        or len({item.confirmation_version for item in items}) != 1
    ):
        missing.append("photo_classification_required")
    if not any(item.final_category == "real_photo" for item in items):
        missing.append("real_photo_required")
    if sum(item.is_main_photo for item in items) != 1:
        missing.append("main_photo_required")
    if not items or any(item.transform_permission is not True for item in items):
        missing.append("transform_permission_required")
    if _has_photo_conflict(items):
        missing.append("photo_conflict_present")
    if not items or any(not _has_test_only_boundary(item) for item in items):
        missing.append("desensitized_test_boundary_required")
    if not items or any(
        item.style_service_available is not True or item.photo_id not in media_receipts
        for item in items
    ):
        missing.append("style_service_unavailable")
    return StyleHandoffReadiness(ready=not missing, missing_codes=tuple(missing))


def _has_valid_final_classification(item: ConfirmedUploadItem) -> bool:
    if item.final_category is None or item.classifier_version != LOCAL_CLASSIFIER_VERSION:
        return False
    if item.human_correction is None:
        return item.final_category == item.automatic_decision.category
    return (
        item.human_correction.automatic_decision == item.automatic_decision
        and item.final_category == item.human_correction.corrected_category
    )


def _has_photo_conflict(items: Sequence[ConfirmedUploadItem]) -> bool:
    photo_ids = [item.photo_id for item in items]
    content_sha256s = [item.content_sha256 for item in items]
    return (
        any(item.conflict_free is not True for item in items)
        or len(photo_ids) != len(set(photo_ids))
        or len(content_sha256s) != len(set(content_sha256s))
    )


def _is_positive_confirmation_version(value: object) -> bool:
    return type(value) is int and value > 0


def _has_test_only_boundary(item: ConfirmedUploadItem) -> bool:
    return (
        item.desensitized_test_only is True
        and item.source == "classification_local_page"
        and item.publishable is False
        and item.production_eligible is False
    )


def build_style_handoff(
    *,
    customer_test_id: str,
    product_id: str,
    confirmed_items: Sequence[ConfirmedUploadItem],
    media_receipts: Mapping[UUID, UUID],
) -> ClassificationStyleHandoffV1:
    """Build one canonical package, or fail closed without returning a partial one."""

    readiness = assess_style_handoff_readiness(
        confirmed_items,
        media_receipts,
        product_id=product_id,
    )
    if not readiness.ready:
        raise StyleHandoffFailure(",".join(readiness.missing_codes))
    items = tuple(sorted(confirmed_items, key=lambda item: str(item.photo_id)))
    product_identity = items[0].product_identity
    if any(item.product_identity != product_identity for item in items):
        raise StyleHandoffFailure("product_confirmation_required")
    if (
        not product_identity.confirmed
        or product_identity.product_id != product_id
        or product_identity.product_category is None
        or product_identity.product_form is None
    ):
        raise StyleHandoffFailure("product_confirmation_required")
    confirmation_version = items[0].confirmation_version
    if not _is_positive_confirmation_version(confirmation_version) or any(
        item.confirmation_version != confirmation_version for item in items
    ):
        raise StyleHandoffFailure("photo_classification_required")
    photos_list: list[ConfirmedPhotoV1] = []
    for item in items:
        if item.final_category is None:
            raise StyleHandoffFailure("photo_classification_required")
        photos_list.append(
            ConfirmedPhotoV1(
                photo_id=item.photo_id,
                content_sha256=item.content_sha256,
                mime_type=item.mime_type,
                width=item.width,
                height=item.height,
                automatic_category=item.automatic_decision.category,
                confidence=item.automatic_decision.confidence,
                human_corrected=item.human_correction is not None,
                confirmed_category=item.final_category,
                media_receipt_id=media_receipts[item.photo_id],
            )
        )
    photos = tuple(photos_list)
    main_photo_id = next(item.photo_id for item in items if item.is_main_photo)
    provisional = ClassificationStyleHandoffV1(
        schema_version="classification-style-handoff-v1",
        handoff_id=_ZERO_HANDOFF_ID,
        customer_test_id=customer_test_id,
        product_id=product_id,
        product_category=product_identity.product_category,
        product_form=product_identity.product_form,
        classification_version=CLASSIFICATION_VERSION,
        confirmation_version=confirmation_version,
        main_photo_id=main_photo_id,
        transform_permission=True,
        style_generation_allowed=True,
        photos=photos,
        idempotency_key=f"style-test:{product_id}:v{confirmation_version}",
        desensitized_test_only=True,
        source="classification_local_page",
        publishable=False,
        production_eligible=False,
    )
    handoff_id = uuid5(_HANDOFF_NAMESPACE, sha256(canonical_handoff_bytes(provisional)).hexdigest())
    return provisional.model_copy(update={"handoff_id": handoff_id})


def _handoff_fingerprint(handoff: ClassificationStyleHandoffV1) -> str:
    """Fingerprint facts independently from their deterministic derived identifier."""

    without_derived_id = handoff.model_copy(update={"handoff_id": _ZERO_HANDOFF_ID})
    return sha256(canonical_handoff_bytes(without_derived_id)).hexdigest()


class StyleHandoffIdempotencyRegistry:
    """Small in-process ledger for retriable local-test submissions.

    It stores no production permission.  A real consumer receipt/task store owns
    durable delivery in later tasks; this registry solely prevents producer facts
    from being silently overwritten during a local retry.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, tuple[str, ClassificationStyleHandoffV1]] = {}
        self._lock = RLock()

    def build(
        self,
        *,
        customer_test_id: str,
        product_id: str,
        confirmed_items: Sequence[ConfirmedUploadItem],
        media_receipts: Mapping[UUID, UUID],
    ) -> ClassificationStyleHandoffV1:
        handoff = build_style_handoff(
            customer_test_id=customer_test_id,
            product_id=product_id,
            confirmed_items=confirmed_items,
            media_receipts=media_receipts,
        )
        fingerprint = _handoff_fingerprint(handoff)
        with self._lock:
            existing = self._by_key.get(handoff.idempotency_key)
            if existing is None:
                self._by_key[handoff.idempotency_key] = (fingerprint, handoff)
                return handoff
            if existing[0] != fingerprint:
                raise StyleHandoffFailure("handoff_idempotency_conflict")
            return existing[1]
