import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "check_local_pilot_release.py"


def complete_training_version() -> dict[str, object]:
    return {
        "sha256": "a" * 64,
        "main_image_template_code": "LOCAL-GREEN-BRACELET",
        "product_type": "first_sale_bead",
        "inventory_status": "in_stock",
        "material_label": "绿松",
        "product_form": "手串",
        "visual_observations": ["绿色调明显"],
        "selling_points": ["近景实拍展示珠体轮廓"],
        "specification_note": "珠径 5 mm；尺寸由训练员测量",
        "quality_note": "图片可见颜色分布较均匀，仅为观察记录",
        "defect_note": "图片可见轻微表面纹理",
        "price_range": {
            "currency": "CNY",
            "minimum": "1200.00",
            "maximum": "1500.00",
            "basis": "训练员录入的同品类价格标准",
        },
        "price_confirmation": {
            "standard_code": "PRICE-TURQUOISE-BRACELET-001",
            "anchor_version": 1,
            "anchor_confirmed_by": "synthetic-trainer-001",
            "anchor_confirmed_at": "2026-08-30T07:30:00Z",
            "confirmed_by": "synthetic-trainer-002",
            "confirmed_at": "2026-08-30T08:00:00Z",
            "candidate_contract_version": "0.1-candidate",
            "automatic_publication_allowed": False,
        },
        "service_rules": [
            {
                "service_code": "scan_identity",
                "eligibility": "购买后可用",
                "customer_summary": "购买后可扫码进入身份与服务页面",
            }
        ],
        "version_id": "11111111-1111-4111-8111-111111111111",
        "template_code": "LOCAL-GREEN-BRACELET",
        "version": 3,
        "previous_version_id": "22222222-2222-4222-8222-222222222222",
        "rollback_target_version_id": None,
        "change_source": "trainer_confirmed",
        "material_evidence": "trainer_confirmed",
        "match_method": None,
        "matched_from_template_code": None,
        "authenticity_verification": "not_verified",
        "origin_verification": "not_verified",
        "quality_verification": "not_verified",
        "warehouse_verification": "pending_warehouse_verification",
        "training_state": "label_confirmed_model_not_trained",
        "created_at": "2026-08-30T08:00:00Z",
        "can_rollback": True,
    }


def run_checker(data_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "LOCAL-GREEN-BRACELET",
            "--data-root",
            str(data_root),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_record(data_root: Path, record: dict[str, object]) -> None:
    data_root.mkdir()
    (data_root / "training-labels.jsonl").write_text("", encoding="utf-8")
    (data_root / "training-label-versions.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_checker_outputs_local_only_release_decision(tmp_path: Path) -> None:
    data_root = tmp_path / "local-data"
    write_record(data_root, complete_training_version())

    result = run_checker(data_root)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "allowed_for_local_pilot"
    assert payload["reason_codes"] == []
    assert payload["local_synthetic_only"] is True
    assert payload["real_customer_data_allowed"] is False
    assert payload["external_actions_allowed"] is False


def test_checker_returns_input_error_when_template_is_missing(tmp_path: Path) -> None:
    data_root = tmp_path / "local-data"
    data_root.mkdir()

    result = run_checker(data_root)

    assert result.returncode == 2
    assert json.loads(result.stdout) == {"error_code": "training_result_not_found"}


def test_checker_rejects_unsigned_price_range_from_local_pilot(tmp_path: Path) -> None:
    data_root = tmp_path / "unsigned-price"
    record = complete_training_version()
    record["price_confirmation"] = None
    write_record(data_root, record)

    result = run_checker(data_root)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["decision"] == "needs_human_input"
    assert payload["reason_codes"] == ["price_confirmation_required"]


def test_checker_rejects_legacy_price_audit_without_anchor_confirmation(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "legacy-price-audit"
    record = complete_training_version()
    price_confirmation = dict(record["price_confirmation"])
    price_confirmation["anchor_confirmed_by"] = None
    price_confirmation["anchor_confirmed_at"] = None
    record["price_confirmation"] = price_confirmation
    write_record(data_root, record)

    result = run_checker(data_root)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["decision"] == "needs_human_input"
    assert payload["reason_codes"] == ["price_confirmation_required"]


def test_checker_uses_distinct_nonzero_exits_for_review_and_blocking(
    tmp_path: Path,
) -> None:
    needs_review_root = tmp_path / "needs-review"
    needs_review = complete_training_version()
    needs_review["specification_note"] = ""
    write_record(needs_review_root, needs_review)

    blocked_root = tmp_path / "blocked"
    blocked = complete_training_version()
    blocked["selling_points"] = ["官方认证正品并保证升值"]
    write_record(blocked_root, blocked)

    review_result = run_checker(needs_review_root)
    blocked_result = run_checker(blocked_root)

    assert review_result.returncode == 1
    assert json.loads(review_result.stdout)["decision"] == "needs_human_input"
    assert blocked_result.returncode == 3
    assert json.loads(blocked_result.stdout)["decision"] == "blocked"
