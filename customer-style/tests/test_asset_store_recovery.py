"""Recovery bindings preserve legacy facts without granting broad trust."""

import importlib.util
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    AssetStoreRecoveryBinding,
    AuditEvent,
    Customer,
    StyleSample,
)
from opc_style.services.asset_store_recovery import (
    AssetStoreRecoveryService,
    namespace_is_accepted,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_atomic_recovery_service_is_available() -> None:
    assert importlib.util.find_spec("opc_style.services.asset_store_recovery") is not None


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 8), color).save(output, format="JPEG", quality=95)
    return output.getvalue()


def _seed_recovery(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    tmp_path: Path,
    *,
    count: int = 10,
) -> tuple[LocalSyntheticAssetStore, LocalSyntheticAssetStore, list[StyleSample]]:
    customer_factory(CUSTOMER_A_ID)
    source_root = tmp_path / "legacy-source"
    source_root.mkdir(mode=0o700)
    source = LocalSyntheticAssetStore(source_root, reopen=True)
    target = LocalSyntheticAssetStore(tmp_path / "stable-target")
    samples = []
    for index in range(count):
        content = _jpeg_bytes((index, 50, 100))
        digest = sha256(content).hexdigest()
        sample = StyleSample(
            id=uuid4(),
            customer_id=CUSTOMER_A_ID,
            photo_id=f"history-{index}",
            authorized_at=datetime.now(UTC),
            consent_provenance="customer_opt_in",
            consent_text_version="style-consent-v1",
            content_sha256=digest,
            mime_type="image/jpeg",
            width=12,
            height=8,
            asset_store_namespace=source.store_namespace,
        )
        session.add(sample)
        source.persist_style_sample_content(CUSTOMER_A_ID, sample.id, content, digest)
        samples.append(sample)
    session.commit()
    return source, target, samples


def test_recovery_copies_all_verified_samples_and_preserves_history(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    tmp_path: Path,
) -> None:
    source, target, samples = _seed_recovery(
        session, customer_factory, tmp_path
    )
    source_namespace = source.store_namespace
    historical_namespaces = {
        sample.id: sample.asset_store_namespace for sample in samples
    }

    result = AssetStoreRecoveryService(session).recover(
        CUSTOMER_A_ID,
        source_namespace,
        source,
        target,
        "backup-2026-09-12",
    )

    assert result.sample_count == 10
    assert result.source_namespace == source_namespace
    assert result.target_namespace == target.store_namespace
    assert {sample.id: sample.asset_store_namespace for sample in samples} == (
        historical_namespaces
    )
    assert all(
        target.verify_style_sample_content(
            CUSTOMER_A_ID, sample.id, sample.content_sha256 or ""
        )
        for sample in samples
    )
    assert namespace_is_accepted(
        session, CUSTOMER_A_ID, source_namespace, target.store_namespace
    )
    assert not namespace_is_accepted(
        session,
        UUID("22222222-2222-4222-8222-222222222222"),
        source_namespace,
        target.store_namespace,
    )


def test_recovery_missing_source_file_leaves_no_target_or_binding(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    tmp_path: Path,
) -> None:
    source, target, samples = _seed_recovery(
        session, customer_factory, tmp_path, count=3
    )
    source_namespace = source.store_namespace
    missing = samples[-1]
    source.discard_style_sample_content(CUSTOMER_A_ID, missing.id)

    with pytest.raises(DomainRuleError, match="asset_store_recovery_source_invalid"):
        AssetStoreRecoveryService(session).recover(
            CUSTOMER_A_ID,
            source_namespace,
            source,
            target,
            "missing-source-test",
        )

    assert target.sample_content_count == 0
    assert session.scalar(select(func.count(AssetStoreRecoveryBinding.id))) == 0
    assert session.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.event_type == "asset_store.recovery.completed"
        )
    ) == 0


class _FailingTarget:
    def __init__(self, delegate: LocalSyntheticAssetStore, fail_on: int) -> None:
        self._delegate = delegate
        self._fail_on = fail_on
        self._writes = 0

    @property
    def store_namespace(self) -> str:
        return self._delegate.store_namespace

    @property
    def copy_safe_namespace(self) -> bool:
        return True

    def verify_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, expected_sha256: str
    ) -> bool:
        return self._delegate.verify_style_sample_content(
            customer_id, sample_id, expected_sha256
        )

    def persist_style_sample_content(
        self, customer_id: UUID, sample_id: UUID, content: bytes, expected_sha256: str
    ) -> None:
        self._writes += 1
        if self._writes == self._fail_on:
            raise OSError("injected target failure")
        self._delegate.persist_style_sample_content(
            customer_id, sample_id, content, expected_sha256
        )

    def discard_style_sample_content(self, customer_id: UUID, sample_id: UUID) -> None:
        self._delegate.discard_style_sample_content(customer_id, sample_id)


def test_recovery_rolls_back_all_new_target_files_when_copy_fails(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    tmp_path: Path,
) -> None:
    source, target, _ = _seed_recovery(session, customer_factory, tmp_path, count=3)

    with pytest.raises(DomainRuleError, match="asset_store_recovery_copy_failed"):
        AssetStoreRecoveryService(session).recover(
            CUSTOMER_A_ID,
            source.store_namespace,
            source,
            _FailingTarget(target, fail_on=3),
            "atomic-copy-test",
        )

    assert target.sample_content_count == 0
    assert session.scalar(select(func.count(AssetStoreRecoveryBinding.id))) == 0


def test_recovery_rejects_customer_or_digest_mismatch(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    tmp_path: Path,
) -> None:
    source, target, samples = _seed_recovery(
        session, customer_factory, tmp_path, count=2
    )
    other_customer = UUID("22222222-2222-4222-8222-222222222222")
    customer_factory(other_customer)
    session.commit()

    with pytest.raises(DomainRuleError, match="asset_store_recovery_source_invalid"):
        AssetStoreRecoveryService(session).recover(
            other_customer,
            source.store_namespace,
            source,
            target,
            "wrong-customer-test",
        )

    sample = samples[0]
    session.execute(
        StyleSample.__table__.update()
        .where(StyleSample.id == sample.id)
        .values(content_sha256="f" * 64)
    )
    session.commit()
    with pytest.raises(DomainRuleError, match="asset_store_recovery_source_invalid"):
        AssetStoreRecoveryService(session).recover(
            CUSTOMER_A_ID,
            source.store_namespace,
            source,
            target,
            "wrong-digest-test",
        )
    assert target.sample_content_count == 0


def test_recovery_is_idempotent_and_revocation_fails_namespace_acceptance(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
    tmp_path: Path,
) -> None:
    source, target, _ = _seed_recovery(session, customer_factory, tmp_path, count=2)
    service = AssetStoreRecoveryService(session)
    first = service.recover(
        CUSTOMER_A_ID,
        source.store_namespace,
        source,
        target,
        "idempotency-test",
    )
    second = service.recover(
        CUSTOMER_A_ID,
        source.store_namespace,
        source,
        target,
        "idempotency-test",
    )
    assert second == first
    assert session.scalar(select(func.count(AssetStoreRecoveryBinding.id))) == 1

    session.add(
        AuditEvent(
            id=uuid4(),
            customer_id=CUSTOMER_A_ID,
            event_type="asset_store.recovery.revoked",
            payload={"binding_id": str(first.binding_id)},
            occurred_at=datetime.now(UTC),
        )
    )
    session.commit()

    assert not namespace_is_accepted(
        session,
        CUSTOMER_A_ID,
        source.store_namespace,
        target.store_namespace,
    )


def _binding(customer_id: UUID) -> AssetStoreRecoveryBinding:
    return AssetStoreRecoveryBinding(
        id=uuid4(),
        customer_id=customer_id,
        source_namespace="a" * 64,
        target_namespace="b" * 64,
        sample_count=10,
        manifest_sha256="c" * 64,
        source_label="backup-2026-09-12",
        status="active",
        created_at=datetime.now(UTC),
    )


def test_recovery_binding_is_customer_scoped_and_immutable(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
) -> None:
    customer_factory(CUSTOMER_A_ID)
    binding = _binding(CUSTOMER_A_ID)
    session.add(binding)
    session.commit()

    binding.sample_count = 9

    with pytest.raises(
        DomainRuleError, match="asset_store_recovery_binding_immutable"
    ):
        session.flush()


def test_recovery_binding_cannot_be_deleted(
    session: Session,
    customer_factory: Callable[[UUID], Customer],
) -> None:
    customer_factory(CUSTOMER_A_ID)
    binding = _binding(CUSTOMER_A_ID)
    session.add(binding)
    session.commit()

    session.delete(binding)

    with pytest.raises(
        DomainRuleError, match="asset_store_recovery_binding_immutable"
    ):
        session.flush()
