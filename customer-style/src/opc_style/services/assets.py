"""Confirmed product-asset intake and event-derived readiness."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    AuditEvent,
    Customer,
    ProductAsset,
    ProductAssetPermissionEvent,
)
from opc_style.repositories import ProductImageRepository
from opc_style.schemas import ProductAssetIntakeRequest


class ProductAssetsNotFoundError(Exception):
    """Raised when a product has no assets visible to the requesting customer."""


@dataclass(frozen=True)
class ProductAssetReadiness:
    """Stable generation gates derived from immutable facts and permission events."""

    transform_permission: bool | None
    generation_ready: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ProductAssetIntake:
    """Intake result that distinguishes creation from an idempotent replay."""

    asset: ProductAsset
    readiness: ProductAssetReadiness
    created: bool


class ProductAssetService:
    """Admit product metadata atomically within one customer boundary."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)

    def intake(
        self,
        customer_id: UUID,
        request: ProductAssetIntakeRequest,
        *,
        commit: bool = True,
        asset_id: UUID | None = None,
        permission_provenance: str = "product_asset_intake",
    ) -> ProductAssetIntake:
        """Create one asset and permission event, or resolve a safe replay."""
        try:
            return self._intake(
                customer_id,
                request,
                commit=commit,
                asset_id=asset_id,
                permission_provenance=permission_provenance,
            )
        except IntegrityError as error:
            return self._recover_concurrent_intake(
                error,
                customer_id,
                request,
                permission_provenance=permission_provenance,
            )

    def _intake(
        self,
        customer_id: UUID,
        request: ProductAssetIntakeRequest,
        *,
        commit: bool,
        asset_id: UUID | None,
        permission_provenance: str,
    ) -> ProductAssetIntake:
        self._ensure_customer(customer_id)
        existing = self._repository.get_asset_by_photo(customer_id, request.photo_id)
        if existing is not None:
            return self._resolve_existing(
                customer_id,
                existing,
                request,
                permission_provenance=permission_provenance,
            )

        occurred_at = _as_utc(request.occurred_at)
        asset = ProductAsset(
            id=asset_id,
            customer_id=customer_id,
            product_id=request.product_id,
            photo_id=request.photo_id,
            identity="original_real_photo",
            confirmed=request.confirmed,
            source_type=request.source_type,
            quality_flags=list(request.quality_flags),
            occurred_at=occurred_at,
            content_sha256=request.content_sha256,
            mime_type=request.mime_type,
            width=request.width,
            height=request.height,
            storage_path=None,
        )
        self._session.add(asset)
        self._session.flush()
        permission = ProductAssetPermissionEvent(
            customer_id=customer_id,
            product_asset_id=asset.id,
            transform_permission=request.transform_permission,
            occurred_at=occurred_at,
            provenance=permission_provenance,
        )
        self._session.add(permission)
        self._record_audit(
            customer_id,
            "product_asset.intaken",
            {
                "asset_id": str(asset.id),
                "product_id": asset.product_id,
                "photo_id": asset.photo_id,
                "content_sha256": asset.content_sha256,
            },
        )
        self._record_audit(
            customer_id,
            "product_asset.transform_permission_recorded",
            {
                "asset_id": str(asset.id),
                "transform_permission": permission.transform_permission,
                "provenance": permission.provenance,
            },
        )
        if commit:
            self._session.commit()
            self._session.refresh(asset)
        else:
            self._session.flush()
        return ProductAssetIntake(
            asset=asset,
            readiness=self.readiness(customer_id, asset),
            created=True,
        )

    def _recover_concurrent_intake(
        self,
        error: IntegrityError,
        customer_id: UUID,
        request: ProductAssetIntakeRequest,
        *,
        permission_provenance: str,
    ) -> ProductAssetIntake:
        self._session.rollback()
        if "unique" not in str(error).lower():
            raise error
        existing = self._repository.get_asset_by_photo(customer_id, request.photo_id)
        if existing is None:
            raise error
        return self._resolve_existing(
            customer_id,
            existing,
            request,
            permission_provenance=permission_provenance,
        )

    def _resolve_existing(
        self,
        customer_id: UUID,
        asset: ProductAsset,
        request: ProductAssetIntakeRequest,
        *,
        permission_provenance: str,
    ) -> ProductAssetIntake:
        if asset.content_sha256 != request.content_sha256:
            raise DomainRuleError("product_asset_content_conflict")
        if not self._facts_match(asset, request):
            raise DomainRuleError("product_asset_facts_conflict")
        permission = self._repository.get_asset_intake_permission(
            customer_id,
            asset.id,
            asset.occurred_at,
            provenance=permission_provenance,
        )
        if permission is None:
            raise DomainRuleError("product_asset_permission_history_missing")
        if permission.transform_permission != request.transform_permission:
            raise DomainRuleError("product_asset_permission_conflict")
        return ProductAssetIntake(
            asset=asset,
            readiness=self.readiness(customer_id, asset),
            created=False,
        )

    def get_product_assets(
        self, customer_id: UUID, product_id: str
    ) -> list[ProductAssetIntake]:
        """Return only customer-owned assets for a product, or conceal absence."""
        assets = self._repository.list_assets(customer_id, product_id=product_id)
        if not assets:
            raise ProductAssetsNotFoundError
        return [
            ProductAssetIntake(
                asset=asset,
                readiness=self.readiness(customer_id, asset),
                created=False,
            )
            for asset in assets
        ]

    def readiness(
        self, customer_id: UUID, asset: ProductAsset
    ) -> ProductAssetReadiness:
        """Derive readiness from immutable confirmation and the latest permission."""
        reasons: list[str] = []
        if not asset.confirmed:
            reasons.append("organizer_confirmation_missing")
        permission = self._repository.get_latest_asset_permission(customer_id, asset.id)
        transform_permission = (
            permission.transform_permission if permission is not None else None
        )
        if transform_permission is None:
            reasons.append("transform_permission_unknown")
        elif not transform_permission:
            reasons.append("transform_permission_denied")
        return ProductAssetReadiness(
            transform_permission=transform_permission,
            generation_ready=not reasons,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _facts_match(
        asset: ProductAsset, request: ProductAssetIntakeRequest
    ) -> bool:
        return (
            asset.product_id == request.product_id
            and asset.photo_id == request.photo_id
            and asset.identity == "original_real_photo"
            and asset.confirmed is request.confirmed
            and asset.source_type == request.source_type
            and asset.quality_flags == request.quality_flags
            and _same_instant(asset.occurred_at, request.occurred_at)
            and asset.mime_type == request.mime_type
            and asset.width == request.width
            and asset.height == request.height
        )

    def _ensure_customer(self, customer_id: UUID) -> None:
        if self._session.get(Customer, customer_id) is None:
            self._session.add(
                Customer(id=customer_id, display_name=f"Customer {customer_id}")
            )
            self._session.flush()

    def _record_audit(
        self, customer_id: UUID, event_type: str, payload: dict[str, object]
    ) -> None:
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type=event_type,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )


def _same_instant(left: datetime, right: datetime) -> bool:
    """Compare SQLite-naive UTC values with timezone-aware API timestamps."""
    return _as_utc(left) == _as_utc(right)


def _as_utc(value: datetime) -> datetime:
    """Canonicalize timestamps before SQLite removes timezone information."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
