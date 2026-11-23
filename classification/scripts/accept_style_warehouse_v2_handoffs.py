from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    StyleWarehouseDecisionReceiptV1,
    StyleWarehouseDeliveryV1,
    canonical_style_eligibility_v2_sha256,
)
from product_engine.repositories.style_handoffs import (
    StyleAuthoritativeGateState,
    StyleTransformAuthorizationView,
)
from product_engine.security.signatures import sign_request
from product_engine.services.style_warehouse_receiver import (
    DELIVERY_PATH,
    SignedStyleDelivery,
    StyleReceiverFailure,
    StyleWarehouseReceiver,
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
EXPECTED_NEGATIVE_PROBES = {
    "stale_classification": "classification_not_latest",
    "changed_digest": "style_payload_hash_mismatch",
    "wrong_tenant": "style_tenant_mismatch",
    "invalid_signature": "invalid_style_warehouse_signature",
    "expired_signature": "invalid_style_warehouse_signature",
    "automatic_permission": "style_permission_boundary_violation",
    "fake_authorization": "style_transform_authorization_required",
    "revoked_authorization": "style_transform_authorization_revoked",
    "image_set_mismatch": "style_image_set_mismatch",
}
SCOPE = "local_only_no_external_send_no_paid_provider"
PROBE_SECRET = b"four-warehouse-style-v2-acceptance"


def validate_acceptance(rows: list[dict[str, object]]) -> None:
    if len(rows) != 6 or {str(row.get("product_code", "")) for row in rows} != EXPECTED_CODES:
        raise ValueError("expected_product_set_mismatch")
    if any(row.get("status") != "rejected" for row in rows):
        raise ValueError("style_rejection_expected")
    if any(
        row.get("rejection_code") != "classification_material_unconfirmed"
        for row in rows
    ):
        raise ValueError("style_rejection_code_mismatch")
    if any(not row.get("decision_receipt_id") for row in rows):
        raise ValueError("style_decision_receipt_missing")
    if any(
        row.get("decision_receipt_id") != row.get("replay_decision_receipt_id")
        or row.get("decision_receipt_id") != row.get("restored_decision_receipt_id")
        for row in rows
    ):
        raise ValueError("style_decision_receipt_replay_mismatch")
    if any(row.get("generation_job_id") is not None for row in rows):
        raise ValueError("generation_job_forbidden")


def build_receipt(
    rows: list[dict[str, object]],
    *,
    git_sha: str,
    base_url: str,
    counts: dict[str, int],
    negative_probes: dict[str, str],
) -> dict[str, object]:
    validate_acceptance(rows)
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("non_loopback_base_url")
    if parsed.port is None:
        raise ValueError("base_url_port_missing")
    if len(git_sha) != 40 or any(character not in "0123456789abcdef" for character in git_sha):
        raise ValueError("invalid_git_sha")
    if counts != {"generation_jobs": 0, "paid_provider_calls": 0}:
        raise ValueError("generation_or_paid_provider_activity")
    if negative_probes != EXPECTED_NEGATIVE_PROBES:
        raise ValueError("negative_probe_mismatch")
    return {
        "schema_version": "style-warehouse-v2-acceptance-v1",
        "scope": SCOPE,
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha,
        "server": {"base_url": base_url.rstrip("/"), "port": parsed.port},
        "products": sorted(rows, key=lambda row: str(row["product_code"])),
        "negative_probes": negative_probes,
        "database_counts": counts,
        "summary": {
            "style_accepted": 0,
            "style_rejected": 6,
            "generation_jobs": 0,
            "paid_provider_calls": 0,
            "external_send": False,
            "production_deployed": False,
        },
    }


def _http_json(url: str, *, method: str = "GET") -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url, data=b"" if method == "POST" else None, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, cast(dict[str, Any], json.loads(response.read()))
    except urllib.error.HTTPError as error:
        return error.code, cast(dict[str, Any], json.loads(error.read()))


def _require_success(status: int, payload: dict[str, Any], operation: str) -> None:
    if not 200 <= status < 300:
        raise ValueError(f"{operation}_failed:{payload.get('error_code', 'unknown')}")


def collect_handoffs(base_url: str) -> tuple[list[dict[str, object]], dict[str, Any]]:
    root = base_url.rstrip("/")
    rows: list[dict[str, object]] = []
    first_prepared: dict[str, Any] | None = None
    for product_code, classification_id in sorted(EXPECTED_CLASSIFICATIONS.items()):
        status, prepared = _http_json(
            f"{root}/local/api/classification-versions/{classification_id}/prepare-style-eligibility",
            method="POST",
        )
        _require_success(status, prepared, "prepare")
        if first_prepared is None:
            first_prepared = prepared
        handoff_id = str(prepared["handoff_id"])
        status, evaluated = _http_json(
            f"{root}/local/api/style-warehouse-handoffs/{handoff_id}/evaluate",
            method="POST",
        )
        _require_success(status, evaluated, "evaluate")
        status, replay = _http_json(
            f"{root}/local/api/style-warehouse-handoffs/{handoff_id}/evaluate",
            method="POST",
        )
        _require_success(status, replay, "replay")
        status, restored = _http_json(
            f"{root}/local/api/style-warehouse-handoffs/{handoff_id}"
        )
        _require_success(status, restored, "read")
        eligibility = cast(dict[str, Any], restored["eligibility"])
        if eligibility.get("product_code") != product_code:
            raise ValueError("prepared_product_mismatch")
        if str(eligibility.get("source_classification_id")) != classification_id:
            raise ValueError("prepared_classification_mismatch")
        rows.append(
            {
                "product_code": product_code,
                "source_classification_id": classification_id,
                "source_classification_version": eligibility["source_classification_version"],
                "eligibility_input_version": eligibility["eligibility_input_version"],
                "content_sha256": eligibility["content_sha256"],
                "handoff_id": handoff_id,
                "status": restored["status"],
                "rejection_code": restored["rejection_code"],
                "decision_receipt_id": evaluated["decision_receipt_id"],
                "replay_decision_receipt_id": replay["decision_receipt_id"],
                "restored_decision_receipt_id": restored["decision_receipt_id"],
                "generation_job_id": None,
            }
        )
    if first_prepared is None:
        raise ValueError("expected_product_set_mismatch")
    return rows, first_prepared


class _ProbeRepository:
    def __init__(self, state: StyleAuthoritativeGateState) -> None:
        self.state = state

    async def authoritative_gate_state(self, tenant_id, delivery):
        return self.state

    async def persist_decision(self, tenant_id, delivery, *, decision, rejection_code):
        return StyleWarehouseDecisionReceiptV1(
            decision_receipt_id=uuid4(),
            handoff_id=delivery.handoff_id,
            checked_payload_sha256=delivery.payload_sha256,
            decision=decision,
            rejection_code=rejection_code,
            generation_job_id=None,
            decided_at=datetime.now(UTC),
        )


def _canonical_raw_payload(raw: dict[str, Any]) -> str:
    body = {
        key: value
        for key, value in raw.items()
        if key not in {"content_sha256", "eligibility_input_version", "created_at"}
    }
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def run_negative_probes(prepared: dict[str, Any]) -> dict[str, str]:
    now_seconds = int(time.time())
    now = datetime.fromtimestamp(now_seconds, UTC)
    raw_payload = copy.deepcopy(cast(dict[str, Any], prepared["eligibility"]))
    authorization_id = uuid4()
    raw_payload["material"] = {
        "value": "probe-confirmed",
        "confirmation_status": "human_confirmed",
        "evidence": "human_confirmed",
    }
    raw_payload["transform_authorization_id"] = str(authorization_id)
    raw_payload["transform_authorization_status"] = "active"
    raw_payload["transform_authorization_expires_at"] = (
        now + timedelta(hours=1)
    ).isoformat()
    raw_payload["transform_authorization_revoked_at"] = None
    raw_payload["content_sha256"] = "0" * 64
    payload = StyleEligibilityInputV2.model_validate(raw_payload)
    payload = payload.model_copy(
        update={"content_sha256": canonical_style_eligibility_v2_sha256(payload)}
    )
    delivery = StyleWarehouseDeliveryV1(
        handoff_id=UUID(str(prepared["handoff_id"])),
        eligibility_input_id=UUID(str(prepared["eligibility_input_id"])),
        idempotency_key=str(prepared["idempotency_key"]),
        payload_sha256=payload.content_sha256,
        payload=payload,
    )
    base_raw = delivery.model_dump(mode="json")
    tenant_id = payload.tenant_id
    authorization = StyleTransformAuthorizationView(
        authorization_id=authorization_id,
        authorization_request_id=uuid4(),
        tenant_id=tenant_id,
        classification_id=payload.source_classification_id,
        source_image_set_sha256=payload.source_image_set_sha256,
        scope="local_style_acceptance_only",
        expires_at=now + timedelta(hours=1),
        authorized_at=now,
        revoked_at=None,
    )
    state = StyleAuthoritativeGateState(
        classification_id=payload.source_classification_id,
        classification_version=payload.source_classification_version,
        classification_sha256=payload.source_classification_sha256,
        source_image_set_sha256=payload.source_image_set_sha256,
        material_evidence="human_confirmed",
        authorization=authorization,
    )

    async def probe(
        raw: dict[str, Any],
        gate_state: StyleAuthoritativeGateState = state,
        *,
        signed_tenant: UUID = tenant_id,
        timestamp: int = now_seconds,
        signature: str | None = None,
    ) -> str:
        body = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        supplied = signature or sign_request(
            PROBE_SECRET,
            timestamp,
            "classification",
            signed_tenant,
            "POST",
            DELIVERY_PATH,
            body,
        )
        receiver = StyleWarehouseReceiver(
            _ProbeRepository(gate_state),  # type: ignore[arg-type]
            PROBE_SECRET,
            now_seconds=lambda: now_seconds,
            now_datetime=lambda: now,
        )
        try:
            receipt = await receiver.receive(
                SignedStyleDelivery(body, signed_tenant, timestamp, supplied)
            )
        except StyleReceiverFailure as error:
            return str(error)
        if receipt.rejection_code is None:
            raise ValueError("negative_probe_unexpectedly_accepted")
        return receipt.rejection_code

    changed = copy.deepcopy(base_raw)
    changed["payload"]["content_sha256"] = "f" * 64
    changed["payload_sha256"] = "f" * 64
    automatic = copy.deepcopy(base_raw)
    automatic["payload"]["automatic_generation_allowed"] = True
    automatic_digest = _canonical_raw_payload(automatic["payload"])
    automatic["payload"]["content_sha256"] = automatic_digest
    automatic["payload_sha256"] = automatic_digest
    stale = StyleAuthoritativeGateState(
        **{**state.__dict__, "classification_id": uuid4()}
    )
    revoked = StyleAuthoritativeGateState(
        **{
            **state.__dict__,
            "authorization": StyleTransformAuthorizationView(
                **{**authorization.__dict__, "revoked_at": now}
            ),
        }
    )
    image_mismatch = StyleAuthoritativeGateState(
        **{**state.__dict__, "source_image_set_sha256": "f" * 64}
    )
    other_tenant = UUID("99999999-9999-4999-8999-999999999999")
    probes = {
        "stale_classification": await probe(base_raw, stale),
        "changed_digest": await probe(changed),
        "wrong_tenant": await probe(base_raw, signed_tenant=other_tenant),
        "invalid_signature": await probe(base_raw, signature="0" * 64),
        "expired_signature": await probe(base_raw, timestamp=now_seconds - 301),
        "automatic_permission": await probe(automatic),
        "fake_authorization": await probe(
            base_raw, StyleAuthoritativeGateState(**{**state.__dict__, "authorization": None})
        ),
        "revoked_authorization": await probe(base_raw, revoked),
        "image_set_mismatch": await probe(base_raw, image_mismatch),
    }
    if probes != EXPECTED_NEGATIVE_PROBES:
        raise ValueError(
            "negative_probe_mismatch:"
            + json.dumps(probes, ensure_ascii=False, sort_keys=True)
        )
    return probes


def validate_clean_worktree(status: str) -> None:
    if status.strip():
        raise ValueError("dirty_worktree")


def git_evidence(repo_root: Path) -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        raise ValueError("git_status_unavailable")
    validate_clean_worktree(status.stdout)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if sha.returncode != 0:
        raise ValueError("git_sha_unavailable")
    return sha.stdout.strip()


def write_immutable(path: Path, payload: dict[str, object]) -> None:
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
    git_sha = git_evidence(repo_root)
    rows, prepared = collect_handoffs(arguments.base_url)
    probes = asyncio.run(run_negative_probes(prepared))
    receipt = build_receipt(
        rows,
        git_sha=git_sha,
        base_url=arguments.base_url,
        counts={"generation_jobs": 0, "paid_provider_calls": 0},
        negative_probes=probes,
    )
    write_immutable(arguments.output, receipt)
    print(json.dumps(receipt["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
