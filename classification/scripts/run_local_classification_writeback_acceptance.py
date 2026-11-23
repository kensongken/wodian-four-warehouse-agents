from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

import asyncpg  # type: ignore[import-untyped]
import boto3  # type: ignore[import-untyped]
from product_engine.db import Database
from product_engine.models.classifications import (
    ProductClassificationAsset,
    ProductClassificationVersion,
)
from product_engine.models.events import OutboxEvent
from product_engine.models.products import (
    MaterialPackage,
    MaterialPackageAsset,
    Product,
    SourceImage,
)
from sqlalchemy import delete, func, select

EXPECTED_BATCH_ID = "four-warehouse-real-local-20260920-001"
EXPECTED_PRODUCTS = {
    "BCNHDIY1047": "unknown",
    "BCNHDIY914": "earring",
    "BCNHDIY933": "unknown",
    "BCNHDIY951": "necklace",
    "BCNHDIY952": "bracelet",
    "BCNHDIY997": "bracelet",
}
LOCAL_DATA_DIRECTORY = "integration-replay-clean-v2-data"
ADMIN_DSN = "postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres"


class AcceptanceRefusal(ValueError):
    pass


def validate_git_state(actual_sha: str, status: str, expected_sha: str) -> None:
    if status.strip():
        raise AcceptanceRefusal("worktree_not_clean")
    if actual_sha != expected_sha:
        raise AcceptanceRefusal("candidate_sha_mismatch")


def validate_database_name(database_name: str) -> None:
    if not re.fullmatch(r"[a-z0-9_]+_acceptance_[a-z0-9_]+", database_name):
        raise AcceptanceRefusal("unsafe_database_name")


def validate_loopback_urls(urls: list[str]) -> None:
    for value in urls:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise AcceptanceRefusal("external_network_forbidden")


def validate_batch(batch_dir: Path) -> list[dict[str, Any]]:
    try:
        manifest = json.loads((batch_dir / "batch-manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceRefusal("batch_manifest_unreadable") from error
    if manifest.get("batch_id") != EXPECTED_BATCH_ID:
        raise AcceptanceRefusal("unauthorized_batch")
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise AcceptanceRefusal("batch_manifest_unreadable")
    for item in items:
        path = (batch_dir / str(item["sanitized_file"])).resolve()
        if not path.is_relative_to(batch_dir.resolve()) or not path.is_file():
            raise AcceptanceRefusal("image_hash_mismatch")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("sanitized_sha256"):
            raise AcceptanceRefusal("image_hash_mismatch")
    return items


def verify_object_hashes(
    objects: list[dict[str, Any]], read_object: Any
) -> list[dict[str, str]]:
    verified: list[dict[str, str]] = []
    for item in objects:
        object_key = str(item.get("object_key", ""))
        expected = str(item.get("sha256", ""))
        actual = hashlib.sha256(read_object(object_key)).hexdigest()
        if not object_key or actual != expected:
            raise AcceptanceRefusal("minio_object_hash_mismatch")
        verified.append({"object_key": object_key, "sha256": actual})
    return verified


def verify_minio_objects(batch_dir: Path) -> list[dict[str, str]]:
    receipt = _read_json(batch_dir / "local-upload-receipt.json")
    objects = receipt.get("objects")
    if not isinstance(objects, list) or len(objects) != 12:
        raise AcceptanceRefusal("minio_object_count_mismatch")
    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:9000",
        region_name="us-east-1",
        aws_access_key_id="product_engine_dev",
        aws_secret_access_key="local-development-only",
    )

    def read_object(object_key: str) -> bytes:
        response = client.get_object(
            Bucket="photo-organizer-four-warehouse-20260920",
            Key=object_key,
        )
        body = response["Body"]
        try:
            return cast(bytes, body.read())
        finally:
            body.close()

    try:
        return verify_object_hashes(cast(list[dict[str, Any]], objects), read_object)
    except AcceptanceRefusal:
        raise
    except Exception as error:
        raise AcceptanceRefusal("minio_object_unavailable") from error


def validate_object_set(
    local_items: list[dict[str, Any]], verified_objects: list[dict[str, str]]
) -> None:
    local_hashes = sorted(str(item.get("sanitized_sha256", "")) for item in local_items)
    remote_hashes = sorted(item["sha256"] for item in verified_objects)
    if local_hashes != remote_hashes:
        raise AcceptanceRefusal("minio_manifest_hash_mismatch")


def require_no_style_handoffs(data_root: Path) -> int:
    path = data_root / "style-handoffs.json"
    if not path.exists():
        return 0
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceRefusal("style_handoff_receipt_unreadable") from error
    if payload not in ([], {}):
        raise AcceptanceRefusal("style_handoff_not_zero")
    return 0


def _git(repo_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AcceptanceRefusal("git_state_unavailable")
    return result.stdout.strip()


def _unused_loopback_port() -> int:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    return port


def _http_json(
    url: str, *, payload: Mapping[str, object] | None = None
) -> tuple[int, dict[str, Any]]:
    validate_loopback_urls([url])
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="GET" if data is None else "POST",
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, cast(dict[str, Any], json.loads(response.read()))
    except urllib.error.HTTPError as error:
        return error.code, cast(dict[str, Any], json.loads(error.read()))


def _wait_for_server(base_url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            status, _ = _http_json(f"{base_url}/health")
            if status == 200:
                return
        except (OSError, ValueError):
            time.sleep(0.1)
    raise RuntimeError("local_acceptance_server_timeout")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.writing")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def _create_database(database_name: str) -> bool:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database_name
        )
        if exists:
            return False
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        return True
    finally:
        await admin.close()


def _database_url(database_name: str) -> str:
    return (
        "postgresql+asyncpg://product_engine:product_engine_dev@127.0.0.1:5432/"
        f"{database_name}"
    )


def _migrate(repo_root: Path, database_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root / "services/backend",
        env={**os.environ, "DATABASE_URL": database_url},
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("acceptance_database_migration_failed")


def _receipt_products(batch_dir: Path) -> list[dict[str, Any]]:
    receipt = _read_json(batch_dir / "local-upload-receipt.json")
    products = receipt.get("products")
    if not isinstance(products, list) or len(products) != 6:
        raise AcceptanceRefusal("local_upload_receipt_mismatch")
    return products


async def _seed_database(database_url: str, batch_dir: Path) -> UUID:
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    receipt = _read_json(batch_dir / "local-upload-receipt.json")
    object_by_hash = {
        item["sha256"]: item["object_key"] for item in receipt.get("objects", [])
    }
    database = Database(database_url)
    try:
        async with database.session() as session:
            for product_payload in _receipt_products(batch_dir):
                product_code = str(product_payload["product_code"])
                product_id = uuid5(NAMESPACE_URL, f"classification-acceptance:{product_code}")
                package_id = uuid5(NAMESPACE_URL, f"classification-package:{product_code}:1")
                session.add(
                    Product(
                        id=product_id,
                        tenant_id=tenant_id,
                        product_code=product_code,
                        source_task_id=UUID(str(product_payload["task_id"])),
                        category="信息不足",
                        form=EXPECTED_PRODUCTS[product_code],
                        classification_confidence=Decimal("1.0000"),
                    )
                )
                await session.flush()
                session.add(
                    MaterialPackage(
                        id=package_id,
                        tenant_id=tenant_id,
                        product_id=product_id,
                        version=1,
                        status="source_ready",
                        content_status="pending",
                        price_status="pending",
                        published_event_id=uuid5(
                            NAMESPACE_URL, f"classification-published:{product_code}:1"
                        ),
                    )
                )
                for position, asset in enumerate(product_payload["items"]):
                    source_id = uuid5(
                        NAMESPACE_URL,
                        f"classification-source:{product_code}:{position}",
                    )
                    sha256 = str(asset["sha256"])
                    session.add(
                        SourceImage(
                            id=source_id,
                            tenant_id=tenant_id,
                            product_id=product_id,
                            upstream_asset_id=UUID(str(asset["asset_id"])),
                            object_key=str(object_by_hash[sha256]),
                            sha256=sha256,
                            mime_type="image/jpeg",
                            kind="original",
                            role="main_candidate" if position == 0 else "gallery",
                            quality_confidence=Decimal("1.0000"),
                        )
                    )
                    await session.flush()
                    session.add(
                        MaterialPackageAsset(
                            id=uuid5(
                                NAMESPACE_URL,
                                f"classification-package-asset:{product_code}:{position}",
                            ),
                            tenant_id=tenant_id,
                            package_id=package_id,
                            source_image_id=source_id,
                            position=position,
                        )
                    )
    finally:
        await database.dispose()
    return tenant_id


async def _validate_seeded_database(database_url: str, batch_dir: Path) -> UUID:
    expected = {
        (
            str(product["product_code"]),
            position,
            str(asset["asset_id"]),
            str(asset["sha256"]),
            "main_candidate" if position == 0 else "gallery",
        )
        for product in _receipt_products(batch_dir)
        for position, asset in enumerate(product["items"])
    }
    database = Database(database_url)
    try:
        async with database.session() as session:
            rows = (
                await session.execute(
                    select(
                        Product.tenant_id,
                        Product.product_code,
                        MaterialPackageAsset.position,
                        SourceImage.upstream_asset_id,
                        SourceImage.sha256,
                        SourceImage.role,
                    )
                    .join(MaterialPackage, MaterialPackage.product_id == Product.id)
                    .join(
                        MaterialPackageAsset,
                        MaterialPackageAsset.package_id == MaterialPackage.id,
                    )
                    .join(
                        SourceImage,
                        SourceImage.id == MaterialPackageAsset.source_image_id,
                    )
                    .where(MaterialPackage.version == 1)
                )
            ).all()
        tenants = {row[0] for row in rows}
        actual = {
            (str(row[1]), int(row[2]), str(row[3]), str(row[4]), str(row[5]))
            for row in rows
        }
        if len(tenants) != 1 or actual != expected:
            raise AcceptanceRefusal("existing_acceptance_seed_mismatch")
        return next(iter(tenants))
    finally:
        await database.dispose()


def _upgrade_local_provenance(batch_dir: Path) -> dict[str, str]:
    data_root = batch_dir / LOCAL_DATA_DIRECTORY
    batches = _read_json(data_root / "upload-batches.json")
    products = {str(item["product_code"]): item for item in _receipt_products(batch_dir)}
    batch_ids: dict[str, str] = {}
    imports: dict[str, object] = {}
    for batch in batches:
        items = sorted(batch.get("items", []), key=lambda item: item["position"])
        if len(items) != 2:
            continue
        product_code = str(items[0].get("product_code"))
        if product_code not in products:
            continue
        receipt_items = products[product_code]["items"]
        if [item["sha256"] for item in items] != [item["sha256"] for item in receipt_items]:
            raise AcceptanceRefusal("local_review_hash_mismatch")
        batch_id = str(batch["batch_id"])
        batch_ids[product_code] = batch_id
        imports[f"{product_code}:v1"] = {
            "batch_id": batch_id,
            "assets": [
                {
                    "item_id": str(item["item_id"]),
                    "asset_id": str(receipt_asset["asset_id"]),
                    "position": position,
                    "role": "main_candidate" if position == 0 else "gallery",
                }
                for position, (item, receipt_asset) in enumerate(
                    zip(items, receipt_items, strict=True)
                )
            ],
        }
    if set(batch_ids) != set(EXPECTED_PRODUCTS):
        raise AcceptanceRefusal("local_review_batch_mismatch")
    _write_json(data_root / "album-review-imports.json", imports)
    return batch_ids


async def _database_counts(database_url: str) -> dict[str, int]:
    database = Database(database_url)
    try:
        async with database.session() as session:
            versions = await session.scalar(
                select(func.count()).select_from(ProductClassificationVersion)
            )
            assets = await session.scalar(
                select(func.count()).select_from(ProductClassificationAsset)
            )
            sales = await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.topic == "sales.strategy.input.updated.v1",
                    OutboxEvent.status == "delivered",
                )
            )
        return {
            "classification_versions": int(versions or 0),
            "classification_assets": int(assets or 0),
            "sales_delivered": int(sales or 0),
        }
    finally:
        await database.dispose()


async def _install_stale_material_probe(database_url: str, product_code: str) -> UUID:
    database = Database(database_url)
    probe_id = uuid5(NAMESPACE_URL, f"classification-stale-probe:{product_code}:2")
    try:
        async with database.session() as session:
            product = (
                await session.execute(select(Product).where(Product.product_code == product_code))
            ).scalar_one()
            original = (
                await session.execute(
                    select(MaterialPackage).where(
                        MaterialPackage.product_id == product.id,
                        MaterialPackage.version == 1,
                    )
                )
            ).scalar_one()
            source_assets = (
                await session.execute(
                    select(MaterialPackageAsset)
                    .where(MaterialPackageAsset.package_id == original.id)
                    .order_by(MaterialPackageAsset.position)
                )
            ).scalars().all()
            session.add(
                MaterialPackage(
                    id=probe_id,
                    tenant_id=original.tenant_id,
                    product_id=original.product_id,
                    version=2,
                    status=original.status,
                    content_status=original.content_status,
                    price_status=original.price_status,
                    published_event_id=uuid5(
                        NAMESPACE_URL, f"classification-stale-event:{product_code}:2"
                    ),
                )
            )
            await session.flush()
            session.add_all(
                MaterialPackageAsset(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"classification-stale-asset:{product_code}:{asset.position}",
                    ),
                    tenant_id=asset.tenant_id,
                    package_id=probe_id,
                    source_image_id=asset.source_image_id,
                    position=asset.position,
                )
                for asset in source_assets
            )
    finally:
        await database.dispose()
    return probe_id


async def _remove_stale_material_probe(database_url: str, probe_id: UUID) -> None:
    database = Database(database_url)
    try:
        async with database.session() as session:
            await session.execute(
                delete(MaterialPackageAsset).where(
                    MaterialPackageAsset.package_id == probe_id
                )
            )
            await session.execute(
                delete(MaterialPackage).where(MaterialPackage.id == probe_id)
            )
    finally:
        await database.dispose()


def _mutate_product_item(
    batches_path: Path,
    *,
    batch_id: str,
    product_code: str,
    field: str,
    value: str,
) -> bytes:
    original = batches_path.read_bytes()
    batches = json.loads(original)
    changed = False
    for batch in batches:
        if batch.get("batch_id") != batch_id:
            continue
        for item in batch.get("items", []):
            if item.get("product_code") == product_code:
                item[field] = value
                changed = True
                break
    if not changed:
        raise AcceptanceRefusal("negative_probe_item_missing")
    _write_json(batches_path, batches)
    return original


def _restore_file(path: Path, original: bytes) -> None:
    temporary = path.with_name(f".{path.name}.probe-restore")
    temporary.write_bytes(original)
    temporary.replace(path)


def _require_rejection(
    base_url: str,
    *,
    product_code: str,
    batch_id: str,
    request_suffix: str,
    expected_code: str,
) -> dict[str, str]:
    url = (
        f"{base_url}/local/api/album-review-inbox/{product_code}/versions/1/"
        "classification-confirmations"
    )
    status, payload = _http_json(
        url,
        payload={
            "request_id": str(
                uuid5(
                    NAMESPACE_URL,
                    f"classification-negative:{product_code}:{request_suffix}",
                )
            ),
            "local_batch_id": batch_id,
        },
    )
    if status != 409 or payload.get("error_code") != expected_code:
        raise RuntimeError(
            f"negative_probe_failed:{request_suffix}:{status}:{payload}"
        )
    return {"probe": request_suffix, "error_code": expected_code}


def _run_negative_probes(
    *,
    base_url: str,
    database_url: str,
    data_root: Path,
    batch_ids: dict[str, str],
) -> list[dict[str, str]]:
    product_code = "BCNHDIY1047"
    batch_id = batch_ids[product_code]
    batches_path = data_root / "upload-batches.json"
    results: list[dict[str, str]] = []

    original = _mutate_product_item(
        batches_path,
        batch_id=batch_id,
        product_code=product_code,
        field="status",
        value="pending_review",
    )
    try:
        results.append(
            _require_rejection(
                base_url,
                product_code=product_code,
                batch_id=batch_id,
                request_suffix="incomplete-review",
                expected_code="product_review_incomplete",
            )
        )
    finally:
        _restore_file(batches_path, original)

    original = _mutate_product_item(
        batches_path,
        batch_id=batch_id,
        product_code=product_code,
        field="sha256",
        value="0" * 64,
    )
    try:
        results.append(
            _require_rejection(
                base_url,
                product_code=product_code,
                batch_id=batch_id,
                request_suffix="changed-hash",
                expected_code="classification_asset_mismatch",
            )
        )
    finally:
        _restore_file(batches_path, original)

    probe_id = asyncio.run(_install_stale_material_probe(database_url, product_code))
    try:
        results.append(
            _require_rejection(
                base_url,
                product_code=product_code,
                batch_id=batch_id,
                request_suffix="stale-version",
                expected_code="stale_material_package_version",
            )
        )
    finally:
        asyncio.run(_remove_stale_material_probe(database_url, probe_id))
    return results


def _start_server(
    repo_root: Path,
    database_url: str,
    tenant_id: UUID,
    data_root: Path,
    port: int,
) -> tuple[subprocess.Popen[bytes], Any]:
    log_path = data_root / "classification-acceptance-server.log"
    log = log_path.open("wb")
    environment = _server_environment(
        database_url=database_url,
        tenant_id=str(tenant_id),
        data_root=data_root,
        python_path=repo_root / "services/backend/src",
        inherited=os.environ,
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "product_engine.api.local_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=repo_root / "services/backend",
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return process, log


def _server_environment(
    *,
    database_url: str,
    tenant_id: str,
    data_root: Path,
    python_path: Path,
    inherited: Mapping[str, str],
) -> dict[str, str]:
    return {
        **inherited,
        "APP_ENV": "development",
        "DATABASE_URL": database_url,
        "LOCAL_ALBUM_SYNC_TENANT_ID": tenant_id,
        "LOCAL_DATA_ROOT": str(data_root),
        "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "S3_REGION": "us-east-1",
        "S3_BUCKET": "photo-organizer-four-warehouse-20260920",
        "S3_ACCESS_KEY_ID": "product_engine_dev",
        "S3_SECRET_ACCESS_KEY": "local-development-only",
        "ALBUM_SIGNING_SECRET": "local-acceptance-album-signing-secret",
        "SALES_SIGNING_SECRET": "local-acceptance-sales-signing-secret",
        "STYLE_SIGNING_SECRET": "local-acceptance-style-signing-secret",
        "ADMIN_SIGNING_SECRET": "local-acceptance-admin-signing-secret",
        "SALES_WEBHOOK_URL": "http://127.0.0.1:9/disabled",
        "PYTHONPATH": str(python_path),
        "NO_PROXY": "127.0.0.1,localhost",
    }


def execute(batch_dir: Path, expected_sha: str, database_name: str) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[1]
    validate_database_name(database_name)
    actual_sha = _git(repo_root, "rev-parse", "HEAD")
    validate_git_state(actual_sha, _git(repo_root, "status", "--porcelain"), expected_sha)
    verified_items = validate_batch(batch_dir)
    validate_loopback_urls(["http://127.0.0.1:9000"])
    verified_objects = verify_minio_objects(batch_dir)
    validate_object_set(verified_items, verified_objects)
    database_url = _database_url(database_name)
    database_created = asyncio.run(_create_database(database_name))
    _migrate(repo_root, database_url)
    tenant_id = asyncio.run(
        _seed_database(database_url, batch_dir)
        if database_created
        else _validate_seeded_database(database_url, batch_dir)
    )
    batch_ids = _upgrade_local_provenance(batch_dir)
    data_root = batch_dir / LOCAL_DATA_DIRECTORY
    style_delivered_before = require_no_style_handoffs(data_root)
    port = _unused_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    validate_loopback_urls([base_url])
    process, log = _start_server(
        repo_root,
        database_url,
        tenant_id,
        data_root,
        port,
    )
    created_receipts: list[dict[str, Any]] = []
    duplicate_receipts: list[dict[str, Any]] = []
    negative_probes: list[dict[str, str]] = []
    try:
        _wait_for_server(base_url)
        page_status, _ = _http_json(f"{base_url}/local/api/album-review-inbox")
        if page_status != 200:
            raise RuntimeError("album_inbox_unavailable")
        for product_code in sorted(EXPECTED_PRODUCTS):
            payload = {
                "request_id": str(
                    uuid5(NAMESPACE_URL, f"classification-request:{product_code}")
                ),
                "local_batch_id": batch_ids[product_code],
            }
            url = (
                f"{base_url}/local/api/album-review-inbox/{product_code}/versions/1/"
                "classification-confirmations"
            )
            status, receipt = _http_json(url, payload=payload)
            if status != 201 or receipt.get("disposition") != "created":
                raise RuntimeError(f"classification_create_failed:{product_code}:{receipt}")
            created_receipts.append(receipt)
            replay_status, replay = _http_json(url, payload=payload)
            if replay_status != 200 or replay.get("disposition") != "duplicate":
                raise RuntimeError(f"classification_replay_failed:{product_code}:{replay}")
            duplicate_receipts.append(replay)
        negative_probes = _run_negative_probes(
            base_url=base_url,
            database_url=database_url,
            data_root=data_root,
            batch_ids=batch_ids,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()

    counts = asyncio.run(_database_counts(database_url))
    if counts != {
        "classification_versions": 6,
        "classification_assets": 12,
        "sales_delivered": 0,
    }:
        raise RuntimeError(f"classification_acceptance_count_mismatch:{counts}")
    style_delivered_after = require_no_style_handoffs(data_root)
    if style_delivered_before != style_delivered_after:
        raise RuntimeError("style_handoff_count_changed")
    receipt = {
        "schema_version": "authoritative-classification-writeback/1.0",
        "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "batch_id": EXPECTED_BATCH_ID,
        "git_sha": actual_sha,
        "database": database_name,
        "page_url": f"{base_url}/local/templates",
        "products": 6,
        "images": len(verified_items),
        **counts,
        "style_delivered": style_delivered_after,
        "minio_verified_objects": len(verified_objects),
        "negative_probes": negative_probes,
        "duplicate_replay_created_rows": 0,
        "created_receipts": created_receipts,
        "duplicate_receipts": duplicate_receipts,
        "image_hashes": [item["sanitized_sha256"] for item in verified_items],
        "scope": "local_only_no_external_send_no_paid_provider",
        "decision": "authoritative_classification_writeback_passed_downstream_not_sent",
        "remaining_gate": (
            "sales_strategy_v2_and_customer_style_handoffs_require_separate_implementation_"
            "and_independent_receipts"
        ),
    }
    _write_json(batch_dir / "authoritative-classification-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--database-name", required=True)
    arguments = parser.parse_args()
    try:
        receipt = execute(
            arguments.batch_dir.resolve(),
            arguments.expected_sha,
            arguments.database_name,
        )
    except AcceptanceRefusal as error:
        print(json.dumps({"status": "refused", "issue_code": str(error)}))
        return 2
    except Exception as error:  # noqa: BLE001 - CLI must emit a machine-readable failure
        print(
            json.dumps(
                {
                    "status": "failed",
                    "issue_code": (
                        str(error)
                        if re.fullmatch(r"[a-z0-9_:.-]+", str(error))
                        else "classification_acceptance_failed"
                    ),
                }
            )
        )
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
