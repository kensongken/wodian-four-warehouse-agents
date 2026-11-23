import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "check_local_mall_classification.py"


def confirmed_training_version() -> dict[str, object]:
    return {
        "sha256": "a" * 64,
        "product_type": None,
        "inventory_status": None,
        "material_label": "绿松",
        "product_form": "手串",
        "visual_observations": ["绿色调明显", "珠体排列成环"],
        "selling_points": ["近景实拍展示珠体轮廓"],
        "specification_note": "",
        "quality_note": "",
        "defect_note": "",
        "price_range": None,
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
        "created_at": "2026-08-17T08:44:04Z",
        "can_rollback": True,
    }


def test_checker_outputs_only_confirmed_classification_and_publication_boundary(
    tmp_path,
) -> None:
    data_root = tmp_path / "local-data"
    data_root.mkdir()
    (data_root / "training-labels.jsonl").write_text("", encoding="utf-8")
    (data_root / "training-label-versions.jsonl").write_text(
        json.dumps(confirmed_training_version(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
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

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["template_code"] == "LOCAL-GREEN-BRACELET"
    assert payload["source_version"] == 3
    assert payload["material_classification"]["category_code"] == "turquoise"
    assert payload["material_classification"]["category_label"] == "绿松石"
    assert payload["material_classification"]["decision_status"] == "human_confirmed"
    assert payload["classification_ready_for_mall"] is True
    assert payload["product_publication_allowed"] is False
    assert "visual_observations" not in payload
    assert "selling_points" not in payload
