from __future__ import annotations

import argparse
import asyncio
import copy
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast
from urllib.parse import urlparse
from uuid import UUID

from product_engine.security.signatures import sign_request
from product_engine.services.sales_strategy_v2 import SalesPreviewFailure, SalesPreviewPreparer
from product_engine.services.sales_warehouse_receiver import (
    DELIVERY_PATH,
    SalesReceiverFailure,
    SalesWarehouseReceiver,
    SignedSalesDelivery,
)

EXPECTED_CLASSIFICATIONS = {
    "BCNHDIY1047": "8a4fd639-b97c-4e6f-a89e-7e3a220ab3b8",
    "BCNHDIY914": "69b8fcc2-637d-4482-b481-d5b4394b8d6c",
    "BCNHDIY933": "2f3614e6-5d9a-4c79-b200-cd677161cf1b",
    "BCNHDIY951": "7b40987d-170a-4190-928d-b7dc387d95cf",
    "BCNHDIY952": "4ec1d1be-3d74-4bba-ab1d-b98590782e92",
    "BCNHDIY997": "19d3353f-7871-420c-abd3-3d2f9270b9ff",
}
EXPECTED_CODES = set(EXPECTED_CLASSIFICATIONS)
SCOPE = "local_only_no_external_send_no_paid_provider"
PROBE_SECRET = b"four-warehouse-sales-v2-acceptance"


def validate_acceptance(rows: list[dict[str, object]]) -> None:
    if {str(row.get("product_code", "")) for row in rows} != EXPECTED_CODES:
        raise ValueError("expected_product_set_mismatch")
    if len(rows) != len(EXPECTED_CODES):
        raise ValueError("expected_product_set_mismatch")
    if any(row.get("status") != "accepted" for row in rows):
        raise ValueError("sales_handoff_not_accepted")
    if any(not row.get("receiver_receipt_id") for row in rows):
        raise ValueError("receiver_receipt_missing")
    if any(
        row.get("receiver_receipt_id") != row.get("replay_receiver_receipt_id")
        for row in rows
    ):
        raise ValueError("receiver_receipt_replay_mismatch")


def build_receipt(
    rows: list[dict[str, object]],
    *,
    git_sha: str,
    base_url: str,
    negative_probes: dict[str, str] | None = None,
) -> dict[str, object]:
    validate_acceptance(rows)
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("non_loopback_base_url")
    if parsed.port is None:
        raise ValueError("base_url_port_missing")
    if len(git_sha) != 40:
        raise ValueError("invalid_git_sha")
    return {
        "schema_version": "sales-strategy-v2-handoff-acceptance-v1",
        "scope": SCOPE,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "server": {"base_url": base_url.rstrip("/"), "port": parsed.port},
        "products": sorted(rows, key=lambda row: str(row["product_code"])),
        "negative_probes": negative_probes or {},
        "summary": {
            "sales_delivered": len(rows),
            "style_delivered": 0,
            "production_deployed": False,
            "external_send": False,
        },
    }


def _http_json(url: str, *, method: str = "GET") -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, cast(dict[str, Any], json.loads(response.read()))
    except urllib.error.HTTPError as error:
        return error.code, cast(dict[str, Any], json.loads(error.read()))


def _require_success(status: int, payload: dict[str, Any], operation: str) -> None:
    if status < 200 or status >= 300:
        code = payload.get("error_code", "unknown")
        raise ValueError(f"{operation}_failed:{code}")


def collect_handoffs(base_url: str) -> tuple[list[dict[str, object]], dict[str, Any]]:
    rows: list[dict[str, object]] = []
    first_prepared: dict[str, Any] | None = None
    root = base_url.rstrip("/")
    for product_code, classification_id in sorted(EXPECTED_CLASSIFICATIONS.items()):
        status, prepared = _http_json(
            f"{root}/local/api/classification-versions/{classification_id}/prepare-sales-preview",
            method="POST",
        )
        _require_success(status, prepared, "prepare")
        if first_prepared is None:
            first_prepared = prepared
        handoff_id = str(prepared["handoff_id"])
        delivery_status, delivered = _http_json(
            f"{root}/local/api/warehouse-handoffs/{handoff_id}/deliver",
            method="POST",
        )
        _require_success(delivery_status, delivered, "deliver")
        replay_status, replay = _http_json(
            f"{root}/local/api/warehouse-handoffs/{handoff_id}/deliver",
            method="POST",
        )
        _require_success(replay_status, replay, "replay")
        read_status, restored = _http_json(
            f"{root}/local/api/warehouse-handoffs/{handoff_id}"
        )
        _require_success(read_status, restored, "read")
        preview = cast(dict[str, Any], restored["preview"])
        if str(preview["product_code"]) != product_code:
            raise ValueError("prepared_product_mismatch")
        if str(preview["source_classification_id"]) != classification_id:
            raise ValueError("prepared_classification_mismatch")
        if delivered.get("receiver_receipt_id") != restored.get("receiver_receipt_id"):
            raise ValueError("receiver_receipt_reload_mismatch")
        rows.append(
            {
                "product_code": product_code,
                "source_classification_id": classification_id,
                "source_classification_version": preview["source_classification_version"],
                "strategy_input_version": preview["strategy_input_version"],
                "content_sha256": preview["content_sha256"],
                "handoff_id": handoff_id,
                "status": restored["status"],
                "receiver_receipt_id": restored["receiver_receipt_id"],
                "replay_receiver_receipt_id": replay["receiver_receipt_id"],
                "payload_sha256": restored["payload_sha256"],
            }
        )
    if first_prepared is None:
        raise ValueError("expected_product_set_mismatch")
    return rows, first_prepared


class _RejectIfReachedRepository:
    async def accept(self, tenant_id: UUID, delivery: object) -> NoReturn:
        raise AssertionError("negative_probe_reached_repository")


class _StaleSourceRepository:
    async def load_latest_source(self, tenant_id: UUID, classification_id: UUID):
        return SimpleNamespace(is_latest=False)

    async def append_preview(self, candidate: object) -> NoReturn:
        raise AssertionError("stale_probe_appended_preview")


async def run_negative_probes(prepared: dict[str, Any]) -> dict[str, str]:
    tenant_id = UUID(str(prepared["preview"]["tenant_id"]))
    now = int(time.time())
    receiver = SalesWarehouseReceiver(
        _RejectIfReachedRepository(),  # type: ignore[arg-type]
        PROBE_SECRET,
        now=lambda: now,
    )
    base_raw = {
        "schema_version": "sales-warehouse-delivery-v1",
        "handoff_id": prepared["handoff_id"],
        "idempotency_key": prepared["idempotency_key"],
        "payload_sha256": prepared["payload_sha256"],
        "payload": prepared["preview"],
    }

    async def rejection(
        raw: dict[str, Any],
        *,
        signed_tenant: UUID = tenant_id,
        signature: str | None = None,
    ) -> str:
        body = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        supplied = signature or sign_request(
            PROBE_SECRET,
            now,
            "classification",
            signed_tenant,
            "POST",
            DELIVERY_PATH,
            body,
        )
        try:
            await receiver.receive(
                SignedSalesDelivery(body, signed_tenant, now, supplied)
            )
        except SalesReceiverFailure as error:
            return str(error)
        raise ValueError("negative_probe_unexpectedly_accepted")

    changed_digest = copy.deepcopy(base_raw)
    changed_digest["payload"]["content_sha256"] = "f" * 64
    changed_digest["payload_sha256"] = "f" * 64
    automatic = copy.deepcopy(base_raw)
    automatic["payload"]["automatic_quote_allowed"] = True
    other_tenant = UUID("99999999-9999-4999-8999-999999999999")

    try:
        await SalesPreviewPreparer(_StaleSourceRepository()).prepare(
            tenant_id,
            UUID(str(prepared["preview"]["source_classification_id"])),
        )
    except SalesPreviewFailure as error:
        stale_code = str(error)
    else:
        raise ValueError("stale_probe_unexpectedly_accepted")

    probes = {
        "stale_classification": stale_code,
        "changed_digest": await rejection(changed_digest),
        "wrong_tenant": await rejection(base_raw, signed_tenant=other_tenant),
        "invalid_signature": await rejection(base_raw, signature="0" * 64),
        "automatic_permission": await rejection(automatic),
    }
    expected = {
        "stale_classification": "classification_not_latest",
        "changed_digest": "sales_payload_hash_mismatch",
        "wrong_tenant": "sales_tenant_mismatch",
        "invalid_signature": "invalid_warehouse_signature",
        "automatic_permission": "sales_permission_boundary_violation",
    }
    if probes != expected:
        raise ValueError("negative_probe_mismatch")
    return probes


def _git_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("git_sha_unavailable")
    return result.stdout.strip()


def _write_immutable(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise ValueError("acceptance_receipt_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.writing")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    rows, prepared = collect_handoffs(arguments.base_url)
    probes = asyncio.run(run_negative_probes(prepared))
    receipt = build_receipt(
        rows,
        git_sha=_git_sha(repo_root),
        base_url=arguments.base_url,
        negative_probes=probes,
    )
    _write_immutable(arguments.output, receipt)
    print(json.dumps(receipt["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
