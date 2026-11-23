"""Durable, explicitly authorized product-fidelity execution boundary."""

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.authenticity import (
    FidelityAssessment,
    FidelityBatchAssessor,
    FidelityBatchRequest,
    FidelityCandidateInput,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import normalize_identity_draft
from opc_style.models import (
    ImageCandidate,
    ProductAsset,
    RealFidelityRun,
    StyleSuiteCandidateFidelity,
    StyleSuiteRun,
    StyleSuiteRunItem,
)
from opc_style.services.product_identity import (
    validate_confirmed_product_identity_card,
)
from opc_style.services.real_photos import AssetContentStore


@dataclass(frozen=True)
class RealFidelityPreview:
    model: str
    maximum_usd: Decimal
    currency: str
    image_count: int
    candidate_count: int
    input_manifest_sha256: str
    attempt_state: Literal[
        "not_started", "reserved", "dispatch_uncertain", "provider_responded"
    ]
    publishable: bool = False


@dataclass(frozen=True)
class RealFidelityOutcome:
    run_id: UUID
    created: bool


@dataclass(frozen=True)
class _PreparedBatch:
    suite: StyleSuiteRun
    candidate_items: tuple[StyleSuiteRunItem, ...]
    request: FidelityBatchRequest
    manifest: list[dict[str, object]]
    manifest_sha256: str


class RealFidelityService:
    """Quote exact inputs, then permit at most one external batch comparison."""

    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore,
        assessor: FidelityBatchAssessor,
        model: str,
        maximum_usd: Decimal,
    ) -> None:
        if maximum_usd <= 0 or not getattr(assessor, "assessor_version", ""):
            raise DomainRuleError("real_fidelity_provider_disabled")
        self._session = session
        self._content_store = content_store
        self._assessor = assessor
        self._model = model
        self._maximum_usd = maximum_usd

    def preview(self, customer_id: UUID, suite_run_id: UUID) -> RealFidelityPreview:
        prepared = self._prepare(customer_id, suite_run_id)
        existing = self._existing_suite_attempt(customer_id, suite_run_id)
        return RealFidelityPreview(
            model=self._model,
            maximum_usd=self._maximum_usd,
            currency="USD",
            image_count=1 + len(prepared.request.candidates),
            candidate_count=len(prepared.request.candidates),
            input_manifest_sha256=prepared.manifest_sha256,
            attempt_state=cast(
                Literal[
                    "not_started",
                    "reserved",
                    "dispatch_uncertain",
                    "provider_responded",
                ],
                existing.dispatch_state if existing is not None else "not_started",
            ),
        )

    def execute(
        self,
        customer_id: UUID,
        suite_run_id: UUID,
        *,
        idempotency_key: str,
        confirmed_maximum_usd: Decimal,
        confirmed_manifest_sha256: str,
    ) -> RealFidelityOutcome:
        existing_by_key = self._session.scalar(
            select(RealFidelityRun).where(
                RealFidelityRun.customer_id == customer_id,
                RealFidelityRun.idempotency_key == idempotency_key,
            )
        )
        existing_for_suite = self._existing_suite_attempt(customer_id, suite_run_id)
        if (
            existing_by_key is not None
            and existing_for_suite is not None
            and existing_by_key.id != existing_for_suite.id
        ):
            raise DomainRuleError("real_fidelity_outcome_uncertain")
        existing = existing_for_suite or existing_by_key
        if existing is not None:
            prepared = self._prepare(customer_id, suite_run_id)
            if (
                existing.style_suite_run_id == suite_run_id
                and existing.dispatch_state == "provider_responded"
                and existing.assessment_json is not None
                and existing.input_manifest_sha256 == prepared.manifest_sha256
                and existing.input_manifest == prepared.manifest
            ):
                return RealFidelityOutcome(existing.id, False)
            raise DomainRuleError("real_fidelity_outcome_uncertain")

        prepared = self._prepare(customer_id, suite_run_id)
        if (
            confirmed_maximum_usd != self._maximum_usd
            or confirmed_manifest_sha256 != prepared.manifest_sha256
        ):
            raise DomainRuleError("real_fidelity_confirmation_required")

        run = RealFidelityRun(
            customer_id=customer_id,
            style_suite_run_id=suite_run_id,
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
        except IntegrityError as error:
            self._session.rollback()
            raise DomainRuleError("real_fidelity_outcome_uncertain") from error
        self._session.refresh(run)

        current = self._prepare(customer_id, suite_run_id)
        if (
            current.manifest_sha256 != run.input_manifest_sha256
            or current.manifest != run.input_manifest
        ):
            raise DomainRuleError("real_fidelity_inputs_changed")
        run.dispatch_state = "dispatch_uncertain"
        run.outbound_attempts = 1
        self._session.commit()
        try:
            batch = self._assessor.assess_batch(current.request)
            expected_ids = tuple(
                candidate.candidate_id for candidate in current.request.candidates
            )
            if (
                batch.assessor_version != self._assessor.assessor_version
                or tuple(row.candidate_id for row in batch.assessments) != expected_ids
            ):
                raise DomainRuleError("fidelity_provider_response_invalid")
            refreshed = self._prepare(customer_id, suite_run_id)
            if refreshed.manifest_sha256 != run.input_manifest_sha256:
                raise DomainRuleError("real_fidelity_inputs_changed")
            self._persist_assessments(current, batch.assessments)
        except Exception as error:
            self._session.rollback()
            persisted_run = self._session.get(RealFidelityRun, run.id)
            if persisted_run is None:
                raise DomainRuleError("real_fidelity_outcome_uncertain") from error
            run = persisted_run
            run.failure_code = getattr(error, "code", "fidelity_provider_unavailable")
            self._session.commit()
            raise DomainRuleError("real_fidelity_outcome_uncertain") from error

        run.assessment_json = [
            {
                "candidate_id": str(row.candidate_id),
                "state": row.state,
                "flags": list(row.flags),
                "summary_zh": row.summary_zh,
                "assessor_version": row.assessor_version,
                "comparisons": dict(row.comparisons),
            }
            for row in batch.assessments
        ]
        run.dispatch_state = "provider_responded"
        self._session.commit()
        return RealFidelityOutcome(run.id, True)

    def _existing_suite_attempt(
        self, customer_id: UUID, suite_run_id: UUID
    ) -> RealFidelityRun | None:
        return self._session.scalar(
            select(RealFidelityRun).where(
                RealFidelityRun.customer_id == customer_id,
                RealFidelityRun.style_suite_run_id == suite_run_id,
            )
        )

    def _prepare(self, customer_id: UUID, suite_run_id: UUID) -> _PreparedBatch:
        suite = self._session.scalar(
            select(StyleSuiteRun).where(
                StyleSuiteRun.customer_id == customer_id,
                StyleSuiteRun.id == suite_run_id,
            )
        )
        if suite is None:
            raise DomainRuleError("style_suite_not_found")
        if suite.product_identity_card_version_id is None:
            raise DomainRuleError("confirmed_product_identity_card_required")
        if suite.product_identity_card_sha256 is None:
            raise DomainRuleError("product_identity_card_scope_mismatch")
        try:
            card, card_payload = validate_confirmed_product_identity_card(
                self._session,
                self._content_store,
                customer_id,
                suite.product_id,
                suite.source_asset_id,
                suite.product_identity_card_version_id,
            )
            identity_card = normalize_identity_draft(card_payload)
        except DomainRuleError as error:
            raise DomainRuleError("product_identity_card_scope_mismatch") from error
        if (
            card.card_sha256 != suite.product_identity_card_sha256
            or card.source_content_sha256 != suite.source_content_sha256
        ):
            raise DomainRuleError("product_identity_card_scope_mismatch")
        items = list(
            self._session.scalars(
                select(StyleSuiteRunItem).where(
                    StyleSuiteRunItem.customer_id == customer_id,
                    StyleSuiteRunItem.run_id == suite_run_id,
                ).order_by(StyleSuiteRunItem.created_at, StyleSuiteRunItem.id)
            )
        )
        if not items or any(item.output_job_id is None for item in items):
            raise DomainRuleError("real_fidelity_requires_single_suite_slot")
        candidate_rows: list[tuple[ImageCandidate, StyleSuiteRunItem]] = []
        for item in items:
            candidates = list(
                self._session.scalars(
                    select(ImageCandidate)
                    .where(
                        ImageCandidate.customer_id == customer_id,
                        ImageCandidate.output_job_id == item.output_job_id,
                    )
                    .order_by(ImageCandidate.sequence_number)
                )
            )
            candidate_rows.extend((candidate, item) for candidate in candidates)
        if not 1 <= len(candidate_rows) <= 3:
            raise DomainRuleError("real_fidelity_candidate_count_invalid")
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.id == suite.source_asset_id,
                ProductAsset.content_sha256 == suite.source_content_sha256,
            )
        )
        if asset is None:
            raise DomainRuleError("real_fidelity_product_unavailable")
        product_content = self._content_store.read_original(
            customer_id, asset.id, asset.content_sha256
        )
        candidate_inputs = tuple(
            FidelityCandidateInput(
                candidate_id=candidate.id,
                content=self._content_store.read_derivative(
                    customer_id,
                    candidate.output_job_id,
                    candidate.sequence_number,
                    candidate.content_sha256,
                ),
                content_sha256=candidate.content_sha256,
                mime_type=cast(
                    "Literal['image/jpeg', 'image/png']", candidate.mime_type
                ),
            )
            for candidate, _item in candidate_rows
        )
        request = FidelityBatchRequest(
            identity_card_version_id=card.id,
            identity_card=identity_card,
            product_content=product_content,
            product_sha256=asset.content_sha256,
            product_mime_type=cast(
                "Literal['image/jpeg', 'image/png']", asset.mime_type
            ),
            candidates=candidate_inputs,
        )
        manifest: list[dict[str, object]] = [
            {
                "role": "product_identity",
                "id": str(asset.id),
                "sha256": asset.content_sha256,
            }
        ]
        manifest.append(
            {
                "role": "confirmed_product_identity_card",
                "identity_card_version_id": str(card.id),
                "identity_card_sha256": card.card_sha256,
            }
        )
        manifest.extend(
            [
                {
                    "role": "candidate",
                    "id": str(candidate.candidate_id),
                    "sha256": candidate.content_sha256,
                }
                for candidate in candidate_inputs
            ]
        )
        digest = sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return _PreparedBatch(
            suite,
            tuple(item for _candidate, item in candidate_rows),
            request,
            manifest,
            digest,
        )

    def _persist_assessments(
        self,
        prepared: _PreparedBatch,
        assessments: tuple[FidelityAssessment, ...],
    ) -> None:
        for item, assessment in zip(
            prepared.candidate_items, assessments, strict=True
        ):
            if assessment.candidate_id is None:
                raise DomainRuleError("fidelity_provider_response_invalid")
            if item.output_job_id is None:
                raise DomainRuleError("real_fidelity_requires_single_suite_slot")
            self._session.add(
                StyleSuiteCandidateFidelity(
                    customer_id=item.customer_id,
                    run_item_id=item.id,
                    output_job_id=item.output_job_id,
                    image_candidate_id=assessment.candidate_id,
                    fidelity_state=assessment.state,
                    fidelity_flags=list(assessment.flags),
                    warning_acknowledged=False,
                    summary_zh=assessment.summary_zh,
                )
            )
        self._session.flush()
