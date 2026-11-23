import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.routes_mall_style_handoff_v1 import router
from opc_style.contracts.mall_style_handoff_v1 import (
    MallStyleAssetRefV1,
    MallStyleHandoffV1,
    MallStyleSelectionV1,
    canonical_mall_handoff_bytes,
    canonical_mall_payload_sha256,
)
from opc_style.domain.subject_seal import HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION
from opc_style.models import (
    Base,
    Customer,
    MallStyleHandoffReceipt,
    ProductAsset,
    ProductAssetPermissionEvent,
    ProductSubjectSeal,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
    StyleTemplateSlot,
)
from opc_style.services.style_warehouse_v3 import style_request_signature

MERCHANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPC = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SECRET = "mall-style-receiver-test-secret"
PATH = "/internal/mall-style/v1/handoffs"


@contextmanager
def receiver(tmp_path: Path) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'mall-style.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    app = FastAPI()
    app.state.style_warehouse_v3_session_factory = sessions
    app.state.style_warehouse_v3_settings = SimpleNamespace(
        signing_secret=SecretStr(SECRET), signature_tolerance_seconds=300
    )
    app.state.style_suite_planning_settings = SimpleNamespace(
        candidate_estimate_usd=Decimal("0.12")
    )
    app.include_router(router)
    with TestClient(app) as client:
        yield client, sessions
    engine.dispose()


def seed_style(session: Session, merchant_id: UUID = MERCHANT) -> tuple[SceneStyle, StyleCardVersion]:
    session.add(Customer(id=merchant_id, display_name="Mall merchant"))
    card = StyleCard(customer_id=merchant_id, kind="derived", active=True)
    session.add(card)
    session.flush()
    version_id = uuid4()
    scene_id = uuid4()
    slot_id = uuid4()
    slot_rules = {"aspect_ratio": "1:1"}
    version = StyleCardVersion(
        id=version_id,
        customer_id=merchant_id,
        style_card_id=card.id,
        version=1,
        content={
            "tone": ["warm"],
            "slots": [
                {
                    "id": str(slot_id),
                    "customer_id": str(merchant_id),
                    "scene_style_id": str(scene_id),
                    "style_card_version_id": str(version_id),
                    "slot_key": "hero",
                    "label": "主图",
                    "position": 0,
                    "active": True,
                    "rules": slot_rules,
                }
            ],
        },
    )
    scene = SceneStyle(
        id=scene_id,
        customer_id=merchant_id,
        style_card_id=card.id,
        name="mall-style",
        active=True,
        rules={},
    )
    session.add_all([version, scene])
    session.flush()
    session.add(
        StyleTemplateSlot(
            id=slot_id,
            customer_id=merchant_id,
            scene_style_id=scene.id,
            style_card_version_id=version.id,
            slot_key="hero",
            label="主图",
            rules=slot_rules,
            active=True,
            position=0,
        )
    )
    session.commit()
    return scene, version


def seed_product(
    session: Session, *, reviewed: bool = False, permitted: bool = True
) -> tuple[ProductAsset, ProductAssetPermissionEvent]:
    asset = ProductAsset(
        customer_id=MERCHANT,
        product_id="SKU-001",
        photo_id=f"mall-photo-{uuid4()}",
        identity="original_real_photo",
        confirmed=True,
        source_type="mall_authorized_upload",
        quality_flags=[],
        occurred_at=datetime.now(UTC),
        content_sha256="b" * 64,
        mime_type="image/png",
        width=1200,
        height=1200,
    )
    session.add(asset)
    session.flush()
    permission = ProductAssetPermissionEvent(
        customer_id=MERCHANT,
        product_asset_id=asset.id,
        transform_permission=permitted,
        occurred_at=datetime.now(UTC),
        provenance="mall-merchant-explicit-consent",
    )
    session.add(permission)
    session.flush()
    if reviewed:
        session.add(
            ProductSubjectSeal(
                customer_id=MERCHANT,
                product_id=asset.product_id,
                product_asset_id=asset.id,
                transform_permission_event_id=permission.id,
                source_sha256=asset.content_sha256,
                segmentation_adapter="human-review",
                segmentation_adapter_version=HUMAN_REVIEWED_SUBJECT_ADAPTER_VERSION,
                mask_sha256="c" * 64,
                normalized_subject_sha256="d" * 64,
                normalized_width=1200,
                normalized_height=1200,
                core_mask_pixel_count=100,
                feather_width_px=2,
                idempotency_fingerprint="e" * 64,
                artifact_store_namespace="test-only",
                seal_version=1,
                synthetic_test_only=True,
                publishable=False,
            )
        )
    session.commit()
    return asset, permission


def make_handoff(
    scene: SceneStyle,
    version: StyleCardVersion,
    *,
    product: ProductAsset | None = None,
    submission_id: UUID | None = None,
    handoff_id: UUID | None = None,
    label_source_id: UUID | None = None,
    label_sha256: str = "a" * 64,
) -> MallStyleHandoffV1:
    submission_id = submission_id or uuid4()
    label = MallStyleAssetRefV1(
        source_asset_id=label_source_id or uuid4(),
        source_content_sha256=label_sha256,
        asset_role="label_photo",
        mime_type="image/jpeg",
        authorized_purposes=("product_metadata_extraction",),
    )
    assets = [label]
    selected = None
    if product is not None:
        product_ref = MallStyleAssetRefV1(
            source_asset_id=uuid4(),
            source_content_sha256=product.content_sha256,
            asset_role="product_photo",
            mime_type="image/png",
            style_product_asset_id=product.id,
            authorized_purposes=("style_subject_transform",),
        )
        assets.append(product_ref)
        selected = product_ref.source_asset_id
    data: dict[str, object] = {
        "handoff_id": handoff_id or uuid4(),
        "merchant_id": MERCHANT,
        "opc_id": OPC,
        "product_entry_submission_id": submission_id,
        "product_entry_draft_version": 1,
        "product_entry_draft_sha256": "9" * 64,
        "product_id": "SKU-001",
        "correlation_id": f"mall-{submission_id}",
        "idempotency_key": f"mall-style:{submission_id}",
        "payload_sha256": "0" * 64,
        "authorized_purpose": "style_candidate_generation_for_merchant_review",
        "assets": tuple(assets),
        "selected_product_photo_source_asset_id": selected,
        "style_selection": MallStyleSelectionV1(
            scene_style_id=scene.id,
            style_card_version_id=version.id,
            slot_key="hero",
        ),
        "candidate_count": 3,
        "created_at": datetime.now(UTC),
        "automatic_generation_allowed": False,
        "automatic_publication_allowed": False,
        "publishable": False,
    }
    unsealed = MallStyleHandoffV1.model_construct(**data)
    data["payload_sha256"] = canonical_mall_payload_sha256(unsealed)
    return MallStyleHandoffV1.model_validate(data)


def signed_headers(
    body: bytes,
    *,
    merchant_id: UUID = MERCHANT,
    opc_id: UUID = OPC,
    method: str = "POST",
    path: str = PATH,
) -> dict[str, str]:
    timestamp = int(time.time())
    return {
        "Content-Type": "application/json",
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


def post(client: TestClient, value: MallStyleHandoffV1):
    body = canonical_mall_handoff_bytes(value)
    return client.post(PATH, content=body, headers=signed_headers(body))


def test_label_only_stops_at_product_photo_without_job_or_publication(tmp_path: Path) -> None:
    with receiver(tmp_path) as (client, sessions), sessions() as session:
        scene, version = seed_style(session)
        response = post(client, make_handoff(scene, version))

        assert response.status_code == 202
        assert response.json()["status"] == "awaiting_product_photo"
        assert response.json()["generation_job_id"] is None
        assert response.json()["publishable"] is False
        assert response.json()["approval_required"] is True
        assert session.scalar(select(func.count()).select_from(MallStyleHandoffReceipt)) == 1


def test_product_requires_permission_then_subject_review_then_fee_approval(tmp_path: Path) -> None:
    with receiver(tmp_path) as (client, sessions), sessions() as session:
        scene, version = seed_style(session)
        product, _ = seed_product(session)
        pending = post(client, make_handoff(scene, version, product=product))
        assert pending.status_code == 202
        assert pending.json()["status"] == "awaiting_subject_confirmation"

    with receiver(tmp_path / "reviewed") as (client, sessions), sessions() as session:
        scene, version = seed_style(session)
        product, _ = seed_product(session, reviewed=True)
        ready = post(client, make_handoff(scene, version, product=product))
        assert ready.status_code == 202
        assert ready.json()["status"] == "awaiting_generation_approval"
        assert ready.json()["candidate_count"] == 3
        assert Decimal(ready.json()["maximum_cost_usd"]) == Decimal("0.36")
        assert ready.json()["generation_job_id"] is None


def test_exact_replay_is_duplicate_but_changed_payload_conflicts(tmp_path: Path) -> None:
    with receiver(tmp_path) as (client, sessions), sessions() as session:
        scene, version = seed_style(session)
        submission_id = uuid4()
        first_value = make_handoff(scene, version, submission_id=submission_id)
        first = post(client, first_value)
        replay = post(client, first_value)
        changed = make_handoff(
            scene,
            version,
            submission_id=submission_id,
            handoff_id=first_value.handoff_id,
        )
        conflict = post(client, changed)

        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["disposition"] == "duplicate"
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == "mall_style_handoff_conflict"


def test_source_digest_conflict_and_cross_tenant_access_fail_closed(tmp_path: Path) -> None:
    with receiver(tmp_path) as (client, sessions), sessions() as session:
        scene, version = seed_style(session)
        shared_source = uuid4()
        first = make_handoff(scene, version, label_source_id=shared_source)
        accepted = post(client, first)
        changed = make_handoff(
            scene,
            version,
            label_source_id=shared_source,
            label_sha256="f" * 64,
        )
        digest_conflict = post(client, changed)
        receipt_id = accepted.json()["receipt_id"]
        receipt_path = f"/internal/mall-style/v1/receipts/{receipt_id}"
        foreign_headers = signed_headers(
            b"", merchant_id=OTHER, method="GET", path=receipt_path
        )
        hidden = client.get(receipt_path, headers=foreign_headers)

        assert digest_conflict.status_code == 409
        assert digest_conflict.json()["error_code"] == "mall_style_source_asset_conflict"
        assert hidden.status_code == 404


def test_unpermitted_product_and_invalid_signature_are_rejected(tmp_path: Path) -> None:
    with receiver(tmp_path) as (client, sessions), sessions() as session:
        scene, version = seed_style(session)
        product, _ = seed_product(session, permitted=False)
        denied = post(client, make_handoff(scene, version, product=product))
        value = make_handoff(scene, version)
        body = canonical_mall_handoff_bytes(value)
        headers = signed_headers(body)
        headers["X-Signature-Timestamp"] = str(
            int((datetime.now(UTC) - timedelta(minutes=10)).timestamp())
        )
        invalid = client.post(PATH, content=body, headers=headers)

        assert denied.status_code == 409
        assert denied.json()["error_code"] == "mall_product_asset_not_authorized"
        assert invalid.status_code == 401
