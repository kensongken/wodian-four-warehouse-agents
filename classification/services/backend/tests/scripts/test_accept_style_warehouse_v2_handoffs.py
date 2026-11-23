from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from product_engine.contracts.style_warehouse_v2 import (
    StyleEligibilityInputV2,
    canonical_style_eligibility_v2_sha256,
)

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/accept_style_warehouse_v2_handoffs.py"


def load_script():
    spec = importlib.util.spec_from_file_location("style_v2_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rejected_row(product_code: str, receipt_id: str | None = None):
    receipt = receipt_id or f"receipt-{product_code}"
    return {
        "product_code": product_code,
        "source_classification_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "source_classification_version": 1,
        "eligibility_input_version": 1,
        "content_sha256": "a" * 64,
        "handoff_id": f"handoff-{product_code}",
        "status": "rejected",
        "rejection_code": "classification_material_unconfirmed",
        "decision_receipt_id": receipt,
        "replay_decision_receipt_id": receipt,
        "restored_decision_receipt_id": receipt,
        "generation_job_id": None,
    }


def all_rows(module):
    return [rejected_row(code) for code in sorted(module.EXPECTED_CODES)]


def metadata(module):
    return {
        "git_sha": "a" * 40,
        "base_url": "http://127.0.0.1:18038",
        "counts": {"generation_jobs": 0, "paid_provider_calls": 0},
        "negative_probes": module.EXPECTED_NEGATIVE_PROBES,
    }


def test_receipt_requires_six_rejections_and_zero_generation() -> None:
    module = load_script()
    receipt = module.build_receipt(all_rows(module), **metadata(module))

    assert receipt["summary"] == {
        "style_accepted": 0,
        "style_rejected": 6,
        "generation_jobs": 0,
        "paid_provider_calls": 0,
        "external_send": False,
        "production_deployed": False,
    }


def test_receipt_rejects_missing_or_changed_replay_receipt() -> None:
    module = load_script()
    rows = all_rows(module)
    rows[0]["replay_decision_receipt_id"] = "changed"

    with pytest.raises(ValueError, match="style_decision_receipt_replay_mismatch"):
        module.build_receipt(rows, **metadata(module))


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda rows: rows.pop(), "expected_product_set_mismatch"),
        (lambda rows: rows[0].update(status="accepted"), "style_rejection_expected"),
        (
            lambda rows: rows[0].update(rejection_code="other"),
            "style_rejection_code_mismatch",
        ),
        (lambda rows: rows[0].update(generation_job_id="job"), "generation_job_forbidden"),
    ],
)
def test_receipt_fails_closed_on_product_mutations(mutation, code) -> None:
    module = load_script()
    rows = all_rows(module)
    mutation(rows)
    with pytest.raises(ValueError, match=code):
        module.build_receipt(rows, **metadata(module))


def test_receipt_rejects_nonzero_calls_bad_probes_and_non_loopback() -> None:
    module = load_script()
    values = metadata(module)
    values["counts"] = {"generation_jobs": 1, "paid_provider_calls": 0}
    with pytest.raises(ValueError, match="generation_or_paid_provider_activity"):
        module.build_receipt(all_rows(module), **values)

    values = metadata(module)
    values["negative_probes"] = {}
    with pytest.raises(ValueError, match="negative_probe_mismatch"):
        module.build_receipt(all_rows(module), **values)

    values = metadata(module)
    values["base_url"] = "https://example.com"
    with pytest.raises(ValueError, match="non_loopback_base_url"):
        module.build_receipt(all_rows(module), **values)


def test_clean_sha_and_immutable_output_are_required(tmp_path: Path) -> None:
    module = load_script()
    with pytest.raises(ValueError, match="dirty_worktree"):
        module.validate_clean_worktree(" M changed.py\n")
    with pytest.raises(ValueError, match="invalid_git_sha"):
        module.build_receipt(all_rows(module), **{**metadata(module), "git_sha": "short"})

    output = tmp_path / "receipt.json"
    module.write_immutable(output, {"ok": True})
    with pytest.raises(ValueError, match="acceptance_receipt_already_exists"):
        module.write_immutable(output, {"ok": True})


@pytest.mark.asyncio
async def test_negative_probes_hit_every_expected_boundary() -> None:
    module = load_script()
    tenant_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    classification_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    eligibility_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    handoff_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    value = StyleEligibilityInputV2(
        tenant_id=tenant_id,
        product_code="BCNHDIY914",
        eligibility_input_version=1,
        source_material_package_version=1,
        source_classification_id=classification_id,
        source_classification_version=1,
        source_classification_sha256="a" * 64,
        source_image_set_sha256="b" * 64,
        main_image={
            "asset_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
            "role": "main_candidate",
        },
        images=(
            {
                "asset_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                "sha256": "c" * 64,
                "mime_type": "image/jpeg",
                "role": "main_candidate",
            },
        ),
        material={"value": "信息不足", "evidence": "information_insufficient"},
        form={"value": "earring", "evidence": "human_confirmed"},
        transform_authorization_status="none",
        created_at=datetime(2026, 9, 20, tzinfo=UTC),
        content_sha256="0" * 64,
    )
    value = value.model_copy(
        update={"content_sha256": canonical_style_eligibility_v2_sha256(value)}
    )
    prepared = {
        "handoff_id": str(handoff_id),
        "eligibility_input_id": str(eligibility_id),
        "idempotency_key": f"style:{tenant_id}:{eligibility_id}",
        "eligibility": value.model_dump(mode="json"),
    }

    assert await module.run_negative_probes(prepared) == (module.EXPECTED_NEGATIVE_PROBES)
