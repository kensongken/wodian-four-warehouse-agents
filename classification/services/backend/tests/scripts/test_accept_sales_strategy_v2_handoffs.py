from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from product_engine.contracts.sales_strategy_v2 import (
    SalesStrategyInputV2,
    canonical_sales_strategy_v2_sha256,
)

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/accept_sales_strategy_v2_handoffs.py"


def load_script():
    spec = importlib.util.spec_from_file_location("sales_v2_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def accepted_handoff(
    *,
    product_code: str,
    receiver_receipt_id: str | None = "88888888-8888-4888-8888-888888888888",
) -> dict[str, object]:
    return {
        "product_code": product_code,
        "source_classification_id": "66666666-6666-4666-8666-666666666666",
        "source_classification_version": 1,
        "strategy_input_version": 1,
        "content_sha256": "a" * 64,
        "handoff_id": f"handoff-{product_code}",
        "status": "accepted",
        "receiver_receipt_id": receiver_receipt_id,
        "replay_receiver_receipt_id": receiver_receipt_id,
    }


def all_rows(module) -> list[dict[str, object]]:
    return [accepted_handoff(product_code=code) for code in sorted(module.EXPECTED_CODES)]


def test_receipt_rejects_missing_receiver_receipt() -> None:
    module = load_script()
    rows = all_rows(module)
    rows[0]["receiver_receipt_id"] = None

    with pytest.raises(ValueError, match="receiver_receipt_missing"):
        module.build_receipt(rows, git_sha="a" * 40, base_url="http://127.0.0.1:18037")


def test_receipt_requires_all_six_expected_product_codes() -> None:
    module = load_script()

    with pytest.raises(ValueError, match="expected_product_set_mismatch"):
        module.build_receipt(
            [accepted_handoff(product_code="BCNHDIY914")],
            git_sha="a" * 40,
            base_url="http://127.0.0.1:18037",
        )


def test_receipt_rejects_changed_replay_receipt() -> None:
    module = load_script()
    rows = all_rows(module)
    rows[0]["replay_receiver_receipt_id"] = "changed"

    with pytest.raises(ValueError, match="receiver_receipt_replay_mismatch"):
        module.build_receipt(rows, git_sha="a" * 40, base_url="http://127.0.0.1:18037")


def test_receipt_states_local_scope_and_counts() -> None:
    module = load_script()

    receipt = module.build_receipt(
        all_rows(module),
        git_sha="a" * 40,
        base_url="http://127.0.0.1:18037",
        negative_probes={"stale_classification": "classification_not_latest"},
    )

    assert receipt["scope"] == "local_only_no_external_send_no_paid_provider"
    assert receipt["summary"] == {
        "sales_delivered": 6,
        "style_delivered": 0,
        "production_deployed": False,
        "external_send": False,
    }
    assert receipt["server"]["port"] == 18037


@pytest.mark.asyncio
async def test_negative_probes_reject_every_forbidden_boundary() -> None:
    module = load_script()
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    classification_id = UUID("66666666-6666-4666-8666-666666666666")
    payload = SalesStrategyInputV2(
        tenant_id=tenant_id,
        product_code="BCNHDIY914",
        strategy_input_version=1,
        source_material_package_version=1,
        source_classification_id=classification_id,
        source_classification_version=1,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        images=(
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={"value": "信息不足", "evidence": "information_insufficient"},
        form={"value": "earring", "evidence": "human_confirmed"},
        unverified_items=("材质待人工确认",),
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
        content_sha256="0" * 64,
    )
    payload = payload.model_copy(
        update={"content_sha256": canonical_sales_strategy_v2_sha256(payload)}
    )
    prepared = {
        "handoff_id": "77777777-7777-4777-8777-777777777777",
        "idempotency_key": f"sales:{tenant_id}:{classification_id}",
        "payload_sha256": payload.content_sha256,
        "preview": payload.model_dump(mode="json"),
    }

    assert await module.run_negative_probes(prepared) == {
        "stale_classification": "classification_not_latest",
        "changed_digest": "sales_payload_hash_mismatch",
        "wrong_tenant": "sales_tenant_mismatch",
        "invalid_signature": "invalid_warehouse_signature",
        "automatic_permission": "sales_permission_boundary_violation",
    }
