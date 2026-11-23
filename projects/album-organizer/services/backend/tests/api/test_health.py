from fastapi.testclient import TestClient

from photo_organizer.api.main import create_app
from photo_organizer.config import Settings
from photo_organizer.storage.s3_storage import S3Storage


async def passing_probe() -> bool:
    return True


async def failing_probe() -> bool:
    return False


def test_live_health_is_process_only() -> None:
    client = TestClient(create_app(readiness_probe=passing_probe))
    assert client.get("/health/live").json() == {"status": "ok"}


def test_ready_returns_ready_when_dependency_probe_succeeds() -> None:
    client = TestClient(create_app(readiness_probe=passing_probe))
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_ready_returns_503_when_dependency_probe_fails() -> None:
    client = TestClient(create_app(readiness_probe=failing_probe))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "dependencies_unavailable"


def test_production_app_wires_configured_s3_adapter_without_storage_override() -> None:
    """The application must use the real private S3 adapter outside test injection."""
    settings = Settings(
        app_env="test",
        s3_endpoint_url="http://storage.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="configured-private-bucket",
        s3_access_key_id="configured-access",
        s3_secret_access_key="configured-secret",
        jwt_signing_key="test-signing-key-with-more-than-thirty-two-bytes",
        otp_pepper="test-otp-pepper-with-more-than-thirty-two-bytes",
        otp_sender="tencent",
        tencent_secret_id="id",
        tencent_secret_key="secret",
        tencent_sms_sdk_app_id="app",
        tencent_sms_sign_name="sign",
        tencent_sms_template_id="template",
    )
    app = create_app(readiness_probe=passing_probe, settings_override=settings)

    storage = app.state.upload_service.storage
    assert isinstance(storage, S3Storage)
    assert storage._bucket == "configured-private-bucket"
