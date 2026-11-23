"""Controlled, durable execution boundary for paid visual style analysis."""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.analyzer import (
    StyleGroupProposal,
    VisualStyleAnalyzer,
    validate_visual_proposals,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.models import RealStyleAnalysisRun
from opc_style.services.real_photos import AssetContentStore
from opc_style.services.style_analysis import (
    StyleAnalysisProposal,
    StyleAnalysisService,
    build_style_analysis_proposal,
)
from opc_style.services.style_consent import coordinate_style_consent


@dataclass(frozen=True)
class RealStyleAnalysisPreview:
    library_photo_count: int
    selected_photo_count: int
    model: str
    maximum_usd: Decimal
    currency: str
    reference_selection_version: str
    reference_manifest_sha256: str
    publishable: bool = False


@dataclass(frozen=True)
class RealStyleAnalysisOutcome:
    run_id: UUID
    proposal: StyleAnalysisProposal
    created: bool


class RealStyleAnalysisService:
    """Reserve one exact paid scope before at-most-once provider dispatch."""

    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore,
        analyzer: VisualStyleAnalyzer,
        model: str,
        maximum_usd: Decimal,
    ) -> None:
        if (
            maximum_usd <= 0
            or getattr(analyzer, "analysis_mode", "")
            != "real_visual_analysis_proposal"
        ):
            raise DomainRuleError("real_style_analysis_provider_disabled")
        self._session = session
        self._content_store = content_store
        self._analyzer = analyzer
        self._model = model
        self._maximum_usd = maximum_usd

    def _analysis(self) -> StyleAnalysisService:
        return StyleAnalysisService(
            self._session,
            content_store=self._content_store,
            analyzer=self._analyzer,
        )

    def preview(self, customer_id: UUID) -> RealStyleAnalysisPreview:
        selection = self._analysis().verified_selection(customer_id)
        if len(selection.library_sample_ids) < 10 or len(selection.selected) < 4:
            raise DomainRuleError("real_style_analysis_insufficient_photos")
        return RealStyleAnalysisPreview(
            library_photo_count=len(selection.library_sample_ids),
            selected_photo_count=len(selection.selected),
            model=self._model,
            maximum_usd=self._maximum_usd,
            currency="USD",
            reference_selection_version=selection.algorithm_version,
            reference_manifest_sha256=selection.manifest_sha256,
        )

    def execute(
        self,
        customer_id: UUID,
        *,
        idempotency_key: str,
        confirmed_maximum_usd: Decimal,
        confirmed_reference_manifest_sha256: str,
    ) -> RealStyleAnalysisOutcome:
        existing = self._session.scalar(
            select(RealStyleAnalysisRun).where(
                RealStyleAnalysisRun.customer_id == customer_id,
                RealStyleAnalysisRun.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return self._replay(existing)
        preview = self.preview(customer_id)
        if (
            confirmed_maximum_usd != preview.maximum_usd
            or confirmed_reference_manifest_sha256
            != preview.reference_manifest_sha256
        ):
            raise DomainRuleError("real_style_analysis_confirmation_required")
        selection = self._analysis().verified_selection(customer_id)
        run = RealStyleAnalysisRun(
            customer_id=customer_id,
            idempotency_key=idempotency_key,
            model=self._model,
            authorized_usd=self._maximum_usd,
            reference_manifest_sha256=selection.manifest_sha256,
            reference_selection_version=selection.algorithm_version,
            library_sample_ids=[str(value) for value in selection.library_sample_ids],
            selected_sample_ids=[str(value.sample_id) for value in selection.selected],
            dispatch_state="reserved",
            outbound_attempts=0,
        )
        self._session.add(run)
        self._session.commit()
        self._session.refresh(run)

        with coordinate_style_consent(customer_id):
            current = self._analysis().verified_selection(customer_id)
            if (
                current.manifest_sha256 != run.reference_manifest_sha256
                or [str(value) for value in current.library_sample_ids]
                != run.library_sample_ids
                or [str(value.sample_id) for value in current.selected]
                != run.selected_sample_ids
            ):
                raise DomainRuleError("style_samples_changed")
            # Persist uncertainty before bytes can cross the provider boundary.
            run.dispatch_state = "dispatch_uncertain"
            run.outbound_attempts = 1
            self._session.commit()
            try:
                groups = validate_visual_proposals(
                    self._analyzer.analyze(current.selected),
                    frozenset(value.sample_id for value in current.selected),
                )
                if [
                    str(row.id) for row in self._analysis()._eligible(customer_id)
                ] != run.library_sample_ids:
                    raise DomainRuleError("style_samples_changed")
                proposal = build_style_analysis_proposal(
                    current, groups, self._analyzer
                )
            except Exception as error:
                failure_code = getattr(
                    error, "code", "style_visual_analysis_unavailable"
                )
                run.failure_code = failure_code
                if failure_code == "style_visual_analysis_budget_exhausted":
                    # The adapter rejects this before opening the HTTP request. Keep
                    # the durable ledger honest so operators can distinguish a local
                    # fuse from a provider dispatch.
                    run.dispatch_state = "reserved"
                    run.outbound_attempts = 0
                    self._session.commit()
                    raise DomainRuleError(
                        "real_style_analysis_budget_exhausted"
                    ) from error
                self._session.commit()
                raise DomainRuleError("real_style_analysis_outcome_uncertain") from error
        run.proposal = _serialize_proposal(proposal)
        run.dispatch_state = "provider_responded"
        self._session.commit()
        return RealStyleAnalysisOutcome(run.id, proposal, True)

    def _replay(self, run: RealStyleAnalysisRun) -> RealStyleAnalysisOutcome:
        if (
            run.dispatch_state == "reserved"
            and run.outbound_attempts == 0
            and run.failure_code == "style_visual_analysis_budget_exhausted"
        ):
            raise DomainRuleError(
                "real_style_analysis_budget_exhausted"
            )
        if run.dispatch_state != "provider_responded" or run.proposal is None:
            raise DomainRuleError("real_style_analysis_outcome_uncertain")
        return RealStyleAnalysisOutcome(run.id, _deserialize_proposal(run.proposal), False)


def latest_successful_real_style_analysis(
    session: Session, customer_id: UUID
) -> RealStyleAnalysisOutcome | None:
    """Recover the newest completed proposal without provider access or spending."""
    run = session.scalar(
        select(RealStyleAnalysisRun)
        .where(
            RealStyleAnalysisRun.customer_id == customer_id,
            RealStyleAnalysisRun.dispatch_state == "provider_responded",
            RealStyleAnalysisRun.proposal.is_not(None),
        )
        .order_by(
            RealStyleAnalysisRun.created_at.desc(),
            RealStyleAnalysisRun.id.desc(),
        )
        .limit(1)
    )
    if run is None or run.proposal is None:
        return None
    return RealStyleAnalysisOutcome(
        run.id,
        _deserialize_proposal(run.proposal),
        False,
    )

def _serialize_proposal(proposal: StyleAnalysisProposal) -> dict[str, Any]:
    payload = asdict(proposal)
    payload["library_sample_ids"] = [str(value) for value in proposal.library_sample_ids]
    for group in payload["groups"]:
        group["evidence_sample_ids"] = [
            str(value) for value in group["evidence_sample_ids"]
        ]
    return payload


def _deserialize_proposal(payload: dict[str, Any]) -> StyleAnalysisProposal:
    groups = tuple(
        StyleGroupProposal(
            proposal_key=group["proposal_key"],
            suggested_name=group["suggested_name"],
            evidence_sample_ids=tuple(
                UUID(value) for value in group["evidence_sample_ids"]
            ),
            observations={
                key: tuple(values)
                for key, values in group["observations"].items()
            },
            suggested_slots=tuple(group["suggested_slots"]),
        )
        for group in payload["groups"]
    )
    return StyleAnalysisProposal(
        groups=groups,
        analyzer_version=payload["analyzer_version"],
        analysis_mode=payload["analysis_mode"],
        proposal_fingerprint=payload["proposal_fingerprint"],
        library_sample_ids=tuple(UUID(value) for value in payload["library_sample_ids"]),
        reference_selection_version=payload["reference_selection_version"],
        reference_manifest_sha256=payload["reference_manifest_sha256"],
        authoritative=payload.get("authoritative", False),
    )
