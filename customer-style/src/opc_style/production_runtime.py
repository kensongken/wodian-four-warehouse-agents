"""Production storage factory and safe readiness probes."""

import importlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlparse

from sqlalchemy import Engine, text

from opc_style.production_settings import ProductionSettings
from opc_style.services.real_photos import AssetContentStoreError
from opc_style.services.s3_asset_store import S3AssetStore

PG_HEAD = "pg0002_style_tenant_registry"
TKE_STS_INTERNAL_ENDPOINT = "sts.internal.tencentcloudapi.com"


class DeferredProductionStore:
    """Open the network-backed store on first use, after HTTP is listening.

    CloudBase starts probing the container quickly. Building the COS client can
    perform an OIDC/STSes exchange, so doing it while importing the ASGI app
    prevents even the lightweight liveness endpoint from binding to its port.
    The proxy keeps that work behind the readiness/business boundary instead.
    """

    def __init__(self, builder: Callable[[], S3AssetStore]) -> None:
        self._builder = builder
        self._store: S3AssetStore | None = None
        self._lock = Lock()

    def _get(self) -> S3AssetStore:
        if self._store is None:
            with self._lock:
                if self._store is None:
                    try:
                        self._store = self._builder()
                    except AssetContentStoreError:
                        raise
                    except Exception as error:
                        raise AssetContentStoreError(
                            "private_storage_unavailable"
                        ) from error
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def probe(self) -> None:
        self._get().probe()

    def close(self) -> None:
        if self._store is not None:
            self._store.close()


class ReadinessStore(Protocol):
    """Small storage surface required by the production readiness probe."""

    def probe(self) -> None: ...


def _enforce_tencent_cos_no_overwrite(params: dict[str, Any], **_kwargs: Any) -> None:
    """Translate the immutable-create contract to Tencent COS's PUT header."""
    headers = params.setdefault("headers", {})
    headers.pop("If-None-Match", None)
    headers["x-cos-forbid-overwrite"] = "true"


def _refresh_tke_oidc_credentials() -> dict[str, str]:
    """Exchange the projected TKE identity for short-lived CAM credentials."""
    provider_type = importlib.import_module(
        "tencentcloud.common.credential"
    ).DefaultTkeOIDCRoleArnProvider
    credential = provider_type().get_credential()
    # The Tencent SDK defaults to the public STS hostname. This workload runs
    # without a public egress path, so the exchange must stay on Tencent's
    # private service network. The endpoint is hostname-only because the SDK
    # supplies HTTPS and signs the Host header itself.
    credential.endpoint = TKE_STS_INTERNAL_ENDPOINT
    secret_id, secret_key, token = credential.get_credential_info()
    if not all((secret_id, secret_key, token)):
        raise RuntimeError("tke_oidc_credentials_incomplete")
    # Tencent's default STS lifetime is two hours. Advertising only 90 minutes
    # makes botocore refresh well before the actual credential expires.
    expiry = datetime.now(UTC) + timedelta(minutes=90)
    return {
        "access_key": secret_id,
        "secret_key": secret_key,
        "token": token,
        "expiry_time": expiry.isoformat(),
    }


def _build_tke_oidc_s3_client(settings: ProductionSettings, config: Any) -> Any:
    refreshable_type = importlib.import_module(
        "botocore.credentials"
    ).RefreshableCredentials
    botocore_session = importlib.import_module("botocore.session").get_session()
    botocore_session._credentials = refreshable_type.create_from_metadata(
        metadata=_refresh_tke_oidc_credentials(),
        refresh_using=_refresh_tke_oidc_credentials,
        method="tke-oidc",
    )
    botocore_session.set_config_variable("region", settings.s3_region)
    boto3 = importlib.import_module("boto3")
    return boto3.Session(botocore_session=botocore_session).client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        config=config,
    )


def build_production_store(settings: ProductionSettings) -> S3AssetStore | None:
    if settings.asset_backend != "s3":
        return None
    assert settings.storage_capability_secret
    boto3 = importlib.import_module("boto3")
    config_type = importlib.import_module("botocore.config").Config
    client_config = config_type(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
        retries={"max_attempts": 0},
        connect_timeout=5,
        read_timeout=15,
    )
    if settings.s3_auth_mode == "tke_oidc":
        client = _build_tke_oidc_s3_client(settings, client_config)
    else:
        assert settings.s3_access_key and settings.s3_secret_key
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            aws_session_token=(
                settings.s3_session_token.get_secret_value()
                if settings.s3_session_token
                else None
            ),
            config=client_config,
        )
    hostname = urlparse(settings.s3_endpoint).hostname or ""
    if (
        hostname == "cos.myqcloud.com"
        or hostname.startswith("cos.")
        and hostname.endswith(".myqcloud.com")
    ):
        client.meta.events.register(
            "before-call.s3.PutObject", _enforce_tencent_cos_no_overwrite
        )
    return S3AssetStore(
        client,
        bucket=settings.s3_bucket,
        prefix=settings.s3_prefix,
        capability_secret=settings.storage_capability_secret.get_secret_value().encode(),
    )


def readiness(
    engine: Engine | None,
    store: ReadinessStore | None,
    settings: ProductionSettings,
    *,
    integration_enabled: bool,
) -> tuple[bool, dict[str, Any]]:
    probes: dict[str, str] = {}
    probes["source"] = (
        "ok"
        if re.fullmatch(r"[a-f0-9]{40}", settings.source_sha)
        else "invalid_source_sha"
    )
    probes["integration"] = "ok" if integration_enabled else "mall_integration_disabled"
    if engine is None or engine.dialect.name != "postgresql":
        probes["database"] = "production_postgresql_required"
        probes["schema"] = "unavailable"
    else:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                probes["database"] = "ok"
                version = (
                    connection.execute(
                        text("SELECT version_num FROM style_pg_alembic_version")
                    )
                    .scalars()
                    .all()
                )
                if version != [PG_HEAD]:
                    probes["schema"] = "migration_head_mismatch"
                else:
                    # Require the single-dispatch fence and database audit guard;
                    # a stamped-but-empty DB must never be considered ready.
                    connection.execute(
                        text(
                            "SELECT queue_id, outbound_attempts FROM mall_style_executions LIMIT 0"
                        )
                    )
                    connection.execute(
                        text(
                            "SELECT source_system, source_opc_id, merchant_id, opc_id "
                            "FROM mall_style_tenant_registrations LIMIT 0"
                        )
                    )
                    triggers = connection.execute(
                        text(
                            "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
                            "AND tgname IN ('guard_audit_events', "
                            "'guard_mall_style_tenant_registrations') "
                            "AND tgenabled = 'O'"
                        )
                    ).scalar_one()
                    probes["schema"] = (
                        "ok" if triggers == 2 else "immutable_audit_guard_missing"
                    )
        except Exception:  # noqa: BLE001 - probes never expose driver diagnostics
            probes.setdefault("database", "database_unavailable")
            probes["schema"] = "schema_unavailable"
    if store is None:
        probes["storage"] = "private_storage_unconfigured"
    else:
        try:
            store.probe()
            probes["storage"] = "ok"
        except AssetContentStoreError:
            probes["storage"] = "private_storage_unavailable"
    ok = all(value == "ok" for value in probes.values())
    return ok, {
        "status": "ready" if ok else "not_ready",
        "service": "opc-style-agent",
        "sourceSha": settings.source_sha,
        "probes": probes,
        "paidProviderEnabled": settings.executor_enabled,
    }
