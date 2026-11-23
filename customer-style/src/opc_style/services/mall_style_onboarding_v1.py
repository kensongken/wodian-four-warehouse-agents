"""Merchant-owned style onboarding without generation or publication side effects."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.mall_style_onboarding_v1 import (
    MallStyleOnboardingConfirmationReceiptV1,
    MallStyleOnboardingConfirmationV1,
    MallStyleOnboardingSampleReceiptV1,
)
from opc_style.models import (
    AuditEvent,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.services.real_photos import AssetContentStore
from opc_style.services.samples import StyleSampleService

CONSENT_PROVENANCE = "mall_merchant_owner_upload"
CONSENT_TEXT_VERSION = "style-sample-consent-v1"
MINIMUM_EVIDENCE_SAMPLES = 10


class MallStyleOnboardingRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MallStyleOnboardingConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SampleImportResult:
    receipt: MallStyleOnboardingSampleReceiptV1
    created: bool


@dataclass(frozen=True)
class CandidateConfirmationResult:
    receipt: MallStyleOnboardingConfirmationReceiptV1
    created: bool


class MallStyleOnboardingService:
    """Bridge Mall owner consent into the existing sealed style catalogue."""

    def __init__(self, session: Session, store: AssetContentStore) -> None:
        self._session = session
        self._store = store

    def import_sample(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        source_asset_id: str,
        content: bytes,
        claimed_sha256: str,
        mime_type: str,
        consent_provenance: str,
        consent_text_version: str,
    ) -> SampleImportResult:
        if consent_provenance != CONSENT_PROVENANCE:
            raise MallStyleOnboardingRejected("mall_style_consent_provenance_invalid")
        if consent_text_version != CONSENT_TEXT_VERSION:
            raise MallStyleOnboardingRejected("mall_style_consent_version_invalid")
        existing = self._session.scalar(
            select(StyleSample).where(
                StyleSample.customer_id == merchant_id,
                StyleSample.photo_id == source_asset_id,
            )
        )
        selection = StyleSampleService(self._session, self._store).authorize_content(
            merchant_id,
            photo_id=source_asset_id,
            content=content,
            claimed_sha256=claimed_sha256,
            mime_type=mime_type,
            consent_provenance=consent_provenance,
            consent_text_version=consent_text_version,
        )
        return SampleImportResult(
            receipt=MallStyleOnboardingSampleReceiptV1.model_validate(
                {
                    "merchant_id": merchant_id,
                    "opc_id": opc_id,
                    "source_asset_id": source_asset_id,
                    "style_sample_id": selection.sample_id,
                    "content_sha256": selection.content_sha256,
                    "mime_type": selection.mime_type,
                }
            ),
            created=existing is None,
        )

    def confirm_candidate(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        candidate_id: UUID,
        confirmation: MallStyleOnboardingConfirmationV1,
    ) -> CandidateConfirmationResult:
        if confirmation.consent_text_version != CONSENT_TEXT_VERSION:
            raise MallStyleOnboardingRejected("mall_style_consent_version_invalid")
        samples = list(
            self._session.scalars(
                select(StyleSample).where(
                    StyleSample.customer_id == merchant_id,
                    StyleSample.id.in_(confirmation.evidence_sample_ids),
                )
            )
        )
        if len(samples) != len(confirmation.evidence_sample_ids):
            raise MallStyleOnboardingRejected("mall_style_evidence_not_owned")
        if len(samples) < MINIMUM_EVIDENCE_SAMPLES:
            raise MallStyleOnboardingRejected("mall_style_evidence_insufficient")
        if any(not _sample_is_eligible(sample) for sample in samples):
            raise MallStyleOnboardingRejected("mall_style_evidence_not_usable")
        if any(
            not self._store.verify_style_sample_content(
                merchant_id,
                sample.id,
                sample.content_sha256 or "",
            )
            for sample in samples
        ):
            raise MallStyleOnboardingRejected("mall_style_evidence_content_unavailable")

        existing = self._session.get(StyleCard, candidate_id)
        if existing is not None:
            return CandidateConfirmationResult(
                receipt=self._resolve_existing(
                    merchant_id=merchant_id,
                    opc_id=opc_id,
                    candidate_id=candidate_id,
                    confirmation=confirmation,
                ),
                created=False,
            )

        version_id = _derived_id(candidate_id, "version")
        slot_id = _derived_id(candidate_id, "slot")
        slot_key = (
            "hero" if confirmation.default_image_placement == "primary" else "detail"
        )
        slot_label = "主图" if slot_key == "hero" else "详情图"
        slot_rules = {
            "placement": confirmation.default_image_placement,
            "aspect_ratio": "1:1",
        }
        slot_snapshot = {
            "id": str(slot_id),
            "customer_id": str(merchant_id),
            "scene_style_id": str(candidate_id),
            "style_card_version_id": str(version_id),
            "slot_key": slot_key,
            "label": slot_label,
            "rules": slot_rules,
            "active": True,
            "position": 0,
        }
        card = StyleCard(
            id=candidate_id, customer_id=merchant_id, kind="scene", active=True
        )
        scene = SceneStyle(
            id=candidate_id,
            customer_id=merchant_id,
            style_card_id=candidate_id,
            name=confirmation.candidate_name,
            style_origin="customer_upload",
            active=True,
            rules={
                "mall_candidate_id": str(candidate_id),
                "default_image_placement": confirmation.default_image_placement,
            },
        )
        version = StyleCardVersion(
            id=version_id,
            customer_id=merchant_id,
            style_card_id=candidate_id,
            version=1,
            content={
                "name": confirmation.candidate_name,
                "mall_candidate_id": str(candidate_id),
                "owner_actor_reference": confirmation.owner_actor_reference,
                "consent_text_version": confirmation.consent_text_version,
                "default_image_placement": confirmation.default_image_placement,
                "main_style_card_version_id": None,
                "historical_style": True,
                "style_origin": "customer_upload",
                "stability": "customer_confirmed",
                "slots": [slot_snapshot],
                "mall_default_slot_key": slot_key,
                "evidence_sample_ids": sorted(
                    str(sample_id) for sample_id in confirmation.evidence_sample_ids
                ),
            },
        )
        slot = StyleTemplateSlot(
            id=slot_id,
            customer_id=merchant_id,
            scene_style_id=candidate_id,
            style_card_version_id=version_id,
            slot_key=slot_key,
            label=slot_label,
            rules=slot_rules,
            active=True,
            position=0,
        )
        # Flush each FK tier explicitly.  These models intentionally expose no
        # ORM relationships, so SQLAlchemy cannot infer their insertion order.
        try:
            self._session.add(card)
            self._session.flush()
            self._session.add_all([scene, version])
            self._session.flush()
            self._session.add(slot)
            self._session.flush()
            self._session.add_all(
                [
                    StyleCardSampleLink(
                        customer_id=merchant_id,
                        style_card_version_id=version_id,
                        style_sample_id=sample_id,
                    )
                    for sample_id in confirmation.evidence_sample_ids
                ]
            )
            self._session.add(
                AuditEvent(
                    customer_id=merchant_id,
                    event_type="mall_style_onboarding.confirmed",
                    payload={
                        "candidate_id": str(candidate_id),
                        "scene_style_id": str(candidate_id),
                        "style_card_version_id": str(version_id),
                        "evidence_sample_count": len(confirmation.evidence_sample_ids),
                        "default_image_placement": (
                            confirmation.default_image_placement
                        ),
                        "generation_started": False,
                        "publishable": False,
                    },
                )
            )
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            try:
                receipt = self._resolve_existing(
                    merchant_id=merchant_id,
                    opc_id=opc_id,
                    candidate_id=candidate_id,
                    confirmation=confirmation,
                )
            except MallStyleOnboardingConflict:
                raise MallStyleOnboardingConflict(
                    "mall_style_candidate_conflict"
                ) from error
            return CandidateConfirmationResult(receipt=receipt, created=False)
        return CandidateConfirmationResult(
            receipt=_confirmation_receipt(
                merchant_id=merchant_id,
                opc_id=opc_id,
                candidate_id=candidate_id,
                version_id=version_id,
                placement=confirmation.default_image_placement,
            ),
            created=True,
        )

    def _resolve_existing(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        candidate_id: UUID,
        confirmation: MallStyleOnboardingConfirmationV1,
    ) -> MallStyleOnboardingConfirmationReceiptV1:
        version_id = _derived_id(candidate_id, "version")
        card = self._session.get(StyleCard, candidate_id)
        scene = self._session.get(SceneStyle, candidate_id)
        version = self._session.get(StyleCardVersion, version_id)
        if (
            card is None
            or card.customer_id != merchant_id
            or card.kind != "scene"
            or scene is None
            or scene.customer_id != merchant_id
            or version is None
            or version.customer_id != merchant_id
        ):
            raise MallStyleOnboardingConflict("mall_style_candidate_conflict")
        expected_evidence = sorted(
            str(value) for value in confirmation.evidence_sample_ids
        )
        content = version.content
        if (
            scene.name != confirmation.candidate_name
            or content.get("mall_candidate_id") != str(candidate_id)
            or content.get("owner_actor_reference")
            != confirmation.owner_actor_reference
            or content.get("consent_text_version") != confirmation.consent_text_version
            or content.get("default_image_placement")
            != confirmation.default_image_placement
            or content.get("evidence_sample_ids") != expected_evidence
        ):
            raise MallStyleOnboardingConflict("mall_style_candidate_conflict")
        return _confirmation_receipt(
            merchant_id=merchant_id,
            opc_id=opc_id,
            candidate_id=candidate_id,
            version_id=version_id,
            placement=confirmation.default_image_placement,
        )


def _sample_is_eligible(sample: StyleSample) -> bool:
    return (
        sample.withdrawn_at is None
        and sample.has_complete_content_seal
        and sample.consent_provenance == CONSENT_PROVENANCE
        and sample.consent_text_version == CONSENT_TEXT_VERSION
    )


def _derived_id(candidate_id: UUID, suffix: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"mall-style-onboarding:{candidate_id}:{suffix}")


def _confirmation_receipt(
    *,
    merchant_id: UUID,
    opc_id: UUID,
    candidate_id: UUID,
    version_id: UUID,
    placement: str,
) -> MallStyleOnboardingConfirmationReceiptV1:
    return MallStyleOnboardingConfirmationReceiptV1.model_validate(
        {
            "merchant_id": merchant_id,
            "opc_id": opc_id,
            "candidate_id": candidate_id,
            "scene_style_id": candidate_id,
            "style_card_version_id": version_id,
            "style_version": 1,
            "default_image_placement": placement,
        }
    )
