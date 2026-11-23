"""Durable single-dispatch analysis and human-confirmed identity-card versions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import (
    ProductIdentityAnalyzer,
    ProductIdentityDraft,
    ProductIdentityImage,
    empty_manual_identity_draft,
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.models import (
    ProductAsset,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
)
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError
from opc_style.services.style_consent import coordinate_style_consent

_USD_QUANTUM = Decimal("0.000001")
MANUAL_IDENTITY_MODEL = "local-manual-entry-v1"
_MANUAL_REQUIRED_FIELDS = (
    "category",
    "primary_colors",
    "component_shapes",
    "overall_form",
)


@dataclass(frozen=True)
class ProductIdentityPreview:
    asset_id: UUID
    product_id: str
    source_content_sha256: str
    model: str
    maximum_usd: Decimal
    currency: str
    input_image_count: int
    manifest_sha256: str
    publishable: bool = False


@dataclass(frozen=True)
class ProductIdentityAnalysisOutcome:
    run_id: UUID
    proposal: ProductIdentityDraft
    created: bool


@dataclass(frozen=True)
class ProductIdentityCardOutcome:
    card_version_id: UUID
    version: int
    card_sha256: str
    card: ProductIdentityDraft
    created: bool = field(compare=False)


@dataclass(frozen=True)
class _PreparedIdentity:
    asset: ProductAsset
    image: ProductIdentityImage
    manifest: dict[str, object]
    manifest_sha256: str


def validate_confirmed_product_identity_card(
    session: Session,
    content_store: AssetContentStore,
    customer_id: UUID,
    product_id: str,
    source_asset_id: UUID,
    card_version_id: UUID | None,
) -> tuple[ProductIdentityCardVersion, dict[str, object]]:
    """Recompute the current asset/card seal before paid authorization."""
    if card_version_id is None:
        raise DomainRuleError("confirmed_product_identity_card_required")
    card = session.scalar(
        select(ProductIdentityCardVersion).where(
            ProductIdentityCardVersion.customer_id == customer_id,
            ProductIdentityCardVersion.id == card_version_id,
        )
    )
    source = session.scalar(
        select(ProductAsset).where(
            ProductAsset.customer_id == customer_id,
            ProductAsset.id == source_asset_id,
            ProductAsset.product_id == product_id,
            ProductAsset.identity == "original_real_photo",
            ProductAsset.confirmed.is_(True),
        )
    )
    if card is None or source is None:
        raise DomainRuleError("product_identity_card_scope_mismatch")
    try:
        content = content_store.read_original(
            customer_id, source.id, source.content_sha256
        )
        draft = normalize_identity_draft(card.card_payload)
    except (AssetContentStoreError, DomainRuleError, OSError) as error:
        raise DomainRuleError("product_identity_card_scope_mismatch") from error
    payload = identity_card_payload(draft)
    if (
        card.product_asset_id != source.id
        or card.product_id != source.product_id
        or card.source_content_sha256 != source.content_sha256
        or sha256(content).hexdigest() != source.content_sha256
        or identity_card_sha256(draft) != card.card_sha256
    ):
        raise DomainRuleError("product_identity_card_scope_mismatch")
    return card, payload


class ProductIdentityService:
    """Quote one exact asset, dispatch at most once, then append confirmed cards."""

    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore,
        analyzer: ProductIdentityAnalyzer,
        model: str,
        maximum_usd: Decimal,
    ) -> None:
        try:
            canonical_maximum_usd = maximum_usd.quantize(
                _USD_QUANTUM, rounding=ROUND_HALF_UP
            )
        except InvalidOperation as error:
            raise DomainRuleError(
                "product_identity_analysis_provider_disabled"
            ) from error
        if (
            canonical_maximum_usd <= 0
            or not model.strip()
            or getattr(analyzer, "analysis_mode", "")
            != "real_product_identity_proposal"
            or not getattr(analyzer, "analyzer_version", "")
        ):
            raise DomainRuleError("product_identity_analysis_provider_disabled")
        self._session = session
        self._content_store = content_store
        self._analyzer = analyzer
        self._model = model
        self._maximum_usd = canonical_maximum_usd

    def preview(self, customer_id: UUID, asset_id: UUID) -> ProductIdentityPreview:
        prepared = self._prepare(customer_id, asset_id)
        return ProductIdentityPreview(
            asset_id=prepared.asset.id,
            product_id=prepared.asset.product_id,
            source_content_sha256=prepared.asset.content_sha256,
            model=self._model,
            maximum_usd=self._maximum_usd,
            currency="USD",
            input_image_count=1,
            manifest_sha256=prepared.manifest_sha256,
        )

    def execute(
        self,
        customer_id: UUID,
        asset_id: UUID,
        *,
        idempotency_key: str,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> ProductIdentityAnalysisOutcome:
        existing = self._analysis_by_key(customer_id, idempotency_key)
        if existing is not None:
            return self._replay_existing(
                customer_id,
                asset_id,
                existing,
                confirmed_maximum_usd=confirmed_maximum_usd,
                confirmed_manifest_sha256=confirmed_manifest_sha256,
            )

        prepared = self._prepare(customer_id, asset_id)
        if (
            confirmed_maximum_usd != self._maximum_usd
            or confirmed_manifest_sha256 != prepared.manifest_sha256
        ):
            raise DomainRuleError("product_identity_analysis_confirmation_required")

        run = ProductIdentityAnalysisRun(
            customer_id=customer_id,
            product_asset_id=prepared.asset.id,
            product_id=prepared.asset.product_id,
            source_content_sha256=prepared.asset.content_sha256,
            idempotency_key=idempotency_key,
            model=self._model,
            authorized_usd=self._maximum_usd,
            input_manifest_sha256=prepared.manifest_sha256,
            input_manifest=prepared.manifest,
            dispatch_state="reserved",
            outbound_attempts=0,
        )
        self._session.add(run)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            concurrent = self._analysis_by_key(customer_id, idempotency_key)
            if concurrent is None:
                raise
            return self._replay_existing(
                customer_id,
                asset_id,
                concurrent,
                confirmed_maximum_usd=confirmed_maximum_usd,
                confirmed_manifest_sha256=confirmed_manifest_sha256,
            )
        self._session.refresh(run)

        with coordinate_style_consent(customer_id):
            try:
                current = self._prepare(customer_id, asset_id)
            except DomainRuleError as error:
                run.failure_code = "product_identity_manifest_changed"
                self._session.commit()
                raise DomainRuleError("product_identity_manifest_changed") from error
            if not self._same_authorized_input(run, current):
                run.failure_code = "product_identity_manifest_changed"
                self._session.commit()
                raise DomainRuleError("product_identity_manifest_changed")

            # Seal uncertainty before any image byte can cross the provider boundary.
            run.dispatch_state = "dispatch_uncertain"
            run.outbound_attempts = 1
            self._session.commit()
            try:
                proposal_payload = identity_card_payload(
                    self._analyzer.analyze(current.image)
                )
                proposal = normalize_identity_draft(proposal_payload)
                refreshed = self._prepare(customer_id, asset_id)
                if not self._same_authorized_input(run, refreshed):
                    raise DomainRuleError("product_identity_manifest_changed")
            except Exception as error:
                self._persist_terminal_failure(run.id, error)
                raise DomainRuleError(
                    "product_identity_analysis_outcome_uncertain"
                ) from error

        run.proposal = proposal_payload
        run.dispatch_state = "provider_responded"
        run.failure_code = None
        self._session.commit()
        return ProductIdentityAnalysisOutcome(run.id, proposal, True)

    def confirm(
        self,
        customer_id: UUID,
        asset_id: UUID,
        *,
        analysis_run_id: UUID,
        card: Mapping[str, object],
        confirmed: bool,
    ) -> ProductIdentityCardOutcome:
        if confirmed is not True:
            raise DomainRuleError("product_identity_card_confirmation_required")
        try:
            normalized = normalize_identity_draft(card)
            canonical_payload = identity_card_payload(normalized)
            card_digest = identity_card_sha256(normalized)
        except DomainRuleError as error:
            raise DomainRuleError("product_identity_card_incomplete") from error

        with coordinate_style_consent(customer_id):
            self._begin_serialized_sqlite_write()
            asset = self._locked_asset(customer_id, asset_id)
            if asset is None:
                self._session.rollback()
                raise DomainRuleError("product_identity_card_scope_mismatch")
            try:
                prepared = self._prepare_asset(customer_id, asset)
            except DomainRuleError as error:
                self._session.rollback()
                raise DomainRuleError("product_identity_manifest_changed") from error
            run = self._session.scalar(
                select(ProductIdentityAnalysisRun).where(
                    ProductIdentityAnalysisRun.customer_id == customer_id,
                    ProductIdentityAnalysisRun.id == analysis_run_id,
                    ProductIdentityAnalysisRun.product_asset_id == asset_id,
                    ProductIdentityAnalysisRun.product_id == asset.product_id,
                    ProductIdentityAnalysisRun.source_content_sha256
                    == asset.content_sha256,
                )
            )
            if not self._confirmable_run(run):
                self._session.rollback()
                raise DomainRuleError("product_identity_card_scope_mismatch")
            assert run is not None
            if run.model == MANUAL_IDENTITY_MODEL and any(
                getattr(normalized, field).certainty != "observed"
                for field in _MANUAL_REQUIRED_FIELDS
            ):
                self._session.rollback()
                raise DomainRuleError("product_identity_card_incomplete")
            if not self._same_authorized_input(run, prepared):
                self._session.rollback()
                raise DomainRuleError("product_identity_card_scope_mismatch")

            existing = self._session.scalar(
                select(ProductIdentityCardVersion).where(
                    ProductIdentityCardVersion.customer_id == customer_id,
                    ProductIdentityCardVersion.product_asset_id == asset_id,
                    ProductIdentityCardVersion.analysis_run_id == analysis_run_id,
                    ProductIdentityCardVersion.card_sha256 == card_digest,
                )
            )
            if existing is not None:
                self._session.commit()
                return self._card_outcome(existing, created=False)

            highest = self._session.scalar(
                select(func.max(ProductIdentityCardVersion.version)).where(
                    ProductIdentityCardVersion.customer_id == customer_id,
                    ProductIdentityCardVersion.product_asset_id == asset_id,
                )
            )
            version = ProductIdentityCardVersion(
                customer_id=customer_id,
                product_asset_id=asset.id,
                product_id=asset.product_id,
                analysis_run_id=analysis_run_id,
                version=(highest or 0) + 1,
                source_content_sha256=asset.content_sha256,
                card_payload=canonical_payload,
                card_sha256=card_digest,
                confirmed_by="local_customer",
            )
            self._session.add(version)
            self._session.commit()
            self._session.refresh(version)
            return self._card_outcome(version, created=True)

    def create_manual_draft(
        self,
        customer_id: UUID,
        asset_id: UUID,
        *,
        idempotency_key: str,
    ) -> ProductIdentityAnalysisOutcome:
        """Create a zero-cost local draft with no provider dispatch."""
        existing = self._analysis_by_key(customer_id, idempotency_key)
        if existing is not None:
            return self._replay_manual(customer_id, asset_id, existing)

        prepared = self._prepare(customer_id, asset_id)
        proposal = empty_manual_identity_draft()
        run = ProductIdentityAnalysisRun(
            customer_id=customer_id,
            product_asset_id=prepared.asset.id,
            product_id=prepared.asset.product_id,
            source_content_sha256=prepared.asset.content_sha256,
            idempotency_key=idempotency_key,
            model=MANUAL_IDENTITY_MODEL,
            authorized_usd=Decimal(0),
            input_manifest_sha256=prepared.manifest_sha256,
            input_manifest=prepared.manifest,
            dispatch_state="provider_responded",
            outbound_attempts=0,
            proposal=identity_card_payload(proposal),
            failure_code=None,
        )
        self._session.add(run)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            concurrent = self._analysis_by_key(customer_id, idempotency_key)
            if concurrent is None:
                raise
            return self._replay_manual(customer_id, asset_id, concurrent)
        self._session.refresh(run)
        return ProductIdentityAnalysisOutcome(run.id, proposal, True)

    def versions(
        self, customer_id: UUID, asset_id: UUID
    ) -> list[ProductIdentityCardOutcome]:
        with coordinate_style_consent(customer_id):
            asset = self._asset(customer_id, asset_id)
            if asset is None:
                raise DomainRuleError("product_identity_asset_unavailable")
            try:
                prepared = self._prepare_asset(customer_id, asset)
            except DomainRuleError as error:
                raise DomainRuleError("product_identity_manifest_changed") from error
            rows = list(
                self._session.scalars(
                    select(ProductIdentityCardVersion)
                    .where(
                        ProductIdentityCardVersion.customer_id == customer_id,
                        ProductIdentityCardVersion.product_asset_id == asset_id,
                        ProductIdentityCardVersion.product_id == asset.product_id,
                        ProductIdentityCardVersion.source_content_sha256
                        == asset.content_sha256,
                    )
                    .order_by(
                        ProductIdentityCardVersion.version.desc(),
                        ProductIdentityCardVersion.id,
                    )
                )
            )
            if any(
                row.product_id != prepared.asset.product_id
                or row.source_content_sha256 != prepared.asset.content_sha256
                for row in rows
            ):
                raise DomainRuleError("product_identity_card_scope_mismatch")
            try:
                return [self._card_outcome(row, created=False) for row in rows]
            except DomainRuleError as error:
                raise DomainRuleError("product_identity_card_scope_mismatch") from error

    def _prepare(self, customer_id: UUID, asset_id: UUID) -> _PreparedIdentity:
        asset = self._asset(customer_id, asset_id)
        if asset is None:
            raise DomainRuleError("product_identity_asset_unavailable")
        return self._prepare_asset(customer_id, asset)

    def _prepare_asset(
        self, customer_id: UUID, asset: ProductAsset
    ) -> _PreparedIdentity:
        try:
            content = self._content_store.read_original(
                customer_id, asset.id, asset.content_sha256
            )
        except AssetContentStoreError as error:
            raise DomainRuleError("product_identity_asset_unavailable") from error
        if sha256(content).hexdigest() != asset.content_sha256:
            raise DomainRuleError("product_identity_asset_unavailable")
        manifest: dict[str, object] = {
            "asset_id": str(asset.id),
            "product_id": asset.product_id,
            "source_content_sha256": asset.content_sha256,
            "mime_type": asset.mime_type,
            "width": asset.width,
            "height": asset.height,
            "input_image_count": 1,
        }
        manifest_sha256 = sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return _PreparedIdentity(
            asset=asset,
            image=ProductIdentityImage(
                asset_id=asset.id,
                product_id=asset.product_id,
                content=content,
                content_sha256=asset.content_sha256,
                mime_type=asset.mime_type,
            ),
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )

    def _asset(self, customer_id: UUID, asset_id: UUID) -> ProductAsset | None:
        return self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.id == asset_id,
                ProductAsset.identity == "original_real_photo",
                ProductAsset.confirmed.is_(True),
            )
        )

    def _locked_asset(self, customer_id: UUID, asset_id: UUID) -> ProductAsset | None:
        return self._session.scalar(
            select(ProductAsset)
            .where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.id == asset_id,
                ProductAsset.identity == "original_real_photo",
                ProductAsset.confirmed.is_(True),
            )
            .with_for_update()
        )

    def _analysis_by_key(
        self, customer_id: UUID, idempotency_key: str
    ) -> ProductIdentityAnalysisRun | None:
        return self._session.scalar(
            select(ProductIdentityAnalysisRun).where(
                ProductIdentityAnalysisRun.customer_id == customer_id,
                ProductIdentityAnalysisRun.idempotency_key == idempotency_key,
            )
        )

    def _replay_existing(
        self,
        customer_id: UUID,
        asset_id: UUID,
        run: ProductIdentityAnalysisRun,
        *,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> ProductIdentityAnalysisOutcome:
        if run.customer_id != customer_id or run.product_asset_id != asset_id:
            raise DomainRuleError("product_identity_analysis_outcome_uncertain")
        with coordinate_style_consent(customer_id):
            asset = self._asset(customer_id, asset_id)
            if asset is None:
                raise DomainRuleError("product_identity_analysis_outcome_uncertain")
            try:
                prepared = self._prepare_asset(customer_id, asset)
            except DomainRuleError as error:
                raise DomainRuleError("product_identity_manifest_changed") from error
            return self._replay(
                run,
                prepared,
                confirmed_maximum_usd=confirmed_maximum_usd,
                confirmed_manifest_sha256=confirmed_manifest_sha256,
            )

    def _replay(
        self,
        run: ProductIdentityAnalysisRun,
        prepared: _PreparedIdentity,
        *,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> ProductIdentityAnalysisOutcome:
        if (
            confirmed_maximum_usd != self._maximum_usd
            or confirmed_maximum_usd != run.authorized_usd
            or confirmed_manifest_sha256 != prepared.manifest_sha256
            or confirmed_manifest_sha256 != run.input_manifest_sha256
            or self._model != run.model
        ):
            raise DomainRuleError("product_identity_analysis_confirmation_required")
        if (
            not self._same_authorized_input(run, prepared)
            or run.dispatch_state != "provider_responded"
            or run.outbound_attempts != 1
            or run.proposal is None
            or run.failure_code is not None
        ):
            raise DomainRuleError("product_identity_analysis_outcome_uncertain")
        try:
            proposal = normalize_identity_draft(run.proposal)
        except DomainRuleError as error:
            raise DomainRuleError(
                "product_identity_analysis_outcome_uncertain"
            ) from error
        return ProductIdentityAnalysisOutcome(run.id, proposal, False)

    def _replay_manual(
        self,
        customer_id: UUID,
        asset_id: UUID,
        run: ProductIdentityAnalysisRun,
    ) -> ProductIdentityAnalysisOutcome:
        prepared = self._prepare(customer_id, asset_id)
        if (
            run.customer_id != customer_id
            or run.product_asset_id != asset_id
            or run.model != MANUAL_IDENTITY_MODEL
            or run.authorized_usd != Decimal(0)
            or run.dispatch_state != "provider_responded"
            or run.outbound_attempts != 0
            or run.failure_code is not None
            or run.proposal is None
            or not self._same_authorized_input(run, prepared)
        ):
            raise DomainRuleError("product_identity_card_scope_mismatch")
        try:
            proposal = normalize_identity_draft(run.proposal)
        except DomainRuleError as error:
            raise DomainRuleError("product_identity_card_scope_mismatch") from error
        return ProductIdentityAnalysisOutcome(run.id, proposal, False)

    @staticmethod
    def _same_authorized_input(
        run: ProductIdentityAnalysisRun, prepared: _PreparedIdentity
    ) -> bool:
        return (
            run.product_asset_id == prepared.asset.id
            and run.product_id == prepared.asset.product_id
            and run.source_content_sha256 == prepared.asset.content_sha256
            and run.input_manifest_sha256 == prepared.manifest_sha256
            and run.input_manifest == prepared.manifest
        )

    def _persist_terminal_failure(self, run_id: UUID, error: Exception) -> None:
        self._session.rollback()
        run = self._session.get(ProductIdentityAnalysisRun, run_id)
        if run is None:
            return
        run.failure_code = self._failure_code(error)
        self._session.commit()

    @staticmethod
    def _failure_code(error: Exception) -> str:
        code = getattr(error, "code", "product_identity_analysis_unavailable")
        if code == "product_identity_draft_invalid":
            return "product_identity_analysis_invalid_response"
        if code == "product_identity_asset_unavailable":
            return "product_identity_manifest_changed"
        if not isinstance(code, str) or not code or len(code) > 100:
            return "product_identity_analysis_unavailable"
        return code

    @staticmethod
    def _confirmable_run(run: ProductIdentityAnalysisRun | None) -> bool:
        if (
            run is None
            or run.dispatch_state != "provider_responded"
            or run.failure_code is not None
            or run.proposal is None
        ):
            return False
        provider_run = run.outbound_attempts == 1 and run.authorized_usd > 0
        manual_run = (
            run.model == MANUAL_IDENTITY_MODEL
            and run.outbound_attempts == 0
            and run.authorized_usd == 0
        )
        if not (provider_run or manual_run):
            return False
        try:
            normalize_identity_draft(run.proposal)
        except DomainRuleError:
            return False
        return True

    @staticmethod
    def _card_outcome(
        row: ProductIdentityCardVersion, *, created: bool
    ) -> ProductIdentityCardOutcome:
        card = normalize_identity_draft(row.card_payload)
        if identity_card_sha256(card) != row.card_sha256:
            raise DomainRuleError("product_identity_card_scope_mismatch")
        return ProductIdentityCardOutcome(
            card_version_id=row.id,
            version=row.version,
            card_sha256=row.card_sha256,
            card=card,
            created=created,
        )

    def _begin_serialized_sqlite_write(self) -> None:
        connection = self._session.connection()
        if connection.dialect.name != "sqlite":
            return
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as error:
            if "cannot start a transaction within a transaction" not in str(error):
                raise


__all__ = [
    "ProductIdentityAnalysisOutcome",
    "ProductIdentityCardOutcome",
    "ProductIdentityPreview",
    "ProductIdentityService",
    "validate_confirmed_product_identity_card",
]
