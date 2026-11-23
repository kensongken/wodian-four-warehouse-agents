"""Read verified authorized pixels into non-persisted visual proposals."""

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.analyzer import (
    StyleGroupProposal,
    VisualStyleAnalyzer,
    VisualStyleSample,
    validate_visual_proposals,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_evidence import LOCAL_SYNTHETIC_CONSENT_PROVENANCES
from opc_style.domain.style_reference_selection import (
    StyleReferenceSelection,
    select_representative_style_samples,
)
from opc_style.models import StyleSample
from opc_style.services.asset_store_recovery import namespace_is_accepted
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError
from opc_style.services.style_consent import coordinate_style_consent


@dataclass(frozen=True)
class StyleAnalysisProposal:
    """Ephemeral suggestions requiring a separate customer-confirmation action."""

    groups: tuple[StyleGroupProposal, ...]
    analyzer_version: str
    analysis_mode: str
    proposal_fingerprint: str
    library_sample_ids: tuple[UUID, ...]
    reference_selection_version: str
    reference_manifest_sha256: str
    authoritative: bool = False


@dataclass(frozen=True)
class StyleLibraryStatus:
    active_sample_count: int
    available_sample_count: int
    analysis_ready: bool
    maximum_representative_count: int = 8


class StyleAnalysisService:
    def __init__(
        self,
        session: Session,
        *,
        content_store: AssetContentStore | None,
        analyzer: VisualStyleAnalyzer | None,
    ) -> None:
        self._session = session
        self._content_store = content_store
        self._analyzer = analyzer

    def _eligible(self, customer_id: UUID) -> tuple[StyleSample, ...]:
        return tuple(
            row
            for row in self._session.scalars(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.withdrawn_at.is_(None),
                    StyleSample.consent_provenance.not_in(
                        LOCAL_SYNTHETIC_CONSENT_PROVENANCES
                    ),
                )
                .order_by(StyleSample.id)
                .execution_options(populate_existing=True)
            )
            if row.has_complete_content_seal
        )

    def library_status(self, customer_id: UUID) -> StyleLibraryStatus:
        rows = self._eligible(customer_id)
        available = 0
        if self._content_store is not None:
            for row in rows:
                digest = row.content_sha256
                if (
                    digest
                    and row.asset_store_namespace is not None
                    and namespace_is_accepted(
                        self._session,
                        customer_id,
                        row.asset_store_namespace,
                        self._content_store.store_namespace,
                    )
                ):
                    try:
                        if self._content_store.verify_style_sample_content(
                            customer_id, row.id, digest
                        ):
                            available += 1
                    except AssetContentStoreError:
                        continue
        return StyleLibraryStatus(
            active_sample_count=len(rows),
            available_sample_count=available,
            analysis_ready=len(rows) >= 10 and available == len(rows),
        )

    def propose(self, customer_id: UUID) -> StyleAnalysisProposal:
        """Verify store/namespace before pixels; never create or confirm a version."""
        if self._analyzer is None:
            raise DomainRuleError("style_visual_analysis_unavailable")
        selection = self.verified_selection(customer_id)
        sample_ids = frozenset(selection.library_sample_ids)
        selected_ids = frozenset(sample.sample_id for sample in selection.selected)
        with coordinate_style_consent(customer_id):
            # Byte loading may have taken time. Refresh consent immediately before
            # dispatch while withdrawal cannot commit between this check and call.
            if frozenset(row.id for row in self._eligible(customer_id)) != sample_ids:
                raise DomainRuleError("style_samples_changed")
            groups = validate_visual_proposals(
                self._analyzer.analyze(selection.selected), selected_ids
            )
            # Retain the post-call check for same-thread/in-process mutations too.
            if frozenset(row.id for row in self._eligible(customer_id)) != sample_ids:
                raise DomainRuleError("style_samples_changed")
            return build_style_analysis_proposal(selection, groups, self._analyzer)

    def verified_selection(self, customer_id: UUID) -> StyleReferenceSelection:
        """Read and verify the representative pixels without calling an analyzer."""
        if self._content_store is None:
            raise DomainRuleError("style_visual_analysis_unavailable")
        store = self._content_store
        rows = self._eligible(customer_id)
        if not rows or len(rows) > 100:
            raise DomainRuleError("style_visual_analysis_unavailable")
        samples = []
        try:
            for row in rows:
                digest, mime = row.content_sha256, row.mime_type
                if (
                    not digest
                    or not mime
                    or row.asset_store_namespace is None
                    or not namespace_is_accepted(
                        self._session,
                        customer_id,
                        row.asset_store_namespace,
                        store.store_namespace,
                    )
                    or not store.verify_style_sample_content(
                        customer_id, row.id, digest
                    )
                ):
                    raise DomainRuleError("style_visual_analysis_unavailable")
                content = store.read_style_sample_content(customer_id, row.id, digest)
                if sha256(content).hexdigest() != digest:
                    raise DomainRuleError("style_visual_analysis_unavailable")
                samples.append(
                    VisualStyleSample(
                        row.id,
                        content,
                        digest,
                        mime,
                    )
                )
        except AssetContentStoreError as error:
            raise DomainRuleError("style_visual_analysis_unavailable") from error
        return select_representative_style_samples(tuple(samples))


def build_style_analysis_proposal(
    selection: StyleReferenceSelection,
    groups: tuple[StyleGroupProposal, ...],
    analyzer: VisualStyleAnalyzer,
) -> StyleAnalysisProposal:
    """Build the stable proposal seal shared by local and paid analysis flows."""
    analyzer_version = analyzer.analyzer_version
    analysis_mode = getattr(analyzer, "analysis_mode", "visual_analysis_proposal")
    proposal_fingerprint = sha256(
                json.dumps(
                    {
                        "analysis_mode": analysis_mode,
                        "analyzer_version": analyzer_version,
                        "library_sample_ids": [
                            str(sample_id)
                            for sample_id in selection.library_sample_ids
                        ],
                        "reference_manifest_sha256": (
                            selection.manifest_sha256
                        ),
                        "reference_selection_version": (
                            selection.algorithm_version
                        ),
                        "groups": [
                            {
                                "evidence_sample_ids": [
                                    str(sample_id)
                                    for sample_id in group.evidence_sample_ids
                                ],
                                "observations": {
                                    key: list(values)
                                    for key, values in sorted(
                                        group.observations.items()
                                    )
                                },
                                "proposal_key": group.proposal_key,
                                "suggested_name": group.suggested_name,
                                "suggested_slots": list(group.suggested_slots),
                            }
                            for group in groups
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
    return StyleAnalysisProposal(
        groups,
        analyzer_version,
        analysis_mode,
        proposal_fingerprint,
        selection.library_sample_ids,
        selection.algorithm_version,
        selection.manifest_sha256,
    )
