"""Explicit, audited recovery of immutable local style-sample bytes."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Protocol
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.models import AssetStoreRecoveryBinding, AuditEvent, StyleSample
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError


class RecoveryTargetStore(Protocol):
    @property
    def store_namespace(self) -> str: ...

    @property
    def copy_safe_namespace(self) -> bool: ...

    def verify_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bool: ...

    def persist_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None: ...

    def discard_style_sample_content(self, customer_id: UUID, sample_id: UUID) -> None:
        ...


@dataclass(frozen=True)
class RecoveryResult:
    binding_id: UUID
    sample_count: int
    manifest_sha256: str
    source_namespace: str
    target_namespace: str


def namespace_is_accepted(
    session: Session,
    customer_id: UUID,
    recorded_namespace: str,
    current_namespace: str,
) -> bool:
    if recorded_namespace == current_namespace:
        return True
    binding = session.scalar(
        select(AssetStoreRecoveryBinding).where(
            AssetStoreRecoveryBinding.customer_id == customer_id,
            AssetStoreRecoveryBinding.source_namespace == recorded_namespace,
            AssetStoreRecoveryBinding.target_namespace == current_namespace,
            AssetStoreRecoveryBinding.status == "active",
        )
    )
    if binding is None:
        return False
    revocations = session.scalars(
        select(AuditEvent).where(
            AuditEvent.customer_id == customer_id,
            AuditEvent.event_type == "asset_store.recovery.revoked",
        )
    )
    return not any(
        event.payload.get("binding_id") == str(binding.id) for event in revocations
    )


class AssetStoreRecoveryService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def recover(
        self,
        customer_id: UUID,
        source_namespace: str,
        source_store: AssetContentStore,
        target_store: RecoveryTargetStore,
        source_label: str,
    ) -> RecoveryResult:
        if (
            re.fullmatch(r"[0-9a-f]{64}", source_namespace) is None
            or not source_label.strip()
            or len(source_label) > 255
            or not target_store.copy_safe_namespace
            or source_namespace == target_store.store_namespace
        ):
            raise DomainRuleError("asset_store_recovery_scope_invalid")
        rows = list(
            self._session.scalars(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.asset_store_namespace == source_namespace,
                    StyleSample.withdrawn_at.is_(None),
                )
                .order_by(StyleSample.id)
                .execution_options(populate_existing=True)
            )
        )
        if not rows or any(not row.has_complete_content_seal for row in rows):
            raise DomainRuleError("asset_store_recovery_source_invalid")

        verified: list[tuple[StyleSample, bytes]] = []
        try:
            for row in rows:
                assert row.content_sha256 is not None
                content = source_store.read_style_sample_content(
                    customer_id, row.id, row.content_sha256
                )
                self._verify_image(row, content)
                verified.append((row, content))
        except (AssetContentStoreError, OSError, UnidentifiedImageError) as error:
            raise DomainRuleError("asset_store_recovery_source_invalid") from error

        manifest = [
            {
                "height": row.height,
                "mime_type": row.mime_type,
                "sample_id": str(row.id),
                "sha256": row.content_sha256,
                "width": row.width,
            }
            for row in rows
        ]
        manifest_sha256 = sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = self._session.scalar(
            select(AssetStoreRecoveryBinding).where(
                AssetStoreRecoveryBinding.customer_id == customer_id,
                AssetStoreRecoveryBinding.source_namespace == source_namespace,
                AssetStoreRecoveryBinding.target_namespace
                == target_store.store_namespace,
                AssetStoreRecoveryBinding.manifest_sha256 == manifest_sha256,
                AssetStoreRecoveryBinding.status == "active",
            )
        )
        if existing is not None:
            if all(
                target_store.verify_style_sample_content(
                    customer_id, row.id, row.content_sha256 or ""
                )
                for row in rows
            ):
                return RecoveryResult(
                    existing.id,
                    existing.sample_count,
                    existing.manifest_sha256,
                    existing.source_namespace,
                    existing.target_namespace,
                )
            raise DomainRuleError("asset_store_recovery_target_invalid")
        if any(
            target_store.verify_style_sample_content(
                customer_id, row.id, row.content_sha256 or ""
            )
            for row in rows
        ):
            raise DomainRuleError("asset_store_recovery_target_invalid")

        written: list[StyleSample] = []
        try:
            for row, content in verified:
                assert row.content_sha256 is not None
                target_store.persist_style_sample_content(
                    customer_id, row.id, content, row.content_sha256
                )
                if not target_store.verify_style_sample_content(
                    customer_id, row.id, row.content_sha256
                ):
                    raise AssetContentStoreError("style_sample_content_unavailable")
                written.append(row)
            binding = AssetStoreRecoveryBinding(
                id=uuid4(),
                customer_id=customer_id,
                source_namespace=source_namespace,
                target_namespace=target_store.store_namespace,
                sample_count=len(rows),
                manifest_sha256=manifest_sha256,
                source_label=source_label.strip(),
                status="active",
                created_at=datetime.now(UTC),
            )
            self._session.add(binding)
            self._session.add(
                AuditEvent(
                    id=uuid4(),
                    customer_id=customer_id,
                    event_type="asset_store.recovery.completed",
                    payload={
                        "binding_id": str(binding.id),
                        "source_namespace": source_namespace,
                        "target_namespace": target_store.store_namespace,
                        "sample_count": len(rows),
                        "manifest_sha256": manifest_sha256,
                        "source_label": source_label.strip(),
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            self._session.commit()
            return RecoveryResult(
                binding.id,
                len(rows),
                manifest_sha256,
                source_namespace,
                target_store.store_namespace,
            )
        except Exception as error:
            self._session.rollback()
            cleanup_failed = False
            for row in reversed(written):
                try:
                    target_store.discard_style_sample_content(customer_id, row.id)
                except Exception:  # noqa: BLE001 - continue compensating every write
                    cleanup_failed = True
            if cleanup_failed:
                raise DomainRuleError("asset_store_recovery_cleanup_failed") from error
            if isinstance(error, DomainRuleError):
                raise
            raise DomainRuleError("asset_store_recovery_copy_failed") from error

    @staticmethod
    def _verify_image(row: StyleSample, content: bytes) -> None:
        if sha256(content).hexdigest() != row.content_sha256:
            raise DomainRuleError("asset_store_recovery_source_invalid")
        with Image.open(BytesIO(content)) as image:
            image.load()
            mime_type = Image.MIME.get(image.format or "")
            if (
                mime_type != row.mime_type
                or image.width != row.width
                or image.height != row.height
            ):
                raise DomainRuleError("asset_store_recovery_source_invalid")
