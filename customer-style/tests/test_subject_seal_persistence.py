"""Durable customer-owned subject-seal service contracts."""

from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from PIL import Image
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from alembic import command
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.subject_seal import DeterministicLocalSubjectSegmenter
from opc_style.models import (
    AuditEvent,
    Base,
    Customer,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
)
from opc_style.services.real_photos import (
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.subject_seals import (
    ProductSubjectSealNotFoundError,
    ProductSubjectSealService,
)

CUSTOMER_A = UUID("11111111-1111-4111-8111-111111111111")
CUSTOMER_B = UUID("22222222-2222-4222-8222-222222222222")
ASSET_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _product_png() -> bytes:
    image = Image.new("RGBA", (6, 6), (10, 20, 30, 0))
    image.putpixel((2, 2), (190, 20, 10, 255))
    image.putpixel((3, 2), (210, 40, 20, 255))
    image.putpixel((2, 3), (200, 30, 20, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def subject_context(tmp_path: Path) -> tuple[Session, LocalSyntheticAssetStore, bytes]:
    engine = create_engine(f"sqlite:///{tmp_path / 'subject.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    content = _product_png()
    digest = sha256(content).hexdigest()
    session.add_all(
        [
            Customer(id=CUSTOMER_A, display_name="A"),
            Customer(id=CUSTOMER_B, display_name="B"),
            ProductAsset(
                id=ASSET_ID,
                customer_id=CUSTOMER_A,
                product_id="PRODUCT-001",
                photo_id="PHOTO-001",
                identity="original_real_photo",
                confirmed=True,
                source_type="classification_handoff",
                quality_flags=[],
                occurred_at=NOW,
                content_sha256=digest,
                mime_type="image/png",
                width=6,
                height=6,
            ),
        ]
    )
    session.flush()
    session.add(
        ProductAssetPermissionEvent(
            customer_id=CUSTOMER_A,
            product_asset_id=ASSET_ID,
            transform_permission=True,
            occurred_at=NOW,
            provenance="classification_handoff",
        )
    )
    session.commit()
    store = LocalSyntheticAssetStore(tmp_path / "media")
    store.persist_original(CUSTOMER_A, ASSET_ID, content, digest)
    yield session, store, content
    store.close()
    session.close()


def test_seal_is_durable_idempotent_and_customer_scoped(
    subject_context: tuple[Session, LocalSyntheticAssetStore, bytes],
) -> None:
    """A replay must reuse one seal, while a foreign customer sees no identity."""
    session, store, _ = subject_context
    service = ProductSubjectSealService(session, store)

    first = service.seal(
        CUSTOMER_A,
        asset_id=ASSET_ID,
        segmenter=DeterministicLocalSubjectSegmenter(),
        canvas_size=(8, 10),
    )
    replay = service.seal(
        CUSTOMER_A,
        asset_id=ASSET_ID,
        segmenter=DeterministicLocalSubjectSegmenter(),
        canvas_size=(8, 10),
    )

    assert first.created is True
    assert replay.created is False
    assert replay.seal.id == first.seal.id
    assert session.scalar(select(func.count()).select_from(ProductSubjectSeal)) == 1
    assert store.read_subject_artifact(
        CUSTOMER_A, first.seal.id, "rgba", first.seal.normalized_subject_sha256
    )
    assert store.read_subject_artifact(
        CUSTOMER_A, first.seal.id, "core-mask", first.seal.mask_sha256
    )
    with pytest.raises(ProductSubjectSealNotFoundError):
        service.get(CUSTOMER_B, first.seal.id)


def test_denied_permission_blocks_before_segmenter_and_storage(
    subject_context: tuple[Session, LocalSyntheticAssetStore, bytes],
) -> None:
    """The visual adapter must never see bytes after permission is withdrawn."""
    session, store, _ = subject_context
    session.add(
        ProductAssetPermissionEvent(
            customer_id=CUSTOMER_A,
            product_asset_id=ASSET_ID,
            transform_permission=False,
            occurred_at=datetime(2026, 9, 11, 12, 1, tzinfo=UTC),
            provenance="operator_withdrawal",
        )
    )
    session.commit()

    with pytest.raises(DomainRuleError, match="transform_permission_denied"):
        ProductSubjectSealService(session, store).seal(
            CUSTOMER_A,
            asset_id=ASSET_ID,
            segmenter=DeterministicLocalSubjectSegmenter(),
            canvas_size=(8, 10),
        )

    assert session.scalar(select(func.count()).select_from(ProductSubjectSeal)) == 0


def test_replay_fails_if_a_sealed_artifact_was_tampered(
    subject_context: tuple[Session, LocalSyntheticAssetStore, bytes],
    tmp_path: Path,
) -> None:
    """A database row alone must not make changed subject bytes trustworthy."""
    session, store, _ = subject_context
    service = ProductSubjectSealService(session, store)
    result = service.seal(
        CUSTOMER_A,
        asset_id=ASSET_ID,
        segmenter=DeterministicLocalSubjectSegmenter(),
        canvas_size=(8, 10),
    )
    target = (
        tmp_path
        / "media"
        / "customers"
        / str(CUSTOMER_A)
        / "subject-rgba"
        / f"{result.seal.id}.bin"
    )
    target.write_bytes(b"tampered")

    with pytest.raises(AssetContentStoreError, match="subject_artifact_unavailable"):
        service.seal(
            CUSTOMER_A,
            asset_id=ASSET_ID,
            segmenter=DeterministicLocalSubjectSegmenter(),
            canvas_size=(8, 10),
        )


def test_subject_seal_and_audit_are_immutable_in_the_orm(
    subject_context: tuple[Session, LocalSyntheticAssetStore, bytes],
) -> None:
    """Changing a sealed hash would destroy the product-identity guarantee."""
    session, store, _ = subject_context
    result = ProductSubjectSealService(session, store).seal(
        CUSTOMER_A,
        asset_id=ASSET_ID,
        segmenter=DeterministicLocalSubjectSegmenter(),
        canvas_size=(8, 10),
    )
    result.seal.mask_sha256 = "0" * 64

    with pytest.raises(DomainRuleError, match="product_subject_seal_immutable"):
        session.flush()
    session.rollback()

    event = session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "product_subject.sealed")
    )
    assert event is not None
    assert event.payload["subject_seal_id"] == str(result.seal.id)


def test_human_review_creates_reusable_immutable_seal(
    subject_context: tuple[Session, LocalSyntheticAssetStore, bytes],
) -> None:
    """One confirmed correction is replayed instead of edited in place."""
    session, store, _ = subject_context
    service = ProductSubjectSealService(session, store)
    automatic = service.seal(
        CUSTOMER_A,
        asset_id=ASSET_ID,
        segmenter=DeterministicLocalSubjectSegmenter(),
        canvas_size=(8, 10),
    )
    rgba = store.read_subject_artifact(
        CUSTOMER_A,
        automatic.seal.id,
        "rgba",
        automatic.seal.normalized_subject_sha256,
    )
    with Image.open(BytesIO(rgba)) as source:
        mask = source.getchannel("A")
        mask.putpixel((3, 2), 0)
        output = BytesIO()
        mask.save(output, format="PNG")
    first = service.confirm_reviewed_mask(
        CUSTOMER_A,
        base_seal_id=automatic.seal.id,
        reviewed_mask_png=output.getvalue(),
        confirmation_key="review-001",
    )
    replay = service.confirm_reviewed_mask(
        CUSTOMER_A,
        base_seal_id=automatic.seal.id,
        reviewed_mask_png=output.getvalue(),
        confirmation_key="review-001",
    )

    assert first.created is True
    assert replay.created is False
    assert replay.seal.id == first.seal.id
    assert first.seal.segmentation_adapter_version == "human-reviewed-subject-mask-v1"
    assert service.require_reviewed(
        CUSTOMER_A, first.seal.id, asset_id=ASSET_ID
    ).id == first.seal.id
    events = list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "product_subject.mask_confirmed"
            )
        )
    )
    assert len(events) == 1
    assert events[0].payload["base_subject_seal_id"] == str(automatic.seal.id)


def test_migration_triggers_reject_raw_subject_seal_mutation(tmp_path: Path) -> None:
    """Direct SQL must not bypass the ORM immutability guard."""
    database_url = f"sqlite:///{tmp_path / 'migration.db'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='product_subject_seals'"
                )
            )
        }
    assert names == {
        "trg_product_subject_seals_no_update",
        "trg_product_subject_seals_no_delete",
    }
