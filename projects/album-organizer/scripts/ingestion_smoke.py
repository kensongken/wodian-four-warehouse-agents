#!/usr/bin/env python3
"""Exercise the Phase 1 upload contract against real or explicit offline targets."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import boto3
import httpx


async def run_smoke(
    *,
    client: httpx.AsyncClient,
    access_token: str,
    manifest_path: Path,
    object_count: Callable[[], int],
    max_concurrency: int = 8,
) -> str:
    assets = _load_manifest(manifest_path)
    if max_concurrency < 1 or max_concurrency > 8:
        raise ValueError("max_concurrency must be between 1 and 8")
    before = await asyncio.to_thread(object_count)
    if before != 0:
        raise RuntimeError(
            f"smoke bucket must be empty before the run; found {before} objects"
        )
    headers = {"Authorization": f"Bearer {access_token}"}
    task_response = await client.post(
        "/v1/tasks",
        headers=headers,
        json={"name": f"Phase 1 smoke ({len(assets)} images)"},
    )
    task_response.raise_for_status()
    task_id = _required_string(_json_object(task_response), "id")
    semaphore = asyncio.Semaphore(max_concurrency)

    async def upload(asset: dict[str, object]) -> None:
        async with semaphore:
            await _upload_asset(
                client=client,
                headers=headers,
                task_id=task_id,
                manifest_directory=manifest_path.parent,
                asset=asset,
            )

    await asyncio.gather(*(upload(asset) for asset in assets))
    seal = await client.post(f"/v1/tasks/{task_id}/seal", headers=headers)
    seal.raise_for_status()
    if _required_string(_json_object(seal), "status") != "ai_processing":
        raise RuntimeError("task did not enter ai_processing after sealing")
    summary_response = await client.get(f"/v1/tasks/{task_id}", headers=headers)
    summary_response.raise_for_status()
    summary = _json_object(summary_response)
    expected = len(assets)
    actual = (
        _required_int(summary, "total_assets"),
        _required_int(summary, "uploaded_assets"),
        _required_int(summary, "failed_assets"),
        _required_int(summary, "unsupported_assets"),
    )
    if actual != (expected, expected, 0, 0):
        raise RuntimeError(f"unexpected final task counts: {actual}")
    after = await asyncio.to_thread(object_count)
    if after != expected:
        raise RuntimeError(
            f"object count mismatch: expected {expected}, found {after}"
        )
    return (
        f"PASS: {expected} accepted, {expected} uploaded, 0 omitted, "
        "task sealed once"
    )


async def run_offline_smoke(*, manifest_path: Path) -> str:
    """Exercise the API contract in memory without any network or credentials."""

    task_id = "11111111-1111-4111-8111-111111111111"
    completed_asset_ids: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "offline-storage.invalid":
            if request.method != "PUT":
                raise AssertionError("offline storage received a non-PUT request")
            return httpx.Response(200, headers={"ETag": '"offline-etag"'})
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(201, json={"id": task_id})
        if request.url.path.endswith("/assets/initiate"):
            body = json.loads(request.content)
            local_asset_id = _required_string(body, "local_asset_id")
            asset_id = str(uuid5(UUID(task_id), local_asset_id))
            return httpx.Response(
                201,
                json={
                    "asset_id": asset_id,
                    "already_uploaded": False,
                    "provider_upload_id": "offline-provider-upload",
                    "part_size": 8 * 1024 * 1024,
                    "completed_parts": [],
                },
            )
        if request.url.path.endswith("/parts/sign"):
            asset_id = request.url.path.split("/")[3]
            return httpx.Response(
                200,
                json={
                    "url": f"https://offline-storage.invalid/{asset_id}/part",
                    "expires_seconds": 900,
                },
            )
        if request.url.path.endswith("/complete"):
            completed_asset_ids.add(request.url.path.split("/")[3])
            return httpx.Response(200, json={"status": "uploaded"})
        if request.method == "POST" and request.url.path == f"/v1/tasks/{task_id}/seal":
            return httpx.Response(200, json={"status": "ai_processing"})
        if request.method == "GET" and request.url.path == f"/v1/tasks/{task_id}":
            count = len(completed_asset_ids)
            return httpx.Response(
                200,
                json={
                    "id": task_id,
                    "status": "ai_processing",
                    "total_assets": count,
                    "uploaded_assets": count,
                    "failed_assets": 0,
                    "unsupported_assets": 0,
                },
            )
        raise AssertionError(f"unexpected offline request {request.method} {request.url.path}")

    async with httpx.AsyncClient(
        base_url="http://offline-api.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await run_smoke(
            client=client,
            access_token="offline-token",
            manifest_path=manifest_path,
            object_count=lambda: len(completed_asset_ids),
        )
    return result.replace("PASS:", "OFFLINE PASS:", 1)


async def _upload_asset(
    *,
    client: httpx.AsyncClient,
    headers: dict[str, str],
    task_id: str,
    manifest_directory: Path,
    asset: dict[str, object],
) -> None:
    filename = _required_string(asset, "filename")
    content = (manifest_directory / filename).read_bytes()
    expected_size = _required_int(asset, "byte_size")
    expected_sha = _required_string(asset, "sha256")
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha:
        raise RuntimeError(f"fixture integrity mismatch: {filename}")
    initiated_response = await client.post(
        f"/v1/tasks/{task_id}/assets/initiate",
        headers=headers,
        json={
            "local_asset_id": _required_string(asset, "local_asset_id"),
            "filename": filename,
            "media_type": _required_string(asset, "media_type"),
            "byte_size": expected_size,
            "sha256": expected_sha,
        },
    )
    initiated_response.raise_for_status()
    initiated = _json_object(initiated_response)
    if initiated.get("already_uploaded") is not False:
        raise RuntimeError("clean-bucket smoke unexpectedly deduplicated an asset")
    asset_id = _required_string(initiated, "asset_id")
    part_size = _required_int(initiated, "part_size")
    if part_size <= 0:
        raise RuntimeError("server returned an invalid part size")
    completed_parts = initiated.get("completed_parts")
    if completed_parts != []:
        raise RuntimeError("clean-bucket smoke unexpectedly resumed multipart state")
    parts: list[dict[str, object]] = []
    part_count = (len(content) + part_size - 1) // part_size
    for part_number in range(1, part_count + 1):
        signed_response = await client.post(
            f"/v1/assets/{asset_id}/parts/sign",
            headers=headers,
            json={"part_number": part_number},
        )
        signed_response.raise_for_status()
        signed_url = _required_string(_json_object(signed_response), "url")
        start = (part_number - 1) * part_size
        payload = content[start : start + part_size]
        uploaded = await client.put(
            signed_url,
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        uploaded.raise_for_status()
        etag = uploaded.headers.get("etag", "").strip('"')
        if not etag or len(etag) > 256:
            raise RuntimeError("object storage returned an invalid ETag")
        parts.append({"part_number": part_number, "etag": etag})
    completed_response = await client.post(
        f"/v1/assets/{asset_id}/complete",
        headers=headers,
        json={"parts": parts},
    )
    completed_response.raise_for_status()


def _load_manifest(path: Path) -> list[dict[str, object]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("unsupported fixture manifest")
    count = raw.get("count")
    assets = raw.get("assets")
    if not isinstance(count, int) or not isinstance(assets, list) or count != len(assets):
        raise ValueError("invalid fixture manifest count")
    if count < 1 or count > 3000 or not all(isinstance(asset, dict) for asset in assets):
        raise ValueError("invalid fixture manifest assets")
    return [dict(asset) for asset in assets]


def _json_object(response: httpx.Response) -> dict[str, Any]:
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError("server returned a non-object response")
    return value


def _required_string(value: dict[str, Any] | dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeError(f"missing response field: {key}")
    return item


def _required_int(value: dict[str, Any] | dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise TypeError(f"missing response field: {key}")
    return item


def _object_counter(args: argparse.Namespace) -> Callable[[], int]:
    s3 = boto3.client(
        "s3",
        endpoint_url=args.s3_endpoint,
        region_name=args.s3_region,
        aws_access_key_id=args.s3_access_key,
        aws_secret_access_key=args.s3_secret_key,
    )

    def count() -> int:
        paginator = s3.get_paginator("list_objects_v2")
        return sum(
            page.get("KeyCount", 0)
            for page in paginator.paginate(Bucket=args.s3_bucket)
        )

    return count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--offline-manifest", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--access-token")
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument(
        "--s3-endpoint",
        default=os.getenv("S3_ENDPOINT_URL", "http://localhost:9000"),
    )
    parser.add_argument("--s3-region", default=os.getenv("S3_REGION", "us-east-1"))
    parser.add_argument(
        "--s3-bucket",
        default=os.getenv("S3_BUCKET", "photo-organizer-dev"),
    )
    parser.add_argument(
        "--s3-access-key",
        default=os.getenv("S3_ACCESS_KEY_ID", "minioadmin"),
    )
    parser.add_argument(
        "--s3-secret-key",
        default=os.getenv("S3_SECRET_ACCESS_KEY", "minioadmin"),
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    if args.offline_manifest is not None:
        print(
            await run_offline_smoke(
                manifest_path=args.offline_manifest.expanduser().resolve(),
            )
        )
        return
    if args.base_url is None or args.access_token is None:
        raise SystemExit("--base-url and --access-token are required with --manifest")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=120) as client:
        result = await run_smoke(
            client=client,
            access_token=args.access_token,
            manifest_path=args.manifest.expanduser().resolve(),
            object_count=_object_counter(args),
            max_concurrency=args.max_concurrency,
        )
    print(result)


def main() -> int:
    try:
        asyncio.run(_main())
    except Exception:  # noqa: BLE001 - CLI output must not expose paths or traces.
        print("ingestion_smoke_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
