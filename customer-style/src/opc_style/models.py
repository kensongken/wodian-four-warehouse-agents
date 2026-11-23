"""Portable SQLAlchemy models for customer-owned style assets."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    and_,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, InstanceState, Mapped, Mapper, mapped_column
from sqlalchemy.types import TypeDecorator

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.output_rules import validate_job_status_transition
from opc_style.domain.real_style_eligibility import assess_real_style


class UUIDType(TypeDecorator[UUID]):
    """Store UUIDs as canonical strings, including on SQLite."""

    impl = String(36)
    cache_ok = True

    def process_bind_param(
        self, value: UUID | str | None, dialect: Dialect
    ) -> str | None:
        del dialect
        if value is None:
            return None
        return str(UUID(str(value)))

    def process_result_value(self, value: str | None, dialect: Dialect) -> UUID | None:
        del dialect
        if value is None:
            return None
        return UUID(value)

    def copy(self, **kw: Any) -> "UUIDType":
        del kw
        return UUIDType()


class Base(DeclarativeBase):
    """Base class for style-agent persistence models."""


def _now() -> datetime:
    return datetime.now(UTC)


class Customer(Base):
    """A single isolated OpC designer account."""

    __tablename__ = "customers"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StyleWarehouseV3Receipt(Base):
    """One immutable eligibility decision for an independent warehouse delivery."""

    __tablename__ = "style_warehouse_v3_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_style_warehouse_v3_tenant_key"
        ),
        UniqueConstraint(
            "tenant_id", "handoff_id", name="uq_style_warehouse_v3_tenant_handoff"
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected')",
            name="ck_style_warehouse_v3_decision",
        ),
        CheckConstraint(
            "generation_job_id IS NULL",
            name="ck_style_warehouse_v3_generation_empty",
        ),
        CheckConstraint(
            "(decision = 'accepted' AND rejection_code IS NULL) OR "
            "(decision = 'rejected' AND rejection_code IS NOT NULL)",
            name="ck_style_warehouse_v3_rejection_code",
        ),
        Index("ix_style_warehouse_v3_tenant_decided", "tenant_id", "decided_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    checked_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(64))
    generation_job_id: Mapped[UUID | None] = mapped_column(UUIDType())
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StyleWarehouseV3Attempt(Base):
    """Sanitized append-only intake evidence without request bytes or secrets."""

    __tablename__ = "style_warehouse_v3_attempts"
    __table_args__ = (
        CheckConstraint(
            "result IN ('accepted', 'rejected', 'duplicate', 'conflict', 'error')",
            name="ck_style_warehouse_v3_attempt_result",
        ),
        Index("ix_style_warehouse_v3_attempt_received", "tenant_id", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(UUIDType())
    handoff_id: Mapped[UUID | None] = mapped_column(UUIDType())
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MallStyleHandoffReceipt(Base):
    """Durable fail-closed receipt for one Mall SaaS product-entry handoff."""

    __tablename__ = "mall_style_handoff_receipts"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "opc_id",
            "idempotency_key",
            name="uq_mall_style_tenant_key",
        ),
        UniqueConstraint(
            "merchant_id",
            "opc_id",
            "handoff_id",
            name="uq_mall_style_tenant_handoff",
        ),
        CheckConstraint(
            "status IN ('awaiting_product_photo','awaiting_subject_confirmation',"
            "'awaiting_generation_approval')",
            name="ck_mall_style_receipt_status",
        ),
        CheckConstraint("generation_job_id IS NULL", name="ck_mall_style_no_job"),
        CheckConstraint("publishable = false", name="ck_mall_style_not_publishable"),
        Index("ix_mall_style_merchant_received", "merchant_id", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    handoff_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    merchant_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    opc_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_entry_submission_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False
    )
    product_entry_draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    product_entry_draft_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    checked_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    product_asset_id: Mapped[UUID | None] = mapped_column(UUIDType())
    subject_seal_id: Mapped[UUID | None] = mapped_column(UUIDType())
    scene_style_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    style_card_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    generation_job_id: Mapped[UUID | None] = mapped_column(UUIDType())
    publishable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MallStyleSourceAssetBinding(Base):
    """Immutable source-id/checksum binding; it stores no image bytes."""

    __tablename__ = "mall_style_source_asset_bindings"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "opc_id",
            "source_asset_id",
            name="uq_mall_style_source_asset",
        ),
        CheckConstraint(
            "asset_role IN ('label_photo','product_photo')",
            name="ck_mall_style_source_asset_role",
        ),
        Index("ix_mall_style_source_merchant", "merchant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    merchant_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    opc_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    source_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_role: Mapped[str] = mapped_column(String(32), nullable=False)
    style_product_asset_id: Mapped[UUID | None] = mapped_column(UUIDType())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MallStyleGenerationQueueItem(Base):
    """One explicitly approved Mall request waiting for the existing image pipeline."""

    __tablename__ = "mall_style_generation_queue_items"
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_mall_style_generation_receipt"),
        UniqueConstraint(
            "merchant_id",
            "opc_id",
            "idempotency_key",
            name="uq_mall_style_generation_tenant_key",
        ),
        UniqueConstraint("generation_job_id", name="uq_mall_style_generation_job"),
        CheckConstraint(
            "status IN ('pending_execution','dispatched','failed','revoked','expired')",
            name="ck_mall_style_generation_status",
        ),
        CheckConstraint(
            "candidate_count BETWEEN 1 AND 3",
            name="ck_mall_style_generation_candidate_count",
        ),
        CheckConstraint(
            "actor_role = 'merchant_owner'", name="ck_mall_style_generation_owner"
        ),
        CheckConstraint("maximum_cost_usd >= 0", name="ck_mall_style_generation_cost"),
        CheckConstraint(
            "length(approval_payload_sha256) = 64",
            name="ck_mall_style_generation_payload_sha",
        ),
        CheckConstraint(
            "publishable = false", name="ck_mall_style_generation_not_publishable"
        ),
        Index(
            "ix_mall_style_generation_tenant_product",
            "merchant_id",
            "opc_id",
            "product_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    receipt_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        ForeignKey("mall_style_handoff_receipts.id"),
        nullable=False,
    )
    merchant_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    opc_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    approval_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_actor_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_audit_event_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    approval_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    rights_permission_event_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    rights_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    subject_seal_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    scene_style_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    style_card_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    generation_job_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), ForeignKey("image_output_jobs.id")
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    publishable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class MallStyleSubjectConfirmation(Base):
    """Mall owner audit binding for one existing human-reviewed subject seal."""

    __tablename__ = "mall_style_subject_confirmations"
    __table_args__ = (
        UniqueConstraint("receipt_id", name="uq_mall_style_subject_receipt"),
        UniqueConstraint(
            "merchant_id",
            "opc_id",
            "idempotency_key",
            name="uq_mall_style_subject_tenant_key",
        ),
        CheckConstraint("proposal_version = 1", name="ck_mall_style_subject_version"),
        CheckConstraint(
            "actor_role = 'merchant_owner'", name="ck_mall_style_subject_owner"
        ),
        CheckConstraint(
            "length(source_sha256) = 64 AND length(base_mask_sha256) = 64 "
            "AND length(reviewed_mask_sha256) = 64 "
            "AND length(confirmation_payload_sha256) = 64",
            name="ck_mall_style_subject_digests",
        ),
        CheckConstraint(
            "publishable = false", name="ck_mall_style_subject_not_publishable"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    receipt_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("mall_style_handoff_receipts.id"), nullable=False
    )
    merchant_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    opc_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_subject_seal_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    base_mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_subject_seal_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    owner_actor_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    confirmation_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_event_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    publishable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleSample(Base):
    """An explicitly authorized photo reference owned by one customer."""

    __tablename__ = "style_samples"
    __table_args__ = (
        UniqueConstraint("customer_id", "id", name="uq_style_samples_customer_id_id"),
        CheckConstraint(
            "content_sha256 IS NULL OR length(content_sha256) = 64",
            name="ck_style_samples_content_sha256_length",
        ),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
            name="ck_style_samples_content_dimensions_positive",
        ),
        CheckConstraint(
            "(content_sha256 IS NULL AND mime_type IS NULL AND width IS NULL AND height IS NULL AND asset_store_namespace IS NULL) OR "
            "(content_sha256 IS NOT NULL AND mime_type IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL AND asset_store_namespace IS NOT NULL)",
            name="ck_style_samples_content_seal_complete",
        ),
        CheckConstraint(
            "(content_sha256 IS NULL AND consent_text_version IS NULL) OR "
            "(content_sha256 IS NOT NULL AND consent_text_version IS NOT NULL AND length(trim(consent_text_version)) > 0)",
            name="ck_style_samples_content_consent_complete",
        ),
        Index(
            "uq_style_samples_customer_photo", "customer_id", "photo_id", unique=True
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    photo_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consent_provenance: Mapped[str] = mapped_column(String(100), nullable=False)
    consent_text_version: Mapped[str | None] = mapped_column(String(100))
    # Legacy consent-only rows remain readable, but content-bearing rows are
    # sealed by the 0016 database trigger and are the only rows future visual
    # analysis may consume.
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(100))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    asset_store_namespace: Mapped[str | None] = mapped_column(String(255))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @property
    def has_complete_content_seal(self) -> bool:
        """Return whether this row can safely participate in style analysis."""
        return (
            bool(self.consent_provenance.strip())
            and self.content_sha256 is not None
            and self.mime_type in {"image/jpeg", "image/png"}
            and self.width is not None
            and self.width > 0
            and self.height is not None
            and self.height > 0
            and self.asset_store_namespace is not None
            and self.consent_text_version is not None
            and bool(self.consent_text_version.strip())
        )


class AssetStoreRecoveryBinding(Base):
    """Append-only proof that a current store carries one legacy namespace."""

    __tablename__ = "asset_store_recovery_bindings"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "source_namespace",
            "target_namespace",
            "manifest_sha256",
            name="uq_asset_store_recovery_binding_manifest",
        ),
        CheckConstraint(
            "length(source_namespace) = 64 AND source_namespace = lower(source_namespace)",
            name="ck_asset_store_recovery_source_namespace",
        ),
        CheckConstraint(
            "length(target_namespace) = 64 AND target_namespace = lower(target_namespace)",
            name="ck_asset_store_recovery_target_namespace",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64 AND manifest_sha256 = lower(manifest_sha256)",
            name="ck_asset_store_recovery_manifest_sha256",
        ),
        CheckConstraint(
            "sample_count > 0", name="ck_asset_store_recovery_sample_count"
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_asset_store_recovery_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    source_namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    target_namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_label: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleCard(Base):
    """A customer-owned main or derived style card."""

    __tablename__ = "style_cards"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="main")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("customer_id", "id", name="uq_style_cards_customer_id_id"),
        Index(
            "uq_style_cards_active_main_customer",
            "customer_id",
            unique=True,
            sqlite_where=and_(kind == "main", active.is_(True)),
            postgresql_where=and_(kind == "main", active.is_(True)),
        ),
    )


class StyleCardVersion(Base):
    """An immutable, customer-owned revision of a style card."""

    __tablename__ = "style_card_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "style_card_id"],
            ["style_cards.customer_id", "style_cards.id"],
            name="fk_style_card_versions_customer_style_card",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_style_card_versions_customer_id_id"
        ),
        Index(
            "uq_style_card_versions_card_version",
            "style_card_id",
            "version",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    style_card_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StyleCardSampleLink(Base):
    """A customer-owned link from a card version to supporting samples."""

    __tablename__ = "style_card_sample_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_card_sample_links_customer_version",
        ),
        ForeignKeyConstraint(
            ["customer_id", "style_sample_id"],
            ["style_samples.customer_id", "style_samples.id"],
            name="fk_style_card_sample_links_customer_sample",
        ),
        Index(
            "uq_style_card_sample_link",
            "style_card_version_id",
            "style_sample_id",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    style_card_version_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    style_sample_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StyleReferencePack(Base):
    """An immutable provider-sized selection for one confirmed style revision."""

    __tablename__ = "style_reference_packs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "scene_style_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_reference_packs_customer_version",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_style_reference_packs_customer_id"
        ),
        UniqueConstraint(
            "scene_style_version_id",
            "manifest_sha256",
            name="uq_style_reference_packs_version_manifest",
        ),
        CheckConstraint(
            "length(manifest_sha256) = 64",
            name="ck_style_reference_packs_manifest_sha256",
        ),
        CheckConstraint(
            "length(trim(selection_version)) > 0",
            name="ck_style_reference_packs_selection_version",
        ),
        CheckConstraint(
            "length(trim(confirmed_by)) > 0",
            name="ck_style_reference_packs_confirmed_by",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    scene_style_version_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    selection_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleReferencePackItem(Base):
    """One immutable ordered reference inside a provider-sized pack."""

    __tablename__ = "style_reference_pack_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "pack_id"],
            ["style_reference_packs.customer_id", "style_reference_packs.id"],
            name="fk_style_reference_pack_items_customer_pack",
        ),
        ForeignKeyConstraint(
            ["customer_id", "style_sample_id"],
            ["style_samples.customer_id", "style_samples.id"],
            name="fk_style_reference_pack_items_customer_sample",
        ),
        UniqueConstraint(
            "pack_id", "position", name="uq_style_reference_pack_items_position"
        ),
        UniqueConstraint(
            "pack_id", "style_sample_id", name="uq_style_reference_pack_items_sample"
        ),
        CheckConstraint(
            "position BETWEEN 0 AND 3",
            name="ck_style_reference_pack_items_position",
        ),
        CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_style_reference_pack_items_content_sha256",
        ),
        CheckConstraint(
            "mime_type IN ('image/jpeg','image/png')",
            name="ck_style_reference_pack_items_mime_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    pack_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    style_sample_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleFieldEvidence(Base):
    """One immutable customer-reviewed style conclusion and supporting sample."""

    __tablename__ = "style_field_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_field_evidence_customer_version",
        ),
        ForeignKeyConstraint(
            ["customer_id", "style_sample_id"],
            ["style_samples.customer_id", "style_samples.id"],
            name="fk_style_field_evidence_customer_sample",
        ),
        UniqueConstraint(
            "style_card_version_id",
            "field_name",
            "field_value",
            "style_sample_id",
            name="uq_style_field_evidence_fact_sample",
        ),
        CheckConstraint(
            "field_name IN ('tone', 'palette', 'lighting', 'composition', "
            "'backgrounds', 'people_and_props', 'material_treatment', "
            "'typography', 'forbidden_elements')",
            name="ck_style_field_evidence_field_name",
        ),
        CheckConstraint(
            "length(trim(field_value)) > 0",
            name="ck_style_field_evidence_value_nonempty",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    style_card_version_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    field_value: Mapped[str] = mapped_column(String(200), nullable=False)
    style_sample_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class SceneStyle(Base):
    """A named, customer-owned scene style that can inherit a main card."""

    __tablename__ = "scene_styles"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    style_card_id: Mapped[UUID | None] = mapped_column(UUIDType(), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    style_origin: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="customer_upload",
        server_default="customer_upload",
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (
        UniqueConstraint("customer_id", "id", name="uq_scene_styles_customer_id_id"),
        ForeignKeyConstraint(
            ["customer_id", "style_card_id"],
            ["style_cards.customer_id", "style_cards.id"],
            name="fk_scene_styles_customer_style_card",
        ),
        Index(
            "uq_scene_styles_active_customer_origin_name",
            "customer_id",
            "style_origin",
            "name",
            unique=True,
            sqlite_where=active.is_(True),
            postgresql_where=active.is_(True),
        ),
    )


@event.listens_for(SceneStyle, "before_insert")
def _validate_scene_style_origin_insert(
    mapper: Mapper[SceneStyle], connection: Connection, target: SceneStyle
) -> None:
    del mapper, connection
    if target.style_origin is None:
        target.style_origin = "customer_upload"
    if target.style_origin not in {"customer_upload", "local_demo"}:
        raise DomainRuleError("scene_style_origin_invalid")


@event.listens_for(SceneStyle, "before_update")
def _reject_scene_style_origin_mutation(
    mapper: Mapper[SceneStyle], connection: Connection, target: SceneStyle
) -> None:
    del mapper, connection
    state = inspect(target)
    if state.attrs.style_origin.history.has_changes():
        raise DomainRuleError("scene_style_origin_immutable")


class StyleTemplateSlot(Base):
    """An immutable suite position belonging to one scene-card revision."""

    __tablename__ = "style_template_slots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "scene_style_id"],
            ["scene_styles.customer_id", "scene_styles.id"],
            name="fk_style_template_slots_customer_scene",
        ),
        ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_template_slots_customer_version",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_style_template_slots_customer_id"
        ),
        UniqueConstraint(
            "style_card_version_id",
            "slot_key",
            name="uq_style_template_slots_version_key",
        ),
        UniqueConstraint(
            "style_card_version_id",
            "position",
            name="uq_style_template_slots_version_position",
        ),
        CheckConstraint("position >= 0", name="ck_style_template_slots_position"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    scene_style_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    style_card_version_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    slot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class StyleSuiteRun(Base):
    """Sealed planning facts with a separately mutable execution state."""

    __tablename__ = "style_suite_runs"
    __table_args__ = (
        UniqueConstraint("customer_id", "id", name="uq_style_suite_runs_customer_id"),
        Index(
            "uq_style_suite_runs_customer_key",
            "customer_id",
            "idempotency_key",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["customer_id", "source_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_style_suite_runs_customer_source_product",
        ),
        ForeignKeyConstraint(
            ["customer_id", "product_identity_card_version_id"],
            [
                "product_identity_card_versions.customer_id",
                "product_identity_card_versions.id",
            ],
            name="fk_style_suite_runs_customer_identity_card",
        ),
        CheckConstraint(
            "style_count > 0 AND slot_count >= style_count AND child_job_count = slot_count "
            "AND candidate_count = child_job_count * COALESCE(candidate_count_per_item, 3)",
            name="ck_style_suite_runs_counts",
        ),
        CheckConstraint(
            "candidate_estimate_usd >= 0 AND maximum_estimated_cost_usd >= 0",
            name="ck_style_suite_runs_cost",
        ),
        CheckConstraint(
            "NOT real_generation_enabled OR candidate_estimate_usd > 0",
            name="ck_style_suite_runs_real_cost",
        ),
        CheckConstraint(
            "currency = 'USD' AND cost_basis = 'local_planning_estimate'",
            name="ck_style_suite_runs_cost_basis",
        ),
        CheckConstraint(
            "(product_identity_card_version_id IS NULL AND product_identity_card_sha256 IS NULL) "
            "OR (product_identity_card_version_id IS NOT NULL AND "
            "product_identity_card_sha256 IS NOT NULL AND "
            "length(product_identity_card_sha256) = 64)",
            name="ck_style_suite_runs_identity_card_complete",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    product_identity_card_version_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), nullable=True
    )
    product_identity_card_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    product_identity_observation_run_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), nullable=True
    )
    product_identity_observation_result_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    product_identity_observation_review_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), nullable=True
    )
    style_count: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    child_job_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_estimate_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    maximum_estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    cost_basis: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    real_generation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    preview_payload_sha256: Mapped[str | None] = mapped_column(String(64))
    confirmed_maximum_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    candidate_count_per_item: Mapped[int | None] = mapped_column(Integer)
    authorization_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    pilot_scope: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, default=lambda: f"legacy:{uuid4()}"
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleSuiteRunItem(Base):
    """One immutable scene-version/slot selection inside a customer-owned run."""

    __tablename__ = "style_suite_run_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["style_suite_runs.customer_id", "style_suite_runs.id"],
            name="fk_style_suite_items_customer_run",
        ),
        ForeignKeyConstraint(
            ["customer_id", "scene_style_id"],
            ["scene_styles.customer_id", "scene_styles.id"],
            name="fk_style_suite_items_customer_scene",
        ),
        ForeignKeyConstraint(
            ["customer_id", "scene_style_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_style_suite_items_customer_version",
        ),
        ForeignKeyConstraint(
            ["customer_id", "slot_id"],
            ["style_template_slots.customer_id", "style_template_slots.id"],
            name="fk_style_suite_items_customer_slot",
        ),
        UniqueConstraint(
            "run_id",
            "scene_style_version_id",
            "slot_key",
            name="uq_style_suite_run_items_version_slot",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    scene_style_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    scene_style_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    style_name: Mapped[str] = mapped_column(String(255), nullable=False)
    style_version: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(64), nullable=False)
    style_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    slot_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    style_reference_pack_id: Mapped[UUID | None] = mapped_column(UUIDType())
    style_reference_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    item_authorized_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    output_job_id: Mapped[UUID | None] = mapped_column(UUIDType(), nullable=True)
    selected_candidate_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), nullable=True
    )
    selected_review_id: Mapped[UUID | None] = mapped_column(UUIDType(), nullable=True)
    fidelity_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fidelity_flags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleSuiteCompletion(Base):
    """An immutable customer-approved version of one complete suite."""

    __tablename__ = "style_suite_completions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["style_suite_runs.customer_id", "style_suite_runs.id"],
            name="fk_style_suite_completions_customer_run",
        ),
        UniqueConstraint("run_id", name="uq_style_suite_completion_run"),
        CheckConstraint("version = 1", name="ck_style_suite_completion_version"),
    )
    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class StyleSuiteCandidateFidelity(Base):
    """Immutable bounded fidelity result for one candidate in one suite slot."""

    __tablename__ = "style_suite_candidate_fidelities"
    __table_args__ = (
        UniqueConstraint(
            "image_candidate_id", name="uq_style_suite_candidate_fidelity"
        ),
        CheckConstraint(
            "fidelity_state IN ('green','yellow','red','unknown')",
            name="ck_style_suite_candidate_fidelity_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    run_item_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("style_suite_run_items.id"), nullable=False, index=True
    )
    output_job_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("image_output_jobs.id"), nullable=False
    )
    image_candidate_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("image_candidates.id"), nullable=False
    )
    fidelity_state: Mapped[str] = mapped_column(String(16), nullable=False)
    fidelity_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    summary_zh: Mapped[str | None] = mapped_column(String(200))
    warning_acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductAsset(Base):
    """An immutable customer-owned product photo or derived image asset."""

    __tablename__ = "product_assets"
    __table_args__ = (
        UniqueConstraint("customer_id", "id", name="uq_product_assets_customer_id_id"),
        Index(
            "uq_product_assets_customer_id_product",
            "customer_id",
            "id",
            "product_id",
            unique=True,
        ),
        Index(
            "uq_product_assets_customer_photo_identity",
            "customer_id",
            "photo_id",
            unique=True,
        ),
        UniqueConstraint(
            "customer_id",
            "photo_id",
            "content_sha256",
            name="uq_product_assets_customer_photo_hash",
        ),
        CheckConstraint("width > 0", name="ck_product_assets_width_positive"),
        CheckConstraint("height > 0", name="ck_product_assets_height_positive"),
        CheckConstraint(
            "length(content_sha256) = 64", name="ck_product_assets_sha256_length"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    photo_id: Mapped[str] = mapped_column(String(255), nullable=False)
    identity: Mapped[str] = mapped_column(
        String(64), nullable=False, default="original_real_photo"
    )
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductAssetPermissionEvent(Base):
    """An immutable grant or withdrawal of product-asset transformation rights."""

    __tablename__ = "product_asset_permission_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id"],
            ["product_assets.customer_id", "product_assets.id"],
            name="fk_product_asset_permission_events_customer_asset",
        ),
        UniqueConstraint(
            "customer_id",
            "id",
            name="uq_product_asset_permission_events_customer_id_id",
        ),
        CheckConstraint(
            "length(trim(provenance)) > 0",
            name="ck_product_asset_permission_events_provenance",
        ),
        Index(
            "ix_product_asset_permission_events_latest",
            "customer_id",
            "product_asset_id",
            "occurred_at",
        ),
        Index(
            "uq_product_asset_permission_events_asset_time",
            "customer_id",
            "product_asset_id",
            "occurred_at",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    transform_permission: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provenance: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductSubjectSeal(Base):
    """An immutable normalized product subject and protected-mask identity."""

    __tablename__ = "product_subject_seals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id"],
            ["product_assets.customer_id", "product_assets.id"],
            name="fk_product_subject_seals_customer_asset",
        ),
        ForeignKeyConstraint(
            ["customer_id", "transform_permission_event_id"],
            [
                "product_asset_permission_events.customer_id",
                "product_asset_permission_events.id",
            ],
            name="fk_product_subject_seals_customer_permission",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_product_subject_seals_customer_id_id"
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_fingerprint",
            name="uq_product_subject_seals_customer_fingerprint",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_product_subject_seals_source_sha256_length",
        ),
        CheckConstraint(
            "length(mask_sha256) = 64",
            name="ck_product_subject_seals_mask_sha256_length",
        ),
        CheckConstraint(
            "length(normalized_subject_sha256) = 64",
            name="ck_product_subject_seals_subject_sha256_length",
        ),
        CheckConstraint(
            "length(idempotency_fingerprint) = 64",
            name="ck_product_subject_seals_fingerprint_length",
        ),
        CheckConstraint(
            "normalized_width > 0 AND normalized_height > 0",
            name="ck_product_subject_seals_dimensions_positive",
        ),
        CheckConstraint(
            "core_mask_pixel_count > 0",
            name="ck_product_subject_seals_core_pixels_positive",
        ),
        CheckConstraint(
            "feather_width_px >= 0",
            name="ck_product_subject_seals_feather_nonnegative",
        ),
        CheckConstraint("seal_version = 1", name="ck_product_subject_seals_version"),
        CheckConstraint(
            "publishable = false", name="ck_product_subject_seals_not_publishable"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    transform_permission_event_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    segmentation_adapter: Mapped[str] = mapped_column(String(100), nullable=False)
    segmentation_adapter_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_subject_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_width: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_height: Mapped[int] = mapped_column(Integer, nullable=False)
    core_mask_pixel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    feather_width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_store_namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    seal_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    synthetic_test_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    publishable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


@event.listens_for(ProductSubjectSeal, "before_update")
def _reject_product_subject_seal_update(
    mapper: Mapper[ProductSubjectSeal],
    connection: Connection,
    target: ProductSubjectSeal,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("product_subject_seal_immutable")


@event.listens_for(ProductSubjectSeal, "before_delete")
def _reject_product_subject_seal_delete(
    mapper: Mapper[ProductSubjectSeal],
    connection: Connection,
    target: ProductSubjectSeal,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("product_subject_seal_immutable")


class ImageOutputJob(Base):
    """A customer-owned request to normalize or generate product images."""

    __tablename__ = "image_output_jobs"
    __mapper_args__: dict[str, Any] = {"eager_defaults": False}  # noqa: RUF012
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "id", name="uq_image_output_jobs_customer_id_id"
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_image_output_jobs_customer_idempotency",
        ),
        ForeignKeyConstraint(
            ["customer_id", "style_card_version_id"],
            ["style_card_versions.customer_id", "style_card_versions.id"],
            name="fk_image_output_jobs_customer_style_version",
        ),
        ForeignKeyConstraint(
            ["customer_id", "scene_style_id"],
            ["scene_styles.customer_id", "scene_styles.id"],
            name="fk_image_output_jobs_customer_scene_style",
        ),
        ForeignKeyConstraint(
            ["customer_id", "subject_seal_id"],
            ["product_subject_seals.customer_id", "product_subject_seals.id"],
            name="fk_image_output_jobs_customer_subject_seal",
        ),
        CheckConstraint(
            "identity_lock_phase IN ('legacy', 'subject_sealed', 'backgrounds_generated', "
            "'backgrounds_safe', 'candidates_attested', 'generation_failed', "
            "'generation_uncertain')",
            name="ck_image_output_jobs_identity_lock_phase",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    output_type: Mapped[str] = mapped_column(String(32), nullable=False)
    style_card_version_id: Mapped[UUID | None] = mapped_column(UUIDType(), index=True)
    scene_style_id: Mapped[UUID | None] = mapped_column(UUIDType(), index=True)
    subject_seal_id: Mapped[UUID | None] = mapped_column(
        UUIDType(), index=True, server_default=text("NULL")
    )
    identity_lock_phase: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="legacy"
    )
    output_ratio: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(100), nullable=False)
    generation_claim_token: Mapped[str | None] = mapped_column(String(36))
    generation_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    artifact_store_identity: Mapped[str | None] = mapped_column(String(100))
    artifact_store_namespace: Mapped[str | None] = mapped_column(String(64))
    artifact_store_capability: Mapped[str | None] = mapped_column(String(128))
    expected_artifact_sequences: Mapped[list[int] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="queued", active_history=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    request_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


@event.listens_for(ImageOutputJob, "before_update")
def _reject_identity_locked_job_lineage_mutation(
    mapper: Mapper[ImageOutputJob],
    connection: Connection,
    target: ImageOutputJob,
) -> None:
    """Once attached, immutable product/style lineage cannot be rewritten."""
    del mapper, connection
    state = inspect(target)
    if target.subject_seal_id is not None and any(
        state.attrs[name].history.has_changes()
        for name in (
            "customer_id",
            "subject_seal_id",
            "style_card_version_id",
            "scene_style_id",
            "output_ratio",
            "idempotency_key",
        )
    ):
        raise DomainRuleError("identity_locked_job_lineage_immutable")


class RealImagePilotAuthorization(Base):
    """One durable, single-use paid-dispatch reservation for a confirmed pilot."""

    __tablename__ = "real_image_pilot_authorizations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_real_pilot_authorizations_customer_job",
        ),
        UniqueConstraint("output_job_id", name="uq_real_pilot_authorization_job"),
        UniqueConstraint("budget_scope", name="uq_real_pilot_authorization_scope"),
        Index("ix_real_pilot_authorizations_customer", "customer_id"),
        CheckConstraint("authorized_usd > 0", name="ck_real_pilot_authorized_positive"),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_real_pilot_dispatch_state",
        ),
        CheckConstraint(
            "outbound_attempts >= 0", name="ck_real_pilot_attempts_nonnegative"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    output_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    # Each confirmed job owns one immutable scope. Its idempotency key prevents
    # replays while a newly confirmed job receives a distinct authorization.
    budget_scope: Mapped[str] = mapped_column(
        String(64), nullable=False, default="real_image_process_budget"
    )
    authorized_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    dispatch_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved"
    )
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class RealStyleAnalysisRun(Base):
    """One durable, single-dispatch paid style-analysis authorization."""

    __tablename__ = "real_style_analysis_runs"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_real_style_analysis_customer_key"
        ),
        CheckConstraint("authorized_usd > 0", name="ck_real_style_analysis_usd_positive"),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_real_style_analysis_dispatch_state",
        ),
        CheckConstraint(
            "outbound_attempts IN (0, 1)", name="ck_real_style_analysis_one_attempt"
        ),
        Index("ix_real_style_analysis_customer", "customer_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    authorized_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    reference_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_selection_version: Mapped[str] = mapped_column(String(100), nullable=False)
    library_sample_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    selected_sample_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    dispatch_state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RealFidelityRun(Base):
    """One durable, single-dispatch paid product-fidelity authorization."""

    __tablename__ = "real_fidelity_runs"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_real_fidelity_customer_key"
        ),
        Index(
            "uq_real_fidelity_customer_suite",
            "customer_id",
            "style_suite_run_id",
            unique=True,
        ),
        CheckConstraint("authorized_usd > 0", name="ck_real_fidelity_usd_positive"),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_real_fidelity_dispatch_state",
        ),
        CheckConstraint(
            "outbound_attempts IN (0, 1)", name="ck_real_fidelity_one_attempt"
        ),
        Index("ix_real_fidelity_customer", "customer_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    style_suite_run_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("style_suite_runs.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    authorized_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    dispatch_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved"
    )
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assessment_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ProductIdentityAnalysisRun(Base):
    """One durable, single-dispatch product identity analysis authorization."""

    __tablename__ = "product_identity_analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_identity_analysis_customer_asset_product",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_product_identity_analysis_customer_id"
        ),
        UniqueConstraint(
            "customer_id",
            "id",
            "product_asset_id",
            "product_id",
            "source_content_sha256",
            name="uq_product_identity_analysis_source_lineage",
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_product_identity_analysis_customer_key",
        ),
        CheckConstraint(
            "length(source_content_sha256) = 64",
            name="ck_product_identity_analysis_source_sha256",
        ),
        CheckConstraint(
            "length(input_manifest_sha256) = 64",
            name="ck_product_identity_analysis_manifest_sha256",
        ),
        CheckConstraint(
            "authorized_usd >= 0", name="ck_product_identity_analysis_usd_nonnegative"
        ),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_product_identity_analysis_dispatch_state",
        ),
        CheckConstraint(
            "outbound_attempts IN (0, 1)",
            name="ck_product_identity_analysis_one_attempt",
        ),
        Index("ix_product_identity_analysis_customer", "customer_id"),
        Index(
            "ix_product_identity_analysis_customer_asset",
            "customer_id",
            "product_asset_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    authorized_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dispatch_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved"
    )
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class ProductIdentityCardVersion(Base):
    """An immutable, customer-confirmed product identity card revision."""

    __tablename__ = "product_identity_card_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_identity_cards_customer_asset_product",
        ),
        ForeignKeyConstraint(
            [
                "customer_id",
                "analysis_run_id",
                "product_asset_id",
                "product_id",
                "source_content_sha256",
            ],
            [
                "product_identity_analysis_runs.customer_id",
                "product_identity_analysis_runs.id",
                "product_identity_analysis_runs.product_asset_id",
                "product_identity_analysis_runs.product_id",
                "product_identity_analysis_runs.source_content_sha256",
            ],
            name="fk_product_identity_cards_analysis_lineage",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_product_identity_cards_customer_id"
        ),
        Index(
            "uq_product_identity_cards_reusable_scope",
            "customer_id",
            "id",
            "product_id",
            "card_sha256",
            unique=True,
        ),
        UniqueConstraint(
            "customer_id",
            "product_asset_id",
            "version",
            name="uq_product_identity_cards_customer_asset_version",
        ),
        UniqueConstraint(
            "analysis_run_id",
            "card_sha256",
            name="uq_product_identity_cards_analysis_hash",
        ),
        CheckConstraint(
            "version > 0", name="ck_product_identity_cards_version_positive"
        ),
        CheckConstraint(
            "length(source_content_sha256) = 64",
            name="ck_product_identity_cards_source_sha256",
        ),
        CheckConstraint(
            "length(card_sha256) = 64", name="ck_product_identity_cards_sha256"
        ),
        CheckConstraint(
            "confirmed_by = 'local_customer'",
            name="ck_product_identity_cards_confirmer",
        ),
        Index(
            "ix_product_identity_cards_customer_asset",
            "customer_id",
            "product_asset_id",
        ),
        Index("ix_product_identity_cards_analysis", "analysis_run_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    analysis_run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    card_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    card_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local_customer"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductIdentityObservationRun(Base):
    """One sealed observation of a new photo against a confirmed product card."""

    __tablename__ = "product_identity_observation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_identity_observation_asset",
        ),
        ForeignKeyConstraint(
            [
                "customer_id",
                "product_identity_card_version_id",
                "product_id",
                "product_identity_card_sha256",
            ],
            [
                "product_identity_card_versions.customer_id",
                "product_identity_card_versions.id",
                "product_identity_card_versions.product_id",
                "product_identity_card_versions.card_sha256",
            ],
            name="fk_product_identity_observation_card",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_product_identity_observation_customer_id"
        ),
        UniqueConstraint(
            "customer_id",
            "id",
            "product_asset_id",
            "product_id",
            "source_content_sha256",
            "product_identity_card_version_id",
            "product_identity_card_sha256",
            "result_sha256",
            name="uq_product_identity_observation_evidence_scope",
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_product_identity_observation_customer_key",
        ),
        CheckConstraint(
            "length(source_content_sha256) = 64 AND "
            "length(product_identity_card_sha256) = 64 AND "
            "length(input_manifest_sha256) = 64 AND length(result_sha256) = 64",
            name="ck_product_identity_observation_digests",
        ),
        CheckConstraint(
            "authorized_usd >= 0",
            name="ck_product_identity_observation_usd_nonnegative",
        ),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', "
            "'provider_responded', 'locally_verified')",
            name="ck_product_identity_observation_dispatch_state",
        ),
        CheckConstraint(
            "outbound_attempts IN (0, 1)",
            name="ck_product_identity_observation_one_attempt",
        ),
        CheckConstraint(
            "decision IN ('pass', 'needs_review', 'block')",
            name="ck_product_identity_observation_decision",
        ),
        Index(
            "ix_product_identity_observation_asset_card",
            "customer_id",
            "product_asset_id",
            "product_identity_card_version_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    product_identity_card_version_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False
    )
    product_identity_card_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    observer: Mapped[str] = mapped_column(String(100), nullable=False)
    observer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    observation_mode: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    authorized_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dispatch_state: Mapped[str] = mapped_column(String(32), nullable=False)
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    decision: Mapped[str | None] = mapped_column(String(16))
    reason_codes: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class ProductIdentityObservationReview(Base):
    """One immutable customer ruling for a non-passing observation."""

    __tablename__ = "product_identity_observation_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "customer_id",
                "observation_run_id",
                "product_asset_id",
                "product_id",
                "source_content_sha256",
                "product_identity_card_version_id",
                "product_identity_card_sha256",
                "observation_result_sha256",
            ],
            [
                "product_identity_observation_runs.customer_id",
                "product_identity_observation_runs.id",
                "product_identity_observation_runs.product_asset_id",
                "product_identity_observation_runs.product_id",
                "product_identity_observation_runs.source_content_sha256",
                "product_identity_observation_runs.product_identity_card_version_id",
                "product_identity_observation_runs.product_identity_card_sha256",
                "product_identity_observation_runs.result_sha256",
            ],
            name="fk_product_identity_observation_review_scope",
        ),
        UniqueConstraint(
            "customer_id",
            "id",
            name="uq_product_identity_observation_reviews_customer_id",
        ),
        UniqueConstraint(
            "observation_run_id",
            name="uq_product_identity_observation_review_run",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_product_identity_observation_review_decision",
        ),
        CheckConstraint(
            "reviewed_by = 'local_customer'",
            name="ck_product_identity_observation_review_reviewer",
        ),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 500",
            name="ck_product_identity_observation_review_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    observation_run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    product_identity_card_version_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False
    )
    product_identity_card_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    observation_result_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="local_customer"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductForegroundVersion(Base):
    """An append-only proposal or customer decision for one extracted foreground."""

    __tablename__ = "product_foreground_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_product_foregrounds_customer_asset_product",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_product_foregrounds_customer_id"
        ),
        UniqueConstraint(
            "customer_id",
            "product_asset_id",
            "version",
            name="uq_product_foregrounds_customer_asset_version",
        ),
        CheckConstraint("version > 0", name="ck_product_foregrounds_version"),
        CheckConstraint("width > 0 AND height > 0", name="ck_product_foregrounds_size"),
        CheckConstraint(
            "length(source_content_sha256) = 64 "
            "AND length(foreground_sha256) = 64 "
            "AND length(mask_sha256) = 64 "
            "AND length(manifest_sha256) = 64",
            name="ck_product_foregrounds_digests",
        ),
        CheckConstraint(
            "state IN ('proposed', 'confirmed', 'rejected')",
            name="ck_product_foregrounds_state",
        ),
        CheckConstraint(
            "(state = 'proposed' AND confirmation_key IS NULL AND decided_at IS NULL) OR "
            "(state IN ('confirmed', 'rejected') AND "
            "length(trim(confirmation_key)) > 0 AND decided_at IS NOT NULL)",
            name="ck_product_foregrounds_decision",
        ),
        Index(
            "ix_product_foregrounds_customer_asset",
            "customer_id",
            "product_asset_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    foreground_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    stage_receipts_sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    quality_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confirmation_key: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class ProductForegroundMaskRevision(Base):
    """Immutable lineage from one foreground mask to a human-corrected proposal."""

    __tablename__ = "product_foreground_mask_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "base_foreground_version_id"],
            [
                "product_foreground_versions.customer_id",
                "product_foreground_versions.id",
            ],
            name="fk_mask_revision_customer_base",
        ),
        ForeignKeyConstraint(
            ["customer_id", "result_foreground_version_id"],
            [
                "product_foreground_versions.customer_id",
                "product_foreground_versions.id",
            ],
            name="fk_mask_revision_customer_result",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_mask_revisions_customer_id"
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_mask_revisions_customer_key",
        ),
        UniqueConstraint(
            "result_foreground_version_id", name="uq_mask_revisions_result"
        ),
        CheckConstraint(
            "base_foreground_version_id != result_foreground_version_id",
            name="ck_mask_revisions_distinct_versions",
        ),
        CheckConstraint(
            "length(source_content_sha256) = 64 "
            "AND length(base_mask_sha256) = 64 "
            "AND length(result_mask_sha256) = 64 "
            "AND length(edit_manifest_sha256) = 64",
            name="ck_mask_revisions_digests",
        ),
        CheckConstraint(
            "editor_version = 'human-mask-editor-v1'",
            name="ck_mask_revisions_editor",
        ),
        CheckConstraint(
            "magic_wand_count >= 0 AND erase_stroke_count >= 0 "
            "AND restore_stroke_count >= 0",
            name="ck_mask_revisions_operation_counts",
        ),
        CheckConstraint(
            "added_pixel_count >= 0 AND deleted_pixel_count >= 0 "
            "AND result_pixel_count > 0 AND component_count > 0",
            name="ck_mask_revisions_metrics",
        ),
        Index(
            "ix_mask_revisions_customer_asset",
            "customer_id",
            "product_asset_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    product_asset_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    base_foreground_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    result_foreground_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    edit_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    editor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    magic_wand_count: Mapped[int] = mapped_column(Integer, nullable=False)
    erase_stroke_count: Mapped[int] = mapped_column(Integer, nullable=False)
    restore_stroke_count: Mapped[int] = mapped_column(Integer, nullable=False)
    added_pixel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_pixel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    result_pixel_count: Mapped[int] = mapped_column(Integer, nullable=False)
    component_count: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ForegroundBenchmarkRun(Base):
    """One customer-owned, digest-bound foreground benchmark execution."""

    __tablename__ = "foreground_benchmark_runs"
    __table_args__ = (
        UniqueConstraint("customer_id", "id", name="uq_foreground_benchmark_runs_customer_id"),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_foreground_benchmark_runs_customer_idempotency",
        ),
        CheckConstraint(
            "length(dataset_seal_sha256) = 64 AND length(model_manifest_sha256) = 64 "
            "AND length(config_sha256) = 64",
            name="ck_foreground_benchmark_runs_digests",
        ),
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_foreground_benchmark_runs_status",
        ),
        CheckConstraint(
            "total_samples > 0 AND completed_samples >= 0 AND failed_samples >= 0 "
            "AND completed_samples + failed_samples <= total_samples",
            name="ck_foreground_benchmark_runs_counts",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND failure_code IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND length(trim(failure_code)) > 0)",
            name="ck_foreground_benchmark_runs_terminal",
        ),
        Index("ix_foreground_benchmark_runs_customer_created", "customer_id", "created_at"),
        Index("ix_foreground_benchmark_runs_dataset", "dataset_seal_sha256"),
        Index("ix_foreground_benchmark_runs_model", "model_manifest_sha256"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    dataset_seal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    total_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ForegroundBenchmarkSampleResult(Base):
    """Immutable path-free metrics for one benchmark sample."""

    __tablename__ = "foreground_benchmark_sample_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["foreground_benchmark_runs.customer_id", "foreground_benchmark_runs.id"],
            name="fk_foreground_benchmark_results_customer_run",
        ),
        UniqueConstraint("run_id", "sample_id", name="uq_foreground_benchmark_result_sample"),
        UniqueConstraint("customer_id", "id", name="uq_foreground_benchmark_results_customer_id"),
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="ck_foreground_benchmark_results_status",
        ),
        CheckConstraint(
            "verdict IN ('auto_eligible', 'human_review_required', 'blocked')",
            name="ck_foreground_benchmark_results_verdict",
        ),
        CheckConstraint(
            "true_positive_pixels >= 0 AND false_positive_pixels >= 0 "
            "AND false_negative_pixels >= 0 AND elapsed_ms >= 0",
            name="ck_foreground_benchmark_results_counts",
        ),
        Index("ix_foreground_benchmark_results_customer_run", "customer_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    sample_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    true_positive_pixels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_positive_pixels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_negative_pixels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iou: Mapped[Decimal] = mapped_column(Numeric(12, 10), nullable=False, default=0)
    foreground_recall: Mapped[Decimal] = mapped_column(Numeric(12, 10), nullable=False, default=0)
    background_contamination: Mapped[Decimal] = mapped_column(Numeric(12, 10), nullable=False, default=0)
    critical_component_recalls: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False, default=dict)
    critical_component_preservation_rate: Mapped[Decimal] = mapped_column(
        Numeric(12, 10), nullable=False, default=0
    )
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ForegroundShadowDecision(Base):
    """Immutable shadow verdict derived from one sample metric record."""

    __tablename__ = "foreground_shadow_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "run_id"],
            ["foreground_benchmark_runs.customer_id", "foreground_benchmark_runs.id"],
            name="fk_foreground_shadow_customer_run",
        ),
        ForeignKeyConstraint(
            ["customer_id", "sample_result_id"],
            [
                "foreground_benchmark_sample_results.customer_id",
                "foreground_benchmark_sample_results.id",
            ],
            name="fk_foreground_shadow_customer_result",
        ),
        ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            ["product_foreground_versions.customer_id", "product_foreground_versions.id"],
            name="fk_foreground_shadow_customer_version",
        ),
        UniqueConstraint("sample_result_id", name="uq_foreground_shadow_result"),
        UniqueConstraint("foreground_version_id", name="uq_foreground_shadow_version"),
        CheckConstraint(
            "(run_id IS NOT NULL AND sample_result_id IS NOT NULL AND foreground_version_id IS NULL) OR "
            "(run_id IS NULL AND sample_result_id IS NULL AND foreground_version_id IS NOT NULL)",
            name="ck_foreground_shadow_context",
        ),
        CheckConstraint(
            "length(model_manifest_sha256) = 64 AND length(config_sha256) = 64",
            name="ck_foreground_shadow_digests",
        ),
        CheckConstraint(
            "verdict IN ('auto_eligible', 'human_review_required', 'blocked')",
            name="ck_foreground_shadow_verdict",
        ),
        Index("ix_foreground_shadow_customer_run", "customer_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(UUIDType())
    sample_result_id: Mapped[UUID | None] = mapped_column(UUIDType())
    foreground_version_id: Mapped[UUID | None] = mapped_column(UUIDType())
    model_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    stage_receipts_sha256: Mapped[str | None] = mapped_column(String(64))
    risk_parameters_sha256: Mapped[str | None] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductForegroundDisagreementEvidence(Base):
    """Append-only coarse-stage evidence used to render a private risk map."""

    __tablename__ = "product_foreground_disagreement_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            ["product_foreground_versions.customer_id", "product_foreground_versions.id"],
            name="fk_foreground_disagreement_customer_version",
        ),
        UniqueConstraint(
            "customer_id",
            "foreground_version_id",
            name="uq_foreground_disagreement_customer_version",
        ),
        CheckConstraint(
            "length(coarse_mask_sha256) = 64",
            name="ck_foreground_disagreement_digest",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    foreground_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    coarse_mask_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class PixelBackgroundRun(Base):
    """One separately authorized, single-attempt empty-background generation."""

    __tablename__ = "pixel_background_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "style_suite_run_id"],
            ["style_suite_runs.customer_id", "style_suite_runs.id"],
            name="fk_pixel_background_customer_suite",
        ),
        ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            ["product_foreground_versions.customer_id", "product_foreground_versions.id"],
            name="fk_pixel_background_customer_foreground",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_pixel_background_customer_id"
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_pixel_background_customer_key",
        ),
        CheckConstraint("maximum_usd > 0", name="ck_pixel_background_usd"),
        CheckConstraint(
            "length(authorized_manifest_sha256) = 64",
            name="ck_pixel_background_manifest_digest",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR length(result_sha256) = 64",
            name="ck_pixel_background_result_digest",
        ),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatch_uncertain', 'provider_responded')",
            name="ck_pixel_background_dispatch_state",
        ),
        CheckConstraint(
            "outbound_attempts IN (0, 1)", name="ck_pixel_background_one_attempt"
        ),
        CheckConstraint(
            "review_state IN ('awaiting_review', 'confirmed', 'rejected')",
            name="ck_pixel_background_review_state",
        ),
        CheckConstraint(
            "(review_state = 'awaiting_review' AND reviewed_at IS NULL) OR "
            "(review_state IN ('confirmed', 'rejected') AND "
            "reviewed_at IS NOT NULL AND result_sha256 IS NOT NULL)",
            name="ck_pixel_background_review_decision",
        ),
        Index("ix_pixel_background_customer_suite", "customer_id", "style_suite_run_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    style_suite_run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    foreground_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    authorized_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authorized_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    maximum_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    dispatch_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reserved"
    )
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    review_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="awaiting_review"
    )
    failure_code: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


class PixelCompositionReceipt(Base):
    """Immutable proof binding confirmed pixels, background, transform, and result."""

    __tablename__ = "pixel_composition_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "foreground_version_id"],
            ["product_foreground_versions.customer_id", "product_foreground_versions.id"],
            name="fk_pixel_receipt_customer_foreground",
        ),
        ForeignKeyConstraint(
            ["customer_id", "background_run_id"],
            ["pixel_background_runs.customer_id", "pixel_background_runs.id"],
            name="fk_pixel_receipt_customer_background",
        ),
        ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_pixel_receipt_customer_candidate",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_pixel_receipts_customer_id"
        ),
        UniqueConstraint(
            "image_candidate_id", name="uq_pixel_receipts_candidate"
        ),
        CheckConstraint(
            "verification_state = 'verified'",
            name="ck_pixel_receipts_verification_state",
        ),
        CheckConstraint("publishable = 0", name="ck_pixel_receipts_not_publishable"),
        CheckConstraint(
            "length(foreground_sha256) = 64 "
            "AND length(background_sha256) = 64 "
            "AND length(result_sha256) = 64 "
            "AND length(transformed_alpha_sha256) = 64 "
            "AND length(foreground_rgb_sha256) = 64",
            name="ck_pixel_receipts_digests",
        ),
        Index("ix_pixel_receipts_customer_foreground", "customer_id", "foreground_version_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False
    )
    foreground_version_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    background_run_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    image_candidate_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    transform_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    compositor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    verification_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="verified"
    )
    foreground_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    background_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    transformed_alpha_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    foreground_rgb_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    publishable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


_PRODUCT_IDENTITY_ANALYSIS_MUTABLE_FIELDS = {
    "dispatch_state",
    "outbound_attempts",
    "proposal",
    "failure_code",
    "updated_at",
}


@event.listens_for(ProductIdentityAnalysisRun, "before_update")
def _guard_product_identity_analysis_update(
    mapper: Mapper[ProductIdentityAnalysisRun],
    connection: Connection,
    target: ProductIdentityAnalysisRun,
) -> None:
    """Permit outcome progress while freezing the authorized input scope."""
    del mapper, connection
    state = inspect(target)
    changed_fields = {
        attribute.key for attribute in state.attrs if attribute.history.has_changes()
    }
    if changed_fields - _PRODUCT_IDENTITY_ANALYSIS_MUTABLE_FIELDS:
        raise DomainRuleError("product_identity_analysis_authorization_immutable")


@event.listens_for(ProductIdentityAnalysisRun, "before_delete")
def _reject_product_identity_analysis_delete(
    mapper: Mapper[ProductIdentityAnalysisRun],
    connection: Connection,
    target: ProductIdentityAnalysisRun,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("product_identity_analysis_authorization_immutable")


@event.listens_for(RealStyleAnalysisRun, "before_update")
def _reject_real_style_analysis_scope_mutation(
    mapper: Mapper[RealStyleAnalysisRun],
    connection: Connection,
    target: RealStyleAnalysisRun,
) -> None:
    del mapper, connection
    state = inspect(target)
    immutable = (
        "customer_id", "idempotency_key", "model", "authorized_usd",
        "reference_manifest_sha256", "reference_selection_version",
        "library_sample_ids", "selected_sample_ids",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise DomainRuleError("real_style_analysis_authorization_immutable")


@event.listens_for(RealFidelityRun, "before_update")
def _reject_real_fidelity_scope_mutation(
    mapper: Mapper[RealFidelityRun],
    connection: Connection,
    target: RealFidelityRun,
) -> None:
    del mapper, connection
    state = inspect(target)
    immutable = (
        "customer_id",
        "style_suite_run_id",
        "idempotency_key",
        "model",
        "authorized_usd",
        "input_manifest_sha256",
        "input_manifest",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise DomainRuleError("real_fidelity_authorization_immutable")


@event.listens_for(RealFidelityRun, "before_delete")
def _reject_real_fidelity_delete(
    mapper: Mapper[RealFidelityRun],
    connection: Connection,
    target: RealFidelityRun,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("real_fidelity_authorization_immutable")


@event.listens_for(RealStyleAnalysisRun, "before_delete")
def _reject_real_style_analysis_delete(
    mapper: Mapper[RealStyleAnalysisRun],
    connection: Connection,
    target: RealStyleAnalysisRun,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("real_style_analysis_authorization_immutable")


@event.listens_for(RealImagePilotAuthorization, "before_update")
def _reject_real_pilot_authorization_mutation(
    mapper: Mapper[RealImagePilotAuthorization],
    connection: Connection,
    target: RealImagePilotAuthorization,
) -> None:
    """The authorized ceiling and sealed job can never be rewritten."""
    del mapper, connection
    state = inspect(target)
    if any(
        state.attrs[name].history.has_changes()
        for name in ("customer_id", "output_job_id", "budget_scope", "authorized_usd")
    ):
        raise DomainRuleError("real_pilot_authorization_immutable")


@event.listens_for(RealImagePilotAuthorization, "before_delete")
def _reject_real_pilot_authorization_delete(
    mapper: Mapper[RealImagePilotAuthorization],
    connection: Connection,
    target: RealImagePilotAuthorization,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("real_pilot_authorization_immutable")


class ImageReviewSession(Base):
    """One initial output's review workflow within a customer handoff."""

    __tablename__ = "image_review_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "initial_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_sessions_customer_initial_job",
        ),
        UniqueConstraint("initial_job_id", name="uq_image_review_sessions_initial_job"),
        ForeignKeyConstraint(
            ["customer_id", "current_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_sessions_customer_job",
        ),
        UniqueConstraint(
            "customer_id", "id", name="uq_image_review_sessions_customer_id_id"
        ),
        CheckConstraint(
            "regeneration_count BETWEEN 0 AND 3",
            name="ck_image_review_sessions_regeneration_count",
        ),
        CheckConstraint("revision >= 0", name="ck_image_review_sessions_revision"),
        CheckConstraint(
            "(status = 'regenerating' AND claim_owner_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND claim_expires_at IS NOT NULL) "
            "OR (status != 'regenerating' AND claim_owner_token IS NULL "
            "AND claimed_at IS NULL AND claim_expires_at IS NULL)",
            name="ck_image_review_sessions_claim_state",
        ),
        CheckConstraint(
            "claim_expires_at IS NULL OR claim_expires_at >= claimed_at",
            name="ck_image_review_sessions_claim_expiry",
        ),
        Index("ix_image_review_sessions_customer_product", "customer_id", "product_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    initial_handoff_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    initial_job_id: Mapped[UUID] = mapped_column(
        UUIDType(),
        nullable=False,
        default=lambda context: context.get_current_parameters()["current_job_id"],
    )
    current_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    regeneration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="awaiting_review"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_owner_token: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )


@event.listens_for(ImageReviewSession, "before_update")
def _guard_review_session_identity(
    mapper: Mapper[ImageReviewSession],
    connection: Connection,
    target: ImageReviewSession,
) -> None:
    del mapper, connection
    if any(
        inspect(target).attrs[field].history.has_changes()
        for field in (
            "customer_id",
            "initial_job_id",
            "initial_handoff_id",
            "product_id",
        )
    ):
        raise DomainRuleError("image_review_session_identity_immutable")


class ImageReviewSessionJob(Base):
    """One permanent customer-owned job assignment to its review workflow."""

    __tablename__ = "image_review_session_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "session_id"],
            ["image_review_sessions.customer_id", "image_review_sessions.id"],
            name="fk_image_review_session_jobs_customer_session",
        ),
        ForeignKeyConstraint(
            ["customer_id", "job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_session_jobs_customer_job",
        ),
        UniqueConstraint(
            "customer_id", "job_id", name="uq_review_session_jobs_customer_job"
        ),
        Index("ix_review_session_jobs_customer_session", "customer_id", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    session_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ImageReviewRound(Base):
    """An immutable record of one candidate-image review round."""

    __tablename__ = "image_review_rounds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "session_id"],
            ["image_review_sessions.customer_id", "image_review_sessions.id"],
            name="fk_image_review_rounds_customer_session",
        ),
        ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_review_rounds_customer_job",
        ),
        ForeignKeyConstraint(
            ["customer_id", "image_review_id", "image_candidate_id"],
            [
                "image_reviews.customer_id",
                "image_reviews.id",
                "image_reviews.image_candidate_id",
            ],
            name="fk_image_review_rounds_customer_review_candidate",
        ),
        UniqueConstraint(
            "session_id", "round_number", name="uq_image_review_rounds_session_number"
        ),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_image_review_rounds_customer_idempotency",
        ),
        CheckConstraint(
            "round_number BETWEEN 1 AND 4",
            name="ck_image_review_rounds_number",
        ),
        Index("ix_image_review_rounds_customer_session", "customer_id", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    session_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    output_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    image_review_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    image_candidate_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ProductAssetLink(Base):
    """An immutable source-asset association for an image output job."""

    __tablename__ = "product_asset_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "product_asset_id"],
            ["product_assets.customer_id", "product_assets.id"],
            name="fk_product_asset_links_customer_asset",
        ),
        ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_product_asset_links_customer_job",
        ),
        UniqueConstraint(
            "output_job_id", "product_asset_id", name="uq_product_asset_links_job_asset"
        ),
        UniqueConstraint(
            "output_job_id", "position", name="uq_product_asset_links_job_position"
        ),
        Index(
            "uq_product_asset_links_customer_link_asset_job",
            "customer_id",
            "id",
            "product_asset_id",
            "output_job_id",
            unique=True,
        ),
        CheckConstraint(
            "position BETWEEN 1 AND 5", name="ck_product_asset_links_position"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    product_asset_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    output_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ImageCandidate(Base):
    """An immutable generated or normalized candidate artifact."""

    __tablename__ = "image_candidates"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "id", name="uq_image_candidates_customer_id_id"
        ),
        Index(
            "uq_image_candidates_customer_id_job",
            "customer_id",
            "id",
            "output_job_id",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_image_candidates_customer_job",
        ),
        UniqueConstraint(
            "output_job_id", "sequence_number", name="uq_image_candidates_job_sequence"
        ),
        CheckConstraint(
            "sequence_number BETWEEN 1 AND 3",
            name="ck_image_candidates_sequence_number",
        ),
        CheckConstraint("width > 0", name="ck_image_candidates_width_positive"),
        CheckConstraint("height > 0", name="ck_image_candidates_height_positive"),
        CheckConstraint(
            "length(content_sha256) = 64", name="ck_image_candidates_sha256_length"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    output_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    identity: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class AuthenticityCheck(Base):
    """An immutable machine-check result for one image candidate."""

    __tablename__ = "authenticity_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_authenticity_checks_customer_candidate",
        ),
        UniqueConstraint(
            "image_candidate_id",
            "check_type",
            "attempt",
            name="uq_authenticity_checks_candidate_type_attempt",
        ),
        CheckConstraint("attempt > 0", name="ck_authenticity_checks_attempt_positive"),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    image_candidate_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    check_type: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    checker_version: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class AuthenticityCompletion(Base):
    """Append-only proof that Task 5 atomically sealed one complete batch."""

    __tablename__ = "authenticity_completions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "output_job_id"],
            ["image_output_jobs.customer_id", "image_output_jobs.id"],
            name="fk_authenticity_completions_customer_job",
        ),
        UniqueConstraint(
            "output_job_id", name="uq_authenticity_completions_output_job"
        ),
        CheckConstraint(
            "length(candidate_batch_digest) = 64",
            name="ck_authenticity_completions_digest_length",
        ),
        CheckConstraint(
            "result_status IN ('awaiting_customer_review', 'authenticity_failed')",
            name="ck_authenticity_completions_result_status",
        ),
        CheckConstraint(
            "previous_status = 'machine_check'",
            name="ck_authenticity_completions_previous_status",
        ),
        CheckConstraint(
            "checker_version = 'deterministic-local-authenticity-rules-v1'",
            name="ck_authenticity_completions_checker_version",
        ),
        CheckConstraint(
            "check_mode = 'synthetic_local_rule_based'",
            name="ck_authenticity_completions_check_mode",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    output_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    checker_version: Mapped[str] = mapped_column(String(100), nullable=False)
    check_mode: Mapped[str] = mapped_column(String(100), nullable=False)
    candidate_batch_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(64), nullable=False)
    result_status: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ImageReview(Base):
    """An immutable customer decision on one machine-checked candidate."""

    __tablename__ = "image_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "image_candidate_id"],
            ["image_candidates.customer_id", "image_candidates.id"],
            name="fk_image_reviews_customer_candidate",
        ),
        UniqueConstraint("image_candidate_id", name="uq_image_reviews_candidate"),
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_image_reviews_customer_idempotency",
        ),
        Index(
            "uq_image_reviews_customer_id_candidate",
            "customer_id",
            "id",
            "image_candidate_id",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    image_candidate_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


@event.listens_for(ImageReview, "before_insert")
def _validate_image_review_decision(
    mapper: Mapper[ImageReview], connection: Connection, target: ImageReview
) -> None:
    """Reject persistence calls that bypass the strict HTTP decision schema."""
    del mapper, connection
    if target.decision not in {
        "approved",
        "not_my_style",
        "product_detail_inaccurate",
        "rejected",
    }:
        raise DomainRuleError("invalid_image_review_decision")


class ApprovedImageAssociation(Base):
    """An immutable approved derivative-to-real-source product association."""

    __tablename__ = "approved_image_associations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "image_candidate_id", "output_job_id"],
            [
                "image_candidates.customer_id",
                "image_candidates.id",
                "image_candidates.output_job_id",
            ],
            name="fk_approved_image_associations_customer_candidate_job",
        ),
        ForeignKeyConstraint(
            [
                "customer_id",
                "product_asset_link_id",
                "source_asset_id",
                "output_job_id",
            ],
            [
                "product_asset_links.customer_id",
                "product_asset_links.id",
                "product_asset_links.product_asset_id",
                "product_asset_links.output_job_id",
            ],
            name="fk_approved_image_associations_customer_source_link_job",
        ),
        ForeignKeyConstraint(
            ["customer_id", "source_asset_id", "product_id"],
            [
                "product_assets.customer_id",
                "product_assets.id",
                "product_assets.product_id",
            ],
            name="fk_approved_image_associations_customer_source_product",
        ),
        ForeignKeyConstraint(
            ["customer_id", "image_review_id", "image_candidate_id"],
            [
                "image_reviews.customer_id",
                "image_reviews.id",
                "image_reviews.image_candidate_id",
            ],
            name="fk_approved_image_associations_customer_review_candidate",
        ),
        Index(
            "uq_approved_image_associations_candidate_source",
            "image_candidate_id",
            "source_asset_id",
            unique=True,
        ),
        UniqueConstraint(
            "image_review_id",
            "source_asset_id",
            name="uq_approved_image_associations_review_source",
        ),
        CheckConstraint(
            "image_kind IN ('real_photo', 'style_image')",
            name="ck_approved_image_associations_image_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    image_review_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    image_candidate_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    output_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False, index=True)
    product_asset_link_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    source_asset_id: Mapped[UUID] = mapped_column(
        UUIDType(), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    image_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


@event.listens_for(ApprovedImageAssociation, "before_insert")
def _require_approved_review_for_association(
    mapper: Mapper[ApprovedImageAssociation],
    connection: Connection,
    target: ApprovedImageAssociation,
) -> None:
    """Keep ORM-created associations bound to the exact approved decision."""
    del mapper
    approved = connection.execute(
        select(ImageReview.id).where(
            ImageReview.customer_id == target.customer_id,
            ImageReview.id == target.image_review_id,
            ImageReview.image_candidate_id == target.image_candidate_id,
            ImageReview.decision == "approved",
        )
    ).scalar_one_or_none()
    if approved is None:
        raise DomainRuleError("association_requires_approved_review")


_OUTPUT_IMMUTABILITY_ERRORS: dict[type[Base], str] = {
    ProductAsset: "product_asset_immutable",
    ProductAssetPermissionEvent: "product_asset_permission_event_immutable",
    ProductIdentityCardVersion: "product_identity_card_immutable",
    ProductAssetLink: "product_asset_link_immutable",
    ImageCandidate: "image_candidate_immutable",
    AuthenticityCheck: "authenticity_check_immutable",
    AuthenticityCompletion: "authenticity_completion_immutable",
    ImageReviewRound: "image_review_round_immutable",
    ImageReviewSessionJob: "image_review_session_job_immutable",
    ImageReview: "image_review_immutable",
    ApprovedImageAssociation: "approved_image_association_immutable",
}


@event.listens_for(ProductIdentityObservationRun, "before_update")
def _guard_product_identity_observation_update(
    mapper: Mapper[Any],
    connection: Connection,
    target: ProductIdentityObservationRun,
) -> None:
    del mapper, connection
    state = inspect(target)
    mutable = {
        "dispatch_state",
        "outbound_attempts",
        "observation_payload",
        "decision",
        "reason_codes",
        "result_sha256",
        "failure_code",
        "updated_at",
    }
    changed = {attribute.key for attribute in state.attrs if attribute.history.has_changes()}
    dispatch_history = state.attrs.dispatch_state.history
    previous_dispatch = dispatch_history.deleted[0] if dispatch_history.deleted else target.dispatch_state
    if changed - mutable or previous_dispatch in {"provider_responded", "locally_verified"}:
        raise DomainRuleError("product_identity_observation_immutable")


@event.listens_for(ProductIdentityObservationRun, "before_delete")
def _reject_product_identity_observation_delete(
    mapper: Mapper[Any],
    connection: Connection,
    target: ProductIdentityObservationRun,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("product_identity_observation_immutable")


@event.listens_for(ProductIdentityObservationRun, "before_insert")
def _validate_product_identity_observation_scope(
    mapper: Mapper[ProductIdentityObservationRun],
    connection: Connection,
    target: ProductIdentityObservationRun,
) -> None:
    del mapper
    source = connection.scalar(
        select(ProductAsset.id).where(
            ProductAsset.customer_id == target.customer_id,
            ProductAsset.id == target.product_asset_id,
            ProductAsset.product_id == target.product_id,
            ProductAsset.content_sha256 == target.source_content_sha256,
            ProductAsset.confirmed.is_(True),
            ProductAsset.identity == "original_real_photo",
        )
    )
    card = connection.scalar(
        select(ProductIdentityCardVersion.id).where(
            ProductIdentityCardVersion.customer_id == target.customer_id,
            ProductIdentityCardVersion.id
            == target.product_identity_card_version_id,
            ProductIdentityCardVersion.product_id == target.product_id,
            ProductIdentityCardVersion.card_sha256
            == target.product_identity_card_sha256,
        )
    )
    if source is None or card is None:
        raise IntegrityError(
            "product_identity_observation_scope_mismatch",
            {},
            RuntimeError("product_identity_observation_scope_mismatch"),
        )


@event.listens_for(ProductIdentityObservationReview, "before_insert")
def _validate_product_identity_observation_review(
    mapper: Mapper[ProductIdentityObservationReview],
    connection: Connection,
    target: ProductIdentityObservationReview,
) -> None:
    del mapper
    decision = connection.scalar(
        select(ProductIdentityObservationRun.decision).where(
            ProductIdentityObservationRun.customer_id == target.customer_id,
            ProductIdentityObservationRun.id == target.observation_run_id,
            ProductIdentityObservationRun.product_asset_id == target.product_asset_id,
            ProductIdentityObservationRun.product_id == target.product_id,
            ProductIdentityObservationRun.source_content_sha256
            == target.source_content_sha256,
            ProductIdentityObservationRun.product_identity_card_version_id
            == target.product_identity_card_version_id,
            ProductIdentityObservationRun.product_identity_card_sha256
            == target.product_identity_card_sha256,
            ProductIdentityObservationRun.result_sha256
            == target.observation_result_sha256,
        )
    )
    if decision not in {"needs_review", "block"}:
        raise DomainRuleError("product_identity_observation_review_invalid")


@event.listens_for(ProductIdentityObservationReview, "before_update")
@event.listens_for(ProductIdentityObservationReview, "before_delete")
def _reject_product_identity_observation_review_mutation(
    mapper: Mapper[Any],
    connection: Connection,
    target: ProductIdentityObservationReview,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("product_identity_observation_review_immutable")


@event.listens_for(ForegroundBenchmarkRun, "before_update")
def _validate_foreground_benchmark_run_update(
    mapper: Mapper[Any], connection: Connection, target: ForegroundBenchmarkRun
) -> None:
    """Allow progress while running and exactly one transition to a terminal state."""
    del mapper, connection
    state = inspect(target)
    immutable = (
        "customer_id",
        "dataset_seal_sha256",
        "model_manifest_sha256",
        "config_sha256",
        "total_samples",
        "created_at",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise DomainRuleError("foreground_benchmark_evidence_immutable")
    history = state.attrs.status.history
    if history.has_changes():
        previous = history.deleted[0] if history.deleted else None
        if previous != "running" or target.status not in {"completed", "failed"}:
            raise DomainRuleError("foreground_benchmark_evidence_immutable")
    elif target.status != "running":
        raise DomainRuleError("foreground_benchmark_evidence_immutable")


@event.listens_for(ForegroundBenchmarkRun, "before_delete")
@event.listens_for(ForegroundBenchmarkSampleResult, "before_update")
@event.listens_for(ForegroundBenchmarkSampleResult, "before_delete")
@event.listens_for(ForegroundShadowDecision, "before_update")
@event.listens_for(ForegroundShadowDecision, "before_delete")
def _reject_foreground_benchmark_evidence_mutation(
    mapper: Mapper[Any], connection: Connection, target: Base
) -> None:
    del mapper, connection, target
    raise DomainRuleError("foreground_benchmark_evidence_immutable")


@event.listens_for(ProductAsset, "before_update")
@event.listens_for(ProductAssetPermissionEvent, "before_update")
@event.listens_for(ProductIdentityCardVersion, "before_update")
@event.listens_for(ProductAssetLink, "before_update")
@event.listens_for(ImageCandidate, "before_update")
@event.listens_for(AuthenticityCheck, "before_update")
@event.listens_for(AuthenticityCompletion, "before_update")
@event.listens_for(ImageReviewRound, "before_update")
@event.listens_for(ImageReviewSessionJob, "before_update")
@event.listens_for(ImageReview, "before_update")
@event.listens_for(ApprovedImageAssociation, "before_update")
def _reject_output_history_update(
    mapper: Mapper[Any], connection: Connection, target: Base
) -> None:
    """Keep ORM changes from overwriting original assets or output history."""
    del mapper, connection
    raise DomainRuleError(_OUTPUT_IMMUTABILITY_ERRORS[type(target)])


@event.listens_for(ProductAsset, "before_delete")
@event.listens_for(ProductAssetPermissionEvent, "before_delete")
@event.listens_for(ProductIdentityCardVersion, "before_delete")
@event.listens_for(ProductAssetLink, "before_delete")
@event.listens_for(ImageCandidate, "before_delete")
@event.listens_for(AuthenticityCheck, "before_delete")
@event.listens_for(AuthenticityCompletion, "before_delete")
@event.listens_for(ImageReviewRound, "before_delete")
@event.listens_for(ImageReviewSessionJob, "before_delete")
@event.listens_for(ImageReview, "before_delete")
@event.listens_for(ApprovedImageAssociation, "before_delete")
def _reject_output_history_delete(
    mapper: Mapper[Any], connection: Connection, target: Base
) -> None:
    """Keep ORM changes from deleting original assets or output history."""
    del mapper, connection
    raise DomainRuleError(_OUTPUT_IMMUTABILITY_ERRORS[type(target)])


_IMAGE_OUTPUT_JOB_MUTABLE_FIELDS = {
    "status",
    "failure_reason",
    "identity_lock_phase",
    "generation_claim_token",
    "generation_lease_expires_at",
    "updated_at",
}


@event.listens_for(ImageOutputJob, "before_update")
def _guard_image_output_job_update(
    mapper: Mapper[ImageOutputJob],
    connection: Connection,
    target: ImageOutputJob,
) -> None:
    """Allow only legal state progress while freezing job provenance."""
    del mapper
    state = inspect(target)
    changed_fields = {
        attribute.key for attribute in state.attrs if attribute.history.has_changes()
    }
    if changed_fields - _IMAGE_OUTPUT_JOB_MUTABLE_FIELDS:
        raise DomainRuleError("image_output_job_provenance_immutable")
    if "status" not in changed_fields:
        return
    status_history = state.attrs.status.history
    if status_history.deleted:
        current_status = status_history.deleted[0]
    else:
        current_status = connection.execute(
            select(ImageOutputJob.status).where(ImageOutputJob.id == target.id)
        ).scalar_one()
    validate_job_status_transition(current_status, target.status)


@event.listens_for(ImageOutputJob, "before_delete")
def _reject_image_output_job_delete(
    mapper: Mapper[ImageOutputJob],
    connection: Connection,
    target: ImageOutputJob,
) -> None:
    """Keep the job and its frozen provenance available for traceability."""
    del mapper, connection, target
    raise DomainRuleError("image_output_job_provenance_immutable")


class AuditEvent(Base):
    """An append-only customer-owned record of a material style action."""

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ClassificationBridgeClaim(Base):
    """One durable fence for a producer handoff before consumer side effects."""

    __tablename__ = "classification_bridge_claims"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "handoff_id", name="uq_bridge_claim_customer_handoff"
        ),
        UniqueConstraint(
            "customer_id", "idempotency_key", name="uq_bridge_claim_customer_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("customers.id"), nullable=False, index=True
    )
    handoff_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    handoff_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    owner_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


@event.listens_for(StyleCardVersion, "before_update")
def _reject_style_card_version_update(
    mapper: Mapper[StyleCardVersion], connection: Connection, target: StyleCardVersion
) -> None:
    """Keep ORM changes from silently rewriting a historical version."""
    del mapper, connection, target
    raise DomainRuleError("style_card_version_immutable")


@event.listens_for(StyleCardVersion, "before_delete")
def _reject_style_card_version_delete(
    mapper: Mapper[StyleCardVersion], connection: Connection, target: StyleCardVersion
) -> None:
    """Keep ORM changes from silently deleting a historical version."""
    del mapper, connection, target
    raise DomainRuleError("style_card_version_immutable")


@event.listens_for(StyleCardSampleLink, "before_update")
def _reject_style_card_sample_link_update(
    mapper: Mapper[StyleCardSampleLink],
    connection: Connection,
    target: StyleCardSampleLink,
) -> None:
    """Keep ORM changes from rewriting historical evidence associations."""
    del mapper, connection, target
    raise DomainRuleError("style_card_sample_link_immutable")


@event.listens_for(StyleCardSampleLink, "before_delete")
def _reject_style_card_sample_link_delete(
    mapper: Mapper[StyleCardSampleLink],
    connection: Connection,
    target: StyleCardSampleLink,
) -> None:
    """Keep ORM changes from deleting historical evidence associations."""
    del mapper, connection, target
    raise DomainRuleError("style_card_sample_link_immutable")


@event.listens_for(StyleReferencePack, "before_update")
@event.listens_for(StyleReferencePack, "before_delete")
def _reject_style_reference_pack_mutation(
    mapper: Mapper[StyleReferencePack],
    connection: Connection,
    target: StyleReferencePack,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("style_reference_pack_immutable")


@event.listens_for(StyleReferencePackItem, "before_update")
@event.listens_for(StyleReferencePackItem, "before_delete")
def _reject_style_reference_pack_item_mutation(
    mapper: Mapper[StyleReferencePackItem],
    connection: Connection,
    target: StyleReferencePackItem,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("style_reference_pack_immutable")


@event.listens_for(AssetStoreRecoveryBinding, "before_update")
@event.listens_for(AssetStoreRecoveryBinding, "before_delete")
def _reject_asset_store_recovery_binding_mutation(
    mapper: Mapper[AssetStoreRecoveryBinding],
    connection: Connection,
    target: AssetStoreRecoveryBinding,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("asset_store_recovery_binding_immutable")


@event.listens_for(StyleFieldEvidence, "before_update")
@event.listens_for(StyleFieldEvidence, "before_delete")
def _reject_style_field_evidence_mutation(
    mapper: Mapper[StyleFieldEvidence],
    connection: Connection,
    target: StyleFieldEvidence,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("style_field_evidence_immutable")


@event.listens_for(StyleTemplateSlot, "before_update")
@event.listens_for(StyleTemplateSlot, "before_delete")
def _reject_style_template_slot_mutation(
    mapper: Mapper[StyleTemplateSlot], connection: Connection, target: StyleTemplateSlot
) -> None:
    del mapper, connection, target
    raise DomainRuleError("style_template_slot_immutable")


@event.listens_for(StyleSuiteRun, "before_update")
@event.listens_for(StyleSuiteRunItem, "before_update")
def _reject_style_suite_fact_update(
    mapper: Mapper[Any],
    connection: Connection,
    target: StyleSuiteRun | StyleSuiteRunItem,
) -> None:
    del mapper
    state = cast(InstanceState[Any], inspect(target))
    if (
        isinstance(target, StyleSuiteRun)
        and state.attrs.state.history.has_changes()
        and target.state == "completed"
        and not _style_suite_run_is_approvable(
            connection, target.customer_id, target.id
        )
    ):
        raise DomainRuleError("style_suite_completion_requirements_not_met")
    execution_fields = {"state"}
    if isinstance(target, StyleSuiteRunItem):
        execution_fields.update(
            {
                "output_job_id",
                "failure_reason",
                "selected_candidate_id",
                "selected_review_id",
                "fidelity_state",
                "fidelity_flags",
            }
        )
        output_history = state.attrs.output_job_id.history
        if output_history.deleted and output_history.deleted[0] is not None:
            raise DomainRuleError("style_suite_item_job_immutable")
        if target.output_job_id is not None and not _style_suite_item_job_matches(
            connection, target
        ):
            raise DomainRuleError("style_suite_item_job_mismatch")
        for field in (
            "selected_candidate_id",
            "selected_review_id",
            "fidelity_state",
            "fidelity_flags",
        ):
            history = state.attrs[field].history
            if history.deleted and history.deleted[0] is not None:
                raise DomainRuleError("style_suite_item_selection_immutable")
        if (
            target.selected_candidate_id is not None
            and not _style_suite_selection_matches(connection, target)
        ):
            raise DomainRuleError("style_suite_selection_lineage_mismatch")
    if any(
        attr.history.has_changes()
        for attr in state.attrs
        if attr.key not in execution_fields
    ):
        code = (
            "style_suite_run_immutable"
            if isinstance(target, StyleSuiteRun)
            else "style_suite_run_item_immutable"
        )
        raise DomainRuleError(code)


@event.listens_for(StyleSuiteCompletion, "before_update")
@event.listens_for(StyleSuiteCompletion, "before_delete")
@event.listens_for(StyleSuiteCandidateFidelity, "before_delete")
def _reject_style_suite_completion_mutation(
    mapper: Mapper[Any],
    connection: Connection,
    target: StyleSuiteCompletion | StyleSuiteCandidateFidelity,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("style_suite_completion_immutable")


@event.listens_for(StyleSuiteCandidateFidelity, "before_update")
def _allow_only_fidelity_warning_acknowledgement(
    mapper: Mapper[StyleSuiteCandidateFidelity],
    connection: Connection,
    target: StyleSuiteCandidateFidelity,
) -> None:
    """Keep the machine result immutable while allowing one human ack transition."""
    del mapper, connection
    state = inspect(target)
    warning = state.attrs.warning_acknowledged.history
    changed_elsewhere = any(
        attribute.history.has_changes()
        for name, attribute in state.attrs.items()
        if name != "warning_acknowledged"
    )
    if (
        changed_elsewhere
        or tuple(warning.deleted) != (False,)
        or tuple(warning.added) != (True,)
    ):
        raise DomainRuleError("style_suite_candidate_fidelity_immutable")


@event.listens_for(StyleSuiteCandidateFidelity, "before_insert")
def _validate_style_suite_candidate_fidelity(
    mapper: Mapper[StyleSuiteCandidateFidelity],
    connection: Connection,
    target: StyleSuiteCandidateFidelity,
) -> None:
    del mapper
    allowed = {
        "subject_incomplete",
        "component_count_changed",
        "primary_color_changed",
        "core_hardware_changed",
        "product_form_changed",
        "product_category_changed",
        "component_shape_changed",
        "component_arrangement_changed",
        "core_accessory_changed",
        "provider_check_unavailable",
        "minor_color_drift",
        "minor_detail_uncertain",
        "background_style_drift",
    }
    flags = tuple(sorted(set(target.fidelity_flags)))
    valid_decision = (
        not (set(flags) - allowed)
        and (target.fidelity_state != "green" or not flags)
        and (target.fidelity_state != "red" or bool(flags))
        and (
            target.fidelity_state != "yellow"
            or (
                bool(flags)
                and set(flags)
                <= {
                    "minor_color_drift",
                    "minor_detail_uncertain",
                    "background_style_drift",
                }
            )
        )
        and (
            target.fidelity_state != "unknown"
            or flags == ("provider_check_unavailable",)
        )
    )
    lineage = connection.execute(
        select(StyleSuiteRunItem.id)
        .join(
            ImageCandidate,
            (ImageCandidate.id == target.image_candidate_id)
            & (ImageCandidate.customer_id == target.customer_id)
            & (ImageCandidate.output_job_id == target.output_job_id),
        )
        .where(
            StyleSuiteRunItem.id == target.run_item_id,
            StyleSuiteRunItem.customer_id == target.customer_id,
            StyleSuiteRunItem.output_job_id == target.output_job_id,
        )
    ).first()
    if not valid_decision or lineage is None:
        raise DomainRuleError("style_suite_candidate_fidelity_invalid")


@event.listens_for(StyleSuiteCompletion, "before_insert")
def _validate_style_suite_completion_insert(
    mapper: Mapper[StyleSuiteCompletion],
    connection: Connection,
    target: StyleSuiteCompletion,
) -> None:
    del mapper
    if target.version != 1:
        raise DomainRuleError("style_suite_completion_version_invalid")
    run = (
        connection.execute(
            select(
                StyleSuiteRun.state,
                StyleSuiteRun.product_id,
                StyleSuiteRun.source_asset_id,
                StyleSuiteRun.source_content_sha256,
            ).where(
                StyleSuiteRun.customer_id == target.customer_id,
                StyleSuiteRun.id == target.run_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    rows = _style_suite_completion_rows(connection, target.customer_id, target.run_id)
    expected = [
        {
            "item_id": str(row.id),
            "style_version_id": str(row.scene_style_version_id),
            "slot_key": row.slot_key,
            "selected_candidate_id": str(row.selected_candidate_id),
            "selected_review_id": str(row.selected_review_id),
            "fidelity_state": row.fidelity_state,
            "fidelity_flags": row.fidelity_flags,
        }
        for row in rows
    ]
    snapshot = target.snapshot
    if (
        run is None
        or run["state"] != "completed"
        or snapshot.get("run_id") != str(target.run_id)
        or snapshot.get("product_id") != run["product_id"]
        or snapshot.get("source_asset_id") != str(run["source_asset_id"])
        or snapshot.get("source_content_sha256") != run["source_content_sha256"]
        or snapshot.get("items") != expected
    ):
        raise DomainRuleError("style_suite_completion_snapshot_invalid")


def _style_suite_completion_rows(
    connection: Connection, customer_id: UUID, run_id: UUID
) -> list[Any]:
    return list(
        connection.execute(
            select(
                StyleSuiteRunItem.id,
                StyleSuiteRunItem.scene_style_version_id,
                StyleSuiteRunItem.slot_key,
                StyleSuiteRunItem.selected_candidate_id,
                StyleSuiteRunItem.selected_review_id,
                StyleSuiteRunItem.fidelity_state,
                StyleSuiteRunItem.fidelity_flags,
                StyleSuiteRunItem.state,
                StyleSuiteRunItem.output_job_id,
            )
            .where(
                StyleSuiteRunItem.customer_id == customer_id,
                StyleSuiteRunItem.run_id == run_id,
            )
            .order_by(StyleSuiteRunItem.style_name, StyleSuiteRunItem.slot_key)
        )
    )


def _style_suite_run_is_approvable(
    connection: Connection, customer_id: UUID, run_id: UUID
) -> bool:
    rows = _style_suite_completion_rows(connection, customer_id, run_id)
    if not rows:
        return False
    for row in rows:
        yellow_flags = {
            "minor_color_drift",
            "minor_detail_uncertain",
            "background_style_drift",
        }
        fidelity_valid = (
            row.fidelity_state == "green" and row.fidelity_flags == []
        ) or (
            row.fidelity_state == "yellow"
            and bool(row.fidelity_flags)
            and set(row.fidelity_flags) <= yellow_flags
            and connection.scalar(
                select(StyleSuiteCandidateFidelity.warning_acknowledged).where(
                    StyleSuiteCandidateFidelity.customer_id == customer_id,
                    StyleSuiteCandidateFidelity.image_candidate_id
                    == row.selected_candidate_id,
                )
            )
            is True
        )
        if (
            row.state != "approved"
            or not fidelity_valid
            or row.selected_candidate_id is None
            or row.selected_review_id is None
        ):
            return False
        valid = connection.execute(
            select(ImageCandidate.id)
            .join(
                ImageReview,
                (ImageReview.id == row.selected_review_id)
                & (ImageReview.customer_id == customer_id)
                & (ImageReview.image_candidate_id == ImageCandidate.id)
                & (ImageReview.decision == "approved"),
            )
            .join(
                ImageOutputJob,
                (ImageOutputJob.id == ImageCandidate.output_job_id)
                & (ImageOutputJob.customer_id == customer_id)
                & (ImageOutputJob.status == "customer_approved"),
            )
            .where(
                ImageCandidate.id == row.selected_candidate_id,
                ImageCandidate.customer_id == customer_id,
                ImageCandidate.output_job_id == row.output_job_id,
            )
        ).first()
        if valid is None:
            return False
    return True


@event.listens_for(StyleSuiteRunItem, "before_insert")
def _validate_initial_style_suite_job(
    mapper: Mapper[StyleSuiteRunItem],
    connection: Connection,
    target: StyleSuiteRunItem,
) -> None:
    del mapper
    if target.output_job_id is not None and not _style_suite_item_job_matches(
        connection, target
    ):
        raise DomainRuleError("style_suite_item_job_mismatch")


def _style_suite_item_job_matches(
    connection: Connection, target: StyleSuiteRunItem
) -> bool:
    row = connection.execute(
        select(
            StyleSuiteRun.id,
            StyleSuiteRun.product_id,
            StyleSuiteRun.source_asset_id,
            StyleSuiteRun.source_content_sha256,
            ImageOutputJob.id,
            ImageOutputJob.output_type,
            ImageOutputJob.style_card_version_id,
            ImageOutputJob.scene_style_id,
            ImageOutputJob.idempotency_key,
            ImageOutputJob.request_metadata,
            ProductAsset.id,
            ProductAsset.product_id,
            ProductAsset.content_sha256,
        )
        .select_from(StyleSuiteRun)
        .join(
            ImageOutputJob,
            (ImageOutputJob.id == target.output_job_id)
            & (ImageOutputJob.customer_id == target.customer_id),
        )
        .join(
            ProductAssetLink,
            (ProductAssetLink.output_job_id == ImageOutputJob.id)
            & (ProductAssetLink.customer_id == target.customer_id),
        )
        .join(
            ProductAsset,
            (ProductAsset.id == ProductAssetLink.product_asset_id)
            & (ProductAsset.customer_id == target.customer_id),
        )
        .where(
            StyleSuiteRun.id == target.run_id,
            StyleSuiteRun.customer_id == target.customer_id,
        )
    ).first()
    if row is None:
        return False
    (
        run_id,
        run_product_id,
        run_source_asset_id,
        run_source_sha256,
        job_id,
        job_output_type,
        job_style_version_id,
        job_scene_style_id,
        job_idempotency_key,
        job_metadata,
        asset_id,
        asset_product_id,
        asset_sha256,
    ) = row
    expected_key = (
        f"suite:{run_id}:{target.scene_style_version_id}:"
        f"{target.slot_key}:{run_source_sha256}"
    )
    if target.style_reference_manifest_sha256 is not None:
        expected_key = f"{expected_key}:{target.style_reference_manifest_sha256}"
    sources = job_metadata.get("sources")
    link_count = connection.scalar(
        select(func.count())
        .select_from(ProductAssetLink)
        .where(ProductAssetLink.output_job_id == job_id)
    )
    return bool(
        job_output_type == "product_style"
        and job_style_version_id == target.scene_style_version_id
        and job_scene_style_id == target.scene_style_id
        and job_idempotency_key == expected_key
        and job_metadata.get("slot_key") == target.slot_key
        and sources
        == [{"product_id": run_product_id, "asset_id": str(run_source_asset_id)}]
        and link_count == 1
        and asset_id == run_source_asset_id
        and asset_product_id == run_product_id
        and asset_sha256 == run_source_sha256
    )


def _style_suite_selection_matches(
    connection: Connection, target: StyleSuiteRunItem
) -> bool:
    if target.selected_review_id is None:
        return False
    yellow_flags = {
        "minor_color_drift",
        "minor_detail_uncertain",
        "background_style_drift",
    }
    fidelity_valid = (
        target.fidelity_state == "green" and target.fidelity_flags in ([], ())
    ) or (
        target.fidelity_state == "yellow"
        and bool(target.fidelity_flags)
        and set(target.fidelity_flags or ()) <= yellow_flags
        and connection.scalar(
            select(StyleSuiteCandidateFidelity.warning_acknowledged).where(
                StyleSuiteCandidateFidelity.customer_id == target.customer_id,
                StyleSuiteCandidateFidelity.image_candidate_id
                == target.selected_candidate_id,
            )
        )
        is True
    )
    if not fidelity_valid:
        return False
    return (
        connection.execute(
            select(ImageCandidate.id)
            .join(
                ImageReview,
                (ImageReview.id == target.selected_review_id)
                & (ImageReview.customer_id == target.customer_id)
                & (ImageReview.image_candidate_id == ImageCandidate.id)
                & (ImageReview.decision == "approved"),
            )
            .where(
                ImageCandidate.id == target.selected_candidate_id,
                ImageCandidate.customer_id == target.customer_id,
                ImageCandidate.output_job_id == target.output_job_id,
            )
        ).first()
        is not None
    )


@event.listens_for(StyleSuiteRun, "before_delete")
@event.listens_for(StyleSuiteRunItem, "before_delete")
def _reject_style_suite_delete(
    mapper: Mapper[Any],
    connection: Connection,
    target: StyleSuiteRun | StyleSuiteRunItem,
) -> None:
    del mapper, connection
    code = (
        "style_suite_run_immutable"
        if isinstance(target, StyleSuiteRun)
        else "style_suite_run_item_immutable"
    )
    raise DomainRuleError(code)


@event.listens_for(StyleSuiteRun, "before_insert")
def _validate_style_suite_source(
    mapper: Mapper[StyleSuiteRun], connection: Connection, target: StyleSuiteRun
) -> None:
    del mapper
    source = connection.execute(
        select(ProductAsset.id).where(
            ProductAsset.customer_id == target.customer_id,
            ProductAsset.id == target.source_asset_id,
            ProductAsset.product_id == target.product_id,
            ProductAsset.content_sha256 == target.source_content_sha256,
            ProductAsset.confirmed.is_(True),
            ProductAsset.identity == "original_real_photo",
        )
    ).first()
    observation_bound = target.product_identity_observation_run_id is not None
    result_bound = target.product_identity_observation_result_sha256 is not None
    if observation_bound != result_bound or (
        target.product_identity_observation_review_id is not None
        and not observation_bound
    ):
        raise IntegrityError(
            "style_suite_identity_observation_incomplete",
            {},
            RuntimeError("style_suite_identity_observation_incomplete"),
        )
    if observation_bound:
        observation = connection.scalar(
            select(ProductIdentityObservationRun.id).where(
                ProductIdentityObservationRun.customer_id == target.customer_id,
                ProductIdentityObservationRun.id
                == target.product_identity_observation_run_id,
                ProductIdentityObservationRun.product_asset_id
                == target.source_asset_id,
                ProductIdentityObservationRun.product_id == target.product_id,
                ProductIdentityObservationRun.source_content_sha256
                == target.source_content_sha256,
                ProductIdentityObservationRun.product_identity_card_version_id
                == target.product_identity_card_version_id,
                ProductIdentityObservationRun.product_identity_card_sha256
                == target.product_identity_card_sha256,
                ProductIdentityObservationRun.result_sha256
                == target.product_identity_observation_result_sha256,
            )
        )
        if observation is None:
            raise IntegrityError(
                "style_suite_identity_observation_mismatch",
                {},
                RuntimeError("style_suite_identity_observation_mismatch"),
            )
        if target.product_identity_observation_review_id is not None:
            review = connection.scalar(
                select(ProductIdentityObservationReview.id).where(
                    ProductIdentityObservationReview.customer_id
                    == target.customer_id,
                    ProductIdentityObservationReview.id
                    == target.product_identity_observation_review_id,
                    ProductIdentityObservationReview.observation_run_id
                    == target.product_identity_observation_run_id,
                    ProductIdentityObservationReview.observation_result_sha256
                    == target.product_identity_observation_result_sha256,
                )
            )
            if review is None:
                raise IntegrityError(
                    "style_suite_identity_observation_review_mismatch",
                    {},
                    RuntimeError(
                        "style_suite_identity_observation_review_mismatch"
                    ),
                )
    if (
        source is None
        or target.maximum_estimated_cost_usd
        != target.candidate_estimate_usd
        * (
            target.child_job_count * target.candidate_count_per_item
            if target.candidate_count_per_item is not None
            else target.candidate_count
        )
    ):
        raise DomainRuleError("style_suite_run_facts_mismatch")


@event.listens_for(StyleSuiteRunItem, "before_insert")
def _validate_style_suite_item_snapshot(
    mapper: Mapper[StyleSuiteRunItem], connection: Connection, target: StyleSuiteRunItem
) -> None:
    del mapper
    run = connection.execute(
        select(
            StyleSuiteRun.real_generation_enabled,
            StyleSuiteRun.pilot_scope,
        ).where(
            StyleSuiteRun.customer_id == target.customer_id,
            StyleSuiteRun.id == target.run_id,
        )
    ).first()
    version = connection.execute(
        select(StyleCardVersion.version, StyleCardVersion.content)
        .join(
            StyleTemplateSlot,
            StyleTemplateSlot.style_card_version_id == StyleCardVersion.id,
        )
        .where(
            StyleCardVersion.customer_id == target.customer_id,
            StyleCardVersion.id == target.scene_style_version_id,
            StyleTemplateSlot.customer_id == target.customer_id,
            StyleTemplateSlot.id == target.slot_id,
            StyleTemplateSlot.scene_style_id == target.scene_style_id,
            StyleTemplateSlot.slot_key == target.slot_key,
            StyleTemplateSlot.active.is_(True),
        )
    ).first()
    if (
        run is None
        or version is None
        or version.version != target.style_version
        or version.content.get("name") != target.style_name
        or version.content.get("historical_style") is not True
        or version.content.get("stability") != "customer_confirmed"
        or version.content != target.style_snapshot
        or target.slot_snapshot.get("id") != str(target.slot_id)
        or target.slot_snapshot not in version.content.get("slots", [])
    ):
        raise DomainRuleError("style_suite_run_item_snapshot_mismatch")
    if (
        run.real_generation_enabled
        and run.pilot_scope is not None
        and version.content.get("style_origin") == "customer_upload"
    ):
        sample_count = connection.scalar(
            select(func.count())
            .select_from(StyleCardSampleLink)
            .join(
                StyleSample,
                and_(
                    StyleSample.customer_id == StyleCardSampleLink.customer_id,
                    StyleSample.id == StyleCardSampleLink.style_sample_id,
                ),
            )
            .where(
                StyleCardSampleLink.customer_id == target.customer_id,
                StyleCardSampleLink.style_card_version_id
                == target.scene_style_version_id,
                StyleSample.withdrawn_at.is_(None),
                StyleSample.consent_provenance != "local_synthetic_acceptance",
                StyleSample.content_sha256.is_not(None),
                StyleSample.mime_type.in_(("image/jpeg", "image/png")),
                StyleSample.width.is_not(None),
                StyleSample.height.is_not(None),
                StyleSample.asset_store_namespace.is_not(None),
            )
        )
        evidence = connection.execute(
            select(StyleFieldEvidence.field_name, StyleFieldEvidence.field_value).where(
                StyleFieldEvidence.customer_id == target.customer_id,
                StyleFieldEvidence.style_card_version_id
                == target.scene_style_version_id,
            )
        ).all()
        eligibility = assess_real_style(
            version.content,
            evidence,
            int(sample_count or 0),
            (
                SimpleNamespace(
                    active=True,
                    rules=target.slot_snapshot.get("rules"),
                ),
            ),
        )
        if not eligibility.eligible:
            raise DomainRuleError(
                "style_suite_real_style_ineligible:"
                + eligibility.reason_codes[0]
            )


@event.listens_for(StyleTemplateSlot, "before_insert")
def _validate_style_template_slot_scene_version(
    mapper: Mapper[StyleTemplateSlot], connection: Connection, target: StyleTemplateSlot
) -> None:
    del mapper
    owned = connection.execute(
        select(SceneStyle.id, StyleCardVersion.content)
        .join(
            StyleCardVersion,
            StyleCardVersion.style_card_id == SceneStyle.style_card_id,
        )
        .where(
            SceneStyle.id == target.scene_style_id,
            SceneStyle.customer_id == target.customer_id,
            StyleCardVersion.id == target.style_card_version_id,
            StyleCardVersion.customer_id == target.customer_id,
        )
    ).first()
    if owned is None:
        raise DomainRuleError("style_template_slot_version_mismatch")
    expected = {
        "id": str(target.id),
        "customer_id": str(target.customer_id),
        "scene_style_id": str(target.scene_style_id),
        "style_card_version_id": str(target.style_card_version_id),
        "slot_key": target.slot_key,
        "label": target.label,
        "position": target.position,
        "active": target.active,
        "rules": target.rules,
    }
    if expected not in owned[1].get("slots", []):
        raise DomainRuleError("style_template_slot_snapshot_mismatch")


@event.listens_for(AuditEvent, "before_update")
def _reject_audit_event_update(
    mapper: Mapper[AuditEvent], connection: Connection, target: AuditEvent
) -> None:
    """Keep ORM changes from rewriting audit evidence."""
    del mapper, connection, target
    raise DomainRuleError("audit_event_immutable")


@event.listens_for(AuditEvent, "before_delete")
def _reject_audit_event_delete(
    mapper: Mapper[AuditEvent], connection: Connection, target: AuditEvent
) -> None:
    """Keep ORM changes from deleting audit evidence."""
    del mapper, connection, target
    raise DomainRuleError("audit_event_immutable")


class MallStyleExecution(Base):
    """Durable claim and single-use dispatch fence for the approved queue."""

    __tablename__ = "mall_style_executions"
    __table_args__ = (
        UniqueConstraint("queue_id", name="uq_mall_style_execution_queue"),
        CheckConstraint("outbound_attempts IN (0,1)", name="ck_mall_execution_once"),
        CheckConstraint(
            "state IN ('leased','reserved','completed','blocked','uncertain')",
            name="ck_mall_execution_state",
        ),
    )
    id: Mapped[UUID] = mapped_column(UUIDType(), primary_key=True, default=uuid4)
    queue_id: Mapped[UUID] = mapped_column(
        UUIDType(), ForeignKey("mall_style_generation_queue_items.id"), nullable=False
    )
    claim_token: Mapped[str] = mapped_column(String(36), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    outbound_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    planned_job_id: Mapped[UUID] = mapped_column(UUIDType(), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


@event.listens_for(MallStyleExecution, "before_update")
def _guard_mall_execution_update(
    mapper: Mapper[MallStyleExecution],
    connection: Connection,
    target: MallStyleExecution,
) -> None:
    """A committed dispatch reservation can never become unused again."""
    del mapper
    state = inspect(target)
    previous = connection.execute(
        select(MallStyleExecution.outbound_attempts).where(
            MallStyleExecution.id == target.id
        )
    ).scalar_one()
    if (
        state.attrs.queue_id.history.has_changes()
        or state.attrs.planned_job_id.history.has_changes()
    ):
        raise DomainRuleError("mall_execution_lineage_immutable")
    if target.outbound_attempts < previous or (
        previous == 1 and target.state == "leased"
    ):
        raise DomainRuleError("mall_execution_dispatch_fence_immutable")


@event.listens_for(MallStyleExecution, "before_delete")
def _guard_mall_execution_delete(
    mapper: Mapper[MallStyleExecution],
    connection: Connection,
    target: MallStyleExecution,
) -> None:
    del mapper, connection, target
    raise DomainRuleError("mall_execution_dispatch_fence_immutable")
