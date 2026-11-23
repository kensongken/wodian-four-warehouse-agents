"""Single-dispatch observation of new photos against reusable identity cards."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import (
    ProductIdentityAnalyzer,
    ProductIdentityDraft,
    ProductIdentityImage,
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.domain.product_identity_gate import (
    IDENTITY_GATE_POLICY_VERSION,
    compare_product_identity,
)
from opc_style.models import (
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductIdentityCardVersion,
    ProductIdentityObservationReview,
    ProductIdentityObservationRun,
)
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError
from opc_style.services.style_consent import coordinate_style_consent

_USD_QUANTUM = Decimal("0.000001")
_ZERO_USD = Decimal("0.000000")
_LOCAL_EXACT_MATCH_MODEL = "local-exact-source-match"
_LOCAL_EXACT_MATCH_VERSION = "local-exact-source-match-v1"


@dataclass(frozen=True)
class ProductIdentityObservationPreview:
    asset_id: UUID
    product_id: str
    source_content_sha256: str
    product_identity_card_version_id: UUID
    product_identity_card_sha256: str
    model: str
    observer_version: str
    policy_version: str
    maximum_usd: Decimal
    currency: str
    input_image_count: int
    manifest_sha256: str
    publishable: bool = False


@dataclass(frozen=True)
class ProductIdentityObservationOutcome:
    run_id: UUID
    observation: ProductIdentityDraft
    decision: str
    reason_codes: tuple[str, ...]
    result_sha256: str
    created: bool = field(compare=False)


@dataclass(frozen=True)
class ProductIdentityObservationReviewOutcome:
    review_id: UUID
    observation_run_id: UUID
    decision: str
    reason: str
    created: bool = field(compare=False)


@dataclass(frozen=True)
class ValidatedProductIdentityEvidence:
    observation_run_id: UUID
    observation_result_sha256: str
    review_id: UUID | None


@dataclass(frozen=True)
class _PreparedObservation:
    asset: ProductAsset
    card: ProductIdentityCardVersion
    card_payload: dict[str, object]
    image: ProductIdentityImage
    manifest: dict[str, object]
    manifest_sha256: str


def validate_product_identity_card_for_product(
    session: Session,
    content_store: AssetContentStore,
    customer_id: UUID,
    product_id: str,
    card_version_id: UUID,
) -> tuple[ProductIdentityCardVersion, dict[str, object]]:
    card = session.scalar(
        select(ProductIdentityCardVersion).where(
            ProductIdentityCardVersion.customer_id == customer_id,
            ProductIdentityCardVersion.id == card_version_id,
            ProductIdentityCardVersion.product_id == product_id,
        )
    )
    if card is None:
        raise DomainRuleError("product_identity_observation_scope_mismatch")
    source = session.scalar(
        select(ProductAsset).where(
            ProductAsset.customer_id == customer_id,
            ProductAsset.id == card.product_asset_id,
            ProductAsset.product_id == product_id,
            ProductAsset.content_sha256 == card.source_content_sha256,
            ProductAsset.confirmed.is_(True),
            ProductAsset.identity == "original_real_photo",
        )
    )
    if source is None:
        raise DomainRuleError("product_identity_observation_scope_mismatch")
    _require_transform_permission(session, customer_id, source.id)
    try:
        content = content_store.read_original(customer_id, source.id, source.content_sha256)
        draft = normalize_identity_draft(card.card_payload)
    except (AssetContentStoreError, DomainRuleError, OSError) as error:
        raise DomainRuleError("product_identity_observation_scope_mismatch") from error
    if sha256(content).hexdigest() != source.content_sha256 or identity_card_sha256(draft) != card.card_sha256:
        raise DomainRuleError("product_identity_observation_scope_mismatch")
    return card, identity_card_payload(draft)


def validate_product_identity_generation_evidence(
    session: Session,
    content_store: AssetContentStore,
    customer_id: UUID,
    product_id: str,
    asset_id: UUID,
    card_version_id: UUID,
    observation_run_id: UUID,
    review_id: UUID | None = None,
) -> ValidatedProductIdentityEvidence:
    """Validate sealed same-product evidence without provider authority."""
    asset = session.scalar(
        select(ProductAsset).where(
            ProductAsset.customer_id == customer_id,
            ProductAsset.id == asset_id,
            ProductAsset.product_id == product_id,
            ProductAsset.confirmed.is_(True),
            ProductAsset.identity == "original_real_photo",
        )
    )
    if asset is None:
        raise DomainRuleError("product_identity_observation_evidence_invalid")
    _require_transform_permission(session, customer_id, asset.id)
    try:
        content = content_store.read_original(customer_id, asset.id, asset.content_sha256)
    except (AssetContentStoreError, OSError) as error:
        raise DomainRuleError("product_identity_observation_evidence_invalid") from error
    if sha256(content).hexdigest() != asset.content_sha256:
        raise DomainRuleError("product_identity_observation_evidence_invalid")
    card, card_payload = validate_product_identity_card_for_product(
        session, content_store, customer_id, product_id, card_version_id
    )
    run = session.scalar(
        select(ProductIdentityObservationRun).where(
            ProductIdentityObservationRun.customer_id == customer_id,
            ProductIdentityObservationRun.id == observation_run_id,
            ProductIdentityObservationRun.product_asset_id == asset.id,
            ProductIdentityObservationRun.product_id == product_id,
            ProductIdentityObservationRun.source_content_sha256 == asset.content_sha256,
            ProductIdentityObservationRun.product_identity_card_version_id == card.id,
            ProductIdentityObservationRun.product_identity_card_sha256 == card.card_sha256,
            ProductIdentityObservationRun.dispatch_state.in_(
                ("provider_responded", "locally_verified")
            ),
            ProductIdentityObservationRun.failure_code.is_(None),
            ProductIdentityObservationRun.policy_version
            == IDENTITY_GATE_POLICY_VERSION,
        )
    )
    if (
        run is None
        or run.observation_payload is None
        or run.decision not in {"pass", "needs_review", "block"}
        or run.reason_codes is None
        or run.result_sha256 is None
    ):
        raise DomainRuleError("product_identity_observation_evidence_invalid")
    provider_evidence = (
        run.dispatch_state == "provider_responded" and run.outbound_attempts == 1
    )
    local_evidence = (
        run.dispatch_state == "locally_verified"
        and run.outbound_attempts == 0
        and run.authorized_usd == 0
        and run.observation_mode == "local_exact_source_match"
        and run.source_content_sha256 == card.source_content_sha256
    )
    if not (provider_evidence or local_evidence):
        raise DomainRuleError("product_identity_observation_evidence_invalid")
    observation = normalize_identity_draft(run.observation_payload)
    if local_evidence and (
        run.decision != "pass"
        or run.reason_codes != ["card_source_exact_match"]
        or identity_card_payload(observation) != card_payload
    ):
        raise DomainRuleError("product_identity_observation_evidence_invalid")
    expected = _canonical_sha256(
        {
            "observation": identity_card_payload(observation),
            "decision": run.decision,
            "reason_codes": run.reason_codes,
            "policy_version": run.policy_version,
        }
    )
    if expected != run.result_sha256:
        raise DomainRuleError("product_identity_observation_evidence_invalid")
    if run.decision == "block":
        raise DomainRuleError("product_identity_observation_blocked")
    if run.decision == "pass":
        if review_id is not None:
            raise DomainRuleError("product_identity_observation_review_invalid")
        return ValidatedProductIdentityEvidence(run.id, run.result_sha256, None)
    if review_id is None:
        raise DomainRuleError("product_identity_observation_review_required")
    review = session.scalar(
        select(ProductIdentityObservationReview).where(
            ProductIdentityObservationReview.customer_id == customer_id,
            ProductIdentityObservationReview.id == review_id,
            ProductIdentityObservationReview.observation_run_id == run.id,
            ProductIdentityObservationReview.product_asset_id == asset.id,
            ProductIdentityObservationReview.product_id == product_id,
            ProductIdentityObservationReview.source_content_sha256
            == asset.content_sha256,
            ProductIdentityObservationReview.product_identity_card_version_id
            == card.id,
            ProductIdentityObservationReview.product_identity_card_sha256
            == card.card_sha256,
            ProductIdentityObservationReview.observation_result_sha256
            == run.result_sha256,
        )
    )
    if review is None or review.decision != "approved":
        raise DomainRuleError("product_identity_observation_not_approved")
    return ValidatedProductIdentityEvidence(run.id, run.result_sha256, review.id)


class ProductIdentityObservationService:
    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore,
        analyzer: ProductIdentityAnalyzer,
        model: str,
        maximum_usd: Decimal,
        provider_enabled: bool = True,
    ) -> None:
        try:
            cost = maximum_usd.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
        except InvalidOperation as error:
            raise DomainRuleError("product_identity_observation_provider_disabled") from error
        if (
            (provider_enabled and cost <= 0)
            or (not provider_enabled and cost < 0)
            or not model.strip()
            or getattr(analyzer, "analysis_mode", "") != "real_product_identity_proposal"
            or not getattr(analyzer, "analyzer_version", "")
        ):
            raise DomainRuleError("product_identity_observation_provider_disabled")
        self._session = session
        self._content_store = content_store
        self._analyzer = analyzer
        self._model = model
        self._observer_version = analyzer.analyzer_version
        self._maximum_usd = cost
        self._provider_enabled = provider_enabled

    def preview(
        self, customer_id: UUID, asset_id: UUID, card_version_id: UUID
    ) -> ProductIdentityObservationPreview:
        try:
            prepared = self._prepare(customer_id, asset_id, card_version_id)
        except DomainRuleError as error:
            if not self._provider_enabled:
                raise DomainRuleError(
                    "product_identity_observation_provider_disabled"
                ) from error
            raise
        local_match = self._is_local_exact_match(prepared)
        if not local_match and not self._provider_enabled:
            raise DomainRuleError("product_identity_observation_provider_disabled")
        return ProductIdentityObservationPreview(
            asset_id=asset_id,
            product_id=prepared.asset.product_id,
            source_content_sha256=prepared.asset.content_sha256,
            product_identity_card_version_id=prepared.card.id,
            product_identity_card_sha256=prepared.card.card_sha256,
            model=(self._model if not local_match else _LOCAL_EXACT_MATCH_MODEL),
            observer_version=(
                self._observer_version
                if not local_match
                else _LOCAL_EXACT_MATCH_VERSION
            ),
            policy_version=IDENTITY_GATE_POLICY_VERSION,
            maximum_usd=(self._maximum_usd if not local_match else _ZERO_USD),
            currency="USD",
            input_image_count=(1 if not local_match else 0),
            manifest_sha256=prepared.manifest_sha256,
        )

    def execute(
        self,
        customer_id: UUID,
        asset_id: UUID,
        card_version_id: UUID,
        *,
        idempotency_key: str,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> ProductIdentityObservationOutcome:
        existing = self._by_key(customer_id, idempotency_key)
        if existing is not None:
            prepared = self._validate_existing_request(
                customer_id,
                asset_id,
                card_version_id,
                existing,
                confirmed_maximum_usd,
                confirmed_manifest_sha256,
            )
            if not self._is_resumable_reservation(existing):
                return self._outcome(existing, created=False)
            if (
                not self._is_local_exact_match(prepared)
                and not self._provider_enabled
            ):
                raise DomainRuleError("product_identity_observation_provider_disabled")
            run = existing
            created = False
        else:
            try:
                prepared = self._prepare(customer_id, asset_id, card_version_id)
            except DomainRuleError as error:
                if not self._provider_enabled:
                    raise DomainRuleError(
                        "product_identity_observation_provider_disabled"
                    ) from error
                raise
            local_match = self._is_local_exact_match(prepared)
            if not local_match and not self._provider_enabled:
                raise DomainRuleError("product_identity_observation_provider_disabled")
            self._require_confirmation(
                prepared, confirmed_maximum_usd, confirmed_manifest_sha256
            )
            observation = (
                normalize_identity_draft(prepared.card_payload) if local_match else None
            )
            reason_codes = ["card_source_exact_match"] if local_match else None
            result_sha256 = (
                _canonical_sha256(
                    {
                        "observation": identity_card_payload(observation),
                        "decision": "pass",
                        "reason_codes": reason_codes,
                        "policy_version": IDENTITY_GATE_POLICY_VERSION,
                    }
                )
                if observation is not None
                else None
            )
            run = ProductIdentityObservationRun(
                customer_id=customer_id,
                product_asset_id=asset_id,
                product_id=prepared.asset.product_id,
                source_content_sha256=prepared.asset.content_sha256,
                product_identity_card_version_id=prepared.card.id,
                product_identity_card_sha256=prepared.card.card_sha256,
                idempotency_key=idempotency_key,
                observer=(self._model if not local_match else _LOCAL_EXACT_MATCH_MODEL),
                observer_version=(
                    self._observer_version
                    if not local_match
                    else _LOCAL_EXACT_MATCH_VERSION
                ),
                observation_mode=(
                    "real_product_identity_proposal"
                    if not local_match
                    else "local_exact_source_match"
                ),
                policy_version=IDENTITY_GATE_POLICY_VERSION,
                authorized_usd=(self._maximum_usd if not local_match else _ZERO_USD),
                input_manifest_sha256=prepared.manifest_sha256,
                input_manifest=prepared.manifest,
                dispatch_state=("reserved" if not local_match else "locally_verified"),
                outbound_attempts=0,
                observation_payload=(
                    identity_card_payload(observation)
                    if observation is not None
                    else None
                ),
                decision=("pass" if local_match else None),
                reason_codes=reason_codes,
                result_sha256=result_sha256,
            )
            self._session.add(run)
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                concurrent = self._by_key(customer_id, idempotency_key)
                if concurrent is None:
                    raise
                prepared = self._validate_existing_request(
                    customer_id,
                    asset_id,
                    card_version_id,
                    concurrent,
                    confirmed_maximum_usd,
                    confirmed_manifest_sha256,
                )
                if not self._is_resumable_reservation(concurrent):
                    return self._outcome(concurrent, created=False)
                run = concurrent
                created = False
            else:
                self._session.refresh(run)
                created = True
                if local_match:
                    return self._outcome(run, created=True)
        with coordinate_style_consent(customer_id):
            self._session.refresh(run)
            if not self._is_resumable_reservation(run):
                return self._outcome(run, created=False)
            current = self._prepare(customer_id, asset_id, card_version_id)
            if not self._same_input(run, current):
                run.failure_code = "product_identity_observation_manifest_changed"
                self._session.commit()
                raise DomainRuleError("product_identity_observation_manifest_changed")
            claimed = self._session.execute(
                update(ProductIdentityObservationRun)
                .where(
                    ProductIdentityObservationRun.id == run.id,
                    ProductIdentityObservationRun.dispatch_state == "reserved",
                    ProductIdentityObservationRun.outbound_attempts == 0,
                    ProductIdentityObservationRun.failure_code.is_(None),
                )
                .values(dispatch_state="dispatch_uncertain", outbound_attempts=1)
                .execution_options(synchronize_session=False)
            )
            self._session.commit()
            self._session.refresh(run)
            if claimed.rowcount != 1:  # type: ignore[attr-defined]
                return self._outcome(run, created=False)
            try:
                observation = normalize_identity_draft(
                    identity_card_payload(self._analyzer.analyze(current.image))
                )
                refreshed = self._prepare(customer_id, asset_id, card_version_id)
                if not self._same_input(run, refreshed):
                    raise DomainRuleError("product_identity_observation_manifest_changed")
            except Exception as error:
                self._persist_failure(run.id, error)
                raise DomainRuleError("product_identity_observation_outcome_uncertain") from error
        card = normalize_identity_draft(prepared.card_payload)
        gate = compare_product_identity(card, observation)
        result_payload: dict[str, object] = {
            "observation": identity_card_payload(observation),
            "decision": gate.decision,
            "reason_codes": list(gate.reason_codes),
            "policy_version": gate.policy_version,
        }
        result_sha256 = _canonical_sha256(result_payload)
        run.observation_payload = identity_card_payload(observation)
        run.decision = gate.decision
        run.reason_codes = list(gate.reason_codes)
        run.result_sha256 = result_sha256
        run.dispatch_state = "provider_responded"
        run.failure_code = None
        self._session.commit()
        return ProductIdentityObservationOutcome(
            run.id, observation, gate.decision, gate.reason_codes, result_sha256, created
        )

    def current(
        self, customer_id: UUID, asset_id: UUID, card_version_id: UUID
    ) -> ProductIdentityObservationOutcome | None:
        try:
            prepared = self._prepare(customer_id, asset_id, card_version_id)
        except DomainRuleError:
            return None
        row = self._session.scalar(
            select(ProductIdentityObservationRun)
            .where(
                ProductIdentityObservationRun.customer_id == customer_id,
                ProductIdentityObservationRun.product_asset_id == asset_id,
                ProductIdentityObservationRun.product_identity_card_version_id == card_version_id,
                ProductIdentityObservationRun.dispatch_state.in_(
                    ("provider_responded", "locally_verified")
                ),
                ProductIdentityObservationRun.failure_code.is_(None),
            )
            .order_by(ProductIdentityObservationRun.created_at.desc())
        )
        if row is None or not self._same_input(row, prepared):
            return None
        try:
            return self._outcome(row, created=False)
        except DomainRuleError:
            return None

    def review(
        self,
        customer_id: UUID,
        observation_run_id: UUID,
        *,
        decision: str,
        reason: str,
        confirmed: bool,
    ) -> ProductIdentityObservationReviewOutcome:
        normalized_reason = reason.strip()
        if (
            confirmed is not True
            or decision not in {"approved", "rejected"}
            or not 1 <= len(normalized_reason) <= 500
        ):
            raise DomainRuleError("product_identity_observation_review_confirmation_required")
        run = self._session.scalar(
            select(ProductIdentityObservationRun).where(
                ProductIdentityObservationRun.customer_id == customer_id,
                ProductIdentityObservationRun.id == observation_run_id,
            )
        )
        if run is None:
            raise DomainRuleError("product_identity_observation_review_invalid")
        existing = self._review_for_run(customer_id, observation_run_id)
        if existing is not None:
            return self._resolve_review_replay(existing, decision, normalized_reason)
        if run.decision == "block":
            raise DomainRuleError("product_identity_observation_blocked")
        if run.decision != "needs_review":
            raise DomainRuleError("product_identity_observation_review_invalid")
        self._validated_run_scope(
            customer_id,
            run.product_id,
            run.product_asset_id,
            run.product_identity_card_version_id,
            run,
        )
        review = ProductIdentityObservationReview(
            customer_id=customer_id,
            observation_run_id=run.id,
            product_asset_id=run.product_asset_id,
            product_id=run.product_id,
            source_content_sha256=run.source_content_sha256,
            product_identity_card_version_id=run.product_identity_card_version_id,
            product_identity_card_sha256=run.product_identity_card_sha256,
            observation_result_sha256=run.result_sha256,
            decision=decision,
            reason=normalized_reason,
        )
        self._session.add(review)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            concurrent = self._review_for_run(customer_id, observation_run_id)
            if concurrent is None:
                raise
            return self._resolve_review_replay(concurrent, decision, normalized_reason)
        self._session.refresh(review)
        return self._review_outcome(review, created=True)

    def current_review(
        self, customer_id: UUID, observation_run_id: UUID
    ) -> ProductIdentityObservationReviewOutcome | None:
        """Restore the append-only human ruling for one authorized observation."""
        review = self._review_for_run(customer_id, observation_run_id)
        return self._review_outcome(review, created=False) if review is not None else None

    def validate_generation_evidence(
        self,
        customer_id: UUID,
        product_id: str,
        asset_id: UUID,
        card_version_id: UUID,
        observation_run_id: UUID,
        review_id: UUID | None = None,
    ) -> ValidatedProductIdentityEvidence:
        run = self._session.scalar(
            select(ProductIdentityObservationRun).where(
                ProductIdentityObservationRun.customer_id == customer_id,
                ProductIdentityObservationRun.id == observation_run_id,
            )
        )
        if run is None:
            raise DomainRuleError("product_identity_observation_evidence_invalid")
        outcome = self._validated_run_scope(
            customer_id, product_id, asset_id, card_version_id, run
        )
        if run.decision == "block":
            raise DomainRuleError("product_identity_observation_blocked")
        if run.decision == "pass":
            if review_id is not None:
                raise DomainRuleError("product_identity_observation_review_invalid")
            return ValidatedProductIdentityEvidence(
                run.id, outcome.result_sha256, None
            )
        if review_id is None:
            raise DomainRuleError("product_identity_observation_review_required")
        review = self._session.scalar(
            select(ProductIdentityObservationReview).where(
                ProductIdentityObservationReview.customer_id == customer_id,
                ProductIdentityObservationReview.id == review_id,
                ProductIdentityObservationReview.observation_run_id == run.id,
                ProductIdentityObservationReview.observation_result_sha256
                == run.result_sha256,
            )
        )
        if review is None or review.decision != "approved":
            raise DomainRuleError("product_identity_observation_not_approved")
        return ValidatedProductIdentityEvidence(
            run.id, outcome.result_sha256, review.id
        )

    def _prepare(self, customer_id: UUID, asset_id: UUID, card_version_id: UUID) -> _PreparedObservation:
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.id == asset_id,
                ProductAsset.confirmed.is_(True),
                ProductAsset.identity == "original_real_photo",
            )
        )
        if asset is None:
            raise DomainRuleError("product_identity_observation_asset_unavailable")
        _require_transform_permission(self._session, customer_id, asset.id)
        card, card_payload = validate_product_identity_card_for_product(
            self._session,
            self._content_store,
            customer_id,
            asset.product_id,
            card_version_id,
        )
        try:
            content = self._content_store.read_original(customer_id, asset.id, asset.content_sha256)
        except AssetContentStoreError as error:
            raise DomainRuleError("product_identity_observation_asset_unavailable") from error
        if sha256(content).hexdigest() != asset.content_sha256:
            raise DomainRuleError("product_identity_observation_asset_unavailable")
        manifest: dict[str, object] = {
            "asset_id": str(asset.id),
            "product_id": asset.product_id,
            "source_content_sha256": asset.content_sha256,
            "product_identity_card_version_id": str(card.id),
            "product_identity_card_sha256": card.card_sha256,
            "model": (
                self._model
                if asset.content_sha256 != card.source_content_sha256
                else _LOCAL_EXACT_MATCH_MODEL
            ),
            "observer_version": (
                self._observer_version
                if asset.content_sha256 != card.source_content_sha256
                else _LOCAL_EXACT_MATCH_VERSION
            ),
            "policy_version": IDENTITY_GATE_POLICY_VERSION,
            "input_image_count": (
                1 if asset.content_sha256 != card.source_content_sha256 else 0
            ),
        }
        return _PreparedObservation(
            asset=asset,
            card=card,
            card_payload=card_payload,
            image=ProductIdentityImage(asset.id, asset.product_id, content, asset.content_sha256, asset.mime_type),
            manifest=manifest,
            manifest_sha256=_canonical_sha256(manifest),
        )

    def _validated_run_scope(
        self,
        customer_id: UUID,
        product_id: str,
        asset_id: UUID,
        card_version_id: UUID,
        run: ProductIdentityObservationRun,
    ) -> ProductIdentityObservationOutcome:
        if (
            run.product_id != product_id
            or run.product_asset_id != asset_id
            or run.product_identity_card_version_id != card_version_id
        ):
            raise DomainRuleError("product_identity_observation_evidence_invalid")
        prepared = self._prepare(customer_id, asset_id, card_version_id)
        if not self._same_input(run, prepared):
            raise DomainRuleError("product_identity_observation_evidence_invalid")
        try:
            return self._outcome(run, created=False)
        except DomainRuleError as error:
            raise DomainRuleError("product_identity_observation_evidence_invalid") from error

    def _review_for_run(
        self, customer_id: UUID, observation_run_id: UUID
    ) -> ProductIdentityObservationReview | None:
        return self._session.scalar(
            select(ProductIdentityObservationReview).where(
                ProductIdentityObservationReview.customer_id == customer_id,
                ProductIdentityObservationReview.observation_run_id
                == observation_run_id,
            )
        )

    def _resolve_review_replay(
        self,
        review: ProductIdentityObservationReview,
        decision: str,
        reason: str,
    ) -> ProductIdentityObservationReviewOutcome:
        if review.decision != decision or review.reason != reason:
            raise DomainRuleError("product_identity_observation_review_conflict")
        return self._review_outcome(review, created=False)

    @staticmethod
    def _review_outcome(
        review: ProductIdentityObservationReview, *, created: bool
    ) -> ProductIdentityObservationReviewOutcome:
        return ProductIdentityObservationReviewOutcome(
            review.id,
            review.observation_run_id,
            review.decision,
            review.reason,
            created,
        )

    def _replay_existing(
        self,
        customer_id: UUID,
        asset_id: UUID,
        card_version_id: UUID,
        run: ProductIdentityObservationRun,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> ProductIdentityObservationOutcome:
        self._validate_existing_request(
            customer_id,
            asset_id,
            card_version_id,
            run,
            confirmed_maximum_usd,
            confirmed_manifest_sha256,
        )
        return self._outcome(run, created=False)

    def _validate_existing_request(
        self,
        customer_id: UUID,
        asset_id: UUID,
        card_version_id: UUID,
        run: ProductIdentityObservationRun,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> _PreparedObservation:
        if run.product_asset_id != asset_id or run.product_identity_card_version_id != card_version_id:
            raise DomainRuleError("product_identity_observation_scope_mismatch")
        prepared = self._prepare(customer_id, asset_id, card_version_id)
        self._require_confirmation(prepared, confirmed_maximum_usd, confirmed_manifest_sha256)
        if run.authorized_usd != self._cost_for(prepared) or not self._same_input(run, prepared):
            raise DomainRuleError("product_identity_observation_confirmation_required")
        return prepared

    @staticmethod
    def _is_resumable_reservation(run: ProductIdentityObservationRun) -> bool:
        return (
            run.dispatch_state == "reserved"
            and run.outbound_attempts == 0
            and run.failure_code is None
            and run.observation_mode == "real_product_identity_proposal"
            and run.observation_payload is None
            and run.decision is None
            and run.reason_codes is None
            and run.result_sha256 is None
        )

    def _require_confirmation(self, prepared: _PreparedObservation, cost: Decimal, manifest: str) -> None:
        if cost != self._cost_for(prepared) or manifest != prepared.manifest_sha256:
            raise DomainRuleError("product_identity_observation_confirmation_required")

    @staticmethod
    def _is_local_exact_match(prepared: _PreparedObservation) -> bool:
        return prepared.asset.content_sha256 == prepared.card.source_content_sha256

    def _cost_for(self, prepared: _PreparedObservation) -> Decimal:
        return _ZERO_USD if self._is_local_exact_match(prepared) else self._maximum_usd

    def _same_input(self, run: ProductIdentityObservationRun, prepared: _PreparedObservation) -> bool:
        return (
            run.product_asset_id == prepared.asset.id
            and run.product_id == prepared.asset.product_id
            and run.source_content_sha256 == prepared.asset.content_sha256
            and run.product_identity_card_version_id == prepared.card.id
            and run.product_identity_card_sha256 == prepared.card.card_sha256
            and run.observer == prepared.manifest["model"]
            and run.observer_version == prepared.manifest["observer_version"]
            and run.policy_version == IDENTITY_GATE_POLICY_VERSION
            and run.input_manifest_sha256 == prepared.manifest_sha256
            and run.input_manifest == prepared.manifest
        )

    def _outcome(self, run: ProductIdentityObservationRun, *, created: bool) -> ProductIdentityObservationOutcome:
        provider_completed = (
            run.dispatch_state == "provider_responded" and run.outbound_attempts == 1
        )
        local_completed = (
            run.dispatch_state == "locally_verified"
            and run.outbound_attempts == 0
            and run.authorized_usd == 0
            and run.observation_mode == "local_exact_source_match"
            and run.observer == _LOCAL_EXACT_MATCH_MODEL
            and run.observer_version == _LOCAL_EXACT_MATCH_VERSION
        )
        if (
            not (provider_completed or local_completed)
            or run.failure_code is not None
            or run.observation_payload is None
            or run.decision not in {"pass", "needs_review", "block"}
            or run.reason_codes is None
            or run.result_sha256 is None
        ):
            raise DomainRuleError("product_identity_observation_outcome_uncertain")
        observation = normalize_identity_draft(run.observation_payload)
        expected = _canonical_sha256(
            {
                "observation": identity_card_payload(observation),
                "decision": run.decision,
                "reason_codes": run.reason_codes,
                "policy_version": run.policy_version,
            }
        )
        if expected != run.result_sha256:
            raise DomainRuleError("product_identity_observation_outcome_uncertain")
        return ProductIdentityObservationOutcome(
            run.id,
            observation,
            run.decision,
            tuple(run.reason_codes),
            run.result_sha256,
            created,
        )

    def _by_key(self, customer_id: UUID, key: str) -> ProductIdentityObservationRun | None:
        return self._session.scalar(
            select(ProductIdentityObservationRun).where(
                ProductIdentityObservationRun.customer_id == customer_id,
                ProductIdentityObservationRun.idempotency_key == key,
            )
        )

    def _persist_failure(self, run_id: UUID, error: Exception) -> None:
        self._session.rollback()
        run = self._session.get(ProductIdentityObservationRun, run_id)
        if run is None:
            return
        code = getattr(error, "code", "product_identity_observation_unavailable")
        run.failure_code = code if isinstance(code, str) and 0 < len(code) <= 100 else "product_identity_observation_unavailable"
        self._session.commit()


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_transform_permission(
    session: Session, customer_id: UUID, asset_id: UUID
) -> None:
    permission = session.scalar(
        select(ProductAssetPermissionEvent)
        .where(
            ProductAssetPermissionEvent.customer_id == customer_id,
            ProductAssetPermissionEvent.product_asset_id == asset_id,
        )
        .order_by(ProductAssetPermissionEvent.occurred_at.desc())
        .limit(1)
    )
    if permission is None:
        raise DomainRuleError("transform_permission_unknown")
    if not permission.transform_permission:
        raise DomainRuleError("transform_permission_denied")


__all__ = [
    "ProductIdentityObservationOutcome",
    "ProductIdentityObservationPreview",
    "ProductIdentityObservationReviewOutcome",
    "ProductIdentityObservationService",
    "ValidatedProductIdentityEvidence",
    "validate_product_identity_card_for_product",
    "validate_product_identity_generation_evidence",
]
