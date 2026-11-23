"""Explicit consent, withdrawal, and readiness application service."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from uuid import UUID

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_evidence import LOCAL_SYNTHETIC_CONSENT_PROVENANCES
from opc_style.domain.style_rules import SampleReadiness, assess_sample_count
from opc_style.models import AuditEvent, Customer, StyleSample
from opc_style.repositories import StyleRepository
from opc_style.services.asset_store_recovery import namespace_is_accepted
from opc_style.services.real_photos import (
    AssetContentStore,
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.style_consent import coordinate_style_consent


class StyleSampleNotFoundError(Exception):
    """Raised when a sample is not visible to the requesting customer."""


@dataclass(frozen=True)
class StyleSampleAuthorization:
    """A consent result that distinguishes creation from a safe replay."""

    sample: StyleSample
    created: bool


@dataclass(frozen=True)
class StyleSampleContentSelection:
    """The immutable media identity selected for later style analysis."""

    sample_id: UUID
    content_sha256: str
    mime_type: str


class StyleSampleService:
    """Apply style-sample rules inside one customer boundary at a time."""

    def __init__(
        self, session: Session, content_store: AssetContentStore | None = None
    ) -> None:
        self._session = session
        self._repository = StyleRepository(session)
        self._content_store = content_store

    def authorize(
        self, customer_id: UUID, *, photo_id: str, consent_text_version: str
    ) -> StyleSampleAuthorization:
        """Create one auditable consent record, or return its active equivalent."""
        try:
            return self._authorize(
                customer_id,
                photo_id=photo_id,
                consent_text_version=consent_text_version,
            )
        except IntegrityError as error:
            return self._recover_concurrent_authorization(
                error,
                customer_id,
                photo_id=photo_id,
                consent_text_version=consent_text_version,
            )

    def _authorize(
        self, customer_id: UUID, *, photo_id: str, consent_text_version: str
    ) -> StyleSampleAuthorization:
        """Run one authorization attempt before the uniqueness-race boundary."""
        self._ensure_customer(customer_id)
        existing_sample = self._repository.get_sample_by_photo(customer_id, photo_id)
        if existing_sample is not None:
            return self._resolve_existing_authorization(
                existing_sample,
                consent_text_version=consent_text_version,
            )

        sample = self._repository.create_sample(
            customer_id,
            photo_id,
            authorized_at=datetime.now(UTC),
            consent_provenance=consent_text_version,
        )
        self._session.flush()
        self._record_audit(
            customer_id,
            "style_sample.authorized",
            {
                "sample_id": str(sample.id),
                "photo_id": sample.photo_id,
                "consent_text_version": consent_text_version,
            },
        )
        self._session.commit()
        self._session.refresh(sample)
        return StyleSampleAuthorization(sample=sample, created=True)

    def authorize_content(
        self,
        customer_id: UUID,
        *,
        photo_id: str,
        content: bytes,
        claimed_sha256: str,
        mime_type: str,
        consent_provenance: str,
        consent_text_version: str,
    ) -> StyleSampleContentSelection:
        """Seal consented raw bytes before they can become a style input."""
        try:
            return self._authorize_verified_content(
                customer_id,
                photo_id=photo_id,
                content=content,
                claimed_sha256=claimed_sha256,
                mime_type=mime_type,
                consent_provenance=consent_provenance,
                consent_text_version=consent_text_version,
            )
        except IntegrityError as error:
            return self._recover_concurrent_content_authorization(
                error,
                customer_id,
                photo_id=photo_id,
                content=content,
                claimed_sha256=claimed_sha256,
                mime_type=mime_type,
                consent_provenance=consent_provenance,
                consent_text_version=consent_text_version,
            )

    def _recover_concurrent_content_authorization(
        self,
        error: IntegrityError,
        customer_id: UUID,
        *,
        photo_id: str,
        content: bytes,
        claimed_sha256: str,
        mime_type: str,
        consent_provenance: str,
        consent_text_version: str,
    ) -> StyleSampleContentSelection:
        """Resolve only an identical customer-owned content seal after a race."""
        self._session.rollback()
        if "unique" not in str(error).lower():
            raise error
        content_sha256 = _verify_content(content, claimed_sha256, mime_type)
        if not consent_text_version.strip():
            raise DomainRuleError("style_sample_consent_required")
        existing = self._repository.get_sample_by_photo(customer_id, photo_id)
        if existing is None:
            raise error
        return self._resolve_existing_content_authorization(
            existing,
            content_store=self._require_content_store(),
            content_sha256=content_sha256,
            content=content,
            mime_type=mime_type,
            consent_provenance=consent_provenance,
            consent_text_version=consent_text_version,
        )

    def _authorize_verified_content(
        self,
        customer_id: UUID,
        *,
        photo_id: str,
        content: bytes,
        claimed_sha256: str,
        mime_type: str,
        consent_provenance: str,
        consent_text_version: str,
    ) -> StyleSampleContentSelection:
        """Validate all caller-controlled facts before either persistence side effect."""
        content_store = self._require_content_store()
        content_sha256 = _verify_content(content, claimed_sha256, mime_type)
        if not consent_provenance.strip() or not consent_text_version.strip():
            raise DomainRuleError("style_sample_consent_required")
        width, height = _decoded_dimensions(content, mime_type)
        self._ensure_customer(customer_id)
        existing = self._repository.get_sample_by_photo(customer_id, photo_id)
        if existing is not None:
            return self._resolve_existing_content_authorization(
                existing,
                content_store=content_store,
                content_sha256=content_sha256,
                content=content,
                mime_type=mime_type,
                consent_provenance=consent_provenance,
                consent_text_version=consent_text_version,
            )

        sample = StyleSample(
            customer_id=customer_id,
            photo_id=photo_id,
            authorized_at=datetime.now(UTC),
            consent_provenance=consent_provenance,
            consent_text_version=consent_text_version,
            content_sha256=content_sha256,
            mime_type=mime_type,
            width=width,
            height=height,
            asset_store_namespace=content_store.store_namespace,
        )
        self._session.add(sample)
        try:
            self._session.flush()
            _store_style_sample_content(
                content_store, customer_id, sample.id, content, content_sha256
            )
            self._record_audit(
                customer_id,
                "style_sample.content_authorized",
                {
                    "sample_id": str(sample.id),
                    "photo_id": sample.photo_id,
                    "content_sha256": content_sha256,
                    "mime_type": mime_type,
                    "consent_provenance": consent_provenance,
                    "consent_text_version": consent_text_version,
                },
            )
            self._session.commit()
        except BaseException:
            self._session.rollback()
            if sample.id is not None:
                try:
                    content_store.discard_style_sample_content(customer_id, sample.id)
                except AssetContentStoreError:
                    pass
            raise
        self._session.refresh(sample)
        return StyleSampleContentSelection(sample.id, content_sha256, mime_type)

    def _resolve_existing_content_authorization(
        self,
        sample: StyleSample,
        *,
        content_store: AssetContentStore,
        content_sha256: str,
        content: bytes,
        mime_type: str,
        consent_provenance: str,
        consent_text_version: str,
    ) -> StyleSampleContentSelection:
        """Only the same active immutable media-and-consent fact can replay."""
        if sample.withdrawn_at is not None:
            raise DomainRuleError("style_sample_consent_withdrawn")
        if sample.content_sha256 is None:
            raise DomainRuleError("style_sample_content_missing")
        if sample.asset_store_namespace is None:
            raise DomainRuleError("style_sample_content_missing")
        if sample.content_sha256 != content_sha256:
            raise DomainRuleError("style_sample_content_conflict")
        if sample.mime_type != mime_type:
            raise DomainRuleError("style_sample_content_facts_conflict")
        if sample.consent_provenance != consent_provenance:
            raise DomainRuleError("style_sample_consent_provenance_conflict")
        if sample.consent_text_version != consent_text_version:
            raise DomainRuleError("style_sample_consent_version_conflict")
        if not namespace_is_accepted(
            self._session,
            sample.customer_id,
            sample.asset_store_namespace,
            content_store.store_namespace,
        ):
            raise DomainRuleError("style_sample_content_store_mismatch")
        if not content_store.verify_style_sample_content(
            sample.customer_id, sample.id, content_sha256
        ):
            raise DomainRuleError("style_sample_content_unavailable")
        _store_style_sample_content(
            content_store, sample.customer_id, sample.id, content, content_sha256
        )
        return StyleSampleContentSelection(sample.id, sample.content_sha256, mime_type)

    def _recover_concurrent_authorization(
        self,
        error: IntegrityError,
        customer_id: UUID,
        *,
        photo_id: str,
        consent_text_version: str,
    ) -> StyleSampleAuthorization:
        """Rollback a unique race, then resolve only the request customer's row."""
        self._session.rollback()
        if "unique" not in str(error).lower():
            raise error
        existing_sample = self._repository.get_sample_by_photo(customer_id, photo_id)
        if existing_sample is None:
            raise error
        return self._resolve_existing_authorization(
            existing_sample,
            consent_text_version=consent_text_version,
        )

    @staticmethod
    def _resolve_existing_authorization(
        sample: StyleSample, *, consent_text_version: str
    ) -> StyleSampleAuthorization:
        """Treat only an identical active consent record as an idempotent replay."""
        if sample.withdrawn_at is not None:
            raise DomainRuleError("style_sample_consent_withdrawn")
        if sample.consent_provenance != consent_text_version:
            raise DomainRuleError("style_sample_consent_version_conflict")
        return StyleSampleAuthorization(sample=sample, created=False)

    def readiness(self, customer_id: UUID) -> SampleReadiness:
        """Assess only samples whose authorization is still active."""
        active_count = sum(
            sample.has_complete_content_seal
            for sample in self._session.scalars(
                select(StyleSample).where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.withdrawn_at.is_(None),
                    StyleSample.consent_provenance.not_in(
                        LOCAL_SYNTHETIC_CONSENT_PROVENANCES
                    ),
                )
            )
        )
        return assess_sample_count(active_count)

    def withdraw(self, customer_id: UUID, sample_id: UUID) -> StyleSample:
        """Withdraw consent without deleting either the sample or its audit trail."""
        with coordinate_style_consent(customer_id):
            return self._withdraw(customer_id, sample_id)

    def _withdraw(self, customer_id: UUID, sample_id: UUID) -> StyleSample:
        """Commit withdrawal while excluding a new analysis dispatch."""
        sample = self._repository.get_sample(customer_id, sample_id)
        if sample is None:
            raise StyleSampleNotFoundError
        if sample.withdrawn_at is not None:
            return sample

        sample.withdrawn_at = datetime.now(UTC)
        self._session.flush()
        self._record_audit(
            customer_id,
            "style_sample.withdrawn",
            {"sample_id": str(sample.id), "photo_id": sample.photo_id},
        )
        self._session.commit()
        self._session.refresh(sample)
        return sample

    def _ensure_customer(self, customer_id: UUID) -> None:
        """Create a local customer row when a valid customer header first appears."""
        if self._session.get(Customer, customer_id) is None:
            self._session.add(
                Customer(id=customer_id, display_name=f"Customer {customer_id}")
            )
            self._session.flush()

    def _record_audit(
        self, customer_id: UUID, event_type: str, payload: dict[str, str]
    ) -> None:
        """Append one durable audit fact for a material consent action."""
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type=event_type,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )

    def _require_content_store(self) -> AssetContentStore:
        if self._content_store is None:
            raise AssetContentStoreError("asset_content_store_unconfigured")
        return self._content_store


def _store_style_sample_content(
    content_store: AssetContentStore,
    customer_id: UUID,
    sample_id: UUID,
    content: bytes,
    content_sha256: str,
) -> None:
    """Use durable bytes for the local workspace and preserve generic stores."""
    if isinstance(content_store, LocalSyntheticAssetStore):
        content_store.persist_style_sample_content(
            customer_id, sample_id, content, content_sha256
        )
        return
    content_store.register_style_sample_content(
        customer_id, sample_id, content, content_sha256
    )


def _verify_content(content: bytes, claimed_sha256: str, mime_type: str) -> str:
    """Fail closed unless raw bytes, declared digest, and declared type agree."""
    if not content:
        raise DomainRuleError("style_sample_content_invalid")
    actual_sha256 = sha256(content).hexdigest()
    if actual_sha256 != claimed_sha256:
        raise DomainRuleError("style_sample_content_hash_mismatch")
    _decoded_dimensions(content, mime_type)
    return actual_sha256


def _decoded_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    """Decode JPEG/PNG bytes twice so verification cannot accept truncated media."""
    expected_format = {"image/jpeg": "JPEG", "image/png": "PNG"}.get(mime_type)
    if expected_format is None:
        raise DomainRuleError("style_sample_content_mime_invalid")
    try:
        with Image.open(BytesIO(content)) as verified:
            if verified.format != expected_format:
                raise DomainRuleError("style_sample_content_mime_mismatch")
            verified.verify()
        with Image.open(BytesIO(content)) as decoded:
            if decoded.format != expected_format:
                raise DomainRuleError("style_sample_content_mime_mismatch")
            decoded.load()
            width, height = decoded.size
    except (OSError, UnidentifiedImageError) as error:
        raise DomainRuleError("style_sample_content_invalid") from error
    if width <= 0 or height <= 0:
        raise DomainRuleError("style_sample_content_invalid")
    return width, height
