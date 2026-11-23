"""Zero-network checks; these are NOT cloud PG/COS/container acceptance."""

from __future__ import annotations

import io
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from alembic import command
from opc_style.db import create_database_engine
from opc_style.domain.identity_background_generator import (
    BackgroundGenerationRequest,
    DeterministicBackgroundSafetyChecker,
    DeterministicLocalBackgroundGenerator,
)
from opc_style.domain.image_normalizer import NormalizedPayload
from opc_style.main import create_app
from opc_style.models import MallStyleGenerationQueueItem
from opc_style.production_runtime import (
    DeferredProductionStore,
    _refresh_tke_oidc_credentials,
    build_production_store,
    readiness,
)
from opc_style.production_settings import ProductionSettings
from opc_style.services.mall_style_executor import (
    MallStyleExecutor,
    ManualProductionBackgroundSafetyChecker,
)
from opc_style.services.real_photos import (
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.s3_asset_store import S3AssetStore

ROOT = Path(__file__).resolve().parents[1]


class ConditionalFailure(Exception):
    response: ClassVar[dict[str, Any]] = {"Error": {"Code": "PreconditionFailed"}}


class TencentConditionalFailure(Exception):
    response: ClassVar[dict[str, Any]] = {"Error": {"Code": "FileAlreadyExists"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_writes = False

    def get_object(self, **kwargs: Any) -> Any:
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def put_object(self, **kwargs: Any) -> Any:
        if self.fail_writes:
            raise OSError("network_down")
        assert kwargs["IfNoneMatch"] == "*"
        assert "ACL" not in kwargs
        key = kwargs["Key"]
        if key in self.objects:
            raise ConditionalFailure
        self.objects[key] = kwargs["Body"]
        return {}

    def delete_object(self, **kwargs: Any) -> Any:
        self.objects.pop(kwargs["Key"], None)
        return {}

    def head_bucket(self, **kwargs: Any) -> Any:
        return {}


def store(client: FakeS3) -> S3AssetStore:
    return S3AssetStore(
        client, bucket="private-test", prefix="style-v1", capability_secret=b"x" * 32
    )


@pytest.mark.parametrize(
    "artifact_kind",
    ["product_foreground", "product_mask", "pixel_background", "pixel_composition"],
)
def test_merged_artifact_store_keeps_tenant_digest_and_restart_boundaries(
    artifact_kind: str,
) -> None:
    client, tenant, other, artifact = FakeS3(), uuid4(), uuid4(), uuid4()
    first = store(client)
    content = b"private-unified-artifact"
    digest = sha256(content).hexdigest()
    persist = getattr(first, f"persist_{artifact_kind}")
    persist(tenant, artifact, content, digest)
    persist(tenant, artifact, content, digest)
    first.close()
    reopened = store(client)
    read = getattr(reopened, f"read_{artifact_kind}")
    verify = getattr(reopened, f"verify_{artifact_kind}")
    assert read(tenant, artifact, digest) == content
    assert verify(tenant, artifact, digest)
    assert not verify(other, artifact, digest)
    assert not verify(tenant, artifact, "0" * 64)
    with pytest.raises(AssetContentStoreError):
        read(other, artifact, digest)
    changed = b"changed-product-artifact"
    with pytest.raises(AssetContentStoreError):
        getattr(reopened, f"persist_{artifact_kind}")(
            tenant, artifact, changed, sha256(changed).hexdigest()
        )
    assert read(tenant, artifact, digest) == content


def test_s3_restart_tenant_digest_and_immutable_replay() -> None:
    client, tenant, other, asset = FakeS3(), uuid4(), uuid4(), uuid4()
    first = store(client)
    payload, digest = (
        b"original-product-photo",
        sha256(b"original-product-photo").hexdigest(),
    )
    first.persist_original(tenant, asset, payload, digest)
    first.persist_original(tenant, asset, payload, digest)
    first.close()
    second = store(client)
    assert second.read_original(tenant, asset, digest) == payload
    assert any(str(tenant) in key and digest in key for key in client.objects)
    with pytest.raises(AssetContentStoreError):
        second.read_original(other, asset, digest)
    with pytest.raises(AssetContentStoreError):
        second.persist_original(
            tenant, asset, b"different", sha256(b"different").hexdigest()
        )
    assert second.read_original(tenant, asset, digest) == payload
    with pytest.raises(AssetContentStoreError):
        second.read_original(tenant, asset, "f" * 64)


def test_s3_samples_masks_candidates_restart_and_cleanup_fails_closed() -> None:
    client, tenant, identity, job = FakeS3(), uuid4(), uuid4(), uuid4()
    first = store(client)
    content, digest = b"private-bytes", sha256(b"private-bytes").hexdigest()
    first.register_style_sample_content(tenant, identity, content, digest)
    for kind in ("rgba", "core-mask"):
        first.persist_subject_artifact(tenant, identity, kind, content, digest)
    target = first.reserve_derivative(tenant, job, 1, extension="png")
    first.create_derivative(
        target, NormalizedPayload(content, digest, "image/png", 12, 9, {})
    )
    cap = first.issue_cleanup_capability(tenant, job, (1, 2, 3))
    second = store(client)
    assert second.verify_style_sample_content(tenant, identity, digest)
    assert (
        second.read_subject_artifact(tenant, identity, "core-mask", digest) == content
    )
    assert second.read_derivative(tenant, job, 1, digest) == content
    assert second.verify_cleanup_capability(tenant, job, (1, 2, 3), cap)
    assert not second.verify_cleanup_capability(uuid4(), job, (1, 2, 3), cap)
    assert second.cleanup_scoped_derivatives(tenant, job, cap) == (
        "private_object_cleanup_requires_reconciliation",
    )
    assert second.read_derivative(tenant, job, 1, digest) == content
    first.persist_original(tenant, identity, content, digest)
    with pytest.raises(AssetContentStoreError, match="reconciliation"):
        first.discard_uncommitted_asset(tenant, "originals", identity, digest)
    assert second.read_original(tenant, identity, digest) == content
    assert any(key.endswith("cleanup-pending.json") for key in client.objects)


def test_s3_write_error_not_treated_as_duplicate() -> None:
    client = FakeS3()
    client.fail_writes = True
    with pytest.raises(AssetContentStoreError, match="write_failed"):
        store(client).persist_original(uuid4(), uuid4(), b"x", sha256(b"x").hexdigest())


def test_pg_baseline_offline_uses_postgres_guards_and_partial_indices(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic-postgresql.ini"), output_buffer=output)
    config.set_main_option("script_location", str(ROOT / "alembic_postgresql"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://invalid/unused")
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE FUNCTION style_reject_history_mutation" in sql
    assert "guard_audit_events" in sql
    assert "publishable = false" in sql
    assert "WHERE kind = 'main' AND active IS true" in sql
    assert "WHERE active IS true" in sql
    assert "style_pg_alembic_version" in sql
    assert "mall_style_executions" in sql
    assert "RAISE(ABORT" not in sql and "PRAGMA" not in sql
    config.set_main_option("sqlalchemy.url", "sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="postgresql_required"):
        command.upgrade(config, "head", sql=True)


def test_postgres_fixed_driver() -> None:
    engine = create_database_engine(
        "postgresql://user:unused@127.0.0.1:1/never-connect"
    )
    assert engine.dialect.driver == "psycopg"
    engine.dispose()


def test_production_store_forwards_temporary_s3_session_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeEvents:
        def register(self, event: str, callback: Any) -> None:
            captured["registered_event"] = event
            params: dict[str, Any] = {"headers": {"If-None-Match": "*"}}
            callback(params=params)
            captured["registered_headers"] = params["headers"]

    class FakeMeta:
        events = FakeEvents()

    class FakeClient(FakeS3):
        meta = FakeMeta()

    class FakeBoto3:
        @staticmethod
        def client(_service: str, **kwargs: Any) -> FakeS3:
            captured.update(kwargs)
            return FakeClient()

    class FakeConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured["botocore_config"] = kwargs

    def fake_import(name: str) -> Any:
        if name == "boto3":
            return FakeBoto3
        if name == "botocore.config":
            return type("BotocoreConfig", (), {"Config": FakeConfig})
        raise AssertionError(name)

    monkeypatch.setattr(
        "opc_style.production_runtime.importlib.import_module", fake_import
    )
    settings = ProductionSettings(
        source_sha="a" * 40,
        asset_backend="s3",
        s3_endpoint="https://cos.ap-guangzhou.myqcloud.com",
        s3_region="ap-guangzhou",
        s3_bucket="private-test",
        s3_access_key="temporary-id",
        s3_secret_key="temporary-key",
        s3_session_token="temporary-token",
        storage_capability_secret="x" * 32,
    )
    assert build_production_store(settings) is not None
    assert captured["aws_session_token"] == "temporary-token"
    assert captured["botocore_config"]["s3"] == {"addressing_style": "virtual"}
    assert captured["registered_event"] == "before-call.s3.PutObject"
    assert captured["registered_headers"] == {"x-cos-forbid-overwrite": "true"}


def test_tke_oidc_storage_settings_require_injected_identity_and_no_static_keys() -> (
    None
):
    common = {
        "source_sha": "a" * 40,
        "asset_backend": "s3",
        "s3_endpoint": "https://cos.ap-guangzhou.myqcloud.com",
        "s3_region": "ap-guangzhou",
        "s3_bucket": "private-test",
        "s3_auth_mode": "tke_oidc",
        "storage_capability_secret": "x" * 32,
    }
    identity = {
        "tke_region": "ap-guangzhou",
        "tke_provider_id": "cls-example",
        "tke_role_arn": "qcs::cam::uin/100000000000:roleName/runtime",
        "tke_web_identity_token_file": "/var/run/secrets/tke/token",
    }
    assert ProductionSettings(**common, **identity).s3_auth_mode == "tke_oidc"
    with pytest.raises(ValueError, match="tke_oidc_environment_required"):
        ProductionSettings(**common)
    with pytest.raises(ValueError, match="oidc_rejects_static_credentials"):
        ProductionSettings(**common, **identity, s3_access_key="long-lived-key")


def test_tke_oidc_credential_exchange_returns_refreshable_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCredential:
        endpoint = "sts.tencentcloudapi.com"

        @staticmethod
        def get_credential_info() -> tuple[str, str, str]:
            return "temporary-id", "temporary-key", "temporary-token"

    class FakeProvider:
        credential = FakeCredential()

        @staticmethod
        def get_credential() -> FakeCredential:
            return FakeProvider.credential

    def fake_import(name: str) -> Any:
        assert name == "tencentcloud.common.credential"
        return type(
            "CredentialModule", (), {"DefaultTkeOIDCRoleArnProvider": FakeProvider}
        )

    monkeypatch.setattr(
        "opc_style.production_runtime.importlib.import_module", fake_import
    )
    metadata = _refresh_tke_oidc_credentials()
    assert metadata["access_key"] == "temporary-id"
    assert metadata["secret_key"] == "temporary-key"
    assert metadata["token"] == "temporary-token"
    assert datetime.fromisoformat(metadata["expiry_time"]) > datetime.now(UTC)
    assert FakeProvider.credential.endpoint == "sts.internal.tencentcloudapi.com"


def test_tencent_cos_duplicate_code_is_treated_as_conditional_failure() -> None:
    class TencentCosFake(FakeS3):
        def put_object(self, **kwargs: Any) -> Any:
            key = kwargs["Key"]
            if key in self.objects:
                raise TencentConditionalFailure
            self.objects[key] = kwargs["Body"]
            return {}

    client = TencentCosFake()
    first = store(client)
    first.validate_backend()
    first.probe()


def test_production_hides_workbench_and_ready_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("STYLE_WAREHOUSE_V3_ENABLED", "false")
    monkeypatch.setenv("STYLE_ASSET_BACKEND", "disabled")
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["sourceSha"] == "a" * 40
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["probes"]["storage"] == "private_storage_unconfigured"
        for path in (
            "/v1/styles",
            "/docs",
            "/openapi.json",
            "/v1/assets",
            "/style-suite",
        ):
            assert client.get(path).status_code == 404


def test_production_health_binds_before_private_storage_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("STYLE_WAREHOUSE_V3_ENABLED", "false")
    monkeypatch.setenv("STYLE_ASSET_BACKEND", "s3")
    monkeypatch.setenv("STYLE_S3_ENDPOINT", "https://cos.ap-shanghai.myqcloud.com")
    monkeypatch.setenv("STYLE_S3_REGION", "ap-shanghai")
    monkeypatch.setenv("STYLE_S3_BUCKET", "style-private")
    monkeypatch.setenv("STYLE_S3_ACCESS_KEY", "temporary-id")
    monkeypatch.setenv("STYLE_S3_SECRET_KEY", "temporary-key")
    monkeypatch.setenv("STYLE_STORAGE_CAPABILITY_SECRET", "x" * 32)
    opened = 0

    class DeliberatelyUnavailableStore:
        def probe(self) -> None:
            raise AssetContentStoreError("private_storage_unavailable")

        def close(self) -> None:
            return None

    def build(_settings: ProductionSettings) -> DeliberatelyUnavailableStore:
        nonlocal opened
        opened += 1
        return DeliberatelyUnavailableStore()

    monkeypatch.setattr("opc_style.main.build_production_store", build)
    with TestClient(create_app()) as client:
        assert opened == 0
        assert client.get("/health").status_code == 200
        assert opened == 0
        ready = client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["probes"]["storage"] == "private_storage_unavailable"
        assert opened == 1


def test_production_mall_intake_does_not_require_unmounted_classification_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("STYLE_WAREHOUSE_V3_ENABLED", "true")
    monkeypatch.setenv("STYLE_WAREHOUSE_SIGNING_SECRET", "x" * 32)
    monkeypatch.setenv("STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES", "true")
    monkeypatch.delenv("CLASSIFICATION_MEDIA_BASE_URL", raising=False)
    monkeypatch.delenv("CLASSIFICATION_STYLE_MEDIA_SECRET", raising=False)
    monkeypatch.setenv("STYLE_ASSET_BACKEND", "disabled")

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        settings = client.app.state.style_warehouse_v3_settings
        assert settings.enabled is True
        assert settings.verify_source_images is False


def test_deferred_production_store_initializes_once_and_closes() -> None:
    built: list[FakeS3] = []

    def build() -> S3AssetStore:
        client = FakeS3()
        built.append(client)
        result = store(client)
        result.validate_backend()
        return result

    deferred = DeferredProductionStore(build)
    assert built == []
    deferred.probe()
    deferred.probe()
    assert len(built) == 1
    deferred.close()


def test_ready_rejects_sqlite_even_with_storage() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    ok, body = readiness(
        engine,
        store(FakeS3()),
        ProductionSettings(source_sha="a" * 40),
        integration_enabled=True,
    )
    assert not ok
    assert body["probes"]["database"] == "production_postgresql_required"


def test_provider_default_disabled_no_factory_no_db(tmp_path: Path) -> None:
    engine = create_database_engine("sqlite:///:memory:")
    # No schema: disabled executor must not even attempt a queue read.
    with LocalSyntheticAssetStore(tmp_path / "media") as media:
        executor = MallStyleExecutor(
            sessionmaker(bind=engine), media, ProductionSettings()
        )
        assert executor.run_once() == "provider_disabled"


def test_fixture_safety_cannot_pass_real_background() -> None:
    from dataclasses import replace

    background = DeterministicLocalBackgroundGenerator().generate(
        BackgroundGenerationRequest({}, {}, "4:5")
    )[0]
    real = replace(background, synthetic_test_only=False)
    assert DeterministicBackgroundSafetyChecker().check(real).safe is False
    assert ManualProductionBackgroundSafetyChecker().check(real).safe is None
    assert ManualProductionBackgroundSafetyChecker().check(background).safe is False


def test_container_is_non_root_and_locked() -> None:
    docker = (ROOT / "Dockerfile").read_text()
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert "USER 10001:10001" in docker
    assert "uv sync --frozen --no-dev" in docker
    assert '"opc_style.production_bootstrap"' in docker
    assert "/usr/local/bin/api" in docker
    assert "exec python -m opc_style.production_bootstrap" in docker
    assert ".env*" in ignored
    assert {"交付文档", "待验收素材", "dist", "tools"}.issubset(ignored)


def test_production_smoke_scripts_are_fail_closed_and_target_scoped() -> None:
    postgres_smoke = ROOT / "scripts" / "run_production_container_smoke.sh"
    s3_smoke = ROOT / "scripts" / "run_s3_contract_smoke.sh"
    for script in (postgres_smoke, s3_smoke):
        source = script.read_text()
        assert "set -euo pipefail" in source
        subprocess.run(["bash", "-n", str(script)], check=True)

    postgres_source = postgres_smoke.read_text()
    assert "trap cleanup EXIT" in postgres_source
    assert "STYLE_WAREHOUSE_SIGNING_SECRET" in postgres_source
    assert "private_storage_unconfigured" not in postgres_source
    assert "pg_isready -U postgres -d style_prod" not in postgres_source
    assert (
        "psql -v ON_ERROR_STOP=1 -U postgres -d style_prod -c 'SELECT 1'"
        in postgres_source
    )
    assert postgres_source.count("run --rm --no-healthcheck --network") == 3

    s3_source = s3_smoke.read_text()
    assert "STYLE_S3_ENDPOINT" in s3_source
    assert "https_endpoint_required" in s3_source
    assert "localstack" not in s3_source.lower()
    missing = subprocess.run(
        ["bash", str(s3_smoke)],
        env={},
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    assert missing.stderr.startswith("missing_required_environment:")


def test_configuration_rejects_unsafe_storage_and_unbounded_generation() -> None:
    with pytest.raises(ValueError, match="https_endpoint_required"):
        ProductionSettings(asset_backend="s3", s3_endpoint="http://insecure")
    with pytest.raises(ValueError, match="budget_required"):
        ProductionSettings(
            executor_enabled=True, provider="openai", model="gpt-image-2"
        )


@pytest.fixture
def approved_request(tmp_path: Path):
    """Full signed ingress + human mask confirmation; no provider network."""
    import json
    from uuid import UUID

    from PIL import Image
    from sqlalchemy import update
    from test_mall_style_integration_v1_api import (
        MERCHANT,
        _approval_body,
        _post_handoff_for_imported_product,
        _subject_confirmation_headers,
        import_headers,
        import_path,
        receiver,
        seed_confirmed_style,
        signed_headers,
    )

    from opc_style.contracts.mall_style_handoff_v1 import (
        MallStyleGenerationApprovalV1,
        canonical_mall_generation_approval_sha256,
    )
    from opc_style.models import (
        StyleCardSampleLink,
        StyleCardVersion,
        StyleSample,
        StyleTemplateSlot,
    )

    source = Image.new("RGBA", (12, 9), (0, 0, 0, 0))
    mask = Image.new("L", (12, 9), 0)
    for y in range(2, 7):
        for x in range(3, 9):
            source.putpixel((x, y), (10, 20, 30, 255))
            mask.putpixel((x, y), 255)
    buffer, mask_buffer = io.BytesIO(), io.BytesIO()
    source.save(buffer, format="PNG")
    mask.save(mask_buffer, format="PNG")
    content, mask_bytes = buffer.getvalue(), mask_buffer.getvalue()
    digest, source_id = sha256(content).hexdigest(), uuid4()
    with receiver(tmp_path, manual_subject=True) as (client, sessions, media):
        with sessions() as session:
            scene, version, slot = seed_confirmed_style(
                session, name="模拟生产审核风格", default_slot="hero"
            )
            # Update only synthetic test rows to establish the real-readiness
            # contract without a paid style analyzer or private user data.
            observations = {
                **version.content,
                "tone": "quiet",
                "palette": ["warm"],
                "lighting": "soft daylight",
            }
            slot_rules = {"aspect_ratio": "4:5", "background": ["white"]}
            observations["slots"][0]["rules"] = slot_rules
            session.execute(
                update(StyleCardVersion)
                .where(StyleCardVersion.id == version.id)
                .values(content=observations)
            )
            session.execute(
                update(StyleTemplateSlot)
                .where(StyleTemplateSlot.id == slot.id)
                .values(rules=slot_rules)
            )
            first_sample = session.scalar(select(StyleSample))
            assert first_sample
            media.persist_style_sample_content(
                MERCHANT, first_sample.id, content, digest
            )
            session.execute(
                update(StyleSample)
                .where(StyleSample.id == first_sample.id)
                .values(
                    content_sha256=digest, asset_store_namespace=media.store_namespace
                )
            )
            for _ in range(9):
                sample = StyleSample(
                    customer_id=MERCHANT,
                    photo_id=f"evidence-{uuid4()}",
                    authorized_at=datetime.now(UTC),
                    consent_provenance="mock-explicit-consent",
                    consent_text_version="v1",
                    content_sha256=digest,
                    mime_type="image/png",
                    width=12,
                    height=9,
                    asset_store_namespace=media.store_namespace,
                )
                session.add(sample)
                session.flush()
                media.persist_style_sample_content(MERCHANT, sample.id, content, digest)
                session.add(
                    StyleCardSampleLink(
                        customer_id=MERCHANT,
                        style_card_version_id=version.id,
                        style_sample_id=sample.id,
                    )
                )
            session.commit()
        path = import_path(source_id)
        response = client.post(
            path, content=content, headers=import_headers(content, path=path)
        )
        assert response.status_code == 201, response.text
        asset_id = UUID(response.json()["style_product_asset_id"])
        handoff = _post_handoff_for_imported_product(
            client,
            scene=scene,
            version=version,
            source_id=source_id,
            asset_id=asset_id,
            content_sha256=digest,
        )
        receipt = UUID(handoff["receipt_id"])
        proposal_path = f"/internal/mall-style/v1/products/SKU-001/receipts/{receipt}/subject-proposals"
        proposal = client.post(
            proposal_path,
            headers=signed_headers(b"", path=proposal_path, method="POST"),
        )
        assert proposal.status_code == 200, proposal.text
        base = UUID(proposal.json()["base_subject_seal_id"])
        path = f"{proposal_path}/{base}/confirm"
        response = client.post(
            path,
            content=mask_bytes,
            headers=_subject_confirmation_headers(
                mask_bytes,
                path=path,
                receipt_id=receipt,
                asset_sha256=digest,
                base_seal_id=base,
                base_mask_sha256=proposal.json()["base_mask_sha256"],
            ),
        )
        assert response.status_code == 201, response.text
        unsealed = MallStyleGenerationApprovalV1.model_validate_json(
            _approval_body(receipt)
        ).model_copy(update={"provider": "openai", "model": "gpt-image-2"})
        raw = unsealed.model_dump(mode="json")
        raw["approval_payload_sha256"] = canonical_mall_generation_approval_sha256(
            unsealed
        )
        approved = MallStyleGenerationApprovalV1.model_validate(raw)
        body = json.dumps(
            approved.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        path = f"/internal/mall-style/v1/receipts/{receipt}/generation-approvals"
        response = client.post(
            path, content=body, headers=signed_headers(body, path=path, method="POST")
        )
        assert response.status_code == 201, response.text
        yield sessions, media, receipt


class FakeProductionGenerator:
    """Simulated real-provider boundary; NEVER contacts a provider."""

    synthetic_test_only = False

    def __init__(self, *, uncertain: bool = False) -> None:
        self.calls = 0
        self.uncertain = uncertain

    def generate(self, request):
        from dataclasses import replace

        from opc_style.domain.identity_background_generator import (
            BackgroundGenerationError,
        )

        self.calls += 1
        if self.uncertain:
            raise BackgroundGenerationError(
                "background_generation_uncertain", outbound_attempts=1
            )
        return [
            replace(item, synthetic_test_only=False, metadata={"mock_provider": True})
            for item in DeterministicLocalBackgroundGenerator().generate(request)
        ]


def enabled_settings() -> ProductionSettings:
    return ProductionSettings(
        executor_enabled=True,
        provider="openai",
        model="gpt-image-2",
        maximum_cost_usd=Decimal("0.36"),
        estimated_cost_usd=Decimal("0.30"),
    )


def test_executor_complete_lineage_candidates_and_no_second_dispatch(
    approved_request,
) -> None:
    from test_mall_style_integration_v1_api import MERCHANT, OPC

    from opc_style.models import ImageCandidate, ImageOutputJob, MallStyleExecution
    from opc_style.services.mall_style_results_v1 import MallStyleResultService

    sessions, media, receipt = approved_request
    generator = FakeProductionGenerator()
    worker = MallStyleExecutor(sessions, media, enabled_settings(), generator=generator)
    assert worker.run_once() == "completed"
    assert worker.run_once() == "idle"
    assert generator.calls == 1
    with sessions() as session:
        execution = session.scalar(select(MallStyleExecution))
        queue = session.scalar(select(MallStyleGenerationQueueItem))
        assert execution and queue
        assert execution.outbound_attempts == 1
        job = session.get(ImageOutputJob, queue.generation_job_id)
        assert job and job.request_metadata["mall_generation_request_id"] == str(
            queue.id
        )
        candidates = session.scalars(select(ImageCandidate)).all()
        assert len(candidates) == 3
        assert all(
            row.artifact_metadata["background_review_state"] == "pending_human_review"
            for row in candidates
        )
        result = MallStyleResultService(session).by_receipt(
            merchant_id=MERCHANT, opc_id=OPC, product_id="SKU-001", receipt_id=receipt
        )
        assert len(result.candidates) == 3
        assert all(
            row.publishable is False and row.approval_state == "pending_review"
            for row in result.candidates
        )


def test_executor_network_uncertain_is_never_retried_after_restart(
    approved_request,
) -> None:
    from opc_style.models import MallStyleExecution

    sessions, media, _ = approved_request
    generator = FakeProductionGenerator(uncertain=True)
    worker = MallStyleExecutor(sessions, media, enabled_settings(), generator=generator)
    assert worker.run_once() == "uncertain"
    restarted = MallStyleExecutor(
        sessions, media, enabled_settings(), generator=generator
    )
    assert restarted.run_once() == "idle"
    assert generator.calls == 1
    with sessions() as session:
        assert session.scalar(select(MallStyleExecution)).outbound_attempts == 1


@pytest.mark.parametrize("gate", ["expiry", "rights", "model", "cost", "sample"])
def test_executor_rechecks_gates_before_any_dispatch(approved_request, gate) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from opc_style.models import ProductAssetPermissionEvent, StyleSample

    sessions, media, _ = approved_request
    with sessions() as session:
        queue = session.scalar(select(MallStyleGenerationQueueItem))
        assert queue
        if gate == "expiry":
            queue.approval_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        elif gate == "rights":
            session.add(
                ProductAssetPermissionEvent(
                    customer_id=queue.merchant_id,
                    product_asset_id=queue.product_asset_id,
                    transform_permission=False,
                    occurred_at=datetime.now(UTC),
                    provenance="explicit-test-revocation",
                )
            )
        elif gate == "model":
            queue.model = "wrong-model"
        elif gate == "cost":
            queue.maximum_cost_usd = Decimal("0.01")
        else:
            sample = session.scalar(select(StyleSample))
            session.execute(
                update(StyleSample)
                .where(StyleSample.id == sample.id)
                .values(asset_store_namespace="wrong-store")
            )
        session.commit()
    generator = FakeProductionGenerator()
    worker = MallStyleExecutor(sessions, media, enabled_settings(), generator=generator)
    assert worker.run_once() != "completed"
    assert generator.calls == 0
    with sessions() as session:
        from opc_style.models import MallStyleExecution

        assert session.scalar(select(MallStyleExecution)).outbound_attempts == 0


def test_executor_lease_race_and_reserved_crash_fence(approved_request) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from opc_style.models import MallStyleExecution

    sessions, media, _ = approved_request
    worker = MallStyleExecutor(
        sessions, media, enabled_settings(), generator=FakeProductionGenerator()
    )
    with sessions() as one, sessions() as two:
        queue1 = one.scalar(select(MallStyleGenerationQueueItem))
        queue2 = two.scalar(select(MallStyleGenerationQueueItem))
        claim = worker._claim(one, queue1)
        assert claim is not None
        assert worker._claim(two, queue2) is None
        # Simulate process dying after the durable pre-network reservation.
        one.execute(
            update(MallStyleExecution)
            .where(MallStyleExecution.id == claim.id)
            .values(
                outbound_attempts=1,
                state="reserved",
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        one.commit()
    assert worker.reconcile_expired_reservations() == 1
    assert worker.run_once() == "idle"
    with sessions() as session:
        fence = session.scalar(select(MallStyleExecution))
        assert fence.state == "uncertain" and fence.outbound_attempts == 1


def test_storage_deployment_probe_installs_durable_readiness_canary() -> None:
    client = FakeS3()
    first = store(client)
    with pytest.raises(AssetContentStoreError, match="probe_failed"):
        first.probe()
    first.validate_backend()
    first.probe()
    assert all("/probes/backend-validated.json" in key for key in client.objects)
    store(client).probe()


def test_storage_probe_rejects_backend_that_silently_overwrites() -> None:
    class UnsafeOverwriteS3(FakeS3):
        def put_object(self, **kwargs: Any) -> Any:
            self.objects[kwargs["Key"]] = kwargs["Body"]
            return {}

    client = UnsafeOverwriteS3()
    with pytest.raises(
        AssetContentStoreError, match="conditional_object_creation_unsupported"
    ):
        store(client).validate_backend()
    assert not any("backend-validated.json" in key for key in client.objects)


def test_executor_shutdown_releases_only_unreserved_owned_claim(
    approved_request,
) -> None:
    from threading import Event

    from opc_style.models import MallStyleExecution

    sessions, media, _ = approved_request
    stop = Event()
    generator = FakeProductionGenerator()
    worker = MallStyleExecutor(
        sessions, media, enabled_settings(), generator=generator, stop_event=stop
    )
    with sessions() as session:
        queue = session.scalar(select(MallStyleGenerationQueueItem))
        claim = worker._claim(session, queue)
        assert claim is not None
        stop.set()
        assert worker._execute(session, queue, claim) == "stopping"
    worker.release_unreserved_leases()
    assert worker.run_once() == "stopping"
    assert generator.calls == 0
    with sessions() as session:
        released = session.scalar(select(MallStyleExecution))
        assert released.state == "blocked" and released.outbound_attempts == 0
    restarted = MallStyleExecutor(
        sessions, media, enabled_settings(), generator=generator
    )
    assert restarted.run_once() == "completed"
    assert generator.calls == 1


def test_executor_shutdown_never_releases_reserved_fence(approved_request) -> None:
    from sqlalchemy import update

    from opc_style.models import MallStyleExecution

    sessions, media, _ = approved_request
    worker = MallStyleExecutor(
        sessions, media, enabled_settings(), generator=FakeProductionGenerator()
    )
    with sessions() as session:
        queue = session.scalar(select(MallStyleGenerationQueueItem))
        claim = worker._claim(session, queue)
        assert claim is not None
        session.execute(
            update(MallStyleExecution)
            .where(MallStyleExecution.id == claim.id)
            .values(state="reserved", outbound_attempts=1)
        )
        session.commit()
    worker.release_unreserved_leases()
    with sessions() as session:
        fence = session.scalar(select(MallStyleExecution))
        assert fence.state == "reserved" and fence.outbound_attempts == 1
    assert worker.run_once() == "idle"
