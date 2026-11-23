import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "validate_album_handoff_sample.py"
SYNTHETIC_SAMPLE = ROOT / "fixtures" / "v0.1" / "album-handoff-compatible.json"


def valid_payload() -> dict[str, object]:
    tenant_id = "22222222-2222-4222-8222-222222222222"
    return {
        "schema_version": 1,
        "event_id": "11111111-1111-4111-8111-111111111111",
        "occurred_at": "2026-08-30T08:00:00Z",
        "tenant_id": tenant_id,
        "source_task_id": "33333333-3333-4333-8333-333333333333",
        "product_code": "SYNTHETIC-001",
        "category": "南红",
        "form": "手串",
        "classification_confidence": 0.8,
        "images": [
            {
                "asset_id": "44444444-4444-4444-8444-444444444444",
                "object_key": f"tenants/{tenant_id}/products/SYNTHETIC-001/main.png",
                "sha256": "a" * 64,
                "mime_type": "image/png",
                "kind": "original",
                "role": "main_candidate",
                "quality_confidence": 0.9,
            }
        ],
    }


def run_validator(tmp_path: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(sample), "--confirm-synthetic"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_compatible_sample_returns_contract_identity(tmp_path: Path) -> None:
    result = run_validator(tmp_path, valid_payload())

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "compatible"
    assert report["contract"] == "ProductImageSetReadyV1"
    assert report["schema_version"] == 1
    assert report["issue_codes"] == []
    assert report["synthetic_only"] is True


def test_validator_requires_explicit_synthetic_confirmation(tmp_path: Path) -> None:
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(valid_payload(), ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(sample)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "rejected"
    assert report["issue_codes"] == ["synthetic_confirmation_required"]
    assert report["issues"][0]["action_zh"] == "请确认文件只包含脱敏合成元数据后再运行。"
    assert report["synthetic_only"] is False


def test_unreadable_sample_returns_rejected_report_with_chinese_action(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "broken.json"
    sample.write_text("{not-json", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(sample), "--confirm-synthetic"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["status"] == "rejected"
    assert report["issue_codes"] == ["sample_unreadable"]
    assert report["issues"][0]["action_zh"] == "请提供可读取且格式正确的 UTF-8 JSON 文件。"


def test_committed_synthetic_sample_is_compatible() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(SYNTHETIC_SAMPLE), "--confirm-synthetic"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "compatible"


def test_missing_field_returns_stable_field_path_and_chinese_action(tmp_path: Path) -> None:
    payload = valid_payload()
    del payload["source_task_id"]

    result = run_validator(tmp_path, payload)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "rejected"
    assert report["issue_codes"] == ["contract_validation_failed"]
    assert report["issues"][0]["field"] == "source_task_id"
    assert report["issues"][0]["action_zh"] == "请按版本 1 输入规格补齐或修正该字段。"


def test_foreign_tenant_path_and_duplicate_asset_ids_are_rejected(tmp_path: Path) -> None:
    payload = valid_payload()
    first = payload["images"][0]  # type: ignore[index]
    first["object_key"] = "tenants/other/products/leak.png"  # type: ignore[index]
    payload["images"] = [first, dict(first)]  # type: ignore[arg-type]

    result = run_validator(tmp_path, payload)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "rejected"
    assert report["issue_codes"] == [
        "duplicate_asset_id",
        "duplicate_object_key",
        "object_outside_tenant_prefix",
    ]


def test_extra_top_level_and_image_fields_are_rejected(tmp_path: Path) -> None:
    payload = valid_payload()
    payload["customer_name"] = "不应跨项目传输"
    payload["images"][0]["private_album_name"] = "不应跨项目传输"  # type: ignore[index]

    result = run_validator(tmp_path, payload)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "rejected"
    assert report["issue_codes"] == ["contract_validation_failed"]
    assert {issue["field"] for issue in report["issues"]} == {
        "customer_name",
        "images.0.private_album_name",
    }
