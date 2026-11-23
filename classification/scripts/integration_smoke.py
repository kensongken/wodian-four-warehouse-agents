"""Phase 1 smoke 入口；仅在明确指定目标地址时才允许网络请求。"""

import argparse
import hashlib
import ipaddress
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID, uuid4, uuid5

import httpx
from product_engine.security.signatures import (
    sign_request,  # type: ignore[import-untyped]
)


class PilotFailure(RuntimeError):
    pass


def _is_loopback(hostname: str | None) -> bool:
    if hostname == "localhost":
        return True
    if hostname is None:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_target(base_url: str, *, allow_remote: bool, full: bool) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PilotFailure("invalid_base_url")
    is_local = _is_loopback(parsed.hostname)
    if not is_local and not allow_remote:
        raise PilotFailure("remote_target_not_allowed")
    if full and not is_local and parsed.scheme != "https":
        raise PilotFailure("remote_full_requires_https")


@dataclass(frozen=True)
class PilotConfig:
    base_url: str
    tenant_id: UUID
    album_secret: bytes
    sales_secret: bytes


@dataclass(frozen=True)
class PilotResult:
    run_id: UUID
    product_code: str
    product_status: str
    album_dispositions: tuple[str, str]
    usage_dispositions: tuple[str, str]
    screenshot_dispositions: tuple[str, str]


def _body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _signed_headers(
    *,
    secret: bytes,
    source_system: str,
    tenant_id: UUID,
    method: str,
    path: str,
    body: bytes,
) -> dict[str, str]:
    timestamp = int(time.time())
    return {
        "Content-Type": "application/json",
        "X-Source-System": source_system,
        "X-Tenant-Id": str(tenant_id),
        "X-Signature-Timestamp": str(timestamp),
        "X-Signature": sign_request(
            secret, timestamp, source_system, tenant_id, method, path, body
        ),
    }


def _request_json(
    client: httpx.Client,
    config: PilotConfig,
    *,
    source_system: str,
    secret: bytes,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    expected_statuses: set[int],
) -> dict[str, object]:
    body = _body(payload) if payload is not None else b""
    response = client.request(
        method,
        f"{config.base_url.rstrip('/')}{path}",
        content=body,
        headers=_signed_headers(
            secret=secret,
            source_system=source_system,
            tenant_id=config.tenant_id,
            method=method,
            path=path,
            body=body,
        ),
        timeout=10,
    )
    if response.status_code not in expected_statuses:
        raise PilotFailure(f"unexpected_{path.rsplit('/', 1)[-1]}_status")
    result = response.json()
    if not isinstance(result, dict):
        raise PilotFailure("invalid_json_response")
    return result


def _disposition(payload: dict[str, object], expected: str, step: str) -> str:
    value = payload.get("disposition")
    if value != expected:
        raise PilotFailure(f"{step}_{expected}_missing")
    return str(value)


def run_vertical_slice(
    client: httpx.Client,
    config: PilotConfig,
    run_id: UUID,
) -> PilotResult:
    occurred_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    product_code = f"PILOT-{run_id.hex[:12].upper()}"
    album_path = "/v1/integrations/album/product-image-sets"
    source_bytes = b"synthetic-album-image:" + run_id.bytes
    album_payload: dict[str, object] = {
        "schema_version": 1,
        "event_id": str(uuid5(run_id, "album-event")),
        "occurred_at": occurred_at,
        "tenant_id": str(config.tenant_id),
        "source_task_id": str(uuid5(run_id, "source-task")),
        "product_code": product_code,
        "category": "synthetic_pilot",
        "form": "bracelet",
        "classification_confidence": 0.5,
        "images": [
            {
                "asset_id": str(uuid5(run_id, "source-image")),
                "object_key": f"tenants/{config.tenant_id}/pilot/{run_id}.png",
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "mime_type": "image/png",
                "kind": "original",
                "role": "main_candidate",
                "quality_confidence": 0.5,
            }
        ],
    }
    album_first = _request_json(
        client,
        config,
        source_system="album",
        secret=config.album_secret,
        method="POST",
        path=album_path,
        payload=album_payload,
        expected_statuses={202},
    )
    album_replay = _request_json(
        client,
        config,
        source_system="album",
        secret=config.album_secret,
        method="POST",
        path=album_path,
        payload=album_payload,
        expected_statuses={200},
    )

    materials_path = f"/v1/products/{product_code}/materials/latest"
    materials = _request_json(
        client,
        config,
        source_system="sales",
        secret=config.sales_secret,
        method="GET",
        path=materials_path,
        expected_statuses={200},
    )
    if (
        materials.get("status") != "source_ready"
        or materials.get("content_status") != "pending"
        or materials.get("price_status") != "pending"
    ):
        raise PilotFailure("unsafe_material_package_state")
    assets = materials.get("source_assets")
    if not isinstance(assets, list) or not assets or not isinstance(assets[0], dict):
        raise PilotFailure("source_material_missing")

    usage_path = "/v1/integrations/sales/material-usage"
    usage_payload: dict[str, object] = {
        "schema_version": 1,
        "event_id": str(uuid5(run_id, "usage-event")),
        "occurred_at": occurred_at,
        "tenant_id": str(config.tenant_id),
        "session_id": str(uuid5(run_id, "sales-session")),
        "product_code": product_code,
        "package_version": materials["package_version"],
        "usage_type": "copied",
        "material_ids": [assets[0]["material_id"]],
    }
    usage_first = _request_json(
        client,
        config,
        source_system="sales",
        secret=config.sales_secret,
        method="POST",
        path=usage_path,
        payload=usage_payload,
        expected_statuses={202},
    )
    usage_replay = _request_json(
        client,
        config,
        source_system="sales",
        secret=config.sales_secret,
        method="POST",
        path=usage_path,
        payload=usage_payload,
        expected_statuses={200},
    )

    screenshot_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + run_id.bytes + b"synthetic-pilot"
    )
    screenshot_sha256 = hashlib.sha256(screenshot_bytes).hexdigest()
    upload_path = "/v1/integrations/sales/screenshots/uploads"
    upload = _request_json(
        client,
        config,
        source_system="sales",
        secret=config.sales_secret,
        method="POST",
        path=upload_path,
        payload={
            "mime_type": "image/png",
            "byte_size": len(screenshot_bytes),
            "sha256": screenshot_sha256,
        },
        expected_statuses={201},
    )
    upload_url = upload.get("url")
    upload_headers = upload.get("headers")
    object_key = upload.get("object_key")
    if not isinstance(upload_url, str) or not isinstance(upload_headers, dict):
        raise PilotFailure("invalid_screenshot_upload_target")
    if not isinstance(object_key, str):
        raise PilotFailure("invalid_screenshot_object_key")
    upload_target = urlparse(upload_url)
    base_target = urlparse(config.base_url)
    if (
        upload_target.scheme not in {"http", "https"}
        or not upload_target.hostname
        or (
            _is_loopback(base_target.hostname)
            and not _is_loopback(upload_target.hostname)
        )
    ):
        raise PilotFailure("unsafe_screenshot_upload_target")
    uploaded = client.put(
        upload_url, content=screenshot_bytes, headers=upload_headers, timeout=10
    )
    if uploaded.status_code not in {200, 201, 204}:
        raise PilotFailure(f"screenshot_upload_failed_{uploaded.status_code}")

    screenshot_path = "/v1/integrations/sales/screenshots/imports"
    screenshot_payload: dict[str, object] = {
        "schema_version": 1,
        "event_id": str(uuid5(run_id, "screenshot-event")),
        "occurred_at": occurred_at,
        "tenant_id": str(config.tenant_id),
        "screenshot_import_id": str(uuid5(run_id, "screenshot-import")),
        "object_key": object_key,
        "sha256": screenshot_sha256,
        "mime_type": "image/png",
        "byte_size": len(screenshot_bytes),
        "session_id": str(uuid5(run_id, "sales-session")),
        "product_code": product_code,
    }
    screenshot_first = _request_json(
        client,
        config,
        source_system="sales",
        secret=config.sales_secret,
        method="POST",
        path=screenshot_path,
        payload=screenshot_payload,
        expected_statuses={202},
    )
    screenshot_replay = _request_json(
        client,
        config,
        source_system="sales",
        secret=config.sales_secret,
        method="POST",
        path=screenshot_path,
        payload=screenshot_payload,
        expected_statuses={200},
    )

    return PilotResult(
        run_id=run_id,
        product_code=product_code,
        product_status=str(materials["status"]),
        album_dispositions=(
            _disposition(album_first, "accepted", "album"),
            _disposition(album_replay, "duplicate", "album"),
        ),
        usage_dispositions=(
            _disposition(usage_first, "accepted", "usage"),
            _disposition(usage_replay, "duplicate", "usage"),
        ),
        screenshot_dispositions=(
            _disposition(screenshot_first, "accepted", "screenshot"),
            _disposition(screenshot_replay, "duplicate", "screenshot"),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--tenant-id")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        validate_target(args.base_url, allow_remote=args.allow_remote, full=args.full)
    except PilotFailure as error:
        print(f"{error}=1")
        return 2
    try:
        response = httpx.get(f"{args.base_url.rstrip('/')}/health/live", timeout=5)
    except httpx.HTTPError:
        print("health_unreachable=1")
        return 2
    if response.status_code != 200:
        print("health_status_invalid=1")
        return 1
    if args.full:
        album_secret = os.environ.get("ALBUM_SIGNING_SECRET")
        sales_secret = os.environ.get("SALES_SIGNING_SECRET")
        if not args.tenant_id or not album_secret or not sales_secret:
            print("pilot_config_missing=1")
            return 2
        try:
            config = PilotConfig(
                base_url=args.base_url,
                tenant_id=UUID(args.tenant_id),
                album_secret=album_secret.encode(),
                sales_secret=sales_secret.encode(),
            )
            run_id = UUID(args.run_id) if args.run_id else uuid4()
            with httpx.Client(trust_env=False) as client:
                result = run_vertical_slice(client, config, run_id)
        except (ValueError, PilotFailure, httpx.HTTPError) as error:
            code = (
                str(error)
                if isinstance(error, PilotFailure)
                else "pilot_request_failed"
            )
            print(f"pilot_error={code}")
            return 1
        print(f"pilot_run_id={result.run_id}")
        print(f"product_status={result.product_status}")
        print("album_replay=duplicate")
        print("usage_replay=duplicate")
        print("screenshot_replay=duplicate")
    print("health_ok=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
