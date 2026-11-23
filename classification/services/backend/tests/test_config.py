from product_engine.config import Settings


def test_settings_load_required_dependencies_and_safe_default(monkeypatch) -> None:
    values = {
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/product_engine",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_REGION": "us-east-1",
        "S3_BUCKET": "product-engine-test",
        "S3_ACCESS_KEY_ID": "test-access",
        "S3_SECRET_ACCESS_KEY": "test-secret",
        "ALBUM_SIGNING_SECRET": "album-secret",
        "SALES_SIGNING_SECRET": "sales-secret",
        "STYLE_SIGNING_SECRET": "style-secret",
        "ADMIN_SIGNING_SECRET": "admin-secret",
        "SALES_WEBHOOK_URL": "http://localhost:9100/hooks/materials",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.database_url.endswith("/product_engine")
    assert settings.s3_bucket == "product-engine-test"
    assert settings.album_signing_secret.get_secret_value() == "album-secret"
    assert settings.sales_signing_secret.get_secret_value() == "sales-secret"
    assert settings.style_signing_secret.get_secret_value() == "style-secret"
    assert settings.admin_signing_secret.get_secret_value() == "admin-secret"
    assert str(settings.sales_webhook_url) == "http://localhost:9100/hooks/materials"
    assert settings.signature_tolerance_seconds == 300
