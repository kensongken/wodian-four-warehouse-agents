#!/usr/bin/env python3
"""Run a fresh zero-network identity-locked product-image acceptance."""

from __future__ import annotations

import json
import socket
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import mkdtemp
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import get_identity_subject_segmenter
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.identity_compositor import (
    IdentityLockAttestation,
    IdentityLockedComposite,
    Placement,
    attest_identity_lock,
)
from opc_style.domain.subject_seal import (
    DeterministicLocalSubjectSegmenter,
    SealedSubject,
)
from opc_style.main import create_app
from opc_style.models import (
    Base,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ProductSubjectSeal,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.repositories import ProductImageRepository
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
FOREIGN_CUSTOMER = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@contextmanager
def block_outbound_network():  # type: ignore[no-untyped-def]
    """Fail and count any attempted non-TestClient network connection."""
    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    def blocked_connect(sock: socket.socket, address: object) -> None:
        del sock
        attempts.append(repr(address))
        raise RuntimeError("outbound_network_attempt_blocked")

    def blocked_create_connection(*args: object, **kwargs: object) -> socket.socket:
        attempts.append(repr((args, kwargs)))
        raise RuntimeError("outbound_network_attempt_blocked")

    socket.socket.connect = blocked_connect  # type: ignore[method-assign]
    socket.create_connection = blocked_create_connection  # type: ignore[assignment]
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.create_connection = original_create_connection


def _alpha_fixture() -> bytes:
    """Create an explicit-alpha local fixture; it is not real-photo segmentation."""
    stream = BytesIO()
    image = Image.new("RGBA", (96, 120), (0, 0, 0, 0))
    for y in range(28, 92):
        for x in range(18, 78):
            distance = ((x - 48) ** 2 + (y - 60) ** 2) ** 0.5
            if 22 <= distance <= 30:
                image.putpixel((x, y), (174, 44, 36, 255))
    image.save(stream, format="PNG")
    return stream.getvalue()


def _expect(response: Any, status_code: int) -> dict[str, Any]:
    if response.status_code != status_code:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    return dict(response.json())


def _tamper_attestation(
    candidate_content: bytes,
    candidate: ImageCandidate,
    seal: ProductSubjectSeal,
    store: LocalSyntheticAssetStore,
) -> str:
    subject = SealedSubject(
        rgba_png=store.read_subject_artifact(
            CUSTOMER, seal.id, "rgba", seal.normalized_subject_sha256
        ),
        rgba_sha256=seal.normalized_subject_sha256,
        core_mask_png=store.read_subject_artifact(
            CUSTOMER, seal.id, "core-mask", seal.mask_sha256
        ),
        core_mask_sha256=seal.mask_sha256,
        width=seal.normalized_width,
        height=seal.normalized_height,
        core_mask_pixel_count=seal.core_mask_pixel_count,
        feather_width_px=seal.feather_width_px,
        adapter_version=seal.segmentation_adapter_version,
        synthetic_test_only=seal.synthetic_test_only,
        publishable=False,
    )
    metadata = dict(candidate.artifact_metadata)
    x = int(metadata["placement_x"])
    y = int(metadata["placement_y"])
    with Image.open(BytesIO(subject.core_mask_png)) as mask_image:
        mask_image.load()
        protected = next(
            (px, py)
            for py in range(mask_image.height)
            for px in range(mask_image.width)
            if mask_image.getpixel((px, py)) == 255
        )
    with Image.open(BytesIO(candidate_content)) as opened:
        opened.load()
        changed = opened.convert("RGBA")
    target = (x + protected[0], y + protected[1])
    red, green, blue, alpha = changed.getpixel(target)
    changed.putpixel(target, ((red + 1) % 256, green, blue, alpha))
    stream = BytesIO()
    changed.save(stream, format="PNG", optimize=False, compress_level=9)
    tampered = stream.getvalue()
    isolated = IdentityLockedComposite(
        content=tampered,
        content_sha256=sha256(tampered).hexdigest(),
        mime_type="image/png",
        width=candidate.width,
        height=candidate.height,
        placement=Placement(x, y),
        metadata={**metadata},
        attestation=IdentityLockAttestation(
            protected_pixels_match=True,
            protected_pixel_count=seal.core_mask_pixel_count,
            code="identity_lock_passed",
            candidate_sha256=sha256(tampered).hexdigest(),
            subject_rgba_sha256=seal.normalized_subject_sha256,
            core_mask_sha256=seal.mask_sha256,
            compositor_version=str(metadata["compositor_version"]),
        ),
        synthetic_test_only=True,
        publishable=False,
    )
    isolated.metadata["publishable"] = False
    result = attest_identity_lock(isolated, subject)
    if result.protected_pixels_match:
        raise RuntimeError("tampered protected pixel was accepted")
    return result.code


def main() -> int:
    evidence_root = Path(mkdtemp(prefix="identity-locked-acceptance-"))
    database = evidence_root / "acceptance.db"
    media_root = evidence_root / "media"
    engine = create_database_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sessions() as session:
        customer = Customer(id=CUSTOMER, display_name="Identity acceptance")
        foreign = Customer(id=FOREIGN_CUSTOMER, display_name="Foreign")
        card = StyleCard(customer_id=CUSTOMER, kind="derived", active=True)
        session.add_all([customer, foreign, card])
        session.flush()
        version = StyleCardVersion(
            customer_id=CUSTOMER,
            style_card_id=card.id,
            version=1,
            content={"lighting": "soft daylight"},
        )
        scene = SceneStyle(
            customer_id=CUSTOMER,
            style_card_id=card.id,
            name="tabletop",
            active=True,
            rules={"surface": "stone", "no_product": True},
        )
        session.add_all([version, scene])
        session.commit()

    store = LocalSyntheticAssetStore(media_root)
    app = create_app()

    def session_override():  # type: ignore[no-untyped-def]
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_asset_content_store] = lambda: store
    app.dependency_overrides[get_identity_subject_segmenter] = (
        DeterministicLocalSubjectSegmenter
    )
    headers = {"X-Customer-Id": str(CUSTOMER)}
    fixture = _alpha_fixture()
    steps: list[str] = []

    try:
        with block_outbound_network() as network_attempts:
            with TestClient(app) as client:
                if client.get("/health").status_code != 200:
                    raise RuntimeError("health endpoint failed")
                if client.get("/local/style-suite").status_code != 200:
                    raise RuntimeError("page endpoint failed")
                steps.append("health_and_page_http_200")
                asset = _expect(
                    client.post(
                        "/local/style-suite/api/product",
                        headers={
                            **headers,
                            "X-Product-Id": "IDENTITY-ACCEPTANCE-001",
                            "X-Photo-Id": "IDENTITY-ACCEPTANCE-PHOTO-001",
                            "Content-Type": "image/png",
                        },
                        content=fixture,
                    ),
                    201,
                )
                steps.append("source_uploaded_and_transform_permission_recorded")
                payload = {
                    "sources": [
                        {"product_id": asset["product_id"], "asset_id": asset["id"]}
                    ],
                    "scene_style_id": str(scene.id),
                    "style_card_version_id": str(version.id),
                    "output_ratio": "4:5",
                    "slot_key": "tabletop-hero",
                    "idempotency_key": "identity-acceptance-v1",
                }
                created = _expect(
                    client.post(
                        "/v1/image-outputs/product-style/identity-locked",
                        headers=headers,
                        json=payload,
                    ),
                    201,
                )
                replay = _expect(
                    client.post(
                        "/v1/image-outputs/product-style/identity-locked",
                        headers=headers,
                        json=payload,
                    ),
                    200,
                )
                refreshed = _expect(
                    client.get(f"/v1/image-outputs/{created['id']}", headers=headers),
                    200,
                )
                if not (
                    created["id"] == replay["id"] == refreshed["id"]
                    and len(created["candidates"]) == 3
                    and all(
                        item["protected_pixels_match"] is True
                        for item in created["candidates"]
                    )
                ):
                    raise RuntimeError("identity-locked create/replay/refresh failed")
                steps.extend(
                    [
                        "subject_sealed_with_explicit_alpha_fixture",
                        "three_backgrounds_composed_and_pixels_attested",
                        "idempotent_replay_returned_same_job",
                        "job_state_reloaded_from_database",
                    ]
                )
                first = created["candidates"][0]
                candidate_content = client.get(
                    f"/v1/image-outputs/{created['id']}/candidates/{first['id']}/content",
                    headers=headers,
                )
                original_content = client.get(
                    f"/v1/image-outputs/{created['id']}/original-content",
                    headers=headers,
                )
                if candidate_content.status_code != 200 or original_content.content != fixture:
                    raise RuntimeError("candidate/original display failed")
                checked = _expect(
                    client.post(
                        f"/v1/image-outputs/{created['id']}/authenticity-checks",
                        headers=headers,
                    ),
                    201,
                )
                reviewed = _expect(
                    client.post(
                        f"/v1/image-outputs/{created['id']}/candidates/"
                        f"{first['id']}/review",
                        headers=headers,
                        json={
                            "decision": "approved",
                            "reason": None,
                            "idempotency_key": "identity-acceptance-review-v1",
                            "fidelity_state": "green",
                            "fidelity_flags": [],
                        },
                    ),
                    201,
                )
                default_view = _expect(
                    client.get(f"/v1/products/{asset['product_id']}/images", headers=headers),
                    200,
                )
                real_view = _expect(
                    client.get(
                        f"/v1/products/{asset['product_id']}/images?view=real",
                        headers=headers,
                    ),
                    200,
                )
                foreign_status = client.get(
                    f"/v1/image-outputs/{created['id']}",
                    headers={"X-Customer-Id": str(FOREIGN_CUSTOMER)},
                ).status_code
                if (
                    checked["status"] != "awaiting_customer_review"
                    or reviewed["customer_approved"] is not True
                    or default_view["resolved_view"] != "style_image"
                    or real_view["resolved_view"] != "real_photo"
                    or foreign_status != 404
                ):
                    raise RuntimeError("review/association/ownership acceptance failed")
                steps.extend(
                    [
                        "authenticity_rules_completed",
                        "candidate_human_approved",
                        "original_and_style_asset_association_created",
                        "storefront_defaults_to_style_and_can_switch_to_original",
                        "foreign_customer_concealed",
                    ]
                )

            with TestClient(app) as refreshed_client:
                persisted = _expect(
                    refreshed_client.get(
                        f"/v1/image-outputs/{created['id']}", headers=headers
                    ),
                    200,
                )
                if persisted["id"] != created["id"]:
                    raise RuntimeError("fresh-client persisted state failed")
                steps.append("fresh_client_refresh_preserved_state")

        with sessions() as session:
            repository = ProductImageRepository(session)
            job = repository.get_output_job(CUSTOMER, UUID(str(created["id"])))
            if job is None or job.subject_seal_id is None:
                raise RuntimeError("persisted job/seal missing")
            seal = repository.get_subject_seal(CUSTOMER, job.subject_seal_id)
            candidates = repository.list_candidates(CUSTOMER, job.id)
            if seal is None or not candidates:
                raise RuntimeError("persisted seal/candidates missing")
            tamper_code = _tamper_attestation(
                candidate_content.content, candidates[0], seal, store
            )
            job_count = int(
                session.scalar(select(func.count()).select_from(ImageOutputJob)) or 0
            )
        if tamper_code != "identity_lock_pixel_mismatch":
            raise RuntimeError(f"unexpected tamper result: {tamper_code}")
        steps.append("isolated_one_pixel_tamper_rejected")

        report = {
            "acceptance_batch_id": evidence_root.name,
            "recorded_at": datetime.now(UTC).isoformat(),
            "status": "passed",
            "evidence_level": "local_deterministic_image_acceptance",
            "segmentation_adapter": "deterministic-local-subject-v1",
            "real_photo_segmentation_success": False,
            "fixture_fallback": "explicit_alpha_synthetic_fixture",
            "synthetic_test_only": True,
            "publishable": False,
            "network_calls": len(network_attempts),
            "real_model_calls": 0,
            "fees_usd": 0,
            "job_id": created["id"],
            "subject_seal_id": created["subject_seal_id"],
            "candidate_count": len(created["candidates"]),
            "candidate_sha256": [
                item["content_sha256"] for item in created["candidates"]
            ],
            "original_sha256": sha256(fixture).hexdigest(),
            "job_count_after_replay": job_count,
            "tamper_rejection_code": tamper_code,
            "steps_passed": steps,
            "database_path": str(database),
            "media_root": str(media_root),
            "page_path": "/local/style-suite",
        }
        report_path = evidence_root / "identity-locked-acceptance.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(json.dumps({**report, "report_path": str(report_path)}, indent=2))
        return 0
    finally:
        store.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
