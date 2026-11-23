import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/verify_employee_formal_handoff.py"
SPEC = importlib.util.spec_from_file_location("verify_employee_formal_handoff", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

AcceptanceSnapshot = MODULE.AcceptanceSnapshot
verify = MODULE.verify

EXPECTED_FORMS = {
    "BCNHDIY914": "earring",
    "BCNHDIY933": "unknown",
    "BCNHDIY951": "necklace",
    "BCNHDIY952": "bracelet",
    "BCNHDIY997": "bracelet",
    "BCNHDIY1047": "unknown",
}


def accepted_snapshot(**updates):
    products = tuple(
        {
            "product_code": code,
            "package_version": index,
            "content_sha256": f"{index}" * 64,
        }
        for index, code in enumerate(EXPECTED_FORMS, start=1)
    )
    classifications = tuple(
        {
            "product_code": code,
            "classification_version": 1,
            "classification_id": f"classification-{index}",
            "material_label": "信息不足",
            "product_form": form,
            "downstream_status": "not_sent",
        }
        for index, (code, form) in enumerate(EXPECTED_FORMS.items(), start=1)
    )
    asset_checks = tuple(
        {
            "product_code": code,
            "asset_id": f"asset-{index}",
            "expected_sha256": "b" * 64,
            "actual_sha256": "b" * 64,
            "matched": True,
        }
        for index, code in enumerate((code for code in EXPECTED_FORMS for _ in range(2)), start=1)
    )
    values = {
        "git_sha": "a" * 40,
        "batch_id": "BATCH-20260921035620-3A284AA4",
        "forms": EXPECTED_FORMS,
        "material_package_receipts": products,
        "classification_receipts": classifications,
        "asset_checks": asset_checks,
        "promotion_replay_exact": True,
        "classification_replay_exact": True,
        "postgres_material_package_count": 6,
        "postgres_classification_count": 6,
        "sales_outbox_count": 0,
        "style_outbox_count": 0,
        "outbound_delivery_count": 0,
        "paid_call_count": 0,
        "generated_image_count": 0,
    }
    values.update(updates)
    return AcceptanceSnapshot(**values)


def test_verifier_requires_exact_six_product_shape() -> None:
    snapshot = accepted_snapshot(
        material_package_receipts=accepted_snapshot().material_package_receipts[:5]
    )

    result = verify(snapshot)

    assert result.ok is False
    assert result.error_code == "employee_acceptance_product_count_mismatch"


def test_verifier_rejects_unknown_products_auto_changed_from_unknown() -> None:
    forms = {**EXPECTED_FORMS, "BCNHDIY933": "bracelet", "BCNHDIY1047": "ring"}

    result = verify(accepted_snapshot(forms=forms))

    assert result.ok is False
    assert result.error_code == "employee_acceptance_form_mismatch"


def test_verifier_requires_zero_automatic_downstream_effects() -> None:
    result = verify(accepted_snapshot(sales_outbox_count=1))

    assert result.ok is False
    assert result.error_code == "employee_acceptance_downstream_side_effect"


def test_run_record_contains_fixed_sha_and_all_receipts() -> None:
    result = verify(accepted_snapshot())

    assert result.ok is True
    record = result.run_record
    assert record["git_sha"] == "a" * 40
    assert len(record["material_package_receipts"]) == 6
    assert len(record["classification_receipts"]) == 6
    assert len(record["asset_checks"]) == 12
    assert record["paid_call_count"] == 0
    assert record["generated_image_count"] == 0


def test_verifier_rejects_hash_or_replay_mismatch() -> None:
    checks = list(accepted_snapshot().asset_checks)
    checks[4] = {**checks[4], "actual_sha256": "c" * 64, "matched": False}

    hash_result = verify(accepted_snapshot(asset_checks=tuple(checks)))
    replay_result = verify(accepted_snapshot(classification_replay_exact=False))

    assert hash_result.error_code == "employee_acceptance_asset_hash_mismatch"
    assert replay_result.error_code == "employee_acceptance_replay_mismatch"
