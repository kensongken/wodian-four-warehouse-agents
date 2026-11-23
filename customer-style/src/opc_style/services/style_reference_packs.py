"""Create and revalidate immutable provider-sized style reference packs."""

from typing import Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_reference_pack import (
    StyleReference,
    StyleReferencePackSnapshot,
    reference_manifest_sha256,
)
from opc_style.models import (
    StyleCardSampleLink,
    StyleCardVersion,
    StyleReferencePack,
    StyleReferencePackItem,
    StyleSample,
)

MIN_STYLE_REFERENCE_PACK_SIZE = 2
MAX_STYLE_REFERENCE_PACK_SIZE = 4


class StyleReferencePackService:
    """Seal a small exact reference set and keep permissions fail-closed."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(
        self, customer_id: UUID, scene_style_version_id: UUID
    ) -> StyleReferencePackSnapshot:
        version = self._confirmed_version(customer_id, scene_style_version_id)
        samples = self._eligible_samples(customer_id, version.id)
        selected = samples[:MAX_STYLE_REFERENCE_PACK_SIZE]
        if len(selected) < MIN_STYLE_REFERENCE_PACK_SIZE:
            raise DomainRuleError("style_reference_pack_unavailable")
        references = tuple(self._reference(sample) for sample in selected)
        manifest = reference_manifest_sha256(references)
        existing = self._session.scalar(
            select(StyleReferencePack).where(
                StyleReferencePack.customer_id == customer_id,
                StyleReferencePack.scene_style_version_id == version.id,
                StyleReferencePack.manifest_sha256 == manifest,
            )
        )
        if existing is not None:
            return self.get(customer_id, existing.id)
        pack = StyleReferencePack(
            id=uuid4(),
            customer_id=customer_id,
            scene_style_version_id=version.id,
            selection_version=str(
                version.content.get(
                    "reference_selection_version", "linked-sample-order-v1"
                )
            ),
            manifest_sha256=manifest,
            confirmed_by=str(customer_id),
            active=True,
        )
        self._session.add(pack)
        self._session.flush()
        self._session.add_all(
            StyleReferencePackItem(
                id=uuid4(),
                customer_id=customer_id,
                pack_id=pack.id,
                style_sample_id=reference.sample_id,
                position=position,
                content_sha256=reference.content_sha256,
                mime_type=reference.mime_type,
            )
            for position, reference in enumerate(references)
        )
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._session.scalar(
                select(StyleReferencePack).where(
                    StyleReferencePack.customer_id == customer_id,
                    StyleReferencePack.scene_style_version_id == version.id,
                    StyleReferencePack.manifest_sha256 == manifest,
                )
            )
            if existing is None:
                raise
            pack = existing
        return self.get(customer_id, pack.id)

    def get(
        self, customer_id: UUID, pack_id: UUID
    ) -> StyleReferencePackSnapshot:
        pack = self._session.scalar(
            select(StyleReferencePack)
            .where(
                StyleReferencePack.customer_id == customer_id,
                StyleReferencePack.id == pack_id,
            )
            .execution_options(populate_existing=True)
        )
        if pack is None or not pack.active:
            raise DomainRuleError("style_reference_pack_unavailable")
        version = self._confirmed_version(customer_id, pack.scene_style_version_id)
        items = list(
            self._session.scalars(
                select(StyleReferencePackItem)
                .where(
                    StyleReferencePackItem.customer_id == customer_id,
                    StyleReferencePackItem.pack_id == pack.id,
                )
                .order_by(StyleReferencePackItem.position)
            )
        )
        if not MIN_STYLE_REFERENCE_PACK_SIZE <= len(items) <= MAX_STYLE_REFERENCE_PACK_SIZE:
            raise DomainRuleError("style_reference_pack_unavailable")
        references: list[StyleReference] = []
        for item in items:
            sample = self._session.scalar(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.id == item.style_sample_id,
                )
                .execution_options(populate_existing=True)
            )
            if not self._sample_is_eligible(sample) or (
                sample is not None
                and (
                    sample.content_sha256 != item.content_sha256
                    or sample.mime_type != item.mime_type
                )
            ):
                raise DomainRuleError("style_reference_pack_unavailable")
            assert sample is not None
            references.append(self._reference(sample))
        frozen = tuple(references)
        if reference_manifest_sha256(frozen) != pack.manifest_sha256:
            raise DomainRuleError("style_reference_pack_unavailable")
        return StyleReferencePackSnapshot(
            id=pack.id,
            customer_id=customer_id,
            scene_style_version_id=version.id,
            selection_version=pack.selection_version,
            manifest_sha256=pack.manifest_sha256,
            confirmed_by=pack.confirmed_by,
            active=pack.active,
            created_at=pack.created_at,
            references=frozen,
        )

    def _confirmed_version(
        self, customer_id: UUID, scene_style_version_id: UUID
    ) -> StyleCardVersion:
        version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == scene_style_version_id,
            )
        )
        if version is None or any(
            (
                version.content.get("historical_style") is not True,
                version.content.get("stability") != "customer_confirmed",
                version.content.get("style_origin") != "customer_upload",
            )
        ):
            raise DomainRuleError("style_reference_pack_unavailable")
        return version

    def _eligible_samples(
        self, customer_id: UUID, version_id: UUID
    ) -> list[StyleSample]:
        rows = list(
            self._session.scalars(
                select(StyleSample)
                .join(
                    StyleCardSampleLink,
                    StyleCardSampleLink.style_sample_id == StyleSample.id,
                )
                .where(
                    StyleCardSampleLink.customer_id == customer_id,
                    StyleCardSampleLink.style_card_version_id == version_id,
                    StyleSample.customer_id == customer_id,
                )
                .order_by(StyleCardSampleLink.created_at, StyleCardSampleLink.id)
            )
        )
        if not rows or any(not self._sample_is_eligible(row) for row in rows):
            raise DomainRuleError("style_reference_pack_unavailable")
        return rows

    @staticmethod
    def _sample_is_eligible(sample: StyleSample | None) -> bool:
        return bool(
            sample is not None
            and sample.withdrawn_at is None
            and sample.consent_provenance != "local_synthetic_acceptance"
            and sample.has_complete_content_seal
        )

    @staticmethod
    def _reference(sample: StyleSample) -> StyleReference:
        assert sample.content_sha256 is not None and sample.mime_type is not None
        return StyleReference(
            sample_id=sample.id,
            content_sha256=sample.content_sha256,
            mime_type=cast(Literal["image/jpeg", "image/png"], sample.mime_type),
        )
