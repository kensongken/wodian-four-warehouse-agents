"""Customer-owned durable product-subject sealing orchestration."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.subject_seal import (
    HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION,
    SubjectSealError,
    SubjectSegmentationRequest,
    SubjectSegmenter,
    review_sealed_subject_mask,
    validate_sealed_subject_for_generation,
)
from opc_style.models import AuditEvent, ProductSubjectSeal
from opc_style.repositories import ProductImageRepository
from opc_style.services.real_photos import AssetContentStoreError


class ProductSubjectSealNotFoundError(Exception):
    """Conceal a missing or foreign subject-seal identity."""


class SubjectSealContentStore(Protocol):
    """Minimum private-store boundary needed by subject sealing."""

    @property
    def store_namespace(self) -> str: ...

    def read_original(
        self, customer_id: UUID, asset_id: UUID, expected_sha256: str
    ) -> bytes: ...

    def persist_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
        content: bytes,
        expected_sha256: str,
    ) -> None: ...

    def read_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
        expected_sha256: str,
    ) -> bytes: ...

    def discard_subject_artifact(
        self,
        customer_id: UUID,
        seal_id: UUID,
        kind: Literal["rgba", "core-mask"],
    ) -> None: ...


@dataclass(frozen=True)
class ProductSubjectSealOutput:
    """One immutable seal plus replay status."""

    seal: ProductSubjectSeal
    created: bool


class ProductSubjectSealService:
    """Seal one confirmed product source without weakening its permission history."""

    def __init__(
        self, session: Session, content_store: SubjectSealContentStore
    ) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)
        self._content_store = content_store

    def seal(
        self,
        customer_id: UUID,
        *,
        asset_id: UUID,
        segmenter: SubjectSegmenter,
        canvas_size: tuple[int, int],
    ) -> ProductSubjectSealOutput:
        asset = self._repository.get_asset(customer_id, asset_id)
        if asset is None:
            raise ProductSubjectSealNotFoundError
        if not asset.confirmed:
            raise DomainRuleError("organizer_confirmation_missing")
        permission = self._repository.get_latest_asset_permission(customer_id, asset_id)
        if permission is None:
            raise DomainRuleError("transform_permission_unknown")
        if not permission.transform_permission:
            raise DomainRuleError("transform_permission_denied")
        fingerprint = _seal_fingerprint(
            customer_id=customer_id,
            asset_id=asset.id,
            source_sha256=asset.content_sha256,
            permission_event_id=permission.id,
            adapter_version=segmenter.adapter_version,
            canvas_size=canvas_size,
        )
        existing = self._repository.get_subject_seal_by_fingerprint(
            customer_id, fingerprint
        )
        if existing is not None:
            self._verify_artifacts(customer_id, existing)
            return ProductSubjectSealOutput(existing, created=False)

        source = self._content_store.read_original(
            customer_id, asset.id, asset.content_sha256
        )
        sealed = segmenter.seal(
            SubjectSegmentationRequest(
                content=source,
                content_sha256=asset.content_sha256,
                mime_type=asset.mime_type,
                target_canvas=canvas_size,
            )
        )
        seal_id = uuid4()
        persisted: list[Literal["rgba", "core-mask"]] = []
        try:
            self._content_store.persist_subject_artifact(
                customer_id,
                seal_id,
                "rgba",
                sealed.rgba_png,
                sealed.rgba_sha256,
            )
            persisted.append("rgba")
            self._content_store.persist_subject_artifact(
                customer_id,
                seal_id,
                "core-mask",
                sealed.core_mask_png,
                sealed.core_mask_sha256,
            )
            persisted.append("core-mask")
            row = ProductSubjectSeal(
                id=seal_id,
                customer_id=customer_id,
                product_id=asset.product_id,
                product_asset_id=asset.id,
                transform_permission_event_id=permission.id,
                source_sha256=asset.content_sha256,
                segmentation_adapter=type(segmenter).__name__,
                segmentation_adapter_version=segmenter.adapter_version,
                mask_sha256=sealed.core_mask_sha256,
                normalized_subject_sha256=sealed.rgba_sha256,
                normalized_width=sealed.width,
                normalized_height=sealed.height,
                core_mask_pixel_count=sealed.core_mask_pixel_count,
                feather_width_px=sealed.feather_width_px,
                idempotency_fingerprint=fingerprint,
                artifact_store_namespace=self._content_store.store_namespace,
                seal_version=1,
                synthetic_test_only=sealed.synthetic_test_only,
                publishable=False,
            )
            self._session.add(row)
            self._session.add(
                AuditEvent(
                    customer_id=customer_id,
                    event_type="product_subject.sealed",
                    payload={
                        "subject_seal_id": str(row.id),
                        "product_id": row.product_id,
                        "product_asset_id": str(row.product_asset_id),
                        "source_sha256": row.source_sha256,
                        "normalized_subject_sha256": row.normalized_subject_sha256,
                        "mask_sha256": row.mask_sha256,
                        "segmentation_adapter_version": (
                            row.segmentation_adapter_version
                        ),
                        "synthetic_test_only": row.synthetic_test_only,
                        "publishable": False,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            self._session.commit()
            self._session.refresh(row)
            return ProductSubjectSealOutput(row, created=True)
        except IntegrityError:
            self._session.rollback()
            self._discard_persisted(customer_id, seal_id, persisted)
            winner = self._repository.get_subject_seal_by_fingerprint(
                customer_id, fingerprint
            )
            if winner is None:
                raise
            self._verify_artifacts(customer_id, winner)
            return ProductSubjectSealOutput(winner, created=False)
        except Exception:
            self._session.rollback()
            self._discard_persisted(customer_id, seal_id, persisted)
            raise

    def get(self, customer_id: UUID, seal_id: UUID) -> ProductSubjectSeal:
        row = self._repository.get_subject_seal(customer_id, seal_id)
        if row is None:
            raise ProductSubjectSealNotFoundError
        self._verify_artifacts(customer_id, row)
        return row

    def confirm_reviewed_mask(
        self,
        customer_id: UUID,
        *,
        base_seal_id: UUID,
        reviewed_mask_png: bytes,
        confirmation_key: str,
        manual_selection: bool = False,
    ) -> ProductSubjectSealOutput:
        """Persist one human-confirmed erase-only correction as a new seal."""
        if not confirmation_key.strip() or len(confirmation_key) > 200:
            raise DomainRuleError("subject_mask_confirmation_invalid")
        base = self.get(customer_id, base_seal_id)
        if (
            manual_selection
            and base.segmentation_adapter_version != "manual-full-frame-v1"
        ):
            raise DomainRuleError("subject_manual_proposal_required")
        asset = self._repository.get_asset(customer_id, base.product_asset_id)
        permission = self._repository.get_latest_asset_permission(
            customer_id, base.product_asset_id
        )
        if asset is None:
            raise ProductSubjectSealNotFoundError
        if permission is None:
            raise DomainRuleError("transform_permission_unknown")
        if not permission.transform_permission:
            raise DomainRuleError("transform_permission_denied")
        base_rgba = self._content_store.read_subject_artifact(
            customer_id, base.id, "rgba", base.normalized_subject_sha256
        )
        reviewed = review_sealed_subject_mask(
            base_rgba,
            reviewed_mask_png,
            synthetic_test_only=base.synthetic_test_only,
            manual_selection=manual_selection,
        )
        fingerprint = _reviewed_seal_fingerprint(
            customer_id=customer_id,
            base_seal_id=base.id,
            asset_id=asset.id,
            permission_event_id=permission.id,
            reviewed_subject_sha256=reviewed.rgba_sha256,
        )
        existing = self._repository.get_subject_seal_by_fingerprint(
            customer_id, fingerprint
        )
        if existing is not None:
            self._verify_artifacts(customer_id, existing)
            return ProductSubjectSealOutput(existing, created=False)

        seal_id = uuid4()
        persisted: list[Literal["rgba", "core-mask"]] = []
        try:
            self._content_store.persist_subject_artifact(
                customer_id,
                seal_id,
                "rgba",
                reviewed.rgba_png,
                reviewed.rgba_sha256,
            )
            persisted.append("rgba")
            self._content_store.persist_subject_artifact(
                customer_id,
                seal_id,
                "core-mask",
                reviewed.core_mask_png,
                reviewed.core_mask_sha256,
            )
            persisted.append("core-mask")
            row = ProductSubjectSeal(
                id=seal_id,
                customer_id=customer_id,
                product_id=asset.product_id,
                product_asset_id=asset.id,
                transform_permission_event_id=permission.id,
                source_sha256=asset.content_sha256,
                segmentation_adapter="HumanReviewedSubjectMask",
                segmentation_adapter_version=(HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION),
                mask_sha256=reviewed.core_mask_sha256,
                normalized_subject_sha256=reviewed.rgba_sha256,
                normalized_width=reviewed.width,
                normalized_height=reviewed.height,
                core_mask_pixel_count=reviewed.core_mask_pixel_count,
                feather_width_px=reviewed.feather_width_px,
                idempotency_fingerprint=fingerprint,
                artifact_store_namespace=self._content_store.store_namespace,
                seal_version=1,
                synthetic_test_only=reviewed.synthetic_test_only,
                publishable=False,
            )
            self._session.add(row)
            self._session.add(
                AuditEvent(
                    customer_id=customer_id,
                    event_type="product_subject.mask_confirmed",
                    payload={
                        "subject_seal_id": str(row.id),
                        "base_subject_seal_id": str(base.id),
                        "product_asset_id": str(asset.id),
                        "confirmation_key": confirmation_key,
                        "mask_sha256": row.mask_sha256,
                        "adapter_version": row.segmentation_adapter_version,
                        "publishable": False,
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            self._session.commit()
            self._session.refresh(row)
            return ProductSubjectSealOutput(row, created=True)
        except IntegrityError:
            self._session.rollback()
            self._discard_persisted(customer_id, seal_id, persisted)
            winner = self._repository.get_subject_seal_by_fingerprint(
                customer_id, fingerprint
            )
            if winner is None:
                raise
            self._verify_artifacts(customer_id, winner)
            return ProductSubjectSealOutput(winner, created=False)
        except Exception:
            self._session.rollback()
            self._discard_persisted(customer_id, seal_id, persisted)
            raise

    def require_reviewed(
        self, customer_id: UUID, seal_id: UUID, *, asset_id: UUID
    ) -> ProductSubjectSeal:
        """Resolve a reviewed seal only for its original asset and live permission."""
        row = self.get(customer_id, seal_id)
        if (
            row.product_asset_id != asset_id
            or row.segmentation_adapter_version
            != HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
        ):
            raise DomainRuleError("subject_mask_confirmation_required")
        permission = self._repository.get_latest_asset_permission(customer_id, asset_id)
        if permission is None:
            raise DomainRuleError("transform_permission_unknown")
        if not permission.transform_permission:
            raise DomainRuleError("transform_permission_denied")
        rgba = self._content_store.read_subject_artifact(
            customer_id, row.id, "rgba", row.normalized_subject_sha256
        )
        try:
            validate_sealed_subject_for_generation(rgba)
        except SubjectSealError as error:
            raise DomainRuleError(error.code) from error
        return row

    def _verify_artifacts(self, customer_id: UUID, row: ProductSubjectSeal) -> None:
        if row.artifact_store_namespace != self._content_store.store_namespace:
            raise AssetContentStoreError("subject_artifact_store_mismatch")
        self._content_store.read_subject_artifact(
            customer_id, row.id, "rgba", row.normalized_subject_sha256
        )
        self._content_store.read_subject_artifact(
            customer_id, row.id, "core-mask", row.mask_sha256
        )

    def _discard_persisted(
        self,
        customer_id: UUID,
        seal_id: UUID,
        persisted: list[Literal["rgba", "core-mask"]],
    ) -> None:
        for kind in reversed(persisted):
            self._content_store.discard_subject_artifact(customer_id, seal_id, kind)


def _seal_fingerprint(
    *,
    customer_id: UUID,
    asset_id: UUID,
    source_sha256: str,
    permission_event_id: UUID,
    adapter_version: str,
    canvas_size: tuple[int, int],
) -> str:
    canonical = json.dumps(
        {
            "schema": "ProductSubjectSealV1",
            "customer_id": str(customer_id),
            "asset_id": str(asset_id),
            "source_sha256": source_sha256,
            "permission_event_id": str(permission_event_id),
            "adapter_version": adapter_version,
            "canvas_size": list(canvas_size),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _reviewed_seal_fingerprint(
    *,
    customer_id: UUID,
    base_seal_id: UUID,
    asset_id: UUID,
    permission_event_id: UUID,
    reviewed_subject_sha256: str,
) -> str:
    canonical = json.dumps(
        {
            "schema": "HumanReviewedSubjectMaskV1",
            "customer_id": str(customer_id),
            "base_seal_id": str(base_seal_id),
            "asset_id": str(asset_id),
            "permission_event_id": str(permission_event_id),
            "reviewed_subject_sha256": reviewed_subject_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()
