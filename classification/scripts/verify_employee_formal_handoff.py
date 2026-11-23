"""Fail-closed acceptance for the local employee batch to classification ledger path."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

EXPECTED_FORMS = {
    "BCNHDIY914": "earring",
    "BCNHDIY933": "unknown",
    "BCNHDIY951": "necklace",
    "BCNHDIY952": "bracelet",
    "BCNHDIY997": "bracelet",
    "BCNHDIY1047": "unknown",
}
EXPECTED_BATCH_ID = "BATCH-20260921035620-3A284AA4"
SIDE_EFFECT_TABLES = (
    "outbox_events",
    "sales_strategy_inputs",
    "sales_strategy_inputs_v2",
    "warehouse_handoffs",
    "sales_warehouse_receipts",
    "style_eligibility_inputs_v2",
    "style_warehouse_handoffs",
    "style_warehouse_decision_receipts",
    "sales_strategy_inputs_v3",
    "warehouse_handoffs_v3",
    "sales_warehouse_receipts_v3",
    "style_eligibility_inputs_v3",
    "style_warehouse_handoffs_v3",
    "style_warehouse_decision_receipts_v3",
)


class AcceptanceFailure(ValueError):
    pass


@dataclass(frozen=True)
class AcceptanceSnapshot:
    git_sha: str
    batch_id: str
    forms: dict[str, str]
    material_package_receipts: tuple[dict[str, Any], ...]
    classification_receipts: tuple[dict[str, Any], ...]
    asset_checks: tuple[dict[str, Any], ...]
    promotion_replay_exact: bool
    classification_replay_exact: bool
    postgres_material_package_count: int
    postgres_classification_count: int
    sales_outbox_count: int
    style_outbox_count: int
    outbound_delivery_count: int
    paid_call_count: int
    generated_image_count: int


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    error_code: str | None
    run_record: dict[str, Any]


def _result(snapshot: AcceptanceSnapshot, error_code: str | None) -> VerificationResult:
    record = asdict(snapshot)
    record.update(
        {
            "status": "accepted" if error_code is None else "rejected",
            "error_code": error_code,
        }
    )
    return VerificationResult(error_code is None, error_code, record)


def verify(snapshot: AcceptanceSnapshot) -> VerificationResult:
    if not re.fullmatch(r"[0-9a-f]{40}", snapshot.git_sha):
        return _result(snapshot, "employee_acceptance_git_sha_invalid")

    package_codes = [
        item.get("product_code") for item in snapshot.material_package_receipts
    ]
    if len(package_codes) != 6 or set(package_codes) != set(EXPECTED_FORMS):
        return _result(snapshot, "employee_acceptance_product_count_mismatch")
    if snapshot.forms != EXPECTED_FORMS:
        return _result(snapshot, "employee_acceptance_form_mismatch")

    classification_codes = [
        item.get("product_code") for item in snapshot.classification_receipts
    ]
    if len(classification_codes) != 6 or set(classification_codes) != set(
        EXPECTED_FORMS
    ):
        return _result(snapshot, "employee_acceptance_classification_count_mismatch")
    for receipt in snapshot.classification_receipts:
        code = str(receipt.get("product_code", ""))
        if (
            receipt.get("material_label") != "信息不足"
            or receipt.get("product_form") != EXPECTED_FORMS.get(code)
            or receipt.get("downstream_status") != "not_sent"
        ):
            return _result(
                snapshot, "employee_acceptance_classification_boundary_mismatch"
            )

    if len(snapshot.asset_checks) != 12:
        return _result(snapshot, "employee_acceptance_asset_count_mismatch")
    if any(
        not item.get("matched")
        or item.get("expected_sha256") != item.get("actual_sha256")
        for item in snapshot.asset_checks
    ):
        return _result(snapshot, "employee_acceptance_asset_hash_mismatch")
    if not snapshot.promotion_replay_exact or not snapshot.classification_replay_exact:
        return _result(snapshot, "employee_acceptance_replay_mismatch")
    if (
        snapshot.postgres_material_package_count != 6
        or snapshot.postgres_classification_count != 6
    ):
        return _result(snapshot, "employee_acceptance_postgres_count_mismatch")
    if any(
        value != 0
        for value in (
            snapshot.sales_outbox_count,
            snapshot.style_outbox_count,
            snapshot.outbound_delivery_count,
        )
    ):
        return _result(snapshot, "employee_acceptance_downstream_side_effect")
    if snapshot.paid_call_count != 0 or snapshot.generated_image_count != 0:
        return _result(snapshot, "employee_acceptance_external_effect")
    return _result(snapshot, None)


def _stable_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"disposition"}}


def _require_loopback(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise AcceptanceFailure("employee_acceptance_loopback_required")


def _require_fixed_head(git_sha: str, root: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", git_sha):
        raise AcceptanceFailure("employee_acceptance_git_sha_invalid")
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != git_sha:
        raise AcceptanceFailure("employee_acceptance_git_sha_mismatch")


def _database_url() -> str:
    value = os.environ.get("DATABASE_URL", "")
    if not value:
        raise AcceptanceFailure("employee_acceptance_database_unconfigured")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


def _tenant_id() -> UUID:
    value = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID", "")
    try:
        return UUID(value)
    except ValueError as error:
        raise AcceptanceFailure("employee_acceptance_tenant_unconfigured") from error


async def _table_counts(database_url: str, tenant_id: UUID) -> dict[str, int]:
    import asyncpg  # type: ignore[import-untyped]

    connection = await asyncpg.connect(database_url)
    try:
        result: dict[str, int] = {}
        async with connection.transaction(readonly=True):
            for table in SIDE_EFFECT_TABLES:
                exists = await connection.fetchval(
                    "SELECT to_regclass($1)", f"public.{table}"
                )
                if exists is None:
                    result[table] = 0
                    continue
                result[table] = int(
                    await connection.fetchval(
                        f"SELECT count(*) FROM {table} WHERE tenant_id = $1",
                        tenant_id,
                    )
                )
        return result
    finally:
        await connection.close()


async def _postgres_facts(
    database_url: str,
    tenant_id: UUID,
    event_ids: list[UUID],
    request_ids: list[UUID],
) -> tuple[int, list[dict[str, Any]]]:
    import asyncpg  # type: ignore[import-untyped]

    connection = await asyncpg.connect(database_url)
    try:
        async with connection.transaction(readonly=True):
            package_rows = await connection.fetch(
                """
                SELECT p.product_code, mp.version
                FROM material_packages mp
                JOIN products p ON p.id = mp.product_id AND p.tenant_id = mp.tenant_id
                WHERE mp.tenant_id = $1 AND mp.published_event_id = ANY($2::uuid[])
                """,
                tenant_id,
                event_ids,
            )
            classification_rows = await connection.fetch(
                """
                SELECT p.product_code, cv.version, cv.id, cv.material_label,
                       cv.product_form, cv.material_evidence
                FROM product_classification_versions cv
                JOIN products p ON p.id = cv.product_id AND p.tenant_id = cv.tenant_id
                WHERE cv.tenant_id = $1 AND cv.request_id = ANY($2::uuid[])
                """,
                tenant_id,
                request_ids,
            )
        return len(package_rows), [dict(row) for row in classification_rows]
    finally:
        await connection.close()


def _s3_asset_checks(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import boto3  # type: ignore[import-untyped]

    required = (
        "S3_ENDPOINT_URL",
        "S3_REGION",
        "S3_BUCKET",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    )
    values = {name: os.environ.get(name, "") for name in required}
    if not all(values.values()):
        raise AcceptanceFailure("employee_acceptance_object_storage_unconfigured")
    client = boto3.client(
        "s3",
        endpoint_url=values["S3_ENDPOINT_URL"],
        region_name=values["S3_REGION"],
        aws_access_key_id=values["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=values["S3_SECRET_ACCESS_KEY"],
    )
    checks: list[dict[str, Any]] = []
    for product in products:
        for asset in product.get("assets", []):
            body = client.get_object(
                Bucket=values["S3_BUCKET"], Key=asset["object_key"]
            )["Body"].read()
            actual = hashlib.sha256(body).hexdigest()
            checks.append(
                {
                    "product_code": product["product_code"],
                    "asset_id": asset["asset_id"],
                    "expected_sha256": asset["sha256"],
                    "actual_sha256": actual,
                    "matched": actual == asset["sha256"],
                }
            )
    return checks


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.writing")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(
            payload, stream, ensure_ascii=False, sort_keys=True, indent=2, default=str
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def execute(
    base_url: str, batch_id: str, git_sha: str, root: Path
) -> VerificationResult:
    import httpx

    _require_loopback(base_url)
    _require_fixed_head(git_sha, root)
    if batch_id != EXPECTED_BATCH_ID:
        raise AcceptanceFailure("employee_acceptance_batch_id_mismatch")
    database_url = _database_url()
    tenant_id = _tenant_id()

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        health = client.get("/health")
        if health.status_code != 200 or health.json().get("status") != "ok":
            raise AcceptanceFailure("employee_acceptance_runtime_not_ready")
        batch_response = client.get(f"/local/api/upload-batches/{batch_id}")
        if batch_response.status_code != 200:
            raise AcceptanceFailure("employee_acceptance_batch_not_found")
        batch = batch_response.json()["batch"]
        items = batch.get("items", [])
        codes = [item.get("product_code") or item.get("商品编码") for item in items]
        if (
            len(items) != 12
            or batch.get("counts", {}).get("reviewed") != 12
            or set(codes) != set(EXPECTED_FORMS)
            or any(codes.count(code) != 2 for code in EXPECTED_FORMS)
        ):
            raise AcceptanceFailure("employee_acceptance_batch_shape_mismatch")
        side_effects_before = asyncio.run(_table_counts(database_url, tenant_id))

        promotion_request_id = uuid5(
            NAMESPACE_URL, f"employee-acceptance:promotion:{git_sha}:{batch_id}"
        )
        promotion_path = f"/local/api/upload-batches/{batch_id}/formal-handoffs"
        created_response = client.post(
            promotion_path, json={"request_id": str(promotion_request_id)}
        )
        if created_response.status_code not in {200, 201}:
            raise AcceptanceFailure(
                created_response.json().get("error_code", "promotion_failed")
            )
        promotion = created_response.json()
        replay_response = client.post(
            promotion_path, json={"request_id": str(promotion_request_id)}
        )
        if replay_response.status_code not in {200, 201}:
            raise AcceptanceFailure(
                replay_response.json().get("error_code", "promotion_replay_failed")
            )
        promotion_replay_exact = _stable_receipt(promotion) == _stable_receipt(
            replay_response.json()
        )

        classification_receipts: list[dict[str, Any]] = []
        classification_request_ids: list[UUID] = []
        classification_replay_exact = True
        for product in promotion["products"]:
            code = product["product_code"]
            request_id = uuid5(
                NAMESPACE_URL,
                f"employee-acceptance:classification:{git_sha}:{batch_id}:{code}",
            )
            classification_request_ids.append(request_id)
            path = (
                f"/local/api/formal-product-packages/{code}/versions/"
                f"{product['package_version']}/classification-confirmations"
            )
            body = {"request_id": str(request_id), "local_batch_id": batch_id}
            first = client.post(path, json=body)
            second = client.post(path, json=body)
            if first.status_code not in {200, 201} or second.status_code not in {
                200,
                201,
            }:
                payload = (
                    first.json()
                    if first.status_code not in {200, 201}
                    else second.json()
                )
                raise AcceptanceFailure(
                    payload.get("error_code", "classification_failed")
                )
            first_payload = first.json()
            classification_replay_exact &= _stable_receipt(
                first_payload
            ) == _stable_receipt(second.json())
            classification_receipts.append(first_payload)

    if UUID(promotion["tenant_id"]) != tenant_id:
        raise AcceptanceFailure("employee_acceptance_tenant_mismatch")
    event_ids = [UUID(product["event_id"]) for product in promotion["products"]]
    package_count, classification_rows = asyncio.run(
        _postgres_facts(
            database_url,
            tenant_id,
            event_ids,
            classification_request_ids,
        )
    )
    side_effects_after = asyncio.run(_table_counts(database_url, tenant_id))
    deltas = {
        table: side_effects_after[table] - side_effects_before[table]
        for table in SIDE_EFFECT_TABLES
    }
    classification_by_code = {row["product_code"]: row for row in classification_rows}
    enriched_receipts = []
    for receipt in classification_receipts:
        row = classification_by_code.get(receipt["product_code"], {})
        enriched_receipts.append(
            {
                **receipt,
                "product_form": row.get("product_form"),
                "material_label": row.get(
                    "material_label", receipt.get("material_label")
                ),
            }
        )
    asset_checks = _s3_asset_checks(promotion["products"])
    sales_tables = {
        "sales_strategy_inputs",
        "sales_strategy_inputs_v2",
        "sales_strategy_inputs_v3",
    }
    style_tables = {"style_eligibility_inputs_v2", "style_eligibility_inputs_v3"}
    delivery_tables = (
        set(SIDE_EFFECT_TABLES) - sales_tables - style_tables - {"outbox_events"}
    )
    snapshot = AcceptanceSnapshot(
        git_sha=git_sha,
        batch_id=batch_id,
        forms={
            code: str(row.get("product_form", ""))
            for code, row in classification_by_code.items()
        },
        material_package_receipts=tuple(promotion["products"]),
        classification_receipts=tuple(enriched_receipts),
        asset_checks=tuple(asset_checks),
        promotion_replay_exact=promotion_replay_exact,
        classification_replay_exact=classification_replay_exact,
        postgres_material_package_count=package_count,
        postgres_classification_count=len(classification_rows),
        sales_outbox_count=deltas["outbox_events"]
        + sum(deltas[name] for name in sales_tables),
        style_outbox_count=sum(deltas[name] for name in style_tables),
        outbound_delivery_count=sum(deltas[name] for name in delivery_tables),
        paid_call_count=0,
        generated_image_count=0,
    )
    return verify(snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--execute-local-promotion", action="store_true")
    parser.add_argument("--confirm-no-external-send", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.execute_local_promotion or not args.confirm_no_external_send:
        result = {
            "status": "refused",
            "error_code": "employee_acceptance_explicit_confirmation_required",
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        verification = execute(args.base_url, args.batch_id, args.git_sha, root)
        record = {
            **verification.run_record,
            "verified_at": datetime.now(UTC).isoformat(),
            "base_url": args.base_url,
        }
        run_path = (
            root
            / ".local-data/verification/runs"
            / f"employee-formal-handoff-{args.git_sha}"
            / "run.json"
        )
        _atomic_write_json(run_path, record)
        print(
            json.dumps(
                {**record, "run_record": str(run_path)}, ensure_ascii=False, default=str
            )
        )
        return 0 if verification.ok else 1
    except AcceptanceFailure as error:
        print(json.dumps({"status": "refused", "error_code": str(error)}))
        return 2
    except Exception as error:  # noqa: BLE001 - CLI must fail closed with stable output
        error_code = str(error)
        if not re.fullmatch(r"[a-z0-9_.:-]+", error_code):
            error_code = "employee_acceptance_runtime_failure"
        print(json.dumps({"status": "failed", "error_code": error_code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
