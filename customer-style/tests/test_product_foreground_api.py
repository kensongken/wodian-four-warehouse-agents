"""HTTP proof for local foreground preview, decision, and recovery."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import (
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundStageReceipt,
)
from opc_style.main import create_app
from opc_style.models import (
    Base,
    ProductAsset,
    ProductIdentityAnalysisRun,
    ProductIdentityCardVersion,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A = UUID("00000000-0000-0000-0000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-0000-0000-0000000000b2")


class FakeForegroundExtractor:
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        mask = Image.new("L", (request.width, request.height), 0)
        foreground = Image.new("RGBA", (request.width, request.height), (0, 0, 0, 0))
        box = (2, 2, request.width - 2, request.height - 2)
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                mask.putpixel((x, y), 255)
                foreground.putpixel((x, y), (18, 115, 80, 255))
        mask_output, foreground_output = BytesIO(), BytesIO()
        mask.save(mask_output, format="PNG")
        foreground.save(foreground_output, format="PNG")
        mask_bytes = mask_output.getvalue()
        foreground_bytes = foreground_output.getvalue()
        return ExtractedForeground(
            foreground_png=foreground_bytes,
            foreground_sha256=sha256(foreground_bytes).hexdigest(),
            mask_png=mask_bytes,
            mask_sha256=sha256(mask_bytes).hexdigest(),
            width=request.width,
            height=request.height,
            extractor_version="local-sam2-birefnet-v1:123456789abc",
            coarse_mask_png=mask_bytes,
            coarse_mask_sha256=sha256(mask_bytes).hexdigest(),
            stage_receipts=(
                ForegroundStageReceipt(
                    role="coarse_segmenter",
                    artifact_sha256="a" * 64,
                    output_sha256=sha256(mask_bytes).hexdigest(),
                    parameters_sha256="b" * 64,
                ),
                ForegroundStageReceipt(
                    role="alpha_refiner",
                    artifact_sha256="c" * 64,
                    output_sha256=sha256(mask_bytes).hexdigest(),
                    parameters_sha256="d" * 64,
                ),
            ),
            pipeline_manifest_sha256="e" * 64,
        )


def _foreground_result(
    request: ForegroundExtractionRequest,
    box: tuple[int, int, int, int],
) -> ExtractedForeground:
    baseline = FakeForegroundExtractor().extract(request)
    mask = Image.new("L", (request.width, request.height), 0)
    foreground = Image.new("RGBA", (request.width, request.height), (0, 0, 0, 0))
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            mask.putpixel((x, y), 255)
            foreground.putpixel((x, y), (18, 115, 80, 255))
    mask_output, foreground_output = BytesIO(), BytesIO()
    mask.save(mask_output, format="PNG")
    foreground.save(foreground_output, format="PNG")
    mask_bytes = mask_output.getvalue()
    foreground_bytes = foreground_output.getvalue()
    return ExtractedForeground(
        foreground_png=foreground_bytes,
        foreground_sha256=sha256(foreground_bytes).hexdigest(),
        mask_png=mask_bytes,
        mask_sha256=sha256(mask_bytes).hexdigest(),
        width=request.width,
        height=request.height,
        extractor_version=baseline.extractor_version,
        coarse_mask_png=mask_bytes,
        coarse_mask_sha256=sha256(mask_bytes).hexdigest(),
        stage_receipts=(
            ForegroundStageReceipt(
                role="coarse_segmenter",
                artifact_sha256="a" * 64,
                output_sha256=sha256(mask_bytes).hexdigest(),
                parameters_sha256="b" * 64,
            ),
            ForegroundStageReceipt(
                role="alpha_refiner",
                artifact_sha256="c" * 64,
                output_sha256=sha256(mask_bytes).hexdigest(),
                parameters_sha256="d" * 64,
            ),
        ),
        pipeline_manifest_sha256="e" * 64,
    )


class SmallSubjectForegroundExtractor(FakeForegroundExtractor):
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        return _foreground_result(
            request,
            (
                request.width * 7 // 20,
                request.height * 7 // 20,
                request.width * 13 // 20,
                request.height * 13 // 20,
            ),
        )


class EdgeTouchingForegroundExtractor(FakeForegroundExtractor):
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        return _foreground_result(
            request,
            (0, 2, request.width - 2, request.height - 2),
        )


class BusyForegroundExtractor:
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        del request
        raise DomainRuleError("foreground_candidate_busy")


def _headers(customer_id: UUID = CUSTOMER_A) -> dict[str, str]:
    return {"X-Customer-Id": str(customer_id)}


@contextmanager
def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    extractor: FakeForegroundExtractor | None = None,
) -> Generator[tuple[TestClient, sessionmaker[Session], Path], None, None]:
    workspace = tmp_path / f"workspace-{uuid4().hex}"
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(workspace))
    monkeypatch.setenv("FOREGROUND_EXTRACTOR_ENABLED", "false")
    engine = create_database_engine(f"sqlite:///{tmp_path / f'{uuid4().hex}.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    app = create_app()
    app.state.session_factory = sessions
    app.state.product_foreground_extractor = extractor or FakeForegroundExtractor()

    def session_override() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app) as client:
            yield client, sessions, workspace
    finally:
        engine.dispose()


def _upload(client: TestClient, customer_id: UUID = CUSTOMER_A) -> dict[str, object]:
    image = BytesIO()
    Image.new("RGB", (24, 18), (235, 230, 220)).save(image, format="PNG")
    token = uuid4().hex
    response = client.post(
        "/local/style-suite/api/product",
        headers={
            **_headers(customer_id),
            "X-Product-Id": f"foreground-product-{token}",
            "X-Photo-Id": f"foreground-photo-{token}",
            "Content-Type": "image/png",
        },
        content=image.getvalue(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_identity_card(
    sessions: sessionmaker[Session], asset_id: UUID, customer_id: UUID = CUSTOMER_A
) -> None:
    with sessions() as session:
        asset = session.scalar(
            select(ProductAsset).where(
                ProductAsset.id == asset_id, ProductAsset.customer_id == customer_id
            )
        )
        assert asset is not None
        run = ProductIdentityAnalysisRun(
            id=uuid4(),
            customer_id=customer_id,
            product_asset_id=asset.id,
            product_id=asset.product_id,
            source_content_sha256=asset.content_sha256,
            idempotency_key=f"seed-{uuid4()}",
            model="fake-identity-v1",
            authorized_usd=Decimal("0.01"),
            input_manifest_sha256="1" * 64,
            input_manifest={},
            dispatch_state="provider_responded",
            outbound_attempts=1,
            proposal={},
        )
        session.add(run)
        session.flush()
        session.add(
            ProductIdentityCardVersion(
                id=uuid4(),
                customer_id=customer_id,
                product_asset_id=asset.id,
                product_id=asset.product_id,
                analysis_run_id=run.id,
                version=1,
                source_content_sha256=asset.content_sha256,
                card_payload={},
                card_sha256="2" * 64,
                confirmed_by="local_customer",
            )
        )
        session.commit()


def _preview(client: TestClient, asset_id: str) -> dict[str, object]:
    response = client.post(
        f"/local/style-suite/api/products/{asset_id}/foreground-preview",
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _decide(
    client: TestClient,
    asset_id: str,
    preview: dict[str, object],
    *,
    decision: str = "confirmed",
    confirmed: bool = True,
    key: str = "foreground-confirm-1",
):
    return client.post(
        f"/local/style-suite/api/products/{asset_id}/foreground-versions",
        headers=_headers(),
        json={
            "version_id": preview["id"],
            "manifest_sha256": preview["manifest_sha256"],
            "confirmation_key": key,
            "decision": decision,
            "confirmed": confirmed,
        },
    )


def test_preview_confirm_replay_list_and_private_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, sessions, _):
        asset = _upload(client)
        _seed_identity_card(sessions, UUID(str(asset["id"])))
        preview = _preview(client, str(asset["id"]))
        foreground = client.get(preview["foreground_content_url"], headers=_headers())
        mask = client.get(preview["mask_content_url"], headers=_headers())
        disagreement = client.get(
            preview["disagreement_content_url"], headers=_headers()
        )
        confirmed = _decide(client, str(asset["id"]), preview)
        replay = _decide(client, str(asset["id"]), preview)
        versions = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(),
        )

    assert preview["state"] == "proposed"
    assert preview["publishable"] is False
    assert preview["disagreement_classification"] in {
        "coarse_only_detached_dominant",
        "coarse_only_boundary_dominant",
        "refined_only_dominant",
        "mixed_disagreement",
        "no_disagreement",
    }
    assert 0 <= preview["disagreement_ratio"] <= 1
    assert 0 <= preview["coarse_only_ratio"] <= 1
    assert 0 <= preview["refined_only_ratio"] <= 1
    assert 0 <= preview["coarse_only_detached_ratio"] <= 1
    assert foreground.status_code == 200
    assert foreground.headers["content-type"] == "image/png"
    assert mask.status_code == 200
    assert disagreement.status_code == 200
    assert disagreement.headers["content-type"] == "image/png"
    with Image.open(BytesIO(disagreement.content)) as disagreement_image:
        assert disagreement_image.mode == "RGBA"
        assert disagreement_image.size == (24, 18)
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["state"] == "confirmed"
    assert replay.status_code == 200, replay.text
    assert replay.json()["created"] is False
    assert versions.status_code == 200
    assert [item["state"] for item in versions.json()] == ["confirmed"]


def test_preview_reports_local_model_contention_as_retryable_busy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path, extractor=BusyForegroundExtractor()) as (
        client,
        _,
        _,
    ):
        asset = _upload(client)
        response = client.post(
            f"/local/style-suite/api/products/{asset['id']}/foreground-preview",
            headers=_headers(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "foreground_candidate_busy", "retryable": True}
    }


def test_preview_warns_when_subject_is_too_small_for_primary_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(
        monkeypatch, tmp_path, extractor=SmallSubjectForegroundExtractor()
    ) as (client, _, _):
        asset = _upload(client)
        preview = _preview(client, str(asset["id"]))
        versions = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(),
        )

    assert preview["quality_flags"] == ["subject_too_small_for_primary_image"]
    assert versions.status_code == 200
    assert versions.json()[0]["quality_flags"] == [
        "subject_too_small_for_primary_image"
    ]


def test_preview_warns_instead_of_rejecting_edge_touching_long_product(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(
        monkeypatch, tmp_path, extractor=EdgeTouchingForegroundExtractor()
    ) as (client, _, _):
        asset = _upload(client)
        preview = _preview(client, str(asset["id"]))
        versions = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(),
        )

    assert preview["state"] == "proposed"
    assert preview["quality_flags"] == ["touches_edge"]
    assert versions.status_code == 200
    assert versions.json()[0]["quality_flags"] == ["touches_edge"]


def test_confirmation_requires_exact_checkbox_and_confirmed_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, sessions, _):
        asset = _upload(client)
        preview = _preview(client, str(asset["id"]))
        unchecked = _decide(
            client, str(asset["id"]), preview, decision="confirmed", confirmed=False
        )
        missing_identity = _decide(client, str(asset["id"]), preview)
        _seed_identity_card(sessions, UUID(str(asset["id"])))
        accepted = _decide(client, str(asset["id"]), preview)

    assert unchecked.status_code == 409
    assert unchecked.json()["error"]["code"] == "foreground_decision_invalid"
    assert missing_identity.status_code == 409
    assert (
        missing_identity.json()["error"]["code"] == "foreground_identity_card_required"
    )
    assert accepted.status_code == 201


def test_rejected_preview_cannot_be_reused_as_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, sessions, _):
        asset = _upload(client)
        _seed_identity_card(sessions, UUID(str(asset["id"])))
        preview = _preview(client, str(asset["id"]))
        rejected = _decide(
            client,
            str(asset["id"]),
            preview,
            decision="rejected",
            confirmed=False,
            key="foreground-reject-1",
        )
        reused = _decide(client, str(asset["id"]), preview)

    assert rejected.status_code == 201
    assert rejected.json()["state"] == "rejected"
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "foreground_version_rejected"


@pytest.mark.parametrize("artifact_kind", ["product-masks", "originals"])
def test_source_or_mask_tamper_blocks_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, artifact_kind: str
) -> None:
    with _client(monkeypatch, tmp_path) as (client, sessions, workspace):
        asset = _upload(client)
        _seed_identity_card(sessions, UUID(str(asset["id"])))
        preview = _preview(client, str(asset["id"]))
        artifact_id = preview["id"] if artifact_kind == "product-masks" else asset["id"]
        target = (
            workspace
            / "media"
            / "customers"
            / str(CUSTOMER_A)
            / artifact_kind
            / f"{artifact_id}.bin"
        )
        target.write_bytes(b"tampered")
        response = _decide(client, str(asset["id"]), preview)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "foreground_manifest_mismatch"


def test_pipeline_evidence_survives_service_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, sessions, workspace):
        asset = _upload(client)
        _seed_identity_card(sessions, UUID(str(asset["id"])))
        preview = _preview(client, str(asset["id"]))
        foreign = client.get(
            preview["foreground_content_url"], headers=_headers(CUSTOMER_B)
        )

        current = client.app.state.classification_bridge_store
        current.close()
        client.app.state.classification_bridge_store = LocalSyntheticAssetStore(
            workspace / "media", reopen=True
        )
        recovered = client.get(preview["mask_content_url"], headers=_headers())
        versions = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(),
        )
        foreign_versions = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(CUSTOMER_B),
        )

    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "foreground_asset_unavailable"
    assert recovered.status_code == 200
    assert versions.status_code == 200
    assert versions.json()[0]["id"] == preview["id"]
    assert versions.json()[0]["pipeline_manifest_sha256"] == "e" * 64
    assert len(versions.json()[0]["stage_receipts_sha256"]) == 64
    assert foreign_versions.status_code == 409
    serialized = versions.text
    assert "/private/" not in serialized
    assert "model-cache" not in serialized
    assert "SECRET" not in serialized


def test_cross_customer_cannot_read_pipeline_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _client(monkeypatch, tmp_path) as (client, _, _workspace):
        asset = _upload(client)
        _preview(client, str(asset["id"]))
        response = client.get(
            f"/local/style-suite/api/products/{asset['id']}/foreground-versions",
            headers=_headers(CUSTOMER_B),
        )

    assert response.status_code == 409
    assert "pipeline_manifest_sha256" not in response.text
    assert "stage_receipts_sha256" not in response.text
