import hashlib
import io
import json
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.routes_mall_style_handoff_v1 import router
from opc_style.contracts.mall_style_handoff_v1 import (
    MallStyleAssetRefV1,
    MallStyleGenerationApprovalV1,
    MallStyleHandoffV1,
    MallStyleSelectionV1,
    canonical_mall_generation_approval_sha256,
    canonical_mall_handoff_bytes,
    canonical_mall_payload_sha256,
    canonical_mall_product_asset_signature_bytes,
    canonical_mall_subject_confirmation_signature_bytes,
)
from opc_style.domain.image_normalizer import NormalizedPayload
from opc_style.domain.subject_seal import DeterministicLocalSubjectSegmenter
from opc_style.models import (
    AuditEvent,
    Base,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    MallStyleGenerationQueueItem,
    MallStyleSourceAssetBinding,
    MallStyleSubjectConfirmation,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.schemas import ProductAssetIntakeRequest
from opc_style.services.assets import ProductAssetService
from opc_style.services.mall_style_integration_v1 import (
    MallProductAssetImportService,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.style_warehouse_v3 import style_request_signature
from opc_style.settings import StyleWarehouseV3Settings

MERCHANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPC = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SECRET = "mall-style-integration-test-secret"


@contextmanager
def receiver(
    tmp_path: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    manual_subject: bool = False,
) -> Generator[
    tuple[TestClient, sessionmaker[Session], LocalSyntheticAssetStore], None, None
]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'mall-style-integration.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    store = LocalSyntheticAssetStore(tmp_path / "private-media")
    app = FastAPI()
    app.state.style_warehouse_v3_session_factory = sessions
    app.state.style_warehouse_v3_settings = StyleWarehouseV3Settings(
        enabled=True,
        signing_secret=SecretStr(SECRET),
        verify_source_images=False,
        mall_product_asset_max_bytes=max_bytes,
    )
    app.state.mall_style_asset_store = store
    app.state.mall_style_subject_segmenter = (
        None if manual_subject else DeterministicLocalSubjectSegmenter()
    )
    app.state.style_suite_planning_settings = SimpleNamespace(
        candidate_estimate_usd=Decimal("0.12")
    )
    app.include_router(router)
    with TestClient(app) as client:
        yield client, sessions, store
    store.close()
    engine.dispose()


def signed_headers(
    body: bytes,
    *,
    path: str,
    method: str,
    merchant_id: UUID = MERCHANT,
    opc_id: UUID = OPC,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    timestamp = int(time.time())
    headers = {
        "X-Source-System": "mall-saas",
        "X-Merchant-Id": str(merchant_id),
        "X-OpC-Id": str(opc_id),
        "X-Signature-Timestamp": str(timestamp),
        "X-Signature": style_request_signature(
            secret=SECRET,
            timestamp=timestamp,
            source_system="mall-saas",
            tenant_id=f"{merchant_id}:{opc_id}",
            method=method,
            path=path,
            body=body,
        ),
    }
    headers.update(extra or {})
    return headers


def png_bytes(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (12, 9), color).save(buffer, format="PNG")
    return buffer.getvalue()


def seed_confirmed_style(
    session: Session,
    *,
    merchant_id: UUID = MERCHANT,
    name: str,
    default_slot: str | None = None,
    confirmed: bool = True,
) -> tuple[SceneStyle, StyleCardVersion, StyleTemplateSlot]:
    if session.get(Customer, merchant_id) is None:
        session.add(Customer(id=merchant_id, display_name=str(merchant_id)))
        session.flush()
    sample = StyleSample(
        customer_id=merchant_id,
        photo_id=f"style-sample-{uuid4()}",
        authorized_at=datetime.now(UTC),
        consent_provenance="merchant-confirmed-upload",
        consent_text_version="v1",
        content_sha256="a" * 64,
        mime_type="image/jpeg",
        width=10,
        height=10,
        asset_store_namespace="test-private",
    )
    card = StyleCard(customer_id=merchant_id, kind="scene", active=True)
    session.add_all([sample, card])
    session.flush()
    scene = SceneStyle(
        id=card.id,
        customer_id=merchant_id,
        style_card_id=card.id,
        name=name,
        style_origin="customer_upload",
        active=True,
        rules={},
    )
    version_id = uuid4()
    slot_id = uuid4()
    rules = {"aspect_ratio": "1:1", "background": ["white"]}
    slot_snapshot = {
        "id": str(slot_id),
        "customer_id": str(merchant_id),
        "scene_style_id": str(scene.id),
        "style_card_version_id": str(version_id),
        "slot_key": "hero",
        "label": "主图",
        "rules": rules,
        "active": True,
        "position": 0,
    }
    content: dict[str, object] = {
        "name": name,
        "main_style_card_version_id": None,
        "historical_style": True,
        "style_origin": "customer_upload",
        "stability": "customer_confirmed" if confirmed else "proposal_only",
        "slots": [slot_snapshot],
    }
    if default_slot is not None:
        content["mall_default_slot_key"] = default_slot
    version = StyleCardVersion(
        id=version_id,
        customer_id=merchant_id,
        style_card_id=card.id,
        version=1,
        content=content,
    )
    slot = StyleTemplateSlot(
        id=slot_id,
        customer_id=merchant_id,
        scene_style_id=scene.id,
        style_card_version_id=version.id,
        slot_key="hero",
        label="主图",
        rules=rules,
        active=True,
        position=0,
    )
    session.add_all([scene, version, slot])
    session.flush()
    session.add(
        StyleCardSampleLink(
            customer_id=merchant_id,
            style_card_version_id=version.id,
            style_sample_id=sample.id,
        )
    )
    session.commit()
    return scene, version, slot


def import_path(source_id: UUID, product_id: str = "SKU-001") -> str:
    return f"/internal/mall-style/v1/products/{product_id}/product-assets/{source_id}"


def import_headers(
    body: bytes,
    *,
    path: str,
    merchant_id: UUID = MERCHANT,
    opc_id: UUID = OPC,
    role: str = "product_photo",
    content_type: str = "image/png",
    claimed_sha256: str | None = None,
    authorized_purpose: str = "style_subject_transform",
    authorization_reference: str = "mall-consent-001",
) -> dict[str, str]:
    digest = claimed_sha256 or hashlib.sha256(body).hexdigest()
    signed_body = canonical_mall_product_asset_signature_bytes(
        content=body,
        content_sha256=digest,
        mime_type=content_type,
        asset_role=role,
        authorized_purpose=authorized_purpose,
        authorization_reference=authorization_reference,
    )
    return signed_headers(
        signed_body,
        path=path,
        method="POST",
        merchant_id=merchant_id,
        opc_id=opc_id,
        extra={
            "Content-Type": content_type,
            "X-Content-SHA256": digest,
            "X-Asset-Role": role,
            "X-Authorized-Purpose": authorized_purpose,
            "X-Authorization-Reference": authorization_reference,
        },
    )


def test_style_catalogue_requires_human_choice_when_multiple_have_no_default(
    tmp_path: Path,
) -> None:
    path = "/internal/mall-style/v1/style-selections"
    with receiver(tmp_path) as (client, sessions, _), sessions() as session:
        seed_confirmed_style(session, name="暖白")
        seed_confirmed_style(session, name="深色")

        response = client.get(
            path, headers=signed_headers(b"", path=path, method="GET")
        )

        assert response.status_code == 200
        assert response.json()["selection_status"] == "selection_required"
        assert response.json()["selection_reason"] == "multiple_without_default"
        assert response.json()["default_selection"] is None
        assert len(response.json()["selections"]) == 2
        assert not any(row["is_default"] for row in response.json()["selections"])


def test_style_catalogue_returns_only_owned_confirmed_current_styles_and_default(
    tmp_path: Path,
) -> None:
    path = "/internal/mall-style/v1/style-selections"
    with receiver(tmp_path) as (client, sessions, _), sessions() as session:
        selected_scene, selected_version, _ = seed_confirmed_style(
            session, name="暖白", default_slot="hero"
        )
        seed_confirmed_style(session, name="深色")
        seed_confirmed_style(session, merchant_id=OTHER, name="他店风格")

        response = client.get(
            path, headers=signed_headers(b"", path=path, method="GET")
        )
        foreign = client.get(
            path,
            headers=signed_headers(b"", path=path, method="GET", merchant_id=OTHER),
        )

        assert response.status_code == 200
        assert response.json()["selection_status"] == "ready"
        assert response.json()["selection_reason"] == "explicit_default"
        assert response.json()["default_selection"] == {
            "scene_style_id": str(selected_scene.id),
            "style_card_version_id": str(selected_version.id),
            "slot_key": "hero",
        }
        assert {row["scene_name"] for row in response.json()["selections"]} == {
            "暖白",
            "深色",
        }
        assert {row["scene_name"] for row in foreign.json()["selections"]} == {
            "他店风格"
        }


def test_style_catalogue_hides_unconfirmed_styles_and_returns_no_media(
    tmp_path: Path,
) -> None:
    path = "/internal/mall-style/v1/style-selections"
    with receiver(tmp_path) as (client, sessions, _), sessions() as session:
        seed_confirmed_style(session, name="未确认", confirmed=False)

        response = client.get(
            path, headers=signed_headers(b"", path=path, method="GET")
        )

        assert response.status_code == 200
        assert response.json()["selection_status"] == "unavailable"
        assert response.json()["selection_reason"] == "no_confirmed_style"
        assert response.json()["selections"] == []
        assert "photo" not in response.text.casefold()
        assert "image" not in response.text.casefold()


def test_product_photo_import_is_idempotent_and_starts_no_job(tmp_path: Path) -> None:
    body = png_bytes()
    source_id = uuid4()
    path = import_path(source_id)
    headers = import_headers(body, path=path)
    with receiver(tmp_path) as (client, sessions, store):
        first = client.post(path, content=body, headers=headers)
        replay = client.post(path, content=body, headers=headers)

        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["disposition"] == "duplicate"
        assert replay.json()["authorization_reference"] == "mall-consent-001"
        assert (
            first.json()["style_product_asset_id"]
            == replay.json()["style_product_asset_id"]
        )
        assert first.json()["subject_confirmation_required"] is True
        assert first.json()["generation_started"] is False
        assert first.json()["publishable"] is False
        asset_id = UUID(first.json()["style_product_asset_id"])
        assert (
            store.read_original(MERCHANT, asset_id, hashlib.sha256(body).hexdigest())
            == body
        )
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ProductAsset)) == 1
            permission = session.scalar(select(ProductAssetPermissionEvent))
            assert permission is not None
            assert permission.provenance == (
                "mall_product_asset_import:mall-consent-001"
            )
            assert (
                session.scalar(select(func.count()).select_from(ProductSubjectSeal))
                == 0
            )
            assert session.scalar(select(func.count()).select_from(ImageOutputJob)) == 0


def test_source_digest_conflict_label_role_and_bad_signature_fail_closed(
    tmp_path: Path,
) -> None:
    source_id = uuid4()
    first_body = png_bytes()
    path = import_path(source_id)
    with receiver(tmp_path) as (client, sessions, _):
        first = client.post(
            path,
            content=first_body,
            headers=import_headers(first_body, path=path),
        )
        changed_body = png_bytes((90, 80, 70))
        conflict = client.post(
            path,
            content=changed_body,
            headers=import_headers(changed_body, path=path),
        )
        label_source = uuid4()
        label_path = import_path(label_source)
        label = client.post(
            label_path,
            content=first_body,
            headers=import_headers(first_body, path=label_path, role="label_photo"),
        )
        invalid_headers = import_headers(first_body, path=import_path(uuid4()))
        invalid_headers["X-Signature"] = "0" * 64
        invalid = client.post(
            import_path(uuid4()), content=first_body, headers=invalid_headers
        )
        tampered_source = uuid4()
        tampered_path = import_path(tampered_source)
        tampered_headers = import_headers(first_body, path=tampered_path)
        tampered_headers["X-Authorization-Reference"] = "different-consent"
        tampered_authority = client.post(
            tampered_path, content=first_body, headers=tampered_headers
        )
        tampered_opc_source = uuid4()
        tampered_opc_path = import_path(tampered_opc_source)
        tampered_opc_headers = import_headers(first_body, path=tampered_opc_path)
        tampered_opc_headers["X-OpC-Id"] = str(uuid4())
        tampered_opc = client.post(
            tampered_opc_path, content=first_body, headers=tampered_opc_headers
        )
        opc_tamper_source = uuid4()
        opc_tamper_path = import_path(opc_tamper_source)
        opc_tamper_headers = import_headers(first_body, path=opc_tamper_path)
        opc_tamper_headers["X-OpC-Id"] = str(OTHER)
        tampered_opc = client.post(
            opc_tamper_path, content=first_body, headers=opc_tamper_headers
        )

        assert first.status_code == 201
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "mall_product_asset_source_conflict"
        assert label.status_code == 422
        assert label.json()["error_code"] == "mall_product_asset_role_invalid"
        assert invalid.status_code == 401
        assert tampered_authority.status_code == 401
        assert tampered_opc.status_code == 401
        assert tampered_opc.status_code == 401
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(MallStyleSourceAssetBinding)
                    .where(MallStyleSourceAssetBinding.asset_role == "label_photo")
                )
                == 0
            )


def test_same_source_id_is_isolated_between_merchants(tmp_path: Path) -> None:
    body = png_bytes()
    source_id = uuid4()
    path = import_path(source_id)
    with receiver(tmp_path) as (client, sessions, _):
        first = client.post(
            path,
            content=body,
            headers=import_headers(body, path=path, merchant_id=MERCHANT),
        )
        second = client.post(
            path,
            content=body,
            headers=import_headers(body, path=path, merchant_id=OTHER),
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert (
            first.json()["style_product_asset_id"]
            != second.json()["style_product_asset_id"]
        )
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ProductAsset)) == 2


def test_same_source_id_is_isolated_between_opcs(tmp_path: Path) -> None:
    body = png_bytes()
    source_id = uuid4()
    other_opc = uuid4()
    path = import_path(source_id)
    with receiver(tmp_path) as (client, sessions, _):
        first = client.post(path, content=body, headers=import_headers(body, path=path))
        second = client.post(
            path,
            content=body,
            headers=import_headers(body, path=path, opc_id=other_opc),
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert (
            first.json()["style_product_asset_id"]
            != second.json()["style_product_asset_id"]
        )
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ProductAsset)) == 2


def test_product_import_validates_digest_mime_decode_and_size(tmp_path: Path) -> None:
    body = png_bytes()
    path = import_path(uuid4())
    with receiver(tmp_path, max_bytes=len(body) - 1) as (client, _, _):
        too_large = client.post(
            path, content=body, headers=import_headers(body, path=path)
        )
        assert too_large.status_code == 413
        assert too_large.json()["error_code"] == "mall_product_asset_too_large"

    with receiver(tmp_path / "other") as (client, _, _):
        wrong_digest_headers = import_headers(body, path=path, claimed_sha256="0" * 64)
        wrong_digest = client.post(path, content=body, headers=wrong_digest_headers)
        wrong_mime_headers = import_headers(body, path=path, content_type="image/jpeg")
        wrong_mime = client.post(path, content=body, headers=wrong_mime_headers)
        invalid_body = b"not-an-image"
        invalid_path = import_path(uuid4())
        invalid_decode = client.post(
            invalid_path,
            content=invalid_body,
            headers=import_headers(invalid_body, path=invalid_path),
        )

        assert wrong_digest.status_code == 422
        assert wrong_digest.json()["error_code"] == (
            "mall_product_asset_digest_mismatch"
        )
        assert wrong_mime.status_code == 422
        assert wrong_mime.json()["error_code"] == "mall_product_asset_mime_mismatch"
        assert invalid_decode.status_code == 422
        assert invalid_decode.json()["error_code"] == (
            "mall_product_asset_decode_invalid"
        )


def test_product_import_fails_closed_without_private_storage(tmp_path: Path) -> None:
    body = png_bytes()
    path = import_path(uuid4())
    with receiver(tmp_path) as (client, _, _):
        delattr(client.app.state, "mall_style_asset_store")

        response = client.post(
            path, content=body, headers=import_headers(body, path=path)
        )

        assert response.status_code == 503
        assert response.json()["error_code"] == (
            "mall_product_asset_storage_unavailable"
        )


def test_concurrent_identical_imports_create_one_asset_and_safe_replay(
    tmp_path: Path,
) -> None:
    body = png_bytes()
    source_id = uuid4()
    path = import_path(source_id)
    headers = import_headers(body, path=path)
    with receiver(tmp_path) as (client, sessions, _):
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _: client.post(path, content=body, headers=headers),
                    range(2),
                )
            )

        assert sorted(response.status_code for response in responses) == [200, 201]
        assert (
            len({response.json()["style_product_asset_id"] for response in responses})
            == 1
        )
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(ProductAsset)) == 1
            assert (
                session.scalar(
                    select(func.count()).select_from(ProductAssetPermissionEvent)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count()).select_from(MallStyleSourceAssetBinding)
                )
                == 1
            )


def test_failed_database_commit_rolls_back_asset_and_private_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    Base.metadata.create_all(database)
    store = LocalSyntheticAssetStore(tmp_path / "rollback-media")
    body = png_bytes()
    digest = hashlib.sha256(body).hexdigest()
    source_id = uuid4()
    with Session(database) as session:

        def fail_commit() -> None:
            raise IntegrityError("forced commit failure", {}, RuntimeError("boom"))

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(IntegrityError, match="forced commit failure"):
            MallProductAssetImportService(session, store).import_product_photo(
                merchant_id=MERCHANT,
                opc_id=OPC,
                product_id="SKU-001",
                source_asset_id=source_id,
                claimed_sha256=digest,
                authorization_reference="mall-consent-rollback",
                content=body,
                mime_type="image/png",
                width=12,
                height=9,
            )

    with Session(database) as session:
        assert session.scalar(select(func.count()).select_from(ProductAsset)) == 0
        assert (
            session.scalar(
                select(func.count()).select_from(ProductAssetPermissionEvent)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(MallStyleSourceAssetBinding)
            )
            == 0
        )
    assert list((tmp_path / "rollback-media").rglob("*.bin")) == []
    store.close()
    database.dispose()


def test_existing_product_asset_intake_keeps_default_permission_provenance(
    tmp_path: Path,
) -> None:
    database = create_engine(f"sqlite:///{tmp_path / 'default-provenance.db'}")
    Base.metadata.create_all(database)
    request = ProductAssetIntakeRequest.model_validate(
        {
            "product_id": "SKU-OLD",
            "photo_id": "existing-flow-photo",
            "confirmed": True,
            "source_type": "photo_organizer",
            "quality_flags": [],
            "occurred_at": datetime.now(UTC).isoformat(),
            "transform_permission": True,
            "content_sha256": "e" * 64,
            "mime_type": "image/jpeg",
            "width": 12,
            "height": 9,
        }
    )
    with Session(database) as session:
        ProductAssetService(session).intake(MERCHANT, request)
        permission = session.scalar(select(ProductAssetPermissionEvent))
        assert permission is not None
        assert permission.provenance == "product_asset_intake"
    database.dispose()


def _post_handoff_for_imported_product(
    client: TestClient,
    *,
    scene: SceneStyle,
    version: StyleCardVersion,
    source_id: UUID,
    asset_id: UUID,
    content_sha256: str,
) -> dict[str, object]:
    submission_id = uuid4()
    unsealed = MallStyleHandoffV1.model_construct(
        schema_version="mall-style-handoff-v1",
        handoff_id=uuid4(),
        merchant_id=MERCHANT,
        opc_id=OPC,
        product_entry_submission_id=submission_id,
        product_entry_draft_version=1,
        product_entry_draft_sha256="9" * 64,
        product_id="SKU-001",
        correlation_id="mall-results-e2e",
        idempotency_key=f"mall-style:{submission_id}",
        payload_sha256="0" * 64,
        authorized_purpose="style_candidate_generation_for_merchant_review",
        assets=(
            MallStyleAssetRefV1(
                source_asset_id=uuid4(),
                source_content_sha256="8" * 64,
                asset_role="label_photo",
                mime_type="image/jpeg",
                authorized_purposes=("product_metadata_extraction",),
            ),
            MallStyleAssetRefV1(
                source_asset_id=source_id,
                source_content_sha256=content_sha256,
                asset_role="product_photo",
                mime_type="image/png",
                style_product_asset_id=asset_id,
                authorized_purposes=("style_subject_transform",),
            ),
        ),
        selected_product_photo_source_asset_id=source_id,
        style_selection=MallStyleSelectionV1(
            scene_style_id=scene.id,
            style_card_version_id=version.id,
            slot_key="hero",
        ),
        candidate_count=3,
        created_at=datetime.now(UTC),
        automatic_generation_allowed=False,
        automatic_publication_allowed=False,
        publishable=False,
    )
    data = unsealed.model_dump(mode="python")
    data["payload_sha256"] = canonical_mall_payload_sha256(unsealed)
    handoff = MallStyleHandoffV1.model_validate(data)
    body = canonical_mall_handoff_bytes(handoff)
    path = "/internal/mall-style/v1/handoffs"
    response = client.post(
        path,
        content=body,
        headers=signed_headers(body, path=path, method="POST"),
    )
    assert response.status_code == 202, response.text
    return response.json()


def _subject_confirmation_headers(
    mask: bytes,
    *,
    path: str,
    receipt_id: UUID,
    asset_sha256: str,
    base_seal_id: UUID,
    base_mask_sha256: str,
    actor_role: str = "merchant_owner",
) -> dict[str, str]:
    metadata_data: dict[str, object] = {
        "schema_version": "mall-subject-confirmation-metadata-v1",
        "receipt_id": str(receipt_id),
        "product_id": "SKU-001",
        "product_asset_sha256": asset_sha256,
        "base_subject_seal_id": str(base_seal_id),
        "base_mask_sha256": base_mask_sha256,
        "proposal_version": 1,
        "reviewed_mask_sha256": hashlib.sha256(mask).hexdigest(),
        "owner_actor_reference": "mall-owner-001",
        "actor_role": actor_role,
        "idempotency_key": f"mall-subject-confirmation:{receipt_id}",
        "mime_type": "image/png",
        "confirmed": True,
    }
    signed_body = canonical_mall_subject_confirmation_signature_bytes(
        metadata_data, mask
    )
    return signed_headers(
        signed_body,
        path=path,
        method="POST",
        extra={
            "Content-Type": "image/png",
            "X-Product-Asset-SHA256": asset_sha256,
            "X-Base-Subject-Seal-Id": str(base_seal_id),
            "X-Base-Mask-SHA256": base_mask_sha256,
            "X-Proposal-Version": "1",
            "X-Reviewed-Mask-SHA256": hashlib.sha256(mask).hexdigest(),
            "X-Owner-Actor-Reference": "mall-owner-001",
            "X-Actor-Role": actor_role,
            "X-Confirmation-Key": f"mall-subject-confirmation:{receipt_id}",
            "X-Confirmed-Subject-Mask": "true",
        },
    )


def _approval_body(receipt_id: UUID) -> bytes:
    approved_at = datetime.now(UTC)
    unsealed = MallStyleGenerationApprovalV1.model_construct(
        schema_version="mall-style-generation-approval-v1",
        receipt_id=receipt_id,
        product_id="SKU-001",
        idempotency_key=f"mall-style-generation:{receipt_id}",
        approval_reference="mall-fee-approval-001",
        provider="test-disabled-provider",
        model="test-disabled-model",
        owner_actor_reference="mall-owner-001",
        actor_role="merchant_owner",
        automatic_retry_allowed=False,
        approved_candidate_count=3,
        approved_maximum_cost_usd=Decimal("0.360000"),
        currency="USD",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(hours=1),
        approval_payload_sha256="0" * 64,
        automatic_publication_allowed=False,
        publishable=False,
    )
    data = unsealed.model_dump(mode="python")
    data["approval_payload_sha256"] = canonical_mall_generation_approval_sha256(
        unsealed
    )
    approval = MallStyleGenerationApprovalV1.model_validate(data)
    return json.dumps(
        approval.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize("manual_subject", [False, True])
@pytest.mark.parametrize("invalidation", ["rights", "expiry", "asset_digest", "style"])
def test_mall_owner_subject_confirmation_fee_queue_and_results_are_closed_loop(
    tmp_path: Path,
    invalidation: str,
    manual_subject: bool,
) -> None:
    product_image = Image.new("RGBA", (12, 9), (0, 0, 0, 0))
    for y in range(2, 7):
        for x in range(3, 9):
            product_image.putpixel((x, y), (10, 20, 30, 255))
    product_output = io.BytesIO()
    product_image.save(product_output, format="PNG")
    product_bytes = product_output.getvalue()
    source_id = uuid4()
    source_digest = hashlib.sha256(product_bytes).hexdigest()
    with receiver(tmp_path, manual_subject=manual_subject) as (client, sessions, store):
        with sessions() as session:
            scene, version, _ = seed_confirmed_style(
                session, name="商城主图", default_slot="hero"
            )
        asset_path = import_path(source_id)
        imported = client.post(
            asset_path,
            content=product_bytes,
            headers=import_headers(product_bytes, path=asset_path),
        )
        assert imported.status_code == 201, imported.text
        asset_id = UUID(imported.json()["style_product_asset_id"])
        handoff = _post_handoff_for_imported_product(
            client,
            scene=scene,
            version=version,
            source_id=source_id,
            asset_id=asset_id,
            content_sha256=source_digest,
        )
        receipt_id = UUID(str(handoff["receipt_id"]))
        assert handoff["status"] == "awaiting_subject_confirmation"

        inspect_path = (
            f"/internal/mall-style/v1/products/SKU-001/receipts/{receipt_id}/"
            "subject-confirmation"
        )
        before = client.get(
            inspect_path,
            headers=signed_headers(b"", path=inspect_path, method="GET"),
        )
        assert before.status_code == 200
        assert before.json()["status"] == "proposal_required"
        original_path = before.json()["original_url"]
        original = client.get(
            original_path,
            headers=signed_headers(b"", path=original_path, method="GET"),
        )
        assert original.status_code == 200
        assert original.content == product_bytes

        proposal_path = (
            f"/internal/mall-style/v1/products/SKU-001/receipts/{receipt_id}/"
            "subject-proposals"
        )
        proposal = client.post(
            proposal_path,
            headers=signed_headers(b"", path=proposal_path, method="POST"),
        )
        assert proposal.status_code == 200, proposal.text
        assert proposal.json()["status"] == "needs_human_review"
        mask_path = proposal.json()["mask_url"]
        mask_response = client.get(
            mask_path,
            headers=signed_headers(b"", path=mask_path, method="GET"),
        )
        assert mask_response.status_code == 200
        mask = mask_response.content
        assert proposal.json()["manual_confirmation_required"] is True
        assert proposal.json()["proposal_method"] == (
            "manual_full_frame" if manual_subject else "automatic"
        )
        if manual_subject:
            assert proposal.json()["quality_warning"]
            full_mask = mask
            reviewed = Image.new("L", (12, 9), 0)
            for y in range(2, 7):
                for x in range(3, 9):
                    reviewed.putpixel((x, y), 255)
            output = io.BytesIO()
            reviewed.save(output, format="PNG")
            mask = output.getvalue()
        base_seal_id = UUID(proposal.json()["base_subject_seal_id"])
        confirm_path = f"{proposal_path}/{base_seal_id}/confirm"
        confirm_headers = _subject_confirmation_headers(
            mask,
            path=confirm_path,
            receipt_id=receipt_id,
            asset_sha256=source_digest,
            base_seal_id=base_seal_id,
            base_mask_sha256=proposal.json()["base_mask_sha256"],
        )
        if manual_subject:
            unreviewed_headers = _subject_confirmation_headers(
                full_mask,
                path=confirm_path,
                receipt_id=receipt_id,
                asset_sha256=source_digest,
                base_seal_id=base_seal_id,
                base_mask_sha256=proposal.json()["base_mask_sha256"],
            )
            assert (
                client.post(
                    confirm_path, content=full_mask, headers=unreviewed_headers
                ).status_code
                == 409
            )
        confirmed = client.post(confirm_path, content=mask, headers=confirm_headers)
        replay = client.post(confirm_path, content=mask, headers=confirm_headers)
        assert confirmed.status_code == 201, confirmed.text
        assert replay.status_code == 200, replay.text
        assert confirmed.json()["confirmation_id"] == replay.json()["confirmation_id"]
        assert confirmed.json()["owner_actor_reference"] == "mall-owner-001"
        checked = client.get(
            inspect_path, headers=signed_headers(b"", path=inspect_path, method="GET")
        )
        assert checked.json()["status"] == "confirmed"
        assert checked.json()["manual_confirmation_required"] is False
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(MallStyleSubjectConfirmation)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.event_type == "mall_product_subject.owner_confirmed"
                    )
                )
                == 1
            )

        supplier_headers = _subject_confirmation_headers(
            mask,
            path=confirm_path,
            receipt_id=receipt_id,
            asset_sha256=source_digest,
            base_seal_id=base_seal_id,
            base_mask_sha256=proposal.json()["base_mask_sha256"],
            actor_role="supplier",
        )
        assert (
            client.post(
                confirm_path, content=mask, headers=supplier_headers
            ).status_code
            == 403
        )

        approval_path = (
            f"/internal/mall-style/v1/receipts/{receipt_id}/generation-approvals"
        )
        approval_body = _approval_body(receipt_id)
        approval_headers = signed_headers(
            approval_body, path=approval_path, method="POST"
        )
        approved = client.post(
            approval_path, content=approval_body, headers=approval_headers
        )
        approval_replay = client.post(
            approval_path, content=approval_body, headers=approval_headers
        )
        assert approved.status_code == 201, approved.text
        assert approval_replay.status_code == 200
        assert approved.json()["status"] == "pending_execution"
        assert approved.json()["publishable"] is False
        assert approved.json()["provider"] == "test-disabled-provider"
        assert approved.json()["model"] == "test-disabled-model"
        assert approved.json()["automatic_retry_allowed"] is False
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.event_type == "mall_style_generation.owner_approved"
                    )
                )
                == 1
            )
            assert session.scalar(select(func.count()).select_from(ImageOutputJob)) == 0
        changed_approval = json.loads(approval_body)
        changed_approval["model"] = "changed-model"
        changed_unsealed = MallStyleGenerationApprovalV1.model_validate_json(
            approval_body
        ).model_copy(update={"model": "changed-model"})
        changed_approval["approval_payload_sha256"] = (
            canonical_mall_generation_approval_sha256(changed_unsealed)
        )
        changed_body = json.dumps(
            changed_approval, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        assert (
            client.post(
                approval_path,
                content=changed_body,
                headers=signed_headers(changed_body, path=approval_path, method="POST"),
            ).status_code
            == 409
        )

        results_path = (
            f"/internal/mall-style/v1/products/SKU-001/receipts/{receipt_id}/results"
        )
        pending = client.get(
            results_path,
            headers=signed_headers(b"", path=results_path, method="GET"),
        )
        assert pending.status_code == 200
        assert pending.json()["status"] == "pending_execution"
        assert pending.json()["candidates"] == []

        candidate_bytes = png_bytes((90, 80, 70))
        candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
        with sessions() as session:
            queue = session.scalar(select(MallStyleGenerationQueueItem))
            assert queue is not None
            job = ImageOutputJob(
                customer_id=MERCHANT,
                output_type="product_style",
                style_card_version_id=version.id,
                scene_style_id=scene.id,
                subject_seal_id=queue.subject_seal_id,
                identity_lock_phase="candidates_attested",
                output_ratio="1:1",
                adapter_version="test-no-provider-v1",
                status="awaiting_customer_review",
                failure_reason=None,
                request_metadata={
                    "publishable": False,
                    "mall_generation_request_id": str(queue.id),
                    "generation_approval_reference": queue.approval_reference,
                    "rights_reference": queue.rights_reference,
                    "provider": queue.provider,
                    "model": queue.model,
                    "source_product_ids": ["SKU-001"],
                    "source_asset_ids": [str(asset_id)],
                    "scene_style_id": str(scene.id),
                    "style_card_version_id": str(version.id),
                    "slot_key": "hero",
                },
                idempotency_key=f"mall-queue-test:{queue.id}",
            )
            session.add(job)
            session.flush()
            target = store.reserve_derivative(MERCHANT, job.id, 1, extension="png")
            artifact = store.create_derivative(
                target,
                NormalizedPayload(
                    content=candidate_bytes,
                    content_sha256=candidate_digest,
                    mime_type="image/png",
                    width=12,
                    height=9,
                    metadata={},
                ),
            )
            candidate = ImageCandidate(
                customer_id=MERCHANT,
                output_job_id=job.id,
                sequence_number=1,
                identity="synthetic_style_image_candidate",
                artifact_path=str(artifact.output_path),
                content_sha256=candidate_digest,
                mime_type="image/png",
                width=12,
                height=9,
                adapter_version="test-no-provider-v1",
                artifact_metadata={
                    "publishable": False,
                    "mall_generation_request_id": str(queue.id),
                    "generation_approval_reference": queue.approval_reference,
                    "rights_reference": queue.rights_reference,
                    "provider": queue.provider,
                    "model": queue.model,
                    "source_product_ids": ["SKU-001"],
                    "source_asset_ids": [str(asset_id)],
                    "scene_style_id": str(scene.id),
                    "style_card_version_id": str(version.id),
                    "slot_key": "hero",
                },
            )
            session.add(candidate)
            queue.generation_job_id = job.id
            queue.status = "dispatched"
            session.commit()
            job_id = job.id
            candidate_id = candidate.id

        job_results_path = (
            f"/internal/mall-style/v1/products/SKU-001/jobs/{job_id}/results"
        )
        job_results = client.get(
            job_results_path,
            headers=signed_headers(b"", path=job_results_path, method="GET"),
        )
        assert job_results.status_code == 200, job_results.text
        result_candidate = job_results.json()["candidates"][0]
        assert result_candidate["asset_id"] == str(candidate_id)
        assert result_candidate["asset_version"] == 1
        assert result_candidate["content_sha256"] == candidate_digest
        assert result_candidate["rights_reference"].startswith(
            "product_asset_permission_event:"
        )
        assert result_candidate["generation_approval_reference"] == (
            "mall-fee-approval-001"
        )
        assert result_candidate["publishable"] is False
        content_path = result_candidate["content_url"]
        content = client.get(
            content_path,
            headers=signed_headers(b"", path=content_path, method="GET"),
        )
        assert content.status_code == 200
        assert content.content == candidate_bytes
        assert content.headers["x-content-sha256"] == candidate_digest

        foreign = client.get(
            job_results_path,
            headers=signed_headers(
                b"", path=job_results_path, method="GET", merchant_id=OTHER
            ),
        )
        assert foreign.status_code == 404

        with sessions() as session:
            if invalidation == "rights":
                session.add(
                    ProductAssetPermissionEvent(
                        customer_id=MERCHANT,
                        product_asset_id=asset_id,
                        transform_permission=False,
                        occurred_at=datetime.now(UTC) + timedelta(seconds=1),
                        provenance="mall_owner_revoked",
                    )
                )
            elif invalidation == "expiry":
                queue = session.scalar(select(MallStyleGenerationQueueItem))
                assert queue is not None
                queue.approval_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif invalidation == "asset_digest":
                # Simulate storage/DB corruption: the normal ORM already forbids mutation.
                session.execute(
                    update(ProductAsset.__table__)
                    .where(ProductAsset.id == asset_id)
                    .values(content_sha256="f" * 64)
                )
            else:
                target_scene = session.get(SceneStyle, scene.id)
                assert target_scene is not None
                target_scene.active = False
            session.commit()
        revoked = client.get(
            job_results_path,
            headers=signed_headers(b"", path=job_results_path, method="GET"),
        )
        assert revoked.status_code == 200
        expected_status = "expired" if invalidation == "expiry" else "revoked"
        assert revoked.json()["status"] == expected_status
        assert revoked.json()["candidates"][0]["availability"] == expected_status
        assert revoked.json()["candidates"][0]["content_url"] is None
        assert (
            client.get(
                content_path,
                headers=signed_headers(b"", path=content_path, method="GET"),
            ).status_code
            == 404
        )

        approval_after_invalidation = client.post(
            approval_path, content=approval_body, headers=approval_headers
        )
        assert approval_after_invalidation.status_code == 409
        if invalidation in {"rights", "asset_digest"}:
            assert client.post(
                confirm_path, content=mask, headers=confirm_headers
            ).status_code in {404, 409}
            assert (
                client.get(
                    original_path,
                    headers=signed_headers(b"", path=original_path, method="GET"),
                ).status_code
                == 404
            )
