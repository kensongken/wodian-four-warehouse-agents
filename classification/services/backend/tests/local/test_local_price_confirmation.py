from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from product_engine.api.local_app import create_local_app
from product_engine.services.training_label_versions import (
    ServiceRuleInput,
    TrainingLabelVersion,
)


def seeded_client(tmp_path) -> TestClient:
    data_root = tmp_path / "local-data"
    data_root.mkdir(parents=True)
    record = TrainingLabelVersion(
        sha256="d" * 64,
        main_image_template_code="LOCAL-GREEN-BRACELET",
        product_type="first_sale_bead",
        inventory_status="in_stock",
        material_label="绿松",
        product_form="手串",
        visual_observations=["绿色调明显"],
        selling_points=["近景实拍展示珠体轮廓"],
        specification_note="珠径 5 mm；尺寸由训练员测量",
        quality_note="图片可见颜色分布较均匀，仅为观察记录",
        defect_note="图片可见轻微表面纹理",
        price_range=None,
        service_rules=[
            ServiceRuleInput(
                service_code="scan_identity",
                eligibility="购买后可用",
                customer_summary="购买后可扫码进入身份与服务页面",
            )
        ],
        version_id=uuid4(),
        template_code="LOCAL-GREEN-BRACELET",
        version=3,
        previous_version_id=uuid4(),
        rollback_target_version_id=None,
        change_source="trainer_confirmed",
        material_evidence="trainer_confirmed",
        created_at=datetime(2026, 8, 30, 15, 0, tzinfo=UTC),
        can_rollback=True,
    )
    (data_root / "training-labels.jsonl").write_text(
        record.model_dump_json() + "\n",
        encoding="utf-8",
    )
    return TestClient(create_local_app(data_root))


def confirmation_payload() -> dict[str, object]:
    return {
        "base_version": 3,
        "standard_code": "PRICE-TURQUOISE-BRACELET-001",
        "anchor_version": 1,
        "minimum": "1200",
        "maximum": "1500",
        "basis": "训练员录入的同品类同形态价格标准",
        "anchor_confirmed_by": "synthetic-trainer-001",
        "confirmed_by": "synthetic-trainer-002",
    }


def test_local_confirmation_endpoint_appends_a_blocked_human_version(tmp_path) -> None:
    client = seeded_client(tmp_path)

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=confirmation_payload(),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["publication_status"] == "blocked"
    assert payload["automatic_publication_allowed"] is False
    assert payload["result"]["version"] == 4
    assert payload["result"]["price_range"] == {
        "currency": "CNY",
        "minimum": "1200.00",
        "maximum": "1500.00",
        "basis": "训练员录入的同品类同形态价格标准",
    }
    assert payload["result"]["price_confirmation"]["standard_code"] == (
        "PRICE-TURQUOISE-BRACELET-001"
    )
    assert payload["result"]["price_confirmation"]["confirmed_by"] == ("synthetic-trainer-002")
    assert payload["result"]["price_confirmation"]["anchor_confirmed_by"] == (
        "synthetic-trainer-001"
    )
    assert payload["result"]["price_confirmation"]["anchor_confirmed_at"]
    package = payload["content_package"]
    assert package["contract_version"] == "0.1-internal-confirmed"
    assert package["source_version"] == 4
    assert package["price_review_status"] == "human_confirmed"
    assert package["price_confirmation"]["confirmed_by"] == "synthetic-trainer-002"
    assert "price_range_missing" not in [item["code"] for item in package["draft"]["blockers"]]
    assert package["publication_status"] == "blocked"
    assert package["automatic_publication_allowed"] is False


def test_local_confirmation_rejects_stale_page_without_another_append(tmp_path) -> None:
    client = seeded_client(tmp_path)
    first = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=confirmation_payload(),
    )

    stale = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=confirmation_payload(),
    )
    current = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET").json()["result"]

    assert first.status_code == 201
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "version_conflict"
    assert current["version"] == 4


def test_local_confirmation_rejects_invalid_range_without_writing(tmp_path) -> None:
    client = seeded_client(tmp_path)
    invalid = {**confirmation_payload(), "minimum": "1500", "maximum": "1500"}

    response = client.post(
        "/local/api/training-results/LOCAL-GREEN-BRACELET/confirm-price-candidate",
        json=invalid,
    )
    current = client.get("/local/api/training-results/LOCAL-GREEN-BRACELET").json()["result"]

    assert response.status_code == 422
    assert response.json()["error_code"] == "invalid_price_anchor"
    assert current["version"] == 3


def test_local_page_exposes_manual_price_confirmation_without_publish_language(tmp_path) -> None:
    response = seeded_client(tmp_path).get("/local/templates")

    assert response.status_code == 200
    assert 'id="price-confirmation-panel"' in response.text
    assert 'id="confirm-price-candidate"' in response.text
    assert 'data-price-field="standard_code"' in response.text
    assert 'data-price-field="confirmed_by"' in response.text
    assert "确认候选价格并生成新版本" in response.text
    assert "不会自动发布" in response.text
    assert "内部资料包状态：价格已人工确认，仍禁止自动发布" in response.text
